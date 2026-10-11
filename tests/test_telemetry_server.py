from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.server import TelemetryServer
from bot.telemetry.wire import BlockEvent, Meta, PriceMessage

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "jev_wire_samples.json").read_text())
HOLD = BlockEvent.model_validate(SAMPLES["events"]["hold"])
ORIGIN = "http://localhost:3001"
TIMEOUT = 3.0


def get(path: str, host: str | None = "localhost", extra: str = "", method: str = "GET") -> bytes:
    host_line = f"Host: {host}\r\n" if host is not None else ""
    return f"{method} {path} HTTP/1.1\r\n{host_line}{extra}\r\n".encode()


def parse(data: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = {k.lower(): v for k, v in (line.split(": ", 1) for line in lines[1:])}
    return int(lines[0].split()[1]), headers, body


async def request(port: int, raw: bytes) -> tuple[int | None, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(raw)
        await writer.drain()
        try:
            data = await asyncio.wait_for(reader.read(-1), TIMEOUT)
        except ConnectionResetError:
            return None, {}, b""
        return parse(data) if data else (None, {}, b"")
    finally:
        writer.close()


async def open_events(port: int, path: str = "/events") -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(get(path))
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), TIMEOUT)
    return reader, writer, head


async def next_frame(reader: asyncio.StreamReader) -> tuple[str, Any]:
    raw = await asyncio.wait_for(reader.readuntil(b"\n\n"), TIMEOUT)
    name_line, data_line = raw.decode().strip().split("\n")
    return name_line[len("event: "):], json.loads(data_line[len("data: "):])


def price(mid: float = 100.0) -> PriceMessage:
    return PriceMessage(coin="BTC", ts=1_791_661_170_000, mid=mid, best_bid=mid - 0.5, best_ask=mid + 0.5, spread_bps=1.0)


class ServerCase(unittest.IsolatedAsyncioTestCase):
    max_clients = 4

    async def asyncSetUp(self) -> None:
        self.hub = TelemetryHub(Meta.model_validate(SAMPLES["meta"]), TelemetryBus())
        self.server = TelemetryServer(
            self.hub,
            host="127.0.0.1",
            port=0,
            allowed_origin=ORIGIN,
            ping_seconds=0.05,
            request_timeout=0.3,
            max_clients=self.max_clients,
        )
        await self.server.start()
        self.port = self.server.bound_port

    async def asyncTearDown(self) -> None:
        await self.server.stop()


class JsonEndpointTests(ServerCase):
    async def test_root_returns_meta_and_the_latest_event_per_coin(self) -> None:
        self.hub.record_block(HOLD)

        status, headers, body = await request(self.port, get("/"))

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(payload["venue"], SAMPLES["meta"]["venue"])
        self.assertEqual(payload["latestByCoin"]["BTC"]["block"], HOLD.block)

    async def test_snapshot_history_and_tape_are_served(self) -> None:
        self.hub.record_block(HOLD)
        self.hub.record_price(price())

        snapshot = json.loads((await request(self.port, get("/snapshot")))[2])
        history = json.loads((await request(self.port, get("/history")))[2])
        tape = json.loads((await request(self.port, get("/tape")))[2])

        self.assertIn("historyByCoin", snapshot)
        self.assertEqual(history["BTC"][0]["block"], HOLD.block)
        self.assertEqual(tape["BTC"][0]["bar"], "1s")

    async def test_responses_are_not_cacheable_and_close_the_connection(self) -> None:
        _, headers, _ = await request(self.port, get("/"))

        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["connection"], "close")
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    async def test_unknown_paths_return_404_json(self) -> None:
        status, _, body = await request(self.port, get("/nope"))

        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    async def test_a_query_string_does_not_change_the_route(self) -> None:
        status, _, _ = await request(self.port, get("/history?x=1"))

        self.assertEqual(status, 200)

    async def test_a_handler_failure_returns_500_and_the_server_keeps_serving(self) -> None:
        with mock.patch.object(self.hub, "snapshot_body", side_effect=RuntimeError("boom")):
            with self.assertLogs("volume_bot.telemetry", level="WARNING"):
                status, _, _ = await request(self.port, get("/snapshot"))
        after, _, _ = await request(self.port, get("/"))

        self.assertEqual(status, 500)
        self.assertEqual(after, 200)


class AccessControlTests(ServerCase):
    async def test_the_configured_origin_is_allowed_by_cors(self) -> None:
        _, headers, _ = await request(self.port, get("/", extra=f"Origin: {ORIGIN}\r\n"))

        self.assertEqual(headers["access-control-allow-origin"], ORIGIN)
        self.assertEqual(headers["vary"], "Origin")

    async def test_any_other_origin_gets_no_cors_header(self) -> None:
        _, headers, _ = await request(self.port, get("/", extra="Origin: https://evil.example\r\n"))

        self.assertNotIn("access-control-allow-origin", headers)

    async def test_preflight_answers_204_with_the_allowed_methods(self) -> None:
        status, headers, _ = await request(self.port, get("/events", extra=f"Origin: {ORIGIN}\r\n", method="OPTIONS"))

        self.assertEqual(status, 204)
        self.assertEqual(headers["access-control-allow-methods"], "GET, OPTIONS")

    async def test_only_get_and_options_are_accepted(self) -> None:
        status, headers, _ = await request(self.port, get("/", method="POST"))

        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, OPTIONS")

    async def test_a_foreign_host_header_is_refused_to_stop_dns_rebinding(self) -> None:
        status, _, _ = await request(self.port, get("/", host="evil.example.com"))

        self.assertEqual(status, 403)

    async def test_loopback_host_names_are_accepted_on_any_port(self) -> None:
        for host in ("localhost", "localhost:4000", "127.0.0.1", "127.0.0.1:3000", "[::1]:3000"):
            with self.subTest(host=host):
                status, _, _ = await request(self.port, get("/", host=host))
                self.assertEqual(status, 200)

    async def test_a_missing_host_header_is_a_bad_request(self) -> None:
        status, _, _ = await request(self.port, get("/", host=None))

        self.assertEqual(status, 400)


class HostileInputTests(ServerCase):
    async def test_a_garbage_request_line_is_a_bad_request(self) -> None:
        status, _, _ = await request(self.port, b"garbage\r\n\r\n")

        self.assertEqual(status, 400)

    async def test_an_oversized_request_is_refused_and_the_server_survives(self) -> None:
        status, _, _ = await request(self.port, get("/", extra="X-Pad: " + "a" * 12_000 + "\r\n"))
        after, _, _ = await request(self.port, get("/"))

        self.assertIn(status, (431, None))
        self.assertEqual(after, 200)

    async def test_a_silent_connection_is_closed_after_the_request_timeout(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)

        data = await asyncio.wait_for(reader.read(-1), TIMEOUT)
        writer.close()
        after, _, _ = await request(self.port, get("/"))

        self.assertEqual(data, b"")
        self.assertEqual(after, 200)


class EventStreamTests(ServerCase):
    async def test_the_stream_opens_with_a_snapshot_then_relays_messages(self) -> None:
        reader, writer, head = await open_events(self.port)

        self.assertIn(b"200 OK", head)
        self.assertIn(b"text/event-stream", head)
        name, payload = await next_frame(reader)
        self.assertEqual(name, "snapshot")
        self.assertIn("historyByCoin", payload)

        self.hub.record_price(price(123.0))
        while True:
            name, payload = await next_frame(reader)
            if name != "ping":
                break
        writer.close()
        self.assertEqual((name, payload["mid"]), ("price", 123.0))

    async def test_lite_mode_sends_ready_instead_of_the_snapshot(self) -> None:
        reader, writer, _ = await open_events(self.port, "/events?lite=1")

        name, payload = await next_frame(reader)
        writer.close()

        self.assertEqual((name, payload), ("ready", SAMPLES["meta"]["startedAt"]))

    async def test_an_idle_stream_sends_pings(self) -> None:
        reader, writer, _ = await open_events(self.port)
        await next_frame(reader)

        name, payload = await next_frame(reader)
        writer.close()

        self.assertEqual(name, "ping")
        self.assertIsInstance(payload, int)

    async def test_a_disconnected_client_releases_its_subscription(self) -> None:
        reader, writer, _ = await open_events(self.port)
        await next_frame(reader)
        self.assertEqual(self.hub.bus.subscriber_count, 1)

        writer.close()
        for _ in range(60):
            if self.hub.bus.subscriber_count == 0:
                break
            await asyncio.sleep(0.05)

        self.assertEqual(self.hub.bus.subscriber_count, 0)

    async def test_stopping_the_server_ends_open_streams(self) -> None:
        reader, writer, _ = await open_events(self.port)
        await next_frame(reader)

        await self.server.stop()
        rest = await asyncio.wait_for(reader.read(-1), TIMEOUT)
        writer.close()

        self.assertNotIn(b"event: snapshot", rest)


class LifecycleTests(ServerCase):
    async def test_starting_twice_and_stopping_twice_are_harmless(self) -> None:
        port = self.port

        await self.server.start()
        self.assertEqual(self.server.bound_port, port)
        await self.server.stop()
        await self.server.stop()

        with self.assertRaises(RuntimeError):
            self.server.bound_port

    async def test_an_unexpected_connection_failure_is_logged_and_the_server_survives(self) -> None:
        with mock.patch.object(self.server, "_serve", side_effect=RuntimeError("boom")):
            with self.assertLogs("volume_bot.telemetry", level="WARNING"):
                status, _, _ = await request(self.port, get("/"))
        after, _, _ = await request(self.port, get("/"))

        self.assertIsNone(status)
        self.assertEqual(after, 200)


class ClientLimitTests(ServerCase):
    max_clients = 1

    async def test_a_second_stream_is_refused_until_the_first_leaves(self) -> None:
        first_reader, first_writer, _ = await open_events(self.port)
        await next_frame(first_reader)

        status, _, _ = await request(self.port, get("/events"))
        first_writer.close()
        for _ in range(60):
            if self.hub.bus.subscriber_count == 0:
                break
            await asyncio.sleep(0.05)
        third_reader, third_writer, third_head = await open_events(self.port)
        third_writer.close()

        self.assertEqual(status, 503)
        self.assertIn(b"200 OK", third_head)


if __name__ == "__main__":
    unittest.main()
