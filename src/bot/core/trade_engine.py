from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from bot.config import Settings
from bot.exchange.okx_client import OKXFuturesClient, OKXOrderBook
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
        self._taker_fee_pct_by_symbol: dict[str, float] = {}
        self._maker_fee_pct_by_symbol: dict[str, float | None] = {}

    async def run_forever(self) -> None:
        await self._trade_store.initialize()
        await self._startup_safety_checks()
        await self._refresh_exchange_fee_rates()
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
        target_notional = plan.usdt_size * plan.leverage
        guard_allowed, order_book = await self._pre_entry_execution_checks(plan, target_notional)
        if not guard_allowed:
            return

        entry_price = order_book.mid_price if order_book is not None else await self._client.get_last_price(plan.symbol)
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
            "Opening %s %s | margin=%.2f | leverage=%sx | target_notional=%.2f | target_round_trip_volume=%.2f | "
            "base_qty=%.8f | okx_size=%.8f | entry_price=%.8f | spread_bps=%.2f | taker_fee_pct=%.4f | "
            "conf=%.2f | setup=%.2f | decision=%s",
            plan.symbol,
            plan.direction.value,
            plan.usdt_size,
            plan.leverage,
            target_notional,
            plan.usdt_size * plan.leverage * self._settings.volume_sides_per_entry,
            base_quantity,
            exchange_size,
            entry_price,
            order_book.spread_bps if order_book is not None else -1.0,
            self._taker_fee_pct(plan.symbol),
            plan.confidence,
            plan.setup_score,
            plan.decision_reason,
        )

        entry_order_id = await self._place_entry_order(plan, exchange_size, order_book)
        if entry_order_id is None:
            return
        await asyncio.sleep(self._settings.order_fill_wait_seconds)
        entry_fill = await self._client.get_order_fill(plan.symbol, entry_order_id)
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
        if entry_fill is not None and entry_fill.base_quantity > 0:
            filled_base_quantity = entry_fill.base_quantity
        if filled_entry_price:
            entry_price = filled_entry_price
        if entry_fill is not None and entry_fill.avg_price is not None:
            entry_price = entry_fill.avg_price
        entry_fee_usdt = entry_fill.fee_usdt if entry_fill is not None else 0.0
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

        exit_reason = await self._post_entry_position_guard(open_position, plan)
        if exit_reason is None:
            exit_reason = await self._monitor_position(open_position, plan)

        # It is possible that the exchange position was already closed or resized externally
        # (manual close, liquidation, partial close, etc.). In that case we should not try to
        # send a reduce-only order for the original size.
        exchange_size_to_close, _ = await self._client.get_symbol_position_snapshot(open_position.symbol)
        realized_qty = open_position.quantity
        exit_fill = None
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
            exit_fill = await self._client.get_order_fill(open_position.symbol, exit_order_id)
            if exit_fill is not None and exit_fill.base_quantity > 0:
                realized_qty = exit_fill.base_quantity
            if await self._client.has_open_position(open_position.symbol):
                raise RuntimeError(
                    f"Exit order placed but position still open for {open_position.symbol}."
                )
            if exit_fill is not None and exit_fill.avg_price is not None:
                exit_price = exit_fill.avg_price
            else:
                exit_price = await self._client.get_last_price(open_position.symbol)
        closed_at = datetime.now(timezone.utc)
        gross_pnl = self._estimate_gross_pnl(
            open_position.direction,
            open_position.entry_price,
            exit_price,
            realized_qty,
        )
        actual_fee = entry_fee_usdt + (exit_fill.fee_usdt if exit_fill is not None else 0.0)
        if entry_fill is not None and exit_fill is not None and actual_fee > 0:
            total_fee = actual_fee
        else:
            total_fee = self._estimate_total_taker_fee(
                open_position.entry_price,
                exit_price,
                realized_qty,
                self._taker_fee_pct(open_position.symbol),
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
            "Closed %s %s | reason=%s | entry=%.4f | exit=%.4f | qty=%.8f | gross=%.4f | fee=%.4f | net_pnl=%.4f USDT | stop_losses=%s/%s | day_volume=%.2f",
            closed_trade.symbol,
            closed_trade.direction.value,
            closed_trade.reason,
            closed_trade.entry_price,
            closed_trade.exit_price,
            closed_trade.quantity,
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
                position.entry_price, current_price, position.quantity, self._taker_fee_pct(position.symbol)
            )
            net_after_fee = gross - fee
            if self._settings.volume_neutral_exit_enabled and elapsed >= self._settings.volume_neutral_exit_min_seconds:
                min_net = self._settings.volume_neutral_exit_min_net_usdt
                max_net = min_net + self._settings.volume_neutral_exit_band_usdt
                if min_net <= net_after_fee <= max_net:
                    return "neutral_exit"
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

    async def _refresh_exchange_fee_rates(self) -> None:
        if not self._settings.use_exchange_fee_rates:
            return
        refreshed: dict[str, float] = {}
        for symbol in self._settings.symbols:
            try:
                rates = await self._client.get_trade_fee_rates(symbol)
            except Exception as exc:
                self._logger.warning(
                    "Could not fetch exchange fee rate for %s; using configured TAKER_FEE_PCT %.4f. error=%s",
                    symbol,
                    self._settings.taker_fee_pct,
                    exc,
                )
                continue
            if rates.taker_fee_pct <= 0:
                continue
            self._taker_fee_pct_by_symbol[symbol] = rates.taker_fee_pct
            self._maker_fee_pct_by_symbol[symbol] = rates.maker_fee_pct
            refreshed[symbol] = rates.taker_fee_pct
            self._logger.info(
                "Exchange fee rate for %s: maker=%s%% taker=%.4f%%",
                symbol,
                f"{rates.maker_fee_pct:.4f}" if rates.maker_fee_pct is not None else "unknown",
                rates.taker_fee_pct,
            )
        if refreshed:
            self._settings.taker_fee_pct = max(self._settings.taker_fee_pct, max(refreshed.values()))

    async def _pre_entry_execution_checks(
        self,
        plan: TradePlan,
        target_notional: float,
    ) -> tuple[bool, OKXOrderBook | None]:
        if not self._settings.execution_guards_enabled:
            return True, None

        try:
            order_book = await self._client.get_order_book(
                plan.symbol,
                depth=self._settings.orderbook_depth_levels,
            )
        except Exception as exc:
            self._logger.warning("Skipped %s entry: order book guard failed. error=%s", plan.symbol, exc)
            return False, None

        if order_book.spread_bps > self._settings.perp_max_spread_bps:
            self._logger.warning(
                "Skipped %s entry: spread %.2fbps > max %.2fbps.",
                plan.symbol,
                order_book.spread_bps,
                self._settings.perp_max_spread_bps,
            )
            return False, order_book

        executable_depth = (
            order_book.ask_depth_notional
            if plan.direction == PositionDirection.LONG
            else order_book.bid_depth_notional
        )
        required_depth = target_notional * self._settings.min_orderbook_depth_multiplier
        if required_depth > 0 and executable_depth < required_depth:
            self._logger.warning(
                "Skipped %s %s entry: depth %.2f USDT < required %.2f USDT for target_notional %.2f.",
                plan.symbol,
                plan.direction.value,
                executable_depth,
                required_depth,
                target_notional,
            )
            return False, order_book

        try:
            funding = await self._client.get_funding_rate_info(plan.symbol)
        except Exception as exc:
            self._logger.warning("Skipped %s entry: funding guard failed. error=%s", plan.symbol, exc)
            return False, order_book

        if self._settings.max_abs_funding_rate > 0 and abs(funding.funding_rate) > self._settings.max_abs_funding_rate:
            self._logger.warning(
                "Skipped %s entry: abs funding rate %.6f > max %.6f.",
                plan.symbol,
                abs(funding.funding_rate),
                self._settings.max_abs_funding_rate,
            )
            return False, order_book

        if funding.next_funding_time is not None and self._settings.perp_funding_blackout_minutes > 0:
            seconds_to_funding = (funding.next_funding_time - datetime.now(timezone.utc)).total_seconds()
            blackout_seconds = self._settings.perp_funding_blackout_minutes * 60
            if 0 <= seconds_to_funding <= blackout_seconds:
                self._logger.warning(
                    "Skipped %s entry: funding blackout active (%.0fs to funding, rate=%.6f).",
                    plan.symbol,
                    seconds_to_funding,
                    funding.funding_rate,
                )
                return False, order_book

        self._logger.info(
            "Execution guard passed for %s: spread=%.2fbps depth_bid=%.2f depth_ask=%.2f funding=%.6f.",
            plan.symbol,
            order_book.spread_bps,
            order_book.bid_depth_notional,
            order_book.ask_depth_notional,
            funding.funding_rate,
        )
        return True, order_book

    async def _place_entry_order(
        self,
        plan: TradePlan,
        exchange_size: float,
        order_book: OKXOrderBook | None,
    ) -> str | None:
        if self._settings.execution_entry_order_type == "post_only":
            order_id = await self._place_post_only_entry(plan, exchange_size, order_book)
            if order_id is not None:
                return order_id
            if not self._settings.execution_post_only_fallback_to_market:
                self._logger.info("Skipped %s entry: post-only order did not fill.", plan.symbol)
                return None
            self._logger.warning("Post-only entry for %s did not fill; falling back to market order.", plan.symbol)

        entry_resp = await self._client.place_market_order(
            symbol=plan.symbol,
            direction=plan.direction,
            size=exchange_size,
            reduce_only=False,
        )
        return self._extract_order_id(entry_resp)

    async def _place_post_only_entry(
        self,
        plan: TradePlan,
        exchange_size: float,
        order_book: OKXOrderBook | None,
    ) -> str | None:
        attempts = max(1, self._settings.execution_post_only_max_reprices + 1)
        terminal_states = {"filled", "canceled", "mmp_canceled"}
        for attempt in range(attempts):
            book = order_book if attempt == 0 and order_book is not None else await self._client.get_order_book(
                plan.symbol,
                depth=self._settings.orderbook_depth_levels,
            )
            price = book.best_bid if plan.direction == PositionDirection.LONG else book.best_ask
            response = await self._client.place_limit_order(
                symbol=plan.symbol,
                direction=plan.direction,
                size=exchange_size,
                price=price,
                reduce_only=False,
                post_only=True,
            )
            order_id = self._extract_order_id(response)
            deadline = datetime.now(timezone.utc).timestamp() + self._settings.execution_post_only_timeout_seconds
            while datetime.now(timezone.utc).timestamp() < deadline:
                await asyncio.sleep(min(1.0, self._settings.execution_post_only_timeout_seconds))
                fill = await self._client.get_order_fill(plan.symbol, order_id)
                if fill is not None and fill.filled_size > 0:
                    if fill.state not in {"filled", None}:
                        await self._cancel_order_best_effort(plan.symbol, order_id)
                    return order_id
                if fill is not None and fill.state in terminal_states:
                    break
            await self._cancel_order_best_effort(plan.symbol, order_id)
        return None

    async def _cancel_order_best_effort(self, symbol: str, order_id: str) -> None:
        try:
            await self._client.cancel_order(symbol, order_id)
        except Exception as exc:
            self._logger.debug("Could not cancel order %s for %s: %s", order_id, symbol, exc)

    async def _post_entry_position_guard(self, position: OpenPosition, plan: TradePlan) -> str | None:
        if self._settings.perp_min_liquidation_distance_bps <= 0:
            return None
        try:
            details = await self._client.get_symbol_position_details(position.symbol)
        except Exception as exc:
            self._logger.warning("Could not verify liquidation distance for %s: %s", position.symbol, exc)
            return "liquidation_guard_unverified"

        if details.liquidation_price is None:
            return None
        reference_price = details.mark_price or details.avg_price or position.entry_price
        if reference_price <= 0:
            return None
        distance_bps = abs(reference_price - details.liquidation_price) / reference_price * 10_000
        stop_price = self._planned_stop_price(position.direction, position.entry_price, plan.stop_loss_pct)
        stop_beyond_liq = (
            position.direction == PositionDirection.LONG
            and stop_price <= details.liquidation_price
        ) or (
            position.direction == PositionDirection.SHORT
            and stop_price >= details.liquidation_price
        )
        if distance_bps < self._settings.perp_min_liquidation_distance_bps or stop_beyond_liq:
            self._logger.warning(
                "Liquidation guard closing %s: liq=%.4f ref=%.4f distance=%.2fbps min=%.2fbps stop=%.4f.",
                position.symbol,
                details.liquidation_price,
                reference_price,
                distance_bps,
                self._settings.perp_min_liquidation_distance_bps,
                stop_price,
            )
            return "liquidation_guard"
        return None

    def _taker_fee_pct(self, symbol: str) -> float:
        return self._taker_fee_pct_by_symbol.get(symbol, self._settings.taker_fee_pct)

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
    def _planned_stop_price(direction: PositionDirection, entry: float, pct: float) -> float:
        if direction == PositionDirection.LONG:
            return entry * (1 - pct / 100)
        return entry * (1 + pct / 100)

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
