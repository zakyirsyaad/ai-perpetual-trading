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
    aggregate,
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
from .features import FeatureFrame, htf_agreement
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
from .pipeline import SYMBOLS, TRAIN_DATA_UNTIL_MS
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
    """``covers <first> .. <last>, N <unit>`` (WIB) for a file being skipped."""
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
    """Funding times in [WIB midnight of --start, of --end) per symbol."""
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
            times = saved if isinstance(saved, list) else []
            print(f"{label}: skipped, file exists ({covered(times, 'times')})")
            continue
        pace()
        try:
            times = fetch_funding_times(common.http_get, PROD_HOST, symbol, start_ms, end_ms,
                                        sleep=common.sleep)
        except _NETWORK_ERRORS as exc:
            failed_any = True
            print(f"{label}: failed: {common.one_line(exc)}")
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


def days_check(data_dir: Path) -> Tuple[List[str], bool]:
    """Every WIB day of the training start .. the last holdout day, prod BTC/ETH 1m."""
    first, last = wib_day(ms(TRAIN_START)), wib_day(ms(HOLDOUT_END) - 1)
    lines: List[str] = []
    for symbol in SYMBOLS:
        missing = [d for d in _days(first, last)
                   if not day_path(data_dir, "prod", symbol, "1m", d).exists()]
        if missing:
            shown = ", ".join(d.isoformat() for d in missing[:_LISTED_DAYS])
            more = ", ..." if len(missing) > _LISTED_DAYS else ""
            lines.append(f"  prod/{symbol}/1m missing {len(missing)} days: {shown}{more}")
    verdict = "FAIL" if lines else "PASS"
    head = f"{verdict} complete days: prod BTC/ETH 1m {first}..{last}"
    return [head] + lines, not lines


def read_warmups(data_dir: Path, symbol: str) -> Tuple[Optional[Bars], Optional[Bars]]:
    """Native 1h and 4h warm-up bars of ``symbol``; ``None`` for a missing file."""
    out: List[Optional[Bars]] = []
    for interval in WARMUP_INTERVALS:
        path = warmup_path(data_dir, symbol, interval)
        out.append(bars_from_rows(read_day(path), interval_to_ms(interval))
                   if path.exists() else None)
    return out[0], out[1]


def native_check(data_dir: Path, symbol: str, interval: str) -> Tuple[str, bool]:
    """Native bars of ``native-check-fetch`` vs the bars aggregated from 1m day files.

    Compared over their overlap; at least 99.9% must agree. A missing or empty
    native file, missing 1m days or no overlap all FAIL: the aggregation is
    unchecked then, which is never a pass.
    """
    label = f"native vs aggregated prod/{symbol}/{interval}"
    path = native_check_path(data_dir, symbol, interval)
    if not path.exists():
        return f"FAIL {label}: {path.name} missing; run native-check-fetch", False
    step = interval_to_ms(interval)
    native = bars_from_rows(read_day(path), step)
    if not len(native):
        return f"FAIL {label}: {path.name} holds no bars", False
    first = max(wib_day(native.open_ms[0]), wib_day(ms(TRAIN_START)))
    last = wib_day(native.open_ms[-1] + step - 1)
    if last < first:
        return f"FAIL {label}: no overlap with the 1m data (native bars precede it)", False
    try:
        bars = load_days(data_dir, "prod", symbol, "1m", first, last)
    except MissingDayError as exc:
        return f"FAIL {label}: {common.one_line(exc)}", False
    aggregated = aggregate(bars, step)
    shared = set(native.open_ms) & set(aggregated.open_ms)
    if not shared:
        return f"FAIL {label}: no overlap between the native and the aggregated bars", False
    share = htf_agreement(native, aggregated)
    ok = share >= NATIVE_AGREEMENT_MIN
    return (f"{'PASS' if ok else 'FAIL'} {label}: {share:.4%} of {len(shared)} bars agree "
            f"(need >= {NATIVE_AGREEMENT_MIN:.1%})"), ok


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
        for interval in WARMUP_INTERVALS:
            line, ok = native_check(args.data_dir, symbol, interval)
            lines.append(line)
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
    p = sub.add_parser("fetch", help="fetch missing 1m WIB day files")
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

    p = sub.add_parser("check-data", help="complete days, native agreement, truncation test")
    p.add_argument("--truncation-times", type=common.positive_int, default=500)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(handler=cmd_check_data)
