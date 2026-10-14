"""Loading the stored data for the Pine gate research (spec 5.2, 5.3).

Research code; the bot never imports it. Standard library only.

``load_dataset`` reads production 1m day files (each exactly its own WIB day), the
native 1h/4h warm-up and the funding times (at the funding cadence over the whole
loaded range), and cuts every bar series at ``until_ms``. A warm-up that is
missing, or present but unused (it starts at or after the first aggregated bar),
is recorded in ``Dataset.missing_warmups``. The ``Dataset`` carries the hashes the
selection and holdout files bind (spec 5.1).
"""

from __future__ import annotations

import hashlib
import json
import sys
from array import array
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .bars import (
    Bars,
    bars_from_rows,
    day_path,
    funding_gap_problem,
    funding_path,
    interval_to_ms,
    load_days,
    read_day,
    slice_until,
    warmup_path,
    wib_day,
    wib_day_start_ms,
)
from .features import FeatureFrame, warmup_unused
from .outcomes import OutcomeCache
from .simulate import Market
from .spec import HOLDOUT_END, TRAIN_END, TRAIN_START, DataConfig, ExecConfig, ms

SYMBOLS: Tuple[str, ...] = ("BTC-USDT", "ETH-USDT")  # tick order: BTC first

_MS_PER_HOUR = 3_600_000
# Outcomes may resolve up to 60 minutes after the training end; nothing later is loaded.
TRAIN_DATA_UNTIL_MS = ms(TRAIN_END) + _MS_PER_HOUR
_WARMUP_INTERVALS = ("1h", "4h")
_DATA = DataConfig()
TimeRange = Tuple[int, int]


@dataclass(frozen=True)
class Dataset:
    frames: Mapping[str, FeatureFrame]
    markets: Mapping[str, Market]
    data_sha256: str  # sha256 over the sorted "relative_path:sha256" of every file read
    loaded_until_ms: int  # nothing at or after this time was kept
    missing_warmups: Tuple[str, ...] = ()
    # sha256 over the arrays actually kept after the cut: 1m bars, warm-ups, funding times
    train_data_sha256: str = ""
    # the same hash over those arrays cut at TRAIN_DATA_UNTIL_MS: for a training load it
    # equals train_data_sha256; the holdout compares it with the selection's (spec 5.1)
    train_part_sha256: str = ""


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _file_line(data_dir: Path, path: Path) -> str:
    return f"{Path(path).relative_to(data_dir).as_posix()}:{_file_sha256(path)}"


_BAR_FIELDS = ("open_ms", "open", "high", "low", "close", "volume")


def _array_bytes(values: array) -> bytes:
    """Little-endian bytes of an array, whatever the platform."""
    if sys.byteorder == "little":
        return values.tobytes()
    swapped = array(values.typecode, values)
    swapped.byteswap()
    return swapped.tobytes()


def _content_sha256(
    bars: Bars, warmups: Mapping[str, Optional[Bars]], funding: Sequence[int]
) -> str:
    """sha256 over one symbol's kept arrays, each section labelled with its length."""
    h = hashlib.sha256()
    sections = [("1m", bars)] + [(k, warmups[k]) for k in _WARMUP_INTERVALS]
    for label, part in sections:
        if part is None:
            h.update(f"{label}:absent\n".encode("ascii"))
            continue
        for field in _BAR_FIELDS:
            values = getattr(part, field)
            h.update(f"{label}/{field}:{len(values)}\n".encode("ascii"))
            h.update(_array_bytes(values))
    h.update(f"funding:{len(funding)}\n".encode("ascii"))
    h.update(_array_bytes(array("q", funding)))
    return h.hexdigest()


def _read_warmup(path: Path, interval: str, until_ms: Optional[int]) -> Bars:
    bars = bars_from_rows(read_day(path), interval_to_ms(interval))
    return bars if until_ms is None else slice_until(bars, until_ms)


def read_funding_times(path: Path) -> List[int]:
    """Every funding time of a funding file (required), sorted."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or any(type(t) is not int for t in raw):
        raise ValueError(f"{path}: expected a JSON list of integer milliseconds")
    return sorted(raw)


def funding_problem(times: Sequence[int], start_ms: int, end_ms: int) -> Optional[str]:
    """``bars.funding_gap_problem`` at the pre-registered cadence (``DataConfig``)."""
    max_gap_ms = (_DATA.funding_interval_s + _DATA.funding_gap_tolerance_s) * 1000
    return funding_gap_problem(times, start_ms, end_ms, max_gap_ms)


def _read_funding(path: Path, span: TimeRange, until_ms: Optional[int]) -> Tuple[int, ...]:
    """Funding times from JSON (required), at the funding cadence over ``span`` (the
    loaded range); kept below ``until_ms``.

    A tick never runs at or after ``until_ms``, so a later funding time can never
    start a blackout."""
    raw = read_funding_times(path)
    problem = funding_problem(raw, *span)
    if problem is not None:
        raise ValueError(f"{path}: {problem}")
    return tuple(t for t in raw if until_ms is None or t < until_ms)


def _training_part_sha256(
    bars: Bars, warmups: Mapping[str, Optional[Bars]], funding: Sequence[int]
) -> str:
    """``_content_sha256`` of the arrays cut as a training load cuts them."""
    cut = TRAIN_DATA_UNTIL_MS
    part = {k: None if w is None else slice_until(w, cut) for k, w in warmups.items()}
    return _content_sha256(slice_until(bars, cut), part, [t for t in funding if t < cut])


@dataclass(frozen=True)
class _Loaded:
    frame: FeatureFrame
    market: Market
    lines: List[str]  # "relative_path:sha256" of every file read
    missing: List[str]  # warm-up files not found
    content: str  # sha256 of the arrays kept after the cut
    train_part: str  # the same over the training part


def _load_symbol(
    data_dir: Path,
    symbol: str,
    days: Tuple[date, date],
    until_ms: Optional[int],
    exec_cfg: ExecConfig,
) -> _Loaded:
    """One symbol's frame and market with the hashes of what was read and kept."""
    start_day, end_day = days
    bars = load_days(data_dir, "prod", symbol, "1m", start_day, end_day)
    if until_ms is not None:
        bars = slice_until(bars, until_ms)
    lines = [
        _file_line(data_dir, day_path(data_dir, "prod", symbol, "1m", start_day + timedelta(k)))
        for k in range((end_day - start_day).days + 1)
    ]
    warmups: Dict[str, Optional[Bars]] = {}
    missing: List[str] = []
    for interval in _WARMUP_INTERVALS:
        path = warmup_path(data_dir, symbol, interval)
        rel = path.relative_to(data_dir).as_posix()
        if path.exists():
            lines.append(_file_line(data_dir, path))
            warmups[interval] = _read_warmup(path, interval, until_ms)
            if warmup_unused(bars, warmups[interval], interval_to_ms(interval)):
                missing.append(f"{rel} (unused: starts at or after the first aggregated bar)")
        else:
            missing.append(rel)
            warmups[interval] = None
    fpath = funding_path(data_dir, symbol)
    span = (wib_day_start_ms(start_day), wib_day_start_ms(end_day + timedelta(days=1)))
    if until_ms is not None:
        span = (span[0], min(span[1], until_ms))
    try:
        funding = _read_funding(fpath, span, until_ms)
    except ValueError as exc:
        raise ValueError(f"{symbol} funding: {exc}") from None
    lines.append(_file_line(data_dir, fpath))
    frame = FeatureFrame.build(symbol, bars, warmups["1h"], warmups["4h"])
    market = Market(symbol, bars, OutcomeCache(bars, exec_cfg), funding)
    return _Loaded(frame, market, lines, missing, _content_sha256(bars, warmups, funding),
                   _training_part_sha256(bars, warmups, funding))


def load_dataset(
    data_dir: Path,
    until_ms: Optional[int],
    *,
    start_day: Optional[date] = None,
    exec_cfg: Optional[ExecConfig] = None,
) -> Dataset:
    """Load both symbols from ``start_day`` (default: the training start's WIB day).

    With ``until_ms`` the day files run to ``wib_day(until_ms - 1)`` and every bar
    series is cut to bars closed by ``until_ms``; without it they run to the last
    holdout day. Missing day or funding files raise; a missing warm-up is recorded.
    """
    data_dir = Path(data_dir)
    first = start_day if start_day is not None else wib_day(ms(TRAIN_START))
    end_ms = until_ms if until_ms is not None else ms(HOLDOUT_END)
    last = wib_day(end_ms - 1)
    cfg = exec_cfg if exec_cfg is not None else ExecConfig()
    loaded = {s: _load_symbol(data_dir, s, (first, last), until_ms, cfg) for s in SYMBOLS}
    lines = sorted(line for x in loaded.values() for line in x.lines)
    loaded_until = until_ms if until_ms is not None else wib_day_start_ms(last + timedelta(1))
    return Dataset(
        frames=MappingProxyType({s: x.frame for s, x in loaded.items()}),
        markets=MappingProxyType({s: x.market for s, x in loaded.items()}),
        data_sha256=_text_sha256(lines),
        loaded_until_ms=loaded_until,
        missing_warmups=tuple(m for x in loaded.values() for m in x.missing),
        train_data_sha256=_text_sha256([f"{s}:{x.content}" for s, x in loaded.items()]),
        train_part_sha256=_text_sha256([f"{s}:{x.train_part}" for s, x in loaded.items()]),
    )


def _text_sha256(lines: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
