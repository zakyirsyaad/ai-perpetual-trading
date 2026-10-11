"""Live best bid/ask for the dashboard chart, from BingX's public VST market stream.

Read-only by construction: one outbound connection to the simulated-trading market host,
subscribing to nothing but `<PAIR>@bookTicker`. No credential, listen key or account stream
is involved, and the host is fixed (not configurable) so it can only ever be the VST one,
matching the REST safety invariant.

Every failure is contained here: the feed reconnects with a capped backoff, a callback that
raises is logged and skipped, and nothing it does can reach the trading loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import ssl
import uuid
import zlib
from collections.abc import Awaitable, Callable, Sequence
from typing import NamedTuple

from bot.telemetry.ws import WebSocketClosed, WebSocketConnection, with_timeout

LOGGER = logging.getLogger("volume_bot.telemetry")

VST_WS_HOST = "vst-open-api-ws.bingx.com"
VST_WS_PORT = 443
VST_WS_PATH = "/swap-market"
CHANNEL = "bookTicker"
MAX_DECOMPRESSED_BYTES = 1 << 20
FAILURE_LOG_EVERY = 12  # log the first failure of a streak, then one in twelve
_GZIP_MAGIC = b"\x1f\x8b"


class BookTick(NamedTuple):
    pair: str  # venue style, e.g. BTC-USDT
    bid: float
    ask: float


def decode_payload(payload: bytes) -> str:
    """Message text from a frame: market frames are gzip, the heartbeat is plain text.

    Returns an empty string for a corrupt or oversized gzip body instead of raising.
    """
    if payload[:2] == _GZIP_MAGIC:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        try:
            data = inflater.decompress(payload, MAX_DECOMPRESSED_BYTES)
        except zlib.error:
            return ""
        if inflater.unconsumed_tail:  # more than the cap would expand: refuse, do not allocate it
            return ""
        return data.decode("utf-8", errors="replace")
    return payload.decode("utf-8", errors="replace")


def parse_book_ticker(text: str) -> BookTick | None:
    """A usable top-of-book tick, or None for acks, other channels and unusable prices."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    data_type, data = doc.get("dataType"), doc.get("data")
    if not isinstance(data_type, str) or not data_type.endswith(f"@{CHANNEL}") or not isinstance(data, dict):
        return None
    pair = data.get("s")
    try:
        bid, ask = float(data["b"]), float(data["a"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(pair, str) or not pair:
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask < bid:
        return None
    return BookTick(pair, bid, ask)


async def connect_vst() -> WebSocketConnection:
    return await WebSocketConnection.connect(
        VST_WS_HOST,
        VST_WS_PORT,
        VST_WS_PATH,
        ssl_context=ssl.create_default_context(),
    )


class BookTickerFeed:
    def __init__(
        self,
        pairs: Sequence[str],
        on_tick: Callable[[BookTick], None],
        *,
        connect: Callable[[], Awaitable[WebSocketConnection]] = connect_vst,
        idle_timeout: float = 15.0,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._pairs = tuple(pairs)
        self._on_tick = on_tick
        self._connect = connect
        self._idle_timeout = idle_timeout
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._sleep = sleep
        self._logger = logger
        self._delay = initial_backoff
        self._failures = 0

    async def run(self) -> None:
        """Stay connected forever, reconnecting after any failure. Only cancellation ends it."""
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._failures += 1
                if self._failures % FAILURE_LOG_EVERY == 1:
                    self._logger.warning(
                        "telemetry price stream lost (failure %s in a row); falling back to polling and retrying",
                        self._failures,
                        exc_info=True,
                    )
            await self._sleep(self._delay)
            self._delay = min(self._delay * 2, self._max_backoff)

    async def _session(self) -> None:
        connection = await self._connect()
        try:
            for pair in self._pairs:
                request = {"id": str(uuid.uuid4()), "reqType": "sub", "dataType": f"{pair}@{CHANNEL}"}
                await connection.send_text(json.dumps(request))
            while True:
                try:
                    _, payload = await with_timeout(connection.receive(), self._idle_timeout)
                except asyncio.TimeoutError as exc:  # a distinct class from TimeoutError before 3.11
                    raise WebSocketClosed(f"no message for {self._idle_timeout}s") from exc
                text = decode_payload(payload)
                if text == "Ping":
                    await connection.send_text("Pong")
                    continue
                tick = parse_book_ticker(text)
                if tick is None:
                    continue
                self._delay = self._initial_backoff
                self._failures = 0
                self._deliver(tick)
        finally:
            await connection.close()

    def _deliver(self, tick: BookTick) -> None:
        try:
            self._on_tick(tick)
        except Exception:
            self._logger.warning("telemetry price stream callback failed", exc_info=True)
