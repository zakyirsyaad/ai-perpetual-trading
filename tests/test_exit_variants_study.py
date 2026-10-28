"""Tests of the shadow study, ``research.exit_variants.study``.

The study turns closed-trade rows and decision rows (the shapes the Supabase store returns) plus 1m candles into
a paired comparison of pre-registered exit variants against the live exits, and evaluates the pre-registered
decision rule as text. Everything here is synthetic: bars are described in net space and rows are built by hand.
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Optional
from unittest import mock

from research.exit_variants import study, walker
from research.exit_variants.study import (
    ALL_POSITIONS,
    CALM_POSITIONS,
    CONVENTIONS,
    DECISION_CONVENTION,
    PRIMARY_VARIANT,
    VARIANTS,
    Delta,
    DecisionIndex,
    LiveRules,
    Position,
    Simulated,
    Summary,
    WeekDelta,
)
from research.exit_variants.walker import LONG, SHORT, Cols
from tests.exit_variants_helpers import (
    ENTRY,
    ENTRY_BAR_MS,
    FLAT,
    LOCK_CATCHES,
    NOTHING_HAPPENS,
    NOTIONAL,
    OPENED,
    QTY,
    RULES,
    STEP,
    UTC,
    cols_for,
    decision_row,
    position,
    trade_row,
)


class RegistryTests(unittest.TestCase):
    """The pre-registered numbers. A change here is a change of hypothesis, not a tuning: it needs a new TDD doc."""

    def test_the_variants_are_the_pre_registered_ones_in_order(self) -> None:
        described = [(v.name, v.tp_net, v.lock_steps, v.trail_arm, v.trail_dist) for v in VARIANTS]
        self.assertEqual(
            described,
            [
                ("lock 45->20", None, ((45.0, 20.0),), None, 0.0),
                ("floor +45", 45.0, (), None, 0.0),
                ("lock 40->15", None, ((40.0, 15.0),), None, 0.0),
                ("trail 40/20", None, (), 40.0, 20.0),
            ],
        )
        self.assertEqual(PRIMARY_VARIANT, "lock 45->20")

    def test_the_conventions_are_c_plus_one_bp_c_and_b(self) -> None:
        described = [(c.name, c.same_bar, c.close_check, c.lock_extra_bps) for c in CONVENTIONS]
        self.assertEqual(
            described,
            [("C+1bp", False, True, 1.0), ("C", False, True, 0.0), ("B", True, False, 0.0)],
        )
        self.assertEqual(DECISION_CONVENTION, "C+1bp")

    def test_the_decision_rule_thresholds(self) -> None:
        self.assertEqual((ALL_POSITIONS.name, ALL_POSITIONS.min_trades, ALL_POSITIONS.min_mean), ("semua", 60, 5.0))
        self.assertEqual((CALM_POSITIONS.name, CALM_POSITIONS.min_trades, CALM_POSITIONS.min_mean), ("tenang", 250, 2.0))
        self.assertEqual(
            (study.MIN_T, study.WEEKS_CONSIDERED, study.WEEKS_REQUIRED, study.MIN_CALIBRATION, study.CALM_ATR_PCT),
            (2.0, 4, 3, 0.90, 0.113),
        )

    def test_the_variants_were_registered_for_a_live_floor_of_56(self) -> None:
        self.assertEqual(study.REGISTERED_FLOOR_NET, 56.0)

    def test_the_market_costs_are_the_measured_live_ones(self) -> None:
        self.assertEqual(study.LIVE_COSTS, (0.5, 0.0, 0.0))  # market exit 0.5 bps, the target rests at its price


class LiveRulesTests(unittest.TestCase):
    def settings(self, **overrides: Any) -> SimpleNamespace:
        base = dict(
            taker_fee_pct=0.05,
            volume_neutral_exit_enabled=True,
            volume_neutral_exit_mode="floor",
            volume_neutral_exit_basis="net",
            volume_neutral_exit_min_net_usdt=56.0,
            position_hold_minutes=60,
            stop_loss_pct=0.35,
            atr_sl_multiplier=1.5,
            max_stop_loss_pct=0.40,
            use_exchange_fee_rates=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_the_live_numbers_come_from_the_settings(self) -> None:
        self.assertEqual(study.live_rules_from_settings(self.settings()), RULES)

    def test_a_bot_that_nets_the_venue_fee_is_flagged(self) -> None:
        rules = study.live_rules_from_settings(self.settings(use_exchange_fee_rates=True))
        self.assertTrue(rules.venue_fee_rates)

    def test_exits_this_study_cannot_model_are_refused_naming_every_key(self) -> None:
        with self.assertRaises(ValueError) as raised:
            study.live_rules_from_settings(
                self.settings(
                    volume_neutral_exit_enabled=False,
                    volume_neutral_exit_mode="window",
                    volume_neutral_exit_basis="gross",
                    volume_neutral_exit_min_net_usdt=0.0,
                )
            )
        message = str(raised.exception)
        self.assertEqual(len(message.splitlines()), 1)
        for key in (
            "VOLUME_NEUTRAL_EXIT_ENABLED",
            "VOLUME_NEUTRAL_EXIT_MODE",
            "VOLUME_NEUTRAL_EXIT_BASIS",
            "VOLUME_NEUTRAL_EXIT_MIN_NET_USDT",
        ):
            self.assertIn(key, message)

    def test_a_hold_limit_below_two_minutes_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            study.live_rules_from_settings(self.settings(position_hold_minutes=1))


class DeriveStopTests(unittest.TestCase):
    def test_the_model_stop_wins_when_it_is_wider_than_the_atr_stop(self) -> None:
        self.assertAlmostEqual(study.derive_stop_pct(0.30, 0.12, RULES), 0.30)

    def test_the_atr_stop_wins_when_it_is_wider(self) -> None:
        self.assertAlmostEqual(study.derive_stop_pct(0.28, 0.25, RULES), 0.375)

    def test_the_cap_limits_the_stop(self) -> None:
        self.assertAlmostEqual(study.derive_stop_pct(0.28, 0.30, RULES), 0.40)

    def test_without_a_cap_the_atr_stop_is_unbounded(self) -> None:
        uncapped = LiveRules(**{**RULES.__dict__, "max_stop_loss_pct": 0.0})
        self.assertAlmostEqual(study.derive_stop_pct(0.28, 0.30, uncapped), 0.45)

    def test_a_missing_or_zero_model_stop_falls_back_to_the_configured_one(self) -> None:
        self.assertAlmostEqual(study.derive_stop_pct(None, 0.10, RULES), 0.35)
        self.assertAlmostEqual(study.derive_stop_pct(0.0, 0.10, RULES), 0.35)


class DecisionIndexTests(unittest.TestCase):
    def nearest(self, rows: list, **overrides: Any):
        query: dict[str, Any] = dict(symbol="BTC/USDT", side=SHORT, confidence=0.78, opened_at=OPENED)
        query.update(overrides)
        return DecisionIndex(rows).nearest(**query)

    def test_the_nearest_preceding_decision_wins(self) -> None:
        rows = [
            decision_row(OPENED - timedelta(minutes=9), stop_loss_pct="0.28"),
            decision_row(OPENED - timedelta(seconds=4), stop_loss_pct="0.31"),
            decision_row(OPENED - timedelta(minutes=2), stop_loss_pct="0.29"),
        ]
        found = self.nearest(rows)
        self.assertEqual(found.created_at, OPENED - timedelta(seconds=4))
        self.assertAlmostEqual(found.stop_loss_pct, 0.31)

    def test_the_other_side_symbol_and_confidence_are_not_the_trade(self) -> None:
        for label, overrides in (
            ("action", dict(action="OPEN_LONG")),
            ("symbol", dict(symbol="ETH/USDT")),
            ("confidence", dict(confidence=0.80)),
            ("hold", dict(action="HOLD")),
        ):
            with self.subTest(label):
                self.assertIsNone(self.nearest([decision_row(**overrides)]))

    def test_confidence_is_compared_with_a_tolerance_for_float_noise(self) -> None:
        self.assertIsNotNone(self.nearest([decision_row(confidence=0.78 + 1e-9)]))

    def test_a_decision_after_the_open_is_not_the_one_that_opened_it(self) -> None:
        self.assertIsNone(self.nearest([decision_row(OPENED + timedelta(seconds=1))]))

    def test_the_window_is_fifteen_minutes_inclusive(self) -> None:
        self.assertIsNotNone(self.nearest([decision_row(OPENED - timedelta(minutes=15))]))
        self.assertIsNone(self.nearest([decision_row(OPENED - timedelta(minutes=15, seconds=1))]))

    def test_a_decision_at_the_very_second_of_the_open_counts(self) -> None:
        self.assertIsNotNone(self.nearest([decision_row(OPENED)]))

    def test_the_stop_may_be_a_number_text_or_missing(self) -> None:
        self.assertAlmostEqual(self.nearest([decision_row(stop_loss_pct=0.27)]).stop_loss_pct, 0.27)
        self.assertAlmostEqual(self.nearest([decision_row(stop_loss_pct="0.27")]).stop_loss_pct, 0.27)
        self.assertIsNone(self.nearest([decision_row(stop_loss_pct=None)]).stop_loss_pct)
        self.assertIsNone(self.nearest([decision_row(stop_loss_pct="n/a")]).stop_loss_pct)

    def test_rows_that_cannot_be_read_are_dropped(self) -> None:
        rows = [
            decision_row(created_at="not a time"),
            decision_row(confidence=None),
            decision_row(confidence="high"),
            decision_row(stop_loss_pct="0.33"),
        ]
        self.assertAlmostEqual(self.nearest(rows).stop_loss_pct, 0.33)

    def test_supabase_timestamps_with_five_digit_fractions_are_read(self) -> None:
        row = decision_row(created_at="2026-10-26T00:15:08.97456+00:00")
        self.assertIsNotNone(self.nearest([row]))

    def test_an_empty_index_finds_nothing(self) -> None:
        self.assertIsNone(self.nearest([]))


class EntryIndexTests(unittest.TestCase):
    def cols(self, start_ms: int, count: int = 5) -> Cols:
        return Cols([start_ms + i * STEP for i in range(count)], [1.0] * count, [1.0] * count, [1.0] * count)

    def test_the_entry_bar_is_the_first_full_bar_after_the_entry_minute(self) -> None:
        cols = self.cols(ENTRY_BAR_MS - 2 * STEP)
        self.assertEqual(study.entry_index(cols, OPENED), 2)

    def test_an_entry_exactly_on_a_minute_still_waits_for_the_next_bar(self) -> None:
        cols = self.cols(ENTRY_BAR_MS - 2 * STEP)
        self.assertEqual(study.entry_index(cols, datetime(2026, 10, 26, 0, 15, 0, tzinfo=UTC)), 2)

    def test_a_cache_without_that_bar_gives_none(self) -> None:
        self.assertIsNone(study.entry_index(self.cols(ENTRY_BAR_MS + STEP), OPENED))  # starts a minute late
        self.assertIsNone(study.entry_index(self.cols(ENTRY_BAR_MS - 9 * STEP, count=3), OPENED))  # ends early
        self.assertIsNone(study.entry_index(Cols([], [], [], []), OPENED))

    def test_a_missing_bar_inside_the_cache_gives_none(self) -> None:
        cols = Cols([ENTRY_BAR_MS - STEP, ENTRY_BAR_MS + STEP], [1.0, 1.0], [1.0, 1.0], [1.0, 1.0])
        self.assertIsNone(study.entry_index(cols, OPENED))


class LoadPositionsTests(unittest.TestCase):
    def load(self, trades: list, decisions: Optional[list] = None):
        index = DecisionIndex([decision_row()] if decisions is None else decisions)
        return study.load_positions(trades, index, RULES)

    def test_a_trade_becomes_a_position_with_the_derived_stop(self) -> None:
        positions, skipped = self.load([trade_row()])
        self.assertEqual(skipped, [])
        (pos,) = positions
        self.assertEqual((pos.side, pos.entry_fill, pos.actual_exit), (SHORT, ENTRY, "WAKTU"))
        self.assertAlmostEqual(pos.notional, NOTIONAL)
        self.assertAlmostEqual(pos.sl_pct, 0.30)  # max(model 0.30, 1.5 x 0.12) capped at 0.40
        self.assertAlmostEqual(pos.atr_pct, 0.12)
        self.assertEqual(pos.opened_at, OPENED)
        self.assertAlmostEqual(pos.volume_usdt, (ENTRY + 99_950.0) * QTY)

    def test_a_long_trade_is_matched_to_an_open_long_decision(self) -> None:
        decisions = [decision_row(action="OPEN_LONG", stop_loss_pct="0.29")]
        positions, skipped = self.load([trade_row(direction="LONG")], decisions)
        self.assertEqual(skipped, [])
        self.assertEqual(positions[0].side, LONG)
        self.assertAlmostEqual(positions[0].sl_pct, 0.29)  # the model's 0.29 is wider than 1.5 x 0.12

    def test_the_atr_stop_is_used_when_it_is_wider(self) -> None:
        positions, _ = self.load([trade_row(factor_metadata={"setup": {"atr_pct": 0.22}})])
        self.assertAlmostEqual(positions[0].sl_pct, 0.33)

    def test_actual_exit_reasons_map_to_the_walkers(self) -> None:
        for reason, expected in (
            ("neutral_exit", "TP"),
            ("take_profit", "TP"),
            ("stop_loss", "SL"),
            ("time_stop", "WAKTU"),
            ("strategy_exit:model said close", ""),
            ("liquidation_guard", ""),
            ("", ""),
        ):
            with self.subTest(reason=reason):
                positions, _ = self.load([trade_row(reason=reason)])
                self.assertEqual(positions[0].actual_exit, expected)

    def test_positions_come_back_in_the_order_they_opened(self) -> None:
        later = OPENED + timedelta(hours=3)
        trades = [
            trade_row(opened_at=later.isoformat(), closed_at=(later + timedelta(minutes=60)).isoformat()),
            trade_row(),
        ]
        decisions = [decision_row(), decision_row(later - timedelta(seconds=4))]
        positions, _ = self.load(trades, decisions)
        self.assertEqual([p.opened_at for p in positions], [OPENED, later])

    def test_each_reason_to_skip_is_listed_with_its_code(self) -> None:
        cases = (
            ("direction", trade_row(direction="FLAT"), study.SKIP_UNREADABLE),
            ("opened", trade_row(opened_at="soon"), study.SKIP_UNREADABLE),
            ("closed", trade_row(closed_at=None), study.SKIP_UNREADABLE),
            ("entry", trade_row(entry_price=0.0), study.SKIP_UNREADABLE),
            ("exit", trade_row(exit_price=float("nan")), study.SKIP_UNREADABLE),
            ("quantity", trade_row(quantity=-1.0), study.SKIP_UNREADABLE),
            ("symbol", trade_row(symbol="ETH/USDT"), study.SKIP_SYMBOL),
            ("no atr", trade_row(factor_metadata={}), study.SKIP_ATR),
            ("zero atr", trade_row(factor_metadata={"setup": {"atr_pct": 0.0}}), study.SKIP_ATR),
            ("text atr", trade_row(factor_metadata={"setup": {"atr_pct": "wide"}}), study.SKIP_ATR),
            ("no setup", trade_row(factor_metadata=None), study.SKIP_ATR),
            ("no decision", trade_row(confidence=0.5), study.SKIP_DECISION),
            ("no confidence", trade_row(confidence=None), study.SKIP_DECISION),
        )
        for label, row, code in cases:
            with self.subTest(label):
                positions, skipped = self.load([row])
                self.assertEqual(positions, [])
                self.assertEqual([s.code for s in skipped], [code])

    def test_a_skip_names_the_trade_by_its_open_time_and_side(self) -> None:
        _, skipped = self.load([trade_row(symbol="ETH/USDT")])
        self.assertEqual((skipped[0].opened_at, skipped[0].side), (OPENED, "SHORT"))
        _, skipped = self.load([trade_row(opened_at="soon")])
        self.assertEqual((skipped[0].opened_at, skipped[0].side), (None, "SHORT"))
        _, skipped = self.load([trade_row(direction="FLAT")])
        self.assertEqual(skipped[0].side, "?")


class SimulateTests(unittest.TestCase):
    """The lock path: +47 net then a retrace to +18, then +5 until the time exit (the live exit books about +3.75)."""

    def simulate(self, path: list, pos: Optional[Position] = None):
        return study.simulate([pos or position()], cols_for(SHORT, path), RULES)

    def delta(self, sim: Simulated, variant: str, convention: str) -> Delta:
        return sim.deltas[(variant, convention)]

    def test_the_baseline_is_the_live_walk_settled_with_the_market_costs(self) -> None:
        (sim,), skipped = self.simulate(LOCK_CATCHES)
        self.assertEqual(skipped, [])
        self.assertEqual(sim.baseline_reason, "WAKTU")
        self.assertAlmostEqual(sim.baseline_net, 5.0 - 1.25, delta=0.02)  # +5 net at the open, 0.5 bp slippage

    def test_every_variant_and_convention_has_a_paired_delta(self) -> None:
        (sim,), _ = self.simulate(LOCK_CATCHES)
        self.assertEqual(
            sorted(sim.deltas),
            sorted((v.name, c.name) for v in VARIANTS for c in CONVENTIONS),
        )

    def test_the_primary_lock_beats_the_time_exit_and_pays_for_the_extra_slippage(self) -> None:
        (sim,), _ = self.simulate(LOCK_CATCHES)
        plain = self.delta(sim, "lock 45->20", "C")
        costly = self.delta(sim, "lock 45->20", "C+1bp")
        self.assertAlmostEqual(plain.usdt, (20.0 - 1.25) - (5.0 - 1.25), delta=0.05)
        self.assertAlmostEqual(plain.usdt - costly.usdt, 2.5, delta=0.02)  # 1 bp of the 25,000 notional
        self.assertTrue(plain.changed and costly.changed)
        self.assertAlmostEqual(self.delta(sim, "lock 45->20", "B").usdt, plain.usdt, delta=0.05)

    def test_a_lower_floor_takes_the_target_at_the_peak(self) -> None:
        (sim,), _ = self.simulate(LOCK_CATCHES)
        for convention in ("C+1bp", "C", "B"):
            self.assertAlmostEqual(self.delta(sim, "floor +45", convention).usdt, 45.0 - (5.0 - 1.25), delta=0.05)

    def test_the_trailing_stop_follows_the_peak(self) -> None:
        (sim,), _ = self.simulate(LOCK_CATCHES)
        self.assertAlmostEqual(self.delta(sim, "trail 40/20", "C").usdt, (27.0 - 1.25) - (5.0 - 1.25), delta=0.05)
        self.assertAlmostEqual(self.delta(sim, "trail 40/20", "C+1bp").usdt, (27.0 - 3.75) - (5.0 - 1.25), delta=0.05)

    def test_a_lock_that_exits_at_the_same_price_as_the_time_exit_still_counts_as_a_change(self) -> None:
        # lock 40->15 is gapped through at +5 on bar 3, the price the time exit would also have booked
        (sim,), _ = self.simulate(LOCK_CATCHES)
        free, costly = self.delta(sim, "lock 40->15", "C"), self.delta(sim, "lock 40->15", "C+1bp")
        self.assertAlmostEqual(free.usdt, 0.0, delta=0.05)
        self.assertTrue(free.changed)
        self.assertAlmostEqual(costly.usdt, -2.5, delta=0.05)

    def test_the_bad_case_bound_stops_a_lock_inside_the_bar_that_armed_it(self) -> None:
        (sim,), _ = self.simulate(LOCK_CATCHES)
        self.assertAlmostEqual(self.delta(sim, "lock 40->15", "B").usdt, (15.0 - 1.25) - (5.0 - 1.25), delta=0.05)

    def test_a_trade_that_never_arms_anything_is_unchanged_everywhere(self) -> None:
        (sim,), _ = self.simulate(NOTHING_HAPPENS)
        for key, delta in sim.deltas.items():
            with self.subTest(key=key):
                self.assertEqual((delta.usdt, delta.changed), (0.0, False))

    def test_a_long_position_is_walked_with_its_own_sign(self) -> None:
        pos = position(side=LONG)
        sims, skipped = study.simulate([pos], cols_for(LONG, LOCK_CATCHES), RULES)
        self.assertEqual(skipped, [])
        self.assertAlmostEqual(sims[0].deltas[("lock 45->20", "C")].usdt, 15.0, delta=0.05)

    def test_a_trade_without_its_candles_is_skipped_and_listed(self) -> None:
        late = cols_for(SHORT, LOCK_CATCHES, start_ms=ENTRY_BAR_MS + 600 * STEP)
        sims, skipped = study.simulate([position()], late, RULES)
        self.assertEqual(sims, [])
        self.assertEqual([(s.code, s.opened_at, s.side) for s in skipped], [(study.SKIP_CANDLES, OPENED, "SHORT")])

    def test_a_cache_that_ends_before_the_exit_skips_the_trade(self) -> None:
        sims, skipped = self.simulate([FLAT] + [(2.0, 3.0, 1.0)] * 20)
        self.assertEqual(sims, [])
        self.assertEqual([s.code for s in skipped], [study.SKIP_CANDLES])

    def test_a_variant_that_runs_out_of_bars_where_the_live_walk_did_not_is_a_bug_not_a_skip(self) -> None:
        # a variant only adds exits, so it can never need more bars than the live walk; if it does, say so loudly
        calls = []

        def only_the_live_walk_succeeds(*args):
            calls.append(args)
            return walker.walk(*args) if len(calls) == 1 else None

        with mock.patch.object(study, "walk", only_the_live_walk_succeeds), self.assertRaises(RuntimeError) as raised:
            study.simulate([position()], cols_for(SHORT, NOTHING_HAPPENS), RULES)
        self.assertIn("lock 45->20 [C+1bp]", str(raised.exception))

    def test_a_minute_missing_from_the_cache_skips_the_trade(self) -> None:
        cols = cols_for(SHORT, NOTHING_HAPPENS)
        holed = Cols(
            [ms + (STEP if i >= 10 else 0) for i, ms in enumerate(cols.open_ms)], cols.open, cols.high, cols.low
        )
        sims, skipped = study.simulate([position()], holed, RULES)
        self.assertEqual(sims, [])
        self.assertEqual([s.code for s in skipped], [study.SKIP_CANDLES])


class CalibrationTests(unittest.TestCase):
    def sim(self, baseline: str, actual: str) -> Simulated:
        return Simulated(position(actual_exit=actual), baseline, 0.0, {})

    def test_it_counts_the_trades_whose_simulated_exit_reason_is_the_real_one(self) -> None:
        sims = [self.sim("WAKTU", "WAKTU"), self.sim("TP", "TP"), self.sim("SL", "WAKTU"), self.sim("TP", "")]
        result = study.calibration(sims)
        self.assertEqual((result.matches, result.total), (2, 4))
        self.assertAlmostEqual(result.ratio, 0.5)

    def test_no_trades_have_no_ratio(self) -> None:
        result = study.calibration([])
        self.assertEqual((result.matches, result.total), (0, 0))
        self.assertIsNone(result.ratio)


def sim_at(
    closed: datetime,
    delta: float,
    atr: float = 0.10,
    volume: float = 50_000.0,
    changed: bool = True,
    key: tuple[str, str] = (PRIMARY_VARIANT, DECISION_CONVENTION),
) -> Simulated:
    pos = position(closed_at=closed, opened_at=closed - timedelta(minutes=60), atr_pct=atr, volume_usdt=volume)
    return Simulated(pos, "WAKTU", 0.0, {key: Delta(delta, changed)})


MONDAY = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)  # ISO week 2026-W44 (Monday 19:00 WIB)


class SummarizeTests(unittest.TestCase):
    def summarize(self, sims: list, variant: str = PRIMARY_VARIANT, convention: str = DECISION_CONVENTION) -> Summary:
        return study.summarize(sims, variant, convention)

    def test_mean_standard_error_t_and_per_million(self) -> None:
        sims = [sim_at(MONDAY + timedelta(hours=h), d) for h, d in enumerate([1.0, 2.0, 3.0, 4.0])]
        result = self.summarize(sims)
        self.assertEqual(result.n, 4)
        self.assertAlmostEqual(result.mean, 2.5)
        self.assertAlmostEqual(result.se, math.sqrt((5.0 / 3.0) / 4.0))
        self.assertAlmostEqual(result.t, 2.5 / result.se)
        self.assertAlmostEqual(result.per_million, 1e6 * 10.0 / 200_000.0)

    def test_it_counts_the_trades_the_variant_changed(self) -> None:
        sims = [sim_at(MONDAY + timedelta(hours=h), 0.0, changed=bool(h % 2)) for h in range(5)]
        self.assertEqual(self.summarize(sims).changed, 2)

    def test_one_trade_has_a_mean_but_no_error_and_no_t(self) -> None:
        result = self.summarize([sim_at(MONDAY, 3.0)])
        self.assertEqual((result.n, result.mean), (1, 3.0))
        self.assertTrue(math.isnan(result.se))
        self.assertIsNone(result.t)

    def test_identical_deltas_have_no_t(self) -> None:
        result = self.summarize([sim_at(MONDAY + timedelta(hours=h), 2.0) for h in range(4)])
        self.assertEqual(result.se, 0.0)
        self.assertIsNone(result.t)

    def test_no_trades_have_nothing_to_report(self) -> None:
        result = self.summarize([])
        self.assertEqual((result.n, result.changed, result.weeks), (0, 0, ()))
        self.assertTrue(math.isnan(result.mean))
        self.assertIsNone(result.t)
        self.assertIsNone(result.per_million)
        self.assertIsNone(result.interval)

    def test_zero_volume_has_no_per_million(self) -> None:
        self.assertIsNone(self.summarize([sim_at(MONDAY, 1.0, volume=0.0)]).per_million)

    def test_weeks_are_wib_iso_weeks_in_order_with_their_own_mean(self) -> None:
        sims = [
            sim_at(datetime(2026, 11, 2, 3, 0, tzinfo=UTC), 6.0),  # Monday 10:00 WIB, W45
            sim_at(MONDAY, 2.0),  # W44
            sim_at(MONDAY + timedelta(days=1), 4.0),  # W44
        ]
        self.assertEqual(
            self.summarize(sims).weeks,
            (WeekDelta("2026-W44", 2, 3.0), WeekDelta("2026-W45", 1, 6.0)),
        )

    def test_the_week_turns_at_midnight_wib_not_utc(self) -> None:
        sims = [
            sim_at(datetime(2026, 11, 1, 16, 30, tzinfo=UTC), 1.0),  # Sunday 23:30 WIB
            sim_at(datetime(2026, 11, 1, 17, 30, tzinfo=UTC), 2.0),  # Monday 00:30 WIB
        ]
        self.assertEqual([w.week for w in self.summarize(sims).weeks], ["2026-W44", "2026-W45"])

    def test_the_iso_year_is_the_weeks_own(self) -> None:
        self.assertEqual(study.iso_week(datetime(2027, 1, 3, 10, 0, tzinfo=UTC)), "2026-W53")

    def test_the_day_bootstrap_needs_enough_days(self) -> None:
        few = [sim_at(MONDAY + timedelta(days=d), 1.0 + d) for d in range(study.MIN_BOOTSTRAP_DAYS - 1)]
        self.assertIsNone(self.summarize(few).interval)
        enough = [sim_at(MONDAY + timedelta(days=d), 1.0 + d) for d in range(study.MIN_BOOTSTRAP_DAYS)]
        result = self.summarize(enough)
        self.assertIsNotNone(result.interval)
        low, high = result.interval
        self.assertLessEqual(low, result.mean)
        self.assertGreaterEqual(high, result.mean)

    def test_the_variant_and_the_convention_choose_the_column(self) -> None:
        sims = [
            Simulated(
                position(closed_at=MONDAY),
                "WAKTU",
                0.0,
                {("lock 45->20", "C"): Delta(1.0, True), ("floor +45", "C"): Delta(7.0, False)},
            )
        ]
        self.assertEqual(self.summarize(sims, "floor +45", "C").mean, 7.0)
        self.assertEqual(self.summarize(sims, "lock 45->20", "C").mean, 1.0)

    def test_the_calm_subset_keeps_trades_below_the_atr_threshold(self) -> None:
        sims = [sim_at(MONDAY, 1.0, atr=0.112), sim_at(MONDAY, 2.0, atr=0.113), sim_at(MONDAY, 3.0, atr=0.30)]
        self.assertEqual([s.position.atr_pct for s in study.calm_subset(sims)], [0.112])


def week(label: str, mean: float, n: int = 10) -> WeekDelta:
    return WeekDelta(label, n, mean)


def passing_summary(**overrides: Any) -> Summary:
    fields: dict[str, Any] = dict(
        n=100,
        mean=6.0,
        se=2.0,
        t=3.0,
        per_million=120.0,
        changed=40,
        interval=None,
        weeks=(
            week("2026-W41", 1.0),
            week("2026-W42", 5.0),
            week("2026-W43", -1.0),
            week("2026-W44", 4.0),
            week("2026-W45", 9.0),
        ),
    )
    fields.update(overrides)
    return Summary(**fields)


class EvaluateRuleTests(unittest.TestCase):
    def met(self, verdict) -> dict[str, bool]:
        return {c.name: c.met for c in verdict.criteria}

    def test_a_candidate_that_meets_every_criterion_is_viable(self) -> None:
        verdict = study.evaluate_rule(ALL_POSITIONS, passing_summary(), 0.95)
        self.assertTrue(verdict.passed)
        self.assertEqual(list(self.met(verdict)), ["n", "mean", "t", "weeks", "calibration"])
        self.assertTrue(all(self.met(verdict).values()))
        self.assertEqual(verdict.candidate, "semua")

    def test_each_criterion_can_fail_on_its_own(self) -> None:
        cases = (
            ("n", passing_summary(n=59), 0.95),
            ("mean", passing_summary(mean=4.99), 0.95),
            ("t", passing_summary(t=1.99), 0.95),
            ("t", passing_summary(t=None), 0.95),
            ("weeks", passing_summary(weeks=passing_summary().weeks[:3]), 0.95),
            ("calibration", passing_summary(), 0.899),
            ("calibration", passing_summary(), None),
        )
        for name, summary, calibration in cases:
            with self.subTest(name=name, n=summary.n, mean=summary.mean, t=summary.t, calibration=calibration):
                verdict = study.evaluate_rule(ALL_POSITIONS, summary, calibration)
                self.assertFalse(verdict.passed)
                self.assertFalse(self.met(verdict)[name])
                self.assertEqual(sum(1 for ok in self.met(verdict).values() if not ok), 1)

    def test_the_boundaries_are_inclusive(self) -> None:
        summary = passing_summary(n=60, mean=5.0, t=2.0)
        self.assertTrue(study.evaluate_rule(ALL_POSITIONS, summary, 0.90).passed)

    def test_three_of_the_last_four_weeks_must_share_the_positive_sign(self) -> None:
        # the last four are W42..W45: +5, -1, +4, +9 -> 3 positive
        verdict = study.evaluate_rule(ALL_POSITIONS, passing_summary(), 0.95)
        weeks = next(c for c in verdict.criteria if c.name == "weeks")
        self.assertEqual((weeks.value, weeks.of, weeks.needed), (3, 4, 3))
        two = passing_summary(weeks=(week("W1", 1.0), week("W2", 1.0), week("W3", -1.0), week("W4", -1.0)))
        self.assertFalse(self.met(study.evaluate_rule(ALL_POSITIONS, two, 0.95))["weeks"])

    def test_a_week_with_no_effect_is_not_a_positive_week(self) -> None:
        zero = passing_summary(weeks=(week("W1", 1.0), week("W2", 1.0), week("W3", 0.0), week("W4", 0.0)))
        self.assertFalse(self.met(study.evaluate_rule(ALL_POSITIONS, zero, 0.95))["weeks"])

    def test_fewer_than_four_weeks_cannot_show_consistency(self) -> None:
        short = passing_summary(weeks=(week("W1", 1.0), week("W2", 1.0), week("W3", 1.0)))
        verdict = study.evaluate_rule(ALL_POSITIONS, short, 0.95)
        self.assertFalse(self.met(verdict)["weeks"])
        self.assertEqual(next(c for c in verdict.criteria if c.name == "weeks").of, 3)

    def test_the_calm_candidate_needs_more_trades_and_a_smaller_mean(self) -> None:
        summary = passing_summary(n=249, mean=2.0)
        verdict = study.evaluate_rule(CALM_POSITIONS, summary, 0.95)
        self.assertEqual(verdict.candidate, "tenang")
        self.assertEqual(
            {name: ok for name, ok in self.met(verdict).items() if not ok},
            {"n": False},
        )
        self.assertTrue(study.evaluate_rule(CALM_POSITIONS, passing_summary(n=250, mean=2.0), 0.95).passed)
        self.assertFalse(self.met(study.evaluate_rule(CALM_POSITIONS, passing_summary(n=250, mean=1.99), 0.95))["mean"])

    def test_an_empty_summary_meets_nothing(self) -> None:
        empty = study.summarize([], PRIMARY_VARIANT, DECISION_CONVENTION)
        verdict = study.evaluate_rule(ALL_POSITIONS, empty, None)
        self.assertFalse(verdict.passed)
        self.assertFalse(any(c.met for c in verdict.criteria))


class RunStudyTests(unittest.TestCase):
    def test_rows_in_a_study_out(self) -> None:
        trades = [trade_row(), trade_row(symbol="ETH/USDT")]
        result = study.run_study(trades, [decision_row()], cols_for(SHORT, LOCK_CATCHES), RULES)
        self.assertEqual(result.rows_read, 2)
        self.assertEqual(len(result.simulated), 1)
        self.assertEqual([s.code for s in result.skipped], [study.SKIP_SYMBOL])
        self.assertAlmostEqual(
            result.simulated[0].deltas[(PRIMARY_VARIANT, DECISION_CONVENTION)].usdt, 12.5, delta=0.05
        )
        self.assertEqual(study.calibration(result.simulated).ratio, 1.0)  # time_stop, simulated WAKTU

    def test_skips_from_every_stage_are_collected_in_one_list(self) -> None:
        trades = [trade_row(), trade_row(confidence=0.5)]
        late = cols_for(SHORT, LOCK_CATCHES, ENTRY_BAR_MS + 900 * STEP)
        result = study.run_study(trades, [decision_row()], late, RULES)
        self.assertEqual(sorted(s.code for s in result.skipped), sorted([study.SKIP_DECISION, study.SKIP_CANDLES]))
        self.assertEqual(result.simulated, ())

    def test_no_trades_is_a_valid_empty_study(self) -> None:
        result = study.run_study([], [], cols_for(SHORT, NOTHING_HAPPENS), RULES)
        self.assertEqual((result.rows_read, result.simulated, result.skipped), (0, (), ()))


if __name__ == "__main__":
    unittest.main()
