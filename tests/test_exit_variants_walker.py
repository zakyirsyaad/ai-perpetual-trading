"""Tests of the exit-variant walker, ``research.exit_variants.walker``.

Three layers:

* Hand-built bars described in *net space* (the bot's own net estimate after both taker fees), run for
  both sides, pin each rule of the walk: the profit lock and the trailing stop under the booking
  conventions A, B and C, the stop-first order inside a bar, gaps and the time limit.
* The null rule (no lock, no trail) must give the exit reason, bar and raw price of the backtest's own
  walk, ``research.mode_backtest.live_sim.resolve_live_exit``, on seeded random bars of both sides.
* The money of a settled walk (``settle``) and the statistics the report uses.

Booking conventions of a lock inside a 1m bar (see the module docstring of the walker):
A arms from the next bar; B arms and stops inside the same bar (an extreme bound); C is A plus "a bar
that arms or ratchets the stop and closes beyond it is stopped inside the bar at the level".
"""

from __future__ import annotations

import dataclasses
import math
import random
import unittest
from array import array
from collections import Counter
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

from bot.strategy.modes.live_exit import LiveExitLevels, net_target_price
from research.exit_variants.walker import (
    LOCK,
    LONG,
    SHORT,
    SL,
    TP,
    WAKTU,
    Cols,
    Costs,
    ExitRule,
    Outcome,
    day_bootstrap,
    group_by_day,
    mean_se,
    net_for_price,
    price_for_net,
    settle,
    walk,
)
from research.mode_backtest.live_sim import resolve_live_exit
from research.pine_gate.bars import Bars

E, N, FEE_PCT, SL_PCT = 100_000.0, 25_000.0, 0.05, 0.30
LIVE_FLOOR = 56.0
STEP = 60_000

NULL = ExitRule("null", limit_min=10)
LOCK_A = ExitRule("lock", lock_steps=((40.0, 10.0),), limit_min=10)
LOCK_B = dataclasses.replace(LOCK_A, same_bar=True)
LOCK_C = dataclasses.replace(LOCK_A, close_check=True)


class SideConstantsTests(unittest.TestCase):
    def test_the_sides_are_the_rule_cores_plus_and_minus_one(self) -> None:
        self.assertEqual((LONG, SHORT), (1, -1))


if TYPE_CHECKING:  # the mixin below is only ever combined with unittest.TestCase
    _MixinBase = unittest.TestCase
else:
    _MixinBase = object


class _SideCase(_MixinBase):
    """Hand-built bars for one side; the concrete classes below set ``side``."""

    side = 0

    def setUp(self) -> None:
        self.flat = self.bar(0.0, 0.5, -0.5)
        self.tail = [self.quiet(44.0)] * 8

    def price(self, net: float) -> float:
        return price_for_net(E, self.side, N, FEE_PCT, net)

    def net_of(self, price: float) -> float:
        return net_for_price(E, self.side, N, FEE_PCT, price)

    def bar(self, open_net: float, best_net: float, worst_net: float) -> tuple[float, float, float]:
        """(open, high, low) of a bar given in net space; the best net is the favourable extreme."""
        o, b, w = self.price(open_net), self.price(best_net), self.price(worst_net)
        return o, max(o, b, w), min(o, b, w)

    def quiet(self, net: float) -> tuple[float, float, float]:
        return self.bar(net, net + 0.5, net - 0.5)

    def try_walk(self, bars: list, rule: ExitRule, sl_pct: float = SL_PCT) -> Optional[Outcome]:
        cols = Cols(
            [i * STEP for i in range(len(bars))],
            [b[0] for b in bars],
            [b[1] for b in bars],
            [b[2] for b in bars],
        )
        return walk(cols, 0, self.side, E, sl_pct, N, FEE_PCT, rule)

    def run_walk(self, bars: list, rule: ExitRule, sl_pct: float = SL_PCT) -> Outcome:
        """The outcome of a walk that must end; the test fails when the data runs out first."""
        outcome = self.try_walk(bars, rule, sl_pct)
        if outcome is None:
            self.fail("the walk ended without an exit")
        return outcome

    def assertAt(self, outcome: Outcome, reason: str, idx: int, net: Optional[float] = None) -> None:
        self.assertEqual((outcome.reason, outcome.idx), (reason, idx))
        if net is not None:
            self.assertAlmostEqual(outcome.raw_price, self.price(net), places=6)


class PriceNetTests(_SideCase):
    def test_price_and_net_are_inverse(self) -> None:
        for net in (-95.0, 0.0, 15.0, 56.0):
            self.assertAlmostEqual(self.net_of(self.price(net)), net, places=9)

    def test_a_better_net_is_a_better_price_for_the_side(self) -> None:
        better = self.price(20.0) > self.price(10.0)
        self.assertEqual(better, self.side == LONG)

    def test_a_net_that_cannot_be_priced_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            price_for_net(E, self.side, N, FEE_PCT, math.nan)
        with self.assertRaises(ValueError):  # a loss so large that the exit price would be zero or below
            price_for_net(E, self.side, N, FEE_PCT, -self.side * 1e9)

    def test_the_target_price_is_the_backtests_take_profit_level(self) -> None:
        rules: Any = SimpleNamespace(fee_perkiraan_persen=FEE_PCT, tp_net_usdt=LIVE_FLOOR)  # all the function reads
        self.assertAlmostEqual(self.price(LIVE_FLOOR), net_target_price(E, self.side, N, rules), places=9)


class ShortPriceNetTests(PriceNetTests, unittest.TestCase):
    side = SHORT


class LongPriceNetTests(PriceNetTests, unittest.TestCase):
    side = LONG


class LockRuleTests(_SideCase):
    def test_arm_then_retrace_stops_at_the_lock_level_in_the_next_bar(self) -> None:
        bars = [self.flat, self.bar(0, 45, -2), self.bar(40, 40, 8)] + self.tail
        self.assertAt(self.run_walk(bars, LOCK_A), LOCK, 2, 10.0)

    def test_the_arming_bar_cannot_stop_itself_under_a(self) -> None:
        bars = [self.flat, self.bar(0, 45, 5), self.bar(44, 44.5, 43.5)] + self.tail
        self.assertEqual(self.run_walk(bars, LOCK_A).reason, WAKTU)

    def test_the_arming_bar_stops_itself_at_the_level_under_b(self) -> None:
        bars = [self.flat, self.bar(0, 45, 5), self.bar(44, 44.5, 43.5)] + self.tail
        self.assertAt(self.run_walk(bars, LOCK_B), LOCK, 1, 10.0)

    def test_c_holds_when_the_arming_bar_closes_above_the_level(self) -> None:
        bars = [self.flat, self.bar(0, 45, 5), self.bar(44, 44.5, 43.5)] + self.tail
        self.assertEqual(self.run_walk(bars, LOCK_C).reason, WAKTU)  # it closed at +44

    def test_c_stops_inside_the_arming_bar_when_it_closes_beyond_the_level(self) -> None:
        bars = [self.flat, self.bar(0, 45, 5), self.bar(5, 6, 4)] + self.tail
        self.assertAt(self.run_walk(bars, LOCK_C), LOCK, 1, 10.0)

    def test_a_books_the_same_retrace_late_at_the_next_open(self) -> None:
        bars = [self.flat, self.bar(0, 45, 5), self.bar(5, 6, 4)] + self.tail
        out = self.run_walk(bars, LOCK_A)
        self.assertAt(out, LOCK, 2)
        self.assertTrue(out.gapped)

    def test_a_gap_through_the_lock_after_arming_exits_at_the_open(self) -> None:
        bars = [self.flat, self.bar(0, 45, -2), self.bar(2, 3, 1)] + self.tail
        out = self.run_walk(bars, LOCK_A)
        self.assertAt(out, LOCK, 2, 2.0)
        self.assertTrue(out.gapped)

    def test_a_bar_that_runs_straight_to_the_target_takes_the_target(self) -> None:
        # Opens at net -10 and ends at +70 without ever being at the lock level: the target, not the open.
        bars = [self.flat, self.bar(-10, 70, -10)] + self.tail
        for rule in (ExitRule("base", limit_min=10), LOCK_A, LOCK_C):
            with self.subTest(rule=rule.name, close_check=rule.close_check):
                self.assertAt(self.run_walk(bars, rule), TP, 1, LIVE_FLOOR)

    def test_b_books_that_straight_run_as_a_lock_at_the_level_never_at_the_open(self) -> None:
        bars = [self.flat, self.bar(-10, 70, -10)] + self.tail
        out = self.run_walk(bars, LOCK_B)
        self.assertAt(out, LOCK, 1, 10.0)
        self.assertGreater(abs(out.raw_price - self.price(-10.0)), 1e-6)

    def test_a_rule_that_never_arms_equals_the_null_rule(self) -> None:
        bars = [self.flat, self.bar(0, 30, -20), self.bar(10, 20, -5)] + self.tail
        self.assertEqual(self.run_walk(bars, LOCK_A), self.run_walk(bars, NULL))

    def test_the_peak_before_the_exit_is_reported(self) -> None:
        bars = [self.flat, self.bar(0, 45, -2), self.bar(40, 40, 8)] + self.tail
        out = self.run_walk(bars, LOCK_A)
        self.assertAlmostEqual(out.peak_net, 45.0, places=6)

    def test_the_lock_never_loosens_the_hard_stop(self) -> None:
        loose = ExitRule("loose", lock_steps=((10.0, -190.0),), limit_min=10)
        bars = [self.flat, self.bar(0, 12, -2), self.bar(0, 1, -150)] + self.tail
        out = self.run_walk(bars, loose)
        hard_stop = E * (1 - self.side * SL_PCT / 100)
        self.assertEqual((out.reason, out.idx), (SL, 2))
        self.assertAlmostEqual(out.raw_price, hard_stop, places=6)

    def test_the_highest_armed_step_sets_the_stop(self) -> None:
        steps = ExitRule("steps", lock_steps=((20.0, 0.0), (40.0, 25.0)), limit_min=10)
        bars = [self.flat, self.bar(0, 45, -2), self.bar(30, 30, 20)] + self.tail
        self.assertAt(self.run_walk(bars, steps), LOCK, 2, 25.0)


class ShortLockRuleTests(LockRuleTests, unittest.TestCase):
    side = SHORT


class LongLockRuleTests(LockRuleTests, unittest.TestCase):
    side = LONG


class TrailingRuleTests(_SideCase):
    def test_a_same_bar_peak_above_the_target_never_arms_a_stop_better_than_the_target(self) -> None:
        trail = ExitRule("trail", trail_arm=35.0, trail_dist=15.0, same_bar=True, limit_min=10)
        out = self.run_walk([self.flat, self.bar(0, 80, -5)] + self.tail, trail)
        self.assertEqual(out.reason, LOCK)
        self.assertAlmostEqual(self.net_of(out.raw_price), LIVE_FLOOR - 15.0, places=6)
        self.assertLessEqual(self.net_of(out.raw_price), LIVE_FLOOR + 1e-9)

    def test_the_trail_follows_a_new_peak(self) -> None:
        peak = 52.25
        trail = ExitRule("trail", tp_net=200.0, trail_arm=40.0, trail_dist=15.0, limit_min=10)
        bars = [self.flat, self.bar(0, peak, -2), self.bar(peak, peak, 37.0)] + self.tail
        self.assertAt(self.run_walk(bars, trail), LOCK, 2, peak - 15.0)

    def test_a_trail_that_has_not_armed_leaves_the_hard_stop(self) -> None:
        trail = ExitRule("trail", trail_arm=40.0, trail_dist=20.0, limit_min=10)
        bars = [self.flat, self.bar(0, 30, -2), self.bar(0, 1, -150)] + self.tail
        self.assertEqual(self.run_walk(bars, trail).reason, SL)


class ShortTrailingRuleTests(TrailingRuleTests, unittest.TestCase):
    side = SHORT


class LongTrailingRuleTests(TrailingRuleTests, unittest.TestCase):
    side = LONG


class LiveRuleTests(_SideCase):
    """The rules every variant shares with the live bot: stop first, gaps, the time limit, the floor."""

    def test_a_bar_that_touches_both_the_stop_and_the_target_is_a_stop(self) -> None:
        out = self.run_walk([self.flat, self.bar(0, 60, -110)] + self.tail, NULL)
        self.assertEqual((out.reason, out.idx, out.gapped), (SL, 1, False))
        self.assertAlmostEqual(out.raw_price, E * (1 - self.side * SL_PCT / 100), places=6)

    def test_a_gap_through_the_hard_stop_exits_at_the_open(self) -> None:
        out = self.run_walk([self.flat, self.bar(-120, -110, -125)] + self.tail, NULL)
        self.assertAt(out, SL, 1, -120.0)
        self.assertTrue(out.gapped)

    def test_a_gap_through_the_target_exits_at_the_open(self) -> None:
        out = self.run_walk([self.flat, self.bar(60, 65, 58)] + self.tail, NULL)
        self.assertAt(out, TP, 1, 60.0)
        self.assertTrue(out.gapped)

    def test_a_lower_floor_takes_the_trade_earlier(self) -> None:
        bars = [self.flat, self.bar(0, 47, -2)] + self.tail * 2
        self.assertAt(self.run_walk(bars, NULL), WAKTU, 10)
        self.assertAt(self.run_walk(bars, ExitRule("floor", tp_net=45.0, limit_min=10)), TP, 1, 45.0)

    def test_the_time_exit_is_the_open_of_the_limit_bar_before_its_range_counts(self) -> None:
        bars = [self.quiet(5.0)] * 10 + [self.bar(3, 80, -150)]
        self.assertAt(self.run_walk(bars, NULL), WAKTU, 10, 3.0)

    def test_data_that_ends_before_the_exit_gives_none(self) -> None:
        self.assertIsNone(self.try_walk([self.quiet(5.0)] * 5, NULL))

    def test_a_missing_minute_gives_none(self) -> None:
        bars = [self.quiet(5.0)] * 6
        cols = Cols(
            [0, STEP, 2 * STEP, 4 * STEP, 5 * STEP, 6 * STEP],
            [b[0] for b in bars],
            [b[1] for b in bars],
            [b[2] for b in bars],
        )
        self.assertIsNone(walk(cols, 0, self.side, E, SL_PCT, N, FEE_PCT, NULL))


class ShortLiveRuleTests(LiveRuleTests, unittest.TestCase):
    side = SHORT


class LongLiveRuleTests(LiveRuleTests, unittest.TestCase):
    side = LONG


class InputCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cols = Cols([i * STEP for i in range(12)], [E] * 12, [E + 1] * 12, [E - 1] * 12)

    def call(self, **overrides):
        args = dict(entry_idx=0, side=SHORT, entry_fill=E, sl_pct=SL_PCT, notional=N, fee_pct=FEE_PCT, rule=NULL)
        args.update(overrides)
        return walk(
            self.cols,
            args["entry_idx"],
            args["side"],
            args["entry_fill"],
            args["sl_pct"],
            args["notional"],
            args["fee_pct"],
            args["rule"],
        )

    def test_numbers_that_cannot_give_an_answer_are_refused(self) -> None:
        bad = (
            dict(side=0),
            dict(side=2),
            dict(entry_fill=0.0),
            dict(entry_fill=math.nan),
            dict(sl_pct=0.0),
            dict(sl_pct=math.inf),
            dict(notional=-1.0),
            dict(fee_pct=-0.01),
            dict(fee_pct=math.nan),
            dict(entry_idx=-1),
            dict(entry_idx=12),
            dict(rule=ExitRule("x", limit_min=0)),
            dict(rule=ExitRule("x", tp_net=0.0)),
        )
        for overrides in bad:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.call(**overrides)

    def test_good_numbers_are_accepted(self) -> None:
        # a flat tape of 12 bars with the limit at 10 minutes reaches the time exit
        outcome = self.call()
        self.assertEqual((outcome.reason, outcome.idx), (WAKTU, 10))


def _bars_of(open_ms, opens, highs, lows) -> Bars:
    """The backtest's ``Bars`` for the same columns (the walk reads no close and no volume)."""
    return Bars(
        STEP,
        array("q", open_ms),
        array("d", opens),
        array("d", highs),
        array("d", lows),
        array("d", opens),
        array("d", [0.0] * len(opens)),
    )


def _random_case(rng: random.Random) -> dict:
    """One random walk of 1m bars with the odd jump between two bars and the odd missing minute."""
    count = rng.randint(3, 75)
    sigma = rng.choice((6.0, 20.0, 45.0, 90.0))
    drift = rng.uniform(-0.3, 0.3) * sigma
    price, minute = E, 0
    open_ms, opens, highs, lows = [], [], [], []
    for _ in range(count):
        if rng.random() < 0.05:  # the next bar opens away from the last close
            price += rng.gauss(0.0, 8 * sigma)
        close = price + rng.gauss(drift, sigma)
        high = max(price, close) + abs(rng.gauss(0.0, sigma))
        low = min(price, close) - abs(rng.gauss(0.0, sigma))
        open_ms.append(minute * STEP)
        opens.append(price)
        highs.append(high)
        lows.append(low)
        price = close
        minute += 2 if rng.random() < 0.004 else 1
    return dict(
        open_ms=open_ms,
        opens=opens,
        highs=highs,
        lows=lows,
        entry_idx=rng.randint(0, count - 1),
        sl_pct=rng.uniform(0.15, 0.45),
        limit_min=rng.randint(2, 70),
    )


class NullRuleMatchesTheBacktestWalkTests(_SideCase):
    CASES = 3000

    def test_exit_reason_bar_and_raw_price_match_resolve_live_exit(self) -> None:
        rng = random.Random(20261028 + self.side)
        seen: Counter = Counter()
        gaps = 0
        for number in range(self.CASES):
            case = _random_case(rng)
            fill = case["opens"][case["entry_idx"]]
            sl = fill * (1 - self.side * case["sl_pct"] / 100)
            tp = price_for_net(fill, self.side, N, FEE_PCT, LIVE_FLOOR)
            theirs = resolve_live_exit(
                _bars_of(case["open_ms"], case["opens"], case["highs"], case["lows"]),
                case["entry_idx"],
                self.side,
                LiveExitLevels(sl=sl, tp=tp, cut=None, sl_pct=case["sl_pct"]),
                10,
                case["limit_min"],
            )
            ours = walk(
                Cols(case["open_ms"], case["opens"], case["highs"], case["lows"]),
                case["entry_idx"],
                self.side,
                fill,
                case["sl_pct"],
                N,
                FEE_PCT,
                ExitRule("null", tp_net=LIVE_FLOOR, limit_min=case["limit_min"]),
            )
            with self.subTest(case=number):
                if theirs is None:
                    self.assertIsNone(ours)
                else:
                    self.assertIsNotNone(ours)
                    self.assertEqual(
                        (ours.reason, ours.idx, ours.raw_price, ours.gapped),
                        (theirs.reason, theirs.exit_idx, theirs.raw_price, theirs.gapped),
                    )
            seen["none" if theirs is None else theirs.reason] += 1
            gaps += bool(theirs and theirs.gapped)
        # The comparison is not vacuous: every kind of ending, and gaps, were really compared.
        for kind in ("TP", "SL", "WAKTU", "none"):
            self.assertGreater(seen[kind], 100, f"{kind}: {dict(seen)}")
        self.assertGreater(gaps, 100)


class ShortNullRuleMatchesTests(NullRuleMatchesTheBacktestWalkTests, unittest.TestCase):
    side = SHORT


class LongNullRuleMatchesTests(NullRuleMatchesTheBacktestWalkTests, unittest.TestCase):
    side = LONG


COSTS = Costs(exit_market_bps=0.5, tp_bps=0.0, lock_extra_bps=1.0)


class SettleTests(_SideCase):
    def outcome(self, reason: str, net: float, idx: int = 3) -> Outcome:
        return Outcome(reason, idx, self.price(net), False, 40.0)

    def test_a_take_profit_nets_the_target_exactly(self) -> None:
        money = settle(E, self.side, self.outcome(TP, LIVE_FLOOR), 0, N, FEE_PCT, COSTS)
        self.assertAlmostEqual(money.net, LIVE_FLOOR, places=9)
        self.assertAlmostEqual(money.exit_fill, self.price(LIVE_FLOOR), places=9)

    def test_a_market_exit_gives_up_the_slippage_against_the_position(self) -> None:
        raw = self.price(-100.0)
        money = settle(E, self.side, self.outcome(SL, -100.0), 0, N, FEE_PCT, COSTS)
        self.assertAlmostEqual(money.exit_fill, raw * (1 - self.side * 0.5 / 10_000), places=9)
        # the slippage cost sits inside the net: worse than the level by 0.5 bps of the notional
        self.assertAlmostEqual(money.net, self.net_of(raw) - N * 0.5 / 10_000, delta=0.01)

    def test_a_time_exit_uses_the_market_slippage(self) -> None:
        raw = self.price(5.0)
        money = settle(E, self.side, Outcome(WAKTU, 9, raw, False, 5.0), 0, N, FEE_PCT, COSTS)
        self.assertAlmostEqual(money.exit_fill, raw * (1 - self.side * 0.5 / 10_000), places=9)

    def test_a_lock_exit_pays_the_extra_slippage_on_top(self) -> None:
        raw = self.price(20.0)
        money = settle(E, self.side, self.outcome(LOCK, 20.0), 0, N, FEE_PCT, COSTS)
        self.assertAlmostEqual(money.exit_fill, raw * (1 - self.side * 1.5 / 10_000), places=9)

    def test_the_extra_slippage_costs_one_bp_of_the_notional(self) -> None:
        plain = settle(E, self.side, self.outcome(LOCK, 20.0), 0, N, FEE_PCT, COSTS._replace(lock_extra_bps=0.0))
        extra = settle(E, self.side, self.outcome(LOCK, 20.0), 0, N, FEE_PCT, COSTS)
        self.assertAlmostEqual(plain.net - extra.net, N * 1.0 / 10_000, delta=0.01)

    def test_only_a_lock_exit_pays_the_extra(self) -> None:
        plain = settle(E, self.side, self.outcome(SL, -100.0), 0, N, FEE_PCT, COSTS._replace(lock_extra_bps=0.0))
        extra = settle(E, self.side, self.outcome(SL, -100.0), 0, N, FEE_PCT, COSTS)
        self.assertEqual(plain, extra)

    def test_hold_minutes_count_bars_from_the_entry_bar(self) -> None:
        money = settle(E, self.side, self.outcome(SL, -100.0, idx=7), 2, N, FEE_PCT, COSTS)
        self.assertEqual(money.hold_min, 5)

    def test_gross_bps_is_of_the_entry_notional_before_the_fee(self) -> None:
        costs = Costs(exit_market_bps=0.0, tp_bps=0.0, lock_extra_bps=0.0)
        exit_price = E * (1 + self.side * 0.01)  # 100 bps in the position's favour
        money = settle(E, self.side, Outcome(TP, 4, exit_price, False, 0.0), 0, N, FEE_PCT, costs)
        self.assertAlmostEqual(money.gross_bps, 100.0, places=6)
        exit_notional = N * exit_price / E
        self.assertAlmostEqual(money.net, N * 0.01 - FEE_PCT / 100 * (N + exit_notional), places=6)

    def test_a_better_exit_price_is_a_better_net(self) -> None:
        low = settle(E, self.side, self.outcome(LOCK, 10.0), 0, N, FEE_PCT, COSTS)
        high = settle(E, self.side, self.outcome(LOCK, 30.0), 0, N, FEE_PCT, COSTS)
        self.assertGreater(high.net, low.net)

    def test_numbers_that_cannot_give_an_answer_are_refused(self) -> None:
        outcome = self.outcome(TP, LIVE_FLOOR)
        for label, args in (
            ("side", (E, 0, outcome, 0, N, FEE_PCT, COSTS)),
            ("entry", (0.0, self.side, outcome, 0, N, FEE_PCT, COSTS)),
            ("notional", (E, self.side, outcome, 0, -1.0, FEE_PCT, COSTS)),
            ("fee", (E, self.side, outcome, 0, N, -0.1, COSTS)),
            ("slippage", (E, self.side, outcome, 0, N, FEE_PCT, Costs(-1.0, 0.0, 0.0))),
            ("raw price", (E, self.side, Outcome(TP, 1, math.nan, False, 0.0), 0, N, FEE_PCT, COSTS)),
        ):
            with self.subTest(label), self.assertRaises(ValueError):
                settle(*args)


class ShortSettleTests(SettleTests, unittest.TestCase):
    side = SHORT


class LongSettleTests(SettleTests, unittest.TestCase):
    side = LONG


class StatisticsTests(unittest.TestCase):
    def test_mean_and_standard_error_of_known_values(self) -> None:
        mean, se = mean_se([1.0, 2.0, 3.0, 4.0])
        self.assertAlmostEqual(mean, 2.5)
        self.assertAlmostEqual(se, math.sqrt((5.0 / 3.0) / 4.0))

    def test_one_value_has_a_mean_but_no_standard_error(self) -> None:
        mean, se = mean_se([3.5])
        self.assertEqual(mean, 3.5)
        self.assertTrue(math.isnan(se))

    def test_no_values_have_neither(self) -> None:
        mean, se = mean_se([])
        self.assertTrue(math.isnan(mean) and math.isnan(se))

    def test_group_by_day_keeps_each_days_values_in_order(self) -> None:
        grouped = group_by_day([1.0, 2.0, 3.0, 4.0], ["a", "b", "a", "a"])
        self.assertEqual(dict(grouped), {"a": [1.0, 3.0, 4.0], "b": [2.0]})

    def test_the_bootstrap_point_is_the_mean_per_trade_not_per_day(self) -> None:
        by_day = {"a": [1.0] * 9, "b": [11.0]}
        point, low, high = day_bootstrap(by_day, draws=500)
        self.assertAlmostEqual(point, 2.0)
        self.assertLessEqual(low, point)
        self.assertGreaterEqual(high, point)

    def test_identical_trades_give_a_degenerate_interval(self) -> None:
        by_day = {f"d{i}": [1.5, 1.5] for i in range(6)}
        self.assertEqual(day_bootstrap(by_day, draws=300), (1.5, 1.5, 1.5))

    def test_the_interval_is_wide_when_days_disagree(self) -> None:
        by_day = {f"d{i}": [(-1.0) ** i * 4.0] for i in range(10)}
        point, low, high = day_bootstrap(by_day, draws=1000)
        self.assertLess(low, 0.0)
        self.assertGreater(high, 0.0)

    def test_the_bootstrap_is_deterministic_for_a_seed(self) -> None:
        by_day = {f"d{i}": [float(i), float(i) + 1.0] for i in range(8)}
        self.assertEqual(day_bootstrap(by_day, draws=400, seed=3), day_bootstrap(by_day, draws=400, seed=3))
        self.assertNotEqual(day_bootstrap(by_day, draws=400, seed=3), day_bootstrap(by_day, draws=400, seed=4))

    def test_days_without_trades_are_ignored_and_an_empty_input_has_no_interval(self) -> None:
        self.assertEqual(day_bootstrap({"a": [2.0, 2.0], "b": []}, draws=100), (2.0, 2.0, 2.0))
        point, low, high = day_bootstrap({}, draws=100)
        self.assertTrue(math.isnan(point) and math.isnan(low) and math.isnan(high))


if __name__ == "__main__":
    unittest.main()
