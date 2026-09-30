"""Thin Alpaca wrapper: paper-only, retries, idempotent client_order_id."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, TypeVar

import pandas as pd

from rebalance import AccountSnapshot, OrderIntent, Position

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_MESSAGE = (
    "Live trading needs explicit owner approval. "
    "This bot only accepts https://paper-api.alpaca.markets."
)

T = TypeVar("T")


class LiveTradingNotApprovedError(RuntimeError):
    """Raised on any attempt to use a live Alpaca endpoint or live mode."""


class RetryableError(RuntimeError):
    """Transient broker error that should be retried with backoff."""


def normalize_base_url(url: str | None) -> str:
    return (url or "").strip().rstrip("/")


def is_paper_base_url(url: str | None) -> bool:
    return normalize_base_url(url) == PAPER_BASE_URL


def assert_paper_only(base_url: str | None) -> str:
    """Refuse to run unless the base URL is the Alpaca paper endpoint."""
    normalized = normalize_base_url(base_url)
    if not is_paper_base_url(normalized):
        raise LiveTradingNotApprovedError(LIVE_TRADING_MESSAGE)
    return normalized


def with_backoff(
    fn: Callable[[], T],
    *,
    retries: int = 4,
    initial_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    retryable: tuple[type[BaseException], ...] = (RetryableError,),
) -> T:
    delay = initial_delay
    last: BaseException | None = None
    for attempt in range(retries):
        try:
            return fn()
        except retryable as exc:
            last = exc
            if attempt == retries - 1:
                raise
            sleep(delay)
            delay *= 2
    assert last is not None
    raise last


def make_client_order_id(as_of: date | str, symbol: str, side: str, notional: float) -> str:
    """Stable id so a retry of the same month-end batch is idempotent.

    Alpaca limits client_order_id to 48 characters.
    """
    day = as_of if isinstance(as_of, str) else as_of.isoformat()
    raw = f"{day}|{symbol.upper()}|{side.lower()}|{round(float(notional), 2):.2f}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    cid = f"reb-{day}-{symbol.upper()}-{side[0]}-{digest}"
    return cid[:48]


def _is_retryable_status(status: int | None) -> bool:
    return status in {408, 409, 425, 429, 500, 502, 503, 504}


class AlpacaBroker:
    """Paper-only Trading + Market Data wrapper.

    The live trading URL is rejected before the SDK client is constructed.
    `paper=True` and the paper base URL are forced even if a caller slips.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str = PAPER_BASE_URL,
        *,
        trading_client: Any | None = None,
        data_client: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        assert_paper_only(base_url)
        if not api_key or not secret_key:
            raise ValueError("APCA_API_KEY_ID and APCA_API_SECRET_KEY are required for the paper broker")
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = PAPER_BASE_URL
        self._sleep = sleep
        if trading_client is not None:
            self._trading = trading_client
        else:
            from alpaca.trading.client import TradingClient

            self._trading = TradingClient(
                api_key=api_key,
                secret_key=secret_key,
                paper=True,
                url_override=PAPER_BASE_URL,
            )
        if data_client is not None:
            self._data = data_client
        else:
            try:
                from alpaca.data.historical import StockHistoricalDataClient

                self._data = StockHistoricalDataClient(api_key, secret_key)
            except Exception:
                self._data = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AlpacaBroker":
        environ = env if env is not None else os.environ
        base = environ.get("APCA_API_BASE_URL", PAPER_BASE_URL)
        assert_paper_only(base)
        return cls(
            api_key=environ.get("APCA_API_KEY_ID", ""),
            secret_key=environ.get("APCA_API_SECRET_KEY", ""),
            base_url=base,
        )

    def get_account(self) -> AccountSnapshot:
        acct = with_backoff(lambda: self._trading.get_account(), sleep=self._sleep)
        cash = float(acct.cash)
        equity = float(acct.equity)
        buying_power = float(getattr(acct, "buying_power", cash) or cash)
        status = str(getattr(acct, "status", "ACTIVE"))
        return AccountSnapshot(cash=cash, equity=equity, buying_power=buying_power, status=status)

    def get_positions(self) -> dict[str, Position]:
        raw = with_backoff(lambda: self._trading.get_all_positions(), sleep=self._sleep)
        out: dict[str, Position] = {}
        for p in raw:
            symbol = str(p.symbol).upper()
            qty = float(p.qty)
            mv = float(p.market_value)
            avg = float(getattr(p, "avg_entry_price", 0) or 0)
            px = float(getattr(p, "current_price", 0) or 0)
            out[symbol] = Position(
                symbol=symbol,
                qty=qty,
                market_value=mv,
                avg_price=avg,
                current_price=px or (mv / qty if qty else 0.0),
            )
        return out

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        positions = {s: p.current_price for s, p in self.get_positions().items() if p.current_price}
        missing = [s for s in symbols if s not in positions or positions[s] <= 0]
        if not missing:
            return {s: positions[s] for s in symbols if s in positions}

        def _fetch() -> dict[str, float]:
            if self._data is None:
                raise RetryableError("market data client unavailable")
            from alpaca.data.requests import StockLatestTradeRequest

            req = StockLatestTradeRequest(symbol_or_symbols=missing)
            trades = self._data.get_stock_latest_trade(req)
            prices: dict[str, float] = {}
            for sym, trade in trades.items():
                prices[str(sym).upper()] = float(trade.price)
            return prices

        try:
            fetched = with_backoff(_fetch, sleep=self._sleep)
            positions.update(fetched)
        except Exception:
            # Last resort: last daily close.
            for sym in missing:
                closes = self.get_daily_closes(sym, limit=5)
                if not closes.empty:
                    positions[sym] = float(closes.iloc[-1])
        return {s: positions[s] for s in symbols if s in positions}

    def get_daily_closes(self, symbol: str, limit: int = 250) -> pd.Series:
        def _fetch() -> pd.Series:
            if self._data is None:
                raise RetryableError("market data client unavailable")
            from alpaca.data.requests import StockBarsRequest
            from alpaca.data.timeframe import TimeFrame

            end = datetime.now(timezone.utc)
            start = end - timedelta(days=int(limit * 2.2) + 10)
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                limit=limit + 50,
            )
            bars = self._data.get_stock_bars(req).df
            if bars is None or bars.empty:
                return pd.Series(dtype=float, name=symbol)
            if isinstance(bars.index, pd.MultiIndex):
                closes = bars.xs(symbol, level=0)["close"] if symbol in bars.index.get_level_values(0) else bars["close"]
            else:
                closes = bars["close"]
            closes = closes.dropna()
            closes.name = symbol
            return closes.tail(limit)

        try:
            return with_backoff(_fetch, sleep=self._sleep)
        except Exception:
            return pd.Series(dtype=float, name=symbol)

    def submit_order(self, intent: OrderIntent, *, client_order_id: str) -> dict[str, Any]:
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        side = OrderSide.BUY if intent.side.lower() == "buy" else OrderSide.SELL
        kwargs: dict[str, Any] = {
            "symbol": intent.symbol,
            "side": side,
            "time_in_force": TimeInForce.DAY,
            "client_order_id": client_order_id,
        }
        if intent.close_position and intent.qty:
            kwargs["qty"] = intent.qty
        else:
            kwargs["notional"] = round(intent.notional, 2)

        def _submit() -> Any:
            req = MarketOrderRequest(**kwargs)
            try:
                return self._trading.submit_order(order_data=req)
            except APIError as exc:
                status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                msg = str(exc).lower()
                if "client_order_id" in msg and ("already" in msg or "exists" in msg or "duplicate" in msg):
                    return self._trading.get_order_by_client_id(client_order_id)
                if _is_retryable_status(int(status) if status is not None else None):
                    raise RetryableError(str(exc)) from exc
                raise

        order = with_backoff(_submit, sleep=self._sleep)
        return _order_to_dict(order, client_order_id)

    def cancel_order(self, order_id: str) -> None:
        def _cancel() -> None:
            self._trading.cancel_order_by_id(order_id)

        with_backoff(_cancel, sleep=self._sleep)

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        try:
            order = with_backoff(
                lambda: self._trading.get_order_by_client_id(client_order_id),
                sleep=self._sleep,
            )
        except Exception:
            return None
        return _order_to_dict(order, client_order_id)


def _order_to_dict(order: Any, client_order_id: str) -> dict[str, Any]:
    return {
        "id": str(getattr(order, "id", "")),
        "client_order_id": str(getattr(order, "client_order_id", client_order_id)),
        "symbol": str(getattr(order, "symbol", "")),
        "side": str(getattr(order, "side", "")),
        "notional": getattr(order, "notional", None),
        "qty": getattr(order, "qty", None),
        "status": str(getattr(order, "status", "")),
        "filled_qty": getattr(order, "filled_qty", None),
        "filled_avg_price": getattr(order, "filled_avg_price", None),
    }


@dataclass
class MockBroker:
    """In-memory broker used for --dry-run without keys and for offline tests."""

    account: AccountSnapshot
    positions: dict[str, Position]
    prices: dict[str, float]
    daily_closes: dict[str, pd.Series]
    submitted: list[dict[str, Any]]
    canceled: list[str]

    def __init__(
        self,
        *,
        cash: float = 1_000.0,
        equity: float | None = None,
        positions: dict[str, Position] | None = None,
        prices: dict[str, float] | None = None,
        daily_closes: dict[str, pd.Series] | None = None,
    ) -> None:
        self.positions = drifted_demo_positions() if positions is None else positions
        self.prices = prices or {s: p.current_price for s, p in self.positions.items()}
        pos_value = sum(p.market_value for p in self.positions.values())
        self.account = AccountSnapshot(
            cash=cash,
            equity=equity if equity is not None else pos_value + cash,
            buying_power=cash + pos_value,
        )
        self.daily_closes = daily_closes or demo_closes_above_sma()
        self.submitted = []
        self.canceled = []

    def get_account(self) -> AccountSnapshot:
        return self.account

    def get_positions(self) -> dict[str, Position]:
        return dict(self.positions)

    def get_latest_prices(self, symbols: list[str]) -> dict[str, float]:
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def get_daily_closes(self, symbol: str, limit: int = 250) -> pd.Series:
        series = self.daily_closes.get(symbol, pd.Series(dtype=float, name=symbol))
        return series.tail(limit)

    def submit_order(self, intent: OrderIntent, *, client_order_id: str) -> dict[str, Any]:
        record = {
            "id": f"mock-{len(self.submitted) + 1}",
            "client_order_id": client_order_id,
            "symbol": intent.symbol,
            "side": intent.side,
            "notional": intent.notional,
            "qty": intent.qty,
            "status": "accepted",
        }
        self.submitted.append(record)
        return record

    def cancel_order(self, order_id: str) -> None:
        self.canceled.append(order_id)

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        for rec in self.submitted:
            if rec["client_order_id"] == client_order_id:
                return rec
        return None


def drifted_demo_positions() -> dict[str, Position]:
    """~70/15/15 mix so a dry-run against the 55/25/20 target prints orders."""
    specs = {
        "VTI": (250.0, 280.0),
        "VXUS": (240.0, 62.50),
        "BND": (225.0, 66.67),
    }
    out: dict[str, Position] = {}
    for symbol, (qty, price) in specs.items():
        out[symbol] = Position(
            symbol=symbol,
            qty=qty,
            market_value=qty * price,
            avg_price=price,
            current_price=price,
        )
    return out


def demo_closes_above_sma(symbol: str = "VTI", days: int = 250, last: float = 280.0) -> dict[str, pd.Series]:
    """Synthetic uptrend so the default dry-run stays in risk-on mode."""
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    values = [last * (0.85 + 0.15 * (i / (days - 1))) for i in range(days)]
    series = pd.Series(values, index=idx, name=symbol)
    return {symbol: series}
