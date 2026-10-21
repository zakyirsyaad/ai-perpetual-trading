from __future__ import annotations

import logging
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

from bot.storage.supabase_trade_store import _CLOSED_TRADE_COLUMNS, SupabaseTradeStore

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "supabase" / "schema.sql"
PAGE = 1000
SINCE = datetime(2026, 10, 14, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-key",
        supabase_schema="public",
        supabase_trades_table="trades",
        supabase_ai_decisions_table="ai_decisions",
        supabase_ai_exit_verdicts_table="ai_exit_verdicts",
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
    """Records every request; GET serves `rows` honouring PostgREST's limit and offset."""

    def __init__(self, rows=None, status: int = 200) -> None:
        self.rows = rows if rows is not None else []
        self.status = status
        self.posts: list[dict] = []
        self.gets: list[dict] = []
        self.requests: list[dict] = []

    def _response(self, method: str, url: str, payload):
        return httpx.Response(self.status, request=httpx.Request(method, url), json=payload)

    async def post(self, url, json=None, headers=None, params=None):
        self.posts.append({"url": url, "json": json, "headers": headers, "params": params})
        return self._response("POST", url, {})

    async def get(self, url, params=None, headers=None):
        self.gets.append({"url": url, "params": params, "headers": headers})
        query = dict(params or [])
        offset, limit = int(query.get("offset", 0)), int(query.get("limit", PAGE))
        return self._response("GET", url, self.rows[offset : offset + limit])

    async def request(self, method, url, params=None, headers=None, json=None):
        self.requests.append({"method": method, "url": url, "params": params, "headers": headers, "json": json})
        return self._response(method, url, {})

    async def aclose(self) -> None:
        return None


def _store(http: _FakeHTTP, **overrides) -> SupabaseTradeStore:
    store = SupabaseTradeStore(_settings(**overrides), logging.getLogger("test.shadow"))
    store._client = http
    return store


def _shadow_row(**overrides) -> dict:
    row = {
        "decided_at": "2026-10-21T03:00:00+00:00",
        "symbol": "BTC/USDT",
        "side": "short",
        "bucket_key": "TURUN|short|stretch_flat",
        "source": "shadow_side",
        "ref_price": 78000.5,
        "stop_loss_pct": 0.28,
        "floor_pct": 0.324,
        "hold_minutes": 60,
    }
    row.update(overrides)
    return row


class SaveShadowEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_save_posts_to_the_configured_schema(self) -> None:
        http = _FakeHTTP()

        await _store(http, supabase_schema="long_forward").save_shadow_entry(_shadow_row())

        post = http.posts[0]
        self.assertEqual(post["url"], "https://example.supabase.co/rest/v1/shadow_entries")
        self.assertEqual(post["headers"]["Content-Profile"], "long_forward")
        self.assertEqual(post["headers"]["Prefer"], "return=minimal")
        self.assertEqual(post["json"]["bucket_key"], "TURUN|short|stretch_flat")
        self.assertEqual(post["json"]["hold_minutes"], 60)

    async def test_save_stamps_the_exchange_without_mutating_the_caller_row(self) -> None:
        http = _FakeHTTP()
        row = _shadow_row()

        await _store(http).save_shadow_entry(row)

        self.assertEqual(http.posts[0]["json"]["exchange"], "bingx")
        self.assertNotIn("exchange", row)

    async def test_save_keeps_an_exchange_the_row_already_carries(self) -> None:
        http = _FakeHTTP()

        await _store(http).save_shadow_entry(_shadow_row(exchange="okx"))

        self.assertEqual(http.posts[0]["json"]["exchange"], "okx")

    async def test_save_serialises_aware_datetimes_to_utc_iso(self) -> None:
        http = _FakeHTTP()
        wib = timezone(timedelta(hours=7))

        await _store(http).save_shadow_entry(_shadow_row(decided_at=datetime(2026, 10, 21, 10, 0, tzinfo=wib)))

        self.assertEqual(http.posts[0]["json"]["decided_at"], "2026-10-21T03:00:00+00:00")

    async def test_save_refuses_a_naive_datetime(self) -> None:
        http = _FakeHTTP()

        with self.assertRaises(ValueError):
            await _store(http).save_shadow_entry(_shadow_row(decided_at=datetime(2026, 10, 21, 10, 0)))

        self.assertEqual(http.posts, [])

    async def test_save_failure_raises_with_the_table_name(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            await _store(_FakeHTTP(status=400)).save_shadow_entry(_shadow_row())

        self.assertIn("shadow_entries", str(ctx.exception))


class PendingShadowEntriesTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_filters_unevaluated_rows_before_the_cutoff_oldest_first(self) -> None:
        http = _FakeHTTP(rows=[{"id": 1}, {"id": 2}])
        cutoff = datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)

        rows = await _store(http).pending_shadow_entries(cutoff, 3)

        self.assertEqual(rows, [{"id": 1}, {"id": 2}])
        params = http.gets[0]["params"]
        self.assertIn("/rest/v1/shadow_entries", http.gets[0]["url"])
        self.assertIn(("evaluated_at", "is.null"), params)
        self.assertIn(("decided_at", "lt.2026-10-21T04:00:00+00:00"), params)
        self.assertIn(("order", "decided_at.asc"), params)
        self.assertIn(("limit", "3"), params)
        self.assertIn(("exchange", "eq.bingx"), params)

    async def test_pending_normalises_the_cutoff_to_utc(self) -> None:
        http = _FakeHTTP()
        wib = timezone(timedelta(hours=7))

        await _store(http).pending_shadow_entries(datetime(2026, 10, 21, 11, 0, tzinfo=wib), 3)

        self.assertIn(("decided_at", "lt.2026-10-21T04:00:00+00:00"), http.gets[0]["params"])

    async def test_pending_refuses_a_naive_cutoff(self) -> None:
        http = _FakeHTTP()

        with self.assertRaises(ValueError):
            await _store(http).pending_shadow_entries(datetime(2026, 10, 21, 4, 0), 3)

        self.assertEqual(http.gets, [])

    async def test_pending_reads_the_configured_schema(self) -> None:
        http = _FakeHTTP()

        await _store(http, supabase_schema="long_forward").pending_shadow_entries(SINCE, 3)

        self.assertEqual(http.gets[0]["headers"]["Accept-Profile"], "long_forward")


class CompleteShadowEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_patches_one_row_by_id(self) -> None:
        http = _FakeHTTP()
        before = datetime.now(timezone.utc)

        await _store(http, supabase_schema="long_forward").complete_shadow_entry(7, -3.5, "stop")

        patch = http.requests[0]
        self.assertEqual(patch["method"], "PATCH")
        self.assertEqual(patch["url"], "https://example.supabase.co/rest/v1/shadow_entries")
        self.assertIn(("id", "eq.7"), patch["params"])
        self.assertIn(("exchange", "eq.bingx"), patch["params"])
        self.assertEqual(patch["headers"]["Content-Profile"], "long_forward")
        self.assertEqual(patch["headers"]["Prefer"], "return=minimal")
        self.assertEqual(patch["json"]["gross_bps"], -3.5)
        self.assertEqual(patch["json"]["exit_reason"], "stop")
        evaluated_at = datetime.fromisoformat(patch["json"]["evaluated_at"])
        self.assertGreaterEqual(evaluated_at, before)
        self.assertEqual(evaluated_at.utcoffset(), timedelta(0))
        self.assertEqual(http.posts, [])

    async def test_complete_without_an_outcome_still_marks_the_row_evaluated(self) -> None:
        http = _FakeHTTP()

        await _store(http).complete_shadow_entry(9, None, "expired")

        body = http.requests[0]["json"]
        self.assertIsNone(body["gross_bps"])
        self.assertEqual(body["exit_reason"], "expired")
        self.assertIsNotNone(body["evaluated_at"])

    async def test_complete_failure_raises_with_the_table_name(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            await _store(_FakeHTTP(status=500)).complete_shadow_entry(7, 1.0, "time")

        self.assertIn("shadow_entries", str(ctx.exception))


class LatestShadowDecidedAtTests(unittest.IsolatedAsyncioTestCase):
    """The spacing seed: the newest row of one (symbol, side, source, bucket) in any evaluation state."""

    async def test_it_reads_the_newest_row_of_one_key_for_this_exchange(self) -> None:
        http = _FakeHTTP(rows=[{"decided_at": "2026-10-21T03:00:00.12345+00:00"}])

        latest = await _store(http, supabase_schema="long_forward").latest_shadow_decided_at(
            "BTC/USDT", "long", "shadow_side", "LONG|NAIK|atr_mid"
        )

        self.assertEqual(latest, datetime(2026, 10, 21, 3, 0, 0, 123450, tzinfo=timezone.utc))
        get = http.gets[0]
        self.assertEqual(get["url"], "https://example.supabase.co/rest/v1/shadow_entries")
        self.assertEqual(get["headers"]["Accept-Profile"], "long_forward")
        self.assertEqual(
            get["params"],
            [
                ("select", "decided_at"),
                ("exchange", "eq.bingx"),
                ("symbol", "eq.BTC/USDT"),
                ("side", "eq.long"),
                ("source", "eq.shadow_side"),
                ("bucket_key", "eq.LONG|NAIK|atr_mid"),
                ("order", "decided_at.desc"),
                ("limit", "1"),
            ],
        )
        # any evaluation state: no evaluated_at filter
        self.assertNotIn("evaluated_at", [key for key, _ in get["params"]])

    async def test_no_row_or_an_unreadable_time_is_none(self) -> None:
        for rows in ([], [{"decided_at": None}], [{"decided_at": "not a time"}]):
            with self.subTest(rows=rows):
                latest = await _store(_FakeHTTP(rows=rows)).latest_shadow_decided_at(
                    "BTC/USDT", "long", "shadow_side", "LONG|NAIK|atr_mid"
                )
                self.assertIsNone(latest)

    async def test_a_failed_read_raises_with_the_table_name(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            await _store(_FakeHTTP(status=500)).latest_shadow_decided_at("BTC/USDT", "long", "shadow_side", "b")

        self.assertIn("shadow_entries", str(ctx.exception))


class PagedReadsTests(unittest.IsolatedAsyncioTestCase):
    async def test_outcomes_and_trades_since_page_past_1000(self) -> None:
        rows = [{"id": n} for n in range(2 * PAGE + 1)]
        outcomes_http, trades_http = _FakeHTTP(rows=rows), _FakeHTTP(rows=rows)

        outcomes = await _store(outcomes_http).shadow_outcomes_since(SINCE)
        trades = await _store(trades_http).closed_trade_rows_since(SINCE)

        self.assertEqual(outcomes, rows)
        self.assertEqual(trades, rows)
        for http in (outcomes_http, trades_http):
            self.assertEqual([dict(get["params"])["offset"] for get in http.gets], ["0", "1000", "2000"])
            self.assertTrue(all(dict(get["params"])["limit"] == "1000" for get in http.gets))

    async def test_a_short_first_page_costs_one_request(self) -> None:
        http = _FakeHTTP(rows=[{"id": 1}])

        await _store(http).shadow_outcomes_since(SINCE)

        self.assertEqual(len(http.gets), 1)

    async def test_outcomes_are_evaluated_rows_since_their_decision_time_for_this_exchange(self) -> None:
        http = _FakeHTTP()

        await _store(http).shadow_outcomes_since(SINCE)

        params = http.gets[0]["params"]
        self.assertIn("/rest/v1/shadow_entries", http.gets[0]["url"])
        self.assertIn(("evaluated_at", "not.is.null"), params)
        self.assertIn(("decided_at", "gte.2026-10-14T00:00:00+00:00"), params)
        self.assertIn(("exchange", "eq.bingx"), params)
        self.assertIn(("order", "decided_at.asc,id.asc"), params)

    async def test_outcomes_read_both_sources_unless_one_is_given(self) -> None:
        both_http, ledger_http = _FakeHTTP(), _FakeHTTP()

        await _store(both_http).shadow_outcomes_since(SINCE)
        await _store(ledger_http).shadow_outcomes_since(SINCE, source="ledger_block")

        self.assertEqual([key for key, _ in both_http.gets[0]["params"] if key == "source"], [])
        params = ledger_http.gets[0]["params"]
        self.assertEqual([value for key, value in params if key == "source"], ["eq.ledger_block"])
        # the other filters are unchanged
        self.assertIn(("evaluated_at", "not.is.null"), params)
        self.assertIn(("decided_at", "gte.2026-10-14T00:00:00+00:00"), params)
        self.assertIn(("exchange", "eq.bingx"), params)
        self.assertIn(("order", "decided_at.asc,id.asc"), params)

    async def test_the_source_filter_holds_on_every_page(self) -> None:
        http = _FakeHTTP(rows=[{"id": n} for n in range(PAGE + 1)])

        await _store(http).shadow_outcomes_since(SINCE, source="ledger_block")

        self.assertEqual(len(http.gets), 2)
        for get in http.gets:
            self.assertIn(("source", "eq.ledger_block"), get["params"])

    async def test_closed_trades_are_the_closed_trade_columns_since_the_cutoff_oldest_first(self) -> None:
        http = _FakeHTTP()

        await _store(http).closed_trade_rows_since(SINCE)

        params = http.gets[0]["params"]
        self.assertIn("/rest/v1/trades", http.gets[0]["url"])
        self.assertIn(("select", _CLOSED_TRADE_COLUMNS), params)
        self.assertIn(("closed_at", "gte.2026-10-14T00:00:00+00:00"), params)
        self.assertIn(("exchange", "eq.bingx"), params)
        self.assertIn(("order", "closed_at.asc,id.asc"), params)

    async def test_the_volume_cycle_start_never_narrows_an_explicit_since(self) -> None:
        cycle_start = datetime(2026, 10, 19, 4, 14, 18, tzinfo=timezone.utc)
        outcomes_http, trades_http = _FakeHTTP(), _FakeHTTP()

        await _store(outcomes_http, volume_cycle_started_at=cycle_start).shadow_outcomes_since(SINCE)
        await _store(trades_http, volume_cycle_started_at=cycle_start).closed_trade_rows_since(SINCE)

        for http, column in ((outcomes_http, "decided_at"), (trades_http, "closed_at")):
            bounds = [value for key, value in http.gets[0]["params"] if key == column]
            self.assertEqual(bounds, ["gte.2026-10-14T00:00:00+00:00"])

    async def test_reads_refuse_a_naive_since(self) -> None:
        http = _FakeHTTP()
        store = _store(http)

        with self.assertRaises(ValueError):
            await store.shadow_outcomes_since(datetime(2026, 10, 14))
        with self.assertRaises(ValueError):
            await store.closed_trade_rows_since(datetime(2026, 10, 14))

        self.assertEqual(http.gets, [])


class ShadowEntriesSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sql = SCHEMA_SQL.read_text(encoding="utf-8")
        match = re.search(r"create table if not exists public\.shadow_entries \((.*?)\n\);", sql, re.DOTALL)
        assert match is not None, "public.shadow_entries is not defined in supabase/schema.sql"
        cls.sql = sql
        cls.table = match.group(1)

    def test_schema_sql_defines_shadow_entries(self) -> None:
        expected = (
            "id bigint generated always as identity primary key",
            "created_at timestamptz not null default now()",
            "exchange text not null default 'bingx'",
            "decided_at timestamptz not null",
            "symbol text not null",
            "side text not null",
            "bucket_key text not null",
            "source text not null",
            "ref_price double precision not null",
            "stop_loss_pct double precision not null",
            "floor_pct double precision not null",
            "hold_minutes integer not null",
            "gross_bps double precision",
            "exit_reason text",
            "evaluated_at timestamptz",
        )
        columns = [line.strip().rstrip(",") for line in self.table.strip().splitlines()]
        self.assertEqual(columns, list(expected))

    def test_schema_sql_indexes_the_pending_scan_and_locks_the_table_down(self) -> None:
        self.assertIn(
            "create index if not exists shadow_entries_exchange_evaluated_decided_idx "
            "on public.shadow_entries (exchange, evaluated_at, decided_at);",
            self.sql,
        )
        self.assertIn("alter table public.shadow_entries enable row level security;", self.sql)


if __name__ == "__main__":
    unittest.main()
