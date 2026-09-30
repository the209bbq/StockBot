from __future__ import annotations

from rebalance import (
    Position,
    cash_buffer_dollars,
    managed_capital,
    plan_orders,
    portfolio_weights,
    reconcile,
)


def test_order_diff_sells_overweight_and_buys_underweight(cfg, drifted_positions):
    prices = {s: p.current_price for s, p in drifted_positions.items()}
    equity = 100_000.0
    cash = 1_000.0
    orders = plan_orders(
        positions=drifted_positions,
        cash=cash,
        equity=equity,
        target_weights=cfg.target_weights,
        prices=prices,
        cfg=cfg,
        reason="band_rebalance",
    )
    by_sym = {o.symbol: o for o in orders}
    assert by_sym["VTI"].side == "sell"
    assert by_sym["VXUS"].side == "buy"
    assert by_sym["BND"].side == "buy"
    assert [o.side for o in orders].index("sell") < [o.side for o in orders].index("buy")
    # $1 cash buffer: investable 99999; VTI target 54999.45 vs 70000.
    assert by_sym["VTI"].notional == 15000.55


def test_min_trade_size_skips_tiny_drift(cfg, on_target_positions):
    # Nudge VTI by $0.50 — below the $1 Alpaca fractional minimum.
    pos = dict(on_target_positions)
    vti = pos["VTI"]
    pos["VTI"] = Position(vti.symbol, vti.qty, vti.market_value + 0.50, vti.avg_price, vti.current_price)
    prices = {s: p.current_price for s, p in pos.items()}
    orders = plan_orders(
        positions=pos,
        cash=980.0,
        equity=100_000.0,
        target_weights=cfg.target_weights,
        prices=prices,
        cfg=cfg,
    )
    assert orders == []


def test_cash_buffer_is_one_dollar(cfg):
    prices = {"VTI": 220.0, "VXUS": 62.5, "BND": 80.0}
    orders = plan_orders(
        positions={},
        cash=100.0,
        equity=100.0,
        target_weights=cfg.target_weights,
        prices=prices,
        cfg=cfg,
    )
    invested = sum(o.notional for o in orders if o.side == "buy")
    assert abs(invested - 99.0) < 0.02
    assert {o.symbol: o.notional for o in orders}["VTI"] == 54.45


def test_extra_symbol_is_sold(cfg, on_target_positions):
    pos = dict(on_target_positions)
    pos["XYZ"] = Position("XYZ", qty=10, market_value=800.0, avg_price=80.0, current_price=80.0)
    prices = {s: p.current_price for s, p in pos.items()}
    orders = plan_orders(
        positions=pos,
        cash=200.0,
        equity=100_000.0,
        target_weights=cfg.target_weights,
        prices=prices,
        cfg=cfg,
    )
    extra = [o for o in orders if o.symbol == "XYZ"]
    assert extra and extra[0].side == "sell" and extra[0].close_position


def test_reconcile_reports_qty_mismatch(on_target_positions):
    stored = dict(on_target_positions)
    broker = dict(on_target_positions)
    vti = broker["VTI"]
    broker["VTI"] = Position(vti.symbol, vti.qty + 3, vti.market_value, vti.avg_price, vti.current_price)
    mismatches = reconcile(broker, stored)
    assert any(m.startswith("VTI:") for m in mismatches)


def test_portfolio_weights_sum_to_positions_share(on_target_positions):
    w = portfolio_weights(on_target_positions, cash=1.0, equity=100_000.0)
    assert abs(sum(w.values()) - 0.99999) < 1e-9
    assert abs(w["VTI"] - 0.5499945) < 1e-9


def test_managed_capital_caps_flat_100k_account(cfg):
    managed, bot_cash, bot_mv = managed_capital(
        positions={},
        account_cash=100_000.0,
        symbols=cfg.symbols,
        capital_cap_usd=100.0,
    )
    assert bot_mv == 0.0
    assert managed == 100.0
    assert bot_cash == 100.0


def test_managed_capital_ignores_surplus_cash_once_sleeved(cfg):
    pos = {
        "VTI": Position("VTI", qty=0.1, market_value=55.0, current_price=550.0),
        "VXUS": Position("VXUS", qty=0.4, market_value=25.0, current_price=62.5),
        "BND": Position("BND", qty=0.24, market_value=19.0, current_price=80.0),
    }
    managed, bot_cash, bot_mv = managed_capital(
        positions=pos,
        account_cash=99_901.0,
        symbols=cfg.symbols,
        capital_cap_usd=100.0,
    )
    assert bot_mv == 99.0
    assert bot_cash == 1.0  # unused cap only, not the leftover $99,901
    assert managed == 100.0


def test_managed_capital_lets_sleeve_grow(cfg):
    pos = {"VTI": Position("VTI", qty=1, market_value=150.0, current_price=150.0)}
    managed, bot_cash, bot_mv = managed_capital(
        positions=pos,
        account_cash=99_850.0,
        symbols=cfg.symbols,
        capital_cap_usd=100.0,
    )
    assert bot_mv == 150.0
    assert bot_cash == 0.0
    assert managed == 150.0


def test_cash_buffer_dollars(cfg):
    assert cash_buffer_dollars(cfg, 100.0) == 1.0
    assert cash_buffer_dollars(cfg, 0.5) == 0.5
