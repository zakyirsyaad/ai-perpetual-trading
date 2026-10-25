"""Shadow evaluation: the pure walk over 1m candles and the bounded, failure-tolerant row loop.

A shadow row is a hypothetical entry the bot did not take. `walk_exit` replays the live exit rules over the
candles that followed it (stop first, floor second, time exit at the last close of the hold); `evaluate_pending`
completes at most three due rows per call and never lets one bad row stop the others.
"""
from __future__ import annotations

import asyncio
import ast
import logging
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bot.core.shadow_eval import (
    CANDLE_WINDOW,
    EVAL_BUDGET_SECONDS,
    MAX_ROWS_PER_TICK,
    STOP_SLIP_BPS,
    evaluate_pending,
    walk_exit,
)

LOG_NAME = "test.shadow"
MINUTE_MS = 60_000
DECISION_MINUTE = datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)
DECIDED_AT = DECISION_MINUTE + timedelta(seconds=30)
REF = 100.0
STOP_PCT = 0.30
FLOOR_PCT = 0.324
HOLD = 5


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _candle(open_time: datetime, *, high: float = 100.1, low: float = 99.9, close: float = 100.0) -> dict:
    return {
        "timestamp": _ms(open_time), "open": close, "high": high, "low": low, "close": close, "volume": 1.0,
    }


def _quiet(count: int, *, start_minute: int = 1, close: float = 100.0) -> list[dict]:
    """`count` candles from `start_minute` minutes after the decision minute, never near either level."""
    return [
        _candle(DECISION_MINUTE + timedelta(minutes=start_minute + index), close=close)
        for index in range(count)
    ]


def _row(row_id: int = 1, **overrides) -> dict:
    row = {
        "id": row_id, "decided_at": DECIDED_AT.isoformat(), "symbol": "BTC/USDT", "side": "long",
        "bucket_key": "long|b", "source": "map", "ref_price": REF, "stop_loss_pct": STOP_PCT,
        "floor_pct": FLOOR_PCT, "hold_minutes": HOLD, "gross_bps": None, "exit_reason": None,
        "evaluated_at": None,
    }
    row.update(overrides)
    return row


class WalkExitTests(unittest.TestCase):
    def test_long_hits_the_floor(self) -> None:
        candles = _quiet(2) + [_candle(DECISION_MINUTE + timedelta(minutes=3), high=100.4)]

        bps, reason = walk_exit(candles, "long", REF, STOP_PCT, FLOOR_PCT, HOLD)

        self.assertEqual(reason, "floor")
        self.assertAlmostEqual(bps, FLOOR_PCT * 100)

    def test_short_hits_the_floor_on_a_low(self) -> None:
        candles = _quiet(1) + [_candle(DECISION_MINUTE + timedelta(minutes=2), low=99.6)]

        bps, reason = walk_exit(candles, "short", REF, STOP_PCT, FLOOR_PCT, HOLD)

        self.assertEqual(reason, "floor")
        self.assertAlmostEqual(bps, FLOOR_PCT * 100)

    def test_short_hits_the_stop_one_bp_worse(self) -> None:
        candles = _quiet(1) + [_candle(DECISION_MINUTE + timedelta(minutes=2), high=100.5)]

        bps, reason = walk_exit(candles, "short", REF, STOP_PCT, FLOOR_PCT, HOLD)

        self.assertEqual(reason, "stop")
        self.assertAlmostEqual(bps, -STOP_PCT * 100 - STOP_SLIP_BPS)
        self.assertAlmostEqual(bps, -31.0)

    def test_long_hits_the_stop_on_a_low(self) -> None:
        candles = [_candle(DECISION_MINUTE + timedelta(minutes=1), low=99.6)]

        bps, reason = walk_exit(candles, "long", REF, STOP_PCT, FLOOR_PCT, HOLD)

        self.assertEqual((reason, round(bps, 6)), ("stop", -31.0))

    def test_a_candle_touching_both_levels_is_a_stop(self) -> None:
        candles = [_candle(DECISION_MINUTE + timedelta(minutes=1), high=100.5, low=99.5)]

        for side in ("long", "short"):
            with self.subTest(side=side):
                bps, reason = walk_exit(candles, side, REF, STOP_PCT, FLOOR_PCT, HOLD)
                self.assertEqual(reason, "stop")
                self.assertAlmostEqual(bps, -31.0)

    def test_the_first_candle_to_touch_a_level_decides(self) -> None:
        floor_first = [
            _candle(DECISION_MINUTE + timedelta(minutes=1), high=100.4),
            _candle(DECISION_MINUTE + timedelta(minutes=2), low=99.5),
        ]
        stop_first = [
            _candle(DECISION_MINUTE + timedelta(minutes=1), low=99.5),
            _candle(DECISION_MINUTE + timedelta(minutes=2), high=100.4),
        ]

        self.assertEqual(walk_exit(floor_first, "long", REF, STOP_PCT, FLOOR_PCT, HOLD)[1], "floor")
        self.assertEqual(walk_exit(stop_first, "long", REF, STOP_PCT, FLOOR_PCT, HOLD)[1], "stop")

    def test_time_exit_at_the_last_close_of_the_hold(self) -> None:
        # The hold is exactly HOLD candles: the stop-sized candle after them is never walked.
        candles = _quiet(HOLD - 1) + [_candle(DECISION_MINUTE + timedelta(minutes=HOLD), close=100.1)]
        candles.append(_candle(DECISION_MINUTE + timedelta(minutes=HOLD + 1), high=101.0, low=98.0, close=98.0))

        bps, reason = walk_exit(candles, "long", REF, STOP_PCT, FLOOR_PCT, HOLD)

        self.assertEqual(reason, "time")
        self.assertAlmostEqual(bps, 10.0)  # (100.1 / 100 - 1) in bps, no slippage

    def test_a_time_exit_is_signed_by_side(self) -> None:
        candles = _quiet(HOLD, close=99.9)

        long_bps, _ = walk_exit(candles, "long", REF, STOP_PCT, FLOOR_PCT, HOLD)
        short_bps, _ = walk_exit(candles, "short", REF, STOP_PCT, FLOOR_PCT, HOLD)

        self.assertAlmostEqual(long_bps, -10.0)
        self.assertAlmostEqual(short_bps, 10.0)

    def test_a_stop_or_floor_inside_the_available_candles_needs_no_full_hold(self) -> None:
        candles = [_candle(DECISION_MINUTE + timedelta(minutes=1), high=100.4)]

        self.assertEqual(walk_exit(candles, "long", REF, STOP_PCT, FLOOR_PCT, 60)[1], "floor")

    def test_an_incomplete_hold_without_a_touch_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            walk_exit(_quiet(HOLD - 1), "long", REF, STOP_PCT, FLOOR_PCT, HOLD)

    def test_bad_input_is_refused(self) -> None:
        candles = _quiet(HOLD)
        for side, ref, hold in (("flat", REF, HOLD), ("long", 0.0, HOLD), ("long", REF, 0)):
            with self.subTest(side=side, ref=ref, hold=hold), self.assertRaises(ValueError):
                walk_exit(candles, side, ref, STOP_PCT, FLOOR_PCT, hold)
        with self.assertRaises(ValueError):
            walk_exit([], "long", REF, STOP_PCT, FLOOR_PCT, HOLD)

    def test_the_constants_are_the_spec_numbers(self) -> None:
        self.assertEqual((STOP_SLIP_BPS, MAX_ROWS_PER_TICK, CANDLE_WINDOW), (1.0, 3, 1000))


class FakeShadowStore:
    """Serves unevaluated rows like the real store: older than the cutoff, oldest first, capped at `limit`."""

    def __init__(self, rows: list[dict], *, ignore_limit: bool = False, fail_on_complete=()) -> None:
        self.rows = list(rows)
        self.ignore_limit = ignore_limit
        self.fail_on_complete = set(fail_on_complete)
        self.requests: list[tuple[datetime, int]] = []
        self.completed: list[tuple[int, float | None, str]] = []

    async def pending_shadow_entries(self, decided_before: datetime, limit: int) -> list[dict]:
        self.requests.append((decided_before, limit))
        done = {entry_id for entry_id, _, _ in self.completed}
        due = [
            row for row in self.rows
            if row["id"] not in done and datetime.fromisoformat(row["decided_at"]) < decided_before
        ]
        return due if self.ignore_limit else due[:limit]

    async def complete_shadow_entry(self, entry_id: int, gross_bps: float | None, exit_reason: str) -> None:
        if entry_id in self.fail_on_complete:
            raise RuntimeError("Supabase request failed for table shadow_entries: 503")
        self.completed.append((entry_id, gross_bps, exit_reason))


class AnyRowStore(FakeShadowStore):
    """Serves every unfinished row whatever its decided_at, so malformed timestamps reach the evaluator."""

    async def pending_shadow_entries(self, decided_before: datetime, limit: int) -> list[dict]:
        self.requests.append((decided_before, limit))
        done = {entry_id for entry_id, _, _ in self.completed}
        return [row for row in self.rows if row.get("id") not in done][:limit]


class FakeKlineClient:
    """Serves the newest `limit` closed 1m candles of one scripted series, like the venue."""

    def __init__(self, candles: list[dict], *, error: Exception | None = None) -> None:
        self.candles = list(candles)
        self.error = error
        self.calls: list[tuple[str, str, int]] = []

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 120) -> list[dict]:
        self.calls.append((symbol, interval, limit))
        if self.error is not None:
            raise self.error
        return self.candles[-limit:]


def _logger() -> logging.Logger:
    return logging.getLogger(LOG_NAME)


class EvaluatePendingTests(unittest.IsolatedAsyncioTestCase):
    NOW = DECISION_MINUTE + timedelta(minutes=HOLD + 3, seconds=10)

    def _series(self, count: int, *, close: float = 100.0) -> list[dict]:
        """The decision-minute candle (close REF: the walk's reference), then `count` candles closing at `close`."""
        return [_candle(DECISION_MINUTE, close=REF)] + _quiet(count, close=close)

    async def test_a_due_row_is_completed_with_the_walk_result(self) -> None:
        store = FakeShadowStore([_row(7)])
        client = FakeKlineClient(self._series(HOLD + 2, close=100.1))

        done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(done, 1)
        self.assertEqual(len(store.completed), 1)
        entry_id, bps, reason = store.completed[0]
        self.assertEqual((entry_id, reason), (7, "time"))
        self.assertAlmostEqual(bps, 10.0)
        self.assertEqual(client.calls[0][:2], ("BTC/USDT", "1m"))

    async def test_evaluates_at_most_three_rows_per_call(self) -> None:
        store = FakeShadowStore([_row(index) for index in range(1, 6)])
        client = FakeKlineClient(self._series(HOLD + 2))

        done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(done, 3)
        self.assertEqual([entry_id for entry_id, _, _ in store.completed], [1, 2, 3])
        self.assertEqual([limit for _, limit in store.requests], [MAX_ROWS_PER_TICK])

    async def test_a_store_that_ignores_the_limit_is_still_capped_at_three(self) -> None:
        store = FakeShadowStore([_row(index) for index in range(1, 6)], ignore_limit=True)
        client = FakeKlineClient(self._series(HOLD + 2))

        done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(done, 3)
        self.assertEqual(len(store.completed), 3)

    async def test_rows_younger_than_hold_plus_one_minute_are_not_read(self) -> None:
        now = DECISION_MINUTE + timedelta(hours=2)
        store = FakeShadowStore([_row(1), _row(2, decided_at=(now - timedelta(minutes=60)).isoformat())])
        client = FakeKlineClient(_quiet(125, start_minute=-5))  # closed candles up to 05:59

        await evaluate_pending(store, client, now=now, logger=_logger(), max_hold_minutes=60)

        self.assertEqual(store.requests, [(now - timedelta(minutes=61), MAX_ROWS_PER_TICK)])
        self.assertEqual([entry_id for entry_id, _, _ in store.completed], [1])  # row 2 is 60 min old: not due

    async def test_the_cutoff_uses_the_hold_setting_it_is_given(self) -> None:
        store = FakeShadowStore([])
        client = FakeKlineClient([])

        await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=15)

        self.assertEqual(store.requests[0][0], self.NOW - timedelta(minutes=16))

    async def test_candles_start_after_the_decision_minute(self) -> None:
        # The decision minute's own candle (a stop-sized low) only prices the walk with its close; it is not
        # walked, nor is the one before it.
        before = _candle(DECISION_MINUTE - timedelta(minutes=1), low=90.0)
        own = _candle(DECISION_MINUTE, low=90.0)
        store = FakeShadowStore([_row(1)])
        client = FakeKlineClient([before, own] + _quiet(HOLD + 1, close=100.0))

        await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(store.completed, [(1, 0.0, "time")])

    async def test_the_candle_request_is_sized_to_the_row_age_and_capped(self) -> None:
        store = FakeShadowStore([_row(1)])
        client = FakeKlineClient(self._series(HOLD + 2))
        now = DECISION_MINUTE + timedelta(minutes=12, seconds=5)

        await evaluate_pending(store, client, now=now, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(client.calls[0][2], 14)  # minutes since the decision minute + 2

        far = FakeShadowStore([_row(2)])
        far_client = FakeKlineClient(_quiet(CANDLE_WINDOW))
        await evaluate_pending(
            far, far_client, now=DECISION_MINUTE + timedelta(days=2), logger=_logger(), max_hold_minutes=HOLD
        )
        self.assertEqual(far_client.calls[0][2], CANDLE_WINDOW)

    async def test_a_venue_failure_leaves_the_row_pending_and_warns(self) -> None:
        store = FakeShadowStore([_row(1)])
        client = FakeKlineClient([], error=RuntimeError("klines 503"))

        with self.assertLogs(LOG_NAME, level="WARNING") as captured:
            done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(done, 0)
        self.assertEqual(store.completed, [])
        self.assertTrue(any("1" in line and "RuntimeError" in line for line in captured.output))

    async def test_a_failed_write_leaves_the_row_pending_and_warns(self) -> None:
        store = FakeShadowStore([_row(1), _row(2)], fail_on_complete={1})
        client = FakeKlineClient(self._series(HOLD + 2))

        with self.assertLogs(LOG_NAME, level="WARNING") as captured:
            done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(done, 1)
        self.assertEqual([entry_id for entry_id, _, _ in store.completed], [2])
        self.assertTrue(any("RuntimeError" in line for line in captured.output))

    async def test_an_empty_candle_answer_leaves_the_row_pending_and_warns(self) -> None:
        store = FakeShadowStore([_row(1)])
        client = FakeKlineClient([])

        with self.assertLogs(LOG_NAME, level="WARNING"):
            done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual((done, store.completed), (0, []))

    async def test_a_row_older_than_the_candle_window_expires(self) -> None:
        # The venue serves at most CANDLE_WINDOW candles, so a row older than that cannot be walked.
        now = DECISION_MINUTE + timedelta(days=3)
        newest = [_candle(now - timedelta(minutes=CANDLE_WINDOW - index)) for index in range(CANDLE_WINDOW)]
        store = FakeShadowStore([_row(9)])
        client = FakeKlineClient(newest)

        done = await evaluate_pending(store, client, now=now, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(done, 1)
        self.assertEqual(store.completed, [(9, None, "expired")])

    async def test_a_window_starting_exactly_at_the_decision_minute_does_not_expire(self) -> None:
        store = FakeShadowStore([_row(1)])
        client = FakeKlineClient(self._series(HOLD + 2))  # oldest candle opens at the decision minute

        await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(store.completed[0][2], "time")

    async def test_a_short_answer_past_the_decision_minute_stays_pending(self) -> None:
        # Every candle of the hold is there, but the decision-minute candle is not, and the venue served fewer
        # candles than were asked for: it did not honour the limit, so the row stays pending (not expired) and
        # the loop stops, as for any venue failure.
        last_reachable = CANDLE_WINDOW - 1   # minutes since the decision minute: a full window still holds it
        for now, candles in (
            (self.NOW, _quiet(HOLD + 2)),   # limit 10, 7 served
            (   # limit 1000 (capped), 500 served
                DECISION_MINUTE + timedelta(minutes=last_reachable),
                _quiet(500, start_minute=last_reachable - 500),
            ),
        ):
            with self.subTest(served=len(candles)):
                store = FakeShadowStore([_row(1), _row(2)])
                client = FakeKlineClient(candles)

                with self.assertLogs(LOG_NAME, level="WARNING") as captured:
                    done = await evaluate_pending(store, client, now=now, logger=_logger(), max_hold_minutes=HOLD)

                self.assertEqual((done, store.completed), (0, []))
                self.assertEqual(len(client.calls), 1)   # the loop stopped at the first row
                self.assertIn("2 row(s) stay pending", "\n".join(captured.output))

    async def test_a_full_window_past_the_decision_minute_expires(self) -> None:
        # The venue served the whole window it was asked for (the forming candle may be dropped, so limit - 1
        # counts as full) and it still starts after the decision minute: that candle is gone for good.
        now = DECISION_MINUTE + timedelta(days=3)
        for served in (CANDLE_WINDOW, CANDLE_WINDOW - 1):
            with self.subTest(served=served):
                newest = [_candle(now - timedelta(minutes=served - index)) for index in range(served)]
                store = FakeShadowStore([_row(9)])
                client = FakeKlineClient(newest)

                done = await evaluate_pending(store, client, now=now, logger=_logger(), max_hold_minutes=HOLD)

                self.assertEqual(client.calls[0][2], CANDLE_WINDOW)
                self.assertEqual((done, store.completed), (1, [(9, None, "expired")]))

    async def test_a_row_past_the_candle_window_expires_whatever_the_answer_length(self) -> None:
        # CANDLE_WINDOW or more minutes after the decision minute, no window the evaluator can ask for holds
        # that candle, so a short answer cannot leave the row pending and stall every newer row behind it.
        for minutes in (CANDLE_WINDOW, 3 * 1440):
            with self.subTest(minutes=minutes):
                now = DECISION_MINUTE + timedelta(minutes=minutes)
                store = FakeShadowStore([_row(9), _row(10)])
                client = FakeKlineClient(_quiet(500, start_minute=minutes - 500))

                done = await evaluate_pending(store, client, now=now, logger=_logger(), max_hold_minutes=HOLD)

                self.assertEqual((done, store.completed), (2, [(9, None, "expired"), (10, None, "expired")]))

    async def test_a_hole_at_the_decision_minute_expires(self) -> None:
        before = _candle(DECISION_MINUTE - timedelta(minutes=1))
        store = FakeShadowStore([_row(1)])
        client = FakeKlineClient([before] + _quiet(HOLD + 2))

        done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual((done, store.completed), (1, [(1, None, "expired")]))

    async def test_the_walk_starts_from_the_decision_minute_close(self) -> None:
        # The stored ref_price is the price the entry path saw before the AI call; the walk ignores it. At 90 it
        # would put the long's floor below every candle and book +32.4 bps at once.
        outcomes = []
        for stored in (90.0, REF):
            store = FakeShadowStore([_row(1, ref_price=stored)])
            client = FakeKlineClient(self._series(HOLD + 2, close=100.1))
            await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)
            outcomes.append(store.completed[0])
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][2], "time")
        self.assertAlmostEqual(outcomes[0][1], 10.0)

        # A decision-minute close of 100.05 is the reference: (100.1 - 100.05) / 100.05 = +4.9975 bps.
        store = FakeShadowStore([_row(2)])
        client = FakeKlineClient([_candle(DECISION_MINUTE, close=100.05)] + _quiet(HOLD + 2, close=100.1))
        await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)
        entry_id, bps, reason = store.completed[0]
        self.assertEqual((entry_id, reason), (2, "time"))
        self.assertAlmostEqual(bps, (100.1 - 100.05) / 100.05 * 10_000)

    async def test_an_unusable_decision_minute_close_is_a_venue_failure(self) -> None:
        for close in (0.0, float("nan"), "garbage", None):
            with self.subTest(close=close):
                reference = dict(_candle(DECISION_MINUTE), close=close)
                store = FakeShadowStore([_row(1)])
                client = FakeKlineClient([reference] + _quiet(HOLD + 2))

                with self.assertLogs(LOG_NAME, level="WARNING") as captured:
                    done = await evaluate_pending(
                        store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD
                    )

                self.assertEqual((done, store.completed), (0, []))  # pending, not closed as invalid
                self.assertTrue(all(record.levelno == logging.WARNING for record in captured.records))

    async def test_the_request_always_reaches_the_decision_minute_candle(self) -> None:
        # `minutes since the decision minute + 2` reaches one minute before the decision minute, whether the venue
        # serves the forming candle or lags a bar, up to the venue's CANDLE_WINDOW.
        for minutes in (HOLD + 2, 60, CANDLE_WINDOW - 1):
            for seconds in (0, 59):
                for forming in (True, False):
                    with self.subTest(minutes=minutes, seconds=seconds, forming=forming):
                        now = DECISION_MINUTE + timedelta(minutes=minutes, seconds=seconds)
                        newest = minutes if forming else minutes - 1
                        store = FakeShadowStore([_row(1)])
                        client = FakeKlineClient(_quiet(newest + 6, start_minute=-5))

                        await evaluate_pending(store, client, now=now, logger=_logger(), max_hold_minutes=HOLD)

                        self.assertEqual(store.completed, [(1, 0.0, "time")])

    async def test_too_few_candles_yet_leaves_the_row_pending(self) -> None:
        store = FakeShadowStore([_row(1, hold_minutes=30)])
        client = FakeKlineClient(self._series(HOLD + 2))  # only 7 of the 30 candles of this row's hold

        done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual((done, store.completed), (0, []))

    async def test_a_stop_before_the_end_of_the_hold_completes_the_row_as_a_stop(self) -> None:
        series = self._series(2) + [_candle(DECISION_MINUTE + timedelta(minutes=3), low=99.0)] + _quiet(
            4, start_minute=4
        )
        store = FakeShadowStore([_row(1)])
        client = FakeKlineClient(series)

        await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        entry_id, bps, reason = store.completed[0]
        self.assertEqual((entry_id, reason), (1, "stop"))
        self.assertAlmostEqual(bps, -31.0)

    async def test_timestamps_with_odd_fractions_and_zulu_parse_on_python_3_10(self) -> None:
        for text in (
            "2026-10-21T04:00:30.12+00:00", "2026-10-21T04:00:30.123456+00:00", "2026-10-21T04:00:30Z",
            "2026-10-21T04:00:30+00", "2026-10-21 04:00:30.5+00:00", "2026-10-21T11:00:30+07:00",
        ):
            with self.subTest(text=text):
                store = AnyRowStore([_row(1, decided_at=text)])
                client = FakeKlineClient(self._series(HOLD + 2))

                done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

                self.assertEqual((done, store.completed[0][2]), (1, "time"))

    async def test_a_naive_timestamp_is_not_guessed_and_the_row_is_closed_as_invalid(self) -> None:
        store = AnyRowStore([_row(1, decided_at="2026-10-21T04:00:30")])

        with self.assertLogs(LOG_NAME, level="ERROR"):
            done = await evaluate_pending(
                store, FakeKlineClient(self._series(HOLD + 2)), now=self.NOW, logger=_logger(),
                max_hold_minutes=HOLD,
            )

        self.assertEqual((done, store.completed), (1, [(1, None, "invalid")]))

    async def test_a_datetime_decided_at_is_accepted_and_a_naive_one_or_garbage_is_not(self) -> None:
        aware = AnyRowStore([_row(1, decided_at=DECIDED_AT)])
        done = await evaluate_pending(
            aware, FakeKlineClient(self._series(HOLD + 2)), now=self.NOW, logger=_logger(), max_hold_minutes=HOLD
        )
        self.assertEqual(done, 1)

        for bad in (DECIDED_AT.replace(tzinfo=None), "yesterday"):
            with self.subTest(bad=bad):
                store = AnyRowStore([_row(2, decided_at=bad)])
                with self.assertLogs(LOG_NAME, level="ERROR"):
                    done = await evaluate_pending(
                        store, FakeKlineClient(self._series(HOLD + 2)), now=self.NOW, logger=_logger(),
                        max_hold_minutes=HOLD,
                    )
                self.assertEqual((done, store.completed), (1, [(2, None, "invalid")]))

    async def test_a_venue_failure_stops_the_row_loop(self) -> None:
        store = FakeShadowStore([_row(1), _row(2), _row(3)])
        client = FakeKlineClient([], error=RuntimeError("klines 503"))

        with self.assertLogs(LOG_NAME, level="WARNING") as captured:
            done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(done, 0)
        self.assertEqual(len(client.calls), 1)  # rows 2 and 3 do not repeat the call that just failed
        self.assertEqual(store.completed, [])
        self.assertEqual(len(captured.records), 1)  # one WARNING for the whole stop
        self.assertIn("RuntimeError", captured.output[0])

    async def test_rows_completed_before_a_venue_failure_are_kept(self) -> None:
        class FailsSecondCall(FakeKlineClient):
            async def get_klines(self, symbol, interval="1m", limit=120):
                self.calls.append((symbol, interval, limit))
                if len(self.calls) == 2:
                    raise RuntimeError("klines 503")
                return self.candles[-limit:]

        store = FakeShadowStore([_row(1), _row(2), _row(3)])
        client = FailsSecondCall(self._series(HOLD + 2))

        with self.assertLogs(LOG_NAME, level="WARNING"):
            done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(done, 1)
        self.assertEqual([entry_id for entry_id, _, _ in store.completed], [1])
        self.assertEqual(len(client.calls), 2)

    async def test_an_empty_candle_answer_stops_the_row_loop(self) -> None:
        store = FakeShadowStore([_row(1), _row(2)])
        client = FakeKlineClient([])

        with self.assertLogs(LOG_NAME, level="WARNING"):
            await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(len(client.calls), 1)

    async def test_a_malformed_candle_is_a_venue_failure_and_not_an_invalid_row(self) -> None:
        broken = [{"timestamp": candle["timestamp"], "close": candle["close"]} for candle in self._series(HOLD + 2)]
        store = FakeShadowStore([_row(1), _row(2)])
        client = FakeKlineClient(broken)

        with self.assertLogs(LOG_NAME, level="WARNING"):
            done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual((done, store.completed), (0, []))  # not closed as invalid: the rows are fine
        self.assertEqual(len(client.calls), 1)

    async def test_a_malformed_row_is_closed_as_invalid_and_does_not_block_newer_rows(self) -> None:
        missing_price = _row(2)
        del missing_price["ref_price"]
        bad_rows = [
            _row(1, decided_at="yesterday"),
            missing_price,
            _row(3, stop_loss_pct=0.0),
        ]
        good = _row(4)
        store = AnyRowStore(bad_rows + [good])
        client = FakeKlineClient(self._series(HOLD + 2, close=100.1))

        with self.assertLogs(LOG_NAME, level="ERROR") as captured:
            first = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        # All three slots went to the bad rows, which are closed instead of staying pending ...
        self.assertEqual(first, 3)
        self.assertEqual(store.completed, [(1, None, "invalid"), (2, None, "invalid"), (3, None, "invalid")])
        self.assertEqual(client.calls, [])  # a malformed row costs no venue call
        self.assertEqual(len(captured.records), 3)
        for entry_id, line in zip((1, 2, 3), captured.output):
            self.assertIn(f"entry {entry_id} ", line)

        # ... so the next call reaches the newer row.
        second = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertEqual(second, 1)
        self.assertEqual(store.completed[-1][0::2], (4, "time"))

    async def test_an_invalid_sign_or_side_is_closed_as_invalid(self) -> None:
        for overrides in ({"side": "sideways"}, {"floor_pct": -0.1}, {"hold_minutes": 0}, {"ref_price": 0.0}):
            with self.subTest(overrides=overrides):
                store = FakeShadowStore([_row(1, **overrides)])
                with self.assertLogs(LOG_NAME, level="ERROR"):
                    done = await evaluate_pending(
                        store, FakeKlineClient(self._series(HOLD + 2)), now=self.NOW, logger=_logger(),
                        max_hold_minutes=HOLD,
                    )
                self.assertEqual((done, store.completed), (1, [(1, None, "invalid")]))

    async def test_a_write_failure_is_not_a_reason_to_close_the_row_as_invalid(self) -> None:
        class RejectingStore(FakeShadowStore):
            async def complete_shadow_entry(self, entry_id, gross_bps, exit_reason):
                raise ValueError("serialisation")  # even a ValueError from the write is transient

        store = RejectingStore([_row(1)])

        with self.assertLogs(LOG_NAME, level="WARNING") as captured:
            done = await evaluate_pending(
                store, FakeKlineClient(self._series(HOLD + 2)), now=self.NOW, logger=_logger(),
                max_hold_minutes=HOLD,
            )

        self.assertEqual((done, store.completed), (0, []))
        self.assertTrue(all(record.levelno == logging.WARNING for record in captured.records))

    async def test_an_infinite_hold_is_closed_as_invalid(self) -> None:
        store = FakeShadowStore([_row(1, hold_minutes=float("inf"))])

        with self.assertLogs(LOG_NAME, level="ERROR"):
            done = await evaluate_pending(
                store, FakeKlineClient(self._series(HOLD + 2)), now=self.NOW, logger=_logger(),
                max_hold_minutes=HOLD,
            )

        self.assertEqual((done, store.completed), (1, [(1, None, "invalid")]))

    async def test_an_unexpected_error_leaves_the_row_pending_and_the_loop_goes_on(self) -> None:
        calls = []

        async def flaky(client, row, now):
            calls.append(row["id"])
            if row["id"] == 1:
                raise RuntimeError("boom")
            return 5.0, "time"

        store = FakeShadowStore([_row(1), _row(2)])

        with mock.patch("bot.core.shadow_eval._outcome", new=flaky):
            with self.assertLogs(LOG_NAME, level="WARNING") as captured:
                done = await evaluate_pending(
                    store, FakeKlineClient([]), now=self.NOW, logger=_logger(), max_hold_minutes=HOLD
                )

        self.assertEqual(calls, [1, 2])
        self.assertEqual((done, store.completed), (1, [(2, 5.0, "time")]))
        self.assertIn("RuntimeError", captured.output[0])

    async def test_a_naive_now_is_refused_and_is_not_blamed_on_the_rows(self) -> None:
        store = FakeShadowStore([_row(1)])

        with self.assertRaises(ValueError):
            await evaluate_pending(
                store, FakeKlineClient([]), now=self.NOW.replace(tzinfo=None), logger=_logger(),
                max_hold_minutes=HOLD,
            )

        self.assertEqual(store.completed, [])

    async def test_a_row_without_an_id_is_skipped_with_an_error_and_the_rest_go_on(self) -> None:
        nameless = _row(1)
        del nameless["id"]
        store = AnyRowStore([nameless, _row(2)])

        with self.assertLogs(LOG_NAME, level="ERROR"):
            done = await evaluate_pending(
                store, FakeKlineClient(self._series(HOLD + 2)), now=self.NOW, logger=_logger(),
                max_hold_minutes=HOLD,
            )

        self.assertEqual([entry_id for entry_id, _, _ in store.completed], [2])
        self.assertEqual(done, 1)

    async def test_the_budget_stops_a_hanging_venue_call_and_keeps_finished_rows(self) -> None:
        class HangsOnSecondCall(FakeKlineClient):
            cancelled = False

            async def get_klines(self, symbol, interval="1m", limit=120):
                self.calls.append((symbol, interval, limit))
                if len(self.calls) == 2:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        type(self).cancelled = True
                        raise
                return self.candles[-limit:]

        store = FakeShadowStore([_row(1), _row(2), _row(3)])
        client = HangsOnSecondCall(self._series(HOLD + 2))
        started = time.monotonic()

        with mock.patch("bot.core.shadow_eval.EVAL_BUDGET_SECONDS", 0.05):
            with self.assertLogs(LOG_NAME, level="WARNING") as captured:
                done = await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(done, 1)
        self.assertEqual([entry_id for entry_id, _, _ in store.completed], [1])
        self.assertTrue(HangsOnSecondCall.cancelled)  # the stuck call is cancelled, not abandoned
        self.assertEqual(len(captured.records), 1)
        self.assertIn("budget", captured.output[0])

    async def test_the_budget_is_twenty_seconds(self) -> None:
        self.assertEqual(EVAL_BUDGET_SECONDS, 20.0)

    async def test_a_failing_pending_read_propagates_to_the_caller(self) -> None:
        class BrokenStore(FakeShadowStore):
            async def pending_shadow_entries(self, decided_before, limit):
                raise RuntimeError("Supabase request failed for table shadow_entries: 500")

        with self.assertRaises(RuntimeError):
            await evaluate_pending(
                BrokenStore([]), FakeKlineClient([]), now=self.NOW, logger=_logger(), max_hold_minutes=HOLD
            )

    async def test_nothing_due_makes_no_venue_call(self) -> None:
        client = FakeKlineClient(self._series(3))

        done = await evaluate_pending(
            FakeShadowStore([]), client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD
        )

        self.assertEqual((done, client.calls), (0, []))

    async def test_a_short_row_is_walked_as_a_short(self) -> None:
        store = FakeShadowStore([_row(1, side="short")])
        client = FakeKlineClient(self._series(HOLD + 2, close=99.9))

        await evaluate_pending(store, client, now=self.NOW, logger=_logger(), max_hold_minutes=HOLD)

        entry_id, bps, reason = store.completed[0]
        self.assertEqual((entry_id, reason), (1, "time"))
        self.assertAlmostEqual(bps, 10.0)


class ModuleBoundaryTests(unittest.TestCase):
    """The core reads timestamps through one public parser each and never imports the telemetry package."""

    CORE = ("src/bot/core/shadow_eval.py", "src/bot/core/setup_ledger.py")

    @staticmethod
    def _imports(path: str) -> set[str]:
        tree = ast.parse((Path(__file__).resolve().parents[1] / path).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        return names

    def test_the_core_does_not_import_telemetry(self) -> None:
        for path in self.CORE:
            with self.subTest(path=path):
                self.assertEqual([name for name in self._imports(path) if name.startswith("bot.telemetry")], [])
        self.assertIn("bot.asyncutil", self._imports("src/bot/core/shadow_eval.py"))

    def test_with_timeout_has_one_definition_re_exported_by_telemetry(self) -> None:
        from bot import asyncutil
        from bot.telemetry import ws

        self.assertIs(ws.with_timeout, asyncutil.with_timeout)

    def test_the_timestamp_parsers_are_public_with_their_old_names_as_aliases(self) -> None:
        from bot.core import setup_ledger, shadow_eval
        from bot.storage import supabase_trade_store

        self.assertIs(shadow_eval._parse_utc, shadow_eval.parse_utc)
        self.assertIs(supabase_trade_store._parse_timestamp, supabase_trade_store.parse_timestamp)
        self.assertIn("parse_utc", vars(setup_ledger))
        self.assertNotIn("_parse_utc", vars(setup_ledger))
        report_imports = self._imports("src/report_live_stages.py")
        self.assertIn("bot.storage.supabase_trade_store", report_imports)
        report_source = (Path(__file__).resolve().parents[1] / "src/report_live_stages.py").read_text(encoding="utf-8")
        self.assertNotIn("_parse_timestamp", report_source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
