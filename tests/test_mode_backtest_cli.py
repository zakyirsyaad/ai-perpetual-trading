"""Tests for the mode backtest command line: exit codes, one error line, and its commands.

The ``train`` and ``test`` stages themselves are tested in ``tests/test_mode_backtest_lock.py``;
here they are replaced, except where the command must refuse before any stage work.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from research.mode_backtest import cli, stages, stages_live, step0
from research.mode_backtest.lock import LockError
from research.mode_backtest.step0 import PINE_FILES, ReproductionError
from tests.mode_backtest_helpers import sha256sum_text, write_days, write_warmups
from tests.mode_backtest_stages import CLOCK as STAGE_CLOCK
from tests.mode_backtest_stages import write_naik_market, write_stage_config

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "modes.yaml"
LIVE_CONFIG = REPO / "config" / "modes_live_exit.yaml"
SYMBOL = "BTC-USDT"
CLOCK = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def run_cli(argv, debug=False):
    """``cli.main(argv)`` with stdout and stderr captured; returns (code, out, err)."""
    out, err = io.StringIO(), io.StringIO()
    env = {cli.DEBUG_ENV: "1" if debug else ""}
    with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), \
            contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.pine = self.root / "pine"
        self.out = self.root / "out"
        self.pine.mkdir()

    def step0_args(self, config=CONFIG):
        return ["step0", "--data-dir", str(self.data), "--pine-dir", str(self.pine),
                "--config", str(config), "--out-dir", str(self.out)]

    def assert_one_error_line(self, err):
        lines = err.splitlines()
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith("error: "), err)
        self.assertNotIn("Traceback", err)
        return lines[0]


class UsageTests(CliTestCase):
    def test_cli_unknown_command_exits_2(self):
        code, out, err = run_cli(["nope"])

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("invalid choice", err)

    def test_no_command_exits_2(self):
        code, _out, err = run_cli([])

        self.assertEqual(code, 2)
        self.assertIn("usage", err)

    def test_a_missing_option_exits_2(self):
        args = self.step0_args()
        for option in ("--data-dir", "--pine-dir", "--config", "--out-dir"):
            with self.subTest(option=option):
                index = args.index(option)
                code, _out, err = run_cli(args[:index] + args[index + 2:])
                self.assertEqual(code, 2)
                self.assertIn(option, err)
        code, _out, err = run_cli(["verify-copy", "--data-dir", str(self.data)])
        self.assertEqual(code, 2)
        self.assertIn("--sha256sum", err)

    def test_help_exits_0(self):
        code, out, _err = run_cli(["--help"])

        self.assertEqual(code, 0)
        self.assertIn("step0", out)
        self.assertIn("verify-copy", out)

    def test_the_module_runs_the_cli(self):
        env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
        done = subprocess.run([sys.executable, "-m", "research.mode_backtest", "nope"],
                              cwd=REPO, env=env, capture_output=True, text=True, timeout=120)

        self.assertEqual(done.returncode, 2)
        self.assertIn("invalid choice", done.stderr)


class Step0CommandTests(CliTestCase):
    def test_cli_missing_pine_file_exits_1_with_one_error_line(self):
        code, out, err = run_cli(self.step0_args())

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        line = self.assert_one_error_line(err)
        for name in PINE_FILES.values():
            self.assertIn(name, line)
        self.assertFalse(self.out.exists())

    def test_step0_runs_with_the_paths_and_the_clock_and_prints_both_files(self):
        written = self.out / "step0_20261017.json"
        with mock.patch.object(step0, "run_step0", return_value=written) as run, \
                mock.patch.object(cli, "now", return_value=CLOCK):
            code, out, err = run_cli(self.step0_args())

        self.assertEqual(code, 0, err)
        run.assert_called_once_with(self.data, self.pine, CONFIG, self.out, CLOCK)
        self.assertEqual(out.splitlines(), [str(written), str(self.out / "step0_20261017.md")])
        self.assertEqual(err, "")

    def test_paths_are_expanded(self):
        with mock.patch.object(step0, "run_step0", return_value=self.out / "x.json") as run, \
                mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            code, _out, _err = run_cli(["step0", "--data-dir", "~/data", "--pine-dir", "~/pine",
                                        "--config", "~/modes.yaml", "--out-dir", "~/out"])

        self.assertEqual(code, 0)
        data_dir, pine_dir, config, out_dir, clock = run.call_args.args
        self.assertEqual((data_dir, pine_dir, config, out_dir),
                         (self.data, self.pine, self.root / "modes.yaml", self.out))
        self.assertIsNotNone(clock.utcoffset())

    def test_a_reproduction_error_prints_every_difference_then_one_error_line(self):
        problems = ("c1 BTC-USDT wr: 48.1 != 48.2", "c2 ETH-USDT n: 3 != 4")
        error = ReproductionError("the replay does not reproduce the holdout file: 2 differences",
                                  problems)
        with mock.patch.object(step0, "run_step0", side_effect=error):
            code, out, err = run_cli(self.step0_args())

        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), list(problems))
        line = self.assert_one_error_line(err)
        self.assertIn("2 differences", line)

    def test_a_long_message_stays_one_line(self):
        error = ValueError("first line\nsecond line " + "x" * 1000)
        with mock.patch.object(step0, "run_step0", side_effect=error):
            code, _out, err = run_cli(self.step0_args())

        self.assertEqual(code, 1)
        line = self.assert_one_error_line(err)
        self.assertLessEqual(len(line), len("error: ") + 400)

    def test_debug_prints_the_traceback_before_the_error_line(self):
        with mock.patch.object(step0, "run_step0", side_effect=ValueError("bad value")):
            code, _out, err = run_cli(self.step0_args(), debug=True)

        self.assertEqual(code, 1)
        self.assertIn("Traceback", err)
        self.assertEqual(err.splitlines()[-1], "error: bad value")

    def test_an_unexpected_error_is_not_hidden(self):
        with mock.patch.object(step0, "run_step0", side_effect=KeyError("bug")):
            with self.assertRaises(KeyError):
                run_cli(self.step0_args())


class VerifyCopyCommandTests(CliTestCase):
    def setUp(self):
        super().setUp()
        first = date(2026, 7, 1)
        write_days(self.data, SYMBOL, [first, date(2026, 7, 2)])
        write_warmups(self.data, SYMBOL, first, hours_1h=4, bars_4h=2)
        self.listing = self.root / "vps_sha256sum.txt"
        self.listing.write_text(sha256sum_text(self.data), encoding="utf-8")

    def verify(self):
        return run_cli(["verify-copy", "--data-dir", str(self.data),
                        "--sha256sum", str(self.listing)])

    def test_an_exact_copy_exits_0(self):
        code, out, err = self.verify()

        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["0 problems: every file matches the VPS listing"])
        self.assertEqual(err, "")

    def test_every_problem_is_printed_and_exits_1(self):
        stray = self.data / "prod" / SYMBOL / "1m" / "2026-07-03.csv.gz"
        stray.write_bytes(b"not listed")
        changed = self.data / "prod" / SYMBOL / "1m" / "2026-07-01.csv.gz"
        changed.write_bytes(changed.read_bytes() + b"\x00")

        code, out, err = self.verify()

        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertEqual(len(lines), 2, out)
        self.assertIn("prod/BTC-USDT/1m/2026-07-01.csv.gz: sha256 differs", lines[0])
        self.assertIn("prod/BTC-USDT/1m/2026-07-03.csv.gz: local file is not in the listing",
                      lines[1])
        line = self.assert_one_error_line(err)
        self.assertIn("2 problems", line)

    def test_a_missing_listing_exits_1_with_one_error_line(self):
        self.listing.unlink()

        code, out, err = self.verify()

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("vps_sha256sum.txt", self.assert_one_error_line(err))



class StageCommandTests(CliTestCase):
    def stage_args(self, command, mode="A", pair=SYMBOL):
        return [command, "--mode", mode, "--pair", pair, "--config", str(CONFIG),
                "--data-dir", str(self.data), "--out-dir", str(self.out)]

    def test_a_missing_stage_option_exits_2(self):
        for command in ("train", "test"):
            args = self.stage_args(command)
            for option in ("--mode", "--pair", "--config", "--data-dir", "--out-dir"):
                with self.subTest(command=command, option=option):
                    index = args.index(option)
                    code, _out, err = run_cli(args[:index] + args[index + 2:])
                    self.assertEqual(code, 2)
                    self.assertIn(option, err)

    def test_help_lists_the_stages(self):
        code, out, _err = run_cli(["--help"])

        self.assertEqual(code, 0)
        self.assertIn("train", out)
        self.assertIn("test", out)

    def test_phase_1_refuses_another_mode_or_pair_with_exit_1(self):
        for command in ("train", "test"):
            for mode, pair in (("D", SYMBOL), ("B", SYMBOL), ("A", "ETH-USDT")):
                with self.subTest(command=command, mode=mode, pair=pair):
                    code, out, err = run_cli(self.stage_args(command, mode, pair))
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    line = self.assert_one_error_line(err)
                    self.assertIn("Phase 1", line)
                    self.assertFalse(self.out.exists())
                    self.assertFalse(self.data.exists())

    def written_result(self, stage, marker=None):
        """A result file as a stage writes it, with the outputs and the status line it lists."""
        self.out.mkdir(exist_ok=True)
        path = self.out / f"mode_A_BTC-USDT_{stage}_20261017.json"
        doc = {"files": [f"mode_A_BTC-USDT_{stage}_20261017_A_15m_NAIK.csv", path.name],
               "status_line": f"Status {stage}: -149.63184210526315"}
        if marker is not None:
            doc["marker"] = marker
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path, doc

    def test_each_stage_gets_the_paths_and_the_clock_and_its_outputs_are_printed(self):
        marker = str(self.data / "holdout_runs" / ("mode_A_BTC-USDT_" + "a" * 64 + ".json"))
        for command, name, marker_line in (("train", "train_stage", None),
                                           ("test", "test_stage", marker)):
            with self.subTest(command=command):
                path, doc = self.written_result(command, marker_line)
                with mock.patch.object(stages, name, return_value=path) as stage, \
                        mock.patch.object(cli, "now", return_value=CLOCK):
                    code, out, err = run_cli(self.stage_args(command))
                self.assertEqual(code, 0, err)
                self.assertEqual(err, "")
                stage.assert_called_once_with(CONFIG, self.data, self.out, "A", SYMBOL, CLOCK)
                expected = [str(self.out / name) for name in doc["files"]]
                expected += [marker_line] if marker_line else []
                self.assertEqual(out.splitlines(), expected + [doc["status_line"]])

    def test_a_lock_refusal_prints_every_problem_then_one_error_line_and_exits_2(self):
        error = LockError("the lock does not match the current files: 2 mismatches",
                          ("config_sha256: the lock has 1, the current files give 2",
                           "python: the lock has 3.10, this run is 3.11"))
        with mock.patch.object(stages, "test_stage", side_effect=error):
            code, out, err = run_cli(self.stage_args("test"))

        self.assertEqual(code, 2)
        self.assertEqual(out.splitlines(), list(error.problems))
        self.assertIn("2 mismatches", self.assert_one_error_line(err))

    def test_a_refusal_without_problems_is_one_error_line(self):
        with mock.patch.object(stages, "train_stage", side_effect=LockError("already ran")):
            code, out, err = run_cli(self.stage_args("train"))

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(self.assert_one_error_line(err), "error: already ran")

    def test_a_stage_that_cannot_run_exits_1(self):
        error = stages.StageError("the warm-up is too short")
        with mock.patch.object(stages, "train_stage", side_effect=error):
            code, out, err = run_cli(self.stage_args("train"))

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(self.assert_one_error_line(err), "error: the warm-up is too short")


class LiveStageCommandTests(CliTestCase):
    """``--mode L`` runs the live-exit stages of ``stages_live``; mode A keeps its Phase 1 path."""

    def live_args(self, command, mode="L", pair=SYMBOL, live_config=LIVE_CONFIG, config=CONFIG,
                  data=None, out=None):
        args = [command, "--mode", mode, "--pair", pair, "--config", str(config),
                "--data-dir", str(data or self.data), "--out-dir", str(out or self.out)]
        if live_config is not None:
            args += ["--live-config", str(live_config)]
        return args

    def written_result(self, stage, marker=None):
        self.out.mkdir(exist_ok=True)
        path = self.out / f"mode_L_BTC-USDT_{stage}_20261017.json"
        doc = {"files": [f"mode_L_BTC-USDT_{stage}_20261017_L_i_NAIK.csv", path.name],
               "status_line": f"Status L {stage}: LOLOS"}
        if marker is not None:
            doc["marker"] = marker
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path, doc

    def test_train_mode_l_dispatches_to_the_live_stage(self):
        marker = str(self.data / "holdout_runs" / ("mode_L_BTC-USDT_" + "b" * 64 + ".json"))
        for command, name, marker_line in (("train", "train_live_stage", None),
                                           ("test", "test_live_stage", marker)):
            with self.subTest(command=command):
                path, doc = self.written_result(command, marker_line)
                with mock.patch.object(stages_live, name, return_value=path) as live, \
                        mock.patch.object(stages, "train_stage") as phase1_train, \
                        mock.patch.object(stages, "test_stage") as phase1_test, \
                        mock.patch.object(cli, "now", return_value=CLOCK):
                    code, out, err = run_cli(self.live_args(command))
                self.assertEqual(code, 0, err)
                self.assertEqual(err, "")
                live.assert_called_once_with(CONFIG, LIVE_CONFIG, self.data, self.out, SYMBOL,
                                             CLOCK)
                phase1_train.assert_not_called()
                phase1_test.assert_not_called()
                expected = [str(self.out / name) for name in doc["files"]]
                expected += [marker_line] if marker_line else []
                self.assertEqual(out.splitlines(), expected + [doc["status_line"]])

    def test_mode_l_without_live_config_is_a_usage_error(self):
        for command in ("train", "test"):
            with self.subTest(command=command), \
                    mock.patch.object(stages_live, "train_live_stage") as train, \
                    mock.patch.object(stages_live, "test_live_stage") as test:
                code, out, err = run_cli(self.live_args(command, live_config=None))
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--live-config", self.assert_one_error_line(err))
                train.assert_not_called()
                test.assert_not_called()
                self.assertFalse(self.out.exists())

    def test_live_config_with_mode_a_is_a_usage_error(self):
        for command in ("train", "test"):
            with self.subTest(command=command), \
                    mock.patch.object(stages, "train_stage") as train, \
                    mock.patch.object(stages, "test_stage") as test:
                code, out, err = run_cli(self.live_args(command, mode="A"))
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("--live-config", self.assert_one_error_line(err))
                train.assert_not_called()
                test.assert_not_called()

    def test_mode_a_unchanged(self):
        for command, name in (("train", "train_stage"), ("test", "test_stage")):
            with self.subTest(command=command):
                self.out.mkdir(exist_ok=True)
                path = self.out / f"mode_A_BTC-USDT_{command}_20261017.json"
                path.write_text(json.dumps({"files": [path.name], "status_line": "Status A"}),
                                encoding="utf-8")
                with mock.patch.object(stages, name, return_value=path) as stage, \
                        mock.patch.object(stages_live, "train_live_stage") as live_train, \
                        mock.patch.object(stages_live, "test_live_stage") as live_test, \
                        mock.patch.object(cli, "now", return_value=CLOCK):
                    code, out, err = run_cli(self.live_args(command, mode="A", live_config=None))
                self.assertEqual(code, 0, err)
                stage.assert_called_once_with(CONFIG, self.data, self.out, "A", SYMBOL, CLOCK)
                live_train.assert_not_called()
                live_test.assert_not_called()
                self.assertEqual(out.splitlines(), [str(path), "Status A"])

    def test_other_modes_exit_1(self):
        for command in ("train", "test"):
            for mode in ("B", "C", "D", "l"):
                for live_config in (None, LIVE_CONFIG):
                    with self.subTest(command=command, mode=mode, live=live_config is not None):
                        code, out, err = run_cli(self.live_args(command, mode=mode,
                                                                live_config=live_config))
                        self.assertEqual(code, 1)
                        self.assertEqual(out, "")
                        self.assert_one_error_line(err)
                        self.assertFalse(self.out.exists())
                        self.assertFalse(self.data.exists())

    def test_mode_l_on_another_pair_exits_1_and_writes_nothing(self):
        for command in ("train", "test"):
            with self.subTest(command=command):
                code, out, err = run_cli(self.live_args(command, pair="ETH-USDT"))
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("BTC-USDT", self.assert_one_error_line(err))
                self.assertFalse(self.out.exists())
                self.assertFalse(self.data.exists())

    def test_a_lock_refusal_of_mode_l_exits_2(self):
        error = LockError("the lock does not match", ("live_config_sha256: locked 1, now 2",))
        with mock.patch.object(stages_live, "test_live_stage", side_effect=error):
            code, out, err = run_cli(self.live_args("test"))

        self.assertEqual(code, 2)
        self.assertEqual(out.splitlines(), list(error.problems))
        self.assert_one_error_line(err)


class LiveLockEndToEndTests(CliTestCase):
    """The real stages on the synthetic week: an edited live config makes ``test`` exit 2."""

    def test_test_after_the_live_config_was_edited_exits_2_and_leaves_no_record(self):
        config = write_stage_config(self.root / "modes.yaml")
        live = self.root / "modes_live_exit.yaml"
        shutil.copyfile(LIVE_CONFIG, live)
        write_naik_market(self.data)
        args = ["--mode", "L", "--pair", SYMBOL, "--config", str(config), "--live-config",
                str(live), "--data-dir", str(self.data), "--out-dir", str(self.out)]
        with mock.patch.object(cli, "now", return_value=STAGE_CLOCK):
            code, _out, err = run_cli(["train"] + args)
            self.assertEqual(code, 0, err)
            live.write_bytes(live.read_bytes() + b"# edited after train\n")
            code, out, err = run_cli(["test"] + args)

        self.assertEqual(code, 2)
        self.assertTrue(any(line.startswith("live_config_sha256") for line in out.splitlines()),
                        out)
        self.assert_one_error_line(err)
        self.assertEqual(list(self.out.glob("*_test_*")), [])
        self.assertFalse((self.data / "holdout_runs").exists())


if __name__ == "__main__":
    unittest.main()
