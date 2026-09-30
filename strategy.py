"""Static mix + optional 200-day trend filter (single on/off flag)."""

from __future__ import annotations

from dataclasses import dataclass

from config import Config


@dataclass(frozen=True)
class StrategyDecision:
    target_weights: dict[str, float]
    risk_on: bool
    should_rebalance: bool
    reason: str
    drifts: dict[str, float]
    max_drift_pp: float
    signal_close: float | None = None
    sma: float | None = None


def _weight_drifts(current_weights: dict[str, float], target_weights: dict[str, float]) -> dict[str, float]:
    symbols = set(current_weights) | set(target_weights)
    return {s: current_weights.get(s, 0.0) - target_weights.get(s, 0.0) for s in sorted(symbols)}


def band_breached(current_weights: dict[str, float], target_weights: dict[str, float], band_pp: float) -> bool:
    """True if any symbol is more than `band_pp` percentage points from target."""
    if not target_weights:
        return False
    drifts = _weight_drifts(current_weights, target_weights)
    return max(abs(v) for v in drifts.values()) > (band_pp / 100.0)


def risk_off_weights(cfg: Config) -> dict[str, float]:
    ticker = cfg.trend_filter.risk_off_ticker
    return {symbol: (1.0 if symbol == ticker else 0.0) for symbol in cfg.target_weights}


def decide(
    *,
    current_weights: dict[str, float],
    cfg: Config,
    risk_on: bool,
    signal_close: float | None = None,
    sma: float | None = None,
) -> StrategyDecision:
    """Month-end decision: stay, band-rebalance, or flip the trend filter.

    Matches the attached backtest rules:
    - below SMA and currently risk-on  -> 100% risk-off asset
    - above SMA and currently risk-off -> return to the configured mix
    - above SMA and risk-on            -> rebalance only if a band is breached
    - below SMA and already risk-off   -> hold
    """
    mix = dict(cfg.target_weights)
    off = risk_off_weights(cfg)
    enabled = cfg.trend_filter.enabled

    if enabled:
        if signal_close is None or sma is None:
            raise ValueError("trend filter is enabled but signal_close/sma were not provided")
        below = signal_close < sma
        if below and risk_on:
            target = off
            drifts = _weight_drifts(current_weights, target)
            return StrategyDecision(
                target_weights=target,
                risk_on=False,
                should_rebalance=True,
                reason="trend_filter_risk_off",
                drifts=drifts,
                max_drift_pp=max(abs(v) for v in drifts.values()) * 100.0,
                signal_close=signal_close,
                sma=sma,
            )
        if (not below) and (not risk_on):
            target = mix
            drifts = _weight_drifts(current_weights, target)
            return StrategyDecision(
                target_weights=target,
                risk_on=True,
                should_rebalance=True,
                reason="trend_filter_risk_on",
                drifts=drifts,
                max_drift_pp=max(abs(v) for v in drifts.values()) * 100.0,
                signal_close=signal_close,
                sma=sma,
            )
        if below and not risk_on:
            target = off
            drifts = _weight_drifts(current_weights, target)
            return StrategyDecision(
                target_weights=target,
                risk_on=False,
                should_rebalance=False,
                reason="trend_filter_hold_risk_off",
                drifts=drifts,
                max_drift_pp=max((abs(v) for v in drifts.values()), default=0.0) * 100.0,
                signal_close=signal_close,
                sma=sma,
            )

    target = mix
    drifts = _weight_drifts(current_weights, target)
    max_drift = max((abs(v) for v in drifts.values()), default=0.0)
    breached = max_drift > (cfg.rebalance_band_pp / 100.0)
    return StrategyDecision(
        target_weights=target,
        risk_on=True,
        should_rebalance=breached,
        reason="band_rebalance" if breached else "within_band",
        drifts=drifts,
        max_drift_pp=max_drift * 100.0,
        signal_close=signal_close,
        sma=sma,
    )
