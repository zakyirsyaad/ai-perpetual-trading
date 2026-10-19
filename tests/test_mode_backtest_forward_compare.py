"""Tests for the weekly ``compare-forward`` stage (``research.mode_backtest.forward_compare``).

The stage backtests mode L (i) and (ii) over the forward test's week and writes it beside the
forward bot's own summary. It runs here on synthetic days after the end of Phase 1's test period
(``uji.ujian_selesai``, 2026-10-14 WIB, read from the repo's ``config/modes.yaml``): Monday 19 to
Thursday 22 October 2026 are written, the window is Tuesday 20 to Friday 23 (exclusive), and the
Monday is read only to warm the 3m trigger. The prices are ``naik_price``, a NAIK market in which
mode L trades. No lock, start record or marker exists or may appear.
"""

from __future__ import annotations

import builtins
import contextlib
import csv
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bot.core import forward_summary as fs
from bot.strategy.modes.checks import LONG
from bot.strategy.modes.config import load_live_exit_config_with_sha256, load_modes_config
from bot.strategy.modes.regime import NAIK
from research.mode_backtest import cli, forward_compare
from research.mode_backtest.data import load_pair
from research.mode_backtest.frames import build_pair_frames
from research.mode_backtest.live_sim import LiveRunSpec, simulate_live_run
from research.mode_backtest.metrics import stage_metrics
from research.pine_gate.bars import wib_day_start_ms
from tests.mode_backtest_helpers import write_minute_warmups
from tests.mode_backtest_stages import CLOCK, SYMBOL, naik_price, write_naik_market

REPO = Path(__file__).resolve().parents[1]
REPO_CONFIG = REPO / "config" / "modes.yaml"
REPO_LIVE_CONFIG = REPO / "config" / "modes_live_exit.yaml"
WIB = timezone(timedelta(hours=7))

LOADED_DAYS = tuple(date(2026, 10, d) for d in (19, 20, 21, 22))  # Monday to Thursday
FIRST, END = date(2026, 10, 20), date(2026, 10, 23)  # the window [Tuesday, Friday)
STEM = "forward_compare_2026-10-20_2026-10-23"
RUN_KEYS = ("L_i_NAIK", "L_ii_NAIK")


def forward_rows():
    """Three forward positions: two closed inside the window, one closed before it."""
    def row(opened, closed, pnl, reason, entry=100.0, exit_=100.2, qty=180.0):
        return [opened.astimezone(timezone.utc).isoformat(),
                closed.astimezone(timezone.utc).isoformat(), "LONG", entry, exit_, qty, pnl,
                reason, (entry + exit_) * qty]
    return [
        row(datetime(2026, 10, 19, 9, 0, tzinfo=WIB), datetime(2026, 10, 19, 9, 40, tzinfo=WIB),
            50.0, "neutral_exit"),  # closed before the window: left out
        row(datetime(2026, 10, 20, 9, 0, tzinfo=WIB), datetime(2026, 10, 20, 9, 30, tzinfo=WIB),
            12.5, "neutral_exit"),
        row(datetime(2026, 10, 21, 14, 0, tzinfo=WIB), datetime(2026, 10, 21, 14, 50, tzinfo=WIB),
            -20.0, "strategy_exit:momentum faded", exit_=99.9),
    ]


class OpenedFiles:
    """Every path opened through ``open``, ``Path.open`` (``read_bytes`` and ``read_text`` go
    through it) or ``gzip.open`` while active."""

    def __init__(self):
        self.paths = []

    def __enter__(self):
        real_builtin, real_path_open, real_gzip = builtins.open, Path.open, gzip.open

        def opener(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)):
                self.paths.append(Path(file).resolve())
            return real_builtin(file, *args, **kwargs)

        def path_opener(path, *args, **kwargs):
            self.paths.append(Path(path).resolve())
            return real_path_open(path, *args, **kwargs)

        def gzip_opener(file, *args, **kwargs):
            if isinstance(file, (str, os.PathLike)):
                self.paths.append(Path(file).resolve())
            return real_gzip(file, *args, **kwargs)

        self._patches = [mock.patch.object(builtins, "open", opener),
                         mock.patch.object(Path, "open", path_opener),
                         mock.patch.object(gzip, "open", gzip_opener)]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc_info):
        for patch in reversed(self._patches):
            patch.stop()


class ForwardCompareCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "forward_data"
        self.out = self.root / "out"
        self.config = self.root / "modes.yaml"
        self.live = self.root / "modes_live_exit.yaml"
        shutil.copyfile(REPO_CONFIG, self.config)
        shutil.copyfile(REPO_LIVE_CONFIG, self.live)
        self.trades = self.root / "forward_trades_2026-10-20_2026-10-23.csv"
        with self.trades.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(fs.CSV_COLUMNS)
            writer.writerows(forward_rows())

    def write_market(self):
        write_naik_market(self.data, days=LOADED_DAYS)

    def run_stage(self, first=FIRST, end=END, pair=SYMBOL, data=None, now=CLOCK):
        return forward_compare.compare_forward_stage(
            self.config, self.live, self.data if data is None else data, self.trades, pair,
            first, end, self.out, now)

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
                for path in sorted(self.root.rglob("*")) if path.is_file()}

    def booby_trap(self):
        """Day files and warm-ups that raise if read, and a trade file that is no CSV."""
        for day in (date(2026, 10, 13), date(2026, 10, 14), *LOADED_DAYS):
            path = self.data / "prod" / SYMBOL / "1m" / f"{day.isoformat()}.csv.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not gzip")
        for interval in ("1h", "4h"):
            path = self.data / "prod" / SYMBOL / interval / "warmup.csv.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not gzip")
        self.trades.write_bytes(b"\xff\xfe not a csv")


class RefusalTests(ForwardCompareCase):
    def assert_refused_opening_nothing_but_the_config(self, **kwargs):
        self.booby_trap()
        before = self.snapshot()
        with OpenedFiles() as opened:
            with self.assertRaises(forward_compare.CompareRefusal):
                self.run_stage(**kwargs)
        self.assertLessEqual(set(opened.paths), {self.config.resolve()})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.out.exists())

    def test_refuses_any_day_before_the_end_of_the_test_period(self):
        # ujian_selesai is 2026-10-14 00:00 WIB: the day before first_day must not be earlier
        for first, end in ((date(2026, 10, 14), date(2026, 10, 21)),
                           (date(2026, 10, 1), date(2026, 10, 23)),
                           (date(2026, 7, 9), date(2026, 10, 23))):
            with self.subTest(first=first):
                self.assert_refused_opening_nothing_but_the_config(first=first, end=end)

    def test_the_day_after_the_test_period_may_warm_the_trigger(self):
        # first_day - 1 = 2026-10-14, the first day after the test: allowed; the run then fails
        # on the missing data, an expected failure and no refusal
        with self.assertRaises(FileNotFoundError) as raised:
            self.run_stage(first=date(2026, 10, 15), end=date(2026, 10, 16))
        self.assertNotIsInstance(raised.exception, forward_compare.CompareRefusal)
        self.assertIn("2026-10-14", str(raised.exception))

    def test_refuses_a_pair_other_than_btc(self):
        self.assert_refused_opening_nothing_but_the_config(pair="ETH-USDT")

    def test_refuses_an_empty_or_reversed_window(self):
        for first, end in ((END, END), (END, FIRST)):
            with self.subTest(first=first, end=end):
                self.assert_refused_opening_nothing_but_the_config(first=first, end=end)

    def test_refuses_the_phase_1_data_dir(self):
        (self.data / "holdout_runs").mkdir(parents=True)
        self.assert_refused_opening_nothing_but_the_config()

    def test_refuses_a_naive_clock(self):
        with self.assertRaises(ValueError):
            self.run_stage(now=datetime(2026, 10, 24, 10, 0))


class RunTests(ForwardCompareCase):
    def setUp(self):
        super().setUp()
        self.write_market()

    def expected_metrics(self, versi):
        """Mode L run directly over the window on the same frames: what the stage must report."""
        cfg, live = load_modes_config(self.config), load_live_exit_config_with_sha256(self.live)[0]
        frames = build_pair_frames(load_pair(self.data, SYMBOL, LOADED_DAYS[0], LOADED_DAYS[-1]),
                                   cfg)
        spec = LiveRunSpec(SYMBOL, LONG, NAIK, versi, wib_day_start_ms(FIRST),
                           wib_day_start_ms(END))
        result = simulate_live_run(frames, cfg, live, spec)
        return result, stage_metrics(result.trades, 3, 3, 3)

    def test_runs_both_versions_and_writes_the_side_by_side(self):
        path = self.run_stage()

        self.assertEqual(path, self.out / f"{STEM}.json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["stage"], "compare_forward")
        self.assertEqual((doc["mode"], doc["pair"]), ("L", SYMBOL))
        self.assertEqual(doc["window"]["first_day"], "2026-10-20")
        self.assertEqual(doc["window"]["end_day"], "2026-10-23")
        self.assertEqual(doc["window"]["start_ms"], wib_day_start_ms(FIRST))
        self.assertEqual(doc["window"]["end_ms"], wib_day_start_ms(END))
        self.assertEqual(doc["loaded_days"], {"first": "2026-10-19", "last": "2026-10-22"})
        self.assertEqual(doc["hari_kalender"], 3)
        self.assertEqual(doc["hari_naik"], 3)
        self.assertEqual(sorted(doc["runs"]), sorted(RUN_KEYS))
        for key, versi in zip(RUN_KEYS, ("i", "ii")):
            result, expected = self.expected_metrics(versi)
            run = doc["runs"][key]
            self.assertEqual(run["versi"], versi)
            self.assertEqual(run["metrics"]["posisi"], expected.posisi)
            self.assertGreater(expected.posisi, 0)
            self.assertEqual(run["metrics"]["hasil_per_1_juta"], expected.hasil_per_1_juta)
            self.assertEqual(run["metrics"]["hasil_tanpa_teratas"], expected.hasil_tanpa_teratas)
            self.assertEqual(run["metrics"]["entry_per_hari_kalender"],
                             expected.entry_per_hari_kalender)
            self.assertTrue(all(wib_day_start_ms(FIRST) <= t.entry_ms
                                and t.exit_ms <= wib_day_start_ms(END) for t in result.trades))
            # one definition on both sides: the share of positions with a booked pnl above zero
            wins = sum(1 for t in result.trades if t.pnl_usdt > 0)
            self.assertAlmostEqual(run["untung_pct"], 100.0 * wins / len(result.trades))
        forward = doc["forward"]
        self.assertEqual(forward["posisi"], 2)  # the one closed on Monday is outside the window
        self.assertEqual(forward["exits"], {"tp": 1, "sl": 0, "waktu": 0, "ai": 1, "lain": 0})
        volume = (100.0 + 100.2) * 180.0 + (100.0 + 99.9) * 180.0
        self.assertAlmostEqual(forward["hasil_per_1_juta"], 1_000_000 * -7.5 / volume)
        self.assertAlmostEqual(forward["entry_per_hari_kalender"], 2 / 3)
        self.assertEqual(forward["untung_pct"], 50.0)  # +12.5 and -20.0
        self.assertIsNone(forward["hasil_tanpa_teratas"])  # 2 positions, the top 3 dropped
        self.assertEqual(doc["forward_trades"]["rows"], 3)
        self.assertEqual(doc["forward_trades"]["rows_in_window"], 2)
        self.assertEqual(doc["files"], [f"{STEM}.json", f"{STEM}.md"])
        self.assertIn("status_line", doc)

        report = (self.out / f"{STEM}.md").read_text(encoding="utf-8")
        for text in ("Mode L (i)", "Mode L (ii)", "Uji maju", "Hasil per 1 juta",
                     "Tanpa 3 teratas", "untung (pnl > 0)", "2026-10-20", "2026-10-23",
                     "hari kalender"):
            self.assertIn(text, report)
        header = next(line for line in report.splitlines() if line.startswith("| Baris"))
        self.assertNotIn("menang", header.lower())  # TP share is a different measure: not in the table

    def test_the_report_states_the_edge_asymmetry(self):
        self.run_stage()

        report = (self.out / f"{STEM}.md").read_text(encoding="utf-8")
        self.assertIn("60 menit terakhir", report)  # mode L takes no entry in the window's last 60 minutes
        self.assertIn("closed_at", report)  # the forward side counts a position by when it closed
        self.assertNotIn("PERINGATAN", report)

    def test_a_short_forward_row_in_the_window_is_flagged_in_the_report_and_the_result(self):
        rows = forward_rows()
        rows[1][2] = "SHORT"  # the first row closed inside the window
        with self.trades.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(fs.CSV_COLUMNS)
            writer.writerows(rows)

        path = self.run_stage()

        report = (self.out / f"{STEM}.md").read_text(encoding="utf-8")
        self.assertIn("PERINGATAN: 1 posisi bukan LONG", report)
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["forward"]["non_long"], 1)
        self.assertIn("PERINGATAN: 1 posisi bukan LONG", doc["status_line"])

    def test_never_writes_a_marker_or_lock(self):
        self.run_stage()

        for folder in (self.data, self.out):
            self.assertFalse((folder / "holdout_runs").exists())
            self.assertEqual(list(folder.rglob("*_lock.json")), [])
            self.assertEqual(list(folder.rglob("*_started.json")), [])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         [f"{STEM}.json", f"{STEM}.md"])

    def test_outputs_never_overwritten(self):
        first = self.run_stage()
        before = self.snapshot()

        with self.assertRaises(FileExistsError):
            self.run_stage(now=CLOCK + timedelta(days=1))
        self.assertEqual(self.snapshot(), before)

        first.unlink()  # the report alone also refuses, before any work
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            self.run_stage()
        self.assertEqual(self.snapshot(), before)

    def test_a_missing_day_names_the_day(self):
        (self.data / "prod" / SYMBOL / "1m" / "2026-10-21.csv.gz").unlink()
        with self.assertRaises(FileNotFoundError) as raised:
            self.run_stage()
        self.assertIn("2026-10-21", str(raised.exception))
        self.assertFalse(self.out.exists())

    def test_a_gap_after_the_warm_up_names_the_warm_up(self):
        # warm-ups that end before Saturday 17 October: a hole up to Monday 19
        write_minute_warmups(self.data, SYMBOL, date(2026, 10, 17), 831, 401, naik_price)
        with self.assertRaises(forward_compare.StageError) as raised:
            self.run_stage()
        self.assertIn("warm-up", str(raised.exception))
        self.assertIn("2026-10-19", str(raised.exception))
        self.assertFalse(self.out.exists())


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, {cli.DEBUG_ENV: ""}), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err), mock.patch.object(cli, "now", return_value=CLOCK):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTests(ForwardCompareCase):
    def args(self, first="2026-10-20", end="2026-10-23", pair=SYMBOL):
        return ["compare-forward", "--pair", pair, "--config", str(self.config),
                "--live-config", str(self.live), "--data-dir", str(self.data),
                "--forward-trades", str(self.trades), "--from", first, "--to", end,
                "--out-dir", str(self.out)]

    def assert_one_error_line(self, err):
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertTrue(err.startswith("error: "), err)

    def test_cli_exit_codes(self):
        self.write_market()
        code, out, err = run_cli(self.args())
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[:2], [str(self.out / f"{STEM}.json"), str(self.out / f"{STEM}.md")])
        self.assertIn("mode L", lines[2])

        refusals = {
            "an existing output": (self.args(), 1),
            "a day before the end of the test": (self.args(first="2026-10-13"), 2),
            "another pair": (self.args(pair="ETH-USDT"), 2),
            "a reversed window": (self.args(first="2026-10-23", end="2026-10-20"), 2),
            "not a date": (self.args(first="20 Oct"), 2),
        }
        for name, (argv, expected) in refusals.items():
            with self.subTest(name):
                code, _out, err = run_cli(argv)
                self.assertEqual(code, expected, err)
                self.assertTrue(err.strip())
                if expected == 1 or name != "not a date":
                    self.assert_one_error_line(err)

        (self.data / "holdout_runs").mkdir()
        code, _out, err = run_cli(self.args(first="2026-10-21"))
        self.assertEqual(code, 2)
        self.assert_one_error_line(err)
        self.assertIn("holdout_runs", err)

    def test_missing_data_exits_1_with_one_error_line(self):
        code, _out, err = run_cli(self.args())
        self.assertEqual(code, 1)
        self.assert_one_error_line(err)
        self.assertIn("2026-10-19", err)

    def test_missing_option_exits_2(self):
        code, _out, _err = run_cli(["compare-forward", "--pair", SYMBOL])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
