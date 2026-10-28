"""Tests of the exit-variants shadow report script, ``report_exit_variants``.

The script reads closed trades and the model's OPEN decisions through the store, 1m candles from the kline cache, runs
``research.exit_variants.study`` and prints an Indonesian text report. The tests use a fake store (only the two reads
the report may use; any other attribute raises AttributeError), a temporary kline file and synthetic rows, so nothing
touches the network or a ``.env``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest import mock

import httpx
from pydantic import BaseModel, ValidationError

import report_exit_variants as report
from research.exit_variants import study
from research.exit_variants.walker import SHORT, Cols
from tests.exit_variants_helpers import (
    FLAT,
    LOCK_CATCHES,
    NOTHING_HAPPENS,
    OPENED,
    UTC,
    cols_for,
    decision_row,
    position,
    trade_row,
)

WIB = timezone(timedelta(hours=7))
SINCE = datetime(2026, 10, 26, 0, 0, tzinfo=WIB)
UNTIL = datetime(2026, 10, 29, 0, 0, tzinfo=WIB)
ONE_PRIMARY_ROW = "lock 45->20"


def make_settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        supabase_schema="public",
        exchange_name="bingx",
        taker_fee_pct=0.05,
        volume_neutral_exit_enabled=True,
        volume_neutral_exit_mode="floor",
        volume_neutral_exit_basis="net",
        volume_neutral_exit_min_net_usdt=56.0,
        position_hold_minutes=60,
        stop_loss_pct=0.35,
        atr_sl_multiplier=1.5,
        max_stop_loss_pct=0.40,
        use_exchange_fee_rates=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def write_klines(folder: Path, *bar_sets: Cols, name: str = "klines.json") -> Path:
    """The kline cache layout: {"k1": {"<bar open ms>": [open, high, low, close]}} (the close is not read)."""
    k1: dict[str, list[float]] = {}
    for cols in bar_sets:
        for ms, o, h, low in zip(cols.open_ms, cols.open, cols.high, cols.low):
            k1[str(ms)] = [o, h, low, o]
    path = folder / name
    path.write_text(json.dumps({"k1": k1, "k3": {}}), encoding="utf-8")
    return path


def trade_at(opened: datetime, path: list = LOCK_CATCHES, **overrides: Any):
    """(trade row, its decision row, its candles) for a SHORT opened at ``opened``."""
    start_ms = (int(opened.timestamp()) // 60 + 1) * 60_000
    row = trade_row(
        opened_at=opened.isoformat(), closed_at=(opened + timedelta(minutes=60)).isoformat(), **overrides
    )
    decision = decision_row(opened - timedelta(seconds=4), confidence=row["confidence"])
    return row, decision, cols_for(SHORT, path, start_ms)


class _FakeStore:
    """Only the two reads the report may use; any other attribute raises AttributeError."""

    def __init__(self, trades: list, decisions: Optional[list] = None, fail: Optional[Exception] = None) -> None:
        self._trades = trades
        self._decisions = decisions or []
        self._fail = fail
        self.trade_since: list[datetime] = []
        self.decision_windows: list[tuple[datetime, datetime]] = []
        self.closed = False

    async def closed_trade_rows_since(self, since: datetime) -> list:
        self.trade_since.append(since)
        if self._fail is not None:
            raise self._fail
        return list(self._trades)

    async def open_decision_rows_between(self, start: datetime, end: datetime) -> list:
        self.decision_windows.append((start, end))
        return list(self._decisions)

    async def close(self) -> None:
        self.closed = True


def block_of(out: str, title_start: str) -> str:
    start = out.index(title_start)
    end = out.find("\n=== ", start + 1)
    return out[start : end if end != -1 else len(out)]


def row_cells(block: str, name: str) -> list[str]:
    """The cells after the variant name of its row: n, difference, SE, t, interval, per 1M, changed."""
    line = next(row for row in block.splitlines() if row.startswith(name + " "))
    return line[len(name) :].split()


class _ReportCase(unittest.TestCase):
    def setUp(self) -> None:
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.folder = Path(self._folder.name)

    def run_report(
        self, argv: list[str], store: _FakeStore, settings: Optional[SimpleNamespace] = None
    ) -> tuple[int, str, str]:
        args = report.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = asyncio.run(report.execute(args, store, settings or make_settings()))
        return code, out.getvalue(), err.getvalue()

    def run_scenario(
        self,
        scenario: list,
        extra_argv: tuple[str, ...] = (),
        settings: Optional[SimpleNamespace] = None,
        extra_trades: tuple = (),
    ) -> tuple[int, str, str, _FakeStore]:
        trades = [row for row, _, _ in scenario] + list(extra_trades)
        store = _FakeStore(trades, [decision for _, decision, _ in scenario])
        path = write_klines(self.folder, *(bars for _, _, bars in scenario))
        argv = ["--since", SINCE.isoformat(), "--until", UNTIL.isoformat(), "--klines", str(path), *extra_argv]
        code, out, err = self.run_report(argv, store, settings)
        return code, out, err, store


class LoadKlinesTests(_ReportCase):
    def write(self, payload: Any) -> Path:
        path = self.folder / "k.json"
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
        return path

    def test_it_reads_the_one_minute_bars_in_time_order(self) -> None:
        cols = report.load_klines(self.write({"k1": {"120000": [3, 4, 2, 3.5], "60000": [1, 2, 0.5, 1.5]}, "k3": {}}))
        self.assertEqual(list(cols.open_ms), [60_000, 120_000])
        self.assertEqual(list(cols.open), [1.0, 3.0])
        self.assertEqual(list(cols.high), [2.0, 4.0])
        self.assertEqual(list(cols.low), [0.5, 2.0])

    def test_a_file_that_cannot_be_used_is_refused_with_its_own_reason(self) -> None:
        # each payload is written when its case runs: the helper reuses one file name
        no_bars = 'no 1m bars under "k1"'
        shape = "not [open, high, low, close]"
        prices = "not four positive prices"
        cases = (
            ("not json", "{", "is not valid JSON"),
            ("not an object", [1, 2], no_bars),
            ("no k1", {"k3": {}}, no_bars),
            ("empty k1", {"k1": {}}, no_bars),
            ("k1 not an object", {"k1": [1]}, no_bars),
            ("bad key", {"k1": {"soon": [1, 2, 0.5, 1]}}, shape),
            ("text value", {"k1": {"60000": [1, "high", 0.5, 1]}}, shape),
            ("not a list", {"k1": {"60000": 7}}, shape),
            ("a string that spells four digits", {"k1": {"60000": "1234"}}, shape),
            ("an object", {"k1": {"60000": {"open": 1}}}, shape),
            ("short row", {"k1": {"60000": [1, 2, 0.5]}}, prices),
            ("zero price", {"k1": {"60000": [1, 2, 0, 1]}}, prices),
            ("infinite", '{"k1": {"60000": [1, Infinity, 0.5, 1]}}', prices),
        )
        for label, payload, reason in cases:
            with self.subTest(label), self.assertRaises(report.KlinesError) as raised:
                report.load_klines(self.write(payload))
            self.assertIn(reason, str(raised.exception))
            self.assertEqual(len(str(raised.exception).splitlines()), 1)

    def test_a_missing_file_is_refused_naming_it(self) -> None:
        with self.assertRaises(report.KlinesError) as raised:
            report.load_klines(self.folder / "nope.json")
        self.assertIn("cannot read the kline cache", str(raised.exception))
        self.assertIn("nope.json", str(raised.exception))


class _FakeHTTP:
    """Serves `rows` the way PostgREST pages them and records every GET."""

    def __init__(self, rows: list) -> None:
        self.rows = rows
        self.gets: list[dict] = []

    async def get(self, url, params=None, headers=None):
        self.gets.append({"url": url, "params": list(params or []), "headers": headers})
        query = dict(params or [])
        offset, limit = int(query["offset"]), int(query["limit"])
        return httpx.Response(200, request=httpx.Request("GET", url), json=self.rows[offset : offset + limit])

    async def aclose(self) -> None:
        return None


def store_settings(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
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


class ReportStoreTests(unittest.TestCase):
    START = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    END = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)

    def store(self, http: _FakeHTTP, **overrides: Any) -> report.ReportStore:
        store = report.ReportStore(store_settings(**overrides), logging.getLogger("test.exit_variants"))
        store._client = http  # type: ignore[assignment]
        return store

    def test_it_asks_for_the_open_decisions_of_the_window_with_the_stop_only(self) -> None:
        http = _FakeHTTP([{"created_at": "x", "stop_loss_pct": "0.3"}])
        rows = asyncio.run(self.store(http).open_decision_rows_between(self.START, self.END))
        self.assertEqual(rows, [{"created_at": "x", "stop_loss_pct": "0.3"}])
        (call,) = http.gets
        self.assertEqual(call["url"], "https://example.supabase.co/rest/v1/ai_decisions")
        params = call["params"]
        self.assertIn(("select", "created_at,symbol,action,confidence,payload_json->decision->>stop_loss_pct"), params)
        self.assertIn(("exchange", "eq.bingx"), params)
        self.assertIn(("action", "in.(OPEN_LONG,OPEN_SHORT)"), params)
        self.assertIn(("created_at", "gte.2026-10-26T00:00:00+00:00"), params)
        self.assertIn(("created_at", "lt.2026-10-26T06:00:00+00:00"), params)
        self.assertIn(("order", "created_at.asc,id.asc"), params)

    def test_it_reads_the_schema_of_the_bot_it_runs_in(self) -> None:
        for schema in ("public", "long_forward"):
            with self.subTest(schema=schema):
                http = _FakeHTTP([])
                asyncio.run(self.store(http, supabase_schema=schema).open_decision_rows_between(self.START, self.END))
                self.assertEqual(http.gets[0]["headers"]["Accept-Profile"], schema)

    def test_it_pages_past_the_thousand_row_cap(self) -> None:
        http = _FakeHTTP([{"created_at": str(i)} for i in range(1001)])
        rows = asyncio.run(self.store(http).open_decision_rows_between(self.START, self.END))
        self.assertEqual(len(rows), 1001)
        self.assertEqual(len(http.gets), 2)

    def test_a_naive_time_is_refused_not_guessed(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(self.store(_FakeHTTP([])).open_decision_rows_between(datetime(2026, 10, 26), self.END))


class ParserTests(unittest.TestCase):
    def test_since_is_required(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            report.build_parser().parse_args([])
        self.assertEqual(raised.exception.code, 2)

    def test_text_that_is_not_a_time_is_refused(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            report.build_parser().parse_args(["--since", "yesterday"])
        self.assertEqual(raised.exception.code, 2)

    def test_z_and_offsets_are_read_and_until_defaults_to_none(self) -> None:
        args = report.build_parser().parse_args(["--since", "2026-10-25T17:00:00Z"])
        self.assertEqual(args.since, SINCE)
        self.assertIsNone(args.until)
        args = report.build_parser().parse_args(["--since", "2026-10-26T00:00:00+07:00", "--until", "2026-10-29T00:00:00+07:00"])
        self.assertEqual((args.since, args.until), (SINCE, UNTIL))

    def test_the_kline_cache_defaults_to_the_replay_folder(self) -> None:
        args = report.build_parser().parse_args(["--since", SINCE.isoformat()])
        self.assertEqual(args.klines, report.DEFAULT_KLINES)
        self.assertEqual(report.DEFAULT_KLINES, Path.home() / "replay" / "edge_klines.json")


class UsageAndFailureTests(_ReportCase):
    def one_line_error(self, err: str) -> None:
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertTrue(err.startswith("error:"), err)

    def test_a_naive_since_is_a_usage_error_and_nothing_is_read(self) -> None:
        store = _FakeStore([])
        code, out, err = self.run_report(["--since", "2026-10-26T00:00:00"], store)
        self.assertEqual((code, out), (2, ""))
        self.one_line_error(err)
        self.assertEqual(store.trade_since, [])
        self.assertTrue(store.closed)

    def test_a_naive_until_is_a_usage_error(self) -> None:
        store = _FakeStore([])
        code, _, err = self.run_report(["--since", SINCE.isoformat(), "--until", "2026-10-29T00:00:00"], store)
        self.assertEqual(code, 2)
        self.one_line_error(err)
        self.assertEqual(store.trade_since, [])

    def test_until_not_after_since_is_a_usage_error(self) -> None:
        for until in (SINCE, SINCE - timedelta(days=1)):
            with self.subTest(until=until):
                store = _FakeStore([])
                code, _, err = self.run_report(["--since", SINCE.isoformat(), "--until", until.isoformat()], store)
                self.assertEqual(code, 2)
                self.one_line_error(err)
                self.assertEqual(store.trade_since, [])

    def test_a_missing_kline_cache_is_an_expected_failure_before_anything_is_read(self) -> None:
        store = _FakeStore([])
        argv = ["--since", SINCE.isoformat(), "--klines", str(self.folder / "absent.json")]
        code, out, err = self.run_report(argv, store)
        self.assertEqual((code, out), (1, ""))
        self.one_line_error(err)
        self.assertIn("absent.json", err)
        self.assertEqual(store.trade_since, [])
        self.assertTrue(store.closed)

    def test_live_exits_the_study_cannot_model_are_an_expected_failure(self) -> None:
        path = write_klines(self.folder, cols_for(SHORT, NOTHING_HAPPENS))
        store = _FakeStore([])
        argv = ["--since", SINCE.isoformat(), "--klines", str(path)]
        code, _, err = self.run_report(argv, store, make_settings(volume_neutral_exit_mode="window"))
        self.assertEqual(code, 1)
        self.one_line_error(err)
        self.assertIn("VOLUME_NEUTRAL_EXIT_MODE", err)
        self.assertEqual(store.trade_since, [])

    def test_a_store_failure_is_one_error_line_even_when_the_message_has_many(self) -> None:
        path = write_klines(self.folder, cols_for(SHORT, NOTHING_HAPPENS))
        failure = RuntimeError("Supabase request failed for table trades: 401\nbody={\n  \"message\": \"denied\"\n}")
        store = _FakeStore([], fail=failure)
        code, out, err = self.run_report(["--since", SINCE.isoformat(), "--klines", str(path)], store)
        self.assertEqual((code, out), (1, ""))
        self.one_line_error(err)
        self.assertIn("denied", err)
        self.assertTrue(store.closed)

    def test_a_network_error_is_an_expected_failure_too(self) -> None:
        path = write_klines(self.folder, cols_for(SHORT, NOTHING_HAPPENS))
        store = _FakeStore([], fail=httpx.ConnectError("name resolution failed"))
        code, _, err = self.run_report(["--since", SINCE.isoformat(), "--klines", str(path)], store)
        self.assertEqual(code, 1)
        self.one_line_error(err)

    def test_a_failed_decision_read_is_an_expected_failure(self) -> None:
        class _DecisionsFail(_FakeStore):
            async def open_decision_rows_between(self, start, end):
                raise RuntimeError("Supabase request failed for table ai_decisions: 400")

        row, _, bars = trade_at(OPENED)
        store = _DecisionsFail([row])
        code, _, err = self.run_report(
            ["--since", SINCE.isoformat(), "--klines", str(write_klines(self.folder, bars))], store
        )
        self.assertEqual(code, 1)
        self.one_line_error(err)
        self.assertIn("ai_decisions", err)


class ReportContentTests(_ReportCase):
    def test_one_trade_prints_every_section_and_the_closing_line(self) -> None:
        code, out, err, store = self.run_scenario([trade_at(OPENED)])
        self.assertEqual((code, err), (0, ""))
        for text in (
            "Varian keluar bayangan",
            "Skema Supabase: public | bursa: bingx",
            "Dibaca: posisi tertutup sejak 2026-10-26T00:00+07:00 sampai 2026-10-29T00:00+07:00",
            "=== Sampel ===",
            "=== Kalibrasi ===",
            "=== Parameter (didaftarkan sebelumnya, tidak disetel) ===",
            "=== Aturan keputusan yang didaftarkan sebelumnya ===",
            "Laporan ini tidak mengubah satu pun trade",
        ):
            self.assertIn(text, out)
        for subset in ("Semua posisi", "Posisi tenang"):
            for convention in ("C+1bp", "C", "B"):
                self.assertIn(f"=== {subset} | {convention}:", out)
        self.assertTrue(out.rstrip().endswith("tidak menulis apa pun selain layar."))
        self.assertTrue(store.closed)

    def test_it_reads_from_since_and_the_decisions_of_the_fifteen_minutes_before_the_first_open(self) -> None:
        _, _, _, store = self.run_scenario([trade_at(OPENED), trade_at(OPENED + timedelta(hours=5))])
        self.assertEqual(store.trade_since, [SINCE])
        self.assertEqual(
            store.decision_windows,
            [(OPENED - timedelta(minutes=15), OPENED + timedelta(hours=5, seconds=1))],
        )

    def test_the_primary_row_shows_the_paired_difference_of_one_trade(self) -> None:
        scenario = [trade_at(OPENED)]
        _, out, _, _ = self.run_scenario(scenario)
        found = study.run_study(
            [scenario[0][0]], [scenario[0][1]], scenario[0][2], study.live_rules_from_settings(make_settings())
        )
        delta = found.simulated[0].deltas[("lock 45->20", "C+1bp")].usdt
        cells = row_cells(block_of(out, "=== Semua posisi | C+1bp:"), ONE_PRIMARY_ROW)
        self.assertEqual(cells[0], "1")
        self.assertEqual(cells[1], f"+{delta:.2f}".replace(".", ","))
        self.assertEqual(cells[2:5], ["–", "–", "–"])  # one trade: no SE, no t, no day interval
        self.assertEqual(cells[-1], "1")  # the lock changed that trade
        self.assertAlmostEqual(delta, 12.5, delta=0.05)

    def test_every_variant_has_a_row_in_every_block(self) -> None:
        _, out, _, _ = self.run_scenario([trade_at(OPENED)])
        for subset in ("Semua posisi", "Posisi tenang"):
            for convention in ("C+1bp:", "C:", "B:"):
                block = block_of(out, f"=== {subset} | {convention}")
                for name in ("lock 45->20", "floor +45", "lock 40->15", "trail 40/20"):
                    self.assertTrue(any(row.startswith(name + " ") for row in block.splitlines()), (subset, convention, name))

    def test_the_weekly_table_lists_each_wib_iso_week(self) -> None:
        scenario = [trade_at(OPENED), trade_at(OPENED + timedelta(days=7))]
        _, out, _, _ = self.run_scenario(scenario, extra_argv=("--until", "2026-11-09T00:00:00+07:00"))
        block = block_of(out, "=== Semua posisi | C+1bp:")
        weeks = [row.split() for row in block.splitlines() if row.startswith("2026-W")]
        self.assertEqual([(w[0], w[1]) for w in weeks], [("2026-W44", "1"), ("2026-W45", "1")])
        self.assertIn("lock 45->20", block.split("pekan", 1)[1])  # the table's header names the variants

    def test_the_calm_blocks_hold_only_calm_trades(self) -> None:
        calm = trade_at(OPENED, factor_metadata={"setup": {"atr_pct": 0.10}})
        wild = trade_at(OPENED + timedelta(hours=5), factor_metadata={"setup": {"atr_pct": 0.20}})
        _, out, _, _ = self.run_scenario([calm, wild])
        self.assertEqual(row_cells(block_of(out, "=== Semua posisi | C:"), ONE_PRIMARY_ROW)[0], "2")
        self.assertEqual(row_cells(block_of(out, "=== Posisi tenang | C:"), ONE_PRIMARY_ROW)[0], "1")

    def test_the_sample_section_counts_and_lists_what_was_skipped_with_its_reason(self) -> None:
        good = trade_at(OPENED)
        no_decision = trade_at(OPENED + timedelta(hours=2), confidence=0.5)
        no_decision = (no_decision[0], decision_row(OPENED, confidence=0.99), no_decision[2])  # nothing matches
        no_candles = trade_at(OPENED + timedelta(hours=9))
        no_candles = (no_candles[0], no_candles[1], cols_for(SHORT, FLAT_ONLY, 0))
        _, out, _, _ = self.run_scenario([good, no_decision, no_candles])
        sample = block_of(out, "=== Sampel ===")
        self.assertIn("posisi tertutup dalam jendela: 3", sample)
        self.assertIn("dipakai: 1 | dilewati: 2", sample)
        self.assertIn("periode posisi dipakai (dibuka, WIB): 2026-10-26 sampai 2026-10-26", sample)
        listed = [line for line in sample.splitlines() if line.startswith("  2026-")]
        self.assertEqual(len(listed), 2)
        self.assertIn("2026-10-26T09:15+07:00", listed[0])
        self.assertIn("SHORT", listed[0])
        self.assertIn("tanpa keputusan OPEN yang cocok", listed[0])
        self.assertIn("2026-10-26T16:15+07:00", listed[1])
        self.assertIn("candle 1m tidak lengkap", listed[1])

    def test_skips_are_listed_in_the_order_the_trades_opened_whichever_stage_dropped_them(self) -> None:
        early_no_candles = trade_at(OPENED)
        early_no_candles = (early_no_candles[0], early_no_candles[1], cols_for(SHORT, FLAT_ONLY, 0))
        later_no_decision = trade_at(OPENED + timedelta(hours=3), confidence=0.5)  # dropped before the candles stage
        _, out, _, _ = self.run_scenario([early_no_candles, (later_no_decision[0], decision_row(OPENED), later_no_decision[2])])
        listed = [line for line in block_of(out, "=== Sampel ===").splitlines() if line.startswith("  2026-")]
        self.assertEqual([("candle 1m tidak lengkap" in line) for line in listed], [True, False])

    def test_a_skipped_trade_with_an_unreadable_open_time_is_listed_last(self) -> None:
        broken = trade_row(opened_at="soon")
        _, out, _, _ = self.run_scenario([trade_at(OPENED + timedelta(hours=2), confidence=0.5)], extra_trades=(broken,))
        listed = [line for line in block_of(out, "=== Sampel ===").splitlines() if line.startswith("  ")]
        self.assertIn("waktu tak terbaca", listed[-1])

    def test_trades_closed_at_or_after_until_are_left_out(self) -> None:
        inside = trade_at(OPENED)
        late_open = UNTIL - timedelta(minutes=60)  # closes exactly at --until
        at_until = trade_at(late_open)
        _, out, _, _ = self.run_scenario([inside, at_until])
        self.assertIn("posisi tertutup dalam jendela: 1", out)

    def test_rows_without_a_readable_close_time_are_counted_not_used(self) -> None:
        broken = trade_row(closed_at="soon")
        _, out, _, _ = self.run_scenario([trade_at(OPENED)], extra_trades=(broken,))
        self.assertIn("PERINGATAN: 1 posisi tanpa waktu tutup yang terbaca dilewati.", out)
        self.assertIn("posisi tertutup dalam jendela: 1", out)

    def test_the_calibration_counts_the_matching_exit_reasons(self) -> None:
        _, out, _, _ = self.run_scenario([trade_at(OPENED)])  # a time stop, and the live walk ends on time too
        calibration = block_of(out, "=== Kalibrasi ===")
        self.assertIn("sama dengan yang sebenarnya: 1/1 (100,0%)", calibration)
        self.assertNotIn("PERINGATAN", calibration)

    def test_a_calibration_below_ninety_percent_warns(self) -> None:
        wrong = trade_at(OPENED, reason="stop_loss")  # the real trade was stopped; the walk times out
        _, out, _, _ = self.run_scenario([wrong])
        calibration = block_of(out, "=== Kalibrasi ===")
        self.assertIn("sama dengan yang sebenarnya: 0/1 (0,0%)", calibration)
        self.assertIn("PERINGATAN: kalibrasi 0,0% di bawah 90,0%", calibration)

    def test_exits_the_walk_does_not_model_are_named_in_the_calibration(self) -> None:
        ai_exit = trade_at(OPENED, reason="strategy_exit:model said close")
        _, out, _, _ = self.run_scenario([ai_exit])
        self.assertIn("tidak pernah cocok: 1 posisi", block_of(out, "=== Kalibrasi ==="))

    def test_the_parameters_show_the_live_numbers_the_walk_used(self) -> None:
        _, out, _, _ = self.run_scenario([trade_at(OPENED)])
        parameters = block_of(out, "=== Parameter")
        self.assertIn("biaya taker 0,0500% per sisi", parameters)
        self.assertIn("lantai live net +56 USDT", parameters)
        self.assertIn("batas waktu 60 menit", parameters)
        self.assertIn("posisi tenang: setup.atr_pct di bawah 0,113%", parameters)
        self.assertIn("lock 45->20 (utama), floor +45, lock 40->15, trail 40/20", parameters)
        self.assertIn(
            "selisih/posisi = net varian dikurangi net aturan live pada posisi yang sama (USDT, setelah biaya); "
            "positif berarti varian lebih baik",
            parameters,
        )
        self.assertNotIn("PERINGATAN", parameters)

    def test_a_floor_other_than_the_registered_one_warns(self) -> None:
        settings = make_settings(volume_neutral_exit_min_net_usdt=40.0)
        _, out, _, _ = self.run_scenario([trade_at(OPENED)], settings=settings)
        self.assertIn("PERINGATAN: lantai live net +40 USDT bukan +56", block_of(out, "=== Parameter"))

    def test_a_bot_that_nets_the_venue_fee_warns(self) -> None:
        settings = make_settings(use_exchange_fee_rates=True)
        _, out, _, _ = self.run_scenario([trade_at(OPENED)], settings=settings)
        self.assertIn("PERINGATAN: USE_EXCHANGE_FEE_RATES=true", block_of(out, "=== Parameter"))

    def test_the_schema_of_the_bot_is_named(self) -> None:
        settings = make_settings(supabase_schema="long_forward")
        _, out, _, _ = self.run_scenario([trade_at(OPENED)], settings=settings)
        self.assertIn("Skema Supabase: long_forward", out)

    def test_no_trades_is_a_valid_report_and_reads_no_decisions(self) -> None:
        path = write_klines(self.folder, cols_for(SHORT, NOTHING_HAPPENS))
        store = _FakeStore([])
        code, out, err = self.run_report(["--since", SINCE.isoformat(), "--until", UNTIL.isoformat(), "--klines", str(path)], store)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("dipakai: 0 | dilewati: 0", out)
        self.assertIn("belum ada posisi yang disimulasikan", out)
        self.assertEqual(out.count("-> belum cukup"), 2)
        self.assertEqual(store.decision_windows, [])

    def test_a_small_sample_is_not_enough_and_the_report_says_which_criteria_failed(self) -> None:
        _, out, _, _ = self.run_scenario([trade_at(OPENED, factor_metadata={"setup": {"atr_pct": 0.10}})])  # calm too
        rule = block_of(out, "=== Aturan keputusan")
        self.assertIn("varian utama: lock 45->20, dibaca pada konvensi C+1bp", rule)
        self.assertIn("kandidat semua posisi (n >= 60, rata-rata >= +5,00 USDT/posisi):", rule)
        self.assertIn("[ ] jumlah posisi: 1 (perlu >= 60)", rule)
        self.assertIn("kandidat posisi tenang (setup.atr_pct < 0,113%; n >= 250, rata-rata >= +2,00 USDT/posisi):", rule)
        self.assertIn("[ ] jumlah posisi: 1 (perlu >= 250)", rule)
        self.assertEqual(rule.count("-> belum cukup"), 2)
        self.assertNotIn("layak jadi lengan live", rule)


FLAT_ONLY = [FLAT] * 3  # far too few bars, starting at the epoch: the trade's candles are missing


def viable_sims() -> list:
    """70 trades over six weeks, each worth about +6 USDT under the decision convention, calibrated 100 %."""
    sims = []
    for i in range(70):
        closed = datetime(2026, 10, 12, 12, 0, tzinfo=UTC) + timedelta(days=i % 40, minutes=i)
        delta = 6.0 + 0.5 * (i % 3 - 1)
        pos = position(closed_at=closed, opened_at=closed - timedelta(minutes=60))
        sims.append(study.Simulated(pos, "WAKTU", 0.0, {(study.PRIMARY_VARIANT, study.DECISION_CONVENTION): study.Delta(delta, True)}))
    return sims


class CandidateTextTests(unittest.TestCase):
    def verdict_for(self, sims: list, candidate: study.Candidate) -> study.Verdict:
        summary = study.summarize(sims, study.PRIMARY_VARIANT, study.DECISION_CONVENTION)
        return study.evaluate_rule(candidate, summary, study.calibration(sims).ratio)

    def test_a_candidate_that_meets_every_criterion_is_called_viable(self) -> None:
        verdict = self.verdict_for(viable_sims(), study.ALL_POSITIONS)
        text = report.render_candidate(study.ALL_POSITIONS, verdict)
        self.assertTrue(verdict.passed)
        self.assertIn("-> layak jadi lengan live", text)
        self.assertNotIn("belum cukup", text)
        self.assertIn("[x] jumlah posisi: 70 (perlu >= 60)", text)
        self.assertIn("[x] pekan searah: 4 dari 4 pekan terakhir positif (perlu >= 3 dari 4)", text)
        self.assertIn("[x] kalibrasi: 100,0% (perlu >= 90,0%)", text)

    def test_the_viable_text_is_never_an_instruction(self) -> None:
        text = report.render_candidate(study.ALL_POSITIONS, self.verdict_for(viable_sims(), study.ALL_POSITIONS))
        self.assertIn("hasil aturan, bukan perintah untuk mengaktifkan apa pun", text)

    def test_a_failed_criterion_is_marked_open_and_the_candidate_is_not_enough(self) -> None:
        verdict = self.verdict_for(viable_sims()[:30], study.ALL_POSITIONS)
        text = report.render_candidate(study.ALL_POSITIONS, verdict)
        self.assertIn("[ ] jumlah posisi: 30 (perlu >= 60)", text)
        self.assertIn("-> belum cukup", text)
        self.assertNotIn("layak jadi lengan live", text)

    def test_fewer_than_four_weeks_are_said_plainly(self) -> None:
        few = [s for s in viable_sims() if s.position.closed_at < datetime(2026, 10, 18, tzinfo=UTC)]  # week 42 only
        text = report.render_candidate(study.ALL_POSITIONS, self.verdict_for(few, study.ALL_POSITIONS))
        self.assertIn("baru 1 pekan", text)

    def test_the_calm_candidate_names_its_threshold(self) -> None:
        text = report.render_candidate(study.CALM_POSITIONS, self.verdict_for(viable_sims(), study.CALM_POSITIONS))
        self.assertIn("kandidat posisi tenang (setup.atr_pct < 0,113%; n >= 250, rata-rata >= +2,00 USDT/posisi):", text)
        self.assertIn("[ ] jumlah posisi: 70 (perlu >= 250)", text)

    def test_missing_figures_print_as_dashes(self) -> None:
        empty = study.summarize([], study.PRIMARY_VARIANT, study.DECISION_CONVENTION)
        text = report.render_candidate(study.ALL_POSITIONS, study.evaluate_rule(study.ALL_POSITIONS, empty, None))
        self.assertIn("[ ] rata-rata selisih: – USDT/posisi (perlu >= +5,00)", text)
        self.assertIn("[ ] t: – (perlu >= 2,0)", text)
        self.assertIn("[ ] pekan searah: belum ada pekan (perlu >= 3 dari 4)", text)
        self.assertIn("[ ] kalibrasi: – (perlu >= 90,0%)", text)


class FormatTests(unittest.TestCase):
    def test_signed_numbers_use_indonesian_notation(self) -> None:
        self.assertEqual(report.signed(1.5, 2), "+1,50")
        self.assertEqual(report.signed(-1.5, 2), "-1,50")
        self.assertEqual(report.signed(1234.6, 0), "+1.235")
        self.assertEqual(report.signed(0.0, 2), "0,00")

    def test_a_value_that_rounds_to_zero_carries_no_sign(self) -> None:
        self.assertEqual(report.signed(0.004, 2), "0,00")
        self.assertEqual(report.signed(-0.004, 2), "0,00")

    def test_missing_or_non_finite_numbers_are_a_dash(self) -> None:
        for value in (None, float("nan"), float("inf")):
            self.assertEqual(report.signed(value, 2), "–")


class RunTests(unittest.TestCase):
    """`_run` is the shell between `Settings()`, the store and `execute`: it must leave the bot's directory alone."""

    def setUp(self) -> None:
        self._cwd, self._data = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(self._cwd.cleanup)
        self.addCleanup(self._data.cleanup)
        self.cwd = Path(self._cwd.name)
        klines = write_klines(Path(self._data.name), cols_for(SHORT, NOTHING_HAPPENS))
        self.args = report.build_parser().parse_args(["--since", SINCE.isoformat(), "--klines", str(klines)])

    def run_shell(self, settings_patch: Any) -> tuple[int, str, str, Any]:
        """`_run` from inside ``self.cwd`` with ``Settings`` and the store replaced; returns code, stdout, stderr, store."""
        store = _FakeStore([])
        previous, out, err = os.getcwd(), io.StringIO(), io.StringIO()
        os.chdir(self.cwd)
        try:
            with redirect_stdout(out), redirect_stderr(err), settings_patch, mock.patch.object(
                report, "ReportStore", return_value=store
            ) as made:
                code = asyncio.run(report._run(self.args))
        finally:
            os.chdir(previous)
        return code, out.getvalue(), err.getvalue(), made

    def test_a_run_creates_no_log_file_or_directory_where_it_runs(self) -> None:
        # setup_logging() opens logs/bot.log in the working directory: for the live bot that is its own rotating log,
        # which a second process must not touch (and the report promises to write nothing but stdout)
        patch = mock.patch.object(report, "Settings", return_value=make_settings(log_level="INFO"))
        code, out, _, made = self.run_shell(patch)
        self.assertEqual(code, 0)
        self.assertIn("=== Sampel ===", out)
        self.assertEqual(os.listdir(self.cwd), [])
        made.assert_called_once()

    def test_invalid_settings_are_one_error_line_without_any_value_and_no_store_is_opened(self) -> None:
        class Model(BaseModel):
            leverage: int

        with self.assertRaises(ValidationError) as raised:
            Model(leverage="sentinel-4242-value")
        patch = mock.patch.object(report, "Settings", side_effect=raised.exception)
        code, out, err, made = self.run_shell(patch)
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertTrue(err.startswith("error:"))
        self.assertIn("leverage", err)
        self.assertNotIn("sentinel", err)
        made.assert_not_called()

    def test_main_parses_the_command_line_and_returns_the_exit_code(self) -> None:
        shell = mock.AsyncMock(return_value=0)
        argv = ["report_exit_variants.py", "--since", SINCE.isoformat()]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(report, "_run", shell):
            self.assertEqual(report.main(), 0)
        self.assertEqual(shell.await_args.args[0].since, SINCE)


class SettingsProblemTests(unittest.TestCase):
    def test_it_names_the_fields_and_never_prints_a_value(self) -> None:
        # pydantic's own message echoes `input_value=...`; a bad value in a real .env could be a credential
        class Model(BaseModel):
            leverage: int
            size: int

        with self.assertRaises(ValidationError) as raised:
            Model(leverage="x", size="sentinel-4242-value")
        self.assertIn("sentinel-4242-value", str(raised.exception))  # the problem this guards against is real
        text = report.settings_problem(raised.exception)
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("leverage", text)
        self.assertIn("size", text)
        self.assertNotIn("sentinel", text)


if __name__ == "__main__":
    unittest.main()
