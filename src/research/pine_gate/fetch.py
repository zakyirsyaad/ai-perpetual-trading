"""BingX public kline and funding fetcher plus the daily collector.

Research code; the bot never imports it. Public market-data endpoints only: no
keys, no signing, no orders. Every network and clock dependency is injected
(``http_get``, ``sleep``) so the module is testable without a network.

Rules enforced here:

* fixed ``startTime`` + ``endTime`` windows, never pagination on ``startTime``
  alone, at most ``MAX_SPAN_DAYS`` per call and ``MAX_FUNDING_REQUESTS`` per
  funding fetch;
* at least ``MIN_REQUEST_GAP_SECONDS`` between two requests of one job;
* every parsed row is validated and a violation raises ``ExchangeError``;
* only transport errors, short windows and HTTP 5xx are retried quickly, and the
  in-body code 100410 ("please retry later") is retried with a 5/15/45 s backoff; HTTP
  403/418/429 raise ``RateLimitedError``, which is never retried and stops the
  job; a range stops after ``MAX_CONSECUTIVE_FAILED_DAYS`` failed days in a row;
* a day is stored only when it holds exactly 1,440 consecutive 1m bars, and a
  day file is never replaced (another writer's file wins).
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, List, Mapping, Optional, Tuple

import httpx

from .bars import (
    Row,
    bars_from_rows,
    day_path,
    interval_to_ms,
    wib_day_start_ms,
    write_day_exclusive,
)
from .fileio import exists_message

PROD_HOST = "https://open-api.bingx.com"
VST_HOST = "https://open-api-vst.bingx.com"
KLINES_PATH = "/openApi/swap/v3/quote/klines"
FUNDING_PATH = "/openApi/swap/v2/quote/fundingRate"
HEADERS = {"X-SOURCE-KEY": "BX-AI-SKILL"}

MIN_REQUEST_GAP_SECONDS = 1.0
MAX_KLINES_PER_REQUEST = 1440
MAX_FUNDING_PER_REQUEST = 1000
MAX_WARMUP_BARS_PER_REQUEST = 1000
MAX_FUNDING_WINDOW_DAYS = 30
TRANSIENT_BACKOFF_SECONDS = (5, 15, 45)
TRANSIENT_EXCHANGE_CODE = 100410
BARS_PER_DAY = 1440
DEFAULT_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 20
MAX_BODY_BYTES = 4 * 1024 * 1024  # a full kline or funding window is far below 1 MB
RATE_LIMIT_STATUSES = frozenset({403, 418, 429})
_MS_PER_DAY = 86_400_000
SOURCE_HOSTS = {"prod": PROD_HOST, "vst": VST_HOST}
_ALLOWED_HOSTS = frozenset(SOURCE_HOSTS.values())
MAX_SPAN_DAYS = 900  # training + holdout is about 440 days
MAX_CONSECUTIVE_FAILED_DAYS = 3
COLLECT_BACKFILL_DAYS = 7
_MAX_REASON_CHARS = 200

HttpGet = Callable[[str, Mapping[str, str], Mapping[str, str]], Any]
Sleep = Callable[[float], Any]

_log = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    """The exchange answered with an error or with data that fails validation."""


class TransientExchangeError(ExchangeError):
    """In-body code 100410 ("please retry later"); retried with backoff, then an
    ordinary failure. It is not a rate limit and never stops a job."""


class IncompleteDayError(RuntimeError):
    """A window returned fewer bars than the window holds."""


class HttpStatusError(ExchangeError):
    """A non-2xx answer; only 5xx is worth retrying."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        return 500 <= self.status < 600


class RateLimitedError(RuntimeError):
    """HTTP 403/418/429: the venue asks the client to stop. Never retried; it stops the
    whole job or command (its message reports any Retry-After)."""


MAX_FUNDING_REQUESTS = 500
# A failed day is recorded and the range goes on; a rate limit stops it.
_DAY_ERRORS = (ExchangeError, IncompleteDayError, httpx.HTTPError)
# The collector also survives local and argument errors of one job.
_JOB_ERRORS = _DAY_ERRORS + (OSError, ValueError)


def check_status(status: int, headers: Mapping[str, str], host: str) -> None:
    """Raise for a non-2xx status: ``RateLimitedError`` for 403/418/429, else
    ``HttpStatusError`` (3xx included: redirects are never followed)."""
    if 200 <= status < 300:
        return
    if status in RATE_LIMIT_STATUSES:
        retry = headers.get("Retry-After")
        hint = f"; Retry-After {' '.join(str(retry).split())[:32]!r}" if retry else ""
        raise RateLimitedError(f"HTTP {status} from {host}{hint}")
    raise HttpStatusError(status, f"HTTP {status} from {host}")


def read_capped(chunks: Iterable[bytes], content_length: Optional[str]) -> bytes:
    """The body, refused (``ExchangeError``) when it is larger than ``MAX_BODY_BYTES``,
    by its Content-Length before reading or by its size while reading."""
    if content_length is not None and content_length.strip().isdigit():
        if int(content_length) > MAX_BODY_BYTES:
            raise ExchangeError(f"response body of {int(content_length)} bytes exceeds the cap")
    body = bytearray()
    for chunk in chunks:
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise ExchangeError(f"response body exceeds {MAX_BODY_BYTES} bytes")
    return bytes(body)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON number {name}")


def parse_json_body(body: bytes) -> Any:
    """Strict JSON (NaN and Infinity refused); anything else is an ``ExchangeError``."""
    try:
        return json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
    except ValueError as exc:  # includes UnicodeDecodeError and JSONDecodeError
        raise ExchangeError("response body is not JSON") from exc


def httpx_get(url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> Any:
    """Real transport: one streamed GET, no redirects, no proxy or other environment
    settings (``trust_env=False``); the status is checked before the body is read and
    the body is capped at ``MAX_BODY_BYTES``."""
    with httpx.stream(
        "GET",
        url,
        params=dict(params),
        headers=dict(headers),
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    ) as response:
        check_status(response.status_code, response.headers, httpx.URL(url).host)
        body = read_capped(response.iter_bytes(), response.headers.get("Content-Length"))
    return parse_json_body(body)


class _Throttled:
    """Wrap ``http_get`` so every request after the first is preceded by a sleep."""

    def __init__(self, http_get: HttpGet, sleep: Sleep) -> None:
        self._http_get = http_get
        self._sleep = sleep
        self._started = False

    def __call__(
        self, url: str, params: Mapping[str, str], headers: Mapping[str, str]
    ) -> Any:
        if self._started:
            self._sleep(MIN_REQUEST_GAP_SECONDS)
        self._started = True
        return self._http_get(url, params, headers)


def _throttled(http_get: HttpGet, sleep: Sleep) -> HttpGet:
    """Throttle once: an already throttled getter is reused so gaps never double."""
    if isinstance(http_get, _Throttled):
        return http_get
    return _Throttled(http_get, sleep)


def _check_host(host: str) -> None:
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"host {host!r} is not a known BingX public host")


def _check_source(source: str, host: str) -> None:
    """Day files of ``source`` come from its own host only (prod <-> PROD_HOST, vst <-> VST_HOST)."""
    if SOURCE_HOSTS.get(source) != host:
        raise ValueError(f"source {source!r} does not match host {host!r}")


def _check_span_ms(start_ms: int, end_ms: int) -> None:
    if end_ms - start_ms > MAX_SPAN_DAYS * _MS_PER_DAY:
        raise ValueError(f"range {start_ms}..{end_ms} spans more than {MAX_SPAN_DAYS} days")


def _get_data_list(http_get: HttpGet, url: str, params: Mapping[str, str]) -> list:
    payload = http_get(url, params, HEADERS)
    if not isinstance(payload, Mapping):
        raise ExchangeError("response is not a JSON object")
    code = payload.get("code")
    # Code 100410 ("请稍后重试", "please retry later") is a transient in-body answer: the
    # VPS run of 2026-10-15 had 11 days fail on it after the quick retries, and a plain
    # re-run filled them. It is retried with backoff (``_call_with_retries``) and is
    # not a rate limit, so nothing stops the job.
    if code == TRANSIENT_EXCHANGE_CODE:
        raise TransientExchangeError(f"exchange code {code!r}: {str(payload.get('msg'))[:100]}")
    if code != 0:
        raise ExchangeError(f"exchange code {code!r}: {str(payload.get('msg'))[:100]}")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ExchangeError("response data is not a list")
    return data


def _as_int_ms(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise ExchangeError(f"{what} is not an integer: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ExchangeError(f"{what} is not an integer: {value!r}")


def _as_float(value: Any, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ExchangeError(f"{what} is not numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise ExchangeError(f"{what} is not finite: {value!r}")
    return number


def _parse_kline(raw: Any, interval_ms: int, start_ms: int, end_ms: int) -> Row:
    if not isinstance(raw, Mapping):
        raise ExchangeError(f"kline row is not an object: {raw!r}")
    try:
        t = _as_int_ms(raw["time"], "time")
        values = {k: _as_float(raw[k], k) for k in ("open", "high", "low", "close", "volume")}
    except KeyError as exc:
        raise ExchangeError(f"kline row lacks field {exc}") from exc
    o, h, low, c, v = (values[k] for k in ("open", "high", "low", "close", "volume"))
    if not start_ms <= t <= end_ms:
        raise ExchangeError(f"bar {t} lies outside the window {start_ms}..{end_ms}")
    if t % interval_ms:
        raise ExchangeError(f"bar {t} is not a multiple of the {interval_ms} ms interval")
    if min(o, h, low, c) <= 0 or v < 0:
        raise ExchangeError(f"bar {t} has a non-positive price or negative volume")
    if h < max(o, c) or low > min(o, c):
        raise ExchangeError(f"bar {t} high/low do not bracket open and close")
    return (t, o, h, low, c, v)


def fetch_window(
    http_get: HttpGet,
    host: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
) -> List[Row]:
    """Fetch the bars of ``[start_ms, end_ms]`` (inclusive), sorted ascending.

    The window must hold a whole number of 1..1,440 bars. Rows outside it,
    duplicated or invalid raise ``ExchangeError``; a short answer is returned
    as is, and completeness is the caller's check.
    """
    _check_host(host)
    interval_ms = interval_to_ms(interval)
    span = end_ms - start_ms + 1
    if start_ms % interval_ms or span <= 0 or span % interval_ms:
        raise ValueError(
            f"window {start_ms}..{end_ms} is not a whole number of {interval} bars"
        )
    count = span // interval_ms
    if count > MAX_KLINES_PER_REQUEST:
        raise ValueError(f"window holds {count} bars, more than {MAX_KLINES_PER_REQUEST}")
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": str(count),
    }
    data = _get_data_list(http_get, host + KLINES_PATH, params)
    rows = sorted(_parse_kline(raw, interval_ms, start_ms, end_ms) for raw in data)
    for previous, current in zip(rows, rows[1:]):
        if previous[0] == current[0]:
            raise ExchangeError(f"duplicate bar at {current[0]}")
    return rows


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, HttpStatusError):
        return exc.retryable
    return isinstance(exc, (httpx.TransportError, IncompleteDayError))


def _call_with_retries(attempt: Callable[[], Any], retries: int, sleep: Sleep) -> Any:
    """Up to ``retries`` attempts for ``_retryable`` errors, and, separately, one more
    attempt after each ``TRANSIENT_BACKOFF_SECONDS`` sleep for code 100410."""
    if retries < 1:
        raise ValueError("retries must be at least 1")
    waits = list(TRANSIENT_BACKOFF_SECONDS)
    quick_left = retries
    while True:
        try:
            return attempt()
        except TransientExchangeError:
            if not waits:
                raise
            sleep(waits.pop(0))
        except (httpx.TransportError, IncompleteDayError, HttpStatusError) as exc:
            quick_left -= 1
            if not _retryable(exc) or quick_left <= 0:
                raise


def _fetch_complete_window(
    http_get: HttpGet,
    host: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    retries: int,
    sleep: Sleep,
) -> List[Row]:
    expected = (end_ms - start_ms + 1) // interval_to_ms(interval)

    def attempt() -> List[Row]:
        rows = fetch_window(http_get, host, symbol, interval, start_ms, end_ms)
        if len(rows) != expected:
            raise IncompleteDayError(
                f"{symbol} {interval} {start_ms}..{end_ms}: got {len(rows)} of {expected} bars"
            )
        return rows

    return _call_with_retries(attempt, retries, sleep)


def fetch_day(
    http_get: HttpGet,
    host: str,
    symbol: str,
    day: date,
    retries: int = DEFAULT_RETRIES,
    sleep: Sleep = time.sleep,
) -> List[Row]:
    """Fetch one WIB day of 1m bars; exactly 1,440 consecutive bars or an error.

    Up to ``retries`` attempts in total (at least one second apart), plus the
    100410 backoff retries, then the last error is raised (``IncompleteDayError``
    for a short day).
    """
    start_ms = wib_day_start_ms(day)
    return _fetch_complete_window(
        _throttled(http_get, sleep),
        host,
        symbol,
        "1m",
        start_ms,
        start_ms + _MS_PER_DAY - 1,
        retries,
        sleep,
    )


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    skipped: int
    failed: Tuple[date, ...]
    not_attempted: Tuple[date, ...] = ()  # after MAX_CONSECUTIVE_FAILED_DAYS failures


def _sync_one_day(
    http_get: HttpGet,
    host: str,
    source: str,
    symbol: str,
    day: date,
    data_dir: Path,
    sleep: Sleep,
) -> str:
    """Store one day unless its file exists; returns ``"fetched"`` or ``"skipped"``
    (also when another writer stored the day while it was being fetched)."""
    path = day_path(data_dir, source, symbol, "1m", day)
    if path.exists():
        return "skipped"
    rows = fetch_day(http_get, host, symbol, day, sleep=sleep)
    bars_from_rows(rows, interval_to_ms("1m"))  # contiguity check before storing
    return "fetched" if write_day_exclusive(path, rows) else "skipped"


def sync_days(
    http_get: HttpGet,
    host: str,
    source: str,
    symbol: str,
    start_day: date,
    end_day: date,
    data_dir: Path,
    sleep: Sleep = time.sleep,
) -> SyncResult:
    """Fetch every missing WIB day of ``[start_day, end_day]`` into day files.

    Existing files are skipped. A day that cannot be fetched completely is
    recorded in ``failed`` and nothing is written for it; after
    ``MAX_CONSECUTIVE_FAILED_DAYS`` failed days in a row the remaining days are
    ``not_attempted``. ``RateLimitedError`` propagates and stops the range.
    """
    if end_day < start_day:
        raise ValueError(f"end_day {end_day} is before start_day {start_day}")
    days = [start_day + timedelta(days=k) for k in range((end_day - start_day).days + 1)]
    if len(days) > MAX_SPAN_DAYS:
        raise ValueError(f"{start_day}..{end_day} spans more than {MAX_SPAN_DAYS} days")
    _check_source(source, host)
    paced = _throttled(http_get, sleep)
    counts = {"fetched": 0, "skipped": 0}
    failed: List[date] = []
    streak = 0
    for i, day in enumerate(days):
        try:
            outcome = _sync_one_day(paced, host, source, symbol, day, data_dir, sleep)
        except _DAY_ERRORS as exc:
            failed.append(day)
            streak += 1
            _log.warning("%s/%s/1m %s: failed: %s", source, symbol, day, _reason(exc))
            if streak >= MAX_CONSECUTIVE_FAILED_DAYS:
                return SyncResult(counts["fetched"], counts["skipped"], tuple(failed),
                                  tuple(days[i + 1:]))
            continue
        streak = 0
        counts[outcome] += 1
        _log.info("%s/%s/1m %s: %s", source, symbol, day, outcome)
    return SyncResult(counts["fetched"], counts["skipped"], tuple(failed))


def sync_warmup(
    http_get: HttpGet,
    host: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    path: Path,
    sleep: Sleep = time.sleep,
) -> None:
    """Fetch native bars with open time in ``[start_ms, end_ms)`` into one gz file.

    Windows of at most ``MAX_WARMUP_BARS_PER_REQUEST`` bars are requested in order
    (the venue returns at most 1,000 bars for 1h and 4h; 1,440-bar windows came
    back short on the VPS); the file is written only when every bar was received.
    """
    interval_ms = interval_to_ms(interval)
    if start_ms % interval_ms or end_ms % interval_ms or end_ms <= start_ms:
        raise ValueError(
            f"range {start_ms}..{end_ms} is not a non-empty whole number of {interval} bars"
        )
    _check_span_ms(start_ms, end_ms)
    paced = _throttled(http_get, sleep)
    window_ms = MAX_WARMUP_BARS_PER_REQUEST * interval_ms
    rows: List[Row] = []
    cursor = start_ms
    while cursor < end_ms:
        window_end = min(cursor + window_ms, end_ms)
        rows.extend(
            _fetch_complete_window(
                paced, host, symbol, interval, cursor, window_end - 1, DEFAULT_RETRIES, sleep
            )
        )
        cursor = window_end
    bars_from_rows(rows, interval_ms)  # contiguity check before storing
    if not write_day_exclusive(path, rows):
        raise FileExistsError(exists_message(path, "warm-up"))
    _log.info("prod/%s/%s warmup: %d bars", symbol, interval, len(rows))


class _RequestBudget:
    """At most ``limit`` requests; one more raises ``ExchangeError``."""

    def __init__(self, limit: int) -> None:
        self._left = limit
        self._limit = limit

    def take(self) -> None:
        if self._left <= 0:
            raise ExchangeError(f"funding fetch needs more than {self._limit} requests")
        self._left -= 1


def _funding_window(
    http_get: HttpGet, host: str, symbol: str, start_ms: int, end_ms: int,
    budget: _RequestBudget, sleep: Sleep,
) -> List[int]:
    budget.take()
    params = {
        "symbol": symbol,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": str(MAX_FUNDING_PER_REQUEST),
    }
    data = _call_with_retries(
        lambda: _get_data_list(http_get, host + FUNDING_PATH, params), 1, sleep)
    times: List[int] = []
    for raw in data:
        if not isinstance(raw, Mapping) or "fundingTime" not in raw:
            raise ExchangeError(f"funding row lacks fundingTime: {raw!r}")
        t = _as_int_ms(raw["fundingTime"], "fundingTime")
        if not start_ms <= t <= end_ms:
            raise ExchangeError(f"funding time {t} lies outside {start_ms}..{end_ms}")
        times.append(t)
    times.sort()
    for previous, current in zip(times, times[1:]):
        if previous == current:
            raise ExchangeError(f"duplicate funding time {current}")
    if len(times) >= MAX_FUNDING_PER_REQUEST and start_ms < end_ms:
        # The answer may have been cut at the limit: split the window by time
        # and fetch both halves with fixed bounds.
        middle = (start_ms + end_ms) // 2
        return (_funding_window(http_get, host, symbol, start_ms, middle, budget, sleep)
                + _funding_window(http_get, host, symbol, middle + 1, end_ms, budget, sleep))
    return times


def fetch_funding_times(
    http_get: HttpGet,
    host: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    sleep: Sleep = time.sleep,
) -> List[int]:
    """Funding times (UTC ms) inside ``[start_ms, end_ms]``, sorted, no duplicates.

    The range is fetched in fixed windows of at most ``MAX_FUNDING_WINDOW_DAYS`` days
    (the venue silently truncates longer windows at their start). Each request
    carries both bounds and asks for at most 1,000 rows; a window that fills the
    limit is split in two rather than paginated, with at most
    ``MAX_FUNDING_REQUESTS`` requests in all.
    """
    _check_host(host)
    if end_ms < start_ms:
        raise ValueError(f"end_ms {end_ms} is before start_ms {start_ms}")
    _check_span_ms(start_ms, end_ms)
    paced = _throttled(http_get, sleep)
    budget = _RequestBudget(MAX_FUNDING_REQUESTS)
    window_ms = MAX_FUNDING_WINDOW_DAYS * _MS_PER_DAY
    times: List[int] = []
    cursor = start_ms
    while cursor <= end_ms:
        window_end = min(cursor + window_ms - 1, end_ms)
        times.extend(_funding_window(paced, host, symbol, cursor, window_end, budget, sleep))
        cursor = window_end + 1
    return times


def _reason(exc: BaseException) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text[:_MAX_REASON_CHARS]


def sync_line(label: str, result: SyncResult) -> str:
    """``<label>: fetched N, skipped M, failed K`` plus the failed and not-attempted days.

    A line with any failure contains ``failed:``; a clean line never does."""
    line = f"{label}: fetched {result.fetched}, skipped {result.skipped}, failed {len(result.failed)}"
    if result.failed:
        line += "; failed: " + ", ".join(d.isoformat() for d in result.failed)
    if result.not_attempted:
        line += "; not attempted: " + ", ".join(d.isoformat() for d in result.not_attempted)
    return line


COLLECT_JOBS = (
    ("vst", VST_HOST, "ETH-USDT"),
    ("prod", PROD_HOST, "BTC-USDT"),
    ("prod", PROD_HOST, "ETH-USDT"),
)


def collect(
    http_get: HttpGet,
    data_dir: Path,
    today: date,
    sleep: Sleep = time.sleep,
) -> List[str]:
    """Sync the last ``COLLECT_BACKFILL_DAYS`` WIB days (to yesterday) for each job.

    Existing days are skipped, so a day missed earlier is fetched on a later run.
    Returns one line per job (``sync_line``, or ``<label>: failed: <reason>``); a
    failing job never stops the others, except a rate limit, after which the
    remaining jobs are not attempted.
    """
    start, end = today - timedelta(days=COLLECT_BACKFILL_DAYS), today - timedelta(days=1)
    paced = _throttled(http_get, sleep)
    lines: List[str] = []
    limited = False
    for source, host, symbol in COLLECT_JOBS:
        label = f"{source}/{symbol}/1m {start.isoformat()}..{end.isoformat()}"
        if limited:
            line = f"{label}: failed: not attempted (rate limited)"
        else:
            try:
                line = sync_line(label, sync_days(paced, host, source, symbol, start, end,
                                                  Path(data_dir), sleep=sleep))
            except RateLimitedError as exc:
                limited, line = True, f"{label}: failed: {_reason(exc)}"
            except _JOB_ERRORS as exc:
                line = f"{label}: failed: {_reason(exc)}"
        _log.info("%s", line)
        lines.append(line)
    return lines
