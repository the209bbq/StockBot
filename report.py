#!/usr/bin/env python3
"""Weekly summary: bot sleeve vs the virtual buy-and-hold benchmark."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from config import load_config
from store import Store


def _ret(start: float, end: float) -> float | None:
    if start is None or end is None or start == 0:
        return None
    return end / start - 1.0


def _closest_on_or_before(rows: list, target: str):
    eligible = [r for r in rows if r.as_of <= target]
    return eligible[-1] if eligible else None


def _hold_label(store: Store) -> str:
    raw = store.get_state("benchmark_lots") or ""
    if "QQQ" in raw and raw.count(":") <= 2:
        return "Buy & hold QQQ:"
    return "Buy & hold mix:"


def summarize(store: Store, *, as_of: str | None = None) -> str:
    port = store.equity_history()
    bench = store.benchmark_history()
    if not port:
        return "No equity snapshots yet. Run `python run.py --dry-run` first."

    end_day = as_of or port[-1].as_of
    latest = _closest_on_or_before(port, end_day) or port[-1]
    first = port[0]
    inception = store.inception_date() or first.as_of

    latest_b = _closest_on_or_before(bench, latest.as_of)
    first_b = bench[0] if bench else None

    week_target = (
        datetime.fromisoformat(latest.as_of) - timedelta(days=7)
    ).date().isoformat()
    week_p = _closest_on_or_before(port, week_target)
    week_b = _closest_on_or_before(bench, week_target) if bench else None
    hold = _hold_label(store)
    stats = store.trade_stats()

    lines = [
        f"StockBot weekly summary  ({inception} → {latest.as_of})",
        "",
        "Since inception",
    ]
    p_inc = _ret(first.equity, latest.equity)
    lines.append(f"  Bot sleeve:          {_fmt(p_inc)}   (${first.equity:,.2f} → ${latest.equity:,.2f})")
    # Keep the historical "Portfolio:" line so older tests still match.
    lines.append(f"  Portfolio:           {_fmt(p_inc)}   (${first.equity:,.2f} → ${latest.equity:,.2f})")
    if first_b and latest_b:
        b_inc = _ret(first_b.equity, latest_b.equity)
        pad = " " * max(1, 22 - len(hold))
        lines.append(f"  {hold}{pad}{_fmt(b_inc)}   (${first_b.equity:,.2f} → ${latest_b.equity:,.2f})")
        if p_inc is not None and b_inc is not None:
            lines.append(f"  Excess vs benchmark: {(p_inc - b_inc) * 100:+.2f} pp")
    else:
        lines.append(f"  {hold}      n/a (benchmark not initialized)")

    lines += ["", "Last week"]
    if week_p and week_p.as_of != latest.as_of:
        p_w = _ret(week_p.equity, latest.equity)
        lines.append(f"  Portfolio:           {_fmt(p_w)}   ({week_p.as_of} ${week_p.equity:,.2f} → ${latest.equity:,.2f})")
        if week_b and latest_b:
            b_w = _ret(week_b.equity, latest_b.equity)
            lines.append(f"  {hold} {_fmt(b_w)}")
            if p_w is not None and b_w is not None:
                lines.append(f"  Excess vs benchmark: {(p_w - b_w) * 100:+.2f} pp")
    else:
        lines.append("  Not enough snapshots yet for a 7-day window.")

    lines += ["", "Trades"]
    lines.append(f"  Closed trades:       {stats['trades']}")
    lines.append(f"  Wins:                {stats['wins']}")
    lines.append(f"  Win rate:            {stats['win_rate'] * 100:.1f}%")
    lines.append(f"  Realized P&L:        ${float(stats['realized_pnl']):,.2f}")
    return "\n".join(lines)


def _fmt(r: float | None) -> str:
    if r is None:
        return "n/a"
    return f"{r * 100:+.2f}%"


def _return_pair(store: Store, *, as_of: str | None = None) -> dict[str, Any]:
    port = store.equity_history()
    bench = store.benchmark_history()
    empty = {"bot": None, "hold": None, "bot_start": None, "bot_end": None, "hold_start": None, "hold_end": None}
    if not port:
        return {"since_inception": empty, "last_7_days": empty, "inception": None, "as_of": as_of}
    end_day = as_of or port[-1].as_of
    latest = _closest_on_or_before(port, end_day) or port[-1]
    first = port[0]
    latest_b = _closest_on_or_before(bench, latest.as_of)
    first_b = bench[0] if bench else None
    week_target = (datetime.fromisoformat(latest.as_of) - timedelta(days=7)).date().isoformat()
    week_p = _closest_on_or_before(port, week_target)
    week_b = _closest_on_or_before(bench, week_target) if bench else None
    return {
        "inception": store.inception_date() or first.as_of,
        "as_of": latest.as_of,
        "since_inception": {
            "bot": _ret(first.equity, latest.equity),
            "hold": _ret(first_b.equity, latest_b.equity) if first_b and latest_b else None,
            "bot_start": first.equity,
            "bot_end": latest.equity,
            "hold_start": first_b.equity if first_b else None,
            "hold_end": latest_b.equity if latest_b else None,
        },
        "last_7_days": {
            "bot": _ret(week_p.equity, latest.equity) if week_p and week_p.as_of != latest.as_of else None,
            "hold": _ret(week_b.equity, latest_b.equity) if week_b and latest_b and week_p and week_p.as_of != latest.as_of else None,
            "bot_start": week_p.equity if week_p and week_p.as_of != latest.as_of else None,
            "bot_end": latest.equity,
            "hold_start": week_b.equity if week_b and week_p and week_p.as_of != latest.as_of else None,
            "hold_end": latest_b.equity if latest_b else None,
        },
    }


def build_latest(store: Store, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """JSON payload written to the bot-state branch for GitHub API readers."""
    result = result or {}
    managed = dict(result.get("managed") or {})
    pos = store.get_open_position()
    close = None
    signal = result.get("signal") or {}
    if signal.get("close") is not None:
        close = float(signal["close"])
    unrealized = 0.0
    open_rec = None
    if pos is not None:
        mark_px = close or pos.high_close
        unrealized = (mark_px - pos.entry_price) * pos.qty
        open_rec = {
            "symbol": pos.symbol,
            "qty": pos.qty,
            "entry_date": pos.entry_date,
            "entry_price": pos.entry_price,
            "high_close": pos.high_close,
            "mark": mark_px,
            "unrealized_pnl": round(unrealized, 2),
        }
    bench_hist = store.benchmark_history()
    bench_val = bench_hist[-1].equity if bench_hist else None
    returns = _return_pair(store, as_of=result.get("as_of"))
    stats = store.trade_stats()
    return {
        "as_of": result.get("as_of"),
        "dry_run": bool(result.get("dry_run", False)),
        "action": result.get("action"),
        "reason": result.get("reason") or signal.get("reason"),
        "orders_submitted": bool(result.get("orders_submitted", False)),
        "live_started": store.is_live_started(),
        "signal": signal,
        "session": result.get("session"),
        "managed": {
            **managed,
            "unrealized_pnl": round(unrealized, 2),
        },
        "open_position": open_rec,
        "trades": store.closed_trades(),
        "trade_stats": stats,
        "benchmark": {
            "start": store.inception_date(),
            "value": bench_val,
            "lots": None,
        },
        "returns": returns,
        "price_source": result.get("price_source"),
        "reconcile_mismatches": result.get("reconcile_mismatches") or [],
    }


def format_weekly_markdown(store: Store, result: dict[str, Any] | None = None) -> str:
    result = result or {}
    latest = build_latest(store, result)
    signal = latest.get("signal") or {}
    lines = [summarize(store, as_of=result.get("as_of")), "", "Last signal"]
    if signal:
        lines.append(f"  Action:              {signal.get('action')} ({signal.get('reason')})")
        close = signal.get("close")
        high = signal.get("prior_20d_high")
        lines.append(f"  Close vs 20d high:   {close} vs {high}")
        lines.append(f"  Breakout:            {signal.get('breakout')}")
    else:
        lines.append(f"  Action:              {latest.get('action')} ({latest.get('reason')})")
    pos = latest.get("open_position")
    lines += ["", "Open position"]
    if pos:
        lines.append(
            f"  {pos['symbol']} qty={pos['qty']:.6f} entry {pos['entry_date']} "
            f"@ {pos['entry_price']:.4f}  uPnL ${pos['unrealized_pnl']:.2f}"
        )
    else:
        lines.append("  Flat")
    lines += ["", "Fetch"]
    lines.append("  reports/latest.json and reports/weekly.md on the bot-state branch")
    return "\n".join(lines)


def write_state_reports(
    store: Store,
    result: dict[str, Any] | None = None,
    dest: str | Path = "reports",
) -> dict[str, Path]:
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)
    payload = build_latest(store, result)
    json_path = dest_path / "latest.json"
    md_path = dest_path / "weekly.md"
    json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    md_path.write_text(format_weekly_markdown(store, result) + "\n")
    return {"latest_json": json_path, "weekly_md": md_path}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Print bot sleeve vs buy-and-hold benchmark.")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--db", default=None, help="Override SQLite path")
    p.add_argument("--as-of", default=None)
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    db = args.db or cfg.db_path
    with Store(db) as store:
        print(summarize(store, as_of=args.as_of))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
