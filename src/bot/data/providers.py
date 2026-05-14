from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from bot.config import Settings
from bot.data.market_signals import ExternalMarketSignals


BUILTIN_DUNE_SIGNAL_SQL: dict[str, str] = {
    "eth_dex_volume_regime": """
        SELECT
            'eth_dex_volume_regime' AS signal_name,
            'ETH/USDT' AS symbol,
            '1h' AS timeframe,
            SUM(amount_usd) AS value,
            COUNT(*) AS trade_count,
            CASE
                WHEN SUM(amount_usd) >= 100000000 THEN 'bullish_liquidity'
                WHEN SUM(amount_usd) <= 20000000 THEN 'thin_liquidity'
                ELSE 'neutral'
            END AS bias,
            CASE
                WHEN SUM(amount_usd) >= 100000000 THEN 0.70
                WHEN SUM(amount_usd) <= 20000000 THEN 0.65
                ELSE 0.50
            END AS confidence,
            CURRENT_TIMESTAMP AS updated_at
        FROM dex.trades
        WHERE block_time >= NOW() - INTERVAL '1' hour
          AND amount_usd IS NOT NULL
          AND (
              token_bought_symbol IN ('ETH', 'WETH')
              OR token_sold_symbol IN ('ETH', 'WETH')
          )
    """,
    "stablecoin_liquidity_flow": """
        SELECT
            'stablecoin_liquidity_flow' AS signal_name,
            'MARKET' AS symbol,
            '1h' AS timeframe,
            SUM(amount_usd) AS value,
            COUNT(*) AS transfer_count,
            CASE
                WHEN SUM(amount_usd) >= 500000000 THEN 'risk_on_liquidity'
                WHEN SUM(amount_usd) <= 50000000 THEN 'thin_liquidity'
                ELSE 'neutral'
            END AS bias,
            CASE
                WHEN SUM(amount_usd) >= 500000000 THEN 0.68
                WHEN SUM(amount_usd) <= 50000000 THEN 0.62
                ELSE 0.50
            END AS confidence,
            CURRENT_TIMESTAMP AS updated_at
        FROM tokens.transfers
        WHERE block_time >= NOW() - INTERVAL '1' hour
          AND amount_usd IS NOT NULL
          AND symbol IN ('USDT', 'USDC')
    """,
    "large_eth_transfer_activity": """
        SELECT
            'large_eth_transfer_activity' AS signal_name,
            'ETH/USDT' AS symbol,
            '1h' AS timeframe,
            COUNT(*) AS value,
            SUM(amount_usd) AS total_usd,
            CASE
                WHEN COUNT(*) >= 50 THEN 'high_whale_activity'
                WHEN COUNT(*) = 0 THEN 'quiet'
                ELSE 'neutral'
            END AS bias,
            CASE
                WHEN COUNT(*) >= 50 THEN 0.66
                WHEN COUNT(*) = 0 THEN 0.45
                ELSE 0.52
            END AS confidence,
            CURRENT_TIMESTAMP AS updated_at
        FROM tokens.transfers
        WHERE block_time >= NOW() - INTERVAL '1' hour
          AND amount_usd >= 1000000
          AND symbol IN ('ETH', 'WETH')
    """,
}


class ExternalSignalProvider:
    async def fetch(self) -> dict[str, Any]:
        return {}

    async def close(self) -> None:
        return None


class CoinMarketCapProvider(ExternalSignalProvider):
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._api_key = settings.coinmarketcap_api_key
        self._base_url = settings.coinmarketcap_base_url.rstrip("/")
        self._symbols = self._base_symbols(settings.symbols)
        self._logger = logger
        self._client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)

    async def fetch(self) -> dict[str, Any]:
        if not self._api_key or not self._symbols:
            return {}
        try:
            response = await self._client.get(
                f"{self._base_url}/v2/cryptocurrency/quotes/latest",
                params={"symbol": ",".join(self._symbols), "convert": "USD"},
                headers={"X-CMC_PRO_API_KEY": self._api_key},
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else {}
            return self._summarize_quotes(data if isinstance(data, dict) else {})
        except Exception as exc:
            self._logger.warning("CoinMarketCap signal fetch failed: %s", exc)
            return {}

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _base_symbols(symbols: tuple[str, ...]) -> list[str]:
        bases: list[str] = []
        for symbol in symbols:
            base = symbol.split("/")[0].split("-")[0].upper()
            if base and base not in bases:
                bases.append(base)
        return bases

    @staticmethod
    def _summarize_quotes(data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for symbol, value in data.items():
            row = value[0] if isinstance(value, list) and value else value
            if not isinstance(row, dict):
                continue
            quote = row.get("quote", {}).get("USD", {})
            result[str(symbol).upper()] = {
                "price": quote.get("price"),
                "volume_24h": quote.get("volume_24h"),
                "volume_change_24h": quote.get("volume_change_24h"),
                "percent_change_1h": quote.get("percent_change_1h"),
                "percent_change_24h": quote.get("percent_change_24h"),
                "percent_change_7d": quote.get("percent_change_7d"),
                "market_cap": quote.get("market_cap"),
                "market_cap_dominance": quote.get("market_cap_dominance"),
                "cmc_rank": row.get("cmc_rank"),
                "last_updated": quote.get("last_updated") or row.get("last_updated"),
            }
        return result


class DuneProvider(ExternalSignalProvider):
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._api_key = settings.dune_api_key
        self._query_ids = settings.dune_query_ids
        self._limit = settings.dune_result_limit
        self._use_builtin_signals = settings.dune_use_builtin_signals
        self._performance_tier = settings.dune_performance_tier
        self._poll_seconds = settings.dune_execution_poll_seconds
        self._execution_timeout_seconds = settings.dune_execution_timeout_seconds
        self._base_url = settings.dune_base_url.rstrip("/")
        self._logger = logger
        self._client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)

    async def fetch(self) -> dict[str, Any]:
        if not self._api_key:
            return {}
        output: dict[str, Any] = {"saved_queries": {}, "builtin_signals": {}}
        for query_id in self._query_ids:
            try:
                response = await self._client.get(
                    f"{self._base_url}/query/{query_id}/results",
                    params={"limit": str(self._limit)},
                    headers={"X-Dune-Api-Key": self._api_key},
                )
                response.raise_for_status()
                payload = response.json()
                result = payload.get("result") if isinstance(payload, dict) else {}
                output["saved_queries"][str(query_id)] = {
                    "state": payload.get("state") if isinstance(payload, dict) else None,
                    "submitted_at": payload.get("submitted_at") if isinstance(payload, dict) else None,
                    "rows": result.get("rows", []) if isinstance(result, dict) else [],
                    "metadata": result.get("metadata", {}) if isinstance(result, dict) else {},
                }
            except Exception as exc:
                self._logger.warning("Dune signal fetch failed for query %s: %s", query_id, exc)
        if self._use_builtin_signals:
            output["builtin_signals"] = await self._fetch_builtin_signals()
        return output

    async def close(self) -> None:
        await self._client.aclose()

    async def _fetch_builtin_signals(self) -> dict[str, Any]:
        tasks = {
            name: asyncio.create_task(self._run_sql(name, sql))
            for name, sql in BUILTIN_DUNE_SIGNAL_SQL.items()
        }
        output: dict[str, Any] = {}
        for name, task in tasks.items():
            try:
                output[name] = await task
            except Exception as exc:
                self._logger.warning("Dune built-in signal failed for %s: %s", name, exc)
        return output

    async def _run_sql(self, name: str, sql: str) -> dict[str, Any]:
        response = await self._client.post(
            f"{self._base_url}/sql/execute",
            json={"sql": sql, "performance": self._performance_tier},
            headers={"X-Dune-Api-Key": str(self._api_key), "Content-Type": "application/json"},
        )
        if response.status_code == 400 and "performance tier" in response.text.lower():
            response = await self._client.post(
                f"{self._base_url}/sql/execute",
                json={"sql": sql, "performance": "medium"},
                headers={"X-Dune-Api-Key": str(self._api_key), "Content-Type": "application/json"},
            )
        response.raise_for_status()
        execution = response.json()
        execution_id = execution.get("execution_id")
        if not execution_id:
            raise RuntimeError(f"Dune SQL execution did not return execution_id: {execution}")

        deadline = time.monotonic() + self._execution_timeout_seconds
        status_payload: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status_response = await self._client.get(
                f"{self._base_url}/execution/{execution_id}/status",
                headers={"X-Dune-Api-Key": str(self._api_key)},
            )
            status_response.raise_for_status()
            status_payload = status_response.json()
            if status_payload.get("is_execution_finished"):
                break
            await asyncio.sleep(self._poll_seconds)

        state = str(status_payload.get("state") or execution.get("state") or "")
        if state not in {"QUERY_STATE_COMPLETED", "QUERY_STATE_COMPLETED_PARTIAL"}:
            return {
                "state": state or "QUERY_STATE_UNKNOWN",
                "execution_id": execution_id,
                "rows": [],
                "error": status_payload.get("error"),
            }

        result_response = await self._client.get(
            f"{self._base_url}/execution/{execution_id}/results",
            params={"limit": str(self._limit)},
            headers={"X-Dune-Api-Key": str(self._api_key)},
        )
        result_response.raise_for_status()
        payload = result_response.json()
        result = payload.get("result") if isinstance(payload, dict) else {}
        return {
            "state": payload.get("state"),
            "execution_id": execution_id,
            "rows": result.get("rows", []) if isinstance(result, dict) else [],
            "metadata": result.get("metadata", {}) if isinstance(result, dict) else {},
        }


class TradingViewSignalProvider(ExternalSignalProvider):
    """
    Pulls TradingView-derived signals from a user-owned endpoint.

    TradingView does not provide a general public market-data API for bots. This
    adapter expects either a TradingView alert webhook relay or another endpoint
    you control that exposes TradingView signals as JSON.
    """

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._url = settings.tradingview_signal_url
        self._token = settings.tradingview_signal_token
        self._logger = logger
        self._client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)

    async def fetch(self) -> dict[str, Any]:
        if not self._url:
            return {}
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            response = await self._client.get(self._url, headers=headers)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {"signals": payload}
        except Exception as exc:
            self._logger.warning("TradingView signal fetch failed: %s", exc)
            return {}

    async def close(self) -> None:
        await self._client.aclose()


class ExternalSignalAggregator:
    def __init__(self, providers: list[ExternalSignalProvider], cache_seconds: int) -> None:
        self._providers = providers
        self._cache_seconds = cache_seconds
        self._cached: ExternalMarketSignals | None = None
        self._cached_at = 0.0

    @classmethod
    def from_settings(cls, settings: Settings, logger: logging.Logger) -> "ExternalSignalAggregator":
        return cls(
            providers=[
                CoinMarketCapProvider(settings, logger),
                DuneProvider(settings, logger),
                TradingViewSignalProvider(settings, logger),
            ],
            cache_seconds=settings.external_signal_cache_seconds,
        )

    async def fetch(self) -> ExternalMarketSignals:
        now = time.monotonic()
        if self._cached is not None and now - self._cached_at < self._cache_seconds:
            return self._cached
        cmc, dune, tradingview = await asyncio.gather(
            self._providers[0].fetch(),
            self._providers[1].fetch(),
            self._providers[2].fetch(),
        )
        self._cached = ExternalMarketSignals(
            coinmarketcap=cmc,
            dune=dune,
            tradingview=tradingview,
        )
        self._cached_at = now
        return self._cached

    async def close(self) -> None:
        await asyncio.gather(*(provider.close() for provider in self._providers), return_exceptions=True)
