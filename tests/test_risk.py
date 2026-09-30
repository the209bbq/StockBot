from __future__ import annotations

import pytest

from rebalance import OrderIntent
from risk import RiskError, check_orders, kill_switch_active


def _buy(symbol="VTI", notional=1_000.0) -> OrderIntent:
    return OrderIntent(symbol=symbol, side="buy", notional=notional, qty=1.0)


def test_kill_switch_file(tmp_path):
    path = tmp_path / ".killswitch"
    assert not kill_switch_active(path, env={})
    path.write_text("stop\n")
    assert kill_switch_active(path, env={})


def test_kill_switch_env(tmp_path):
    path = tmp_path / "missing"
    assert kill_switch_active(path, env={"KILL_SWITCH": "1"})
    assert kill_switch_active(path, env={"KILL_SWITCH": "true"})
    assert not kill_switch_active(path, env={"KILL_SWITCH": "0"})


def test_check_orders_blocks_when_kill_switch_on(tmp_path):
    path = tmp_path / ".killswitch"
    path.write_text("1")
    with pytest.raises(RiskError, match="kill switch"):
        check_orders(
            [_buy()],
            equity=100_000,
            max_order_notional=50_000,
            max_daily_turnover_pct=2.0,
            kill_switch_path=path,
            env={},
        )


def test_max_order_size():
    with pytest.raises(RiskError, match="max_order_notional"):
        check_orders(
            [_buy(notional=60_000)],
            equity=100_000,
            max_order_notional=50_000,
            max_daily_turnover_pct=2.0,
            env={},
        )


def test_max_daily_turnover_includes_prior_fills():
    with pytest.raises(RiskError, match="daily turnover"):
        check_orders(
            [_buy("VTI", 80_000), _buy("BND", 80_000)],
            equity=100_000,
            max_order_notional=200_000,
            max_daily_turnover_pct=1.0,
            already_traded_today=0,
            env={},
        )
    # 160k of a 200k cap is allowed
    check_orders(
        [_buy("VTI", 80_000), _buy("BND", 80_000)],
        equity=100_000,
        max_order_notional=200_000,
        max_daily_turnover_pct=2.0,
        env={},
    )
