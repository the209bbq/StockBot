from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from market_clock import ET, evaluate_session, now_et, session_close_et


def test_now_et_converts_utc():
    utc = datetime(2026, 1, 15, 20, 50, tzinfo=ZoneInfo("UTC"))  # EST
    assert now_et(utc).hour == 15
    utc_edt = datetime(2026, 7, 15, 19, 50, tzinfo=ZoneInfo("UTC"))
    assert now_et(utc_edt).hour == 15


def test_wrong_dst_hour_is_outside_window():
    # 20:50 UTC in July is 16:50 EDT — past the 15:50-16:10 window
    info = evaluate_session(
        as_of=date(2026, 7, 15),
        now=datetime(2026, 7, 15, 20, 50, tzinfo=ZoneInfo("UTC")),
        calendar_row=object(),
        calendar_available=True,
    )
    assert info.skip_reason == "outside_near_close_window"


def test_session_close_from_alpaca_datetime():
    close = datetime(2026, 11, 27, 18, 0, tzinfo=ZoneInfo("UTC"))  # 13:00 ET
    assert session_close_et(close) == time(13, 0)


def test_early_close_evaluates_in_pre_close_window():
    info = evaluate_session(
        as_of=date(2026, 11, 27),
        now=datetime(2026, 11, 27, 12, 50, tzinfo=ET),
        calendar_close=time(13, 0),
        calendar_row=object(),
        calendar_available=True,
    )
    assert info.is_early_close
    assert info.early_close_policy == "evaluate_before_close"
    assert info.skip_reason is None


def test_early_close_skips_at_regular_1550():
    info = evaluate_session(
        as_of=date(2026, 11, 27),
        now=datetime(2026, 11, 27, 15, 50, tzinfo=ET),
        calendar_close=time(13, 0),
        calendar_row=object(),
        calendar_available=True,
    )
    assert info.is_early_close
    assert info.early_close_policy == "skip"
    assert info.skip_reason == "early_close"
