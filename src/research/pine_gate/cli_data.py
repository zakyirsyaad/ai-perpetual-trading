"""Data subcommands of the Pine gate CLI: fetch, warmup, native-check-fetch, funding,
collect and check-data.

Research code; the bot never imports it. Standard library plus ``httpx`` (only
for its transport error type; requests go through ``fetch``). Network access
goes through ``cli_common.http_get`` (public BingX market data, no keys) and
every pause through ``cli_common.sleep``; the library throttles the requests
of one call, and the CLI sleeps one second between two library calls so the
gap also holds across symbols and jobs. Existing files are never overwritten:
day files and warm-up, native-check or funding files that exist are skipped,
and a skipped file's covered range is printed so the operator can confirm it.
A rate limit (``RateLimitedError``) stops the whole command.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

import httpx

from . import cli_common as common
from .bars import (
    Bars,
    MissingDayError,
    Row,
    aggregate,
    day_shape_problem,
    bars_from_rows,
    day_path,
    funding_path,
    interval_to_ms,
    load_days,
    read_day,
    slice_until,
    warmup_path,
    wib_day,
    wib_day_start_ms,
)
from .features import (
    VOLUME_REL_TOL,
    VOLUME_WORST_REL_MAX,
    FeatureFrame,
    NoOverlapError,
    htf_agreement,
)
from .fetch import (
    MIN_REQUEST_GAP_SECONDS,
    PROD_HOST,
    SOURCE_HOSTS,
    ExchangeError,
    IncompleteDayError,
    collect,
    fetch_funding_times,
    sync_days,
    sync_line,
    sync_warmup,
)
from .dataset import SYMBOLS, TRAIN_DATA_UNTIL_MS, funding_problem, read_funding_times
from .spec import HOLDOUT_END, TRAIN_END, TRAIN_START, WIB, ms

HOSTS = SOURCE_HOSTS
WARMUP_INTERVALS = ("1h", "4h")
NATIVE_CHECK_FILE = "native_check.csv.gz"
NATIVE_CHECK_DEFAULT = (date(2025, 8, 1), date(2025, 9, 1))  # inside training
NATIVE_AGREEMENT_MIN = 0.999
# Per symbol: recorded and the next symbol goes on. RateLimitedError is not here.
_NETWORK_ERRORS = (ExchangeError, IncompleteDayError, httpx.HTTPError)
_MS_1M = 60_000
_LISTED_DAYS = 10

Series = Tuple[Bars, Optional[Bars], Optional[Bars]]  # 1m bars, native 1h and 4h warm-up


class _Pacer:
    """Sleep one request gap before every library call after the first."""

    def __init__(self) -> None:
        self._started = False

    def __call__(self) -> None:
        if self._started:
            common.sleep(MIN_REQUEST_GAP_SECONDS)
        self._started = True


def _ceil_to(t_ms: int, step_ms: int) -> int:
    return -(-t_ms // step_ms) * step_ms


# --------------------------------------------------------------------------- fetch


def cmd_fetch(args: argparse.Namespace) -> int:
    today = common.wib_today()
    if args.end >= today:
        raise ValueError(f"--end {args.end} is not over yet (today is {today} WIB); its last "
                         "bars are still forming, so the last day to fetch is yesterday")
    pace, failed_any = _Pacer(), False
    for symbol in args.symbols:
        pace()
        result = sync_days(common.http_get, HOSTS[args.source], args.source, symbol,
                           args.start, args.end, args.data_dir, sleep=common.sleep)
        print(sync_line(f"{args.source}/{symbol}/1m {args.start}..{args.end}", result))
        failed_any = failed_any or bool(result.failed or result.not_attempted)
    return 1 if failed_any else 0


def native_check_path(data_dir: Path, symbol: str, interval: str) -> Path:
    """Native production bars fetched only to check the 1m aggregation (never features)."""
    return Path(data_dir) / "prod" / symbol / interval / NATIVE_CHECK_FILE


def _wib_iso(t_ms: int) -> str:
    return datetime.fromtimestamp(t_ms / 1000, WIB).isoformat()


def covered(times: Sequence[int], unit: str) -> str:
    """``covers <first> .. <last>, N <unit>`` (WIB) for a file being skipped; anything
    but a list of integer milliseconds is a ``ValueError``."""
    if not isinstance(times, (list, tuple)) or any(
            isinstance(t, bool) or not isinstance(t, int) for t in times):
        raise ValueError(f"expected a list of integer milliseconds, got {str(times)[:80]}")
    if not times:
        return f"covers nothing, 0 {unit}"
    return f"covers {_wib_iso(times[0])} .. {_wib_iso(times[-1])}, {len(times)} {unit}"


def _sync_native(args: argparse.Namespace, kind: str,
                 path_for: Callable[[Path, str, str], Path]) -> int:
    """Native 1h/4h bars whose open time lies in [WIB midnight of --start, of --end)."""
    pace, failed_any = _Pacer(), False
    for symbol in SYMBOLS:
        for interval in WARMUP_INTERVALS:
            label = f"prod/{symbol}/{interval} {kind}"
            path = path_for(args.data_dir, symbol, interval)
            if path.exists():
                opens = [row[0] for row in read_day(path)]
                print(f"{label}: skipped, file exists ({covered(opens, 'bars')})")
                continue
            step = interval_to_ms(interval)
            start_ms = _ceil_to(wib_day_start_ms(args.start), step)
            end_ms = _ceil_to(wib_day_start_ms(args.end), step)
            pace()
            try:
                sync_warmup(common.http_get, PROD_HOST, symbol, interval, start_ms, end_ms,
                            path, sleep=common.sleep)
            except _NETWORK_ERRORS as exc:
                failed_any = True
                print(f"{label}: failed: {common.one_line(exc)}")
                continue
            print(f"{label}: {len(read_day(path))} bars")
    return 1 if failed_any else 0


def cmd_warmup(args: argparse.Namespace) -> int:
    return _sync_native(args, "warmup", warmup_path)


def cmd_native_check_fetch(args: argparse.Namespace) -> int:
    return _sync_native(args, "native check", native_check_path)


def cmd_funding(args: argparse.Namespace) -> int:
    """Funding times in [WIB midnight of --start, of --end) per symbol; a list (fetched or
    stored) with a gap wider than the funding cadence over that range fails."""
    start_ms = wib_day_start_ms(args.start)
    end_ms = wib_day_start_ms(args.end) - 1
    if end_ms < start_ms:
        raise ValueError(f"--end {args.end} must be after --start {args.start}")
    pace, failed_any = _Pacer(), False
    for symbol in SYMBOLS:
        label = f"prod/{symbol} funding"
        path = funding_path(args.data_dir, symbol)
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            try:
                coverage = covered(saved, "times")
            except ValueError as exc:
                raise ValueError(f"{path}: {exc}") from None
            problem = funding_problem(saved, start_ms, end_ms + 1)
            if problem is not None:  # never replaced: removing it is the operator's call
                failed_any = True
                print(f"{label}: failed: stored file ({coverage}) {problem}; remove it "
                      "deliberately to fetch again")
                continue
            print(f"{label}: skipped, file exists ({coverage})")
            continue
        pace()
        try:
            times = fetch_funding_times(common.http_get, PROD_HOST, symbol, start_ms, end_ms,
                                        sleep=common.sleep)
        except _NETWORK_ERRORS as exc:
            failed_any = True
            print(f"{label}: failed: {common.one_line(exc)}")
            continue
        problem = funding_problem(times, start_ms, end_ms + 1)
        if problem is not None:  # a short or capped answer is never stored
            failed_any = True
            print(f"{label}: failed: fetched {len(times)} times, {problem}; nothing stored")
            continue
        common.write_exclusive(path, json.dumps(times) + "\n", "funding")
        print(f"{label}: {len(times)} times")
    return 1 if failed_any else 0


def cmd_collect(args: argparse.Namespace) -> int:
    lines = collect(common.http_get, args.data_dir, common.wib_today(), sleep=common.sleep)
    for line in lines:
        print(line)
    return 1 if any("failed:" in line for line in lines) else 0


# --------------------------------------------------------------------------- check-data


def _days(first: date, last: date) -> List[date]:
    return [first + timedelta(days=k) for k in range((last - first).days + 1)]


def _listed(days: Sequence[date]) -> str:
    shown = ", ".join(d.isoformat() for d in days[:_LISTED_DAYS])
    return shown + (", ..." if len(days) > _LISTED_DAYS else "")


ZERO_VOLUME_RUN_MAX = 60  # minutes; the stored production data has none at all

# Why a stored 1m day is unusable, in the order the lines are printed.
_DAY_PROBLEMS = {
    "shape": "not a whole WIB day (1440 bars)",
    "bars": "invalid bars",
    "zero_volume": f"a run of >= {ZERO_VOLUME_RUN_MAX} zero-volume minutes",
}


def _longest_zero_volume_run(rows: Sequence[Row]) -> int:
    longest = run = 0
    for row in rows:
        run = run + 1 if row[5] == 0.0 else 0
        longest = max(longest, run)
    return longest


def _day_problem(path: Path, day: date) -> Optional[str]:
    """The ``_DAY_PROBLEMS`` key of one 1m day file, read once, or ``None``.

    A corrupt file is not a whole day; bars the loader would reject (non-finite,
    high/low not bracketing, negative volume, a gap) are invalid; a long run of
    zero volume skews every 24-hour POC that covers it while OHLC looks fine.
    """
    try:
        rows = read_day(path)
    except ValueError:
        return "shape"
    if day_shape_problem(rows, day, _MS_1M) is not None:
        return "shape"
    try:
        bars_from_rows(rows, _MS_1M)
    except ValueError:
        return "bars"
    if _longest_zero_volume_run(rows) >= ZERO_VOLUME_RUN_MAX:
        return "zero_volume"
    return None


def days_check(data_dir: Path) -> Tuple[List[str], bool]:
    """Every WIB day of the training start .. the last holdout day, prod BTC/ETH 1m: each
    day file exists, holds exactly its own day's 1,440 valid bars and has no long run
    of zero volume."""
    first, last = wib_day(ms(TRAIN_START)), wib_day(ms(HOLDOUT_END) - 1)
    lines: List[str] = []
    for symbol in SYMBOLS:
        paths = {d: day_path(data_dir, "prod", symbol, "1m", d) for d in _days(first, last)}
        missing = [d for d, p in paths.items() if not p.exists()]
        found = {d: _day_problem(p, d) for d, p in paths.items() if p.exists()}
        if missing:
            lines.append(f"  prod/{symbol}/1m missing {len(missing)} days: {_listed(missing)}")
        for kind, text in _DAY_PROBLEMS.items():
            days = [d for d, problem in found.items() if problem == kind]
            if days:
                lines.append(f"  prod/{symbol}/1m {text} on {len(days)} days: {_listed(days)}")
    verdict = "FAIL" if lines else "PASS"
    head = f"{verdict} complete days: prod BTC/ETH 1m {first}..{last}"
    return [head] + lines, not lines


def funding_check(data_dir: Path, symbol: str) -> Tuple[str, bool]:
    """The stored funding times cover training start .. holdout end at the cadence."""
    label = f"funding cadence prod/{symbol}"
    path = funding_path(data_dir, symbol)
    try:
        times = read_funding_times(path)
    except (OSError, ValueError) as exc:
        return f"FAIL {label}: {common.one_line(exc)}", False
    problem = funding_problem(times, ms(TRAIN_START), ms(HOLDOUT_END))
    if problem is not None:
        return f"FAIL {label}: {problem}", False
    return f"PASS {label}: {covered(times, 'times')}", True


def read_warmups(data_dir: Path, symbol: str) -> Tuple[Optional[Bars], Optional[Bars]]:
    """Native 1h and 4h warm-up bars of ``symbol``; ``None`` for a missing file."""
    out: List[Optional[Bars]] = []
    for interval in WARMUP_INTERVALS:
        path = warmup_path(data_dir, symbol, interval)
        out.append(bars_from_rows(read_day(path), interval_to_ms(interval))
                   if path.exists() else None)
    return out[0], out[1]


def native_check(data_dir: Path, symbol: str, interval: str) -> Tuple[List[str], bool]:
    """Native bars of ``native-check-fetch`` vs the bars aggregated from 1m day files.

    Two gating lines over their overlap, each naming the WIB days it covers.
    OHLC, which is all the 1h/4h features read: at least 99.9% of the bars must
    agree. Volume: no feature reads 1h/4h volume, but this cross-checks the 1m
    volume the POC reads, and BingX's native volume differs from the 1m sum in a
    few bars, so only the worst relative difference is bounded
    (``VOLUME_WORST_REL_MAX``; a NaN fails); the share within ``VOLUME_REL_TOL``
    is shown for information. It covers only the native-check window (by
    default 2025-08), cut to the 1m range (training start .. last holdout day);
    ``days_check`` catches long zero-volume runs on every day, and a wider
    window needs ``native-check-fetch --start/--end`` into a fresh file.
    A missing, empty or unreadable native file, missing or unreadable 1m days
    and no overlap all FAIL: the aggregation is unchecked then, which is never
    a pass.
    """
    where = f"vs aggregated prod/{symbol}/{interval}"
    label = f"native check {where}"
    path = native_check_path(data_dir, symbol, interval)
    if not path.exists():
        return [f"FAIL {label}: {path.name} missing; run native-check-fetch"], False
    step = interval_to_ms(interval)
    try:
        native = bars_from_rows(read_day(path), step)
    except ValueError as exc:
        return [f"FAIL {label}: {path.name} unreadable ({common.one_line(exc)})"], False
    if not len(native):
        return [f"FAIL {label}: {path.name} holds no bars"], False
    first = max(wib_day(native.open_ms[0]), wib_day(ms(TRAIN_START)))
    last = min(wib_day(native.open_ms[-1] + step - 1), wib_day(ms(HOLDOUT_END) - 1))
    if last < first:
        return [f"FAIL {label}: no overlap with the 1m data range "
                f"(native bars lie outside it)"], False
    try:
        bars = load_days(data_dir, "prod", symbol, "1m", first, last)
    except MissingDayError as exc:
        return [f"FAIL {label}: {common.one_line(exc)}"], False
    except ValueError as exc:  # corrupt, mis-shaped or non-joining day files
        return [f"FAIL {label}: 1m data unreadable ({common.one_line(exc)})"], False
    try:
        agreement = htf_agreement(native, aggregate(bars, step))
    except NoOverlapError as exc:
        return [f"FAIL {label}: {common.one_line(exc)}"], False
    span = f"over {first}..{last}"
    ohlc_ok = agreement.ohlc_share >= NATIVE_AGREEMENT_MIN
    volume_ok = agreement.worst_volume_rel <= VOLUME_WORST_REL_MAX  # False for NaN
    return [
        (f"{'PASS' if ohlc_ok else 'FAIL'} native OHLC {where}: {agreement.ohlc_share:.4%} "
         f"of {agreement.bars} bars agree {span} (need >= {NATIVE_AGREEMENT_MIN:.1%})"),
        (f"{'PASS' if volume_ok else 'FAIL'} native volume {where}: worst relative "
         f"difference {agreement.worst_volume_rel:.2e} (need <= {VOLUME_WORST_REL_MAX:.1e}) "
         f"{span}; {agreement.volume_share:.4%} of {agreement.bars} bars within "
         f"{VOLUME_REL_TOL:.0e}"),
    ], ohlc_ok and volume_ok


def load_training_series(data_dir: Path, symbol: str, last_day: Optional[date] = None) -> Series:
    """Training 1m bars (cut like the train stage) and the native warm-ups."""
    first = wib_day(ms(TRAIN_START))
    last = last_day if last_day is not None else wib_day(TRAIN_DATA_UNTIL_MS - 1)
    bars = slice_until(load_days(data_dir, "prod", symbol, "1m", first, last),
                       TRAIN_DATA_UNTIL_MS)
    return (bars, *read_warmups(data_dir, symbol))


def _cut(bars: Optional[Bars], t_ms: int) -> Optional[Bars]:
    return None if bars is None else slice_until(bars, t_ms)


def truncation_check(series: Mapping[str, Series], times: int, seed: int) -> Tuple[str, bool]:
    """Frames built on data cut at ``t`` give the full frame's snapshot and POC at ``t``.

    ``times`` seeded random times in the training range (symbols alternate);
    every time needs a full ``FeatureFrame.build``, so this is the slow check.
    """
    symbols = sorted(series)
    frames = {s: FeatureFrame.build(s, *series[s]) for s in symbols}
    rng = random.Random(seed)
    mismatches: List[str] = []
    for k in range(times):
        symbol = symbols[k % len(symbols)]
        bars, warm_1h, warm_4h = series[symbol]
        lo = max(ms(TRAIN_START), bars.open_ms[0]) + _MS_1M
        hi = min(ms(TRAIN_END), bars.open_ms[-1] + bars.interval_ms)
        t = rng.randrange(lo, hi)
        part = FeatureFrame.build(symbol, slice_until(bars, t), _cut(warm_1h, t), _cut(warm_4h, t))
        full = frames[symbol]
        if part.snapshot(t) != full.snapshot(t) or part.poc_24h(t) != full.poc_24h(t):
            mismatches.append(f"{symbol}@{t}")
    label = f"truncation invariance: {times} seeded times (seed {seed})"
    if mismatches:
        return f"FAIL {label}: {len(mismatches)} differ, first {mismatches[0]}", False
    return f"PASS {label}: snapshot and poc_24h identical", True


def cmd_check_data(args: argparse.Namespace) -> int:
    lines, all_ok = days_check(args.data_dir)
    for symbol in SYMBOLS:
        line, ok = funding_check(args.data_dir, symbol)
        lines.append(line)
        all_ok = all_ok and ok
        for interval in WARMUP_INTERVALS:
            native_lines, ok = native_check(args.data_dir, symbol, interval)
            lines.extend(native_lines)
            all_ok = all_ok and ok
    try:
        series = {s: load_training_series(args.data_dir, s) for s in SYMBOLS}
    except MissingDayError as exc:
        lines.append(f"FAIL truncation invariance: cannot load the training data "
                     f"({common.one_line(exc)})")
        all_ok = False
    else:
        line, ok = truncation_check(series, args.truncation_times, args.seed)
        lines.append(line)
        all_ok = all_ok and ok
    for line in lines:
        print(line)
    return 0 if all_ok else 1


def add_parsers(sub: Any, date_type: Callable[[str], date],
                symbols_type: Callable[[str], List[str]]) -> None:
    p = sub.add_parser("fetch", help="fetch missing 1m WIB day files (days before today WIB)")
    p.add_argument("--source", choices=sorted(HOSTS), required=True)
    p.add_argument("--symbols", type=symbols_type, required=True, help="e.g. BTC-USDT,ETH-USDT")
    p.add_argument("--start", type=date_type, required=True)
    p.add_argument("--end", type=date_type, required=True, help="inclusive WIB day")
    p.set_defaults(handler=cmd_fetch)

    p = sub.add_parser("warmup", help="native 1h/4h warm-up bars (production)")
    p.add_argument("--start", type=date_type, required=True)
    p.add_argument("--end", type=date_type, required=True, help="exclusive (WIB midnight)")
    p.set_defaults(handler=cmd_warmup)

    p = sub.add_parser("native-check-fetch",
                       help="native 1h/4h bars inside training, to check the 1m aggregation")
    p.add_argument("--start", type=date_type, default=NATIVE_CHECK_DEFAULT[0])
    p.add_argument("--end", type=date_type, default=NATIVE_CHECK_DEFAULT[1],
                   help="exclusive (WIB midnight)")
    p.set_defaults(handler=cmd_native_check_fetch)

    p = sub.add_parser("funding", help="production funding times")
    p.add_argument("--start", type=date_type, required=True)
    p.add_argument("--end", type=date_type, required=True, help="exclusive (WIB midnight)")
    p.set_defaults(handler=cmd_funding)

    p = sub.add_parser("collect", help="daily collector: the last 7 WIB days, missing ones only")
    p.set_defaults(handler=cmd_collect)

    p = sub.add_parser("check-data", help="complete days, funding cadence, native agreement, "
                                          "truncation test")
    p.add_argument("--truncation-times", type=common.positive_int, default=500)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(handler=cmd_check_data)
