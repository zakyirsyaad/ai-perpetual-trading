import json
import math
import random
import unittest
from dataclasses import fields
from unittest import mock

from research.pine_gate import metalabel
from research.pine_gate.bars import wib_hour
from research.pine_gate.features import Snapshot
from research.pine_gate.metalabel import (
    FEATURES,
    Candidate,
    MetaModel,
    candidate_features,
    choose_C,
    choose_threshold,
    fit_logistic,
    reliability,
    train_meta,
    uniqueness_weights,
)
from research.pine_gate.spec import BLOCKS, CONTENDER3_FEATURES, EMBARGO_MINUTES, ModelConfig, ms

MIN = 60_000
HOUR = 60 * MIN
CFG = ModelConfig()
BLOCK_STARTS = [ms(start) for start, _ in BLOCKS]
BLOCK_ENDS = [ms(end) for _, end in BLOCKS]


def snapshot(**overrides):
    values = {f.name: None for f in fields(Snapshot)}
    values.update(
        t_ms=BLOCK_STARTS[0],
        close_5m=101.0, ema200_5m=100.0, atr14_5m=2.0,
        rsi14_5m=30.0, adx14_5m=25.0, di_plus14_5m=20.0, di_minus14_5m=12.0,
        h1_ema20=105.0, h1_ema50=100.0, h1_atr14=4.0, h1_vol_pct=70.0,
        h4_ema20=110.0, h4_ema50=100.0, h4_atr14=8.0,
    )
    values.update(overrides)
    return Snapshot(**values)


def make_candidates(per_block=120, seed=3, hold_min=30):
    """Synthetic candidates in every block whose label follows feature 0."""
    rng = random.Random(seed)
    out = []
    for block, (start, end) in enumerate(zip(BLOCK_STARTS, BLOCK_ENDS)):
        span = end - start - 2 * HOUR
        for i in range(per_block):
            t = start + (i * span) // per_block
            x = tuple(rng.gauss(0.0, 1.0) for _ in FEATURES)
            p = 1.0 / (1.0 + math.exp(-2.0 * x[0]))
            label = 1 if rng.random() < p else 0
            out.append(Candidate("BTC-USDT", t, 1, x, label, t, t + hold_min * MIN, block))
    return out


def logistic_gradient(X, y, w, C, coef, intercept):
    grad = [0.0] * (len(coef) + 1)
    for xi, yi, wi in zip(X, y, w):
        z = intercept + sum(b * v for b, v in zip(coef, xi))
        r = wi * (1.0 / (1.0 + math.exp(-z)) - yi)
        for j, v in enumerate(xi):
            grad[j] += r * v
        grad[-1] += r
    for j, b in enumerate(coef):
        grad[j] += b / C
    return grad


class FeatureTests(unittest.TestCase):
    def test_feature_names_match_the_spec(self):
        self.assertEqual(FEATURES, CONTENDER3_FEATURES)
        self.assertEqual(FEATURES, ("trend_strength", "rsi_side", "adx", "di_diff_side",
                                    "vol_pct", "poc_dist_side", "hour_sin", "hour_cos"))

    def test_long_features_by_trend_source(self):
        s = snapshot()
        t = s.t_ms
        angle = 2.0 * math.pi * wib_hour(t) / 24.0
        e200 = candidate_features(s, 1, "E200", 99.0, t)
        self.assertEqual(len(e200), len(FEATURES))
        self.assertAlmostEqual(e200[0], (101.0 - 100.0) / 2.0)
        self.assertEqual(e200[1:5], (30.0, 25.0, 8.0, 70.0))
        self.assertAlmostEqual(e200[5], (101.0 - 99.0) / 2.0)
        self.assertAlmostEqual(e200[6], math.sin(angle))
        self.assertAlmostEqual(e200[7], math.cos(angle))
        self.assertAlmostEqual(candidate_features(s, 1, "H1", 99.0, t)[0], 5.0 / 4.0)
        self.assertAlmostEqual(candidate_features(s, 1, "H4", 99.0, t)[0], 10.0 / 8.0)

    def test_short_features_are_signed_by_side(self):
        s = snapshot()
        short = candidate_features(s, -1, "H1", 99.0, s.t_ms)
        self.assertAlmostEqual(short[0], -5.0 / 4.0)
        self.assertEqual(short[1], 70.0)
        self.assertEqual(short[2], 25.0)
        self.assertEqual(short[3], -8.0)
        self.assertAlmostEqual(short[5], -1.0)

    def test_hour_uses_the_given_tick_time(self):
        s = snapshot()
        t = BLOCK_STARTS[0] + 6 * HOUR  # 06:00 WIB
        x = candidate_features(s, 1, "E200", 99.0, t)
        self.assertAlmostEqual(x[6], 1.0)
        self.assertAlmostEqual(x[7], 0.0)

    def test_features_none_when_any_input_missing(self):
        needed = {
            "E200": ("close_5m", "ema200_5m", "atr14_5m", "rsi14_5m", "adx14_5m",
                     "di_plus14_5m", "di_minus14_5m", "h1_vol_pct"),
            "H1": ("h1_ema20", "h1_ema50", "h1_atr14"),
            "H4": ("h4_ema20", "h4_ema50", "h4_atr14"),
        }
        for trend, names in needed.items():
            for name in names:
                with self.subTest(trend=trend, missing=name):
                    s = snapshot(**{name: None})
                    self.assertIsNone(candidate_features(s, 1, trend, 99.0, s.t_ms))
        s = snapshot()
        self.assertIsNone(candidate_features(s, 1, "E200", None, s.t_ms))
        # Inputs of another trend source are not needed.
        self.assertIsNotNone(candidate_features(snapshot(h4_atr14=None), 1, "H1", 99.0, s.t_ms))

    def test_features_none_when_an_atr_is_not_positive(self):
        for trend, name in (("E200", "atr14_5m"), ("H1", "h1_atr14"), ("H4", "h4_atr14"),
                            ("H1", "atr14_5m")):
            for value in (0.0, -1.0):
                with self.subTest(trend=trend, name=name, value=value):
                    s = snapshot(**{name: value})
                    self.assertIsNone(candidate_features(s, 1, trend, 99.0, s.t_ms))

    def test_features_reject_bad_side_or_trend(self):
        s = snapshot()
        with self.assertRaises(ValueError):
            candidate_features(s, 0, "E200", 99.0, s.t_ms)
        with self.assertRaises(ValueError):
            candidate_features(s, 1, "D1", 99.0, s.t_ms)


class CandidateTests(unittest.TestCase):
    def test_candidate_validates_its_fields(self):
        x = (0.0,) * len(FEATURES)
        t = BLOCK_STARTS[0]
        Candidate("BTC-USDT", t, 1, x, 1, t, t + MIN, 0)
        bad = (
            dict(side=0), dict(label=2), dict(x=(0.0,) * 3), dict(block=4),
            dict(block=-1), dict(end_ms=t - 1),
        )
        for override in bad:
            args = dict(symbol="BTC-USDT", t_ms=t, side=1, x=x, label=1,
                        start_ms=t, end_ms=t + MIN, block=0)
            args.update(override)
            with self.subTest(override=override), self.assertRaises(ValueError):
                Candidate(**args)


class UniquenessTests(unittest.TestCase):
    def test_uniqueness_halves_for_identical_intervals_and_is_one_when_disjoint(self):
        self.assertEqual(uniqueness_weights([(0, 10 * MIN), (0, 10 * MIN)]), [0.5, 0.5])
        self.assertEqual(uniqueness_weights([(0, 10 * MIN), (10 * MIN, 20 * MIN)]), [1.0, 1.0])

    def test_uniqueness_averages_over_minutes(self):
        weights = uniqueness_weights([(0, 2 * MIN), (MIN, 3 * MIN)])
        self.assertEqual(weights, [0.75, 0.75])
        three = uniqueness_weights([(0, 4 * MIN), (0, 2 * MIN), (MIN, 2 * MIN)])
        # Minutes 0..3 of the first: concurrency 2, 3, 1, 1.
        self.assertAlmostEqual(three[0], (0.5 + 1 / 3 + 1 + 1) / 4)
        self.assertAlmostEqual(three[1], (0.5 + 1 / 3) / 2)
        self.assertAlmostEqual(three[2], 1 / 3)

    def test_uniqueness_floors_to_minutes_and_handles_zero_length(self):
        self.assertEqual(uniqueness_weights([(30_000, 59_000), (0, MIN)]), [1.0, 1.0])
        self.assertEqual(uniqueness_weights([(5 * MIN, 5 * MIN)]), [1.0])
        self.assertEqual(uniqueness_weights([]), [])
        with self.assertRaises(ValueError):
            uniqueness_weights([(2 * MIN, MIN)])


class LogisticTests(unittest.TestCase):
    def setUp(self):
        rng = random.Random(7)
        self.X = [(rng.gauss(0, 1), rng.gauss(0, 1)) for _ in range(300)]
        self.y = [1 if a - 0.5 * b + rng.gauss(0, 0.5) > 0 else 0 for a, b in self.X]
        self.w = [rng.uniform(0.2, 1.0) for _ in self.X]

    def test_logistic_recovers_sign_on_separable_data(self):
        X = [(float(v),) for v in range(-10, 11) if v != 0]
        y = [1 if x[0] > 0 else 0 for x in X]
        coef, intercept = fit_logistic(X, y, [1.0] * len(X), 1.0)
        self.assertGreater(coef[0], 0.0)
        flipped, _ = fit_logistic([(-x[0],) for x in X], y, [1.0] * len(X), 1.0)
        self.assertLess(flipped[0], 0.0)
        self.assertAlmostEqual(intercept, 0.0, places=6)

    def test_fit_satisfies_first_order_conditions(self):
        C = 0.5
        coef, intercept = fit_logistic(self.X, self.y, self.w, C)
        self.assertGreater(coef[0], 0.0)
        self.assertLess(coef[1], 0.0)
        grad = logistic_gradient(self.X, self.y, self.w, C, coef, intercept)
        for g in grad:
            self.assertAlmostEqual(g, 0.0, places=6)

    def test_stronger_penalty_shrinks_coefficients(self):
        weak = fit_logistic(self.X, self.y, self.w, 1.0)[0]
        strong = fit_logistic(self.X, self.y, self.w, 0.01)[0]
        self.assertLess(math.hypot(*strong), math.hypot(*weak))
        self.assertLess(abs(strong[0]), abs(weak[0]))

    def test_intercept_is_not_penalised(self):
        X = [(0.0,)] * 100
        y = [1] * 80 + [0] * 20
        coef, intercept = fit_logistic(X, y, [1.0] * 100, 0.01)
        self.assertAlmostEqual(intercept, math.log(80 / 20), places=6)
        self.assertEqual(coef, (0.0,))

    def test_fit_validates_inputs(self):
        with self.assertRaises(ValueError):
            fit_logistic(self.X, self.y[:-1], self.w, 1.0)
        with self.assertRaises(ValueError):
            fit_logistic(self.X, self.y, self.w[:-1], 1.0)
        with self.assertRaises(ValueError):
            fit_logistic(self.X, self.y, self.w, 0.0)
        with self.assertRaises(ValueError):
            fit_logistic([(1.0, 2.0), (1.0,)], [0, 1], [1.0, 1.0], 1.0)
        with self.assertRaises(ValueError):
            fit_logistic([], [], [], 1.0)


class ModelTests(unittest.TestCase):
    def model(self):
        n = len(FEATURES)
        return MetaModel(
            coef=tuple(0.1 * (j + 1) for j in range(n)),
            intercept=-0.3,
            means=tuple(float(j) for j in range(n)),
            stds=tuple([2.0] * (n - 1) + [0.0]),
            threshold=0.63,
            C=0.1,
        )

    def test_prob_standardises_and_treats_zero_std_as_one(self):
        m = self.model()
        x = tuple(float(j) + 2.0 for j in range(len(FEATURES)))
        z = -0.3 + sum(0.1 * (j + 1) * (1.0 if j < len(FEATURES) - 1 else 2.0)
                       for j in range(len(FEATURES)))
        self.assertAlmostEqual(m.prob(x), 1.0 / (1.0 + math.exp(-z)))
        with self.assertRaises(ValueError):
            m.prob((0.0,))

    def test_prob_is_stable_for_extreme_inputs(self):
        m = self.model()
        self.assertEqual(m.prob((1e6,) * len(FEATURES)), 1.0)
        self.assertEqual(m.prob((-1e6,) * len(FEATURES)), 0.0)

    def test_model_dict_roundtrip(self):
        m = self.model()
        d = m.to_dict()
        restored = MetaModel.from_dict(json.loads(json.dumps(d)))
        self.assertEqual(restored, m)
        self.assertEqual(MetaModel.from_dict(restored.to_dict()), m)
        self.assertEqual(d["features"], list(FEATURES))
        self.assertIsInstance(d["coef"], list)
        x = (0.5,) * len(FEATURES)
        self.assertEqual(restored.prob(x), m.prob(x))
        bad = dict(d, features=list(reversed(FEATURES)))
        with self.assertRaises(ValueError):
            MetaModel.from_dict(bad)

    def test_model_validates_lengths(self):
        with self.assertRaises(ValueError):
            MetaModel(coef=(1.0,), intercept=0.0, means=(0.0,), stds=(1.0,),
                      threshold=0.5, C=1.0)


class BestCTests(unittest.TestCase):
    def test_lowest_pooled_loss_wins_and_non_finite_scores_are_refused(self):
        self.assertEqual(metalabel._best_C({0.1: [0.6, 0.6], 1.0: [0.7, 0.5]}, [10, 30]), 1.0)
        self.assertEqual(metalabel._best_C({0.1: [0.5], 1.0: [0.5]}, [5]), 0.1)  # tie: smaller
        for losses in ({0.1: [math.nan], 1.0: [math.inf]}, {}):
            with self.subTest(losses=losses):
                with self.assertRaises(ValueError):
                    metalabel._best_C(losses, [5])


class ReliabilityTests(unittest.TestCase):
    def test_reliability_bins_non_empty_deciles(self):
        rows = reliability([0.05, 0.08, 0.55, 0.59, 1.0], [0, 1, 1, 1, 1])
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(rows[0][0], 0.065)
        self.assertEqual(rows[0][1:], (0.5, 2))
        self.assertAlmostEqual(rows[1][0], 0.57)
        self.assertEqual(rows[1][1:], (1.0, 2))
        self.assertEqual(rows[2], (1.0, 1.0, 1))
        with self.assertRaises(ValueError):
            reliability([0.5], [1, 0])
        with self.assertRaises(ValueError):
            reliability([1.5], [1])


class ThresholdTests(unittest.TestCase):
    def test_threshold_scan_highest_meeting_supply_else_half(self):
        asked = []

        def supply(p):
            asked.append(p)
            return 12.0 if p <= 0.63 + 1e-9 else 5.0

        self.assertAlmostEqual(choose_threshold(supply, CFG), 0.63)
        self.assertAlmostEqual(asked[0], 0.70)
        self.assertEqual(len(asked), 8)
        self.assertAlmostEqual(choose_threshold(lambda p: 10.0, CFG), 0.70)
        calls = []
        self.assertEqual(choose_threshold(lambda p: calls.append(p) or 9.99, CFG), 0.5)
        self.assertEqual(len(calls), 21)
        self.assertAlmostEqual(calls[-1], 0.50)


class CrossValidationTests(unittest.TestCase):
    def test_choose_C_only_fits_on_past_blocks(self):
        cands = make_candidates(per_block=60)
        # A block-1 trade that ends after B3 starts: purged from fold 2's
        # training set, allowed in fold 3's.
        t = BLOCK_ENDS[1] - 3 * HOUR
        straddler = Candidate("ETH-USDT", t, -1, (0.1,) * len(FEATURES), 1,
                              t, BLOCK_ENDS[1] + HOUR, 1)
        cands.append(straddler)
        seen = []
        real = metalabel._fit_candidates

        def spy(train, C):
            seen.append((tuple(train), C))
            return real(train, C)

        with mock.patch.object(metalabel, "_fit_candidates", side_effect=spy):
            chosen, losses = choose_C(cands, CFG.c_grid)

        self.assertIn(chosen, CFG.c_grid)
        self.assertEqual(set(losses), set(CFG.c_grid))
        self.assertTrue(all(len(v) == 3 for v in losses.values()))
        self.assertEqual(len(seen), 3 * len(CFG.c_grid))
        folds = {}
        for train, C in seen:
            k = 1 + max(c.block for c in train)
            folds.setdefault(k, []).append(C)
            self.assertTrue(all(c.block < k for c in train))
            self.assertTrue(all(c.end_ms <= BLOCK_STARTS[k] for c in train))
            self.assertEqual(straddler in train, k == 3)
        self.assertEqual(sorted(folds), [1, 2, 3])
        for k in folds:
            self.assertEqual(sorted(folds[k]), sorted(CFG.c_grid))

    def test_choose_C_scores_pooled_log_loss_and_breaks_ties_to_smaller_C(self):
        cands = make_candidates(per_block=60)
        # Pooled over folds of 10, 10 and 80: 0.01 -> 0.56, 0.1 -> 0.66,
        # 1.0 -> 0.56 (a tie, so the smaller C). Unweighted fold means would
        # prefer 0.1 (0.567 < 0.7).
        fake = {0.01: [0.8, 0.8, 0.5], 0.1: [0.5, 0.5, 0.7], 1.0: [0.8, 0.8, 0.5]}
        sizes = [10, 10, 80]

        def fake_cv(candidates, c_grid):
            return {c: list(fake[c]) for c in c_grid}, sizes, {c: [] for c in c_grid}

        with mock.patch.object(metalabel, "_cross_validate", side_effect=fake_cv):
            chosen, losses = choose_C(cands, CFG.c_grid)
        self.assertEqual(chosen, 0.01)
        self.assertEqual(losses, fake)

    def test_choose_C_picks_finite_losses_and_validates_grid(self):
        cands = make_candidates(per_block=60)
        chosen, losses = choose_C(cands, CFG.c_grid)
        for values in losses.values():
            self.assertTrue(all(math.isfinite(v) and v > 0 for v in values))
        with self.assertRaises(ValueError):
            choose_C(cands, ())
        with self.assertRaises(ValueError):
            choose_C(cands, (0.0, 1.0))

    def test_empty_fold_is_an_error(self):
        cands = [c for c in make_candidates(per_block=30) if c.block != 2]
        with self.assertRaises(ValueError):
            choose_C(cands, CFG.c_grid)


class TrainMetaTests(unittest.TestCase):
    def test_train_meta_scans_threshold_on_out_of_fold_probs_and_refits_on_all(self):
        cands = make_candidates(per_block=120)
        early = BLOCK_STARTS[2] + (EMBARGO_MINUTES // 2) * MIN
        late = BLOCK_STARTS[2] + (EMBARGO_MINUTES + 1) * MIN
        x = (0.0,) * len(FEATURES)
        cands.append(Candidate("ETH-USDT", early, 1, x, 0, early, early + MIN, 2))
        cands.append(Candidate("ETH-USDT", late, 1, x, 0, late, late + MIN, 2))
        seen = []

        def supply_for(oof, p):
            seen.append(dict(oof))
            return sum(1 for v in oof.values() if v >= p) / 10.0

        model, diag = train_meta(cands, supply_for, CFG)

        oof_keys = {(row[0], row[1]) for row in diag["oof"]}
        validation = [c for c in cands if c.block >= 1
                      and c.t_ms >= BLOCK_STARTS[c.block] + EMBARGO_MINUTES * MIN]
        self.assertEqual(oof_keys, {(c.symbol, c.t_ms) for c in validation})
        self.assertNotIn(("ETH-USDT", early), oof_keys)
        self.assertIn(("ETH-USDT", late), oof_keys)
        self.assertEqual(diag["chosen_C"], model.C)
        self.assertEqual(set(diag["cv_logloss"]), set(CFG.c_grid))
        self.assertEqual(seen[0], {(r[0], r[1]): r[2] for r in diag["oof"]})
        expected = choose_threshold(lambda p: supply_for(seen[0], p), CFG)
        self.assertAlmostEqual(model.threshold, expected)
        self.assertGreater(model.coef[0], 0.0)
        self.assertEqual(sum(n for _, _, n in diag["reliability"]), len(diag["oof"]))
        # The final scaler is fit on every training candidate.
        mean0 = sum(c.x[0] for c in cands) / len(cands)
        self.assertAlmostEqual(model.means[0], mean0)
        for row in diag["oof"]:
            self.assertTrue(0.0 < row[2] < 1.0)
            self.assertIn(row[3], (0, 1))

    def test_train_meta_falls_back_when_supply_is_short(self):
        cands = make_candidates(per_block=60)
        model, _ = train_meta(cands, lambda oof, p: 0.0, CFG)
        self.assertEqual(model.threshold, CFG.fallback_threshold)

    def test_duplicate_candidate_keys_are_rejected(self):
        cands = make_candidates(per_block=60)
        cands.append(cands[-1])
        with self.assertRaises(ValueError):
            train_meta(cands, lambda oof, p: 0.0, CFG)


if __name__ == "__main__":
    unittest.main()
