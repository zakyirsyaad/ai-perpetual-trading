from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from typing import Any


@dataclass(frozen=True, slots=True)
class VolumeBuildProfile:
    """Explicit volume-builder math shared by strategies, logs, and tests."""

    account_equity_usdt: Decimal
    margin_per_entry_usdt: Decimal
    leverage: int
    sides_per_entry: int
    target_entries_per_day: int
    target_volume_usdt: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_equity_usdt", Decimal(str(self.account_equity_usdt)))
        object.__setattr__(self, "margin_per_entry_usdt", Decimal(str(self.margin_per_entry_usdt)))
        object.__setattr__(self, "target_volume_usdt", Decimal(str(self.target_volume_usdt)))
        if self.account_equity_usdt <= 0:
            raise ValueError("account_equity_usdt must be positive.")
        if self.margin_per_entry_usdt <= 0:
            raise ValueError("margin_per_entry_usdt must be positive.")
        if self.leverage <= 0:
            raise ValueError("leverage must be positive.")
        if self.sides_per_entry <= 0:
            raise ValueError("sides_per_entry must be positive.")
        if self.target_entries_per_day <= 0:
            raise ValueError("target_entries_per_day must be positive.")
        if self.target_volume_usdt < 0:
            raise ValueError("target_volume_usdt must be non-negative.")

    @property
    def notional_per_side_usdt(self) -> Decimal:
        return self.margin_per_entry_usdt * Decimal(self.leverage)

    @property
    def volume_per_entry_usdt(self) -> Decimal:
        return self.notional_per_side_usdt * Decimal(self.sides_per_entry)

    @property
    def daily_volume_usdt(self) -> Decimal:
        return self.volume_per_entry_usdt * Decimal(self.target_entries_per_day)

    @property
    def target_days_exact(self) -> Decimal:
        if self.daily_volume_usdt <= 0:
            return Decimal("0")
        return self.target_volume_usdt / self.daily_volume_usdt

    @property
    def target_days_conservative(self) -> int:
        if self.target_volume_usdt <= 0:
            return 0
        return int(self.target_days_exact.to_integral_value(rounding=ROUND_CEILING))

    @property
    def margin_utilization_pct(self) -> Decimal:
        return (self.margin_per_entry_usdt / self.account_equity_usdt) * Decimal("100")

    def as_log_fields(self) -> dict[str, float | int]:
        return {
            "account_equity_usdt": float(self.account_equity_usdt),
            "margin_per_entry_usdt": float(self.margin_per_entry_usdt),
            "leverage": self.leverage,
            "sides_per_entry": self.sides_per_entry,
            "target_entries_per_day": self.target_entries_per_day,
            "notional_per_side_usdt": float(self.notional_per_side_usdt),
            "volume_per_entry_usdt": float(self.volume_per_entry_usdt),
            "daily_volume_usdt": float(self.daily_volume_usdt),
            "target_volume_usdt": float(self.target_volume_usdt),
            "target_days_exact": float(self.target_days_exact),
            "target_days_conservative": self.target_days_conservative,
            "margin_utilization_pct": float(self.margin_utilization_pct),
        }

    @classmethod
    def from_settings(cls, settings: Any) -> "VolumeBuildProfile":
        margin = getattr(settings, "volume_margin_per_entry_usdt", None)
        if margin in (None, ""):
            margin = getattr(settings, "position_usdt")
        return cls(
            account_equity_usdt=Decimal(str(getattr(settings, "account_balance_usdt"))),
            margin_per_entry_usdt=Decimal(str(margin)),
            leverage=int(getattr(settings, "leverage")),
            sides_per_entry=int(getattr(settings, "volume_sides_per_entry")),
            target_entries_per_day=int(getattr(settings, "trade_target_per_day")),
            target_volume_usdt=Decimal(str(getattr(settings, "volume_target_usdt"))),
        )
