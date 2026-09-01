"""Venue-neutral futures-exchange contract.

`TradeEngine`, `MarketIntelligence`, leverage resolution, and the realtime
backtest depend on these immutable models and on `FuturesExchangeProtocol`
instead of importing a specific venue client. Adapters (OKX, BingX) return
these types so the runtime never has to know which venue is wired in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from bot.models import PositionDirection


class ExchangeAPIError(Exception):
    """Raised when a venue returns a non-success response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        payload: dict[str, Any] | None = None,
        retryable: bool = True,
        venue: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload
        self.retryable = retryable
        self.venue = venue


class UncertainOrderSubmissionError(ExchangeAPIError):
    """An order request may have been accepted, but cannot be reconciled safely."""

    def __init__(
        self,
        message: str,
        *,
        symbol: str,
        client_order_id: str,
        order_id: str | None = None,
        venue: str = "unknown",
    ) -> None:
        super().__init__(message, retryable=False, venue=venue)
        self.symbol = symbol
        self.client_order_id = client_order_id
        self.order_id = order_id


@dataclass(slots=True)
class Instrument:
    """Venue instrument metadata normalized to internal units.

    `contract_value` is the base-asset amount represented by one exchange
    "size" unit. Venues that size orders directly in base asset (BingX) use
    1; venues that size in contracts (OKX) use the contract multiplier.
    """

    symbol: str
    venue_symbol: str
    contract_value: Decimal = Decimal("1")
    lot_size: Decimal = Decimal("1")
    min_size: Decimal = Decimal("0")
    tick_size: Decimal = Decimal("0.01")
    quantity_precision: int = 8
    price_precision: int = 2
    min_notional_usdt: Decimal = Decimal("0")
    max_leverage: int | None = None
    api_trading_enabled: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderBookSnapshot:
    symbol: str
    best_bid: float
    best_ask: float
    mid_price: float
    spread_bps: float
    bid_depth_notional: float
    ask_depth_notional: float
    timestamp: datetime | None
    raw: dict[str, Any]


@dataclass(slots=True)
class FundingRateInfo:
    symbol: str
    funding_rate: float
    next_funding_time: datetime | None
    raw: dict[str, Any]


@dataclass(slots=True)
class PositionDetails:
    symbol: str
    exchange_size: float
    base_quantity: float
    avg_price: float | None
    mark_price: float | None
    liquidation_price: float | None
    pos_side: str | None
    raw: dict[str, Any]
    position_id: str | None = None
    unrealized_pnl_usdt: float | None = None


@dataclass(slots=True)
class OrderAck:
    """Acknowledgement of an accepted order intent."""

    order_id: str
    client_order_id: str | None
    symbol: str
    raw: dict[str, Any]


@dataclass(slots=True)
class OrderFill:
    order_id: str
    avg_price: float | None
    filled_size: float
    base_quantity: float
    fee_usdt: float
    pnl_usdt: float | None
    state: str | None
    raw: dict[str, Any]
    client_order_id: str | None = None


@dataclass(slots=True)
class TradeFeeRates:
    maker_fee_pct: float | None
    taker_fee_pct: float
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MaintenanceMarginTier:
    """Normalized isolated-margin maintenance tier in USDT notional units."""

    min_position_value: Decimal
    max_position_value: Decimal
    maintenance_margin_ratio: Decimal
    maintenance_amount: Decimal


@runtime_checkable
class MaintenanceMarginTierProvider(Protocol):
    """Optional capability needed for pre-order liquidation safety."""

    async def get_maintenance_margin_tiers(self, symbol: str) -> list[MaintenanceMarginTier]: ...


@runtime_checkable
class FreshOrderBookProvider(Protocol):
    """Optional capability for last-mile entry proofs that cannot use cached data."""

    async def get_fresh_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot: ...


@runtime_checkable
class FuturesExchangeProtocol(Protocol):
    """Capabilities the runtime requires from any perpetual-futures venue."""

    venue: str

    # --- lifecycle -----------------------------------------------------
    async def validate_api_access(self, symbol: str) -> None: ...

    async def close(self) -> None: ...

    # --- market data ---------------------------------------------------
    async def get_last_price(self, symbol: str) -> float: ...

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 120,
    ) -> list[dict[str, float]]: ...

    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot: ...

    async def get_funding_rate(self, symbol: str) -> float: ...

    async def get_funding_rate_info(self, symbol: str) -> FundingRateInfo: ...

    async def get_open_interest(self, symbol: str) -> float: ...

    # --- account -------------------------------------------------------
    async def get_trade_fee_rates(self, symbol: str) -> TradeFeeRates: ...

    async def get_position_tiers(
        self,
        symbol: str,
        td_mode: str = "isolated",
    ) -> list[dict[str, Any]]: ...

    async def set_position_mode(self, position_mode: str) -> dict[str, Any]: ...

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]: ...

    # --- sizing --------------------------------------------------------
    async def calculate_order_size(self, symbol: str, base_quantity: float) -> float: ...

    async def estimate_base_quantity(self, symbol: str, order_size: float) -> float: ...

    # --- orders --------------------------------------------------------
    async def place_market_order(
        self,
        symbol: str,
        direction: PositionDirection,
        size: float,
        reduce_only: bool = False,
    ) -> dict[str, Any]: ...

    async def place_limit_order(
        self,
        symbol: str,
        direction: PositionDirection,
        size: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> dict[str, Any]: ...

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]: ...

    async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None: ...

    # --- positions -----------------------------------------------------
    async def get_position_info(self, symbol: str) -> dict[str, Any]: ...

    async def get_symbol_position_details(self, symbol: str) -> PositionDetails: ...

    async def get_symbol_position_snapshot(self, symbol: str) -> tuple[float, float | None]: ...

    async def get_symbol_position_quantity(self, symbol: str) -> float: ...

    async def has_open_position(self, symbol: str) -> bool: ...

    async def has_any_open_position(self, symbols: tuple[str, ...]) -> bool: ...

    async def flatten_position(self, symbol: str) -> dict[str, Any] | None: ...
