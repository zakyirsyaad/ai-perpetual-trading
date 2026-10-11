"""In-memory fan-out for dashboard messages.

Telemetry is observability, never a safety path. `publish` is synchronous and
swallows every failure after logging it, so a stuck dashboard, a slow client or a
malformed message can never delay or stop a trading tick. The engine is a single
asyncio loop, so no locking is needed.
"""

from __future__ import annotations

import asyncio
import logging

from bot.telemetry.wire import WIRE_MESSAGE_TYPES, WireMessage

LOGGER = logging.getLogger("volume_bot.telemetry")

DEFAULT_SUBSCRIBER_QUEUE = 256


class TelemetryBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[WireMessage]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, maxsize: int = DEFAULT_SUBSCRIBER_QUEUE) -> asyncio.Queue[WireMessage]:
        queue: asyncio.Queue[WireMessage] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[WireMessage]) -> None:
        self._subscribers.discard(queue)

    def publish(self, message: WireMessage) -> None:
        """Fan one message out to every subscriber. Never raises."""
        try:
            if not isinstance(message, WIRE_MESSAGE_TYPES):
                LOGGER.warning("telemetry dropped non-message object of type %s", type(message).__name__)
                return
            for queue in tuple(self._subscribers):
                self._deliver(queue, message)
        except Exception:  # telemetry must never break trading
            LOGGER.warning("telemetry publish failed", exc_info=True)

    @staticmethod
    def _deliver(queue: asyncio.Queue[WireMessage], message: WireMessage) -> None:
        try:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                queue.get_nowait()
                queue.put_nowait(message)
        except Exception:  # one bad subscriber must not starve the rest
            LOGGER.warning("telemetry subscriber delivery failed", exc_info=True)
