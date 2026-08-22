from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from decimal import Decimal
from typing import Any

import httpx

from bot.exchange.bingx_client import (
    BINGX_VST_BASE_URL,
    BingXFuturesClient,
    build_signature,
    canonical_param_string,
)
from bot.exchange.contracts import ExchangeAPIError
from bot.models import PositionDirection


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200, url: str = "https://open-api-vst.bingx.com/x") -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload
        self.request = httpx.Request("GET", url)

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> Any:
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class _RecordingClient:
    """Minimal httpx.AsyncClient stand-in that records requests."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kwargs: Any):
        self.requests.append({"method": method, "url": url, **kwargs})
        item = self._responses.pop(0) if self._responses else _FakeResponse({"code": 0, "data": {}})
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:  # pragma: no cover - trivial
        return None


def _client(responses: list[Any] | None = None, **kwargs: Any) -> BingXFuturesClient:
    client = BingXFuturesClient(
        api_key="test-key",
        secret_key="test-secret",
        base_url=kwargs.pop("base_url", BINGX_VST_BASE_URL),
        env=kwargs.pop("env", "vst"),
        **kwargs,
    )
    client._client = _RecordingClient(responses or [])  # type: ignore[assignment]
    return client


_CONTRACT_ROW = {
    "symbol": "BTC-USDT",
    "quantityPrecision": 4,
    "pricePrecision": 1,
    "tradeMinQuantity": 0.0001,
    "tradeMinUSDT": 2,
    "maxLongLeverage": 125,
    "maxShortLeverage": 125,
    "apiStateOpen": "true",
    "status": 1,
}


class SigningTests(unittest.TestCase):
    def test_canonical_param_string_is_ascii_key_ordered(self) -> None:
        params = {"symbol": "BTC-USDT", "recvWindow": 5000, "timestamp": 1, "amount": 2}
        self.assertEqual(
            canonical_param_string(params),
            "amount=2&recvWindow=5000&symbol=BTC-USDT&timestamp=1",
        )

    def test_canonical_param_string_drops_none(self) -> None:
        self.assertEqual(canonical_param_string({"a": 1, "b": None}), "a=1")

    def test_signature_golden_vector(self) -> None:
        payload = "recvWindow=5000&symbol=BTC-USDT&timestamp=1700000000000"
        expected = hmac.new(b"test-secret", payload.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(build_signature("test-secret", payload), expected)
        self.assertEqual(expected, expected.lower())

    def test_bool_params_are_lowercased(self) -> None:
        self.assertEqual(canonical_param_string({"z": True, "a": False}), "a=false&z=true")


class EnvironmentIsolationTests(unittest.TestCase):
    def test_live_base_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BingXFuturesClient(
                api_key="k",
                secret_key="s",
                base_url="https://open-api.bingx.com",
                env="vst",
            )

    def test_non_vst_env_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BingXFuturesClient(api_key="k", secret_key="s", base_url=BINGX_VST_BASE_URL, env="live")

    def test_fallback_host_is_pro_vst_domain(self) -> None:
        client = _client()
        self.assertEqual(client.fallback_base_url, "https://open-api-vst.bingx.pro")


class SymbolAndHeaderTests(unittest.IsolatedAsyncioTestCase):
    def test_symbol_conversion(self) -> None:
        client = _client()
        self.assertEqual(client.venue_symbol("BTC/USDT"), "BTC-USDT")
        self.assertEqual(client.venue_symbol("eth-usdt"), "ETH-USDT")

    async def test_signed_request_sends_headers_timestamp_and_recv_window(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": {"ok": 1}})])
        await client._signed_request("GET", "/openApi/swap/v2/user/positions", {"symbol": "BTC-USDT"})
        sent = client._client.requests[0]  # type: ignore[attr-defined]
        self.assertEqual(sent["headers"]["X-BX-APIKEY"], "test-key")
        self.assertIn("X-SOURCE-KEY", sent["headers"])
        params = sent["params"]
        self.assertEqual(params["recvWindow"], 5000)
        self.assertGreater(int(params["timestamp"]), 1_600_000_000_000)
        self.assertEqual(
            params["signature"],
            build_signature("test-secret", canonical_param_string({k: v for k, v in params.items() if k != "signature"})),
        )

    async def test_business_error_is_not_retried_on_pro_domain(self) -> None:
        client = _client([_FakeResponse({"code": 100413, "msg": "Incorrect apiKey"})])
        with self.assertRaises(ExchangeAPIError) as ctx:
            await client._signed_request("GET", "/openApi/swap/v2/user/positions", {})
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(client._client.requests), 1)  # type: ignore[attr-defined]

    async def test_network_error_falls_back_to_pro_domain(self) -> None:
        client = _client(
            [
                httpx.ConnectError("boom"),
                _FakeResponse({"code": 0, "data": {"ok": 1}}),
            ]
        )
        body = await client._public_request("GET", "/openApi/swap/v2/quote/price", {"symbol": "BTC-USDT"})
        self.assertEqual(body["data"]["ok"], 1)
        urls = [item["url"] for item in client._client.requests]  # type: ignore[attr-defined]
        self.assertTrue(urls[0].startswith(BINGX_VST_BASE_URL))
        self.assertTrue(urls[1].startswith("https://open-api-vst.bingx.pro"))

    async def test_clock_drift_error_is_reported_clearly(self) -> None:
        client = _client([_FakeResponse({"code": 100421, "msg": "timestamp expired"})])
        with self.assertRaises(ExchangeAPIError) as ctx:
            await client._signed_request("GET", "/openApi/swap/v2/user/positions", {})
        self.assertIn("clock", str(ctx.exception).lower())


class ContractMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_contract_parsing_and_precision(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]})])
        instrument = await client.get_instrument("BTC/USDT")
        self.assertEqual(instrument.venue_symbol, "BTC-USDT")
        self.assertEqual(instrument.quantity_precision, 4)
        self.assertEqual(instrument.min_notional_usdt, Decimal("2"))
        self.assertEqual(instrument.max_leverage, 125)
        self.assertTrue(instrument.api_trading_enabled)

    async def test_instrument_is_cached(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]})])
        await client.get_instrument("BTC/USDT")
        await client.get_instrument("BTC/USDT")
        self.assertEqual(len(client._client.requests), 1)  # type: ignore[attr-defined]

    async def test_calculate_order_size_rounds_down_to_precision(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]})])
        size = await client.calculate_order_size("BTC/USDT", 0.123456789)
        self.assertEqual(size, 0.1234)

    async def test_calculate_order_size_returns_zero_below_min_quantity(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]})])
        self.assertEqual(await client.calculate_order_size("BTC/USDT", 0.00001), 0.0)

    async def test_base_quantity_is_identity_for_bingx(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]})])
        self.assertEqual(await client.estimate_base_quantity("BTC/USDT", 0.25), 0.25)

    async def test_position_tiers_expose_venue_leverage_cap(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]})])
        tiers = await client.get_position_tiers("BTC/USDT")
        self.assertEqual(tiers[0]["maxLever"], 125)


class MarketDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_last_price(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": {"price": "64000.5"}})])
        self.assertEqual(await client.get_last_price("BTC/USDT"), 64000.5)

    async def test_klines_filter_unclosed_candles(self) -> None:
        now_ms = 1_700_000_600_000
        rows = [
            {"time": 1_700_000_000_000, "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"},
            {"time": now_ms + 60_000, "open": "1", "high": "2", "low": "0.5", "close": "1.6", "volume": "11"},
        ]
        client = _client([_FakeResponse({"code": 0, "data": rows})])
        client._now_ms = lambda: now_ms  # type: ignore[assignment]
        parsed = await client.get_klines("BTC/USDT", interval="3m", limit=10)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["close"], 1.5)

    async def test_klines_use_v3_endpoint(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": []})])
        await client.get_klines("BTC/USDT")
        self.assertIn("/openApi/swap/v3/quote/klines", client._client.requests[0]["url"])  # type: ignore[attr-defined]

    async def test_order_book_parsing(self) -> None:
        payload = {"code": 0, "data": {"bids": [["100", "2"]], "asks": [["101", "3"]], "T": 1_700_000_000_000}}
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}), _FakeResponse(payload)])
        book = await client.get_order_book("BTC/USDT", depth=5)
        self.assertEqual(book.best_bid, 100.0)
        self.assertEqual(book.best_ask, 101.0)
        self.assertAlmostEqual(book.mid_price, 100.5)
        self.assertAlmostEqual(book.spread_bps, (1 / 100.5) * 10_000, places=6)
        self.assertAlmostEqual(book.bid_depth_notional, 200.0)
        self.assertAlmostEqual(book.ask_depth_notional, 303.0)

    async def test_order_book_rejects_crossed_book(self) -> None:
        payload = {"code": 0, "data": {"bids": [["102", "2"]], "asks": [["101", "3"]]}}
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}), _FakeResponse(payload)])
        with self.assertRaises(ExchangeAPIError):
            await client.get_order_book("BTC/USDT")

    async def test_funding_rate_info(self) -> None:
        payload = {"code": 0, "data": {"lastFundingRate": "0.0001", "nextFundingTime": 1_700_000_000_000}}
        client = _client([_FakeResponse(payload)])
        info = await client.get_funding_rate_info("BTC/USDT")
        self.assertAlmostEqual(info.funding_rate, 0.0001)
        self.assertIsNotNone(info.next_funding_time)

    async def test_open_interest(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": {"openInterest": "1234.5"}})])
        self.assertEqual(await client.get_open_interest("BTC/USDT"), 1234.5)

    async def test_commission_rates(self) -> None:
        payload = {"code": 0, "data": {"commission": {"takerCommissionRate": 0.0005, "makerCommissionRate": -0.0002}}}
        client = _client([_FakeResponse(payload)])
        rates = await client.get_trade_fee_rates("BTC/USDT")
        self.assertAlmostEqual(rates.taker_fee_pct, 0.05)
        self.assertAlmostEqual(rates.maker_fee_pct, -0.02)


class PositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_position_details_parsing(self) -> None:
        payload = {
            "code": 0,
            "data": [
                {
                    "symbol": "BTC-USDT",
                    "positionId": "p-1",
                    "positionSide": "LONG",
                    "positionAmt": "0.5",
                    "avgPrice": "64000",
                    "markPrice": "64100",
                    "liquidationPrice": "60000",
                    "unrealizedProfit": "50",
                }
            ],
        }
        client = _client([_FakeResponse(payload)])
        details = await client.get_symbol_position_details("BTC/USDT")
        self.assertEqual(details.exchange_size, 0.5)
        self.assertEqual(details.base_quantity, 0.5)
        self.assertEqual(details.avg_price, 64000.0)
        self.assertEqual(details.pos_side, "LONG")
        self.assertEqual(details.position_id, "p-1")

    async def test_no_position_returns_zero(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": []})])
        details = await client.get_symbol_position_details("BTC/USDT")
        self.assertEqual(details.exchange_size, 0.0)
        self.assertIsNone(details.avg_price)


class OrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_order_hedge_mode_entry(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                _FakeResponse({"code": 0, "data": {"order": {"orderId": 42, "clientOrderId": "x"}}}),
            ]
        )
        await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        sent = client._client.requests[-1]["params"]  # type: ignore[attr-defined]
        self.assertEqual(sent["side"], "BUY")
        self.assertEqual(sent["positionSide"], "LONG")
        self.assertEqual(sent["type"], "MARKET")
        self.assertNotIn("reduceOnly", sent)
        self.assertTrue(sent["clientOrderId"])

    async def test_hedge_close_uses_opposite_side_same_position_side(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                _FakeResponse({"code": 0, "data": {"order": {"orderId": 43}}}),
            ]
        )
        await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1, reduce_only=True)
        sent = client._client.requests[-1]["params"]  # type: ignore[attr-defined]
        self.assertEqual(sent["side"], "SELL")
        self.assertEqual(sent["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", sent)

    async def test_short_close_uses_buy_with_short_position_side(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                _FakeResponse({"code": 0, "data": {"order": {"orderId": 44}}}),
            ]
        )
        await client.place_market_order("BTC/USDT", PositionDirection.SHORT, 0.1, reduce_only=True)
        sent = client._client.requests[-1]["params"]  # type: ignore[attr-defined]
        self.assertEqual(sent["side"], "BUY")
        self.assertEqual(sent["positionSide"], "SHORT")

    async def test_post_only_limit_order(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                _FakeResponse({"code": 0, "data": {"order": {"orderId": 45}}}),
            ]
        )
        await client.place_limit_order("BTC/USDT", PositionDirection.LONG, 0.1, 64000.0, post_only=True)
        sent = client._client.requests[-1]["params"]  # type: ignore[attr-defined]
        self.assertEqual(sent["type"], "LIMIT")
        self.assertEqual(sent["timeInForce"], "PostOnly")
        self.assertEqual(sent["price"], "64000.0")

    async def test_client_order_ids_are_unique(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                _FakeResponse({"code": 0, "data": {"order": {"orderId": 1}}}),
                _FakeResponse({"code": 0, "data": {"order": {"orderId": 2}}}),
            ]
        )
        await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        ids = [item["params"]["clientOrderId"] for item in client._client.requests[1:]]  # type: ignore[attr-defined]
        self.assertEqual(len(set(ids)), 2)

    async def test_order_below_min_notional_is_rejected(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]})])
        with self.assertRaises(ExchangeAPIError):
            await client.place_limit_order("BTC/USDT", PositionDirection.LONG, 0.0001, 1.0)

    async def test_order_rejected_when_api_trading_disabled(self) -> None:
        row = dict(_CONTRACT_ROW, apiStateOpen="false")
        client = _client([_FakeResponse({"code": 0, "data": [row]})])
        with self.assertRaises(ExchangeAPIError):
            await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)

    async def test_leverage_above_venue_cap_is_rejected(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": [_CONTRACT_ROW]})])
        with self.assertRaises(ExchangeAPIError):
            await client.set_leverage("BTC/USDT", 200)

    async def test_timeout_on_post_looks_up_client_order_id_instead_of_resubmitting(self) -> None:
        found = {
            "code": 0,
            "data": {"order": {"orderId": "99", "clientOrderId": "seen", "status": "FILLED"}},
        }
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                httpx.ReadTimeout("timeout"),
                _FakeResponse(found),
            ]
        )
        ack = await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        methods = [item["method"] for item in client._client.requests]  # type: ignore[attr-defined]
        self.assertEqual(methods.count("POST"), 1)
        self.assertEqual(str(ack["data"]["order"]["orderId"]), "99")

    async def test_conclusively_absent_order_is_resubmitted_once(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                httpx.ReadTimeout("timeout"),
                # Explicit "order does not exist" proves the first POST never landed.
                _FakeResponse({"code": 109421, "msg": "The specified order does not exist"}),
                _FakeResponse({"code": 0, "data": {"order": {"orderId": "7"}}}),
            ]
        )
        ack = await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        methods = [item["method"] for item in client._client.requests]  # type: ignore[attr-defined]
        self.assertEqual(methods.count("POST"), 2)
        self.assertEqual(str(ack["data"]["order"]["orderId"]), "7")

    async def test_empty_lookup_result_is_inconclusive_not_absent(self) -> None:
        # A success envelope with no order payload is ambiguous; resubmitting on
        # that basis could duplicate a position.
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                httpx.ReadTimeout("timeout"),
                _FakeResponse({"code": 0, "data": {}}),
            ]
        )
        with self.assertRaises(ExchangeAPIError):
            await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        methods = [item["method"] for item in client._client.requests]  # type: ignore[attr-defined]
        self.assertEqual(methods.count("POST"), 1)

    async def test_parameter_error_is_not_treated_as_order_not_found(self) -> None:
        # 101400 is "order parameter validation failed", not proof of absence.
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                httpx.ReadTimeout("timeout"),
                _FakeResponse({"code": 101400, "msg": "order parameter validation failed"}),
            ]
        )
        with self.assertRaises(ExchangeAPIError):
            await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        methods = [item["method"] for item in client._client.requests]  # type: ignore[attr-defined]
        self.assertEqual(methods.count("POST"), 1)

    async def test_inconclusive_lookup_fails_closed_without_resubmitting(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                httpx.ReadTimeout("timeout"),
                # Lookup itself fails: the order state is unknown, so resubmitting
                # could create a duplicate position.
                httpx.ReadTimeout("lookup timeout"),
            ]
        )
        with self.assertRaises(ExchangeAPIError) as ctx:
            await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        self.assertIn("unknown state", str(ctx.exception))
        methods = [item["method"] for item in client._client.requests]  # type: ignore[attr-defined]
        self.assertEqual(methods.count("POST"), 1)

    async def test_lookup_error_that_is_not_order_not_found_fails_closed(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                httpx.ReadTimeout("timeout"),
                _FakeResponse({"code": 100500, "msg": "internal error"}),
            ]
        )
        with self.assertRaises(ExchangeAPIError):
            await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        methods = [item["method"] for item in client._client.requests]  # type: ignore[attr-defined]
        self.assertEqual(methods.count("POST"), 1)

    async def test_order_post_is_never_retried_by_the_transport(self) -> None:
        # A 5xx can still mean the order reached the exchange, so the POST itself
        # must be single-attempt; recovery goes through the clientOrderId lookup.
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                _FakeResponse({"code": 0, "msg": "bad gateway"}, status_code=502),
                _FakeResponse({"code": 109421, "msg": "The specified order does not exist"}),
                _FakeResponse({"code": 0, "data": {"order": {"orderId": "5"}}}),
            ]
        )
        ack = await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        methods = [item["method"] for item in client._client.requests]  # type: ignore[attr-defined]
        self.assertEqual(methods.count("POST"), 2, "one initial attempt plus one post-lookup resubmit")
        self.assertEqual(str(ack["data"]["order"]["orderId"]), "5")

    async def test_definitive_business_rejection_is_not_looked_up(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                _FakeResponse({"code": 100400, "msg": "insufficient margin"}),
            ]
        )
        with self.assertRaises(ExchangeAPIError):
            await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        self.assertEqual(len(client._client.requests), 2)  # type: ignore[attr-defined]

    async def test_order_post_does_not_fail_over_to_the_pro_domain(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                httpx.ConnectError("boom"),
                httpx.ConnectError("lookup boom"),
            ]
        )
        with self.assertRaises(ExchangeAPIError):
            await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        hosts = {item["url"].split("/openApi")[0] for item in client._client.requests}  # type: ignore[attr-defined]
        self.assertEqual(hosts, {BINGX_VST_BASE_URL})

    async def test_cancel_order(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": {}})])
        await client.cancel_order("BTC/USDT", "123")
        sent = client._client.requests[-1]  # type: ignore[attr-defined]
        self.assertEqual(sent["method"], "DELETE")
        self.assertEqual(sent["params"]["orderId"], "123")


class WireContractTests(unittest.IsolatedAsyncioTestCase):
    """Assert the exact parameter names BingX documents, not our internal casing.

    Reference: https://github.com/BingX-API/api-ai-skills/blob/main/skills/swap-trade/api-reference.md
    The documented field is `clientOrderId` (camelCase); an internally-consistent
    but wrong casing would be silently ignored by the venue.
    """

    async def test_create_order_sends_documented_client_order_id_field(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "data": [_CONTRACT_ROW]}),
                _FakeResponse({"code": 0, "data": {"order": {"orderId": 1}}}),
            ]
        )
        await client.place_market_order("BTC/USDT", PositionDirection.LONG, 0.1)
        params = client._client.requests[-1]["params"]  # type: ignore[attr-defined]
        self.assertIn("clientOrderId", params)
        self.assertNotIn("clientOrderID", params)

    async def test_order_query_sends_documented_client_order_id_field(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": {}})])
        await client.get_order_fill_by_client_id("BTC/USDT", "abc")
        params = client._client.requests[-1]["params"]  # type: ignore[attr-defined]
        self.assertIn("clientOrderId", params)
        self.assertNotIn("clientOrderID", params)

    async def test_response_client_order_id_is_parsed(self) -> None:
        payload = {
            "code": 0,
            "data": {"order": {"orderId": "1", "clientOrderId": "cid-1", "executedQty": "1", "status": "FILLED"}},
        }
        client = _client([_FakeResponse(payload)])
        fill = await client.get_order_fill("BTC/USDT", "1")
        self.assertEqual(fill.client_order_id, "cid-1")

    async def test_recv_window_never_exceeds_the_documented_maximum(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": {"ok": 1}})])
        await client._signed_request("GET", "/openApi/swap/v2/user/positions", {})
        params = client._client.requests[0]["params"]  # type: ignore[attr-defined]
        self.assertLessEqual(int(params["recvWindow"]), 5000)


class FillTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_fill_parsing_with_partial_fill(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "order": {
                    "orderId": "1",
                    "clientOrderId": "c1",
                    "executedQty": "0.05",
                    "avgPrice": "64000",
                    "fee": "-0.5",
                    "realizedPnl": "1.25",
                    "status": "PARTIALLY_FILLED",
                }
            },
        }
        client = _client([_FakeResponse(payload)])
        fill = await client.get_order_fill("BTC/USDT", "1")
        self.assertIsNotNone(fill)
        self.assertEqual(fill.filled_size, 0.05)
        self.assertEqual(fill.base_quantity, 0.05)
        self.assertEqual(fill.avg_price, 64000.0)
        self.assertAlmostEqual(fill.fee_usdt, 0.5)
        self.assertAlmostEqual(fill.pnl_usdt, 1.25)
        self.assertEqual(fill.state, "partially_filled")

    async def test_missing_order_returns_none(self) -> None:
        client = _client([_FakeResponse({"code": 0, "data": {}})])
        self.assertIsNone(await client.get_order_fill("BTC/USDT", "nope"))

    async def test_fill_history_request_matches_the_documented_contract(self) -> None:
        order_payload = {
            "code": 0,
            "data": {"order": {"orderId": "1", "executedQty": "0.05", "avgPrice": "64000", "status": "FILLED"}},
        }
        client = _client([_FakeResponse(order_payload), _FakeResponse({"code": 0, "data": {"fill_orders": []}})])
        await client.get_order_fill("BTC/USDT", "1")
        request = client._client.requests[-1]  # type: ignore[attr-defined]
        self.assertIn("/openApi/swap/v2/trade/allFillOrders", request["url"])
        params = request["params"]
        # startTs/endTs/tradingUnit are required by the documented endpoint.
        self.assertIn("tradingUnit", params)
        self.assertIn("startTs", params)
        self.assertIn("endTs", params)
        self.assertLess(int(params["startTs"]), int(params["endTs"]))

    async def test_fill_history_failure_is_logged_not_silently_swallowed(self) -> None:
        class _Logger:
            def __init__(self) -> None:
                self.warnings: list[str] = []

            def warning(self, message: str, *args) -> None:
                self.warnings.append(message % args if args else message)

        logger = _Logger()
        order_payload = {
            "code": 0,
            "data": {"order": {"orderId": "1", "executedQty": "0.05", "avgPrice": "64000", "status": "FILLED"}},
        }
        client = _client(
            [_FakeResponse(order_payload), _FakeResponse({"code": 100400, "msg": "bad range"})],
            logger=logger,
        )
        fill = await client.get_order_fill("BTC/USDT", "1")
        self.assertEqual(fill.fee_usdt, 0.0)
        self.assertTrue(any("fill history unavailable" in item.lower() for item in logger.warnings))

    async def test_fee_and_realized_pnl_use_the_documented_fill_fields(self) -> None:
        # allFillOrders documents `fee` (negative = paid) and `realizedPnl`.
        order_payload = {
            "code": 0,
            "data": {"order": {"orderId": "1", "executedQty": "0.05", "avgPrice": "64000", "status": "FILLED"}},
        }
        fills_payload = {
            "code": 0,
            "data": {
                "fill_orders": [
                    {
                        "tradeId": "t1",
                        "symbol": "BTC-USDT",
                        "orderId": "1",
                        "side": "BUY",
                        "price": "64000",
                        "qty": "0.05",
                        "fee": "-0.5",
                        "realizedPnl": "1.25",
                        "time": 1_700_000_000_000,
                    }
                ]
            },
        }
        client = _client([_FakeResponse(order_payload), _FakeResponse(fills_payload)])
        fill = await client.get_order_fill("BTC/USDT", "1")
        self.assertAlmostEqual(fill.fee_usdt, 0.5)
        self.assertAlmostEqual(fill.pnl_usdt, 1.25)

    async def test_fee_and_pnl_aggregate_across_partial_fills(self) -> None:
        order_payload = {
            "code": 0,
            "data": {"order": {"orderId": "1", "executedQty": "0.05", "avgPrice": "64000", "status": "FILLED"}},
        }
        fills_payload = {
            "code": 0,
            "data": {
                "fill_orders": [
                    {"orderId": "1", "qty": "0.03", "price": "64000", "fee": "-0.3", "realizedPnl": "1"},
                    {"orderId": "1", "qty": "0.02", "price": "64010", "fee": "-0.2", "realizedPnl": "0.5"},
                    {"orderId": "2", "qty": "9", "price": "1", "fee": "-9", "realizedPnl": "9"},
                ]
            },
        }
        client = _client([_FakeResponse(order_payload), _FakeResponse(fills_payload)])
        fill = await client.get_order_fill("BTC/USDT", "1")
        self.assertAlmostEqual(fill.fee_usdt, 0.5)
        self.assertAlmostEqual(fill.pnl_usdt, 1.5)


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_throttle_delays_second_call(self) -> None:
        client = _client(
            [_FakeResponse({"code": 0, "data": {"price": "1"}}), _FakeResponse({"code": 0, "data": {"price": "2"}})],
            route_min_interval_seconds=0.05,
        )
        sleeps: list[float] = []

        async def _sleep(seconds: float) -> None:
            sleeps.append(seconds)

        client._sleep = _sleep  # type: ignore[assignment]
        await client.get_last_price("BTC/USDT")
        await client.get_last_price("BTC/USDT")
        self.assertTrue(any(value > 0 for value in sleeps))

    async def test_http_429_is_retryable(self) -> None:
        client = _client(
            [
                _FakeResponse({"code": 0, "msg": "rate"}, status_code=429),
                _FakeResponse({"code": 0, "data": {"price": "3"}}),
            ]
        )
        client._sleep = lambda seconds: _noop()  # type: ignore[assignment]
        self.assertEqual(await client.get_last_price("BTC/USDT"), 3.0)


async def _noop() -> None:
    return None


class CachingTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_lived_cache_reuses_market_response(self) -> None:
        client = _client(
            [_FakeResponse({"code": 0, "data": {"price": "1"}})],
            market_cache_seconds=60.0,
        )
        first = await client.get_last_price("BTC/USDT")
        second = await client.get_last_price("BTC/USDT")
        self.assertEqual(first, second)
        self.assertEqual(len(client._client.requests), 1)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
