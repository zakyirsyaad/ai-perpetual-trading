"""The dashboard shows how many MiMo tokens each trading day spent and what that came to per closed trade.

The figures come from `ai_call_metrics` in the trade store (one row per provider request), not from process
memory, because memory restarts from zero with the bot. They travel on a small `/usage` route, like `/volume`,
so they stay readable while the weekly entry window is closed.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.ai.usage import DailyAiUsage
from bot.storage.supabase_trade_store import SupabaseTradeStore
from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.observer import NULL_OBSERVER, EngineTelemetry
from bot.telemetry.server import TelemetryServer
from bot.telemetry.wire import Meta, UsageDay, UsageSummary, to_wire

try:  # `python -m unittest tests.x` (package) and `discover -s tests` (top-level) both work
    from tests.test_trade_engine_execution import FakeClient, FakeRisk, FakeStore, FakeStrategy, _engine, _plan, _settings
    from tests.test_telemetry_server import ORIGIN, SAMPLES, get, request
except ImportError:  # pragma: no cover - depends on how unittest was launched
    from test_trade_engine_execution import FakeClient, FakeRisk, FakeStore, FakeStrategy, _engine, _plan, _settings
    from test_telemetry_server import ORIGIN, SAMPLES, get, request

META = Meta.model_validate(SAMPLES["meta"])
WIB = ZoneInfo("Asia/Jakarta")


def _usage(day: str = "2026-10-12", **kw) -> DailyAiUsage:
    base = dict(day=day, calls=0, failed=0, input_tokens=0, output_tokens=0, reasoning_tokens=0,
                total_tokens=0, entry_tokens=0, exit_tokens=0, closed_trades=0)
    base.update(kw)
    return DailyAiUsage(**base)


def _observer() -> tuple[EngineTelemetry, TelemetryHub]:
    hub = TelemetryHub(META, TelemetryBus())
    return EngineTelemetry(hub, ("BTC/USDT",), account_balance_usdt=1500.0), hub


class UsageWireTests(unittest.TestCase):
    def test_summary_dumps_the_camel_case_keys_the_dashboard_reads(self) -> None:
        day = UsageDay(day="2026-10-12", calls=4, failed=1, input_tokens=100, output_tokens=40, reasoning_tokens=25,
                       total_tokens=140, entry_tokens=100, exit_tokens=40, closed_trades=2)
        self.assertEqual(
            to_wire(UsageSummary(today="2026-10-12", days=[day])),
            {"today": "2026-10-12",
             "days": [{"day": "2026-10-12", "calls": 4, "failed": 1, "inputTokens": 100, "outputTokens": 40,
                       "reasoningTokens": 25, "totalTokens": 140, "entryTokens": 100, "exitTokens": 40,
                       "closedTrades": 2}]},
        )

    def test_unknown_keys_are_refused_so_nothing_else_can_ride_along(self) -> None:
        with self.assertRaises(ValueError):
            UsageDay.model_validate({"day": "2026-10-12", "calls": 0, "failed": 0, "inputTokens": 0, "outputTokens": 0,
                                     "reasoningTokens": 0, "totalTokens": 0, "entryTokens": 0, "exitTokens": 0,
                                     "closedTrades": 0, "apiKey": "x"})


class ObserverUsageTests(unittest.TestCase):
    def test_days_are_served_oldest_first_with_the_store_figures(self) -> None:
        observer, hub = _observer()
        observer.usage_updated(today="2026-10-13", daily=[
            _usage("2026-10-13", calls=2, total_tokens=50), _usage("2026-10-12", calls=7, total_tokens=900)])
        body = hub.usage_body()
        self.assertEqual(body["today"], "2026-10-13")
        self.assertEqual([d["day"] for d in body["days"]], ["2026-10-12", "2026-10-13"])
        self.assertEqual(body["days"][0]["totalTokens"], 900)

    def test_today_is_listed_with_zeros_before_its_first_call(self) -> None:
        observer, hub = _observer()
        observer.usage_updated(today="2026-10-13", daily=[_usage("2026-10-12", calls=7)])
        last = hub.usage_body()["days"][-1]
        self.assertEqual((last["day"], last["calls"], last["totalTokens"], last["closedTrades"]), ("2026-10-13", 0, 0, 0))

    def test_bad_input_never_raises_and_keeps_the_previous_figures(self) -> None:
        observer, hub = _observer()
        observer.usage_updated(today="2026-10-12", daily=[_usage(calls=3)])
        observer.usage_updated(today="2026-10-12", daily=None)  # type: ignore[arg-type]
        self.assertEqual(hub.usage_body()["days"][-1]["calls"], 3)


class HubAndServerUsageTests(unittest.IsolatedAsyncioTestCase):
    def test_nothing_to_show_before_the_first_update(self) -> None:
        self.assertEqual(TelemetryHub(META, TelemetryBus()).usage_body(), {})

    async def test_the_usage_route_serves_the_hub_body(self) -> None:
        observer, hub = _observer()
        server = TelemetryServer(hub, host="127.0.0.1", port=0, allowed_origin=ORIGIN, request_timeout=0.5)
        await server.start()
        try:
            status, _, body = await request(server.bound_port, get("/usage"))
            self.assertEqual((status, json.loads(body)), (200, {}))
            observer.usage_updated(today="2026-10-12", daily=[_usage(calls=9)])
            status, _, body = await request(server.bound_port, get("/usage"))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["days"][-1]["calls"], 9)
        finally:
            await server.stop()


class DailyAiUsageSeriesTests(unittest.IsolatedAsyncioTestCase):
    def _store(self, metrics: list[dict], closed: list[dict]) -> SupabaseTradeStore:
        store = object.__new__(SupabaseTradeStore)
        store._trading_tz = WIB
        store._trades_table, store._ai_call_metrics_table = "trades", "ai_call_metrics"
        self.requested: list[tuple[str, str, str]] = []

        async def fake_pages(table: str, select: str, time_column: str) -> list[dict]:
            self.requested.append((table, select, time_column))
            return metrics if table == "ai_call_metrics" else closed

        store._all_cycle_rows = fake_pages  # type: ignore[method-assign]
        return store

    async def test_days_follow_the_trading_timezone_not_utc(self) -> None:
        metrics = [
            {"created_at": "2026-10-05T16:59:59+00:00", "purpose": "entry", "success": True, "input_tokens": 100,
             "output_tokens": 30, "reasoning_tokens": 20, "total_tokens": 130},          # 23:59:59 WIB, 5 Oct
            {"created_at": "2026-10-05T17:30:00.23315+00:00", "purpose": "exit", "success": True, "input_tokens": 50,
             "output_tokens": 10, "reasoning_tokens": 0, "total_tokens": 60},            # 00:30 WIB, 6 Oct, 5-digit fraction
        ]
        series = await self._store(metrics, []).daily_ai_usage_series()
        self.assertEqual([(d.day, d.total_tokens) for d in series], [("2026-10-05", 130), ("2026-10-06", 60)])

    async def test_sums_tokens_and_splits_entry_from_exit(self) -> None:
        metrics = [
            {"created_at": "2026-10-12T03:00:00+00:00", "purpose": "entry", "success": True, "input_tokens": 100,
             "output_tokens": 30, "reasoning_tokens": 20, "total_tokens": 130},
            {"created_at": "2026-10-12T04:00:00+00:00", "purpose": "exit", "success": True, "input_tokens": 50,
             "output_tokens": 10, "reasoning_tokens": 5, "total_tokens": 60},
            {"created_at": "2026-10-12T05:00:00+00:00", "purpose": "arena_review", "success": True, "input_tokens": 10,
             "output_tokens": 5, "reasoning_tokens": 0, "total_tokens": 15},
        ]
        (day,) = await self._store(metrics, []).daily_ai_usage_series()
        self.assertEqual((day.calls, day.failed), (3, 0))
        self.assertEqual((day.input_tokens, day.output_tokens, day.reasoning_tokens), (160, 45, 25))
        self.assertEqual((day.total_tokens, day.entry_tokens, day.exit_tokens), (205, 130, 60))   # arena only in the total

    async def test_failed_calls_still_count_their_tokens_and_are_flagged(self) -> None:
        metrics = [
            {"created_at": "2026-10-12T03:00:00+00:00", "purpose": "entry", "success": False, "input_tokens": 100,
             "output_tokens": 0, "reasoning_tokens": 0, "total_tokens": 100},
            {"created_at": "2026-10-12T03:05:00+00:00", "purpose": "entry", "success": True, "input_tokens": 100,
             "output_tokens": 20, "reasoning_tokens": 0, "total_tokens": 120},
        ]
        (day,) = await self._store(metrics, []).daily_ai_usage_series()
        self.assertEqual((day.calls, day.failed, day.total_tokens), (2, 1, 220))

    async def test_a_missing_total_falls_back_to_input_plus_output(self) -> None:
        metrics = [{"created_at": "2026-10-12T03:00:00+00:00", "purpose": "entry", "success": True,
                    "input_tokens": 100, "output_tokens": 30, "reasoning_tokens": None, "total_tokens": None}]
        (day,) = await self._store(metrics, []).daily_ai_usage_series()
        self.assertEqual((day.total_tokens, day.reasoning_tokens), (130, 0))

    async def test_closed_trades_are_counted_per_trading_day(self) -> None:
        closed = [{"closed_at": "2026-10-12T03:00:00+00:00"}, {"closed_at": "2026-10-12T09:00:00.23315+00:00"},
                  {"closed_at": "2026-10-12T20:00:00+00:00"}]                       # the last is 03:00 WIB on the 13th
        metrics = [{"created_at": "2026-10-12T03:00:00+00:00", "purpose": "entry", "success": True,
                    "input_tokens": 1, "output_tokens": 1, "reasoning_tokens": 0, "total_tokens": 2}]
        series = await self._store(metrics, closed).daily_ai_usage_series()
        self.assertEqual([(d.day, d.calls, d.closed_trades) for d in series], [("2026-10-12", 1, 2), ("2026-10-13", 0, 1)])

    async def test_unreadable_rows_are_skipped_not_fatal(self) -> None:
        metrics = [{"created_at": "garbage", "purpose": "entry", "success": True, "total_tokens": 9},
                   {"created_at": "2026-10-12T03:00:00+00:00", "purpose": "entry", "success": True,
                    "input_tokens": 1, "output_tokens": 1, "reasoning_tokens": 0, "total_tokens": 2}]
        series = await self._store(metrics, [{"closed_at": None}]).daily_ai_usage_series()
        self.assertEqual([(d.day, d.total_tokens) for d in series], [("2026-10-12", 2)])

    async def test_asks_only_for_the_columns_it_sums(self) -> None:
        await self._store([], []).daily_ai_usage_series()
        self.assertCountEqual([(t, c) for t, _, c in self.requested], [("ai_call_metrics", "created_at"), ("trades", "closed_at")])
        selects = {t: s for t, s, _ in self.requested}
        self.assertEqual(set(selects["ai_call_metrics"].split(",")), {
            "created_at", "purpose", "success", "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"})

    async def test_no_calls_and_no_trades_gives_an_empty_series(self) -> None:
        self.assertEqual(await self._store([], []).daily_ai_usage_series(), [])


class PagedCycleRowsTests(unittest.IsolatedAsyncioTestCase):
    """PostgREST caps a response at 1000 rows, and a day of model calls can pass that."""

    def _store(self, pages: list[list[dict]]) -> tuple[SupabaseTradeStore, list[list[tuple[str, str]]]]:
        store = object.__new__(SupabaseTradeStore)
        store._exchange = "bingx"
        store._volume_cycle_started_at = datetime(2026, 10, 12, tzinfo=WIB)
        seen: list[list[tuple[str, str]]] = []

        async def fake_get_rows(table: str, params: list[tuple[str, str]]) -> list[dict]:
            seen.append(params)
            return pages[len(seen) - 1]

        store._get_rows = fake_get_rows  # type: ignore[method-assign]
        return store, seen

    async def test_pages_until_a_short_page_and_never_loses_a_row(self) -> None:
        full = [{"n": i} for i in range(1000)]
        store, seen = self._store([full, full, [{"n": 2000}]])
        rows = await store._all_cycle_rows("ai_call_metrics", "created_at", "created_at")
        self.assertEqual(len(rows), 2001)
        self.assertEqual([dict(p)["offset"] for p in seen], ["0", "1000", "2000"])

    async def test_scopes_to_the_exchange_and_the_cycle_start(self) -> None:
        store, seen = self._store([[]])
        await store._all_cycle_rows("ai_call_metrics", "created_at", "created_at")
        params = dict(seen[0])
        self.assertEqual(params["exchange"], "eq.bingx")
        self.assertEqual(params["created_at"], "gte.2026-10-11T17:00:00+00:00")
        self.assertEqual(params["order"], "created_at.asc")


class _Recorder:
    def __init__(self) -> None:
        self.usage_calls: list[dict] = []

    def usage_updated(self, **kwargs) -> None:
        self.usage_calls.append(kwargs)

    def __getattr__(self, name: str):  # every other hook is a no-op
        return lambda *a, **k: None


class _UsageStore(FakeStore):
    def __init__(self, series=None, error: Exception | None = None) -> None:
        super().__init__()
        self.series, self.error, self.reads = series or [], error, 0

    async def daily_volume_series(self):  # the same tick also publishes volume
        return []

    async def daily_ai_usage_series(self):
        self.reads += 1
        if self.error:
            raise self.error
        return self.series


NOW = datetime(2026, 10, 12, 19, 40, tzinfo=WIB)


class EnginePublishesUsageTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, store: FakeStore, **settings):
        engine = _engine(FakeClient(), store, FakeStrategy(_plan()), FakeRisk(), _settings(**settings))
        engine._telemetry = _Recorder()
        engine._now_in_trading_tz = lambda: NOW
        return engine

    async def test_hands_the_observer_the_series_and_the_trading_day(self) -> None:
        series = [_usage(calls=3)]
        engine = self._engine(_UsageStore(series))
        await engine._publish_usage()
        self.assertEqual(engine._telemetry.usage_calls, [{"today": "2026-10-12", "daily": series}])

    async def test_costs_nothing_when_the_dashboard_feed_is_off(self) -> None:
        store = _UsageStore([_usage()])
        engine = self._engine(store)
        engine._telemetry = NULL_OBSERVER
        await engine._publish_usage()
        self.assertEqual(store.reads, 0)

    async def test_a_failing_store_read_never_reaches_the_trading_loop(self) -> None:
        engine = self._engine(_UsageStore(error=RuntimeError("supabase is down")))
        with self.assertLogs("test.engine", level="WARNING"):
            await engine._publish_usage()          # must not raise
        self.assertEqual(engine._telemetry.usage_calls, [])

    async def test_each_tick_refreshes_it_even_while_the_entry_window_is_closed(self) -> None:
        engine = self._engine(_UsageStore([_usage()]), entry_weekly_window="mon 07:00-sat 00:00")
        engine._now_in_trading_tz = lambda: datetime(2026, 10, 10, 12, 0, tzinfo=WIB)   # Saturday
        await engine._tick()
        self.assertEqual(len(engine._telemetry.usage_calls), 1)

    async def test_a_closed_trade_refreshes_it(self) -> None:
        store = _UsageStore([_usage()])
        engine = self._engine(store)
        await engine._execute_single_trade(_plan())
        self.assertEqual(len(store.closed), 1)
        self.assertGreaterEqual(len(engine._telemetry.usage_calls), 1)


if __name__ == "__main__":
    unittest.main()
