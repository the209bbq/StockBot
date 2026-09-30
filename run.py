#!/usr/bin/env python3
"""Paper-only ETF rebalance entrypoint.

Default is --dry-run: print the order plan and send nothing.
`--paper` talks to Alpaca paper (keys required). There is no live path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any

from dotenv import load_dotenv

from broker_alpaca import (
    LIVE_TRADING_MESSAGE,
    PAPER_BASE_URL,
    AlpacaBroker,
    LiveTradingNotApprovedError,
    MockBroker,
    assert_paper_only,
    make_client_order_id,
)
from config import Config, load_config
from notifier import Notifier, build_notifier
from rebalance import Position, plan_orders, portfolio_weights, reconcile
from risk import RiskError, check_orders, kill_switch_active
from store import Store, today_iso
from strategy import decide

load_dotenv()


class LiveModeForbidden(LiveTradingNotApprovedError):
    pass


def _reject_live_flags(args: argparse.Namespace, env: dict[str, str]) -> None:
    if getattr(args, "live", False):
        raise LiveModeForbidden(LIVE_TRADING_MESSAGE)
    if str(env.get("TRADER_LIVE", "")).lower() in {"1", "true", "yes"}:
        raise LiveModeForbidden(LIVE_TRADING_MESSAGE)
    base = env.get("APCA_API_BASE_URL") or PAPER_BASE_URL
    assert_paper_only(base)


def is_month_end(as_of: date, calendar_index: list[date] | None = None) -> bool:
    """True if `as_of` is the last date of its calendar month (or last trading day)."""
    if calendar_index:
        same = [d for d in calendar_index if d.year == as_of.year and d.month == as_of.month]
        return bool(same) and as_of == max(same)
    if as_of.month == 12:
        nxt = date(as_of.year + 1, 1, 1)
    else:
        nxt = date(as_of.year, as_of.month + 1, 1)
    from datetime import timedelta

    return as_of == (nxt - timedelta(days=1))


def _sma(closes, window: int) -> float | None:
    if closes is None or len(closes) < window:
        return None
    return float(closes.tail(window).mean())


def _yfinance_closes(symbol: str, limit: int = 250):
    """Offline-friendly fallback when the Alpaca data client has no bars."""
    import pandas as pd
    import yfinance as yf

    df = yf.download(symbol, period="2y", auto_adjust=True, progress=False)
    if df is None or df.empty:
        return pd.Series(dtype=float, name=symbol)
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.dropna()
    s.name = symbol
    return s.tail(limit)


def _print_status(payload: dict[str, Any]) -> None:
    """Structured report. Never include env or secret fields."""
    banned = {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "api_key", "secret_key", "secret"}
    clean = {k: v for k, v in payload.items() if k not in banned}
    print(json.dumps(clean, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Paper-only ETF rebalancer (dry-run by default).")
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Print orders and write planned rows; do not submit (default: true).",
    )
    p.add_argument(
        "--paper",
        action="store_true",
        help="Use the Alpaca paper account (requires APCA_API_KEY_ID / APCA_API_SECRET_KEY).",
    )
    p.add_argument(
        "--submit",
        action="store_true",
        help="Actually submit paper orders. Implies --no-dry-run. Requires --paper.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Run the check even when today is not a month-end.",
    )
    p.add_argument(
        "--as-of",
        default=None,
        help="YYYY-MM-DD to treat as the check date (testing).",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help=argparse.SUPPRESS,  # hidden; always rejected
    )
    return p


def run_rebalance(
    cfg: Config,
    broker: Any,
    store: Store,
    notifier: Notifier,
    *,
    dry_run: bool = True,
    force: bool = False,
    as_of: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    environ = env if env is not None else os.environ
    day = today_iso(as_of)
    check_date = date.fromisoformat(day)

    result: dict[str, Any] = {"as_of": day, "dry_run": dry_run, "orders": [], "action": "none"}

    if kill_switch_active(cfg.kill_switch_path, env=environ):
        msg = f"kill switch is active ({cfg.kill_switch_path} or KILL_SWITCH); blocking all orders"
        notifier.notify("kill_switch", msg)
        result["action"] = "blocked_kill_switch"
        result["error"] = msg
        result["orders_submitted"] = False
        _print_status(result)
        return result

    try:
        account = broker.get_account()
        positions = broker.get_positions()
    except Exception as exc:
        result["auth"] = "failed"
        result["base_url"] = PAPER_BASE_URL
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
        result["orders_submitted"] = False
        _print_status(result)
        raise
    result["auth"] = "ok"
    result["base_url"] = PAPER_BASE_URL
    result["account"] = {
        "status": account.status,
        "equity": round(account.equity, 2),
        "cash": round(account.cash, 2),
        "buying_power": round(account.buying_power, 2),
    }
    result["positions"] = {
        s: {
            "qty": p.qty,
            "market_value": round(p.market_value, 2),
            "price": p.current_price,
        }
        for s, p in positions.items()
    }
    stored = store.last_positions()
    mismatches = reconcile(positions, stored) if stored else []
    if mismatches:
        notifier.notify("reconcile", "Broker positions differ from last snapshot; using broker.", {"mismatches": mismatches})
        result["reconcile_mismatches"] = mismatches

    symbols = list(dict.fromkeys([*cfg.symbols, cfg.trend_filter.signal_ticker, cfg.trend_filter.risk_off_ticker]))
    prices = broker.get_latest_prices(symbols)
    for symbol, pos in positions.items():
        prices.setdefault(symbol, pos.current_price)

    weights = portfolio_weights(positions, account.cash, account.equity)
    signal_close = prices.get(cfg.trend_filter.signal_ticker)
    sma = None
    sma_source = None
    if cfg.trend_filter.enabled:
        closes = broker.get_daily_closes(cfg.trend_filter.signal_ticker, limit=cfg.trend_filter.sma_days + 20)
        sma_source = "broker"
        if closes is None or len(closes) < cfg.trend_filter.sma_days:
            closes = _yfinance_closes(cfg.trend_filter.signal_ticker, cfg.trend_filter.sma_days + 20)
            sma_source = "yfinance"
        sma = _sma(closes, cfg.trend_filter.sma_days)
        if closes is not None and not closes.empty:
            signal_close = float(closes.iloc[-1])
        result["sma_source"] = sma_source

    risk_on = store.get_risk_on(default=True)
    try:
        decision = decide(
            current_weights=weights,
            cfg=cfg,
            risk_on=risk_on,
            signal_close=signal_close if cfg.trend_filter.enabled else None,
            sma=sma if cfg.trend_filter.enabled else None,
        )
    except ValueError as exc:
        result["error"] = str(exc)
        result["orders_submitted"] = False
        result["trend"] = {
            "enabled": cfg.trend_filter.enabled,
            "signal": cfg.trend_filter.signal_ticker,
            "close": signal_close,
            "sma_200": sma,
            "source": sma_source,
        }
        _print_status(result)
        raise
    result["trend"] = {
        "enabled": cfg.trend_filter.enabled,
        "signal": cfg.trend_filter.signal_ticker,
        "close": decision.signal_close,
        "sma_200": decision.sma,
        "vs_sma": (
            None
            if decision.signal_close is None or decision.sma is None
            else ("below" if decision.signal_close < decision.sma else "above_or_equal")
        ),
        "risk_on": decision.risk_on,
        "source": sma_source,
    }
    result["decision"] = {
        "reason": decision.reason,
        "risk_on": decision.risk_on,
        "should_rebalance": decision.should_rebalance,
        "max_drift_pp": round(decision.max_drift_pp, 3),
        "target_weights": decision.target_weights,
        "current_weights": {k: round(v, 4) for k, v in weights.items()},
        "signal_close": decision.signal_close,
        "sma": decision.sma,
    }

    if decision.max_drift_pp > cfg.rebalance_band_pp:
        notifier.notify(
            "drift",
            f"Max drift {decision.max_drift_pp:.2f} pp vs band {cfg.rebalance_band_pp:.2f} pp",
            result["decision"],
        )

    store.snapshot_equity(day, account.equity, account.cash, positions)
    store.init_benchmark_if_needed(day, account.equity, prices, cfg.target_weights)
    store.mark_benchmark(day, prices)
    store.record_target(day, decision.target_weights, decision.risk_on, decision.reason)

    month_end = is_month_end(check_date)
    if not force and not month_end and not dry_run:
        result["action"] = "skipped_not_month_end"
        result["orders_submitted"] = False
        _print_status(result)
        return result

    if not decision.should_rebalance:
        result["action"] = "within_band"
        result["orders_submitted"] = False
        store.set_risk_on(decision.risk_on)
        _print_status(result)
        return result

    orders = plan_orders(
        positions=positions,
        cash=account.cash,
        equity=account.equity,
        target_weights=decision.target_weights,
        prices=prices,
        cfg=cfg,
        reason=decision.reason,
    )
    result["orders"] = [o.as_dict() for o in orders]

    try:
        check_orders(
            orders,
            equity=account.equity,
            max_order_notional=cfg.risk.max_order_notional,
            max_daily_turnover_pct=cfg.risk.max_daily_turnover_pct,
            already_traded_today=store.turnover_today(day),
            kill_switch_path=cfg.kill_switch_path,
            env=environ,
        )
    except RiskError as exc:
        notifier.notify("risk", str(exc), {"orders": result["orders"]})
        result["action"] = "blocked_risk"
        result["error"] = str(exc)
        result["orders_submitted"] = False
        _print_status(result)
        return result

    if dry_run:
        for intent in orders:
            cid = make_client_order_id(day, intent.symbol, intent.side, intent.notional)
            store.record_order(
                client_order_id=cid,
                symbol=intent.symbol,
                side=intent.side,
                notional=intent.notional,
                qty=intent.qty,
                status="dry_run",
                dry_run=True,
                raw=intent.as_dict(),
            )
        result["action"] = "dry_run"
        result["orders_submitted"] = False
        store.set_risk_on(decision.risk_on)
        _print_status(result)
        return result

    submitted = []
    for intent in orders:
        cid = make_client_order_id(day, intent.symbol, intent.side, intent.notional)
        try:
            rec = broker.submit_order(intent, client_order_id=cid)
        except Exception as exc:
            notifier.notify("error", f"submit failed for {intent.symbol}: {exc}", intent.as_dict())
            store.record_order(
                client_order_id=cid,
                symbol=intent.symbol,
                side=intent.side,
                notional=intent.notional,
                qty=intent.qty,
                status="error",
                dry_run=False,
                raw={"error": str(exc)},
            )
            raise
        store.record_order(
            client_order_id=cid,
            symbol=intent.symbol,
            side=intent.side,
            notional=intent.notional,
            qty=intent.qty,
            status=str(rec.get("status", "submitted")),
            dry_run=False,
            raw=rec,
        )
        if rec.get("filled_qty") and rec.get("filled_avg_price"):
            store.record_fill(
                client_order_id=cid,
                symbol=intent.symbol,
                side=intent.side,
                qty=float(rec["filled_qty"]),
                price=float(rec["filled_avg_price"]),
            )
            notifier.notify(
                "fill",
                f"{intent.side} {intent.symbol} qty={rec['filled_qty']} @ {rec['filled_avg_price']}",
                rec,
            )
        submitted.append(rec)
    result["submitted"] = submitted
    result["action"] = "submitted"
    result["orders_submitted"] = True
    store.set_risk_on(decision.risk_on)
    _print_status(result)
    return result


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    environ = dict(os.environ if env is None else env)
    args = build_parser().parse_args(argv)
    try:
        _reject_live_flags(args, environ)
    except LiveTradingNotApprovedError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.submit:
        args.dry_run = False
        if not args.paper:
            print("--submit requires --paper (there is no live-trading path).", file=sys.stderr)
            return 2

    if not args.dry_run and not args.paper:
        print("Refusing to submit without --paper. Live trading needs explicit owner approval.", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    try:
        assert_paper_only(cfg.paper_base_url)
    except LiveTradingNotApprovedError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    notifier = build_notifier(environ)
    store = Store(cfg.db_path)

    try:
        if args.paper:
            print("PAPER DRY-RUN" if args.dry_run else "PAPER SUBMIT")
            print(f"base_url={PAPER_BASE_URL}")
            print("orders_will_be_sent=false" if args.dry_run else "orders_will_be_sent=true")
            try:
                broker: Any = AlpacaBroker.from_env(environ)
            except Exception as exc:
                _print_status(
                    {
                        "auth": "failed",
                        "base_url": PAPER_BASE_URL,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "orders_submitted": False,
                    }
                )
                raise
        else:
            broker = MockBroker()
            print("Using in-memory mock broker (no API keys required).")
        run_rebalance(
            cfg,
            broker,
            store,
            notifier,
            dry_run=args.dry_run,
            force=args.force or args.dry_run,
            as_of=args.as_of,
            env=environ,
        )
        return 0
    except LiveTradingNotApprovedError as exc:
        notifier.notify("error", str(exc))
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        notifier.notify("error", str(exc))
        raise
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
