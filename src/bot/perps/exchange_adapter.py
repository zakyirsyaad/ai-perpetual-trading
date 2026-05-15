from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_MARKET = "stop_market"


class OrderStatusValue(str, Enum):
    NEW = "new"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class InstrumentMetadata:
    symbol: str
    base_asset: str
    quote_asset: str
    price_tick: Decimal
    qty_step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal | None = None
    max_leverage: int | None = None
    margin_mode: str = "isolated"
    maker_fee_rate: Decimal = Decimal("0.0002")
    taker_fee_rate: Decimal = Decimal("0.0005")
    maintenance_margin_rate: Decimal | None = None

    def round_price(self, price: float | Decimal, side: OrderSide) -> Decimal:
        value = Decimal(str(price))
        if self.price_tick <= 0:
            return value
        rounding = ROUND_DOWN if side == OrderSide.BUY else ROUND_CEILING
        return (value / self.price_tick).to_integral_value(rounding=rounding) * self.price_tick

    def round_qty(self, qty: float | Decimal) -> Decimal:
        value = Decimal(str(qty))
        if self.qty_step <= 0:
            return value
        return (value / self.qty_step).to_integral_value(rounding=ROUND_DOWN) * self.qty_step

    def notional(self, qty: float | Decimal, price: float | Decimal) -> Decimal:
        return Decimal(str(qty)) * Decimal(str(price))


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    symbol: str
    bid: float
    ask: float
    last: float
    mark: float
    timestamp: datetime
    next_funding_time: datetime | None = None
    funding_rate: float = 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        if self.mid <= 0:
            return float("inf")
        return ((self.ask - self.bid) / self.mid) * 10_000.0

    @property
    def age_seconds(self) -> float:
        return max(0.0, (datetime.now(timezone.utc) - self.timestamp).total_seconds())


@dataclass(frozen=True, slots=True)
class HealthStatus:
    websocket_healthy: bool = True
    market_data_fresh: bool = True
    position_reconciled: bool = True
    rate_limit_remaining_pct: float = 1.0
    api_latency_ms: float = 0.0
    order_ack_latency_ms: float = 0.0
    reason: str = "ok"

    @property
    def can_open_new_positions(self) -> bool:
        return (
            self.websocket_healthy
            and self.market_data_fresh
            and self.position_reconciled
            and self.rate_limit_remaining_pct > 0
        )


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    symbol: str
    side: str
    qty: Decimal
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    leverage: int = 1
    margin_mode: str = "isolated"

    @property
    def notional(self) -> Decimal:
        return abs(self.qty) * self.mark_price


@dataclass(frozen=True, slots=True)
class OpenOrderSnapshot:
    symbol: str
    exchange_order_id: str
    client_order_id: str
    side: OrderSide
    order_type: OrderType
    qty: Decimal
    price: Decimal | None
    filled_qty: Decimal = Decimal("0")
    reduce_only: bool = False
    post_only: bool = False
    status: OrderStatusValue = OrderStatusValue.OPEN


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: Decimal
    price: Decimal | None = None
    post_only: bool = False
    reduce_only: bool = False
    client_order_id: str = ""
    leverage: int = 1
    stop_price: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderAck:
    exchange_order_id: str
    client_order_id: str
    status: OrderStatusValue
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class OrderStatus:
    exchange_order_id: str
    client_order_id: str
    status: OrderStatusValue
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal | None = None
    realized_fee: Decimal = Decimal("0")
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OrderStatusValue.FILLED,
            OrderStatusValue.CANCELED,
            OrderStatusValue.REJECTED,
            OrderStatusValue.EXPIRED,
        }


def opposite_exit_side(position_side: str) -> OrderSide:
    clean = position_side.lower()
    if clean == "long":
        return OrderSide.SELL
    if clean == "short":
        return OrderSide.BUY
    raise ValueError(f"Unknown position side: {position_side}")


class ExchangeAdapter(ABC):
    """Venue boundary for perpetual futures execution.

    Concrete adapters own REST/WebSocket/auth/instrument quirks. Core execution
    code must use only this interface.
    """

    @abstractmethod
    async def load_instruments(self, symbols: tuple[str, ...]) -> dict[str, InstrumentMetadata]:
        """Load and cache venue instrument metadata."""

    @abstractmethod
    async def get_instrument_metadata(self, symbol: str) -> InstrumentMetadata:
        """Return tick size, qty step, min quantity/notional, margin, and leverage metadata."""

    @abstractmethod
    async def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        """Return latest bid/ask/last/mark/funding state from venue data."""

    @abstractmethod
    async def get_health(self) -> HealthStatus:
        """Return feed/API/reconciliation health for fail-safe checks."""

    @abstractmethod
    async def get_account_equity(self) -> Decimal:
        """Return total account equity in quote currency."""

    @abstractmethod
    async def set_margin_mode(self, symbol: str, margin_mode: str) -> None:
        """Set isolated margin for the symbol when the venue requires it."""

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None:
        """Set leverage for the symbol after venue tier checks."""

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderAck:
        """Place a venue order."""

    @abstractmethod
    async def cancel_order(self, symbol: str, exchange_order_id: str) -> OrderStatus:
        """Cancel an open order."""

    @abstractmethod
    async def get_order_status(self, symbol: str, exchange_order_id: str) -> OrderStatus:
        """Fetch order status and fills."""

    @abstractmethod
    async def get_open_positions(self) -> list[PositionSnapshot]:
        """Fetch all open perpetual positions."""

    @abstractmethod
    async def get_open_orders(self) -> list[OpenOrderSnapshot]:
        """Fetch all open orders."""

    async def close(self) -> None:
        """Release adapter resources."""
