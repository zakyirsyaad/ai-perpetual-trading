from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from bot.config import Settings
from bot.exchange.bingx_client import BingXFuturesClient
from bot.models import ClosedTrade, OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import RiskManager
from bot.storage.trade_store import TradeStore
from bot.strategy.base import BaseStrategy


class PaperTradeEngine:
    """Realtime paper trading engine (no real order placement)."""

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        client: BingXFuturesClient,
        strategy: BaseStrategy,
        risk_manager: RiskManager,
        trade_store: TradeStore,
    ) -> None:
        self._settings = settings
        self._logger = logger
        self._client = client
        self._strategy = strategy
        self._risk_manager = risk_manager
        self._trade_store = trade_store

    async def run_forever(self) -> None:
        await self._trade_store.initialize()
        await self._startup_safety_checks()
        self._logger.info("Paper trade engine started (no real orders).")

        while True:
            try:
                await self._tick()
            except Exception:
                self._logger.exception("Unhandled error in paper trading loop.")
            await asyncio.sleep(self._settings.entry_interval_minutes * 60)

    async def _tick(self) -> None:
        state = await self._risk_manager.get_daily_state()
        if not state.allowed_to_trade:
            self._logger.warning(
                "Paper risk guard active. stop_losses=%s/%s, consec_losses=%s/%s, day_pnl=%.4f, entries paused.",
                state.stop_losses,
                self._settings.max_stop_loss_per_day,
                state.consecutive_losses,
                self._settings.max_consecutive_losses,
                state.total_pnl_usdt,
            )
            return

        if state.closed_trades >= self._settings.trade_target_per_day:
            self._logger.info(
                "Paper daily target reached (%s/%s trades). Waiting for next day.",
                state.closed_trades,
                self._settings.trade_target_per_day,
            )
            return

        plan = await self._strategy.next_trade_plan()
        if plan is None:
            self._logger.info("Strategy returned HOLD; no paper entry opened this tick.")
            return
        await self._execute_single_trade(plan)

    async def _execute_single_trade(self, plan: TradePlan) -> None:
        entry_price = await self._client.get_last_price(plan.symbol)
        quantity = self._calculate_quantity(plan.usdt_size, plan.leverage, entry_price)
        if quantity < self._settings.min_order_quantity:
            self._logger.warning(
                "Paper skipped trade for %s, quantity %.8f below min_order_quantity %.8f",
                plan.symbol,
                quantity,
                self._settings.min_order_quantity,
            )
            return

        opened_at = datetime.now(timezone.utc)
        order_id = f"PAPER-{int(opened_at.timestamp() * 1000)}"
        open_position = OpenPosition(
            symbol=plan.symbol,
            direction=plan.direction,
            entry_price=entry_price,
            quantity=quantity,
            exchange_order_id=order_id,
            opened_at=opened_at,
            entry_reason=plan.decision_reason,
        )
        await self._trade_store.save_open_position(open_position)
        self._logger.info(
            "Paper open %s %s | qty=%.8f | entry_price=%.8f | conf=%.2f | setup=%.2f | decision=%s",
            plan.symbol,
            plan.direction.value,
            quantity,
            entry_price,
            plan.confidence,
            plan.setup_score,
            plan.decision_reason,
        )

        exit_reason = await self._monitor_position(open_position, plan)
        exit_price = await self._client.get_last_price(open_position.symbol)
        closed_at = datetime.now(timezone.utc)
        gross_pnl = self._estimate_gross_pnl(
            open_position.direction, open_position.entry_price, exit_price, quantity
        )
        total_fee = self._estimate_total_taker_fee(
            open_position.entry_price,
            exit_price,
            quantity,
            self._settings.taker_fee_pct,
        )
        pnl = gross_pnl - total_fee
        exit_order_id = f"PAPER-CLOSE-{int(closed_at.timestamp() * 1000)}"

        closed_trade = ClosedTrade(
            symbol=open_position.symbol,
            direction=open_position.direction,
            entry_price=open_position.entry_price,
            exit_price=exit_price,
            quantity=open_position.quantity,
            pnl_usdt=pnl,
            reason=exit_reason,
            entry_reason=open_position.entry_reason,
            opened_at=open_position.opened_at,
            closed_at=closed_at,
            exchange_entry_order_id=order_id,
            exchange_exit_order_id=exit_order_id,
        )
        await self._trade_store.save_closed_trade(closed_trade)
        await self._trade_store.remove_open_position(open_position.symbol)

        daily_volume = await self._trade_store.total_volume_for_day()
        state = await self._risk_manager.get_daily_state()
        self._logger.info(
            "Paper close %s %s | reason=%s | gross=%.4f | fee=%.4f | net_pnl=%.4f USDT | stop_losses=%s/%s | day_volume=%.2f",
            closed_trade.symbol,
            closed_trade.direction.value,
            closed_trade.reason,
            gross_pnl,
            total_fee,
            closed_trade.pnl_usdt,
            state.stop_losses,
            self._settings.max_stop_loss_per_day,
            daily_volume,
        )

    async def _monitor_position(self, position: OpenPosition, plan: TradePlan) -> str:
        max_hold_seconds = self._resolve_max_hold_seconds(plan)
        while True:
            current_price = await self._client.get_last_price(position.symbol)
            elapsed = datetime.now(timezone.utc).timestamp() - position.opened_at.timestamp()
            if self._hit_stop_loss(position.direction, position.entry_price, current_price, plan.stop_loss_pct):
                return "stop_loss"
            gross = self._estimate_gross_pnl(
                position.direction, position.entry_price, current_price, position.quantity
            )
            fee = self._estimate_total_taker_fee(
                position.entry_price, current_price, position.quantity, self._settings.taker_fee_pct
            )
            if self._hit_take_profit(position.direction, position.entry_price, current_price, plan.take_profit_pct):
                if gross >= fee:
                    return "take_profit"
                self._logger.debug(
                    "TP hit but gross %.4f < fee %.4f; holding for better exit.",
                    gross, fee,
                )
            if elapsed >= max_hold_seconds:
                return "time_stop"
            ai_exit = await self._strategy.exit_signal(position, current_price, elapsed)
            if ai_exit.close_now:
                if gross >= fee:
                    return f"strategy_exit:{ai_exit.reason}"
                self._logger.debug(
                    "Strategy exit but gross %.4f < fee %.4f; holding to avoid fee loss.",
                    gross, fee,
                )
            await asyncio.sleep(self._settings.position_monitor_interval_seconds)

    async def _startup_safety_checks(self) -> None:
        await self._client.get_last_price(self._settings.symbols[0])
        stale = await self._trade_store.list_open_positions()
        for pos in stale:
            await self._trade_store.remove_open_position(pos.symbol)
            self._logger.warning("Removed stale local paper open position for %s.", pos.symbol)

    def _resolve_max_hold_seconds(self, plan: TradePlan) -> float:
        hold_minutes = plan.max_hold_minutes or self._settings.position_hold_minutes
        return max(float(hold_minutes), 1.0) * 60.0

    @staticmethod
    def _hit_take_profit(direction: PositionDirection, entry: float, current: float, pct: float) -> bool:
        if direction == PositionDirection.LONG:
            return current >= entry * (1 + pct / 100)
        return current <= entry * (1 - pct / 100)

    @staticmethod
    def _hit_stop_loss(direction: PositionDirection, entry: float, current: float, pct: float) -> bool:
        if direction == PositionDirection.LONG:
            return current <= entry * (1 - pct / 100)
        return current >= entry * (1 + pct / 100)

    @staticmethod
    def _calculate_quantity(usdt_size: float, leverage: int, price: float) -> float:
        return (usdt_size * leverage) / price

    @staticmethod
    def _estimate_gross_pnl(direction: PositionDirection, entry: float, exit_price: float, qty: float) -> float:
        if direction == PositionDirection.LONG:
            return (exit_price - entry) * qty
        return (entry - exit_price) * qty

    @staticmethod
    def _estimate_total_taker_fee(
        entry_price: float,
        exit_price: float,
        qty: float,
        taker_fee_pct: float,
    ) -> float:
        fee_rate = taker_fee_pct / 100
        entry_notional = entry_price * qty
        exit_notional = exit_price * qty
        return (entry_notional * fee_rate) + (exit_notional * fee_rate)

