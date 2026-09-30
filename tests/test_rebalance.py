from __future__ import annotations

from rebalance import Position, plan_orders, portfolio_weights, reconcile


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
    # Sells come first so cash is freed before buys.
    assert [o.side for o in orders].index("sell") < [o.side for o in orders].index("buy")
    # Target VTI is 55% of investable (99k) = 54450; have 70000.
    assert by_sym["VTI"].notional == 15550.0


def test_min_trade_size_skips_tiny_drift(cfg, on_target_positions):
    # Nudge VTI by $20 — below the $50 min trade — without changing equity.
    pos = dict(on_target_positions)
    vti = pos["VTI"]
    pos["VTI"] = Position(vti.symbol, vti.qty, vti.market_value + 20, vti.avg_price, vti.current_price)
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


def test_cash_buffer_leaves_uninvested_cash(cfg, on_target_positions):
    # All cash, no positions: should buy 99% of equity, not 100%.
    prices = {"VTI": 220.0, "VXUS": 62.5, "BND": 80.0}
    orders = plan_orders(
        positions={},
        cash=100_000.0,
        equity=100_000.0,
        target_weights=cfg.target_weights,
        prices=prices,
        cfg=cfg,
    )
    invested = sum(o.notional for o in orders if o.side == "buy")
    assert abs(invested - 99_000.0) < 1.0
    assert {o.symbol: o.notional for o in orders}["VTI"] == 54_450.0


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
    w = portfolio_weights(on_target_positions, cash=1000.0, equity=100_000.0)
    assert abs(sum(w.values()) - 0.99) < 1e-9
    assert abs(w["VTI"] - 0.5445) < 1e-9
