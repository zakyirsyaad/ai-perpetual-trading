"""A small WebSocket client (RFC 6455) on asyncio streams, standard library only.

The dashboard price stream needs one outbound, read-only connection to a public market-data
host. This process holds trading keys, so it takes no new dependency for that: the framing
needed here is a few dozen lines, and every byte is covered by a test.

Scope is deliberately narrow: client side only, text and binary messages, fragments
reassembled, protocol pings answered, no extensions and no compression negotiation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import ssl
import struct

# Re-exported: telemetry imports `with_timeout` from here; it lives in `bot.asyncutil` so the trading core
# can use it without importing the telemetry package.
from bot.asyncutil import with_timeout

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_FRAME_BYTES = 1 << 20  # market frames are tiny; refuse anything near a megabyte
MAX_HEADER_BYTES = 8192
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(Exception):
    """The peer broke the protocol or refused the upgrade."""


class WebSocketClosed(Exception):
    """The connection ended, by a close frame or because the socket dropped."""


def accept_key(key: str) -> str:
    """The `Sec-WebSocket-Accept` value a server must return for `key`.

    RFC 6455 fixes SHA-1 here. It only proves the server understood the upgrade request and
    protects nothing, hence `usedforsecurity=False`.
    """
    # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1
    digest = hashlib.sha1((key + _GUID).encode(), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode()


def encode_frame(opcode: int, payload: bytes, *, mask: bytes) -> bytes:
    """One final client frame. Clients must mask every frame they send."""
    if len(mask) != 4:
        raise ValueError("mask must be 4 bytes")
    size = len(payload)
    if size < 126:
        head = bytes([0x80 | opcode, 0x80 | size])
    elif size < 65536:
        head = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", size)
    else:
        head = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", size)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return head + mask + masked


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """Read one frame as (fin, opcode, payload). Raises on a truncated stream or an oversize frame."""
    try:
        first, second = await reader.readexactly(2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack(">H", await reader.readexactly(2))[0]
        elif size == 127:
            size = struct.unpack(">Q", await reader.readexactly(8))[0]
        if size > MAX_FRAME_BYTES:
            raise WebSocketError(f"frame of {size} bytes exceeds the {MAX_FRAME_BYTES} byte cap")
        mask = await reader.readexactly(4) if second & 0x80 else b""
        payload = await reader.readexactly(size) if size else b""
    except (asyncio.IncompleteReadError, ConnectionError) as exc:
        raise WebSocketClosed("connection dropped") from exc
    if mask:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bool(first & 0x80), first & 0x0F, payload


class WebSocketConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        path: str,
        *,
        ssl_context: ssl.SSLContext | None,
        timeout: float = 10.0,
    ) -> WebSocketConnection:
        """Open the socket and complete the upgrade handshake, or raise within `timeout`."""
        reader, writer = await with_timeout(
            asyncio.open_connection(host, port, ssl=ssl_context, server_hostname=host if ssl_context else None),
            timeout,
        )
        try:
            await with_timeout(cls._handshake(reader, writer, host, port, path), timeout)
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer)

    @staticmethod
    async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        host_header = host if port in (80, 443) else f"{host}:{port}"
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError) as exc:
            raise WebSocketError("no usable handshake response") from exc
        if len(head) > MAX_HEADER_BYTES:
            raise WebSocketError("handshake response too large")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise WebSocketError(f"upgrade refused: {lines[0]}")
        headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:] if ":" in line)}
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise WebSocketError("handshake accept key does not match")

    async def send_text(self, text: str) -> None:
        await self._send(OP_TEXT, text.encode())

    async def _send(self, opcode: int, payload: bytes) -> None:
        try:
            self._writer.write(encode_frame(opcode, payload, mask=os.urandom(4)))
            await self._writer.drain()
        except ConnectionError as exc:
            raise WebSocketClosed("connection dropped") from exc

    async def receive(self) -> tuple[int, bytes]:
        """Next complete text or binary message as (opcode, payload).

        Protocol pings are answered here and never surface; a close frame or a dropped
        socket raises `WebSocketClosed`.
        """
        opcode = 0
        parts: list[bytes] = []
        size = 0
        while True:
            fin, code, payload = await read_frame(self._reader)
            if code == OP_PING:
                await self._send(OP_PONG, payload)
                continue
            if code == OP_PONG:
                continue
            if code == OP_CLOSE:
                raise WebSocketClosed("closed by peer")
            if code in (OP_TEXT, OP_BINARY):
                opcode, parts, size = code, [payload], len(payload)
            elif code == OP_CONTINUATION and parts:
                parts.append(payload)
                size += len(payload)
            else:
                raise WebSocketError(f"unexpected opcode {code}")
            if size > MAX_FRAME_BYTES:
                raise WebSocketError("message exceeds the size cap")
            if fin:
                return opcode, b"".join(parts)

    async def close(self) -> None:
        """Close the socket. Never raises."""
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001 - shutdown path
            pass
