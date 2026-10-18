"""Tests for bot.strategy.modes.position_rules: the prices of a position and the gates of an entry.

Every expected number is worked out here from the rules of the document (the bracket percents, a
slippage of 0.01 %, the weekly window, a pause of 15 minutes, six entries per pair and ten in total
per day, a volatility guard of twice the median) and never computed with the code under test. A
time is built from a WIB wall-clock reading by integer arithmetic on the epoch, and the weekly
window is also checked against an oracle that does not use ``datetime`` at all, so the rule and the
tests do not share one mistake about the calendar.
"""

import ast
import dataclasses
import math
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bot.core.entry_window import parse_weekly_window
from bot.strategy.modes import position_rules
from bot.strategy.modes.position_rules import (
    LONG,
    SHORT,
    Bracket,
    adverse_fill,
    bracket_prices,
    entry_window_open,
    pause_over,
    under_daily_caps,
    volatility_ok,
)

WINDOW = parse_weekly_window("mon 07:00-sat 00:00")
WIB = timezone(timedelta(hours=7))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MS = timedelta(milliseconds=1)
MINUTE_MS = 60_000
WEEK_MINUTES = 7 * 24 * 60

MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 10, 5) + timedelta(days=k) for k in range(7))
NEW_YEAR_MON = date(2025, 12, 29)  # a Monday: its week runs into 2026


def wib_ms(day, hour=0, minute=0, second=0, millisecond=0):
    """UTC milliseconds of a WIB wall-clock time on ``day``, by integer arithmetic."""
    local = datetime(
        day.year, day.month, day.day, hour, minute, second, millisecond * 1000, tzinfo=WIB
    )
    return (local - EPOCH) // MS


def utc_ms(day, hour, minute=0):
    """UTC milliseconds of a UTC wall-clock time on ``day``."""
    return (datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc) - EPOCH) // MS


def reference_open(t_ms):
    """Open from Monday 07:00 to Saturday 00:00 WIB, from the epoch alone (no ``datetime``).

    The epoch, 1970-01-01, was a Thursday: three days after a Monday."""
    wib_minutes = (t_ms + 7 * 60 * MINUTE_MS) // MINUTE_MS  # minutes since 1970-01-01 00:00 WIB
    minute_of_week = (wib_minutes + 3 * 24 * 60) % WEEK_MINUTES
    return 7 * 60 <= minute_of_week < 5 * 24 * 60


def short_repr(value):
    """A value as a short label for a subtest header (``10**400`` has 401 digits)."""
    text = repr(value)
    return text if len(text) <= 20 else text[:17] + "..."


class CalendarOfTheTestsTests(unittest.TestCase):
    def test_the_days_are_the_weekdays_they_are_named_after(self):
        days = (MON, TUE, WED, THU, FRI, SAT, SUN)

        self.assertEqual([d.weekday() for d in days], [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(NEW_YEAR_MON.weekday(), 0)

    def test_the_time_helpers_agree_on_one_instant(self):
        # Monday 07:00 in Jakarta is Monday 00:00 UTC.
        self.assertEqual(wib_ms(MON, 7), utc_ms(MON, 0))
        self.assertEqual(wib_ms(MON, 0), utc_ms(MON - timedelta(days=1), 17))

    def test_the_oracle_knows_the_edges_of_the_window(self):
        self.assertFalse(reference_open(wib_ms(MON, 6, 59)))
        self.assertTrue(reference_open(wib_ms(MON, 7)))
        self.assertTrue(reference_open(wib_ms(FRI, 23, 59)))
        self.assertFalse(reference_open(wib_ms(SAT, 0)))
        self.assertFalse(reference_open(wib_ms(SUN, 12)))


class SidesTests(unittest.TestCase):
    def test_a_long_is_plus_one_and_a_short_is_minus_one(self):
        self.assertEqual((LONG, SHORT), (1, -1))


class BracketTests(unittest.TestCase):
    def test_brackets(self):
        long = bracket_prices(100.0, LONG, 0.40, 0.25)
        short = bracket_prices(100.0, SHORT, 0.20, 0.20)

        self.assertAlmostEqual(long.tp, 100.4)
        self.assertAlmostEqual(long.sl, 99.75)
        self.assertAlmostEqual(short.tp, 99.8)
        self.assertAlmostEqual(short.sl, 100.2)

    def test_the_take_profit_and_the_stop_each_use_their_own_percent(self):
        short = bracket_prices(100.0, SHORT, 0.40, 0.25)
        swapped = bracket_prices(100.0, LONG, 0.25, 0.40)

        self.assertAlmostEqual(short.tp, 99.6)
        self.assertAlmostEqual(short.sl, 100.25)
        self.assertAlmostEqual(swapped.tp, 100.25)
        self.assertAlmostEqual(swapped.sl, 99.6)

    def test_the_levels_are_a_percent_of_the_entry_fill(self):
        for entry in (0.5, 100.0, 63_250.5):
            with self.subTest(entry=entry):
                long = bracket_prices(entry, LONG, 0.40, 0.25)
                self.assertAlmostEqual(long.tp / entry, 1.004)
                self.assertAlmostEqual(long.sl / entry, 0.9975)

    def test_a_long_takes_profit_above_and_stops_below_and_a_short_the_other_way(self):
        for entry in (0.5, 100.0, 63_250.5):
            for tp_pct, sl_pct in ((0.40, 0.25), (0.20, 0.20), (1.0, 0.05)):
                with self.subTest(entry=entry, tp_pct=tp_pct, sl_pct=sl_pct):
                    long = bracket_prices(entry, LONG, tp_pct, sl_pct)
                    short = bracket_prices(entry, SHORT, tp_pct, sl_pct)
                    self.assertGreater(long.tp, entry)
                    self.assertLess(long.sl, entry)
                    self.assertLess(short.tp, entry)
                    self.assertGreater(short.sl, entry)

    def test_a_short_is_the_mirror_of_a_long_around_the_entry(self):
        long = bracket_prices(250.0, LONG, 0.40, 0.25)
        short = bracket_prices(250.0, SHORT, 0.40, 0.25)

        self.assertAlmostEqual(long.tp - 250.0, 250.0 - short.tp)
        self.assertAlmostEqual(250.0 - long.sl, short.sl - 250.0)

    def test_a_bracket_is_a_frozen_pair_of_take_profit_then_stop(self):
        bracket = Bracket(101.0, 99.0)

        self.assertEqual([f.name for f in dataclasses.fields(Bracket)], ["tp", "sl"])
        self.assertEqual((bracket.tp, bracket.sl), (101.0, 99.0))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            bracket.tp = 0.0

    def test_a_side_that_is_not_long_or_short_is_refused(self):
        for side in (0, 2, -2, 0.5, 1.0, -1.0, True, False, None, "LONG"):
            with self.subTest(side=side), self.assertRaises(ValueError):
                bracket_prices(100.0, side, 0.40, 0.25)

    def test_an_entry_fill_that_is_not_a_positive_number_is_refused(self):
        for entry in (0.0, -100.0, math.nan, math.inf, None, "100", 10**400, True):
            with self.subTest(entry=short_repr(entry)), self.assertRaises(ValueError):
                bracket_prices(entry, LONG, 0.40, 0.25)

    def test_a_percent_that_would_misplace_a_level_is_refused(self):
        for bad in (0.0, -0.25, math.nan, math.inf, None, "0.4", 10**400, True):
            with self.subTest(bad=short_repr(bad)):
                with self.assertRaises(ValueError):
                    bracket_prices(100.0, LONG, bad, 0.25)
                with self.assertRaises(ValueError):
                    bracket_prices(100.0, LONG, 0.40, bad)


class AdverseFillTests(unittest.TestCase):
    def test_adverse_fills(self):
        long_open = adverse_fill(100.0, LONG, 0.01, closing=False)
        long_close = adverse_fill(100.0, LONG, 0.01, closing=True)
        short_open = adverse_fill(100.0, SHORT, 0.01, closing=False)
        short_close = adverse_fill(100.0, SHORT, 0.01, closing=True)

        self.assertAlmostEqual(long_open, 100.0 * 1.0001)
        self.assertAlmostEqual(long_close, 100.0 * 0.9999)
        self.assertAlmostEqual(short_open, 100.0 * 0.9999)
        self.assertAlmostEqual(short_close, 100.0 * 1.0001)

    def test_a_fill_on_a_real_price_moves_by_one_basis_point(self):
        price = 63_250.5  # one basis point of it is 6.32505

        self.assertAlmostEqual(adverse_fill(price, LONG, 0.01, False), 63_256.82505, places=6)
        self.assertAlmostEqual(adverse_fill(price, LONG, 0.01, True), 63_244.17495, places=6)
        self.assertAlmostEqual(adverse_fill(price, SHORT, 0.01, False), 63_244.17495, places=6)
        self.assertAlmostEqual(adverse_fill(price, SHORT, 0.01, True), 63_256.82505, places=6)

    def test_a_fill_never_moves_in_favour_of_the_position(self):
        # A LONG pays more on the way in and gets less on the way out; a SHORT the other way round.
        for side in (LONG, SHORT):
            with self.subTest(side=side):
                self.assertGreater(side * (adverse_fill(100.0, side, 0.01, False) - 100.0), 0)
                self.assertLess(side * (adverse_fill(100.0, side, 0.01, True) - 100.0), 0)

    def test_no_slippage_leaves_the_price_alone(self):
        for side in (LONG, SHORT):
            for closing in (False, True):
                with self.subTest(side=side, closing=closing):
                    self.assertEqual(adverse_fill(100.0, side, 0.0, closing), 100.0)

    def test_the_slippage_is_a_percent(self):
        self.assertAlmostEqual(adverse_fill(100.0, LONG, 0.05, False), 100.05)
        self.assertAlmostEqual(adverse_fill(100.0, LONG, 1.0, True), 99.0)
        self.assertAlmostEqual(adverse_fill(200.0, SHORT, 0.5, False), 199.0)

    def test_the_closing_flag_may_be_given_by_position(self):
        self.assertAlmostEqual(adverse_fill(100.0, LONG, 0.01, False), 100.01)
        self.assertAlmostEqual(adverse_fill(100.0, LONG, 0.01, True), 99.99)

    def test_a_side_that_is_not_long_or_short_is_refused(self):
        for side in (0, 2, 0.5, 1.0, -1.0, True, None, "SHORT"):
            with self.subTest(side=side), self.assertRaises(ValueError):
                adverse_fill(100.0, side, 0.01, False)

    def test_a_closing_flag_that_is_not_a_bool_is_refused(self):
        for closing in (0, 1, None, "open", "close"):
            with self.subTest(closing=closing), self.assertRaises(ValueError):
                adverse_fill(100.0, LONG, 0.01, closing)

    def test_a_slippage_that_would_help_the_position_is_refused(self):
        for slippage in (-0.01, math.nan, math.inf, None, "0.01", 10**400, True, False):
            with self.subTest(slippage=short_repr(slippage)), self.assertRaises(ValueError):
                adverse_fill(100.0, LONG, slippage, False)

    def test_a_price_that_is_not_a_positive_number_is_refused(self):
        for price in (0.0, -100.0, math.nan, math.inf, None, "100", 10**400, True):
            with self.subTest(price=short_repr(price)), self.assertRaises(ValueError):
                adverse_fill(price, LONG, 0.01, False)


class EntryWindowTests(unittest.TestCase):
    def test_window_edges(self):
        self.assertIs(entry_window_open(WINDOW, wib_ms(MON, 6, 59)), False)
        self.assertIs(entry_window_open(WINDOW, wib_ms(MON, 7)), True)
        self.assertIs(entry_window_open(WINDOW, wib_ms(FRI, 23, 59)), True)
        self.assertIs(entry_window_open(WINDOW, wib_ms(SAT, 0)), False)

    def test_the_edges_hold_to_the_millisecond(self):
        before_open = wib_ms(MON, 6, 59, 59, 999)
        before_close = wib_ms(FRI, 23, 59, 59, 999)

        self.assertIs(entry_window_open(WINDOW, before_open), False)
        self.assertIs(entry_window_open(WINDOW, before_open + 1), True)
        self.assertIs(entry_window_open(WINDOW, before_close), True)
        self.assertIs(entry_window_open(WINDOW, before_close + 1), False)

    def test_the_window_is_read_in_wib_and_not_in_utc(self):
        sunday_before = MON - timedelta(days=1)
        cases = (
            ("Sun 23:59 UTC is Mon 06:59 WIB: closed", utc_ms(sunday_before, 23, 59), False),
            ("Mon 00:00 UTC is Mon 07:00 WIB: open", utc_ms(MON, 0), True),
            ("Mon 03:00 UTC is Mon 10:00 WIB: open", utc_ms(MON, 3), True),
            ("Fri 12:00 UTC is Fri 19:00 WIB: open", utc_ms(FRI, 12), True),
            ("Fri 16:59 UTC is Fri 23:59 WIB: open", utc_ms(FRI, 16, 59), True),
            ("Fri 17:00 UTC is Sat 00:00 WIB: closed", utc_ms(FRI, 17), False),
            ("Fri 20:00 UTC is Sat 03:00 WIB: closed", utc_ms(FRI, 20), False),
        )
        for label, t_ms, expected in cases:
            with self.subTest(label):
                self.assertIs(entry_window_open(WINDOW, t_ms), expected)

    def test_every_minute_of_two_weeks_matches_the_calendar(self):
        # The second week runs across New Year, so the month and the year change inside it.
        for monday in (MON, NEW_YEAR_MON):
            start = wib_ms(monday)
            wrong = []
            open_minutes = 0
            for minute in range(WEEK_MINUTES):
                first = start + minute * MINUTE_MS
                expected = reference_open(first)
                open_minutes += expected
                # The same minute, to its last millisecond, reads the same.
                got = (
                    entry_window_open(WINDOW, first),
                    entry_window_open(WINDOW, first + MINUTE_MS - 1),
                )
                if got != (expected, expected):
                    wrong.append(minute)
            with self.subTest(monday=monday):
                self.assertEqual(wrong, [])
                self.assertEqual(open_minutes, 5 * 24 * 60 - 7 * 60)  # Mon 07:00 to Sat 00:00

    def test_no_window_is_always_open(self):
        for t_ms in (wib_ms(SAT, 3), wib_ms(SUN, 12), wib_ms(MON, 0), wib_ms(MON, 6, 59)):
            with self.subTest(t_ms=t_ms):
                self.assertIs(entry_window_open(None, t_ms), True)

    def test_the_window_that_is_passed_is_the_one_applied(self):
        tuesday_morning = parse_weekly_window("tue 10:00-tue 12:00")

        self.assertIs(entry_window_open(tuesday_morning, wib_ms(TUE, 9, 59)), False)
        self.assertIs(entry_window_open(tuesday_morning, wib_ms(TUE, 10)), True)
        self.assertIs(entry_window_open(tuesday_morning, wib_ms(TUE, 11, 59)), True)
        self.assertIs(entry_window_open(tuesday_morning, wib_ms(TUE, 12)), False)
        self.assertIs(entry_window_open(tuesday_morning, wib_ms(WED, 10)), False)


class PauseTests(unittest.TestCase):
    EXIT = wib_ms(TUE, 10, 30)

    def test_pause_of_exactly_15_minutes_is_over(self):
        self.assertIs(pause_over(self.EXIT + 15 * MINUTE_MS, self.EXIT, 15), True)

    def test_a_millisecond_before_the_end_of_the_pause_is_not_over(self):
        self.assertIs(pause_over(self.EXIT + 15 * MINUTE_MS - 1, self.EXIT, 15), False)

    def test_a_signal_at_the_exit_or_before_it_is_inside_the_pause(self):
        self.assertIs(pause_over(self.EXIT, self.EXIT, 15), False)
        self.assertIs(pause_over(self.EXIT - 1, self.EXIT, 15), False)
        self.assertIs(pause_over(self.EXIT + MINUTE_MS, self.EXIT, 15), False)

    def test_long_after_the_exit_the_pause_is_over(self):
        self.assertIs(pause_over(self.EXIT + 24 * 60 * MINUTE_MS, self.EXIT, 15), True)

    def test_a_pair_that_has_not_exited_yet_is_never_paused(self):
        self.assertIs(pause_over(0, None, 15), True)
        self.assertIs(pause_over(self.EXIT, None, 15), True)

    def test_the_pause_is_counted_in_minutes(self):
        self.assertIs(pause_over(self.EXIT + 15 * MINUTE_MS, self.EXIT, 30), False)
        self.assertIs(pause_over(self.EXIT + 30 * MINUTE_MS - 1, self.EXIT, 30), False)
        self.assertIs(pause_over(self.EXIT + 30 * MINUTE_MS, self.EXIT, 30), True)

    def test_no_pause_is_over_at_the_exit_itself(self):
        self.assertIs(pause_over(self.EXIT, self.EXIT, 0), True)
        self.assertIs(pause_over(self.EXIT - 1, self.EXIT, 0), False)


class DailyCapsTests(unittest.TestCase):
    def test_daily_caps(self):
        cases = (
            ("nothing opened today", 0, 0, True),
            ("one entry short of both caps", 5, 9, True),
            ("the pair reached its cap", 6, 9, False),
            ("the total reached its cap", 5, 10, False),
            ("both reached their caps", 6, 10, False),
            ("the pair is over its cap", 7, 3, False),
            ("the total is over its cap", 3, 11, False),
            ("the pair is full, the total is not", 6, 7, False),
            ("the total is full, the pair is not", 2, 10, False),
            ("far below both", 1, 2, True),
        )
        for label, pair_today, total_today, expected in cases:
            with self.subTest(label):
                self.assertIs(under_daily_caps(pair_today, total_today, 6, 10), expected)

    def test_each_cap_is_applied_to_its_own_count(self):
        # A pair count of 7 is under the total cap of 10 but over the pair cap of 6, and the
        # reverse for a total of 7: swapping the two counts or the two caps would show here.
        self.assertIs(under_daily_caps(7, 3, 6, 10), False)
        self.assertIs(under_daily_caps(3, 7, 6, 10), True)

    def test_the_caps_that_are_passed_are_the_ones_applied(self):
        self.assertIs(under_daily_caps(1, 2, 2, 3), True)
        self.assertIs(under_daily_caps(2, 2, 2, 3), False)
        self.assertIs(under_daily_caps(1, 3, 2, 3), False)

    def test_the_arguments_may_be_given_by_name(self):
        self.assertIs(under_daily_caps(pair_today=5, total_today=9, max_pair=6, max_total=10), True)
        self.assertIs(
            under_daily_caps(pair_today=6, total_today=9, max_pair=6, max_total=10), False
        )


class VolatilityGuardTests(unittest.TestCase):
    def test_volatility_guard_equal_to_twice_the_median_passes(self):
        self.assertIs(volatility_ok(2.0, 1.0, 2.0), True)

    def test_above_twice_the_median_is_skipped(self):
        self.assertIs(volatility_ok(2.0000001, 1.0, 2.0), False)
        self.assertIs(volatility_ok(2.5, 1.0, 2.0), False)
        self.assertIs(volatility_ok(250.0, 100.0, 2.0), False)

    def test_below_twice_the_median_passes(self):
        self.assertIs(volatility_ok(1.9999999, 1.0, 2.0), True)
        self.assertIs(volatility_ok(1.0, 1.0, 2.0), True)
        self.assertIs(volatility_ok(0.0, 1.0, 2.0), True)

    def test_a_missing_value_is_skipped(self):
        self.assertIs(volatility_ok(None, 1.0, 2.0), False)
        self.assertIs(volatility_ok(1.0, None, 2.0), False)
        self.assertIs(volatility_ok(None, None, 2.0), False)

    def test_a_value_that_is_not_a_number_is_skipped(self):
        self.assertIs(volatility_ok(math.nan, 1.0, 2.0), False)
        self.assertIs(volatility_ok(1.0, math.nan, 2.0), False)
        self.assertIs(volatility_ok(math.inf, 1.0, 2.0), False)

    def test_the_multiplier_that_is_passed_is_the_one_applied(self):
        self.assertIs(volatility_ok(2.5, 1.0, 3.0), True)
        self.assertIs(volatility_ok(3.0, 1.0, 3.0), True)
        self.assertIs(volatility_ok(3.5, 1.0, 3.0), False)
        self.assertIs(volatility_ok(1.5, 1.0, 1.0), False)

    def test_the_limit_is_exact_to_the_last_float(self):
        for median in (0.1, 1234.5678, 63.25):
            limit = 2.0 * median  # a doubling is exact
            with self.subTest(median=median):
                self.assertIs(volatility_ok(limit, median, 2.0), True)
                self.assertIs(volatility_ok(math.nextafter(limit, math.inf), median, 2.0), False)
                self.assertIs(volatility_ok(math.nextafter(limit, 0.0), median, 2.0), True)


def imported_names(source):
    """Every dotted name that an import statement of ``source`` reaches."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + (f"{node.module}." if node.module else "")
            names.update(prefix + alias.name for alias in node.names)
    return names


def reaches_research_or_indicators(name):
    parts = name.split(".")
    return "research" in parts or "pine_indicators" in parts


class ModuleBoundaryTests(unittest.TestCase):
    def test_the_module_imports_no_indicator_or_research_code(self):
        source = Path(position_rules.__file__).read_text(encoding="utf-8")

        offenders = sorted(n for n in imported_names(source) if reaches_research_or_indicators(n))

        self.assertEqual(offenders, [])

    def test_the_module_reuses_the_live_weekly_window(self):
        source = Path(position_rules.__file__).read_text(encoding="utf-8")

        self.assertIn("bot.core.entry_window.window_is_open", imported_names(source))

    def test_the_import_guard_sees_every_form_of_an_import(self):
        source = (
            "import research.pine_gate.bars\n"
            "from research.mode_backtest import frames\n"
            "from bot.strategy import pine_indicators\n"
            "from bot.strategy.pine_indicators import ema\n"
            "from . import pine_indicators as local\n"
            "from bot.core.entry_window import window_is_open\n"
            "import math\n"
        )

        flagged = sorted(n for n in imported_names(source) if reaches_research_or_indicators(n))

        self.assertEqual(
            flagged,
            [
                ".pine_indicators",
                "bot.strategy.pine_indicators",
                "bot.strategy.pine_indicators.ema",
                "research.mode_backtest.frames",
                "research.pine_gate.bars",
            ],
        )


if __name__ == "__main__":
    unittest.main()
