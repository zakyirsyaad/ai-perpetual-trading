from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from bot.volume import VolumeBuildProfile


@dataclass(frozen=True, slots=True)
class PerpsConfig:
    """Safe defaults for the exchange-agnostic perpetual futures layer."""

    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
    timeframes: tuple[str, ...] = ("1m", "3m", "5m")

    # Research/sizing assumptions.
    starting_equity_usdt: float = 60.0
    target_trades_per_day: int = 20
    daily_fee_budget_pct: float = 0.03
    risk_per_trade_pct: float = 0.0075
    fee_round_trip: float = 0.005
    slippage_round_trip: float = 0.0004
    edge_buffer_bps: float = 5.0
    sizing_mode: str = "risk"
    target_margin_per_entry_usdt: float | None = None
    target_volume_usdt: float = 10_000_000.0
    volume_sides_per_entry: int = 2

    # Venue/risk constraints. The exact venue API and venue max leverage must
    # remain explicit because maintenance margin tiers are venue-specific.
    exchange_api_name: str | None = None
    margin_mode: str = "isolated"
    default_leverage: int = 3
    leverage_cap: int = 3
    hard_max_leverage: int = 5
    venue_max_leverage: int | None = None
    max_open_positions: int = 1
    liquidation_notional_buffer: float = 0.90
    maintenance_margin_fallback: float = 0.005
    min_liquidation_distance_bps: float = 250.0

    # Entry/execution controls.
    maker_first: bool = True
    post_only_entries: bool = True
    reduce_only_exits: bool = True
    entry_timeout_seconds: float = 10.0
    poll_interval_seconds: float = 0.25
    max_reprice_count: int = 2
    max_spread_bps: float = 6.0
    cooldown_seconds: float = 180.0
    funding_blackout_minutes: int = 10

    # Fail-safe health and kill-switch defaults.
    stale_market_data_seconds: float = 15.0
    min_rate_limit_remaining_pct: float = 0.10
    max_daily_realized_drawdown_pct: float = 0.03
    max_rolling_drawdown_pct: float = 0.05
    max_consecutive_losses: int = 3
    max_abnormal_slippage_bps: float = 12.0

    # Backtest assumptions.
    funding_interval_hours: int = 8
    maker_fill_ratio: float = 1.0
    partial_fill_ratio_on_timeout: float = 0.50

    def __post_init__(self) -> None:
        normalized_symbols = tuple(s.upper().replace("-", "/") for s in self.symbols)
        normalized_timeframes = tuple(str(tf).lower() for tf in self.timeframes)
        object.__setattr__(self, "symbols", normalized_symbols)
        object.__setattr__(self, "timeframes", normalized_timeframes)
        object.__setattr__(self, "sizing_mode", self.sizing_mode.lower().strip())

        if self.margin_mode.lower() != "isolated":
            raise ValueError("PerpsConfig requires isolated margin only.")
        if self.sizing_mode not in {"risk", "volume"}:
            raise ValueError("sizing_mode must be risk or volume.")
        if self.sizing_mode == "volume" and self.target_margin_per_entry_usdt is None:
            raise ValueError("target_margin_per_entry_usdt is required when sizing_mode='volume'.")
        if self.target_margin_per_entry_usdt is not None and self.target_margin_per_entry_usdt <= 0:
            raise ValueError("target_margin_per_entry_usdt must be positive when configured.")
        if self.volume_sides_per_entry <= 0:
            raise ValueError("volume_sides_per_entry must be positive.")
        if self.default_leverage <= 0:
            raise ValueError("default_leverage must be positive.")
        if self.default_leverage > self.hard_max_leverage:
            raise ValueError("default_leverage cannot exceed hard_max_leverage.")
        if self.leverage_cap > self.hard_max_leverage:
            raise ValueError("leverage_cap cannot exceed hard_max_leverage.")
        if self.venue_max_leverage is not None and self.venue_max_leverage <= 0:
            raise ValueError("venue_max_leverage must be positive when configured.")
        if self.max_open_positions != 1:
            raise ValueError("Default operating profile allows one net position total.")
        if self.fee_round_trip <= 0 or self.slippage_round_trip < 0:
            raise ValueError("Fee and slippage assumptions must be non-negative, with fee > 0.")
        if self.target_trades_per_day <= 0:
            raise ValueError("target_trades_per_day must be positive.")

    @property
    def effective_leverage_cap(self) -> int:
        caps = [self.leverage_cap, self.hard_max_leverage]
        if self.venue_max_leverage is not None:
            caps.append(self.venue_max_leverage)
        return max(1, min(caps))

    @property
    def round_trip_cost_bps(self) -> float:
        return (self.fee_round_trip + self.slippage_round_trip) * 10_000.0

    @property
    def daily_fee_budget_usdt(self) -> float:
        return self.starting_equity_usdt * self.daily_fee_budget_pct

    @property
    def volume_margin_per_entry_usdt(self) -> float:
        return self.target_margin_per_entry_usdt or self.starting_equity_usdt

    @property
    def volume_profile(self) -> VolumeBuildProfile:
        return VolumeBuildProfile(
            account_equity_usdt=Decimal(str(self.starting_equity_usdt)),
            margin_per_entry_usdt=Decimal(str(self.volume_margin_per_entry_usdt)),
            leverage=self.default_leverage,
            sides_per_entry=self.volume_sides_per_entry,
            target_entries_per_day=self.target_trades_per_day,
            target_volume_usdt=Decimal(str(self.target_volume_usdt)),
        )

    def with_updates(self, **updates: Any) -> "PerpsConfig":
        return replace(self, **updates)

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "PerpsConfig":
        allowed = set(cls.__dataclass_fields__.keys())
        return cls(**{key: value for key, value in values.items() if key in allowed})

    @classmethod
    def from_settings(cls, settings: Any) -> "PerpsConfig":
        defaults = cls()
        return cls(
            symbols=tuple(getattr(settings, "symbols", defaults.symbols)),
            timeframes=tuple(getattr(settings, "decision_timeframes", defaults.timeframes)),
            starting_equity_usdt=float(getattr(settings, "account_balance_usdt", defaults.starting_equity_usdt)),
            target_trades_per_day=int(getattr(settings, "trade_target_per_day", defaults.target_trades_per_day)),
            daily_fee_budget_pct=float(getattr(settings, "perp_daily_fee_budget_pct", defaults.daily_fee_budget_pct)),
            risk_per_trade_pct=float(getattr(settings, "perp_risk_per_trade_pct", defaults.risk_per_trade_pct)),
            fee_round_trip=float(getattr(settings, "perp_fee_round_trip", defaults.fee_round_trip)),
            slippage_round_trip=float(getattr(settings, "perp_slippage_round_trip", defaults.slippage_round_trip)),
            edge_buffer_bps=float(getattr(settings, "perp_edge_buffer_bps", defaults.edge_buffer_bps)),
            sizing_mode=str(getattr(settings, "perp_sizing_mode", defaults.sizing_mode)),
            target_margin_per_entry_usdt=getattr(
                settings,
                "perp_target_margin_per_entry_usdt",
                defaults.target_margin_per_entry_usdt,
            ),
            target_volume_usdt=float(getattr(settings, "volume_target_usdt", defaults.target_volume_usdt)),
            volume_sides_per_entry=int(
                getattr(settings, "volume_sides_per_entry", defaults.volume_sides_per_entry)
            ),
            exchange_api_name=getattr(settings, "perp_exchange_api_name", defaults.exchange_api_name),
            margin_mode=str(getattr(settings, "okx_margin_mode", defaults.margin_mode)),
            default_leverage=int(getattr(settings, "leverage", defaults.default_leverage)),
            leverage_cap=int(getattr(settings, "perp_leverage_cap", defaults.leverage_cap)),
            hard_max_leverage=int(getattr(settings, "perp_hard_max_leverage", defaults.hard_max_leverage)),
            venue_max_leverage=getattr(settings, "perp_venue_max_leverage", defaults.venue_max_leverage),
            maintenance_margin_fallback=float(
                getattr(settings, "perp_maintenance_margin_fallback", defaults.maintenance_margin_fallback)
            ),
            min_liquidation_distance_bps=float(
                getattr(settings, "perp_min_liquidation_distance_bps", defaults.min_liquidation_distance_bps)
            ),
            max_spread_bps=float(getattr(settings, "perp_max_spread_bps", defaults.max_spread_bps)),
            funding_blackout_minutes=int(
                getattr(settings, "perp_funding_blackout_minutes", defaults.funding_blackout_minutes)
            ),
            max_daily_realized_drawdown_pct=float(
                getattr(settings, "perp_max_daily_realized_drawdown_pct", defaults.max_daily_realized_drawdown_pct)
            ),
            max_rolling_drawdown_pct=float(
                getattr(settings, "perp_max_rolling_drawdown_pct", defaults.max_rolling_drawdown_pct)
            ),
            max_consecutive_losses=int(getattr(settings, "max_consecutive_losses", defaults.max_consecutive_losses)),
        )
