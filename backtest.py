#!/usr/bin/env python3
"""
Reusable portfolio backtester: buy-and-hold vs. drift-band rebalancing vs.
drift-band + 200-day trend filter.

Adapted from the research backtest that produced tests/fixtures/expected_results.csv.
Data: free daily adjusted closes from yfinance (auto_adjust=True -> dividends
and splits folded into the close). Cache hits are preferred so tests stay offline.

Usage:
    python backtest.py                # runs both ETF and proxy universes
    python backtest.py --universe etf
    python backtest.py --offline      # cache only; fail if a ticker is missing
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

CONFIG = {
    "out_dir": os.path.dirname(os.path.abspath(__file__)),
    "start_capital": 100_000.0,
    "slippage_bps": 5.0,            # per side, on traded notional; commission = $0
    "band_pp": 5.0,                 # rebalance if any |weight - target| > this (pct points)
    "sma_days": 200,
    "execution_lag_days": 1,        # decide at month-end close, trade N trading days later at close
    "risk_off_asset": "bond",       # role key the stock sleeve moves into when trend is off
    "rf_ticker": "^IRX",            # 13-week T-bill yield (annualized %), used for Sharpe
    "cache_dir": "data_cache",
    "universes": {
        "etf": {
            "label": "ETF",
            "tickers": {"us": "VTI", "intl": "VXUS", "bond": "BND"},
            "signal": "VTI",
        },
        "proxy": {
            "label": "PROXY (mutual funds)",
            "tickers": {"us": "VTSMX", "intl": "VGTSX", "bond": "VBMFX"},
            "signal": "VTSMX",
        },
    },
    "mixes": {
        "80/20": {"us": 0.55, "intl": 0.25, "bond": 0.20},
        "60/40": {"us": 0.40, "intl": 0.20, "bond": 0.40},
    },
    "stock_roles": ["us", "intl"],
}


def fetch(ticker, cache_dir, refresh=False):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{ticker.replace('^', '_')}.csv")
    if os.path.exists(path) and not refresh:
        s = pd.read_csv(path, index_col=0, parse_dates=True)
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        s = s.dropna()
        s.name = ticker
        return s
    import yfinance as yf
    df = yf.download(ticker, period="max", auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise RuntimeError(f"No data returned for {ticker}")
    s = df["Close"]
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    s = s.dropna()
    s.name = ticker
    s.to_csv(path)
    return s


def load_prices(tickers, signal, cache_dir, refresh=False):
    needed = sorted(set(tickers) | {signal})
    return {t: fetch(t, cache_dir, refresh=refresh) for t in needed}


def month_end_flags(index):
    s = pd.Series(index, index=index)
    return s.groupby([index.year, index.month]).transform("max").eq(s).values


def simulate(prices, target, variant, cfg, sma=None, signal_px=None):
    """prices: DataFrame (roles as columns) aligned on trading days.
    variant: 'buyhold' | 'band' | 'band_trend'.  Returns (equity Series, stats dict, trade log)."""
    roles = list(prices.columns)
    tgt = np.array([target[r] for r in roles])
    slip = cfg["slippage_bps"] / 1e4
    band = cfg["band_pp"] / 100.0
    lag = cfg["execution_lag_days"]
    stock_idx = [roles.index(r) for r in cfg["stock_roles"]]  # noqa: F841  (kept for parity)
    off_idx = roles.index(cfg["risk_off_asset"])
    off_tgt = np.zeros(len(roles)); off_tgt[off_idx] = 1.0

    px = prices.values
    rets = np.vstack([np.zeros(len(roles)), px[1:] / px[:-1] - 1.0])
    me = month_end_flags(prices.index)
    n = len(prices)

    total_cost = 0.0
    trades_events = 0
    fund_trades = 0
    log = []

    def trade_to(hold, w_target, day):
        nonlocal total_cost, trades_events, fund_trades
        V = hold.sum()
        desired = w_target * V
        turnover = np.abs(desired - hold).sum()
        cost = turnover * slip
        V_net = V - cost
        new = w_target * V_net
        total_cost += cost
        trades_events += 1
        fund_trades += int((np.abs(new - hold) > 1e-6).sum())
        log.append({"date": prices.index[day].date(), "turnover": round(turnover, 2),
                    "cost": round(cost, 2), "value_after": round(V_net, 2),
                    "weights": dict(zip(roles, np.round(w_target, 3)))})
        return new

    # initial purchase (cash -> targets), costed but not counted as a rebalance
    cap = cfg["start_capital"]
    init_w = tgt.copy()
    risk_on = True
    if variant == "band_trend":
        risk_on = not (signal_px.iloc[0] < sma.iloc[0])
        if not risk_on:
            init_w = off_tgt.copy()
    init_cost = cap * slip
    hold = init_w * (cap - init_cost)
    total_cost += init_cost

    eq = np.empty(n); eq[0] = hold.sum()
    pending = None  # (exec_day, target_weights, new_risk_state)
    for t in range(1, n):
        hold = hold * (1.0 + rets[t])
        if pending is not None and pending[0] == t:
            hold = trade_to(hold, pending[1], t)
            if pending[2] is not None:
                risk_on = pending[2]
            pending = None
        if variant != "buyhold" and me[t] and pending is None and t + lag < n:
            w = hold / hold.sum()
            decision = None
            if variant == "band":
                if np.max(np.abs(w - tgt)) > band:
                    decision = (tgt, None)
            else:  # band_trend
                below = bool(signal_px.iloc[t] < sma.iloc[t])
                if below and risk_on:
                    decision = (off_tgt, False)
                elif not below and not risk_on:
                    decision = (tgt, True)
                elif not below and risk_on and np.max(np.abs(w - tgt)) > band:
                    decision = (tgt, None)
                # below and already risk-off: hold 100% risk-off asset, nothing to do
            if decision is not None:
                if lag == 0:
                    hold = trade_to(hold, decision[0], t)
                    if decision[1] is not None:
                        risk_on = decision[1]
                else:
                    pending = (t + lag, decision[0], decision[1])
        eq[t] = hold.sum()

    equity = pd.Series(eq, index=prices.index)
    return equity, {"rebalances": trades_events, "fund_trades": fund_trades,
                    "total_cost": total_cost}, log


def metrics(equity, rf_daily):
    r = equity.pct_change().dropna()
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    vol = r.std() * np.sqrt(252)
    dd = (equity / equity.cummax() - 1).min()
    rf = rf_daily.reindex(r.index).ffill().fillna(0.0)
    ex = r - rf
    sharpe_rf = ex.mean() / ex.std() * np.sqrt(252)
    sharpe_0 = r.mean() / r.std() * np.sqrt(252)
    return {"start": equity.index[0].date(), "end": equity.index[-1].date(),
            "years": round(years, 2), "CAGR_%": round(cagr * 100, 2),
            "vol_%": round(vol * 100, 2), "maxDD_%": round(dd * 100, 2),
            "sharpe_tbill": round(sharpe_rf, 2), "sharpe_rf0": round(sharpe_0, 2),
            "final_value": round(equity.iloc[-1], 2)}


VARIANTS = {"buyhold": "1 Buy&Hold", "band": "2 Band5pp", "band_trend": "3 Band5pp+SMA200"}


def run_period(prices, sig, sma, rf_daily, cfg, universe_label, period_label):
    rows, curves = [], {}
    for mix_name, target in cfg["mixes"].items():
        for v, vlabel in VARIANTS.items():
            eq, st, _ = simulate(prices, target, v, cfg, sma=sma, signal_px=sig)
            m = metrics(eq, rf_daily)
            rows.append({"universe": universe_label, "period": period_label, "mix": mix_name,
                         "variant": vlabel, **m, "rebalances": st["rebalances"],
                         "fund_trades": st["fund_trades"],
                         "total_cost_$": round(st["total_cost"], 2)})
            curves[f"{mix_name} {vlabel}"] = eq
    return rows, curves


def run_universe(key, cfg=CONFIG, refresh=False):
    u = cfg["universes"][key]
    cache = os.path.join(cfg["out_dir"], cfg["cache_dir"])
    raw = load_prices(list(u["tickers"].values()), u["signal"], cache, refresh=refresh)
    rf = fetch(cfg["rf_ticker"], cache, refresh=refresh)
    rf_daily = (rf / 100.0) / 252.0

    sig_full = raw[u["signal"]]
    sma_full = sig_full.rolling(cfg["sma_days"]).mean()
    px = pd.concat({role: raw[t] for role, t in u["tickers"].items()}, axis=1, sort=True).dropna()
    # start = first day all funds have data AND the SMA is defined
    start = max(px.index[0], sma_full.dropna().index[0])
    px = px.loc[start:]
    sig = sig_full.reindex(px.index).ffill()
    sma = sma_full.reindex(px.index).ffill()
    info = {"universe": u["label"], "first_common_date": str(px.index[0].date()),
            "last_date": str(px.index[-1].date()), "trading_days": len(px),
            "per_ticker_first_date": {t: str(raw[t].index[0].date()) for t in raw}}

    rows, curves = run_period(px, sig, sma, rf_daily, cfg, u["label"], "full")
    # out-of-sample style split: two halves by calendar midpoint, each re-run fresh from start capital
    mid = px.index[0] + (px.index[-1] - px.index[0]) / 2
    for lbl, sl in [("1st half", px.loc[:mid]), ("2nd half", px.loc[mid:])]:
        r, _ = run_period(sl, sig.loc[sl.index], sma.loc[sl.index], rf_daily, cfg, u["label"], lbl)
        rows += r
    return rows, curves, info


def plot(curves_by_universe, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = len(curves_by_universe)
    fig, axes = plt.subplots(n, 1, figsize=(13, 6.5 * n))
    axes = np.atleast_1d(axes)
    styles = {"1 Buy&Hold": ":", "2 Band5pp": "--", "3 Band5pp+SMA200": "-"}
    colors = {"80/20": "tab:blue", "60/40": "tab:orange"}
    for ax, (ulabel, curves) in zip(axes, curves_by_universe.items()):
        for name, eq in curves.items():
            mix, var = name.split(" ", 1)
            ax.plot(eq.index, eq.values, styles[var], color=colors[mix], lw=1.4, label=name)
        ax.set_yscale("log")
        ax.set_title(f"{ulabel}: growth of $100,000 (log scale, net of 5 bps slippage)")
        ax.set_ylabel("Portfolio value ($)")
        ax.grid(True, which="both", alpha=0.3)
        ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=["etf", "proxy", "all"], default="all")
    ap.add_argument("--offline", action="store_true", help="Use cached CSVs only.")
    ap.add_argument("--refresh", action="store_true", help="Re-download prices.")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--out-dir", default=None)
    a = ap.parse_args()
    cfg = dict(CONFIG)
    if a.out_dir:
        cfg["out_dir"] = a.out_dir
    if a.cache_dir:
        cfg["cache_dir"] = a.cache_dir
    refresh = a.refresh and not a.offline
    keys = ["etf", "proxy"] if a.universe == "all" else [a.universe]
    all_rows, curves_by_u, infos = [], {}, []
    for k in keys:
        try:
            rows, curves, info = run_universe(k, cfg, refresh=refresh)
        except Exception as e:  # report failure honestly, never substitute data
            print(f"[FAIL] universe {k}: {e}", file=sys.stderr)
            continue
        all_rows += rows
        curves_by_u[info["universe"]] = curves
        infos.append(info)
    if not all_rows:
        sys.exit("No universes ran successfully.")
    df = pd.DataFrame(all_rows)
    out = cfg["out_dir"]
    df.to_csv(os.path.join(out, "results.csv"), index=False)
    with open(os.path.join(out, "data_info.json"), "w") as f:
        json.dump(infos, f, indent=2)
    try:
        plot(curves_by_u, os.path.join(out, "equity_curves.png"))
    except Exception as e:
        print(f"[warn] plot skipped: {e}", file=sys.stderr)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    print(json.dumps(infos, indent=2))
    print(df.to_string(index=False))


if __name__ == "__main__":
    main()
