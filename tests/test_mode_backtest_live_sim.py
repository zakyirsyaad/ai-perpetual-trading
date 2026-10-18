"""Tests for research.mode_backtest.live_sim: mode L's exit walk, its fills and its event loop.

Expected numbers are worked out by hand from the spec (sections 4 and 5), not with the code under
test. Bars are flat at 100.0 except the few that decide the outcome. A LONG enters at 100.0 with its
stop at 99.72, its cut at 99.90 (active from minute 10) and its take-profit at 100.33; each SHORT
case is written out by hand as the mirror image (100.28, 100.10, 99.67), not derived.
"""

import dataclasses
import unittest
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from bot.strategy.modes.config import CostConfig, ModesConfigError, load_live_exit_config_with_sha256
from bot.strategy.modes.live_exit import LiveExitLevels, live_exit_levels
from bot.strategy.modes.position_rules import LONG, SHORT
from bot.strategy.modes.regime import NAIK, SIDEWAYS, TURUN
from research.mode_backtest import live_sim
from research.mode_backtest.frames import FrameParams, PairFrames, stretch_and_atr_series
from research.mode_backtest.live_sim import (
    MODE_L,
    LiveRunSpec,
    resolve_live_exit,
    settle_live,
    simulate_live_run,
)
from research.mode_backtest.simulate import (
    EXIT_POTONG,
    EXIT_SL,
    EXIT_TP,
    EXIT_WAKTU,
    ExitOutcome,
)
from research.pine_gate.bars import bars_from_rows, concat
from tests.mode_backtest_helpers import MINUTE_MS, minute_rows
from tests.test_mode_backtest_loop import (
    CFG,
    FAR,
    M3,
    PAIR,
    TUE,
    StubHourly,
    at,
    context,
    counts,
    fixed,
    flat,
    minutes,
    stretch,
    variant,
)
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



# --- the event loop ---------------------------------------------------------------------------------

LIVE_PATH = Path(__file__).resolve().parents[1] / "config" / "modes_live_exit.yaml"
LIVE = load_live_exit_config_with_sha256(LIVE_PATH)[0]
ATR_PCT = 0.2  # 1.5 x 0.2 = 0.30 %: inside the 0.28 to 0.40 band, so the stop is 0.30 % away

MIN = MINUTE_MS
SIGNAL = at(TUE, 10, 0)
START, END = at(TUE, 9), at(TUE, 13)
LONG_FILL = 100.003  # 100.0 open, 0.3 bps against a LONG
SHORT_FILL = 99.997
# Worked by hand from the spec: LONG stop 100.003 x 0.997, cut 100.003 x 0.999, take-profit
# (e x 1.0005 + 56 / (25000 / e)) / 0.9995; the SHORT levels are the mirror image.
LONG_SL, LONG_CUT, LONG_TP = 99.702991, 99.902997, 100.32717
SHORT_SL, SHORT_CUT, SHORT_TP = 100.296991, 100.096997, 99.67317


def live_frames(bars_1m, hourly, stretch_bars, column, cfg=CFG, atr=ATR_PCT, symbol=PAIR):
    """Frames written by hand: the stretch of each candle and, where it is not None, ``atr``."""
    atr_column = tuple(None if value is None else atr for value in column)
    return PairFrames(
        symbol, bars_1m, hourly, MappingProxyType({}), stretch_bars, column,
        FrameParams.of(cfg), atr_column,
    )


def run_live(
    values, start=START, end=END, *, side=LONG, versi="i", gate=None, hourly=None, edits=None,
    cfg=CFG, live=LIVE, atr=ATR_PCT,
):
    """Mode L (LONG) or comparator S (SHORT) over [start, end), a stretch of FAR unless ``values``."""
    gate = gate or (NAIK if side == LONG else TURUN)
    stretch_bars, column = stretch(start, end - M3, values)
    frames = live_frames(
        minutes(start - M3, end, edits),
        hourly or StubHourly(fixed(context(gate))),
        stretch_bars,
        column,
        cfg,
        atr,
    )
    return simulate_live_run(frames, cfg, live, LiveRunSpec(PAIR, side, gate, versi, start, end))


def only(result):
    """The one trade of a run."""
    assert len(result.trades) == 1, result
    return result.trades[0]


class LiveRunSpecTests(unittest.TestCase):
    def test_long_pairs_with_naik_and_short_with_turun(self):
        self.assertEqual(MODE_L, "L")
        for side, gate in ((LONG, NAIK), (SHORT, TURUN)):
            with self.subTest(side=side):
                spec = LiveRunSpec(PAIR, side, gate, "ii", 1, 2)
                self.assertEqual((spec.side, spec.kondisi_gate, spec.versi), (side, gate, "ii"))
        for side, gate in ((LONG, TURUN), (SHORT, NAIK), (LONG, SIDEWAYS), (SHORT, "x")):
            with self.subTest(side=side, gate=gate), self.assertRaises(ValueError):
                LiveRunSpec(PAIR, side, gate, "i", 1, 2)

    def test_only_versions_i_and_ii_exist(self):
        for versi in ("", "I", "iii", "1", None):
            with self.subTest(versi=versi), self.assertRaises(ValueError):
                LiveRunSpec(PAIR, LONG, NAIK, versi, 1, 2)

    def test_the_times_are_whole_and_ordered(self):
        for start, end in ((1.5, 2), (1, 2.5), (5, 4), (True, 3), ("1", 2)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                LiveRunSpec(PAIR, LONG, NAIK, "i", start, end)
        self.assertEqual(LiveRunSpec(PAIR, LONG, NAIK, "i", 5, 5).end_ms, 5)

    def test_a_side_that_is_not_long_or_short_is_refused(self):
        for side in (0, 2, "LONG", None):
            with self.subTest(side=side), self.assertRaises(ValueError):
                LiveRunSpec(PAIR, side, NAIK, "i", 1, 2)


class LiveEntryTests(unittest.TestCase):
    EDITS = {SIGNAL - MIN: flat(99.0), SIGNAL: flat(100.0), SIGNAL + MIN: flat(100.0)}

    def test_l_enters_long_only_in_naik_at_the_signal_open(self):
        trade = only(run_live({SIGNAL: 0.4}, edits=self.EDITS))

        self.assertEqual((trade.mode, trade.pair, trade.side), ("L", PAIR, LONG))
        self.assertEqual((trade.kondisi, trade.versi, trade.faktor_k), (NAIK, "i", 1.0))
        self.assertEqual((trade.signal_ms, trade.entry_ms), (SIGNAL, SIGNAL))
        self.assertAlmostEqual(trade.entry_fill, LONG_FILL, places=6)  # the open of the bar at t
        self.assertEqual(trade.notional_usdt, 25_000.0)

    def test_l_does_not_enter_outside_naik(self):
        for kondisi in (TURUN, SIDEWAYS):
            with self.subTest(kondisi=kondisi):
                hourly = StubHourly(fixed(context(kondisi)))

                result = run_live({SIGNAL: 0.4}, hourly=hourly)

                self.assertEqual((result.trades, counts(result)), ((), {"kondisi": 1}))

    def test_s_enters_short_only_in_turun(self):
        trade = only(run_live({SIGNAL: 0.4}, side=SHORT, edits=self.EDITS))

        self.assertEqual((trade.mode, trade.side, trade.kondisi), ("S", SHORT, TURUN))
        self.assertEqual((trade.signal_ms, trade.entry_ms), (SIGNAL, SIGNAL))
        self.assertAlmostEqual(trade.entry_fill, SHORT_FILL, places=6)
        for kondisi in (NAIK, SIDEWAYS):
            with self.subTest(kondisi=kondisi):
                hourly = StubHourly(fixed(context(kondisi)))

                result = run_live({SIGNAL: 0.4}, side=SHORT, hourly=hourly)

                self.assertEqual((result.trades, counts(result)), ((), {"kondisi": 1}))

    def test_the_band_is_inclusive_and_looks_at_both_sides_of_the_mean(self):
        for value, enters in ((0.75, True), (-0.75, True), (0.0, True), (0.7501, False), (-0.7501, False)):
            with self.subTest(stretch=value):
                result = run_live({SIGNAL: value})

                self.assertEqual(len(result.trades), 1 if enters else 0)
                self.assertEqual(counts(result), {})

    def test_the_condition_is_read_only_at_the_signal_close(self):
        hourly = StubHourly(fixed(context(NAIK)))

        run_live({SIGNAL: 0.4}, hourly=hourly)

        self.assertEqual(hourly.asked, [SIGNAL])

    def test_the_trade_holds_the_levels_the_stop_and_the_walk_decide(self):
        cases = {
            "stop at the floor": (0.1, 0.28),
            "stop from the ATR": (0.2, 0.30),
            "stop at the ceiling": (0.5, 0.40),
        }
        for name, (atr, sl_pct) in cases.items():
            with self.subTest(case=name):
                trade = only(run_live({SIGNAL: 0.4}, atr=atr))

                self.assertAlmostEqual(trade.sl_pct, sl_pct, places=9)
                self.assertAlmostEqual(trade.sl, LONG_FILL * (1 - sl_pct / 100), places=6)

    def test_a_long_takes_profit_at_the_target_without_slippage(self):
        trade = only(run_live({SIGNAL: 0.4}, edits={SIGNAL + 5 * MIN: (100.0, 100.5, 100.0, 100.0)}))

        self.assertAlmostEqual(trade.tp, LONG_TP, places=3)
        self.assertAlmostEqual(trade.sl, LONG_SL, places=5)
        self.assertEqual((trade.exit_reason, trade.exit_ms), ("TP", SIGNAL + 6 * MIN))
        self.assertEqual(trade.exit_fill, trade.tp)  # a resting limit order: no slippage
        self.assertAlmostEqual(trade.pnl_usdt, 63.51, places=1)  # 56 after the estimated fees

    def test_a_short_takes_profit_at_the_mirror_target(self):
        edits = {SIGNAL + 5 * MIN: (100.0, 100.0, 99.5, 100.0)}

        trade = only(run_live({SIGNAL: 0.4}, side=SHORT, edits=edits))

        self.assertAlmostEqual(trade.tp, SHORT_TP, places=3)
        self.assertAlmostEqual(trade.sl, SHORT_SL, places=5)
        self.assertEqual((trade.exit_reason, trade.exit_ms), ("TP", SIGNAL + 6 * MIN))
        self.assertEqual(trade.exit_fill, trade.tp)
        self.assertAlmostEqual(trade.pnl_usdt, 63.49, places=1)

    def test_a_stop_exits_with_the_market_slippage(self):
        trade = only(run_live({SIGNAL: 0.4}, edits={SIGNAL + 5 * MIN: (100.0, 100.0, 99.6, 100.0)}))

        self.assertEqual((trade.exit_reason, trade.gapped), ("SL", False))
        self.assertAlmostEqual(trade.exit_fill, LONG_SL * (1 - 0.5 / 10_000), places=6)
        self.assertAlmostEqual(trade.pnl_usdt, -93.72, places=1)  # 0.30 % + costs

    def test_a_time_exit_leaves_at_the_bar_an_hour_after_the_entry(self):
        edits = {SIGNAL + 60 * MIN: flat(100.2)}

        trade = only(run_live({SIGNAL: 0.4}, edits=edits))

        self.assertEqual((trade.exit_reason, trade.exit_ms), ("WAKTU", SIGNAL + 60 * MIN))
        self.assertAlmostEqual(trade.exit_fill, 100.2 * (1 - 0.5 / 10_000), places=6)

    def test_the_fee_and_notional_come_from_the_general_block(self):
        cfg = variant(umum={"fee_taker_persen": 0.1, "notional_usdt": 10_000.0})

        trade = only(run_live({SIGNAL: 0.4}, cfg=cfg))

        self.assertEqual(trade.notional_usdt, 10_000.0)
        self.assertAlmostEqual(trade.fee_usdt, 0.001 * trade.volume_usdt, places=9)
        self.assertAlmostEqual(trade.qty * trade.entry_fill, 10_000.0, places=6)

    def test_a_pair_whose_factor_is_otomatis_cannot_run(self):
        pairs = dict(CFG.pair)
        pairs[PAIR] = dataclasses.replace(pairs[PAIR], faktor_k=None)
        cfg = dataclasses.replace(CFG, pair=MappingProxyType(pairs))

        with self.assertRaisesRegex(ValueError, "otomatis"):
            run_live({SIGNAL: 0.4}, cfg=cfg)


class LiveGateTests(unittest.TestCase):
    """The gates of phase 1, in phase 1's order, with the 60-minute limit of mode L at the edge."""

    def test_general_gates_apply_in_phase_1_order(self):
        sideways = StubHourly(fixed(context(SIDEWAYS)))
        edge = END - 60 * MIN
        sunday = at(TUE + timedelta(days=5), 12, 0)
        # every signal below also fails the later gates: only the first failure is counted
        cases = (
            # a Sunday: the window is closed, and the edge (60 minutes) and the condition fail too
            ("window", sunday - 180 * MIN, sunday + 60 * MIN, sunday, "window"),
            ("segment_edge", START, END, edge, "segment_edge"),
            ("kondisi", START, END, SIGNAL, "kondisi"),
        )
        for name, start, end, signal, reason in cases:
            with self.subTest(gate=name):
                result = run_live({signal: 0.4}, start, end, hourly=sideways)

                self.assertEqual((result.trades, counts(result)), ((), {reason: 1}))

    def test_the_edge_is_the_time_limit_before_the_end_of_the_segment(self):
        last_ok = END - 63 * MIN  # its time-exit bar opens at END - 3 minutes
        edge = END - 60 * MIN  # its time-exit bar would open at END, in the next segment

        self.assertEqual(len(run_live({last_ok: 0.4}).trades), 1)
        result = run_live({edge: 0.4})
        self.assertEqual((result.trades, counts(result)), ((), {"segment_edge": 1}))

    def test_the_limit_of_the_edge_is_the_one_of_mode_l(self):
        live = dataclasses.replace(LIVE, mode_l=dataclasses.replace(LIVE.mode_l, batas_waktu_menit=90))
        signal = END - 90 * MIN  # fine with the 60-minute limit, at the edge with a 90-minute one

        self.assertEqual(len(run_live({signal: 0.4}).trades), 1)
        result = run_live({signal: 0.4}, live=live)
        self.assertEqual((result.trades, counts(result)), ((), {"segment_edge": 1}))

    def test_a_condition_failure_beats_an_open_position(self):
        def hourly(t_ms):
            return context(SIDEWAYS if t_ms == SIGNAL + 3 * MIN else NAIK)

        result = run_live({SIGNAL: 0.4, SIGNAL + 3 * MIN: 0.4}, hourly=StubHourly(hourly))

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(counts(result), {"kondisi": 1})

    def test_position_open_then_pause_then_daily_cap_then_volatility(self):
        cfg = variant(umum={"maks_entry_per_hari_per_pair": 1})
        first_exit = SIGNAL + 60 * MIN
        values = {
            SIGNAL: 0.4,  # enters, leaves on the time limit at 11:00
            SIGNAL + 3 * MIN: 0.4,  # the position is open
            first_exit + 3 * MIN: 0.4,  # out for 3 minutes: paused for 15
            first_exit + 18 * MIN: 0.4,  # the pause is over, the day is full
            first_exit + 21 * MIN: 0.4,  # also volatile: the cap is counted first
        }

        def hourly(t_ms):
            return context(NAIK, atr14=2.5 if t_ms == first_exit + 21 * MIN else 1.0)

        result = run_live(values, hourly=StubHourly(hourly), cfg=cfg)

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(counts(result), {"position_open": 1, "pause": 1, "daily_cap": 2})

    def test_volatility_blocks_when_the_hourly_atr_is_above_twice_its_median(self):
        for atr14, enters in ((2.0, True), (2.5, False)):
            with self.subTest(atr14=atr14):
                hourly = StubHourly(fixed(context(NAIK, atr14=atr14, median=1.0)))

                result = run_live({SIGNAL: 0.4}, hourly=hourly)

                self.assertEqual(len(result.trades), 1 if enters else 0)
                self.assertEqual(counts(result), {} if enters else {"volatility": 1})

    def test_the_blocked_counts_hold_every_reason_and_cannot_be_edited(self):
        result = run_live({})

        self.assertEqual(list(result.blocked), list(live_sim.BLOCK_REASONS))
        with self.assertRaises(TypeError):
            result.blocked["window"] = 1


class LiveNoSignalTests(unittest.TestCase):
    def test_flat_window_gives_no_signal_and_no_levels(self):
        # Flat prices: the ATR is 0, so the stretch and the ATR percent are None and there is
        # neither a signal nor a stop to price.
        rules = LIVE.mode_l
        flat_bars = minutes(START - 3 * 60 * MIN, END)
        stretch_bars, column, atr_column = stretch_and_atr_series(
            flat_bars, rules.candle_jendela, rules.ema_periode, rules.atr_periode
        )
        self.assertTrue(len(column) > 0 and all(v is None for v in column))
        self.assertTrue(all(v is None for v in atr_column))
        frames = PairFrames(
            PAIR, flat_bars, StubHourly(fixed(context(NAIK))), MappingProxyType({}),
            stretch_bars, column, FrameParams.of(CFG), atr_column,
        )

        with mock.patch.object(live_sim, "live_exit_levels", side_effect=AssertionError("priced")):
            result = simulate_live_run(
                frames, CFG, LIVE, LiveRunSpec(PAIR, LONG, NAIK, "ii", START, END)
            )

        self.assertEqual((result.trades, counts(result)), ((), {}))

    def test_a_missing_stretch_is_no_signal_and_no_levels(self):
        with mock.patch.object(live_sim, "live_exit_levels", side_effect=AssertionError("priced")):
            result = run_live({SIGNAL: None})

        self.assertEqual((result.trades, counts(result)), ((), {}))

    def test_a_signal_without_an_atr_percent_stops_the_run_and_names_the_time(self):
        stretch_bars, column = stretch(START, END - M3, {SIGNAL: 0.4})
        frames = live_frames(minutes(START - M3, END), StubHourly(fixed(context(NAIK))), stretch_bars, column)
        atr_column = tuple(None if close == SIGNAL else ATR_PCT for close in range(START, END - M3 + 1, M3))
        frames = dataclasses.replace(frames, atr_pct_3m=atr_column)

        with self.assertRaisesRegex(RuntimeError, "ATR percent"):
            simulate_live_run(frames, CFG, LIVE, LiveRunSpec(PAIR, LONG, NAIK, "i", START, END))


class LiveRefusalTests(unittest.TestCase):
    def frames(self, cfg=CFG):
        stretch_bars, column = stretch(START, END - M3, {})
        return live_frames(
            minutes(START - M3, END), StubHourly(fixed(context(NAIK))), stretch_bars, column, cfg
        )

    def go(self, frames, cfg=CFG, live=LIVE):
        return simulate_live_run(frames, cfg, live, LiveRunSpec(PAIR, LONG, NAIK, "i", START, END))

    def test_live_run_refuses_frames_built_with_other_periods(self):
        other = variant(mode_d={"candle_jendela": 40})
        self.assertNotEqual(FrameParams.of(other), FrameParams.of(CFG))

        with self.assertRaisesRegex(ValueError, "built under another config"):
            self.go(self.frames(other))

    def test_live_run_refuses_a_trigger_that_differs_from_mode_d(self):
        live = dataclasses.replace(LIVE, mode_l=dataclasses.replace(LIVE.mode_l, ema_periode=20))

        with self.assertRaises(ModesConfigError):
            self.go(self.frames(), live=live)

    def test_live_run_refuses_frames_without_an_atr_percent_column(self):
        frames = self.frames()
        for name, column in (("empty", ()), ("short", frames.atr_pct_3m[:-1]), ("long", frames.atr_pct_3m + (0.2,))):
            with self.subTest(column=name), self.assertRaisesRegex(ValueError, "ATR percent"):
                self.go(dataclasses.replace(frames, atr_pct_3m=column))

    def test_live_run_refuses_frames_of_another_pair_or_interval(self):
        frames = self.frames()
        with self.assertRaises(ValueError):
            self.go(dataclasses.replace(frames, symbol="ETH-USDT"))
        with self.assertRaises(ValueError):
            self.go(dataclasses.replace(frames, stretch_bars=frames.bars_1m))

    def test_live_run_holds_one_position_per_pair(self):
        cfg = variant(umum={"maks_posisi_per_pair": 2})

        with self.assertRaisesRegex(ValueError, "one position per pair"):
            self.go(self.frames(cfg), cfg=cfg)


class LiveVersionTests(unittest.TestCase):
    DIP = {SIGNAL + 10 * MIN: (100.0, 100.0, 99.85, 100.0)}  # through the cut, not the stop

    def test_version_ii_trades_carry_the_cut_and_version_i_none(self):
        one = only(run_live({SIGNAL: 0.4}, versi="i"))
        two = only(run_live({SIGNAL: 0.4}, versi="ii"))
        short = only(run_live({SIGNAL: 0.4}, versi="ii", side=SHORT))

        self.assertEqual((one.versi, two.versi), ("i", "ii"))
        self.assertIsNone(one.potong)
        self.assertAlmostEqual(two.potong, LONG_CUT, places=5)
        self.assertAlmostEqual(short.potong, SHORT_CUT, places=5)
        for trade in (one, two):
            self.assertAlmostEqual(trade.sl_pct, 0.30, places=9)
            self.assertAlmostEqual(trade.sl, LONG_SL, places=5)

    def test_version_ii_cuts_a_loser_from_minute_ten_and_version_i_holds_it(self):
        one = only(run_live({SIGNAL: 0.4}, versi="i", edits=self.DIP))
        two = only(run_live({SIGNAL: 0.4}, versi="ii", edits=self.DIP))

        self.assertEqual((one.exit_reason, one.exit_ms), ("WAKTU", SIGNAL + 60 * MIN))
        self.assertEqual((two.exit_reason, two.exit_ms), ("POTONG", SIGNAL + 11 * MIN))
        self.assertAlmostEqual(two.exit_fill, LONG_CUT * (1 - 0.5 / 10_000), places=5)
        self.assertLess(two.pnl_usdt, 0)

    def test_the_cut_waits_for_the_configured_minute(self):
        late = dataclasses.replace(LIVE.mode_l, potong_rugi_setelah_menit=20)
        live = dataclasses.replace(LIVE, mode_l=late)

        trade = only(run_live({SIGNAL: 0.4}, versi="ii", edits=self.DIP, live=live))

        self.assertEqual(trade.exit_reason, "WAKTU")

    def test_a_short_is_cut_when_the_price_rises_through_the_mirror_cut(self):
        edits = {SIGNAL + 10 * MIN: (100.0, 100.15, 100.0, 100.0)}

        trade = only(run_live({SIGNAL: 0.4}, side=SHORT, versi="ii", edits=edits))

        self.assertEqual((trade.exit_reason, trade.exit_ms), ("POTONG", SIGNAL + 11 * MIN))
        self.assertAlmostEqual(trade.exit_fill, SHORT_CUT * (1 + 0.5 / 10_000), places=5)

    def test_the_comparator_and_the_long_use_the_same_trigger_numbers(self):
        # The same candles signal for either side, so the entries are the same minutes.
        values = {SIGNAL: 0.4, SIGNAL + 90 * MIN: -0.6}

        long = run_live(values)
        short = run_live(values, side=SHORT)

        self.assertEqual([t.entry_ms for t in long.trades], [t.entry_ms for t in short.trades])
        self.assertEqual(len(long.trades), 2)


if __name__ == "__main__":
    unittest.main()
