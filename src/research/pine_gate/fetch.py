"""BingX public kline and funding fetcher plus the daily collector.

Research code; the bot never imports it. Public market-data endpoints only: no
keys, no signing, no orders. Every network and clock dependency is injected
(``http_get``, ``sleep``) so the module is testable without a network.

Rules enforced here:

* fixed ``startTime`` + ``endTime`` windows, never pagination on ``startTime``
  alone;
* at least ``MIN_REQUEST_GAP_SECONDS`` between two requests of one job;
* every parsed row is validated and a violation raises ``ExchangeError``;
* a day is stored only when it holds exactly 1,440 consecutive 1m bars.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Tuple

import httpx

from .bars import (
    Row,
    bars_from_rows,
    day_path,
    interval_to_ms,
    wib_day_start_ms,
    write_day,
)

PROD_HOST = "https://open-api.bingx.com"
VST_HOST = "https://open-api-vst.bingx.com"
KLINES_PATH = "/openApi/swap/v3/quote/klines"
FUNDING_PATH = "/openApi/swap/v2/quote/fundingRate"
HEADERS = {"X-SOURCE-KEY": "BX-AI-SKILL"}

MIN_REQUEST_GAP_SECONDS = 1.0
MAX_KLINES_PER_REQUEST = 1440
MAX_FUNDING_PER_REQUEST = 1000
BARS_PER_DAY = 1440
DEFAULT_RETRIES = 3
REQUEST_TIMEOUT_SECONDS = 20
_MS_PER_DAY = 86_400_000
_ALLOWED_HOSTS = frozenset({PROD_HOST, VST_HOST})
_MAX_REASON_CHARS = 200

HttpGet = Callable[[str, Mapping[str, str], Mapping[str, str]], Any]
Sleep = Callable[[float], Any]

_log = logging.getLogger(__name__)


class ExchangeError(RuntimeError):
    """The exchange answered with an error or with data that fails validation."""


class IncompleteDayError(RuntimeError):
    """A window returned fewer bars than the window holds."""


_TRANSIENT_ERRORS = (IncompleteDayError, ExchangeError, httpx.TransportError)


def httpx_get(
    url: str, params: Mapping[str, str], headers: Mapping[str, str]
) -> Any:  # pragma: no cover
    """Real transport: GET without redirects; non-2xx and 3xx raise ``ExchangeError``."""
    response = httpx.get(
        url,
        params=dict(params),
        headers=dict(headers),
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
    )
    status = response.status_code
    if not 200 <= status < 300:
        raise ExchangeError(f"HTTP {status} from {httpx.URL(url).host}")
    try:
        return response.json()
    except ValueError as exc:
        raise ExchangeError("response body is not JSON") from exc


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


def _get_data_list(http_get: HttpGet, url: str, params: Mapping[str, str]) -> list:
    payload = http_get(url, params, HEADERS)
    if not isinstance(payload, Mapping):
        raise ExchangeError("response is not a JSON object")
    code = payload.get("code")
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


def _call_with_retries(attempt: Callable[[], Any], retries: int) -> Any:
    if retries < 1:
        raise ValueError("retries must be at least 1")
    last: Optional[BaseException] = None
    for _ in range(retries):
        try:
            return attempt()
        except _TRANSIENT_ERRORS as exc:
            last = exc
    assert last is not None
    raise last


def _fetch_complete_window(
    http_get: HttpGet,
    host: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    retries: int,
) -> List[Row]:
    expected = (end_ms - start_ms + 1) // interval_to_ms(interval)

    def attempt() -> List[Row]:
        rows = fetch_window(http_get, host, symbol, interval, start_ms, end_ms)
        if len(rows) != expected:
            raise IncompleteDayError(
                f"{symbol} {interval} {start_ms}..{end_ms}: got {len(rows)} of {expected} bars"
            )
        return rows

    return _call_with_retries(attempt, retries)


def fetch_day(
    http_get: HttpGet,
    host: str,
    symbol: str,
    day: date,
    retries: int = DEFAULT_RETRIES,
    sleep: Sleep = time.sleep,
) -> List[Row]:
    """Fetch one WIB day of 1m bars; exactly 1,440 consecutive bars or an error.

    Up to ``retries`` attempts in total (at least one second apart), then the
    last error is raised (``IncompleteDayError`` for a short day).
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
    )


@dataclass(frozen=True)
class SyncResult:
    fetched: int
    skipped: int
    failed: Tuple[date, ...]


def _sync_one_day(
    http_get: HttpGet,
    host: str,
    source: str,
    symbol: str,
    day: date,
    data_dir: Path,
    sleep: Sleep,
) -> str:
    """Store one day unless its file exists; returns ``"fetched"`` or ``"skipped"``."""
    path = day_path(data_dir, source, symbol, "1m", day)
    if path.exists():
        return "skipped"
    rows = fetch_day(http_get, host, symbol, day, sleep=sleep)
    bars_from_rows(rows, interval_to_ms("1m"))  # contiguity check before storing
    write_day(path, rows)
    return "fetched"


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
    recorded in ``failed`` and nothing is written for it.
    """
    if end_day < start_day:
        raise ValueError(f"end_day {end_day} is before start_day {start_day}")
    paced = _throttled(http_get, sleep)
    fetched = skipped = 0
    failed: List[date] = []
    day = start_day
    while day <= end_day:
        try:
            outcome = _sync_one_day(paced, host, source, symbol, day, data_dir, sleep)
        except _TRANSIENT_ERRORS as exc:
            failed.append(day)
            _log.warning("%s/%s/1m %s: failed: %s", source, symbol, day, _reason(exc))
        else:
            if outcome == "fetched":
                fetched += 1
            else:
                skipped += 1
            _log.info("%s/%s/1m %s: %s", source, symbol, day, outcome)
        day += timedelta(days=1)
    return SyncResult(fetched, skipped, tuple(failed))


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

    Windows of at most 1,440 bars are requested in order; the file is written
    only when every bar of the range was received.
    """
    interval_ms = interval_to_ms(interval)
    if start_ms % interval_ms or end_ms % interval_ms or end_ms <= start_ms:
        raise ValueError(
            f"range {start_ms}..{end_ms} is not a non-empty whole number of {interval} bars"
        )
    paced = _throttled(http_get, sleep)
    window_ms = MAX_KLINES_PER_REQUEST * interval_ms
    rows: List[Row] = []
    cursor = start_ms
    while cursor < end_ms:
        window_end = min(cursor + window_ms, end_ms)
        rows.extend(
            _fetch_complete_window(
                paced, host, symbol, interval, cursor, window_end - 1, DEFAULT_RETRIES
            )
        )
        cursor = window_end
    bars_from_rows(rows, interval_ms)  # contiguity check before storing
    write_day(path, rows)
    _log.info("prod/%s/%s warmup: %d bars", symbol, interval, len(rows))


def _funding_window(
    http_get: HttpGet, host: str, symbol: str, start_ms: int, end_ms: int
) -> List[int]:
    params = {
        "symbol": symbol,
        "startTime": str(start_ms),
        "endTime": str(end_ms),
        "limit": str(MAX_FUNDING_PER_REQUEST),
    }
    data = _get_data_list(http_get, host + FUNDING_PATH, params)
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
        return _funding_window(http_get, host, symbol, start_ms, middle) + _funding_window(
            http_get, host, symbol, middle + 1, end_ms
        )
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

    Each request carries both bounds and asks for at most 1,000 rows; a window
    that fills the limit is split in two rather than paginated.
    """
    _check_host(host)
    if end_ms < start_ms:
        raise ValueError(f"end_ms {end_ms} is before start_ms {start_ms}")
    return _funding_window(_throttled(http_get, sleep), host, symbol, start_ms, end_ms)


def _reason(exc: BaseException) -> str:
    text = " ".join(f"{type(exc).__name__}: {exc}".split())
    return text[:_MAX_REASON_CHARS]


def collect(
    http_get: HttpGet,
    data_dir: Path,
    today: date,
    sleep: Sleep = time.sleep,
) -> List[str]:
    """Fetch yesterday's (WIB) 1m bars for the three collector jobs.

    Returns one log line per job. A failing job never stops the others.
    """
    day = today - timedelta(days=1)
    jobs = (
        ("vst", VST_HOST, "ETH-USDT"),
        ("prod", PROD_HOST, "BTC-USDT"),
        ("prod", PROD_HOST, "ETH-USDT"),
    )
    paced = _throttled(http_get, sleep)
    lines: List[str] = []
    for source, host, symbol in jobs:
        label = f"{source}/{symbol}/1m {day.isoformat()}"
        try:
            outcome = _sync_one_day(paced, host, source, symbol, day, Path(data_dir), sleep)
        except _TRANSIENT_ERRORS as exc:
            outcome = f"failed: {_reason(exc)}"
        line = f"{label}: {outcome}"
        _log.info("%s", line)
        lines.append(line)
    return lines
