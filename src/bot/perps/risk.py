from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot.perps.config import PerpsConfig
from bot.perps.exchange_adapter import HealthStatus, InstrumentMetadata, MarketSnapshot, PositionSnapshot
from bot.perps.signal import NormalizedSignal


@dataclass(frozen=True, slots=True)
class SizingResult:
    accepted: bool
    rejected_reason: str | None
    n_fee: Decimal
    n_risk: Decimal
    n_liq: Decimal
    n_exchange_limits: Decimal
    n_margin: Decimal
    n_final: Decimal
    qty: Decimal
    notional: Decimal
    leverage: int
    margin_used: Decimal
    projected_round_trip_volume: Decimal
    projected_fee: Decimal
    projected_loss_at_stop: Decimal
    projected_net_profit_at_target: Decimal
    liquidation_distance_bps: float
    expected_cost_bps: float


@dataclass(frozen=True, slots=True)
class RiskLedger:
    equity_usdt: Decimal
    realized_pnl_today: Decimal = Decimal("0")
    rolling_pnl: Decimal = Decimal("0")
    fee_spent_today: Decimal = Decimal("0")
    consecutive_losses: int = 0
    open_positions: tuple[PositionSnapshot, ...] = ()
    cooldown_until: datetime | None = None
    position_desync: bool = False


@dataclass(frozen=True, slots=True)
class RiskCheckResult:
    allowed: bool
    reasons: tuple[str, ...] = ()

    @property
    def reason_text(self) -> str | None:
        return ",".join(self.reasons) if self.reasons else None


@dataclass(frozen=True, slots=True)
class EntryRiskContext:
    signal: NormalizedSignal
    snapshot: MarketSnapshot
    health: HealthStatus
    ledger: RiskLedger
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PerpsRiskEngine:
    def __init__(self, config: PerpsConfig) -> None:
        self._config = config

    def evaluate_entry(self, context: EntryRiskContext) -> RiskCheckResult:
        reasons: list[str] = []
        signal = context.signal
        snapshot = context.snapshot
        ledger = context.ledger
        health = context.health

        if signal.side == "flat":
            reasons.append("signal_flat")
        if not health.websocket_healthy:
            reasons.append("websocket_unhealthy")
        if not health.market_data_fresh:
            reasons.append("market_data_unhealthy")
        if snapshot.age_seconds > self._config.stale_market_data_seconds:
            reasons.append("stale_market_data")
        if not health.position_reconciled or ledger.position_desync:
            reasons.append("position_order_desync")
        if health.rate_limit_remaining_pct <= self._config.min_rate_limit_remaining_pct:
            reasons.append("rate_limit_near_exhaustion")
        if snapshot.spread_bps > self._config.max_spread_bps:
            reasons.append("spread_too_wide")
        if len(ledger.open_positions) >= self._config.max_open_positions:
            reasons.append("max_open_exposure")
        if ledger.cooldown_until is not None and context.now < ledger.cooldown_until:
            reasons.append("cooldown_active")
        if self._inside_funding_blackout(snapshot, context.now):
            reasons.append("funding_blackout")

        equity = ledger.equity_usdt
        daily_limit = equity * Decimal(str(self._config.max_daily_realized_drawdown_pct))
        rolling_limit = equity * Decimal(str(self._config.max_rolling_drawdown_pct))
        fee_budget = equity * Decimal(str(self._config.daily_fee_budget_pct))

        if ledger.realized_pnl_today <= -daily_limit:
            reasons.append("daily_realized_drawdown_breach")
        if ledger.rolling_pnl <= -rolling_limit:
            reasons.append("rolling_drawdown_breach")
        if ledger.consecutive_losses >= self._config.max_consecutive_losses:
            reasons.append("too_many_consecutive_losses")
        if ledger.fee_spent_today >= fee_budget:
            reasons.append("fee_budget_breach")

        return RiskCheckResult(allowed=not reasons, reasons=tuple(reasons))

    def size_order(
        self,
        *,
        signal: NormalizedSignal,
        instrument: InstrumentMetadata,
        snapshot: MarketSnapshot,
        equity_usdt: Decimal,
        requested_leverage: int | None = None,
    ) -> SizingResult:
        leverage = self._resolve_leverage(instrument, requested_leverage)
        zero = Decimal("0")

        stop_distance = Decimal(str(signal.stop_bps / 10_000.0))
        take_distance = Decimal(str(signal.take_bps / 10_000.0))
        fee_round_trip = Decimal(str(self._config.fee_round_trip))
        slippage_round_trip = Decimal(str(self._config.slippage_round_trip))
        edge_cost_bps = self._config.round_trip_cost_bps + self._config.edge_buffer_bps

        if self._config.sizing_mode == "risk" and signal.expected_move_bps <= edge_cost_bps:
            return self._rejected(
                reason=(
                    f"expected_move_bps {signal.expected_move_bps:.2f} <= "
                    f"cost_plus_edge_bps {edge_cost_bps:.2f}"
                ),
                leverage=leverage,
                expected_cost_bps=edge_cost_bps,
            )
        if stop_distance <= 0 or take_distance <= 0:
            return self._rejected("invalid_stop_or_take", leverage, edge_cost_bps)
        if instrument.margin_mode.lower() != "isolated":
            return self._rejected("exchange_margin_mode_not_isolated", leverage, edge_cost_bps)

        n_fee = (
            equity_usdt
            * Decimal(str(self._config.daily_fee_budget_pct))
            / Decimal(str(self._config.target_trades_per_day))
            / fee_round_trip
        )
        n_risk = (
            equity_usdt
            * Decimal(str(self._config.risk_per_trade_pct))
            / (stop_distance + fee_round_trip + slippage_round_trip)
        )
        n_liq = equity_usdt * Decimal(leverage) * Decimal(str(self._config.liquidation_notional_buffer))
        n_exchange_limits = instrument.max_notional or n_liq
        n_margin = Decimal("0")
        if self._config.sizing_mode == "volume":
            n_margin = Decimal(str(self._config.volume_margin_per_entry_usdt)) * Decimal(leverage)
            n_final = min(n_margin, n_liq, n_exchange_limits)
        else:
            n_final = min(n_fee, n_risk, n_liq, n_exchange_limits)

        raw_qty = Decimal(n_final) / Decimal(str(snapshot.mark))
        qty = instrument.round_qty(raw_qty)
        notional = qty * Decimal(str(snapshot.mark))
        if qty < instrument.min_qty:
            return self._rejected(
                "qty_below_exchange_min",
                leverage,
                edge_cost_bps,
                n_fee,
                n_risk,
                n_liq,
                n_exchange_limits,
                n_margin,
            )
        if notional < instrument.min_notional:
            return self._rejected(
                "notional_below_exchange_min",
                leverage,
                edge_cost_bps,
                n_fee,
                n_risk,
                n_liq,
                n_exchange_limits,
                n_margin,
            )

        mmr = instrument.maintenance_margin_rate
        if mmr is None:
            mmr = Decimal(str(self._config.maintenance_margin_fallback))
        liquidation_distance = max(Decimal("0"), (Decimal("1") / Decimal(leverage)) - mmr)
        liquidation_distance_bps = float(liquidation_distance * Decimal("10000"))
        if liquidation_distance_bps < self._config.min_liquidation_distance_bps:
            return self._rejected(
                "liquidation_distance_too_close",
                leverage,
                edge_cost_bps,
                n_fee,
                n_risk,
                n_liq,
                n_exchange_limits,
                n_margin,
            )
        if signal.stop_bps >= liquidation_distance_bps:
            return self._rejected(
                "stop_beyond_liquidation_guard",
                leverage,
                edge_cost_bps,
                n_fee,
                n_risk,
                n_liq,
                n_exchange_limits,
                n_margin,
            )

        projected_fee = notional * fee_round_trip
        projected_loss = notional * (stop_distance + fee_round_trip + slippage_round_trip)
        projected_net_profit = notional * (take_distance - fee_round_trip - slippage_round_trip)
        if self._config.sizing_mode == "risk" and projected_net_profit <= zero:
            return self._rejected(
                "target_not_profitable_after_costs",
                leverage,
                edge_cost_bps,
                n_fee,
                n_risk,
                n_liq,
                n_exchange_limits,
                n_margin,
            )

        return SizingResult(
            accepted=True,
            rejected_reason=None,
            n_fee=n_fee,
            n_risk=n_risk,
            n_liq=n_liq,
            n_exchange_limits=n_exchange_limits,
            n_margin=n_margin,
            n_final=n_final,
            qty=qty,
            notional=notional,
            leverage=leverage,
            margin_used=notional / Decimal(leverage),
            projected_round_trip_volume=notional * Decimal(self._config.volume_sides_per_entry),
            projected_fee=projected_fee,
            projected_loss_at_stop=projected_loss,
            projected_net_profit_at_target=projected_net_profit,
            liquidation_distance_bps=liquidation_distance_bps,
            expected_cost_bps=edge_cost_bps,
        )

    def _resolve_leverage(self, instrument: InstrumentMetadata, requested: int | None) -> int:
        leverage = requested or self._config.default_leverage
        cap = self._config.effective_leverage_cap
        if instrument.max_leverage is not None:
            cap = min(cap, instrument.max_leverage)
        return max(1, min(leverage, cap))

    def _inside_funding_blackout(self, snapshot: MarketSnapshot, now: datetime) -> bool:
        if snapshot.next_funding_time is None:
            return False
        window = timedelta(minutes=self._config.funding_blackout_minutes)
        return abs(snapshot.next_funding_time - now) <= window

    @staticmethod
    def _rejected(
        reason: str,
        leverage: int,
        expected_cost_bps: float,
        n_fee: Decimal = Decimal("0"),
        n_risk: Decimal = Decimal("0"),
        n_liq: Decimal = Decimal("0"),
        n_exchange_limits: Decimal = Decimal("0"),
        n_margin: Decimal = Decimal("0"),
    ) -> SizingResult:
        return SizingResult(
            accepted=False,
            rejected_reason=reason,
            n_fee=n_fee,
            n_risk=n_risk,
            n_liq=n_liq,
            n_exchange_limits=n_exchange_limits,
            n_margin=n_margin,
            n_final=Decimal("0"),
            qty=Decimal("0"),
            notional=Decimal("0"),
            leverage=leverage,
            margin_used=Decimal("0"),
            projected_round_trip_volume=Decimal("0"),
            projected_fee=Decimal("0"),
            projected_loss_at_stop=Decimal("0"),
            projected_net_profit_at_target=Decimal("0"),
            liquidation_distance_bps=0.0,
            expected_cost_bps=expected_cost_bps,
        )
