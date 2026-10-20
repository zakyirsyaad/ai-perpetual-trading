from __future__ import annotations

import math
import unittest

from bot.core.setup_ledger import (
    Z_ONE_SIDED_90,
    BucketStats,
    bucket_key,
    bucket_stats,
    gross_bps,
    ledger_status,
    ledger_table,
)

MIN_SAMPLES = 60
HURDLE_BPS = 4.0


def _trade(bucket: str | None, direction: str, entry: float, exit_: float) -> dict:
    setup = {} if bucket is None else {"bucket_key": bucket}
    return {
        "direction": direction,
        "entry_price": entry,
        "exit_price": exit_,
        "factor_metadata": {"setup": setup},
    }


def _shadow(bucket: str, gross: float | None, source: str = "ledger_block", side: str = "LONG") -> dict:
    return {"bucket_key": bucket, "source": source, "gross_bps": gross, "side": side}


class BucketKeyTests(unittest.TestCase):
    def test_bucket_key_format(self) -> None:
        self.assertEqual(bucket_key("short", "TURUN", "atr_mid"), "SHORT|TURUN|atr_mid")
        self.assertEqual(bucket_key("LONG", "NAIK", "atr_high"), "LONG|NAIK|atr_high")


class GrossBpsTests(unittest.TestCase):
    def test_gross_bps_signs_by_direction(self) -> None:
        self.assertAlmostEqual(gross_bps("LONG", 100.0, 100.5), 50.0)
        self.assertAlmostEqual(gross_bps("LONG", 100.0, 99.5), -50.0)
        self.assertAlmostEqual(gross_bps("SHORT", 100.0, 99.5), 50.0)
        self.assertAlmostEqual(gross_bps("SHORT", 100.0, 100.5), -50.0)

    def test_gross_bps_accepts_lower_case_direction(self) -> None:
        self.assertAlmostEqual(gross_bps("short", 100.0, 99.0), 100.0)

    def test_gross_bps_rejects_an_unknown_direction(self) -> None:
        with self.assertRaises(ValueError):
            gross_bps("FLAT", 100.0, 101.0)


class BucketStatsTests(unittest.TestCase):
    def test_z_constant_is_the_spec_value(self) -> None:
        self.assertEqual(Z_ONE_SIDED_90, 1.28)

    def test_stats_below_two_have_no_se(self) -> None:
        self.assertEqual(bucket_stats([]), BucketStats(0, None, None, None))
        one = bucket_stats([3.5])
        self.assertEqual(one, BucketStats(1, 3.5, None, None))

    def test_stats_use_sample_stdev_and_the_one_sided_bound(self) -> None:
        stats = bucket_stats([0.0, 2.0, 4.0, 6.0])
        sample_stdev = math.sqrt(20.0 / 3.0)
        expected_se = sample_stdev / math.sqrt(4)
        self.assertEqual(stats.n, 4)
        self.assertAlmostEqual(stats.mean_bps, 3.0)
        self.assertAlmostEqual(stats.se_bps, expected_se)
        self.assertAlmostEqual(stats.upper_bps, 3.0 + 1.28 * expected_se)

    def test_stats_accept_any_sequence_without_mutating_it(self) -> None:
        values = (1.0, 2.0, 3.0)
        self.assertEqual(bucket_stats(values).n, 3)
        self.assertEqual(values, (1.0, 2.0, 3.0))


class LedgerStatusTests(unittest.TestCase):
    def test_identical_values_have_zero_se_and_still_decide(self) -> None:
        low = bucket_stats([1.0] * 60)
        self.assertEqual(low.se_bps, 0.0)
        self.assertEqual(ledger_status(low, BucketStats(0, None, None, None), MIN_SAMPLES, HURDLE_BPS), "blocked")
        high = bucket_stats([4.0] * 60)
        self.assertEqual(ledger_status(high, BucketStats(0, None, None, None), MIN_SAMPLES, HURDLE_BPS), "open")

    def test_learning_below_min_samples(self) -> None:
        empty = BucketStats(0, None, None, None)
        self.assertEqual(ledger_status(empty, empty, MIN_SAMPLES, HURDLE_BPS), "learning")
        one = bucket_stats([-50.0])
        self.assertEqual(ledger_status(one, empty, MIN_SAMPLES, HURDLE_BPS), "learning")
        short = bucket_stats([-50.0] * 59)
        self.assertEqual(ledger_status(short, empty, MIN_SAMPLES, HURDLE_BPS), "learning")
        full = bucket_stats([-50.0] * 60)
        self.assertEqual(ledger_status(full, empty, MIN_SAMPLES, HURDLE_BPS), "blocked")

    def test_blocked_when_the_upper_bound_is_below_the_hurdle(self) -> None:
        real = BucketStats(60, 1.0, 1.5, 1.0 + Z_ONE_SIDED_90 * 1.5)
        self.assertAlmostEqual(real.upper_bps, 2.92)
        empty = BucketStats(0, None, None, None)
        self.assertEqual(ledger_status(real, empty, MIN_SAMPLES, HURDLE_BPS), "blocked")

    def test_open_when_the_upper_bound_reaches_the_hurdle(self) -> None:
        empty = BucketStats(0, None, None, None)
        at_hurdle = BucketStats(60, 2.0, 1.5625, 4.0)
        self.assertEqual(ledger_status(at_hurdle, empty, MIN_SAMPLES, HURDLE_BPS), "open")
        above = BucketStats(60, 3.0, 1.5, 3.0 + Z_ONE_SIDED_90 * 1.5)
        self.assertEqual(ledger_status(above, empty, MIN_SAMPLES, HURDLE_BPS), "open")

    def test_reopened_needs_min_samples_of_shadow_at_or_above_the_hurdle(self) -> None:
        real = bucket_stats([1.0] * 60)
        good_shadow = bucket_stats([5.0] * 60)
        self.assertEqual(ledger_status(real, good_shadow, MIN_SAMPLES, HURDLE_BPS), "reopened")
        at_hurdle = bucket_stats([4.0] * 60)
        self.assertEqual(ledger_status(real, at_hurdle, MIN_SAMPLES, HURDLE_BPS), "reopened")
        too_few = bucket_stats([5.0] * 59)
        self.assertEqual(ledger_status(real, too_few, MIN_SAMPLES, HURDLE_BPS), "blocked")
        below = bucket_stats([3.0] * 60)
        self.assertEqual(ledger_status(real, below, MIN_SAMPLES, HURDLE_BPS), "blocked")

    def test_shadow_cannot_reopen_a_bucket_that_is_still_learning_or_open(self) -> None:
        good_shadow = bucket_stats([5.0] * 60)
        learning = bucket_stats([1.0] * 10)
        self.assertEqual(ledger_status(learning, good_shadow, MIN_SAMPLES, HURDLE_BPS), "learning")
        open_ = bucket_stats([6.0] * 60)
        self.assertEqual(ledger_status(open_, good_shadow, MIN_SAMPLES, HURDLE_BPS), "open")


class LedgerTableTests(unittest.TestCase):
    def test_table_skips_rows_without_a_bucket_and_shadow_side_rows(self) -> None:
        bucket = "SHORT|TURUN|atr_mid"
        trades = [
            _trade(bucket, "SHORT", 100.0, 99.0),
            _trade(bucket, "SHORT", 100.0, 100.5),
            _trade("", "SHORT", 100.0, 99.0),
            _trade(None, "SHORT", 100.0, 99.0),
            {"direction": "SHORT", "entry_price": 100.0, "exit_price": 99.0, "factor_metadata": {}},
            {"direction": "SHORT", "entry_price": 100.0, "exit_price": 99.0},
            {"direction": "SHORT", "entry_price": 100.0, "exit_price": 99.0, "factor_metadata": None},
        ]
        shadows = [
            _shadow(bucket, 7.0),
            _shadow(bucket, None),
            _shadow(bucket, 9.0, source="shadow_side"),
            _shadow("", 9.0),
            {"source": "ledger_block", "gross_bps": 9.0},
        ]
        table = ledger_table(trades, shadows)
        self.assertEqual(set(table), {bucket})
        real, shadow = table[bucket]
        self.assertEqual(real.n, 2)
        self.assertAlmostEqual(real.mean_bps, (100.0 - 50.0) / 2)
        self.assertEqual(shadow, BucketStats(1, 7.0, None, None))

    def test_a_shadow_only_bucket_has_empty_real_stats(self) -> None:
        bucket = "LONG|NAIK|atr_high"
        table = ledger_table([], [_shadow(bucket, 2.0), _shadow(bucket, 4.0)])
        real, shadow = table[bucket]
        self.assertEqual(real, BucketStats(0, None, None, None))
        self.assertEqual(shadow.n, 2)
        self.assertAlmostEqual(shadow.mean_bps, 3.0)
        self.assertIsNotNone(shadow.upper_bps)

    def test_a_trade_only_bucket_has_empty_shadow_stats(self) -> None:
        bucket = "LONG|NAIK|atr_high"
        table = ledger_table([_trade(bucket, "LONG", 100.0, 100.1)], [])
        real, shadow = table[bucket]
        self.assertEqual(real.n, 1)
        self.assertAlmostEqual(real.mean_bps, 10.0)
        self.assertIsNone(real.upper_bps)
        self.assertEqual(shadow, BucketStats(0, None, None, None))

    def test_a_shadow_side_only_bucket_is_not_a_table_key(self) -> None:
        table = ledger_table([], [_shadow("LONG|NAIK|atr_high", 5.0, source="shadow_side")])
        self.assertEqual(table, {})

    def test_trade_rows_that_cannot_be_priced_are_skipped(self) -> None:
        bucket = "SHORT|TURUN|atr_mid"
        trades = [
            _trade(bucket, "SHORT", 0.0, 99.0),
            _trade(bucket, "SHORT", -5.0, 99.0),
            _trade(bucket, "SHORT", 100.0, 0.0),
            _trade(bucket, "FLAT", 100.0, 99.0),
            {"direction": "SHORT", "entry_price": None, "exit_price": 99.0, "factor_metadata": {"setup": {"bucket_key": bucket}}},
            {"direction": "SHORT", "entry_price": 100.0, "exit_price": None, "factor_metadata": {"setup": {"bucket_key": bucket}}},
            _trade(bucket, "SHORT", 100.0, 99.0),
        ]
        table = ledger_table(trades, [])
        real, _ = table[bucket]
        self.assertEqual(real.n, 1)
        self.assertAlmostEqual(real.mean_bps, 100.0)

    def test_non_finite_shadow_values_are_skipped(self) -> None:
        bucket = "SHORT|TURUN|atr_mid"
        table = ledger_table([], [_shadow(bucket, float("nan")), _shadow(bucket, float("inf")), _shadow(bucket, 1.0)])
        self.assertEqual(table[bucket][1], BucketStats(1, 1.0, None, None))

    def test_table_feeds_the_status_end_to_end(self) -> None:
        bucket = "SHORT|TURUN|atr_mid"
        trades = [_trade(bucket, "SHORT", 100.0, 100.01) for _ in range(60)]
        shadows = [_shadow(bucket, 6.0) for _ in range(60)]
        real, shadow = ledger_table(trades, shadows)[bucket]
        self.assertAlmostEqual(real.mean_bps, -1.0)
        self.assertEqual(ledger_status(real, shadow, MIN_SAMPLES, HURDLE_BPS), "reopened")


if __name__ == "__main__":
    unittest.main()
