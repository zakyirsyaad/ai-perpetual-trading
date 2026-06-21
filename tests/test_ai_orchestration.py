from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace

from bot.ai.brain import EntryDecision
from bot.ai.orchestration import AIOrchestrator, EntryCandidate, ResolvedTradeTargets
from bot.models import PositionDirection


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
        ai_min_fee_adjusted_rr=0.1,
    )


class EntryGateRemovalTests(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self) -> AIOrchestrator:
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = _volume_run_settings()
        orchestrator._logger = logging.getLogger("test.entry_gate_removal")
        return orchestrator

    def test_low_confidence_high_risk_low_rr_decision_still_opens(self) -> None:
        # A decision that the deleted gates would have vetoed: confidence below the
        # old floor, max systemic risk, and risk/reward under the old 1.2 floor.
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
        )

        candidate = orchestrator._build_entry_candidate(decision, {"technicals": []})

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.direction, PositionDirection.LONG)
        self.assertEqual(candidate.decision.symbol, "BTC/USDT")

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
    async def test_candidate_to_trade_plan_uses_candidate_direction(self) -> None:
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = SimpleNamespace(
            strategy_mode="grok",
            account_balance_usdt=60.0,
            max_risk_pct_per_trade=0.5,
            leverage=3,
            position_usdt=20.0,
            taker_fee_pct=0.05,
            ai_pre_trade_check=False,
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
            ai_pre_trade_check=False,
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
