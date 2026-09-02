from __future__ import annotations

import logging
import unittest
from datetime import date
from types import SimpleNamespace

from bot.ai.brain import EntryDecision, RiskReview
from bot.ai.compact_context import CompactDecisionContextBuilder
from bot.ai.orchestration import AIOrchestrator, EntryCandidate, ResolvedTradeTargets
from bot.models import PositionDirection
from bot.risk.risk_manager import DailyRiskState


def _approved_review() -> RiskReview:
    return RiskReview(
        approved=True,
        confirmations="live confirmations present",
        invalidation_trigger="trend structure breaks",
    )


def _volume_run_settings() -> SimpleNamespace:
    """Settings mirroring the live volume-building run where trade-suppressing
    gates are removed (confidence/systemic/RR floors no longer veto)."""
    return SimpleNamespace(
        strategy_mode="ai_volume",
        symbols=["BTC/USDT", "ETH/USDT"],
        stop_loss_pct=0.08,
        take_profit_pct=0.12,
        atr_sl_multiplier=1.5,
        atr_tp_multiplier=3.0,
        taker_fee_pct=0.05,
        maker_fee_pct=-0.02,
        leverage=100,
        ai_min_fee_adjusted_rr=0.1,
        ai_min_confidence=0.75,
        ai_compact_max_factors_per_symbol=6,
        ai_compact_max_flow_signals=5,
        ai_compact_max_memory_lessons=3,
    )


class EntryGateRemovalTests(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self) -> AIOrchestrator:
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = _volume_run_settings()
        orchestrator._logger = logging.getLogger("test.entry_gate_removal")
        return orchestrator

    def test_low_confidence_decision_is_rejected_even_for_ai_volume(self) -> None:
        orchestrator = self._orchestrator()
        decision = EntryDecision(
            symbol="BTC/USDT",
            action="OPEN_LONG",
            confidence=0.10,
            market_trend="Sideways",
            stop_loss_pct=0.08,
            take_profit_pct=0.12,
            risk_reward_ratio=0.5,
            systemic_risk=5,
            reason="weak/choppy setup that the old guards would reject",
            risk_review=_approved_review(),
        )

        self.assertIsNone(orchestrator._build_entry_candidate(decision, {"technicals": []}))

    def test_hold_decision_is_skipped(self) -> None:
        # HOLD is no longer offered to the model, but if one slips through it must be
        # skipped rather than crash.
        orchestrator = self._orchestrator()
        decision = EntryDecision(
            symbol="BTC/USDT",
            action="HOLD",
            confidence=0.9,
            market_trend="Sideways",
            systemic_risk=1,
            reason="model returned hold despite the volume mandate",
        )

        self.assertIsNone(orchestrator._build_entry_candidate(decision, {"technicals": []}))


class AIOrchestrationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_payload_uses_configured_history_limits(self) -> None:
        class FakeTradeStore:
            def __init__(self) -> None:
                self.performance_limit: int | None = None
                self.memory_limit: int | None = None

            async def recent_performance_summary(self, limit: int = 50) -> dict:
                self.performance_limit = limit
                return {"sample_size": limit}

            async def recent_closed_trade_records(self, limit: int = 100) -> list[dict]:
                self.memory_limit = limit
                return []

        class FakeBrain:
            async def entry_decisions(self, payload: dict) -> None:
                return None

        class FakeIntelligence:
            async def build_context(self) -> SimpleNamespace:
                return SimpleNamespace(
                    to_dict=lambda: {
                        "technicals": [],
                        "external_signals": {},
                        "intelligence": {},
                        "factor_snapshots": [],
                        "active_flow_signals": [],
                    }
                )

        store = FakeTradeStore()
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = SimpleNamespace(
            strategy_mode="grok",
            okx_instrument_type="SWAP",
            okx_margin_mode="isolated",
            symbols=["BTC/USDT", "ETH/USDT"],
            trade_target_per_day=28,
            max_stop_loss_per_day=2,
            max_consecutive_losses=3,
            max_daily_drawdown_usdt=100.0,
            leverage=100,
            position_usdt=180.0,
            ai_min_fee_adjusted_rr=0.1,
            taker_fee_pct=0.05,
            ai_recent_performance_limit=12,
            ai_trade_memory_limit=24,
            maker_fee_pct=-0.02,
            exchange_name="bingx",
            margin_mode="isolated",
            position_mode="hedge",
            ai_compact_max_factors_per_symbol=6,
            ai_compact_max_flow_signals=5,
            ai_compact_max_memory_lessons=3,
        )
        orchestrator._brain = FakeBrain()
        orchestrator._trade_store = store
        orchestrator._intelligence = FakeIntelligence()
        orchestrator._market_analyzer = SimpleNamespace(review=lambda context: SimpleNamespace(to_dict=lambda: {}))
        orchestrator._signal_designer = SimpleNamespace(review=lambda context: SimpleNamespace(to_dict=lambda: {}))
        orchestrator._compact_context = CompactDecisionContextBuilder(orchestrator._settings)

        plan = await orchestrator.next_trade_plan(
            DailyRiskState(
                day=date(2026, 6, 27),
                stop_losses=0,
                closed_trades=0,
                total_pnl_usdt=0.0,
                consecutive_losses=0,
                allowed_to_trade=True,
            )
        )

        self.assertIsNone(plan)
        self.assertEqual(store.performance_limit, 12)
        self.assertEqual(store.memory_limit, 24)

    async def test_candidate_to_trade_plan_uses_candidate_direction(self) -> None:
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = SimpleNamespace(
            strategy_mode="grok",
            account_balance_usdt=60.0,
            max_risk_pct_per_trade=0.5,
            leverage=3,
            position_usdt=20.0,
            taker_fee_pct=0.05,
        )

        candidate = EntryCandidate(
            decision=EntryDecision(
                symbol="ETH/USDT",
                action="OPEN_SHORT",
                confidence=0.81,
                market_trend="Bearish",
                stop_loss_pct=0.5,
                take_profit_pct=1.0,
                risk_reward_ratio=2.0,
                systemic_risk=2,
                reason="bearish continuation setup",
                risk_review=_approved_review(),
            ),
            direction=PositionDirection.SHORT,
            target=ResolvedTradeTargets(
                stop_loss_pct=0.5,
                take_profit_pct=1.0,
                atr_pct=0.1,
                fee_adjusted_rr=1.5,
                round_trip_fee_pct=0.1,
            ),
            score=2.0,
        )

        plan = await orchestrator._candidate_to_trade_plan(candidate, {"technicals": []})

        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, PositionDirection.SHORT)
        self.assertEqual(plan.symbol, "ETH/USDT")
        self.assertEqual(plan.stop_loss_pct, 0.5)
        self.assertEqual(plan.take_profit_pct, 1.0)

    async def test_ai_volume_plan_uses_volume_margin_size(self) -> None:
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = SimpleNamespace(
            strategy_mode="ai_volume",
            account_balance_usdt=1500.0,
            max_risk_pct_per_trade=0.5,
            leverage=100,
            position_usdt=20.0,
            taker_fee_pct=0.05,
            volume_profile=SimpleNamespace(
                margin_per_entry_usdt=180.0,
                volume_per_entry_usdt=36_000.0,
            ),
        )

        candidate = EntryCandidate(
            decision=EntryDecision(
                symbol="BTC/USDT",
                action="OPEN_LONG",
                confidence=0.84,
                market_trend="Bullish",
                stop_loss_pct=0.5,
                take_profit_pct=1.2,
                risk_reward_ratio=2.4,
                systemic_risk=2,
                reason="bullish continuation with clean liquidity",
                risk_review=_approved_review(),
            ),
            direction=PositionDirection.LONG,
            target=ResolvedTradeTargets(
                stop_loss_pct=0.5,
                take_profit_pct=1.2,
                atr_pct=0.1,
                fee_adjusted_rr=1.8,
                round_trip_fee_pct=0.1,
            ),
            score=2.0,
        )

        plan = await orchestrator._candidate_to_trade_plan(candidate, {"technicals": []})

        self.assertIsNotNone(plan)
        self.assertEqual(plan.usdt_size, 180.0)
        self.assertEqual(plan.leverage, 100)
        self.assertIn("ai_volume:OPEN_LONG", plan.decision_reason)
        self.assertIn("entry_volume=36000.00", plan.decision_reason)


if __name__ == "__main__":
    unittest.main()


class MergedRiskReviewGateTests(unittest.TestCase):
    def _orchestrator(self) -> AIOrchestrator:
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = _volume_run_settings()
        orchestrator._logger = logging.getLogger("test.risk_review")
        return orchestrator

    def _decision(self, **overrides) -> EntryDecision:
        base = dict(
            symbol="BTC/USDT",
            action="OPEN_LONG",
            confidence=0.8,
            market_trend="Bullish",
            stop_loss_pct=0.08,
            take_profit_pct=0.12,
            systemic_risk=2,
            reason="clean breakout",
            risk_review=_approved_review(),
        )
        base.update(overrides)
        return EntryDecision(**base)

    def test_approved_review_allows_candidate(self) -> None:
        candidate = self._orchestrator()._build_entry_candidate(
            self._decision(),
            {
                "technicals": [],
                "liquidation_constraints": {
                    "BTC/USDT": {"status": "available", "max_long_stop_loss_pct": 0.5}
                },
            },
        )
        self.assertIsNotNone(candidate)

    def test_stop_at_or_beyond_estimated_liquidation_is_rejected(self) -> None:
        decision = self._decision(stop_loss_pct=0.5)
        context = {
            "technicals": [],
            "liquidation_constraints": {
                "BTC/USDT": {"status": "available", "max_long_stop_loss_pct": 0.5}
            },
        }
        self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, context))

    def test_non_finite_stop_or_liquidation_limit_is_rejected(self) -> None:
        for stop_loss_pct, maximum in ((float("nan"), 0.5), (float("inf"), 0.5), (0.1, float("nan")), (0.1, float("inf"))):
            with self.subTest(stop_loss_pct=stop_loss_pct, maximum=maximum):
                decision = self._decision()
                decision.stop_loss_pct = stop_loss_pct
                context = {
                    "technicals": [],
                    "liquidation_constraints": {
                        "BTC/USDT": {"status": "available", "max_long_stop_loss_pct": maximum}
                    },
                }
                self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, context))

    def test_vetoed_review_blocks_candidate(self) -> None:
        decision = self._decision(
            risk_review=RiskReview(
                approved=False,
                confirmations="thin confirmations",
                invalidation_trigger="no real level to lean on",
            )
        )
        self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, {"technicals": []}))

    def test_missing_review_blocks_candidate_instead_of_failing_open(self) -> None:
        decision = self._decision(risk_review=None)
        self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, {"technicals": []}))

    def test_structurally_invalid_review_blocks_candidate(self) -> None:
        decision = EntryDecision.model_validate(
            {
                "symbol": "BTC/USDT",
                "action": "OPEN_SHORT",
                "confidence": 0.7,
                "market_trend": "Bearish",
                "stop_loss_pct": 0.08,
                "take_profit_pct": 0.12,
                "systemic_risk": 2,
                "reason": "breakdown",
                "risk_review": {"approved": True},
            }
        )
        self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, {"technicals": []}))


class StopLossCeilingTests(unittest.TestCase):
    """MAX_STOP_LOSS_PCT restores the hard loss bound that the removed
    liquidation_guard exit used to provide implicitly.

    Without a ceiling `sl_pct = max(base_sl, atr_pct * ATR_SL_MULTIPLIER)` grows
    without limit, so a volatile entry opens with a stop far wider than the
    profit-taking band and a single adverse move costs several winners.
    """

    def _orchestrator(self, **overrides) -> AIOrchestrator:
        settings = _volume_run_settings()
        for key, value in overrides.items():
            setattr(settings, key, value)
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = settings
        orchestrator._logger = logging.getLogger("test.stop_loss_ceiling")
        return orchestrator

    def _decision(self, **overrides) -> EntryDecision:
        base = dict(
            symbol="BTC/USDT",
            action="OPEN_LONG",
            confidence=0.9,
            market_trend="Bullish",
            stop_loss_pct=0.08,
            take_profit_pct=0.12,
            risk_reward_ratio=2.0,
            systemic_risk=1,
            reason="ceiling test",
            risk_review=_approved_review(),
        )
        base.update(overrides)
        return EntryDecision(**base)

    def _context(self, atr_14: float, *, max_stop: float = 5.0) -> dict:
        return {
            "technicals": [{"symbol": "BTC/USDT", "atr_14": atr_14, "last_price": 100.0}],
            "liquidation_constraints": {
                "BTC/USDT": {"status": "available", "max_long_stop_loss_pct": max_stop}
            },
        }

    def test_atr_scaled_stop_is_capped(self) -> None:
        # atr_pct 0.8% * 1.5 = 1.20%, ceiling 0.35% must win.
        target = self._orchestrator(max_stop_loss_pct=0.35)._resolve_trade_targets(
            self._decision(), self._context(0.8)
        )
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target.stop_loss_pct, 0.35, places=6)

    def test_ceiling_is_disabled_by_default(self) -> None:
        target = self._orchestrator(max_stop_loss_pct=0.0)._resolve_trade_targets(
            self._decision(), self._context(0.8)
        )
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target.stop_loss_pct, 1.20, places=6)

    def test_ceiling_never_widens_an_already_tighter_stop(self) -> None:
        # atr_pct 0.1% * 1.5 = 0.15% stays 0.15%; the ceiling is a maximum, not a floor.
        target = self._orchestrator(max_stop_loss_pct=0.35)._resolve_trade_targets(
            self._decision(), self._context(0.1)
        )
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target.stop_loss_pct, 0.15, places=6)

    def test_capping_improves_fee_adjusted_rr(self) -> None:
        capped = self._orchestrator(max_stop_loss_pct=0.35)._resolve_trade_targets(
            self._decision(), self._context(0.8)
        )
        uncapped = self._orchestrator(max_stop_loss_pct=0.0)._resolve_trade_targets(
            self._decision(), self._context(0.8)
        )
        self.assertGreater(capped.fee_adjusted_rr, uncapped.fee_adjusted_rr)

    def test_ceiling_does_not_bypass_the_liquidation_guard(self) -> None:
        # A ceiling at or beyond the liquidation limit must still reject the entry.
        target = self._orchestrator(max_stop_loss_pct=0.35)._resolve_trade_targets(
            self._decision(), self._context(0.8, max_stop=0.30)
        )
        self.assertIsNone(target)
