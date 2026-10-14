import math
import random
import unittest
from datetime import date, timedelta

from research.pine_gate.simulate import Trade
from research.pine_gate.stats import (
    DayAgg,
    bootstrap_p_above,
    bootstrap_paired_p,
    day_aggregates,
    holm,
    mean_gross_bps,
    net_per_1m,
    pbo_cscv,
    permutation_p,
    spa_pvalue,
    trades_per_trading_day,
    win_rate,
    wr_lower_bound,
    wr_se_clustered,
)

MON = date(2026, 9, 14)


def trade(kind, day=MON, gross=0.0):
    return Trade(
        symbol="BTC-USDT",
        side=1,
        signal_ms=0,
        entry_ms=0,
        exit_ms=0,
        kind=kind,
        gross_bps=gross,
        net_usdt=0.0,
        wib_day=day,
    )


def agg(day_offset, tp, sl, n=None, gross_sum=0.0):
    return DayAgg(MON + timedelta(days=day_offset), tp, sl, tp + sl if n is None else n, gross_sum)


class WinRateTests(unittest.TestCase):
    def test_win_rate_ignores_time_stops(self):
        trades = [trade("TP")] * 3 + [trade("SL")] + [trade("TIME")] * 10
        self.assertAlmostEqual(win_rate(trades), 75.0)

    def test_win_rate_none_without_bracket_exits(self):
        self.assertIsNone(win_rate([]))
        self.assertIsNone(win_rate([trade("TIME")]))

    def test_mean_gross_counts_time_stops(self):
        trades = [trade("TP", gross=20.0), trade("SL", gross=-21.0), trade("TIME", gross=4.0)]
        self.assertAlmostEqual(mean_gross_bps(trades), 1.0)
        self.assertIsNone(mean_gross_bps([]))

    def test_net_per_1m_formula(self):
        self.assertAlmostEqual(net_per_1m(4.0), -150.0)
        self.assertAlmostEqual(net_per_1m(7.0), 0.0)
        self.assertAlmostEqual(net_per_1m(0.0, fee_pct=0.05), -500.0)


class DayAggregateTests(unittest.TestCase):
    def test_day_aggregates_cover_listed_days_and_stray_trade_days(self):
        days = [MON, MON + timedelta(days=1), MON + timedelta(days=2)]
        saturday = MON + timedelta(days=5)
        trades = [
            trade("TP", MON, 20.0),
            trade("SL", MON, -21.0),
            trade("TIME", MON, 3.0),
            trade("TP", MON + timedelta(days=2), 20.0),
            trade("SL", saturday, -21.0),
        ]
        aggs = day_aggregates(trades, days)
        self.assertEqual(
            aggs,
            [
                DayAgg(MON, 1, 1, 3, 2.0),
                DayAgg(MON + timedelta(days=1), 0, 0, 0, 0.0),
                DayAgg(MON + timedelta(days=2), 1, 0, 1, 20.0),
                DayAgg(saturday, 0, 1, 1, -21.0),
            ],
        )
        self.assertEqual(sum(a.n for a in aggs), len(trades))

    def test_day_aggregate_is_frozen(self):
        with self.assertRaises(Exception):
            DayAgg(MON, 1, 1, 2, 0.0).tp = 5

    def test_trades_per_trading_day_counts_every_trade(self):
        days = [MON, MON + timedelta(days=1)]
        trades = [trade("TP"), trade("TIME"), trade("SL", MON + timedelta(days=5))]
        self.assertAlmostEqual(trades_per_trading_day(trades, days), 1.5)
        with self.assertRaises(ValueError):
            trades_per_trading_day(trades, [])


class ClusteredSETests(unittest.TestCase):
    def test_clustered_se_grows_with_day_correlation(self):
        mixed = [agg(d, 5, 5) for d in range(20)]
        clustered = [agg(d, 10, 0) for d in range(10)] + [agg(d, 0, 10) for d in range(10, 20)]
        se_mixed = wr_se_clustered(mixed)
        se_clustered = wr_se_clustered(clustered)
        self.assertAlmostEqual(se_mixed, 0.0)
        # e_d = +-5, var = 20/19 * 500 / 200^2, in percentage points.
        self.assertAlmostEqual(se_clustered, 100 * math.sqrt(20 / 19 * 500 / 200**2))
        self.assertGreater(se_clustered, se_mixed)

    def test_clustered_se_near_binomial_for_independent_trades(self):
        rng = random.Random(7)
        aggs = []
        for d in range(100):
            tp = sum(1 for _ in range(20) if rng.random() < 0.5)
            aggs.append(agg(d, tp, 20 - tp))
        binomial = 100 * math.sqrt(0.25 / 2000)
        self.assertTrue(0.75 * binomial < wr_se_clustered(aggs) < 1.25 * binomial)

    def test_clustered_se_uses_only_informative_days(self):
        base = [agg(0, 8, 2), agg(1, 2, 8)]
        padded = base + [agg(2, 0, 0, n=3), agg(3, 0, 0, n=0)]
        self.assertAlmostEqual(wr_se_clustered(base), wr_se_clustered(padded))

    def test_clustered_se_none_without_two_informative_days(self):
        self.assertIsNone(wr_se_clustered([]))
        self.assertIsNone(wr_se_clustered([agg(0, 4, 1), agg(1, 0, 0)]))

    def test_lower_bound(self):
        aggs = [agg(0, 8, 2), agg(1, 2, 8), agg(2, 6, 4)]
        se = wr_se_clustered(aggs)
        self.assertAlmostEqual(wr_lower_bound(aggs), 16 / 30 * 100 - 2.0 * se)
        self.assertAlmostEqual(wr_lower_bound(aggs, z=1.0), 16 / 30 * 100 - se)
        self.assertIsNone(wr_lower_bound([agg(0, 1, 0)]))


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_p_small_for_edge_large_for_none(self):
        rng = random.Random(3)
        edge = []
        for d in range(60):
            tp = sum(1 for _ in range(20) if rng.random() < 0.65)
            edge.append(agg(d, tp, 20 - tp))
        none = [agg(d, 12, 8) if d % 2 else agg(d, 8, 12) for d in range(60)]
        p_edge = bootstrap_p_above(edge, 50.0, n=1000, seed=0)
        p_none = bootstrap_p_above(none, 50.0, n=1000, seed=0)
        self.assertAlmostEqual(p_edge, 1 / 1001)
        self.assertGreater(p_none, 0.4)

    def test_bootstrap_is_seeded(self):
        aggs = [agg(d, 6 + d % 3, 5) for d in range(30)]
        # Pooled WR 58.33%; a baseline of 58 leaves p well inside (0, 1).
        first = bootstrap_p_above(aggs, 58.0, n=500, seed=11)
        self.assertEqual(first, bootstrap_p_above(aggs, 58.0, n=500, seed=11))
        self.assertNotEqual(first, bootstrap_p_above(aggs, 58.0, n=500, seed=12))

    def test_bootstrap_without_informative_days_is_one(self):
        self.assertEqual(bootstrap_p_above([agg(0, 0, 0, n=2)], 50.0, n=100, seed=0), 1.0)

    def test_paired_bootstrap_detects_better_rule(self):
        a = [agg(d, 13, 7) for d in range(40)]
        b = [agg(d, 10, 10) for d in range(40)]
        self.assertAlmostEqual(bootstrap_paired_p(a, b, n=500, seed=0), 1 / 501)
        self.assertEqual(bootstrap_paired_p(b, b, n=500, seed=0), 1.0)
        self.assertGreater(bootstrap_paired_p(b, a, n=500, seed=0), 0.99)

    def test_paired_bootstrap_aligns_days_missing_on_one_side(self):
        # b trades only on odd days; missing days carry zero counts, not a shift.
        a = [agg(d, 9, 1) for d in range(30)]
        b = [agg(d, 3, 7) for d in range(1, 30, 2)]
        self.assertAlmostEqual(bootstrap_paired_p(a, b, n=300, seed=1), 1 / 301)

    def test_paired_bootstrap_without_informative_days_is_one(self):
        self.assertEqual(bootstrap_paired_p([agg(0, 0, 0, n=1)], [], n=50, seed=0), 1.0)

    def test_paired_bootstrap_resample_without_trades_counts_against(self):
        # b has one informative day out of 20; a resample misses it with
        # probability (19/20)^20 = 0.358, and those resamples count as <= 0.
        # Were they skipped, a (90%) would beat b (0%) everywhere: p = 1/1001.
        a = [agg(d, 9, 1) for d in range(20)]
        b = [agg(0, 0, 1)]
        p = bootstrap_paired_p(a, b, n=1000, seed=0)
        self.assertTrue(0.31 < p < 0.41, p)


class HolmAndPermutationTests(unittest.TestCase):
    def test_holm_textbook(self):
        adjusted = holm({"a": 0.01, "b": 0.03, "c": 0.04})
        self.assertEqual(set(adjusted), {"a", "b", "c"})
        self.assertAlmostEqual(adjusted["a"], 0.03)
        self.assertAlmostEqual(adjusted["b"], 0.06)
        self.assertAlmostEqual(adjusted["c"], 0.06)

    def test_holm_caps_at_one_and_keeps_order(self):
        adjusted = holm({"x": 0.6, "y": 0.2, "z": 0.001})
        self.assertAlmostEqual(adjusted["z"], 0.003)
        self.assertAlmostEqual(adjusted["y"], 0.4)
        self.assertAlmostEqual(adjusted["x"], 0.6)
        self.assertEqual(holm({"a": 0.5, "b": 0.9}), {"a": 1.0, "b": 1.0})
        self.assertEqual(holm({}), {})
        with self.assertRaises(ValueError):
            holm({"a": 1.5})

    def test_permutation_p_smoothing(self):
        self.assertAlmostEqual(permutation_p(5.0, [1.0, 2.0, 3.0]), 0.25)
        self.assertAlmostEqual(permutation_p(2.0, [1.0, 2.0, 3.0]), 0.75)
        self.assertAlmostEqual(permutation_p(0.0, []), 1.0)


def noise_blocks(rng, blocks=8, trades=50):
    out = []
    for _ in range(blocks):
        tp = sum(1 for _ in range(trades) if rng.random() < 0.5)
        out.append((tp, trades - tp))
    return out


class PBOTests(unittest.TestCase):
    def test_pbo_zero_for_dominant_rule(self):
        rng = random.Random(5)
        counts = {f"r{i:02d}": noise_blocks(rng) for i in range(10)}
        counts["dominant"] = [(40, 10)] * 8
        self.assertEqual(pbo_cscv(counts), 0.0)

    def test_pbo_mid_for_noise(self):
        # One noise dataset's PBO has a std of about 0.2 (its 70 splits are
        # strongly correlated), so average ten seeded datasets: the mean of
        # pure noise must sit near 0.5 (200 datasets: 0.497).
        values = []
        for seed in range(10):
            rng = random.Random(seed)
            values.append(pbo_cscv({f"r{i:02d}": noise_blocks(rng) for i in range(20)}))
        mean = sum(values) / len(values)
        self.assertTrue(0.3 < mean < 0.7, mean)

    def test_pbo_one_when_in_sample_winner_never_beats_the_median(self):
        # The rules alternate block by block: a split that separates the blocks
        # picks the IS winner, which is then the OOS loser (omega 1/3); a split
        # that mixes them ties both halves (IS tie -> "a", OOS average rank ->
        # omega 1/2, lambda 0). Every split has lambda <= 0.
        counts = {"a": [(10, 0), (0, 10)] * 2, "b": [(0, 10), (10, 0)] * 2}
        self.assertEqual(pbo_cscv(counts), 1.0)

    def test_pbo_validates_input(self):
        with self.assertRaises(ValueError):
            pbo_cscv({"only": [(1, 1)] * 8})
        with self.assertRaises(ValueError):
            pbo_cscv({"a": [(1, 1)] * 8, "b": [(1, 1)] * 6})
        with self.assertRaises(ValueError):
            pbo_cscv({"a": [(1, 1)] * 7, "b": [(1, 1)] * 7})
        with self.assertRaises(ValueError):
            pbo_cscv({"a": [], "b": []})
        with self.assertRaises(ValueError):
            pbo_cscv({"a": [(1, -1)] * 2, "b": [(1, 1)] * 2})

    def test_pbo_rules_without_trades_rank_last(self):
        counts = {"a": [(6, 4)] * 4, "b": [(4, 6)] * 4, "empty": [(0, 0)] * 4}
        self.assertEqual(pbo_cscv(counts), 0.0)


class SPATests(unittest.TestCase):
    def test_spa_small_for_clear_winner_large_for_noise(self):
        rng = random.Random(42)
        noise = {f"r{k}": [rng.gauss(0.0, 1.0) for _ in range(250)] for k in range(10)}
        p_noise = spa_pvalue(noise, n=1000, mean_block=5.0, seed=0)
        self.assertGreater(p_noise, 0.2)
        winner = dict(noise)
        winner["edge"] = [1.0 + rng.gauss(0.0, 1.0) for _ in range(250)]
        p_winner = spa_pvalue(winner, n=1000, mean_block=5.0, seed=0)
        self.assertLess(p_winner, 0.05)

    def test_spa_is_seeded_and_bounded(self):
        rng = random.Random(9)
        diffs = {f"r{k}": [0.1 + rng.gauss(0.0, 1.0) for _ in range(120)] for k in range(4)}
        first = spa_pvalue(diffs, n=300, seed=4)
        self.assertEqual(first, spa_pvalue(diffs, n=300, seed=4))
        self.assertTrue(0.0 <= first <= 1.0)

    def test_spa_all_negative_means_give_large_p(self):
        rng = random.Random(1)
        diffs = {f"r{k}": [-0.5 + rng.gauss(0.0, 1.0) for _ in range(200)] for k in range(5)}
        self.assertGreater(spa_pvalue(diffs, n=500, seed=0), 0.5)

    def test_spa_constant_rule_is_excluded(self):
        self.assertEqual(spa_pvalue({"flat": [0.3] * 50}, n=100, seed=0), 1.0)
        rng = random.Random(8)
        noise = [rng.gauss(0.0, 1.0) for _ in range(100)]
        with_flat = spa_pvalue({"flat": [5.0] * 100, "noise": noise}, n=200, seed=0)
        alone = spa_pvalue({"noise": noise}, n=200, seed=0)
        self.assertEqual(with_flat, alone)

    def test_spa_short_series(self):
        p = spa_pvalue({"a": [1.0, -0.5], "b": [0.2, 0.1]}, n=100, seed=0)
        self.assertTrue(0.0 <= p <= 1.0)

    def test_spa_p_is_never_zero(self):
        rng = random.Random(3)
        edge = {"edge": [5.0 + rng.gauss(0.0, 0.1) for _ in range(100)]}
        self.assertEqual(spa_pvalue(edge, n=200, seed=0), 1 / 201)

    def test_spa_validates_input(self):
        with self.assertRaises(ValueError):
            spa_pvalue({}, n=10)
        with self.assertRaises(ValueError):
            spa_pvalue({"a": [1.0, 2.0, 3.0], "b": [1.0, 2.0]}, n=10)
        with self.assertRaises(ValueError):
            spa_pvalue({"a": [1.0]}, n=10)
        with self.assertRaises(ValueError):
            spa_pvalue({"a": [1.0, 2.0]}, n=0)
        with self.assertRaises(ValueError):
            spa_pvalue({"a": [1.0, 2.0]}, n=10, mean_block=0.5)


if __name__ == "__main__":
    unittest.main()
