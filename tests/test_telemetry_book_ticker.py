from __future__ import annotations

import asyncio
import gzip
import json
import logging
import unittest
from collections.abc import Callable

from bot.telemetry.book_ticker import BookTick, BookTickerFeed, decode_payload, parse_book_ticker
from bot.telemetry.ws import OP_BINARY, OP_TEXT, WebSocketConnection
from tests.test_telemetry_ws import FakeServer, read_client_frame, server_frame

LOGGER = logging.getLogger("volume_bot.test_book_ticker")
# Reconnect tests fail on purpose and the feed logs each failure; keep that out of the test output.
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


def ticker_json(bid: str = "83045.7", ask: str = "83048.7", pair: str = "BTC-USDT") -> str:
    return json.dumps(
        {
            "code": 0,
            "dataType": f"{pair}@bookTicker",
            "data": {"e": "bookTicker", "u": 1, "E": 1791728117031, "T": 1791728117017, "s": pair, "b": bid, "B": "0.0029", "a": ask, "A": "0.0929"},
        }
    )


def gz_frame(text: str) -> bytes:
    """BingX sends every market frame gzip-compressed, as a binary message."""
    return server_frame(OP_BINARY, gzip.compress(text.encode()))


class ParseTests(unittest.TestCase):
    def test_a_book_ticker_message_becomes_a_tick(self) -> None:
        self.assertEqual(parse_book_ticker(ticker_json()), BookTick("BTC-USDT", 83045.7, 83048.7))

    def test_the_subscription_ack_is_not_a_tick(self) -> None:
        ack = json.dumps({"id": "x", "code": 0, "msg": "", "dataType": "", "data": None})

        self.assertIsNone(parse_book_ticker(ack))

    def test_other_channels_are_not_ticks(self) -> None:
        other = json.dumps({"code": 0, "dataType": "BTC-USDT@trade", "data": {"e": "trade", "s": "BTC-USDT", "p": "1", "q": "1"}})

        self.assertIsNone(parse_book_ticker(other))

    def test_bad_input_is_ignored_not_raised(self) -> None:
        for text in ("", "Ping", "not json", "[]", "null", '{"dataType":"BTC-USDT@bookTicker","data":{"s":"BTC-USDT"}}'):
            with self.subTest(text=text):
                self.assertIsNone(parse_book_ticker(text))

    def test_unusable_prices_are_rejected(self) -> None:
        for bid, ask in (("0", "1"), ("-1", "1"), ("abc", "1"), ("1", "nan"), ("1", "inf"), ("101", "100")):
            with self.subTest(bid=bid, ask=ask):
                self.assertIsNone(parse_book_ticker(ticker_json(bid, ask)))

    def test_a_locked_book_with_equal_bid_and_ask_is_accepted(self) -> None:
        self.assertEqual(parse_book_ticker(ticker_json("100", "100")), BookTick("BTC-USDT", 100.0, 100.0))


class DecodeTests(unittest.TestCase):
    def test_gzip_payloads_are_decompressed(self) -> None:
        self.assertEqual(decode_payload(gzip.compress(b"hello")), "hello")

    def test_plain_payloads_pass_through(self) -> None:
        self.assertEqual(decode_payload(b"Ping"), "Ping")

    def test_undecodable_bytes_do_not_raise(self) -> None:
        self.assertIsInstance(decode_payload(b"\xff\xfe\x00bad"), str)

    def test_a_decompression_bomb_is_refused(self) -> None:
        bomb = gzip.compress(b"0" * (4 * 1024 * 1024))

        self.assertEqual(decode_payload(bomb), "")


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def feed_for(server: FakeServer, ticks: list[BookTick], **kwargs: object) -> BookTickerFeed:
    async def connect() -> WebSocketConnection:
        return await WebSocketConnection.connect("127.0.0.1", server.port, "/swap-market", ssl_context=None, timeout=2)

    options: dict[str, object] = {"connect": connect, "idle_timeout": 5.0, "initial_backoff": 0.01, "max_backoff": 0.05, "logger": LOGGER}
    options.update(kwargs)
    return BookTickerFeed(["BTC-USDT"], ticks.append, **options)  # type: ignore[arg-type]


class FeedTests(unittest.IsolatedAsyncioTestCase):
    async def run_feed(self, feed: BookTickerFeed, until: Callable[[], bool]) -> None:
        task = asyncio.create_task(feed.run())
        try:
            await wait_until(until)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_subscribes_only_to_the_book_ticker_of_each_pair(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            srv.received.append(await read_client_frame(reader))
            await asyncio.sleep(0.3)

        ticks: list[BookTick] = []
        async with FakeServer(script) as server:
            feed = feed_for(server, ticks)
            await self.run_feed(feed, lambda: bool(server.received))

        opcode, masked, payload = server.received[0]
        request = json.loads(payload)
        self.assertEqual((opcode, masked), (OP_TEXT, True))
        self.assertEqual((request["reqType"], request["dataType"]), ("sub", "BTC-USDT@bookTicker"))
        self.assertTrue(request["id"])

    async def test_never_sends_anything_but_book_ticker_subscriptions_and_pongs(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            writer.write(gz_frame(ticker_json()) + server_frame(OP_TEXT, b"Ping"))
            await writer.drain()
            for _ in range(2):
                srv.received.append(await read_client_frame(reader))
            await asyncio.sleep(0.3)

        ticks: list[BookTick] = []
        async with FakeServer(script) as server:
            await self.run_feed(feed_for(server, ticks), lambda: len(server.received) == 2)

        sent = [json.loads(p) if p != b"Pong" else p for _, _, p in server.received]
        self.assertEqual(sent[1], b"Pong")
        self.assertEqual(sent[0]["reqType"], "sub")
        self.assertTrue(sent[0]["dataType"].endswith("@bookTicker"))
        self.assertNotIn("listenKey", json.dumps(sent[0]))

    async def test_gzip_ticks_reach_the_callback(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            await read_client_frame(reader)
            writer.write(gz_frame(ticker_json("100", "102")) + gz_frame(ticker_json("101", "103")))
            await writer.drain()
            await asyncio.sleep(0.3)

        ticks: list[BookTick] = []
        async with FakeServer(script) as server:
            await self.run_feed(feed_for(server, ticks), lambda: len(ticks) == 2)

        self.assertEqual(ticks, [BookTick("BTC-USDT", 100.0, 102.0), BookTick("BTC-USDT", 101.0, 103.0)])

    async def test_a_callback_failure_does_not_stop_the_feed(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            await read_client_frame(reader)
            writer.write(gz_frame(ticker_json("1", "2")) + gz_frame(ticker_json("3", "4")))
            await writer.drain()
            await asyncio.sleep(0.3)

        seen: list[BookTick] = []

        def flaky(tick: BookTick) -> None:
            seen.append(tick)
            if len(seen) == 1:
                raise RuntimeError("callback exploded")

        async def connect() -> WebSocketConnection:
            return await WebSocketConnection.connect("127.0.0.1", server.port, "/swap-market", ssl_context=None, timeout=2)

        async with FakeServer(script) as server:
            feed = BookTickerFeed(["BTC-USDT"], flaky, connect=connect, idle_timeout=5.0, logger=LOGGER)
            await self.run_feed(feed, lambda: len(seen) == 2)

        self.assertEqual([t.bid for t in seen], [1.0, 3.0])

    async def test_reconnects_and_resubscribes_after_the_server_drops_it(self) -> None:
        connections = 0

        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            nonlocal connections
            connections += 1
            await read_client_frame(reader)
            writer.write(gz_frame(ticker_json(str(connections), str(connections + 1))))
            await writer.drain()
            if connections == 1:
                return  # drop the first connection right after one tick
            await asyncio.sleep(0.3)

        ticks: list[BookTick] = []
        async with FakeServer(script) as server:
            await self.run_feed(feed_for(server, ticks), lambda: len(ticks) == 2)

        self.assertEqual([t.bid for t in ticks], [1.0, 2.0])
        self.assertEqual(connections, 2)

    async def test_a_silent_connection_is_replaced_after_the_idle_timeout(self) -> None:
        connections = 0

        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            nonlocal connections
            connections += 1
            await read_client_frame(reader)
            await asyncio.sleep(1.0)  # accept the subscription, then say nothing

        ticks: list[BookTick] = []
        async with FakeServer(script) as server:
            await self.run_feed(feed_for(server, ticks, idle_timeout=0.15), lambda: connections >= 2)

    async def test_backoff_doubles_to_the_cap_and_resets_after_a_good_tick(self) -> None:
        delays: list[float] = []
        attempts = 0
        ticks: list[BookTick] = []

        async def fake_sleep(seconds: float) -> None:
            delays.append(seconds)
            await asyncio.sleep(0)

        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            await read_client_frame(reader)
            writer.write(gz_frame(ticker_json()))
            await writer.drain()
            await asyncio.sleep(0.05)

        async def connect() -> WebSocketConnection:
            nonlocal attempts
            attempts += 1
            if attempts <= 4:
                raise OSError("network down")
            return await WebSocketConnection.connect("127.0.0.1", server.port, "/swap-market", ssl_context=None, timeout=2)

        async with FakeServer(script) as server:
            feed = BookTickerFeed(
                ["BTC-USDT"], ticks.append, connect=connect, idle_timeout=5.0, initial_backoff=1.0, max_backoff=4.0, sleep=fake_sleep, logger=LOGGER
            )
            await self.run_feed(feed, lambda: len(delays) >= 6)

        self.assertEqual(delays[:4], [1.0, 2.0, 4.0, 4.0])  # doubled, then held at the cap
        self.assertEqual(delays[4], 1.0)  # a good tick reset it before the connection ended

    async def test_cancelling_the_feed_closes_the_connection(self) -> None:
        closed = asyncio.Event()

        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            await read_client_frame(reader)
            try:
                await reader.read(-1)  # returns once the client closes the socket
            finally:
                closed.set()

        ticks: list[BookTick] = []
        async with FakeServer(script) as server:
            feed = feed_for(server, ticks)
            task = asyncio.create_task(feed.run())
            await asyncio.sleep(0.2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.wait_for(closed.wait(), 2)

        self.assertTrue(task.cancelled())


if __name__ == "__main__":
    unittest.main()
