"""Load config.yaml plus a few environment overrides (never secrets from git)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
STRATEGIES = ("qqq_momentum", "etf_rebalance")


class ConfigError(ValueError):
    """Invalid or incomplete configuration."""


@dataclass(frozen=True)
class TrendFilterConfig:
    enabled: bool = True
    sma_days: int = 200
    signal_ticker: str = "VTI"
    risk_off_ticker: str = "BND"


@dataclass(frozen=True)
class QQQMomentumConfig:
    symbol: str = "QQQ"
    lookback_days: int = 20
    trail_pct: float = 0.05
    max_hold_days: int = 20
    evaluate_et: str = "15:50"
    evaluate_window_minutes: int = 20
    skip_early_closes: bool = True
    slippage_bps: float = 2.5


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional_pct: float = 1.0
    max_daily_loss_pct: float = 1.0
    max_daily_turnover_pct: float = 2.0
    max_order_notional: float | None = None
    kill_switch_file: str = ".killswitch"


@dataclass(frozen=True)
class BacktestConfig:
    start_capital: float = 100.0
    slippage_bps: float = 2.5
    execution_lag_days: int = 0
    rf_ticker: str = "^IRX"
    start: str = "2005-01-03"
    end: str = "2026-09-30"


@dataclass(frozen=True)
class Config:
    strategy: str = "qqq_momentum"
    paper_base_url: str = PAPER_BASE_URL
    capital_cap_usd: float = 100.0
    min_trade_notional: float = 1.0
    qqq_momentum: QQQMomentumConfig = field(default_factory=QQQMomentumConfig)
    target_weights: dict[str, float] = field(
        default_factory=lambda: {"VTI": 0.55, "VXUS": 0.25, "BND": 0.20}
    )
    rebalance_band_pp: float = 5.0
    trend_filter: TrendFilterConfig = field(default_factory=TrendFilterConfig)
    cash_buffer_pct: float = 0.01
    risk: RiskLimits = field(default_factory=RiskLimits)
    timezone: str = "America/New_York"
    db_path: str = "data/trader.db"
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    @property
    def symbols(self) -> list[str]:
        if self.strategy == "qqq_momentum":
            return [self.qqq_momentum.symbol]
        return list(self.target_weights.keys())

    @property
    def kill_switch_path(self) -> Path:
        return Path(self.risk.kill_switch_file)


def _optional_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    return float(value)


def _require_weights(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("target_weights must be a non-empty mapping of ticker -> weight")
    weights = {str(k).upper(): float(v) for k, v in raw.items()}
    total = sum(weights.values())
    if abs(total - 1.0) > 1e-6:
        raise ConfigError(f"target_weights must sum to 1.0, got {total}")
    if any(w < 0 for w in weights.values()):
        raise ConfigError("target_weights must be non-negative")
    return weights


def load_config(path: str | Path | None = None) -> Config:
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"config file not found: {cfg_path}")
    with cfg_path.open() as f:
        raw = yaml.safe_load(f) or {}

    strategy = str(raw.get("strategy") or "qqq_momentum").strip().lower()
    if strategy not in STRATEGIES:
        raise ConfigError(f"strategy must be one of {STRATEGIES}, got {strategy!r}")

    tf_raw = raw.get("trend_filter") or {}
    risk_raw = raw.get("risk") or {}
    bt_raw = raw.get("backtest") or {}
    mom_raw = raw.get("qqq_momentum") or {}
    paper_url = os.environ.get("APCA_API_BASE_URL") or raw.get("paper_base_url") or PAPER_BASE_URL
    weights_raw = raw.get("target_weights") or {"VTI": 0.55, "VXUS": 0.25, "BND": 0.20}

    return Config(
        strategy=strategy,
        paper_base_url=str(paper_url).rstrip("/"),
        capital_cap_usd=float(raw.get("capital_cap_usd", 100.0)),
        min_trade_notional=float(raw.get("min_trade_notional", 1.0)),
        qqq_momentum=QQQMomentumConfig(
            symbol=str(mom_raw.get("symbol", "QQQ")).upper(),
            lookback_days=int(mom_raw.get("lookback_days", 20)),
            trail_pct=float(mom_raw.get("trail_pct", 0.05)),
            max_hold_days=int(mom_raw.get("max_hold_days", 20)),
            evaluate_et=str(mom_raw.get("evaluate_et", "15:50")),
            evaluate_window_minutes=int(mom_raw.get("evaluate_window_minutes", 20)),
            skip_early_closes=bool(mom_raw.get("skip_early_closes", True)),
            slippage_bps=float(mom_raw.get("slippage_bps", 2.5)),
        ),
        target_weights=_require_weights(weights_raw),
        rebalance_band_pp=float(raw.get("rebalance_band_pp", 5.0)),
        trend_filter=TrendFilterConfig(
            enabled=bool(tf_raw.get("enabled", True)),
            sma_days=int(tf_raw.get("sma_days", 200)),
            signal_ticker=str(tf_raw.get("signal_ticker", "VTI")).upper(),
            risk_off_ticker=str(tf_raw.get("risk_off_ticker", "BND")).upper(),
        ),
        cash_buffer_pct=float(raw.get("cash_buffer_pct", 0.01)),
        risk=RiskLimits(
            max_order_notional_pct=float(risk_raw.get("max_order_notional_pct", 1.0)),
            max_daily_loss_pct=float(risk_raw.get("max_daily_loss_pct", 1.0)),
            max_daily_turnover_pct=float(risk_raw.get("max_daily_turnover_pct", 2.0)),
            max_order_notional=_optional_float(risk_raw.get("max_order_notional")),
            kill_switch_file=str(risk_raw.get("kill_switch_file", ".killswitch")),
        ),
        timezone=str(raw.get("timezone", "America/New_York")),
        db_path=str(raw.get("db_path", "data/trader.db")),
        backtest=BacktestConfig(
            start_capital=float(bt_raw.get("start_capital", 100.0)),
            slippage_bps=float(bt_raw.get("slippage_bps", 2.5)),
            execution_lag_days=int(bt_raw.get("execution_lag_days", 0)),
            rf_ticker=str(bt_raw.get("rf_ticker", "^IRX")),
            start=str(bt_raw.get("start", "2005-01-03")),
            end=str(bt_raw.get("end", "2026-09-30")),
        ),
    )
