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

    order_cap = float("inf") if max_order_notional is None else float(max_order_notional)
    for order in orders:
        n = order_notional(order)
        if n > order_cap + 1e-6:
            raise RiskError(
                f"order {order.side} {order.symbol} ${n:.2f} exceeds max_order_notional ${order_cap:.2f}"
            )

    turnover = already_traded_today + turnover_notional(orders)
    cap = equity * max_daily_turnover_pct
    if turnover > cap + 1e-6:
        raise RiskError(
            f"daily turnover ${turnover:.2f} exceeds {max_daily_turnover_pct:.2f}x equity "
            f"(${cap:.2f})"
        )


def check_momentum_risk(
    order: OrderIntent | None,
    *,
    tradable: float,
    max_order_notional: float,
    max_daily_loss_pct: float,
    realized_pnl_today: float,
    kill_switch_path: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    if kill_switch_active(kill_switch_path, env=env):
        raise RiskError("kill switch is active; all orders are blocked")
    if realized_pnl_today < 0:
        loss = -realized_pnl_today
        limit = max(0.0, tradable) * max_daily_loss_pct
        if loss + 1e-9 >= limit > 0:
            raise RiskError(
                f"daily sleeve loss ${loss:.2f} reached {max_daily_loss_pct:.0%} of tradable ${tradable:.2f}"
            )
    if order is None:
        return
    n = order_notional(order)
    if n + 1e-9 < 1.0:
        raise RiskError(f"order notional ${n:.2f} is below the $1 Alpaca fractional minimum")
    if n > max_order_notional + 1e-6:
        raise RiskError(
            f"order {order.side} {order.symbol} ${n:.2f} exceeds max_order_notional ${max_order_notional:.2f}"
        )
    if order.side == "buy" and n > tradable + 1e-6:
        raise RiskError(
            f"buy ${n:.2f} exceeds cap+sleeve P&L tradable ${tradable:.2f}"
        )


def scaled_order_cap(tradable: float, cfg) -> float:
    """Never allow more than cap+sleeve P&L (tradable) into a single order."""
    hard = getattr(cfg.risk, "max_order_notional", None)
    pct_cap = float(tradable) * float(cfg.risk.max_order_notional_pct)
    if hard is not None:
        return min(float(tradable), float(hard), pct_cap)
    return min(float(tradable), pct_cap)
