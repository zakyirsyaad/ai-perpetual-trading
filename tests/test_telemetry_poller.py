from __future__ import annotations

import asyncio
import json
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.poller import MarketDataPoller, coin_of, pair_of
from bot.telemetry.wire import Meta

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "jev_wire_samples.json").read_text())
META = Meta.model_validate(SAMPLES["meta"])  # sleeves BTC, ETH, SOL
NOW = 1_791_661_170.0
SYMBOLS = ("BTC/USDT", "ETH/USDT")


def kline(ts: int, close: float = 100.0) -> dict[str, float]:
    return {"timestamp": float(ts), "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0}


class FakeClient:
    """Implements only the two read calls the poller may use. Anything else fails the test."""

    def __init__(self) -> None:
        self.book_calls: list[str] = []
        self.kline_calls: list[tuple[str, str, int]] = []
        self.fail_books: set[str] = set()
        self.fail_klines = False
        self.hang = False

    async def get_order_book(self, symbol: str, depth: int = 20) -> SimpleNamespace:
        self.book_calls.append(symbol)
        if self.hang:
            await asyncio.sleep(10)
        if symbol in self.fail_books:
            raise RuntimeError(f"book down for {symbol}")
        return SimpleNamespace(best_bid=99.5, best_ask=100.5, mid_price=100.0, spread_bps=100.0)

    async def get_klines(self, symbol: str, interval: str = "1m", limit: int = 120) -> list[dict[str, float]]:
        self.kline_calls.append((symbol, interval, limit))
        if self.fail_klines:
            raise RuntimeError("klines down")
        step = 60_000 if interval == "1m" else 900_000
        return [kline(1_791_661_000_000 + i * step) for i in range(3)]

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the poller touched client.{name}, which it must never use")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make(client: FakeClient, **kwargs: object) -> tuple[MarketDataPoller, TelemetryHub, TelemetryBus, Clock]:
    bus = TelemetryBus()
    hub = TelemetryHub(META, bus)
    clock = Clock()
    poller = MarketDataPoller(hub, client, SYMBOLS, clock=lambda: NOW, monotonic=clock, **kwargs)  # type: ignore[arg-type]
    return poller, hub, bus, clock


class SymbolMappingTests(unittest.TestCase):
    def test_internal_symbols_map_to_dashboard_coin_and_venue_pair(self) -> None:
        self.assertEqual(coin_of("BTC/USDT"), "BTC")
        self.assertEqual(pair_of("BTC/USDT"), "BTC-USDT")


class PollOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_symbol_publishes_a_live_price_and_builds_a_second_candle(self) -> None:
        poller, hub, bus, _ = make(FakeClient())
        queue = bus.subscribe()

        await poller.poll_once()

        prices = [m for m in (queue.get_nowait(), queue.get_nowait()) if m.event == "price"]
        self.assertEqual(sorted(m.coin for m in prices), ["BTC", "ETH"])
        self.assertEqual((prices[0].mid, prices[0].best_bid, prices[0].best_ask), (100.0, 99.5, 100.5))
        self.assertEqual(prices[0].ts, int(NOW * 1000))
        seconds = [p for p in hub.tape_body()["BTC"] if p.get("bar") == "1s"]
        self.assertEqual([p["close"] for p in seconds], [100.0])

    async def test_the_first_cycle_loads_minute_and_15_minute_history(self) -> None:
        client = FakeClient()
        poller, hub, _, _ = make(client)

        await poller.poll_once()

        self.assertEqual(
            sorted(client.kline_calls),
            sorted([("BTC/USDT", "1m", 1000), ("BTC/USDT", "15m", 672), ("ETH/USDT", "1m", 1000), ("ETH/USDT", "15m", 672)]),
        )
        bars = {p.get("bar") for p in hub.tape_body()["BTC"]}
        self.assertTrue({"1m", "15m"} <= bars)

    async def test_history_is_refreshed_only_after_the_refresh_interval(self) -> None:
        client = FakeClient()
        poller, _, _, clock = make(client, kline_refresh_seconds=60.0)
        await poller.poll_once()
        first = len(client.kline_calls)

        clock.now = 30.0
        await poller.poll_once()
        self.assertEqual(len(client.kline_calls), first)

        clock.now = 61.0
        await poller.poll_once()
        self.assertEqual(len(client.kline_calls), first * 2)

    async def test_it_only_ever_calls_the_two_read_endpoints(self) -> None:
        client = FakeClient()
        poller, _, _, _ = make(client)

        await poller.poll_once()

        self.assertTrue(client.book_calls and client.kline_calls)  # any other call would have raised AssertionError


class FailureIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_symbol_failing_does_not_stop_the_others_or_raise(self) -> None:
        client = FakeClient()
        client.fail_books.add("BTC/USDT")
        poller, hub, _, _ = make(client)

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            await poller.poll_once()

        self.assertEqual([p["close"] for p in hub.tape_body()["ETH"] if p.get("bar") == "1s"], [100.0])
        self.assertEqual([p for p in hub.tape_body()["BTC"] if p.get("bar") == "1s"], [])

    async def test_failed_history_does_not_block_prices_and_is_retried_next_cycle(self) -> None:
        client = FakeClient()
        client.fail_klines = True
        poller, hub, _, _ = make(client)

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            await poller.poll_once()
        self.assertEqual(len([p for p in hub.tape_body()["BTC"] if p.get("bar") == "1s"]), 1)
        calls_after_failure = len(client.kline_calls)

        client.fail_klines = False
        await poller.poll_once()

        self.assertGreater(len(client.kline_calls), calls_after_failure)
        self.assertIn("1m", {p.get("bar") for p in hub.tape_body()["BTC"]})

    async def test_repeated_failures_are_logged_once_not_every_cycle(self) -> None:
        client = FakeClient()
        client.fail_books.update(SYMBOLS)
        poller, _, _, _ = make(client)

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING) as logs:
            for _ in range(3):
                await poller.poll_once()

        market_warnings = [r for r in logs.records if "market data" in r.getMessage()]
        self.assertEqual(len(market_warnings), len(SYMBOLS))  # first failure per symbol only

    async def test_a_hung_call_is_cut_off_by_the_timeout(self) -> None:
        client = FakeClient()
        client.hang = True
        poller, _, _, _ = make(client, call_timeout=0.05)

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            await asyncio.wait_for(poller.poll_once(), timeout=2.0)


class RunLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_keeps_polling_until_cancelled(self) -> None:
        client = FakeClient()
        poller, _, _, _ = make(client, interval_seconds=0.01)

        task = asyncio.create_task(poller.run())
        for _ in range(100):
            if len(client.book_calls) >= 2 * len(SYMBOLS):
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertGreaterEqual(len(client.book_calls), 2 * len(SYMBOLS))


if __name__ == "__main__":
    unittest.main()
