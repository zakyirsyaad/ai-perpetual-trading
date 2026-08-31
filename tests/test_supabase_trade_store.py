from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx

from bot.ai.usage import AICallMetrics, TokenUsage
from bot.models import ClosedTrade, OpenPosition, PositionDirection
from bot.storage.supabase_trade_store import SupabaseTradeStore


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-key",
        supabase_schema="public",
        supabase_trades_table="trades",
        supabase_ai_decisions_table="ai_decisions",
        supabase_ai_call_metrics_table="ai_call_metrics",
        supabase_open_positions_table="open_position_state",
        exchange_name="bingx",
        volume_cycle_started_at=None,
        request_timeout_seconds=5,
        trading_timezone="Asia/Jakarta",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeHTTP:
    def __init__(self, rows=None, status: int = 200) -> None:
        self.rows = rows if rows is not None else []
        self.status = status
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self.deletes: list[dict] = []

    def _response(self, method: str, url: str, payload):
        request = httpx.Request(method, url)
        return httpx.Response(self.status, request=request, json=payload)

    async def post(self, url, json=None, headers=None, params=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "params": params})
        return self._response("POST", url, {})

    async def get(self, url, params=None, headers=None):
        self.gets.append({"url": url, "params": params})
        return self._response("GET", url, self.rows)

    async def request(self, method, url, params=None, headers=None):
        self.deletes.append({"method": method, "url": url, "params": params})
        return self._response(method, url, {})

    async def aclose(self) -> None:
        return None


def _store(http: _FakeHTTP, settings=None) -> SupabaseTradeStore:
    store = SupabaseTradeStore(settings or _settings(), __import__("logging").getLogger("test.store"))
    store._client = http
    return store


def _closed_trade() -> ClosedTrade:
    now = datetime.now(timezone.utc)
    return ClosedTrade(
        symbol="BTC/USDT",
        direction=PositionDirection.LONG,
        entry_price=60000.0,
        exit_price=60100.0,
        quantity=0.01,
        pnl_usdt=0.9,
        reason="take_profit",
        entry_reason="ai_volume:OPEN_LONG",
        opened_at=now,
        closed_at=now,
        exchange_entry_order_id="e1",
        exchange_exit_order_id="x1",
        confidence=0.8,
    )


def _open_position() -> OpenPosition:
    return OpenPosition(
        symbol="BTC/USDT",
        direction=PositionDirection.LONG,
        entry_price=60000.0,
        quantity=0.01,
        exchange_size=0.01,
        exchange_order_id="e1",
        opened_at=datetime.now(timezone.utc),
        entry_reason="ai_volume:OPEN_LONG",
        position_id="p-1",
        stop_loss_pct=0.5,
        take_profit_pct=1.2,
    )


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_enabled_requires_url_and_key(self) -> None:
        self.assertTrue(_store(_FakeHTTP()).enabled)
        self.assertFalse(_store(_FakeHTTP(), _settings(supabase_url="")).enabled)

    async def test_initialize_fails_without_credentials(self) -> None:
        with self.assertRaises(RuntimeError):
            await _store(_FakeHTTP(), _settings(supabase_service_role_key=None)).initialize()

    async def test_initialize_probes_the_trades_table(self) -> None:
        http = _FakeHTTP()
        await _store(http).initialize()
        self.assertIn("/rest/v1/trades", http.gets[0]["url"])

    async def test_closed_trade_is_stamped_with_the_exchange(self) -> None:
        http = _FakeHTTP()
        await _store(http).save_closed_trade(_closed_trade())
        self.assertEqual(http.posts[0]["json"]["exchange"], "bingx")
        self.assertEqual(http.posts[0]["json"]["direction"], "LONG")

    async def test_ai_decision_records_exchange_and_call_id(self) -> None:
        http = _FakeHTTP()
        await _store(http).save_ai_decision(
            symbol="BTC/USDT",
            action="OPEN_LONG",
            confidence=0.8,
            systemic_risk=2,
            reason="clean setup",
            payload={"a": 1},
            ai_call_id="call-123",
        )
        body = http.posts[0]["json"]
        self.assertEqual(body["exchange"], "bingx")
        self.assertEqual(body["ai_call_id"], "call-123")

    async def test_ai_call_metrics_are_persisted(self) -> None:
        http = _FakeHTTP()
        metrics = AICallMetrics(
            purpose="entry",
            provider="p",
            model="m",
            api_style="messages",
            prompt_version="compact-v1",
            reasoning_effort="high",
            request_hash="h",
            request_bytes=10,
            response_bytes=5,
            latency_ms=1.0,
            attempts=1,
            success=True,
            usage=TokenUsage(100, 10, 110, False),
        )
        await _store(http).save_ai_call_metrics(metrics)
        body = http.posts[0]["json"]
        self.assertEqual(body["purpose"], "entry")
        self.assertEqual(body["total_tokens"], 110)
        self.assertEqual(body["exchange"], "bingx")

    async def test_metrics_storage_failure_is_swallowed(self) -> None:
        http = _FakeHTTP(status=500)
        await _store(http).save_ai_call_metrics(
            AICallMetrics(
                purpose="entry",
                provider="p",
                model="m",
                api_style="chat",
                prompt_version="v",
                reasoning_effort="low",
                request_hash="h",
                request_bytes=1,
                response_bytes=1,
                latency_ms=1.0,
                attempts=1,
                success=True,
            )
        )  # must not raise: telemetry never blocks trading

    async def test_open_position_is_upserted(self) -> None:
        http = _FakeHTTP()
        await _store(http).save_open_position(_open_position())
        post = http.posts[0]
        self.assertIn("open_position_state", post["url"])
        self.assertEqual(post["params"], [("on_conflict", "exchange,symbol")])
        self.assertEqual(post["json"]["status"], "open")
        self.assertEqual(post["json"]["exchange"], "bingx")
        self.assertEqual(post["json"]["position_id"], "p-1")
        self.assertEqual(post["json"]["stop_loss_pct"], 0.5)
        self.assertEqual(post["json"]["take_profit_pct"], 1.2)

    async def test_open_position_write_failure_does_not_raise(self) -> None:
        await _store(_FakeHTTP(status=500)).save_open_position(_open_position())

    async def test_remove_open_position_issues_a_scoped_delete(self) -> None:
        http = _FakeHTTP()
        await _store(http).remove_open_position("BTC/USDT")
        self.assertEqual(http.deletes[0]["method"], "DELETE")
        self.assertIn(("symbol", "eq.BTC/USDT"), http.deletes[0]["params"])
        self.assertIn(("exchange", "eq.bingx"), http.deletes[0]["params"])

    async def test_list_open_positions_parses_rows(self) -> None:
        http = _FakeHTTP(
            rows=[
                {
                    "symbol": "BTC/USDT",
                    "direction": "LONG",
                    "entry_price": 60000.0,
                    "quantity": 0.01,
                    "exchange_size": 0.01,
                    "exchange_order_id": "e1",
                    "opened_at": "2026-08-22T00:00:00+00:00",
                    "entry_reason": "ai_volume",
                    "position_id": "p-1",
                    "stop_loss_pct": 0.5,
                    "take_profit_pct": 1.2,
                }
            ]
        )
        positions = await _store(http).list_open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].direction, PositionDirection.LONG)
        self.assertEqual(positions[0].position_id, "p-1")
        self.assertEqual(positions[0].stop_loss_pct, 0.5)
        self.assertEqual(positions[0].take_profit_pct, 1.2)

    async def test_missing_envelope_columns_load_as_none(self) -> None:
        http = _FakeHTTP(
            rows=[
                {
                    "symbol": "BTC/USDT",
                    "direction": "LONG",
                    "entry_price": 60000.0,
                    "quantity": 0.01,
                    "exchange_size": 0.01,
                    "exchange_order_id": "e1",
                    "opened_at": "2026-08-22T00:00:00+00:00",
                }
            ]
        )
        positions = await _store(http).list_open_positions()
        self.assertIsNone(positions[0].stop_loss_pct)
        self.assertIsNone(positions[0].take_profit_pct)

    async def test_unreadable_position_rows_are_skipped(self) -> None:
        http = _FakeHTTP(rows=[{"symbol": "BTC/USDT"}])
        self.assertEqual(await _store(http).list_open_positions(), [])

    async def test_list_open_positions_survives_a_read_failure(self) -> None:
        self.assertEqual(await _store(_FakeHTTP(status=500)).list_open_positions(), [])

    async def test_recent_ai_call_metrics_filters_by_since(self) -> None:
        http = _FakeHTTP(rows=[{"call_id": "a"}])
        rows = await _store(http).recent_ai_call_metrics(limit=10, since="2026-08-21T00:00:00+00:00")
        self.assertEqual(rows, [{"call_id": "a"}])
        self.assertIn(("created_at", "gte.2026-08-21T00:00:00+00:00"), http.gets[0]["params"])

    async def test_recent_performance_summary_aggregates(self) -> None:
        http = _FakeHTTP(
            rows=[
                {"entry_reason": "ai_volume", "pnl_usdt": 1.0},
                {"entry_reason": "ai_volume", "pnl_usdt": -0.5},
            ]
        )
        summary = await _store(http).recent_performance_summary(limit=10)
        self.assertEqual(summary["total_trades"], 2)
        self.assertAlmostEqual(summary["total_pnl_usdt"], 0.5)
        self.assertEqual(summary["by_entry_reason_sample"][0]["wins"], 1)

    async def test_empty_performance_summary(self) -> None:
        summary = await _store(_FakeHTTP()).recent_performance_summary()
        self.assertEqual(summary["total_trades"], 0)

    async def test_daily_counters(self) -> None:
        http = _FakeHTTP(rows=[{"reason": "stop_loss"}, {"reason": "take_profit"}])
        store = _store(http)
        self.assertEqual(await store.count_closed_trades_for_day(), 2)
        self.assertEqual(await store.count_stop_losses_for_day(), 1)

    async def test_volume_and_pnl_helpers(self) -> None:
        http = _FakeHTTP(rows=[{"entry_price": 100.0, "exit_price": 101.0, "quantity": 2.0, "pnl_usdt": 2.0}])
        store = _store(http)
        self.assertAlmostEqual(await store.total_volume_for_day(), 402.0)
        self.assertAlmostEqual(await store.total_pnl_for_day(), 2.0)

    async def test_all_time_volume_is_scoped_to_current_exchange(self) -> None:
        http = _FakeHTTP(rows=[])

        await _store(http).total_volume_all_time()

        self.assertIn(("exchange", "eq.bingx"), http.gets[0]["params"])

    async def test_all_time_volume_is_scoped_to_cycle_start(self) -> None:
        cycle_start = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        http = _FakeHTTP(rows=[])

        await _store(http, _settings(volume_cycle_started_at=cycle_start)).total_volume_all_time()

        self.assertIn(("exchange", "eq.bingx"), http.gets[0]["params"])
        self.assertIn(("closed_at", "gte.2026-08-31T12:00:00+00:00"), http.gets[0]["params"])

    async def test_daily_volume_uses_later_of_day_start_and_cycle_start(self) -> None:
        cycle_start = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        http = _FakeHTTP(rows=[])

        await _store(http, _settings(volume_cycle_started_at=cycle_start)).total_volume_for_day(
            date(2026, 8, 31)
        )

        params = http.gets[0]["params"]
        self.assertIn(("exchange", "eq.bingx"), params)
        self.assertIn(("closed_at", "gte.2026-08-31T12:00:00+00:00"), params)
        self.assertNotIn(("closed_at", "gte.2026-08-30T17:00:00+00:00"), params)

    async def test_daily_risk_queries_ignore_cycle_start_but_filter_exchange(self) -> None:
        cycle_start = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
        http = _FakeHTTP(rows=[])
        store = _store(http, _settings(volume_cycle_started_at=cycle_start))

        await store.count_closed_trades_for_day(date(2026, 8, 31))
        await store.count_stop_losses_for_day(date(2026, 8, 31))
        await store.total_pnl_for_day(date(2026, 8, 31))

        for request in http.gets:
            params = request["params"]
            self.assertIn(("exchange", "eq.bingx"), params)
            self.assertIn(("closed_at", "gte.2026-08-30T17:00:00+00:00"), params)
            self.assertNotIn(("closed_at", "gte.2026-08-31T12:00:00+00:00"), params)

    async def test_recent_trade_queries_filter_exchange(self) -> None:
        http = _FakeHTTP(rows=[])
        store = _store(http)

        await store.recent_performance_summary()
        await store.recent_closed_trade_records()

        for request in http.gets:
            self.assertIn(("exchange", "eq.bingx"), request["params"])

    async def test_learning_note_is_a_no_op(self) -> None:
        http = _FakeHTTP()
        await _store(http).save_learning_note(note_type="entry_decision", symbol="BTC/USDT", payload={})
        self.assertEqual(http.posts, [])

    async def test_close_releases_the_client(self) -> None:
        await _store(_FakeHTTP()).close()

    async def test_http_error_is_wrapped_with_the_table_name(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            await _store(_FakeHTTP(status=400)).save_closed_trade(_closed_trade())
        self.assertIn("trades", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
