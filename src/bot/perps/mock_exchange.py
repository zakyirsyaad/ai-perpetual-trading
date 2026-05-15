from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bot.perps.exchange_adapter import (
    ExchangeAdapter,
    HealthStatus,
    InstrumentMetadata,
    MarketSnapshot,
    OpenOrderSnapshot,
    OrderAck,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderStatusValue,
    OrderType,
    PositionSnapshot,
)


@dataclass(slots=True)
class _MockOrder:
    request: OrderRequest
    exchange_order_id: str
    status: OrderStatusValue
    status_checks: int = 0
    filled_qty: Decimal = Decimal("0")
    avg_price: Decimal | None = None
    realized_fee: Decimal = Decimal("0")


class MockExchangeAdapter(ExchangeAdapter):
    """Deterministic mock venue for unit and integration tests."""

    def __init__(
        self,
        *,
        equity: Decimal = Decimal("60"),
        fill_after_status_checks: int = 1,
        partial_fill_ratio: Decimal = Decimal("1"),
        health: HealthStatus | None = None,
    ) -> None:
        self.equity = equity
        self.fill_after_status_checks = fill_after_status_checks
        self.partial_fill_ratio = partial_fill_ratio
        self.health = health or HealthStatus()
        self.orders: dict[str, _MockOrder] = {}
        self.positions: dict[str, PositionSnapshot] = {}
        self.margin_modes: dict[str, str] = {}
        self.leverage: dict[str, int] = {}
        self._counter = 0
        now = datetime.now(timezone.utc)
        self.snapshots: dict[str, MarketSnapshot] = {
            "BTC/USDT": MarketSnapshot(
                symbol="BTC/USDT",
                bid=100_000.0,
                ask=100_010.0,
                last=100_005.0,
                mark=100_005.0,
                timestamp=now,
                next_funding_time=now + timedelta(hours=3),
            ),
            "ETH/USDT": MarketSnapshot(
                symbol="ETH/USDT",
                bid=4_000.0,
                ask=4_000.8,
                last=4_000.4,
                mark=4_000.4,
                timestamp=now,
                next_funding_time=now + timedelta(hours=3),
            ),
        }
        self.instruments: dict[str, InstrumentMetadata] = {
            "BTC/USDT": InstrumentMetadata(
                symbol="BTC/USDT",
                base_asset="BTC",
                quote_asset="USDT",
                price_tick=Decimal("0.1"),
                qty_step=Decimal("0.00001"),
                min_qty=Decimal("0.00001"),
                min_notional=Decimal("5"),
                max_leverage=5,
            ),
            "ETH/USDT": InstrumentMetadata(
                symbol="ETH/USDT",
                base_asset="ETH",
                quote_asset="USDT",
                price_tick=Decimal("0.01"),
                qty_step=Decimal("0.0001"),
                min_qty=Decimal("0.0001"),
                min_notional=Decimal("5"),
                max_leverage=5,
            ),
        }

    async def load_instruments(self, symbols: tuple[str, ...]) -> dict[str, InstrumentMetadata]:
        return {symbol: self.instruments[symbol] for symbol in symbols}

    async def get_instrument_metadata(self, symbol: str) -> InstrumentMetadata:
        return self.instruments[symbol.upper().replace("-", "/")]

    async def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        return self.snapshots[symbol.upper().replace("-", "/")]

    async def get_health(self) -> HealthStatus:
        return self.health

    async def get_account_equity(self) -> Decimal:
        return self.equity

    async def set_margin_mode(self, symbol: str, margin_mode: str) -> None:
        self.margin_modes[symbol] = margin_mode

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self.leverage[symbol] = leverage

    async def place_order(self, request: OrderRequest) -> OrderAck:
        self._counter += 1
        order_id = f"MOCK-{self._counter}"
        status = self._initial_status(request)
        order = _MockOrder(request=request, exchange_order_id=order_id, status=status)
        self.orders[order_id] = order
        if request.order_type == OrderType.MARKET:
            self._fill_order(order, Decimal("1"))
        return OrderAck(exchange_order_id=order_id, client_order_id=request.client_order_id, status=order.status)

    async def cancel_order(self, symbol: str, exchange_order_id: str) -> OrderStatus:
        order = self.orders[exchange_order_id]
        if order.status not in {OrderStatusValue.FILLED, OrderStatusValue.PARTIALLY_FILLED}:
            order.status = OrderStatusValue.CANCELED
        return self._to_status(order)

    async def get_order_status(self, symbol: str, exchange_order_id: str) -> OrderStatus:
        order = self.orders[exchange_order_id]
        order.status_checks += 1
        if (
            order.status == OrderStatusValue.OPEN
            and self.fill_after_status_checks >= 0
            and order.status_checks >= self.fill_after_status_checks
        ):
            self._fill_order(order, self.partial_fill_ratio)
        return self._to_status(order)

    async def get_open_positions(self) -> list[PositionSnapshot]:
        return list(self.positions.values())

    async def get_open_orders(self) -> list[OpenOrderSnapshot]:
        snapshots: list[OpenOrderSnapshot] = []
        for order in self.orders.values():
            if order.status != OrderStatusValue.OPEN:
                continue
            req = order.request
            snapshots.append(
                OpenOrderSnapshot(
                    symbol=req.symbol,
                    exchange_order_id=order.exchange_order_id,
                    client_order_id=req.client_order_id,
                    side=req.side,
                    order_type=req.order_type,
                    qty=req.qty,
                    price=req.price,
                    filled_qty=order.filled_qty,
                    reduce_only=req.reduce_only,
                    post_only=req.post_only,
                    status=order.status,
                )
            )
        return snapshots

    def _initial_status(self, request: OrderRequest) -> OrderStatusValue:
        if request.post_only and request.price is not None:
            snapshot = self.snapshots[request.symbol]
            if request.side == OrderSide.BUY and request.price >= Decimal(str(snapshot.ask)):
                return OrderStatusValue.REJECTED
            if request.side == OrderSide.SELL and request.price <= Decimal(str(snapshot.bid)):
                return OrderStatusValue.REJECTED
        return OrderStatusValue.OPEN

    def _fill_order(self, order: _MockOrder, fill_ratio: Decimal) -> None:
        req = order.request
        fill_ratio = max(Decimal("0"), min(Decimal("1"), fill_ratio))
        fill_qty = req.qty * fill_ratio
        price = req.price or req.stop_price or Decimal(str(self.snapshots[req.symbol].mark))
        order.filled_qty = fill_qty
        order.avg_price = price
        order.status = OrderStatusValue.FILLED if fill_ratio >= Decimal("1") else OrderStatusValue.PARTIALLY_FILLED
        instrument = self.instruments[req.symbol]
        fee_rate = instrument.taker_fee_rate if req.order_type == OrderType.MARKET else instrument.maker_fee_rate
        order.realized_fee = abs(fill_qty * price * fee_rate)
        if fill_qty <= 0:
            return
        if req.reduce_only:
            self.positions.pop(req.symbol, None)
            return
        side = "long" if req.side == OrderSide.BUY else "short"
        qty = fill_qty if side == "long" else -fill_qty
        self.positions[req.symbol] = PositionSnapshot(
            symbol=req.symbol,
            side=side,
            qty=qty,
            entry_price=price,
            mark_price=Decimal(str(self.snapshots[req.symbol].mark)),
            leverage=req.leverage,
            margin_mode=self.margin_modes.get(req.symbol, "isolated"),
        )

    @staticmethod
    def _to_status(order: _MockOrder) -> OrderStatus:
        return OrderStatus(
            exchange_order_id=order.exchange_order_id,
            client_order_id=order.request.client_order_id,
            status=order.status,
            filled_qty=order.filled_qty,
            avg_price=order.avg_price,
            realized_fee=order.realized_fee,
        )
