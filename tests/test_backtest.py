from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from backtest import CONFIG, metrics, simulate

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
EXPECTED_CSV = FIXTURE_DIR / "expected_results.csv"
CACHE_DIR = FIXTURE_DIR / "data_cache"


def _synthetic_prices(days=500, start="2014-01-02"):
    idx = pd.bdate_range(start, periods=days)
    # Uptrend, then a sharp multi-month crash so the 200-day filter goes risk-off.
    rets_us = np.full(days, 0.0008)
    rets_us[280:340] = -0.02
    us = 100 * np.cumprod(1.0 + rets_us)
    intl = 80 * np.cumprod(1.0 + rets_us * 0.85)
    bond = 70 * np.cumprod(1.0 + np.full(days, 0.00015))
    return pd.DataFrame({"us": us, "intl": intl, "bond": bond}, index=idx)


def test_simulate_buyhold_never_rebalances():
    px = _synthetic_prices()
    sig = px["us"]
    sma = sig.rolling(200).mean()
    eq, st, log = simulate(px, {"us": 0.55, "intl": 0.25, "bond": 0.20}, "buyhold", CONFIG, sma=sma, signal_px=sig)
    assert st["rebalances"] == 0
    assert not log
    assert eq.iloc[0] == pytest_approx_init()


def pytest_approx_init():
    # initial purchase costs 5 bps of 100_000
    return 100_000.0 * (1 - CONFIG["slippage_bps"] / 1e4)


def test_simulate_band_trades_only_on_breach():
    px = _synthetic_prices()
    sig = px["us"]
    sma = sig.rolling(200).mean()
    _, st_bh, _ = simulate(px, {"us": 0.55, "intl": 0.25, "bond": 0.20}, "buyhold", CONFIG, sma=sma, signal_px=sig)
    _, st_band, log = simulate(px, {"us": 0.55, "intl": 0.25, "bond": 0.20}, "band", CONFIG, sma=sma, signal_px=sig)
    assert st_band["rebalances"] >= 1
    assert st_band["rebalances"] != st_bh["rebalances"]
    assert all(entry["weights"]["bond"] == 0.2 for entry in log)


def test_simulate_trend_goes_to_bonds_in_drawdown():
    px = _synthetic_prices()
    sig = px["us"]
    sma = sig.rolling(200).mean()
    start = sma.dropna().index[0]
    px = px.loc[start:]
    sig = sig.reindex(px.index)
    sma = sma.reindex(px.index)
    _, st, log = simulate(
        px, {"us": 0.55, "intl": 0.25, "bond": 0.20}, "band_trend", CONFIG, sma=sma, signal_px=sig
    )
    assert st["rebalances"] >= 1
    assert any(abs(entry["weights"]["bond"] - 1.0) < 1e-9 for entry in log)


def test_metrics_shape():
    idx = pd.bdate_range("2020-01-02", periods=252)
    eq = pd.Series(np.linspace(100_000, 110_000, len(idx)), index=idx)
    rf = pd.Series(0.0, index=idx)
    m = metrics(eq, rf)
    assert m["CAGR_%"] > 0
    assert m["final_value"] == 110_000.0


def _compare_etf_rows(got: pd.DataFrame, expected: pd.DataFrame) -> None:
    keys = ["universe", "period", "mix", "variant"]
    exp = expected[expected["universe"] == "ETF"].copy()
    got_etf = got[got["universe"] == "ETF"].copy()
    merged = exp.merge(got_etf, on=keys, suffixes=("_e", "_g"))
    assert len(merged) == len(exp), f"missing ETF rows: expected {len(exp)} got {len(got_etf)}"
    for _, row in merged.iterrows():
        label = f"{row['period']} {row['mix']} {row['variant']}"
        assert abs(row["CAGR_%_g"] - row["CAGR_%_e"]) <= 0.05, f"{label} CAGR"
        assert abs(row["vol_%_g"] - row["vol_%_e"]) <= 0.05, f"{label} vol"
        assert abs(row["maxDD_%_g"] - row["maxDD_%_e"]) <= 0.05, f"{label} maxDD"
        assert abs(row["final_value_g"] - row["final_value_e"]) <= 2.0, f"{label} final_value"
        assert row["rebalances_g"] == row["rebalances_e"], f"{label} rebalances"


def test_etf_results_match_attached_csv_within_rounding():
    """Replay the research backtest from cached adjusted closes."""
    if not EXPECTED_CSV.exists():
        raise AssertionError("tests/fixtures/expected_results.csv is missing")
    needed = ["VTI.csv", "VXUS.csv", "BND.csv", "_IRX.csv"]
    if not all((CACHE_DIR / name).exists() for name in needed):
        raise AssertionError(
            f"offline price cache missing under {CACHE_DIR}; expected {needed}"
        )
    from backtest import run_universe

    cfg = dict(CONFIG)
    cfg["out_dir"] = str(FIXTURE_DIR)
    cfg["cache_dir"] = "data_cache"
    rows, _, info = run_universe("etf", cfg, refresh=False)
    got = pd.DataFrame(rows)
    expected = pd.read_csv(EXPECTED_CSV)
    # The attached file ends 2026-09-28; a longer cache is truncated to that window
    # inside run_universe via the cached series themselves.
    _compare_etf_rows(got, expected)
    assert info["universe"] == "ETF"
