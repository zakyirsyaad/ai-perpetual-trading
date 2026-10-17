"""Synthetic production data for the mode backtest tests.

Not a test module: the name does not match ``test*.py``, so discovery skips it.
Tests import it as ``from tests.mode_backtest_helpers import ...`` and run from
the repository root, as ``tests/test_telemetry_book_ticker.py`` does.

The writers produce what the real data looks like. Day files are gzip CSV with
exactly 1,440 flat 1m bars from 00:00 WIB (17:00 UTC of the day before). The
native 1h and 4h warm-ups end with the bar that contains the minute before the
first day starts, on the UTC grid, so they join the aggregated bars of that
first day without a gap.

Every bar is flat (open = high = low = close) at ``price(i)`` and has a volume of
1.0. ``price`` maps a bar number to a positive price; the default is a constant,
so every series joins at 100.0.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, List, Tuple

from research.pine_gate.bars import (
    Row,
    day_path,
    warmup_path,
    wib_day_start_ms,
    write_day,
)

SOURCE = "prod"
MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
FOUR_HOUR_MS = 4 * HOUR_MS
MINUTES_PER_DAY = 1_440

Price = Callable[[int], float]


def flat_price(i: int) -> float:
    return 100.0


def flat_bar(open_ms: int, price: float) -> Row:
    return (open_ms, float(price), float(price), float(price), float(price), 1.0)


def minute_rows(start_ms: int, n: int, price: Price = flat_price) -> List[Row]:
    """``n`` consecutive 1m bars from ``start_ms``; bar ``i`` is flat at ``price(i)``."""
    return [flat_bar(start_ms + i * MINUTE_MS, price(i)) for i in range(n)]


def write_days(
    data_dir: Path, symbol: str, days: Iterable[date], price: Price = flat_price
) -> List[Path]:
    """Write one valid 1,440-bar WIB day file per day; returns the paths, oldest day first.

    ``price(i)`` takes the number of minutes since 00:00 WIB of the earliest day
    written, so one function describes a continuous series across the days, and a
    day left out of ``days`` leaves a hole in time, not in the price."""
    ordered = sorted(set(days))
    if not ordered:
        return []
    origin_ms = wib_day_start_ms(ordered[0])
    paths = []
    for day in ordered:
        start_ms = wib_day_start_ms(day)
        offset = (start_ms - origin_ms) // MINUTE_MS
        rows = minute_rows(start_ms, MINUTES_PER_DAY, lambda i, k=offset: price(k + i))
        path = day_path(data_dir, SOURCE, symbol, "1m", day)
        write_day(path, rows)
        paths.append(path)
    return paths


def write_warmups(
    data_dir: Path,
    symbol: str,
    first_day: date,
    hours_1h: int,
    bars_4h: int,
    price: Price = flat_price,
) -> Tuple[Path, Path]:
    """Write ``warmup.csv.gz`` for 1h and 4h, ending as the real data does.

    The last bar of each is the one that contains the minute before ``first_day``
    starts (00:00 WIB), aligned to the UTC epoch: for the 1h file it closes exactly
    when the first 1m bar opens, for the 4h file it is the 4h bar that opened one
    hour earlier. ``hours_1h`` and ``bars_4h`` are the number of bars; ``price(i)``
    takes the number of the bar, counted from the first bar of that file. Returns
    the 1h path and the 4h path."""
    end_ms = wib_day_start_ms(first_day)
    paths = []
    for interval, interval_ms, count in (
        ("1h", HOUR_MS, hours_1h),
        ("4h", FOUR_HOUR_MS, bars_4h),
    ):
        last_open_ms = (end_ms - 1) // interval_ms * interval_ms
        first_open_ms = last_open_ms - (count - 1) * interval_ms
        rows = [flat_bar(first_open_ms + k * interval_ms, price(k)) for k in range(count)]
        path = warmup_path(data_dir, symbol, interval)
        write_day(path, rows)
        paths.append(path)
    return paths[0], paths[1]


def sha256sum_text(data_dir: Path) -> str:
    """What ``cd <data_dir> && find prod -type f | sort | xargs sha256sum`` prints.

    One ``<hex>  prod/...`` line per file under ``prod``, in path order."""
    root = Path(data_dir)
    rels = sorted(p.relative_to(root).as_posix() for p in (root / SOURCE).rglob("*") if p.is_file())
    return "".join(
        f"{hashlib.sha256((root / rel).read_bytes()).hexdigest()}  {rel}\n" for rel in rels
    )
