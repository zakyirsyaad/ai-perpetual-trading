"""Tests for research.mode_backtest.simulate (exit part): the 1m exit walk and the money of a trade.

Every expected number is worked out here from the rules of the design document (sections 2.6 and
9.1, decisions D6 and D7) and never computed with the code under test. The reference trades were
also worked out in exact decimal arithmetic, and the money of generated trades is compared with an
oracle written in ``Fraction``s, so a rounding habit of ``float`` cannot hide a wrong formula.

The bars of a test are flat at 100.0 except the few that decide the outcome, so one rule at a time
is under test. A LONG enters at 100.0 with its take-profit 0.40 % above and its stop-loss 0.25 %
below; a SHORT is the mirror image at the same distances. The levels are written out as numbers,
and each SHORT case is written out by hand and not derived from the LONG one: a helper that
mirrors the bars could repeat the mistake of the code it checks.
"""

import dataclasses
import hashlib
import itertools
import math
import random
import tempfile
import unittest
from array import array
from collections import Counter
from datetime import timedelta
from fractions import Fraction
from pathlib import Path

from bot.strategy.modes.config import load_modes_config
from bot.strategy.modes.position_rules import LONG, SHORT, adverse_fill, bracket_prices
from bot.strategy.modes.regime import NAIK
from research.mode_backtest.data import load_pair
from research.mode_backtest.frames import build_pair_frames
from research.mode_backtest.simulate import (
    EXIT_SL,
    EXIT_TP,
    EXIT_WAKTU,
    MODE_A,
    ExitOutcome,
    Money,
    RunSpec,
    resolve_exit,
    settle,
    simulate_run,
)
from research.mode_backtest.tradelog import trade_log_text
from research.pine_gate.bars import bars_from_rows, concat, wib_day_start_ms
from research.pine_gate.outcomes import bracket_outcome
from research.pine_gate.spec import ExecConfig
from tests.mode_backtest_helpers import MINUTE_MS, minute_rows
from tests.mode_backtest_stages import ALL_DAYS, REPO_CONFIG, SYMBOL, write_naik_market

START_MS = 29_833_334 * MINUTE_MS  # a minute boundary in October 2026
LIMIT_A = 90  # Mode A's time limit, minutes
LIMIT_D = 60  # Mode D's time limit, minutes

LONG_TP, LONG_SL = 100.4, 99.75
SHORT_TP, SHORT_SL = 99.6, 100.25

# A bar in a test is written (open, high, low, close).


def flat(n, start_ms=START_MS):
    """``n`` flat 1m bars at 100.0 from ``start_ms``."""
    return minute_rows(start_ms, n)


def with_bar(rows, idx, bar):
    """``rows`` with bar ``idx`` replaced by ``bar``, an (open, high, low, close) tuple."""
    o, h, low, c = bar
    return rows[:idx] + [(rows[idx][0], o, h, low, c, 1.0)] + rows[idx + 1 :]


def bars_of(rows):
    return bars_from_rows(rows, MINUTE_MS)


def series(edits, n=120):
    """Flat bars with the bars of ``edits`` (``{index: (open, high, low, close)}``) replaced."""
    rows = flat(n)
    for idx, bar in edits.items():
        rows = with_bar(rows, idx, bar)
    return bars_of(rows)


def walk(side, tp, sl, edits, n=120, entry_idx=0, limit_min=LIMIT_A):
    """``resolve_exit`` over flat bars with the bars of ``edits`` replaced."""
    return resolve_exit(series(edits, n), entry_idx, side, tp, sl, limit_min)


def walk_long(edits, **kwargs):
    return walk(LONG, LONG_TP, LONG_SL, edits, **kwargs)


def walk_short(edits, **kwargs):
    return walk(SHORT, SHORT_TP, SHORT_SL, edits, **kwargs)


class ExitOutcomeTests(unittest.TestCase):
    def test_the_outcome_holds_the_five_documented_fields_in_order(self):
        names = [field.name for field in dataclasses.fields(ExitOutcome)]

        self.assertEqual(names, ["reason", "exit_idx", "raw_price", "gapped", "both_touched"])

    def test_the_three_reasons_are_the_words_of_the_log(self):
        self.assertEqual((EXIT_TP, EXIT_SL, EXIT_WAKTU), ("TP", "SL", "WAKTU"))

    def test_an_outcome_cannot_be_edited(self):
        outcome = ExitOutcome(EXIT_TP, 1, 100.4, False, False)

        with self.assertRaises(dataclasses.FrozenInstanceError):
            outcome.reason = EXIT_SL


class TouchTests(unittest.TestCase):
    def test_a_bar_through_the_take_profit_exits_at_the_take_profit_price(self):
        long = walk_long({5: (100.0, 100.5, 100.0, 100.0)})
        short = walk_short({5: (100.0, 100.0, 99.5, 100.0)})

        self.assertEqual(long, ExitOutcome(EXIT_TP, 5, LONG_TP, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_TP, 5, SHORT_TP, False, False))

    def test_a_bar_through_the_stop_loss_exits_at_the_stop_price(self):
        long = walk_long({3: (100.0, 100.0, 99.6, 100.0)})
        short = walk_short({3: (100.0, 100.3, 100.0, 100.0)})

        self.assertEqual(long, ExitOutcome(EXIT_SL, 3, LONG_SL, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_SL, 3, SHORT_SL, False, False))

    def test_sl_wins_a_bar_that_touches_both(self):
        long = walk_long({4: (100.0, 100.5, 99.6, 100.0)})
        short = walk_short({4: (100.0, 100.3, 99.5, 100.0)})

        self.assertEqual(long, ExitOutcome(EXIT_SL, 4, LONG_SL, False, True))
        self.assertEqual(short, ExitOutcome(EXIT_SL, 4, SHORT_SL, False, True))

    def test_both_touched_belongs_to_the_deciding_bar_only(self):
        # The stop on bar 3 and the take-profit on bar 5 are different bars: the first one decides
        # and nothing was touched twice. The same holds when the take-profit comes first.
        sl_first_long = walk_long({3: (100.0, 100.0, 99.6, 100.0), 5: (100.0, 100.5, 100.0, 100.0)})
        tp_first_long = walk_long({3: (100.0, 100.5, 100.0, 100.0), 5: (100.0, 100.0, 99.6, 100.0)})
        sl_first_short = walk_short(
            {3: (100.0, 100.3, 100.0, 100.0), 5: (100.0, 100.0, 99.5, 100.0)}
        )
        tp_first_short = walk_short(
            {3: (100.0, 100.0, 99.5, 100.0), 5: (100.0, 100.3, 100.0, 100.0)}
        )

        self.assertEqual(sl_first_long, ExitOutcome(EXIT_SL, 3, LONG_SL, False, False))
        self.assertEqual(tp_first_long, ExitOutcome(EXIT_TP, 3, LONG_TP, False, False))
        self.assertEqual(sl_first_short, ExitOutcome(EXIT_SL, 3, SHORT_SL, False, False))
        self.assertEqual(tp_first_short, ExitOutcome(EXIT_TP, 3, SHORT_TP, False, False))

    def test_a_take_profit_touch_is_never_marked_as_a_double_touch(self):
        # A bar that reaches the take-profit and not the stop has nothing to break a tie over.
        long = walk_long({6: (100.0, 100.5, 99.8, 100.0)})
        short = walk_short({6: (100.0, 100.2, 99.5, 100.0)})

        self.assertEqual(long, ExitOutcome(EXIT_TP, 6, LONG_TP, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_TP, 6, SHORT_TP, False, False))

    def test_a_bar_that_just_reaches_a_level_touches_it(self):
        # Inclusive: low <= sl and high >= tp for a LONG, high >= sl and low <= tp for a SHORT.
        long_tp = walk_long({5: (100.0, LONG_TP, 100.0, 100.0)})
        long_sl = walk_long({5: (100.0, 100.0, LONG_SL, 100.0)})
        short_tp = walk_short({5: (100.0, 100.0, SHORT_TP, 100.0)})
        short_sl = walk_short({5: (100.0, SHORT_SL, 100.0, 100.0)})

        self.assertEqual(long_tp, ExitOutcome(EXIT_TP, 5, LONG_TP, False, False))
        self.assertEqual(long_sl, ExitOutcome(EXIT_SL, 5, LONG_SL, False, False))
        self.assertEqual(short_tp, ExitOutcome(EXIT_TP, 5, SHORT_TP, False, False))
        self.assertEqual(short_sl, ExitOutcome(EXIT_SL, 5, SHORT_SL, False, False))

    def test_a_bar_that_just_misses_both_levels_touches_neither(self):
        # One float short of each level: high just under the take-profit and low just over the
        # stop for a LONG, high just under the stop and low just over the take-profit for a SHORT.
        long_near_miss = (
            100.0,
            math.nextafter(LONG_TP, 0.0),
            math.nextafter(LONG_SL, math.inf),
            100.0,
        )
        short_near_miss = (
            100.0,
            math.nextafter(SHORT_SL, 0.0),
            math.nextafter(SHORT_TP, math.inf),
            100.0,
        )

        long = walk_long({5: long_near_miss})
        short = walk_short({5: short_near_miss})

        self.assertEqual(long, ExitOutcome(EXIT_WAKTU, 90, 100.0, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_WAKTU, 90, 100.0, False, False))

    def test_the_stop_loss_and_the_take_profit_are_not_swapped_between_the_sides(self):
        # A rise through 100.4 is a take-profit for a LONG, and for a SHORT (stop 100.25) a stop.
        rise = {5: (100.0, 100.5, 100.0, 100.0)}

        self.assertEqual(walk_long(rise).reason, EXIT_TP)
        self.assertEqual(walk_short(rise).reason, EXIT_SL)
        # A fall through 99.6 is the reverse.
        fall = {5: (100.0, 100.0, 99.5, 100.0)}
        self.assertEqual(walk_long(fall).reason, EXIT_SL)
        self.assertEqual(walk_short(fall).reason, EXIT_TP)


class GapTests(unittest.TestCase):
    def test_bar_opening_beyond_sl_exits_at_its_open(self):
        long = walk_long({3: (99.5, 99.6, 99.4, 99.55)})
        short = walk_short({3: (100.4, 100.5, 100.35, 100.45)})

        self.assertEqual(long, ExitOutcome(EXIT_SL, 3, 99.5, True, False))
        self.assertEqual(short, ExitOutcome(EXIT_SL, 3, 100.4, True, False))

    def test_bar_opening_beyond_tp_exits_at_its_open(self):
        long = walk_long({3: (100.6, 100.7, 100.55, 100.65)})
        short = walk_short({3: (99.4, 99.45, 99.3, 99.35)})

        self.assertEqual(long, ExitOutcome(EXIT_TP, 3, 100.6, True, False))
        self.assertEqual(short, ExitOutcome(EXIT_TP, 3, 99.4, True, False))

    def test_an_open_exactly_at_a_level_is_a_gap(self):
        long_sl = walk_long({3: (LONG_SL, 100.0, LONG_SL, 100.0)})
        long_tp = walk_long({3: (LONG_TP, LONG_TP, 100.0, 100.0)})
        short_sl = walk_short({3: (SHORT_SL, SHORT_SL, 100.0, 100.0)})
        short_tp = walk_short({3: (SHORT_TP, 100.0, SHORT_TP, 100.0)})

        self.assertEqual(long_sl, ExitOutcome(EXIT_SL, 3, LONG_SL, True, False))
        self.assertEqual(long_tp, ExitOutcome(EXIT_TP, 3, LONG_TP, True, False))
        self.assertEqual(short_sl, ExitOutcome(EXIT_SL, 3, SHORT_SL, True, False))
        self.assertEqual(short_tp, ExitOutcome(EXIT_TP, 3, SHORT_TP, True, False))

    def test_an_open_just_inside_a_level_is_a_touch_and_not_a_gap(self):
        # One float inside the level the bar still reaches it, but it did not open beyond it.
        long_sl = walk_long({3: (math.nextafter(LONG_SL, math.inf), 100.0, 99.7, 100.0)})
        long_tp = walk_long({3: (math.nextafter(LONG_TP, 0.0), 100.5, 100.0, 100.0)})
        short_sl = walk_short({3: (math.nextafter(SHORT_SL, 0.0), 100.3, 100.0, 100.0)})
        short_tp = walk_short({3: (math.nextafter(SHORT_TP, math.inf), 100.0, 99.5, 100.0)})

        self.assertEqual(long_sl, ExitOutcome(EXIT_SL, 3, LONG_SL, False, False))
        self.assertEqual(long_tp, ExitOutcome(EXIT_TP, 3, LONG_TP, False, False))
        self.assertEqual(short_sl, ExitOutcome(EXIT_SL, 3, SHORT_SL, False, False))
        self.assertEqual(short_tp, ExitOutcome(EXIT_TP, 3, SHORT_TP, False, False))

    def test_a_gap_at_the_stop_exits_at_the_open_even_if_the_bar_then_reaches_the_target(self):
        long = walk_long({3: (99.5, 100.5, 99.4, 100.0)})
        short = walk_short({3: (100.4, 100.5, 99.5, 100.0)})

        # Open first, so the stop fills at the open; the order inside the minute is not unknown,
        # so this is a gap and not a tie the stop-first rule had to break.
        self.assertEqual(long, ExitOutcome(EXIT_SL, 3, 99.5, True, False))
        self.assertEqual(short, ExitOutcome(EXIT_SL, 3, 100.4, True, False))

    def test_a_gap_at_the_target_exits_at_the_open_even_if_the_bar_then_reaches_the_stop(self):
        long = walk_long({3: (100.6, 100.7, 99.7, 100.0)})
        short = walk_short({3: (99.4, 100.3, 99.3, 100.0)})

        self.assertEqual(long, ExitOutcome(EXIT_TP, 3, 100.6, True, False))
        self.assertEqual(short, ExitOutcome(EXIT_TP, 3, 99.4, True, False))

    def test_the_gap_price_is_the_open_and_not_the_level_or_the_extreme(self):
        deep = walk_long({3: (99.0, 99.2, 98.0, 98.5)})

        self.assertEqual(deep, ExitOutcome(EXIT_SL, 3, 99.0, True, False))

    def test_a_level_already_passed_at_the_entry_open_exits_at_that_open(self):
        # The walk starts on the entry bar and applies the same rules to it. A stop above the
        # entry open is behind the market from the first bar. The document's percents never give
        # that, but the walk is given the levels and not the percents, and a slippage larger than
        # the stop distance would.
        long = resolve_exit(series({}), 0, LONG, 101.0, 100.5, LIMIT_A)
        short = resolve_exit(series({}), 0, SHORT, 99.5, 99.8, LIMIT_A)

        self.assertEqual(long, ExitOutcome(EXIT_SL, 0, 100.0, True, False))
        self.assertEqual(short, ExitOutcome(EXIT_SL, 0, 100.0, True, False))


class TimeExitTests(unittest.TestCase):
    def test_time_exit_at_the_open_of_minute_90_before_checking_levels(self):
        # Bar 90 opens inside the levels, but its range runs through both of them: the limit is
        # checked first, so the exit is at the bar's open and no level is looked at.
        long = walk_long({90: (100.2, 100.5, 99.6, 100.0)})
        short = walk_short({90: (99.8, 100.3, 99.5, 100.0)})

        self.assertEqual(long, ExitOutcome(EXIT_WAKTU, 90, 100.2, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_WAKTU, 90, 99.8, False, False))

    def test_the_time_exit_is_checked_before_an_open_gap_of_its_bar(self):
        beyond_sl_long = walk_long({90: (99.5, 99.6, 99.4, 99.55)})
        beyond_tp_long = walk_long({90: (100.6, 100.7, 100.55, 100.65)})
        beyond_sl_short = walk_short({90: (100.4, 100.5, 100.35, 100.45)})
        beyond_tp_short = walk_short({90: (99.4, 99.45, 99.3, 99.35)})

        self.assertEqual(beyond_sl_long, ExitOutcome(EXIT_WAKTU, 90, 99.5, False, False))
        self.assertEqual(beyond_tp_long, ExitOutcome(EXIT_WAKTU, 90, 100.6, False, False))
        self.assertEqual(beyond_sl_short, ExitOutcome(EXIT_WAKTU, 90, 100.4, False, False))
        self.assertEqual(beyond_tp_short, ExitOutcome(EXIT_WAKTU, 90, 99.4, False, False))

    def test_the_last_bar_before_the_limit_still_counts(self):
        long = walk_long({89: (100.0, 100.5, 100.0, 100.0)})
        short = walk_short({89: (100.0, 100.3, 100.0, 100.0)})

        self.assertEqual(long, ExitOutcome(EXIT_TP, 89, LONG_TP, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_SL, 89, SHORT_SL, False, False))

    def test_nothing_after_the_limit_is_looked_at(self):
        long = walk_long({91: (100.0, 100.5, 99.6, 100.0), 92: (99.0, 99.1, 98.9, 99.0)})

        self.assertEqual(long, ExitOutcome(EXIT_WAKTU, 90, 100.0, False, False))

    def test_the_limit_counts_minutes_from_the_entry_bar(self):
        bars = series({}, n=130)

        outcome = resolve_exit(bars, 10, LONG, LONG_TP, LONG_SL, LIMIT_A)

        self.assertEqual(outcome.exit_idx, 100)
        self.assertEqual(bars.open_ms[100] - bars.open_ms[10], LIMIT_A * MINUTE_MS)

    def test_another_limit_is_honoured(self):
        long = walk_long({}, limit_min=LIMIT_D)
        short = walk_short({60: (99.8, 100.3, 99.5, 100.0)}, limit_min=LIMIT_D)

        self.assertEqual(long, ExitOutcome(EXIT_WAKTU, 60, 100.0, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_WAKTU, 60, 99.8, False, False))

    def test_a_limit_of_one_minute_exits_at_the_open_of_the_next_bar(self):
        outcome = walk_long({1: (100.1, 100.2, 100.0, 100.15)}, limit_min=1)

        self.assertEqual(outcome, ExitOutcome(EXIT_WAKTU, 1, 100.1, False, False))

    def test_the_entry_bar_still_counts_before_a_limit_of_one_minute(self):
        outcome = walk_long({0: (100.0, 100.5, 100.0, 100.0)}, limit_min=1)

        self.assertEqual(outcome, ExitOutcome(EXIT_TP, 0, LONG_TP, False, False))


class EntryBarTests(unittest.TestCase):
    def test_the_entry_bar_range_counts(self):
        # The entry fills at the open of the entry bar, so everything that bar traded after its
        # open happened with the position on.
        long_tp = walk_long({0: (100.0, 100.5, 100.0, 100.0)})
        long_sl = walk_long({0: (100.0, 100.0, 99.6, 100.0)})
        long_both = walk_long({0: (100.0, 100.5, 99.6, 100.0)})
        short_tp = walk_short({0: (100.0, 100.0, 99.5, 100.0)})
        short_sl = walk_short({0: (100.0, 100.3, 100.0, 100.0)})
        short_both = walk_short({0: (100.0, 100.3, 99.5, 100.0)})

        self.assertEqual(long_tp, ExitOutcome(EXIT_TP, 0, LONG_TP, False, False))
        self.assertEqual(long_sl, ExitOutcome(EXIT_SL, 0, LONG_SL, False, False))
        self.assertEqual(long_both, ExitOutcome(EXIT_SL, 0, LONG_SL, False, True))
        self.assertEqual(short_tp, ExitOutcome(EXIT_TP, 0, SHORT_TP, False, False))
        self.assertEqual(short_sl, ExitOutcome(EXIT_SL, 0, SHORT_SL, False, False))
        self.assertEqual(short_both, ExitOutcome(EXIT_SL, 0, SHORT_SL, False, True))

    def test_the_entry_bar_is_the_one_at_entry_idx_and_not_the_first_bar(self):
        long = walk_long({7: (100.0, 100.0, 99.6, 100.0)}, entry_idx=7)
        short = walk_short({7: (100.0, 100.3, 100.0, 100.0)}, entry_idx=7)

        self.assertEqual(long, ExitOutcome(EXIT_SL, 7, LONG_SL, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_SL, 7, SHORT_SL, False, False))

    def test_bars_before_the_entry_bar_are_never_looked_at(self):
        before = {
            2: (100.0, 100.5, 99.6, 100.0),  # would be a double touch
            6: (99.0, 99.1, 98.9, 99.0),  # would be a gap at the stop
        }

        long = walk_long(before, entry_idx=7, n=130)
        short = walk_short(before, entry_idx=7, n=130)

        self.assertEqual(long, ExitOutcome(EXIT_WAKTU, 97, 100.0, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_WAKTU, 97, 100.0, False, False))


class DataEndTests(unittest.TestCase):
    def test_data_ending_first_returns_none(self):
        # Bars 0..89 and no level touched: the bar of the time exit, 90, is not in the data.
        self.assertIsNone(walk_long({}, n=90))
        self.assertIsNone(walk_short({}, n=90))
        self.assertIsNone(walk_long({}, n=40))

    def test_the_bar_of_the_time_exit_is_enough(self):
        long = walk_long({}, n=91)
        short = walk_short({}, n=91)

        self.assertEqual(long, ExitOutcome(EXIT_WAKTU, 90, 100.0, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_WAKTU, 90, 100.0, False, False))

    def test_an_entry_on_the_last_bar_without_a_touch_returns_none(self):
        self.assertIsNone(walk_long({}, n=50, entry_idx=49))

    def test_a_level_touched_before_the_data_ends_still_decides(self):
        long = walk_long({38: (100.0, 100.5, 100.0, 100.0)}, n=40)
        short = walk_short({39: (100.0, 100.3, 100.0, 100.0)}, n=40)
        on_the_entry_bar = walk_long({49: (100.0, 100.0, 99.6, 100.0)}, n=50, entry_idx=49)

        self.assertEqual(long, ExitOutcome(EXIT_TP, 38, LONG_TP, False, False))
        self.assertEqual(short, ExitOutcome(EXIT_SL, 39, SHORT_SL, False, False))
        self.assertEqual(on_the_entry_bar, ExitOutcome(EXIT_SL, 49, LONG_SL, False, False))

    def test_a_late_touch_that_the_limit_already_passed_is_not_a_reason_for_none(self):
        outcome = walk_long({95: (100.0, 100.5, 100.0, 100.0)}, n=96)

        self.assertEqual(outcome.reason, EXIT_WAKTU)


class HoleTests(unittest.TestCase):
    """A hole in the minutes is the end of the data: what happened in it is not known."""

    def with_hole(self, first, after_minutes, second):
        """Two runs of 1m bars: ``first`` bars from minute 0, ``second`` from ``after_minutes``."""
        return concat(
            bars_of(minute_rows(START_MS, first)),
            bars_of(minute_rows(START_MS + after_minutes * MINUTE_MS, second)),
        )

    def test_a_missing_minute_ends_the_walk(self):
        bars = self.with_hole(50, 51, 80)  # minute 50 is missing

        self.assertIsNone(resolve_exit(bars, 0, LONG, LONG_TP, LONG_SL, LIMIT_A))
        self.assertIsNone(resolve_exit(bars, 0, SHORT, SHORT_TP, SHORT_SL, LIMIT_A))

    def test_a_level_after_the_hole_is_not_taken(self):
        rows_after = with_bar(
            minute_rows(START_MS + 52 * MINUTE_MS, 80), 3, (100.0, 100.5, 99.6, 100.0)
        )
        bars = concat(bars_of(minute_rows(START_MS, 50)), bars_of(rows_after))

        self.assertIsNone(resolve_exit(bars, 0, LONG, LONG_TP, LONG_SL, LIMIT_A))

    def test_a_level_before_the_hole_still_decides(self):
        rows_before = with_bar(minute_rows(START_MS, 50), 30, (100.0, 100.5, 100.0, 100.0))
        bars = concat(bars_of(rows_before), bars_of(minute_rows(START_MS + 60 * MINUTE_MS, 80)))

        outcome = resolve_exit(bars, 0, LONG, LONG_TP, LONG_SL, LIMIT_A)

        self.assertEqual(outcome, ExitOutcome(EXIT_TP, 30, LONG_TP, False, False))

    def test_a_hole_where_the_time_exit_bar_should_be_is_not_a_time_exit(self):
        # Bars 0..89 are there, minute 90 is not, and the data goes on at minute 91.
        bars = self.with_hole(90, 91, 40)

        self.assertIsNone(resolve_exit(bars, 0, LONG, LONG_TP, LONG_SL, LIMIT_A))

    def test_a_hole_just_before_the_time_exit_bar_is_not_a_time_exit_either(self):
        bars = self.with_hole(89, 90, 40)  # bars 0..88, then minute 90 as index 89

        self.assertIsNone(resolve_exit(bars, 0, LONG, LONG_TP, LONG_SL, LIMIT_A))

    def test_a_hole_before_the_entry_bar_is_not_part_of_the_walk(self):
        bars = self.with_hole(10, 12, 120)  # the entry is the first bar after the hole

        outcome = resolve_exit(bars, 10, LONG, LONG_TP, LONG_SL, LIMIT_A)

        self.assertEqual(outcome, ExitOutcome(EXIT_WAKTU, 100, 100.0, False, False))

    def test_two_runs_that_join_without_a_hole_walk_like_one(self):
        bars = self.with_hole(50, 50, 80)

        outcome = resolve_exit(bars, 0, LONG, LONG_TP, LONG_SL, LIMIT_A)

        self.assertEqual(outcome, ExitOutcome(EXIT_WAKTU, 90, 100.0, False, False))

    def test_bars_that_repeat_or_go_back_in_time_end_the_walk(self):
        # "Exactly one minute after the one before": a repeated minute and a minute that goes back
        # are no better than a missing one. ``bars_from_rows`` would refuse such bars, but a
        # ``Bars`` can be made by hand. From bar 5 on every bar is moved back by whole minutes, so
        # the only wrong step is the one into bar 5 and none after it looks like a forward gap.
        good = series({}, n=120)
        for label, minutes_back in (("repeat", 1), ("back", 3)):
            stamps = array("q", good.open_ms)
            for k in range(5, len(stamps)):
                stamps[k] -= minutes_back * MINUTE_MS
            bars = dataclasses.replace(good, open_ms=stamps)
            with self.subTest(case=label):
                self.assertIsNone(resolve_exit(bars, 0, LONG, LONG_TP, LONG_SL, LIMIT_A))
                self.assertIsNone(resolve_exit(bars, 0, SHORT, SHORT_TP, SHORT_SL, LIMIT_A))


# --- a plain restatement of the rules, for the random walks below ---------------------------------


def restated_exit(rows, entry_idx, side, tp, sl, limit_min):
    """The walk written out per side with no helper, over plain rows: the rules, not the code.

    Returns ``(reason, index, raw price, gapped, both touched)`` or ``None``."""
    entry_ms = rows[entry_idx][0]
    for i in range(entry_idx, len(rows)):
        t, bar_open, high, low, _close, _volume = rows[i]
        if i > entry_idx and t != rows[i - 1][0] + MINUTE_MS:
            return None
        if t == entry_ms + limit_min * MINUTE_MS:
            return ("WAKTU", i, bar_open, False, False)
        if side == LONG:
            if bar_open <= sl:
                return ("SL", i, bar_open, True, False)
            if bar_open >= tp:
                return ("TP", i, bar_open, True, False)
            if low <= sl:
                return ("SL", i, sl, False, high >= tp)
            if high >= tp:
                return ("TP", i, tp, False, False)
        else:
            if bar_open >= sl:
                return ("SL", i, bar_open, True, False)
            if bar_open <= tp:
                return ("TP", i, bar_open, True, False)
            if high >= sl:
                return ("SL", i, sl, False, low <= tp)
            if low <= tp:
                return ("TP", i, tp, False, False)
    return None


TICK = 0.05  # prices sit on a grid, so a bar lands exactly on a level now and then


def price_of(ticks):
    return round(ticks * TICK, 2)


def random_rows(rng, n, hole_chance):
    """``n`` bars of a random walk on the tick grid, some minutes missing, as plain rows."""
    rows, minute, close = [], 0, 2000
    for _ in range(n):
        minute += 2 if rng.random() < hole_chance else 1
        bar_open = close + (rng.randint(-5, 5) if rng.random() < 0.25 else 0)
        close = bar_open + rng.randint(-3, 3)
        high = max(bar_open, close) + (rng.randint(0, 4) if rng.random() < 0.6 else 0)
        low = min(bar_open, close) - (rng.randint(0, 4) if rng.random() < 0.6 else 0)
        rows.append(
            (
                START_MS + minute * MINUTE_MS,
                price_of(bar_open),
                price_of(high),
                price_of(low),
                price_of(close),
                1.0,
            )
        )
    return rows


def bars_with_holes(rows):
    """``Bars`` of the rows, joined run by run so that a missing minute stays missing."""
    runs, run = [], [rows[0]]
    for row in rows[1:]:
        if row[0] == run[-1][0] + MINUTE_MS:
            run.append(row)
        else:
            runs.append(run)
            run = [row]
    runs.append(run)
    return concat(*(bars_of(r) for r in runs))


def kind_of(side, outcome):
    """The kind of an outcome, for counting what the random walks reached."""
    if outcome is None:
        return (side, "none")
    reason, _idx, _raw, gapped, both = outcome
    return (side, reason, "gap" if gapped else "touch", "both" if both else "single")


class RandomWalkTests(unittest.TestCase):
    CASES = 4_000

    @classmethod
    def setUpClass(cls):
        rng = random.Random(20261017)
        cls.results = []
        for _ in range(cls.CASES):
            rows = random_rows(rng, rng.randint(5, 130), hole_chance=rng.choice((0.0, 0.0, 0.03)))
            entry_idx = rng.randint(0, max(0, len(rows) // 3))
            side = rng.choice((LONG, SHORT))
            entry_open = round(rows[entry_idx][1] / TICK)
            tp_ticks, sl_ticks = rng.choice(((4, 3), (8, 5), (20, 15), (40, 30), (3, 8)))
            if side == LONG:
                tp, sl = price_of(entry_open + tp_ticks), price_of(entry_open - sl_ticks)
            else:
                tp, sl = price_of(entry_open - tp_ticks), price_of(entry_open + sl_ticks)
            limit_min = rng.choice((1, 2, 3, 5, 10, 30, 60, 90))
            actual = resolve_exit(bars_with_holes(rows), entry_idx, side, tp, sl, limit_min)
            expected = restated_exit(rows, entry_idx, side, tp, sl, limit_min)
            cls.results.append((side, rows, entry_idx, tp, sl, limit_min, actual, expected))

    def test_random_walks_agree_with_a_plain_restatement_of_the_rules(self):
        for side, _rows, entry_idx, tp, sl, limit_min, actual, expected in self.results:
            with self.subTest(side=side, entry_idx=entry_idx, tp=tp, sl=sl, limit_min=limit_min):
                if expected is None:
                    self.assertIsNone(actual)
                else:
                    self.assertEqual(dataclasses.astuple(actual), expected)

    def test_the_random_walks_reach_every_kind_of_outcome_on_both_sides(self):
        counts = Counter(kind_of(r[0], r[7]) for r in self.results)
        kinds = [
            (side, *rest)
            for side in (LONG, SHORT)
            for rest in (
                ("none",),
                ("TP", "touch", "single"),
                ("TP", "gap", "single"),
                ("SL", "touch", "single"),
                ("SL", "touch", "both"),
                ("SL", "gap", "single"),
                ("WAKTU", "touch", "single"),
            )
        ]

        for kind in kinds:
            with self.subTest(kind=kind):
                self.assertGreaterEqual(counts[kind], 25, counts)

    def test_the_random_walks_include_holes_and_levels_hit_exactly(self):
        with_hole = sum(
            any(b[0] - a[0] != MINUTE_MS for a, b in itertools.pairwise(r[1])) for r in self.results
        )
        on_a_level = sum(
            any(price in (r[3], r[4]) for row in r[1] for price in row[1:5]) for r in self.results
        )

        self.assertGreaterEqual(with_hole, 100)
        self.assertGreaterEqual(on_a_level, 1_000)


class PineCascadeCrossCheckTests(unittest.TestCase):
    """``resolve_exit`` and the Pine gate's ``bracket_outcome`` walk a bar in the same order: an
    open beyond the stop, an open beyond the target, a touch of the stop (also when the bar
    reaches both), a touch of the target. Nothing else ties the two, so a fix to one would part
    them without a word; here they are held together on random walks.

    Pine fills at the close of its entry bar ``E`` and walks from ``E + 1``. So the mode's walk
    starts on bar ``E + 1``, with Pine's own levels and a limit one minute shorter, which puts
    both limits on bar ``E + time_stop_min``. That bar is the one deliberate difference (spec D7):
    Pine still reads its range and exits a time stop at its close, while the mode exits at its
    open without looking."""

    def test_both_walks_end_alike_except_on_the_time_limit_bar(self):
        rng = random.Random(20261018)
        counts = Counter()
        for case in range(2_500):
            # Pine's own bracket and limit, and wider or shorter ones, so time exits come too.
            cfg = dataclasses.replace(ExecConfig(), bracket_pct=rng.choice((0.2, 0.2, 0.5, 1.0)),
                                      time_stop_min=rng.choice((2, 3, 5, 10, 60)))
            rows = random_rows(rng, rng.randint(10, 3 * cfg.time_stop_min + 10), hole_chance=0.0)
            bars = bars_of(rows)
            entry = rng.randint(0, len(rows) - 2)
            side = rng.choice((LONG, SHORT))
            fill = bars.close[entry] * (1 + side * cfg.entry_slip_bps / 10_000)
            take = fill * (1 + side * cfg.bracket_pct / 100)
            stop = fill * (1 - side * cfg.bracket_pct / 100)
            limit_idx = entry + cfg.time_stop_min

            pine = bracket_outcome(bars, entry, side, cfg)
            mode = resolve_exit(bars, entry + 1, side, take, stop, cfg.time_stop_min - 1)

            with self.subTest(case=case, side=side, entry=entry):
                if pine is None or mode is None:
                    self.assertEqual((pine, mode), (None, None))
                    counts["data ends first"] += 1
                elif mode.reason == EXIT_WAKTU:
                    self.assertEqual((pine.exit_idx, mode.exit_idx), (limit_idx, limit_idx))
                    self.assertEqual(mode.raw_price, bars.open[limit_idx])
                    counts["time limit" if pine.kind == "TIME" else "D7: Pine decides it"] += 1
                else:
                    self.assertEqual(
                        (pine.kind, pine.exit_idx, pine.raw_exit_price, pine.gapped,
                         pine.both_touched),
                        (mode.reason, mode.exit_idx, mode.raw_price, mode.gapped,
                         mode.both_touched),
                    )
                    counts[(mode.reason, "gap" if mode.gapped else "touch",
                            "both" if mode.both_touched else "single")] += 1

        # The comparison is not vacuous: every branch of both cascades is reached.
        for kind in ((EXIT_TP, "touch", "single"), (EXIT_TP, "gap", "single"),
                     (EXIT_SL, "touch", "single"), (EXIT_SL, "touch", "both"),
                     (EXIT_SL, "gap", "single"), "time limit", "D7: Pine decides it",
                     "data ends first"):
            with self.subTest(kind=kind):
                self.assertGreaterEqual(counts[kind], 10, counts)


class WalkArgumentTests(unittest.TestCase):
    def setUp(self):
        self.bars = series({}, n=120)

    def resolve(self, **overrides):
        args = dict(
            bars_1m=self.bars,
            entry_idx=0,
            side=LONG,
            tp=LONG_TP,
            sl=LONG_SL,
            limit_min=LIMIT_A,
        )
        args.update(overrides)
        return resolve_exit(**args)

    def test_the_valid_arguments_of_the_helper_resolve(self):
        self.assertEqual(self.resolve().reason, EXIT_WAKTU)

    def test_bars_that_are_not_one_minute_are_refused(self):
        three_minutes = bars_from_rows(
            [(START_MS + i * 3 * MINUTE_MS, 100.0, 100.0, 100.0, 100.0, 1.0) for i in range(100)],
            3 * MINUTE_MS,
        )

        with self.assertRaises(ValueError):
            self.resolve(bars_1m=three_minutes)

    def test_a_side_that_is_not_long_or_short_is_refused(self):
        # With the levels of a LONG and with those of a SHORT, so that the order of the levels
        # cannot be what refuses it.
        for side in (0, 2, -2, True, False, None, 1.0, "LONG"):
            for tp, sl in ((LONG_TP, LONG_SL), (SHORT_TP, SHORT_SL)):
                with self.subTest(side=side, tp=tp), self.assertRaises(ValueError):
                    self.resolve(side=side, tp=tp, sl=sl)

    def test_an_entry_index_outside_the_bars_is_refused(self):
        for entry_idx in (-1, 120, 500, True, 1.0, None, "0"):
            with self.subTest(entry_idx=entry_idx), self.assertRaises(ValueError):
                self.resolve(entry_idx=entry_idx)

    def test_the_first_and_the_last_bar_are_valid_entries(self):
        self.assertIsNotNone(self.resolve(entry_idx=0))
        self.assertIsNone(self.resolve(entry_idx=119))

    def test_a_limit_that_is_not_a_whole_positive_number_of_minutes_is_refused(self):
        for limit_min in (0, -1, -90, 1.5, 90.0, True, None, "90"):
            with self.subTest(limit_min=limit_min), self.assertRaises(ValueError):
                self.resolve(limit_min=limit_min)

    def test_a_level_that_is_not_a_finite_positive_number_is_refused(self):
        bad = (0, 0.0, -1.0, math.nan, math.inf, -math.inf, None, "100", True, 10**400)
        for value in bad:
            label = repr(value)[:20]
            with self.subTest(tp=label), self.assertRaises(ValueError):
                self.resolve(tp=value)
            with self.subTest(sl=label), self.assertRaises(ValueError):
                self.resolve(sl=value)

    def test_levels_on_the_wrong_side_of_each_other_are_refused(self):
        # A LONG takes profit above its stop and a SHORT below it. Equal levels have no side.
        for side, tp, sl in (
            (LONG, 99.75, 100.4),
            (LONG, 100.0, 100.0),
            (SHORT, 100.25, 99.6),
            (SHORT, 100.0, 100.0),
        ):
            with self.subTest(side=side, tp=tp, sl=sl), self.assertRaises(ValueError):
                self.resolve(side=side, tp=tp, sl=sl)

    def test_the_bars_are_left_as_they_were(self):
        before = [
            list(getattr(self.bars, name)) for name in ("open_ms", "open", "high", "low", "close")
        ]

        self.resolve()
        self.resolve(side=SHORT, tp=SHORT_TP, sl=SHORT_SL)

        after = [
            list(getattr(self.bars, name)) for name in ("open_ms", "open", "high", "low", "close")
        ]
        self.assertEqual(before, after)


# The reference trades of the brief: from an open of 100 with 25,000 notional, a fee of 0.035 %
# and a slippage of 0.01 %. Take profit +0.40 % and stop-loss -0.25 % of the fill (100.01), so the
# levels are 100.41004 and 99.759975. The figures are the brief's, rounded to 6 decimals.
NOTIONAL, FEE_PCT, SLIPPAGE_PCT = 25_000.0, 0.035, 0.01
TP_RAW, SL_RAW = 100.41004, 99.759975
BRIEF_TP = dict(
    entry_fill=100.01,
    gross=97.490000,
    fee_usdt=17.534122,
    pnl_usdt=79.955879,
    slippage_usdt=5.009750,
    volume_usdt=50097.490000,
)
BRIEF_SL = dict(pnl_usdt=-82.471002, fee_usdt=17.477252, volume_usdt=49935.006250)

# The same trades in exact decimal arithmetic. The brief's pnl and fee of the take-profit trade
# end in a 5 at the 7th decimal (79.9558785 and 17.5341215), where a float can fall on either side
# of the rounding, so those are compared with a tolerance of one unit of the 6th decimal and the
# exact values are pinned beside them at nine decimals.
EXACT_LONG_TP = dict(
    entry_fill=100.01,
    exit_fill=100.399998996,
    qty=249.97500249975,
    fee_usdt=17.5341215,
    slippage_usdt=5.009750024998,
    pnl_usdt=79.9558785,
    volume_usdt=50097.49,
)
EXACT_LONG_SL = dict(
    entry_fill=100.01,
    exit_fill=99.7499990025,
    qty=249.97500249975,
    fee_usdt=17.4772521875,
    slippage_usdt=4.993500024998,
    pnl_usdt=-82.4710021875,
    volume_usdt=49935.00625,
)
# Mode D's SHORT: take-profit -0.20 % and stop-loss +0.20 % of the fill (99.99).
SHORT_TP_RAW, SHORT_SL_RAW = 99.79002, 100.18998
EXACT_SHORT_TP = dict(
    entry_fill=99.99,
    exit_fill=99.799999002,
    qty=250.02500250025,
    fee_usdt=17.48337325,
    slippage_usdt=4.995250025003,
    pnl_usdt=30.02162675,
    volume_usdt=49952.495,
)
EXACT_SHORT_SL = dict(
    entry_fill=99.99,
    exit_fill=100.199998998,
    qty=250.02500250025,
    fee_usdt=17.51837675,
    slippage_usdt=5.005250025003,
    pnl_usdt=-70.02337675,
    volume_usdt=50052.505,
)
# A trade that closes at the price it opened at: the 0.07 % of fees and the 0.02 % of slippage.
EXACT_LONG_FLAT = dict(
    entry_fill=100.01,
    exit_fill=99.99,
    qty=249.97500249975,
    fee_usdt=17.498250174983,
    slippage_usdt=4.999500049995,
    pnl_usdt=-22.497750224978,
    volume_usdt=49995.000499950005,
)


class MoneyReferenceTests(unittest.TestCase):
    def money(self, entry_open, raw_exit, side):
        return settle(entry_open, raw_exit, side, NOTIONAL, FEE_PCT, SLIPPAGE_PCT)

    def assert_exact(self, money, expected):
        for name, value in expected.items():
            with self.subTest(field=name):
                self.assertAlmostEqual(getattr(money, name), value, places=9)

    def test_money_on_the_reference_trades(self):
        tp = self.money(100.0, TP_RAW, LONG)
        sl = self.money(100.0, SL_RAW, LONG)

        # The brief's figures, to 6 decimals.
        self.assertAlmostEqual(tp.entry_fill, BRIEF_TP["entry_fill"], places=6)
        self.assertAlmostEqual(tp.pnl_usdt + tp.fee_usdt, BRIEF_TP["gross"], places=6)
        for name in ("fee_usdt", "pnl_usdt", "slippage_usdt", "volume_usdt"):
            with self.subTest(trade="TP", field=name):
                self.assertAlmostEqual(getattr(tp, name), BRIEF_TP[name], delta=1e-6)
        for name in ("fee_usdt", "pnl_usdt", "volume_usdt"):
            with self.subTest(trade="SL", field=name):
                self.assertAlmostEqual(getattr(sl, name), BRIEF_SL[name], delta=1e-6)
        # And the exact values behind them.
        self.assert_exact(tp, EXACT_LONG_TP)
        self.assert_exact(sl, EXACT_LONG_SL)

    def test_the_short_mirror_of_the_reference_trades(self):
        tp = self.money(100.0, SHORT_TP_RAW, SHORT)
        sl = self.money(100.0, SHORT_SL_RAW, SHORT)

        self.assert_exact(tp, EXACT_SHORT_TP)
        self.assert_exact(sl, EXACT_SHORT_SL)

    def test_a_trade_that_closes_where_it_opened_pays_fees_and_slippage(self):
        flat_long = self.money(100.0, 100.0, LONG)

        self.assert_exact(flat_long, EXACT_LONG_FLAT)
        # 7 bps of fee and 2 bps of slippage on 25,000 is 22.50, a little less on the real volume.
        self.assertAlmostEqual(flat_long.pnl_usdt, -22.5, delta=0.01)

    def test_a_trade_without_fee_and_slippage_is_the_price_move_times_the_quantity(self):
        long = settle(100.0, 101.0, LONG, 25_000.0, 0.0, 0.0)
        short = settle(100.0, 99.0, SHORT, 25_000.0, 0.0, 0.0)

        for money, entry_fill, exit_fill in ((long, 100.0, 101.0), (short, 100.0, 99.0)):
            self.assertEqual(money.entry_fill, entry_fill)
            self.assertEqual(money.exit_fill, exit_fill)
            self.assertEqual(money.qty, 250.0)
            self.assertEqual(money.fee_usdt, 0.0)
            self.assertEqual(money.slippage_usdt, 0.0)
            self.assertEqual(money.pnl_usdt, 250.0)
            self.assertEqual(money.volume_usdt, 250.0 * entry_fill + 250.0 * exit_fill)

    def test_a_move_against_the_position_loses_money_on_either_side(self):
        long = settle(100.0, 99.0, LONG, 25_000.0, 0.0, 0.0)
        short = settle(100.0, 101.0, SHORT, 25_000.0, 0.0, 0.0)

        self.assertEqual(long.pnl_usdt, -250.0)
        self.assertEqual(short.pnl_usdt, -250.0)

    def test_the_fills_are_the_adverse_fills_of_the_rule_core(self):
        for side in (LONG, SHORT):
            with self.subTest(side=side):
                money = settle(63_250.5, 63_400.25, side, NOTIONAL, FEE_PCT, SLIPPAGE_PCT)

                self.assertEqual(
                    money.entry_fill, adverse_fill(63_250.5, side, SLIPPAGE_PCT, False)
                )
                self.assertEqual(money.exit_fill, adverse_fill(63_400.25, side, SLIPPAGE_PCT, True))

    def test_the_quantity_is_the_notional_over_the_fill_and_not_over_the_open(self):
        money = self.money(100.0, TP_RAW, LONG)

        self.assertAlmostEqual(money.qty * money.entry_fill, NOTIONAL, places=9)
        self.assertNotAlmostEqual(money.qty, NOTIONAL / 100.0, places=3)

    def test_the_slippage_is_inside_the_fills_and_not_charged_again(self):
        money = self.money(100.0, TP_RAW, LONG)

        gross = money.qty * (money.exit_fill - money.entry_fill)
        self.assertAlmostEqual(money.pnl_usdt, gross - money.fee_usdt, places=9)
        self.assertGreater(money.slippage_usdt, 0.0)

    def test_money_is_a_frozen_record_of_seven_fields_in_order(self):
        names = [field.name for field in dataclasses.fields(Money)]
        money = self.money(100.0, TP_RAW, LONG)

        self.assertEqual(
            names,
            [
                "entry_fill",
                "exit_fill",
                "qty",
                "fee_usdt",
                "slippage_usdt",
                "pnl_usdt",
                "volume_usdt",
            ],
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            money.pnl_usdt = 0.0


def exact_money(entry_open, raw_exit, side, notional, fee_pct, slippage_pct):
    """The money rules of section 9.1 in exact arithmetic: the seven fields of ``Money``."""
    s = Fraction(slippage_pct) / 100
    entry_open, raw_exit, notional = Fraction(entry_open), Fraction(raw_exit), Fraction(notional)
    entry_fill = entry_open * (1 + side * s)
    exit_fill = raw_exit * (1 - side * s)
    qty = notional / entry_fill
    volume = qty * entry_fill + qty * exit_fill
    fee = Fraction(fee_pct) / 100 * volume
    pnl = side * qty * (exit_fill - entry_fill) - fee
    # The cost of the two adjustments, each one counted as a cost whichever way it moved the price.
    slippage = qty * abs(entry_fill - entry_open) + qty * abs(raw_exit - exit_fill)
    return entry_fill, exit_fill, qty, fee, slippage, pnl, volume


class MoneyOracleTests(unittest.TestCase):
    PRICES = (
        (100.0, 100.4),
        (100.0, 99.75),
        (100.0, 100.0),
        (63_250.5, 63_400.25),
        (0.5, 0.49),
        (2_500.0, 2_490.5),
    )

    def test_money_matches_exact_arithmetic_on_a_grid_of_trades(self):
        # The fills are floats, good to about 1e-14, and a pnl is made of their difference times a
        # quantity of a few hundred: a float cannot do better than a few 1e-12 USDT there. A wrong
        # formula is off by far more than a billionth of a USDT.
        grid = list(
            itertools.product(
                (LONG, SHORT), self.PRICES, (0.0, 0.01, 0.1), (0.0, 0.035), (25_000.0, 1_234.5)
            )
        )
        names = [field.name for field in dataclasses.fields(Money)]

        self.assertEqual(len(grid), 2 * 6 * 3 * 2 * 2)
        for side, (entry_open, raw_exit), slippage_pct, fee_pct, notional in grid:
            args = (entry_open, raw_exit, side, notional, fee_pct, slippage_pct)
            actual = dataclasses.astuple(settle(*args))
            for name, value, exact in zip(names, actual, exact_money(*args), strict=True):
                with self.subTest(args=args, field=name):
                    self.assertTrue(
                        math.isclose(value, float(exact), rel_tol=1e-12, abs_tol=1e-9),
                        f"{value!r} != {float(exact)!r}",
                    )


class MoneyArgumentTests(unittest.TestCase):
    def settle(self, **overrides):
        args = dict(
            entry_open=100.0,
            raw_exit=100.4,
            side=LONG,
            notional=NOTIONAL,
            fee_pct=FEE_PCT,
            slippage_pct=SLIPPAGE_PCT,
        )
        args.update(overrides)
        return settle(**args)

    def test_the_valid_arguments_of_the_helper_settle(self):
        self.assertIsInstance(self.settle(), Money)

    def test_a_side_that_is_not_long_or_short_is_refused(self):
        for side in (0, 2, -2, True, None, 1.0, "LONG"):
            with self.subTest(side=side), self.assertRaises(ValueError):
                self.settle(side=side)

    def test_a_price_that_is_not_a_finite_positive_number_is_refused(self):
        bad = (0, 0.0, -1.0, math.nan, math.inf, None, "100", 10**400)
        for value in bad:
            label = repr(value)[:20]
            with self.subTest(entry_open=label), self.assertRaises(ValueError):
                self.settle(entry_open=value)
            with self.subTest(raw_exit=label), self.assertRaises(ValueError):
                self.settle(raw_exit=value)

    def test_a_notional_that_is_not_a_finite_positive_number_is_refused(self):
        for value in (0, 0.0, -25_000.0, math.nan, math.inf, None, "25000", True, 10**400):
            with self.subTest(notional=repr(value)[:20]), self.assertRaises(ValueError):
                self.settle(notional=value)

    def test_a_fee_or_slippage_that_is_negative_or_not_a_number_is_refused(self):
        for value in (-0.001, math.nan, math.inf, None, "0.035", True):
            with self.subTest(fee_pct=value), self.assertRaises(ValueError):
                self.settle(fee_pct=value)
        for value in (-0.001, math.nan, math.inf, None, "0.01", 10**400):
            with self.subTest(slippage_pct=repr(value)[:20]), self.assertRaises(ValueError):
                self.settle(slippage_pct=value)

    def test_a_fee_or_slippage_of_zero_is_allowed(self):
        self.assertEqual(self.settle(fee_pct=0).fee_usdt, 0.0)
        self.assertEqual(self.settle(slippage_pct=0).slippage_usdt, 0.0)

    def test_whole_numbers_are_numbers(self):
        money = settle(100, 101, LONG, 25_000, 0, 0)

        self.assertEqual(money.pnl_usdt, 250.0)


class ComposeTests(unittest.TestCase):
    """What the event loop will do: fill, bracket from the fill, walk, settle (decision D6)."""

    def trade(self, side, tp_pct, sl_pct, edits):
        bars = series(edits)
        entry_open = bars.open[0]
        entry_fill = adverse_fill(entry_open, side, SLIPPAGE_PCT, False)
        bracket = bracket_prices(entry_fill, side, tp_pct, sl_pct)
        outcome = resolve_exit(bars, 0, side, bracket.tp, bracket.sl, LIMIT_A)
        money = settle(entry_open, outcome.raw_price, side, NOTIONAL, FEE_PCT, SLIPPAGE_PCT)
        return bracket, outcome, money

    def test_a_long_that_takes_profit_is_the_reference_take_profit_trade(self):
        bracket, outcome, money = self.trade(LONG, 0.40, 0.25, {5: (100.0, 100.5, 100.0, 100.0)})

        self.assertAlmostEqual(bracket.tp, 100.41004, places=9)
        self.assertAlmostEqual(bracket.sl, 99.759975, places=9)
        self.assertEqual((outcome.reason, outcome.exit_idx), (EXIT_TP, 5))
        self.assertEqual(outcome.raw_price, bracket.tp)
        self.assertAlmostEqual(money.pnl_usdt, EXACT_LONG_TP["pnl_usdt"], places=9)
        self.assertAlmostEqual(money.volume_usdt, EXACT_LONG_TP["volume_usdt"], places=9)

    def test_a_long_that_is_stopped_out_is_the_reference_stop_trade(self):
        bracket, outcome, money = self.trade(LONG, 0.40, 0.25, {4: (100.0, 100.0, 99.7, 100.0)})

        self.assertEqual((outcome.reason, outcome.exit_idx), (EXIT_SL, 4))
        self.assertEqual(outcome.raw_price, bracket.sl)
        self.assertAlmostEqual(money.pnl_usdt, EXACT_LONG_SL["pnl_usdt"], places=9)
        self.assertAlmostEqual(money.fee_usdt, EXACT_LONG_SL["fee_usdt"], places=9)

    def test_a_short_that_takes_profit_is_the_reference_short_trade(self):
        bracket, outcome, money = self.trade(SHORT, 0.20, 0.20, {5: (100.0, 100.0, 99.7, 100.0)})

        self.assertAlmostEqual(bracket.tp, SHORT_TP_RAW, places=9)
        self.assertEqual((outcome.reason, outcome.exit_idx), (EXIT_TP, 5))
        self.assertAlmostEqual(money.pnl_usdt, EXACT_SHORT_TP["pnl_usdt"], places=9)

    def test_a_short_that_is_stopped_out_is_the_reference_short_stop_trade(self):
        bracket, outcome, money = self.trade(SHORT, 0.20, 0.20, {4: (100.0, 100.3, 100.0, 100.0)})

        self.assertAlmostEqual(bracket.sl, SHORT_SL_RAW, places=9)
        self.assertEqual((outcome.reason, outcome.exit_idx), (EXIT_SL, 4))
        self.assertAlmostEqual(money.pnl_usdt, EXACT_SHORT_SL["pnl_usdt"], places=9)

    def test_a_time_exit_settles_at_the_open_of_its_bar(self):
        bracket, outcome, money = self.trade(LONG, 0.40, 0.25, {90: (100.2, 100.3, 100.1, 100.25)})

        self.assertEqual(
            (outcome.reason, outcome.exit_idx, outcome.raw_price), (EXIT_WAKTU, 90, 100.2)
        )
        expected = exact_money(100.0, 100.2, LONG, NOTIONAL, FEE_PCT, SLIPPAGE_PCT)
        self.assertAlmostEqual(money.pnl_usdt, float(expected[5]), places=9)
        self.assertGreater(money.pnl_usdt, 0.0)  # +0.2 % less the 9 bps of costs



# The sha256 of the trade log of Mode A (5m, NAIK, the whole week as one segment) on the synthetic
# week of tests.mode_backtest_stages, taken on the code before mode L's event loop was added (30
# trades). Mode A's walk, money and log must stay as they were.
MODE_A_SYNTHETIC_LOG_SHA = "39aa210b16d9a6ccdf6bf3c78718cdb0cd9411b36dd07cef8913879ca8112431"


class ModeAGoldenTests(unittest.TestCase):
    def test_mode_a_trade_log_unchanged_on_the_synthetic_week(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_naik_market(Path(tmp))
            cfg = load_modes_config(REPO_CONFIG)
            frames = build_pair_frames(load_pair(tmp, SYMBOL, ALL_DAYS[0], ALL_DAYS[-1]), cfg)
        start = wib_day_start_ms(ALL_DAYS[0])
        end = wib_day_start_ms(ALL_DAYS[-1] + timedelta(days=1))

        result = simulate_run(frames, cfg, RunSpec(MODE_A, SYMBOL, "5m", NAIK, start, end))

        self.assertEqual(len(result.trades), 30)
        text = trade_log_text(result.trades)
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), MODE_A_SYNTHETIC_LOG_SHA)


if __name__ == "__main__":
    unittest.main()
