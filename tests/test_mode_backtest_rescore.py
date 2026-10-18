"""Tests for the rescore stage: Mode A's and the Phase 1 comparator's trade logs repriced with the
costs of the live-exit spec (section 5), as a cost sensitivity and never a pass or fail.

Logs are built with the trade-log writer from ``make_trade`` in temporary folders; no bar file and
nothing under ``~/replay/mode_backtest`` is touched.
"""

import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from bot.strategy.modes.config import CostConfig
from bot.strategy.modes.position_rules import LONG, SHORT, adverse_fill
from research.mode_backtest import cli, rescore
from research.mode_backtest.live_sim import settle_live
from research.mode_backtest.simulate import EXIT_POTONG, EXIT_SL, EXIT_TP, EXIT_WAKTU, ExitOutcome
from research.mode_backtest.tradelog import COLUMNS, trade_log_text
from tests.mode_backtest_helpers import MINUTE_MS
from tests.mode_backtest_trades import FIRST_ENTRY_MS, make_trade

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "modes.yaml"
LIVE_CONFIG = REPO / "config" / "modes_live_exit.yaml"
CLOCK = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
OLD_PCT = 0.01
FEE_PCT = 0.035
COSTS = CostConfig(slippage_entry_bps=0.3, slippage_exit_market_bps=0.5, slippage_tp_bps=0.0)
NOTIONAL = 25_000.0


def logged(side, entry_open, raw_exit, reason, hold_min=30, entry_ms=FIRST_ENTRY_MS, **fields):
    """A Mode A trade priced the Phase 1 way: both fills ``OLD_PCT`` against the position."""
    entry_fill = adverse_fill(entry_open, side, OLD_PCT, False)
    exit_fill = adverse_fill(raw_exit, side, OLD_PCT, True)
    qty = NOTIONAL / entry_fill
    volume = qty * entry_fill + qty * exit_fill
    fee = FEE_PCT / 100 * volume
    pnl = side * qty * (exit_fill - entry_fill) - fee
    return make_trade(
        entry_ms=entry_ms, hold_min=hold_min, side=side, entry_fill=entry_fill,
        exit_fill=exit_fill, exit_reason=reason, qty=qty, fee_usdt=fee, volume_usdt=volume,
        pnl_usdt=pnl, slippage_usdt=qty * abs(entry_fill - entry_open) + qty * abs(raw_exit - exit_fill),
        **fields,
    )


def rows_of(trades):
    return list(csv.DictReader(io.StringIO(trade_log_text(trades))))


def rescored(trades):
    return rescore.rescore_rows(rows_of(trades), COSTS, FEE_PCT, OLD_PCT)


class RescoreRowsTests(unittest.TestCase):
    def test_rescore_recovers_the_raw_exit_of_a_gapped_stop(self):
        # the stop rested at 99.76 but the bar opened at 99.50: the fill is the open, not the level
        trade = logged(LONG, 100.0, 99.50, EXIT_SL, sl=99.76, gapped=True)

        (result,) = rescored([trade])

        self.assertAlmostEqual(result.raw_exit, 99.50, places=9)
        self.assertNotAlmostEqual(result.raw_exit, 99.76, places=2)
        self.assertAlmostEqual(result.new.exit_fill, 99.50 * (1 - 0.5 / 10_000), places=9)
        self.assertAlmostEqual(result.raw_entry_open, 100.0, places=9)
        self.assertAlmostEqual(result.new.entry_fill, 100.0 * (1 + 0.3 / 10_000), places=9)

    def test_rescore_of_a_short_gapped_stop_recovers_the_open(self):
        trade = logged(SHORT, 100.0, 100.50, EXIT_SL, sl=100.24, gapped=True)

        (result,) = rescored([trade])

        self.assertAlmostEqual(result.raw_exit, 100.50, places=9)
        self.assertAlmostEqual(result.new.exit_fill, 100.50 * (1 + 0.5 / 10_000), places=9)
        self.assertAlmostEqual(result.new.entry_fill, 100.0 * (1 - 0.3 / 10_000), places=9)

    def test_rescore_of_a_tp_uses_the_level_without_slippage(self):
        trade = logged(LONG, 100.0, 100.40, EXIT_TP, tp=100.40)

        (result,) = rescored([trade])

        self.assertAlmostEqual(result.new.exit_fill, 100.40, places=9)
        # the old log had slipped the level; the new fill is exactly the level
        self.assertLess(trade.exit_fill, 100.40)

    def test_rescore_keeps_each_exit_event(self):
        trades = [
            logged(LONG, 100.0, 100.40, EXIT_TP, hold_min=12, entry_ms=FIRST_ENTRY_MS),
            logged(LONG, 101.0, 100.60, EXIT_SL, hold_min=5, entry_ms=FIRST_ENTRY_MS + 100 * MINUTE_MS),
            logged(SHORT, 102.0, 101.90, EXIT_WAKTU, hold_min=60, entry_ms=FIRST_ENTRY_MS + 200 * MINUTE_MS),
            logged(LONG, 103.0, 102.80, EXIT_POTONG, hold_min=15, entry_ms=FIRST_ENTRY_MS + 300 * MINUTE_MS),
        ]

        results = rescored(trades)

        self.assertEqual(len(results), 4)
        for old, result in zip(trades, results):
            self.assertEqual(result.new.exit_reason, old.exit_reason)
            self.assertEqual(result.new.entry_ms, old.entry_ms)
            self.assertEqual(result.new.exit_ms, old.exit_ms)
            self.assertEqual(result.new.side, old.side)
            self.assertAlmostEqual(result.old.pnl_usdt, old.pnl_usdt, places=9)
            self.assertAlmostEqual(result.old.volume_usdt, old.volume_usdt, places=9)

    def test_rescore_matches_settle_live_on_a_round_trip(self):
        for side, entry_open, raw_exit, reason in (
            (LONG, 100.0, 100.40, EXIT_TP),
            (LONG, 100.0, 99.60, EXIT_SL),
            (SHORT, 100.0, 99.60, EXIT_TP),
            (SHORT, 100.0, 100.30, EXIT_POTONG),
            (LONG, 100.0, 100.05, EXIT_WAKTU),
        ):
            with self.subTest(side=side, reason=reason):
                trade = logged(side, entry_open, raw_exit, reason)

                (result,) = rescored([trade])

                money = settle_live(
                    entry_open, ExitOutcome(reason, 0, raw_exit, False, False), side,
                    NOTIONAL, FEE_PCT, COSTS,
                )
                self.assertAlmostEqual(result.new.entry_fill, money.entry_fill, places=9)
                self.assertAlmostEqual(result.new.exit_fill, money.exit_fill, places=9)
                self.assertAlmostEqual(result.new.qty, money.qty, places=6)
                self.assertAlmostEqual(result.new.fee_usdt, money.fee_usdt, places=6)
                self.assertAlmostEqual(result.new.slippage_usdt, money.slippage_usdt, places=6)
                self.assertAlmostEqual(result.new.pnl_usdt, money.pnl_usdt, places=6)
                self.assertAlmostEqual(result.new.volume_usdt, money.volume_usdt, places=6)

    def test_a_row_the_rescore_cannot_price_is_refused(self):
        good = rows_of([logged(LONG, 100.0, 100.4, EXIT_TP)])[0]
        for field, value in (
            ("alasan_exit", "BUKAN"),
            ("sisi", "FLAT"),
            ("harga_entry", "nan"),
            ("harga_exit", "abc"),
            ("notional_usdt", "0"),
            ("waktu_exit", "kemarin"),
        ):
            with self.subTest(field=field):
                row = dict(good, **{field: value})
                with self.assertRaisesRegex(ValueError, "trade 1"):
                    rescore.rescore_rows([row], COSTS, FEE_PCT, OLD_PCT)

    def test_a_row_missing_a_column_is_refused(self):
        row = rows_of([logged(LONG, 100.0, 100.4, EXIT_TP)])[0]
        del row["harga_exit"]

        with self.assertRaisesRegex(ValueError, "harga_exit"):
            rescore.rescore_rows([row], COSTS, FEE_PCT, OLD_PCT)


class RescoreStageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "out"
        self.log = self.root / "trades_a.csv"
        trades = [
            logged(LONG, 100.0, 100.40, EXIT_TP, entry_ms=FIRST_ENTRY_MS + i * 100 * MINUTE_MS)
            for i in range(5)
        ] + [logged(LONG, 100.0, 99.50, EXIT_SL, sl=99.76, gapped=True,
                    entry_ms=FIRST_ENTRY_MS + 700 * MINUTE_MS)]
        self.trades = trades
        self.log.write_text(trade_log_text(trades), encoding="utf-8")

    def run_stage(self, logs=None, now=CLOCK):
        return rescore.rescore_stage(logs or [self.log], LIVE_CONFIG, CONFIG, self.out, now)

    def test_rescore_stage_writes_json_and_report_labelled_a_sensitivity(self):
        path = self.run_stage()

        self.assertEqual(path.name, "rescore_20261018.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        md = path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertEqual(doc["label"], "sensitivitas biaya, bukan uji")
        self.assertIn("sensitivitas biaya, bukan uji", md.lower())
        (entry,) = doc["logs"]
        self.assertEqual(entry["old"]["posisi"], 6)
        self.assertEqual(entry["new"]["posisi"], 6)
        self.assertEqual(entry["new"]["tp"], 5)
        self.assertEqual(entry["new"]["sl"], 1)
        self.assertIn("hasil_per_1_juta", entry["new"])
        self.assertIn("hasil_tanpa_teratas", entry["new"])
        self.assertEqual(doc["buang_posisi_teratas"], 3)
        self.assertEqual(len(entry["sha256"]), 64)

    def test_rescore_stage_old_result_is_the_logs_own(self):
        doc = json.loads(self.run_stage().read_text(encoding="utf-8"))
        pnl = sum(t.pnl_usdt for t in self.trades)
        volume = sum(t.volume_usdt for t in self.trades)

        self.assertAlmostEqual(doc["logs"][0]["old"]["hasil_per_1_juta"], 1e6 * pnl / volume, places=4)

    def test_rescore_stage_without_the_top_3_drops_the_best_three(self):
        doc = json.loads(self.run_stage().read_text(encoding="utf-8"))
        new = sorted((r.new for r in rescore.rescore_rows(
            rows_of(self.trades), COSTS, FEE_PCT, OLD_PCT)), key=lambda t: (-t.pnl_usdt, t.entry_ms))
        rest = new[3:]

        expected = 1e6 * sum(t.pnl_usdt for t in rest) / sum(t.volume_usdt for t in rest)
        self.assertAlmostEqual(doc["logs"][0]["new"]["hasil_tanpa_teratas"], expected, places=4)

    def test_rescore_never_overwrites(self):
        first = self.run_stage()
        before = first.read_text(encoding="utf-8")

        with self.assertRaises(FileExistsError):
            self.run_stage()

        self.assertEqual(first.read_text(encoding="utf-8"), before)

    def test_rescore_leaves_no_partial_output_when_a_log_is_bad(self):
        bad = self.root / "bad.csv"
        bad.write_text(trade_log_text(self.trades).replace("TP", "XX"), encoding="utf-8")

        with self.assertRaises(ValueError):
            self.run_stage([self.log, bad])

        self.assertFalse(self.out.exists() and any(self.out.iterdir()))

    def test_a_log_with_other_columns_is_refused(self):
        live = self.root / "live.csv"
        live.write_text(trade_log_text(self.trades, live=True), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "columns"):
            self.run_stage([live])

    def test_a_naive_clock_is_refused(self):
        with self.assertRaises(ValueError):
            self.run_stage(now=datetime(2026, 10, 18, 12, 0))

    def test_the_header_is_the_phase_1_columns(self):
        self.assertEqual(list(csv.reader(io.StringIO(trade_log_text(self.trades))))[0], list(COLUMNS))


class RescoreCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.out = self.root / "out"
        self.log = self.root / "a.csv"
        self.log.write_text(trade_log_text([logged(LONG, 100.0, 100.4, EXIT_TP)]), encoding="utf-8")

    def argv(self, *logs):
        args = ["rescore"]
        for log in logs or (self.log,):
            args += ["--trade-log", str(log)]
        return args + ["--config", str(CONFIG), "--live-config", str(LIVE_CONFIG),
                       "--out-dir", str(self.out)]

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {cli.DEBUG_ENV: ""}), \
                mock.patch.object(cli, "now", return_value=CLOCK), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_rescore_exits_0_and_prints_both_files(self):
        code, out, err = self.run_cli(self.argv())

        self.assertEqual((code, err), (0, ""))
        self.assertIn("rescore_20261018.json", out)
        self.assertIn("rescore_20261018.md", out)
        self.assertTrue((self.out / "rescore_20261018.json").is_file())

    def test_rescore_twice_exits_1_with_one_error_line(self):
        self.run_cli(self.argv())

        code, _out, err = self.run_cli(self.argv())

        self.assertEqual(code, 1)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertTrue(err.startswith("error: "))

    def test_a_missing_log_exits_1(self):
        code, _out, err = self.run_cli(self.argv(self.root / "nope.csv"))

        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "))

    def test_no_trade_log_is_a_usage_error(self):
        code, _out, _err = self.run_cli(
            ["rescore", "--config", str(CONFIG), "--live-config", str(LIVE_CONFIG),
             "--out-dir", str(self.out)])

        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
