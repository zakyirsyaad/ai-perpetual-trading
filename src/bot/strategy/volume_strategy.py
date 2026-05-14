from __future__ import annotations

from itertools import cycle

from bot.config import Settings
from bot.models import PositionDirection, TradePlan
from bot.strategy.base import BaseStrategy


class VolumeStrategy(BaseStrategy):
    """
    A deterministic, volume-first strategy:
    - Rotates symbols (BTC/ETH).
    - Alternates LONG and SHORT to keep both sides active.
    - Uses fixed notional size to generate predictable volume.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._symbol_cycle = cycle(settings.symbols)
        self._direction_cycle = cycle((PositionDirection.LONG, PositionDirection.SHORT))

    def next_trade(self) -> TradePlan:
        return TradePlan(
            symbol=next(self._symbol_cycle),
            direction=next(self._direction_cycle),
            usdt_size=self._settings.position_usdt,
            leverage=self._settings.leverage,
            stop_loss_pct=self._settings.stop_loss_pct,
            take_profit_pct=self._settings.take_profit_pct,
            decision_reason="rule_rotation",
            confidence=0.5,
            setup_score=0.5,
            max_hold_minutes=self._settings.position_hold_minutes,
        )

    async def next_trade_plan(self) -> TradePlan:
        return self.next_trade()

