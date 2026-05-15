from __future__ import annotations

import unittest
from types import SimpleNamespace

from bot.ai.brain import EntryDecision
from bot.ai.orchestration import AIOrchestrator, EntryCandidate, ResolvedTradeTargets
from bot.models import PositionDirection


class AIOrchestrationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_to_trade_plan_uses_candidate_direction(self) -> None:
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = SimpleNamespace(
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


if __name__ == "__main__":
    unittest.main()
