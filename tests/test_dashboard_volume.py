"""The dashboard shows today's volume against the daily target and the volume of every day with a running total.

The figures come from the trade store, the same source the bot's own targets read, because the observer's
in-memory counters restart from zero with the process. They travel on a small `/volume` route instead of on
every block event, which also keeps them readable while the weekly entry window is closed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.storage.supabase_trade_store import SupabaseTradeStore, _normalise_iso, _parse_timestamp
from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.observer import NULL_OBSERVER, EngineTelemetry
from bot.telemetry.server import TelemetryServer
from bot.telemetry.wire import Meta, VolumeDay, VolumeSummary, to_wire

try:  # `python -m unittest tests.x` (package) and `discover -s tests` (top-level) both work
    from tests.test_trade_engine_execution import FakeClient, FakeRisk, FakeStore, FakeStrategy, _engine, _plan, _settings
    from tests.test_telemetry_server import ORIGIN, SAMPLES, get, request
except ImportError:  # pragma: no cover - depends on how unittest was launched
    from test_trade_engine_execution import FakeClient, FakeRisk, FakeStore, FakeStrategy, _engine, _plan, _settings
    from test_telemetry_server import ORIGIN, SAMPLES, get, request

META = Meta.model_validate(SAMPLES["meta"])
WIB = ZoneInfo("Asia/Jakarta")
TARGET = 1_000_000.0


def _observer() -> tuple[EngineTelemetry, TelemetryHub]:
    hub = TelemetryHub(META, TelemetryBus())
    return EngineTelemetry(hub, ("BTC/USDT",), account_balance_usdt=1500.0), hub


class VolumeWireTests(unittest.TestCase):
    def test_summary_dumps_the_camel_case_keys_the_dashboard_reads(self) -> None:
        summary = VolumeSummary(
            today="2026-10-12", today_usd=400.0, daily_target_usd=1000.0, total_usd=700.0,
            days=[VolumeDay(day="2026-10-12", volume_usd=400.0, cumulative_usd=700.0)],
        )
        self.assertEqual(
            to_wire(summary),
            {"today": "2026-10-12", "todayUsd": 400.0, "dailyTargetUsd": 1000.0, "totalUsd": 700.0,
             "days": [{"day": "2026-10-12", "volumeUsd": 400.0, "cumulativeUsd": 700.0}]},
        )

    def test_unknown_keys_are_refused_so_nothing_else_can_ride_along(self) -> None:
        with self.assertRaises(ValueError):
            VolumeDay.model_validate({"day": "2026-10-12", "volumeUsd": 1.0, "cumulativeUsd": 1.0, "wallet": "x"})


class ObserverVolumeTests(unittest.TestCase):
    def test_running_total_accumulates_day_by_day(self) -> None:
        observer, hub = _observer()
        observer.volume_updated(
            today="2026-10-12", daily=[("2026-10-09", 300_000.0), ("2026-10-12", 400_000.0)], daily_target_usdt=TARGET
        )
        self.assertEqual(
            hub.volume_body(),
            {"today": "2026-10-12", "todayUsd": 400_000.0, "dailyTargetUsd": TARGET, "totalUsd": 700_000.0,
             "days": [{"day": "2026-10-09", "volumeUsd": 300_000.0, "cumulativeUsd": 300_000.0},
                      {"day": "2026-10-12", "volumeUsd": 400_000.0, "cumulativeUsd": 700_000.0}]},
        )

    def test_today_is_listed_with_zero_volume_before_its_first_trade(self) -> None:
        observer, hub = _observer()
        observer.volume_updated(today="2026-10-12", daily=[("2026-10-09", 300_000.0)], daily_target_usdt=TARGET)
        body = hub.volume_body()
        self.assertEqual(body["todayUsd"], 0.0)
        self.assertEqual(body["days"][-1], {"day": "2026-10-12", "volumeUsd": 0.0, "cumulativeUsd": 300_000.0})

    def test_bad_input_never_raises_and_keeps_the_previous_figures(self) -> None:
        observer, hub = _observer()
        observer.volume_updated(today="2026-10-12", daily=[("2026-10-12", 5.0)], daily_target_usdt=TARGET)
        observer.volume_updated(today="2026-10-12", daily=None, daily_target_usdt=TARGET)  # type: ignore[arg-type]
        self.assertEqual(hub.volume_body()["todayUsd"], 5.0)


class HubAndServerVolumeTests(unittest.IsolatedAsyncioTestCase):
    def test_nothing_to_show_before_the_first_update(self) -> None:
        self.assertEqual(TelemetryHub(META, TelemetryBus()).volume_body(), {})

    async def test_the_volume_route_serves_the_hub_body(self) -> None:
        observer, hub = _observer()
        server = TelemetryServer(hub, host="127.0.0.1", port=0, allowed_origin=ORIGIN, request_timeout=0.5)
        await server.start()
        try:
            status, _, body = await request(server.bound_port, get("/volume"))
            self.assertEqual((status, json.loads(body)), (200, {}))
            observer.volume_updated(today="2026-10-12", daily=[("2026-10-12", 9.0)], daily_target_usdt=TARGET)
            status, _, body = await request(server.bound_port, get("/volume"))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["todayUsd"], 9.0)
        finally:
            await server.stop()


class TimestampNormalisationTests(unittest.TestCase):
    """Python 3.10 (the VPS) only parses fractions of exactly 3 or 6 digits; Supabase writes 5 or 6.

    These tests check the *string* handed to fromisoformat, because Python 3.11 is lenient and would
    hide a malformed one: the first version of this parser also swallowed the digits of the UTC offset,
    which made every row unparseable on the VPS while every test passed on a 3.11 laptop.
    """

    def test_five_digit_fraction_is_padded_and_the_offset_is_left_alone(self) -> None:
        self.assertEqual(_normalise_iso("2026-10-05T17:30:00.23315+00:00"), "2026-10-05T17:30:00.233150+00:00")

    def test_six_digit_fraction_is_unchanged(self) -> None:
        self.assertEqual(_normalise_iso("2026-09-14T04:27:17.838941+00:00"), "2026-09-14T04:27:17.838941+00:00")

    def test_longer_fractions_are_truncated_to_six_digits(self) -> None:
        self.assertEqual(_normalise_iso("2026-09-14T04:27:17.8389419+00:00"), "2026-09-14T04:27:17.838941+00:00")

    def test_no_fraction_and_z_suffix(self) -> None:
        self.assertEqual(_normalise_iso("2026-10-05T16:59:59+00:00"), "2026-10-05T16:59:59+00:00")
        self.assertEqual(_normalise_iso("2026-10-05T16:59:59Z"), "2026-10-05T16:59:59+00:00")

    def test_parse_returns_the_exact_aware_instant(self) -> None:
        parsed = _parse_timestamp("2026-10-05T17:30:00.23315+00:00")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)
        self.assertEqual((parsed.hour, parsed.minute, parsed.microsecond), (17, 30, 233150))

    def test_garbage_is_none_not_an_exception(self) -> None:
        self.assertIsNone(_parse_timestamp("not a time"))
        self.assertIsNone(_parse_timestamp(None))


class DailyVolumeSeriesTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, rows: list[dict]) -> SupabaseTradeStore:
        store = object.__new__(SupabaseTradeStore)
        store._trading_tz = WIB

        async def fake_rows(select: str) -> list[dict]:
            self.selected = select
            return rows

        store._all_closed_trade_rows = fake_rows  # type: ignore[method-assign]
        return store

    async def test_days_follow_the_trading_timezone_not_utc(self) -> None:
        rows = [
            {"closed_at": "2026-10-05T16:59:59+00:00", "entry_price": 100.0, "exit_price": 101.0, "quantity": 1.0},   # 23:59:59 WIB, 5 Oct
            {"closed_at": "2026-10-05T17:30:00.23315+00:00", "entry_price": 100.0, "exit_price": 101.0, "quantity": 2.0},  # 00:30 WIB, 6 Oct, 5-digit fraction
            {"closed_at": "2026-10-06T06:06:36.116863+00:00", "entry_price": 50.0, "exit_price": 50.0, "quantity": 1.0},
        ]
        series = await self._store(rows).daily_volume_series()
        self.assertEqual(series, [("2026-10-05", 201.0), ("2026-10-06", 402.0 + 100.0)])
        self.assertIn("closed_at", self.selected)

    async def test_no_trades_gives_an_empty_series(self) -> None:
        self.assertEqual(await self._store([]).daily_volume_series(), [])


class _Recorder:
    def __init__(self) -> None:
        self.volume_calls: list[dict] = []

    def volume_updated(self, **kwargs) -> None:
        self.volume_calls.append(kwargs)

    def __getattr__(self, name: str):  # every other hook is a no-op
        return lambda *a, **k: None


class _SeriesStore(FakeStore):
    def __init__(self, series=None, error: Exception | None = None) -> None:
        super().__init__()
        self.series, self.error, self.reads = series or [], error, 0

    async def daily_volume_series(self):
        self.reads += 1
        if self.error:
            raise self.error
        return self.series


NOW = datetime(2026, 10, 12, 19, 40, tzinfo=WIB)


class EnginePublishesVolumeTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, store: FakeStore, **settings):
        engine = _engine(FakeClient(), store, FakeStrategy(_plan()), FakeRisk(),
                         _settings(volume_daily_target_usdt=TARGET, **settings))
        engine._telemetry = _Recorder()
        engine._now_in_trading_tz = lambda: NOW
        return engine

    async def test_hands_the_observer_the_series_target_and_trading_day(self) -> None:
        store = _SeriesStore([("2026-10-12", 400_000.0)])
        engine = self._engine(store)
        await engine._publish_volume()
        self.assertEqual(
            engine._telemetry.volume_calls,
            [{"today": "2026-10-12", "daily": [("2026-10-12", 400_000.0)], "daily_target_usdt": TARGET}],
        )

    async def test_costs_nothing_when_the_dashboard_feed_is_off(self) -> None:
        store = _SeriesStore([("2026-10-12", 1.0)])
        engine = self._engine(store)
        engine._telemetry = NULL_OBSERVER
        await engine._publish_volume()
        self.assertEqual(store.reads, 0)

    async def test_a_failing_store_read_never_reaches_the_trading_loop(self) -> None:
        engine = self._engine(_SeriesStore(error=RuntimeError("supabase is down")))
        with self.assertLogs("test.engine", level="WARNING"):
            await engine._publish_volume()          # must not raise
        self.assertEqual(engine._telemetry.volume_calls, [])

    async def test_each_tick_refreshes_it_even_while_the_entry_window_is_closed(self) -> None:
        engine = self._engine(_SeriesStore([("2026-10-12", 1.0)]), entry_weekly_window="mon 07:00-sat 00:00")
        engine._now_in_trading_tz = lambda: datetime(2026, 10, 10, 12, 0, tzinfo=WIB)   # Saturday
        await engine._tick()
        self.assertEqual(len(engine._telemetry.volume_calls), 1)

    async def test_a_closed_trade_refreshes_it(self) -> None:
        store = _SeriesStore([("2026-10-12", 50_000.0)])
        engine = self._engine(store)
        await engine._execute_single_trade(_plan())
        self.assertEqual(len(store.closed), 1)
        self.assertGreaterEqual(len(engine._telemetry.volume_calls), 1)


if __name__ == "__main__":
    unittest.main()
