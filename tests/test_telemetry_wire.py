from __future__ import annotations

import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from bot.telemetry.wire import (
    BlockEvent,
    Decision,
    FillData,
    FillMessage,
    Meta,
    Position,
    PriceMessage,
    PricePoint,
    PricePointFill,
    Probabilities,
    Quote,
    QuoteMessage,
    Resting,
    Totals,
    sse_event,
    sse_frame,
    to_wire,
)

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "jev_wire_samples.json").read_text())


def totals() -> Totals:
    return Totals(
        blocks=1,
        decisions=1,
        quotes=0,
        fills=0,
        reverted=0,
        late_blocks=0,
        ai_cost_usd=0.5,
        fee_sz=0.0,
        fee_usd=0.25,
        realized_usd=0.0,
        pnl_usd=0.0,
        pnl_sz=0.0,
        pnl_pct=0.0,
    )


def flat_position() -> Position:
    return Position(side="flat", size=0.0, entry_price=None, leverage=None, unrealized_usd=0.0, unrealized_sz=0.0)


def decision(**overrides: object) -> Decision:
    base: dict[str, object] = dict(
        action="hold",
        probabilities=Probabilities(buy=0.0, sell=0.0, hold=1.0),
        up_in10=0.5,
        latency_ms=1800,
        late=False,
    )
    base.update(overrides)
    return Decision(**base)


def block(**overrides: object) -> BlockEvent:
    base: dict[str, object] = dict(
        coin="BTC",
        block=1,
        ts=1_791_661_170_177,
        mid=65000.5,
        best_bid=65000.0,
        best_ask=65001.0,
        spread_bps=0.15,
        decision=decision(),
        quote=None,
        fill=None,
        resting=Resting(bid_sz=0.0, ask_sz=0.0),
        position=flat_position(),
        totals=totals(),
        account_value=None,
        withdrawable=None,
    )
    base.update(overrides)
    return BlockEvent(**base)


class RealSampleContractTests(unittest.TestCase):
    """The dashboard was written against jev-trade's wire format. Real samples must pass through unchanged."""

    def test_real_meta_round_trips_unchanged(self) -> None:
        self.assertEqual(to_wire(Meta.model_validate(SAMPLES["meta"])), SAMPLES["meta"])

    def test_each_real_block_event_round_trips_unchanged(self) -> None:
        for name, raw in SAMPLES["events"].items():
            with self.subTest(sample=name):
                self.assertEqual(to_wire(BlockEvent.model_validate(raw)), raw)


class WireShapeTests(unittest.TestCase):
    def test_python_names_dump_as_the_camel_case_the_dashboard_reads(self) -> None:
        wire = to_wire(block())

        self.assertIn("bestBid", wire)
        self.assertIn("spreadBps", wire)
        self.assertEqual(wire["decision"]["upIn10"], 0.5)
        self.assertEqual(wire["decision"]["latencyMs"], 1800)

    def test_ai_cost_and_fees_keep_the_dashboard_key_names(self) -> None:
        wire = to_wire(block())["totals"]

        self.assertEqual(wire["jevUsd"], 0.5)
        self.assertEqual(wire["gasUsd"], 0.25)
        self.assertNotIn("aiCostUsd", wire)

    def test_optional_keys_are_omitted_when_unset(self) -> None:
        quote = Quote(side="buy", price=65000.0, size=0.1, tx_hash=None, cancel=[], status="sim", order_id=None, capped=False)

        wire = to_wire(quote)

        for key in ("reduceOnly", "unchanged", "taker"):
            self.assertNotIn(key, wire)
        self.assertIsNone(wire["txHash"])
        self.assertIsNone(wire["orderId"])

    def test_decision_extras_are_omitted_unless_given(self) -> None:
        self.assertNotIn("confidence", to_wire(decision()))
        self.assertNotIn("reason", to_wire(decision()))
        self.assertEqual(to_wire(decision(confidence=0.7, reason="range break"))["confidence"], 0.7)

    def test_nullable_required_keys_stay_as_explicit_null(self) -> None:
        wire = to_wire(block())

        self.assertIn("accountValue", wire)
        self.assertIsNone(wire["accountValue"])
        self.assertIsNone(wire["quote"])
        self.assertIsNone(wire["fill"])

    def test_fill_direction_uses_the_dir_key(self) -> None:
        fill = FillData(side="sell", size=0.1, price=65010.0, tx_hash=None, order_id=0, simulated=False, direction="close")

        self.assertEqual(to_wire(fill)["dir"], "close")


class WireValidationTests(unittest.TestCase):
    def test_unknown_fields_are_rejected_so_secrets_have_nowhere_to_ride(self) -> None:
        raw = dict(SAMPLES["events"]["hold"], apiKey="not-a-real-secret")

        with self.assertRaises(ValidationError):
            BlockEvent.model_validate(raw)

    def test_timestamps_are_positive_integers(self) -> None:
        with self.assertRaises(ValidationError):
            block(ts=-1)
        with self.assertRaises(ValidationError):
            block(ts="yesterday")

    def test_prices_must_be_positive(self) -> None:
        with self.assertRaises(ValidationError):
            block(mid=0.0)

    def test_reason_is_length_bounded(self) -> None:
        with self.assertRaises(ValidationError):
            decision(reason="x" * 301)

    def test_confidence_is_a_fraction(self) -> None:
        with self.assertRaises(ValidationError):
            decision(confidence=1.5)

    def test_models_are_immutable(self) -> None:
        with self.assertRaises(ValidationError):
            block().mid = 1.0  # type: ignore[misc]


class SseMessageTests(unittest.TestCase):
    def test_event_names_match_the_dashboard_listeners(self) -> None:
        quote = Quote(side="buy", price=1.0, size=1.0, tx_hash=None, cancel=[], status="sim", order_id=None, capped=False)
        fill = FillData(side="buy", size=1.0, price=1.0, tx_hash=None, order_id=0, simulated=False)

        self.assertEqual(block().event, "block")
        self.assertEqual(QuoteMessage(coin="BTC", block=1, quote=quote).event, "quote")
        self.assertEqual(FillMessage(coin="BTC", block=0, fill=fill).event, "fill")
        self.assertEqual(
            PriceMessage(coin="BTC", ts=1, mid=1.0, best_bid=1.0, best_ask=1.0, spread_bps=0.0).event, "price"
        )

    def test_sse_frame_is_event_line_then_compact_json_then_blank_line(self) -> None:
        message = PriceMessage(coin="BTC", ts=1_791_661_170_177, mid=65000.5, best_bid=65000.0, best_ask=65001.0, spread_bps=0.15)

        frame = sse_frame(message)

        self.assertTrue(frame.startswith(b"event: price\ndata: "))
        self.assertTrue(frame.endswith(b"\n\n"))
        payload = json.loads(frame.split(b"data: ", 1)[1])
        self.assertEqual(payload, to_wire(message))
        self.assertNotIn(b", ", frame)

    def test_fill_message_timestamp_is_optional(self) -> None:
        fill = FillData(side="buy", size=1.0, price=1.0, tx_hash=None, order_id=0, simulated=False)

        self.assertNotIn("ts", to_wire(FillMessage(coin="BTC", block=0, fill=fill)))
        self.assertEqual(to_wire(FillMessage(coin="BTC", block=0, fill=fill, ts=5))["ts"], 5)


class TapePointTests(unittest.TestCase):
    def test_a_candle_point_dumps_with_only_the_keys_the_dashboard_reads(self) -> None:
        point = PricePoint(ts=1_791_661_170_000, mid=10.5, open=10.0, high=11.0, low=9.5, close=10.5, bar="1m")

        self.assertEqual(
            to_wire(point),
            {"ts": 1_791_661_170_000, "mid": 10.5, "open": 10.0, "high": 11.0, "low": 9.5, "close": 10.5, "bar": "1m"},
        )

    def test_a_fill_point_carries_the_fill_and_omits_unset_details(self) -> None:
        point = PricePoint(ts=1_791_661_170_000, mid=10.0, fill=PricePointFill(side="sell", price=10.0, size=2.0, direction="close"))

        self.assertEqual(
            to_wire(point)["fill"],
            {"side": "sell", "price": 10.0, "size": 2.0, "dir": "close"},
        )

    def test_a_tape_point_rejects_unknown_fields_and_bad_bars(self) -> None:
        with self.assertRaises(ValidationError):
            PricePoint(ts=1, mid=1.0, bar="5m")
        with self.assertRaises(ValidationError):
            PricePoint(ts=1, mid=1.0, wallet="0xabc")


class SseEventTests(unittest.TestCase):
    def test_a_named_event_is_framed_with_compact_json(self) -> None:
        self.assertEqual(sse_event("ping", 1234), b"event: ping\ndata: 1234\n\n")
        self.assertEqual(sse_event("ready", {"a": 1}), b'event: ready\ndata: {"a":1}\n\n')


if __name__ == "__main__":
    unittest.main()
