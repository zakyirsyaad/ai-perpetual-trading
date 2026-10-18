"""Tests for research.mode_backtest.live_sim (walk and money): mode L's exit walk and fills.

Expected numbers are worked out by hand from the spec (sections 4 and 5), not with the code under
test. Bars are flat at 100.0 except the few that decide the outcome. A LONG enters at 100.0 with its
stop at 99.72, its cut at 99.90 (active from minute 10) and its take-profit at 100.33; each SHORT
case is written out by hand as the mirror image (100.28, 100.10, 99.67), not derived.
"""

import unittest

from bot.strategy.modes.config import CostConfig
from bot.strategy.modes.live_exit import LiveExitLevels, live_exit_levels
from bot.strategy.modes.position_rules import LONG, SHORT
from research.mode_backtest.live_sim import resolve_live_exit, settle_live
from research.mode_backtest.simulate import (
    EXIT_POTONG,
    EXIT_SL,
    EXIT_TP,
    EXIT_WAKTU,
    ExitOutcome,
)
from research.pine_gate.bars import bars_from_rows, concat
from tests.mode_backtest_helpers import MINUTE_MS, minute_rows
from tests.test_modes_live_exit import rules

START_MS = 29_833_334 * MINUTE_MS
CUT_FROM = 10
LIMIT = 60

LONG_LEVELS = LiveExitLevels(sl=99.72, tp=100.33, cut=99.90, sl_pct=0.28)
LONG_NO_CUT = LiveExitLevels(sl=99.72, tp=100.33, cut=None, sl_pct=0.28)
SHORT_LEVELS = LiveExitLevels(sl=100.28, tp=99.67, cut=100.10, sl_pct=0.28)

COSTS = CostConfig(slippage_entry_bps=0.3, slippage_exit_market_bps=0.5, slippage_tp_bps=0.0)


def series(edits, n=120):
    """Flat 1m bars with the bars of ``edits`` (``{index: (open, high, low, close)}``) replaced."""
    rows = minute_rows(START_MS, n)
    for idx, (o, h, low, c) in edits.items():
        rows[idx] = (rows[idx][0], o, h, low, c, 1.0)
    return bars_from_rows(rows, MINUTE_MS)


def walk(side, levels, edits, n=120, entry_idx=0, cut_from=CUT_FROM, limit=LIMIT):
    return resolve_live_exit(series(edits, n), entry_idx, side, levels, cut_from, limit)


class LongWalkTests(unittest.TestCase):
    def test_cut_touch_before_minute_ten_is_ignored(self):
        # minute 9 dips through the cut (99.90) but not the stop (99.72): nothing happens
        out = walk(LONG, LONG_LEVELS, {9: (100.0, 100.0, 99.85, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_WAKTU, 60, 100.0, False, False))

    def test_cut_touch_from_minute_ten_exits_at_the_cut(self):
        out = walk(LONG, LONG_LEVELS, {10: (100.0, 100.0, 99.85, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_POTONG, 10, 99.90, False, False))

    def test_gap_past_the_cut_at_minute_ten_exits_at_the_open(self):
        # the price fell through the cut during minute 9; minute 10 opens below it
        edits = {9: (100.0, 100.0, 99.80, 99.80), 10: (99.80, 99.80, 99.80, 99.80)}
        out = walk(LONG, LONG_LEVELS, edits)
        self.assertEqual(out, ExitOutcome(EXIT_POTONG, 10, 99.80, True, False))

    def test_gap_past_the_stop_at_minute_ten_is_a_stop_not_a_cut(self):
        out = walk(LONG, LONG_LEVELS, {10: (99.70, 99.70, 99.70, 99.70)})
        self.assertEqual(out, ExitOutcome(EXIT_SL, 10, 99.70, True, False))

    def test_a_gap_past_the_cut_before_minute_ten_does_not_exit(self):
        edits = {5: (99.80, 99.80, 99.80, 99.80), 6: (100.0, 100.0, 100.0, 100.0)}
        self.assertEqual(walk(LONG, LONG_LEVELS, edits).reason, EXIT_WAKTU)

    def test_loss_level_wins_a_bar_that_touches_tp_stop(self):
        out = walk(LONG, LONG_LEVELS, {5: (100.0, 100.40, 99.70, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_SL, 5, 99.72, False, True))

    def test_loss_level_wins_a_bar_that_touches_tp_cut(self):
        out = walk(LONG, LONG_LEVELS, {12: (100.0, 100.40, 99.85, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_POTONG, 12, 99.90, False, True))

    def test_stop_wins_a_bar_that_touches_both_stop_and_cut(self):
        out = walk(LONG, LONG_LEVELS, {12: (100.0, 100.0, 99.70, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_SL, 12, 99.72, False, False))

    def test_sl_still_acts_inside_the_first_ten_minutes(self):
        out = walk(LONG, LONG_LEVELS, {3: (100.0, 100.0, 99.70, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_SL, 3, 99.72, False, False))

    def test_take_profit_touch_exits_at_the_level(self):
        out = walk(LONG, LONG_LEVELS, {4: (100.0, 100.35, 100.0, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_TP, 4, 100.33, False, False))

    def test_open_beyond_the_take_profit_exits_at_the_open(self):
        out = walk(LONG, LONG_LEVELS, {4: (100.50, 100.50, 100.50, 100.50)})
        self.assertEqual(out, ExitOutcome(EXIT_TP, 4, 100.50, True, False))

    def test_time_exit_at_minute_sixty_before_levels(self):
        # the time-exit bar's range would hit the stop and the take-profit; it exits at its open
        out = walk(LONG, LONG_LEVELS, {60: (100.1, 101.0, 99.0, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_WAKTU, 60, 100.1, False, False))

    def test_time_exit_bar_opening_beyond_the_stop_is_still_the_time_exit(self):
        out = walk(LONG, LONG_LEVELS, {60: (99.0, 99.0, 99.0, 99.0)})
        self.assertEqual(out, ExitOutcome(EXIT_WAKTU, 60, 99.0, False, False))

    def test_version_i_has_no_cut(self):
        out = walk(LONG, LONG_NO_CUT, {12: (100.0, 100.0, 99.85, 100.0)})
        self.assertEqual(out.reason, EXIT_WAKTU)
        self.assertIsNone(live_exit_levels(100.003, LONG, 0.10, 25_000.0, rules(), False).cut)

    def test_the_cut_clock_starts_at_the_entry_bar(self):
        edits = {30: (100.0, 100.0, 99.85, 100.0), 40: (100.0, 100.0, 99.85, 100.0)}
        out = walk(LONG, LONG_LEVELS, edits, entry_idx=30)
        # bar 30 is minute 0 of the position, so the cut is first active on bar 40
        self.assertEqual(out, ExitOutcome(EXIT_POTONG, 40, 99.90, False, False))

    def test_data_ending_first_returns_none(self):
        self.assertIsNone(walk(LONG, LONG_LEVELS, {}, n=30))

    def test_a_hole_in_the_minutes_returns_none(self):
        rows = minute_rows(START_MS, 120)
        bars = concat(bars_from_rows(rows[:20], MINUTE_MS), bars_from_rows(rows[21:], MINUTE_MS))
        self.assertIsNone(resolve_live_exit(bars, 0, LONG, LONG_LEVELS, CUT_FROM, LIMIT))


class ShortWalkTests(unittest.TestCase):
    def test_cut_touch_before_minute_ten_is_ignored(self):
        out = walk(SHORT, SHORT_LEVELS, {9: (100.0, 100.15, 100.0, 100.0)})
        self.assertEqual(out.reason, EXIT_WAKTU)

    def test_cut_touch_from_minute_ten_exits_at_the_cut(self):
        out = walk(SHORT, SHORT_LEVELS, {10: (100.0, 100.15, 100.0, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_POTONG, 10, 100.10, False, False))

    def test_gap_past_the_cut_at_minute_ten_exits_at_the_open(self):
        edits = {9: (100.0, 100.20, 100.0, 100.20), 10: (100.20, 100.20, 100.20, 100.20)}
        out = walk(SHORT, SHORT_LEVELS, edits)
        self.assertEqual(out, ExitOutcome(EXIT_POTONG, 10, 100.20, True, False))

    def test_loss_level_wins_a_bar_that_touches_tp(self):
        out = walk(SHORT, SHORT_LEVELS, {12: (100.0, 100.15, 99.60, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_POTONG, 12, 100.10, False, True))
        out = walk(SHORT, SHORT_LEVELS, {5: (100.0, 100.30, 99.60, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_SL, 5, 100.28, False, True))

    def test_sl_acts_inside_the_first_ten_minutes(self):
        out = walk(SHORT, SHORT_LEVELS, {3: (100.0, 100.30, 100.0, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_SL, 3, 100.28, False, False))

    def test_take_profit_and_gap(self):
        out = walk(SHORT, SHORT_LEVELS, {4: (100.0, 100.0, 99.60, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_TP, 4, 99.67, False, False))
        out = walk(SHORT, SHORT_LEVELS, {4: (99.50, 99.50, 99.50, 99.50)})
        self.assertEqual(out, ExitOutcome(EXIT_TP, 4, 99.50, True, False))

    def test_time_exit_at_the_open(self):
        out = walk(SHORT, SHORT_LEVELS, {60: (99.9, 101.0, 99.0, 100.0)})
        self.assertEqual(out, ExitOutcome(EXIT_WAKTU, 60, 99.9, False, False))

    def test_version_i_has_no_cut(self):
        no_cut = LiveExitLevels(sl=100.28, tp=99.67, cut=None, sl_pct=0.28)
        out = walk(SHORT, no_cut, {12: (100.0, 100.15, 100.0, 100.0)})
        self.assertEqual(out.reason, EXIT_WAKTU)


class WalkValidationTests(unittest.TestCase):
    def test_bad_inputs_are_refused(self):
        bars = series({})
        bad = [
            dict(side=0),
            dict(entry_idx=-1),
            dict(entry_idx=120),
            dict(cut_from=0),
            dict(cut_from=2.5),
            dict(limit=0),
        ]
        for change in bad:
            args = dict(side=LONG, entry_idx=0, cut_from=CUT_FROM, limit=LIMIT)
            args.update(change)
            with self.assertRaises(ValueError, msg=str(change)):
                resolve_live_exit(
                    bars, args["entry_idx"], args["side"], LONG_LEVELS, args["cut_from"], args["limit"]
                )

    def test_levels_on_the_wrong_side_are_refused(self):
        bars = series({})
        for levels in (
            LiveExitLevels(sl=100.4, tp=100.33, cut=None, sl_pct=0.28),
            LiveExitLevels(sl=99.72, tp=100.33, cut=99.5, sl_pct=0.28),  # cut beyond the stop
            LiveExitLevels(sl=99.72, tp=100.33, cut=100.5, sl_pct=0.28),  # cut beyond the target
        ):
            with self.assertRaises(ValueError):
                resolve_live_exit(bars, 0, LONG, levels, CUT_FROM, LIMIT)

    def test_non_1m_bars_are_refused(self):
        rows = minute_rows(START_MS, 10)
        five = bars_from_rows(rows[:1], 5 * MINUTE_MS)
        with self.assertRaises(ValueError):
            resolve_live_exit(five, 0, LONG, LONG_LEVELS, CUT_FROM, LIMIT)


class SettleLiveTests(unittest.TestCase):
    """Open 100, notional 25,000, fee 0.035 %, entry 0.3 bps, market exit 0.5 bps, TP 0 bps.

    The levels come from the real formulas at atr 0.10 %; the expected money is the brief's."""

    def levels(self, side):
        entry = 100.0 * (1 + side * 0.3 / 10_000)
        return live_exit_levels(entry, side, 0.10, 25_000.0, rules(), True)

    def money(self, side, kind, raw=None):
        lv = self.levels(side)
        raw = {EXIT_TP: lv.tp, EXIT_SL: lv.sl, EXIT_POTONG: lv.cut}.get(kind, raw)
        out = ExitOutcome(kind, 5, raw, False, False)
        return settle_live(100.0, out, side, 25_000.0, 0.035, COSTS)

    def test_money_per_exit_kind(self):
        long_tp = self.money(LONG, EXIT_TP)
        self.assertAlmostEqual(long_tp.pnl_usdt, 63.512156, places=6)
        self.assertAlmostEqual(long_tp.fee_usdt, 17.528364, places=6)
        self.assertAlmostEqual(long_tp.volume_usdt, 50081.04052, places=5)
        long_sl = self.money(LONG, EXIT_SL)
        self.assertAlmostEqual(long_sl.pnl_usdt, -88.721564, places=6)
        self.assertAlmostEqual(long_sl.volume_usdt, 49928.7535, places=4)
        self.assertAlmostEqual(self.money(LONG, EXIT_POTONG).pnl_usdt, -43.739563, places=6)
        self.assertAlmostEqual(self.money(LONG, EXIT_WAKTU, 100.1).pnl_usdt, 5.490011, places=6)
        self.assertAlmostEqual(self.money(SHORT, EXIT_TP).pnl_usdt, 63.487856, places=6)
        self.assertAlmostEqual(self.money(SHORT, EXIT_SL).pnl_usdt, -88.778439, places=6)

    def test_tp_fill_is_the_level_exactly(self):
        for side in (LONG, SHORT):
            lv = self.levels(side)
            out = ExitOutcome(EXIT_TP, 5, lv.tp, False, False)
            self.assertEqual(settle_live(100.0, out, side, 25_000.0, 0.035, COSTS).exit_fill, lv.tp)

    def test_tp_uses_its_own_slippage_and_the_others_the_market_one(self):
        costs = CostConfig(0.3, 0.5, 1.0)
        tp = ExitOutcome(EXIT_TP, 1, 100.3, False, False)
        m = settle_live(100.0, tp, LONG, 25_000.0, 0.035, costs)
        self.assertAlmostEqual(m.exit_fill, 100.3 * (1 - 1.0 / 10_000), places=9)
        for kind in (EXIT_SL, EXIT_POTONG, EXIT_WAKTU):
            out = ExitOutcome(kind, 1, 100.3, False, False)
            m = settle_live(100.0, out, LONG, 25_000.0, 0.035, costs)
            self.assertAlmostEqual(m.exit_fill, 100.3 * (1 - 0.5 / 10_000), places=9)

    def test_slippage_is_the_cost_of_the_two_adjustments(self):
        m = self.money(LONG, EXIT_WAKTU, 100.1)
        expected = m.qty * abs(m.entry_fill - 100.0) + m.qty * abs(100.1 - m.exit_fill)
        self.assertAlmostEqual(m.slippage_usdt, expected, places=9)

    def test_bad_inputs_are_refused(self):
        out = ExitOutcome(EXIT_SL, 1, 99.0, False, False)
        nan_out = ExitOutcome(EXIT_SL, 1, float("nan"), False, False)
        for args in (
            (0.0, out, LONG, 25_000.0, 0.035, COSTS),
            (100.0, out, 0, 25_000.0, 0.035, COSTS),
            (100.0, out, LONG, 0.0, 0.035, COSTS),
            (100.0, out, LONG, 25_000.0, -0.1, COSTS),
            (100.0, nan_out, LONG, 25_000.0, 0.035, COSTS),
        ):
            with self.assertRaises(ValueError):
                settle_live(*args)


if __name__ == "__main__":
    unittest.main()
