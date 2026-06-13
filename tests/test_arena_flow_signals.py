from __future__ import annotations

import unittest

from bot.arena.flow_signals import MarketFlowSignalDetector, SignalRuleConfig
from bot.arena.factors import FactorSnapshot
from bot.data.market_signals import TechnicalFeatures


class ArenaFlowSignalTests(unittest.TestCase):
    def test_detector_emits_edge_trigger_once_until_condition_clears(self) -> None:
        detector = MarketFlowSignalDetector(
            SignalRuleConfig(
                volume_spike_pct=50,
                price_momentum_bps=40,
                open_interest_surge_pct=5,
                factor_score_trigger=0.7,
            )
        )
        features = self._features(volume_change_pct=72, change_pct_5=0.62, open_interest_change_pct=8)
        factors = [
            FactorSnapshot(
                symbol="BTC/USDT",
                name="ema_macd_trend",
                category="trend",
                score=0.84,
                direction="LONG",
                value=0.84,
                reason="bullish trend",
            )
        ]

        first = detector.evaluate_symbol(features, factors)
        second = detector.evaluate_symbol(features, factors)
        cleared = detector.evaluate_symbol(self._features(volume_change_pct=0, change_pct_5=0, open_interest_change_pct=0), [])
        third = detector.evaluate_symbol(features, factors)

        self.assertIn("volume_spike", {item.name for item in first.new_triggers})
        self.assertIn("price_momentum", {item.name for item in first.new_triggers})
        self.assertIn("open_interest_surge", {item.name for item in first.new_triggers})
        self.assertIn("factor_score_breakout", {item.name for item in first.new_triggers})
        self.assertEqual(second.new_triggers, [])
        self.assertEqual(cleared.active, [])
        self.assertIn("volume_spike", {item.name for item in third.new_triggers})

    @staticmethod
    def _features(
        *,
        volume_change_pct: float,
        change_pct_5: float,
        open_interest_change_pct: float,
    ) -> TechnicalFeatures:
        return TechnicalFeatures(
            symbol="BTC/USDT",
            last_price=100_000,
            ema_fast=100_100,
            ema_slow=100_000,
            ema_21=100_000,
            rsi_14=55,
            atr_14=300,
            macd_line=5,
            macd_signal=4,
            macd_hist=0.2,
            change_pct_1=0.12,
            change_pct_5=change_pct_5,
            volume=1000,
            volume_change_pct=volume_change_pct,
            funding_rate=0.0002,
            open_interest=1_000_000,
            open_interest_change_pct=open_interest_change_pct,
            spread_bps=2.0,
            bid_depth_notional=100_000,
            ask_depth_notional=110_000,
        )


if __name__ == "__main__":
    unittest.main()
