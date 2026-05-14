from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from bot.models import OpenPosition, TradePlan


@dataclass(slots=True)
class ExitSignal:
    close_now: bool
    reason: str = "hold"


class BaseStrategy(ABC):
    @abstractmethod
    async def next_trade_plan(self) -> TradePlan | None:
        """Return next trade plan or None (HOLD)."""

    async def exit_signal(
        self,
        position: OpenPosition,
        current_price: float,
        elapsed_seconds: float,
    ) -> ExitSignal:
        return ExitSignal(close_now=False, reason="hold")

