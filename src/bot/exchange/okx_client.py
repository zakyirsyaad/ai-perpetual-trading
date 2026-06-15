from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from bot.models import PositionDirection


class OKXAPIError(Exception):
    """Raised when OKX returns a non-success response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        payload: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload
        self.retryable = retryable


@dataclass(slots=True)
class OKXInstrument:
    inst_id: str
    contract_value: Decimal
    lot_size: Decimal
    min_size: Decimal
    tick_size: Decimal


@dataclass(slots=True)
class OKXOrderFill:
    order_id: str
    avg_price: float | None
    filled_size: float
    base_quantity: float
    fee_usdt: float
    pnl_usdt: float | None
    state: str | None
    raw: dict[str, Any]


@dataclass(slots=True)
class OKXTradeFeeRates:
    maker_fee_pct: float | None
    taker_fee_pct: float
    raw: dict[str, Any]


@dataclass(slots=True)
class OKXOrderBook:
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
class OKXFundingRateInfo:
    symbol: str
    funding_rate: float
    next_funding_time: datetime | None
    raw: dict[str, Any]


@dataclass(slots=True)
class OKXPositionDetails:
    symbol: str
    exchange_size: float
    base_quantity: float
    avg_price: float | None
    mark_price: float | None
    liquidation_price: float | None
    pos_side: str | None
    raw: dict[str, Any]


class OKXFuturesClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        base_url: str,
        demo_trading: bool = True,
        margin_mode: str = "cross",
        instrument_type: str = "SWAP",
        timeout_seconds: int = 20,
        insecure_ssl: bool = False,
        debug_http: bool = False,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key.encode("utf-8")
        self._passphrase = passphrase
        self._base_url = base_url.rstrip("/")
        self._demo_trading = demo_trading
        self._margin_mode = margin_mode
        self._instrument_type = instrument_type.upper()
        self._debug_http = debug_http
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            verify=(not insecure_ssl),
        )
        self._instruments: dict[str, OKXInstrument] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def set_position_mode(self, position_mode: str) -> dict[str, Any]:
        return await self._signed_request(
            "POST",
            "/api/v5/account/set-position-mode",
            {"posMode": position_mode},
        )

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        inst_id = self._normalize_symbol(symbol)
        payload = {
            "instId": inst_id,
            "lever": str(leverage),
            "mgnMode": self._margin_mode,
        }
        if self._margin_mode == "isolated":
            responses: dict[str, Any] = {}
            for pos_side in ("long", "short"):
                responses[pos_side] = await self._signed_request(
                    "POST",
                    "/api/v5/account/set-leverage",
                    {**payload, "posSide": pos_side},
                )
            return {"code": "0", "msg": "", "data": responses}
        return await self._signed_request("POST", "/api/v5/account/set-leverage", payload)

    async def get_last_price(self, symbol: str) -> float:
        inst_id = self._normalize_symbol(symbol)
        resp = await self._public_request("GET", "/api/v5/market/ticker", {"instId": inst_id})
        row = self._first_data_row(resp, f"Missing ticker for {inst_id}")
        price = row.get("last") or row.get("idxPx") or row.get("markPx")
        if price is None:
            raise OKXAPIError(f"Missing last price for {inst_id}: {json.dumps(resp)}")
        return float(price)

    async def place_market_order(
        self,
        symbol: str,
        direction: PositionDirection,
        size: float,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        side, pos_side = self._order_sides(direction, reduce_only=reduce_only)
        payload: dict[str, Any] = {
            "instId": self._normalize_symbol(symbol),
            "tdMode": self._margin_mode,
            "side": side,
            "ordType": "market",
            "sz": self._format_decimal(Decimal(str(size))),
        }
        if self._instrument_type in {"SWAP", "FUTURES"}:
            payload["posSide"] = pos_side
        if reduce_only:
            payload["reduceOnly"] = "true"
        return await self._signed_request("POST", "/api/v5/trade/order", payload)

    async def flatten_position(self, symbol: str) -> dict[str, Any] | None:
        """Market-close any open position for symbol via a reduce-only order.

        Returns the order response, or None if there was nothing to close. Used to
        auto-flat an orphan exchange position found at startup (no local record, so
        its original SL/TP cannot be reconstructed)."""
        details = await self.get_symbol_position_details(symbol)
        if details.exchange_size <= 0:
            return None
        # In long_short (hedge) mode posSide tells us the side; default to LONG only
        # when it is genuinely absent (net mode), which still resolves to a valid close.
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

    async def place_limit_order(
        self,
        symbol: str,
        direction: PositionDirection,
        size: float,
        price: float,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> dict[str, Any]:
        side, pos_side = self._order_sides(direction, reduce_only=reduce_only)
        payload: dict[str, Any] = {
            "instId": self._normalize_symbol(symbol),
            "tdMode": self._margin_mode,
            "side": side,
            "ordType": "post_only" if post_only else "limit",
            "sz": self._format_decimal(Decimal(str(size))),
            "px": self._format_decimal(Decimal(str(price))),
        }
        if self._instrument_type in {"SWAP", "FUTURES"}:
            payload["posSide"] = pos_side
        if reduce_only:
            payload["reduceOnly"] = "true"
        return await self._signed_request("POST", "/api/v5/trade/order", payload)

    async def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        return await self._signed_request(
            "POST",
            "/api/v5/trade/cancel-order",
            {"instId": self._normalize_symbol(symbol), "ordId": order_id},
        )

    async def get_position_info(self, symbol: str) -> dict[str, Any]:
        return await self._signed_request(
            "GET",
            "/api/v5/account/positions",
            {"instId": self._normalize_symbol(symbol)},
        )

    async def get_order_fill(self, symbol: str, order_id: str) -> OKXOrderFill | None:
        inst_id = self._normalize_symbol(symbol)
        resp = await self._signed_request(
            "GET",
            "/api/v5/trade/order",
            {"instId": inst_id, "ordId": order_id},
        )
        row = self._first_data_row(resp, "")
        if not row:
            return None

        avg_price = self._parse_optional_decimal(row.get("avgPx"))
        filled_size = self._parse_decimal(row.get("accFillSz") or row.get("fillSz") or row.get("sz"))
        instrument = await self._get_instrument(symbol)
        base_quantity = filled_size * instrument.contract_value if instrument.contract_value > 0 else filled_size
        fee = self._parse_optional_decimal(row.get("fee"))
        pnl = self._parse_optional_decimal(row.get("pnl"))
        return OKXOrderFill(
            order_id=order_id,
            avg_price=float(avg_price) if avg_price is not None and avg_price > 0 else None,
            filled_size=float(filled_size),
            base_quantity=float(abs(base_quantity)),
            fee_usdt=float(abs(fee)) if fee is not None else 0.0,
            pnl_usdt=float(pnl) if pnl is not None else None,
            state=str(row.get("state") or "") or None,
            raw=row,
        )

    async def get_trade_fee_rates(self, symbol: str) -> OKXTradeFeeRates:
        inst_id = self._normalize_symbol(symbol)
        params: dict[str, Any] = {"instType": self._instrument_type}
        if self._instrument_type in {"SWAP", "FUTURES", "OPTION"}:
            params["instFamily"] = self._instrument_family(inst_id)
        else:
            params["instId"] = inst_id

        try:
            resp = await self._signed_request("GET", "/api/v5/account/trade-fee", params)
        except OKXAPIError:
            if "instFamily" not in params:
                raise
            fallback_params = {"instType": self._instrument_type, "instId": inst_id}
            resp = await self._signed_request("GET", "/api/v5/account/trade-fee", fallback_params)

        row = self._first_data_row(resp, f"Missing fee rates for {inst_id}")
        maker = self._first_fee_rate(row, ("maker", "makerU", "makerUSDT", "makerUSDC", "makerFeeRate"))
        taker = self._first_fee_rate(row, ("taker", "takerU", "takerUSDT", "takerUSDC", "takerFeeRate"))
        if taker is None:
            raise OKXAPIError(f"Missing taker fee rate for {inst_id}: {json.dumps(row, ensure_ascii=True)}")
        return OKXTradeFeeRates(
            maker_fee_pct=float(maker * Decimal("100")) if maker is not None else None,
            taker_fee_pct=float(abs(taker) * Decimal("100")),
            raw=row,
        )

    async def get_position_tiers(self, symbol: str, td_mode: str = "isolated") -> list[dict[str, Any]]:
        inst_id = self._normalize_symbol(symbol)
        params: dict[str, Any] = {
            "instType": self._instrument_type,
            "tdMode": td_mode,
        }
        if self._instrument_type in {"SWAP", "FUTURES", "OPTION"}:
            params["instFamily"] = self._instrument_family(inst_id)
        else:
            params["instId"] = inst_id
        resp = await self._public_request("GET", "/api/v5/public/position-tiers", params)
        data = resp.get("data")
        return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []

    async def validate_api_access(self, symbol: str) -> None:
        await self.get_last_price(symbol)
        await self.get_position_info(symbol)

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 120,
    ) -> list[dict[str, float]]:
        inst_id = self._normalize_symbol(symbol)
        resp = await self._public_request(
            "GET",
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": interval, "limit": str(limit)},
        )
        rows = resp.get("data") if isinstance(resp.get("data"), list) else []
        parsed: list[dict[str, float]] = []
        for item in rows:
            if not isinstance(item, list) or len(item) < 6:
                continue
            parsed.append(
                {
                    "timestamp": float(item[0]),
                    "open": float(item[1]),
                    "high": float(item[2]),
                    "low": float(item[3]),
                    "close": float(item[4]),
                    "volume": float(item[7] if len(item) > 7 and item[7] not in ("", None) else item[5]),
                }
            )
        parsed.sort(key=lambda row: row["timestamp"])
        return [row for row in parsed if row["close"] > 0]

    async def get_funding_rate(self, symbol: str) -> float:
        return (await self.get_funding_rate_info(symbol)).funding_rate

    async def get_funding_rate_info(self, symbol: str) -> OKXFundingRateInfo:
        inst_id = self._normalize_symbol(symbol)
        resp = await self._public_request("GET", "/api/v5/public/funding-rate", {"instId": inst_id})
        row = self._first_data_row(resp, "")
        value = row.get("fundingRate")
        funding_time = row.get("fundingTime") or row.get("nextFundingTime")
        return OKXFundingRateInfo(
            symbol=symbol,
            funding_rate=float(value) if value not in (None, "") else 0.0,
            next_funding_time=self._parse_unix_millis(funding_time),
            raw=row,
        )

    async def get_open_interest(self, symbol: str) -> float:
        inst_id = self._normalize_symbol(symbol)
        resp = await self._public_request(
            "GET",
            "/api/v5/public/open-interest",
            {"instType": self._instrument_type, "instId": inst_id},
        )
        row = self._first_data_row(resp, "")
        value = row.get("oiCcy") or row.get("oi")
        return float(value) if value not in (None, "") else 0.0

    async def has_open_position(self, symbol: str) -> bool:
        size, _ = await self.get_symbol_position_snapshot(symbol)
        return size > 0

    async def has_any_open_position(self, symbols: tuple[str, ...]) -> bool:
        for symbol in symbols:
            if await self.has_open_position(symbol):
                return True
        return False

    async def get_symbol_position_quantity(self, symbol: str) -> float:
        size, _ = await self.get_symbol_position_snapshot(symbol)
        return size

    async def get_symbol_position_snapshot(self, symbol: str) -> tuple[float, float | None]:
        """Return (absolute OKX position size, average entry price) for a symbol."""
        details = await self.get_symbol_position_details(symbol)
        return details.exchange_size, details.avg_price

    async def get_symbol_position_details(self, symbol: str) -> OKXPositionDetails:
        resp = await self.get_position_info(symbol)
        data = resp.get("data") if isinstance(resp.get("data"), list) else []
        target_inst_id = self._normalize_symbol(symbol).upper()
        for row in data:
            if not isinstance(row, dict):
                continue
            if str(row.get("instId", "")).upper() != target_inst_id:
                continue
            size = abs(self._parse_decimal(row.get("pos")))
            if size <= 0:
                continue
            instrument = await self._get_instrument(symbol)
            base_quantity = size * instrument.contract_value if instrument.contract_value > 0 else size
            avg_price = self._parse_decimal(
                row.get("avgPx") or row.get("openAvgPx") or row.get("markPx")
            )
            mark_price = self._parse_optional_decimal(row.get("markPx"))
            liquidation_price = self._parse_optional_decimal(row.get("liqPx"))
            return OKXPositionDetails(
                symbol=symbol,
                exchange_size=float(size),
                base_quantity=float(abs(base_quantity)),
                avg_price=float(avg_price) if avg_price > 0 else None,
                mark_price=float(mark_price) if mark_price is not None and mark_price > 0 else None,
                liquidation_price=(
                    float(liquidation_price)
                    if liquidation_price is not None and liquidation_price > 0
                    else None
                ),
                pos_side=str(row.get("posSide") or "") or None,
                raw=row,
            )
        return OKXPositionDetails(
            symbol=symbol,
            exchange_size=0.0,
            base_quantity=0.0,
            avg_price=None,
            mark_price=None,
            liquidation_price=None,
            pos_side=None,
            raw={},
        )

    async def calculate_order_size(self, symbol: str, base_quantity: float) -> float:
        instrument = await self._get_instrument(symbol)
        if instrument.contract_value <= 0:
            return float(base_quantity)
        raw_contracts = Decimal(str(base_quantity)) / instrument.contract_value
        contracts = self._round_down_to_step(raw_contracts, instrument.lot_size)
        if contracts < instrument.min_size:
            return 0.0
        return float(contracts)

    async def estimate_base_quantity(self, symbol: str, order_size: float) -> float:
        instrument = await self._get_instrument(symbol)
        return float(Decimal(str(order_size)) * instrument.contract_value)

    async def get_order_book(self, symbol: str, depth: int = 20) -> OKXOrderBook:
        inst_id = self._normalize_symbol(symbol)
        resp = await self._public_request(
            "GET",
            "/api/v5/market/books",
            {"instId": inst_id, "sz": str(depth)},
        )
        row = self._first_data_row(resp, f"Missing order book for {inst_id}")
        bids = row.get("bids") if isinstance(row.get("bids"), list) else []
        asks = row.get("asks") if isinstance(row.get("asks"), list) else []
        if not bids or not asks:
            raise OKXAPIError(f"Empty order book for {inst_id}: {json.dumps(row, ensure_ascii=True)}")

        best_bid = self._parse_decimal(bids[0][0])
        best_ask = self._parse_decimal(asks[0][0])
        if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
            raise OKXAPIError(f"Invalid order book top for {inst_id}: {json.dumps(row, ensure_ascii=True)}")

        instrument = await self._get_instrument(symbol)
        bid_depth = self._depth_notional(bids, instrument.contract_value)
        ask_depth = self._depth_notional(asks, instrument.contract_value)
        mid = (best_bid + best_ask) / Decimal("2")
        spread_bps = ((best_ask - best_bid) / mid) * Decimal("10000")
        return OKXOrderBook(
            symbol=symbol,
            best_bid=float(best_bid),
            best_ask=float(best_ask),
            mid_price=float(mid),
            spread_bps=float(spread_bps),
            bid_depth_notional=float(bid_depth),
            ask_depth_notional=float(ask_depth),
            timestamp=self._parse_unix_millis(row.get("ts")),
            raw=row,
        )

    async def _get_instrument(self, symbol: str) -> OKXInstrument:
        inst_id = self._normalize_symbol(symbol)
        cached = self._instruments.get(inst_id)
        if cached is not None:
            return cached

        resp = await self._public_request(
            "GET",
            "/api/v5/public/instruments",
            {"instType": self._instrument_type, "instId": inst_id},
        )
        row = self._first_data_row(resp, f"Missing instrument metadata for {inst_id}")
        instrument = OKXInstrument(
            inst_id=inst_id,
            contract_value=self._parse_decimal(row.get("ctVal"), default=Decimal("1")),
            lot_size=self._parse_decimal(row.get("lotSz"), default=Decimal("1")),
            min_size=self._parse_decimal(row.get("minSz"), default=Decimal("1")),
            tick_size=self._parse_decimal(row.get("tickSz"), default=Decimal("0.01")),
        )
        self._instruments[inst_id] = instrument
        return instrument

    async def _public_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(self._is_retryable_exception),
            wait=wait_exponential(multiplier=0.8, min=0.8, max=8),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                response = await self._client.request(
                    method.upper(),
                    f"{self._base_url}{path}",
                    params=params or {},
                )
                body = self._parse_response(response, "public")
                if body.get("code") not in ("0", 0, None):
                    raise OKXAPIError(f"OKX public API error: {body}")
                return body
        raise OKXAPIError("Retry loop finished unexpectedly on OKX public request.")

    async def _signed_request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        method_upper = method.upper()
        payload = dict(body or {})
        query = ""
        request_body = ""
        request_path = path

        if method_upper == "GET" and payload:
            query = urlencode(payload, doseq=True)
            request_path = f"{path}?{query}"
        elif method_upper != "GET":
            request_body = json.dumps(payload, separators=(",", ":"))

        url = f"{self._base_url}{request_path if method_upper == 'GET' else path}"
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(self._is_retryable_exception),
            wait=wait_exponential(multiplier=0.8, min=0.8, max=8),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                timestamp = self._timestamp()
                headers = self._signed_headers(timestamp, method_upper, request_path, request_body)
                if method_upper == "GET":
                    response = await self._client.get(url, headers=headers)
                else:
                    response = await self._client.request(
                        method_upper,
                        url,
                        content=request_body,
                        headers=headers,
                    )
                body_json = self._parse_response(response, "signed")
                if body_json.get("code") not in ("0", 0, None):
                    raise OKXAPIError(f"OKX signed API error: {body_json}")
                return body_json
        raise OKXAPIError("Retry loop finished unexpectedly on OKX signed request.")

    def _signed_headers(
        self,
        timestamp: str,
        method: str,
        request_path: str,
        body: str,
    ) -> dict[str, str]:
        prehash = f"{timestamp}{method}{request_path}{body}"
        digest = hmac.new(self._secret_key, prehash.encode("utf-8"), hashlib.sha256).digest()
        headers = {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self._api_key,
            "OK-ACCESS-SIGN": base64.b64encode(digest).decode("utf-8"),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
        }
        if self._demo_trading:
            headers["x-simulated-trading"] = "1"
        return headers

    def _parse_response(self, response: httpx.Response, api_kind: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            content_type = response.headers.get("content-type", "")
            snippet = (response.text or "")[:400]
            raise OKXAPIError(
                f"Non-JSON response from OKX {api_kind} API "
                f"(status={response.status_code}, content-type={content_type!r}): {snippet}"
            )
        if self._debug_http:
            print(json.dumps(body, ensure_ascii=True)[:1000])
        if response.is_error:
            payload = body if isinstance(body, dict) else {"body": body}
            code = str(payload.get("code") or "") if isinstance(payload, dict) else None
            hint = ""
            if api_kind == "signed" and response.status_code in {401, 403}:
                hint = (
                    " Check OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, demo-vs-live API key, "
                    "regional OKX_BASE_URL, IP allowlist, and system clock."
                )
            raise OKXAPIError(
                f"OKX {api_kind} API HTTP {response.status_code}: "
                f"{json.dumps(payload, ensure_ascii=True)}.{hint}",
                status_code=response.status_code,
                code=code,
                payload=payload,
                retryable=response.status_code in {408, 409, 425, 429} or response.status_code >= 500,
            )
        return body

    def _normalize_symbol(self, symbol: str) -> str:
        clean = symbol.upper().replace("/", "-")
        if self._instrument_type == "SWAP" and not clean.endswith("-SWAP"):
            return f"{clean}-SWAP"
        return clean

    @staticmethod
    def _instrument_family(inst_id: str) -> str:
        for suffix in ("-SWAP", "-FUTURES"):
            if inst_id.endswith(suffix):
                return inst_id[: -len(suffix)]
        parts = inst_id.split("-")
        return "-".join(parts[:2]) if len(parts) >= 2 else inst_id

    @staticmethod
    def _order_sides(direction: PositionDirection, reduce_only: bool) -> tuple[str, str]:
        if direction == PositionDirection.LONG:
            return ("sell", "long") if reduce_only else ("buy", "long")
        return ("buy", "short") if reduce_only else ("sell", "short")

    @staticmethod
    def _first_data_row(response: dict[str, Any], error_message: str) -> dict[str, Any]:
        data = response.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        if error_message:
            raise OKXAPIError(f"{error_message}: {json.dumps(response)}")
        return {}

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _parse_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
        if value in (None, ""):
            return default
        try:
            return Decimal(str(value))
        except Exception:
            return default

    @staticmethod
    def _parse_optional_decimal(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @classmethod
    def _first_fee_rate(cls, row: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
        candidates: list[dict[str, Any]] = [row]
        for value in row.values():
            if isinstance(value, dict):
                candidates.append(value)
            elif isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
        for candidate in candidates:
            for key in keys:
                parsed = cls._parse_optional_decimal(candidate.get(key))
                if parsed is not None:
                    return parsed
        return None

    @classmethod
    def _depth_notional(cls, levels: list[Any], contract_value: Decimal) -> Decimal:
        total = Decimal("0")
        multiplier = contract_value if contract_value > 0 else Decimal("1")
        for level in levels:
            if not isinstance(level, list) or len(level) < 2:
                continue
            price = cls._parse_decimal(level[0])
            size = cls._parse_decimal(level[1])
            if price <= 0 or size <= 0:
                continue
            total += price * size * multiplier
        return total

    @staticmethod
    def _parse_unix_millis(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            millis = int(str(value))
        except Exception:
            return None
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)

    @staticmethod
    def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _is_retryable_exception(exc: BaseException) -> bool:
        if isinstance(exc, OKXAPIError):
            return exc.retryable
        return isinstance(exc, httpx.HTTPError)

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        normalized = value.normalize()
        if normalized == normalized.to_integral():
            return str(normalized.quantize(Decimal("1")))
        return format(normalized, "f")
