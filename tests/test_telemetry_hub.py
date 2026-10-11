from __future__ import annotations

import json
import logging
import unittest
from pathlib import Path

from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.wire import BlockEvent, FillData, FillMessage, Meta, PriceMessage, Quote, QuoteMessage

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "jev_wire_samples.json").read_text())
META = Meta.model_validate(SAMPLES["meta"])
HOLD = BlockEvent.model_validate(SAMPLES["events"]["hold"])  # BTC
OPEN = BlockEvent.model_validate(SAMPLES["events"]["open"])  # ETH, carries a quote
T0 = 1_791_661_170_000


def make_hub(**kwargs: object) -> tuple[TelemetryHub, TelemetryBus]:
    bus = TelemetryBus()
    return TelemetryHub(META, bus, **kwargs), bus  # type: ignore[arg-type]


def price(coin: str = "BTC", mid: float = 100.0, ts: int = T0) -> PriceMessage:
    return PriceMessage(coin=coin, ts=ts, mid=mid, best_bid=mid - 0.5, best_ask=mid + 0.5, spread_bps=1.0)


def quote() -> Quote:
    return Quote(side="buy", price=100.0, size=0.5, tx_hash=None, cancel=[], status="sim", order_id=None, capped=False)


def fill_data() -> FillData:
    return FillData(side="buy", size=0.5, price=100.0, tx_hash=None, order_id=0, simulated=False, direction="open")


class RecordBlockTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_block_is_stored_published_and_becomes_the_latest(self) -> None:
        hub, bus = make_hub()
        queue = bus.subscribe()

        hub.record_block(HOLD)

        self.assertEqual(queue.get_nowait(), HOLD)
        self.assertEqual(hub.history_body()["BTC"][0]["block"], HOLD.block)
        self.assertEqual(hub.root_body()["latestByCoin"]["BTC"]["block"], HOLD.block)

    async def test_history_keeps_only_the_newest_events(self) -> None:
        hub, _ = make_hub(history_size=3)

        for i in range(1, 6):
            hub.record_block(HOLD.model_copy(update={"block": i}))

        self.assertEqual([e["block"] for e in hub.history_body()["BTC"]], [3, 4, 5])

    async def test_an_event_for_an_unknown_coin_is_dropped_with_a_warning(self) -> None:
        hub, bus = make_hub()
        queue = bus.subscribe()

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            hub.record_block(HOLD.model_copy(update={"coin": "DOGE"}))

        self.assertTrue(queue.empty())
        self.assertNotIn("DOGE", hub.history_body())


class RecordQuoteAndFillTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_quote_is_attached_to_its_block_and_published(self) -> None:
        hub, bus = make_hub()
        hub.record_block(HOLD)
        queue = bus.subscribe()

        hub.record_quote(QuoteMessage(coin="BTC", block=HOLD.block, quote=quote()))

        self.assertEqual(hub.history_body()["BTC"][0]["quote"]["status"], "sim")
        self.assertEqual(queue.get_nowait().event, "quote")

    async def test_a_fill_is_attached_to_its_block_marked_on_the_tape_and_published(self) -> None:
        hub, bus = make_hub()
        hub.record_block(HOLD)
        queue = bus.subscribe()

        hub.record_fill(FillMessage(coin="BTC", block=HOLD.block, fill=fill_data(), ts=T0))

        self.assertEqual(hub.history_body()["BTC"][0]["fill"]["side"], "buy")
        marks = [p for p in hub.tape_body()["BTC"] if "fill" in p]
        self.assertEqual([(m["ts"], m["fill"]["dir"]) for m in marks], [(T0, "open")])
        self.assertEqual(queue.get_nowait().event, "fill")

    async def test_a_fill_without_a_timestamp_uses_the_clock(self) -> None:
        hub, _ = make_hub(clock=lambda: 1_791_661_999.0)

        hub.record_fill(FillMessage(coin="BTC", block=0, fill=fill_data()))

        [mark] = [p for p in hub.tape_body()["BTC"] if "fill" in p]
        self.assertEqual(mark["ts"], 1_791_661_999_000)

    async def test_a_quote_for_a_missing_block_is_still_published(self) -> None:
        hub, bus = make_hub()
        queue = bus.subscribe()

        hub.record_quote(QuoteMessage(coin="BTC", block=999, quote=quote()))

        self.assertEqual(queue.get_nowait().event, "quote")


class RecordPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_price_builds_the_second_candle_and_is_published(self) -> None:
        hub, bus = make_hub()
        queue = bus.subscribe()

        hub.record_price(price(mid=100.0))

        [point] = hub.tape_body()["BTC"]
        self.assertEqual((point["bar"], point["close"]), ("1s", 100.0))
        self.assertEqual(queue.get_nowait().event, "price")

    async def test_klines_load_into_the_tape(self) -> None:
        hub, _ = make_hub()

        hub.load_klines("BTC", "1m", [{"timestamp": float(T0), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}])

        [point] = hub.tape_body()["BTC"]
        self.assertEqual((point["bar"], point["high"]), ("1m", 2.0))


class LatestPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_newest_price_per_coin_is_remembered(self) -> None:
        hub, _ = make_hub()

        hub.record_price(price(mid=100.0, ts=T0))
        hub.record_price(price(mid=101.0, ts=T0 + 1000))

        latest = hub.latest_price("BTC")
        assert latest is not None
        self.assertEqual(latest.mid, 101.0)
        self.assertIsNone(hub.latest_price("ETH"))
        self.assertIsNone(hub.latest_price("DOGE"))


class RefreshLatestTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_latest_block_is_updated_in_place_and_republished_under_the_same_number(self) -> None:
        hub, bus = make_hub()
        hub.record_block(HOLD)
        queue = bus.subscribe()

        hub.refresh_latest("BTC", mid=HOLD.mid + 5)

        [stored] = hub.history_body()["BTC"]
        self.assertEqual((stored["block"], stored["mid"]), (HOLD.block, HOLD.mid + 5))
        republished = queue.get_nowait()
        self.assertEqual((republished.event, republished.block, republished.mid), ("block", HOLD.block, HOLD.mid + 5))

    async def test_nothing_happens_when_the_coin_has_no_block_yet(self) -> None:
        hub, bus = make_hub()
        queue = bus.subscribe()

        hub.refresh_latest("BTC", mid=1.0)

        self.assertEqual(hub.history_body()["BTC"], [])
        self.assertTrue(queue.empty())

    async def test_a_corrupt_block_is_logged_and_never_reaches_the_caller(self) -> None:
        hub, _ = make_hub()
        hub._history["BTC"].append(object())  # type: ignore[arg-type]

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            hub.refresh_latest("BTC", mid=1.0)


class UnknownCoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_kind_of_message_for_an_unknown_coin_is_dropped(self) -> None:
        hub, bus = make_hub()
        queue = bus.subscribe()
        actions = (
            lambda: hub.record_quote(QuoteMessage(coin="DOGE", block=1, quote=quote())),
            lambda: hub.record_fill(FillMessage(coin="DOGE", block=1, fill=fill_data(), ts=T0)),
            lambda: hub.record_price(price("DOGE")),
            lambda: hub.refresh_latest("DOGE", mid=1.0),
            lambda: hub.load_klines("DOGE", "1m", []),
        )

        for action in actions:
            with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
                action()

        self.assertTrue(queue.empty())


class NeverRaisesTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_quote_fill_or_kline_load_is_logged_not_raised(self) -> None:
        hub, _ = make_hub()
        hub._history["BTC"] = None  # type: ignore[assignment]
        hub._charts["BTC"] = object()  # type: ignore[assignment]
        actions = (
            lambda: hub.record_quote(QuoteMessage(coin="BTC", block=1, quote=quote())),
            lambda: hub.record_fill(FillMessage(coin="BTC", block=1, fill=fill_data(), ts=T0)),
            lambda: hub.load_klines("BTC", "1m", []),
        )

        for action in actions:
            with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
                action()

    async def test_a_broken_chart_is_logged_and_never_reaches_the_caller(self) -> None:
        hub, _ = make_hub()
        hub._charts["BTC"] = object()  # type: ignore[assignment]

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            hub.record_price(price())

    async def test_a_broken_history_is_logged_and_never_reaches_the_caller(self) -> None:
        hub, _ = make_hub()
        hub._history["BTC"] = None  # type: ignore[assignment]

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            hub.record_block(HOLD)


class BodyShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_has_the_meta_fields_plus_history_and_tape_for_every_sleeve(self) -> None:
        hub, _ = make_hub()
        hub.record_block(OPEN)

        body = hub.snapshot_body()

        for key in SAMPLES["meta"]:
            self.assertIn(key, body)
        coins = [s["coin"] for s in SAMPLES["meta"]["sleeves"]]
        self.assertEqual(sorted(body["historyByCoin"]), sorted(coins))
        self.assertEqual(sorted(body["tapeByCoin"]), sorted(coins))
        self.assertEqual(body["historyByCoin"]["ETH"][0]["block"], OPEN.block)
        self.assertEqual(body["historyByCoin"]["BTC"], [])

    async def test_snapshot_history_is_clipped_to_the_newest_twelve(self) -> None:
        hub, _ = make_hub()
        for i in range(1, 31):
            hub.record_block(HOLD.model_copy(update={"block": i}))

        blocks = [e["block"] for e in hub.snapshot_body()["historyByCoin"]["BTC"]]

        self.assertEqual(blocks, list(range(19, 31)))

    async def test_root_body_lists_null_for_a_coin_with_no_events_yet(self) -> None:
        hub, _ = make_hub()

        latest = hub.root_body()["latestByCoin"]

        self.assertEqual(sorted(latest), sorted(s["coin"] for s in SAMPLES["meta"]["sleeves"]))
        self.assertTrue(all(v is None for v in latest.values()))

    async def test_bodies_are_json_serialisable(self) -> None:
        hub, _ = make_hub()
        hub.record_block(OPEN)
        hub.record_price(price("ETH"))

        for body in (hub.snapshot_body(), hub.root_body(), hub.history_body(), hub.tape_body()):
            json.dumps(body)


if __name__ == "__main__":
    unittest.main()
