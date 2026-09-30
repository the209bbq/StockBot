"""Daily QQQ momentum runner (paper-only, dry-run by default)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from broker_alpaca import PAPER_BASE_URL, make_client_order_id
from config import Config
from market_clock import evaluate_session, now_et, session_close_et
from momentum import OpenPosition, decide, sleeve_mark, tradable_cash
from notifier import Notifier
from rebalance import Position
from report import write_state_reports
from risk import RiskError, check_momentum_risk, kill_switch_active, scaled_order_cap
from store import Store, today_iso


def _print(payload: dict[str, Any]) -> None:
    banned = {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "api_key", "secret_key"}
    print(json.dumps({k: v for k, v in payload.items() if k not in banned}, indent=2, default=str))


def _yfinance_closes(symbol: str, limit: int = 80) -> pd.Series:
    import yfinance as yf

    df = yf.download(symbol, period="1y", auto_adjust=True, progress=False)
    if df is None or df.empty:
        return pd.Series(dtype=float, name=symbol)
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.dropna()
    s.name = symbol
    return s.tail(limit)


def load_closes(broker: Any, symbol: str, lookback: int) -> tuple[pd.Series, str]:
    series = broker.get_daily_closes(symbol, limit=lookback + 15)
    if series is not None and len(series) >= lookback + 1:
        return series, "alpaca"
    yf = _yfinance_closes(symbol, lookback + 15)
    return yf, "yfinance"


def _calendar_days(broker: Any, as_of: date) -> tuple[list[date], Any | None]:
    start = as_of - timedelta(days=90)
    try:
        rows = broker.get_calendar(start, as_of)
    except Exception:
        return [], None
    days: list[date] = []
    today_row = None
    for row in rows:
        raw = getattr(row, "date", None) or getattr(row, "calendar_date", None)
        if raw is None:
            continue
        if isinstance(raw, datetime):
            d = raw.date()
        elif isinstance(raw, date):
            d = raw
        else:
            d = date.fromisoformat(str(raw)[:10])
        days.append(d)
        if d == as_of:
            today_row = row
    return days, today_row


def _reconcile_position(
    *,
    symbol: str,
    day: str,
    close: float | None,
    qqq: Position | None,
    stored: OpenPosition | None,
    live_started: bool,
) -> tuple[OpenPosition | None, list[str]]:
    """Broker qty wins. Ignore leftover account QQQ until this sleeve has gone live."""
    notes: list[str] = []
    broker_long = qqq is not None and qqq.qty > 0
    if not live_started:
        if broker_long:
            notes.append("ignoring broker QQQ; sleeve has not started live paper trading")
        return None, notes
    if stored and not broker_long:
        notes.append("broker flat vs stored long; using broker (flat)")
        return None, notes
    if broker_long and stored is None:
        notes.append("recovering sleeve position from broker (missing stored row)")
        px = qqq.current_price or close or 0.0
        return OpenPosition(symbol, qqq.qty, day, px, px), notes
    if broker_long and stored is not None:
        if abs(qqq.qty - stored.qty) > 1e-4:
            notes.append(f"qty mismatch stored {stored.qty} vs broker {qqq.qty}; using broker")
        return OpenPosition(
            symbol=stored.symbol,
            qty=qqq.qty,
            entry_date=stored.entry_date,
            entry_price=stored.entry_price,
            high_close=stored.high_close,
        ), notes
    return None, notes


def _finish(store: Store, result: dict[str, Any], reports_dir: str | Path) -> dict[str, Any]:
    write_state_reports(store, result, reports_dir)
    _print(result)
    return result


def run_momentum(
    cfg: Config,
    broker: Any,
    store: Store,
    notifier: Notifier,
    *,
    dry_run: bool = True,
    force: bool = False,
    as_of: str | None = None,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
    reports_dir: str | Path = "reports",
) -> dict[str, Any]:
    environ = env if env is not None else {}
    mom = cfg.qqq_momentum
    clock_now = now or now_et()
    day = today_iso(as_of) if as_of else now_et(clock_now).date().isoformat()
    check_date = date.fromisoformat(day)
    result: dict[str, Any] = {
        "strategy": "qqq_momentum",
        "as_of": day,
        "dry_run": dry_run,
        "orders_submitted": False,
        "orders": [],
        "base_url": PAPER_BASE_URL,
        "reconcile_mismatches": [],
    }

    if kill_switch_active(cfg.kill_switch_path, env=environ):
        result.update({"action": "blocked_kill_switch", "error": "kill switch is active"})
        notifier.notify("kill_switch", result["error"])
        return _finish(store, result, reports_dir)

    try:
        account = broker.get_account()
        positions = broker.get_positions()
    except Exception as exc:
        result.update({"auth": "failed", "error_type": type(exc).__name__, "error": str(exc)})
        _finish(store, result, reports_dir)
        raise
    result["auth"] = "ok"
    result["account"] = {
        "status": account.status,
        "equity": round(account.equity, 2),
        "cash": round(account.cash, 2),
    }

    cal_days, cal_row = _calendar_days(broker, check_date)
    clock = {}
    try:
        clock = broker.get_clock()
    except Exception:
        clock = {}
    session = evaluate_session(
        as_of=check_date,
        evaluate_et=mom.evaluate_et,
        window_minutes=mom.evaluate_window_minutes,
        skip_early_closes=mom.skip_early_closes,
        now=clock_now,
        is_open=clock.get("is_open"),
        calendar_close=session_close_et(clock.get("next_close")) if clock.get("next_close") else None,
        calendar_row=cal_row,
        calendar_available=bool(cal_days),
        force=force,
    )
    result["session"] = session.__dict__

    closes, source = load_closes(broker, mom.symbol, mom.lookback_days)
    result["price_source"] = source
    close = float(closes.iloc[-1]) if closes is not None and not closes.empty else None

    live = store.is_live_started()
    stored, mismatches = _reconcile_position(
        symbol=mom.symbol,
        day=day,
        close=close,
        qqq=positions.get(mom.symbol),
        stored=store.get_open_position() if live else None,
        live_started=live,
    )
    result["reconcile_mismatches"] = mismatches

    realized = store.get_realized_pnl() if live else 0.0
    tradable = tradable_cash(capital_cap_usd=cfg.capital_cap_usd, realized_pnl=realized)
    cash_sleeve = tradable if stored is None else 0.0
    mark = sleeve_mark(position=stored, close=close or 0.0, cash=cash_sleeve)
    result["managed"] = {
        "capital_cap_usd": cfg.capital_cap_usd,
        "realized_pnl": round(realized, 2),
        "tradable": round(tradable, 2),
        "equity": round(mark, 2),
        "in_position": stored is not None,
    }

    if session.skip_reason and not force:
        result["action"] = "skipped"
        result["reason"] = session.skip_reason
        if session.skip_reason == "early_close":
            result["early_close_note"] = (
                f"Early close at {session.next_close_et} ET; "
                f"policy={session.early_close_policy}. "
                "The 19:50/20:50 UTC crons fire at 15:50 ET, so this session is skipped."
            )
        return _finish(store, result, reports_dir)

    if not dry_run and not force and store.last_live_session() == day:
        result["action"] = "skipped"
        result["reason"] = "already_evaluated"
        return _finish(store, result, reports_dir)

    prices = {mom.symbol: close} if close else {}
    if not dry_run:
        if not store.is_live_started():
            store.reset_dry_run_live_state()
            store.mark_live_started(day)
        store.init_benchmark_if_needed(day, cfg.capital_cap_usd, prices, {mom.symbol: 1.0})
        store.mark_benchmark(day, prices)
        sleeve_pos = {}
        if stored and close:
            sleeve_pos[mom.symbol] = Position(
                mom.symbol, stored.qty, stored.qty * close, stored.entry_price, close
            )
        store.snapshot_equity(day, mark, cash_sleeve, sleeve_pos)

    decision = decide(
        closes=closes,
        as_of=check_date,
        position=stored,
        tradable=tradable,
        cfg=mom,
        min_trade_notional=cfg.min_trade_notional,
        calendar=cal_days or None,
    )
    result["signal"] = {
        "action": decision.action,
        "reason": decision.reason,
        "close": decision.close,
        "prior_20d_high": decision.prior_high,
        "trail_high": decision.trail_high,
        "days_held": decision.days_held,
        "breakout": (
            None
            if decision.close is None or decision.prior_high is None
            else decision.close > decision.prior_high
        ),
    }
    if decision.order:
        result["orders"] = [decision.order.as_dict()]

    try:
        check_momentum_risk(
            decision.order,
            tradable=tradable,
            max_order_notional=scaled_order_cap(tradable, cfg),
            max_daily_loss_pct=cfg.risk.max_daily_loss_pct,
            realized_pnl_today=store.realized_pnl_on(day) if not dry_run else 0.0,
            kill_switch_path=cfg.kill_switch_path,
            env=environ,
        )
    except RiskError as exc:
        result["action"] = "blocked_risk"
        result["error"] = str(exc)
        notifier.notify("risk", str(exc))
        if not dry_run:
            store.set_last_live_session(day)
        return _finish(store, result, reports_dir)

    if decision.order is None:
        if stored and close and not dry_run:
            store.set_open_position(
                OpenPosition(
                    symbol=stored.symbol,
                    qty=stored.qty,
                    entry_date=stored.entry_date,
                    entry_price=stored.entry_price,
                    high_close=max(stored.high_close, close),
                )
            )
        if not dry_run:
            store.set_last_live_session(day)
        result["action"] = decision.action
        return _finish(store, result, reports_dir)

    cid = make_client_order_id(day, decision.order.symbol, decision.order.side, decision.order.notional)
    if dry_run:
        store.record_order(
            client_order_id=cid,
            symbol=decision.order.symbol,
            side=decision.order.side,
            notional=decision.order.notional,
            qty=decision.order.qty,
            status="dry_run",
            dry_run=True,
            raw=decision.order.as_dict(),
        )
        result["action"] = "dry_run"
        result["orders_submitted"] = False
        return _finish(store, result, reports_dir)

    if store.live_order_exists(cid):
        result["action"] = "idempotent_skip"
        result["reason"] = "client_order_id already submitted"
        store.set_last_live_session(day)
        return _finish(store, result, reports_dir)

    existing = None
    try:
        existing = broker.get_order_by_client_id(cid)
    except Exception:
        existing = None
    rec = existing or broker.submit_order(decision.order, client_order_id=cid)
    store.record_order(
        client_order_id=cid,
        symbol=decision.order.symbol,
        side=decision.order.side,
        notional=decision.order.notional,
        qty=decision.order.qty,
        status=str(rec.get("status", "submitted")),
        dry_run=False,
        raw=rec,
    )
    fill_px = float(rec.get("filled_avg_price") or close or 0.0)
    fill_qty = float(rec.get("filled_qty") or decision.order.qty or 0.0)
    if fill_qty and fill_px:
        store.record_fill(
            client_order_id=cid,
            symbol=decision.order.symbol,
            side=decision.order.side,
            qty=fill_qty,
            price=fill_px,
        )
        notifier.notify("fill", f"{decision.order.side} {decision.order.symbol} {fill_qty} @ {fill_px}", rec)
    if decision.order.side == "buy":
        store.set_open_position(
            OpenPosition(
                symbol=mom.symbol,
                qty=fill_qty or (decision.order.qty or 0.0),
                entry_date=day,
                entry_price=fill_px or close or 0.0,
                high_close=close or fill_px or 0.0,
            )
        )
    else:
        if stored:
            pnl = (fill_px - stored.entry_price) * (fill_qty or stored.qty)
            store.add_realized_pnl(pnl, day)
            store.record_closed_trade(
                symbol=mom.symbol,
                entry_date=stored.entry_date,
                exit_date=day,
                entry_price=stored.entry_price,
                exit_price=fill_px,
                qty=fill_qty or stored.qty,
                pnl=pnl,
                reason=decision.reason,
            )
        store.clear_open_position()
    store.set_last_live_session(day)
    result["action"] = "submitted"
    result["orders_submitted"] = True
    result["submitted"] = rec
    return _finish(store, result, reports_dir)
