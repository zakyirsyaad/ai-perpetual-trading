"""One ISO-8601 timestamp parser for Supabase rows (Python 3.10 and 3.11 alike).

Research code; the bot never imports it. Standard library only.

``datetime.fromisoformat`` accepts more on Python 3.11 than on 3.10 (``Z``,
fraction lengths other than 3 or 6, offsets without a colon), so the text is
normalised first: ``Z``/``z`` and a missing offset mean UTC, ``+HH``, ``+HHMM``
and ``+HH:MM`` are offsets, and any number of fraction digits is cut or padded
to microseconds.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

_ISO = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}(?::?\d{2})?)?$"
)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _offset(text: Optional[str]) -> str:
    if text is None or text in ("Z", "z"):
        return "+00:00"
    if ":" in text:
        return text
    return text[:3] + ":" + (text[3:] or "00")


def parse_iso_datetime(value: Any) -> Optional[datetime]:
    """A timezone-aware datetime, or ``None`` for anything that is not such a timestamp."""
    if not isinstance(value, str):
        return None
    m = _ISO.match(value.strip())
    if m is None:
        return None
    day, clock, frac, offset = m.groups()
    fraction = (frac or "0")[:6].ljust(6, "0")
    try:
        return datetime.fromisoformat(f"{day}T{clock}.{fraction}{_offset(offset)}")
    except ValueError:
        return None


def parse_iso_ms(value: Any) -> Optional[int]:
    """UTC epoch milliseconds of an ISO-8601 timestamp, or ``None``."""
    dt = parse_iso_datetime(value)
    if dt is None:
        return None
    delta = dt - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
