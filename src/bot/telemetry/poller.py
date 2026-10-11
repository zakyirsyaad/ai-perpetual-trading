"""Background market-data feed for the dashboard chart.

While the bot is flat it reads no prices at all, so the chart would sit still. This
poller keeps it alive with two read-only public calls: the top of the order book for
live prices, and closed candles for history. It never touches an order, account or
position endpoint (a test enforces that), runs in its own task, and every failure is
contained and logged without ever reaching the trading loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence

from bot.exchange.contracts import FuturesExchangeProtocol
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.wire import PriceMessage

LOGGER = logging.getLogger("volume_bot.telemetry")

MINUTE_CANDLES = 1000  # about 16.7 hours of 1 minute history
QUARTER_CANDLES = 672  # 7 days of 15 minute history
FAILURE_LOG_EVERY = 12  # log the first failure of a streak, then one in twelve


def coin_of(symbol: str) -> str:
    """Dashboard coin for an internal symbol: BTC/USDT -> BTC."""
    return symbol.split("/", 1)[0]


def pair_of(symbol: str) -> str:
    """Venue-style pair for display: BTC/USDT -> BTC-USDT."""
    return symbol.replace("/", "-")


class MarketDataPoller:
    def __init__(
        self,
        hub: TelemetryHub,
        client: FuturesExchangeProtocol,
        symbols: Sequence[str],
        *,
        interval_seconds: float = 5.0,
        kline_refresh_seconds: float = 60.0,
        call_timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hub = hub
        self._client = client
        self._symbols = tuple(symbols)
        self._interval = interval_seconds
        self._refresh = kline_refresh_seconds
        self._call_timeout = call_timeout
        self._clock = clock
        self._monotonic = monotonic
        self._history_loaded_at: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    async def run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._interval)

    async def poll_once(self) -> None:
        """One cycle over every symbol. Never raises. Sequential, to keep the request rate low."""
        for symbol in self._symbols:
            await self._guarded(f"book:{symbol}", "price", symbol, lambda s=symbol: self._poll_price(s))
            await self._guarded(f"history:{symbol}", "history", symbol, lambda s=symbol: self._refresh_history(s))

    async def _guarded(self, key: str, label: str, symbol: str, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await asyncio.wait_for(action(), self._call_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            count = self._failures.get(key, 0) + 1
            self._failures[key] = count
            if count % FAILURE_LOG_EVERY == 1:
                LOGGER.warning(
                    "telemetry market data %s failed for %s (failure %s in a row)",
                    label,
                    symbol,
                    count,
                    exc_info=True,
                )
        else:
            self._failures.pop(key, None)

    async def _poll_price(self, symbol: str) -> None:
        book = await self._client.get_order_book(symbol, depth=5)
        self._hub.record_price(
            PriceMessage(
                coin=coin_of(symbol),
                ts=int(self._clock() * 1000),
                mid=book.mid_price,
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                spread_bps=book.spread_bps,
            )
        )

    async def _refresh_history(self, symbol: str) -> None:
        now = self._monotonic()
        loaded = self._history_loaded_at.get(symbol)
        if loaded is not None and now - loaded < self._refresh:
            return
        coin = coin_of(symbol)
        minutes = await self._client.get_klines(symbol, "1m", limit=MINUTE_CANDLES)
        quarters = await self._client.get_klines(symbol, "15m", limit=QUARTER_CANDLES)
        self._hub.load_klines(coin, "1m", minutes)
        self._hub.load_klines(coin, "15m", quarters)
        self._history_loaded_at[symbol] = now
