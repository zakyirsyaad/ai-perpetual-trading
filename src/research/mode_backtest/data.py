"""Production 1m day files and 1h/4h warm-ups of one pair, and the checks on those files.

Research code; the bot never imports it. A missing, unreadable or altered file stops
the run with an error and never shrinks the sample: day files load as whole days or
not at all, an absent or useless warm-up raises, and a copy of the data is accepted only
when it matches the VPS file for file, in both directions: every file the VPS printed has
the same sha256 here, and no file under ``prod/`` here is missing from the listing.

The source is production (VST volume is not real). Files are laid out as the Pine gate
stores them: ``<data_dir>/prod/<SYMBOL>/1m/<YYYY-MM-DD>.csv.gz`` (one WIB day each) and
``<data_dir>/prod/<SYMBOL>/{1h,4h}/warmup.csv.gz`` (native bars, same row format).
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path, PurePosixPath
from typing import Iterable, List, Optional, Tuple, Union

from research.pine_gate.bars import (
    Bars,
    bars_from_rows,
    day_path,
    interval_to_ms,
    load_days,
    read_day,
    warmup_path,
)
from research.pine_gate.cli_common import file_sha256
from research.pine_gate.features import warmup_unused

SOURCE = "prod"

_DAY_INTERVAL = "1m"
_WARMUP_INTERVALS = ("1h", "4h")
# A ``sha256sum`` line in text mode: 64 hex digits, two spaces, the path. A ``*`` in
# place of the second space marks binary mode, which the VPS command does not print.
_LISTING_LINE = re.compile(r"([0-9a-fA-F]{64})  (.+)")
_PREVIEW_CHARS = 80

PathLike = Union[str, Path]


@dataclass(frozen=True)
class PairData:
    """One pair's 1m bars for the loaded days and its native 1h and 4h warm-ups.

    The warm-ups end with the bar that contains the minute before the first day
    starts. Joining them to the aggregated bars is the frame builder's job."""

    symbol: str
    bars_1m: Bars
    warmup_1h: Bars
    warmup_4h: Bars


def _days(first_day: date, last_day: date) -> List[date]:
    if last_day < first_day:
        raise ValueError(f"last_day {last_day} is before first_day {first_day}")
    return [first_day + timedelta(days=k) for k in range((last_day - first_day).days + 1)]


def stage_files(data_dir: PathLike, symbol: str, first_day: date, last_day: date) -> List[Path]:
    """The files ``load_pair`` reads: the day files of the range and both warm-ups, sorted.

    Only names are built; nothing is read and nothing has to exist yet."""
    paths = [
        day_path(data_dir, SOURCE, symbol, _DAY_INTERVAL, day)
        for day in _days(first_day, last_day)
    ]
    paths += [warmup_path(data_dir, symbol, interval) for interval in _WARMUP_INTERVALS]
    return sorted(paths)


def _read_warmup(path: Path, interval: str) -> Bars:
    return bars_from_rows(read_day(path), interval_to_ms(interval))


def _relative(path: Path, root: PathLike) -> str:
    return path.relative_to(root).as_posix()


def load_pair(data_dir: PathLike, symbol: str, first_day: date, last_day: date) -> PairData:
    """Load the 1m bars of the inclusive WIB day range and the two warm-ups.

    Only the days of the range are read. A missing day raises ``MissingDayError``
    (every missing day named) before any file is read, a missing warm-up raises
    ``FileNotFoundError``, and a corrupt file or an invalid bar raises what
    ``research.pine_gate.bars`` raises for it. Once both warm-ups are read, a warm-up
    that adds nothing (``features.warmup_unused``: empty, or without a bar before the
    first aggregated bar) raises ``ValueError`` naming the file: the 4h EMA50 and the
    720-hour ATR median would stay ``None`` for weeks, the selector would answer
    TIDAK_ENTRY and trades would drop with no error."""
    bars_1m = load_days(data_dir, SOURCE, symbol, _DAY_INTERVAL, first_day, last_day)
    paths = {iv: warmup_path(data_dir, symbol, iv) for iv in _WARMUP_INTERVALS}
    missing = [_relative(path, data_dir) for path in paths.values() if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing warm-up files: {', '.join(missing)}")
    warmups = {iv: _read_warmup(paths[iv], iv) for iv in _WARMUP_INTERVALS}
    unused = [
        _relative(paths[iv], data_dir)
        for iv in _WARMUP_INTERVALS
        if warmup_unused(bars_1m, warmups[iv], interval_to_ms(iv))
    ]
    if unused:
        raise ValueError(
            "warm-up files add nothing (empty, or no bar before the first aggregated bar): "
            + ", ".join(unused)
        )
    return PairData(symbol, bars_1m, warmups["1h"], warmups["4h"])


def manifest(data_dir: PathLike, paths: Iterable[PathLike]) -> List[str]:
    """``relpath:sha256`` for every file, sorted; ``relpath`` is relative to ``data_dir``.

    The lines do not depend on where ``data_dir`` is, so a copy of the data has the
    manifest of the original. A missing file raises ``FileNotFoundError``; a path
    outside ``data_dir`` or given twice raises ``ValueError``."""
    root = Path(data_dir)
    files = [Path(path) for path in paths]
    names = [file.relative_to(root).as_posix() for file in files]
    twice = sorted(name for name, count in Counter(names).items() if count > 1)
    if twice:
        raise ValueError(f"paths given twice: {', '.join(twice)}")
    return sorted(f"{name}:{file_sha256(file)}" for name, file in zip(names, files, strict=True))


def manifest_sha256(lines: Iterable[str]) -> str:
    """sha256 of the manifest lines in sorted order, each ended by a newline."""
    if isinstance(lines, str):
        raise TypeError("manifest_sha256 takes the manifest lines, not one string")
    text = "".join(f"{line}\n" for line in sorted(lines))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _preview(line: str) -> str:
    return repr(line[:_PREVIEW_CHARS] + ("..." if len(line) > _PREVIEW_CHARS else ""))


def _listing_entry(line: str) -> Union[Tuple[str, str], str]:
    """``(sha256, path)`` of a listing line, or the reason it is no entry for a file under prod/.

    The path stays inside ``prod``: relative, no ``..`` and no NUL, so no entry can make
    the check read a file elsewhere."""
    match = _LISTING_LINE.fullmatch(line)
    if match is None:
        return "not '<64 hex digits>  <path>'"
    sha, text = match.groups()
    path = PurePosixPath(text)
    inside_source = len(path.parts) >= 2 and path.parts[0] == SOURCE and ".." not in path.parts
    if "\x00" in text or not inside_source:
        return f"not a file inside {SOURCE}/"
    return sha.lower(), path.as_posix()


def _file_problem(root: Path, relative: str, vps_sha: str) -> Optional[str]:
    try:
        local_sha = file_sha256(root / relative)
    except FileNotFoundError:
        return f"{relative}: missing locally"
    except OSError as exc:
        return f"{relative}: cannot be read ({type(exc).__name__})"
    if local_sha != vps_sha:
        return f"{relative}: sha256 differs (local {local_sha}, VPS {vps_sha})"
    return None


def _local_files(root: Path) -> List[str]:
    """The regular files under ``root/prod``, as paths relative to ``root``, sorted.

    Files whose names start with a dot (``.DS_Store``, ``._name``) are left out."""
    return sorted(
        _relative(path, root)
        for path in (root / SOURCE).rglob("*")
        if not path.name.startswith(".") and path.is_file()
    )


def verify_copy(data_dir: PathLike, sha256sum_text: str) -> List[str]:
    """Compare a local data copy with the VPS's ``sha256sum`` output; ``[]`` means exact.

    The text comes from ``cd ~/replay/pine_gate/data && find prod -type f | sort |
    xargs sha256sum``: ``<64 hex>  prod/<symbol>/<interval>/<file>`` per line, paths
    relative to ``data_dir``. The comparison runs both ways. One problem is reported for
    each file that differs, is missing or cannot be read, for each malformed or repeated
    line, and for each regular file under ``<data_dir>/prod`` that the listing does not
    name: a stray file, or a listing cut short (an interrupted ``ssh`` leaves one). Names
    starting with a dot are skipped, and so is everything outside ``prod/``. So ``[]``
    means every local file under ``prod/`` was compared with the VPS and matched.

    A listing without a single file is one problem, so an empty file cannot pass, and the
    local files are then not listed one by one; neither are they when no line is valid."""
    root = Path(data_dir)
    problems: List[str] = []
    listed = set()
    for number, line in enumerate(sha256sum_text.splitlines(), start=1):
        if not line.strip():
            continue
        entry = _listing_entry(line)
        if isinstance(entry, str):
            problems.append(f"line {number}: {entry}: {_preview(line)}")
            continue
        vps_sha, relative = entry
        if relative in listed:
            problems.append(f"line {number}: {relative} is listed twice")
            continue
        listed.add(relative)
        problem = _file_problem(root, relative, vps_sha)
        if problem is not None:
            problems.append(problem)
    if listed:
        problems += [
            f"{relative}: local file is not in the listing"
            for relative in _local_files(root)
            if relative not in listed
        ]
    elif not problems:
        problems.append("the listing holds no files; nothing was verified")
    return problems
