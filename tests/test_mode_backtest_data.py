"""Tests for research.mode_backtest.data: loading a pair, the file manifest and the copy check."""

import dataclasses
import gzip
import hashlib
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from research.mode_backtest import data as backtest_data
from research.mode_backtest.data import (
    PairData,
    load_pair,
    manifest,
    manifest_sha256,
    stage_files,
    verify_copy,
)
from research.pine_gate.bars import (
    BarGapError,
    Bars,
    DayShapeError,
    MissingDayError,
    UnreadableDayError,
    aggregate,
    day_path,
    load_days,
    warmup_path,
    wib_day_start_ms,
    write_day,
)
from research.pine_gate.features import htf_bars
from tests.mode_backtest_helpers import (
    FOUR_HOUR_MS,
    HOUR_MS,
    MINUTE_MS,
    MINUTES_PER_DAY,
    SOURCE,
    flat_bar,
    minute_rows,
    sha256sum_text,
    write_days,
    write_warmups,
)

SYMBOL = "BTC-USDT"
D0 = date(2026, 6, 30)
D1 = date(2026, 7, 1)
D2 = date(2026, 7, 2)
D3 = date(2026, 7, 3)
D4 = date(2026, 7, 4)
WARMUP_HOURS = 48
WARMUP_4H_BARS = 12
GARBAGE = b"this is not a gzip file"


def flip_one_byte(path: Path) -> None:
    raw = bytearray(path.read_bytes())
    raw[len(raw) // 2] ^= 0x01
    path.write_bytes(bytes(raw))


class DataDirTestCase(unittest.TestCase):
    """A fresh data directory for every test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "data"

    def day(self, day):
        return day_path(self.root, SOURCE, SYMBOL, "1m", day)

    def write_pair(self, days):
        """Day files for ``days`` and warm-ups that end where the first of them starts."""
        write_days(self.root, SYMBOL, days)
        write_warmups(self.root, SYMBOL, min(days), WARMUP_HOURS, WARMUP_4H_BARS)

    def corrupt(self, *paths):
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(GARBAGE)

    @contextmanager
    def fresh_data_dir(self):
        """Point ``self.root`` at a new empty directory for the length of the block."""
        with tempfile.TemporaryDirectory() as tmp:
            self.root = Path(tmp)
            yield

    @contextmanager
    def recorded_opens(self):
        """The paths opened through ``gzip.open``, which is how every bar file is read."""
        opened = []
        real_open = gzip.open

        def recording_open(path, *args, **kwargs):
            opened.append(Path(path))
            return real_open(path, *args, **kwargs)

        with mock.patch.object(gzip, "open", recording_open):
            yield opened


class HelperTests(DataDirTestCase):
    """The shared synthetic data is what the real data looks like."""

    def test_minute_rows_are_consecutive_flat_bars_that_follow_the_price(self):
        rows = minute_rows(1_000 * MINUTE_MS, 3, price=lambda i: 100.0 + i)

        self.assertEqual(
            rows,
            [
                (1_000 * MINUTE_MS, 100.0, 100.0, 100.0, 100.0, 1.0),
                (1_001 * MINUTE_MS, 101.0, 101.0, 101.0, 101.0, 1.0),
                (1_002 * MINUTE_MS, 102.0, 102.0, 102.0, 102.0, 1.0),
            ],
        )
        self.assertEqual(minute_rows(0, 2), [flat_bar(0, 100.0), flat_bar(MINUTE_MS, 100.0)])

    def test_day_files_hold_exactly_their_own_wib_day(self):
        paths = write_days(self.root, SYMBOL, [D2, D1])

        self.assertEqual(paths, [self.day(D1), self.day(D2)])
        bars = load_days(self.root, SOURCE, SYMBOL, "1m", D1, D2)
        self.assertEqual(len(bars), 2 * MINUTES_PER_DAY)
        self.assertEqual(bars.open_ms[0], wib_day_start_ms(D1))
        self.assertEqual(bars.open_ms[-1], wib_day_start_ms(D2) + (MINUTES_PER_DAY - 1) * MINUTE_MS)

    def test_the_price_runs_on_across_days_and_across_a_missing_day(self):
        write_days(self.root, SYMBOL, [D1, D3], price=lambda i: 100.0 + i)

        first = load_days(self.root, SOURCE, SYMBOL, "1m", D1, D1)
        third = load_days(self.root, SOURCE, SYMBOL, "1m", D3, D3)
        self.assertEqual(first.close[0], 100.0)
        self.assertEqual(first.close[-1], 100.0 + MINUTES_PER_DAY - 1)
        self.assertEqual(third.close[0], 100.0 + 2 * MINUTES_PER_DAY)
        self.assertFalse(self.day(D2).exists())

    def test_no_days_write_no_files(self):
        self.assertEqual(write_days(self.root, SYMBOL, []), [])
        self.assertFalse(self.root.exists())

    def test_warmups_end_with_the_bar_that_holds_the_minute_before_the_first_day(self):
        one_h, four_h = write_warmups(self.root, SYMBOL, D1, hours_1h=10, bars_4h=3)

        self.assertEqual(one_h, warmup_path(self.root, SYMBOL, "1h"))
        self.assertEqual(four_h, warmup_path(self.root, SYMBOL, "4h"))
        start = wib_day_start_ms(D1)
        self.assertEqual(datetime.fromtimestamp(start / 1000, timezone.utc).hour, 17)
        for path, interval_ms, count in ((one_h, HOUR_MS, 10), (four_h, FOUR_HOUR_MS, 3)):
            with self.subTest(path=path.parent.name):
                with gzip.open(path, "rt") as fh:
                    times = [int(line.split(",")[0]) for line in fh]
                self.assertEqual(len(times), count)
                self.assertEqual(times[-1], start - HOUR_MS)  # opens 16:00 UTC, like the real files
                self.assertEqual(times[-1] % interval_ms, 0)
                self.assertEqual(
                    times, [times[0] + k * interval_ms for k in range(count)]
                )

    def test_warmups_join_the_aggregated_bars_without_a_gap(self):
        self.write_pair([D1, D2])
        pair = load_pair(self.root, SYMBOL, D1, D2)

        for warmup, interval_ms, count in (
            (pair.warmup_1h, HOUR_MS, WARMUP_HOURS),
            (pair.warmup_4h, FOUR_HOUR_MS, WARMUP_4H_BARS),
        ):
            with self.subTest(interval_ms=interval_ms):
                joined = htf_bars(pair.bars_1m, warmup, interval_ms)  # raises on a hole at the seam
                self.assertGreater(len(joined), count)
                self.assertEqual(joined.open_ms[count - 1], warmup.open_ms[-1])
                self.assertEqual(joined.open_ms[count], warmup.open_ms[-1] + interval_ms)

    def test_sha256sum_text_is_what_the_vps_command_prints(self):
        self.write_pair([D1])

        lines = sha256sum_text(self.root).splitlines()

        self.assertEqual(
            [line.split("  ", 1)[1] for line in lines],
            [
                "prod/BTC-USDT/1h/warmup.csv.gz",
                "prod/BTC-USDT/1m/2026-07-01.csv.gz",
                "prod/BTC-USDT/4h/warmup.csv.gz",
            ],
        )
        expected = hashlib.sha256(self.day(D1).read_bytes()).hexdigest()
        self.assertIn(f"{expected}  prod/BTC-USDT/1m/2026-07-01.csv.gz", lines)


class PairDataTests(unittest.TestCase):
    def test_source_is_production(self):
        self.assertEqual(backtest_data.SOURCE, "prod")
        self.assertEqual(SOURCE, backtest_data.SOURCE)

    def test_pair_data_is_frozen(self):
        pair = PairData(SYMBOL, mock.sentinel.bars, mock.sentinel.h1, mock.sentinel.h4)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            pair.symbol = "ETH-USDT"
        self.assertEqual(
            [f.name for f in dataclasses.fields(pair)],
            ["symbol", "bars_1m", "warmup_1h", "warmup_4h"],
        )


class LoadPairTests(DataDirTestCase):
    def test_load_pair_returns_the_days_and_both_warmups(self):
        self.write_pair([D1, D2])

        pair = load_pair(self.root, SYMBOL, D1, D2)

        self.assertIsInstance(pair, PairData)
        self.assertEqual(pair.symbol, SYMBOL)
        self.assertIsInstance(pair.bars_1m, Bars)
        self.assertEqual(pair.bars_1m.interval_ms, MINUTE_MS)
        self.assertEqual(len(pair.bars_1m), 2 * MINUTES_PER_DAY)
        self.assertEqual(pair.bars_1m.open_ms[0], wib_day_start_ms(D1))
        self.assertEqual(pair.warmup_1h.interval_ms, HOUR_MS)
        self.assertEqual(len(pair.warmup_1h), WARMUP_HOURS)
        self.assertEqual(pair.warmup_4h.interval_ms, FOUR_HOUR_MS)
        self.assertEqual(len(pair.warmup_4h), WARMUP_4H_BARS)
        self.assertEqual(pair.warmup_1h.open_ms[-1], wib_day_start_ms(D1) - HOUR_MS)

    def test_a_string_data_dir_works_like_a_path(self):
        self.write_pair([D1])

        pair = load_pair(str(self.root), SYMBOL, D1, D1)

        self.assertEqual(len(pair.bars_1m), MINUTES_PER_DAY)
        warmup_path(self.root, SYMBOL, "1h").unlink()
        with self.assertRaises(FileNotFoundError) as ctx:
            load_pair(str(self.root), SYMBOL, D1, D1)
        self.assertIn("prod/BTC-USDT/1h/warmup.csv.gz", str(ctx.exception))

    def test_a_single_day_is_a_valid_range(self):
        self.write_pair([D1])

        self.assertEqual(len(load_pair(self.root, SYMBOL, D1, D1).bars_1m), MINUTES_PER_DAY)

    def test_load_pair_reads_only_the_requested_days(self):
        self.write_pair([D1, D2])
        self.corrupt(self.day(D0), self.day(D3), self.day(D4))

        pair = load_pair(self.root, SYMBOL, D1, D2)

        self.assertEqual(len(pair.bars_1m), 2 * MINUTES_PER_DAY)

    def test_load_pair_opens_exactly_the_files_stage_files_lists(self):
        self.write_pair([D1, D2, D3])

        with self.recorded_opens() as opened:
            load_pair(self.root, SYMBOL, D1, D3)

        self.assertEqual(sorted(opened), stage_files(self.root, SYMBOL, D1, D3))

    def test_missing_day_raises_before_reading(self):
        self.write_pair([D1, D3])  # D2 is missing
        # Anything read first would raise UnreadableDayError instead.
        warmups = [warmup_path(self.root, SYMBOL, interval) for interval in ("1h", "4h")]
        self.corrupt(self.day(D1), self.day(D3), *warmups)

        with self.recorded_opens() as opened:
            with self.assertRaises(MissingDayError) as ctx:
                load_pair(self.root, SYMBOL, D1, D3)

        self.assertEqual(opened, [])
        self.assertIn("2026-07-02", str(ctx.exception))
        self.assertNotIn("2026-07-01", str(ctx.exception))

    def test_every_missing_day_is_listed(self):
        self.write_pair([D1])

        with self.assertRaises(MissingDayError) as ctx:
            load_pair(self.root, SYMBOL, D1, D4)

        for missing in ("2026-07-02", "2026-07-03", "2026-07-04"):
            self.assertIn(missing, str(ctx.exception))

    def test_a_missing_day_is_reported_before_a_missing_warmup(self):
        write_days(self.root, SYMBOL, [D1, D3])  # no warm-ups at all, and D2 is missing

        with self.assertRaises(MissingDayError):
            load_pair(self.root, SYMBOL, D1, D3)

    def test_missing_warmup_raises(self):
        for missing in (("1h",), ("4h",), ("1h", "4h")):
            with self.subTest(missing=missing), self.fresh_data_dir():
                self.write_pair([D1])
                for interval in missing:
                    warmup_path(self.root, SYMBOL, interval).unlink()

                with self.assertRaises(FileNotFoundError) as ctx:
                    load_pair(self.root, SYMBOL, D1, D1)

                self.assertNotIsInstance(ctx.exception, MissingDayError)
                for interval in ("1h", "4h"):
                    named = f"prod/{SYMBOL}/{interval}/warmup.csv.gz" in str(ctx.exception)
                    self.assertEqual(named, interval in missing, interval)

    def test_an_empty_warmup_is_refused(self):
        for interval, other in (("1h", "4h"), ("4h", "1h")):
            with self.subTest(interval=interval), self.fresh_data_dir():
                self.write_pair([D1])
                write_day(warmup_path(self.root, SYMBOL, interval), [])

                with self.assertRaises(ValueError) as ctx:
                    load_pair(self.root, SYMBOL, D1, D1)

                self.assertNotIsInstance(
                    ctx.exception, (UnreadableDayError, DayShapeError, BarGapError, MissingDayError)
                )
                self.assertIn(f"prod/{SYMBOL}/{interval}/warmup.csv.gz", str(ctx.exception))
                self.assertNotIn(f"prod/{SYMBOL}/{other}/warmup.csv.gz", str(ctx.exception))

    def test_a_warmup_that_starts_at_or_after_the_first_aggregated_bar_is_refused(self):
        for interval, interval_ms in (("1h", HOUR_MS), ("4h", FOUR_HOUR_MS)):
            for bars_late in (0, 5):
                with self.subTest(interval=interval, bars_late=bars_late), self.fresh_data_dir():
                    self.write_pair([D1, D2])
                    bars_1m = load_days(self.root, SOURCE, SYMBOL, "1m", D1, D2)
                    first = aggregate(bars_1m, interval_ms).open_ms[0] + bars_late * interval_ms
                    rows = [flat_bar(first + k * interval_ms, 100.0) for k in range(3)]
                    write_day(warmup_path(self.root, SYMBOL, interval), rows)

                    with self.assertRaises(ValueError) as ctx:
                        load_pair(self.root, SYMBOL, D1, D2)

                    self.assertNotIsInstance(
                        ctx.exception,
                        (UnreadableDayError, DayShapeError, BarGapError, MissingDayError),
                    )
                    self.assertIn(f"prod/{SYMBOL}/{interval}/warmup.csv.gz", str(ctx.exception))

    def test_every_unused_warmup_is_named(self):
        self.write_pair([D1])
        for interval in ("1h", "4h"):
            write_day(warmup_path(self.root, SYMBOL, interval), [])

        with self.assertRaises(ValueError) as ctx:
            load_pair(self.root, SYMBOL, D1, D1)

        for interval in ("1h", "4h"):
            self.assertIn(f"prod/{SYMBOL}/{interval}/warmup.csv.gz", str(ctx.exception))

    def test_one_bar_before_the_first_aggregated_bar_is_enough(self):
        write_days(self.root, SYMBOL, [D1])
        seam_bar = flat_bar(wib_day_start_ms(D1) - HOUR_MS, 100.0)  # opens 16:00 UTC, on both grids
        for interval in ("1h", "4h"):
            write_day(warmup_path(self.root, SYMBOL, interval), [seam_bar])

        pair = load_pair(self.root, SYMBOL, D1, D1)

        self.assertEqual((len(pair.warmup_1h), len(pair.warmup_4h)), (1, 1))

    def test_an_unreadable_warmup_is_reported_before_an_unused_one(self):
        self.write_pair([D1])
        write_day(warmup_path(self.root, SYMBOL, "1h"), [])
        self.corrupt(warmup_path(self.root, SYMBOL, "4h"))

        with self.assertRaises(UnreadableDayError):
            load_pair(self.root, SYMBOL, D1, D1)

    def test_a_corrupt_warmup_stops_the_load(self):
        self.write_pair([D1])
        self.corrupt(warmup_path(self.root, SYMBOL, "4h"))

        with self.assertRaises(UnreadableDayError):
            load_pair(self.root, SYMBOL, D1, D1)

    def test_a_warmup_with_a_hole_stops_the_load(self):
        self.write_pair([D1])
        end = wib_day_start_ms(D1)
        rows = [flat_bar(end - (10 - k) * HOUR_MS, 100.0) for k in range(10) if k != 4]
        write_day(warmup_path(self.root, SYMBOL, "1h"), rows)

        with self.assertRaises(BarGapError):
            load_pair(self.root, SYMBOL, D1, D1)

    def test_a_short_day_stops_the_load(self):
        self.write_pair([D1, D2])
        write_day(self.day(D2), minute_rows(wib_day_start_ms(D2), MINUTES_PER_DAY - 1))

        with self.assertRaises(DayShapeError):
            load_pair(self.root, SYMBOL, D1, D2)

    def test_a_reversed_range_is_refused(self):
        self.write_pair([D1, D2])

        with self.assertRaises(ValueError):
            load_pair(self.root, SYMBOL, D2, D1)

    def test_the_symbol_must_be_a_venue_pair(self):
        for symbol in ("../BTC-USDT", "btc-usdt", "BTCUSDT", ""):
            with self.subTest(symbol=symbol):
                with self.assertRaises(ValueError):
                    load_pair(self.root, symbol, D1, D1)


class StageFilesTests(DataDirTestCase):
    def test_stage_files_are_the_day_files_and_both_warmups_sorted(self):
        paths = stage_files(self.root, SYMBOL, D1, D3)

        self.assertEqual(
            sorted(paths),
            sorted(
                [self.day(D1), self.day(D2), self.day(D3)]
                + [warmup_path(self.root, SYMBOL, i) for i in ("1h", "4h")]
            ),
        )
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), 5)
        self.assertTrue(all(isinstance(p, Path) for p in paths))
        self.assertTrue(all(self.root / SOURCE / SYMBOL in p.parents for p in paths))

    def test_one_day_is_two_warmups_and_that_day(self):
        self.assertEqual(len(stage_files(str(self.root), SYMBOL, D2, D2)), 3)

    def test_stage_files_names_files_without_reading_them(self):
        self.assertFalse(self.root.exists())

        self.assertEqual(len(stage_files(self.root, SYMBOL, D1, D2)), 4)

    def test_a_reversed_range_is_refused(self):
        with self.assertRaises(ValueError):
            stage_files(self.root, SYMBOL, D2, D1)

    def test_the_symbol_must_be_a_venue_pair(self):
        with self.assertRaises(ValueError):
            stage_files(self.root, "../etc", D1, D2)


class ManifestTests(DataDirTestCase):
    def staged(self, days=(D1, D2, D3)):
        self.write_pair(days)
        return stage_files(self.root, SYMBOL, min(days), max(days))

    def test_manifest_is_relative_sorted_and_changes_with_one_byte(self):
        paths = self.staged()

        lines = manifest(self.root, reversed(paths))

        self.assertEqual(lines, sorted(lines))
        self.assertEqual(len(lines), 5)
        for line in lines:
            relative, sha = line.rsplit(":", 1)
            self.assertEqual(sha, hashlib.sha256((self.root / relative).read_bytes()).hexdigest())
            self.assertTrue(relative.startswith("prod/BTC-USDT/"), relative)
            self.assertNotIn(str(self.root), line)
        self.assertEqual(lines, manifest(self.root, paths))
        before = manifest_sha256(lines)
        self.assertRegex(before, r"^[0-9a-f]{64}$")

        flip_one_byte(self.day(D2))

        changed = manifest(self.root, paths)
        self.assertEqual([a != b for a, b in zip(lines, changed, strict=True)].count(True), 1)
        changed_line = next(b for a, b in zip(lines, changed, strict=True) if a != b)
        self.assertTrue(changed_line.startswith("prod/BTC-USDT/1m/2026-07-02.csv.gz:"))
        self.assertNotEqual(manifest_sha256(changed), before)

        flip_one_byte(self.day(D2))  # the same flip again restores the file

        self.assertEqual(manifest_sha256(manifest(self.root, paths)), before)

    def test_manifest_does_not_depend_on_where_the_data_lives(self):
        paths = self.staged()
        elsewhere = self.root.parent / "copy"
        shutil.copytree(self.root, elsewhere)

        self.assertEqual(
            manifest(self.root, paths),
            manifest(elsewhere, stage_files(elsewhere, SYMBOL, D1, D3)),
        )
        self.assertEqual(manifest(str(self.root), paths), manifest(self.root, paths))

    def test_a_missing_file_stops_the_manifest(self):
        paths = self.staged()
        self.day(D2).unlink()

        with self.assertRaises(FileNotFoundError):
            manifest(self.root, paths)

    def test_a_file_outside_the_data_dir_is_refused(self):
        self.staged()
        outside = self.root.parent / "outside.csv.gz"
        outside.write_bytes(b"x")

        with self.assertRaises(ValueError):
            manifest(self.root, [outside])

    def test_a_path_listed_twice_is_refused(self):
        paths = self.staged()

        with self.assertRaises(ValueError) as ctx:
            manifest(self.root, paths + [paths[0]])

        self.assertIn(paths[0].name, str(ctx.exception))

    def test_manifest_sha256_hashes_the_sorted_lines(self):
        lines = ["prod/b:2", "prod/a:1"]

        self.assertEqual(
            manifest_sha256(lines),
            hashlib.sha256(b"prod/a:1\nprod/b:2\n").hexdigest(),
        )
        self.assertEqual(manifest_sha256(lines), manifest_sha256(sorted(lines)))
        self.assertNotEqual(manifest_sha256(lines), manifest_sha256(lines[:1]))
        self.assertNotEqual(manifest_sha256(lines), manifest_sha256(["prod/b:2", "prod/a:3"]))

    def test_manifest_sha256_refuses_one_string_for_a_list_of_lines(self):
        with self.assertRaises(TypeError):
            manifest_sha256("prod/a:1")


class VerifyCopyTests(DataDirTestCase):
    def local_copy(self):
        """Three days, both warm-ups and a funding file, and the listing the VPS would print."""
        self.write_pair([D1, D2, D3])
        funding = self.root / SOURCE / SYMBOL / "funding.json"
        funding.write_text("[1, 2, 3]\n", encoding="utf-8")
        return sha256sum_text(self.root)

    def mentioning(self, problems, relative):
        return [p for p in problems if relative in p]

    def test_an_exact_copy_has_no_problems(self):
        listing = self.local_copy()

        self.assertEqual(verify_copy(self.root, listing), [])
        self.assertEqual(verify_copy(str(self.root), listing), [])

    def test_verify_copy_reports_every_mismatch_and_missing_file(self):
        listing = self.local_copy()
        day2 = "prod/BTC-USDT/1m/2026-07-02.csv.gz"
        day3 = "prod/BTC-USDT/1m/2026-07-03.csv.gz"
        four_h = "prod/BTC-USDT/4h/warmup.csv.gz"
        stray = "prod/BTC-USDT/1m/2026-07-06.csv.gz"
        flip_one_byte(self.day(D2))
        self.day(D3).unlink()
        flip_one_byte(warmup_path(self.root, SYMBOL, "4h"))
        (self.root / stray).write_bytes(b"not on the VPS")
        listing += "deadbeef  prod/BTC-USDT/1m/2026-07-04.csv.gz\n"
        listing += "garbage\n"

        problems = verify_copy(self.root, listing)

        self.assertEqual(len(problems), 6, problems)
        for relative, reason in (
            (day2, "differs"),
            (four_h, "differs"),
            (day3, "missing"),
            (stray, "not in the listing"),
        ):
            with self.subTest(relative=relative):
                found = self.mentioning(problems, relative)
                self.assertEqual(len(found), 1, found)
                self.assertIn(reason, found[0])
        self.assertEqual(len(self.mentioning(problems, "line ")), 2)
        for untouched in (
            "prod/BTC-USDT/1m/2026-07-01.csv.gz",
            "prod/BTC-USDT/1h/warmup.csv.gz",
            "prod/BTC-USDT/funding.json",
        ):
            self.assertEqual(self.mentioning(problems, untouched), [], untouched)

    def listed_paths(self, listing):
        return [line.split("  ", 1)[1] for line in listing.splitlines()]

    def test_a_truncated_listing_is_not_an_exact_copy(self):
        listing = self.local_copy()
        paths = self.listed_paths(listing)
        cut_after_first_line = listing.splitlines()[0] + "\n"

        problems = verify_copy(self.root, cut_after_first_line)

        self.assertEqual(len(problems), len(paths) - 1, problems)
        for relative in paths[1:]:
            with self.subTest(relative=relative):
                found = self.mentioning(problems, relative)
                self.assertEqual(len(found), 1, found)
                self.assertIn("not in the listing", found[0])
        self.assertEqual(self.mentioning(problems, paths[0]), [])

    def test_a_listing_cut_in_the_middle_of_a_line_is_not_an_exact_copy(self):
        listing = self.local_copy()
        lines = listing.splitlines()
        cut_in_a_line = lines[0] + "\n" + lines[1][:40]

        problems = verify_copy(self.root, cut_in_a_line)

        # the half line is one problem, and each of the files from line 2 on is another
        self.assertEqual(len(problems), 1 + len(lines) - 1, problems)
        self.assertTrue(problems[0].startswith("line 2: "), problems[0])

    def test_a_stray_local_file_is_a_problem(self):
        listing = self.local_copy()
        (stray,) = write_days(self.root, SYMBOL, [D4])  # a valid day file the VPS listing lacks
        self.assertIn(stray, stage_files(self.root, SYMBOL, D1, D4))  # a load of D1..D4 reads it

        (problem,) = verify_copy(self.root, listing)

        self.assertIn("prod/BTC-USDT/1m/2026-07-04.csv.gz", problem)
        self.assertIn("not in the listing", problem)

    def test_every_stray_file_is_one_problem_in_path_order(self):
        listing = self.local_copy()
        strays = [
            "prod/BTC-USDT/native/extra.json",
            "prod/ETH-USDT/funding.json",
            "prod/BTC-USDT/1m/2026-07-04.csv.gz.1a2b3c.tmp",
        ]
        for relative in strays:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"left over")

        problems = verify_copy(self.root, listing)

        self.assertEqual(len(problems), 3, problems)
        for problem, relative in zip(problems, sorted(strays), strict=True):
            self.assertTrue(problem.startswith(f"{relative}: "), problem)

    def test_names_starting_with_a_dot_are_not_stray_files(self):
        listing = self.local_copy()
        for relative in (
            ".DS_Store",
            "BTC-USDT/.DS_Store",
            "BTC-USDT/1m/.DS_Store",
            "BTC-USDT/1m/._2026-07-01.csv.gz",
        ):
            (self.root / SOURCE / relative).write_bytes(b"finder leftovers")

        self.assertEqual(verify_copy(self.root, listing), [])

    def test_only_regular_files_under_prod_are_examined(self):
        listing = self.local_copy()
        (self.root / SOURCE / "ETH-USDT" / "1m").mkdir(parents=True)  # a directory, no file
        marker = self.root / "holdout_runs" / "mode_A_BTC-USDT_0123.json"  # beside prod, not in it
        marker.parent.mkdir()
        marker.write_text("{}", encoding="utf-8")
        (self.root / "notes.txt").write_text("not data", encoding="utf-8")

        self.assertEqual(verify_copy(self.root, listing), [])

    def test_a_listed_file_that_is_missing_is_not_also_a_stray_file(self):
        listing = self.local_copy()
        self.day(D2).unlink()

        (problem,) = verify_copy(self.root, listing)

        self.assertIn("missing locally", problem)

    def test_a_mismatch_names_both_hashes(self):
        listing = self.local_copy()
        flip_one_byte(self.day(D1))
        local = hashlib.sha256(self.day(D1).read_bytes()).hexdigest()
        vps = next(
            line.split("  ")[0]
            for line in listing.splitlines()
            if line.endswith("2026-07-01.csv.gz")
        )

        (problem,) = verify_copy(self.root, listing)

        self.assertIn(local, problem)
        self.assertIn(vps, problem)

    def test_files_beside_the_day_files_are_checked_too(self):
        listing = self.local_copy()
        (self.root / SOURCE / SYMBOL / "funding.json").write_text("[1, 2, 4]\n", encoding="utf-8")

        (problem,) = verify_copy(self.root, listing)

        self.assertIn("prod/BTC-USDT/funding.json", problem)

    def test_malformed_lines_are_each_one_problem(self):
        sha = "a" * 64
        # Not in the local copy, so a line that parsed would show up as a missing file.
        fresh = "prod/BTC-USDT/1m/2026-07-09.csv.gz"
        malformed = [
            "garbage",
            f"{sha[:63]}  {fresh}",  # 63 hex digits
            f"{sha}0  {fresh}",  # 65 hex digits
            f"{'g' * 64}  {fresh}",  # not hex
            f"{sha} {fresh}",  # one space
            f"{sha}\t{fresh}",  # a tab
            f"{sha} *{fresh}",  # binary-mode marker
            f"{sha}  ",  # no path
            sha,  # no separator
            "# comment",
        ]
        self.local_copy()  # nothing is listed, so the local files are not listed as strays
        for text in malformed:
            with self.subTest(text=text):
                (problem,) = verify_copy(self.root, text + "\n")
                self.assertTrue(problem.startswith("line 1: "), problem)

    def test_malformed_lines_are_numbered_by_position_in_the_listing(self):
        listing = self.local_copy()
        fresh = "prod/BTC-USDT/1m/2026-07-09.csv.gz"
        text = listing + f"\ngarbage\n{'a' * 64}  {fresh}\n{'b' * 63}  {fresh}\n"
        first_line = len(listing.splitlines()) + 1  # one blank line, then the first bad one

        problems = verify_copy(self.root, text)

        self.assertEqual(len(problems), 3, problems)
        self.assertTrue(problems[0].startswith(f"line {first_line + 1}: "), problems[0])
        self.assertEqual(problems[1], f"{fresh}: missing locally")
        self.assertTrue(problems[2].startswith(f"line {first_line + 3}: "), problems[2])

    def test_a_path_that_leaves_prod_is_a_malformed_line_and_is_never_read(self):
        sha = "b" * 64
        outside = self.root.parent / "secret.txt"
        outside.write_text("do not hash me", encoding="utf-8")
        unsafe = [
            "prod/../../secret.txt",
            "prod/BTC-USDT/../../../secret.txt",
            f"{outside}",
            "/etc/hosts",
            "../secret.txt",
            "secret.txt",
            "holdout_runs/mode_A_BTC-USDT.json",
            "prod",
            "prod/BTC-USDT/\x00funding.json",
        ]
        listing = "".join(f"{sha}  {path}\n" for path in unsafe)

        with mock.patch.object(backtest_data, "file_sha256") as hashed:
            problems = verify_copy(self.root, listing)

        hashed.assert_not_called()
        self.assertEqual(len(problems), len(unsafe), problems)
        for number, problem in enumerate(problems, start=1):
            self.assertTrue(problem.startswith(f"line {number}: "), problem)

    def test_an_empty_listing_is_not_an_exact_copy(self):
        self.local_copy()

        for listing in ("", "\n", "  \n\n \t\n"):
            with self.subTest(listing=listing):
                problems = verify_copy(self.root, listing)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("no files", problems[0])

    def test_a_very_long_line_is_cut_in_its_message(self):
        (problem,) = verify_copy(self.root, "x" * 10_000 + "\n")

        self.assertLess(len(problem), 200)
        self.assertTrue(problem.startswith("line 1: "))
        self.assertIn("...", problem)

    def test_one_malformed_line_is_one_problem_not_two(self):
        self.local_copy()  # a full local copy is no reason for more problems when nothing is listed

        self.assertEqual(len(verify_copy(self.root, "garbage\n")), 1)

    def test_a_path_listed_twice_is_a_problem(self):
        listing = self.local_copy()
        line = next(entry for entry in listing.splitlines() if entry.endswith("funding.json"))

        problems = verify_copy(self.root, listing + line + "\n")

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("prod/BTC-USDT/funding.json", problems[0])
        self.assertIn("twice", problems[0])

    def test_conflicting_entries_for_one_file_cannot_both_pass(self):
        listing = self.local_copy()
        line = next(entry for entry in listing.splitlines() if entry.endswith("funding.json"))
        conflicting = "c" * 64 + "  prod/BTC-USDT/funding.json\n"

        self.assertNotEqual(verify_copy(self.root, listing + conflicting), [])
        self.assertNotEqual(verify_copy(self.root, conflicting + line + "\n"), [])

    def test_an_unreadable_file_is_a_problem_not_a_crash(self):
        listing = self.local_copy()
        blocked = self.root / SOURCE / SYMBOL / "1m" / "2026-07-05.csv.gz"
        blocked.mkdir()
        listing += "d" * 64 + "  prod/BTC-USDT/1m/2026-07-05.csv.gz\n"

        (problem,) = verify_copy(self.root, listing)

        self.assertIn("prod/BTC-USDT/1m/2026-07-05.csv.gz", problem)
        self.assertIn("cannot be read", problem)

    def test_line_endings_blank_lines_and_hex_case_do_not_matter(self):
        listing = self.local_copy()

        crlf = listing.replace("\n", "\r\n")
        spaced = "\n" + listing.replace("\n", "\n\n") + "  \n"
        pairs = [line.split("  ", 1) for line in listing.splitlines()]
        upper = "".join(f"{sha.upper()}  {relative}\n" for sha, relative in pairs)

        for text in (crlf, spaced, upper):
            with self.subTest(text=text[:20]):
                self.assertEqual(verify_copy(self.root, text), [])

    def test_a_missing_data_dir_reports_each_listed_file(self):
        listing = self.local_copy()

        problems = verify_copy(self.root.parent / "nowhere", listing)

        self.assertEqual(len(problems), len(listing.splitlines()))
        self.assertTrue(all("missing" in p for p in problems))

    def test_the_manifest_of_a_verified_copy_matches_the_listing(self):
        listing = self.local_copy()
        paths = stage_files(self.root, SYMBOL, D1, D3)

        by_path = {
            relative: sha
            for sha, relative in (line.split("  ", 1) for line in listing.splitlines())
        }
        expected = sorted(f"{relative}:{by_path[relative]}" for relative in by_path
                          if relative != "prod/BTC-USDT/funding.json")

        self.assertEqual(verify_copy(self.root, listing), [])
        self.assertEqual(manifest(self.root, paths), expected)


if __name__ == "__main__":
    unittest.main()
