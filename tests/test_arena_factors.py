from __future__ import annotations

import unittest
from datetime import datetime, timezone

from bot.arena.factors import FactorEffectivenessAnalyzer, FactorRegistry
from bot.data.market_signals import TechnicalFeatures


class ArenaFactorTests(unittest.TestCase):
    def test_registry_scores_core_factor_categories(self) -> None:
        features = TechnicalFeatures(
            symbol="BTC/USDT",
            last_price=100_000,
            ema_fast=100_600,
            ema_slow=99_800,
            ema_21=99_800,
            rsi_14=63,
            atr_14=420,
            macd_line=120,
            macd_signal=80,
            macd_hist=0.72,
            change_pct_1=0.22,
            change_pct_5=1.4,
            volume=1250,
            volume_change_pct=86,
            funding_rate=0.0008,
            open_interest=2_500_000,
            open_interest_change_pct=9.5,
            spread_bps=2.1,
            bid_depth_notional=220_000,
            ask_depth_notional=210_000,
        )

        snapshots = FactorRegistry().score_symbol(features)

        categories = {item.category for item in snapshots}
        self.assertEqual(
            categories,
            {"momentum", "trend", "volatility", "volume", "microstructure"},
        )
        self.assertTrue(all(-1.0 <= item.score <= 1.0 for item in snapshots))
        self.assertGreater(
            next(item.score for item in snapshots if item.name == "price_momentum_5m"),
            0,
        )
        self.assertEqual(
            next(item.direction for item in snapshots if item.name == "ema_macd_trend"),
            "LONG",
        )

    def test_effectiveness_groups_trades_by_factor_metadata(self) -> None:
        records = [
            {
                "symbol": "BTC/USDT",
                "pnl_usdt": 4.5,
                "closed_at": datetime(2026, 6, 13, 1, tzinfo=timezone.utc).isoformat(),
                "factor_metadata": {"top_factors": ["ema_macd_trend", "volume_participation"]},
                "entry_price": 100,
                "exit_price": 102,
                "direction": "LONG",
            },
            {
                "symbol": "BTC/USDT",
                "pnl_usdt": -1.5,
                "closed_at": datetime(2026, 6, 13, 2, tzinfo=timezone.utc).isoformat(),
                "factor_metadata": {"top_factors": ["ema_macd_trend"]},
                "entry_price": 100,
                "exit_price": 99,
                "direction": "LONG",
            },
        ]

        result = FactorEffectivenessAnalyzer().summarize(records)

        trend = result["ema_macd_trend"]
        self.assertEqual(trend.trade_count, 2)
        self.assertEqual(trend.win_rate_pct, 50.0)
        self.assertAlmostEqual(trend.average_pnl_usdt, 1.5)
        self.assertGreater(trend.forward_return_correlation, 0)


if __name__ == "__main__":
    unittest.main()
