"""Hard risk limits: max order size, max daily turnover, kill switch."""

from __future__ import annotations

import os
from pathlib import Path

from rebalance import OrderIntent


class RiskError(RuntimeError):
    """An order batch violated a risk limit or the kill switch is on."""


KILL_SWITCH_TRUTHY = {"1", "true", "yes", "on"}


def kill_switch_active(
    path: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> bool:
    """True if KILL_SWITCH is set in the environment or the kill-switch file exists."""
    environ = env if env is not None else os.environ
    flag = str(environ.get("KILL_SWITCH", "")).strip().lower()
    if flag in KILL_SWITCH_TRUTHY:
        return True
    if path and Path(path).exists():
        return True
    return False


def order_notional(order: OrderIntent) -> float:
    return abs(float(order.notional))


def turnover_notional(orders: list[OrderIntent]) -> float:
    return sum(order_notional(o) for o in orders)


def check_orders(
    orders: list[OrderIntent],
    *,
    equity: float,
    max_order_notional: float,
    max_daily_turnover_pct: float,
    already_traded_today: float = 0.0,
    kill_switch_path: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Raise RiskError if the kill switch is on or a batch exceeds caps."""
    if kill_switch_active(kill_switch_path, env=env):
        raise RiskError("kill switch is active; all orders are blocked")
    if not orders:
        return
    if equity <= 0:
        raise RiskError("equity must be positive to size risk checks")

    for order in orders:
        n = order_notional(order)
        if n > max_order_notional + 1e-6:
            raise RiskError(
                f"order {order.side} {order.symbol} ${n:.2f} exceeds max_order_notional ${max_order_notional:.2f}"
            )

    turnover = already_traded_today + turnover_notional(orders)
    cap = equity * max_daily_turnover_pct
    if turnover > cap + 1e-6:
        raise RiskError(
            f"daily turnover ${turnover:.2f} exceeds {max_daily_turnover_pct:.2f}x equity "
            f"(${cap:.2f})"
        )
