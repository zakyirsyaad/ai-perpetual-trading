import argparse
import asyncio
import contextlib
import gzip
import hashlib
import io
import json
import logging
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from research.pine_gate import cli, cli_common, cli_stages
from research.pine_gate import lock as pine_lock
from research.pine_gate.bars import (
    day_path,
    funding_path,
    read_day,
    warmup_path,
    wib_day_start_ms,
    write_day,
)
from research.pine_gate.pipeline import TRAIN_DATA_UNTIL_MS, latencies_sha256
from research.pine_gate.spec import spec_document, spec_sha256

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
FIRST_DAY = date(2025, 8, 1)  # the training start's WIB day
NOW = datetime(2026, 10, 14, 3, 0, tzinfo=timezone.utc)  # 10:00 WIB


def setUpModule():
    logging.disable(logging.CRITICAL)


def tearDownModule():
    logging.disable(logging.NOTSET)


def run_cli(argv):
    """Run ``cli.main`` and return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)


def kline_rows(params):
    start, end = int(params["startTime"]), int(params["endTime"])
    step = {"1m": MIN, "1h": HOUR, "4h": 4 * HOUR}[params["interval"]]
    return [
        {"time": t, "open": "100", "high": "102", "low": "99", "close": "101", "volume": "5"}
        for t in range(start, end + 1, step)
    ]


class FakeHttp:
    """Answers klines and funding requests; ``fail_symbols`` get an exchange error."""

    def __init__(self, fail_symbols=(), funding=()):
        self.calls = []
        self.fail_symbols = set(fail_symbols)
        self.funding = list(funding)

    def __call__(self, url, params, headers):
        self.calls.append((url, dict(params), dict(headers)))
        if params["symbol"] in self.fail_symbols:
            return {"code": 109400, "msg": "boom", "data": None}
        if url.endswith("/fundingRate"):
            lo, hi = int(params["startTime"]), int(params["endTime"])
            return {"code": 0, "data": [{"fundingTime": t} for t in self.funding if lo <= t <= hi]}
        return {"code": 0, "data": kline_rows(params)}


def synthetic_rows(start_ms, n, seed=7, base=30_000.0):
    rng = random.Random(seed)
    rows, price = [], base
    for k in range(n):
        o = price
        c = max(1.0, o * (1 + rng.gauss(0, 0.0008)))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.0003)))
        low = min(o, c) * (1 - abs(rng.gauss(0, 0.0003)))
        rows.append((start_ms + k * MIN, o, h, low, c, 1.0 + rng.random()))
        price = c
    return rows


def write_prod_days(data_dir, symbol, rows):
    by_day = {}
    for row in rows:
        day = datetime.fromtimestamp(row[0] / 1000, timezone(timedelta(hours=7))).date()
        by_day.setdefault(day, []).append(row)
    for day, day_rows in by_day.items():
        write_day(day_path(data_dir, "prod", symbol, "1m", day), day_rows)


class Dirs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.data = root / "data"
        self.out = root / "out"
        self.data.mkdir()
        self.out.mkdir()
        patcher = mock.patch.object(cli_common, "now", lambda: NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def base_args(self):
        return ["--data-dir", str(self.data), "--out-dir", str(self.out)]


# --------------------------------------------------------------------------- spec


class SpecCommandTest(Dirs):
    def test_spec_command_writes_document_and_prints_sha(self):
        path = self.root / "pine_gate_spec.json"
        code, out, err = run_cli(["spec", "--write", str(path)])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, spec_sha256() + "\n")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), spec_document())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), spec_sha256())

    def test_spec_refuses_to_overwrite(self):
        path = self.root / "pine_gate_spec.json"
        path.write_text("keep me", encoding="utf-8")
        code, out, err = run_cli(["spec", "--write", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn(str(path), err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(path.read_text(encoding="utf-8"), "keep me")


# --------------------------------------------------------------------------- holdout lock


def calibration_doc(latencies=(1000.0, 2000.0), s_star=1.0):
    return {
        "s_star_bps": s_star,
        "n_stops": 0,
        "n_skipped": 0,
        "latencies_ms": list(latencies),
        "latencies_sha256": latencies_sha256(latencies),
    }


class TestCommandTest(Dirs):
    def write_calibration(self, doc=None):
        path = self.root / "calibration.json"
        path.write_text(json.dumps(doc or calibration_doc()), encoding="utf-8")
        return path

    def test_test_command_refuses_without_selection(self):
        missing = self.out / "pine_gate_selection_20260101.json"
        calib = self.write_calibration()
        with mock.patch.object(cli_stages, "load_dataset",
                               side_effect=AssertionError("holdout data loaded")) as load:
            code, _, err = run_cli(self.base_args() + [
                "test", "--selection", str(missing), "--calibration", str(calib)])
        self.assertNotEqual(code, 0)
        self.assertIn(str(missing), err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        load.assert_not_called()

    def test_test_command_refuses_sha_mismatched_selection(self):
        selection = self.out / "pine_gate_selection_20260101.json"
        selection.write_text(json.dumps({"spec_sha256": "0" * 64}), encoding="utf-8")
        calib = self.write_calibration()
        with mock.patch.object(cli_stages, "load_dataset",
                               side_effect=AssertionError("holdout data loaded")) as load:
            code, _, err = run_cli(self.base_args() + [
                "test", "--selection", str(selection), "--calibration", str(calib)])
        self.assertEqual(code, 2)
        self.assertIn("does not match", err)
        self.assertNotIn("Traceback", err)
        load.assert_not_called()

    def test_test_command_checks_lock_before_loading_then_runs_with_selection_seed(self):
        selection_path = self.out / "pine_gate_selection_20260101.json"
        calib = self.write_calibration()
        cal = calibration_doc()
        selection = {"seed": 3, "s_star_bps": cal["s_star_bps"], "n_latencies": 2,
                     "latencies_sha256": cal["latencies_sha256"],
                     "code_sha256": pine_lock.code_sha256(), "c0_training_pass": True}
        order = []

        def lock(path, sha):
            order.append(("lock", Path(path), sha))
            return selection

        def load(data_dir, until_ms):
            order.append(("load", until_ms))
            return "dataset"

        def stage(dataset, path, calib_obj, out_dir, sha, seed):
            order.append(("stage", dataset, seed, calib_obj.latencies_ms))
            return Path(out_dir) / "pine_gate_holdout_20261014.json"

        with mock.patch.object(cli_stages, "load_selection_checked", lock), \
                mock.patch.object(cli_stages, "load_dataset", load), \
                mock.patch.object(cli_stages, "test_stage", stage):
            code, out, err = run_cli(self.base_args() + [
                "test", "--selection", str(selection_path), "--calibration", str(calib)])
        self.assertEqual(code, 0, err)
        self.assertEqual([step[0] for step in order], ["lock", "load", "stage"])
        self.assertEqual(order[0][2], spec_sha256())
        self.assertIsNone(order[1][1])
        self.assertEqual(order[2][2], 3)
        self.assertEqual(order[2][3], (1000.0, 2000.0))
        self.assertIn("pine_gate_holdout_20261014.json", out)

    def test_test_command_refuses_when_a_holdout_file_exists_before_loading(self):
        (self.out / "pine_gate_holdout_20261001.json").write_text("{}", encoding="utf-8")
        calib = self.write_calibration()
        cal = calibration_doc()
        selection = {"seed": 0, "s_star_bps": cal["s_star_bps"], "n_latencies": 2,
                     "latencies_sha256": cal["latencies_sha256"]}
        with mock.patch.object(cli_stages, "load_selection_checked", lambda p, s: selection), \
                mock.patch.object(cli_stages, "load_dataset",
                                  side_effect=AssertionError("holdout data loaded")) as load:
            code, _, err = run_cli(self.base_args() + [
                "test", "--selection", str(self.out / "sel.json"), "--calibration", str(calib)])
        self.assertEqual(code, 1)
        self.assertIn("holdout file already exists", err)
        load.assert_not_called()

    def test_test_command_refuses_other_code_or_a_failed_training_c0_before_loading(self):
        calib = self.write_calibration()
        cal = calibration_doc()
        base = {"seed": 0, "s_star_bps": cal["s_star_bps"], "n_latencies": 2,
                "latencies_sha256": cal["latencies_sha256"],
                "code_sha256": pine_lock.code_sha256(), "c0_training_pass": True}
        cases = ((dict(base, code_sha256="f" * 64), "code sha256"),
                 (dict(base, c0_training_pass=False), "fill model invalid on training C0"))
        for selection, text in cases:
            with self.subTest(text=text), \
                    mock.patch.object(cli_stages, "load_selection_checked",
                                      lambda p, s, doc=selection: doc), \
                    mock.patch.object(cli_stages, "load_dataset",
                                      side_effect=AssertionError("holdout data loaded")) as load:
                code, _, err = run_cli(self.base_args() + [
                    "test", "--selection", str(self.out / "sel.json"),
                    "--calibration", str(calib)])
                self.assertEqual(code, 2, err)
                self.assertIn(text, err)
                load.assert_not_called()

    def test_calibration_with_a_wrong_latency_hash_is_refused(self):
        doc = calibration_doc()
        doc["latencies_sha256"] = "f" * 64
        calib = self.write_calibration(doc)
        with mock.patch.object(cli_stages, "load_dataset",
                               side_effect=AssertionError("data loaded")) as load:
            code, _, err = run_cli(self.base_args() + ["train", "--calibration", str(calib)])
        self.assertEqual(code, 1)
        self.assertIn("latencies_sha256", err)
        load.assert_not_called()


class TrainCommandTest(Dirs):
    def test_train_loads_training_data_and_prints_selection_path(self):
        calib = self.root / "calibration.json"
        calib.write_text(json.dumps(calibration_doc(s_star=1.5)), encoding="utf-8")
        seen = {}

        def load(data_dir, until_ms):
            seen["load"] = (Path(data_dir), until_ms)
            return "dataset"

        def stage(dataset, calib_obj, out_dir, sha):
            seen["stage"] = (dataset, calib_obj, Path(out_dir), sha)
            return Path(out_dir) / "pine_gate_selection_20261014.json"

        with mock.patch.object(cli_stages, "load_dataset", load), \
                mock.patch.object(cli_stages, "train_stage", stage):
            code, out, err = run_cli(self.base_args() + ["train", "--calibration", str(calib)])
        self.assertEqual(code, 0, err)
        self.assertEqual(seen["load"], (self.data, TRAIN_DATA_UNTIL_MS))
        dataset, calib_obj, out_dir, sha = seen["stage"]
        self.assertEqual(calib_obj.s_star_bps, 1.5)
        self.assertEqual(calib_obj.latencies_ms, (1000.0, 2000.0))
        self.assertEqual((out_dir, sha), (self.out, spec_sha256()))
        self.assertEqual(out.strip(), str(self.out / "pine_gate_selection_20261014.json"))


# --------------------------------------------------------------------------- data commands


class FetchCommandTest(Dirs):
    def test_fetch_prints_counts_with_fake_http(self):
        day1, day2 = date(2026, 7, 1), date(2026, 7, 2)
        existing = day_path(self.data, "prod", "ETH-USDT", "1m", day1)
        write_day(existing, [(wib_day_start_ms(day1), 1.0, 1.0, 1.0, 1.0, 0.0)])
        http, sleeps = FakeHttp(), Sleeps()
        with mock.patch.object(cli_common, "http_get", http), \
                mock.patch.object(cli_common, "sleep", sleeps):
            code, out, err = run_cli(self.base_args() + [
                "fetch", "--source", "prod", "--symbols", "BTC-USDT,ETH-USDT",
                "--start", "2026-07-01", "--end", "2026-07-02"])
        self.assertEqual(code, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(lines, [
            "prod/BTC-USDT/1m 2026-07-01..2026-07-02: fetched 2, skipped 0, failed 0",
            "prod/ETH-USDT/1m 2026-07-01..2026-07-02: fetched 1, skipped 1, failed 0",
        ])
        self.assertEqual(len(read_day(day_path(self.data, "prod", "BTC-USDT", "1m", day2))), 1440)
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(all(c[0].startswith("https://open-api.bingx.com/") for c in http.calls))
        # one second before every request after the first, across symbols too
        self.assertEqual(sleeps.calls, [1.0] * (len(http.calls) - 1))

    def test_fetch_reports_failed_days_and_exits_1(self):
        with mock.patch.object(cli_common, "http_get", FakeHttp(fail_symbols={"ETH-USDT"})), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, _ = run_cli(self.base_args() + [
                "fetch", "--source", "vst", "--symbols", "ETH-USDT",
                "--start", "2026-07-01", "--end", "2026-07-01"])
        self.assertEqual(code, 1)
        self.assertIn("vst/ETH-USDT/1m 2026-07-01..2026-07-01: fetched 0, skipped 0, failed 1; "
                      "failed: 2026-07-01", out)

    def test_fetch_rejects_a_symbol_that_is_not_a_usdt_pair(self):
        code, _, err = run_cli(self.base_args() + [
            "fetch", "--source", "prod", "--symbols", "../etc",
            "--start", "2026-07-01", "--end", "2026-07-01"])
        self.assertEqual(code, 2)
        self.assertIn("symbol", err)


class WarmupCommandTest(Dirs):
    def test_warmup_writes_both_intervals_on_the_utc_grid(self):
        with mock.patch.object(cli_common, "http_get", FakeHttp()), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, err = run_cli(self.base_args() + [
                "warmup", "--start", "2025-07-30", "--end", "2025-08-01"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 4)
        start = wib_day_start_ms(date(2025, 7, 30))
        end = wib_day_start_ms(date(2025, 8, 1))
        one_h = read_day(warmup_path(self.data, "BTC-USDT", "1h"))
        self.assertEqual((one_h[0][0], one_h[-1][0]), (start, end - HOUR))
        four_h = read_day(warmup_path(self.data, "ETH-USDT", "4h"))
        # bars whose open time lies in [start, end): 4h bars sit on the UTC grid
        self.assertEqual(four_h[0][0], -(-start // (4 * HOUR)) * 4 * HOUR)
        self.assertEqual(four_h[-1][0], (end - 1) // (4 * HOUR) * 4 * HOUR)

    def test_warmup_never_overwrites_an_existing_file(self):
        path = warmup_path(self.data, "BTC-USDT", "1h")
        write_day(path, [(0, 1.0, 1.0, 1.0, 1.0, 0.0)])
        with mock.patch.object(cli_common, "http_get", FakeHttp()), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, _ = run_cli(self.base_args() + [
                "warmup", "--start", "2025-07-30", "--end", "2025-08-01"])
        self.assertEqual(code, 0)
        self.assertIn("prod/BTC-USDT/1h warmup: skipped, file exists (covers "
                      "1970-01-01T07:00:00+07:00 .. 1970-01-01T07:00:00+07:00, 1 bars)", out)
        self.assertEqual(read_day(path), [(0, 1.0, 1.0, 1.0, 1.0, 0.0)])


class NativeCheckFetchCommandTest(Dirs):
    def test_native_check_fetch_writes_native_bars_and_skips_with_coverage(self):
        from research.pine_gate import cli_data

        with mock.patch.object(cli_common, "http_get", FakeHttp()), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, err = run_cli(self.base_args() + [
                "native-check-fetch", "--start", "2025-08-01", "--end", "2025-08-03"])
        self.assertEqual(code, 0, err)
        start = wib_day_start_ms(date(2025, 8, 1))
        end = wib_day_start_ms(date(2025, 8, 3))
        for symbol in ("BTC-USDT", "ETH-USDT"):
            rows = read_day(cli_data.native_check_path(self.data, symbol, "1h"))
            self.assertEqual((rows[0][0], rows[-1][0]), (start, end - HOUR))
            self.assertTrue(cli_data.native_check_path(self.data, symbol, "4h").exists())
        self.assertFalse(warmup_path(self.data, "BTC-USDT", "1h").exists())
        with mock.patch.object(cli_common, "http_get", FakeHttp()), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, _ = run_cli(self.base_args() + ["native-check-fetch"])
        self.assertEqual(code, 0)
        self.assertIn("prod/BTC-USDT/1h native check: skipped, file exists (covers "
                      "2025-08-01T00:00:00+07:00 .. 2025-08-02T23:00:00+07:00, 48 bars)", out)

    def test_native_check_fetch_defaults_to_august_2025(self):
        from research.pine_gate import cli

        args = cli.build_parser().parse_args(["native-check-fetch"])
        self.assertEqual((args.start, args.end), (date(2025, 8, 1), date(2025, 9, 1)))


class FundingCommandTest(Dirs):
    def test_funding_writes_funding_path(self):
        start = wib_day_start_ms(date(2026, 7, 1))
        times = [start, start + 8 * HOUR, start + 16 * HOUR, start + DAY]
        with mock.patch.object(cli_common, "http_get", FakeHttp(funding=times)), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, err = run_cli(self.base_args() + [
                "funding", "--start", "2026-07-01", "--end", "2026-07-02"])
        self.assertEqual(code, 0, err)
        for symbol in ("BTC-USDT", "ETH-USDT"):
            saved = json.loads(funding_path(self.data, symbol).read_text(encoding="utf-8"))
            self.assertEqual(saved, times[:3])  # --end is exclusive
            self.assertIn(f"prod/{symbol} funding: 3 times", out)
        with mock.patch.object(cli_common, "http_get", FakeHttp(funding=times)), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, _ = run_cli(self.base_args() + [
                "funding", "--start", "2026-07-01", "--end", "2026-07-03"])
        self.assertEqual(code, 0)
        self.assertIn("prod/BTC-USDT funding: skipped, file exists (covers "
                      "2026-07-01T00:00:00+07:00 .. 2026-07-01T16:00:00+07:00, 3 times)", out)


class CollectCommandTest(Dirs):
    def test_collect_backfills_the_last_week_and_exits_1_when_a_job_fails(self):
        http = FakeHttp(fail_symbols={"ETH-USDT"})
        with mock.patch.object(cli_common, "http_get", http), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, _ = run_cli(self.base_args() + ["collect"])
        self.assertEqual(code, 1)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[1], "prod/BTC-USDT/1m 2026-10-07..2026-10-13: fetched 7, "
                                   "skipped 0, failed 0")
        self.assertIn("failed: 2026-10-07", lines[0])
        for k in range(7):
            day = date(2026, 10, 7) + timedelta(days=k)
            self.assertTrue(day_path(self.data, "prod", "BTC-USDT", "1m", day).exists())
        with mock.patch.object(cli_common, "http_get", FakeHttp()), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, _ = run_cli(self.base_args() + ["collect"])
        self.assertEqual(code, 0, out)

    def test_a_rate_limit_stops_fetch_with_one_error_line(self):
        from research.pine_gate.fetch import RateLimitedError

        def limited(url, params, headers):
            raise RateLimitedError("HTTP 429 from open-api.bingx.com; Retry-After '30'")

        with mock.patch.object(cli_common, "http_get", limited), \
                mock.patch.object(cli_common, "sleep", Sleeps()):
            code, out, err = run_cli(self.base_args() + [
                "fetch", "--source", "prod", "--symbols", "BTC-USDT,ETH-USDT",
                "--start", "2026-07-01", "--end", "2026-07-03"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("HTTP 429", err)
        self.assertIn("Retry-After", err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_symbols_must_match_the_whole_pattern(self):
        for text in ("BTC-USDT\n", "BTC-USDT ", "xBTC-USDT"):
            with self.subTest(text=text):
                with self.assertRaises(argparse.ArgumentTypeError):
                    cli._symbol(text)


class CheckDataCommandTest(Dirs):
    def test_check_data_reports_a_missing_day(self):
        code, out, _ = run_cli(self.base_args() + ["check-data", "--truncation-times", "2"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL complete days", out)
        self.assertIn("prod/BTC-USDT/1m missing 439 days", out)
        self.assertIn("2025-08-01", out)

    def test_native_check_fails_without_the_native_check_file(self):
        write_prod_days(self.data, "BTC-USDT",
                        synthetic_rows(wib_day_start_ms(FIRST_DAY), 2 * 1440))
        from research.pine_gate import cli_data

        line, ok = cli_data.native_check(self.data, "BTC-USDT", "1h")
        self.assertFalse(ok)
        self.assertTrue(line.startswith("FAIL"), line)
        self.assertIn("native-check-fetch", line)

    def test_native_check_without_overlap_fails(self):
        write_prod_days(self.data, "BTC-USDT",
                        synthetic_rows(wib_day_start_ms(FIRST_DAY), 2 * 1440))
        from research.pine_gate import cli_data

        start = wib_day_start_ms(FIRST_DAY) + 5 * DAY  # no 1m day file covers it
        write_day(cli_data.native_check_path(self.data, "BTC-USDT", "1h"),
                  [(start, 1.0, 1.0, 1.0, 1.0, 1.0)])
        line, ok = cli_data.native_check(self.data, "BTC-USDT", "1h")
        self.assertFalse(ok)
        self.assertTrue(line.startswith("FAIL"), line)

    def test_native_check_passes_when_native_bars_match(self):
        rows = synthetic_rows(wib_day_start_ms(FIRST_DAY), 2 * 1440)
        write_prod_days(self.data, "BTC-USDT", rows)
        from research.pine_gate import cli_data
        from research.pine_gate.bars import aggregate, bars_from_rows

        agg = aggregate(bars_from_rows(rows, MIN), HOUR)
        native = [(agg.open_ms[i], agg.open[i], agg.high[i], agg.low[i], agg.close[i],
                   agg.volume[i]) for i in range(len(agg))]
        write_day(cli_data.native_check_path(self.data, "BTC-USDT", "1h"), native)
        line, ok = cli_data.native_check(self.data, "BTC-USDT", "1h")
        self.assertTrue(ok, line)
        self.assertTrue(line.startswith("PASS"), line)
        self.assertIn(f"of {len(agg)} bars", line)

    def test_native_check_fails_when_a_native_bar_differs(self):
        rows = synthetic_rows(wib_day_start_ms(FIRST_DAY), 2 * 1440)
        write_prod_days(self.data, "BTC-USDT", rows)
        from research.pine_gate import cli_data
        from research.pine_gate.bars import aggregate, bars_from_rows

        agg = aggregate(bars_from_rows(rows, MIN), HOUR)
        native = [(agg.open_ms[i], agg.open[i], agg.high[i], agg.low[i], agg.close[i],
                   agg.volume[i] * (2.0 if i == 0 else 1.0)) for i in range(len(agg))]
        write_day(cli_data.native_check_path(self.data, "BTC-USDT", "1h"), native)
        line, ok = cli_data.native_check(self.data, "BTC-USDT", "1h")
        self.assertFalse(ok)
        self.assertTrue(line.startswith("FAIL"), line)

    def test_truncation_times_must_be_positive(self):
        for value in ("0", "-3"):
            code, _, err = run_cli(self.base_args() + ["check-data", "--truncation-times", value])
            self.assertEqual(code, 2, err)
            self.assertIn("positive", err)

    def test_truncation_check_on_real_files(self):
        rows = synthetic_rows(wib_day_start_ms(FIRST_DAY), 2 * 1440)
        write_prod_days(self.data, "BTC-USDT", rows)
        from research.pine_gate import cli_data

        line, ok = cli_data.truncation_check(
            {"BTC-USDT": cli_data.load_training_series(self.data, "BTC-USDT", date(2025, 8, 2))},
            times=3, seed=0)
        self.assertTrue(ok, line)
        self.assertTrue(line.startswith("PASS"), line)


# --------------------------------------------------------------------------- calibrate


class FakeSettings:
    supabase_trades_table = "trades"
    supabase_ai_decisions_table = "ai_decisions"
    supabase_ai_call_metrics_table = "ai_call_metrics"


def reader(rows):
    """A stand-in for ``cli_common.make_get_rows``: an async context manager yielding ``rows``."""

    @contextlib.asynccontextmanager
    async def make(settings):
        yield rows

    return make


class FakeRows:
    """Async get_rows: one page per table, then empty pages; records the params."""

    def __init__(self, tables):
        self.tables = tables
        self.calls = []

    async def __call__(self, table, params):
        self.calls.append((table, list(params)))
        offset = int(dict(params).get("offset", "0"))
        return list(self.tables.get(table, [])) if offset == 0 else []


class CalibrateCommandTest(Dirs):
    def test_calibrate_writes_the_calibration_file_and_refuses_overwrite(self):
        rows = FakeRows({"ai_call_metrics": [{"created_at": "2026-09-26T00:00:00Z",
                                              "latency_ms": 1500.0},
                                             {"created_at": "2026-09-27T00:00:00Z",
                                              "latency_ms": 2500}]})
        with mock.patch.object(cli_common, "make_settings", FakeSettings), \
                mock.patch.object(cli_common, "make_get_rows", reader(rows)):
            code, out, err = run_cli(self.base_args() + ["calibrate"])
            self.assertEqual(code, 0, err)
            path = self.out / "pine_gate_calibration_20261014.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(doc["latencies_ms"], [1500.0, 2500.0])
            self.assertEqual(doc["latencies_sha256"], latencies_sha256([1500.0, 2500.0]))
            self.assertEqual((doc["n_stops"], doc["n_skipped"]), (0, 0))
            self.assertIn("n_latencies 2", out)
            code, _, err = run_cli(self.base_args() + ["calibrate"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_settings_failure_prints_only_the_error_type(self):
        def broken():
            raise RuntimeError("SECRET_VALUE_xyz must be set")

        with mock.patch.object(cli_common, "make_settings", broken):
            code, _, err = run_cli(self.base_args() + ["calibrate"])
        self.assertEqual(code, 1)
        self.assertNotIn("SECRET_VALUE_xyz", err)
        self.assertIn("RuntimeError", err)


# --------------------------------------------------------------------------- checks


def prod_and_vst(test, days=8, vst_from_day=1):
    rows = synthetic_rows(wib_day_start_ms(FIRST_DAY), days * 1440)
    write_prod_days(test.data, "BTC-USDT", rows)
    vst = rows[vst_from_day * 1440:]
    edge = test.root / "edge_klines.json"
    edge.write_text(json.dumps({"k1": {str(r[0]): [r[1], r[2], r[3], r[4]] for r in vst},
                                "k3": {}}), encoding="utf-8")
    return rows, edge


SELECTION = {"spec_sha256": "x", "contender1": "E200|P35|OFF|OFF|OFF", "s_star_bps": 1.0,
             "primary_rule": None, "meta_model": None}


def write_lock_files(test, chosen="c1", **overrides):
    """A selection file ``s.json`` and a holdout file made from it under the current spec."""
    selection = test.out / "s.json"
    selection.write_text(json.dumps(SELECTION), encoding="utf-8")
    holdout = {"chosen": chosen, "spec_sha256": spec_sha256(), "selection_file": "s.json",
               "selection_sha256": hashlib.sha256(selection.read_bytes()).hexdigest()}
    holdout.update(overrides)
    (test.out / "pine_gate_holdout_20261014.json").write_text(json.dumps(holdout),
                                                               encoding="utf-8")
    return selection


class CheckLockTest(Dirs):
    """vst-check and decisions score holdout-period outcomes: only after ``test`` ran."""

    def patches(self):
        from research.pine_gate import cli_checks

        no_data = AssertionError("data loaded")
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(cli_checks, "load_selection_checked",
                                              lambda p, s: SELECTION))
        self.prod_bars = stack.enter_context(
            mock.patch.object(cli_checks, "prod_bars", side_effect=no_data))
        self.edge = stack.enter_context(
            mock.patch.object(cli_checks, "load_edge_klines_vst", side_effect=no_data))
        self.settings = stack.enter_context(
            mock.patch.object(cli_common, "make_settings", side_effect=no_data))
        return stack

    def run_both(self, extra=()):
        selection = str(self.out / "s.json")
        results = []
        with self.patches():
            for argv in (["vst-check", "--symbol", "BTC-USDT", "--selection", selection],
                         ["decisions", "--selection", selection]):
                results.append(run_cli(self.base_args() + argv + list(extra)))
            self.prod_bars.assert_not_called()
            self.edge.assert_not_called()
            self.settings.assert_not_called()
        return results

    def assert_refused(self, results, text):
        for code, out, err in results:
            self.assertEqual(code, 2, err)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn(text, err)
            self.assertNotIn("Traceback", err)

    def test_contender_flag_without_a_holdout_file_is_refused(self):
        (self.out / "s.json").write_text(json.dumps(SELECTION), encoding="utf-8")
        self.assert_refused(self.run_both(["--contender", "c1"]), "no holdout file")

    def test_holdout_of_another_spec_is_refused(self):
        write_lock_files(self, spec_sha256="0" * 64)
        self.assert_refused(self.run_both(["--contender", "c1"]), "spec_sha256")

    def test_holdout_of_another_selection_file_is_refused(self):
        write_lock_files(self, selection_file="pine_gate_selection_20260101.json")
        self.assert_refused(self.run_both(), "selection_file")

    def test_holdout_of_a_changed_selection_file_is_refused(self):
        write_lock_files(self, selection_sha256="f" * 64)
        self.assert_refused(self.run_both(), "selection_sha256")


class VstCheckCommandTest(Dirs):
    def test_vst_check_writes_the_agreement_for_the_chosen_contender(self):
        _, edge = prod_and_vst(self)
        write_lock_files(self, chosen="c1")
        from research.pine_gate import cli_checks

        with mock.patch.object(cli_checks, "load_selection_checked", lambda p, s: SELECTION):
            code, out, err = run_cli(self.base_args() + [
                "vst-check", "--symbol", "BTC-USDT", "--selection", str(self.out / "s.json"),
                "--edge-klines", str(edge)])
        self.assertEqual(code, 0, err)
        path = self.out / "pine_gate_vst_BTC-USDT_20261014.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(set(doc), {"n", "side_agree", "outcome_agree", "wr_prod", "wr_vst",
                                    "passed"})
        self.assertGreater(doc["n"], 100)
        self.assertEqual(doc["side_agree"], 1.0)  # identical prices on both sides
        self.assertIn("contender c1", out)

    def test_vst_check_needs_a_contender_when_the_holdout_chose_none(self):
        write_lock_files(self, chosen=None)
        from research.pine_gate import cli_checks

        with mock.patch.object(cli_checks, "load_selection_checked", lambda p, s: SELECTION):
            code, _, err = run_cli(self.base_args() + [
                "vst-check", "--symbol", "BTC-USDT", "--selection", str(self.out / "s.json")])
        self.assertEqual(code, 1)
        self.assertIn("--contender", err)


class DecisionsCommandTest(Dirs):
    def test_decisions_writes_buckets_with_linked_and_fallback_latency(self):
        rows, _ = prod_and_vst(self, days=8)
        base = wib_day_start_ms(FIRST_DAY) + 7 * DAY

        def iso(t):
            return datetime.fromtimestamp(t / 1000, timezone.utc).isoformat()

        decisions = [
            {"id": 1, "created_at": iso(base + 3 * HOUR), "symbol": "BTC/USDT",
             "action": "OPEN_LONG", "confidence": 0.8,
             "pj_decision": {"risk_review": {"approved": True}}, "pj_ai_call_id": "call-1"},
            {"id": 2, "created_at": iso(base + 5 * HOUR), "symbol": "BTC/USDT",
             "action": "HOLD", "confidence": 0.5, "payload_json": {"ai_call_id": None}},
            {"id": 3, "created_at": iso(base + 30 * DAY), "symbol": "BTC/USDT",
             "action": "HOLD", "confidence": 0.5, "payload_json": {}},
        ]
        metrics = [{"call_id": "call-1", "latency_ms": 30_000.0, "purpose": "entry",
                    "success": True},
                   {"call_id": "call-2", "latency_ms": 10_000.0, "purpose": "entry",
                    "success": True}]
        fake = FakeRows({"ai_decisions": decisions, "ai_call_metrics": metrics})
        write_lock_files(self, chosen="c2")
        from research.pine_gate import cli_checks

        with mock.patch.object(cli_checks, "load_selection_checked", lambda p, s: SELECTION), \
                mock.patch.object(cli_common, "make_settings", FakeSettings), \
                mock.patch.object(cli_common, "make_get_rows", reader(fake)):
            code, out, err = run_cli(self.base_args() + [
                "decisions", "--selection", str(self.out / "s.json")])
        self.assertEqual(code, 0, err)
        doc = json.loads((self.out / "pine_gate_decisions_20261014.json").read_text("utf-8"))
        self.assertEqual(doc["contender"], "c2")
        self.assertEqual((doc["n_rows"], doc["n_outside_data"]), (2, 1))
        self.assertEqual(doc["n_fallback_latency"], 1)
        self.assertEqual(set(doc["buckets"]), {"A", "B", "E", "F"})
        self.assertEqual(doc["buckets"]["A"]["n"], 1)
        decision_params = dict(next(p for t, p in fake.calls if t == "ai_decisions"))
        self.assertEqual(decision_params["exchange"], "eq.bingx")
        self.assertIn("pj_decision:payload_json->decision", decision_params["select"])
        self.assertIn("pj_ai_call_id:payload_json->ai_call_id", decision_params["select"])
        self.assertNotIn(",payload_json,", decision_params["select"] + ",")
        self.assertTrue(decision_params["created_at"].startswith("gte.2026-08-31"))
        self.assertIn("decisions", out)


# --------------------------------------------------------------------------- report


class ReportCommandTest(Dirs):
    def test_report_surfaces_not_implemented_as_one_line(self):
        write_lock_files(self)
        code, out, err = run_cli(self.base_args() + ["report"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn("verdict_sentence", err)
        self.assertNotIn("Traceback", err)

    def test_report_refuses_without_a_holdout_file(self):
        (self.out / "pine_gate_selection_20261014.json").write_text("{}", encoding="utf-8")
        code, _, err = run_cli(self.base_args() + ["report"])
        self.assertEqual(code, 1)
        self.assertIn("no holdout file", err)

    def test_report_refuses_when_the_holdout_selection_file_is_missing(self):
        (self.out / "pine_gate_holdout_20261014.json").write_text(
            json.dumps({"selection_file": "pine_gate_selection_20261001.json"}), encoding="utf-8")
        code, _, err = run_cli(self.base_args() + ["report"])
        self.assertEqual(code, 1)
        self.assertIn("pine_gate_selection_20261001.json", err)

    def test_report_defaults_to_the_selection_the_holdout_used(self):
        used = self.out / "pine_gate_selection_20261001.json"
        used.write_text(json.dumps({"data_sha256": "used"}), encoding="utf-8")
        (self.out / "pine_gate_selection_20261014.json").write_text(
            json.dumps({"data_sha256": "newer"}), encoding="utf-8")
        (self.out / "pine_gate_holdout_20261014.json").write_text(
            json.dumps({"selection_file": used.name}), encoding="utf-8")
        captured = {}

        def fake_write(results, out_dir):
            captured["results"] = results
            return Path(out_dir) / "r.md", Path(out_dir) / "r.json"

        with mock.patch.object(cli_stages, "write_report", fake_write):
            code, _, err = run_cli(self.base_args() + ["report"])
        self.assertEqual(code, 0, err)
        self.assertEqual(captured["results"]["selection"], {"data_sha256": "used"})
        self.assertEqual(captured["results"]["sha256"]["data"], "used")

    def test_report_gathers_the_newest_files_and_their_hashes(self):
        older = self.out / "pine_gate_selection_20261001.json"
        older.write_text(json.dumps({"data_sha256": "old"}), encoding="utf-8")
        newest = self.out / "pine_gate_selection_20261014.json"
        newest.write_text(json.dumps({"data_sha256": "abc"}), encoding="utf-8")
        holdout = self.out / "pine_gate_holdout_20261014.json"
        holdout.write_text(json.dumps({"chosen": "c2", "selection_file": newest.name,
                                       "data_sha256": "def"}), encoding="utf-8")
        (self.out / "pine_gate_vst_BTC-USDT_20261014.json").write_text(
            json.dumps({"n": 5, "passed": True}), encoding="utf-8")
        (self.out / "pine_gate_decisions_20261014.json").write_text(
            json.dumps({"buckets": {"A": {"n": 1}}}), encoding="utf-8")
        captured = {}

        def fake_write(results, out_dir):
            captured["results"] = results
            return Path(out_dir) / "r.md", Path(out_dir) / "r.json"

        with mock.patch.object(cli_stages, "write_report", fake_write):
            code, out, err = run_cli(self.base_args() + ["report"])
        self.assertEqual(code, 0, err)
        results = captured["results"]
        self.assertEqual(results["selection"], {"data_sha256": "abc"})
        self.assertEqual(results["holdout"], {"chosen": "c2", "selection_file": newest.name,
                                              "data_sha256": "def"})
        self.assertEqual(results["vst"], {"BTC-USDT": {"n": 5, "passed": True}})
        self.assertEqual(results["decisions"], {"A": {"n": 1}})
        sha = results["sha256"]
        self.assertEqual(sha["spec"], spec_sha256())
        self.assertEqual(sha["data"], "abc")
        self.assertEqual(sha["data_holdout"], "def")
        self.assertEqual(sha["selection"], hashlib.sha256(newest.read_bytes()).hexdigest())
        self.assertEqual(sha["holdout"], hashlib.sha256(holdout.read_bytes()).hexdigest())
        self.assertEqual(len(sha["code"]), 64)
        self.assertEqual(out.strip().splitlines(), [str(self.out / "r.md"),
                                                    str(self.out / "r.json")])


class CodeHashTest(unittest.TestCase):
    def test_code_hash_covers_the_research_package_and_pine_indicators(self):
        files = cli_stages.code_files()
        names = [rel for rel, _ in files]
        self.assertIn("research/pine_gate/cli.py", names)
        self.assertIn("bot/strategy/pine_indicators.py", names)
        self.assertEqual(names, sorted(names))
        self.assertFalse(any(n.startswith("bot/") and n != "bot/strategy/pine_indicators.py"
                             for n in names))


class PublicHelperNamesTest(unittest.TestCase):
    def test_helpers_shared_with_the_cli_are_public(self):
        from research.pine_gate import bars, calibrate, features, holdout

        for module, name in ((holdout, "check_calibration"), (holdout, "refuse_any_holdout"),
                             (features, "htf_bars"), (bars, "entry_index_for"),
                             (calibrate, "fetch_all")):
            self.assertTrue(callable(getattr(module, name, None)), f"{module.__name__}.{name}")


class UsageTest(unittest.TestCase):
    def test_unknown_subcommand_is_a_usage_error(self):
        code, _, _ = run_cli(["nope"])
        self.assertEqual(code, 2)

    def test_main_module_exists(self):
        import research.pine_gate.__main__  # noqa: F401  (guarded by __name__)


if __name__ == "__main__":
    unittest.main()
