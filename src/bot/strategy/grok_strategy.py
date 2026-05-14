from __future__ import annotations

from bot.ai.orchestration import AIOrchestrator
from bot.models import OpenPosition, TradePlan
from bot.risk.risk_manager import DailyRiskState
from bot.strategy.base import BaseStrategy, ExitSignal


class GrokStrategy(BaseStrategy):
    """AI strategy facade over the four-layer Grok trading stack."""

    def __init__(self, orchestrator: AIOrchestrator) -> None:
        self._orchestrator = orchestrator
        self._daily_state: DailyRiskState | None = None

    def set_daily_state(self, state: DailyRiskState) -> None:
        self._daily_state = state

    async def next_trade_plan(self) -> TradePlan | None:
        if self._daily_state is None:
            raise RuntimeError("Daily risk state must be set before requesting a Grok trade plan.")
        return await self._orchestrator.next_trade_plan(self._daily_state)

    async def exit_signal(
        self,
        position: OpenPosition,
        current_price: float,
        elapsed_seconds: float,
    ) -> ExitSignal:
        return await self._orchestrator.exit_signal(position, current_price, elapsed_seconds)

    async def close(self) -> None:
        await self._orchestrator.close()
