"""BingX Perpetual Futures VST client.

Implements `FuturesExchangeProtocol` against the BingX VST (virtual/simulated)
perpetual-futures API. Live endpoints fail closed: the constructor refuses any
base URL or environment that is not the VST host.

References:
- base URLs:      https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/base-urls.md
- authentication: https://github.com/BingX-API/api-ai-skills/blob/main/skills/references/authentication.md
- swap market:    https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-market/api-reference.md
- swap account:   https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-account/api-reference.md
- swap trade:     https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-trade/api-reference.md
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Any

import httpx

from bot.exchange.contracts import (
    ExchangeAPIError,
    FundingRateInfo,
    Instrument,
    OrderBookSnapshot,
    OrderFill,
    PositionDetails,
    TradeFeeRates,
)
from bot.models import PositionDirection

BINGX_VST_BASE_URL = "https://open-api-vst.bingx.com"
BINGX_VST_FALLBACK_BASE_URL = "https://open-api-vst.bingx.pro"
DEFAULT_SOURCE_KEY = "ai-perpetual-trading"
DEFAULT_RECV_WINDOW_MS = 5000

# BingX business codes that mean "your machine clock is wrong", surfaced with an
# explicit hint because the raw message ("timestamp expired") is easy to misread.
_CLOCK_DRIFT_CODES = {"100421", "100001", "80014"}
_CLOCK_DRIFT_TOKENS = ("timestamp", "recvwindow", "recv window")

# Permanent business failures: never retried, never failed over to `.pro`.
_PERMANENT_CODE_PREFIXES = ("1004", "1005", "8001", "1001")

_KLINE_INTERVALS = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}


def canonical_param_string(params: dict[str, Any]) -> str:
    """ASCII key-ordered `k=v&k=v` string used as the signature payload."""
    parts: list[str] = []
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        parts.append(f"{key}={value}")
    return "&".join(parts)


def build_signature(secret_key: str, payload: str) -> str:
    """HMAC-SHA256, lowercase hexadecimal."""
    return hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


class BingXFuturesClient:
    """BingX VST perpetual-futures adapter."""

    venue = "bingx"

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str = BINGX_VST_BASE_URL,
        *,
        env: str = "vst",
        margin_mode: str = "isolated",
        position_mode: str = "hedge",
        timeout_seconds: int = 20,
        recv_window_ms: int = DEFAULT_RECV_WINDOW_MS,
        source_key: str = DEFAULT_SOURCE_KEY,
        route_min_interval_seconds: float = 0.0,
        market_cache_seconds: float = 0.0,
        insecure_ssl: bool = False,
        debug_http: bool = False,
    ) -> None:
        normalized_env = str(env).strip().lower()
        if normalized_env != "vst":
            raise ValueError("BINGX_ENV must be 'vst'; live BingX trading is not enabled for this runtime.")
        normalized_url = str(base_url).strip().rstrip("/")
        if not self._is_vst_host(normalized_url):
            raise ValueError(
                "BINGX_BASE_URL must point at the BingX VST host "
                f"({BINGX_VST_BASE_URL} or {BINGX_VST_FALLBACK_BASE_URL}); live endpoints fail closed."
            )

        self._api_key = api_key
        self._secret_key = secret_key
        self._base_url = normalized_url
        self._margin_mode = str(margin_mode).strip().lower()
        self._position_mode = str(position_mode).strip().lower()
        self._recv_window_ms = int(recv_window_ms)
        self._source_key = source_key
        self._route_min_interval = float(route_min_interval_seconds)
        self._market_cache_seconds = float(market_cache_seconds)
        self._debug_http = debug_http
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            verify=(not insecure_ssl),
        )
        self._instruments: dict[str, Instrument] = {}
        self._last_route_call: dict[str, float] = {}
        self._market_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def fallback_base_url(self) -> str:
        return BINGX_VST_FALLBACK_BASE_URL

    async def close(self) -> None:
        await self._client.aclose()

    async def validate_api_access(self, symbol: str) -> None:
        await self.get_last_price(symbol)
        await self.get_position_info(symbol)

    # ------------------------------------------------------------------
    # symbols and instruments
    # ------------------------------------------------------------------
    def venue_symbol(self, symbol: str) -> str:
        return symbol.strip().upper().replace("/", "-")

    async def get_instrument(self, symbol: str) -> Instrument:
        venue_symbol = self.venue_symbol(symbol)
        cached = self._instruments.get(venue_symbol)
        if cached is not None:
            return cached

        resp = await self._public_request(
            "GET",
            "/openApi/swap/v2/quote/contracts",
            {"symbol": venue_symbol},
        )
        row = self._match_contract_row(resp, venue_symbol)
        max_leverage = self._min_optional_int(row.get("maxLongLeverage"), row.get("maxShortLeverage"))
        quantity_precision = int(self._decimal(row.get("quantityPrecision"), Decimal("4")))
        price_precision = int(self._decimal(row.get("pricePrecision"), Decimal("2")))
        instrument = Instrument(
            symbol=symbol,
            venue_symbol=venue_symbol,
            contract_value=Decimal("1"),
            lot_size=Decimal(1).scaleb(-quantity_precision),
            min_size=self._decimal(row.get("tradeMinQuantity"), Decimal("0")),
            tick_size=Decimal(1).scaleb(-price_precision),
            quantity_precision=quantity_precision,
            price_precision=price_precision,
            min_notional_usdt=self._decimal(row.get("tradeMinUSDT"), Decimal("0")),
            max_leverage=max_leverage,
            api_trading_enabled=self._truthy(row.get("apiStateOpen", True)),
            raw=row,
        )
        self._instruments[venue_symbol] = instrument
        return instrument

    async def get_position_tiers(self, symbol: str, td_mode: str = "isolated") -> list[dict[str, Any]]:
        """Venue leverage cap in the shape the leverage resolver expects."""
        del td_mode
        instrument = await self.get_instrument(symbol)
        if instrument.max_leverage is None:
            return []
        return [{"maxLever": instrument.max_leverage, "symbol": instrument.venue_symbol}]

    async def calculate_order_size(self, symbol: str, base_quantity: float) -> float:
        """BingX sizes perpetual orders in base asset, not contract count."""
        instrument = await self.get_instrument(symbol)
        quantity = self._round_down_to_precision(
            Decimal(str(base_quantity)),
            instrument.quantity_precision,
        )
        if quantity <= 0 or (instrument.min_size > 0 and quantity < instrument.min_size):
            return 0.0
        return float(quantity)

    async def estimate_base_quantity(self, symbol: str, order_size: float) -> float:
        await self.get_instrument(symbol)
        return float(order_size)

    # ------------------------------------------------------------------
    # market data
    # ------------------------------------------------------------------
    async def get_last_price(self, symbol: str) -> float:
        venue_symbol = self.venue_symbol(symbol)
        resp = await self._public_request(
            "GET",
            "/openApi/swap/v2/quote/price",
            {"symbol": venue_symbol},
            cache_key=f"price:{venue_symbol}",
        )
        row = self._data_object(resp)
        price = row.get("price") or row.get("markPrice") or row.get("indexPrice")
        if price in (None, ""):
            raise ExchangeAPIError(
                f"Missing last price for {venue_symbol}: {json.dumps(resp)[:300]}",
                retryable=False,
                venue=self.venue,
            )
        return float(price)

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 120,
    ) -> list[dict[str, float]]:
        venue_symbol = self.venue_symbol(symbol)
        venue_interval = _KLINE_INTERVALS.get(str(interval).lower())
        if venue_interval is None:
            raise ExchangeAPIError(
                f"Unsupported BingX kline interval: {interval}",
                retryable=False,
                venue=self.venue,
            )
        resp = await self._public_request(
            "GET",
            "/openApi/swap/v3/quote/klines",
            {"symbol": venue_symbol, "interval": venue_interval, "limit": int(limit)},
            cache_key=f"klines:{venue_symbol}:{venue_interval}:{limit}",
        )
        rows = resp.get("data")
        if not isinstance(rows, list):
            return []
        now_ms = self._now_ms()
        interval_ms = self._interval_millis(venue_interval)
        parsed: list[dict[str, float]] = []
        for item in rows:
            row = self._kline_row(item)
            if row is None:
                continue
            close_time = row["timestamp"] + interval_ms
            # Only fully-closed candles: an in-progress candle would make every
            # indicator jump around inside the same three-minute window.
            if close_time > now_ms:
                continue
            parsed.append(row)
        parsed.sort(key=lambda row: row["timestamp"])
        return [row for row in parsed if row["close"] > 0]

    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        venue_symbol = self.venue_symbol(symbol)
        await self.get_instrument(symbol)
        resp = await self._public_request(
            "GET",
            "/openApi/swap/v2/quote/depth",
            {"symbol": venue_symbol, "limit": int(depth)},
            cache_key=f"depth:{venue_symbol}:{depth}",
        )
        row = self._data_object(resp)
        bids = row.get("bids") if isinstance(row.get("bids"), list) else []
        asks = row.get("asks") if isinstance(row.get("asks"), list) else []
        if not bids or not asks:
            raise ExchangeAPIError(
                f"Empty order book for {venue_symbol}",
                retryable=True,
                venue=self.venue,
            )
        best_bid = self._decimal(bids[0][0])
        best_ask = self._decimal(asks[0][0])
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            raise ExchangeAPIError(
                f"Invalid order book top for {venue_symbol}: bid={best_bid} ask={best_ask}",
                retryable=True,
                venue=self.venue,
            )
        mid = (best_bid + best_ask) / Decimal("2")
        return OrderBookSnapshot(
            symbol=symbol,
            best_bid=float(best_bid),
            best_ask=float(best_ask),
            mid_price=float(mid),
            spread_bps=float(((best_ask - best_bid) / mid) * Decimal("10000")),
            bid_depth_notional=float(self._depth_notional(bids)),
            ask_depth_notional=float(self._depth_notional(asks)),
            timestamp=self._millis_to_datetime(row.get("T") or row.get("ts")),
            raw=row,
        )

    async def get_funding_rate(self, symbol: str) -> float:
        return (await self.get_funding_rate_info(symbol)).funding_rate

    async def get_funding_rate_info(self, symbol: str) -> FundingRateInfo:
        venue_symbol = self.venue_symbol(symbol)
        resp = await self._public_request(
            "GET",
            "/openApi/swap/v2/quote/premiumIndex",
            {"symbol": venue_symbol},
            cache_key=f"funding:{venue_symbol}",
        )
        row = self._data_object(resp)
        value = row.get("lastFundingRate")
        return FundingRateInfo(
            symbol=symbol,
            funding_rate=float(value) if value not in (None, "") else 0.0,
            next_funding_time=self._millis_to_datetime(row.get("nextFundingTime")),
            raw=row,
        )

    async def get_open_interest(self, symbol: str) -> float:
        venue_symbol = self.venue_symbol(symbol)
        resp = await self._public_request(
            "GET",
            "/openApi/swap/v2/quote/openInterest",
            {"symbol": venue_symbol},
            cache_key=f"oi:{venue_symbol}",
        )
        row = self._data_object(resp)
        value = row.get("openInterest") or row.get("openInterestAmount")
        return float(value) if value not in (None, "") else 0.0

    # ------------------------------------------------------------------
    # account
    # ------------------------------------------------------------------
    async def get_trade_fee_rates(self, symbol: str) -> TradeFeeRates:
        resp = await self._signed_request(
            "GET",
            "/openApi/swap/v2/user/commissionRate",
            {"symbol": self.venue_symbol(symbol)},
        )
        row = self._data_object(resp)
        commission = row.get("commission") if isinstance(row.get("commission"), dict) else row
        taker = self._optional_decimal(commission.get("takerCommissionRate"))
        maker = self._optional_decimal(commission.get("makerCommissionRate"))
        if taker is None:
            raise ExchangeAPIError(
                f"Missing taker commission rate for {symbol}",
                retryable=False,
                venue=self.venue,
            )
        return TradeFeeRates(
            maker_fee_pct=float(maker * Decimal("100")) if maker is not None else None,
            taker_fee_pct=float(abs(taker) * Decimal("100")),
            raw=row,
        )

    async def set_position_mode(self, position_mode: str) -> dict[str, Any]:
        dual = str(position_mode).strip().lower() in {"hedge", "dual", "long_short_mode", "true"}
        return await self._signed_request(
            "POST",
            "/openApi/swap/v1/positionSide/dual",
            {"dualSidePosition": "true" if dual else "false"},
        )

    async def set_margin_mode(self, symbol: str, margin_mode: str) -> dict[str, Any]:
        mode = str(margin_mode).strip().lower()
        if mode != "isolated":
            raise ExchangeAPIError(
                "BINGX_MARGIN_MODE must be 'isolated'; cross margin is not allowed.",
                retryable=False,
                venue=self.venue,
            )
        return await self._signed_request(
            "POST",
            "/openApi/swap/v2/trade/marginType",
            {"symbol": self.venue_symbol(symbol), "marginType": "ISOLATED"},
        )

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        instrument = await self.get_instrument(symbol)
        if instrument.max_leverage is not None and int(leverage) > instrument.max_leverage:
            raise ExchangeAPIError(
                f"Requested leverage {leverage}x exceeds the BingX venue maximum "
                f"{instrument.max_leverage}x for {instrument.venue_symbol}.",
                retryable=False,
                venue=self.venue,
            )
        responses: dict[str, Any] = {}
        for side in self._position_sides():
            responses[side] = await self._signed_request(
                "POST",
                "/openApi/swap/v2/trade/leverage",
                {
                    "symbol": instrument.venue_symbol,
                    "side": side,
                    "leverage": int(leverage),
                },
            )
        return {"code": 0, "msg": "", "data": responses}

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    async def place_market_order(
        self,
        symbol: str,
        direction: PositionDirection,
        size: float,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        params = await self._order_params(symbol, direction, size, reduce_only=reduce_only)
        params["type"] = "MARKET"
        return await self._submit_order(symbol, params)

    async def place_limit_order(
        self,
        symbol: str,
        direction: PositionDirection,
        size: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> dict[str, Any]:
        instrument = await self.get_instrument(symbol)
        params = await self._order_params(
            symbol,
            direction,
            size,
            reduce_only=reduce_only,
            reference_price=price,
        )
        params["type"] = "LIMIT"
        params["price"] = self._format_price(price, instrument.price_precision)
        params["timeInForce"] = "PostOnly" if post_only else "GTC"
        return await self._submit_order(symbol, params)

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        return await self._signed_request(
            "DELETE",
            "/openApi/swap/v2/trade/order",
            {"symbol": self.venue_symbol(symbol), "orderId": str(order_id)},
        )

    async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
        return await self._order_fill(symbol, {"orderId": str(order_id)})

    async def get_order_fill_by_client_id(self, symbol: str, client_order_id: str) -> OrderFill | None:
        return await self._order_fill(symbol, {"clientOrderID": str(client_order_id)})

    # ------------------------------------------------------------------
    # positions
    # ------------------------------------------------------------------
    async def get_position_info(self, symbol: str) -> dict[str, Any]:
        return await self._signed_request(
            "GET",
            "/openApi/swap/v2/user/positions",
            {"symbol": self.venue_symbol(symbol)},
        )

    async def get_symbol_position_details(self, symbol: str) -> PositionDetails:
        resp = await self.get_position_info(symbol)
        rows = resp.get("data") if isinstance(resp.get("data"), list) else []
        venue_symbol = self.venue_symbol(symbol)
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol", "")).upper() != venue_symbol:
                continue
            size = abs(self._decimal(row.get("positionAmt")))
            if size <= 0:
                continue
            avg_price = self._decimal(row.get("avgPrice"))
            mark_price = self._optional_decimal(row.get("markPrice"))
            liquidation_price = self._optional_decimal(row.get("liquidationPrice"))
            unrealized = self._optional_decimal(row.get("unrealizedProfit"))
            return PositionDetails(
                symbol=symbol,
                exchange_size=float(size),
                base_quantity=float(size),
                avg_price=float(avg_price) if avg_price > 0 else None,
                mark_price=float(mark_price) if mark_price is not None and mark_price > 0 else None,
                liquidation_price=(
                    float(liquidation_price)
                    if liquidation_price is not None and liquidation_price > 0
                    else None
                ),
                pos_side=str(row.get("positionSide") or "") or None,
                raw=row,
                position_id=str(row.get("positionId")) if row.get("positionId") not in (None, "") else None,
                unrealized_pnl_usdt=float(unrealized) if unrealized is not None else None,
            )
        return PositionDetails(
            symbol=symbol,
            exchange_size=0.0,
            base_quantity=0.0,
            avg_price=None,
            mark_price=None,
            liquidation_price=None,
            pos_side=None,
            raw={},
        )

    async def get_symbol_position_snapshot(self, symbol: str) -> tuple[float, float | None]:
        details = await self.get_symbol_position_details(symbol)
        return details.exchange_size, details.avg_price

    async def get_symbol_position_quantity(self, symbol: str) -> float:
        size, _ = await self.get_symbol_position_snapshot(symbol)
        return size

    async def has_open_position(self, symbol: str) -> bool:
        return await self.get_symbol_position_quantity(symbol) > 0

    async def has_any_open_position(self, symbols: tuple[str, ...]) -> bool:
        for symbol in symbols:
            if await self.has_open_position(symbol):
                return True
        return False

    async def flatten_position(self, symbol: str) -> dict[str, Any] | None:
        details = await self.get_symbol_position_details(symbol)
        if details.exchange_size <= 0:
            return None
        direction = (
            PositionDirection.SHORT
            if str(details.pos_side or "").lower() == "short"
            else PositionDirection.LONG
        )
        return await self.place_market_order(
            symbol=symbol,
            direction=direction,
            size=details.exchange_size,
            reduce_only=True,
        )

    # ------------------------------------------------------------------
    # internals: orders
    # ------------------------------------------------------------------
    async def _order_params(
        self,
        symbol: str,
        direction: PositionDirection,
        size: float,
        *,
        reduce_only: bool,
        reference_price: float | None = None,
    ) -> dict[str, Any]:
        instrument = await self.get_instrument(symbol)
        if not instrument.api_trading_enabled:
            raise ExchangeAPIError(
                f"BingX API trading is disabled for {instrument.venue_symbol}; refusing to place an order.",
                retryable=False,
                venue=self.venue,
            )
        quantity = self._round_down_to_precision(Decimal(str(size)), instrument.quantity_precision)
        if quantity <= 0 or (instrument.min_size > 0 and quantity < instrument.min_size):
            raise ExchangeAPIError(
                f"Order quantity {size} for {instrument.venue_symbol} is below tradeMinQuantity "
                f"{instrument.min_size}.",
                retryable=False,
                venue=self.venue,
            )
        if instrument.min_notional_usdt > 0 and reference_price is not None:
            notional = quantity * Decimal(str(reference_price))
            if notional < instrument.min_notional_usdt:
                raise ExchangeAPIError(
                    f"Order notional {notional} USDT for {instrument.venue_symbol} is below tradeMinUSDT "
                    f"{instrument.min_notional_usdt}.",
                    retryable=False,
                    venue=self.venue,
                )
        side, position_side = self._order_sides(direction, reduce_only=reduce_only)
        # Hedge mode: closing uses the opposite side with the SAME positionSide.
        # `reduceOnly` is deliberately omitted — BingX rejects it for dual-side positions.
        return {
            "symbol": instrument.venue_symbol,
            "side": side,
            "positionSide": position_side,
            "quantity": self._format_quantity(quantity),
            "clientOrderID": self._new_client_order_id(),
        }

    async def _submit_order(self, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
        client_order_id = str(params["clientOrderID"])
        try:
            return await self._signed_request(
                "POST",
                "/openApi/swap/v2/trade/order",
                params,
                allow_failover=False,
            )
        except (httpx.TimeoutException, httpx.TransportError, ExchangeAPIError) as exc:
            if isinstance(exc, ExchangeAPIError) and not exc.retryable:
                raise
            # Uncertain POST: the order may already have reached the exchange.
            # Look it up by clientOrderId BEFORE any resubmission or failover.
            existing = await self._lookup_order_by_client_id(symbol, client_order_id)
            if existing is not None:
                return existing
            return await self._signed_request("POST", "/openApi/swap/v2/trade/order", params)

    async def _lookup_order_by_client_id(self, symbol: str, client_order_id: str) -> dict[str, Any] | None:
        try:
            resp = await self._signed_request(
                "GET",
                "/openApi/swap/v2/trade/order",
                {"symbol": self.venue_symbol(symbol), "clientOrderID": client_order_id},
            )
        except ExchangeAPIError:
            return None
        if self._order_row(resp):
            return resp
        return None

    async def _order_fill(self, symbol: str, selector: dict[str, Any]) -> OrderFill | None:
        resp = await self._signed_request(
            "GET",
            "/openApi/swap/v2/trade/order",
            {"symbol": self.venue_symbol(symbol), **selector},
        )
        row = self._order_row(resp)
        if not row:
            return None
        order_id = str(row.get("orderId") or selector.get("orderId") or "")
        filled = self._decimal(row.get("executedQty"))
        avg_price = self._optional_decimal(row.get("avgPrice"))
        fee = self._optional_decimal(row.get("commission") or row.get("fee"))
        pnl = self._optional_decimal(row.get("profit") or row.get("realisedPNL"))
        if fee is None or pnl is None:
            aggregated = await self._aggregate_fills(symbol, order_id)
            if aggregated is not None:
                fee = fee if fee is not None else aggregated[0]
                pnl = pnl if pnl is not None else aggregated[1]
        return OrderFill(
            order_id=order_id,
            avg_price=float(avg_price) if avg_price is not None and avg_price > 0 else None,
            filled_size=float(filled),
            base_quantity=float(abs(filled)),
            fee_usdt=float(abs(fee)) if fee is not None else 0.0,
            pnl_usdt=float(pnl) if pnl is not None else None,
            state=str(row.get("status") or "").lower() or None,
            raw=row,
            client_order_id=str(row.get("clientOrderID") or "") or None,
        )

    async def _aggregate_fills(self, symbol: str, order_id: str) -> tuple[Decimal, Decimal] | None:
        if not order_id:
            return None
        try:
            resp = await self._signed_request(
                "GET",
                "/openApi/swap/v2/trade/fillHistory",
                {"symbol": self.venue_symbol(symbol), "orderId": order_id},
            )
        except ExchangeAPIError:
            return None
        data = resp.get("data")
        rows: list[Any] = []
        if isinstance(data, dict):
            for key in ("fill_orders", "fillOrders", "orders", "list"):
                if isinstance(data.get(key), list):
                    rows = data[key]
                    break
        elif isinstance(data, list):
            rows = data
        if not rows:
            return None
        fee = Decimal("0")
        pnl = Decimal("0")
        matched = False
        for row in rows:
            if not isinstance(row, dict) or str(row.get("orderId")) != order_id:
                continue
            matched = True
            fee += abs(self._decimal(row.get("commission") or row.get("fee")))
            pnl += self._decimal(row.get("profit") or row.get("realisedPNL"))
        return (fee, pnl) if matched else None

    def _position_sides(self) -> tuple[str, ...]:
        return ("LONG", "SHORT") if self._position_mode == "hedge" else ("BOTH",)

    def _order_sides(self, direction: PositionDirection, *, reduce_only: bool) -> tuple[str, str]:
        position_side = "LONG" if direction == PositionDirection.LONG else "SHORT"
        if self._position_mode != "hedge":
            position_side = "BOTH"
        if direction == PositionDirection.LONG:
            side = "SELL" if reduce_only else "BUY"
        else:
            side = "BUY" if reduce_only else "SELL"
        return side, position_side

    @staticmethod
    def _new_client_order_id() -> str:
        return f"apt{uuid.uuid4().hex[:24]}"

    # ------------------------------------------------------------------
    # internals: transport
    # ------------------------------------------------------------------
    async def _public_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        cache_key: str | None = None,
    ) -> dict[str, Any]:
        if cache_key is not None:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
        body = await self._request(method, path, dict(params or {}), signed=False, allow_failover=True)
        if cache_key is not None:
            self._cache_put(cache_key, body)
        return body

    async def _signed_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        allow_failover: bool = True,
    ) -> dict[str, Any]:
        return await self._request(method, path, dict(params or {}), signed=True, allow_failover=allow_failover)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        signed: bool,
        allow_failover: bool,
    ) -> dict[str, Any]:
        method_upper = method.upper()
        hosts = [self._base_url]
        if allow_failover and self.fallback_base_url not in hosts:
            hosts.append(self.fallback_base_url)

        attempts_per_host = 3
        last_error: Exception | None = None
        for host in hosts:
            for attempt in range(attempts_per_host):
                await self._throttle(path)
                request_params = self._sign_params(params) if signed else dict(params)
                try:
                    response = await self._client.request(
                        method_upper,
                        f"{host}{path}",
                        params=request_params,
                        headers=self._headers(signed),
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    # Network-level failure only: this is the sole condition under
                    # which the `.pro` host is allowed to be tried.
                    last_error = exc
                    break
                try:
                    return self._parse_response(response, path)
                except ExchangeAPIError as exc:
                    last_error = exc
                    if not exc.retryable:
                        raise
                    if attempt == attempts_per_host - 1:
                        break
                    await self._sleep(min(0.8 * (2**attempt), 4.0))
        if isinstance(last_error, ExchangeAPIError):
            raise last_error
        raise ExchangeAPIError(
            f"BingX request failed for {path}: {type(last_error).__name__}: {last_error}",
            retryable=True,
            venue=self.venue,
        ) from last_error

    def _sign_params(self, params: dict[str, Any]) -> dict[str, Any]:
        signed = {key: value for key, value in params.items() if value is not None}
        signed["timestamp"] = self._now_ms()
        signed["recvWindow"] = self._recv_window_ms
        signed["signature"] = build_signature(self._secret_key, canonical_param_string(signed))
        return signed

    def _headers(self, signed: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-SOURCE-KEY": self._source_key,
        }
        if signed:
            headers["X-BX-APIKEY"] = self._api_key
        return headers

    def _parse_response(self, response: Any, path: str) -> dict[str, Any]:
        status_code = int(getattr(response, "status_code", 200))
        try:
            body = response.json()
        except ValueError:
            snippet = (getattr(response, "text", "") or "")[:400]
            raise ExchangeAPIError(
                f"Non-JSON response from BingX {path} (status={status_code}): {snippet}",
                status_code=status_code,
                retryable=status_code >= 500 or status_code == 429,
                venue=self.venue,
            )
        if self._debug_http:
            print(json.dumps(body, ensure_ascii=True)[:1000])
        if status_code >= 400:
            raise ExchangeAPIError(
                f"BingX HTTP {status_code} on {path}: {json.dumps(body, ensure_ascii=True)[:300]}",
                status_code=status_code,
                retryable=status_code in {408, 425, 429} or status_code >= 500,
                venue=self.venue,
            )
        if not isinstance(body, dict):
            raise ExchangeAPIError(f"Unexpected BingX body for {path}", retryable=False, venue=self.venue)
        code = str(body.get("code", "0"))
        if code not in ("0", "", "None"):
            message = str(body.get("msg") or "")
            raise ExchangeAPIError(
                self._business_error_message(path, code, message, body),
                status_code=status_code,
                code=code,
                payload=body,
                # Business/API errors NEVER trigger the `.pro` failover and are not
                # retried unless BingX explicitly signals a transient condition.
                retryable=self._is_transient_business_code(code, message),
                venue=self.venue,
            )
        return body

    def _business_error_message(self, path: str, code: str, message: str, body: dict[str, Any]) -> str:
        base = f"BingX API error on {path}: code={code} msg={message or json.dumps(body)[:200]}"
        if self._is_clock_drift(code, message):
            return (
                f"{base}. This is almost always machine clock drift — the request timestamp is outside "
                f"recvWindow={self._recv_window_ms}ms. Synchronize the system clock (NTP) and retry."
            )
        return base

    @staticmethod
    def _is_clock_drift(code: str, message: str) -> bool:
        if code in _CLOCK_DRIFT_CODES:
            return True
        lowered = message.lower()
        return any(token in lowered for token in _CLOCK_DRIFT_TOKENS)

    @classmethod
    def _is_transient_business_code(cls, code: str, message: str) -> bool:
        if cls._is_clock_drift(code, message):
            return False
        if any(code.startswith(prefix) for prefix in _PERMANENT_CODE_PREFIXES):
            return False
        lowered = message.lower()
        return "too many" in lowered or "rate limit" in lowered or "busy" in lowered

    async def _throttle(self, route: str) -> None:
        if self._route_min_interval <= 0:
            return
        last = self._last_route_call.get(route)
        now = time.monotonic()
        if last is not None:
            wait = self._route_min_interval - (now - last)
            if wait > 0:
                await self._sleep(wait)
        self._last_route_call[route] = time.monotonic()

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        if self._market_cache_seconds <= 0:
            return None
        entry = self._market_cache.get(key)
        if entry is None:
            return None
        stored_at, body = entry
        if time.monotonic() - stored_at > self._market_cache_seconds:
            self._market_cache.pop(key, None)
            return None
        return body

    def _cache_put(self, key: str, body: dict[str, Any]) -> None:
        if self._market_cache_seconds <= 0:
            return
        self._market_cache[key] = (time.monotonic(), body)

    @staticmethod
    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)

    # ------------------------------------------------------------------
    # internals: parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_vst_host(url: str) -> bool:
        return url in {BINGX_VST_BASE_URL, BINGX_VST_FALLBACK_BASE_URL}

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _interval_millis(interval: str) -> int:
        unit = interval[-1]
        value = int(interval[:-1])
        factor = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}.get(unit, 60_000)
        return value * factor

    @classmethod
    def _kline_row(cls, item: Any) -> dict[str, float] | None:
        if isinstance(item, dict):
            timestamp = item.get("time") or item.get("openTime") or item.get("t")
            if timestamp in (None, ""):
                return None
            return {
                "timestamp": float(timestamp),
                "open": float(item.get("open") or 0.0),
                "high": float(item.get("high") or 0.0),
                "low": float(item.get("low") or 0.0),
                "close": float(item.get("close") or 0.0),
                "volume": float(item.get("volume") or 0.0),
            }
        if isinstance(item, list) and len(item) >= 6:
            return {
                "timestamp": float(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
            }
        return None

    @staticmethod
    def _data_object(response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return {}

    @staticmethod
    def _order_row(response: dict[str, Any]) -> dict[str, Any]:
        data = response.get("data")
        if isinstance(data, dict):
            order = data.get("order")
            if isinstance(order, dict) and order:
                return order
            if data.get("orderId") not in (None, ""):
                return data
            orders = data.get("orders")
            if isinstance(orders, list) and orders and isinstance(orders[0], dict):
                return orders[0]
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return {}

    def _match_contract_row(self, response: dict[str, Any], venue_symbol: str) -> dict[str, Any]:
        data = response.get("data")
        rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
        for row in rows:
            if isinstance(row, dict) and str(row.get("symbol", "")).upper() == venue_symbol:
                return row
        raise ExchangeAPIError(
            f"Missing BingX contract metadata for {venue_symbol}",
            retryable=False,
            venue=self.venue,
        )

    @classmethod
    def _depth_notional(cls, levels: list[Any]) -> Decimal:
        total = Decimal("0")
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                continue
            price = cls._decimal(level[0])
            size = cls._decimal(level[1])
            if price <= 0 or size <= 0:
                continue
            total += price * size
        return total

    @staticmethod
    def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
        if value in (None, ""):
            return default
        try:
            return Decimal(str(value))
        except Exception:
            return default

    @staticmethod
    def _optional_decimal(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _min_optional_int(*values: Any) -> int | None:
        parsed: list[int] = []
        for value in values:
            if value in (None, ""):
                continue
            try:
                parsed.append(int(float(str(value))))
            except (TypeError, ValueError):
                continue
        return min(parsed) if parsed else None

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes", "open", "on"}

    @staticmethod
    def _round_down_to_precision(value: Decimal, precision: int) -> Decimal:
        quantum = Decimal(1).scaleb(-max(int(precision), 0))
        return value.quantize(quantum, rounding=ROUND_DOWN)

    @staticmethod
    def _format_quantity(value: Decimal) -> str:
        return format(value.normalize(), "f")

    @classmethod
    def _format_price(cls, price: float, precision: int) -> str:
        rounded = cls._round_down_to_precision(Decimal(str(price)), precision)
        return format(rounded, "f")

    @staticmethod
    def _millis_to_datetime(value: Any):
        from datetime import datetime, timezone

        if value in (None, ""):
            return None
        try:
            millis = int(float(str(value)))
        except (TypeError, ValueError):
            return None
        if millis <= 0:
            return None
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
