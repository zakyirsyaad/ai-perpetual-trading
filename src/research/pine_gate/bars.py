"""Columnar OHLCV bars, UTC-aligned aggregation and WIB day files.

Research code; the bot never imports it. Standard library only.

``Bars`` holds one ``array`` per column and is treated as immutable: every
function returns new ``Bars`` and never edits arrays in place after
construction. Day files are gzip CSV, one WIB (UTC+7) calendar day each, with
header-less rows ``open_ms,open,high,low,close,volume``.
"""

from __future__ import annotations

import gzip
import math
import os
import re
import tempfile
import zlib
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from .fileio import write_exclusive
from .spec import WIB, ms

Row = Tuple[int, float, float, float, float, float]

_MS_PER_HOUR = 3_600_000
_MS_PER_DAY = 24 * _MS_PER_HOUR
_WIB_OFFSET_MS = 7 * _MS_PER_HOUR
_ROW_FIELDS = 6
_UNIX_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
# Venue-form USDT pairs only: the symbol becomes part of every data path.
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{2,20}-USDT")


class BarGapError(ValueError):
    """Bars are unsorted, duplicated or not spaced by exactly one interval."""


class MissingDayError(FileNotFoundError):
    """One or more day files of a requested range do not exist."""


class DayShapeError(ValueError):
    """A day file does not hold exactly its own WIB day of bars."""


@dataclass(frozen=True)
class Bars:
    interval_ms: int
    open_ms: array
    open: array
    high: array
    low: array
    close: array
    volume: array

    def __len__(self) -> int:
        return len(self.open_ms)


def _empty(interval_ms: int) -> Bars:
    return Bars(
        interval_ms,
        array("q"),
        array("d"),
        array("d"),
        array("d"),
        array("d"),
        array("d"),
    )


_UNIT_MS = {"m": 60_000, "h": _MS_PER_HOUR, "d": _MS_PER_DAY}


def interval_to_ms(interval: str) -> int:
    """Milliseconds of an interval label such as ``1m``, ``5m``, ``1h`` or ``4h``."""
    unit = interval[-1:]
    if unit not in _UNIT_MS or not interval[:-1].isdigit() or int(interval[:-1]) <= 0:
        raise ValueError(f"unsupported interval label: {interval!r}")
    return int(interval[:-1]) * _UNIT_MS[unit]


def _check_bar(t: int, o: float, h: float, low: float, c: float, v: float) -> None:
    """Finite values, positive prices, high/low bracketing open and close, volume >= 0."""
    if not all(math.isfinite(x) for x in (o, h, low, c, v)):
        raise ValueError(f"bar at {t} has a non-finite value")
    if min(o, h, low, c) <= 0.0:
        raise ValueError(f"bar at {t} has a non-positive price")
    if h < max(o, c) or low > min(o, c):
        raise ValueError(f"bar at {t}: high/low do not bracket open and close")
    if v < 0.0:
        raise ValueError(f"bar at {t} has a negative volume")


def _validated(rows: Iterable[Row], interval_ms: int) -> Iterator[Row]:
    """Each row as ``(int, float, ...)`` after ``_check_bar`` and the spacing check."""
    previous = None
    for row in rows:
        t = int(row[0])
        if previous is not None and t != previous + interval_ms:
            raise BarGapError(
                f"bar at {t} does not follow {previous} by {interval_ms} ms"
            )
        o, h, low, c, v = (float(x) for x in row[1:6])
        _check_bar(t, o, h, low, c, v)
        previous = t
        yield t, o, h, low, c, v


def check_rows(rows: Iterable[Row], interval_ms: int) -> None:
    """Raise exactly what ``bars_from_rows`` would, without building ``Bars``."""
    for _ in _validated(rows, interval_ms):
        pass


def bars_from_rows(rows: Iterable[Row], interval_ms: int) -> Bars:
    """Build ``Bars`` from rows sorted strictly ascending, spaced by ``interval_ms``.

    Every row is validated (``_check_bar``); a bad value raises ``ValueError`` and a
    gap, duplicate or disorder raises ``BarGapError``."""
    out = _empty(interval_ms)
    for t, o, h, low, c, v in _validated(rows, interval_ms):
        out.open_ms.append(t)
        out.open.append(o)
        out.high.append(h)
        out.low.append(low)
        out.close.append(c)
        out.volume.append(v)
    return out


def aggregate(bars: Bars, interval_ms: int) -> Bars:
    """Aggregate to a coarser interval on UTC-epoch aligned buckets.

    A bucket is emitted only when every source bar of it is present, so partial
    leading, trailing and interior-gap buckets are all dropped.
    """
    source = bars.interval_ms
    if interval_ms < source or interval_ms % source:
        raise ValueError(
            f"cannot aggregate {source} ms bars to {interval_ms} ms: "
            "target must be a multiple of the source interval"
        )
    per_bucket = interval_ms // source
    out = _empty(interval_ms)
    n = len(bars)
    i = 0
    while i < n:
        bucket = bars.open_ms[i] // interval_ms * interval_ms
        j = i
        while j < n and bars.open_ms[j] < bucket + interval_ms:
            j += 1
        if j - i == per_bucket:
            out.open_ms.append(bucket)
            out.open.append(bars.open[i])
            out.high.append(max(bars.high[i:j]))
            out.low.append(min(bars.low[i:j]))
            out.close.append(bars.close[j - 1])
            out.volume.append(sum(bars.volume[i:j]))
        i = j
    return out


def concat(*parts: Bars) -> Bars:
    """Join bars of one interval; open times must be strictly increasing across parts."""
    if not parts:
        raise ValueError("concat needs at least one Bars")
    interval_ms = parts[0].interval_ms
    out = _empty(interval_ms)
    last = None
    for part in parts:
        if part.interval_ms != interval_ms:
            raise ValueError(
                f"interval mismatch: {part.interval_ms} ms vs {interval_ms} ms"
            )
        if not len(part):
            continue
        if last is not None and part.open_ms[0] <= last:
            raise ValueError(
                f"parts overlap or are out of order at {part.open_ms[0]} (last was {last})"
            )
        last = part.open_ms[-1]
        out.open_ms.extend(part.open_ms)
        out.open.extend(part.open)
        out.high.extend(part.high)
        out.low.extend(part.low)
        out.close.extend(part.close)
        out.volume.extend(part.volume)
    return out


def last_closed_index(bars: Bars, t_ms: int) -> int:
    """Largest ``i`` with ``open_ms[i] + interval <= t_ms``; -1 when none."""
    return bisect_right(bars.open_ms, t_ms - bars.interval_ms) - 1


def entry_index_for(bars: Bars, order_ms: int) -> Optional[int]:
    """Index of the bar containing ``order_ms`` (the bar an order fills on); ``None``
    when no bar covers it."""
    i = bisect_right(bars.open_ms, order_ms) - 1
    if i < 0 or order_ms >= bars.open_ms[i] + bars.interval_ms:
        return None
    return i


def index_closing_at(bars: Bars, close_ms: int) -> Optional[int]:
    """Index of the bar closing exactly at ``close_ms`` (inverse of an entry fill time)."""
    i = entry_index_for(bars, close_ms - bars.interval_ms)
    if i is None or bars.open_ms[i] + bars.interval_ms != close_ms:
        return None
    return i


def slice_until(bars: Bars, t_ms: int) -> Bars:
    """Bars already closed at ``t_ms`` (the still-forming bar is excluded)."""
    end = last_closed_index(bars, t_ms) + 1
    return Bars(
        bars.interval_ms,
        bars.open_ms[:end],
        bars.open[:end],
        bars.high[:end],
        bars.low[:end],
        bars.close[:end],
        bars.volume[:end],
    )


def wib_day(t_ms: int) -> date:
    """WIB (UTC+7) calendar day of a UTC millisecond timestamp."""
    return date.fromordinal(_UNIX_EPOCH_ORDINAL + (t_ms + _WIB_OFFSET_MS) // _MS_PER_DAY)


def wib_hour(t_ms: int) -> int:
    """WIB wall-clock hour (0-23) of a UTC millisecond timestamp."""
    return ((t_ms + _WIB_OFFSET_MS) // _MS_PER_HOUR) % 24


def wib_day_start_ms(day: date) -> int:
    """UTC milliseconds of 00:00 WIB on ``day``."""
    return ms(datetime(day.year, day.month, day.day, tzinfo=WIB))


def check_symbol(symbol: str) -> str:
    """Accept venue-form USDT pairs only (``BTC-USDT``); anything else is a ``ValueError``."""
    if not isinstance(symbol, str) or SYMBOL_PATTERN.fullmatch(symbol) is None:
        raise ValueError(f"not a BingX USDT symbol: {symbol!r}")
    return symbol


def day_path(data_dir: Path, source: str, symbol: str, interval: str, day: date) -> Path:
    return Path(data_dir) / source / check_symbol(symbol) / interval / f"{day.isoformat()}.csv.gz"


def warmup_path(data_dir: Path, symbol: str, interval: str) -> Path:
    """Warm-up bars (same row format as day files) of the production source."""
    return Path(data_dir) / "prod" / check_symbol(symbol) / interval / "warmup.csv.gz"


def funding_path(data_dir: Path, symbol: str) -> Path:
    """JSON list of funding times as UTC milliseconds."""
    return Path(data_dir) / "prod" / check_symbol(symbol) / "funding.json"


def _day_text(rows: Sequence[Row]) -> str:
    return "".join(f"{int(t)},{o!r},{h!r},{l!r},{c!r},{v!r}\n" for t, o, h, l, c, v in rows)


def write_day(path: Path, rows: Sequence[Row]) -> None:
    """Write rows as gzip CSV atomically (temp file in the same directory, then replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(_day_text(rows).encode("utf-8"))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def write_day_exclusive(path: Path, rows: Sequence[Row]) -> bool:
    """Write a day file only if none exists; ``False`` when one exists or appears
    meanwhile (another writer won), ``True`` when this call wrote it."""
    data = gzip.compress(_day_text(rows).encode("utf-8"), mtime=0)
    try:
        write_exclusive(path, data, "day")
    except FileExistsError:
        return False
    return True


def read_day(path: Path) -> List[Row]:
    """Read a day file; raise ``ValueError`` when it is corrupt or malformed."""
    path = Path(path)
    rows: List[Row] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                fields = line.split(",")
                if len(fields) != _ROW_FIELDS:
                    raise ValueError(
                        f"{path}:{line_no}: expected {_ROW_FIELDS} fields, got {len(fields)}"
                    )
                try:
                    rows.append(
                        (
                            int(fields[0]),
                            float(fields[1]),
                            float(fields[2]),
                            float(fields[3]),
                            float(fields[4]),
                            float(fields[5]),
                        )
                    )
                except ValueError as exc:
                    raise ValueError(f"{path}:{line_no}: non-numeric field ({exc})") from exc
    except (EOFError, zlib.error, UnicodeDecodeError, gzip.BadGzipFile) as exc:
        raise ValueError(f"{path}: corrupt day file ({exc})") from exc
    return rows


def load_days(
    data_dir: Path,
    source: str,
    symbol: str,
    interval: str,
    start_day: date,
    end_day: date,
) -> Bars:
    """Load the inclusive WIB day range into one ``Bars``.

    Raises ``MissingDayError`` listing every missing day before reading any file,
    ``DayShapeError`` when a file does not hold exactly its own WIB day of bars
    (spec 5.2: 1,440 1m bars) and ``BarGapError`` when the bars do not join.
    """
    if end_day < start_day:
        raise ValueError(f"end_day {end_day} is before start_day {start_day}")
    days = [
        start_day + timedelta(days=k) for k in range((end_day - start_day).days + 1)
    ]
    paths = [day_path(data_dir, source, symbol, interval, d) for d in days]
    missing = [d.isoformat() for d, p in zip(days, paths) if not p.exists()]
    if missing:
        raise MissingDayError(
            f"missing {source}/{symbol}/{interval} day files: {', '.join(missing)}"
        )
    interval_ms = interval_to_ms(interval)
    rows: List[Row] = []
    for day, path in zip(days, paths):
        day_rows = read_day(path)
        problem = day_shape_problem(day_rows, day, interval_ms)
        if problem is not None:
            raise DayShapeError(f"{path} ({day.isoformat()}): {problem}")
        rows.extend(day_rows)
    return bars_from_rows(rows, interval_ms)


def day_shape_problem(rows: Sequence[Row], day: date, interval_ms: int) -> Optional[str]:
    """Why ``rows`` are not exactly the bars of WIB ``day`` (count, first and last open
    time), else ``None``; contiguity is ``bars_from_rows``'s check."""
    expected = _MS_PER_DAY // interval_ms
    start = wib_day_start_ms(day)
    last = start + _MS_PER_DAY - interval_ms
    if len(rows) != expected:
        return f"holds {len(rows)} bars, not {expected}"
    if int(rows[0][0]) != start or int(rows[-1][0]) != last:
        return (f"bars run {int(rows[0][0])}..{int(rows[-1][0])}, not the day's "
                f"{start}..{last}")
    return None


def funding_gap_problem(times: Sequence[int], start_ms: int, end_ms: int,
                        max_gap_ms: int) -> Optional[str]:
    """Why ``times`` do not cover [start_ms, end_ms) at the funding cadence, else ``None``.

    The range's start, the funding times inside it and its end must follow each other
    by at most ``max_gap_ms``, so a hole, a late first time, an early last time or a
    list cut at a row limit all fail; times outside the range are ignored."""
    points = [start_ms] + sorted(t for t in times if start_ms <= t < end_ms) + [end_ms]
    widest = max(zip(points, points[1:]), key=lambda ab: ab[1] - ab[0])
    if widest[1] - widest[0] > max_gap_ms:
        return (f"funding gap of {(widest[1] - widest[0]) / _MS_PER_HOUR:.1f} h from "
                f"{widest[0]} to {widest[1]} (at most {max_gap_ms / _MS_PER_HOUR:.2f} h "
                f"over {start_ms}..{end_ms})")
    return None
