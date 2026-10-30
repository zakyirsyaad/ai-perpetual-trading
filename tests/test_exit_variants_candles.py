"""Tests of ``research.exit_variants.candles``: production 1m candles for the exit-variants report.

The network and the clock are injected, so nothing here touches the venue: ``FakeVenue`` answers like the public klines
endpoint and records every request, and ``sleeps.append`` records every wait.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest import mock

import httpx

from research.exit_variants import candles, study
from research.pine_gate import fetch as venue
from tests.exit_variants_helpers import RULES, position

UTC = timezone.utc
STEP = 60_000
MAX_BARS = 1_440
T0 = int(datetime(2026, 10, 29, 12, 0, tzinfo=UTC).timestamp()) * 1000  # a bar open
PRODUCTION_URL = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
TRANSIENT = {"code": 100410, "msg": "retry later"}
BACKOFF_THEN_PACING = [5, 1.0, 15, 1.0, 45, 1.0]  # a wait after each transient answer, a second before each new request


def kline(ms: int) -> dict[str, Any]:
    """The venue's kline object for the minute at ``ms``; prices rise one point a minute so a test can tell them apart."""
    base = 80_000.0 + (ms - T0) // STEP
    return {
        "time": ms,
        "open": f"{base:.1f}",
        "high": f"{base + 2:.1f}",
        "low": f"{base - 3:.1f}",
        "close": f"{base + 1:.1f}",
        "volume": "5.0",
    }


class FakeVenue:
    """The public klines endpoint. ``script`` holds answers or exceptions used first, one per request; after that it
    answers for real, without the minutes in ``holes``."""

    def __init__(self, holes: tuple[int, ...] = (), script: tuple = ()) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self._holes = set(holes)
        self._script = list(script)

    def __call__(self, url: str, params: Any, headers: Any) -> Any:
        self.calls.append((url, dict(params), dict(headers)))
        if self._script:
            step = self._script.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step
        first, last = int(params["startTime"]), int(params["endTime"])
        data = [kline(ms) for ms in range(first, last + 1, STEP) if ms not in self._holes]
        return {"code": 0, "data": list(reversed(data))}  # newest first, as the venue answers


def minute_ms(moment: datetime) -> int:
    return int(moment.timestamp()) // 60 * STEP


OPEN_A = datetime(2026, 10, 29, 11, 14, 1, tzinfo=UTC)
OPEN_B = datetime(2026, 10, 29, 12, 34, 23, tzinfo=UTC)
LONG_AFTER = datetime(2026, 10, 31, 0, 0, 30, tzinfo=UTC)


class WindowTests(unittest.TestCase):
    def test_it_runs_from_the_first_open_to_the_time_exit_bar_of_the_last(self) -> None:
        start, stop = candles.window_for([OPEN_B, OPEN_A], 60, LONG_AFTER)
        self.assertEqual(start, minute_ms(OPEN_A))
        # the walk of the last trade reads up to the bar 60 minutes after its open minute; the end is exclusive and one
        # more minute is kept as margin
        self.assertEqual(stop, minute_ms(OPEN_B) + 62 * STEP)

    def test_the_hold_time_sets_how_far_the_last_trade_reaches(self) -> None:
        _, stop = candles.window_for([OPEN_A, OPEN_B], 30, LONG_AFTER)
        self.assertEqual(stop, minute_ms(OPEN_B) + 32 * STEP)

    def test_it_stops_at_the_last_closed_minute(self) -> None:
        now = datetime(2026, 10, 29, 12, 40, 45, tzinfo=UTC)  # the 12:40 bar is still forming
        _, stop = candles.window_for([OPEN_A, OPEN_B], 60, now)
        self.assertEqual(stop, int(datetime(2026, 10, 29, 12, 40, tzinfo=UTC).timestamp()) * 1000)

    def test_no_trades_need_no_candles(self) -> None:
        self.assertIsNone(candles.window_for([], 60, LONG_AFTER))

    def test_a_trade_opened_in_the_current_minute_leaves_nothing_closed_to_fetch(self) -> None:
        self.assertIsNone(candles.window_for([OPEN_A], 60, OPEN_A.replace(second=21)))


class _Case(unittest.TestCase):
    def fetch(self, start: int, stop: int, fake: Optional[FakeVenue] = None) -> tuple[Any, FakeVenue, list[float]]:
        fake, sleeps = fake or FakeVenue(), []
        cols = candles.production_cols(start, stop, http_get=fake, sleep=sleeps.append)
        return cols, fake, sleeps


class ProductionColsTests(_Case):
    def test_it_asks_the_public_production_host_for_one_minute_bars_of_btc_usdt_without_credentials(self) -> None:
        _, fake, sleeps = self.fetch(T0, T0 + 10 * STEP)
        (url, params, headers), = fake.calls
        self.assertEqual(url, PRODUCTION_URL)
        self.assertEqual(
            params,
            {"symbol": "BTC-USDT", "interval": "1m", "startTime": str(T0), "endTime": str(T0 + 10 * STEP - 1), "limit": "10"},
        )
        self.assertEqual(headers, {"X-SOURCE-KEY": "BX-AI-SKILL"})
        self.assertEqual(sleeps, [])  # the first request waits for nothing

    def test_the_bars_come_back_in_time_order_as_the_walkers_columns(self) -> None:
        cols, _, _ = self.fetch(T0, T0 + 10 * STEP)
        self.assertEqual(list(cols.open_ms), [T0 + i * STEP for i in range(10)])
        self.assertEqual(list(cols.open), [80_000.0 + i for i in range(10)])
        self.assertEqual(list(cols.high), [80_002.0 + i for i in range(10)])
        self.assertEqual(list(cols.low), [79_997.0 + i for i in range(10)])

    def test_a_long_span_is_requested_in_windows_of_at_most_1440_bars_a_second_apart(self) -> None:
        cols, fake, sleeps = self.fetch(T0, T0 + (2 * MAX_BARS + 100) * STEP)
        sizes = [int(params["limit"]) for _, params, _ in fake.calls]
        self.assertEqual(sizes, [MAX_BARS, MAX_BARS, 100])
        for (_, before, _), (_, after, _) in zip(fake.calls, fake.calls[1:]):
            self.assertEqual(int(after["startTime"]), int(before["endTime"]) + 1)
        self.assertEqual(sleeps, [1.0, 1.0])
        self.assertEqual(list(cols.open_ms), [T0 + i * STEP for i in range(2 * MAX_BARS + 100)])

    def test_a_minute_the_venue_does_not_return_stays_a_hole_for_the_study_to_skip(self) -> None:
        cols, _, _ = self.fetch(T0, T0 + 10 * STEP, FakeVenue(holes=(T0 + 3 * STEP,)))
        self.assertEqual(len(cols.open_ms), 9)
        self.assertNotIn(T0 + 3 * STEP, list(cols.open_ms))

    def test_a_row_that_breaks_the_bar_rules_is_refused_not_guessed(self) -> None:
        broken = {"code": 0, "data": [{**kline(T0), "high": "79000.0"}]}  # a high below the open
        with self.assertRaises(venue.ExchangeError):
            self.fetch(T0, T0 + STEP, FakeVenue(script=(broken,)))

    def test_an_exchange_error_code_is_raised(self) -> None:
        with self.assertRaisesRegex(venue.ExchangeError, "80012"):
            self.fetch(T0, T0 + STEP, FakeVenue(script=({"code": 80012, "msg": "bad request"},)))


class RetryTests(_Case):
    def test_the_in_body_retry_later_code_is_retried_after_5_15_and_45_seconds(self) -> None:
        fake = FakeVenue(script=(TRANSIENT, TRANSIENT, TRANSIENT))
        cols, fake, sleeps = self.fetch(T0, T0 + 5 * STEP, fake)
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(sleeps, BACKOFF_THEN_PACING)
        self.assertEqual(len(cols.open_ms), 5)

    def test_a_retry_later_answer_that_does_not_clear_is_raised(self) -> None:
        fake = FakeVenue(script=(TRANSIENT, TRANSIENT, TRANSIENT, TRANSIENT))
        with self.assertRaises(venue.TransientExchangeError):
            self.fetch(T0, T0 + 5 * STEP, fake)
        self.assertEqual(len(fake.calls), 4)

    def test_a_server_error_is_tried_three_times_in_all(self) -> None:
        down = venue.HttpStatusError(503, "HTTP 503 from open-api.bingx.com")
        cols, fake, _ = self.fetch(T0, T0 + 5 * STEP, FakeVenue(script=(down, down)))
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(len(cols.open_ms), 5)
        fake = FakeVenue(script=(down, down, down))
        with self.assertRaises(venue.HttpStatusError):
            self.fetch(T0, T0 + 5 * STEP, fake)
        self.assertEqual(len(fake.calls), 3)

    def test_a_dropped_connection_is_tried_three_times_in_all(self) -> None:
        dropped = httpx.ConnectError("connection reset")
        fake = FakeVenue(script=(dropped, dropped, dropped))
        with self.assertRaises(httpx.ConnectError):
            self.fetch(T0, T0 + 5 * STEP, fake)
        self.assertEqual(len(fake.calls), 3)

    def test_a_client_error_is_not_retried(self) -> None:
        fake = FakeVenue(script=(venue.HttpStatusError(400, "HTTP 400 from open-api.bingx.com"),))
        with self.assertRaises(venue.HttpStatusError):
            self.fetch(T0, T0 + 5 * STEP, fake)
        self.assertEqual(len(fake.calls), 1)

    def test_a_rate_limit_stops_at_once_and_waits_for_nothing(self) -> None:
        fake = FakeVenue(script=(venue.RateLimitedError("HTTP 429 from open-api.bingx.com"),))
        sleeps: list[float] = []
        with self.assertRaises(venue.RateLimitedError):
            candles.production_cols(T0, T0 + 5 * STEP, http_get=fake, sleep=sleeps.append)
        self.assertEqual((len(fake.calls), sleeps), (1, []))


class WindowAgreesWithTheStudyTests(_Case):
    """``window_for`` states how far a walk reads; ``study.simulate`` is what decides whether the candles are enough. The two
    are written apart, so this is the test that notices when one of them changes without the other."""

    def simulate(self, stop_cut_minutes: int) -> tuple[list, list]:
        start, stop = candles.window_for([OPEN_A], 60, LONG_AFTER)
        cols, _, _ = self.fetch(start, stop - stop_cut_minutes * STEP)
        trade = position(opened_at=OPEN_A, closed_at=OPEN_A + timedelta(minutes=60))
        return study.simulate([trade], cols, RULES)

    def test_the_downloaded_window_is_enough_for_the_study_to_walk_the_trade(self) -> None:
        simulated, skipped = self.simulate(0)
        self.assertEqual((len(simulated), skipped), (1, []))

    def test_the_margin_is_one_minute_and_no_more(self) -> None:
        simulated, skipped = self.simulate(1)  # the bar of the time exit is still there
        self.assertEqual((len(simulated), skipped), (1, []))
        simulated, skipped = self.simulate(2)  # the bar of the time exit is gone: the study must skip the trade
        self.assertEqual(simulated, [])
        self.assertEqual([skip.code for skip in skipped], [study.SKIP_CANDLES])


class RefusalTests(_Case):
    def test_a_range_that_is_not_whole_minutes_is_refused_before_any_request(self) -> None:
        for start, stop in ((T0 + 1, T0 + 5 * STEP), (T0, T0 + 5 * STEP + 7), (T0 + 5 * STEP, T0 + 5 * STEP), (T0 + STEP, T0)):
            with self.subTest(start=start, stop=stop):
                fake = FakeVenue()
                with self.assertRaises(ValueError):
                    self.fetch(start, stop, fake)
                self.assertEqual(fake.calls, [])

    def test_a_range_longer_than_the_cap_is_refused_before_any_request(self) -> None:
        with mock.patch.object(candles, "MAX_WINDOW_DAYS", 2):
            self.fetch(T0, T0 + 2 * MAX_BARS * STEP)  # exactly the cap is fine
            fake = FakeVenue()
            with self.assertRaisesRegex(ValueError, "narrow --since"):
                self.fetch(T0, T0 + (2 * MAX_BARS + 1) * STEP, fake)
            self.assertEqual(fake.calls, [])


if __name__ == "__main__":
    unittest.main()
