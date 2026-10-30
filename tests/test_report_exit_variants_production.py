"""Tests of the production-candle reading of the exit-variants report (``--production-candles``).

The registered read of the study uses the VST candles of the cache. This option reads the same minutes from the public
production endpoint instead, as a sensitivity beside it: the numbers come from the same code and only the candles differ,
and the text says so. The download is replaced by a mock, so nothing here touches the network or the cache file.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from datetime import timedelta
from typing import Any, Optional
from unittest import mock

import httpx

import report_exit_variants as report
from research.exit_variants import study
from research.exit_variants.walker import Cols
from research.pine_gate import fetch as venue
from tests.exit_variants_helpers import OPENED
from tests.test_report_exit_variants import (
    SINCE,
    UNTIL,
    _FakeStore,
    _ReportCase,
    make_settings,
    trade_at,
    viable_sims,
)

STEP = 60_000
FIRST_MINUTE_MS = int(OPENED.replace(second=0, microsecond=0).timestamp()) * 1000
LATER = OPENED + timedelta(hours=5)  # far enough from the first trade that their candles do not overlap
CLOSING_VST = (
    "Laporan ini tidak mengubah satu pun trade: hanya membaca (Supabase GET dan berkas candle), "
    "tidak menulis apa pun selain layar."
)
CLOSING_PRODUCTION = (
    "Laporan ini tidak mengubah satu pun trade: hanya membaca (Supabase GET dan GET publik candle produksi), "
    "tidak menulis apa pun selain layar."
)
SOURCE_NOTE = (
    "Sumber candle: PRODUKSI (open-api.bingx.com, publik), bukan cache VST: bacaan sensitivitas. "
    "Vonis terdaftar dibaca dari run tanpa --production-candles."
)
RULE_HEADING = (
    "=== Aturan keputusan yang didaftarkan sebelumnya "
    "(dibaca pada candle PRODUKSI: sensitivitas, bukan vonis terdaftar) ==="
)


def merged(*bar_sets: Cols) -> Cols:
    """One ``Cols`` from several candle sets that share no minute."""
    rows = sorted(
        (ms, o, h, low) for cols in bar_sets for ms, o, h, low in zip(cols.open_ms, cols.open, cols.high, cols.low)
    )
    return Cols([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows])


def two_trades() -> list:
    return [trade_at(OPENED), trade_at(LATER)]


class FlagTests(unittest.TestCase):
    def test_the_flag_is_off_by_default_and_keeps_the_registered_cache(self) -> None:
        args = report.build_parser().parse_args(["--since", SINCE.isoformat()])
        self.assertFalse(args.production_candles)
        self.assertEqual(args.klines, report.DEFAULT_KLINES)

    def test_the_flag_is_a_plain_switch(self) -> None:
        args = report.build_parser().parse_args(["--since", SINCE.isoformat(), "--production-candles"])
        self.assertTrue(args.production_candles)

    def test_the_flag_and_a_cache_file_exclude_each_other(self) -> None:
        argv = ["--since", SINCE.isoformat(), "--production-candles", "--klines", "somewhere.json"]
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            report.build_parser().parse_args(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with", err.getvalue())


class ProductionReadTests(_ReportCase):
    def run_production(
        self,
        scenario: list,
        extra_argv: tuple[str, ...] = (),
        fetch: Optional[mock.Mock] = None,
        settings: Any = None,
    ) -> tuple[int, str, str, _FakeStore, mock.Mock]:
        store = _FakeStore([row for row, _, _ in scenario], [decision for _, decision, _ in scenario])
        fetch = fetch if fetch is not None else mock.Mock(return_value=merged(*(bars for _, _, bars in scenario)))
        argv = ["--since", SINCE.isoformat(), "--until", UNTIL.isoformat(), "--production-candles", *extra_argv]
        with mock.patch.object(report.candles, "production_cols", fetch), mock.patch.object(
            report, "load_klines", side_effect=AssertionError("the cache must not be read")
        ):
            code, out, err = self.run_report(argv, store, settings)
        return code, out, err, store, fetch

    def registered_read(self, scenario: list) -> str:
        code, out, err, _ = self.run_scenario(scenario)
        self.assertEqual((code, err), (0, ""))
        return out

    def test_it_downloads_the_minutes_of_the_trades_and_never_reads_the_cache(self) -> None:
        code, out, err, _, fetch = self.run_production([trade_at(OPENED)])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("=== Sampel ===", out)
        fetch.assert_called_once_with(FIRST_MINUTE_MS, FIRST_MINUTE_MS + 62 * STEP)

    def test_several_trades_ask_for_one_window_from_the_first_open_to_the_last_time_exit(self) -> None:
        _, _, _, _, fetch = self.run_production(two_trades())
        last_minute_ms = int(LATER.replace(second=0, microsecond=0).timestamp()) * 1000
        fetch.assert_called_once_with(FIRST_MINUTE_MS, last_minute_ms + 62 * STEP)

    def test_the_hold_time_of_the_bot_sets_how_far_the_window_reaches(self) -> None:
        settings = make_settings(position_hold_minutes=30)
        _, _, _, _, fetch = self.run_production([trade_at(OPENED)], settings=settings)
        fetch.assert_called_once_with(FIRST_MINUTE_MS, FIRST_MINUTE_MS + 32 * STEP)

    def test_the_numbers_are_those_of_the_registered_read_on_the_same_candles(self) -> None:
        scenario = two_trades()
        _, production, _, _, _ = self.run_production(scenario)
        registered = self.registered_read(scenario)

        def body(text: str) -> str:  # from the sample to the end of the parameters and tables, before the rule
            return text[text.index("=== Sampel ===") : text.index("=== Aturan keputusan")]

        self.assertEqual(body(production), body(registered))

    def test_it_says_it_is_a_sensitivity_and_the_registered_read_says_nothing_of_the_kind(self) -> None:
        scenario = two_trades()
        _, production, _, _, _ = self.run_production(scenario)
        registered = self.registered_read(scenario)
        self.assertIn(SOURCE_NOTE, production)
        self.assertIn(RULE_HEADING, production)
        for word in ("PRODUKSI", "sensitivitas", "produksi"):
            self.assertNotIn(word, registered)

    def test_the_closing_line_names_the_public_candle_read(self) -> None:
        scenario = two_trades()
        _, production, _, _, _ = self.run_production(scenario)
        self.assertTrue(production.rstrip().endswith(CLOSING_PRODUCTION), production[-200:])
        self.assertTrue(self.registered_read(scenario).rstrip().endswith(CLOSING_VST))

    def test_no_trades_download_nothing(self) -> None:
        code, out, err, store, fetch = self.run_production([])
        self.assertEqual((code, err), (0, ""))
        fetch.assert_not_called()
        self.assertIn("posisi tertutup dalam jendela: 0", out)
        self.assertTrue(store.closed)

    def test_a_trade_whose_minutes_were_not_returned_is_listed_as_skipped(self) -> None:
        first, second = two_trades()
        fetch = mock.Mock(return_value=merged(first[2]))  # the venue returned the first trade's minutes only
        _, out, _, _, _ = self.run_production([first, second], fetch=fetch)
        self.assertIn("dipakai: 1 | dilewati: 1", out)
        self.assertIn("candle 1m tidak lengkap", out)

    def test_a_failed_download_is_one_error_line_and_nothing_is_printed(self) -> None:
        failures = (
            (venue.ExchangeError("exchange code 80012: bad request"), "80012"),
            (venue.RateLimitedError("HTTP 429 from open-api.bingx.com"), "429"),
            (venue.TransientExchangeError("exchange code 100410: retry later"), "100410"),
            (httpx.ConnectTimeout(""), "ConnectTimeout"),  # httpx's timeouts carry no message: the class stands in
            (ValueError("the candle window of 130.0 days is longer than 100: narrow --since"), "narrow --since"),
        )
        for failure, shown in failures:
            with self.subTest(failure=type(failure).__name__):
                code, out, err, store, _ = self.run_production([trade_at(OPENED)], fetch=mock.Mock(side_effect=failure))
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(len(err.strip().splitlines()), 1, err)
                self.assertTrue(err.startswith("error:"), err)
                self.assertIn(shown, err)
                self.assertTrue(store.closed)


class SensitivityTextTests(unittest.TestCase):
    def verdict_for(self, sims: list, candidate: study.Candidate) -> study.Verdict:
        summary = study.summarize(sims, study.PRIMARY_VARIANT, study.DECISION_CONVENTION)
        return study.evaluate_rule(candidate, summary, study.calibration(sims).ratio)

    def test_a_candidate_that_meets_every_criterion_is_not_called_viable_on_production_candles(self) -> None:
        verdict = self.verdict_for(viable_sims(), study.ALL_POSITIONS)
        text = report.render_candidate(study.ALL_POSITIONS, verdict, production=True)
        self.assertTrue(verdict.passed)
        self.assertIn("-> memenuhi angka aturan pada candle produksi (sensitivitas, bukan vonis)", text)
        self.assertNotIn("layak jadi lengan live", text)
        self.assertIn("vonis terdaftar dibaca dari cache VST", text)
        self.assertIn("hasil aturan, bukan perintah untuk mengaktifkan apa pun", text)
        self.assertIn("[x] jumlah posisi: 70 (perlu >= 60)", text)

    def test_the_registered_text_of_a_passing_candidate_is_unchanged(self) -> None:
        verdict = self.verdict_for(viable_sims(), study.ALL_POSITIONS)
        text = report.render_candidate(study.ALL_POSITIONS, verdict)
        self.assertEqual(text, report.render_candidate(study.ALL_POSITIONS, verdict, production=False))
        self.assertIn("-> layak jadi lengan live", text)
        self.assertNotIn("sensitivitas", text)

    def test_a_candidate_that_falls_short_reads_the_same_either_way(self) -> None:
        verdict = self.verdict_for(viable_sims()[:30], study.ALL_POSITIONS)
        self.assertEqual(
            report.render_candidate(study.ALL_POSITIONS, verdict, production=True),
            report.render_candidate(study.ALL_POSITIONS, verdict),
        )


if __name__ == "__main__":
    unittest.main()
