from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.core.entry_window import parse_weekly_window, window_is_open
from bot.core.regime_gate import RegimeGate
from bot.exchange.contracts import (
    ExchangeAPIError,
    FreshOrderBookProvider,
    FuturesExchangeProtocol,
    MaintenanceMarginTierProvider,
    OrderBookSnapshot,
    OrderFill,
    UncertainOrderSubmissionError,
)
from bot.exchange.liquidation import estimate_isolated_liquidation_constraint, stop_loss_is_strictly_before_liquidation
from bot.models import ClosedTrade, OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import RiskManager
from bot.storage.interfaces import TradeStoreProtocol
from bot.storage.supabase_journal import SupabaseJournal
from bot.strategy.base import BaseStrategy
from bot.strategy.grok_strategy import GrokStrategy
from bot.telemetry.observer import NULL_OBSERVER, EngineObserver


class _UncertainEntryOrderState(RuntimeError):
    """A prior entry order cannot be proven flat, so no follow-up is safe."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"Prior entry order {order_id} cannot be confirmed canceled or filled.")


# How many of the newest closed trades the dashboard shows after a restart.
DASHBOARD_HISTORY_TRADES = 60


class TradeEngine:
    # Restart matching tolerances: partial fills and mark-price rounding produce
    # small legitimate drift, but anything larger means the exchange position is
    # not the one this bot persisted.
    _POSITION_MATCH_TOLERANCE = 0.02
    _POSITION_PRICE_TOLERANCE = 0.01

    # Class-level default so an engine built without __init__ (as several tests do)
    # still has a harmless observer.
    _telemetry: EngineObserver = NULL_OBSERVER

    def __init__(
        self,
        settings: Settings,
        logger: logging.Logger,
        client: FuturesExchangeProtocol,
        strategy: BaseStrategy,
        risk_manager: RiskManager,
        trade_store: TradeStoreProtocol,
        supabase_journal: SupabaseJournal | None = None,
        telemetry: EngineObserver | None = None,
        regime_gate: RegimeGate | None = None,
    ) -> None:
        self._settings = settings
        self._logger = logger
        self._client = client
        self._strategy = strategy
        self._risk_manager = risk_manager
        self._trade_store = trade_store
        self._supabase_journal = supabase_journal
        # Read-only dashboard hooks. They only observe: no decision reads their result.
        self._telemetry: EngineObserver = telemetry if telemetry is not None else NULL_OBSERVER
        self._last_entry_ts: float | None = None
        self._last_stop_loss_ts: float | None = None
        # Close time of the last trade, whatever the exit reason (pause after exit).
        self._last_exit_ts: float | None = None
        # None = the LONG forward bot's regime gate is not configured: the tick never reads it.
        self._regime_gate = regime_gate
        # Last logged state of each gate that logs on change: gate name -> blocked or not.
        self._gate_blocked: dict[str, bool] = {}
        self._taker_fee_pct_by_symbol: dict[str, float] = {}
        self._maker_fee_pct_by_symbol: dict[str, float | None] = {}
        self._entries_blocked_reason: str | None = None
        # Consecutive exit orders the venue refused outright. Not a latch:
        # a refused request placed nothing, so the position stays ours to manage.
        self._exit_rejections = 0
        # None until the first tick, so an unconfigured window never logs a transition.
        self._entry_window_open_state: bool | None = None
        self._resumable_positions: list[OpenPosition] = []

    async def run_forever(self) -> None:
        await self._trade_store.initialize()
        await self._startup_safety_checks()
        await self._refresh_exchange_fee_rates()
        await self._initialize_leverage()
        self._logger.info("%s VST trade engine started.", self._venue_label.upper())
        # Before the resumed position is watched: that call blocks until the position closes.
        await self._seed_dashboard_history()
        await self._seed_last_exit()
        await self._resume_persisted_positions()

        while True:
            try:
                await self._tick()
            except Exception:
                self._logger.exception("Unhandled error in trading loop.")
            await asyncio.sleep(self._settings.entry_interval_minutes * 60)

    def _now_in_trading_tz(self) -> datetime:
        name = str(getattr(self._settings, "trading_timezone", "") or "Asia/Jakarta")
        try:
            zone = ZoneInfo(name)
        except Exception:  # noqa: BLE001 - a misconfigured zone must not stop the loop
            zone = timezone.utc
        return datetime.now(zone)

    def _entry_window_is_open(self) -> bool:
        """Weekly entry window; logs once per open/close transition, not once per tick."""
        window_text = getattr(self._settings, "entry_weekly_window", "") or ""
        is_open = window_is_open(parse_weekly_window(window_text), self._now_in_trading_tz())
        if is_open != self._entry_window_open_state:
            if not is_open:
                self._logger.info("Entry window closed (%s): no new entries until it opens again.", window_text)
            elif self._entry_window_open_state is False:
                self._logger.info("Entry window open (%s): entries resume.", window_text)
            self._entry_window_open_state = is_open
        return is_open

    def _note_gate(self, name: str, blocked: bool, blocked_message: str, resumed_message: str) -> None:
        """Log a gate's change of state once, the way the weekly window does, not once per tick."""
        if blocked == self._gate_blocked.get(name, False):
            return
        self._gate_blocked[name] = blocked
        self._logger.info(blocked_message if blocked else resumed_message)

    async def _seed_last_exit(self) -> None:
        """Restore the pause after exit from the newest closed trade, so a restart does not forget it.

        Does nothing while the pause is off. A failing read is logged and the pause stays unset:
        startup must not depend on it."""
        if self._pause_after_exit_seconds() <= 0:
            return
        try:
            trades = await self._trade_store.recent_closed_trades(1)
        except Exception:  # noqa: BLE001 - a missing seed must never stop startup
            self._logger.warning("Could not seed the pause after exit from the store.", exc_info=True)
            return
        if trades:
            self._last_exit_ts = trades[-1].closed_at.timestamp()

    def _pause_after_exit_seconds(self) -> float:
        return float(getattr(self._settings, "cooldown_minutes_after_exit", 0) or 0) * 60

    async def _seed_dashboard_history(self) -> None:
        """Fill the dashboard's History and Trades from the store once at startup.

        The tape lives in memory, so without this a restart empties both tabs. Display only: it does nothing
        when the feed is off, and a failing read is logged and swallowed.
        """
        if self._telemetry is NULL_OBSERVER:
            return
        try:
            trades = await self._trade_store.recent_closed_trades(DASHBOARD_HISTORY_TRADES)
            realized = await self._trade_store.realized_pnl_since_cycle()
            self._telemetry.history_loaded(trades, realized_usd=realized)
        except Exception:  # noqa: BLE001 - a dashboard figure must never stop the trading loop
            self._logger.warning("Could not seed the dashboard history.", exc_info=True)

    async def _publish_volume(self) -> None:
        """Hand the dashboard the volume figures. Display only: it never raises and nothing reads it back."""
        if self._telemetry is NULL_OBSERVER:
            return
        try:
            daily = await self._trade_store.daily_volume_series()
            self._telemetry.volume_updated(
                today=self._now_in_trading_tz().date().isoformat(),
                daily=daily,
                daily_target_usdt=float(getattr(self._settings, "volume_daily_target_usdt", 0.0) or 0.0),
            )
        except Exception:  # noqa: BLE001 - a dashboard figure must never stop the trading loop
            self._logger.warning("Could not publish volume to the dashboard.", exc_info=True)

    async def _publish_usage(self) -> None:
        """Hand the dashboard the model token figures. Display only: it never raises and nothing reads it back."""
        if self._telemetry is NULL_OBSERVER:
            return
        try:
            daily = await self._trade_store.daily_ai_usage_series()
            self._telemetry.usage_updated(today=self._now_in_trading_tz().date().isoformat(), daily=daily)
        except Exception:  # noqa: BLE001 - a dashboard figure must never stop the trading loop
            self._logger.warning("Could not publish token usage to the dashboard.", exc_info=True)

    async def _tick(self) -> None:
        await self._publish_volume()
        await self._publish_usage()
        if self._entries_blocked_reason is not None:
            self._logger.error("Entries blocked: %s", self._entries_blocked_reason)
            return
        if not self._entry_window_is_open():
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

        # Pause after a close of any reason; off (0) unless COOLDOWN_MINUTES_AFTER_EXIT is set
        pause_seconds = self._pause_after_exit_seconds()
        if pause_seconds > 0 and self._last_exit_ts is not None:
            remaining = pause_seconds - (now_ts - self._last_exit_ts)
            self._note_gate(
                "pause_after_exit",
                remaining > 0,
                f"Pause after exit active ({remaining:.0f}s remaining); entries paused.",
                "Pause after exit over; entries resume.",
            )
            if remaining > 0:
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

        if self._regime_gate is not None:
            regime = await self._regime_gate.verdict()
            self._note_gate(
                "regime",
                not regime.allowed,
                f"Regime gate blocks entries ({regime.reason}); the model is not called.",
                f"Regime gate allows entries ({regime.reason}).",
            )
            if not regime.allowed:
                return
            set_gate_context = getattr(self._strategy, "set_gate_context", None)
            if set_gate_context is not None:
                set_gate_context(self._regime_gate.as_payload())

        strategy_started = time.monotonic()
        plan = await self._strategy.next_trade_plan()
        self._telemetry.decided(plan, latency_ms=int((time.monotonic() - strategy_started) * 1000))
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
        self._telemetry.entry_quoted(plan, price=entry_price, size=base_quantity, order_id=entry_order_id)
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
        self._telemetry.entry_filled(open_position, leverage=plan.leverage, fee_usdt=entry_fee_usdt)

        await self._manage_open_position_guarded(
            open_position,
            plan,
            entry_order_id=entry_order_id,
            entry_fill=entry_fill,
            entry_fee_usdt=entry_fee_usdt,
            record_entry_cooldown=True,
        )

    async def _passes_pre_order_liquidation_gate(
        self,
        plan: TradePlan,
        entry_price: float,
        quantity_base: float,
    ) -> bool:
        """Fail closed before every entry order, including deterministic plans."""
        if not isinstance(self._client, MaintenanceMarginTierProvider):
            self._logger.warning("Skipped %s entry: exchange has no maintenance-margin tier capability.", plan.symbol)
            return False
        try:
            tiers = await self._client.get_maintenance_margin_tiers(plan.symbol)
            constraint = estimate_isolated_liquidation_constraint(
                reference_price=entry_price,
                quantity_base=quantity_base,
                leverage=plan.leverage,
                taker_close_fee_rate=self._taker_fee_pct(plan.symbol) / 100.0,
                tiers=tiers,
            )
        except Exception as exc:
            self._logger.warning("Skipped %s entry: liquidation constraint unavailable. error=%s", plan.symbol, exc)
            return False
        if not stop_loss_is_strictly_before_liquidation(
            direction=plan.direction,
            stop_loss_pct=plan.stop_loss_pct,
            constraint=constraint,
        ):
            self._logger.warning(
                "Skipped %s entry: stop_loss_pct=%.6f is not strictly before estimated liquidation.",
                plan.symbol,
                plan.stop_loss_pct,
            )
            return False
        return True

    def _log_position_status(self, position: OpenPosition, *, gross: float, fee: float, net: float, basis: str) -> None:
        """One line that maps the BingX position screen onto what the exit logic sees."""
        exit_part = ""
        if self._settings.volume_neutral_exit_enabled:
            threshold = float(self._settings.volume_neutral_exit_min_net_usdt)
            as_gross = threshold if basis == "gross" else threshold + fee
            as_net = threshold - fee if basis == "gross" else threshold
            exit_part = f" | neutral exit at net {as_net:+.2f} = BingX {as_gross:+.2f}"
        self._logger.info(
            "Position %s %s | BingX unrealised (before fees) %+.2f | round-trip fee %.2f | net %+.2f%s",
            position.symbol,
            position.direction.value,
            gross,
            fee,
            net,
            exit_part,
        )

    async def _manage_open_position_guarded(
        self,
        open_position: OpenPosition,
        plan: TradePlan,
        **kwargs,
    ) -> None:
        """Manage a position without ever silently abandoning it.

        Monitoring failures are retried inside `_manage_open_position`, where no
        order has been sent yet. A failure that still escapes happened in the close
        phase, after an exit order may already exist, so retrying automatically
        could duplicate an order. Instead the venue decides: flat means nothing is
        exposed and trading continues; open or unreadable latches entries until an
        operator resolves the position.
        """
        try:
            await self._manage_open_position(open_position, plan, **kwargs)
        except Exception as exc:
            self._logger.exception("Position management for %s failed.", open_position.symbol)
            try:
                still_open: bool | None = await self._client.has_open_position(open_position.symbol)
            except Exception as check_exc:
                still_open = None
                self._logger.error(
                    "Could not read the %s position after the failure: %s", open_position.symbol, check_exc
                )
            if still_open is False:
                self._logger.warning(
                    "%s is flat on the venue after the failure; entries stay enabled, "
                    "but this trade may be missing from the trade record.",
                    open_position.symbol,
                )
                return
            state = "still open" if still_open else "unreadable"
            self._entries_blocked_reason = (
                f"position management for {open_position.symbol} failed ({exc}) and the venue position is "
                f"{state}; verify and resolve it manually before resuming entries"
            )
            self._logger.error("Entries blocked: %s", self._entries_blocked_reason)

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
        exit_reason = None
        monitor_failures = 0
        while True:
            if exit_reason is None:
                try:
                    exit_reason = await self._monitor_position(open_position, plan)
                except Exception:
                    # Returning from here hands control back to the entry loop while
                    # the position is still open and nothing is watching it. No order
                    # has been placed at this point, so monitoring again is safe.
                    monitor_failures += 1
                    self._logger.exception(
                        "Position monitor for %s failed unexpectedly (%d); resuming monitoring.",
                        open_position.symbol,
                        monitor_failures,
                    )
                    await asyncio.sleep(
                        min(self._settings.position_monitor_interval_seconds * monitor_failures, 60.0)
                    )
                    continue

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
                try:
                    exit_resp = await self._client.place_market_order(
                        symbol=open_position.symbol,
                        direction=open_position.direction,
                        size=exchange_size_to_close,
                        reduce_only=True,
                    )
                except ExchangeAPIError as exc:
                    if not self._venue_refused_the_order(exc):
                        raise
                    # The venue said no and placed nothing. Latching here would end
                    # the monitor loop that owns the stop, which is how two refused
                    # closes turned into liquidations on 2026-09-25 and 09-27. Keep
                    # the position watched and ask again.
                    self._exit_rejections += 1
                    retry_seconds = float(getattr(self._settings, "exit_rejection_retry_seconds", 15.0) or 0.0)
                    self._logger.error(
                        "Venue refused the %s exit for %s (code=%s: %s). Refusal %d in a row; "
                        "the position is still open, still monitored, and will be offered again in %.0fs.",
                        exit_reason,
                        open_position.symbol,
                        exc.code,
                        exc,
                        self._exit_rejections,
                        retry_seconds,
                    )
                    if retry_seconds > 0:
                        await asyncio.sleep(retry_seconds)
                    exit_reason = None
                    continue
                exit_order_id = self._extract_order_id(exit_resp)
                self._exit_rejections = 0
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

        self._last_exit_ts = closed_at.timestamp()
        if exit_reason == "stop_loss":
            self._last_stop_loss_ts = closed_at.timestamp()
        # Last, so every piece of engine state that gates the next entry is already set.
        self._telemetry.position_closed(closed_trade, fee_usdt=total_fee)
        await self._publish_volume()
        await self._publish_usage()

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
        # No cumulative-volume stop: only the daily target and the daily fee budget pause entries.
        # `VOLUME_TARGET_USDT` stays as a planning figure for the volume profile, not a gate.
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
            self._telemetry.position_resumed(position, leverage=plan.leverage)
            await self._manage_open_position_guarded(
                position,
                plan,
                entry_order_id=position.exchange_order_id,
                entry_fill=None,
                entry_fee_usdt=0.0,
                record_entry_cooldown=False,
            )

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
        # The model is consulted through a task rather than awaited inline. The call
        # is a network round trip (median 3.9s, p99 78.9s, 316.9s max measured on the
        # live VPS), and awaiting it here would leave _hit_stop_loss unevaluated for
        # its whole duration -- the reason 12 of 19 stop losses overshot their cap.
        exit_task: asyncio.Task | None = None
        price_failures = 0
        profit_peak: float | None = None
        last_status_log_at: float | None = None
        try:
            while True:
                try:
                    current_price = await self._client.get_last_price(position.symbol)
                except ExchangeAPIError as exc:
                    # This loop is the only stop loss the position has. A transient
                    # venue or network failure must cost one iteration, never the
                    # monitor: on 2026-09-12 a single SSL ConnectError here ended
                    # monitoring and left a LONG unwatched for hours.
                    price_failures += 1
                    log = self._logger.error if price_failures >= 3 else self._logger.warning
                    log(
                        "Price read failed for %s while monitoring an open position "
                        "(%d consecutive failures); retrying: %s",
                        position.symbol,
                        price_failures,
                        exc,
                    )
                    await asyncio.sleep(self._settings.position_monitor_interval_seconds)
                    continue
                price_failures = 0
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
                # After the stop-loss check above, so the dashboard can never sit ahead of it.
                self._telemetry.position_marked(position, net_usdt=net_after_fee)
                basis = str(getattr(self._settings, "volume_neutral_exit_basis", "net") or "net")
                # BingX shows unrealised PnL before fees; exits used to be judged only
                # on net after both taker fees, so "+50 on screen" was +32 here.
                profit_value = gross if basis == "gross" else net_after_fee
                status_every = float(getattr(self._settings, "position_status_log_seconds", 60.0) or 0.0)
                now_ts = datetime.now(timezone.utc).timestamp()
                if status_every > 0 and (last_status_log_at is None or now_ts - last_status_log_at >= status_every):
                    last_status_log_at = now_ts
                    self._log_position_status(position, gross=gross, fee=fee, net=net_after_fee, basis=basis)
                if self._settings.volume_neutral_exit_enabled and elapsed >= self._settings.volume_neutral_exit_min_seconds:
                    min_net = self._settings.volume_neutral_exit_min_net_usdt
                    mode = str(getattr(self._settings, "volume_neutral_exit_mode", "window") or "window")
                    if mode == "floor":
                        # Take the profit the first time it is seen at or above the
                        # floor, however far past the floor the price has jumped.
                        if profit_value >= min_net:
                            return "neutral_exit"
                    elif mode == "trailing":
                        # Arm at the floor, then follow the peak: exit once net falls
                        # back to the higher of the floor and `peak - trail`.
                        if profit_value >= min_net:
                            profit_peak = profit_value if profit_peak is None else max(profit_peak, profit_value)
                        if profit_peak is not None:
                            trail = float(getattr(self._settings, "volume_neutral_exit_trail_usdt", 0.0) or 0.0)
                            if profit_value <= max(min_net, profit_peak - trail):
                                return "neutral_exit"
                    else:
                        # Legacy window: only exits while a poll lands inside the band,
                        # so a move that jumps the band in either direction is missed.
                        max_net = min_net + self._settings.volume_neutral_exit_band_usdt
                        if min_net <= profit_value <= max_net:
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
                if exit_task is None:
                    exit_task = asyncio.create_task(
                        self._strategy.exit_signal(position, current_price, elapsed)
                    )
                ai_exit = None
                if exit_task.done():
                    ai_exit = self._collect_exit_signal(exit_task)
                    exit_task = None
                # gross/fee above come from this iteration's price, so a decision the
                # model made seconds ago is judged against fresh numbers.
                if ai_exit is not None:
                    honored = False
                    skip_reason: str | None = None
                    if ai_exit.close_now:
                        if gross >= fee:
                            honored = True
                        else:
                            adverse_cut = float(getattr(self._settings, "ai_exit_adverse_cut_usdt", 0.0) or 0.0)
                            if adverse_cut > 0 and net_after_fee <= -adverse_cut:
                                # Honour the AI on a loser too. Gating solely on `gross >= fee`
                                # leaves the stop loss and the time stop as the only exits from
                                # a losing trade, which lets losses run to their maximum.
                                honored = True
                                self._logger.info(
                                    "Strategy exit on adverse move: net_after_fee %.4f breached cut -%.4f.",
                                    net_after_fee, adverse_cut,
                                )
                            else:
                                skip_reason = "below_fee_and_above_cut"
                                self._logger.debug(
                                    "Strategy exit but gross %.4f < fee %.4f; holding to avoid fee loss.",
                                    gross, fee,
                                )
                    if ai_exit.ai_called:
                        await self._record_exit_verdict(
                            position,
                            ai_exit,
                            honored=honored,
                            skip_reason=skip_reason,
                            elapsed_seconds=elapsed,
                            current_price=current_price,
                            gross=gross,
                            fee=fee,
                            net_after_fee=net_after_fee,
                        )
                    if honored:
                        return f"strategy_exit:{ai_exit.reason}"
                await asyncio.sleep(self._settings.position_monitor_interval_seconds)

        finally:
            if exit_task is not None and not exit_task.done():
                exit_task.cancel()

    async def _record_exit_verdict(
        self,
        position: OpenPosition,
        signal,
        *,
        honored: bool,
        skip_reason: str | None,
        elapsed_seconds: float,
        current_price: float,
        gross: float,
        fee: float,
        net_after_fee: float,
    ) -> None:
        """Persist what the model asked for and whether the engine acted on it.

        Instrumentation only: a storage failure must never take down the monitor
        loop that is protecting an open position.
        """
        try:
            await self._trade_store.save_exit_verdict(
                symbol=position.symbol,
                direction=position.direction.value,
                close_now=bool(signal.close_now),
                reason=str(signal.reason),
                confidence=signal.confidence,
                honored=honored,
                skip_reason=skip_reason,
                elapsed_seconds=round(float(elapsed_seconds), 1),
                entry_price=float(position.entry_price),
                current_price=float(current_price),
                gross_usdt=float(gross),
                fee_usdt=float(fee),
                net_after_fee_usdt=float(net_after_fee),
            )
        except Exception as exc:  # noqa: BLE001 - never break position monitoring
            self._logger.warning("Failed to persist AI exit verdict for %s: %s", position.symbol, exc)

    def _collect_exit_signal(self, task: asyncio.Task):
        """Read a finished exit task, swallowing provider failures.

        Letting the exception escape would unwind _monitor_position through _tick
        to run_forever and leave the open position unmanaged, which is far worse
        than missing one AI opinion.
        """
        try:
            return task.result()
        except asyncio.CancelledError:
            return None
        except Exception:
            self._logger.exception("AI exit evaluation failed; continuing to monitor the position.")
            return None

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
            try:
                order_id = await self._place_post_only_entry(plan, exchange_size, order_book)
            except (_UncertainEntryOrderState, UncertainOrderSubmissionError) as exc:
                self._block_entries_for_uncertain_order(exc, plan.symbol)
                return None
            if order_id is not None:
                return order_id
            if not self._allow_taker_entry_fallback():
                self._logger.info("Skipped %s entry: post-only order did not fill.", plan.symbol)
                return None
            self._logger.warning("Post-only entry for %s did not fill; falling back to market order.", plan.symbol)

        candidate_price = await self._market_entry_candidate_price(plan)
        if candidate_price is None:
            return None
        if not await self._passes_last_mile_liquidation_gate(plan, exchange_size, candidate_price):
            return None
        try:
            entry_resp = await self._client.place_market_order(
                symbol=plan.symbol,
                direction=plan.direction,
                size=exchange_size,
                reduce_only=False,
            )
            return self._extract_entry_order_id(entry_resp, plan.symbol)
        except UncertainOrderSubmissionError as exc:
            self._block_entries_for_uncertain_order(exc, plan.symbol)
            return None

    def _block_entries_for_uncertain_order(
        self,
        exc: _UncertainEntryOrderState | UncertainOrderSubmissionError,
        fallback_symbol: str,
    ) -> None:
        """Latch entry processing when an existing intent may still be live."""
        if isinstance(exc, UncertainOrderSubmissionError):
            symbol = exc.symbol or fallback_symbol
            order_identity = exc.order_id or exc.client_order_id
            state = "accepted or rejected"
        else:
            symbol = fallback_symbol
            order_identity = exc.order_id
            state = "canceled or filled"
        self._entries_blocked_reason = (
            f"Entry order {order_identity} for {symbol} could not be confirmed {state}; "
            "manual reconciliation is required before restart."
        )
        self._logger.error("Entries blocked: %s", self._entries_blocked_reason)

    async def _place_post_only_entry(
        self,
        plan: TradePlan,
        exchange_size: float,
        order_book: OrderBookSnapshot | None,
    ) -> str | None:
        ttl_seconds = self._maker_entry_ttl_seconds()
        attempts = max(1, self._maker_max_reprices() + 1)
        canceled_states = {"canceled", "mmp_canceled"}
        for attempt in range(attempts):
            book = await self._fresh_entry_order_book(plan)
            if book is None:
                return None
            price = book.best_bid if plan.direction == PositionDirection.LONG else book.best_ask
            if not math.isfinite(price) or price <= 0:
                self._logger.warning("Skipped %s entry: fresh executable-side quote is invalid.", plan.symbol)
                return None
            if not await self._passes_last_mile_liquidation_gate(plan, exchange_size, price):
                return None
            response = await self._client.place_limit_order(
                symbol=plan.symbol,
                direction=plan.direction,
                size=exchange_size,
                price=price,
                reduce_only=False,
                post_only=True,
            )
            order_id = self._extract_entry_order_id(response, plan.symbol)
            deadline = datetime.now(timezone.utc).timestamp() + ttl_seconds
            confirmed_canceled = False
            while datetime.now(timezone.utc).timestamp() < deadline:
                await asyncio.sleep(min(1.0, ttl_seconds))
                try:
                    fill = await self._client.get_order_fill(plan.symbol, order_id)
                except Exception as exc:
                    self._logger.warning(
                        "Could not poll post-only entry %s for %s; resolving before any retry. error=%s",
                        order_id,
                        plan.symbol,
                        exc,
                    )
                    break
                if fill is None:
                    continue
                if not self._is_proven_entry_fill(fill, order_id):
                    break
                state = (fill.state or "").lower()
                if state == "filled":
                    return order_id
                if fill.filled_size > 0:
                    if state in canceled_states:
                        # The exchange confirms the unfilled remainder is inactive.
                        return order_id
                    # A non-terminal partial fill may still have a live remainder.
                    break
                if state in canceled_states:
                    confirmed_canceled = True
                    break
            if confirmed_canceled:
                continue
            resolution = await self._cancel_and_resolve_entry_order(plan.symbol, order_id)
            if resolution == "filled":
                return order_id
            if resolution != "canceled":
                raise _UncertainEntryOrderState(order_id)
        return None

    async def _cancel_and_resolve_entry_order(self, symbol: str, order_id: str) -> str:
        """Return canceled, filled, or uncertain; only canceled permits another entry."""
        try:
            response = await self._client.cancel_order(symbol, order_id)
        except Exception as exc:
            self._logger.warning("Skipped entry retry for %s: cancel of %s failed. error=%s", symbol, order_id, exc)
            return "uncertain"
        if not isinstance(response, dict):
            self._logger.warning("Skipped entry retry for %s: cancel of %s returned an ambiguous response.", symbol, order_id)
            return "uncertain"
        try:
            fill = await self._client.get_order_fill(symbol, order_id)
        except Exception as exc:
            self._logger.warning("Skipped entry retry for %s: cannot confirm cancellation of %s. error=%s", symbol, order_id, exc)
            return "uncertain"
        if fill is None:
            self._logger.warning("Skipped entry retry for %s: cancellation of %s is unconfirmed.", symbol, order_id)
            return "uncertain"
        if not self._is_proven_entry_fill(fill, order_id):
            return "uncertain"
        state = (fill.state or "").lower()
        if state == "filled":
            return "filled"
        if state in {"canceled", "mmp_canceled"}:
            # A canceled order is terminal: preserve a partial fill for normal
            # position readback, but allow a retry only when it filled nothing.
            return "filled" if fill.filled_size > 0 else "canceled"
        if fill.filled_size > 0:
            self._logger.warning(
                "Skipped entry retry for %s: %s has a partial fill with an active or unknown remainder.",
                symbol,
                order_id,
            )
            return "uncertain"
        self._logger.warning("Skipped entry retry for %s: cancellation of %s remains non-terminal.", symbol, order_id)
        return "uncertain"

    def _is_proven_entry_fill(self, fill: OrderFill, expected_order_id: str) -> bool:
        """Validate the minimum evidence needed to safely retry a canceled entry."""
        if not isinstance(fill.order_id, str) or not fill.order_id.strip() or fill.order_id != expected_order_id:
            self._logger.warning("Skipped entry retry: cancellation fill identity is missing or mismatched for %s.", expected_order_id)
            return False
        try:
            quantity = float(fill.filled_size)
        except (TypeError, ValueError):
            self._logger.warning("Skipped entry retry: cancellation fill quantity is invalid for %s.", expected_order_id)
            return False
        if not math.isfinite(quantity) or quantity < 0:
            self._logger.warning("Skipped entry retry: cancellation fill quantity is invalid for %s.", expected_order_id)
            return False
        return True

    async def _passes_last_mile_liquidation_gate(
        self,
        plan: TradePlan,
        exchange_size: float,
        candidate_price: float,
    ) -> bool:
        """Prove this exact entry attempt is safe immediately before placement."""
        try:
            quantity_base = await self._client.estimate_base_quantity(plan.symbol, exchange_size)
        except Exception as exc:
            self._logger.warning(
                "Skipped %s entry: exchange size cannot be resolved for liquidation proof. error=%s",
                plan.symbol,
                exc,
            )
            return False
        if not math.isfinite(quantity_base) or quantity_base <= 0:
            self._logger.warning("Skipped %s entry: exchange size cannot be converted to positive base quantity.", plan.symbol)
            return False
        return await self._passes_pre_order_liquidation_gate(plan, candidate_price, quantity_base)

    async def _market_entry_candidate_price(self, plan: TradePlan) -> float | None:
        """Use only a fresh executable-side quote for a market-entry proof."""
        book = await self._fresh_entry_order_book(plan)
        if book is None:
            return None
        candidate = book.best_ask if plan.direction == PositionDirection.LONG else book.best_bid
        if not math.isfinite(candidate) or candidate <= 0:
            self._logger.warning("Skipped %s entry: fresh executable-side quote is invalid.", plan.symbol)
            return None
        return candidate

    async def _fresh_entry_order_book(self, plan: TradePlan) -> OrderBookSnapshot | None:
        if not isinstance(self._client, FreshOrderBookProvider):
            self._logger.warning("Skipped %s entry: exchange has no uncached order-book capability.", plan.symbol)
            return None
        try:
            return await self._client.get_fresh_order_book(plan.symbol, depth=self._settings.orderbook_depth_levels)
        except Exception as exc:
            self._logger.warning("Skipped %s entry: fresh order-book quote unavailable. error=%s", plan.symbol, exc)
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

    def _taker_fee_pct(self, symbol: str) -> float:
        return self._taker_fee_pct_by_symbol.get(symbol, self._settings.taker_fee_pct)

    def _maker_only_mode(self) -> bool:
        return str(getattr(self._settings, "volume_execution_mode", "")).lower() == "maker_only"

    def _uses_post_only_entry(self) -> bool:
        return self._maker_only_mode() or self._settings.execution_entry_order_type == "post_only"

    def _allow_taker_entry_fallback(self) -> bool:
        return (not self._maker_only_mode()) and bool(self._settings.execution_post_only_fallback_to_market)

    @staticmethod
    def _venue_refused_the_order(exc: ExchangeAPIError) -> bool:
        """True when the venue rejected the request outright, so no order exists.

        A business `code` with `retryable=False` is BingX answering "no" — the
        order never reached the book, so sending it again later cannot duplicate
        anything. `UncertainOrderSubmissionError` is the opposite case (an order
        may exist) and a local validation failure carries no venue code at all;
        both keep the old behaviour of latching entries for a human.
        """
        return (
            not isinstance(exc, UncertainOrderSubmissionError)
            and not exc.retryable
            and bool(exc.code)
        )

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
            value = first.get("ordId") or first.get("orderId") or first.get("orderID") or first.get("id") or "UNKNOWN_ORDER_ID"
            return str(value)
        if isinstance(data, dict) and isinstance(data.get("order"), dict):
            order = data["order"]
            value = order.get("ordId") or order.get("orderId") or order.get("orderID") or order.get("id") or "UNKNOWN_ORDER_ID"
            return str(value)
        value = data.get("ordId") or data.get("orderId") or data.get("orderID") or data.get("id") or "UNKNOWN_ORDER_ID"
        return str(value)

    @classmethod
    def _extract_entry_order_id(cls, response: dict, symbol: str) -> str:
        data = response.get("data")
        rows: list[dict] = []
        if isinstance(data, dict):
            nested = data.get("order")
            if isinstance(nested, dict):
                rows.append(nested)
            rows.append(data)
            orders = data.get("orders")
            if isinstance(orders, list) and orders and isinstance(orders[0], dict):
                rows.append(orders[0])
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            rows.append(data[0])
        for row in rows:
            value = row.get("orderId") or row.get("orderID")
            if value not in (None, "") and str(value).strip():
                return str(value).strip()
        raise UncertainOrderSubmissionError(
            "Entry acknowledgement has no supported order identity.",
            symbol=symbol,
            client_order_id="unavailable",
            venue="bingx",
        )
