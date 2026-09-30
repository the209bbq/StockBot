from __future__ import annotations

from config import Config, TrendFilterConfig
from strategy import band_breached, decide, risk_off_weights


def test_band_breached_when_any_leg_exceeds_5pp(cfg):
    current = {"VTI": 0.61, "VXUS": 0.22, "BND": 0.17}
    assert band_breached(current, cfg.target_weights, 5.0)
    current = {"VTI": 0.57, "VXUS": 0.24, "BND": 0.19}
    assert not band_breached(current, cfg.target_weights, 5.0)


def test_band_rebalance_only_when_breached(cfg):
    current = {"VTI": 0.62, "VXUS": 0.20, "BND": 0.18}
    d = decide(current_weights=current, cfg=cfg, risk_on=True, signal_close=200.0, sma=180.0)
    assert d.should_rebalance
    assert d.reason == "band_rebalance"
    assert d.risk_on is True
    assert d.target_weights == cfg.target_weights


def test_within_band_does_not_trade(cfg):
    current = {"VTI": 0.55, "VXUS": 0.25, "BND": 0.20}
    d = decide(current_weights=current, cfg=cfg, risk_on=True, signal_close=200.0, sma=180.0)
    assert not d.should_rebalance
    assert d.reason == "within_band"


def test_trend_filter_goes_to_100_pct_bnd(cfg):
    current = {"VTI": 0.55, "VXUS": 0.25, "BND": 0.20}
    d = decide(current_weights=current, cfg=cfg, risk_on=True, signal_close=170.0, sma=180.0)
    assert d.should_rebalance
    assert d.risk_on is False
    assert d.reason == "trend_filter_risk_off"
    assert d.target_weights == {"VTI": 0.0, "VXUS": 0.0, "BND": 1.0}


def test_trend_filter_returns_to_mix_when_back_above(cfg):
    current = {"VTI": 0.0, "VXUS": 0.0, "BND": 1.0}
    d = decide(current_weights=current, cfg=cfg, risk_on=False, signal_close=190.0, sma=180.0)
    assert d.should_rebalance
    assert d.risk_on is True
    assert d.reason == "trend_filter_risk_on"
    assert d.target_weights == cfg.target_weights


def test_trend_filter_holds_when_already_risk_off(cfg):
    current = {"VTI": 0.0, "VXUS": 0.0, "BND": 1.0}
    d = decide(current_weights=current, cfg=cfg, risk_on=False, signal_close=170.0, sma=180.0)
    assert not d.should_rebalance
    assert d.risk_on is False
    assert d.reason == "trend_filter_hold_risk_off"


def test_trend_filter_off_ignores_sma(cfg):
    disabled = Config(
        target_weights=cfg.target_weights,
        rebalance_band_pp=cfg.rebalance_band_pp,
        trend_filter=TrendFilterConfig(enabled=False),
    )
    current = {"VTI": 0.55, "VXUS": 0.25, "BND": 0.20}
    d = decide(current_weights=current, cfg=disabled, risk_on=True)
    assert d.risk_on is True
    assert d.reason == "within_band"
    assert d.target_weights == cfg.target_weights


def test_risk_off_weights_single_asset(cfg):
    assert risk_off_weights(cfg)["BND"] == 1.0
    assert sum(risk_off_weights(cfg).values()) == 1.0
