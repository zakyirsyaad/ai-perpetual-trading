from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

from bot.config import Settings
from bot.exchange.contracts import ExchangeAPIError, FuturesExchangeProtocol, OrderBookSnapshot, OrderFill
from bot.models import ClosedTrade, OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import RiskManager
from bot.storage.interfaces import TradeStoreProtocol
from bot.storage.supabase_journal import SupabaseJournal
from bot.strategy.base import BaseStrategy
from bot.strategy.grok_strategy import GrokStrategy


class TradeEngine:
    # Restart matching tolerances: partial fills and mark-price rounding produce
    # small legitimate drift, but anything larger means the exchange position is
    # not the one this bot persisted.
    _POSITION_MATCH_TOLERANCE = 0.02
    _POSITION_PRICE_TOLERANCE = 0.01

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        client: FuturesExchangeProtocol,
        strategy: BaseStrategy,
        risk_manager: RiskManager,
        trade_store: TradeStoreProtocol,
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
        self._entries_blocked_reason: str | None = None
        self._resumable_positions: list[OpenPosition] = []

    async def run_forever(self) -> None:
        await self._trade_store.initialize()
        await self._startup_safety_checks()
        await self._refresh_exchange_fee_rates()
        await self._initialize_leverage()
        self._logger.info("%s VST trade engine started.", self._venue_label.upper())
        await self._resume_persisted_positions()

        while True:
            try:
                await self._tick()
            except Exception:
                self._logger.exception("Unhandled error in trading loop.")
            await asyncio.sleep(self._settings.entry_interval_minutes * 60)

    async def _tick(self) -> None:
        if self._entries_blocked_reason is not None:
            self._logger.error("Entries blocked: %s", self._entries_blocked_reason)
            return
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

        volume_pause_reason = await self._volume_runtime_pause_reason()
        if volume_pause_reason is not None:
            self._logger.info("Volume runtime guard active: %s. Entries paused.", volume_pause_reason)
            return

        daily_entry_limit = self._daily_entry_limit()
        if state.closed_trades >= daily_entry_limit:
            self._logger.info(
                "Daily target reached (%s/%s trades). Waiting for next day.",
                state.closed_trades,
                daily_entry_limit,
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
        """Apply margin mode, position mode, and leverage, then VERIFY them.

        Applying is best-effort (the venue may already be configured, which some
        endpoints report as an error), but verification is mandatory: if the
        effective account state does not match the configuration, startup fails
        closed rather than trading with the wrong margin mode or leverage.
        """
        set_margin_mode = getattr(self._client, "set_margin_mode", None)
        if set_margin_mode is not None:
            for symbol in self._settings.symbols:
                try:
                    await set_margin_mode(symbol, self._settings.margin_mode)
                except Exception as exc:
                    self._logger.warning(
                        "set_margin_mode(%s, %s) failed; will verify the effective mode. error=%s",
                        symbol,
                        self._settings.margin_mode,
                        exc,
                    )
        try:
            resp = await self._client.set_position_mode(self._settings.position_mode)
            self._logger.info("Set exchange position mode: %s", resp.get("msg", "ok"))
        except Exception as exc:
            self._logger.warning(
                "set_position_mode(%s) failed; will verify the effective mode. error=%s",
                self._settings.position_mode,
                exc,
            )
        for symbol in self._settings.symbols:
            try:
                resp = await self._client.set_leverage(symbol=symbol, leverage=self._settings.leverage)
                self._logger.info("Set leverage for %s: %s", symbol, resp.get("msg", "ok"))
            except Exception as exc:
                self._logger.warning(
                    "set_leverage(%s, %sx) failed; will verify the effective leverage. error=%s",
                    symbol,
                    self._settings.leverage,
                    exc,
                )

        await self._verify_account_configuration()

    async def _verify_account_configuration(self) -> None:
        """Fail closed when the venue's effective account state is not what we configured."""
        mismatches: list[str] = []

        get_position_mode = getattr(self._client, "get_position_mode", None)
        if get_position_mode is not None:
            effective = await self._checked(get_position_mode(), "position mode", mismatches)
            if effective is not None and str(effective).lower() != self._settings.position_mode:
                mismatches.append(
                    f"position mode is {effective!r}, expected {self._settings.position_mode!r}"
                )

        get_margin_mode = getattr(self._client, "get_margin_mode", None)
        get_effective_leverage = getattr(self._client, "get_effective_leverage", None)
        for symbol in self._settings.symbols:
            if get_margin_mode is not None:
                effective = await self._checked(get_margin_mode(symbol), f"margin mode for {symbol}", mismatches)
                if effective is not None and not self._margin_modes_match(str(effective)):
                    mismatches.append(
                        f"{symbol} margin mode is {effective!r}, expected {self._settings.margin_mode!r}"
                    )
            if get_effective_leverage is not None:
                effective = await self._checked(
                    get_effective_leverage(symbol), f"leverage for {symbol}", mismatches
                )
                if effective is not None:
                    long_leverage, short_leverage = effective
                    configured = int(self._settings.leverage)
                    if long_leverage != configured or short_leverage != configured:
                        mismatches.append(
                            f"{symbol} leverage is long={long_leverage}x short={short_leverage}x, "
                            f"expected {configured}x on both sides"
                        )

        if mismatches:
            raise RuntimeError(
                "Exchange account configuration does not match this runtime's settings: "
                + "; ".join(mismatches)
                + ". Fix the account on the venue (or the configuration) before trading."
            )
        self._logger.info("Verified effective margin mode, position mode, and leverage on the venue.")

    async def _checked(self, awaitable, label: str, mismatches: list[str]):
        """Await a verification query; an unreadable value is itself a mismatch."""
        try:
            return await awaitable
        except Exception as exc:
            mismatches.append(f"could not read {label} ({type(exc).__name__}: {exc})")
            return None

    def _margin_modes_match(self, effective: str) -> bool:
        configured = str(self._settings.margin_mode).strip().lower()
        normalized = effective.strip().lower()
        # BingX reports isolated margin as "isolated"; cross appears as "crossed"/"cross".
        if configured == "isolated":
            return normalized == "isolated"
        return normalized in {configured, f"{configured}ed"}

    async def _execute_single_trade(self, plan: TradePlan) -> None:
        target_notional = plan.usdt_size * plan.leverage
        guard_allowed, order_book = await self._pre_entry_execution_checks(plan, target_notional)
        if not guard_allowed:
            return

        entry_price = order_book.mid_price if order_book is not None else await self._client.get_last_price(plan.symbol)
        base_quantity = self._calculate_quantity(plan.usdt_size, plan.leverage, entry_price)
        # `calculate_order_size` returns 0 when the venue's own tradeMinQuantity /
        # tradeMinUSDT metadata rejects the size. That metadata is the only
        # authority here: the old MIN_ORDER_CONTRACTS floor was an OKX
        # contract-count concept and, applied to BingX base-asset quantity, it
        # silently blocked every BTC entry (18,000 USDT notional is ~0.3 BTC < 1).
        exchange_size = await self._client.calculate_order_size(plan.symbol, base_quantity)
        if exchange_size <= 0:
            self._logger.warning(
                "Skipped trade for %s: base quantity %.8f is below the venue minimum quantity/notional.",
                plan.symbol,
                base_quantity,
            )
            return
        self._logger.info(
            "Opening %s %s | margin=%.2f | leverage=%sx | target_notional=%.2f | target_round_trip_volume=%.2f | "
            "base_qty=%.8f | venue_size=%.8f | entry_price=%.8f | spread_bps=%.2f | taker_fee_pct=%.4f | "
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
        entry_fill = await self._try_get_order_fill(plan.symbol, entry_order_id, phase="entry")
        # Verify that a position actually exists and adjust to the filled quantity.
        # Read full details (not just the snapshot) so the venue position id is
        # captured for restart identity matching.
        position_details = await self._client.get_symbol_position_details(plan.symbol)
        filled_size = position_details.exchange_size
        filled_entry_price = position_details.avg_price
        if filled_size <= 0:
            raise RuntimeError(
                f"Entry order placed but no open position detected for {plan.symbol}."
            )
        self._validate_entry_position_readback(
            plan,
            submitted_size=exchange_size,
            position_details=position_details,
        )
        if filled_size < exchange_size * 0.5:
            self._logger.warning(
                "Entry for %s appears partially filled: target_venue_size=%.8f, exchange_venue_size=%.8f. "
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
            position_id=getattr(position_details, "position_id", None),
            stop_loss_pct=plan.stop_loss_pct,
            take_profit_pct=plan.take_profit_pct,
        )
        await self._trade_store.save_open_position(open_position)

        await self._manage_open_position(
            open_position,
            plan,
            entry_order_id=entry_order_id,
            entry_fill=entry_fill,
            entry_fee_usdt=entry_fee_usdt,
            record_entry_cooldown=True,
        )

    async def _manage_open_position(
        self,
        open_position: OpenPosition,
        plan: TradePlan,
        *,
        entry_order_id: str,
        entry_fill: OrderFill | None,
        entry_fee_usdt: float,
        record_entry_cooldown: bool,
    ) -> None:
        """Monitor an open position to close and record the resulting trade.

        Shared by a freshly-opened trade and by a position resumed from persisted
        state after a restart, so a resumed position is fully monitored rather than
        left running unattended.
        """
        if record_entry_cooldown:
            self._last_entry_ts = open_position.opened_at.timestamp()

        realized_qty = open_position.quantity
        exit_fill = None
        exit_reason = await self._post_entry_position_guard(open_position, plan)
        while True:
            if exit_reason is None:
                exit_reason = await self._monitor_position(open_position, plan)

            # It is possible that the exchange position was already closed or resized externally
            # (manual close, liquidation, partial close, etc.). In that case we should not try to
            # send a reduce-only order for the original size.
            exchange_size_to_close, _ = await self._client.get_symbol_position_snapshot(open_position.symbol)
            if exchange_size_to_close <= 0:
                self._logger.warning(
                    "No position to close on exchange for %s; treating as already closed. reason=%s",
                    open_position.symbol,
                    exit_reason,
                )
                exit_order_id = "NO_POSITION_TO_CLOSE"
                exit_price = await self._client.get_last_price(open_position.symbol)
                break

            realized_qty = await self._client.estimate_base_quantity(open_position.symbol, exchange_size_to_close)
            if realized_qty <= 0:
                realized_qty = open_position.quantity
            if abs(exchange_size_to_close - open_position.exchange_size) > 1e-8:
                self._logger.warning(
                    "Exchange position size for %s changed during trade lifecycle: local_venue_size=%.8f, exchange_venue_size=%.8f.",
                    open_position.symbol,
                    open_position.exchange_size,
                    exchange_size_to_close,
                )
            if self._exit_allows_taker(exit_reason):
                exit_resp = await self._client.place_market_order(
                    symbol=open_position.symbol,
                    direction=open_position.direction,
                    size=exchange_size_to_close,
                    reduce_only=True,
                )
                exit_order_id = self._extract_order_id(exit_resp)
            else:
                exit_order_id = await self._place_post_only_exit(open_position, exchange_size_to_close)
                if exit_order_id is None:
                    self._logger.warning(
                        "Maker-only exit for %s did not fill for non-risk reason=%s; "
                        "resuming position monitor.",
                        open_position.symbol,
                        exit_reason,
                    )
                    exit_reason = None
                    continue
            await asyncio.sleep(self._settings.order_fill_wait_seconds)
            exit_fill = await self._try_get_order_fill(open_position.symbol, exit_order_id, phase="exit")
            if exit_fill is not None and exit_fill.base_quantity > 0:
                realized_qty = exit_fill.base_quantity
            if await self._client.has_open_position(open_position.symbol):
                if not self._exit_allows_taker(exit_reason):
                    self._logger.warning(
                        "Maker-only exit order for %s did not fully close the position; resuming monitor.",
                        open_position.symbol,
                    )
                    exit_reason = None
                    continue
                raise RuntimeError(
                    f"Exit order placed but position still open for {open_position.symbol}."
                )
            if exit_fill is not None and exit_fill.avg_price is not None:
                exit_price = exit_fill.avg_price
            else:
                exit_price = await self._client.get_last_price(open_position.symbol)
            break
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
            confidence=plan.confidence,
            factor_metadata=plan.factor_metadata,
            trigger_metadata=plan.trigger_metadata,
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

    async def _try_get_order_fill(self, symbol: str, order_id: str, *, phase: str) -> OrderFill | None:
        try:
            return await self._client.get_order_fill(symbol, order_id)
        except ExchangeAPIError as exc:
            self._logger.warning(
                "Could not read %s fill for %s order_id=%s; continuing with exchange position state. error=%s",
                phase,
                symbol,
                order_id,
                exc,
            )
            return None

    def _validate_entry_position_readback(
        self,
        plan: TradePlan,
        *,
        submitted_size: float,
        position_details,
    ) -> None:
        expected_side = plan.direction.value
        exchange_side = str(getattr(position_details, "pos_side", "") or "").strip().upper()
        position_mode = str(getattr(self._settings, "position_mode", "") or "").strip().lower()
        allowed_sides = {expected_side} if position_mode == "hedge" else {expected_side, "BOTH"}
        if exchange_side not in allowed_sides:
            qualifier = "opposite-side" if exchange_side and exchange_side != "BOTH" else "unidentified-side"
            raise RuntimeError(
                f"Entry readback for {plan.symbol} returned an {qualifier} position "
                f"({exchange_side or 'UNKNOWN'}); expected {expected_side}."
            )

        filled_size = float(getattr(position_details, "exchange_size", 0.0) or 0.0)
        maximum_expected_size = submitted_size * (1.0 + self._POSITION_MATCH_TOLERANCE)
        if filled_size > maximum_expected_size:
            raise RuntimeError(
                f"Entry readback for {plan.symbol} is larger than submitted entry size: "
                f"submitted={submitted_size:.8f}, exchange={filled_size:.8f}. "
                "Refusing to persist potentially unrelated exposure."
            )

    async def _startup_safety_checks(self) -> None:
        # BingX VST is the only enabled execution environment; live fails closed.
        if str(getattr(self._settings, "bingx_env", "")).lower() != "vst":
            raise RuntimeError("BINGX_ENV must be 'vst' for this project runtime.")

        await self._client.validate_api_access(self._settings.symbols[0])
        self._logger.info("%s VST API connectivity and auth check passed.", self._venue_label.upper())
        await self._reconcile_local_open_positions()

    @property
    def _venue_label(self) -> str:
        return str(getattr(self._client, "venue", None) or getattr(self._settings, "exchange_name", "exchange"))

    async def _volume_runtime_pause_reason(self) -> str | None:
        if self._settings.strategy_mode not in {"volume", "ai_volume"}:
            return None
        daily_volume = await self._trade_store.total_volume_for_day()
        daily_target = float(getattr(self._settings, "volume_daily_target_usdt", 0.0) or 0.0)
        if (
            getattr(self._settings, "volume_stop_at_target_enabled", True)
            and daily_target > 0
            and daily_volume >= daily_target
        ):
            return "daily_volume_target_reached"
        target = float(getattr(self._settings, "volume_target_usdt", 0.0) or 0.0)
        if (
            getattr(self._settings, "volume_stop_at_target_enabled", True)
            and target > 0
            and await self._trade_store.total_volume_all_time() >= target
        ):
            return "volume_target_reached"
        fee_budget = float(getattr(self._settings, "volume_daily_fee_budget_usdt", 0.0) or 0.0)
        estimated_fee = daily_volume * (float(getattr(self._settings, "taker_fee_pct", 0.0) or 0.0) / 100.0)
        if fee_budget > 0 and estimated_fee >= fee_budget:
            return "volume_fee_budget_reached"
        return None

    def _daily_entry_limit(self) -> int:
        if (
            self._settings.strategy_mode in {"volume", "ai_volume"}
            and float(getattr(self._settings, "volume_daily_target_usdt", 0.0) or 0.0) > 0
        ):
            return int(getattr(self._settings, "volume_attempt_target_per_day", self._settings.trade_target_per_day))
        return int(self._settings.trade_target_per_day)

    async def _reconcile_local_open_positions(self) -> None:
        """Match persisted position state against the exchange at startup.

        Matching is on identity, not just symbol: direction/`positionSide`,
        quantity, and — when persisted — average entry price must agree. A
        persisted LONG must never be "matched" to an exchange SHORT, or to a
        position of a materially different size, because the resumed monitor
        would then apply the wrong stop and close a position it does not model.

        A confirmed match is resumed and fully monitored. Anything else is
        unknown exposure: entries are blocked and the operator alerted, and it
        is NEVER auto-flattened.
        """
        local_positions = await self._trade_store.list_open_positions()
        local_by_symbol = {position.symbol: position for position in local_positions}
        if not local_positions:
            self._logger.info("No persisted open positions found during startup reconcile.")

        unknown_exposure: list[str] = []

        for symbol, position in list(local_by_symbol.items()):
            details = await self._position_details(symbol)
            if details is None:
                unknown_exposure.append(f"{symbol} (could not read exchange position state)")
                local_by_symbol.pop(symbol, None)
                continue
            if details.exchange_size <= 0:
                await self._trade_store.remove_open_position(symbol)
                local_by_symbol.pop(symbol, None)
                self._logger.warning(
                    "Removed stale persisted open position for %s (exchange reports flat).",
                    symbol,
                )
                continue

            envelope_problem = self._risk_envelope_problem(position)
            if envelope_problem is not None:
                unknown_exposure.append(f"{symbol} ({envelope_problem})")
                local_by_symbol.pop(symbol, None)
                continue

            mismatch = self._position_mismatch_reason(position, details)
            if mismatch is not None:
                unknown_exposure.append(f"{symbol} ({mismatch})")
                local_by_symbol.pop(symbol, None)
                continue

            self._resumable_positions.append(position)
            self._logger.warning(
                "Resuming persisted %s %s position from restart: entry=%.8f qty=%.8f order_id=%s",
                position.symbol,
                position.direction.value,
                position.entry_price,
                position.quantity,
                position.exchange_order_id,
            )

        for symbol in self._settings.symbols:
            if symbol in local_by_symbol:
                continue
            if await self._client.has_open_position(symbol):
                unknown_exposure.append(f"{symbol} (no persisted record)")

        if unknown_exposure:
            self._entries_blocked_reason = (
                "Unmatched exchange exposure: "
                + "; ".join(sorted(set(unknown_exposure)))
                + ". The bot cannot safely model these positions, so it will not trade or flatten them. "
                "Close or reconcile them manually, then restart."
            )
            self._logger.error("STARTUP ALERT: %s", self._entries_blocked_reason)

    async def _position_details(self, symbol: str):
        try:
            return await self._client.get_symbol_position_details(symbol)
        except Exception as exc:
            self._logger.error("Could not read exchange position details for %s: %s", symbol, exc)
            return None

    @staticmethod
    def _risk_envelope_problem(position: OpenPosition) -> str | None:
        """Reject a persisted position whose own SL/TP cannot be trusted.

        Resuming on the *current* configuration would silently re-price the risk
        of a trade that is already running, so a missing, non-positive, or
        non-finite envelope is unknown exposure requiring an operator, not
        something to paper over with today's settings.
        """
        for label, value in (
            ("stop_loss_pct", position.stop_loss_pct),
            ("take_profit_pct", position.take_profit_pct),
        ):
            if value is None:
                return (
                    f"persisted record has no {label}, so the risk envelope this position "
                    "was opened under is unknown"
                )
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return f"persisted {label} {value!r} is not a number"
            if not math.isfinite(numeric) or numeric <= 0:
                return f"persisted {label} {value!r} is not a valid positive percentage"
        return None

    def _position_mismatch_reason(self, position: OpenPosition, details) -> str | None:
        """Return why a persisted position does not match the exchange, or None."""
        exchange_side = str(getattr(details, "pos_side", "") or "").strip().upper()
        if exchange_side and exchange_side != position.direction.value:
            return f"persisted {position.direction.value} but exchange reports {exchange_side}"

        exchange_size = float(getattr(details, "exchange_size", 0.0) or 0.0)
        expected_size = float(position.exchange_size or position.quantity)
        if expected_size > 0:
            drift = abs(exchange_size - expected_size) / expected_size
            if drift > self._POSITION_MATCH_TOLERANCE:
                return (
                    f"persisted size {expected_size:.8f} but exchange reports {exchange_size:.8f} "
                    f"({drift * 100:.2f}% drift)"
                )
        elif exchange_size > 0:
            return f"persisted size is unknown but exchange reports {exchange_size:.8f}"

        avg_price = getattr(details, "avg_price", None)
        if avg_price and position.entry_price > 0:
            price_drift = abs(float(avg_price) - position.entry_price) / position.entry_price
            if price_drift > self._POSITION_PRICE_TOLERANCE:
                return (
                    f"persisted entry {position.entry_price:.8f} but exchange average price is "
                    f"{float(avg_price):.8f} ({price_drift * 100:.2f}% drift)"
                )

        position_id = getattr(details, "position_id", None)
        expected_id = getattr(position, "position_id", None)
        if expected_id:
            # We knew this position's venue identity at entry. If the readback no
            # longer carries it, identity can no longer be confirmed — treat that
            # as unknown exposure rather than resuming on the weaker checks alone.
            if not position_id:
                return (
                    f"persisted position id {expected_id} but the exchange readback carries no "
                    "position id, so identity cannot be confirmed"
                )
            if str(expected_id) != str(position_id):
                return f"persisted position id {expected_id} but exchange reports {position_id}"
        # A record persisted before position ids were captured has no id to check.
        # Such legacy rows are matched on direction, quantity, and entry price only;
        # they are resumable, and the first close rewrites them with an id.
        return None

    async def _resume_persisted_positions(self) -> None:
        """Take over monitoring of positions that survived a restart."""
        while self._resumable_positions:
            position = self._resumable_positions.pop(0)
            plan = self._plan_from_resumed_position(position)
            self._logger.info("Resumed monitoring for %s %s.", position.symbol, position.direction.value)
            try:
                await self._manage_open_position(
                    position,
                    plan,
                    entry_order_id=position.exchange_order_id,
                    entry_fill=None,
                    entry_fee_usdt=0.0,
                    record_entry_cooldown=False,
                )
            except Exception:
                self._logger.exception("Failed to manage resumed position for %s.", position.symbol)

    def _plan_from_resumed_position(self, position: OpenPosition) -> TradePlan:
        """Rebuild the trade plan for a resumed position.

        The original AI reasoning is not replayed, but the risk envelope is the
        one the position was actually opened under — never the current
        configuration, which may have changed across a redeploy. Reconciliation
        has already rejected any position whose persisted envelope is unusable.
        """
        problem = self._risk_envelope_problem(position)
        if problem is not None:  # pragma: no cover - reconcile filters these first
            raise RuntimeError(f"Refusing to resume {position.symbol}: {problem}")
        return TradePlan(
            symbol=position.symbol,
            direction=position.direction,
            usdt_size=self._settings.position_usdt,
            leverage=self._settings.leverage,
            stop_loss_pct=float(position.stop_loss_pct),
            take_profit_pct=float(position.take_profit_pct),
            decision_reason=f"resumed_after_restart:{position.entry_reason}",
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
            if (
                getattr(self._settings, "volume_breakeven_exit_enabled", False)
                and elapsed >= getattr(self._settings, "volume_breakeven_min_seconds", 0)
            ):
                min_profit = float(getattr(self._settings, "volume_breakeven_min_profit_usdt", 0.0) or 0.0)
                max_loss = float(getattr(self._settings, "volume_breakeven_max_loss_usdt", 0.0) or 0.0)
                if net_after_fee >= min_profit:
                    return "breakeven_exit"
                if max_loss > 0 and -max_loss <= net_after_fee < min_profit:
                    self._logger.debug(
                        "Breakeven guard waiting: net_after_fee %.4f is within allowed drift %.4f.",
                        net_after_fee,
                        max_loss,
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
    ) -> tuple[bool, OrderBookSnapshot | None]:
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

        if order_book.spread_bps > self._settings.max_spread_bps:
            self._logger.warning(
                "Skipped %s entry: spread %.2fbps > max %.2fbps.",
                plan.symbol,
                order_book.spread_bps,
                self._settings.max_spread_bps,
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

        if funding.next_funding_time is not None and self._settings.funding_blackout_minutes > 0:
            seconds_to_funding = (funding.next_funding_time - datetime.now(timezone.utc)).total_seconds()
            blackout_seconds = self._settings.funding_blackout_minutes * 60
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
        order_book: OrderBookSnapshot | None,
    ) -> str | None:
        if self._uses_post_only_entry():
            order_id = await self._place_post_only_entry(plan, exchange_size, order_book)
            if order_id is not None:
                return order_id
            if not self._allow_taker_entry_fallback():
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
        order_book: OrderBookSnapshot | None,
    ) -> str | None:
        ttl_seconds = self._maker_entry_ttl_seconds()
        attempts = max(1, self._maker_max_reprices() + 1)
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
            deadline = datetime.now(timezone.utc).timestamp() + ttl_seconds
            while datetime.now(timezone.utc).timestamp() < deadline:
                await asyncio.sleep(min(1.0, ttl_seconds))
                fill = await self._client.get_order_fill(plan.symbol, order_id)
                if fill is not None and fill.filled_size > 0:
                    if fill.state not in {"filled", None}:
                        await self._cancel_order_best_effort(plan.symbol, order_id)
                    return order_id
                if fill is not None and fill.state in terminal_states:
                    break
            await self._cancel_order_best_effort(plan.symbol, order_id)
        return None

    async def _place_post_only_exit(self, position: OpenPosition, exchange_size: float) -> str | None:
        ttl_seconds = self._maker_exit_ttl_seconds()
        attempts = max(1, self._maker_max_reprices() + 1)
        terminal_states = {"filled", "canceled", "mmp_canceled"}
        for _attempt in range(attempts):
            book = await self._client.get_order_book(
                position.symbol,
                depth=self._settings.orderbook_depth_levels,
            )
            price = book.best_ask if position.direction == PositionDirection.LONG else book.best_bid
            response = await self._client.place_limit_order(
                symbol=position.symbol,
                direction=position.direction,
                size=exchange_size,
                price=price,
                reduce_only=True,
                post_only=True,
            )
            order_id = self._extract_order_id(response)
            deadline = datetime.now(timezone.utc).timestamp() + ttl_seconds
            while datetime.now(timezone.utc).timestamp() < deadline:
                await asyncio.sleep(min(1.0, ttl_seconds))
                fill = await self._client.get_order_fill(position.symbol, order_id)
                if fill is not None and fill.filled_size > 0:
                    if fill.state not in {"filled", None}:
                        await self._cancel_order_best_effort(position.symbol, order_id)
                    return order_id
                if fill is not None and fill.state in terminal_states:
                    break
            await self._cancel_order_best_effort(position.symbol, order_id)
        return None

    async def _cancel_order_best_effort(self, symbol: str, order_id: str) -> None:
        try:
            await self._client.cancel_order(symbol, order_id)
        except Exception as exc:
            self._logger.debug("Could not cancel order %s for %s: %s", order_id, symbol, exc)

    async def _post_entry_position_guard(self, position: OpenPosition, plan: TradePlan) -> str | None:
        if self._settings.min_liquidation_distance_bps <= 0:
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
        if distance_bps < self._settings.min_liquidation_distance_bps or stop_beyond_liq:
            self._logger.warning(
                "Liquidation guard closing %s: liq=%.4f ref=%.4f distance=%.2fbps min=%.2fbps stop=%.4f.",
                position.symbol,
                details.liquidation_price,
                reference_price,
                distance_bps,
                self._settings.min_liquidation_distance_bps,
                stop_price,
            )
            return "liquidation_guard"
        return None

    def _taker_fee_pct(self, symbol: str) -> float:
        return self._taker_fee_pct_by_symbol.get(symbol, self._settings.taker_fee_pct)

    def _maker_only_mode(self) -> bool:
        return str(getattr(self._settings, "volume_execution_mode", "")).lower() == "maker_only"

    def _uses_post_only_entry(self) -> bool:
        return self._maker_only_mode() or self._settings.execution_entry_order_type == "post_only"

    def _allow_taker_entry_fallback(self) -> bool:
        return (not self._maker_only_mode()) and bool(self._settings.execution_post_only_fallback_to_market)

    def _exit_allows_taker(self, exit_reason: str) -> bool:
        if not self._maker_only_mode():
            return True
        if not bool(getattr(self._settings, "maker_allow_taker_emergency_exit", True)):
            return False
        return exit_reason in {"stop_loss", "liquidation_guard", "liquidation_guard_unverified"}

    def _maker_entry_ttl_seconds(self) -> float:
        if self._maker_only_mode():
            return float(getattr(self._settings, "maker_entry_ttl_seconds", 3.0))
        return float(self._settings.execution_post_only_timeout_seconds)

    def _maker_exit_ttl_seconds(self) -> float:
        if self._maker_only_mode():
            return float(getattr(self._settings, "maker_exit_ttl_seconds", 3.0))
        return float(getattr(self._settings, "execution_post_only_timeout_seconds", 3.0))

    def _maker_max_reprices(self) -> int:
        if self._maker_only_mode():
            return int(getattr(self._settings, "maker_max_reprices", 5))
        return int(self._settings.execution_post_only_max_reprices)

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
    def _breakeven_exit_price(
        direction: PositionDirection,
        entry_price: float,
        quantity: float,
        taker_fee_pct: float,
        target_net_usdt: float = 0.0,
    ) -> float:
        fee_rate = taker_fee_pct / 100.0
        qty = max(quantity, 1e-12)
        entry_notional = entry_price * qty
        if direction == PositionDirection.LONG:
            return (entry_notional * (1.0 + fee_rate) + target_net_usdt) / (qty * (1.0 - fee_rate))
        return (entry_notional * (1.0 - fee_rate) - target_net_usdt) / (qty * (1.0 + fee_rate))

    @staticmethod
    def _extract_order_id(response: dict) -> str:
        data = response.get("data") or {}
        if isinstance(data, list) and data:
            first = data[0] if isinstance(data[0], dict) else {}
            value = first.get("ordId") or first.get("orderId") or first.get("id") or "UNKNOWN_ORDER_ID"
            return str(value)
        if isinstance(data, dict) and isinstance(data.get("order"), dict):
            order = data["order"]
            value = order.get("ordId") or order.get("orderId") or order.get("id") or "UNKNOWN_ORDER_ID"
            return str(value)
        value = data.get("ordId") or data.get("orderId") or data.get("id") or "UNKNOWN_ORDER_ID"
        return str(value)
