"""Turn current vs target weights into a list of order intents."""

from __future__ import annotations

from dataclasses import dataclass, field

from config import Config


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    market_value: float
    avg_price: float = 0.0
    current_price: float = 0.0


@dataclass(frozen=True)
class AccountSnapshot:
    cash: float
    equity: float
    buying_power: float = 0.0
    status: str = "ACTIVE"


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str  # buy | sell
    notional: float
    qty: float | None = None
    close_position: bool = False
    reason: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "notional": round(self.notional, 2),
            "qty": None if self.qty is None else round(self.qty, 6),
            "close_position": self.close_position,
            "reason": self.reason,
        }


def portfolio_weights(
    positions: dict[str, Position],
    cash: float,
    equity: float | None = None,
) -> dict[str, float]:
    total = equity if equity is not None else (sum(p.market_value for p in positions.values()) + cash)
    if total <= 0:
        return {s: 0.0 for s in positions}
    weights = {s: p.market_value / total for s, p in positions.items()}
    return weights


def plan_orders(
    *,
    positions: dict[str, Position],
    cash: float,
    equity: float,
    target_weights: dict[str, float],
    prices: dict[str, float],
    cfg: Config,
    reason: str = "",
) -> list[OrderIntent]:
    """Diff current holdings against target weights.

    Invests `equity * (1 - cash_buffer_pct)`. Legs smaller than
    `min_trade_notional` are dropped so tiny drifts do not trade.
    Extra symbols (not in the target mix) are sold if large enough.
    Sells are listed before buys so a later submit pass can free cash first.
    """
    if equity <= 0:
        return []

    investable = equity * (1.0 - cfg.cash_buffer_pct)
    desired = {s: w * investable for s, w in target_weights.items()}

    current_value = {s: positions[s].market_value for s in positions}
    extras = [s for s in current_value if s not in target_weights]

    intents: list[OrderIntent] = []

    for symbol in extras:
        value = current_value[symbol]
        if value < cfg.min_trade_notional:
            continue
        pos = positions[symbol]
        intents.append(
            OrderIntent(
                symbol=symbol,
                side="sell",
                notional=round(value, 2),
                qty=pos.qty,
                close_position=True,
                reason=reason or "sell_non_target",
            )
        )

    diffs: list[tuple[str, float]] = []
    for symbol, want in desired.items():
        have = current_value.get(symbol, 0.0)
        diffs.append((symbol, want - have))

    def _intent(symbol: str, diff: float) -> OrderIntent | None:
        if abs(diff) < cfg.min_trade_notional:
            return None
        side = "buy" if diff > 0 else "sell"
        notional = abs(diff)
        pos = positions.get(symbol)
        price = prices.get(symbol) or (pos.current_price if pos else 0.0)
        qty = (notional / price) if price else None
        close = False
        if side == "sell" and pos is not None and notional >= pos.market_value * 0.995:
            qty = pos.qty
            notional = pos.market_value
            close = True
        return OrderIntent(
            symbol=symbol,
            side=side,
            notional=round(notional, 2),
            qty=None if qty is None else round(qty, 6),
            close_position=close,
            reason=reason or "rebalance",
        )

    sells = []
    buys = []
    for symbol, diff in diffs:
        intent = _intent(symbol, diff)
        if intent is None:
            continue
        (buys if intent.side == "buy" else sells).append(intent)

    return sells + intents + buys


def reconcile(
    broker_positions: dict[str, Position],
    stored_positions: dict[str, Position],
    qty_tol: float = 1e-4,
) -> list[str]:
    """Compare last stored lots to the broker. Broker is always source of truth."""
    mismatches: list[str] = []
    symbols = set(broker_positions) | set(stored_positions)
    for symbol in sorted(symbols):
        bq = broker_positions[symbol].qty if symbol in broker_positions else 0.0
        sq = stored_positions[symbol].qty if symbol in stored_positions else 0.0
        if abs(bq - sq) > qty_tol:
            mismatches.append(f"{symbol}: broker={bq} stored={sq}")
    return mismatches
