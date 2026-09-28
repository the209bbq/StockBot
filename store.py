"""SQLite persistence: orders, fills, targets, equity, buy-and-hold benchmark."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from rebalance import Position

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    client_order_id TEXT UNIQUE NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    notional REAL,
    qty REAL,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    dry_run INTEGER NOT NULL DEFAULT 1,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY,
    client_order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL,
    price REAL,
    filled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS target_weights (
    id INTEGER PRIMARY KEY,
    as_of TEXT NOT NULL,
    weights_json TEXT NOT NULL,
    risk_on INTEGER NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY,
    as_of TEXT UNIQUE NOT NULL,
    equity REAL NOT NULL,
    cash REAL NOT NULL,
    positions_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_snapshots (
    id INTEGER PRIMARY KEY,
    as_of TEXT UNIQUE NOT NULL,
    equity REAL NOT NULL,
    lots_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


@dataclass(frozen=True)
class EquitySnapshot:
    as_of: str
    equity: float
    cash: float
    positions: dict[str, Any]


@dataclass(frozen=True)
class BenchmarkSnapshot:
    as_of: str
    equity: float
    lots: dict[str, float]


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM strategy_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO strategy_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_risk_on(self, default: bool = True) -> bool:
        raw = self.get_state("risk_on")
        if raw is None:
            return default
        return raw.lower() in {"1", "true", "yes"}

    def set_risk_on(self, risk_on: bool) -> None:
        self.set_state("risk_on", "true" if risk_on else "false")

    def last_positions(self) -> dict[str, Position]:
        row = self._conn.execute(
            "SELECT positions_json FROM equity_snapshots ORDER BY as_of DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {}
        raw = json.loads(row["positions_json"])
        out: dict[str, Position] = {}
        for symbol, rec in raw.items():
            out[symbol] = Position(
                symbol=symbol,
                qty=float(rec.get("qty", 0)),
                market_value=float(rec.get("market_value", 0)),
                avg_price=float(rec.get("avg_price", 0)),
                current_price=float(rec.get("current_price", 0)),
            )
        return out

    def record_target(self, as_of: str, weights: dict[str, float], risk_on: bool, reason: str) -> None:
        self._conn.execute(
            "INSERT INTO target_weights(as_of, weights_json, risk_on, reason) VALUES(?,?,?,?)",
            (as_of, _json(weights), 1 if risk_on else 0, reason),
        )
        self._conn.commit()

    def record_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        notional: float | None,
        qty: float | None,
        status: str,
        dry_run: bool,
        raw: dict | None = None,
        submitted_at: str | None = None,
    ) -> None:
        ts = submitted_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self._conn.execute(
            "INSERT OR REPLACE INTO orders(client_order_id, symbol, side, notional, qty, status, "
            "submitted_at, dry_run, raw_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                client_order_id,
                symbol,
                side,
                notional,
                qty,
                status,
                ts,
                1 if dry_run else 0,
                _json(raw or {}),
            ),
        )
        self._conn.commit()

    def record_fill(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        filled_at: str | None = None,
    ) -> None:
        ts = filled_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self._conn.execute(
            "INSERT INTO fills(client_order_id, symbol, side, qty, price, filled_at) VALUES(?,?,?,?,?,?)",
            (client_order_id, symbol, side, qty, price, ts),
        )
        self._conn.commit()

    def turnover_today(self, as_of: str) -> float:
        rows = self._conn.execute(
            "SELECT notional FROM orders WHERE submitted_at LIKE ? AND dry_run = 0",
            (f"{as_of}%",),
        ).fetchall()
        return sum(abs(float(r["notional"] or 0)) for r in rows)

    def snapshot_equity(
        self,
        as_of: str,
        equity: float,
        cash: float,
        positions: dict[str, Position],
    ) -> None:
        payload = {
            s: {
                "qty": p.qty,
                "market_value": p.market_value,
                "avg_price": p.avg_price,
                "current_price": p.current_price,
            }
            for s, p in positions.items()
        }
        self._conn.execute(
            "INSERT INTO equity_snapshots(as_of, equity, cash, positions_json) VALUES(?,?,?,?) "
            "ON CONFLICT(as_of) DO UPDATE SET equity=excluded.equity, cash=excluded.cash, "
            "positions_json=excluded.positions_json",
            (as_of, equity, cash, _json(payload)),
        )
        self._conn.commit()

    def equity_history(self) -> list[EquitySnapshot]:
        rows = self._conn.execute(
            "SELECT as_of, equity, cash, positions_json FROM equity_snapshots ORDER BY as_of"
        ).fetchall()
        return [
            EquitySnapshot(r["as_of"], float(r["equity"]), float(r["cash"]), json.loads(r["positions_json"]))
            for r in rows
        ]

    def init_benchmark_if_needed(
        self,
        as_of: str,
        start_equity: float,
        prices: dict[str, float],
        target_weights: dict[str, float],
    ) -> dict[str, float]:
        existing = self.get_state("benchmark_lots")
        if existing:
            return {k: float(v) for k, v in json.loads(existing).items()}
        lots = {}
        for symbol, weight in target_weights.items():
            px = prices.get(symbol)
            if not px:
                continue
            lots[symbol] = (start_equity * weight) / px
        self.set_state("benchmark_lots", _json(lots))
        self.set_state("benchmark_start", as_of)
        self.set_state("benchmark_start_equity", str(start_equity))
        self.snapshot_benchmark(as_of, start_equity, lots)
        return lots

    def snapshot_benchmark(self, as_of: str, equity: float, lots: dict[str, float]) -> None:
        self._conn.execute(
            "INSERT INTO benchmark_snapshots(as_of, equity, lots_json) VALUES(?,?,?) "
            "ON CONFLICT(as_of) DO UPDATE SET equity=excluded.equity, lots_json=excluded.lots_json",
            (as_of, equity, _json(lots)),
        )
        self._conn.commit()

    def mark_benchmark(self, as_of: str, prices: dict[str, float]) -> float | None:
        raw = self.get_state("benchmark_lots")
        if not raw:
            return None
        lots = {k: float(v) for k, v in json.loads(raw).items()}
        value = 0.0
        for symbol, shares in lots.items():
            if symbol not in prices:
                return None
            value += shares * prices[symbol]
        self.snapshot_benchmark(as_of, value, lots)
        return value

    def benchmark_history(self) -> list[BenchmarkSnapshot]:
        rows = self._conn.execute(
            "SELECT as_of, equity, lots_json FROM benchmark_snapshots ORDER BY as_of"
        ).fetchall()
        return [
            BenchmarkSnapshot(r["as_of"], float(r["equity"]), json.loads(r["lots_json"])) for r in rows
        ]

    def inception_date(self) -> str | None:
        return self.get_state("benchmark_start")


def today_iso(as_of: date | datetime | str | None = None) -> str:
    if as_of is None:
        return date.today().isoformat()
    if isinstance(as_of, str):
        return as_of[:10]
    return as_of.date().isoformat() if isinstance(as_of, datetime) else as_of.isoformat()
