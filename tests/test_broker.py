from __future__ import annotations

import pytest

from broker_alpaca import (
    LIVE_TRADING_MESSAGE,
    PAPER_BASE_URL,
    AlpacaBroker,
    LiveTradingNotApprovedError,
    RetryableError,
    assert_paper_only,
    is_paper_base_url,
    make_client_order_id,
    with_backoff,
)


def test_paper_url_accepted():
    assert is_paper_base_url(PAPER_BASE_URL)
    assert is_paper_base_url(PAPER_BASE_URL + "/")
    assert assert_paper_only(PAPER_BASE_URL) == PAPER_BASE_URL


def test_live_url_rejected():
    with pytest.raises(LiveTradingNotApprovedError, match="explicit owner approval"):
        assert_paper_only("https://api.alpaca.markets")
    with pytest.raises(LiveTradingNotApprovedError, match="explicit owner approval"):
        assert_paper_only("https://api.alpaca.markets/")


def test_empty_or_custom_url_rejected():
    with pytest.raises(LiveTradingNotApprovedError):
        assert_paper_only("")
    with pytest.raises(LiveTradingNotApprovedError):
        assert_paper_only("https://evil.example/paper")


def test_broker_constructor_refuses_live_url():
    with pytest.raises(LiveTradingNotApprovedError) as exc:
        AlpacaBroker("key", "secret", base_url="https://api.alpaca.markets")
    assert str(exc.value) == LIVE_TRADING_MESSAGE


def test_from_env_refuses_live_base_url():
    with pytest.raises(LiveTradingNotApprovedError):
        AlpacaBroker.from_env(
            {
                "APCA_API_KEY_ID": "key",
                "APCA_API_SECRET_KEY": "secret",
                "APCA_API_BASE_URL": "https://api.alpaca.markets",
            }
        )


def test_client_order_id_is_idempotent_and_short():
    a = make_client_order_id("2026-09-30", "VTI", "buy", 1234.5)
    b = make_client_order_id("2026-09-30", "VTI", "buy", 1234.50)
    c = make_client_order_id("2026-09-30", "VTI", "sell", 1234.5)
    assert a == b
    assert a != c
    assert len(a) <= 48


def test_backoff_retries_then_succeeds():
    n = {"i": 0}

    def flaky():
        n["i"] += 1
        if n["i"] < 3:
            raise RetryableError("429")
        return "ok"

    slept = []
    assert with_backoff(flaky, sleep=slept.append) == "ok"
    assert n["i"] == 3
    assert slept == [0.5, 1.0]
