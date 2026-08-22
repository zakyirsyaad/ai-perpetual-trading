from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace

import httpx

from bot.data.market_signals import ExternalMarketSignals
from bot.data.providers import (
    BUILTIN_DUNE_SIGNAL_SQL,
    CoinMarketCapProvider,
    DuneProvider,
    ExternalSignalAggregator,
    ExternalSignalProvider,
    TradingViewSignalProvider,
)

LOGGER = logging.getLogger("test.providers")


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        coinmarketcap_api_key="cmc-key",
        coinmarketcap_base_url="https://pro-api.coinmarketcap.com",
        symbols=("BTC/USDT", "ETH/USDT"),
        request_timeout_seconds=5,
        dune_api_key="dune-key",
        dune_query_ids=("123",),
        dune_result_limit=5,
        dune_use_builtin_signals=False,
        dune_performance_tier="medium",
        dune_execution_poll_seconds=0.0,
        dune_execution_timeout_seconds=1,
        dune_base_url="https://api.dune.com/api/v1",
        tradingview_signal_url="http://relay/latest",
        tradingview_signal_token="token",
        external_signal_cache_seconds=300,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeHTTP:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.requests: list[dict] = []
        self.closed = False

    def _next(self, method: str, url: str):
        item = self._responses.pop(0) if self._responses else {}
        if isinstance(item, Exception):
            raise item
        status = 200
        if isinstance(item, tuple):
            status, item = item
        return httpx.Response(status, request=httpx.Request(method, url), json=item)

    async def get(self, url, params=None, headers=None):
        self.requests.append({"method": "GET", "url": url, "params": params})
        return self._next("GET", url)

    async def post(self, url, json=None, headers=None):
        self.requests.append({"method": "POST", "url": url, "json": json})
        return self._next("POST", url)

    async def aclose(self) -> None:
        self.closed = True


class CoinMarketCapTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, responses, settings=None) -> CoinMarketCapProvider:
        provider = CoinMarketCapProvider(settings or _settings(), LOGGER)
        provider._client = _FakeHTTP(responses)
        return provider

    async def test_base_symbols_are_deduplicated(self) -> None:
        self.assertEqual(
            CoinMarketCapProvider._base_symbols(("BTC/USDT", "BTC-USDT", "ETH/USDT")),
            ["BTC", "ETH"],
        )

    async def test_quotes_are_summarized(self) -> None:
        payload = {
            "data": {
                "BTC": [
                    {
                        "cmc_rank": 1,
                        "quote": {"USD": {"price": 60000.0, "volume_24h": 1.0, "percent_change_24h": 2.0}},
                    }
                ]
            }
        }
        provider = self._provider([payload])
        result = await provider.fetch()
        self.assertEqual(result["BTC"]["price"], 60000.0)
        self.assertEqual(result["BTC"]["cmc_rank"], 1)

    async def test_missing_api_key_returns_empty(self) -> None:
        provider = self._provider([], _settings(coinmarketcap_api_key=None))
        self.assertEqual(await provider.fetch(), {})

    async def test_http_failure_is_swallowed(self) -> None:
        provider = self._provider([(500, {"error": "boom"})])
        self.assertEqual(await provider.fetch(), {})

    async def test_close_releases_the_client(self) -> None:
        provider = self._provider([])
        await provider.close()
        self.assertTrue(provider._client.closed)


class DuneTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, responses, settings=None) -> DuneProvider:
        provider = DuneProvider(settings or _settings(), LOGGER)
        provider._client = _FakeHTTP(responses)
        return provider

    async def test_missing_api_key_returns_empty(self) -> None:
        provider = self._provider([], _settings(dune_api_key=None))
        self.assertEqual(await provider.fetch(), {})

    async def test_saved_query_rows_are_returned(self) -> None:
        payload = {"state": "QUERY_STATE_COMPLETED", "result": {"rows": [{"a": 1}], "metadata": {}}}
        result = await self._provider([payload]).fetch()
        self.assertEqual(result["saved_queries"]["123"]["rows"], [{"a": 1}])

    async def test_saved_query_failure_is_swallowed(self) -> None:
        result = await self._provider([(500, {})]).fetch()
        self.assertEqual(result["saved_queries"], {})

    async def test_builtin_signals_run_when_enabled(self) -> None:
        settings = _settings(dune_query_ids=(), dune_use_builtin_signals=True)
        responses: list = []
        for _ in BUILTIN_DUNE_SIGNAL_SQL:
            responses.extend(
                [
                    {"execution_id": "x", "state": "QUERY_STATE_PENDING"},
                    {"is_execution_finished": True, "state": "QUERY_STATE_COMPLETED"},
                    {"state": "QUERY_STATE_COMPLETED", "result": {"rows": [{"v": 1}], "metadata": {}}},
                ]
            )
        result = await self._provider(responses, settings).fetch()
        self.assertEqual(len(result["builtin_signals"]), len(BUILTIN_DUNE_SIGNAL_SQL))

    async def test_unfinished_execution_returns_no_rows(self) -> None:
        provider = self._provider(
            [
                {"execution_id": "x", "state": "QUERY_STATE_PENDING"},
                {"is_execution_finished": True, "state": "QUERY_STATE_FAILED", "error": "bad sql"},
            ]
        )
        result = await provider._run_sql("name", "select 1")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["error"], "bad sql")

    async def test_missing_execution_id_raises(self) -> None:
        provider = self._provider([{"state": "QUERY_STATE_PENDING"}])
        with self.assertRaises(RuntimeError):
            await provider._run_sql("name", "select 1")

    async def test_close_releases_the_client(self) -> None:
        provider = self._provider([])
        await provider.close()
        self.assertTrue(provider._client.closed)


class TradingViewTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self, responses, settings=None) -> TradingViewSignalProvider:
        provider = TradingViewSignalProvider(settings or _settings(), LOGGER)
        provider._client = _FakeHTTP(responses)
        return provider

    async def test_unconfigured_url_returns_empty(self) -> None:
        provider = self._provider([], _settings(tradingview_signal_url=None))
        self.assertEqual(await provider.fetch(), {})

    async def test_dict_payload_is_returned_as_is(self) -> None:
        self.assertEqual(await self._provider([{"summary": "BUY"}]).fetch(), {"summary": "BUY"})

    async def test_list_payload_is_wrapped(self) -> None:
        self.assertEqual(await self._provider([["a"]]).fetch(), {"signals": ["a"]})

    async def test_relay_failure_is_swallowed(self) -> None:
        self.assertEqual(await self._provider([(404, {})]).fetch(), {})

    async def test_close_releases_the_client(self) -> None:
        provider = self._provider([])
        await provider.close()
        self.assertTrue(provider._client.closed)


class _StubProvider(ExternalSignalProvider):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0
        self.closed = False

    async def fetch(self) -> dict:
        self.calls += 1
        return self.payload

    async def close(self) -> None:
        self.closed = True


class AggregatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_results_from_all_three_providers_are_merged(self) -> None:
        aggregator = ExternalSignalAggregator(
            [_StubProvider({"cmc": 1}), _StubProvider({"dune": 2}), _StubProvider({"tv": 3})],
            cache_seconds=300,
        )
        result = await aggregator.fetch()
        self.assertIsInstance(result, ExternalMarketSignals)
        self.assertEqual(result.coinmarketcap, {"cmc": 1})
        self.assertEqual(result.dune, {"dune": 2})
        self.assertEqual(result.tradingview, {"tv": 3})

    async def test_results_are_cached(self) -> None:
        providers = [_StubProvider({}) for _ in range(3)]
        aggregator = ExternalSignalAggregator(providers, cache_seconds=300)
        await aggregator.fetch()
        await aggregator.fetch()
        self.assertEqual([provider.calls for provider in providers], [1, 1, 1])

    async def test_zero_cache_always_refetches(self) -> None:
        providers = [_StubProvider({}) for _ in range(3)]
        aggregator = ExternalSignalAggregator(providers, cache_seconds=0)
        await aggregator.fetch()
        await aggregator.fetch()
        self.assertEqual([provider.calls for provider in providers], [2, 2, 2])

    async def test_close_closes_every_provider(self) -> None:
        providers = [_StubProvider({}) for _ in range(3)]
        await ExternalSignalAggregator(providers, cache_seconds=0).close()
        self.assertTrue(all(provider.closed for provider in providers))

    async def test_from_settings_builds_the_three_providers(self) -> None:
        aggregator = ExternalSignalAggregator.from_settings(_settings(), LOGGER)
        self.assertEqual(len(aggregator._providers), 3)
        await aggregator.close()

    async def test_base_provider_defaults_are_empty(self) -> None:
        provider = ExternalSignalProvider()
        self.assertEqual(await provider.fetch(), {})
        await provider.close()


if __name__ == "__main__":
    unittest.main()
