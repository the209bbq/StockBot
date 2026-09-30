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


def test_100_dollar_start_and_one_dollar_minimums():
    """$100 start, unconstrained vs $1 min trade + $1 cash buffer, 80/20 full sample."""
    if not all((CACHE_DIR / name).exists() for name in ["VTI.csv", "VXUS.csv", "BND.csv", "_IRX.csv"]):
        raise AssertionError(f"offline price cache missing under {CACHE_DIR}")
    from backtest import run_universe

    base = dict(CONFIG)
    base["out_dir"] = str(FIXTURE_DIR)
    base["cache_dir"] = "data_cache"
    base["start_capital"] = 100.0
    rows_plain, _, _ = run_universe("etf", base, refresh=False)
    capped = dict(base)
    capped["min_trade_notional"] = 1.0
    capped["cash_buffer_usd"] = 1.0
    rows_min, _, _ = run_universe("etf", capped, refresh=False)

    def _row(rows, variant):
        for r in rows:
            if r["universe"] == "ETF" and r["period"] == "full" and r["mix"] == "80/20" and r["variant"] == variant:
                return r
        raise AssertionError(variant)

    labels = ["1 Buy&Hold", "2 Band5pp", "3 Band5pp+SMA200"]
    expected_100k = pd.read_csv(EXPECTED_CSV)
    for lab in labels:
        p = _row(rows_plain, lab)
        m = _row(rows_min, lab)
        e = expected_100k[
            (expected_100k["universe"] == "ETF")
            & (expected_100k["period"] == "full")
            & (expected_100k["mix"] == "80/20")
            & (expected_100k["variant"] == lab)
        ].iloc[0]
        # Unconstrained $100 book is a linear scale of the $100k research run.
        assert abs(p["CAGR_%"] - e["CAGR_%"]) <= 0.05
        assert abs(p["maxDD_%"] - e["maxDD_%"]) <= 0.05
        assert p["rebalances"] == e["rebalances"]
        assert abs(p["final_value"] - e["final_value"] / 1000.0) <= 0.02
        # Persist both rows on the result object for the report helper.
        p["_min_cagr"] = m["CAGR_%"]
        p["_min_dd"] = m["maxDD_%"]
        p["_min_reb"] = m["rebalances"]
        p["_min_cost"] = m["total_cost_$"]
        p["_min_final"] = m["final_value"]

    # $1 floors should not invent extra calendar rebalances on this sample.
    for lab in labels:
        p = _row(rows_plain, lab)
        m = _row(rows_min, lab)
        assert m["rebalances"] <= p["rebalances"] + 2


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
