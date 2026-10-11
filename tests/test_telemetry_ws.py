from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
import unittest

from bot.telemetry.ws import (
    MAX_FRAME_BYTES,
    OP_BINARY,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    WebSocketClosed,
    WebSocketConnection,
    WebSocketError,
    accept_key,
    encode_frame,
    read_frame,
    with_timeout,
)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """A server-to-client frame: never masked."""
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        return head + bytes([n]) + payload
    if n < 65536:
        return head + bytes([126]) + struct.pack(">H", n) + payload
    return head + bytes([127]) + struct.pack(">Q", n) + payload


async def read_client_frame(reader: asyncio.StreamReader) -> tuple[int, bool, bytes]:
    """Independent decoder for what a client sent, so the code under test never checks itself."""
    b1, b2 = await reader.readexactly(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", await reader.readexactly(8))[0]
    masked = bool(b2 & 0x80)
    mask = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(n)
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return b1 & 0x0F, masked, data


class FakeServer:
    """A loopback WebSocket server that runs a script after the handshake."""

    def __init__(self, script, *, status: str = "101 Switching Protocols", bad_accept: bool = False) -> None:  # type: ignore[no-untyped-def]
        self._script = script
        self._status = status
        self._bad_accept = bad_accept
        self.request_head = ""
        self.received: list[tuple[int, bool, bytes]] = []
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def __aenter__(self) -> FakeServer:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            self.request_head = head
            key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n") if line.lower().startswith("sec-websocket-key"))
            accept = accept_key("wrong-key" if self._bad_accept else key)
            writer.write(
                (f"HTTP/1.1 {self._status}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode()
            )
            await writer.drain()
            await self._script(reader, writer, self)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def connect(server: FakeServer) -> WebSocketConnection:
    return await WebSocketConnection.connect("127.0.0.1", server.port, "/swap-market", ssl_context=None, timeout=3)


class CodecTests(unittest.IsolatedAsyncioTestCase):
    def test_accept_key_matches_the_rfc_6455_example(self) -> None:
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        expected = base64.b64encode(hashlib.sha1(("abc" + GUID).encode()).digest()).decode()
        self.assertEqual(accept_key("abc"), expected)

    def test_a_small_client_frame_is_masked_exactly_like_the_rfc_example(self) -> None:
        frame = encode_frame(OP_TEXT, b"Hello", mask=bytes.fromhex("37fa213d"))

        self.assertEqual(frame, bytes.fromhex("818537fa213d7f9f4d5158"))

    def test_longer_client_frames_use_the_extended_length_fields(self) -> None:
        mask = bytes(4)  # an all-zero mask leaves the payload readable

        medium = encode_frame(OP_TEXT, b"x" * 200, mask=mask)
        large = encode_frame(OP_TEXT, b"x" * 70_000, mask=mask)

        self.assertEqual(medium[:4], bytes([0x81, 0x80 | 126]) + struct.pack(">H", 200))
        self.assertEqual(large[:10], bytes([0x81, 0x80 | 127]) + struct.pack(">Q", 70_000))

    async def test_reads_a_short_unmasked_server_frame(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(server_frame(OP_TEXT, b"Hello"))

        self.assertEqual(await read_frame(reader), (True, OP_TEXT, b"Hello"))

    async def test_reads_the_two_extended_length_forms(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(server_frame(OP_BINARY, b"a" * 300) + server_frame(OP_BINARY, b"b" * 70_000))

        self.assertEqual(await read_frame(reader), (True, OP_BINARY, b"a" * 300))
        self.assertEqual(await read_frame(reader), (True, OP_BINARY, b"b" * 70_000))

    async def test_refuses_a_frame_larger_than_the_cap_before_reading_it(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(bytes([0x82, 127]) + struct.pack(">Q", MAX_FRAME_BYTES + 1))

        with self.assertRaises(WebSocketError):
            await read_frame(reader)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_connects_and_receives_a_text_message(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            writer.write(server_frame(OP_TEXT, b"hello"))
            await writer.drain()
            await asyncio.sleep(0.2)

        async with FakeServer(script) as server:
            conn = await connect(server)
            try:
                self.assertEqual(await asyncio.wait_for(conn.receive(), 2), (OP_TEXT, b"hello"))
            finally:
                await conn.close()

        self.assertTrue(server.request_head.startswith("GET /swap-market HTTP/1.1"))
        self.assertIn("Upgrade: websocket", server.request_head)

    async def test_sends_text_as_a_masked_frame(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            srv.received.append(await read_client_frame(reader))

        async with FakeServer(script) as server:
            conn = await connect(server)
            try:
                await conn.send_text('{"reqType":"sub"}')
                await asyncio.sleep(0.2)
            finally:
                await conn.close()

        self.assertEqual(server.received[0], (OP_TEXT, True, b'{"reqType":"sub"}'))

    async def test_reassembles_a_fragmented_message(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            writer.write(server_frame(OP_TEXT, b"foo", fin=False) + server_frame(OP_CONTINUATION, b"bar"))
            await writer.drain()
            await asyncio.sleep(0.2)

        async with FakeServer(script) as server:
            conn = await connect(server)
            try:
                self.assertEqual(await asyncio.wait_for(conn.receive(), 2), (OP_TEXT, b"foobar"))
            finally:
                await conn.close()

    async def test_answers_a_protocol_ping_with_a_pong_that_echoes_it(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            writer.write(server_frame(OP_PING, b"beat") + server_frame(OP_TEXT, b"after"))
            await writer.drain()
            srv.received.append(await read_client_frame(reader))

        async with FakeServer(script) as server:
            conn = await connect(server)
            try:
                # The ping is handled inside receive(); the caller only sees the next message.
                self.assertEqual(await asyncio.wait_for(conn.receive(), 2), (OP_TEXT, b"after"))
                await asyncio.sleep(0.2)
            finally:
                await conn.close()

        self.assertEqual(server.received[0], (OP_PONG, True, b"beat"))

    async def test_a_close_frame_raises_closed(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            writer.write(server_frame(OP_CLOSE, struct.pack(">H", 1000)))
            await writer.drain()
            await asyncio.sleep(0.2)

        async with FakeServer(script) as server:
            conn = await connect(server)
            try:
                with self.assertRaises(WebSocketClosed):
                    await asyncio.wait_for(conn.receive(), 2)
            finally:
                await conn.close()

    async def test_a_dropped_connection_raises_closed(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            return  # the handler closes the socket straight away

        async with FakeServer(script) as server:
            conn = await connect(server)
            try:
                with self.assertRaises(WebSocketClosed):
                    await asyncio.wait_for(conn.receive(), 2)
            finally:
                await conn.close()

    async def test_a_refused_upgrade_is_an_error(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            return

        async with FakeServer(script, status="403 Forbidden") as server:
            with self.assertRaises(WebSocketError):
                await connect(server)

    async def test_a_wrong_accept_key_is_an_error(self) -> None:
        async def script(reader, writer, srv) -> None:  # type: ignore[no-untyped-def]
            return

        async with FakeServer(script, bad_accept=True) as server:
            with self.assertRaises(WebSocketError):
                await connect(server)

    async def test_connecting_to_a_closed_port_is_an_error_not_a_hang(self) -> None:
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()

        with self.assertRaises(OSError):
            await WebSocketConnection.connect("127.0.0.1", port, "/", ssl_context=None, timeout=2)


class WithTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_the_result_when_the_work_finishes_in_time(self) -> None:
        self.assertEqual(await with_timeout(asyncio.sleep(0, "ok"), 1), "ok")

    async def test_times_out_and_cancels_the_work(self) -> None:
        cancelled = asyncio.Event()

        async def slow() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with self.assertRaises(asyncio.TimeoutError):
            await with_timeout(slow(), 0.05)

        self.assertTrue(cancelled.is_set())

    async def test_cancelling_the_waiter_is_never_swallowed_even_if_the_work_finishes_in_the_same_tick(self) -> None:
        # asyncio.wait_for (3.11 and earlier) returns the finished result and drops the
        # cancellation here, which left a stopped feed running forever.
        gate = asyncio.Event()

        async def work() -> str:
            await gate.wait()
            return "late"

        waiter = asyncio.create_task(with_timeout(work(), 5))
        await asyncio.sleep(0.01)  # the waiter is now parked on the work
        gate.set()
        waiter.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await waiter


if __name__ == "__main__":
    unittest.main()
