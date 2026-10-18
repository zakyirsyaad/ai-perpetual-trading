from __future__ import annotations

import random
import unittest

from bot.strategy.pine_indicators import (
    atr,
    dmi,
    ema,
    percent_rank,
    rma,
    rolling_max,
    rolling_median,
    rolling_min,
    rsi,
    sma,
    stdev,
    true_range,
)


def _random_bars(count: int, seed: int) -> tuple[list[float], list[float], list[float]]:
    rng = random.Random(seed)
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    price = 100.0
    for _ in range(count):
        price = max(1.0, price + rng.uniform(-2.0, 2.0))
        closes.append(price)
        highs.append(price + rng.uniform(0.0, 1.5))
        lows.append(price - rng.uniform(0.0, 1.5))
    return highs, lows, closes


class PineIndicatorTests(unittest.TestCase):
    def test_rma_seeds_with_mean_then_wilder(self) -> None:
        self.assertEqual(rma([1, 2, 3, 4], 2, min_bars=2), [None, 1.5, 2.25, 3.125])

    def test_rma_reports_none_until_min_bars_even_after_seed(self) -> None:
        out = rma([1, 2, 3, 4], 2, min_bars=4)
        self.assertEqual(out[:3], [None, None, None])
        self.assertEqual(out[3], 3.125)

    def test_ema_seeds_with_mean_then_alpha(self) -> None:
        self.assertEqual(
            ema([1, 2, 3], 2, min_bars=2), [None, 1.5, 1.5 + (2 / 3) * (3 - 1.5)]
        )

    def test_default_warmup_is_eight_periods(self) -> None:
        out = ema(list(range(100)), 10)
        self.assertIsNone(out[78])
        self.assertIsNotNone(out[79])
        out = rma(list(range(100)), 10)
        self.assertIsNone(out[78])
        self.assertIsNotNone(out[79])

    def test_short_input_returns_all_none(self) -> None:
        self.assertEqual(rma([1, 2], 5, min_bars=1), [None, None])
        self.assertEqual(rma([], 5), [])

    def test_rsi_edges(self) -> None:
        self.assertEqual(rsi([1, 2, 3, 4, 5], 2, min_bars=2)[-1], 100.0)
        self.assertEqual(rsi([5, 4, 3, 2, 1], 2, min_bars=2)[-1], 0.0)
        self.assertEqual(rsi([3, 3, 3, 3, 3], 2, min_bars=2)[-1], 50.0)

    def test_rsi_aligns_to_closes_and_matches_wilder_value(self) -> None:
        closes = [10.0, 11.0, 10.0, 12.0]
        out = rsi(closes, 2, min_bars=3)
        self.assertEqual(len(out), 4)
        self.assertEqual(out[:2], [None, None])
        # seed over changes +1,-1 -> avg gain 0.5, avg loss 0.5 -> RSI 50
        self.assertAlmostEqual(out[2], 50.0)
        # next change +2: gain (0.5+2)/2=1.25 ... wilder: 0.5+(2-0.5)/2=1.25, loss 0.25
        self.assertAlmostEqual(out[3], 100 - 100 / (1 + 1.25 / 0.25))

    def test_true_range_uses_previous_close(self) -> None:
        self.assertEqual(true_range([10, 12], [9, 11], [9.5, 11.5]), [1, 2.5])

    def test_atr_is_rma_of_true_range(self) -> None:
        highs, lows, closes = _random_bars(60, 1)
        self.assertEqual(
            atr(highs, lows, closes, 5, min_bars=10),
            rma(true_range(highs, lows, closes), 5, min_bars=10),
        )

    def test_dmi_zero_sum_gives_zero_dx_not_error(self) -> None:
        flat = [10.0] * 40
        plus_di, minus_di, adx = dmi(flat, flat, flat, 3, 3, min_bars=8)
        self.assertEqual(plus_di[-1], 0.0)
        self.assertEqual(minus_di[-1], 0.0)
        self.assertEqual(adx[-1], 0.0)

    def test_dmi_uptrend_has_plus_above_minus(self) -> None:
        highs = [10.0 + i for i in range(60)]
        lows = [9.0 + i for i in range(60)]
        closes = [9.5 + i for i in range(60)]
        plus_di, minus_di, adx = dmi(highs, lows, closes, 5, 5, min_bars=20)
        self.assertGreater(plus_di[-1], minus_di[-1])
        self.assertEqual(minus_di[-1], 0.0)
        self.assertGreater(adx[-1], 50.0)

    def test_dmi_default_warmup_uses_larger_length(self) -> None:
        highs, lows, closes = _random_bars(100, 2)
        plus_di, minus_di, adx = dmi(highs, lows, closes, 3, 5)
        for series in (plus_di, minus_di, adx):
            self.assertIsNone(series[38])
            self.assertIsNotNone(series[39])

    def test_dmi_pine_formula_on_small_example(self) -> None:
        highs = [10.0, 12.0, 11.0]
        lows = [8.0, 9.0, 8.0]
        closes = [9.0, 11.0, 9.0]
        plus_di, minus_di, adx = dmi(highs, lows, closes, 2, 2, min_bars=1)
        # TR = [2, 3, 3] but Pine's ta.dmi smooths ta.tr, na on bar 0 -> TR[1..2] = [3, 3];
        # +DM = [-, 2, 0]; -DM = [-, 0, 1]. All three RMAs seed at idx2:
        # rma(TR) = 3.0, rma(+DM) = (2+0)/2 = 1.0, rma(-DM) = (0+1)/2 = 0.5
        self.assertIsNone(plus_di[1])
        self.assertIsNone(minus_di[1])
        self.assertAlmostEqual(plus_di[2], 100 * 1.0 / 3.0)
        self.assertAlmostEqual(minus_di[2], 100 * 0.5 / 3.0)
        # DX = 100*|33.33-16.67|/50 = 33.33; ADX (adx_len=2) not seeded until 2 DX values
        self.assertIsNone(adx[2])

    def test_percent_rank(self) -> None:
        self.assertEqual(percent_rank([1, 2, 3, 4, 5], 4)[4], 100.0)
        self.assertEqual(percent_rank([5, 4, 3, 2, 1], 4)[4], 0.0)
        self.assertIsNone(percent_rank([1, 2, 3], 4)[2])
        self.assertIsNone(percent_rank([1, 2, 3, 4], 4)[3])
        self.assertEqual(percent_rank([1, 3, 2, 3, 2], 4)[4], 50.0)
        self.assertEqual(percent_rank([2, 2, 2, 2, 2], 4)[4], 100.0)

    def test_rolling_max_min_none_until_full(self) -> None:
        values = [3, 1, 4, 1, 5]
        self.assertEqual(rolling_max(values, 3), [None, None, 4, 4, 5])
        self.assertEqual(rolling_min(values, 3), [None, None, 1, 1, 1])

    def test_prefix_stability(self) -> None:
        highs, lows, closes = _random_bars(300, 3)
        rng = random.Random(4)
        full = {
            "rma": rma(closes, 7, min_bars=10),
            "ema": ema(closes, 7, min_bars=10),
            "rsi": rsi(closes, 7, min_bars=10),
            "atr": atr(highs, lows, closes, 7, min_bars=10),
            "dmi": dmi(highs, lows, closes, 7, 5, min_bars=20),
            "max": rolling_max(closes, 9),
            "min": rolling_min(closes, 9),
            "rank": percent_rank(closes, 9),
        }
        for _ in range(20):
            k = rng.randint(30, 300)
            h, l, c = highs[:k], lows[:k], closes[:k]
            part = {
                "rma": rma(c, 7, min_bars=10),
                "ema": ema(c, 7, min_bars=10),
                "rsi": rsi(c, 7, min_bars=10),
                "atr": atr(h, l, c, 7, min_bars=10),
                "dmi": dmi(h, l, c, 7, 5, min_bars=20),
                "max": rolling_max(c, 9),
                "min": rolling_min(c, 9),
                "rank": percent_rank(c, 9),
            }
            for name, value in part.items():
                if name == "dmi":
                    for got, want in zip(value, full[name]):
                        self.assertEqual(got[k - 1], want[k - 1], name)
                else:
                    self.assertEqual(value[k - 1], full[name][k - 1], name)


class WindowStatisticTests(unittest.TestCase):
    def test_sma_none_until_full_then_mean(self) -> None:
        self.assertEqual(sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])

    def test_window_holding_none_is_none(self) -> None:
        self.assertEqual(sma([None, 2, 3], 2), [None, None, 2.5])

    def test_stdev_is_population(self) -> None:
        self.assertEqual(stdev([2, 4, 4, 4, 5, 5, 7, 9], 8)[-1], 2.0)

    def test_stdev_of_a_flat_window_is_exactly_zero(self) -> None:
        self.assertEqual(stdev([0.1, 0.1, 0.1, 0.1], 3), [None, None, 0.0, 0.0])

    def test_rolling_median_odd_and_even(self) -> None:
        self.assertEqual(rolling_median([5, 1, 3, 2], 3), [None, None, 3, 2])
        self.assertEqual(rolling_median([1, 2, 3, 4], 4)[-1], 2.5)

    def test_every_statistic_is_none_while_its_window_holds_a_none(self) -> None:
        # The leading Nones stand for an indicator still warming up (ATR, say); the
        # later gap blanks exactly the windows that contain it and no others.
        values = [None, None, 4.0, 1.0, 3.0, None, 2.0, 5.0, 6.0]
        complete = [False, False, False, False, True, False, False, False, True]
        for fn in (sma, stdev, rolling_median):
            with self.subTest(fn.__name__):
                out = fn(values, 3)
                self.assertEqual([v is not None for v in out], complete)
                self.assertEqual(out[4], fn([4.0, 1.0, 3.0], 3)[-1])
                self.assertEqual(out[8], fn([2.0, 5.0, 6.0], 3)[-1])

    def test_short_and_empty_inputs_are_all_none(self) -> None:
        for fn in (sma, stdev, rolling_median):
            with self.subTest(fn.__name__):
                self.assertEqual(fn([], 3), [])
                self.assertEqual(fn([1.0, 2.0], 3), [None, None])

    def test_period_below_one_is_rejected(self) -> None:
        for fn in (sma, stdev, rolling_median):
            with self.subTest(fn.__name__):
                with self.assertRaises(ValueError):
                    fn([1.0, 2.0, 3.0], 0)

    def test_prefix_stability(self) -> None:
        _, _, closes = _random_bars(300, 5)
        # A gap every 25th value, so roughly a third of the windows hold a None.
        values = [None if i % 25 == 10 else c for i, c in enumerate(closes)]
        windowed = {
            "sma": lambda v: sma(v, 7),
            "stdev": lambda v: stdev(v, 7),
            "median": lambda v: rolling_median(v, 9),
        }
        full = {name: fn(values) for name, fn in windowed.items()}
        rng = random.Random(6)
        for _ in range(20):
            k = rng.randint(30, 300)
            for name, fn in windowed.items():
                self.assertEqual(fn(values[:k])[k - 1], full[name][k - 1], name)


if __name__ == "__main__":
    unittest.main()
