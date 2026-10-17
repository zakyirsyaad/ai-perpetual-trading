"""Tests for the mode backtest command line: exit codes, one error line, step0 and verify-copy."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from research.mode_backtest import cli, step0
from research.mode_backtest.step0 import PINE_FILES, ReproductionError
from tests.mode_backtest_helpers import sha256sum_text, write_days, write_warmups

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "modes.yaml"
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


if __name__ == "__main__":
    unittest.main()
