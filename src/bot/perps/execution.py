from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import ValidationError

from bot.perps.config import PerpsConfig
from bot.perps.exchange_adapter import (
    ExchangeAdapter,
    InstrumentMetadata,
    MarketSnapshot,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderStatusValue,
    OrderType,
    PositionSnapshot,
    opposite_exit_side,
)
from bot.perps.risk import EntryRiskContext, PerpsRiskEngine, RiskLedger, SizingResult
from bot.perps.signal import NormalizedSignal
from bot.perps.structured_logging import PerpsJsonLogger


class ExecutionOutcome(str, Enum):
    ENTERED = "entered"
    REJECTED = "rejected"
    CANCELED_UNFILLED = "canceled_unfilled"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    outcome: ExecutionOutcome
    reason: str
    signal: NormalizedSignal | None = None
    sizing: SizingResult | None = None
    entry_order_id: str | None = None
    take_order_id: str | None = None
    stop_order_id: str | None = None
    filled_qty: Decimal = Decimal("0")
    avg_entry_price: Decimal | None = None


@dataclass(slots=True)
class ExecutionLedger:
    realized_pnl_today: Decimal = Decimal("0")
    rolling_pnl: Decimal = Decimal("0")
    fee_spent_today: Decimal = Decimal("0")
    consecutive_losses: int = 0
    cooldown_until: datetime | None = None
    position_desync: bool = False
    open_positions: tuple[PositionSnapshot, ...] = field(default_factory=tuple)


class PerpExecutionManager:
    """Maker-first perpetual futures execution state machine."""

    def __init__(
        self,
        *,
        config: PerpsConfig,
        adapter: ExchangeAdapter,
        logger: logging.Logger,
        risk_engine: PerpsRiskEngine | None = None,
    ) -> None:
        self._config = config
        self._adapter = adapter
        self._risk = risk_engine or PerpsRiskEngine(config)
        self._log = PerpsJsonLogger(logger)
        self._ledger = ExecutionLedger()

    async def reconcile_on_startup(self) -> tuple[PositionSnapshot, ...]:
        positions = tuple(await self._adapter.get_open_positions())
        open_orders = await self._adapter.get_open_orders()
        self._ledger.open_positions = positions
        self._ledger.position_desync = len(positions) > self._config.max_open_positions
        self._log.emit(
            "reconciliation",
            status="desync" if self._ledger.position_desync else "ok",
            position_reconciled=not self._ledger.position_desync,
            kill_switch_reason="position_order_desync" if self._ledger.position_desync else None,
            pnl_unrealized=float(sum((p.unrealized_pnl for p in positions), Decimal("0"))),
            client_order_id=f"open_orders:{len(open_orders)}",
        )
        return positions

    async def process_signal(self, payload: dict[str, Any] | NormalizedSignal) -> ExecutionResult:
        try:
            signal = payload if isinstance(payload, NormalizedSignal) else NormalizedSignal.model_validate(payload)
        except ValidationError as exc:
            self._log.emit("signal_rejected", status="schema_invalid", kill_switch_reason="invalid_signal_schema")
            return ExecutionResult(ExecutionOutcome.REJECTED, f"invalid_signal_schema:{exc}")

        if signal.side == "flat":
            self._log_signal(signal, "signal_flat", "rejected")
            return ExecutionResult(ExecutionOutcome.REJECTED, "signal_flat", signal=signal)

        instrument = await self._adapter.get_instrument_metadata(signal.symbol)
        snapshot = await self._adapter.get_market_snapshot(signal.symbol)
        health = await self._adapter.get_health()
        equity = await self._adapter.get_account_equity()
        positions = tuple(await self._adapter.get_open_positions())
        self._ledger.open_positions = positions

        risk_check = self._risk.evaluate_entry(
            EntryRiskContext(
                signal=signal,
                snapshot=snapshot,
                health=health,
                ledger=RiskLedger(
                    equity_usdt=equity,
                    realized_pnl_today=self._ledger.realized_pnl_today,
                    rolling_pnl=self._ledger.rolling_pnl,
                    fee_spent_today=self._ledger.fee_spent_today,
                    consecutive_losses=self._ledger.consecutive_losses,
                    open_positions=positions,
                    cooldown_until=self._ledger.cooldown_until,
                    position_desync=self._ledger.position_desync,
                ),
            )
        )
        if not risk_check.allowed:
            self._log_signal(signal, risk_check.reason_text or "risk_rejected", "rejected", snapshot=snapshot, health=health)
            return ExecutionResult(ExecutionOutcome.REJECTED, risk_check.reason_text or "risk_rejected", signal=signal)

        sizing = self._risk.size_order(
            signal=signal,
            instrument=instrument,
            snapshot=snapshot,
            equity_usdt=equity,
            requested_leverage=self._config.default_leverage,
        )
        if not sizing.accepted:
            self._log_signal(signal, sizing.rejected_reason or "sizing_rejected", "rejected", snapshot=snapshot, sizing=sizing)
            return ExecutionResult(
                ExecutionOutcome.REJECTED,
                sizing.rejected_reason or "sizing_rejected",
                signal=signal,
                sizing=sizing,
            )

        await self._adapter.set_margin_mode(signal.symbol, "isolated")
        await self._adapter.set_leverage(signal.symbol, sizing.leverage)

        entry_status = await self._place_maker_entry(signal, instrument, snapshot, sizing)
        if entry_status.status not in {OrderStatusValue.FILLED, OrderStatusValue.PARTIALLY_FILLED}:
            self._log_signal(signal, "entry_unfilled", "canceled", snapshot=snapshot, sizing=sizing, entry_status=entry_status)
            return ExecutionResult(
                ExecutionOutcome.CANCELED_UNFILLED,
                "entry_unfilled",
                signal=signal,
                sizing=sizing,
                entry_order_id=entry_status.exchange_order_id,
            )

        filled_qty = entry_status.filled_qty
        avg_price = entry_status.avg_price or Decimal(str(snapshot.mark))
        realized_slippage_bps = self._realized_entry_slippage_bps(signal, snapshot, avg_price)
        if realized_slippage_bps > self._config.max_abnormal_slippage_bps:
            positions_after_fill = await self._adapter.get_open_positions()
            for position in positions_after_fill:
                if position.symbol == signal.symbol:
                    await self.emergency_market_close(position, "abnormal_slippage")
                    break
            self._log_signal(
                signal,
                "abnormal_slippage",
                "emergency_closed",
                snapshot=snapshot,
                sizing=sizing,
                entry_status=entry_status,
                entry_price=avg_price,
                qty=filled_qty,
                realized_fee=entry_status.realized_fee,
                realized_slippage_bps=realized_slippage_bps,
            )
            return ExecutionResult(
                ExecutionOutcome.ERROR,
                "abnormal_slippage",
                signal=signal,
                sizing=sizing,
                entry_order_id=entry_status.exchange_order_id,
                filled_qty=filled_qty,
                avg_entry_price=avg_price,
            )
        take_ack, stop_ack = await self._attach_reduce_only_exits(signal, instrument, filled_qty, avg_price, sizing)
        self._ledger.fee_spent_today += entry_status.realized_fee
        self._ledger.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=self._config.cooldown_seconds)

        self._log_signal(
            signal,
            "entered",
            "filled",
            snapshot=snapshot,
            sizing=sizing,
            entry_status=entry_status,
            entry_price=avg_price,
            qty=filled_qty,
            client_order_id=entry_status.client_order_id,
            exchange_order_id=entry_status.exchange_order_id,
            realized_fee=entry_status.realized_fee,
            realized_slippage_bps=realized_slippage_bps,
        )
        return ExecutionResult(
            ExecutionOutcome.ENTERED,
            "entered",
            signal=signal,
            sizing=sizing,
            entry_order_id=entry_status.exchange_order_id,
            take_order_id=take_ack.exchange_order_id,
            stop_order_id=stop_ack.exchange_order_id,
            filled_qty=filled_qty,
            avg_entry_price=avg_price,
        )

    async def emergency_market_close(self, position: PositionSnapshot, reason: str) -> OrderStatus:
        side = opposite_exit_side(position.side)
        client_order_id = self._client_order_id(position.symbol, "emergency")
        ack = await self._adapter.place_order(
            OrderRequest(
                symbol=position.symbol,
                side=side,
                order_type=OrderType.MARKET,
                qty=abs(position.qty),
                reduce_only=True,
                post_only=False,
                client_order_id=client_order_id,
                leverage=position.leverage,
                metadata={"reason": reason},
            )
        )
        status = await self._adapter.get_order_status(position.symbol, ack.exchange_order_id)
        self._log.emit(
            "emergency_close",
            symbol=position.symbol,
            side=position.side,
            qty=status.filled_qty,
            order_type=OrderType.MARKET.value,
            reduce_only=True,
            post_only=False,
            client_order_id=client_order_id,
            exchange_order_id=ack.exchange_order_id,
            status=status.status.value,
            kill_switch_reason=reason,
            realized_fee=status.realized_fee,
        )
        return status

    async def emit_heartbeat(self) -> None:
        health = await self._adapter.get_health()
        positions = await self._adapter.get_open_positions()
        snapshots = [await self._adapter.get_market_snapshot(symbol) for symbol in self._config.symbols]
        max_age = max((snapshot.age_seconds for snapshot in snapshots), default=None)
        self._log.emit(
            "heartbeat",
            status="ok" if health.can_open_new_positions else "degraded",
            api_latency_ms=health.api_latency_ms,
            order_ack_latency_ms=health.order_ack_latency_ms,
            websocket_freshness_seconds=max_age,
            position_reconciled=health.position_reconciled,
            pnl_unrealized=float(sum((p.unrealized_pnl for p in positions), Decimal("0"))),
            kill_switch_reason=None if health.can_open_new_positions else health.reason,
        )

    async def _place_maker_entry(
        self,
        signal: NormalizedSignal,
        instrument: InstrumentMetadata,
        snapshot: MarketSnapshot,
        sizing: SizingResult,
    ) -> OrderStatus:
        entry_side = OrderSide.BUY if signal.side == "long" else OrderSide.SELL
        last_status: OrderStatus | None = None
        for attempt in range(self._config.max_reprice_count + 1):
            current_snapshot = snapshot if attempt == 0 else await self._adapter.get_market_snapshot(signal.symbol)
            price = current_snapshot.bid if entry_side == OrderSide.BUY else current_snapshot.ask
            rounded_price = instrument.round_price(price, entry_side)
            client_order_id = self._client_order_id(signal.symbol, f"entry-{attempt}")
            ack = await self._adapter.place_order(
                OrderRequest(
                    symbol=signal.symbol,
                    side=entry_side,
                    order_type=OrderType.LIMIT,
                    qty=sizing.qty,
                    price=rounded_price,
                    post_only=True,
                    reduce_only=False,
                    client_order_id=client_order_id,
                    leverage=sizing.leverage,
                    metadata={"timeframe": signal.timeframe, "attempt": attempt},
                )
            )
            self._log_signal(
                signal,
                "entry_order_placed",
                ack.status.value,
                snapshot=current_snapshot,
                sizing=sizing,
                entry_price=rounded_price,
                client_order_id=client_order_id,
                exchange_order_id=ack.exchange_order_id,
                post_only=True,
                reduce_only=False,
                order_type=OrderType.LIMIT.value,
            )
            last_status = await self._wait_for_fill(signal.symbol, ack.exchange_order_id)
            if last_status.status in {OrderStatusValue.FILLED, OrderStatusValue.PARTIALLY_FILLED}:
                return last_status
            await self._adapter.cancel_order(signal.symbol, ack.exchange_order_id)
        if last_status is None:
            raise RuntimeError("Entry order loop ended without an order status.")
        return last_status

    async def _wait_for_fill(self, symbol: str, exchange_order_id: str) -> OrderStatus:
        deadline = time.monotonic() + self._config.entry_timeout_seconds
        last_status: OrderStatus | None = None
        while time.monotonic() <= deadline:
            status = await self._adapter.get_order_status(symbol, exchange_order_id)
            last_status = status
            if status.status == OrderStatusValue.FILLED:
                return status
            if status.status == OrderStatusValue.PARTIALLY_FILLED and status.filled_qty > 0:
                return status
            if status.is_terminal:
                return status
            if self._config.poll_interval_seconds > 0:
                await asyncio.sleep(self._config.poll_interval_seconds)
            else:
                break
        if last_status is None:
            last_status = await self._adapter.get_order_status(symbol, exchange_order_id)
        return last_status

    async def _attach_reduce_only_exits(
        self,
        signal: NormalizedSignal,
        instrument: InstrumentMetadata,
        filled_qty: Decimal,
        avg_entry_price: Decimal,
        sizing: SizingResult,
    ):
        exit_side = OrderSide.SELL if signal.side == "long" else OrderSide.BUY
        one = Decimal("1")
        take_delta = Decimal(str(signal.take_bps / 10_000.0))
        stop_delta = Decimal(str(signal.stop_bps / 10_000.0))
        if signal.side == "long":
            take_price = avg_entry_price * (one + take_delta)
            stop_price = avg_entry_price * (one - stop_delta)
        else:
            take_price = avg_entry_price * (one - take_delta)
            stop_price = avg_entry_price * (one + stop_delta)
        rounded_take = instrument.round_price(take_price, exit_side)
        rounded_stop = instrument.round_price(stop_price, exit_side)

        take_client_id = self._client_order_id(signal.symbol, "take")
        stop_client_id = self._client_order_id(signal.symbol, "stop")
        take_ack = await self._adapter.place_order(
            OrderRequest(
                symbol=signal.symbol,
                side=exit_side,
                order_type=OrderType.LIMIT,
                qty=filled_qty,
                price=rounded_take,
                post_only=False,
                reduce_only=True,
                client_order_id=take_client_id,
                leverage=sizing.leverage,
                metadata={"exit": "take_profit"},
            )
        )
        stop_ack = await self._adapter.place_order(
            OrderRequest(
                symbol=signal.symbol,
                side=exit_side,
                order_type=OrderType.STOP_MARKET,
                qty=filled_qty,
                stop_price=rounded_stop,
                post_only=False,
                reduce_only=True,
                client_order_id=stop_client_id,
                leverage=sizing.leverage,
                metadata={"exit": "stop_loss"},
            )
        )
        self._log_signal(
            signal,
            "exits_attached",
            "open",
            sizing=sizing,
            entry_price=avg_entry_price,
            stop_price=rounded_stop,
            take_price=rounded_take,
            qty=filled_qty,
            order_type="oco_reduce_only",
            reduce_only=True,
            post_only=False,
        )
        return take_ack, stop_ack

    def _log_signal(
        self,
        signal: NormalizedSignal,
        reason: str,
        status: str,
        *,
        snapshot: MarketSnapshot | None = None,
        health=None,
        sizing: SizingResult | None = None,
        entry_status: OrderStatus | None = None,
        entry_price: Decimal | float | None = None,
        stop_price: Decimal | float | None = None,
        take_price: Decimal | float | None = None,
        qty: Decimal | None = None,
        client_order_id: str | None = None,
        exchange_order_id: str | None = None,
        realized_fee: Decimal | None = None,
        realized_slippage_bps: float | None = None,
        order_type: str | None = None,
        post_only: bool | None = None,
        reduce_only: bool | None = None,
    ) -> None:
        self._log.emit(
            "perp_execution",
            symbol=signal.symbol,
            timeframe=signal.timeframe,
            side=signal.side,
            confidence=signal.confidence,
            expected_move_bps=signal.expected_move_bps,
            spread_bps=snapshot.spread_bps if snapshot else None,
            entry_price=entry_price,
            stop_price=stop_price,
            take_price=take_price,
            qty=qty or (sizing.qty if sizing else None),
            notional=sizing.notional if sizing else None,
            leverage=sizing.leverage if sizing else None,
            margin_used=sizing.margin_used if sizing else None,
            projected_round_trip_volume=sizing.projected_round_trip_volume if sizing else None,
            projected_fee=sizing.projected_fee if sizing else None,
            realized_fee=realized_fee or (entry_status.realized_fee if entry_status else None),
            projected_slippage_bps=self._config.slippage_round_trip * 10_000.0,
            realized_slippage_bps=realized_slippage_bps,
            funding_flag=self._funding_flag(snapshot),
            order_type=order_type,
            post_only=post_only,
            reduce_only=reduce_only,
            client_order_id=client_order_id or (entry_status.client_order_id if entry_status else None),
            exchange_order_id=exchange_order_id or (entry_status.exchange_order_id if entry_status else None),
            status=status,
            kill_switch_reason=reason if status in {"rejected", "emergency_closed"} else None,
            api_latency_ms=getattr(health, "api_latency_ms", None),
            order_ack_latency_ms=getattr(health, "order_ack_latency_ms", None),
            websocket_freshness_seconds=snapshot.age_seconds if snapshot else None,
            position_reconciled=getattr(health, "position_reconciled", None),
        )

    def _funding_flag(self, snapshot: MarketSnapshot | None) -> bool | None:
        if snapshot is None or snapshot.next_funding_time is None:
            return None
        window_seconds = self._config.funding_blackout_minutes * 60
        return abs((snapshot.next_funding_time - datetime.now(timezone.utc)).total_seconds()) <= window_seconds

    @staticmethod
    def _realized_entry_slippage_bps(
        signal: NormalizedSignal,
        snapshot: MarketSnapshot,
        avg_price: Decimal,
    ) -> float:
        intended = Decimal(str(snapshot.bid if signal.side == "long" else snapshot.ask))
        if intended <= 0:
            return float("inf")
        return float(abs(avg_price - intended) / intended * Decimal("10000"))

    @staticmethod
    def _client_order_id(symbol: str, kind: str) -> str:
        clean = symbol.replace("/", "").lower()
        return f"perp-{clean}-{kind}-{int(time.time() * 1000)}"
