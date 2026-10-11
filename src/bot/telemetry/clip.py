"""Size limits for what a new dashboard client receives.

Ported from jev-trade's snapshot.ts (MIT). A first paint needs only a short tail of
decisions, a little candle history and the latest fills; the rest streams in or is
fetched from `/tape`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

from bot.telemetry.wire import PricePoint

T = TypeVar("T")

# First paint (/snapshot and the first SSE event).
SNAPSHOT_HISTORY = 12
SNAPSHOT_SECS = 900
SNAPSHOT_MIDS = 400
SNAPSHOT_FILLS = 12
# Full tape (/tape). Older time is covered by 15 minute bars.
TAPE_SECS = 900
TAPE_MIDS = 5000


def _tail(rows: Sequence[T], n: int) -> list[T]:
    return list(rows[-n:]) if n > 0 and len(rows) > n else list(rows)


def _by_time(point: PricePoint) -> tuple[int, int]:
    return (point.ts, 1 if point.fill else 0)


def _split(points: Sequence[PricePoint]) -> tuple[list[PricePoint], list[PricePoint], list[PricePoint], list[PricePoint]]:
    seconds: list[PricePoint] = []
    minutes: list[PricePoint] = []
    quarters: list[PricePoint] = []
    fills: list[PricePoint] = []
    for point in points:
        if point.fill:
            fills.append(point)
        elif point.bar == "1s":
            seconds.append(point)
        elif point.bar == "15m":
            quarters.append(point)
        else:
            minutes.append(point)
    return seconds, minutes, quarters, fills


def clip_history(events: Sequence[T], n: int = SNAPSHOT_HISTORY) -> list[T]:
    return _tail(events, n)


def clip_tape(points: Sequence[PricePoint], max_mids: int = TAPE_MIDS, max_secs: int = TAPE_SECS) -> list[PricePoint]:
    """Keep every fill and 15 minute bar. Cap the 1 minute and 1 second bars separately."""
    seconds, minutes, quarters, fills = _split(points)
    if len(seconds) <= max_secs and len(minutes) <= max_mids:
        return list(points)
    return sorted(quarters + _tail(minutes, max_mids) + _tail(seconds, max_secs) + fills, key=_by_time)


def clip_snapshot_tape(
    points: Sequence[PricePoint],
    max_mids: int = SNAPSHOT_MIDS,
    max_fills: int = SNAPSHOT_FILLS,
    max_secs: int = SNAPSHOT_SECS,
) -> list[PricePoint]:
    """First paint: a 1 minute tail, a 1 second tail and the latest fills. No 15 minute bars."""
    seconds, minutes, _quarters, fills = _split(points)
    return sorted(_tail(minutes, max_mids) + _tail(seconds, max_secs) + _tail(fills, max_fills), key=_by_time)
