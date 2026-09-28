"""Load config.yaml plus a few environment overrides (never secrets from git)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


class ConfigError(ValueError):
    """Invalid or incomplete configuration."""


@dataclass(frozen=True)
class TrendFilterConfig:
    enabled: bool = True
    sma_days: int = 200
    signal_ticker: str = "VTI"
    risk_off_ticker: str = "BND"


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: float = 200_000.0
    max_daily_turnover_pct: float = 2.0
    kill_switch_file: str = ".killswitch"


@dataclass(frozen=True)
class BacktestConfig:
    start_capital: float = 100_000.0
    slippage_bps: float = 5.0
    execution_lag_days: int = 1
    rf_ticker: str = "^IRX"


@dataclass(frozen=True)
class Config:
    paper_base_url: str = PAPER_BASE_URL
    target_weights: dict[str, float] = field(
        default_factory=lambda: {"VTI": 0.55, "VXUS": 0.25, "BND": 0.20}
    )
    rebalance_band_pp: float = 5.0
    trend_filter: TrendFilterConfig = field(default_factory=TrendFilterConfig)
    cash_buffer_pct: float = 0.01
    min_trade_notional: float = 50.0
    risk: RiskLimits = field(default_factory=RiskLimits)
    timezone: str = "America/Los_Angeles"
    db_path: str = "data/trader.db"
    backtest: BacktestConfig = field(default_factory=BacktestConfig)

    @property
    def symbols(self) -> list[str]:
        return list(self.target_weights.keys())

    @property
    def kill_switch_path(self) -> Path:
        return Path(self.risk.kill_switch_file)


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

    tf_raw = raw.get("trend_filter") or {}
    risk_raw = raw.get("risk") or {}
    bt_raw = raw.get("backtest") or {}

    paper_url = os.environ.get("APCA_API_BASE_URL") or raw.get("paper_base_url") or PAPER_BASE_URL

    return Config(
        paper_base_url=str(paper_url).rstrip("/"),
        target_weights=_require_weights(raw.get("target_weights")),
        rebalance_band_pp=float(raw.get("rebalance_band_pp", 5.0)),
        trend_filter=TrendFilterConfig(
            enabled=bool(tf_raw.get("enabled", True)),
            sma_days=int(tf_raw.get("sma_days", 200)),
            signal_ticker=str(tf_raw.get("signal_ticker", "VTI")).upper(),
            risk_off_ticker=str(tf_raw.get("risk_off_ticker", "BND")).upper(),
        ),
        cash_buffer_pct=float(raw.get("cash_buffer_pct", 0.01)),
        min_trade_notional=float(raw.get("min_trade_notional", 50.0)),
        risk=RiskLimits(
            max_order_notional=float(risk_raw.get("max_order_notional", 200_000.0)),
            max_daily_turnover_pct=float(risk_raw.get("max_daily_turnover_pct", 2.0)),
            kill_switch_file=str(risk_raw.get("kill_switch_file", ".killswitch")),
        ),
        timezone=str(raw.get("timezone", "America/Los_Angeles")),
        db_path=str(raw.get("db_path", "data/trader.db")),
        backtest=BacktestConfig(
            start_capital=float(bt_raw.get("start_capital", 100_000.0)),
            slippage_bps=float(bt_raw.get("slippage_bps", 5.0)),
            execution_lag_days=int(bt_raw.get("execution_lag_days", 1)),
            rf_ticker=str(bt_raw.get("rf_ticker", "^IRX")),
        ),
    )
