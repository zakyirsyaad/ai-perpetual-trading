"""Tests for the train and one-shot test stages of mode L (``research.mode_backtest.stages_live``).

The stages run on the synthetic week of ``tests.mode_backtest_stages`` (training Monday to
Wednesday, the test Thursday and Friday) with ``config/modes.yaml`` moved onto that week and the
repo's ``config/modes_live_exit.yaml``. The week reads NAIK throughout, so mode L trades and
comparator S, which needs TURUN, has no day and no trade there; what S adds to the outputs (its
runs, its own day count, no pass status) is checked with fabricated runs. ``FileReads`` records
every file a stage opens, so "never reads" is checked on the reads themselves.
"""

import csv
import dataclasses
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from bot.strategy.modes.config import load_modes_config
from bot.strategy.modes.position_rules import SHORT
from bot.strategy.modes.regime import NAIK, TURUN
from research.mode_backtest import lock, stages, stages_live
from research.mode_backtest.data import load_pair, manifest, manifest_sha256, stage_files
from research.mode_backtest.frames import build_pair_frames
from research.mode_backtest.metrics import StageMetrics
from research.mode_backtest.report import LiveRow, live_status_line, live_table
from research.mode_backtest.simulate import BLOCK_REASONS, EXIT_TP, MODE_A, RunResult
from research.mode_backtest.tradelog import LIVE_COLUMNS
from research.pine_gate.bars import UnreadableDayError, wib_day_start_ms
from tests.mode_backtest_helpers import MINUTE_MS
from tests.mode_backtest_stages import (
    CLOCK,
    STAMP,
    SYMBOL,
    TEST_DAYS,
    TRAIN_DAYS,
    FileReads,
    write_naik_market,
    write_stage_config,
)
from tests.mode_backtest_trades import make_trade

REPO_LIVE_CONFIG = Path(__file__).resolve().parents[1] / "config" / "modes_live_exit.yaml"
LIVE_RUNS = ["L_i_NAIK", "L_ii_NAIK", "S_i_TURUN", "S_ii_TURUN"]
TEST_START_MS = wib_day_start_ms(TEST_DAYS[0])


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_log(path):
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class LiveStageCase(unittest.TestCase):
    """A temporary root with the synthetic week under ``data``, both configs and ``out``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.out = self.root / "out"
        self.config = write_stage_config(self.root / "modes.yaml")
        self.live = self.root / "modes_live_exit.yaml"
        shutil.copyfile(REPO_LIVE_CONFIG, self.live)
        write_naik_market(self.data)

    def train(self, now=CLOCK, pair=SYMBOL):
        return stages_live.train_live_stage(self.config, self.live, self.data, self.out, pair,
                                            now)

    def run_test_stage(self, now=CLOCK, pair=SYMBOL):
        return stages_live.test_live_stage(self.config, self.live, self.data, self.out, pair,
                                           now)

    def run_mode_a_train(self):
        return stages.train_stage(self.config, self.data, self.out, MODE_A, SYMBOL, CLOCK)

    def run_mode_a_test(self):
        return stages.test_stage(self.config, self.data, self.out, MODE_A, SYMBOL, CLOCK)

    def lock_file(self, mode="L"):
        return lock.lock_path(self.out, mode, SYMBOL)

    def marker(self, mode="L"):
        return lock.marker_path(self.data, mode, SYMBOL, sha256_of(self.lock_file(mode)))

    def started_record(self, mode="L"):
        return self.out / f"mode_{mode}_BTC-USDT_test_started.json"

    def day_file(self, day):
        return self.data / "prod" / SYMBOL / "1m" / f"{day.isoformat()}.csv.gz"

    def snapshot(self):
        """Every file under the root and its bytes: what a refused run must leave as it was."""
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def edit_live(self, old, new):
        text = self.live.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, old)
        self.live.write_text(text.replace(old, new), encoding="utf-8")


class TrainLiveStageTests(LiveStageCase):
    def test_train_writes_the_lock_with_both_config_shas(self):
        test_days = {self.day_file(day) for day in TEST_DAYS}
        with FileReads() as reads:
            path = self.train()

        self.assertEqual(path, self.out / f"mode_L_BTC-USDT_train_{STAMP}.json")
        self.assertFalse(test_days & set(reads.paths()))  # training never opens a test day
        doc, locked = read_json(path), read_json(self.lock_file())
        training = stage_files(self.data, SYMBOL, TRAIN_DAYS[0], TRAIN_DAYS[-1])
        expected = {
            "config_sha256": sha256_of(self.config),
            "live_config_sha256": sha256_of(self.live),
            "code_sha256": lock.code_sha256(),
            "train_manifest_sha256": manifest_sha256(manifest(self.data, training)),
            "python": lock.python_version(),
        }
        for record in (doc, locked):
            self.assertEqual({key: record.get(key) for key in expected}, expected)
        self.assertEqual((locked["mode"], locked["pair"]), ("L", SYMBOL))
        self.assertEqual(locked["train_result"], path.name)
        self.assertEqual(sorted(locked["summary"]["runs"]), LIVE_RUNS)
        self.assertEqual(sorted(doc["runs"]), LIVE_RUNS)
        for key, run in doc["runs"].items():
            with self.subTest(run=key):
                rows = read_log(self.out / run["trade_log"])
                self.assertEqual(rows[0], list(LIVE_COLUMNS))
                self.assertEqual(len(rows) - 1, run["metrics"]["posisi"])
                self.assertEqual(locked["summary"]["runs"][key], run["metrics"])
                self.assertEqual(run["versi"], key.split("_")[1])
        self.assertGreater(doc["runs"]["L_i_NAIK"]["metrics"]["posisi"], 0)
        self.assertEqual(doc["runs"]["S_i_TURUN"]["metrics"]["posisi"], 0)  # a NAIK-only week
        self.assertEqual(doc["files"][-1], self.lock_file().name)
        self.assertTrue(doc["status_line"].startswith("Latihan mode L BTC-USDT"), doc)
        self.assertFalse(self.lock_file("A").exists())

    def test_an_inactive_mode_l_or_another_pair_is_refused_before_anything_is_written(self):
        self.edit_live("aktif: true", "aktif: false")
        with self.assertRaises(stages_live.StageError) as caught:
            self.train()
        self.assertIn("aktif", str(caught.exception))
        self.assertFalse(self.out.exists())

        shutil.copyfile(REPO_LIVE_CONFIG, self.live)
        with self.assertRaises(stages_live.StageError):
            self.train(pair="ETH-USDT")
        self.assertFalse(self.out.exists())

    def test_a_live_trigger_other_than_mode_d_is_refused_before_anything_is_written(self):
        self.edit_live("candle_jendela: 39", "candle_jendela: 40")

        with self.assertRaises(ValueError) as caught:
            self.train()

        self.assertIn("candle_jendela", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_a_naive_clock_is_refused(self):
        with self.assertRaises(ValueError):
            self.train(now=CLOCK.replace(tzinfo=None))
        self.assertFalse(self.out.exists())


class MeasuredDaysTests(LiveStageCase):
    def frames(self):
        cfg = load_modes_config(self.config)
        pair = load_pair(self.data, SYMBOL, TRAIN_DAYS[0], TRAIN_DAYS[-1])
        return build_pair_frames(pair, cfg), cfg

    def test_condition_days_start_when_the_first_stretch_is_known(self):
        frames, cfg = self.frames()
        period = stages.training_period(cfg.uji)
        # Training starts with no 1m bar before it: the first stretch is known at 01:57 WIB on
        # Monday, before the weekly window opens at 07:00, so every training day counts.
        self.assertEqual(stages_live.measured_days(frames, cfg, period, NAIK), list(TRAIN_DAYS))
        self.assertEqual(stages_live.measured_days(frames, cfg, period, TURUN), [])

        # A stretch first known at Wednesday 10:00 WIB: the hour closing at 10:00 is the
        # condition of the 3m closes in [10:00, 11:00), so Wednesday is the only day.
        known_ms = wib_day_start_ms(TRAIN_DAYS[2]) + 10 * 60 * MINUTE_MS
        bars = frames.stretch_bars
        late = tuple(None if bars.open_ms[i] + bars.interval_ms < known_ms else value
                     for i, value in enumerate(frames.stretch))
        self.assertIn(None, late)
        later = dataclasses.replace(frames, stretch=late)
        self.assertEqual(stages_live.measured_days(later, cfg, period, NAIK), [TRAIN_DAYS[2]])

        never = dataclasses.replace(frames, stretch=tuple(None for _ in frames.stretch))
        self.assertEqual(stages_live.measured_days(never, cfg, period, NAIK), [])


class TestLiveStageTests(LiveStageCase):
    def test_test_refuses_an_edited_live_config_with_exit_2(self):
        # A LockError is what the command line turns into exit code 2.
        self.train()
        original = self.live.read_bytes()
        edits = (
            ("a comment", lambda: self.live.write_bytes(original + b"# one more comment\n")),
            ("a number", lambda: self.edit_live("tp_net_usdt: 56", "tp_net_usdt: 57")),
        )
        for name, edit in edits:
            with self.subTest(edit=name):
                edit()
                before = self.snapshot()
                with self.assertRaises(lock.LockError) as caught:
                    self.run_test_stage()
                self.assertTrue(any(p.startswith("live_config_sha256")
                                    for p in caught.exception.problems), caught.exception.problems)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.marker().exists())
                self.assertFalse(self.started_record().exists())
                self.live.write_bytes(original)

        self.run_test_stage()  # the unedited file still passes

    def test_test_refuses_an_edited_modes_config_or_a_hand_edited_lock(self):
        self.train()
        before = self.snapshot()
        original = self.config.read_bytes()
        self.config.write_bytes(original + b"# one more comment\n")
        with self.assertRaises(lock.LockError) as caught:
            self.run_test_stage()
        self.assertTrue(any(p.startswith("config_sha256") for p in caught.exception.problems))
        self.config.write_bytes(original)
        self.assertEqual(self.snapshot(), before)

        raw = self.lock_file().read_bytes()
        doc = json.loads(raw)
        runs = doc["summary"]["runs"]
        edits = (
            ("mode", {"mode": "A"}),
            ("pair", {"pair": "ETH-USDT"}),
            ("ujian", {"ujian": {**doc["ujian"], "end": "2026-07-12T00:00:00+07:00"}}),
            ("summary.runs.S_ii_TURUN", {"summary": {**doc["summary"], "runs": {
                k: v for k, v in runs.items() if k != "S_ii_TURUN"}}}),
        )
        for named, change in edits:
            with self.subTest(edit=named):
                self.lock_file().write_text(json.dumps({**doc, **change}), encoding="utf-8")
                before = self.snapshot()
                with self.assertRaises(lock.LockError) as caught:
                    self.run_test_stage()
                self.assertIn(named, str(caught.exception))
                self.assertEqual(self.snapshot(), before)
                self.lock_file().write_bytes(raw)

    def test_test_claims_start_record_and_marker_before_reading_a_test_day(self):
        self.train()
        marker, started = self.marker(), self.started_record()
        lock_sha = sha256_of(self.lock_file())
        test_days = {self.day_file(day) for day in TEST_DAYS}

        with FileReads(probe=lambda: marker.exists() and started.exists()) as reads:
            path = self.run_test_stage()

        test_reads = [claimed for p, claimed in reads.events if p in test_days]
        self.assertTrue(test_reads)
        self.assertTrue(all(test_reads))
        training = [claimed for p, claimed in reads.events if p == self.day_file(TRAIN_DAYS[0])]
        self.assertIn(False, training)  # hashed against the lock before the claims
        record = read_json(started)
        self.assertEqual(record["lock_sha256"], lock_sha)
        self.assertEqual(record["live_config_sha256"], sha256_of(self.live))
        self.assertEqual(record["data_dir"], str(self.data.resolve()))
        self.assertEqual(record["created_at"], CLOCK.isoformat())
        self.assertEqual(read_json(marker)["lock_sha256"], lock_sha)
        self.assertEqual(marker.name, f"mode_L_BTC-USDT_{lock_sha}.json")
        doc = read_json(path)
        self.assertEqual(doc["marker"], str(marker))
        self.assertIn(started.name, doc["files"])

    def test_a_missing_test_day_is_refused_before_the_claims(self):
        self.train()
        self.day_file(TEST_DAYS[1]).unlink()
        before = self.snapshot()

        with self.assertRaises(FileNotFoundError) as caught:
            self.run_test_stage()

        self.assertIn(f"{TEST_DAYS[1].isoformat()}.csv.gz", str(caught.exception))
        self.assertFalse(self.started_record().exists())
        self.assertEqual(self.snapshot(), before)

    def test_test_runs_once(self):
        self.train()
        fresh = self.root / "fresh_copy"
        shutil.copytree(self.data, fresh)  # copied before the test, so it holds no marker
        self.run_test_stage()
        marker = self.marker()
        before = self.snapshot()

        for now in (CLOCK, CLOCK + timedelta(days=3)):
            with self.subTest(now=now), self.assertRaises(lock.LockError) as caught:
                self.run_test_stage(now=now)
            self.assertIn("already ran", str(caught.exception))
            self.assertEqual(self.snapshot(), before)

        # Only the start record left: a fresh data copy, or the marker gone, buys no second test.
        marker.unlink()
        for data_dir in (fresh, self.data):
            with self.subTest(only_the_start_record=data_dir.name):
                self.data = data_dir
                with self.assertRaises(lock.LockError) as caught:
                    self.run_test_stage(now=CLOCK + timedelta(days=1))
                self.assertIn("already ran", str(caught.exception))
                self.assertFalse((fresh / "holdout_runs").exists())

        # Only the marker left: the start record gone, the marker still refuses.
        self.data = self.root / "data"
        self.started_record().rename(self.root / "started.aside")
        marker.write_bytes(b"{}")
        with self.assertRaises(lock.LockError) as caught:
            self.run_test_stage(now=CLOCK + timedelta(days=1))
        self.assertIn("already ran", str(caught.exception))

        # And no new lock once the test ran.
        self.lock_file().unlink()
        with self.assertRaises(lock.LockError):
            self.train(now=CLOCK + timedelta(days=1))
        self.assertFalse(self.lock_file().exists())

    def test_a_test_that_failed_after_its_claims_has_used_the_shot(self):
        self.train()
        # A regular file passes every check before the claims and fails only when it is read.
        self.day_file(TEST_DAYS[1]).write_bytes(b"not a gzip file")

        with self.assertRaises(UnreadableDayError):
            self.run_test_stage()

        self.assertTrue(self.marker().exists())
        self.assertTrue(self.started_record().exists())
        write_naik_market(self.root / "fresh")
        self.data = self.root / "fresh"
        with self.assertRaises(lock.LockError) as caught:
            self.run_test_stage()
        self.assertIn("already ran", str(caught.exception))

    def test_outputs_are_never_overwritten(self):
        self.train()
        before = self.snapshot()

        with self.assertRaises(FileExistsError) as caught:
            self.train()
        self.assertIn("already exist", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

        for name in ("mode_L_BTC-USDT_test_20261001.json",
                     f"mode_L_BTC-USDT_test_{STAMP}_L_ii_NAIK.csv",
                     f"mode_L_BTC-USDT_test_{STAMP}_S_i_TURUN.csv",
                     f"mode_L_BTC-USDT_test_{STAMP}.md"):
            with self.subTest(existing=name):
                existing = self.out / name
                existing.write_text("written before\n", encoding="utf-8")
                with self.assertRaises(FileExistsError) as caught:
                    self.run_test_stage()
                self.assertIn(name, str(caught.exception))
                self.assertEqual(existing.read_text(encoding="utf-8"), "written before\n")
                self.assertFalse(self.marker().exists())
                self.assertFalse(self.started_record().exists())
                existing.unlink()
        self.assertEqual(self.snapshot(), before)

    def test_mode_a_records_do_not_block_mode_l_and_vice_versa(self):
        for first, second in (("A", "L"), ("L", "A")):
            with self.subTest(first=first):
                self.setUp()  # a fresh root: both modes share its data and out folders
                stage = {"A": (self.run_mode_a_train, self.run_mode_a_test),
                         "L": (self.train, self.run_test_stage)}
                for mode in (first, second):
                    train, test = stage[mode]
                    train()
                    test()
                for mode in ("A", "L"):
                    self.assertTrue(self.marker(mode).exists(), mode)
                    self.assertTrue(self.started_record(mode).exists(), mode)
                with self.assertRaises(lock.LockError):
                    self.run_test_stage(now=CLOCK + timedelta(days=1))
                with self.assertRaises(lock.LockError):
                    stages.test_stage(self.config, self.data, self.out, MODE_A, SYMBOL,
                                      CLOCK + timedelta(days=1))

    def test_result_reports_calendar_and_condition_days(self):
        train_doc = read_json(self.train())
        path = self.run_test_stage()
        doc = read_json(path)

        self.assertEqual(path, self.out / f"mode_L_BTC-USDT_test_{STAMP}.json")
        expected_days = (
            (train_doc, 3, [d.isoformat() for d in TRAIN_DAYS]),
            (doc, 2, [d.isoformat() for d in TEST_DAYS]),
        )
        for record, calendar, naik in expected_days:
            with self.subTest(stage=record["stage"]):
                self.assertEqual(record["hari_kalender"], calendar)
                self.assertEqual((record["naik_days"], record["hari_naik"]), (naik, len(naik)))
                self.assertEqual((record["turun_days"], record["hari_turun"]), ([], 0))
                for key, run in record["runs"].items():
                    m = run["metrics"]
                    days = len(naik) if key.startswith("L") else 0
                    self.assertEqual(m["hari_naik"], days, key)
                    rate = m["posisi"] / calendar
                    self.assertEqual(m["entry_per_hari_kalender"], rate, key)
                no_trading = record["tidak_trading"]
                self.assertEqual((no_trading["posisi"], no_trading["hasil_per_1_juta"]), (0, 0.0))
                self.assertEqual(no_trading["entry_per_hari_kalender"], 0.0)

        uji = load_modes_config(self.config).uji
        self.assertEqual(sorted(doc["status"]), ["i", "ii"])
        lines = [live_status_line(versi, StageMetrics(**doc["runs"][f"L_{versi}_NAIK"]["metrics"]),
                                  uji, SYMBOL) for versi in ("i", "ii")]
        self.assertEqual(doc["status_line"], "\n".join(lines))
        for key, run in doc["runs"].items():
            rows = read_log(self.out / run["trade_log"])
            self.assertEqual(len(rows) - 1, run["metrics"]["posisi"], key)
        loaded = stage_files(self.data, SYMBOL, TRAIN_DAYS[0], TEST_DAYS[-1])
        test_days = [self.day_file(d) for d in TEST_DAYS]
        self.assertEqual(doc["loaded_manifest_sha256"],
                         manifest_sha256(manifest(self.data, loaded)))
        self.assertEqual(doc["test_days_manifest_sha256"],
                         manifest_sha256(manifest(self.data, test_days)))
        self.assertEqual((doc["loaded_files"], doc["test_day_files"]), (7, 2))

        report = path.with_suffix(".md").read_text(encoding="utf-8")
        summary = read_json(self.lock_file())["summary"]["runs"]
        rows = []
        for tahap, runs in (("Latihan", summary), ("Ujian", {k: r["metrics"]
                                                             for k, r in doc["runs"].items()})):
            for key in LIVE_RUNS:
                mode, versi, _kondisi = key.split("_")
                rows.append(LiveRow(mode, versi, SYMBOL, tahap, StageMetrics(**runs[key])))
        for row in rows:
            self.assertIn(live_table([row]).splitlines()[2], report)
        self.assertEqual(report.count("| Tidak trading |"), 2)
        for line in lines:
            self.assertIn(line, report)
        for key in ("loaded_manifest_sha256", "test_days_manifest_sha256", "lock_sha256"):
            self.assertIn(f"`{doc[key]}`", report)


def blocked_none():
    return MappingProxyType({reason: 0 for reason in BLOCK_REASONS})


class LiveVerdictTests(LiveStageCase):
    def test_the_pass_status_is_given_for_l_only_and_s_is_shown_as_a_comparator(self):
        # 40 SHORT winners for S in the test: S would pass, yet it has no status; L has 5 trades.
        def fake(frames, cfg, live, run):
            if run.start_ms != TEST_START_MS:
                return RunResult((), blocked_none())
            if run.side == SHORT:
                trades = tuple(make_trade(entry_ms=TEST_START_MS + k * 30 * MINUTE_MS, mode="S",
                                          side=SHORT, kondisi=TURUN, versi=run.versi,
                                          exit_reason=EXIT_TP, pnl_usdt=20.0)
                               for k in range(40))
            else:
                trades = tuple(make_trade(entry_ms=TEST_START_MS + k * 30 * MINUTE_MS, mode="L",
                                          versi=run.versi, pnl_usdt=-5.0) for k in range(5))
            return RunResult(trades, blocked_none())

        with mock.patch.object(stages_live, "simulate_live_run", side_effect=fake):
            self.train()
            doc = read_json(self.run_test_stage())

        self.assertEqual(doc["status"], {"i": "BELUM CUKUP SAMPEL", "ii": "BELUM CUKUP SAMPEL"})
        self.assertEqual(doc["runs"]["S_i_TURUN"]["metrics"]["posisi"], 40)
        self.assertEqual(doc["runs"]["S_i_TURUN"]["metrics"]["hari_naik"], 0)
        self.assertEqual(doc["runs"]["S_i_TURUN"]["metrics"]["entry_per_hari_kalender"], 20.0)
        self.assertNotIn("mode S", doc["status_line"])
        self.assertNotIn("LOLOS", doc["status_line"])


if __name__ == "__main__":
    unittest.main()
