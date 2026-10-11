from __future__ import annotations

import asyncio
import logging
import unittest

from bot.telemetry.bus import TelemetryBus
from bot.telemetry.wire import PriceMessage


def price(i: int) -> PriceMessage:
    return PriceMessage(coin="BTC", ts=1_791_661_170_000 + i, mid=65000.0 + i, best_bid=65000.0, best_ask=65001.0, spread_bps=0.15)


class _BrokenQueue:
    def put_nowait(self, _item: object) -> None:
        raise RuntimeError("subscriber exploded")


class TelemetryBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscriber_receives_messages_in_order(self) -> None:
        bus = TelemetryBus()
        queue = bus.subscribe()

        bus.publish(price(1))
        bus.publish(price(2))

        first = await asyncio.wait_for(queue.get(), timeout=1)
        second = await asyncio.wait_for(queue.get(), timeout=1)
        self.assertEqual([first, second], [price(1), price(2)])

    async def test_every_subscriber_gets_its_own_copy(self) -> None:
        bus = TelemetryBus()
        a = bus.subscribe()
        b = bus.subscribe()

        bus.publish(price(1))

        self.assertEqual(a.get_nowait(), price(1))
        self.assertEqual(b.get_nowait(), price(1))

    async def test_slow_subscriber_drops_oldest_and_never_blocks_publish(self) -> None:
        bus = TelemetryBus()
        queue = bus.subscribe(maxsize=2)

        for i in range(5):
            bus.publish(price(i))

        self.assertEqual([queue.get_nowait(), queue.get_nowait()], [price(3), price(4)])
        self.assertTrue(queue.empty())

    async def test_a_broken_subscriber_does_not_starve_the_others(self) -> None:
        bus = TelemetryBus()
        bus._subscribers.add(_BrokenQueue())  # type: ignore[arg-type]
        healthy = bus.subscribe()

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            bus.publish(price(1))

        self.assertEqual(healthy.get_nowait(), price(1))

    async def test_publishing_a_foreign_object_never_raises_and_reaches_nobody(self) -> None:
        bus = TelemetryBus()
        queue = bus.subscribe()

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            bus.publish(object())  # type: ignore[arg-type]

        self.assertTrue(queue.empty())

    async def test_unsubscribe_stops_delivery(self) -> None:
        bus = TelemetryBus()
        queue = bus.subscribe()
        bus.unsubscribe(queue)

        bus.publish(price(1))

        self.assertTrue(queue.empty())
        self.assertEqual(bus.subscriber_count, 0)

    async def test_unsubscribing_twice_is_harmless(self) -> None:
        bus = TelemetryBus()
        queue = bus.subscribe()

        bus.unsubscribe(queue)
        bus.unsubscribe(queue)

        self.assertEqual(bus.subscriber_count, 0)

    async def test_an_unexpected_failure_inside_publish_is_logged_not_raised(self) -> None:
        class _ExplodingSet(set):
            def __iter__(self):
                raise RuntimeError("iteration exploded")

        bus = TelemetryBus()
        bus._subscribers = _ExplodingSet({object()})  # type: ignore[assignment]

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            bus.publish(price(1))

    async def test_publish_with_no_subscribers_is_a_no_op(self) -> None:
        TelemetryBus().publish(price(1))


if __name__ == "__main__":
    unittest.main()
