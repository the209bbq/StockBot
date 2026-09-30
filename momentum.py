"""QQQ close-to-close Donchian breakout with a trailing stop and time stop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from config import Config, QQQMomentumConfig
from rebalance import OrderIntent


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    qty: float
    entry_date: str
    entry_price: float
    high_close: float


@dataclass(frozen=True)
class MomentumDecision:
    action: str  # hold_flat | buy | sell_trail | sell_time | hold_long | skip
    reason: str
    symbol: str
    close: float | None
    prior_high: float | None
    trail_high: float | None
    days_held: int | None
    tradable: float
    notional: float
    qty: float | None
    order: OrderIntent | None = None


def prior_n_high(closes: pd.Series, lookback: int, *, exclude_last: bool = True) -> float | None:
    """Highest close of the prior `lookback` sessions (excludes today's bar by default)."""
    if closes is None or closes.empty:
        return None
    hist = closes.iloc[:-1] if exclude_last and len(closes) > 1 else closes
    window = hist.tail(lookback)
    if len(window) < lookback:
        return None
    return float(window.max())


def trading_days_held(entry: date | str, as_of: date | str, calendar: list[date] | None = None) -> int:
    start = date.fromisoformat(entry) if isinstance(entry, str) else entry
    end = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of
    if calendar:
        days = [d for d in calendar if start < d <= end]
        return len(days)
    # fallback: business days exclusive of entry
    bdays = pd.bdate_range(start + pd.Timedelta(days=1), end)
    return int(len(bdays))


def tradable_cash(*, capital_cap_usd: float, realized_pnl: float) -> float:
    """Cap plus realized sleeve P&L so wins compound. Never goes negative for sizing."""
    return max(0.0, float(capital_cap_usd) + float(realized_pnl))


def decide(
    *,
    closes: pd.Series,
    as_of: date | str,
    position: OpenPosition | None,
    tradable: float,
    cfg: QQQMomentumConfig,
    min_trade_notional: float = 1.0,
    calendar: list[date] | None = None,
) -> MomentumDecision:
    """One daily decision using today's last print as the proxy close."""
    symbol = cfg.symbol
    if closes is None or closes.empty:
        return MomentumDecision(
            action="skip",
            reason="no_price_data",
            symbol=symbol,
            close=None,
            prior_high=None,
            trail_high=None,
            days_held=None,
            tradable=tradable,
            notional=0.0,
            qty=None,
        )
    close = float(closes.iloc[-1])
    prior = prior_n_high(closes, cfg.lookback_days, exclude_last=True)

    if position is None:
        if prior is None:
            return MomentumDecision(
                "skip", "insufficient_history", symbol, close, prior, None, None, tradable, 0.0, None
            )
        if close > prior:
            notional = round(min(tradable, tradable), 2)
            if notional < min_trade_notional:
                return MomentumDecision(
                    "hold_flat",
                    "below_min_trade",
                    symbol,
                    close,
                    prior,
                    None,
                    None,
                    tradable,
                    0.0,
                    None,
                )
            qty = notional / close if close else None
            order = OrderIntent(
                symbol=symbol,
                side="buy",
                notional=notional,
                qty=None if qty is None else round(qty, 6),
                reason="breakout_buy",
            )
            return MomentumDecision(
                "buy",
                "close_above_prior_20d_high",
                symbol,
                close,
                prior,
                None,
                None,
                tradable,
                notional,
                order.qty,
                order,
            )
        return MomentumDecision(
            "hold_flat",
            "no_breakout",
            symbol,
            close,
            prior,
            None,
            None,
            tradable,
            0.0,
            None,
        )

    days = trading_days_held(position.entry_date, as_of, calendar)
    trail_high = max(float(position.high_close), close)
    stop = float(position.high_close) * (1.0 - cfg.trail_pct)
    qty = float(position.qty)
    notional = round(qty * close, 2)

    if close <= stop:
        order = OrderIntent(
            symbol=symbol,
            side="sell",
            notional=notional,
            qty=qty,
            close_position=True,
            reason="trailing_stop",
        )
        return MomentumDecision(
            "sell_trail",
            "close_below_trail",
            symbol,
            close,
            prior,
            float(position.high_close),
            days,
            tradable,
            notional,
            qty,
            order,
        )
    if days >= cfg.max_hold_days:
        order = OrderIntent(
            symbol=symbol,
            side="sell",
            notional=notional,
            qty=qty,
            close_position=True,
            reason="time_stop",
        )
        return MomentumDecision(
            "sell_time",
            "max_hold_days",
            symbol,
            close,
            prior,
            trail_high,
            days,
            tradable,
            notional,
            qty,
            order,
        )
    return MomentumDecision(
        "hold_long",
        "in_position",
        symbol,
        close,
        prior,
        trail_high,
        days,
        tradable,
        0.0,
        qty,
    )


def sleeve_mark(*, position: OpenPosition | None, close: float, cash: float) -> float:
    if position is None:
        return cash
    return cash + float(position.qty) * close
