from __future__ import annotations

from pathlib import Path

import pandas as pd

from backtest_momentum import run_tuned, simulate_momentum
from momentum import OpenPosition, decide
from config import QQQMomentumConfig


CACHE = Path(__file__).resolve().parent / "fixtures" / "data_cache"


def test_synthetic_roundtrip_entry_trail_and_time():
    # 20 flat days, breakout, grind up, then crash through the 5% trail.
    values = [100.0] * 20 + [101.0, 102.0, 103.0, 104.0, 98.0]
    idx = pd.bdate_range("2020-01-02", periods=len(values))
    closes = pd.Series(values, index=idx, name="QQQ")
    eq, trades, stats = simulate_momentum(closes, start_capital=100.0, slip_bps=2.5)
    assert stats.trades >= 1
    assert trades[0]["reason"] in {"close_below_trail", "max_hold_days"}
    assert eq.iloc[-1] > 0


def test_time_stop_fires_on_synthetic_grind():
    values = [100.0] * 20 + [101.0 + 0.01 * i for i in range(25)]
    idx = pd.bdate_range("2020-01-02", periods=len(values))
    closes = pd.Series(values, index=idx, name="QQQ")
    _, trades, stats = simulate_momentum(closes, start_capital=100.0, slip_bps=0.0)
    assert stats.trades >= 1
    assert any(t["reason"] == "max_hold_days" for t in trades)


def test_tuned_qqq_offline_reports_honest_stats():
    _, trades, stats = run_tuned(cache_dir=str(CACHE), offline=True)
    assert stats.start >= "2005-01-03"
    assert stats.end <= "2026-09-30"
    assert stats.start_capital == 100.0
    assert stats.slip_bps == 2.5
    assert stats.lookback == 20
    assert stats.final_value > 0
    assert -1.0 < stats.cagr < 1.0
    assert stats.trades > 0
    # Ballpark from research was ~8.9% / $639 — we do not force a match.
    assert trades
