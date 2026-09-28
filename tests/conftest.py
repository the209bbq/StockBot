from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from broker_alpaca import MockBroker, demo_closes_above_sma
from config import load_config
from rebalance import Position
from store import Store


@pytest.fixture
def cfg():
    return load_config(Path(__file__).resolve().parents[1] / "config.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "trader.db")


@pytest.fixture
def mock_broker():
    return MockBroker()


@pytest.fixture
def on_target_positions():
    # 55 / 25 / 20 of the $99,000 investable book + $1,000 cash = $100,000 equity
    specs = {"VTI": (54_450.0, 220.0), "VXUS": (24_750.0, 62.5), "BND": (19_800.0, 80.0)}
    out = {}
    for symbol, (value, price) in specs.items():
        qty = value / price
        out[symbol] = Position(symbol, qty=qty, market_value=value, avg_price=price, current_price=price)
    return out


@pytest.fixture
def drifted_positions():
    specs = {"VTI": (70_000.0, 280.0), "VXUS": (15_000.0, 62.5), "BND": (14_000.0, 70.0)}
    out = {}
    for symbol, (value, price) in specs.items():
        qty = value / price
        out[symbol] = Position(symbol, qty=qty, market_value=value, avg_price=price, current_price=price)
    return out


@pytest.fixture
def closes_above_sma():
    return demo_closes_above_sma()


@pytest.fixture
def closes_below_sma():
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=250)
    # last print well below a 200-day average of prior higher prices
    values = [200.0] * 200 + [100.0] * 50
    return {"VTI": pd.Series(values, index=idx, name="VTI")}
