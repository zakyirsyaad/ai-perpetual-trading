"""Tests for the lock and the one-shot train and test stages of the mode backtest.

The stages run on the synthetic week of ``tests.mode_backtest_stages`` (training Monday to
Wednesday, the test Thursday and Friday), where the hourly frame reads NAIK and both Mode A and the
comparator trade. A check that must end in an exit code runs through ``cli.main``; the rest call
``research.mode_backtest.stages`` directly. ``FileReads`` records every day file a stage opens, so
"never reads" is checked on the reads themselves and not only on a corrupt file.
"""

import ast
import contextlib
import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from bot.strategy.modes.config import load_modes_config
from research.mode_backtest import cli, lock, stages
from research.mode_backtest.data import manifest, manifest_sha256, stage_files
from research.mode_backtest.metrics import StageMetrics, pass_status, stage_metrics
from research.mode_backtest.report import ReportRow, mode_table
from research.mode_backtest.simulate import BLOCK_REASONS, MODE_A, RunResult
from research.mode_backtest.tradelog import COLUMNS
from research.pine_gate.bars import wib_day_start_ms
from tests.mode_backtest_helpers import MINUTE_MS, write_days, write_warmups
from tests.mode_backtest_stages import (
    CLOCK,
    STAMP,
    SYMBOL,
    TEST_DAYS,
    TRAIN_DAYS,
    WARM_BARS_4H,
    WARM_HOURS_1H,
    stage_config_text,
    write_naik_market,
    write_stage_config,
)
from tests.mode_backtest_trades import make_trade

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "research" / "mode_backtest"
TRAIN_START_MS = wib_day_start_ms(TRAIN_DAYS[0])
TEST_START_MS = wib_day_start_ms(TEST_DAYS[0])
TEST_END_MS = wib_day_start_ms(TEST_DAYS[-1]) + 1_440 * MINUTE_MS
TRAIN_RUNS = ["A_15m_NAIK", "D_3m_NAIK"]
SWITCHED_RUNS = ["A_15m_NAIK", "A_5m_NAIK", "D_3m_NAIK"]
SHA = "0123456789abcdef" * 4


def wib(text):
    return datetime.fromisoformat(text)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_log(path):
    with Path(path).open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def flip_gzip_mtime(path):
    """Change one byte of a gzip file in place: the modification time in its header, which no
    reader checks. The bytes differ and the bars inside do not."""
    raw = bytearray(path.read_bytes())
    raw[4] ^= 0x01
    path.write_bytes(bytes(raw))


class FileReads:
    """Every path opened through ``gzip.open`` or read through ``Path.read_bytes`` while active.

    ``events`` holds ``(path, probe())`` for each, so a test can ask what was true at that read."""

    def __init__(self, probe=lambda: None):
        self.probe = probe
        self.events = []
        self._patches = []

    def __enter__(self):
        real_open, real_read = gzip.open, Path.read_bytes

        def opening(filename, *args, **kwargs):
            self.events.append((Path(filename), self.probe()))
            return real_open(filename, *args, **kwargs)

        def reading(path):
            self.events.append((Path(path), self.probe()))
            return real_read(path)

        self._patches = [
            mock.patch.object(gzip, "open", opening),
            mock.patch.object(Path, "read_bytes", reading),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc_info):
        for patch in reversed(self._patches):
            patch.stop()

    def paths(self):
        return [path for path, _probe in self.events]


def blocked_none():
    return MappingProxyType({reason: 0 for reason in BLOCK_REASONS})


def trades_from(start_ms, pnls):
    """Mode A trades 30 minutes apart from ``start_ms``, one per profit, 50,000 USDT volume each."""
    return tuple(
        make_trade(entry_ms=start_ms + k * 30 * MINUTE_MS, pnl_usdt=pnl)
        for k, pnl in enumerate(pnls)
    )


def fabricated(by_run):
    """A ``simulate_run`` that returns ``by_run[(mode, tf, stage)]`` (stage "latihan" or
    "ujian") and no trades for a run it does not name."""

    def fake(frames, cfg, run):
        stage = "latihan" if run.start_ms == TRAIN_START_MS else "ujian"
        return RunResult(tuple(by_run.get((run.mode, run.tf, stage), ())), blocked_none())

    return fake


class StageCase(unittest.TestCase):
    """A temporary root with the synthetic week under ``data``, the staged config and ``out``."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.out = self.root / "out"
        self.config = write_stage_config(self.root / "modes.yaml")
        write_naik_market(self.data)

    def train(self, now=CLOCK):
        return stages.train_stage(self.config, self.data, self.out, MODE_A, SYMBOL, now)

    def run_test_stage(self, now=CLOCK):
        return stages.test_stage(self.config, self.data, self.out, MODE_A, SYMBOL, now)

    def cli(self, command, now=CLOCK, mode=MODE_A, pair=SYMBOL):
        argv = [command, "--mode", mode, "--pair", pair, "--config", str(self.config),
                "--data-dir", str(self.data), "--out-dir", str(self.out)]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "now", return_value=now), \
                mock.patch.dict(os.environ, {cli.DEBUG_ENV: ""}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def lock_file(self):
        return lock.lock_path(self.out, MODE_A, SYMBOL)

    def marker(self):
        sha = hashlib.sha256(self.lock_file().read_bytes()).hexdigest()
        return lock.marker_path(self.data, MODE_A, SYMBOL, sha)

    def started_record(self):
        """The start record the test stage claims next to the lock, whatever the data copy."""
        return self.out / "mode_A_BTC-USDT_test_started.json"

    def day_file(self, day):
        return self.data / "prod" / SYMBOL / "1m" / f"{day.isoformat()}.csv.gz"

    def snapshot(self):
        """Every file under the root and its bytes: what a refused run must leave as it was."""
        return {
            path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def assert_one_error_line(self, err):
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("error: "), err)
        return lines[0]


class LockFileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_the_lock_and_the_marker_have_the_names_of_the_spec(self):
        self.assertEqual(lock.lock_path(self.root, "A", SYMBOL),
                         self.root / "mode_A_BTC-USDT_lock.json")
        self.assertEqual(lock.marker_path(self.root, "A", SYMBOL, SHA),
                         self.root / "holdout_runs" / f"mode_A_BTC-USDT_{SHA}.json")
        for bad in ("abc", SHA.upper(), SHA + "0", "../" + SHA[3:]):
            with self.subTest(sha=bad), self.assertRaises(ValueError):
                lock.marker_path(self.root, "A", SYMBOL, bad)
        for mode, pair in (("A/..", SYMBOL), ("A", "../BTC-USDT"), ("", SYMBOL)):
            with self.subTest(mode=mode, pair=pair), self.assertRaises(ValueError):
                lock.lock_path(self.root, mode, pair)

    def bound(self, **changes):
        values = dict(config_sha="c" * 64, code_sha="d" * 64, train_manifest_sha="e" * 64,
                      python="3.10")
        values.update(changes)
        return values

    def test_check_lock_passes_a_lock_of_the_current_files(self):
        doc = {"config_sha256": "c" * 64, "code_sha256": "d" * 64,
               "train_manifest_sha256": "e" * 64, "python": "3.10"}

        self.assertIsNone(lock.check_lock(doc, **self.bound()))

    def test_check_lock_lists_every_mismatch(self):
        doc = {"config_sha256": "1" * 64, "code_sha256": "2" * 64,
               "train_manifest_sha256": "3" * 64, "python": "3.9"}

        with self.assertRaises(lock.LockError) as caught:
            lock.check_lock(doc, **self.bound())

        problems = caught.exception.problems
        self.assertEqual(len(problems), 4, problems)
        for key, locked, current in (("config_sha256", "1" * 64, "c" * 64),
                                     ("code_sha256", "2" * 64, "d" * 64),
                                     ("train_manifest_sha256", "3" * 64, "e" * 64),
                                     ("python", "3.9", "3.10")):
            line = next(p for p in problems if p.startswith(key))
            self.assertIn(locked, line)
            self.assertIn(current, line)
        self.assertIn("4 mismatches", str(caught.exception))

    def test_check_lock_names_a_missing_binding_and_refuses_what_is_not_a_lock(self):
        with self.assertRaises(lock.LockError) as caught:
            lock.check_lock({"config_sha256": "c" * 64}, **self.bound())
        self.assertEqual(len(caught.exception.problems), 3)
        self.assertTrue(all("missing" in p for p in caught.exception.problems))
        for doc in ([], "lock", None):
            with self.subTest(doc=doc), self.assertRaises(lock.LockError):
                lock.check_lock(doc, **self.bound())

    def test_read_lock_returns_the_document_and_the_sha256_of_its_bytes(self):
        path = self.root / "mode_A_BTC-USDT_lock.json"
        raw = b'{"mode": "A", "tf": "15m"}\n'
        path.write_bytes(raw)

        doc, sha = lock.read_lock(path)

        self.assertEqual(doc, {"mode": "A", "tf": "15m"})
        self.assertEqual(sha, hashlib.sha256(raw).hexdigest())

    def test_read_lock_refuses_a_missing_or_broken_lock(self):
        path = self.root / "mode_A_BTC-USDT_lock.json"
        with self.assertRaises(lock.LockError) as caught:
            lock.read_lock(path)
        self.assertIn("train", str(caught.exception))
        for raw in (b"{not json", b"[1, 2]", b"\xff\xfe"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaises(lock.LockError):
                    lock.read_lock(path)
        path.unlink()
        path.mkdir()
        with self.assertRaises(lock.LockError) as caught:
            lock.read_lock(path)
        self.assertIn("cannot be read", str(caught.exception))

    def test_claim_marker_writes_once_and_refuses_an_existing_marker(self):
        path = lock.marker_path(self.root, "A", SYMBOL, SHA)

        lock.claim_marker(path, {"lock_sha256": SHA})
        first = path.read_bytes()
        with self.assertRaises(lock.LockError) as caught:
            lock.claim_marker(path, {"lock_sha256": "other"})

        self.assertEqual(read_json(path), {"lock_sha256": SHA})
        self.assertEqual(path.read_bytes(), first)
        self.assertIn("already ran", str(caught.exception))

    def test_the_start_record_sits_next_to_the_lock_and_is_claimed_once(self):
        path = lock.started_path(self.root, "A", SYMBOL)
        self.assertEqual(path, self.root / "mode_A_BTC-USDT_test_started.json")

        lock.claim_started(path, {"lock_sha256": SHA})
        first = path.read_bytes()
        with self.assertRaises(lock.LockError) as caught:
            lock.claim_started(path, {"lock_sha256": "other"})

        self.assertEqual(read_json(path), {"lock_sha256": SHA})
        self.assertEqual(path.read_bytes(), first)
        self.assertIn("already ran", str(caught.exception))

    def test_existing_markers_are_those_of_the_mode_and_pair(self):
        runs = self.root / "holdout_runs"
        runs.mkdir()
        mine = runs / f"mode_A_BTC-USDT_{SHA}.json"
        for path in (mine, runs / f"mode_A_ETH-USDT_{SHA}.json", runs / f"{SHA}.json",
                     runs / "mode_A_BTC-USDT_notasha.json"):
            path.write_text("{}", encoding="utf-8")

        self.assertEqual(lock.existing_markers(self.root, "A", SYMBOL), [mine])
        self.assertEqual(lock.existing_markers(self.root / "absent", "A", SYMBOL), [])

    def test_code_sha256_is_the_sha256_of_the_code_file_lines(self):
        files = lock.code_files()
        text = "\n".join(f"{rel}:{sha}" for rel, sha in files)

        self.assertEqual(lock.code_sha256(), hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(files, sorted(files))
        src = PACKAGE.parents[1]
        for rel, sha in files:
            self.assertEqual(sha, hashlib.sha256((src / rel).read_bytes()).hexdigest(), rel)

    def test_code_files_cover_the_package_the_rule_core_and_what_they_import(self):
        covered = {rel for rel, _sha in lock.code_files()}
        src = PACKAGE.parents[1]
        own = {p.relative_to(src).as_posix() for p in PACKAGE.glob("*.py")}
        own |= {p.relative_to(src).as_posix()
                for p in (src / "bot" / "strategy" / "modes").glob("*.py")}

        self.assertLessEqual(own, covered)
        for imported in ("bot/arena/__init__.py", "bot/arena/factors.py", "bot/models.py",
                         "research/pine_gate/features.py", "bot/strategy/indicators.py"):
            self.assertIn(imported, covered)
        self.assertNotIn("bot/config.py", covered)  # imported for I/O only, never called
        self.assertIn("research/mode_backtest/stages.py", covered)
        self.assertIn("bot/strategy/modes/near_mean.py", covered)

    def copy_of_src(self) -> Path:
        """A copy of ``src`` under the test's temp folder, caches left out."""
        src = self.root / "src"
        shutil.copytree(PACKAGE.parents[1], src, ignore=shutil.ignore_patterns("__pycache__"))
        return src

    def test_a_new_import_of_a_module_the_package_reaches_needs_no_list_edit(self):
        # bot/arena/__init__.py is live-bot code that the package imports through setup_profile.
        src = self.copy_of_src()
        arena = src / "bot" / "arena" / "__init__.py"
        arena.write_text(arena.read_text(encoding="utf-8") + "\nfrom bot import logger\n",
                         encoding="utf-8")

        with mock.patch.object(lock, "SRC_ROOT", src):
            covered = {rel for rel, _sha in lock.code_files()}

        self.assertIn("bot/logger.py", covered)
        self.assertNotIn("bot/logger.py", {rel for rel, _sha in lock.code_files()})

    def test_files_no_import_can_name_are_not_fingerprinted(self):
        # Dotfiles, and names that are no module name, which no import statement can load.
        src = self.copy_of_src()
        folder = src / "research" / "mode_backtest"
        (folder / ".#stages.py").symlink_to("someone@host.1234:1700000000")  # an editor lock
        (folder / "._stages.py").write_bytes(b"\x00\x05\x16\x07")  # a macOS AppleDouble file
        (folder / "stages.v2.py").write_text("x = 1\n", encoding="utf-8")  # a hand backup
        (folder / "stages copy.py").write_text("x = 1\n", encoding="utf-8")  # a Finder copy

        with mock.patch.object(lock, "SRC_ROOT", src):
            names = [rel for rel, _sha in lock.code_files()]
            sha = lock.code_sha256()

        self.assertEqual([name for name in names if not Path(name).stem.isidentifier()], [])
        self.assertEqual(sha, lock.code_sha256())  # the same bytes as the real tree

    def test_each_file_is_read_once_so_the_hashed_bytes_are_the_parsed_bytes(self):
        real = Path.read_bytes
        reads = Counter()

        def counted(path):
            reads[path] += 1
            return real(path)

        with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=counted):
            files = lock.code_files()

        self.assertEqual(len(reads), len(files))
        self.assertEqual(max(reads.values()), 1)

    def test_python_version_is_major_dot_minor(self):
        self.assertEqual(lock.python_version(), f"{sys.version_info[0]}.{sys.version_info[1]}")


class TrainStageTests(StageCase):
    def test_train_writes_results_logs_and_an_exclusive_lock(self):
        path = self.train()

        self.assertEqual(path, self.out / f"mode_A_BTC-USDT_train_{STAMP}.json")
        doc = read_json(path)
        self.assertEqual((doc["stage"], doc["mode"], doc["pair"], doc["tf"]),
                         ("train", "A", SYMBOL, "15m"))
        # Monday is no NAIK day: the 15m EMA20 is first known on Tuesday (see the test below).
        self.assertEqual(doc["naik_days"], [d.isoformat() for d in TRAIN_DAYS[1:]])
        self.assertEqual(sorted(doc["runs"]), TRAIN_RUNS)
        for key, run in doc["runs"].items():
            with self.subTest(run=key):
                rows = read_log(self.out / run["trade_log"])
                self.assertEqual(rows[0], list(COLUMNS))
                self.assertGreater(run["metrics"]["posisi"], 0)
                self.assertEqual(len(rows) - 1, run["metrics"]["posisi"])
                self.assertEqual(run["metrics"]["hari_naik"], 2)
                self.assertEqual(sorted(run["blocked"]), sorted(BLOCK_REASONS))  # keys sorted
                exits = [wib(row[COLUMNS.index("waktu_exit")]) for row in rows[1:]]
                self.assertLess(max(exits), wib("2026-07-09T00:00:00+07:00"))
        self.assertEqual(sorted(doc["files"]), sorted(p.name for p in self.out.iterdir()))

        locked = read_json(self.lock_file())
        training = stage_files(self.data, SYMBOL, TRAIN_DAYS[0], TRAIN_DAYS[-1])
        self.assertEqual(locked["config_sha256"],
                         hashlib.sha256(self.config.read_bytes()).hexdigest())
        self.assertEqual(locked["code_sha256"], lock.code_sha256())
        self.assertEqual(locked["train_manifest_sha256"],
                         manifest_sha256(manifest(self.data, training)))
        self.assertEqual(locked["python"], lock.python_version())
        self.assertEqual((locked["mode"], locked["pair"], locked["tf"]), ("A", SYMBOL, "15m"))
        self.assertEqual(locked["summary"]["mode_a"], doc["runs"]["A_15m_NAIK"]["metrics"])
        self.assertEqual(locked["summary"]["mode_a_15m"], doc["runs"]["A_15m_NAIK"]["metrics"])
        self.assertEqual(locked["summary"]["pembanding"], doc["runs"]["D_3m_NAIK"]["metrics"])
        self.assertEqual(locked["latihan"]["start"], "2026-07-06T00:00:00+07:00")
        self.assertEqual(locked["latihan"]["end"], "2026-07-09T00:00:00+07:00")
        self.assertEqual(locked["ujian"]["end"], "2026-07-11T00:00:00+07:00")

        lock_bytes = self.lock_file().read_bytes()
        with self.assertRaises(FileExistsError) as caught:
            self.train(now=CLOCK + timedelta(days=1))
        self.assertIn(self.lock_file().name, str(caught.exception))
        self.assertEqual(self.lock_file().read_bytes(), lock_bytes)
        self.assertFalse((self.out / "mode_A_BTC-USDT_train_20261019.json").exists())

    def test_train_never_opens_a_test_day_file(self):
        corrupt, folder = self.day_file(TEST_DAYS[0]), self.day_file(TEST_DAYS[1])
        corrupt.write_bytes(b"not a gzip file")
        folder.unlink()
        folder.mkdir()  # a folder cannot be opened or hashed, whoever runs the test

        with FileReads() as reads:
            code, _out, err = self.cli("train")

        self.assertEqual(code, 0, err)
        self.assertEqual(corrupt.read_bytes(), b"not a gzip file")
        self.assertNotIn(corrupt, reads.paths())
        self.assertNotIn(folder, reads.paths())
        self.assertIn(self.day_file(TRAIN_DAYS[-1]), reads.paths())

    def test_five_minute_switch_runs_once_and_is_locked(self):
        calls = []
        real = stages.simulate_run

        def spy(frames, cfg, run):
            calls.append((run.mode, run.tf, run.kondisi_gate, run.start_ms, run.end_ms))
            return real(frames, cfg, run)

        with mock.patch.object(stages, "wants_5m", return_value=True) as switch, \
                mock.patch.object(stages, "simulate_run", side_effect=spy):
            path = self.train()

        self.assertEqual(sorted(call[:2] for call in calls),
                         [("A", "15m"), ("A", "5m"), ("D", "3m")])
        train_end_ms = TEST_START_MS
        self.assertTrue(all(call[2:] == ("NAIK", TRAIN_START_MS, train_end_ms) for call in calls))
        doc = read_json(path)
        switch.assert_called_once()
        m15, uji = switch.call_args.args
        self.assertEqual(m15, StageMetrics(**doc["runs"]["A_15m_NAIK"]["metrics"]))
        self.assertEqual(uji.pindah_5m_jika_entry_per_hari_naik_kurang_dari, 3.0)
        self.assertEqual(sorted(doc["runs"]), SWITCHED_RUNS)
        self.assertEqual(doc["tf"], "5m")
        self.assertTrue(doc["switch_5m"]["pindah"])
        locked = read_json(self.lock_file())
        self.assertEqual(locked["tf"], "5m")
        self.assertEqual(locked["summary"]["mode_a"], doc["runs"]["A_5m_NAIK"]["metrics"])
        self.assertEqual(locked["summary"]["mode_a_15m"], doc["runs"]["A_15m_NAIK"]["metrics"])
        self.assertTrue((self.out / doc["runs"]["A_5m_NAIK"]["trade_log"]).is_file())

        calls.clear()  # the test stage checks the locked 5m against the same switch rule
        with mock.patch.object(stages, "wants_5m", return_value=True), \
                mock.patch.object(stages, "simulate_run", side_effect=spy):
            self.run_test_stage()
        self.assertEqual(sorted(call[:2] for call in calls), [("A", "5m"), ("D", "3m")])
        self.assertTrue(all(call[2:] == ("NAIK", TEST_START_MS, TEST_END_MS) for call in calls))

    def test_without_the_switch_mode_a_runs_once_on_15m(self):
        with mock.patch.object(stages, "wants_5m", return_value=False), \
                mock.patch.object(stages, "simulate_run", wraps=stages.simulate_run) as runs:
            path = self.train()

        self.assertEqual(sorted((c.args[2].mode, c.args[2].tf) for c in runs.call_args_list),
                         [("A", "15m"), ("D", "3m")])
        doc = read_json(path)
        self.assertEqual((doc["tf"], doc["switch_5m"]["pindah"]), ("15m", False))
        self.assertEqual(read_json(self.lock_file())["tf"], "15m")

    def test_naik_days_start_when_mode_a_can_enter(self):
        # Spec section 5: a NAIK hour is one in which Mode A could enter. Training starts on
        # Monday at 00:00 WIB with no 1m bar before it, and the EMA20 needs 160 candles, so the
        # 15m one is first known on Tuesday at 16:00 WIB and the 5m one on Monday at 13:20 WIB.
        # Monday is no NAIK day of the 15m run nor of the comparator measured with it, while it
        # is one of the 5m run, which can enter from 13:20.
        with mock.patch.object(stages, "wants_5m", return_value=True):
            doc = read_json(self.train())

        self.assertEqual((doc["naik_days"], doc["hari_naik"]), (["2026-07-07", "2026-07-08"], 2))
        self.assertEqual({key: run["metrics"]["hari_naik"] for key, run in doc["runs"].items()},
                         {"A_15m_NAIK": 2, "A_5m_NAIK": 3, "D_3m_NAIK": 2})
        self.assertEqual(read_json(self.lock_file())["summary"]["hari_naik"], 2)

    def test_a_training_too_short_for_the_trigger_ema_has_no_naik_day(self):
        # One training day is 96 15m candles, short of the 160 the EMA20 needs: Mode A never
        # signals on 15m, so no hour is a NAIK hour and the switch cannot be evaluated. Counted
        # as before, Monday would be a NAIK day with 0 entries and lock 5m on a cold EMA.
        write_stage_config(self.config, latihan_selesai='"2026-07-07T00:00:00+07:00"')

        code, _out, err = self.cli("train")

        self.assertEqual(code, 1, err)
        self.assertIn("5m switch cannot be evaluated", self.assert_one_error_line(err))
        self.assertFalse(self.out.exists())

    def test_naik_days_follow_the_minimum_hours_of_the_config(self):
        # Mode A on 15m can enter in 8 hours of Tuesday (the closes 16:00 to 23:00 WIB) and in
        # all 24 of Wednesday; Monday has none (see the test above).
        for minimum, expected in ((8, ["2026-07-07", "2026-07-08"]), (9, ["2026-07-08"])):
            with self.subTest(minimum=minimum):
                self.setUp()
                write_stage_config(self.config, hari_naik_minimal_jam_naik=minimum)

                doc = read_json(self.train())

                self.assertEqual(doc["naik_days"], expected)
                self.assertEqual({run["metrics"]["hari_naik"] for run in doc["runs"].values()},
                                 {len(expected)})

    def test_train_refuses_a_warm_up_too_short_for_the_first_day(self):
        for hours_1h, bars_4h, series in ((WARM_HOURS_1H - 1, WARM_BARS_4H, "atr_median"),
                                          (WARM_HOURS_1H, WARM_BARS_4H - 1, "ema50_4h")):
            with self.subTest(series=series):
                write_naik_market(self.data, hours_1h=hours_1h, bars_4h=bars_4h)
                code, _out, err = self.cli("train")
                self.assertEqual(code, 1, err)
                line = self.assert_one_error_line(err)
                self.assertIn(series, line)
                self.assertIn("2026-07-06", line)
                self.assertFalse(self.out.exists())

    def test_a_naive_clock_is_refused_before_anything_is_written(self):
        with self.assertRaises(ValueError) as caught:
            self.train(now=CLOCK.replace(tzinfo=None))

        self.assertIn("timezone-aware", str(caught.exception))
        self.assertFalse(self.out.exists())

    def test_a_training_stage_without_a_naik_day_fails_loudly(self):
        flat = self.root / "flat"
        write_days(flat, SYMBOL, TRAIN_DAYS)
        write_warmups(flat, SYMBOL, TRAIN_DAYS[0], WARM_HOURS_1H, WARM_BARS_4H)
        self.data = flat

        code, _out, err = self.cli("train")

        self.assertEqual(code, 1, err)
        self.assertIn("5m switch cannot be evaluated", self.assert_one_error_line(err))
        self.assertFalse(self.out.exists())

    def test_train_refuses_mode_a_on_5m_candles_before_the_switch(self):
        text = stage_config_text()
        start = "  A_pullback_long:\n    aktif: true\n    tf_pemicu: "
        self.assertIn(start + "15m", text)
        self.config.write_text(text.replace(start + "15m", start + "5m"), encoding="utf-8")

        code, _out, err = self.cli("train")

        self.assertEqual(code, 1, err)
        self.assertIn("tf_pemicu", self.assert_one_error_line(err))
        self.assertFalse(self.out.exists())


class TestStageTests(StageCase):
    def test_test_runs_the_locked_timeframe_on_the_test_period_and_writes_the_verdict(self):
        self.train()

        path = self.run_test_stage()

        self.assertEqual(path, self.out / f"mode_A_BTC-USDT_test_{STAMP}.json")
        doc = read_json(path)
        self.assertEqual((doc["stage"], doc["tf"]), ("test", "15m"))
        self.assertEqual(doc["naik_days"], [d.isoformat() for d in TEST_DAYS])
        self.assertEqual(sorted(doc["runs"]), TRAIN_RUNS)
        for key, run in doc["runs"].items():
            with self.subTest(run=key):
                rows = read_log(self.out / run["trade_log"])
                self.assertEqual(len(rows) - 1, run["metrics"]["posisi"])
                self.assertGreater(run["metrics"]["posisi"], 0)
                self.assertEqual(run["metrics"]["hari_naik"], 2)
                entries = [wib(row[COLUMNS.index("waktu_entry")]) for row in rows[1:]]
                exits = [wib(row[COLUMNS.index("waktu_exit")]) for row in rows[1:]]
                self.assertGreaterEqual(min(entries), wib("2026-07-09T00:00:00+07:00"))
                self.assertLess(max(exits), wib("2026-07-11T00:00:00+07:00"))
        cfg_uji = load_modes_config(self.config).uji
        mode_a = StageMetrics(**doc["runs"]["A_15m_NAIK"]["metrics"])
        self.assertEqual(doc["status"], pass_status(mode_a, cfg_uji))
        self.assertEqual(doc["status"], "BELUM CUKUP SAMPEL")

        summary = read_json(self.lock_file())["summary"]
        table = mode_table([
            ReportRow("A", SYMBOL, "Latihan", StageMetrics(**summary["mode_a"])),
            ReportRow("D di NAIK", SYMBOL, "Latihan", StageMetrics(**summary["pembanding"])),
            ReportRow("A", SYMBOL, "Ujian", mode_a),
            ReportRow("D di NAIK", SYMBOL, "Ujian",
                      StageMetrics(**doc["runs"]["D_3m_NAIK"]["metrics"])),
        ])
        report = path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn(table, report)
        self.assertIn(doc["status_line"], report)
        marker = read_json(self.marker())
        self.assertEqual(marker["lock_sha256"], self.marker().stem.rsplit("_", 1)[1])
        self.assertEqual(doc["marker"], str(self.marker()))
        self.assertIn(self.started_record().name, doc["files"])

    def test_marker_is_written_before_any_test_day_is_read(self):
        self.train()
        marker, started = self.marker(), self.started_record()
        lock_sha = hashlib.sha256(self.lock_file().read_bytes()).hexdigest()
        test_days = {self.day_file(day) for day in TEST_DAYS}

        with FileReads(probe=lambda: marker.exists() and started.exists()) as reads:
            self.run_test_stage()

        test_reads = [claimed for path, claimed in reads.events if path in test_days]
        self.assertTrue(test_reads)
        self.assertTrue(all(test_reads))
        training = [claimed for path, claimed in reads.events
                    if path == self.day_file(TRAIN_DAYS[0])]
        self.assertIn(False, training)  # hashed against the lock before the claims
        record = read_json(started)
        self.assertEqual(record["lock_sha256"], lock_sha)
        self.assertEqual(record["data_dir"], str(self.data.resolve()))
        self.assertEqual(record["created_at"], CLOCK.isoformat())

    def test_the_verdict_records_the_manifests_of_the_data_it_read(self):
        recorded = {}
        for changed in (False, True):
            with self.subTest(one_byte_of_a_test_day_changed=changed):
                self.setUp()  # a fresh root each time: the test runs once per out-dir
                self.train()
                day = self.day_file(TEST_DAYS[1])
                written = day.read_bytes()
                if changed:  # after train, so the lock still matches; the bars stay the same
                    flip_gzip_mtime(day)
                    self.assertNotEqual(day.read_bytes(), written)
                warmups = [self.data / "prod" / SYMBOL / interval / "warmup.csv.gz"
                           for interval in ("1h", "4h")]
                test_days = [self.day_file(d) for d in TEST_DAYS]
                loaded = [self.day_file(d) for d in TRAIN_DAYS] + test_days + warmups

                path = self.run_test_stage()

                doc = read_json(path)
                expected = {
                    "loaded_files": 7,
                    "loaded_manifest_sha256": manifest_sha256(manifest(self.data, loaded)),
                    "test_day_files": 2,
                    "test_days_manifest_sha256": manifest_sha256(manifest(self.data, test_days)),
                }
                self.assertEqual({key: doc.get(key) for key in expected}, expected)
                report = path.with_suffix(".md").read_text(encoding="utf-8")
                for key in ("loaded_manifest_sha256", "test_days_manifest_sha256"):
                    self.assertIn(f"`{doc[key]}`", report)
                recorded[changed] = doc
        for key in ("loaded_manifest_sha256", "test_days_manifest_sha256"):
            self.assertNotEqual(recorded[False][key], recorded[True][key], key)
        self.assertEqual(recorded[False]["train_manifest_sha256"],
                         recorded[True]["train_manifest_sha256"])
        self.assertEqual(recorded[False]["runs"], recorded[True]["runs"])  # same bars, same runs

    def test_a_test_that_failed_after_its_claim_refuses_a_rerun_on_a_fresh_data_copy(self):
        self.train()
        fresh = self.root / "fresh_copy"
        shutil.copytree(self.data, fresh)
        # A regular file passes every check before the claim and fails only when it is read.
        self.day_file(TEST_DAYS[1]).write_bytes(b"not a gzip file")

        code, _out, err = self.cli("test")
        self.assertEqual(code, 1, err)
        self.assertTrue(self.marker().exists())
        before = self.snapshot()

        self.data = fresh  # the natural recovery: copy the data again, run again
        code, _out, err = self.cli("test")

        self.assertEqual(code, 2, err)
        self.assertIn("already ran", self.assert_one_error_line(err))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((fresh / "holdout_runs").exists())

    def test_second_test_run_refuses(self):
        self.train()
        self.run_test_stage()
        before = self.snapshot()

        for now in (CLOCK, CLOCK + timedelta(days=3)):
            with self.subTest(now=now):
                code, _out, err = self.cli("test", now=now)
                self.assertEqual(code, 2, err)
                self.assertIn("already ran", self.assert_one_error_line(err))
                self.assertEqual(self.snapshot(), before)

    def test_a_new_lock_is_refused_once_the_test_ran(self):
        self.train()
        fresh = self.root / "fresh_copy"
        shutil.copytree(self.data, fresh)  # copied before the test, so it holds no marker
        self.run_test_stage()
        self.lock_file().unlink()
        before = self.snapshot()

        for data_dir in (self.data, fresh):
            with self.subTest(data_dir=data_dir.name):
                self.data = data_dir
                code, _out, err = self.cli("train", now=CLOCK + timedelta(days=1))
                self.assertEqual(code, 2, err)
                self.assertIn("already ran", self.assert_one_error_line(err))
                self.assertEqual(self.snapshot(), before)

    def test_a_hand_edited_lock_is_refused_before_the_marker(self):
        self.train()
        raw = self.lock_file().read_bytes()
        original = json.loads(raw)
        edits = (
            ("tf", "tf", {"tf": "5m"}),  # 12 entries on 2 NAIK days: the switch rule keeps 15m
            ("latihan", "latihan",
             {"latihan": {**original["latihan"], "start": "2026-07-07T00:00:00+07:00"}}),
            ("pair", "pair", {"pair": "ETH-USDT"}),
            ("summary", "summary.mode_a",
             {"summary": {k: v for k, v in original["summary"].items() if k != "mode_a"}}),
            ("15m run without a NAIK day", "summary.mode_a_15m",
             {"summary": {**original["summary"], "mode_a_15m": {
                 **original["summary"]["mode_a_15m"], "entry_per_hari_aktif": None}}}),
        )
        for name, named, change in edits:
            with self.subTest(edit=name):
                self.lock_file().write_text(json.dumps({**original, **change}), encoding="utf-8")
                before = self.snapshot()
                code, out, err = self.cli("test")
                self.assertEqual(code, 2, err)
                self.assert_one_error_line(err)
                self.assertIn(named, out)
                self.assertEqual(self.snapshot(), before)
                self.lock_file().write_bytes(raw)

        code, _out, err = self.cli("test")
        self.assertEqual(code, 0, err)

    def test_a_missing_test_day_is_refused_before_the_marker(self):
        self.train()
        self.day_file(TEST_DAYS[1]).unlink()
        before = self.snapshot()

        code, _out, err = self.cli("test")

        self.assertEqual(code, 1, err)
        self.assertIn(f"{TEST_DAYS[1].isoformat()}.csv.gz", self.assert_one_error_line(err))
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.started_record().exists())
        self.assertEqual(self.snapshot(), before)

    def test_a_folder_in_place_of_a_test_day_is_refused_before_the_marker(self):
        self.train()
        folder = self.day_file(TEST_DAYS[1])
        folder.unlink()
        folder.mkdir()  # the name exists, but it is no regular file
        before = self.snapshot()

        code, _out, err = self.cli("test")

        self.assertEqual(code, 1, err)
        self.assertIn("prod/BTC-USDT/1m/2026-07-10.csv.gz", self.assert_one_error_line(err))
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.started_record().exists())
        self.assertEqual(self.snapshot(), before)

    def test_test_refuses_without_a_lock(self):
        code, _out, err = self.cli("test")

        self.assertEqual(code, 2, err)
        self.assertIn("train", self.assert_one_error_line(err))
        self.assertFalse((self.data / "holdout_runs").exists())
        self.assertFalse(self.out.exists())

    @contextlib.contextmanager
    def changed_bytes(self, path, new_bytes):
        original = path.read_bytes()
        path.write_bytes(new_bytes(original))
        try:
            yield
        finally:
            path.write_bytes(original)

    @contextlib.contextmanager
    def moved_away(self, path):
        aside = path.with_name(path.name + ".aside")
        path.rename(aside)
        try:
            yield
        finally:
            aside.rename(path)

    def test_test_refuses_on_a_changed_config_code_or_training_byte(self):
        self.train()
        before = self.snapshot()
        code_files = lock.code_files()
        changed_code = [(rel, "f" * 64 if rel.endswith("simulate.py") else sha)
                        for rel, sha in code_files]
        warmup = self.data / "prod" / SYMBOL / "1h" / "warmup.csv.gz"
        cases = (
            ("config", "config_sha256",
             lambda: self.changed_bytes(self.config, lambda b: b + b"# one more comment\n")),
            ("training day", "train_manifest_sha256",
             lambda: self.changed_bytes(self.day_file(TRAIN_DAYS[1]), lambda b: b[:-1])),
            ("warm-up", "train_manifest_sha256",
             lambda: self.changed_bytes(warmup, lambda b: b + b"\x00")),
            ("missing training day", "2026-07-07.csv.gz",
             lambda: self.moved_away(self.day_file(TRAIN_DAYS[1]))),
            ("code", "code_sha256",
             lambda: mock.patch.object(lock, "code_files", return_value=changed_code)),
            ("python", "python", lambda: mock.patch.object(lock, "python_version",
                                                           return_value="2.7")),
        )
        for name, named, change in cases:
            with self.subTest(change=name):
                with change():
                    code, out, err = self.cli("test")
                self.assertEqual(code, 2, err)
                self.assert_one_error_line(err)
                self.assertIn(named, out + err)
                self.assertEqual(self.snapshot(), before)

        code, _out, err = self.cli("test")
        self.assertEqual(code, 0, err)

    def test_outputs_are_never_overwritten(self):
        self.train()
        before = self.snapshot()

        code, _out, err = self.cli("train")
        self.assertEqual(code, 1, err)
        self.assertIn("already exist", self.assert_one_error_line(err))
        self.assertEqual(self.snapshot(), before)

        for name in ("mode_A_BTC-USDT_test_20261001.json",
                     f"mode_A_BTC-USDT_test_{STAMP}_A_15m_NAIK.csv",
                     f"mode_A_BTC-USDT_test_{STAMP}.md"):
            with self.subTest(existing=name):
                existing = self.out / name
                existing.write_text("written before\n", encoding="utf-8")
                code, _out, err = self.cli("test")
                self.assertEqual(code, 1, err)
                self.assertIn(name, self.assert_one_error_line(err))
                self.assertEqual(existing.read_text(encoding="utf-8"), "written before\n")
                self.assertFalse(self.marker().exists())
                existing.unlink()
        self.assertEqual(self.snapshot(), before)


class VerdictTests(StageCase):
    def staged(self, by_run, **uji):
        """Train and test on ``config/modes.yaml`` with ``uji`` replaced and ``by_run`` as runs."""
        write_stage_config(self.config, **uji)
        with mock.patch.object(stages, "simulate_run", side_effect=fabricated(by_run)):
            train = read_json(self.train())
            test = read_json(self.run_test_stage())
        return train, test

    def test_drop_top_comes_from_the_config_and_changes_the_verdict(self):
        # 37 losers of -15 and 3 winners of +100: -127.5 per 1M, and -300 without the best 3.
        test_trades = trades_from(TEST_START_MS, [-15.0] * 37 + [100.0] * 3)
        by_run = {("A", "15m", "latihan"): trades_from(TRAIN_START_MS, [10.0] * 40),
                  ("A", "15m", "ujian"): test_trades}
        for drop_top, tanpa, status in ((3, -300.0, "GAGAL"), (0, -127.5, "LOLOS")):
            with self.subTest(drop_top=drop_top):
                self.setUp()
                with mock.patch.object(stages, "stage_metrics",
                                       wraps=stages.stage_metrics) as metrics:
                    _train, test = self.staged(by_run, lolos_buang_posisi_teratas=drop_top)
                passed = {c.args[2] if len(c.args) > 2 else c.kwargs["drop_top"]
                          for c in metrics.call_args_list}
                self.assertEqual(passed, {drop_top})
                mode_a = test["runs"]["A_15m_NAIK"]["metrics"]
                self.assertEqual((mode_a["posisi"], mode_a["hasil_per_1_juta"]), (40, -127.5))
                self.assertEqual(mode_a["hasil_tanpa_teratas"], tanpa)
                self.assertEqual((test["drop_top"], test["status"]), (drop_top, status))

    def test_the_status_lines_and_the_json_carry_the_exact_numbers(self):
        # 5 entries on the 2 NAIK days of the 15m run (5/2) move training to 5m, whose run has 3
        # (it can enter on Monday); the test sits at -149.63 per 1M, which the table rounds to
        # -150 while the rule passes it.
        train_5m = trades_from(TRAIN_START_MS, [10.0 / 3.0] * 40)
        test_5m = trades_from(TEST_START_MS, [-7.48] * 39 + [-7.54])
        by_run = {("A", "15m", "latihan"): trades_from(TRAIN_START_MS, [5.0] * 5),
                  ("A", "5m", "latihan"): train_5m,
                  ("A", "5m", "ujian"): test_5m}

        train, test = self.staged(by_run)

        rate = 5 / 2
        self.assertEqual(train["switch_5m"]["entry_per_hari_naik_15m"], rate)
        self.assertIn(repr(rate), train["status_line"])
        locked = stage_metrics(train_5m, 3, 3)
        for value in (locked.hasil_per_1_juta, locked.hasil_tanpa_teratas):
            self.assertIn(repr(value), train["status_line"])
        self.assertEqual(train["runs"]["A_5m_NAIK"]["metrics"]["hasil_per_1_juta"],
                         locked.hasil_per_1_juta)

        judged = stage_metrics(test_5m, 2, 3)
        self.assertEqual(test["status"], "LOLOS")
        self.assertNotEqual(round(judged.hasil_per_1_juta), judged.hasil_per_1_juta)
        for value in (judged.hasil_per_1_juta, judged.hasil_tanpa_teratas):
            self.assertIn(repr(value), test["status_line"])
        self.assertEqual(test["runs"]["A_5m_NAIK"]["metrics"]["hasil_per_1_juta"],
                         judged.hasil_per_1_juta)
        report = (self.out / f"mode_A_BTC-USDT_test_{STAMP}.md").read_text(encoding="utf-8")
        self.assertIn("| A | BTC-USDT | Ujian | 40 | 20,00 | 100,0% | -150 |", report)
        self.assertIn(test["status_line"], report)

    def test_a_test_without_positions_is_belum_cukup_sampel_and_shows_dashes(self):
        by_run = {("A", "15m", "latihan"): trades_from(TRAIN_START_MS, [10.0] * 40)}

        _train, test = self.staged(by_run)

        mode_a = test["runs"]["A_15m_NAIK"]["metrics"]
        self.assertEqual((mode_a["posisi"], mode_a["hasil_per_1_juta"]), (0, None))
        self.assertEqual(test["status"], "BELUM CUKUP SAMPEL")
        self.assertIn("Posisi 0 (minimal 40)", test["status_line"])
        self.assertIn("Hasil per 1 juta = – ", test["status_line"])
        self.assertNotIn("None", test["status_line"])

    def test_stage_metrics_has_one_call_site_and_it_passes_the_config_drop_top(self):
        calls = []
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "stage_metrics":
                    calls.append((path.name, node))

        self.assertEqual([name for name, _node in calls], ["stages.py"])
        node = calls[0][1]
        drop_top = node.args[2] if len(node.args) > 2 else next(
            k.value for k in node.keywords if k.arg == "drop_top")
        self.assertTrue(ast.unparse(drop_top).endswith(".uji.lolos_buang_posisi_teratas"),
                        ast.unparse(drop_top))


if __name__ == "__main__":
    unittest.main()
