"""TELEMETRY_SYMBOLS: extra symbols the dashboard shows without the bot ever trading them."""

from __future__ import annotations

import asyncio
import logging
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bot.ai.brain import EntryDecision, RiskReview
from bot.ai.orchestration import AIOrchestrator
from bot.config import Settings
from bot.intelligence.market_intelligence import MarketIntelligence
from bot.strategy.volume_strategy import VolumeStrategy
from bot.telemetry.book_ticker import BookTick
from bot.telemetry.runtime import build_meta, build_telemetry
from bot.telemetry.wire import PriceMessage

LOGGER = logging.getLogger("volume_bot.test_display_symbols")
NOW_MS = 1_791_661_170_000
SRC = Path(__file__).resolve().parent.parent / "src"


def settings(**env: object) -> Settings:
    return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", STRATEGY_MODE="volume", **env)


def display_eth(**env: object) -> Settings:
    return settings(SYMBOLS="BTC/USDT", TELEMETRY_SYMBOLS="BTC/USDT,ETH/USDT", **env)


class UnusedClient:
    """Building the runtime must not touch the exchange."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"client.{name} used while building telemetry")


class RecordingMarketClient:
    """Only the two public reads the poller is allowed; records which symbols it asked for."""

    def __init__(self) -> None:
        self.books: list[str] = []
        self.klines: list[str] = []

    async def get_order_book(self, symbol: str, depth: int = 5) -> SimpleNamespace:
        self.books.append(symbol)
        return SimpleNamespace(mid_price=100.0, best_bid=99.5, best_ask=100.5, spread_bps=1.0)

    async def get_klines(self, symbol: str, timeframe: str, limit: int = 100) -> list[dict[str, float]]:
        self.klines.append(symbol)
        return []


class DisplaySymbolsConfigTests(unittest.TestCase):
    def test_unset_means_the_dashboard_shows_exactly_the_traded_symbols(self) -> None:
        s = settings(SYMBOLS="BTC/USDT")

        self.assertEqual(s.telemetry_symbols, ())
        self.assertEqual(s.telemetry_display_symbols, ("BTC/USDT",))

    def test_an_empty_value_is_the_same_as_unset(self) -> None:
        self.assertEqual(settings(SYMBOLS="BTC/USDT", TELEMETRY_SYMBOLS="").telemetry_display_symbols, ("BTC/USDT",))

    def test_entries_are_parsed_like_symbols(self) -> None:
        s = settings(SYMBOLS="BTC/USDT", TELEMETRY_SYMBOLS=" eth-usdt , ,sol/usdt ")

        self.assertEqual(s.telemetry_symbols, ("ETH/USDT", "SOL/USDT"))

    def test_traded_symbols_come_first_and_duplicates_are_dropped(self) -> None:
        s = settings(SYMBOLS="BTC/USDT", TELEMETRY_SYMBOLS="ETH/USDT,BTC/USDT,ETH-USDT")

        self.assertEqual(s.telemetry_display_symbols, ("BTC/USDT", "ETH/USDT"))

    def test_a_traded_symbol_never_disappears_from_the_dashboard(self) -> None:
        s = settings(SYMBOLS="BTC/USDT,ETH/USDT", TELEMETRY_SYMBOLS="SOL/USDT")

        self.assertEqual(s.telemetry_display_symbols, ("BTC/USDT", "ETH/USDT", "SOL/USDT"))

    def test_display_symbols_never_change_the_traded_symbols(self) -> None:
        self.assertEqual(display_eth().symbols, ("BTC/USDT",))


class DisplaySymbolsRuntimeTests(unittest.TestCase):
    def test_the_meta_lists_a_sleeve_for_every_display_symbol_with_the_traded_one_first(self) -> None:
        meta = build_meta(display_eth(), now_ms=NOW_MS)

        self.assertEqual([(s.coin, s.pair) for s in meta.sleeves], [("BTC", "BTC-USDT"), ("ETH", "ETH-USDT")])
        self.assertEqual((meta.coin, meta.pair, meta.market), ("BTC", "BTC-USDT", "BTC-USDT"))

    def test_the_hub_keeps_history_and_tape_for_the_display_symbol(self) -> None:
        runtime = build_telemetry(display_eth(TELEMETRY_ENABLED="true"), UnusedClient(), LOGGER)  # type: ignore[arg-type]

        assert runtime is not None
        self.assertEqual(set(runtime.hub.history_body()), {"BTC", "ETH"})
        self.assertEqual(set(runtime.hub.tape_body()), {"BTC", "ETH"})

    def test_the_poller_reads_prices_and_candles_for_the_display_symbol(self) -> None:
        client = RecordingMarketClient()
        runtime = build_telemetry(display_eth(TELEMETRY_ENABLED="true"), client, LOGGER)  # type: ignore[arg-type]
        assert runtime is not None

        asyncio.run(runtime._poller.poll_once())

        self.assertEqual(client.books, ["BTC/USDT", "ETH/USDT"])
        self.assertIn("ETH/USDT", client.klines)
        self.assertEqual(runtime.hub.latest_price("ETH").mid, 100.0)  # type: ignore[union-attr]

    def test_the_price_stream_subscribes_to_the_display_pairs(self) -> None:
        with mock.patch("bot.telemetry.runtime.BookTickerFeed") as feed_class:
            runtime = build_telemetry(
                display_eth(TELEMETRY_ENABLED="true", TELEMETRY_PRICE_STREAM="true"),
                UnusedClient(),  # type: ignore[arg-type]
                LOGGER,
            )

        assert runtime is not None
        pairs, on_tick = feed_class.call_args.args
        self.assertEqual(list(pairs), ["BTC-USDT", "ETH-USDT"])
        on_tick(BookTick("ETH-USDT", 99.0, 101.0))
        self.assertEqual(runtime.hub.latest_price("ETH").mid, 100.0)  # type: ignore[union-attr]

    def test_an_ai_hold_is_shown_only_on_traded_symbols(self) -> None:
        """The model never looked at a display-only symbol, so it must not appear to have held it.

        It also keeps the header honest: the dashboard sums `totals.realizedUsd` over every coin's
        latest block, and each block carries the bot-wide total, so a block on ETH would double it.
        """
        runtime = build_telemetry(display_eth(TELEMETRY_ENABLED="true"), UnusedClient(), LOGGER)  # type: ignore[arg-type]
        assert runtime is not None
        for coin in ("BTC", "ETH"):
            runtime.hub.record_price(
                PriceMessage(coin=coin, ts=NOW_MS, mid=100.0, best_bid=99.5, best_ask=100.5, spread_bps=1.0)
            )

        runtime.observer.decided(None, latency_ms=1)

        history = runtime.hub.history_body()
        self.assertEqual(len(history["BTC"]), 1)
        self.assertEqual(history["ETH"], [])


class TradingPathIgnoresDisplaySymbolsTests(unittest.TestCase):
    """Adding ETH to TELEMETRY_SYMBOLS must not let any trading component see ETH."""

    def test_the_ai_rejects_a_display_only_symbol_as_unsupported(self) -> None:
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = display_eth()
        orchestrator._logger = logging.getLogger("test.display_symbols.orchestrator")
        decision = EntryDecision(
            symbol="ETH/USDT",
            action="OPEN_LONG",
            confidence=0.95,
            market_trend="Bullish",
            stop_loss_pct=0.2,
            take_profit_pct=0.3,
            risk_reward_ratio=1.5,
            systemic_risk=2,
            reason="display-only symbol",
            risk_review=RiskReview(approved=True, confirmations="ok", invalidation_trigger="trend breaks"),
        )

        with self.assertLogs("test.display_symbols.orchestrator", level=logging.WARNING) as logs:
            self.assertIsNone(orchestrator._build_entry_candidate(decision, {}))
        self.assertIn("unsupported symbol ETH/USDT", logs.output[0])

    def test_market_intelligence_builds_context_for_traded_symbols_only(self) -> None:
        intelligence = object.__new__(MarketIntelligence)
        intelligence._settings = display_eth()
        seen: list[str] = []

        async def features(symbol: str) -> None:
            seen.append(symbol)
            return None

        intelligence._build_symbol_features = features  # type: ignore[method-assign]
        try:
            asyncio.run(intelligence.build_context())
        except Exception:  # only the symbol list matters here; the rest of the context is not stubbed
            pass

        self.assertEqual(seen, ["BTC/USDT"])

    def test_the_volume_strategy_rotates_through_traded_symbols_only(self) -> None:
        strategy = VolumeStrategy(display_eth())

        self.assertEqual({strategy.next_trade().symbol for _ in range(6)}, {"BTC/USDT"})

    def test_no_trading_module_reads_the_display_list(self) -> None:
        pattern = re.compile(r"telemetry_(display_)?symbols")
        offenders = [
            str(path.relative_to(SRC))
            for path in [*SRC.joinpath("bot").rglob("*.py"), SRC / "main.py"]
            if pattern.search(path.read_text())
            and path.relative_to(SRC).parts[:2] != ("bot", "telemetry")
            and path != SRC / "bot" / "config.py"
        ]

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
