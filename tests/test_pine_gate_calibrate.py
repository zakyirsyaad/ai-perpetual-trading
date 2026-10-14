import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from research.pine_gate import calibrate
from research.pine_gate.spec import FillConfig
from research.pine_gate.timeparse import parse_iso_datetime
from research.pine_gate.calibrate import (
    MAX_PAGES,
    PAGE_SIZE,
    READ_TABLES,
    Calibration,
    TooManyPagesError,
    fetch_all,
    load_calibration,
    load_entry_latencies_ms,
    make_get_rows,
    payload_from_row,
    s_star,
    stop_overshoot_detail,
    stop_overshoot_samples,
)

T0 = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


class FakeRows:
    """Async stand-in for SupabaseTradeStore._get_rows: eq./gte./in. filters,
    order, limit and offset over in-memory tables."""

    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    async def __call__(self, table, params):
        self.calls.append((table, list(params)))
        rows = list(self.tables.get(table, []))
        limit, offset, order = None, 0, None
        for key, value in params:
            if key == "limit":
                limit = int(value)
            elif key == "offset":
                offset = int(value)
            elif key == "order":
                order = value
            elif key == "select":
                continue
            elif value.startswith("eq."):
                want = value[3:]
                rows = [r for r in rows if str(r.get(key)).lower() == want]
            elif value.startswith("gte."):
                bound = parse_iso_datetime(value[4:])
                rows = [r for r in rows if parse_iso_datetime(r.get(key)) is not None
                        and parse_iso_datetime(r.get(key)) >= bound]
            elif value.startswith("in.("):
                allowed = value[4:-1].split(",")
                rows = [r for r in rows if r.get(key) in allowed]
        if order == "created_at.asc,id.asc":
            rows.sort(key=lambda r: (r.get("created_at"), r.get("id", 0)))
        rows = rows[offset:]
        return rows if limit is None else rows[:limit]


def metric(created_at, latency, purpose="entry", success=True, idx=0):
    return {
        "id": idx,
        "created_at": created_at,
        "purpose": purpose,
        "success": success,
        "latency_ms": latency,
    }


def decision(idx, created_at, symbol, action, ai_stop, atr, last, extra_techs=True):
    technicals = []
    if extra_techs:
        technicals.append({"symbol": "OTHER/USDT", "atr_14": 9.0, "last_price": 10.0})
    technicals.append({"symbol": symbol, "atr_14": atr, "last_price": last})
    return {
        "id": idx,
        "created_at": iso(created_at),
        "symbol": symbol,
        "action": action,
        "payload_json": {
            "decision": {"stop_loss_pct": ai_stop},
            "request": {"market_context": {"technicals": technicals}},
        },
    }


def stop_trade(idx, direction, entry, exit_, opened_at, symbol="BTC/USDT"):
    return {
        "id": idx,
        "created_at": iso(opened_at + timedelta(minutes=5)),
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry,
        "exit_price": exit_,
        "reason": "stop_loss",
        "exchange": "bingx",
        "opened_at": iso(opened_at),
        "closed_at": iso(opened_at + timedelta(minutes=5)),
    }


class LatencyTests(unittest.TestCase):
    def test_latencies_filter_entry_success_since(self):
        rows = [
            metric("2026-09-26T00:00:00+00:00", 1500.0, idx=1),
            metric("2026-09-27T00:00:00+00:00", 2500.5, idx=2),
            metric("2026-09-24T16:59:59+00:00", 999.0, idx=3),  # before 2026-09-25 00:00 WIB
            metric("2026-09-24T17:00:00+00:00", 1000.0, idx=9),  # 2026-09-25 00:00 WIB
            metric("2026-09-28T00:00:00+00:00", 700.0, purpose="exit", idx=4),
            metric("2026-09-28T00:00:00+00:00", 800.0, success=False, idx=5),
            metric("2026-09-29T00:00:00+00:00", float("nan"), idx=6),
            metric("2026-09-29T00:00:00+00:00", -3.0, idx=7),
            metric("2026-09-29T00:00:00+00:00", None, idx=8),
        ]
        fake = FakeRows({"ai_call_metrics": rows})

        result = asyncio.run(load_entry_latencies_ms(fake, "ai_call_metrics"))

        self.assertEqual(result, [1000.0, 1500.0, 2500.5])
        params = dict(fake.calls[0][1])
        self.assertEqual(params["purpose"], "eq.entry")
        self.assertEqual(params["success"], "eq.true")
        self.assertEqual(params["created_at"], "gte.2026-09-25T00:00:00+07:00")

    def test_paging_returns_all_rows_across_pages(self):
        rows = [
            metric("2026-09-26T00:00:%02d+00:00" % (i % 60), float(i), idx=i)
            for i in range(PAGE_SIZE + 3)
        ]
        fake = FakeRows({"ai_call_metrics": rows})

        result = asyncio.run(load_entry_latencies_ms(fake, "ai_call_metrics"))

        self.assertEqual(len(result), PAGE_SIZE + 3)
        self.assertEqual(len(fake.calls), 2)
        first = dict(fake.calls[0][1])
        second = dict(fake.calls[1][1])
        self.assertEqual((first["limit"], first["offset"]), (str(PAGE_SIZE), "0"))
        self.assertEqual((second["limit"], second["offset"]), (str(PAGE_SIZE), str(PAGE_SIZE)))
        self.assertEqual(first["order"], "created_at.asc,id.asc")


class StopReconstructionTests(unittest.TestCase):
    def _run(self, trades, decisions):
        fake = FakeRows({"trades": trades, "ai_decisions": decisions})
        detail = asyncio.run(stop_overshoot_detail(fake, "trades", "ai_decisions"))
        return fake, detail

    def test_stop_reconstruction_uses_ai_stop_atr_and_cap(self):
        trades = [
            # AI stop 0.30 wins over atr 0.15 -> stop 0.30; adverse 0.50 -> 20 bps
            stop_trade(1, "LONG", 100.0, 99.5, T0),
            # atr_pct 0.2 * 1.5 = 0.30 beats AI 0.10; SHORT adverse 0.40 -> 10 bps
            stop_trade(2, "SHORT", 100.0, 100.4, T0 + timedelta(hours=1)),
            # AI 0.9 capped at 0.4; LONG adverse 0.45 -> 5 bps
            stop_trade(3, "LONG", 100.0, 99.55, T0 + timedelta(hours=2)),
        ]
        decisions = [
            decision(10, T0 - timedelta(seconds=30), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0),
            decision(11, T0 + timedelta(hours=1, seconds=-60), "BTC/USDT", "OPEN_SHORT", 0.10, 0.2, 100.0),
            decision(12, T0 + timedelta(hours=2, seconds=-10), "BTC/USDT", "OPEN_LONG", 0.90, 0.1, 100.0),
        ]

        fake, detail = self._run(trades, decisions)

        self.assertEqual(len(detail.samples), 3)
        for got, want in zip(detail.samples, (20.0, 10.0, 5.0)):
            self.assertAlmostEqual(got, want, places=6)
        self.assertEqual(detail.skipped, 0)
        samples = asyncio.run(stop_overshoot_samples(fake, "trades", "ai_decisions"))
        self.assertEqual(len(samples), 3)
        trade_params = dict(fake.calls[0][1])
        self.assertEqual(trade_params["reason"], "eq.stop_loss")
        self.assertEqual(trade_params["exchange"], "eq.bingx")
        self.assertEqual(trade_params["opened_at"], "gte.2026-09-12T00:00:00+07:00")
        decision_params = dict(fake.calls[1][1])
        self.assertEqual(decision_params["action"], "in.(OPEN_LONG,OPEN_SHORT)")
        self.assertEqual(decision_params["created_at"], "gte.2026-09-11T00:00:00+07:00")

    def test_latest_matching_decision_in_window_is_used(self):
        trades = [stop_trade(1, "LONG", 100.0, 99.5, T0)]
        decisions = [
            # older in-window decision (AI stop 0.4) must lose to the later one (0.3)
            decision(10, T0 - timedelta(seconds=120), "BTC/USDT", "OPEN_LONG", 0.40, 0.1, 100.0),
            decision(11, T0 - timedelta(seconds=20), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0),
            # after opened_at: ignored
            decision(12, T0 + timedelta(seconds=5), "BTC/USDT", "OPEN_LONG", 0.10, 0.1, 100.0),
        ]
        _, detail = self._run(trades, decisions)
        self.assertAlmostEqual(detail.samples[0], 20.0, places=6)

    def test_unmatched_trades_are_skipped(self):
        trades = [
            stop_trade(1, "LONG", 100.0, 99.5, T0),  # ok
            stop_trade(2, "LONG", 100.0, 99.5, T0 + timedelta(hours=1)),  # decision too old (>180 s)
            stop_trade(3, "SHORT", 100.0, 100.5, T0 + timedelta(hours=2)),  # only LONG decision
            stop_trade(4, "LONG", 100.0, 99.5, T0 + timedelta(hours=3), symbol="ETH/USDT"),  # wrong symbol
            stop_trade(5, "LONG", 100.0, 99.5, T0 + timedelta(hours=4)),  # no atr entry for symbol
            stop_trade(6, "LONG", 100.0, None, T0 + timedelta(hours=5)),  # unparseable exit
        ]
        no_atr = decision(15, T0 + timedelta(hours=4, seconds=-5), "BTC/USDT", "OPEN_LONG", 0.3, 0.1, 100.0)
        no_atr["payload_json"]["request"]["market_context"]["technicals"] = [
            {"symbol": "ETH/USDT", "atr_14": 1.0, "last_price": 100.0}
        ]
        decisions = [
            decision(10, T0 - timedelta(seconds=30), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0),
            decision(11, T0 + timedelta(hours=1, seconds=-181), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0),
            decision(12, T0 + timedelta(hours=2, seconds=-30), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0),
            decision(13, T0 + timedelta(hours=3, seconds=-30), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0),
            no_atr,
            decision(16, T0 + timedelta(hours=5, seconds=-5), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0),
        ]

        _, detail = self._run(trades, decisions)

        self.assertEqual(len(detail.samples), 1)
        self.assertEqual(detail.skipped, 5)

    def test_malformed_payload_is_skipped_not_raised(self):
        trades = [stop_trade(1, "LONG", 100.0, 99.5, T0)]
        bad = decision(10, T0 - timedelta(seconds=30), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0)
        bad["payload_json"] = {"decision": "oops"}
        _, detail = self._run(trades, [bad])
        self.assertEqual((detail.samples, detail.skipped), ((), 1))

    def test_paging_trades_across_pages(self):
        trades = [
            stop_trade(i, "LONG", 100.0, 99.5, T0 + timedelta(seconds=i)) for i in range(PAGE_SIZE + 3)
        ]
        fake, detail = self._run(trades, [])
        self.assertEqual(detail.skipped, PAGE_SIZE + 3)
        trade_calls = [c for c in fake.calls if c[0] == "trades"]
        self.assertEqual(len(trade_calls), 2)


class JsonPathSelectTests(unittest.TestCase):
    """Decisions are read as three JSON paths, not the whole payload_json (D2)."""

    def test_decision_query_selects_only_the_needed_paths(self):
        fake = FakeRows({"trades": [], "ai_decisions": []})
        asyncio.run(stop_overshoot_detail(fake, "trades", "ai_decisions"))
        params = dict(fake.calls[1][1])
        self.assertEqual(params["select"], "id,created_at,symbol,action,"
                         "pj_decision:payload_json->decision,"
                         "pj_technicals:payload_json->request->market_context->technicals")
        self.assertNotIn("payload_json,", params["select"] + ",")
        self.assertEqual(params["limit"], str(calibrate.DECISION_PAGE_SIZE))

    def test_rows_with_path_columns_reconstruct_the_stop(self):
        trades = [stop_trade(1, "LONG", 100.0, 99.5, T0)]
        full = decision(10, T0 - timedelta(seconds=30), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0)
        payload = full.pop("payload_json")
        selected = dict(full, pj_decision=payload["decision"],
                        pj_technicals=payload["request"]["market_context"]["technicals"])
        fake = FakeRows({"trades": trades, "ai_decisions": [selected]})
        detail = asyncio.run(stop_overshoot_detail(fake, "trades", "ai_decisions"))
        self.assertEqual(len(detail.samples), 1)
        self.assertAlmostEqual(detail.samples[0], 20.0, places=6)
        absent = dict(full, pj_decision=None, pj_technicals=None)  # a path the row lacks
        fake = FakeRows({"trades": trades, "ai_decisions": [absent]})
        detail = asyncio.run(stop_overshoot_detail(fake, "trades", "ai_decisions"))
        self.assertEqual((detail.samples, detail.skipped), ((), 1))

    def test_payload_from_row_prefers_a_full_payload_and_parses_text(self):
        self.assertEqual(payload_from_row({"payload_json": {"a": 1}, "pj_decision": {"b": 2}}),
                         {"a": 1})
        self.assertEqual(payload_from_row({"payload_json": '{"a": 1}'}), {"a": 1})
        self.assertEqual(payload_from_row({"payload_json": "not json"}), {})
        self.assertEqual(payload_from_row({"pj_decision": {"x": 1}, "pj_ai_call_id": "c"}),
                         {"decision": {"x": 1}, "ai_call_id": "c"})
        self.assertEqual(payload_from_row({"pj_technicals": [1]}),
                         {"request": {"market_context": {"technicals": [1]}}})
        self.assertEqual(payload_from_row({}), {})


class PagingCapTests(unittest.TestCase):
    def test_a_server_that_keeps_returning_full_pages_is_refused(self):
        calls = []

        async def endless(table, params):
            calls.append(dict(params))
            return [{"id": len(calls)}] * 2

        with self.assertRaises(TooManyPagesError):
            asyncio.run(fetch_all(endless, "trades", [], page_size=2, max_pages=3))
        self.assertEqual(len(calls), 3)
        self.assertEqual([c["offset"] for c in calls], ["0", "2", "4"])
        self.assertGreaterEqual(MAX_PAGES, 100)


class FakeStore:
    """Stands in for SupabaseTradeStore: records reads, fails on any write."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.reads = []
        self.closed = False

    async def _get_rows(self, table, params):
        self.reads.append((table, list(params)))
        offset = int(dict(params).get("offset", "0"))
        return list(self.rows) if offset == 0 else []

    async def _insert(self, *a, **k):
        raise AssertionError("write")

    async def _upsert(self, *a, **k):
        raise AssertionError("write")

    async def _delete(self, *a, **k):
        raise AssertionError("write")

    async def close(self):
        self.closed = True


class ReadOnlyReaderTests(unittest.TestCase):
    """make_get_rows yields only a GET closure over three tables and closes the store (D1)."""

    def read(self, store, body):
        async def run():
            async with make_get_rows(object(), store_factory=lambda settings: store) as get_rows:
                return await body(get_rows)

        return asyncio.run(run())

    def test_only_reads_of_the_three_tables_reach_the_store(self):
        self.assertEqual(READ_TABLES, frozenset({"trades", "ai_decisions", "ai_call_metrics"}))
        store = FakeStore([metric("2026-09-26T00:00:00+00:00", 1200.0, idx=1)])

        async def body(get_rows):
            latencies = await load_entry_latencies_ms(get_rows, "ai_call_metrics")
            for table in ("open_position_state", "ai_exit_verdicts", "trades; drop"):
                with self.assertRaises(ValueError):
                    await get_rows(table, [("select", "id")])
            return latencies

        self.assertEqual(self.read(store, body), [1200.0])
        self.assertEqual([t for t, _ in store.reads], ["ai_call_metrics"])
        self.assertTrue(store.closed)

    def test_the_yielded_reader_exposes_no_store_or_write_method(self):
        store = FakeStore()

        async def body(get_rows):
            return get_rows

        get_rows = self.read(store, body)
        self.assertFalse(hasattr(get_rows, "__self__"))
        for name in ("_insert", "_upsert", "_delete", "_client", "_get_rows", "close", "store"):
            self.assertFalse(hasattr(get_rows, name), name)

    def test_the_store_is_closed_when_the_read_fails(self):
        store = FakeStore()

        async def body(get_rows):
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self.read(store, body)
        self.assertTrue(store.closed)


class CalibrationPolicyTests(unittest.TestCase):
    def test_floor_and_wib_dates_live_in_the_hashed_fill_policy(self):
        fill = FillConfig()
        self.assertEqual(fill.floor_bps, 0.0)
        self.assertEqual(fill.since_utc_offset, "+07:00")
        self.assertEqual(calibrate.DEFAULT_OVERSHOOT_SINCE, "2026-09-12T00:00:00+07:00")
        self.assertEqual(calibrate.DEFAULT_LATENCY_SINCE, "2026-09-25T00:00:00+07:00")


class SStarTests(unittest.TestCase):
    def test_s_star_rounds_up_to_half_bp(self):
        self.assertEqual(s_star([1.2] * 15), 1.5)
        self.assertEqual(s_star([1.5] * 15), 1.5)
        self.assertEqual(s_star([1.51] * 15), 2.0)
        self.assertEqual(s_star([-0.7] * 15), 0.0)  # never better than the stop level
        self.assertEqual(s_star([-3.0] * 15), 0.0)
        self.assertEqual(s_star([0.2] * 15), 0.5)
        self.assertEqual(s_star(list(range(1, 17))), 8.5)  # median 8.5

    def test_s_star_falls_back_below_15_samples(self):
        self.assertEqual(s_star([3.0] * 14), 1.0)
        self.assertEqual(s_star([], fallback=2.0), 2.0)
        self.assertEqual(s_star([3.0] * 14, min_samples=10), 3.0)


class LoadCalibrationTests(unittest.TestCase):
    def test_load_calibration_combines_readers(self):
        fake = FakeRows(
            {
                "ai_call_metrics": [metric("2026-09-26T00:00:00+00:00", 1200.0, idx=1)],
                "trades": [stop_trade(1, "LONG", 100.0, 99.5, T0)],
                "ai_decisions": [
                    decision(10, T0 - timedelta(seconds=30), "BTC/USDT", "OPEN_LONG", 0.30, 0.1, 100.0)
                ],
            }
        )
        result = asyncio.run(load_calibration(fake, "trades", "ai_decisions", "ai_call_metrics"))
        self.assertIsInstance(result, Calibration)
        self.assertEqual(result.n_stops, 1)
        self.assertEqual(result.s_star_bps, 1.0)  # below min_samples -> fallback
        self.assertEqual(result.latencies_ms, (1200.0,))


if __name__ == "__main__":
    unittest.main()
