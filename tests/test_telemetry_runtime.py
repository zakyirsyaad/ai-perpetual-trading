from __future__ import annotations

import asyncio
import json
import logging
import socket
import unittest
from pathlib import Path
from unittest import mock

from bot.config import Settings
from bot.telemetry.book_ticker import BookTick
from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.observer import NULL_OBSERVER, EngineTelemetry
from bot.telemetry.runtime import TelemetryRuntime, build_meta, build_telemetry
from bot.telemetry.server import TelemetryServer
from bot.telemetry.wire import Meta, PriceMessage, to_wire

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "jev_wire_samples.json").read_text())
META = Meta.model_validate(SAMPLES["meta"])
LOGGER = logging.getLogger("volume_bot.test_runtime")
NOW_MS = 1_791_661_170_000


def settings(**env: object) -> Settings:
    return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", STRATEGY_MODE="volume", **env)


class IdlePoller:
    async def run(self) -> None:
        await asyncio.Event().wait()


class CrashingPoller:
    async def run(self) -> None:
        raise RuntimeError("poller exploded")


class IdleFeed:
    def __init__(self) -> None:
        self.started = False
        self.cancelled = False

    async def run(self) -> None:
        self.started = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class CrashingFeed:
    async def run(self) -> None:
        raise RuntimeError("price stream exploded")


class UnusedClient:
    """Building the runtime must not touch the exchange."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"client.{name} used while building telemetry")


async def get_status(port: int, path: str = "/") -> int:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(-1), 3)
    writer.close()
    return int(data.split(b" ", 2)[1])


class BuildMetaTests(unittest.TestCase):
    def test_sleeves_follow_the_traded_symbols(self) -> None:
        meta = build_meta(settings(SYMBOLS="BTC/USDT,ETH/USDT", ENTRY_INTERVAL_MINUTES=3), now_ms=NOW_MS)

        self.assertEqual([(s.coin, s.pair, s.label) for s in meta.sleeves], [("BTC", "BTC-USDT", "BTC"), ("ETH", "ETH-USDT", "ETH")])
        self.assertEqual((meta.coin, meta.pair, meta.market), ("BTC", "BTC-USDT", "BTC-USDT"))
        self.assertEqual((meta.venue, meta.dry_run, meta.tick_ms, meta.started_at), ("bingx", False, 180_000, NOW_MS))
        self.assertEqual(meta.explorer_tx, "")

    def test_no_wallet_is_ever_exposed(self) -> None:
        meta = build_meta(settings(SYMBOLS="BTC/USDT"), now_ms=NOW_MS)

        self.assertIsNone(meta.wallet)
        self.assertTrue(all(s.wallet is None for s in meta.sleeves))

    def test_credentials_never_appear_in_the_meta(self) -> None:
        dumped = json.dumps(to_wire(build_meta(settings(), now_ms=NOW_MS)))

        self.assertNotIn("test-key", dumped)
        self.assertNotIn("test-secret", dumped)

    def test_the_model_label_is_volume_for_the_deterministic_strategy(self) -> None:
        self.assertEqual(build_meta(settings(), now_ms=NOW_MS).model, "volume")

    def test_the_model_label_is_the_ai_model_for_ai_strategies(self) -> None:
        s = Settings(
            _env_file=None,
            BINGX_API_KEY="test-key",
            BINGX_SECRET_KEY="test-secret",
            STRATEGY_MODE="ai_volume",
            XAI_API_KEY="test-ai-key",
            XAI_MODEL="some-model",
        )

        self.assertEqual(build_meta(s, now_ms=NOW_MS).model, "some-model")


class BuildTelemetryTests(unittest.TestCase):
    def test_nothing_is_built_while_disabled(self) -> None:
        self.assertIsNone(build_telemetry(settings(), UnusedClient(), LOGGER))  # type: ignore[arg-type]

    def test_a_failure_while_building_is_logged_and_never_stops_the_bot(self) -> None:
        with mock.patch("bot.telemetry.runtime.build_meta", side_effect=RuntimeError("boom")):
            with self.assertLogs(LOGGER, level=logging.WARNING):
                runtime = build_telemetry(settings(TELEMETRY_ENABLED="true"), UnusedClient(), LOGGER)  # type: ignore[arg-type]

        self.assertIsNone(runtime)

    def test_enabling_builds_a_runtime_without_touching_the_exchange(self) -> None:
        runtime = build_telemetry(settings(TELEMETRY_ENABLED="true", SYMBOLS="BTC/USDT"), UnusedClient(), LOGGER)  # type: ignore[arg-type]

        self.assertIsInstance(runtime, TelemetryRuntime)
        assert runtime is not None
        self.assertIn("BTC", runtime.hub.history_body())
        self.assertFalse(runtime.running)

    def test_the_engine_gets_an_observer_that_feeds_the_same_hub(self) -> None:
        runtime = build_telemetry(settings(TELEMETRY_ENABLED="true", SYMBOLS="BTC/USDT"), UnusedClient(), LOGGER)  # type: ignore[arg-type]

        assert runtime is not None
        self.assertIsInstance(runtime.observer, EngineTelemetry)
        runtime.hub.record_price(
            PriceMessage(coin="BTC", ts=NOW_MS, mid=100.0, best_bid=99.5, best_ask=100.5, spread_bps=1.0)
        )
        runtime.observer.decided(None, latency_ms=1)
        self.assertEqual(len(runtime.hub.history_body()["BTC"]), 1)

    def test_no_price_stream_is_built_unless_it_was_asked_for(self) -> None:
        with mock.patch("bot.telemetry.runtime.BookTickerFeed") as feed_class:
            runtime = build_telemetry(settings(TELEMETRY_ENABLED="true", SYMBOLS="BTC/USDT"), UnusedClient(), LOGGER)  # type: ignore[arg-type]

        assert runtime is not None
        feed_class.assert_not_called()
        self.assertIsNone(runtime.stream)

    def test_the_price_stream_follows_the_traded_pairs_and_feeds_the_chart(self) -> None:
        with mock.patch("bot.telemetry.runtime.BookTickerFeed") as feed_class:
            runtime = build_telemetry(
                settings(TELEMETRY_ENABLED="true", TELEMETRY_PRICE_STREAM="true", SYMBOLS="BTC/USDT,ETH/USDT"),
                UnusedClient(),  # type: ignore[arg-type]
                LOGGER,
            )

        assert runtime is not None
        pairs, on_tick = feed_class.call_args.args
        self.assertEqual(list(pairs), ["BTC-USDT", "ETH-USDT"])
        self.assertIs(runtime.stream, feed_class.return_value)
        on_tick(BookTick("BTC-USDT", 99.0, 101.0))
        seconds = [p for p in runtime.hub.tape_body()["BTC"] if p.get("bar") == "1s"]
        self.assertEqual([p["close"] for p in seconds], [100.0])

    def test_a_runtime_built_by_hand_still_offers_a_harmless_observer(self) -> None:
        hub = TelemetryHub(META, TelemetryBus())
        server = TelemetryServer(hub, host="127.0.0.1", port=0)

        runtime = TelemetryRuntime(hub, server, IdlePoller(), LOGGER)  # type: ignore[arg-type]

        self.assertIs(runtime.observer, NULL_OBSERVER)


class RuntimeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def build(
        self, port: int = 0, poller: object | None = None, stream: object | None = None
    ) -> tuple[TelemetryRuntime, TelemetryServer]:
        hub = TelemetryHub(META, TelemetryBus())
        server = TelemetryServer(hub, host="127.0.0.1", port=port)
        return TelemetryRuntime(hub, server, poller or IdlePoller(), LOGGER, stream=stream), server  # type: ignore[arg-type]

    async def test_the_price_stream_runs_with_the_feed_and_is_cancelled_on_stop(self) -> None:
        feed = IdleFeed()
        runtime, _ = self.build(stream=feed)

        await runtime.start()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if feed.started:
                break
        await runtime.stop()

        self.assertTrue(feed.started)
        self.assertTrue(feed.cancelled)

    async def test_a_crashing_price_stream_is_logged_and_the_server_and_poller_carry_on(self) -> None:
        runtime, server = self.build(stream=CrashingFeed())

        with self.assertLogs(LOGGER, level=logging.ERROR):
            await runtime.start()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if runtime.stream_stopped:
                    break
        status = await get_status(server.bound_port)
        await runtime.stop()

        self.assertEqual(status, 200)
        self.assertTrue(runtime.stream_stopped)
        self.assertFalse(runtime.poller_stopped)

    async def test_start_serves_the_feed_and_stop_releases_the_port(self) -> None:
        runtime, server = self.build()

        started = await runtime.start()
        port = server.bound_port
        status = await get_status(port)
        await runtime.stop()

        self.assertTrue(started)
        self.assertEqual(status, 200)
        self.assertFalse(runtime.running)
        with self.assertRaises(OSError):
            await asyncio.open_connection("127.0.0.1", port)

    async def test_stopping_twice_is_harmless(self) -> None:
        runtime, _ = self.build()
        await runtime.start()

        await runtime.stop()
        await runtime.stop()

    async def test_a_failing_shutdown_is_logged_and_never_masks_the_real_error(self) -> None:
        runtime, server = self.build()
        await runtime.start()
        real_stop = server.stop

        async def broken_stop() -> None:
            await real_stop()
            raise RuntimeError("shutdown exploded")

        server.stop = broken_stop  # type: ignore[method-assign]

        with self.assertLogs(LOGGER, level=logging.WARNING):
            await runtime.stop()

    async def test_a_taken_port_is_reported_but_never_raised(self) -> None:
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        try:
            runtime, _ = self.build(port=taken.getsockname()[1])

            with self.assertLogs(LOGGER, level=logging.WARNING):
                started = await runtime.start()
        finally:
            taken.close()

        self.assertFalse(started)
        self.assertFalse(runtime.running)

    async def test_a_crashing_poller_is_logged_and_the_server_keeps_serving(self) -> None:
        runtime, server = self.build(poller=CrashingPoller())

        with self.assertLogs(LOGGER, level=logging.ERROR):
            await runtime.start()
            for _ in range(50):
                await asyncio.sleep(0.02)
                if runtime.poller_stopped:
                    break
        status = await get_status(server.bound_port)
        await runtime.stop()

        self.assertEqual(status, 200)
        self.assertTrue(runtime.poller_stopped)


if __name__ == "__main__":
    unittest.main()
