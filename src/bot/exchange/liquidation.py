"""Venue-neutral, fail-closed isolated-margin liquidation calculations.

Tier intervals use lower-inclusive/upper-exclusive bounds, except the final
tier whose upper bound is inclusive. A directional result is accepted only
when its liquidation notional belongs to the tier used to calculate it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from bot.exchange.contracts import MaintenanceMarginTier
from bot.models import PositionDirection


@dataclass(frozen=True, slots=True)
class LiquidationConstraint:
    reference_price: float
    planned_notional_usdt: float
    leverage: int
    maintenance_margin_ratio: float | None
    maintenance_amount_usdt: float | None
    taker_close_fee_rate: float
    estimated_long_liquidation_price: float
    estimated_short_liquidation_price: float
    max_long_stop_loss_pct: float
    max_short_stop_loss_pct: float
    long_maintenance_margin_ratio: float
    long_maintenance_amount_usdt: float
    long_tier_min_position_value: float
    long_tier_max_position_value: float
    short_maintenance_margin_ratio: float
    short_maintenance_amount_usdt: float
    short_tier_min_position_value: float
    short_tier_max_position_value: float

    def to_dict(self) -> dict[str, float | int | str | None]:
        return {
            "status": "available",
            "reference_price": self.reference_price,
            "planned_notional_usdt": self.planned_notional_usdt,
            "leverage": self.leverage,
            "maintenance_margin_ratio": self.maintenance_margin_ratio,
            "maintenance_amount_usdt": self.maintenance_amount_usdt,
            "taker_close_fee_rate": self.taker_close_fee_rate,
            "estimated_long_liquidation_price": self.estimated_long_liquidation_price,
            "estimated_short_liquidation_price": self.estimated_short_liquidation_price,
            "max_long_stop_loss_pct": self.max_long_stop_loss_pct,
            "max_short_stop_loss_pct": self.max_short_stop_loss_pct,
            "long_maintenance_margin_ratio": self.long_maintenance_margin_ratio,
            "long_maintenance_amount_usdt": self.long_maintenance_amount_usdt,
            "long_tier_min_position_value": self.long_tier_min_position_value,
            "long_tier_max_position_value": self.long_tier_max_position_value,
            "short_maintenance_margin_ratio": self.short_maintenance_margin_ratio,
            "short_maintenance_amount_usdt": self.short_maintenance_amount_usdt,
            "short_tier_min_position_value": self.short_tier_min_position_value,
            "short_tier_max_position_value": self.short_tier_max_position_value,
        }


@dataclass(frozen=True, slots=True)
class _TierSolution:
    price: Decimal
    tier: MaintenanceMarginTier


def estimate_isolated_liquidation_constraint(
    *,
    reference_price: float,
    quantity_base: float,
    leverage: int,
    taker_close_fee_rate: float,
    tiers: list[MaintenanceMarginTier],
) -> LiquidationConstraint:
    """Solve each direction against every valid tier and retain its own tier."""
    reference = _positive_decimal(reference_price, "reference price")
    quantity = _positive_decimal(quantity_base, "quantity")
    leverage_value = _positive_decimal(leverage, "leverage")
    fee = _nonnegative_decimal(taker_close_fee_rate, "close fee rate")
    ordered = _validated_tiers(tiers)
    planned_notional = reference * quantity
    if _tier_for_notional(ordered, planned_notional) is None:
        raise ValueError("entry notional is not covered by a maintenance tier")
    long = _directional_solution(PositionDirection.LONG, reference, quantity, leverage_value, fee, ordered)
    short = _directional_solution(PositionDirection.SHORT, reference, quantity, leverage_value, fee, ordered)
    if long is None or short is None:
        raise ValueError("no self-consistent maintenance tier liquidation solution")

    max_long_stop = ((reference - long.price) / reference) * Decimal("100")
    max_short_stop = ((short.price - reference) / reference) * Decimal("100")
    if not all(value.is_finite() and value > 0 for value in (max_long_stop, max_short_stop)):
        raise ValueError("non-positive liquidation stop distance")

    common_tier = long.tier if long.tier == short.tier else None
    return LiquidationConstraint(
        reference_price=float(reference),
        planned_notional_usdt=float(planned_notional),
        leverage=int(leverage_value),
        maintenance_margin_ratio=float(common_tier.maintenance_margin_ratio) if common_tier else None,
        maintenance_amount_usdt=float(common_tier.maintenance_amount) if common_tier else None,
        taker_close_fee_rate=float(fee),
        estimated_long_liquidation_price=float(long.price),
        estimated_short_liquidation_price=float(short.price),
        max_long_stop_loss_pct=float(max_long_stop),
        max_short_stop_loss_pct=float(max_short_stop),
        long_maintenance_margin_ratio=float(long.tier.maintenance_margin_ratio),
        long_maintenance_amount_usdt=float(long.tier.maintenance_amount),
        long_tier_min_position_value=float(long.tier.min_position_value),
        long_tier_max_position_value=float(long.tier.max_position_value),
        short_maintenance_margin_ratio=float(short.tier.maintenance_margin_ratio),
        short_maintenance_amount_usdt=float(short.tier.maintenance_amount),
        short_tier_min_position_value=float(short.tier.min_position_value),
        short_tier_max_position_value=float(short.tier.max_position_value),
    )


def _directional_solution(
    direction: PositionDirection,
    reference: Decimal,
    quantity: Decimal,
    leverage: Decimal,
    fee: Decimal,
    tiers: list[MaintenanceMarginTier],
) -> _TierSolution | None:
    candidates: list[_TierSolution] = []
    for index, tier in enumerate(tiers):
        ratio = tier.maintenance_margin_ratio
        amount = tier.maintenance_amount
        if direction == PositionDirection.LONG:
            denominator = Decimal("1") - ratio - fee
            numerator = reference * (Decimal("1") - Decimal("1") / leverage) - amount / quantity
        else:
            denominator = Decimal("1") + ratio + fee
            numerator = reference * (Decimal("1") + Decimal("1") / leverage) + amount / quantity
        if denominator <= 0:
            continue
        price = numerator / denominator
        if not price.is_finite() or price <= 0:
            continue
        if direction == PositionDirection.LONG and price >= reference:
            continue
        if direction == PositionDirection.SHORT and price <= reference:
            continue
        if _tier_contains_notional(tier, price * quantity, final=index == len(tiers) - 1):
            candidates.append(_TierSolution(price=price, tier=tier))
    if not candidates:
        return None
    # With non-overlapping tiers, this is the first valid threshold encountered
    # as price moves away from entry; it is therefore the economic liquidation.
    return max(candidates, key=lambda item: item.price) if direction == PositionDirection.LONG else min(candidates, key=lambda item: item.price)


def select_maintenance_margin_tier(
    tiers: list[MaintenanceMarginTier],
    notional: float,
) -> MaintenanceMarginTier | None:
    requested = _positive_decimal(notional, "planned notional")
    ordered = _validated_tiers(tiers)
    return _tier_for_notional(ordered, requested)


def _tier_for_notional(
    tiers: list[MaintenanceMarginTier],
    notional: Decimal,
) -> MaintenanceMarginTier | None:
    for index, tier in enumerate(tiers):
        if _tier_contains_notional(tier, notional, final=index == len(tiers) - 1):
            return tier
    return None


def _validated_tiers(tiers: list[MaintenanceMarginTier]) -> list[MaintenanceMarginTier]:
    if not tiers:
        raise ValueError("maintenance tiers are unavailable")
    ordered = sorted(tiers, key=lambda item: item.min_position_value)
    previous_max: Decimal | None = None
    for tier in ordered:
        values = (tier.min_position_value, tier.max_position_value, tier.maintenance_margin_ratio, tier.maintenance_amount)
        if not all(value.is_finite() for value in values):
            raise ValueError("invalid maintenance tier values")
        if tier.min_position_value < 0 or tier.max_position_value <= tier.min_position_value:
            raise ValueError("invalid maintenance tier bounds")
        if tier.maintenance_margin_ratio < 0 or tier.maintenance_margin_ratio >= 1 or tier.maintenance_amount < 0:
            raise ValueError("invalid maintenance tier values")
        if previous_max is not None and tier.min_position_value != previous_max:
            raise ValueError("maintenance tiers must be contiguous and non-overlapping")
        previous_max = tier.max_position_value
    return ordered


def _tier_contains_notional(tier: MaintenanceMarginTier, notional: Decimal, *, final: bool) -> bool:
    return tier.min_position_value <= notional and (
        notional <= tier.max_position_value if final else notional < tier.max_position_value
    )


def _positive_decimal(value: float | int, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"invalid {label}")
    return parsed


def _nonnegative_decimal(value: float, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid {label}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"invalid {label}")
    return parsed


def stop_loss_is_strictly_before_liquidation(
    *,
    direction: PositionDirection,
    stop_loss_pct: float,
    constraint: LiquidationConstraint,
) -> bool:
    maximum = constraint.max_long_stop_loss_pct if direction == PositionDirection.LONG else constraint.max_short_stop_loss_pct
    return math.isfinite(stop_loss_pct) and math.isfinite(maximum) and stop_loss_pct > 0 and maximum > 0 and stop_loss_pct < maximum
