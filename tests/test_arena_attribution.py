from __future__ import annotations

import unittest
from datetime import datetime, timezone

from bot.arena.attribution import AttributionAnalyzer, DiagnosisGenerator


class ArenaAttributionTests(unittest.TestCase):
    def test_summary_groups_by_symbol_factor_trigger_hour_exit_and_confidence(self) -> None:
        records = [
            {
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "pnl_usdt": 3,
                "reason": "neutral_exit",
                "closed_at": datetime(2026, 6, 13, 9, tzinfo=timezone.utc).isoformat(),
                "confidence": 0.83,
                "factor_metadata": {"top_factors": ["ema_macd_trend"]},
                "trigger_metadata": {"trigger_names": ["volume_spike"]},
            },
            {
                "symbol": "ETH/USDT",
                "direction": "SHORT",
                "pnl_usdt": -2,
                "reason": "stop_loss",
                "closed_at": datetime(2026, 6, 13, 9, tzinfo=timezone.utc).isoformat(),
                "confidence": 0.62,
                "factor_metadata": {"top_factors": ["funding_pressure"]},
                "trigger_metadata": {"trigger_names": ["funding_extreme"]},
            },
        ]

        summary = AttributionAnalyzer().summarize(records)

        self.assertEqual(summary.total_trades, 2)
        self.assertEqual(summary.by_symbol["BTC/USDT"].wins, 1)
        self.assertEqual(summary.by_factor["ema_macd_trend"].total_pnl_usdt, 3)
        self.assertEqual(summary.by_trigger["funding_extreme"].losses, 1)
        self.assertEqual(summary.by_hour["09"].trade_count, 2)
        self.assertEqual(summary.by_confidence_band["0.80-1.00"].wins, 1)

    def test_diagnosis_uses_real_metrics(self) -> None:
        summary = AttributionAnalyzer().summarize(
            [
                {
                    "symbol": "ETH/USDT",
                    "direction": "SHORT",
                    "pnl_usdt": -2,
                    "reason": "stop_loss",
                    "closed_at": datetime(2026, 6, 13, 9, tzinfo=timezone.utc).isoformat(),
                    "confidence": 0.62,
                    "factor_metadata": {"top_factors": ["funding_pressure"]},
                    "trigger_metadata": {"trigger_names": ["funding_extreme"]},
                }
            ]
        )

        cards = DiagnosisGenerator().generate(summary)

        self.assertTrue(cards)
        self.assertIn("ETH/USDT", cards[0].description)
        self.assertEqual(cards[0].severity, "high")


if __name__ == "__main__":
    unittest.main()
