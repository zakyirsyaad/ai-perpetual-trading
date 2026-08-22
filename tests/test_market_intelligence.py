from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace

from bot.data.market_signals import ExternalMarketSignals
from bot.exchange.contracts import OrderBookSnapshot
from bot.intelligence.market_intelligence import MarketIntelligence


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        symbols=("BTC/USDT",),
        ai_kline_interval="3m",
        ai_kline_limit=60,
        orderbook_depth_levels=20,
        stop_loss_pct=0.1,
        arena_factors_enabled=True,
        arena_flow_signals_enabled=True,
        arena_volume_spike_pct=65.0,
        arena_volume_collapse_pct=-70.0,
        arena_price_momentum_bps=55.0,
        arena_open_interest_surge_pct=7.5,
        arena_funding_extreme_abs=0.001,
        arena_factor_score_trigger=0.75,
        arena_factor_percentile_trigger=0.9,
        arena_order_flow_imbalance_pct=25.0,
        arena_min_depth_notional=25_000.0,
        max_spread_bps=6.0,
        dune_query_ids=(),
        tradingview_signal_url=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeClient:
    venue = "bingx"

    def __init__(self, *, klines=None, fail: set[str] | None = None) -> None:
        self._klines = klines
        self._fail = fail or set()
        self.open_interest = 1000.0

    async def get_klines(self, symbol, interval="1m", limit=120):
        if "klines" in self._fail:
            return []
        if self._klines is not None:
            return self._klines
        return [
            {
                "timestamp": float(index * 180_000),
                "open": 100.0 + index,
                "high": 101.0 + index,
                "low": 99.0 + index,
                "close": 100.5 + index,
                "volume": 1000.0 + index * 10,
            }
            for index in range(60)
        ]

    async def get_funding_rate(self, symbol):
        if "funding" in self._fail:
            raise RuntimeError("funding down")
        return 0.0001

    async def get_open_interest(self, symbol):
        if "oi" in self._fail:
            raise RuntimeError("oi down")
        return self.open_interest

    async def get_order_book(self, symbol, depth=20):
        if "book" in self._fail:
            raise RuntimeError("book down")
        return OrderBookSnapshot(
            symbol=symbol,
            best_bid=100.0,
            best_ask=100.01,
            mid_price=100.005,
            spread_bps=1.0,
            bid_depth_notional=500_000.0,
            ask_depth_notional=400_000.0,
            timestamp=None,
            raw={},
        )


class _FakeSignals:
    def __init__(self, external: ExternalMarketSignals | None = None) -> None:
        self._external = external or ExternalMarketSignals()
        self.closed = False

    async def fetch(self) -> ExternalMarketSignals:
        return self._external

    async def close(self) -> None:
        self.closed = True


def _intelligence(client=None, signals=None, settings=None) -> MarketIntelligence:
    return MarketIntelligence(
        settings or _settings(),
        client or _FakeClient(),
        signals or _FakeSignals(),
        logging.getLogger("test.intel"),
    )


class MarketIntelligenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_contains_technicals_factors_and_flow(self) -> None:
        context = await _intelligence().build_context()
        payload = context.to_dict()
        self.assertEqual(len(payload["technicals"]), 1)
        self.assertTrue(payload["factor_snapshots"])
        self.assertIn("factor_intelligence", payload["intelligence"])
        self.assertIn("market_flow_signals", payload["intelligence"])
        self.assertIn("regime", payload["intelligence"])

    async def test_symbol_scores_are_computed(self) -> None:
        context = await _intelligence().build_context()
        scores = context.intelligence["symbol_scores"]["BTC/USDT"]
        self.assertIn("composite_score", scores)
        self.assertIn(scores["trend_bias"], {"bullish", "bearish", "mixed"})

    async def test_missing_candles_produce_a_risk_flag(self) -> None:
        context = await _intelligence(_FakeClient(fail={"klines"})).build_context()
        self.assertIn("missing_exchange_market_data", context.intelligence["risk_flags"])
        self.assertEqual(context.technicals, [])

    async def test_provider_failures_degrade_gracefully(self) -> None:
        context = await _intelligence(_FakeClient(fail={"funding", "oi", "book"})).build_context()
        features = context.technicals[0]
        self.assertEqual(features.funding_rate, 0.0)
        self.assertEqual(features.open_interest, 0.0)
        self.assertIsNone(features.spread_bps)

    async def test_open_interest_change_is_tracked_between_ticks(self) -> None:
        client = _FakeClient()
        intelligence = _intelligence(client)
        await intelligence.build_context()
        client.open_interest = 1100.0
        context = await intelligence.build_context()
        self.assertAlmostEqual(context.technicals[0].open_interest_change_pct, 10.0, places=6)

    async def test_arena_features_can_be_disabled(self) -> None:
        settings = _settings(arena_factors_enabled=False, arena_flow_signals_enabled=False)
        context = await _intelligence(settings=settings).build_context()
        self.assertEqual(context.factor_snapshots, [])
        self.assertEqual(context.active_flow_signals, [])

    async def test_missing_coinmarketcap_is_flagged(self) -> None:
        context = await _intelligence().build_context()
        self.assertIn("coinmarketcap_unavailable", context.intelligence["risk_flags"])

    async def test_dune_unavailable_flag_requires_configured_queries(self) -> None:
        settings = _settings(dune_query_ids=("1",))
        context = await _intelligence(settings=settings).build_context()
        self.assertIn("dune_unavailable", context.intelligence["risk_flags"])

    async def test_tradingview_flag_only_after_the_feed_has_ever_worked(self) -> None:
        settings = _settings(tradingview_signal_url="http://relay")
        signals = _FakeSignals(ExternalMarketSignals(tradingview={"summary": "BUY"}))
        intelligence = _intelligence(signals=signals, settings=settings)
        first = await intelligence.build_context()
        self.assertNotIn("tradingview_signal_unavailable", first.intelligence["risk_flags"])

        signals._external = ExternalMarketSignals()
        second = await intelligence.build_context()
        self.assertIn("tradingview_signal_unavailable", second.intelligence["risk_flags"])

    async def test_never_seen_tradingview_feed_is_not_flagged(self) -> None:
        settings = _settings(tradingview_signal_url="http://relay")
        context = await _intelligence(settings=settings).build_context()
        self.assertNotIn("tradingview_signal_unavailable", context.intelligence["risk_flags"])

    async def test_explicit_symbol_subset_is_honoured(self) -> None:
        context = await _intelligence().build_context(symbols=("BTC/USDT",))
        self.assertEqual([item.symbol for item in context.technicals], ["BTC/USDT"])

    async def test_close_releases_the_signal_aggregator(self) -> None:
        signals = _FakeSignals()
        await _intelligence(signals=signals).close()
        self.assertTrue(signals.closed)


if __name__ == "__main__":
    unittest.main()
