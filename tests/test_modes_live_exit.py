"""Tests for bot.strategy.modes.live_exit: the stop, take-profit and cut levels of mode L.

The expected prices are worked out by hand from the spec's formulas (section 4), not by calling
the code under test: at 25,000 USDT the take-profit sits at ``entry x 1.0032416208`` for LONG and
``entry x 0.9967616192`` for SHORT.
"""

import math
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from bot.strategy.modes.checks import LONG, SHORT
from bot.strategy.modes.config import LiveExitModeConfig, load_live_exit_config_with_sha256
from bot.strategy.modes.live_exit import (
    LiveExitLevels,
    live_exit_levels,
    net_target_price,
    stop_pct,
)

LIVE_PATH = Path(__file__).resolve().parents[1] / "config" / "modes_live_exit.yaml"
NOTIONAL = 25_000.0


def rules(**changes) -> LiveExitModeConfig:
    base = dict(
        aktif=True,
        tf_pemicu="3m",
        candle_jendela=39,
        ema_periode=21,
        atr_periode=14,
        maks_jarak_ke_ema_kali_atr=0.75,
        sl_minimal_persen=0.28,
        sl_kali_atr=1.5,
        sl_maksimal_persen=0.40,
        tp_net_usdt=56.0,
        fee_perkiraan_persen=0.05,
        batas_waktu_menit=60,
        potong_rugi_persen=0.10,
        potong_rugi_setelah_menit=10,
    )
    base.update(changes)
    return LiveExitModeConfig(**base)


class StopPctTests(unittest.TestCase):
    def test_stop_pct_clamps(self):
        self.assertAlmostEqual(stop_pct(0.10, rules()), 0.28)  # 1.5 x 0.10 = 0.15, raised to 0.28
        self.assertAlmostEqual(stop_pct(0.20, rules()), 0.30)  # inside the band
        self.assertAlmostEqual(stop_pct(0.30, rules()), 0.40)  # 0.45, cut to 0.40

    def test_the_bounds_are_inclusive_and_exact(self):
        self.assertEqual(stop_pct(0.28 / 1.5, rules()), 0.28 / 1.5 * 1.5)
        self.assertEqual(stop_pct(1000.0, rules()), 0.40)

    def test_the_numbers_come_from_the_rules(self):
        wide = rules(sl_minimal_persen=0.5, sl_kali_atr=2.0, sl_maksimal_persen=0.9)
        self.assertEqual(stop_pct(0.1, wide), 0.5)
        self.assertAlmostEqual(stop_pct(0.35, wide), 0.7)
        self.assertEqual(stop_pct(0.6, wide), 0.9)

    def test_a_missing_or_flat_atr_is_refused(self):
        for bad in (0.0, -0.1, math.nan, math.inf, None, True, "0.2"):
            with self.subTest(atr_pct=bad), self.assertRaises(ValueError):
                stop_pct(bad, rules())


class NetTargetPriceTests(unittest.TestCase):
    def test_net_target_ratio_at_25000(self):
        for entry in (1.0, 100.003, 65_432.1):
            with self.subTest(entry=entry):
                self.assertAlmostEqual(
                    net_target_price(entry, LONG, NOTIONAL, rules()) / entry, 1.0032416208, places=9
                )
                self.assertAlmostEqual(
                    net_target_price(entry, SHORT, NOTIONAL, rules()) / entry, 0.9967616192, places=9
                )

    def test_the_target_is_where_the_estimated_net_reaches_the_goal(self):
        # Net is gross minus the estimated fee on the entry and on the exit leg.
        entry, r = 250.0, 0.0005
        for side in (LONG, SHORT):
            with self.subTest(side=side):
                exit_price = net_target_price(entry, side, NOTIONAL, rules())
                q = NOTIONAL / entry
                gross = side * (exit_price - entry) * q
                fees = r * (entry * q + exit_price * q)
                self.assertAlmostEqual(gross - fees, 56.0, places=9)

    def test_the_goal_and_fee_are_the_rules(self):
        cheap = rules(tp_net_usdt=0.0001, fee_perkiraan_persen=0.01)
        price = net_target_price(100.0, LONG, NOTIONAL, cheap)
        self.assertAlmostEqual(price, (100.0 * 1.0001 + 0.0001 / 250.0) / 0.9999, places=12)

    def test_bad_inputs_raise(self):
        for entry in (0.0, -1.0, math.nan, math.inf, None, True):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                net_target_price(entry, LONG, NOTIONAL, rules())
        for side in (0, 2, 1.0, True, None):
            with self.subTest(side=side), self.assertRaises(ValueError):
                net_target_price(100.0, side, NOTIONAL, rules())
        for notional in (0.0, -5.0, math.nan, math.inf, None):
            with self.subTest(notional=notional), self.assertRaises(ValueError):
                net_target_price(100.0, LONG, notional, rules())

    def test_a_short_target_at_or_below_zero_is_refused(self):
        # 25,000 USDT at an entry of 100 is 250 units: a 1,000,000 USDT target needs 4,000 per
        # unit, far more than the entry price, so the SHORT target would be negative.
        with self.assertRaisesRegex(ValueError, "above 0"):
            net_target_price(100.0, SHORT, NOTIONAL, rules(tp_net_usdt=1_000_000.0))


class LiveExitLevelsTests(unittest.TestCase):
    def test_levels_from_the_reference_entry(self):
        long = live_exit_levels(100.003, LONG, 0.10, NOTIONAL, rules(), with_cut=True)
        self.assertAlmostEqual(long.sl, 99.722992, places=6)
        self.assertAlmostEqual(long.tp, 100.327172, places=6)
        self.assertAlmostEqual(long.cut, 99.902997, places=6)
        self.assertAlmostEqual(long.sl_pct, 0.28)

        short = live_exit_levels(99.997, SHORT, 0.10, NOTIONAL, rules(), with_cut=True)
        self.assertAlmostEqual(short.sl, 100.276992, places=6)
        self.assertAlmostEqual(short.tp, 99.673172, places=6)
        self.assertAlmostEqual(short.cut, 100.096997, places=6)
        self.assertAlmostEqual(short.sl_pct, 0.28)

    def test_the_stop_follows_the_atr_and_sits_on_the_losing_side(self):
        wide = live_exit_levels(200.0, LONG, 0.30, NOTIONAL, rules(), with_cut=False)
        self.assertAlmostEqual(wide.sl_pct, 0.40)
        self.assertAlmostEqual(wide.sl, 200.0 * (1 - 0.004))
        up = live_exit_levels(200.0, SHORT, 0.30, NOTIONAL, rules(), with_cut=False)
        self.assertAlmostEqual(up.sl, 200.0 * (1 + 0.004))

    def test_the_cut_sits_inside_the_stop_and_the_stop_inside_the_target_side(self):
        for side in (LONG, SHORT):
            with self.subTest(side=side):
                levels = live_exit_levels(100.0, side, 0.10, NOTIONAL, rules(), with_cut=True)
                self.assertLess(side * (levels.sl - 100.0), side * (levels.cut - 100.0))
                self.assertLess(side * (levels.cut - 100.0), 0)
                self.assertGreater(side * (levels.tp - 100.0), 0)

    def test_no_cut_without_version_ii(self):
        for side in (LONG, SHORT):
            levels = live_exit_levels(100.0, side, 0.10, NOTIONAL, rules(), with_cut=False)
            self.assertIsNone(levels.cut)

    def test_levels_are_a_frozen_value(self):
        levels = live_exit_levels(100.0, LONG, 0.10, NOTIONAL, rules(), with_cut=True)
        self.assertIsInstance(levels, LiveExitLevels)
        with self.assertRaises(FrozenInstanceError):
            levels.sl = 1.0

    def test_the_repo_file_gives_the_same_levels(self):
        live, _ = load_live_exit_config_with_sha256(LIVE_PATH)
        levels = live_exit_levels(100.003, LONG, 0.10, NOTIONAL, live.mode_l, with_cut=True)
        self.assertAlmostEqual(levels.tp, 100.003 * 1.0032416208, places=6)

    def test_bad_inputs_raise(self):
        cases = {
            "side 0": (100.0, 0, 0.10, NOTIONAL),
            "side 2": (100.0, 2, 0.10, NOTIONAL),
            "atr nan": (100.0, LONG, math.nan, NOTIONAL),
            "atr inf": (100.0, LONG, math.inf, NOTIONAL),
            "atr None": (100.0, LONG, None, NOTIONAL),
            "atr 0": (100.0, LONG, 0.0, NOTIONAL),
            "notional 0": (100.0, LONG, 0.10, 0.0),
            "notional nan": (100.0, LONG, 0.10, math.nan),
            "entry 0": (0.0, LONG, 0.10, NOTIONAL),
        }
        for name, args in cases.items():
            with self.subTest(name), self.assertRaises(ValueError):
                live_exit_levels(*args, rules(), with_cut=True)


if __name__ == "__main__":
    unittest.main()
