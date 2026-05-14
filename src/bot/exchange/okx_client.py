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
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from bot.models import PositionDirection


class OKXAPIError(Exception):
    """Raised when OKX returns a non-success response."""


@dataclass(slots=True)
class OKXInstrument:
    inst_id: str
    contract_value: Decimal
    lot_size: Decimal
    min_size: Decimal


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

    async def get_position_info(self, symbol: str) -> dict[str, Any]:
        return await self._signed_request(
            "GET",
            "/api/v5/account/positions",
            {"instId": self._normalize_symbol(symbol)},
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
        inst_id = self._normalize_symbol(symbol)
        resp = await self._public_request("GET", "/api/v5/public/funding-rate", {"instId": inst_id})
        row = self._first_data_row(resp, "")
        value = row.get("fundingRate")
        return float(value) if value not in (None, "") else 0.0

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
        resp = await self.get_position_info(symbol)
        data = resp.get("data") if isinstance(resp.get("data"), list) else []
        for row in data:
            if not isinstance(row, dict):
                continue
            if str(row.get("instId", "")).upper() != self._normalize_symbol(symbol).upper():
                continue
            if abs(self._parse_decimal(row.get("pos"))) > 0:
                return True
        return False

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
        )
        self._instruments[inst_id] = instrument
        return instrument

    async def _public_request(self, method: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.HTTPError, OKXAPIError)),
            wait=wait_exponential(multiplier=0.8, min=0.8, max=8),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                response = await self._client.request(method.upper(), url, params=params or {})
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
            query = urlencode(payload)
            request_path = f"{path}?{query}"
        elif method_upper != "GET":
            request_body = json.dumps(payload, separators=(",", ":"))

        url = f"{self._base_url}{path}"
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((httpx.HTTPError, OKXAPIError)),
            wait=wait_exponential(multiplier=0.8, min=0.8, max=8),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                timestamp = self._timestamp()
                headers = self._signed_headers(timestamp, method_upper, request_path, request_body)
                if method_upper == "GET":
                    response = await self._client.get(url, params=query, headers=headers)
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
        response.raise_for_status()
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
        return body

    def _normalize_symbol(self, symbol: str) -> str:
        clean = symbol.upper().replace("/", "-")
        if self._instrument_type == "SWAP" and not clean.endswith("-SWAP"):
            return f"{clean}-SWAP"
        return clean

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
    def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        normalized = value.normalize()
        if normalized == normalized.to_integral():
            return str(normalized.quantize(Decimal("1")))
        return format(normalized, "f")
