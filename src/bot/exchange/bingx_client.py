from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from bot.models import PositionDirection


class BingXAPIError(Exception):
    """Raised when BingX returns a non-success response."""

    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        payload: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload
        self.retryable = retryable


class BingXFuturesClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: str,
        timeout_seconds: int = 15,
        insecure_ssl: bool = False,
        debug_http: bool = False,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key.encode("utf-8")
        self._base_url = base_url.rstrip("/")
        self._debug_http = debug_http
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            verify=(not insecure_ssl),
            follow_redirects=True,
        )
        self._time_offset_ms = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def set_leverage(self, symbol: str, leverage: int) -> dict[str, Any]:
        payload = {"symbol": self._normalize_symbol(symbol), "leverage": leverage}
        try:
            return await self._signed_request("POST", "/openApi/swap/v2/trade/leverage", payload)
        except BingXAPIError as exc:
            # Some BingX environments require side-specific leverage updates.
            error_text = str(exc).lower()
            if "side" not in error_text or "required" not in error_text:
                raise

            long_resp = await self._signed_request(
                "POST",
                "/openApi/swap/v2/trade/leverage",
                {**payload, "side": "LONG"},
            )
            short_resp = await self._signed_request(
                "POST",
                "/openApi/swap/v2/trade/leverage",
                {**payload, "side": "SHORT"},
            )
            return {
                "code": 0,
                "msg": "leverage set for LONG and SHORT",
                "data": {"LONG": long_resp.get("data"), "SHORT": short_resp.get("data")},
            }

    async def get_last_price(self, symbol: str) -> float:
        normalized = self._normalize_symbol(symbol)
        resp = await self._public_request("GET", "/openApi/swap/v2/quote/price", {"symbol": normalized})
        data = resp.get("data") or {}
        price = data.get("price")
        if price is None:
            raise BingXAPIError(f"Missing price for symbol {normalized}: {json.dumps(resp)}")
        return float(price)

    async def place_market_order(
        self,
        symbol: str,
        direction: PositionDirection,
        quantity: float,
        reduce_only: bool = False,
    ) -> dict[str, Any]:
        side, position_side = self._order_sides(direction, reduce_only=reduce_only)
        payload = {
            "symbol": self._normalize_symbol(symbol),
            "type": "MARKET",
            "side": side,
            "positionSide": position_side,
            "quantity": self._format_quantity(quantity),
        }
        return await self._signed_request("POST", "/openApi/swap/v2/trade/order", payload)

    async def get_position_info(self, symbol: str) -> dict[str, Any]:
        return await self._signed_request(
            "GET", "/openApi/swap/v2/user/positions", {"symbol": self._normalize_symbol(symbol)}
        )

    async def validate_api_access(self, symbol: str) -> None:
        await self.get_last_price(symbol)
        await self.get_position_info(symbol)

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1m",
        limit: int = 120,
    ) -> list[dict[str, float]]:
        normalized = self._normalize_symbol(symbol)
        resp = await self._public_request(
            "GET",
            "/openApi/swap/v3/quote/klines",
            {"symbol": normalized, "interval": interval, "limit": str(limit)},
        )
        items = self._extract_kline_items(resp)
        parsed: list[dict[str, float]] = []
        for item in items:
            if isinstance(item, list) and len(item) >= 6:
                parsed.append(
                    {
                        "open": float(item[1]),
                        "high": float(item[2]),
                        "low": float(item[3]),
                        "close": float(item[4]),
                        "volume": float(item[5]),
                    }
                )
                continue
            if isinstance(item, dict):
                parsed.append(
                    {
                        "open": float(item.get("open") or item.get("o") or 0),
                        "high": float(item.get("high") or item.get("h") or 0),
                        "low": float(item.get("low") or item.get("l") or 0),
                        "close": float(item.get("close") or item.get("c") or 0),
                        "volume": float(item.get("volume") or item.get("v") or 0),
                    }
                )
        return [x for x in parsed if x["close"] > 0]

    async def get_funding_rate(self, symbol: str) -> float:
        """Current funding rate for the symbol (e.g. 0.0001 = 0.01%). Returns 0.0 if unavailable."""
        normalized = self._normalize_symbol(symbol)
        resp = await self._public_request(
            "GET",
            "/openApi/swap/v2/quote/fundingRate",
            {"symbol": normalized},
        )
        data = resp.get("data")
        if data is None:
            return 0.0
        if isinstance(data, list) and data:
            data = data[0]
        rate = data.get("fundingRate") or data.get("funding_rate")
        if rate is None:
            return 0.0
        try:
            return float(rate)
        except (TypeError, ValueError):
            return 0.0

    async def get_open_interest(self, symbol: str) -> float:
        """Open interest in contracts (or base units). Returns 0.0 if unavailable."""
        normalized = self._normalize_symbol(symbol)
        resp = await self._public_request(
            "GET",
            "/openApi/swap/v2/quote/openInterest",
            {"symbol": normalized},
        )
        data = resp.get("data")
        if data is None:
            return 0.0
        if isinstance(data, list) and data:
            data = data[0]
        oi = data.get("openInterest") or data.get("open_interest") or data.get("sumOpenInterest")
        if oi is None:
            return 0.0
        try:
            return float(oi)
        except (TypeError, ValueError):
            return 0.0

    async def has_open_position(self, symbol: str) -> bool:
        resp = await self.get_position_info(symbol)
        positions = self._extract_positions(resp)
        normalized = self._normalize_symbol(symbol)
        for pos in positions:
            pos_symbol = str(pos.get("symbol") or pos.get("s") or "").upper()
            if pos_symbol and pos_symbol != normalized.upper():
                continue
            qty = self._extract_position_quantity(pos)
            if abs(qty) > 0:
                return True
        return False

    async def has_any_open_position(self, symbols: tuple[str, ...]) -> bool:
        for symbol in symbols:
            if await self.has_open_position(symbol):
                return True
        return False

    async def get_symbol_position_quantity(self, symbol: str) -> float:
        """Return absolute position quantity for a symbol on the exchange, or 0.0 if none."""
        quantity, _ = await self.get_symbol_position_snapshot(symbol)
        return quantity

    async def get_symbol_position_snapshot(self, symbol: str) -> tuple[float, float | None]:
        """Return (absolute quantity, average entry price) for a symbol, or (0.0, None)."""
        resp = await self.get_position_info(symbol)
        positions = self._extract_positions(resp)
        normalized = self._normalize_symbol(symbol)
        for pos in positions:
            pos_symbol = str(pos.get("symbol") or pos.get("s") or "").upper()
            if pos_symbol and pos_symbol != normalized.upper():
                continue
            qty = self._extract_position_quantity(pos)
            if abs(qty) > 0:
                return abs(qty), self._extract_position_entry_price(pos)
        return 0.0, None

    async def _public_request(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(self._is_retryable_exception),
            wait=wait_exponential(multiplier=0.8, min=0.8, max=8),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                response = await self._client.request(method, url, params=params)
                response.raise_for_status()
                try:
                    body = response.json()
                except ValueError:
                    # Common when a proxy/SSL inspection returns HTML or an empty body.
                    ct = response.headers.get("content-type", "")
                    content_bytes = response.content or b""
                    snippet = (response.text or "")[:400]
                    raise BingXAPIError(
                        f"Non-JSON response from BingX public API (status={response.status_code}). "
                        f"content-type={ct!r} body_len={len(content_bytes)} url={response.url!s}. "
                        f"Body (truncated): {snippet}"
                    )
                if body.get("code") not in (0, "0"):
                    self._raise_api_error(body, signed=False)
                return body

        raise BingXAPIError("Retry loop finished unexpectedly on public request.")

    async def _signed_request(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        base_params = dict(params)
        headers = {"X-BX-APIKEY": self._api_key}
        url = f"{self._base_url}{path}"

        async for attempt in AsyncRetrying(
            retry=retry_if_exception(self._is_retryable_exception),
            wait=wait_exponential(multiplier=0.8, min=0.8, max=8),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                # Re-sign on every retry to keep timestamp fresh.
                signed_params = dict(base_params)
                signed_params["timestamp"] = str(self._now_ms())
                signed_params["recvWindow"] = "10000"
                query_string = urlencode(sorted(signed_params.items()), doseq=True)
                signature = hmac.new(
                    self._secret_key, query_string.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                signed_query = f"{query_string}&signature={signature}"

                if method.upper() == "GET":
                    response = await self._client.get(url, params=signed_query, headers=headers)
                else:
                    response = await self._client.request(
                        method.upper(),
                        url,
                        content=signed_query,
                        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                    )

                response.raise_for_status()
                try:
                    body = response.json()
                except ValueError:
                    ct = response.headers.get("content-type", "")
                    content_bytes = response.content or b""
                    snippet = (response.text or "")[:400]
                    raise BingXAPIError(
                        f"Non-JSON response from BingX signed API (status={response.status_code}). "
                        f"content-type={ct!r} body_len={len(content_bytes)} url={response.url!s}. "
                        f"Body (truncated): {snippet}"
                    )
                if body.get("code") not in (0, "0"):
                    msg = str(body.get("msg", "")).lower()
                    if "timestamp" in msg and "invalid" in msg:
                        await self._sync_server_time_offset()
                    self._raise_api_error(body, signed=True)
                return body

        raise BingXAPIError("Retry loop finished unexpectedly on signed request.")

    @staticmethod
    def _is_retryable_exception(exc: BaseException) -> bool:
        return not isinstance(exc, BingXAPIError) or exc.retryable

    def _raise_api_error(self, body: dict[str, Any], *, signed: bool) -> None:
        code = body.get("code")
        raw_msg = str(body.get("msg", "")).strip() or "Unknown BingX error"
        msg = raw_msg.lower()
        prefix = "Signed" if signed else "Public"
        details = f"{prefix} API error (code={code}, endpoint={self._base_url}): {raw_msg}"
        retryable = True
        hints: list[str] = []

        if code == 100413 or "incorrect apikey" in msg or "incorrect api key" in msg:
            retryable = False
            hints.append("The configured BingX API key was rejected by the current endpoint.")
            if self._is_demo_endpoint():
                hints.append(
                    "You are calling the DEMO/VST endpoint; use a VST API key there, or switch "
                    "BINGX_BASE_URL to https://open-api.bingx.com if these are live keys."
                )
            else:
                hints.append(
                    "You are calling the LIVE endpoint; confirm the key belongs to the live "
                    "account and has futures permissions enabled."
                )
        elif "signature" in msg and ("invalid" in msg or "error" in msg):
            retryable = False
            hints.append(
                "The request signature was rejected. Re-check BINGX_SECRET_KEY and ensure it "
                "matches the API key for this BingX account."
            )
        elif "permission" in msg or "authorize" in msg or "forbidden" in msg:
            retryable = False
            hints.append(
                "The API key appears to be missing the required BingX permissions for this action."
            )
        elif "timestamp" in msg and "invalid" in msg:
            hints.append("Server time offset was refreshed; retrying with a new timestamp is allowed.")

        hint_text = f" {' '.join(hints)}" if hints else ""
        raise BingXAPIError(
            f"{details}{hint_text}",
            code=code,
            payload=body,
            retryable=retryable,
        )

    def _is_demo_endpoint(self) -> bool:
        return "open-api-vst.bingx.com" in self._base_url.lower()

    @staticmethod
    def _format_quantity(quantity: float) -> str:
        # Keep quantity deterministic and avoid rejection due to too many decimals.
        return str(Decimal(str(quantity)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))

    @staticmethod
    def _order_sides(direction: PositionDirection, reduce_only: bool) -> tuple[str, str]:
        if direction == PositionDirection.LONG:
            return ("SELL", "LONG") if reduce_only else ("BUY", "LONG")
        return ("BUY", "SHORT") if reduce_only else ("SELL", "SHORT")

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return symbol.replace("/", "-")

    @staticmethod
    def _extract_positions(response: dict[str, Any]) -> list[dict[str, Any]]:
        data = response.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("positions", "list", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
            return [data]
        return []

    @staticmethod
    def _extract_position_quantity(position: dict[str, Any]) -> float:
        keys = ("positionAmt", "positionSize", "availableAmt", "qty", "positionQty")
        for key in keys:
            value = position.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    @staticmethod
    def _extract_position_entry_price(position: dict[str, Any]) -> float | None:
        keys = (
            "avgPrice",
            "avgOpenPrice",
            "entryPrice",
            "positionAvgPrice",
            "openPrice",
            "price",
        )
        for key in keys:
            value = position.get(key)
            if value is None:
                continue
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
        return None

    @staticmethod
    def _extract_kline_items(response: dict[str, Any]) -> list[Any]:
        data = response.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("klines", "kline", "items", "list", "candles"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return []

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    async def _sync_server_time_offset(self) -> None:
        for endpoint in (
            "/openApi/swap/v2/server/time",
            "/openApi/swap/v3/server/time",
            "/openApi/spot/v1/common/serverTime",
        ):
            try:
                response = await self._client.get(f"{self._base_url}{endpoint}")
                response.raise_for_status()
                body = response.json()
                server_ms = self._extract_server_time_ms(body)
                if server_ms:
                    local_ms = int(time.time() * 1000)
                    self._time_offset_ms = server_ms - local_ms
                    return
            except Exception:
                continue

    @staticmethod
    def _extract_server_time_ms(payload: Any) -> int | None:
        if isinstance(payload, dict):
            for key in ("serverTime", "time", "ts", "timestamp"):
                if key in payload:
                    value = BingXFuturesClient._parse_time_value(payload[key])
                    if value:
                        return value
            for value in payload.values():
                found = BingXFuturesClient._extract_server_time_ms(value)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = BingXFuturesClient._extract_server_time_ms(value)
                if found:
                    return found
        return None

    @staticmethod
    def _parse_time_value(value: Any) -> int | None:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        if n > 10_000_000_000:
            return n
        if n > 1_000_000_000:
            return n * 1000
        return None

