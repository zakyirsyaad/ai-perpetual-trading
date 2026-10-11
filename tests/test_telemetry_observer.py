from __future__ import annotations

import json
import logging
import unittest
from datetime import datetime, timezone
from pathlib import Path

from bot.models import ClosedTrade, OpenPosition, PositionDirection, TradePlan
from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.observer import NULL_OBSERVER, EngineTelemetry
from bot.telemetry.wire import BlockEvent, Meta, PriceMessage

SAMPLES = json.loads((Path(__file__).parent / "fixtures" / "jev_wire_samples.json").read_text())
META = Meta.model_validate(SAMPLES["meta"])  # BTC, ETH, SOL
SYMBOLS = ("BTC/USDT", "ETH/USDT")
T0 = 1_791_661_170_000
NOW = datetime(2026, 10, 11, 7, 0, tzinfo=timezone.utc)
LOGGER_NAME = "volume_bot.telemetry"


class Clock:
    def __init__(self) -> None:
        self.now = T0 / 1000

    def __call__(self) -> float:
        return self.now


def price(coin: str, mid: float, ts: int = T0) -> PriceMessage:
    return PriceMessage(coin=coin, ts=ts, mid=mid, best_bid=mid - 0.5, best_ask=mid + 0.5, spread_bps=1.0)


def plan(symbol: str = "BTC/USDT", direction: PositionDirection = PositionDirection.LONG, **overrides) -> TradePlan:
    base = dict(
        symbol=symbol,
        direction=direction,
        usdt_size=180.0,
        leverage=100,
        stop_loss_pct=0.1,
        take_profit_pct=0.2,
        decision_reason="ai_volume:OPEN_LONG",
        confidence=0.8,
    )
    base.update(overrides)
    return TradePlan(**base)


def position(symbol: str = "BTC/USDT", direction: PositionDirection = PositionDirection.LONG, **overrides) -> OpenPosition:
    base = dict(
        symbol=symbol,
        direction=direction,
        entry_price=100.0,
        quantity=0.5,
        exchange_size=0.5,
        exchange_order_id="1001",
        opened_at=NOW,
    )
    base.update(overrides)
    return OpenPosition(**base)


def trade(pos: OpenPosition | None = None, *, pnl: float = 3.0, reason: str = "take_profit", exit_price: float = 110.0) -> ClosedTrade:
    pos = pos or position()
    return ClosedTrade(
        symbol=pos.symbol,
        direction=pos.direction,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        quantity=pos.quantity,
        pnl_usdt=pnl,
        reason=reason,
        entry_reason=pos.entry_reason,
        opened_at=pos.opened_at,
        closed_at=NOW,
        exchange_entry_order_id=pos.exchange_order_id,
        exchange_exit_order_id="2002",
    )


class Harness:
    def __init__(self, *, balance: float = 1500.0, mark_interval: float = 5.0) -> None:
        self.clock = Clock()
        self.bus = TelemetryBus()
        self.hub = TelemetryHub(META, self.bus, clock=self.clock)
        self.queue = self.bus.subscribe(maxsize=1000)
        self.observer = EngineTelemetry(
            self.hub,
            SYMBOLS,
            account_balance_usdt=balance,
            clock=self.clock,
            mark_interval_seconds=mark_interval,
        )

    def prices(self, btc: float = 100.0, eth: float = 2000.0) -> None:
        self.hub.record_price(price("BTC", btc))
        self.hub.record_price(price("ETH", eth))
        self.drain()

    def drain(self) -> list:
        out = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait())
        return out

    def latest(self, coin: str) -> dict:
        return self.hub.history_body()[coin][-1]


class DecidedTests(unittest.TestCase):
    def test_a_hold_tick_publishes_one_hold_block_per_traded_coin(self) -> None:
        h = Harness()
        h.prices()

        h.observer.decided(None, latency_ms=412)

        blocks = [m for m in h.drain() if isinstance(m, BlockEvent)]
        self.assertEqual([b.coin for b in blocks], ["BTC", "ETH"])
        btc = blocks[0]
        assert btc.decision is not None
        self.assertEqual((btc.decision.action, btc.decision.intent), ("hold", "hold"))
        self.assertEqual((btc.decision.probabilities.buy, btc.decision.probabilities.sell, btc.decision.probabilities.hold), (0.0, 0.0, 1.0))
        self.assertEqual((btc.decision.latency_ms, btc.decision.late), (412, False))
        self.assertEqual((btc.mid, btc.best_bid, btc.best_ask), (100.0, 99.5, 100.5))
        self.assertEqual(btc.position.side, "flat")
        self.assertEqual((btc.totals.blocks, btc.totals.decisions), (1, 1))
        self.assertEqual(blocks[1].mid, 2000.0)

    def test_a_coin_that_is_not_traded_never_gets_a_block(self) -> None:
        h = Harness()
        h.prices()

        h.observer.decided(None, latency_ms=1)

        self.assertEqual(h.hub.history_body()["SOL"], [])

    def test_an_open_plan_publishes_one_block_for_its_coin_with_the_real_confidence(self) -> None:
        h = Harness()
        h.prices()

        h.observer.decided(plan("ETH/USDT", PositionDirection.SHORT, leverage=50, confidence=0.83), latency_ms=900)

        [block] = [m for m in h.drain() if isinstance(m, BlockEvent)]
        decision = block.decision
        assert decision is not None
        self.assertEqual(block.coin, "ETH")
        self.assertEqual((decision.action, decision.intent, decision.bias), ("sell", "open", "short"))
        self.assertEqual((decision.leverage, decision.confidence), (50, 0.83))
        self.assertEqual(decision.reason, "ai_volume:OPEN_LONG")
        self.assertEqual((decision.probabilities.sell, decision.probabilities.hold), (1.0, 0.0))

    def test_a_long_plan_is_a_buy(self) -> None:
        h = Harness()
        h.prices()

        h.observer.decided(plan(), latency_ms=1)

        decision = h.latest("BTC")["decision"]
        self.assertEqual((decision["action"], decision["bias"]), ("buy", "long"))

    def test_an_overlong_reason_is_cut_and_a_confidence_outside_the_range_is_clamped(self) -> None:
        h = Harness()
        h.prices()

        h.observer.decided(plan(decision_reason="x" * 500, confidence=1.7), latency_ms=1)

        decision = h.latest("BTC")["decision"]
        self.assertEqual(len(decision["reason"]), 300)
        self.assertEqual(decision["confidence"], 1.0)

    def test_blocks_are_numbered_per_tick_and_totals_accumulate(self) -> None:
        h = Harness()
        h.prices()

        h.observer.decided(None, latency_ms=1)
        h.observer.decided(plan(), latency_ms=1)

        btc = h.hub.history_body()["BTC"]
        self.assertEqual([b["block"] for b in btc], [1, 2])
        self.assertEqual(btc[1]["totals"]["blocks"], 3)  # 2 hold blocks + 1 plan block
        self.assertEqual(btc[1]["totals"]["decisions"], 3)

    def test_nothing_is_published_while_no_price_is_known(self) -> None:
        h = Harness()

        h.observer.decided(None, latency_ms=1)

        self.assertEqual(h.drain(), [])
        self.assertEqual(h.hub.history_body()["BTC"], [])


class EntryTests(unittest.TestCase):
    def open_btc(self, h: Harness) -> None:
        h.prices()
        h.observer.decided(plan(), latency_ms=1)
        h.drain()

    def test_the_entry_quote_is_attached_to_the_decision_block(self) -> None:
        h = Harness()
        self.open_btc(h)

        h.observer.entry_quoted(plan(), price=100.2, size=0.5, order_id="1001")

        [message] = h.drain()
        self.assertEqual(message.event, "quote")
        self.assertEqual((message.coin, message.block), ("BTC", 1))
        quote = message.quote
        self.assertEqual((quote.side, quote.price, quote.size, quote.status, quote.order_id), ("buy", 100.2, 0.5, "placed", 1001))
        self.assertEqual(h.latest("BTC")["quote"]["orderId"], 1001)

    def test_a_non_numeric_order_id_is_sent_as_null(self) -> None:
        h = Harness()
        self.open_btc(h)

        h.observer.entry_quoted(plan(), price=100.0, size=0.5, order_id="abc-123")

        [message] = h.drain()
        self.assertIsNone(message.quote.order_id)

    def test_the_fill_marks_the_tape_and_the_block_shows_the_open_position(self) -> None:
        h = Harness()
        self.open_btc(h)

        h.observer.entry_filled(position(), leverage=100, fee_usdt=0.04)

        messages = h.drain()
        fill = next(m for m in messages if m.event == "fill")
        self.assertEqual((fill.coin, fill.block), ("BTC", 1))
        self.assertEqual((fill.fill.side, fill.fill.direction, fill.fill.price, fill.fill.size), ("buy", "open", 100.0, 0.5))
        self.assertEqual((fill.fill.fee_usd, fill.fill.simulated, fill.fill.order_id), (0.04, False, 1001))
        marks = [p for p in h.hub.tape_body()["BTC"] if p.get("fill")]
        self.assertEqual(len(marks), 1)
        block = h.latest("BTC")
        self.assertEqual(
            (block["position"]["side"], block["position"]["size"], block["position"]["entryPrice"], block["position"]["leverage"]),
            ("long", 0.5, 100.0, 100),
        )
        self.assertEqual(block["fill"]["dir"], "open")
        self.assertEqual((block["totals"]["fills"], block["totals"]["gasUsd"]), (1, 0.04))

    def test_a_short_entry_fills_as_a_sell(self) -> None:
        h = Harness()
        h.prices()
        h.observer.decided(plan("ETH/USDT", PositionDirection.SHORT), latency_ms=1)
        h.drain()

        h.observer.entry_filled(position("ETH/USDT", PositionDirection.SHORT, entry_price=2000.0), leverage=50, fee_usdt=0.0)

        fill = next(m for m in h.drain() if m.event == "fill")
        self.assertEqual(fill.fill.side, "sell")
        self.assertEqual(h.latest("ETH")["position"]["side"], "short")

    def test_a_fill_with_no_preceding_block_is_still_published_and_never_raises(self) -> None:
        h = Harness()

        h.observer.entry_filled(position(), leverage=100, fee_usdt=0.0)

        fills = [m for m in h.drain() if m.event == "fill"]
        self.assertEqual(len(fills), 1)


class MarkTests(unittest.TestCase):
    def opened(self, h: Harness) -> None:
        h.prices()
        h.observer.decided(plan(), latency_ms=1)
        h.observer.entry_filled(position(), leverage=100, fee_usdt=0.0)
        h.drain()

    def test_a_mark_updates_the_open_block_in_place_without_growing_the_history(self) -> None:
        h = Harness()
        self.opened(h)
        h.hub.record_price(price("BTC", 104.0))
        h.drain()
        h.clock.now += 6

        h.observer.position_marked(position(), net_usdt=1.75)

        [message] = h.drain()
        self.assertEqual((message.event, message.block, message.coin), ("block", 1, "BTC"))
        self.assertEqual(len(h.hub.history_body()["BTC"]), 1)
        block = h.latest("BTC")
        self.assertEqual(block["position"]["unrealizedUsd"], 1.75)
        self.assertEqual(block["totals"]["pnlUsd"], 1.75)
        self.assertEqual(block["mid"], 104.0)
        self.assertEqual(block["position"]["side"], "long")

    def test_marks_closer_together_than_the_interval_are_skipped(self) -> None:
        h = Harness(mark_interval=5.0)
        self.opened(h)
        h.clock.now += 6
        h.observer.position_marked(position(), net_usdt=1.0)
        h.drain()

        h.clock.now += 2
        h.observer.position_marked(position(), net_usdt=2.0)

        self.assertEqual(h.drain(), [])
        self.assertEqual(h.latest("BTC")["position"]["unrealizedUsd"], 1.0)

    def test_a_mark_for_a_position_the_observer_never_saw_open_is_ignored(self) -> None:
        h = Harness()
        h.prices()
        h.observer.decided(None, latency_ms=1)
        h.drain()

        h.observer.position_marked(position(), net_usdt=1.0)

        self.assertEqual(h.drain(), [])


class ResumeTests(unittest.TestCase):
    def test_a_resumed_position_is_shown_without_inventing_a_fill(self) -> None:
        h = Harness()
        h.prices()

        h.observer.position_resumed(position(), leverage=100)

        messages = h.drain()
        self.assertEqual([m.event for m in messages], ["block"])
        block = h.latest("BTC")
        self.assertEqual((block["position"]["side"], block["position"]["size"]), ("long", 0.5))
        self.assertIsNone(block["fill"])
        self.assertEqual([p for p in h.hub.tape_body()["BTC"] if p.get("fill")], [])

    def test_a_resumed_position_without_a_known_price_falls_back_to_its_entry_price(self) -> None:
        h = Harness()

        h.observer.position_resumed(position(entry_price=250.0), leverage=100)

        self.assertEqual(h.latest("BTC")["mid"], 250.0)


class CloseTests(unittest.TestCase):
    def test_a_close_publishes_a_close_block_a_fill_and_a_flat_position(self) -> None:
        h = Harness()
        h.prices(btc=110.0)
        h.observer.decided(plan(), latency_ms=1)
        h.observer.entry_filled(position(), leverage=100, fee_usdt=0.04)
        h.drain()

        h.observer.position_closed(trade(pnl=3.0, reason="take_profit"), fee_usdt=0.1)

        messages = h.drain()
        self.assertEqual([m.event for m in messages], ["block", "fill"])
        block, fill = messages
        decision = block.decision
        assert decision is not None
        self.assertEqual((decision.action, decision.intent, decision.bias), ("sell", "close", "long"))
        self.assertEqual(decision.reason, "take_profit")
        self.assertIsNone(decision.confidence)
        self.assertEqual(block.position.side, "flat")
        self.assertEqual(block.block, 2)
        self.assertEqual(fill.block, 2)
        self.assertEqual((fill.fill.side, fill.fill.direction, fill.fill.closed_pnl, fill.fill.fee_usd), ("sell", "close", 3.0, 0.1))
        self.assertEqual((fill.fill.price, fill.fill.size, fill.fill.order_id), (110.0, 0.5, 2002))
        totals = h.latest("BTC")["totals"]
        self.assertEqual((totals["realizedUsd"], totals["pnlUsd"], totals["fills"]), (3.0, 3.0, 2))
        self.assertAlmostEqual(totals["gasUsd"], 0.14)
        self.assertAlmostEqual(totals["pnlPct"], 0.2)  # 3.0 on a 1500 account

    def test_closing_a_short_buys_back(self) -> None:
        h = Harness()
        h.prices()
        short = position("ETH/USDT", PositionDirection.SHORT, entry_price=2000.0)
        h.observer.position_resumed(short, leverage=50)
        h.drain()

        h.observer.position_closed(trade(short, pnl=-1.0, reason="stop_loss", exit_price=2010.0), fee_usdt=0.1)

        block, fill = h.drain()
        self.assertEqual(block.decision.action, "buy")
        self.assertEqual(fill.fill.side, "buy")
        self.assertEqual(h.latest("ETH")["totals"]["realizedUsd"], -1.0)

    def test_a_close_is_published_even_when_no_price_was_ever_seen(self) -> None:
        h = Harness()
        h.observer.position_resumed(position(), leverage=100)
        h.drain()

        h.observer.position_closed(trade(exit_price=120.0), fee_usdt=0.1)

        block = h.latest("BTC")
        self.assertEqual((block["mid"], block["bestBid"], block["bestAsk"], block["spreadBps"]), (120.0, 120.0, 120.0, 0.0))

    def test_totals_carry_across_trades_and_coins(self) -> None:
        h = Harness()
        h.prices()
        h.observer.position_resumed(position(), leverage=100)
        h.observer.position_closed(trade(pnl=3.0), fee_usdt=0.1)
        eth = position("ETH/USDT", PositionDirection.SHORT, entry_price=2000.0)
        h.observer.position_resumed(eth, leverage=50)
        h.observer.position_closed(trade(eth, pnl=-1.0, exit_price=2010.0), fee_usdt=0.2)

        totals = h.latest("ETH")["totals"]

        self.assertAlmostEqual(totals["realizedUsd"], 2.0)
        self.assertAlmostEqual(totals["gasUsd"], 0.3)

    def test_a_zero_balance_never_divides_by_zero(self) -> None:
        h = Harness(balance=0.0)
        h.prices()
        h.observer.position_resumed(position(), leverage=100)

        h.observer.position_closed(trade(pnl=3.0), fee_usdt=0.1)

        self.assertEqual(h.latest("BTC")["totals"]["pnlPct"], 0.0)


class NeverRaisesTests(unittest.TestCase):
    def test_a_bug_inside_the_observer_is_logged_and_never_reaches_the_caller(self) -> None:
        h = Harness()
        h.prices()
        h.observer._hub = None  # type: ignore[assignment]  # every hook reaches for the hub
        calls = (
            lambda: h.observer.decided(None, latency_ms=1),
            lambda: h.observer.entry_quoted(plan(), price=1.0, size=1.0, order_id="1"),
            lambda: h.observer.entry_filled(position(), leverage=1, fee_usdt=0.0),
            lambda: h.observer.position_resumed(position(), leverage=1),
            lambda: h.observer.position_closed(trade(), fee_usdt=0.0),
        )

        for call in calls:
            with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
                call()

    def test_a_failing_hub_is_contained_by_the_hub(self) -> None:
        h = Harness()
        h.prices()
        h.hub._history["BTC"] = None  # type: ignore[assignment]

        with self.assertLogs(LOGGER_NAME, level=logging.WARNING):
            h.observer.decided(plan(), latency_ms=1)


class NullObserverTests(unittest.TestCase):
    def test_the_null_observer_offers_every_hook_and_does_nothing(self) -> None:
        hooks = ("decided", "entry_quoted", "entry_filled", "position_resumed", "position_marked", "position_closed")

        for name in hooks:
            self.assertTrue(callable(getattr(NULL_OBSERVER, name)), name)
            self.assertTrue(callable(getattr(EngineTelemetry, name)), name)
        NULL_OBSERVER.decided(None, latency_ms=1)
        NULL_OBSERVER.entry_quoted(plan(), price=1.0, size=1.0, order_id="1")
        NULL_OBSERVER.entry_filled(position(), leverage=1, fee_usdt=0.0)
        NULL_OBSERVER.position_resumed(position(), leverage=1)
        NULL_OBSERVER.position_marked(position(), net_usdt=0.0)
        NULL_OBSERVER.position_closed(trade(), fee_usdt=0.0)


if __name__ == "__main__":
    unittest.main()
