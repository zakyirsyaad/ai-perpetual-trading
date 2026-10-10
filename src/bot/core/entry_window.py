"""Weekly window in which the bot may open new entries.

The operator trades weekdays only (Monday 07:00 to Friday 24:00 in the trading
timezone), so the window is one start and one end inside a single Monday-based
week. Positions already open are never touched by it: it only gates the decision
to open, and an empty setting means "always open".
"""
from __future__ import annotations

import re
from datetime import datetime

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY

WeeklyWindow = tuple[int, int]

_PART = r"(mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2}):(\d{2})"
_PATTERN = re.compile(rf"^\s*{_PART}\s*-\s*{_PART}\s*$", re.IGNORECASE)


def _minute_of_week(day: str, hour: int, minute: int) -> int:
    if minute > 59 or hour > 24 or (hour == 24 and minute != 0):
        raise ValueError(f"{day} {hour:02d}:{minute:02d} is not a valid time (hours 0-24, 24 only as 24:00).")
    return DAYS.index(day.lower()) * MINUTES_PER_DAY + hour * 60 + minute


def parse_weekly_window(text: str | None) -> WeeklyWindow | None:
    """`"mon 07:00-sat 00:00"` -> (start, end) in minutes since Monday 00:00; empty -> None."""
    if text is None or not str(text).strip():
        return None
    match = _PATTERN.match(str(text))
    if match is None:
        raise ValueError(f"Weekly window {text!r} must look like 'mon 07:00-sat 00:00'.")
    d1, h1, m1, d2, h2, m2 = match.groups()
    start = _minute_of_week(d1, int(h1), int(m1))
    end = _minute_of_week(d2, int(h2), int(m2))
    if start >= end:
        raise ValueError(f"Weekly window {text!r} must end after it starts within the same Monday-Sunday week.")
    return start, end


def _format_minute(total: int) -> str:
    day, rest = divmod(total, MINUTES_PER_DAY)
    if day == 7:  # the very end of the week
        return "sun 24:00"
    return f"{DAYS[day]} {rest // 60:02d}:{rest % 60:02d}"


def format_weekly_window(window: WeeklyWindow | None) -> str:
    if window is None:
        return ""
    return f"{_format_minute(window[0])}-{_format_minute(window[1])}"


def window_is_open(window: WeeklyWindow | None, moment: datetime) -> bool:
    """`moment` must already be expressed in the trading timezone."""
    if window is None:
        return True
    minute = moment.weekday() * MINUTES_PER_DAY + moment.hour * 60 + moment.minute
    return window[0] <= minute < window[1]
