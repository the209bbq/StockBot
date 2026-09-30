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


def bot_positions_only(positions: dict[str, Position], symbols: list[str]) -> dict[str, Position]:
    return {s: positions[s] for s in symbols if s in positions}


def cash_buffer_dollars(cfg: Config, equity: float) -> float:
    if cfg.cash_buffer_usd and cfg.cash_buffer_usd > 0:
        return min(float(cfg.cash_buffer_usd), max(0.0, equity))
    return max(0.0, equity * float(cfg.cash_buffer_pct))


def managed_capital(
    *,
    positions: dict[str, Position],
    account_cash: float,
    symbols: list[str],
    capital_cap_usd: float | None,
) -> tuple[float, float, float]:
    """Return (managed_equity, bot_cash, bot_market_value).

    Only VTI/VXUS/BND (the configured symbols) count. Surplus paper-account
    cash is ignored. On a flat sleeve we take up to `capital_cap_usd` from
    account cash so a $100k paper account still starts as a $100 bot.
    Once lots exist we never siphon more unmanaged cash; the sleeve can grow.
    """
    bot = bot_positions_only(positions, symbols)
    bot_mv = sum(p.market_value for p in bot.values())
    cash = max(0.0, float(account_cash))
    if capital_cap_usd is None:
        return bot_mv + cash, cash, bot_mv
    cap = float(capital_cap_usd)
    if bot_mv <= 1e-8:
        bot_cash = min(cap, cash)
        return bot_cash, bot_cash, 0.0
    unused_cap = max(0.0, cap - bot_mv)
    bot_cash = min(cash, unused_cap)
    return bot_mv + bot_cash, bot_cash, bot_mv


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

    Invests `equity - cash_buffer`. Legs smaller than
    `min_trade_notional` ($1 Alpaca fractional floor) are dropped.
    Extra symbols (not in the target mix) are sold if large enough.
    Sells are listed before buys so a later submit pass can free cash first.
    Orders are notional (fractional).
    """
    if equity <= 0:
        return []

    investable = max(0.0, equity - cash_buffer_dollars(cfg, equity))
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
