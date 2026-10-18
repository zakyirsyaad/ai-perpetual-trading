"""Tests for bot.strategy.modes.near_mean: the stretch of the live gate and Mode D's trigger.

``stretch_from_window`` is the live bot's own measurement, so it is checked three ways. It must be
the same call sequence as the live gate (``bot.strategy.indicators`` into ``build_setup_profile``).
It must agree with numbers worked out below from the definitions of an EMA and an ATR, so that the
two sides do not share one mistake. And it must behave at the edges of what the live maths can
measure: a window that is too short, a flat market, bars that do not match.
"""

import math
import unittest

from bot.arena.setup_profile import build_setup_profile
from bot.strategy import indicators as live
from bot.strategy.modes.near_mean import near_mean, near_mean_short, stretch_from_window

LIVE_WINDOW = 39  # closed 3m bars the live gate reads
EMA_PERIOD = 21
ATR_PERIOD = 14
MAX_ABS = 0.75
ROUNDING = 5e-5  # build_setup_profile rounds the stretch to 4 decimals


def wavy_window(count=LIVE_WINDOW, drift=0.12):
    """Highs, lows and closes that move inside and between bars and end above their mean."""
    closes = [
        100.0 + 3.0 * math.sin(i / 4.0) + drift * i + 0.5 * math.sin(i * 1.7) for i in range(count)
    ]
    highs = [c + 0.45 + 0.25 * abs(math.sin(i * 2.3)) for i, c in enumerate(closes)]
    lows = [c - 0.40 - 0.30 * abs(math.sin(i * 0.9)) for i, c in enumerate(closes)]
    return highs, lows, closes


def reference_stretch(highs, lows, closes, ema_period, atr_period):
    """(last close - EMA) / ATR, written from the definitions and not from the live code.

    The EMA is the weighted sum of the closes (the first close seeds it, as in the live module),
    the ATR is the mean of the last ``atr_period`` true ranges. The result is not rounded."""
    k = 2.0 / (ema_period + 1)
    n = len(closes)
    mean = (1 - k) ** (n - 1) * closes[0]
    mean += sum(k * (1 - k) ** (n - 1 - i) * closes[i] for i in range(1, n))
    true_ranges = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, n)
    ]
    average_range = sum(true_ranges[-atr_period:]) / atr_period
    return (closes[-1] - mean) / average_range


class NearMeanShortTests(unittest.TestCase):
    def test_near_mean_bounds_are_inclusive(self):
        for stretch in (0.75, -0.75):
            with self.subTest(stretch=stretch):
                self.assertIs(near_mean_short(stretch, MAX_ABS), True)
        for stretch in (0.7500001, -0.7500001, None):
            with self.subTest(stretch=stretch):
                self.assertIs(near_mean_short(stretch, MAX_ABS), False)

    def test_the_bound_is_exact_to_the_last_float(self):
        self.assertIs(near_mean_short(MAX_ABS, MAX_ABS), True)
        self.assertIs(near_mean_short(math.nextafter(MAX_ABS, 1.0), MAX_ABS), False)
        self.assertIs(near_mean_short(-math.nextafter(MAX_ABS, 1.0), MAX_ABS), False)

    def test_a_stretch_inside_the_band_is_near_the_mean_on_either_side(self):
        for stretch in (0.0, 0.3, -0.3, 0.7499999, -0.7499999):
            with self.subTest(stretch=stretch):
                self.assertIs(near_mean_short(stretch, MAX_ABS), True)

    def test_the_band_is_the_argument(self):
        self.assertIs(near_mean_short(1.0, 1.5), True)
        self.assertIs(near_mean_short(-1.0, 1.5), True)
        self.assertIs(near_mean_short(1.0, 0.5), False)
        self.assertIs(near_mean_short(-1.0, 0.5), False)

    def test_a_stretch_that_could_not_be_measured_is_not_near_the_mean(self):
        for stretch in (None, math.nan, math.inf, -math.inf):
            with self.subTest(stretch=stretch):
                self.assertIs(near_mean_short(stretch, MAX_ABS), False)


class NearMeanSideNeutralTests(unittest.TestCase):
    def test_near_mean_is_side_neutral_and_mode_d_name_still_works(self):
        # Mode L (LONG) and Mode D / comparator S (SHORT) read the same trigger.
        self.assertIs(near_mean_short, near_mean)
        for stretch, expected in ((0.0, True), (0.75, True), (-0.75, True), (0.7501, False),
                                  (-0.7501, False), (None, False), (math.nan, False)):
            with self.subTest(stretch=stretch):
                self.assertIs(near_mean(stretch, MAX_ABS), expected)
                self.assertIs(near_mean_short(stretch, MAX_ABS), expected)


class StretchFromWindowTests(unittest.TestCase):
    def test_stretch_matches_the_live_maths_on_39_bars(self):
        highs, lows, closes = wavy_window()
        self.assertEqual(len(closes), LIVE_WINDOW)

        stretch = stretch_from_window(highs, lows, closes, EMA_PERIOD, ATR_PERIOD)

        # The live call sequence: the live indicators, then the setup profile.
        features = {
            "last_price": closes[-1],
            "ema_21": live.ema(closes, 21),
            "atr_14": live.atr(highs, lows, closes, 14),
        }
        self.assertEqual(stretch, build_setup_profile(features, "LONG")["stretch_atr"])
        # The same number from the definitions, within the 4 decimals the profile keeps.
        self.assertIsNotNone(stretch)
        exact = reference_stretch(highs, lows, closes, EMA_PERIOD, ATR_PERIOD)
        self.assertAlmostEqual(stretch, exact, delta=ROUNDING + 1e-9)
        self.assertGreater(abs(exact), 0.1)  # the window gives a real number, not a near-zero

    def test_the_stretch_has_the_sign_of_price_against_the_mean_whatever_the_side(self):
        up_highs, up_lows, up_closes = [], [], []
        for i in range(LIVE_WINDOW):
            up_closes.append(100.0 + i)
            up_highs.append(100.0 + i + 0.5)
            up_lows.append(100.0 + i - 0.5)
        # The same market mirrored around 200: price falls and sits below its mean.
        down_closes = [200.0 - c for c in up_closes]
        down_highs = [200.0 - low for low in up_lows]
        down_lows = [200.0 - high for high in up_highs]

        up = stretch_from_window(up_highs, up_lows, up_closes, EMA_PERIOD, ATR_PERIOD)
        down = stretch_from_window(down_highs, down_lows, down_closes, EMA_PERIOD, ATR_PERIOD)

        self.assertGreater(up, 0)
        self.assertLess(down, 0)
        self.assertAlmostEqual(up, -down, places=4)

    def test_the_periods_are_the_arguments_and_not_the_names_of_the_profile_keys(self):
        highs, lows, closes = wavy_window()

        short = stretch_from_window(highs, lows, closes, 5, 3)

        self.assertAlmostEqual(short, reference_stretch(highs, lows, closes, 5, 3), delta=1e-4)
        self.assertNotEqual(short, stretch_from_window(highs, lows, closes, EMA_PERIOD, ATR_PERIOD))

    def test_a_window_too_short_for_the_atr_has_no_stretch(self):
        highs, lows, closes = wavy_window()
        needed = ATR_PERIOD + 1  # the live ATR needs a previous close for every true range

        for count in range(1, needed):
            with self.subTest(closes=count):
                got = stretch_from_window(
                    highs[:count], lows[:count], closes[:count], EMA_PERIOD, ATR_PERIOD
                )
                self.assertIsNone(got)
        self.assertIsNotNone(
            stretch_from_window(highs[:needed], lows[:needed], closes[:needed], 5, ATR_PERIOD)
        )

    def test_a_window_shorter_than_the_ema_period_reads_the_last_close_as_the_mean(self):
        # The live EMA returns the last close when it has fewer closes than its period, so the
        # stretch is 0.0 there. The function keeps the live behaviour; stretch_series refuses
        # such a window up front.
        highs, lows, closes = wavy_window(count=EMA_PERIOD - 3)

        got = stretch_from_window(highs, lows, closes, EMA_PERIOD, ATR_PERIOD)

        self.assertEqual(got, 0.0)

    def test_a_flat_market_has_no_stretch(self):
        flat = [100.0] * LIVE_WINDOW

        self.assertIsNone(stretch_from_window(flat, flat, flat, EMA_PERIOD, ATR_PERIOD))

    def test_bars_that_do_not_match_are_refused(self):
        highs, lows, closes = wavy_window()
        cases = (
            ("nothing", [], [], []),
            ("one high too many", highs + [highs[-1]], lows, closes),
            ("one low missing", highs, lows[:-1], closes),
            ("one close missing", highs, lows, closes[:-1]),
        )
        for label, h, low, c in cases:
            with self.subTest(label), self.assertRaises(ValueError):
                stretch_from_window(h, low, c, EMA_PERIOD, ATR_PERIOD)


if __name__ == "__main__":
    unittest.main()
