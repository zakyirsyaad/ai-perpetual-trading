from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from bot.config import Settings
from bot.exchange.okx_client import OKXFuturesClient
from bot.models import ClosedTrade, OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import RiskManager
from bot.storage.supabase_journal import SupabaseJournal
from bot.storage.trade_store import TradeStore
from bot.strategy.base import BaseStrategy
from bot.strategy.grok_strategy import GrokStrategy


class TradeEngine:
    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        client: OKXFuturesClient,
        strategy: BaseStrategy,
        risk_manager: RiskManager,
        trade_store: TradeStore,
        supabase_journal: SupabaseJournal | None = None,
    ) -> None:
        self._settings = settings
        self._logger = logger
        self._client = client
        self._strategy = strategy
        self._risk_manager = risk_manager
        self._trade_store = trade_store
        self._supabase_journal = supabase_journal
        self._last_entry_ts: float | None = None
        self._last_stop_loss_ts: float | None = None

    async def run_forever(self) -> None:
        await self._trade_store.initialize()
        await self._startup_safety_checks()
        await self._initialize_leverage()
        self._logger.info("OKX demo trade engine started.")

        while True:
            try:
                await self._tick()
            except Exception:
                self._logger.exception("Unhandled error in trading loop.")
            await asyncio.sleep(self._settings.entry_interval_minutes * 60)

    async def _tick(self) -> None:
        state = await self._risk_manager.get_daily_state()
        if isinstance(self._strategy, GrokStrategy):
            self._strategy.set_daily_state(state)
        if not state.allowed_to_trade:
            self._logger.warning(
                "Risk guard active. stop_losses=%s/%s, consec_losses=%s/%s, day_pnl=%.4f, entries paused.",
                state.stop_losses,
                self._settings.max_stop_loss_per_day,
                state.consecutive_losses,
                self._settings.max_consecutive_losses,
                state.total_pnl_usdt,
            )
            return

        now_ts = datetime.now(timezone.utc).timestamp()
        # Cooldown after recent stop-loss
        if self._last_stop_loss_ts is not None:
            cool_stop = self._settings.cooldown_minutes_after_stop * 60
            if now_ts - self._last_stop_loss_ts < cool_stop:
                self._logger.info(
                    "Cooldown after stop-loss active (%.0fs remaining); entries paused.",
                    cool_stop - (now_ts - self._last_stop_loss_ts),
                )
                return

        # Cooldown after recent entry
        if self._last_entry_ts is not None:
            cool_entry = self._settings.cooldown_minutes_after_entry * 60
            if now_ts - self._last_entry_ts < cool_entry:
                self._logger.info(
                    "Cooldown after last entry active (%.0fs remaining); entries paused.",
                    cool_entry - (now_ts - self._last_entry_ts),
                )
                return

        if state.closed_trades >= self._settings.trade_target_per_day:
            self._logger.info(
                "Daily target reached (%s/%s trades). Waiting for next day.",
                state.closed_trades,
                self._settings.trade_target_per_day,
            )
            return

        plan = await self._strategy.next_trade_plan()
        if plan is None:
            self._logger.info("Strategy returned HOLD; no entry opened this tick.")
            return
        if self._settings.prevent_new_entries_if_exchange_position_open:
            if await self._client.has_any_open_position(self._settings.symbols):
                self._logger.warning(
                    "Skipped entry because an exchange position is already open in monitored symbols.",
                )
                return
        await self._execute_single_trade(plan)

    async def _initialize_leverage(self) -> None:
        try:
            resp = await self._client.set_position_mode(self._settings.okx_position_mode)
            self._logger.info("Set OKX position mode: %s", resp.get("msg", "ok"))
        except Exception as exc:
            self._logger.warning(
                "Could not set OKX position mode to %s. Continuing with current account mode. error=%s",
                self._settings.okx_position_mode,
                exc,
            )
        for symbol in self._settings.symbols:
            try:
                resp = await self._client.set_leverage(symbol=symbol, leverage=self._settings.leverage)
                self._logger.info("Set leverage for %s: %s", symbol, resp.get("msg", "ok"))
            except Exception as exc:
                message = (
                    f"Could not set OKX leverage for {symbol} to {self._settings.leverage}x. "
                    f"Continuing with the leverage currently configured on OKX. error={exc}"
                )
                if self._settings.okx_require_set_leverage:
                    raise RuntimeError(message) from exc
                self._logger.warning(message)

    async def _execute_single_trade(self, plan: TradePlan) -> None:
        entry_price = await self._client.get_last_price(plan.symbol)
        base_quantity = self._calculate_quantity(plan.usdt_size, plan.leverage, entry_price)
        exchange_size = await self._client.calculate_order_size(plan.symbol, base_quantity)
        if exchange_size < self._settings.min_order_contracts:
            self._logger.warning(
                "Skipped trade for %s, order size %.8f below MIN_ORDER_CONTRACTS %.8f",
                plan.symbol,
                exchange_size,
                self._settings.min_order_contracts,
            )
            return
        self._logger.info(
            "Opening %s %s | base_qty=%.8f | okx_size=%.8f | entry_price=%.8f | conf=%.2f | setup=%.2f | decision=%s",
            plan.symbol,
            plan.direction.value,
            base_quantity,
            exchange_size,
            entry_price,
            plan.confidence,
            plan.setup_score,
            plan.decision_reason,
        )

        entry_resp = await self._client.place_market_order(
            symbol=plan.symbol,
            direction=plan.direction,
            size=exchange_size,
            reduce_only=False,
        )
        entry_order_id = self._extract_order_id(entry_resp)
        await asyncio.sleep(self._settings.order_fill_wait_seconds)
        # Verify that a position actually exists and adjust to the filled quantity.
        filled_size, filled_entry_price = await self._client.get_symbol_position_snapshot(plan.symbol)
        if filled_size <= 0:
            raise RuntimeError(
                f"Entry order placed but no open position detected for {plan.symbol}."
            )
        if filled_size < exchange_size * 0.5:
            self._logger.warning(
                "Entry for %s appears partially filled: target_okx_size=%.8f, exchange_okx_size=%.8f. "
                "Proceeding with smaller size.",
                plan.symbol,
                exchange_size,
                filled_size,
            )
        filled_base_quantity = await self._client.estimate_base_quantity(plan.symbol, filled_size)
        if filled_base_quantity <= 0:
            filled_base_quantity = base_quantity
        if filled_entry_price:
            entry_price = filled_entry_price
        opened_at = datetime.now(timezone.utc)

        open_position = OpenPosition(
            symbol=plan.symbol,
            direction=plan.direction,
            entry_price=entry_price,
            quantity=filled_base_quantity,
            exchange_size=filled_size,
            exchange_order_id=entry_order_id,
            opened_at=opened_at,
            entry_reason=plan.decision_reason,
        )
        await self._trade_store.save_open_position(open_position)

        # Record last entry timestamp for cooldown logic.
        self._last_entry_ts = opened_at.timestamp()

        exit_reason = await self._monitor_position(open_position, plan)

        # It is possible that the exchange position was already closed or resized externally
        # (manual close, liquidation, partial close, etc.). In that case we should not try to
        # send a reduce-only order for the original size.
        exchange_size_to_close, _ = await self._client.get_symbol_position_snapshot(open_position.symbol)
        realized_qty = open_position.quantity
        if exchange_size_to_close <= 0:
            self._logger.warning(
                "No position to close on exchange for %s; treating as already closed. reason=%s",
                open_position.symbol,
                exit_reason,
            )
            exit_order_id = "NO_POSITION_TO_CLOSE"
            exit_price = await self._client.get_last_price(open_position.symbol)
        else:
            realized_qty = await self._client.estimate_base_quantity(open_position.symbol, exchange_size_to_close)
            if realized_qty <= 0:
                realized_qty = open_position.quantity
            if abs(exchange_size_to_close - open_position.exchange_size) > 1e-8:
                self._logger.warning(
                    "Exchange position size for %s changed during trade lifecycle: local_okx_size=%.8f, exchange_okx_size=%.8f.",
                    open_position.symbol,
                    open_position.exchange_size,
                    exchange_size_to_close,
                )
            exit_resp = await self._client.place_market_order(
                symbol=open_position.symbol,
                direction=open_position.direction,
                size=exchange_size_to_close,
                reduce_only=True,
            )
            exit_order_id = self._extract_order_id(exit_resp)
            await asyncio.sleep(self._settings.order_fill_wait_seconds)
            if await self._client.has_open_position(open_position.symbol):
                raise RuntimeError(
                    f"Exit order placed but position still open for {open_position.symbol}."
                )
            exit_price = await self._client.get_last_price(open_position.symbol)
        closed_at = datetime.now(timezone.utc)
        gross_pnl = self._estimate_gross_pnl(
            open_position.direction,
            open_position.entry_price,
            exit_price,
            realized_qty,
        )
        total_fee = self._estimate_total_taker_fee(
            open_position.entry_price,
            exit_price,
            realized_qty,
            self._settings.taker_fee_pct,
        )
        pnl = gross_pnl - total_fee

        closed_trade = ClosedTrade(
            symbol=open_position.symbol,
            direction=open_position.direction,
            entry_price=open_position.entry_price,
            exit_price=exit_price,
            quantity=realized_qty,
            pnl_usdt=pnl,
            reason=exit_reason,
            entry_reason=open_position.entry_reason,
            opened_at=open_position.opened_at,
            closed_at=closed_at,
            exchange_entry_order_id=entry_order_id,
            exchange_exit_order_id=exit_order_id,
        )
        await self._trade_store.save_closed_trade(closed_trade)
        if self._supabase_journal is not None:
            await self._supabase_journal.record_closed_trade(closed_trade)
        await self._trade_store.remove_open_position(open_position.symbol)

        if exit_reason == "stop_loss":
            self._last_stop_loss_ts = closed_at.timestamp()

        daily_volume = await self._trade_store.total_volume_for_day()
        state = await self._risk_manager.get_daily_state()
        self._logger.info(
            "Closed %s %s | reason=%s | gross=%.4f | fee=%.4f | net_pnl=%.4f USDT | stop_losses=%s/%s | day_volume=%.2f",
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

    async def _startup_safety_checks(self) -> None:
        if not self._settings.okx_demo_trading:
            raise RuntimeError("OKX_DEMO_TRADING must be true for this project runtime.")

        await self._client.validate_api_access(self._settings.symbols[0])
        self._logger.info("OKX demo API connectivity and auth check passed.")
        await self._reconcile_local_open_positions()

    async def _reconcile_local_open_positions(self) -> None:
        local_positions = await self._trade_store.list_open_positions()
        local_symbols: set[str] = set()
        if not local_positions:
            self._logger.info("No local open positions found during startup reconcile.")
        else:
            for pos in local_positions:
                exchange_has_position = await self._client.has_open_position(pos.symbol)
                if not exchange_has_position:
                    await self._trade_store.remove_open_position(pos.symbol)
                    self._logger.warning(
                        "Removed stale local open position for %s during startup reconcile.",
                        pos.symbol,
                    )
                    continue
                local_symbols.add(pos.symbol)
        for symbol in self._settings.symbols:
            if symbol in local_symbols:
                continue
            if await self._client.has_open_position(symbol):
                self._logger.warning(
                    "Exchange already has an open position for %s but no local record was found. "
                    "New entries will remain blocked until that position is closed.",
                    symbol,
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
        notional = usdt_size * leverage
        return notional / price

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

    @staticmethod
    def _extract_order_id(response: dict) -> str:
        data = response.get("data") or {}
        if isinstance(data, list) and data:
            first = data[0] if isinstance(data[0], dict) else {}
            value = first.get("ordId") or first.get("orderId") or first.get("id") or "UNKNOWN_ORDER_ID"
            return str(value)
        value = data.get("ordId") or data.get("orderId") or data.get("id") or "UNKNOWN_ORDER_ID"
        return str(value)

