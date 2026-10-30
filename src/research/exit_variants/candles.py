"""Production 1m candles for the exit-variants report: a sensitivity read beside the VST cache.

Research code; the bot never imports it. The registered read of the study replays the VST candles of the cache
(``report_exit_variants --klines``). VST is a demo matching engine, and now and then a sweep of its thin book prints a
1m extreme the real market never had (a low 39 bps under production's on 29 Oct 2026, a print 10 % under it on 30 Oct)
that the bot's own 5-second sampling may or may not have seen. This module downloads the same minutes from the public
production endpoint (no keys, no signing) and returns them as the walker's ``Cols``, so the same study can be run on them.

It reuses the validated window fetch of ``research.pine_gate.fetch`` and its rules: fixed ``startTime`` + ``endTime``
windows of at most 1,440 bars, at least a second between two requests, the in-body code 100410 ("retry later") retried
after 5, 15 and 45 seconds, HTTP 403, 418 and 429 raising ``RateLimitedError`` at once, and every row validated. The
network and the clock are injected (``http_get``, ``sleep``), so tests never touch the venue. A minute the venue does not
return stays a hole in the columns: the study then lists the trades that need it as skipped instead of guessing.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

import httpx

from research.exit_variants.walker import Cols
from research.pine_gate import fetch as venue
from research.pine_gate.features import INTERVAL_1M

__all__ = ["MAX_WINDOW_DAYS", "SYMBOL", "production_cols", "window_for"]

SYMBOL = "BTC-USDT"
MAX_WINDOW_DAYS = 100  # a day of candles is a request: a typo in --since must not turn into hundreds of them
# After the latest open the walk reads up to the bar ``hold`` minutes on; the end of the window is exclusive and one more
# minute is kept as margin.
_MARGIN_MINUTES = 2


def _minute_ms(moment: datetime) -> int:
    return int(moment.timestamp()) // 60 * INTERVAL_1M


def window_for(opened: Sequence[datetime], hold_minutes: int, now: datetime) -> Optional[tuple[int, int]]:
    """``(start_ms, stop_ms)``, the minutes the walks of trades opened at ``opened`` can read, cut at the last closed minute.

    The walk of a trade starts at the first full bar after its open minute and ends at the bar ``hold_minutes`` after
    that minute, so the window runs from the earliest open minute to ``hold_minutes + 2`` minutes after the latest one.
    The bar of the current minute is still forming and is never asked for. ``None`` when nothing closed is left to fetch."""
    if not opened:
        return None
    start = _minute_ms(min(opened))
    stop = min(_minute_ms(max(opened)) + (hold_minutes + _MARGIN_MINUTES) * INTERVAL_1M, _minute_ms(now))
    return (start, stop) if stop > start else None


class _Paced:
    """``http_get`` with a pause before every request after the first, so a run never goes faster than the fetcher's rule."""

    def __init__(self, http_get: venue.HttpGet, sleep: venue.Sleep) -> None:
        self._http_get = http_get
        self._sleep = sleep
        self._started = False

    def __call__(self, url: str, params: Mapping[str, str], headers: Mapping[str, str]) -> Any:
        if self._started:
            self._sleep(venue.MIN_REQUEST_GAP_SECONDS)
        self._started = True
        return self._http_get(url, params, headers)


def _window_rows(http_get: venue.HttpGet, start_ms: int, end_ms: int, sleep: venue.Sleep) -> list:
    """The rows of one window, retrying what the fetcher retries: the transient code after 5, 15 and 45 seconds, and a
    dropped connection or a 5xx up to ``DEFAULT_RETRIES`` attempts in all. A rate limit or a 4xx raises at once."""
    waits = list(venue.TRANSIENT_BACKOFF_SECONDS)
    attempts = venue.DEFAULT_RETRIES
    while True:
        try:
            return venue.fetch_window(http_get, venue.PROD_HOST, SYMBOL, "1m", start_ms, end_ms)
        except venue.TransientExchangeError:
            if not waits:
                raise
            sleep(waits.pop(0))
        except (httpx.TransportError, venue.HttpStatusError) as exc:
            attempts -= 1
            if attempts <= 0 or (isinstance(exc, venue.HttpStatusError) and not exc.retryable):
                raise


def production_cols(
    start_ms: int,
    stop_ms: int,
    *,
    http_get: venue.HttpGet = venue.httpx_get,
    sleep: venue.Sleep = time.sleep,
) -> Cols:
    """The production 1m bars with open time in ``[start_ms, stop_ms)`` as the walker's columns, oldest first.

    Both ends are whole minutes and the range is not empty, and it spans at most ``MAX_WINDOW_DAYS`` days: anything else
    raises ``ValueError`` before a request is made. ``RateLimitedError`` means the venue asked the client to stop;
    ``ExchangeError`` (``TransientExchangeError`` after the backoffs) and ``httpx.HTTPError`` mean it could not be read.
    A short answer is not an error: the minutes the venue left out are holes in the columns."""
    if start_ms % INTERVAL_1M or stop_ms % INTERVAL_1M or stop_ms <= start_ms:
        raise ValueError(f"the candle range {start_ms}..{stop_ms} is not a non-empty whole number of minutes")
    days = (stop_ms - start_ms) / (venue.BARS_PER_DAY * INTERVAL_1M)
    if days > MAX_WINDOW_DAYS:
        raise ValueError(f"the candle window of {days:.1f} days is longer than {MAX_WINDOW_DAYS}: narrow --since")
    paced = _Paced(http_get, sleep)
    window_ms = venue.MAX_KLINES_PER_REQUEST * INTERVAL_1M
    rows: list = []
    cursor = start_ms
    while cursor < stop_ms:
        end = min(cursor + window_ms, stop_ms)
        rows += _window_rows(paced, cursor, end - 1, sleep)
        cursor = end
    rows.sort()
    return Cols([row[0] for row in rows], [row[1] for row in rows], [row[2] for row in rows], [row[3] for row in rows])
