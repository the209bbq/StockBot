"""Regular-session timing: 3:50pm ET, DST-safe, skip holidays and early closes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REGULAR_CLOSE = time(16, 0)


@dataclass(frozen=True)
class SessionInfo:
    as_of: str
    now_et: str
    is_trading_day: bool
    is_early_close: bool
    regular_session: bool
    near_close: bool
    next_close_et: str | None
    skip_reason: str | None
    early_close_policy: str | None = None  # evaluate_before_close | skip | None


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def now_et(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(tz=ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc).astimezone(ET)
    return now.astimezone(ET)


def session_close_et(raw: Any) -> time | None:
    if raw is None:
        return None
    if isinstance(raw, time):
        return raw
    if isinstance(raw, datetime):
        return raw.astimezone(ET).time().replace(second=0, microsecond=0)
    text = str(raw)
    if "T" in text or " " in text:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.astimezone(ET).time().replace(second=0, microsecond=0)
        except ValueError:
            pass
    if ":" in text:
        parts = text.split(":")
        return time(int(parts[0]), int(parts[1]))
    return None


def evaluate_session(
    *,
    as_of: date,
    evaluate_et: str = "15:50",
    window_minutes: int = 20,
    skip_early_closes: bool = True,
    now: datetime | None = None,
    is_open: bool | None = None,
    calendar_close: time | None = None,
    calendar_row: Any | None = None,
    calendar_available: bool = False,
    force: bool = False,
) -> SessionInfo:
    """Decide whether this is a valid near-close evaluation.

    DST is handled by using America/New_York. A scheduler should fire at both
    19:50 UTC (EDT) and 20:50 UTC (EST); this function rejects the wrong hour.
    Alpaca's calendar is the source of truth for holidays; `is_open` is not,
    because the clock is False after the regular close on a normal session.
    """
    current = now_et(now)
    target = _parse_hhmm(evaluate_et)
    close_t = session_close_et(calendar_close)
    if close_t is None and calendar_row is not None:
        close_t = session_close_et(getattr(calendar_row, "close", None))
    if close_t is None:
        close_t = REGULAR_CLOSE

    if calendar_row is not None:
        is_trading_day = True
    elif calendar_available:
        is_trading_day = False
    elif is_open is True:
        is_trading_day = True
    else:
        is_trading_day = as_of.weekday() < 5
    early = close_t < time(15, 30)
    window_start = datetime.combine(current.date(), target, tzinfo=ET)
    window_end = window_start + timedelta(minutes=window_minutes)
    near = window_start <= current <= window_end

    early_policy = None
    if early:
        close_dt = datetime.combine(current.date(), close_t, tzinfo=ET)
        early_start = close_dt - timedelta(minutes=max(window_minutes, 10))
        if early_start <= current <= close_dt:
            early_policy = "evaluate_before_close"
            near = True
        else:
            early_policy = "skip"

    skip = None
    if force:
        skip = None
    elif not is_trading_day:
        skip = "not_a_trading_day"
    elif early and early_policy == "evaluate_before_close":
        skip = None
    elif skip_early_closes and early:
        skip = "early_close"
    elif current.date() != as_of:
        # clock date can differ from as-of when we force a date
        pass
    elif not near:
        skip = "outside_near_close_window"

    return SessionInfo(
        as_of=as_of.isoformat(),
        now_et=current.isoformat(),
        is_trading_day=bool(is_trading_day),
        is_early_close=bool(early),
        regular_session=bool(is_trading_day and not early),
        near_close=bool(near or force),
        next_close_et=close_t.strftime("%H:%M"),
        skip_reason=None if force else skip,
        early_close_policy=early_policy,
    )
