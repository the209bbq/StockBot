#!/usr/bin/env python3
"""Weekly summary: portfolio vs the same-mix buy-and-hold benchmark."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta

from config import load_config
from store import Store


def _ret(start: float, end: float) -> float | None:
    if start is None or end is None or start == 0:
        return None
    return end / start - 1.0


def _closest_on_or_before(rows: list, target: str):
    eligible = [r for r in rows if r.as_of <= target]
    return eligible[-1] if eligible else None


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

    lines = [
        f"StockBot weekly summary  ({inception} → {latest.as_of})",
        "",
        "Since inception",
    ]
    p_inc = _ret(first.equity, latest.equity)
    lines.append(f"  Portfolio:           {_fmt(p_inc)}   (${first.equity:,.2f} → ${latest.equity:,.2f})")
    if first_b and latest_b:
        b_inc = _ret(first_b.equity, latest_b.equity)
        lines.append(f"  Buy & hold mix:      {_fmt(b_inc)}   (${first_b.equity:,.2f} → ${latest_b.equity:,.2f})")
        if p_inc is not None and b_inc is not None:
            lines.append(f"  Excess vs benchmark: {(p_inc - b_inc) * 100:+.2f} pp")
    else:
        lines.append("  Buy & hold mix:      n/a (benchmark not initialized)")

    lines += ["", "Last week"]
    if week_p and week_p.as_of != latest.as_of:
        p_w = _ret(week_p.equity, latest.equity)
        lines.append(f"  Portfolio:           {_fmt(p_w)}   ({week_p.as_of} ${week_p.equity:,.2f} → ${latest.equity:,.2f})")
        if week_b and latest_b:
            b_w = _ret(week_b.equity, latest_b.equity)
            lines.append(f"  Buy & hold mix:      {_fmt(b_w)}")
            if p_w is not None and b_w is not None:
                lines.append(f"  Excess vs benchmark: {(p_w - b_w) * 100:+.2f} pp")
    else:
        lines.append("  Not enough snapshots yet for a 7-day window.")
    return "\n".join(lines)


def _fmt(r: float | None) -> str:
    if r is None:
        return "n/a"
    return f"{r * 100:+.2f}%"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Print portfolio vs buy-and-hold benchmark.")
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
