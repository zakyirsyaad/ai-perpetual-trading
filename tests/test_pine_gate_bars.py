import gzip
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from research.pine_gate.bars import (
    BarGapError,
    MissingDayError,
    aggregate,
    bars_from_rows,
    concat,
    day_path,
    entry_index_for,
    index_closing_at,
    interval_to_ms,
    funding_path,
    last_closed_index,
    load_days,
    read_day,
    slice_until,
    warmup_path,
    wib_day,
    wib_day_start_ms,
    wib_hour,
    write_day,
    write_day_exclusive,
)
from research.pine_gate.spec import ms

MIN = 60_000
HOUR = 60 * MIN
T0 = ms(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))


def make_rows(start_ms, interval_ms, count, base=100.0):
    rows = []
    for i in range(count):
        o = base + i
        rows.append((start_ms + i * interval_ms, o, o + 2.0, o - 1.0, o + 0.5, 10.0 + i))
    return rows


class BarsFromRowsTest(unittest.TestCase):
    def test_builds_columns(self):
        bars = bars_from_rows(make_rows(T0, MIN, 3), MIN)
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars.interval_ms, MIN)
        self.assertEqual(bars.open_ms[1], T0 + MIN)
        self.assertEqual(bars.close[2], 102.5)
        self.assertEqual(bars.volume[0], 10.0)

    def test_empty_rows_give_empty_bars(self):
        self.assertEqual(len(bars_from_rows([], MIN)), 0)

    def test_rejects_gap_and_duplicate(self):
        rows = make_rows(T0, MIN, 4)
        gapped = rows[:2] + rows[3:]
        with self.assertRaises(BarGapError) as ctx:
            bars_from_rows(gapped, MIN)
        self.assertIn(str(T0 + 3 * MIN), str(ctx.exception))
        duplicated = rows[:2] + [rows[1]] + rows[2:]
        with self.assertRaises(BarGapError) as ctx:
            bars_from_rows(duplicated, MIN)
        self.assertIn(str(T0 + MIN), str(ctx.exception))

    def test_rejects_unsorted(self):
        rows = make_rows(T0, MIN, 3)
        with self.assertRaises(BarGapError):
            bars_from_rows([rows[1], rows[0], rows[2]], MIN)

    def test_bars_are_frozen(self):
        bars = bars_from_rows(make_rows(T0, MIN, 1), MIN)
        with self.assertRaises(Exception):
            bars.interval_ms = 5  # type: ignore[misc]


class AggregateTest(unittest.TestCase):
    def test_aggregate_5m_first_open_max_high_min_low_last_close_sum_volume(self):
        rows = make_rows(T0, MIN, 10)
        out = aggregate(bars_from_rows(rows, MIN), 5 * MIN)
        self.assertEqual(len(out), 2)
        self.assertEqual(out.interval_ms, 5 * MIN)
        self.assertEqual(out.open_ms[0], T0)
        self.assertEqual(out.open_ms[1], T0 + 5 * MIN)
        self.assertEqual(out.open[0], rows[0][1])
        self.assertEqual(out.high[0], max(r[2] for r in rows[:5]))
        self.assertEqual(out.low[0], min(r[3] for r in rows[:5]))
        self.assertEqual(out.close[0], rows[4][4])
        self.assertEqual(out.volume[0], sum(r[5] for r in rows[:5]))
        self.assertEqual(out.open[1], rows[5][1])
        self.assertEqual(out.close[1], rows[9][4])

    def test_aggregate_drops_incomplete_trailing_bucket(self):
        out = aggregate(bars_from_rows(make_rows(T0, MIN, 8), MIN), 5 * MIN)
        self.assertEqual(len(out), 1)

    def test_aggregate_drops_incomplete_leading_bucket(self):
        start = T0 + 2 * MIN
        out = aggregate(bars_from_rows(make_rows(start, MIN, 8), MIN), 5 * MIN)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.open_ms[0], T0 + 5 * MIN)

    def test_aggregate_drops_bucket_with_interior_gap(self):
        rows = make_rows(T0, MIN, 15)
        del rows[7]
        # bars_from_rows would reject the gap, so assemble through concat of parts
        left = bars_from_rows(rows[:7], MIN)
        right = bars_from_rows(rows[7:], MIN)
        out = aggregate(concat(left, right), 5 * MIN)
        self.assertEqual([out.open_ms[i] for i in range(len(out))], [T0, T0 + 10 * MIN])

    def test_aggregate_3m_and_1h_and_4h_align_to_epoch(self):
        one_min = bars_from_rows(make_rows(T0, MIN, 240), MIN)
        self.assertEqual(len(aggregate(one_min, 3 * MIN)), 80)
        hourly = aggregate(one_min, HOUR)
        self.assertEqual(len(hourly), 4)
        four = aggregate(hourly, 4 * HOUR)
        self.assertEqual(len(four), 1)
        self.assertEqual(four.open_ms[0], T0)
        self.assertEqual(four.volume[0], sum(one_min.volume))

    def test_aggregate_same_interval_is_a_copy(self):
        bars = bars_from_rows(make_rows(T0, MIN, 3), MIN)
        out = aggregate(bars, MIN)
        self.assertEqual(list(out.open_ms), list(bars.open_ms))

    def test_aggregate_rejects_non_multiple_or_finer_target(self):
        bars = bars_from_rows(make_rows(T0, 5 * MIN, 3), 5 * MIN)
        with self.assertRaises(ValueError):
            aggregate(bars, 7 * MIN)
        with self.assertRaises(ValueError):
            aggregate(bars, MIN)

    def test_aggregate_does_not_mutate_input(self):
        bars = bars_from_rows(make_rows(T0, MIN, 10), MIN)
        before = list(bars.close)
        aggregate(bars, 5 * MIN)
        self.assertEqual(list(bars.close), before)


class ConcatSliceTest(unittest.TestCase):
    def test_concat_joins_in_order(self):
        a = bars_from_rows(make_rows(T0, HOUR, 2), HOUR)
        b = bars_from_rows(make_rows(T0 + 2 * HOUR, HOUR, 2), HOUR)
        out = concat(a, b)
        self.assertEqual(len(out), 4)
        self.assertEqual(out.open_ms[3], T0 + 3 * HOUR)
        self.assertEqual(len(a), 2)

    def test_concat_rejects_overlap_and_interval_mismatch(self):
        a = bars_from_rows(make_rows(T0, HOUR, 2), HOUR)
        overlap = bars_from_rows(make_rows(T0 + HOUR, HOUR, 2), HOUR)
        with self.assertRaises(ValueError):
            concat(a, overlap)
        other = bars_from_rows(make_rows(T0 + 5 * HOUR, MIN, 2), MIN)
        with self.assertRaises(ValueError):
            concat(a, other)
        with self.assertRaises(ValueError):
            concat()

    def test_concat_allows_a_gap_between_parts(self):
        a = bars_from_rows(make_rows(T0, HOUR, 1), HOUR)
        b = bars_from_rows(make_rows(T0 + 5 * HOUR, HOUR, 1), HOUR)
        self.assertEqual(len(concat(a, b)), 2)

    def test_concat_skips_empty_parts(self):
        a = bars_from_rows(make_rows(T0, HOUR, 2), HOUR)
        empty = bars_from_rows([], HOUR)
        self.assertEqual(len(concat(empty, a, empty)), 2)

    def test_last_closed_index_excludes_forming_bar(self):
        bars = bars_from_rows(make_rows(T0 + 8 * HOUR, HOUR, 4), HOUR)  # 08..11
        t = T0 + 10 * HOUR + 30 * MIN
        self.assertEqual(last_closed_index(bars, t), 1)  # the 09:00 bar
        self.assertEqual(bars.open_ms[last_closed_index(bars, t)], T0 + 9 * HOUR)

    def test_last_closed_index_boundaries(self):
        bars = bars_from_rows(make_rows(T0, HOUR, 3), HOUR)
        self.assertEqual(last_closed_index(bars, T0 + HOUR - 1), -1)
        self.assertEqual(last_closed_index(bars, T0 + HOUR), 0)
        self.assertEqual(last_closed_index(bars, T0 + 100 * HOUR), 2)
        self.assertEqual(last_closed_index(bars_from_rows([], HOUR), T0), -1)

    def test_slice_until_keeps_closed_bars_only(self):
        bars = bars_from_rows(make_rows(T0, HOUR, 4), HOUR)
        out = slice_until(bars, T0 + 2 * HOUR + 30 * MIN)
        self.assertEqual(len(out), 2)
        self.assertEqual(out.interval_ms, HOUR)
        self.assertEqual(len(bars), 4)
        self.assertEqual(len(slice_until(bars, T0)), 0)


class WibTimeTest(unittest.TestCase):
    def test_wib_day_rolls_at_1700_utc(self):
        before = ms(datetime(2026, 1, 1, 16, 59, tzinfo=timezone.utc))
        at = ms(datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc))
        self.assertEqual(wib_day(before), date(2026, 1, 1))
        self.assertEqual(wib_day(at), date(2026, 1, 2))

    def test_wib_day_start_ms_is_1700_utc_previous_day(self):
        expected = ms(datetime(2025, 12, 31, 17, 0, tzinfo=timezone.utc))
        self.assertEqual(wib_day_start_ms(date(2026, 1, 1)), expected)
        self.assertEqual(wib_day(wib_day_start_ms(date(2026, 1, 1))), date(2026, 1, 1))
        self.assertEqual(wib_day(wib_day_start_ms(date(2026, 1, 1)) - 1), date(2025, 12, 31))

    def test_wib_hour(self):
        self.assertEqual(wib_hour(ms(datetime(2026, 1, 1, 16, 59, tzinfo=timezone.utc))), 23)
        self.assertEqual(wib_hour(ms(datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc))), 0)
        self.assertEqual(wib_hour(ms(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))), 7)


class IntervalLabelTest(unittest.TestCase):
    def test_interval_to_ms(self):
        self.assertEqual(interval_to_ms("1m"), MIN)
        self.assertEqual(interval_to_ms("3m"), 3 * MIN)
        self.assertEqual(interval_to_ms("4h"), 4 * HOUR)
        for bad in ("", "m", "0m", "5x", "abc"):
            with self.assertRaises(ValueError):
                interval_to_ms(bad)


class DayFilesTest(unittest.TestCase):
    def test_paths(self):
        base = Path("/data")
        self.assertEqual(
            day_path(base, "prod", "BTC-USDT", "1m", date(2026, 1, 2)),
            base / "prod" / "BTC-USDT" / "1m" / "2026-01-02.csv.gz",
        )
        self.assertEqual(
            warmup_path(base, "BTC-USDT", "1h"),
            base / "prod" / "BTC-USDT" / "1h" / "warmup.csv.gz",
        )
        self.assertEqual(
            funding_path(base, "ETH-USDT"), base / "prod" / "ETH-USDT" / "funding.json"
        )

    def test_day_file_roundtrip_and_missing_days_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            d1, d2, d3 = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
            start = wib_day_start_ms(d1)
            day_ms = 86_400_000
            per_day = day_ms // HOUR
            rows1 = make_rows(start, HOUR, per_day)
            rows2 = make_rows(start + day_ms, HOUR, per_day, base=200.123456789)
            write_day(day_path(base, "prod", "BTC-USDT", "1h", d1), rows1)
            write_day(day_path(base, "prod", "BTC-USDT", "1h", d2), rows2)

            self.assertEqual(read_day(day_path(base, "prod", "BTC-USDT", "1h", d2)), rows2)

            bars = load_days(base, "prod", "BTC-USDT", "1h", d1, d2)
            self.assertEqual(len(bars), 2 * per_day)
            self.assertEqual(bars.open_ms[per_day], start + day_ms)

            with self.assertRaises(MissingDayError) as ctx:
                load_days(base, "prod", "BTC-USDT", "1h", d1, date(2026, 1, 5))
            message = str(ctx.exception)
            for missing in ("2026-01-03", "2026-01-04", "2026-01-05"):
                self.assertIn(missing, message)
            self.assertNotIn("2026-01-02", message)
            self.assertNotIn("2026-01-01", message)
            self.assertEqual(d3.isoformat(), "2026-01-03")

    def test_load_days_raises_gap_error_between_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            d1, d2 = date(2026, 1, 1), date(2026, 1, 2)
            start = wib_day_start_ms(d1)
            write_day(day_path(base, "prod", "BTC-USDT", "1h", d1), make_rows(start, HOUR, 24))
            write_day(
                day_path(base, "prod", "BTC-USDT", "1h", d2),
                make_rows(start + 86_400_000 + HOUR, HOUR, 23),
            )
            with self.assertRaises(BarGapError):
                load_days(base, "prod", "BTC-USDT", "1h", d1, d2)

    def test_write_day_creates_parents_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a" / "b" / "2026-01-01.csv.gz"
            write_day(path, make_rows(T0, MIN, 3))
            self.assertTrue(path.exists())
            self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])
            write_day(path, make_rows(T0, MIN, 2))  # overwrite
            self.assertEqual(len(read_day(path)), 2)

    def test_read_day_rejects_malformed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv.gz"
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                fh.write("1,2,3,4,5\n")
            with self.assertRaises(ValueError):
                read_day(path)
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                fh.write("1,2,3,4,5,x\n")
            with self.assertRaises(ValueError):
                read_day(path)

    def test_read_day_rejects_corrupt_gzip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv.gz"
            path.write_bytes(b"not a gzip file")
            with self.assertRaises(ValueError):
                read_day(path)

    def test_read_day_missing_file_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                read_day(Path(tmp) / "nope.csv.gz")

    def test_load_days_rejects_reversed_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                load_days(Path(tmp), "prod", "BTC-USDT", "1h", date(2026, 1, 2), date(2026, 1, 1))


class BarValidationTest(unittest.TestCase):
    """bars_from_rows refuses values no real bar can carry (C6)."""

    def test_rejects_non_finite_non_positive_and_broken_ohlc(self):
        good = (T0, 100.0, 102.0, 99.0, 101.0, 5.0)
        bad = {
            "nan open": (T0, float("nan"), 102.0, 99.0, 101.0, 5.0),
            "inf high": (T0, 100.0, float("inf"), 99.0, 101.0, 5.0),
            "nan volume": (T0, 100.0, 102.0, 99.0, 101.0, float("nan")),
            "zero low": (T0, 100.0, 102.0, 0.0, 101.0, 5.0),
            "negative close": (T0, 100.0, 102.0, 99.0, -1.0, 5.0),
            "high below close": (T0, 100.0, 100.5, 99.0, 101.0, 5.0),
            "high below open": (T0, 103.0, 102.0, 99.0, 101.0, 5.0),
            "low above open": (T0, 98.0, 102.0, 99.0, 101.0, 5.0),
            "low above close": (T0, 100.0, 102.0, 99.0, 98.5, 5.0),
            "negative volume": (T0, 100.0, 102.0, 99.0, 101.0, -0.1),
        }
        bars_from_rows([good], MIN)
        for name, row in bad.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as ctx:
                    bars_from_rows([(T0 - MIN,) + good[1:], row], MIN)
                self.assertIn(str(T0), str(ctx.exception))
                self.assertNotIsInstance(ctx.exception, BarGapError)

    def test_zero_volume_and_flat_bars_are_valid(self):
        bars = bars_from_rows([(T0, 1.0, 1.0, 1.0, 1.0, 0.0)], MIN)
        self.assertEqual(len(bars), 1)


class EntryIndexTest(unittest.TestCase):
    """The one entry-index helper (C2) and its inverse."""

    def setUp(self):
        self.bars = bars_from_rows(make_rows(T0, MIN, 3), MIN)

    def test_entry_index_is_the_bar_containing_the_order_time(self):
        self.assertEqual(entry_index_for(self.bars, T0), 0)
        self.assertEqual(entry_index_for(self.bars, T0 + MIN - 1), 0)
        self.assertEqual(entry_index_for(self.bars, T0 + MIN), 1)
        self.assertEqual(entry_index_for(self.bars, T0 + 3 * MIN - 1), 2)
        self.assertIsNone(entry_index_for(self.bars, T0 - 1))
        self.assertIsNone(entry_index_for(self.bars, T0 + 3 * MIN))
        self.assertIsNone(entry_index_for(bars_from_rows([], MIN), T0))

    def test_index_closing_at_inverts_the_entry_fill_time(self):
        for i in range(3):
            close = self.bars.open_ms[i] + MIN
            self.assertEqual(index_closing_at(self.bars, close), i)
        self.assertIsNone(index_closing_at(self.bars, T0))
        self.assertIsNone(index_closing_at(self.bars, T0 + MIN + 1))
        self.assertIsNone(index_closing_at(self.bars, T0 + 4 * MIN))


class PathSymbolTest(unittest.TestCase):
    def test_paths_refuse_symbols_that_are_not_usdt_pairs(self):
        base = Path("/data")
        for symbol in ("../etc", "BTC-USDT\n", "btc-usdt", "BTC/USDT", "BTC-USDT/../x"):
            with self.subTest(symbol=symbol):
                with self.assertRaises(ValueError):
                    day_path(base, "prod", symbol, "1m", date(2026, 1, 2))
                with self.assertRaises(ValueError):
                    warmup_path(base, symbol, "1h")
                with self.assertRaises(ValueError):
                    funding_path(base, symbol)


class ExclusiveDayWriteTest(unittest.TestCase):
    def test_write_day_exclusive_never_replaces_and_reports_a_late_file(self):
        from unittest import mock

        from research.pine_gate import fileio

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prod" / "2026-01-01.csv.gz"
            self.assertTrue(write_day_exclusive(path, make_rows(T0, MIN, 3)))
            self.assertEqual(len(read_day(path)), 3)
            self.assertFalse(write_day_exclusive(path, make_rows(T0, MIN, 2)))
            self.assertEqual(len(read_day(path)), 3)
            late = Path(tmp) / "prod" / "2026-01-02.csv.gz"

            def racing_link(src, dst):
                Path(dst).write_bytes(b"other writer")
                raise FileExistsError(dst)

            with mock.patch.object(fileio.os, "link", side_effect=racing_link):
                self.assertFalse(write_day_exclusive(late, make_rows(T0, MIN, 1)))
            self.assertEqual(late.read_bytes(), b"other writer")
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()),
                             ["2026-01-01.csv.gz", "2026-01-02.csv.gz"])


if __name__ == "__main__":
    unittest.main()
