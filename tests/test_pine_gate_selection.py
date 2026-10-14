import math
import unittest
from datetime import date, timedelta

from research.pine_gate.selection import (
    RuleResult,
    Selection,
    eligible,
    rank_value,
    select,
    summarize,
)
from research.pine_gate.simulate import Trade
from research.pine_gate.spec import (
    RuleSpec,
    SelectionConfig,
    stage1_neighbours,
    stage1_rules,
)
from research.pine_gate.stats import pbo_cscv

CFG = SelectionConfig()
RULES = stage1_rules()
DEFAULT_SCORE = 1.0
DEFAULT_WR = 55.0


def result(rule, score=DEFAULT_SCORE, wr=DEFAULT_WR, n=2000, supply=12.0,
           block_wr=None, block_lb=None):
    return RuleResult(
        rule=rule,
        trades=(),
        block_counts=((60, 40),) * 4,
        cscv_counts=((60, 40),) * 8,
        n_tp_sl=n,
        supply_per_day=supply,
        wr=wr,
        lower_bound=score,
        block_wr=block_wr if block_wr is not None else (60.0,) * 4,
        block_lb=block_lb if block_lb is not None else (score,) * 4,
    )


class Script:
    """A fake evaluate: per-key overrides on top of a flat default result."""

    def __init__(self, overrides=None):
        self.overrides = dict(overrides or {})
        self.calls = []

    def __call__(self, rule):
        self.calls.append(rule.key())
        if rule.key() in self.overrides:
            return self.overrides[rule.key()]
        return result(rule)


class EligibilityAndRankTest(unittest.TestCase):
    def test_eligible_needs_sample_and_supply(self):
        r = RULES[0]
        self.assertTrue(eligible(result(r, n=1000, supply=10.0), CFG))
        self.assertFalse(eligible(result(r, n=999, supply=50.0), CFG))
        self.assertFalse(eligible(result(r, n=5000, supply=9.99), CFG))

    def test_rank_requires_three_of_four_blocks_above_half(self):
        r = RULES[0]
        three = result(r, block_wr=(60.0, 61.0, 62.0, 40.0), block_lb=(5.0, 6.0, 7.0, -3.0))
        self.assertEqual(rank_value(three, CFG), 5.5)  # median of -3, 5, 6, 7
        two = result(r, block_wr=(60.0, 61.0, 50.0, 40.0), block_lb=(5.0, 6.0, 7.0, 8.0))
        self.assertEqual(rank_value(two, CFG), -math.inf)  # 50.0 is not above 50

    def test_block_without_trades_counts_as_failing(self):
        r = RULES[0]
        ok = result(r, block_wr=(60.0, 61.0, 62.0, None), block_lb=(5.0, 6.0, 7.0, None))
        self.assertEqual(rank_value(ok, CFG), 5.5)  # None lb -> -inf; median(-inf, 5, 6, 7)
        bad = result(r, block_wr=(60.0, 61.0, None, None), block_lb=(5.0, 6.0, None, None))
        self.assertEqual(rank_value(bad, CFG), -math.inf)

    def test_none_lower_bound_ranks_as_minus_infinity(self):
        r = RULES[0]
        res = result(r, block_wr=(60.0,) * 4, block_lb=(None, None, 5.0, 6.0))
        self.assertEqual(rank_value(res, CFG), -math.inf)


class SelectTest(unittest.TestCase):
    def test_ineligible_rule_never_selected_even_with_perfect_wr(self):
        star = RULES[3]
        script = Script({
            star.key(): result(star, score=99.0, wr=100.0, n=500),
            RULES[7].key(): result(RULES[7], score=2.0),
        })
        sel = select(script, CFG)
        self.assertIsInstance(sel, Selection)
        self.assertFalse(sel.supply_limited)
        self.assertNotIn(star, sel.top)
        self.assertEqual(sel.choice, RULES[7])
        self.assertTrue(sel.stable)

    def test_stage2_only_for_top5(self):
        scores = {RULES[i].key(): result(RULES[i], score=10.0 - i) for i in range(8)}
        script = Script(scores)
        sel = select(script, CFG)
        self.assertEqual(sel.top, tuple(RULES[:5]))
        filtered = [k for k in script.calls if not k.endswith("|OFF|OFF")]
        self.assertEqual(len(filtered), 5 * 5)
        bases_with_variants = {tuple(k.split("|")[:3]) for k in filtered}
        self.assertEqual(bases_with_variants, {(r.trend, r.entry, r.strength) for r in RULES[:5]})
        self.assertEqual(len(script.calls), 36 + 25)
        self.assertEqual(len(set(script.calls)), len(script.calls))  # evaluated once each
        self.assertEqual(len(sel.results), 61)

    def test_stage2_variant_can_win_and_pbo_covers_every_result(self):
        base = RULES[0]
        variant = RuleSpec(base.trend, base.entry, base.strength, "HIGH", "OFF")
        script = Script({
            base.key(): result(base, score=10.0),
            variant.key(): result(variant, score=12.0),
        })
        sel = select(script, CFG)
        self.assertEqual(sel.choice, variant)
        self.assertTrue(sel.stable)
        expected = pbo_cscv({k: r.cscv_counts for k, r in sel.results.items()})
        self.assertEqual(sel.pbo, expected)

    def test_stable_area_moves_to_next_rule(self):
        first, second = RULES[0], RULES[20]
        overrides = {
            first.key(): result(first, score=10.0, wr=70.0),
            second.key(): result(second, score=9.0, wr=55.0),
        }
        sel = select(Script(overrides), CFG)
        self.assertEqual(sel.top[0], first)
        self.assertEqual(sel.choice, second)
        self.assertTrue(sel.stable)

    def test_stable_area_boundary_is_four_of_seven_inclusive(self):
        base = RULES[0]
        neighbours = stage1_neighbours(base)
        overrides = {base.key(): result(base, score=10.0, wr=60.0)}
        for i, n in enumerate(neighbours):
            wr = 57.0 if i < 4 else 56.9  # 57.0 == base - 3.0 passes
            overrides[n.key()] = result(n, score=0.5, wr=wr)
        sel = select(Script(overrides), CFG)
        self.assertEqual(sel.choice, base)
        overrides[neighbours[3].key()] = result(neighbours[3], score=0.5, wr=56.9)
        sel = select(Script(overrides), CFG)
        self.assertNotEqual(sel.choice, base)

    def test_neighbour_without_wr_is_not_stable_support(self):
        base = RULES[0]
        overrides = {base.key(): result(base, score=10.0, wr=60.0)}
        for i, n in enumerate(stage1_neighbours(base)):
            overrides[n.key()] = result(n, score=0.5, wr=(60.0 if i < 3 else None))
        sel = select(Script(overrides), CFG)
        self.assertNotEqual(sel.choice, base)

    def test_no_stable_candidate_gives_no_choice(self):
        overrides = {r.key(): result(r, block_wr=(40.0,) * 4, wr=50.0) for r in RULES}
        for i in (0, 9, 18):  # three scattered rules pass blocks but have no stable area
            overrides[RULES[i].key()] = result(RULES[i], score=5.0, wr=80.0)
        sel = select(Script(overrides), CFG)
        self.assertIsNone(sel.choice)
        self.assertFalse(sel.stable)
        self.assertEqual(len(sel.top), 3)

    def test_no_rule_passes_block_consistency_gives_no_choice(self):
        overrides = {r.key(): result(r, block_wr=(40.0,) * 4) for r in RULES}
        sel = select(Script(overrides), CFG)
        self.assertIsNone(sel.choice)
        self.assertEqual(sel.top, ())
        self.assertFalse(sel.stable)

    def test_supply_limited_fallback(self):
        thin = RULES[1]
        overrides = {r.key(): result(r, supply=4.0) for r in RULES}
        overrides[thin.key()] = result(thin, score=50.0, supply=4.0, n=900)
        overrides[RULES[2].key()] = result(RULES[2], score=3.0, supply=4.0)
        sel = select(Script(overrides), CFG)
        self.assertTrue(sel.supply_limited)
        self.assertIn(thin, sel.top)  # ranked in the pool of all 36
        self.assertEqual(sel.choice, RULES[2])  # but min_tp_sl still binds the winner
        self.assertTrue(sel.stable)

    def test_supply_limited_not_flagged_when_some_rule_is_eligible(self):
        overrides = {r.key(): result(r, supply=4.0) for r in RULES}
        overrides[RULES[5].key()] = result(RULES[5], score=5.0, supply=10.0)
        sel = select(Script(overrides), CFG)
        self.assertFalse(sel.supply_limited)
        self.assertEqual(sel.top, (RULES[5],))
        self.assertEqual(sel.choice, RULES[5])

    def test_ties_break_by_rule_order(self):
        ordered = sorted(RULES)
        sel = select(Script(), CFG)
        self.assertEqual(sel.top, tuple(ordered[:5]))
        base = RuleSpec(sel.choice.trend, sel.choice.entry, sel.choice.strength)
        self.assertEqual(base, ordered[0])


def trade(entry_ms, kind, day, gross=0.0):
    return Trade(
        symbol="BTC-USDT", side=1, signal_ms=entry_ms - 1000, entry_ms=entry_ms,
        exit_ms=entry_ms + 60_000, kind=kind, gross_bps=gross, net_usdt=0.0, wib_day=day,
    )


class SummarizeTest(unittest.TestCase):
    def test_summarize_assigns_by_entry_and_computes_block_stats(self):
        d0 = date(2026, 1, 5)
        block_days = tuple(tuple(d0 + timedelta(days=4 * b + i) for i in range(4)) for b in range(4))
        all_days = tuple(d for bd in block_days for d in bd)
        block_bounds = tuple((b * 1000, (b + 1) * 1000) for b in range(4))
        cscv_bounds = tuple((b * 500, (b + 1) * 500) for b in range(8))
        trades = []
        # block 0 (entries 0..999): 3 TP over days 0,1,2 and 1 SL; a 'TIME' stop
        for i, (kind, off) in enumerate([("TP", 0), ("TP", 100), ("TP", 200), ("SL", 300), ("TIME", 400)]):
            trades.append(trade(off, kind, block_days[0][min(i, 3)]))
        # block 1: no trades at all; block 2: SL only; block 3: TP on one day
        trades.append(trade(2100, "SL", block_days[2][0]))
        trades.append(trade(3600, "TP", block_days[3][0]))
        rule = RULES[0]
        res = summarize(rule, trades, block_bounds, cscv_bounds, block_days, all_days)
        self.assertEqual(res.rule, rule)
        self.assertEqual(res.block_counts, ((3, 1), (0, 0), (0, 1), (1, 0)))
        self.assertEqual(sum(tp + sl for tp, sl in res.cscv_counts), 6)
        self.assertEqual(res.cscv_counts[0], (3, 1))   # entries 0..400 -> [0, 500)
        self.assertEqual(res.cscv_counts[4], (0, 1))   # 2100 -> [2000, 2500)
        self.assertEqual(res.cscv_counts[7], (1, 0))   # 3600 -> [3500, 4000)
        self.assertEqual(res.n_tp_sl, 6)
        self.assertAlmostEqual(res.supply_per_day, 7 / 16)
        self.assertAlmostEqual(res.wr, 100.0 * 4 / 6)
        self.assertAlmostEqual(res.block_wr[0], 75.0)
        self.assertIsNone(res.block_wr[1])
        self.assertEqual(res.block_wr[2], 0.0)
        self.assertEqual(res.block_wr[3], 100.0)
        self.assertEqual(len(res.block_lb), 4)
        self.assertIsNotNone(res.block_lb[0])
        self.assertIsNone(res.block_lb[1])
        self.assertIsInstance(res.trades, tuple)
        self.assertEqual(rank_value(res, CFG), -math.inf)  # only block 0 and 3 above 50

    def test_summarize_ignores_trades_outside_all_ranges_for_counts(self):
        d = date(2026, 1, 5)
        res = summarize(
            RULES[0], [trade(9999, "TP", d)], ((0, 1000),) * 4, ((0, 500),) * 8,
            ((d,),) * 4, (d,),
        )
        self.assertEqual(res.block_counts, ((0, 0),) * 4)
        self.assertEqual(res.n_tp_sl, 1)  # the pooled total still counts every trade


if __name__ == "__main__":
    unittest.main()
