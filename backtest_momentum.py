#!/usr/bin/env python3
"""QQQ close-to-close Donchian breakout backtest (round-one 'tuned' rule).

Reproduces the paper rule:
  buy when today's close > max of the prior 20 closes
  sell when close <= high-since-entry * (1 - 5%), or after 20 trading days
  2.5 bps slippage per side, $100 start, QQQ 2005-01-03 through 2026-09-30

One decision per bar (no same-day sell-then-rebuy), matching the live bot.

Usage:
    python backtest_momentum.py
    python backtest_momentum.py --offline --cache-dir tests/fixtures/data_cache
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass

import pandas as pd

from backtest import fetch
from momentum import OpenPosition, decide
from config import QQQMomentumConfig


@dataclass
class MomentumBacktestStats:
    start: str
    end: str
    start_capital: float
    final_value: float
    cagr: float
    max_drawdown: float
    trades: int
    wins: int
    win_rate: float
    hold_final: float
    hold_cagr: float
    slip_bps: float
    lookback: int
    trail_pct: float
    max_hold_days: int
    price_source: str


def _years(start: pd.Timestamp, end: pd.Timestamp) -> float:
    return max((end - start).days / 365.25, 1e-9)


def _cagr(start_value: float, end_value: float, years: float) -> float:
    if start_value <= 0 or end_value <= 0:
        return float("nan")
    return (end_value / start_value) ** (1.0 / years) - 1.0


def _max_dd(equity: pd.Series) -> float:
    peak = equity.cummax()
    dd = equity / peak - 1.0
    return float(dd.min()) if len(dd) else 0.0


def simulate_momentum(
    closes: pd.Series,
    *,
    lookback: int = 20,
    trail_pct: float = 0.05,
    max_hold_days: int = 20,
    slip_bps: float = 2.5,
    start_capital: float = 100.0,
) -> tuple[pd.Series, list[dict], MomentumBacktestStats]:
    """Walk forward one trading day at a time. Fills at that day's close ± slip."""
    closes = closes.dropna().astype(float)
    closes.index = pd.to_datetime(closes.index)
    cfg = QQQMomentumConfig(
        symbol=str(closes.name or "QQQ"),
        lookback_days=lookback,
        trail_pct=trail_pct,
        max_hold_days=max_hold_days,
        slippage_bps=slip_bps,
    )
    calendar = [ts.date() for ts in closes.index]
    slip = slip_bps / 10_000.0
    cash = float(start_capital)
    position: OpenPosition | None = None
    equity_rows: list[tuple[pd.Timestamp, float]] = []
    trades: list[dict] = []
    realized = 0.0

    for i, (ts, close) in enumerate(closes.items()):
        as_of = ts.date()
        window = closes.iloc[: i + 1]
        tradable = cash if position is None else 0.0
        decision = decide(
            closes=window,
            as_of=as_of,
            position=position,
            tradable=tradable,
            cfg=cfg,
            min_trade_notional=1.0,
            calendar=calendar[: i + 1],
        )
        # decide() sizes a buy off `tradable`. While long we pass mark for
        # reporting only; override so a buy (shouldn't happen) uses cash.
        if decision.action == "buy" and position is None and cash >= 1.0:
            fill = float(close) * (1.0 + slip)
            qty = cash / fill
            position = OpenPosition(
                symbol=cfg.symbol,
                qty=qty,
                entry_date=as_of.isoformat(),
                entry_price=fill,
                high_close=float(close),
            )
            cash = 0.0
        elif decision.action in {"sell_trail", "sell_time"} and position is not None:
            fill = float(close) * (1.0 - slip)
            proceeds = position.qty * fill
            pnl = proceeds - (position.qty * position.entry_price)
            realized += pnl
            trades.append(
                {
                    "entry_date": position.entry_date,
                    "exit_date": as_of.isoformat(),
                    "entry_price": position.entry_price,
                    "exit_price": fill,
                    "qty": position.qty,
                    "pnl": pnl,
                    "reason": decision.reason,
                }
            )
            cash = proceeds
            position = None
        elif position is not None:
            position = OpenPosition(
                symbol=position.symbol,
                qty=position.qty,
                entry_date=position.entry_date,
                entry_price=position.entry_price,
                high_close=max(position.high_close, float(close)),
            )

        mark = cash if position is None else cash + position.qty * float(close)
        equity_rows.append((ts, mark))

    eq = pd.Series({ts: val for ts, val in equity_rows}, name="equity")
    first, last = eq.index[0], eq.index[-1]
    years = _years(first, last)
    hold_qty = start_capital / float(closes.iloc[0])
    hold_final = hold_qty * float(closes.iloc[-1])
    wins = sum(1 for t in trades if t["pnl"] > 0)
    stats = MomentumBacktestStats(
        start=first.date().isoformat(),
        end=last.date().isoformat(),
        start_capital=start_capital,
        final_value=float(eq.iloc[-1]),
        cagr=_cagr(start_capital, float(eq.iloc[-1]), years),
        max_drawdown=_max_dd(eq),
        trades=len(trades),
        wins=wins,
        win_rate=(wins / len(trades)) if trades else 0.0,
        hold_final=hold_final,
        hold_cagr=_cagr(start_capital, hold_final, years),
        slip_bps=slip_bps,
        lookback=lookback,
        trail_pct=trail_pct,
        max_hold_days=max_hold_days,
        price_source="cache_or_yfinance",
    )
    return eq, trades, stats


def load_qqq(cache_dir: str, refresh: bool = False) -> tuple[pd.Series, str]:
    path = os.path.join(cache_dir, "QQQ.csv")
    if os.path.exists(path) and not refresh:
        return fetch("QQQ", cache_dir, refresh=False), "cache"
    series = fetch("QQQ", cache_dir, refresh=refresh)
    return series, "yfinance" if refresh or not os.path.exists(path) else "cache"


def run_tuned(
    *,
    cache_dir: str = "tests/fixtures/data_cache",
    refresh: bool = False,
    offline: bool = False,
    start: str = "2005-01-03",
    end: str = "2026-09-30",
    start_capital: float = 100.0,
    slip_bps: float = 2.5,
) -> tuple[pd.Series, list[dict], MomentumBacktestStats]:
    if offline:
        path = os.path.join(cache_dir, "QQQ.csv")
        if not os.path.exists(path):
            raise FileNotFoundError(f"offline: missing {path}")
    closes, source = load_qqq(cache_dir, refresh=refresh and not offline)
    closes = closes.loc[start:end]
    if closes.empty:
        raise RuntimeError(f"no QQQ closes in {start}..{end}")
    eq, trades, stats = simulate_momentum(
        closes,
        lookback=20,
        trail_pct=0.05,
        max_hold_days=20,
        slip_bps=slip_bps,
        start_capital=start_capital,
    )
    stats.price_source = source
    stats.start = pd.Timestamp(closes.index[0]).date().isoformat()
    stats.end = pd.Timestamp(closes.index[-1]).date().isoformat()
    return eq, trades, stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="QQQ momentum backtest (20d high / 5% trail / 20d time).")
    p.add_argument("--cache-dir", default="tests/fixtures/data_cache")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--start", default="2005-01-03")
    p.add_argument("--end", default="2026-09-30")
    p.add_argument("--start-capital", type=float, default=100.0)
    p.add_argument("--slip-bps", type=float, default=2.5)
    p.add_argument("--out", default=None, help="Optional JSON path for stats")
    args = p.parse_args(argv)

    _, trades, stats = run_tuned(
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        offline=args.offline,
        start=args.start,
        end=args.end,
        start_capital=args.start_capital,
        slip_bps=args.slip_bps,
    )
    payload = asdict(stats)
    payload["target_note"] = (
        "Round-one tuned ballpark was ~8.9% CAGR / $100 → ~$639. "
        "This run reports the engine's actual number."
    )
    print(json.dumps(payload, indent=2))
    print(
        f"\n{stats.trades} trades, win rate {stats.win_rate:.1%}, "
        f"final ${stats.final_value:.2f}, CAGR {stats.cagr:.2%}, "
        f"maxDD {stats.max_drawdown:.2%}"
    )
    print(
        f"Buy & hold QQQ: ${stats.hold_final:.2f}, CAGR {stats.hold_cagr:.2%} "
        f"(price source: {stats.price_source})"
    )
    if args.out:
        with open(args.out, "w") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
