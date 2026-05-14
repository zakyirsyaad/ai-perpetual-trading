from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from bot.storage.trade_store import TradeStore


@dataclass(slots=True)
class DailyRiskState:
    day: date
    stop_losses: int
    closed_trades: int
    total_pnl_usdt: float
    consecutive_losses: int
    allowed_to_trade: bool


class RiskManager:
    def __init__(
        self,
        trade_store: TradeStore,
        max_stop_losses_per_day: int,
        max_daily_drawdown_usdt: float,
        max_consecutive_losses: int,
    ) -> None:
        self._trade_store = trade_store
        self._max_stop_losses_per_day = max_stop_losses_per_day
        self._max_daily_drawdown_usdt = max_daily_drawdown_usdt
        self._max_consecutive_losses = max_consecutive_losses

    async def get_daily_state(self) -> DailyRiskState:
        today = datetime.now(timezone.utc).date()
        stop_losses = await self._trade_store.count_stop_losses_for_day(today)
        closed_trades = await self._trade_store.count_closed_trades_for_day(today)
        total_pnl_usdt = await self._trade_store.total_pnl_for_day(today)
        consecutive_losses = await self._trade_store.consecutive_losses_for_day(today)
        drawdown_triggered = total_pnl_usdt <= -abs(self._max_daily_drawdown_usdt)
        consecutive_triggered = consecutive_losses >= self._max_consecutive_losses
        allowed = (
            stop_losses < self._max_stop_losses_per_day
            and not drawdown_triggered
            and not consecutive_triggered
        )
        return DailyRiskState(
            day=today,
            stop_losses=stop_losses,
            closed_trades=closed_trades,
            total_pnl_usdt=total_pnl_usdt,
            consecutive_losses=consecutive_losses,
            allowed_to_trade=allowed,
        )

    async def can_open_new_position(self) -> bool:
        state = await self.get_daily_state()
        return state.allowed_to_trade

