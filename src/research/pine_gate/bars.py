"""Columnar OHLCV bars, UTC-aligned aggregation and WIB day files.

Research code; the bot never imports it. Standard library only.

``Bars`` holds one ``array`` per column and is treated as immutable: every
function returns new ``Bars`` and never edits arrays in place after
construction. Day files are gzip CSV, one WIB (UTC+7) calendar day each, with
header-less rows ``open_ms,open,high,low,close,volume``.
"""

from __future__ import annotations

import gzip
import os
import tempfile
import zlib
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

from .spec import WIB, ms

Row = Tuple[int, float, float, float, float, float]

_MS_PER_HOUR = 3_600_000
_MS_PER_DAY = 24 * _MS_PER_HOUR
_WIB_OFFSET_MS = 7 * _MS_PER_HOUR
_ROW_FIELDS = 6
_UNIX_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()


class BarGapError(ValueError):
    """Bars are unsorted, duplicated or not spaced by exactly one interval."""


class MissingDayError(FileNotFoundError):
    """One or more day files of a requested range do not exist."""


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


def bars_from_rows(rows: Iterable[Row], interval_ms: int) -> Bars:
    """Build ``Bars`` from rows sorted strictly ascending, spaced by ``interval_ms``."""
    out = _empty(interval_ms)
    previous = None
    for row in rows:
        t = int(row[0])
        if previous is not None and t != previous + interval_ms:
            raise BarGapError(
                f"bar at {t} does not follow {previous} by {interval_ms} ms"
            )
        previous = t
        out.open_ms.append(t)
        out.open.append(float(row[1]))
        out.high.append(float(row[2]))
        out.low.append(float(row[3]))
        out.close.append(float(row[4]))
        out.volume.append(float(row[5]))
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


def day_path(data_dir: Path, source: str, symbol: str, interval: str, day: date) -> Path:
    return Path(data_dir) / source / symbol / interval / f"{day.isoformat()}.csv.gz"


def warmup_path(data_dir: Path, symbol: str, interval: str) -> Path:
    """Warm-up bars (same row format as day files) of the production source."""
    return Path(data_dir) / "prod" / symbol / interval / "warmup.csv.gz"


def funding_path(data_dir: Path, symbol: str) -> Path:
    """JSON list of funding times as UTC milliseconds."""
    return Path(data_dir) / "prod" / symbol / "funding.json"


def write_day(path: Path, rows: Sequence[Row]) -> None:
    """Write rows as gzip CSV atomically (temp file in the same directory, then replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                for t, o, h, l, c, v in rows:
                    gz.write(f"{int(t)},{o!r},{h!r},{l!r},{c!r},{v!r}\n".encode("utf-8"))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


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
    and ``BarGapError`` when the days do not join without a gap.
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
    rows: List[Row] = []
    for path in paths:
        rows.extend(read_day(path))
    return bars_from_rows(rows, interval_to_ms(interval))
