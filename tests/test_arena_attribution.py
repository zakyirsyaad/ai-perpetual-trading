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


class SetupAttributionTests(unittest.TestCase):
    """Bucket closed trades by entry setup so a repeated losing shape is visible.

    Without this the three LONG entries at 78,293 / 78,309 / 78,301 that were
    each stopped out land in the same by_direction and by_symbol buckets as every
    other trade, so nothing in the feedback loop can single them out.
    """

    def _record(self, setup_key: str, pnl: float) -> dict:
        return {
            "symbol": "BTC/USDT",
            "direction": "LONG",
            "pnl_usdt": pnl,
            "reason": "stop_loss",
            "factor_metadata": {"top_factors": ["ema_gap"], "setup": {"setup_key": setup_key}},
        }

    def test_setup_bucket_aggregates_pnl_and_win_rate(self) -> None:
        summary = AttributionAnalyzer().summarize([
            self._record("LONG|stretch_up|atr_mid", -99.46),
            self._record("LONG|stretch_up|atr_mid", -99.28),
            self._record("LONG|stretch_up|atr_mid", -104.01),
            self._record("LONG|stretch_flat|atr_mid", +40.78),
        ])
        losing = summary.by_setup["LONG|stretch_up|atr_mid"]
        self.assertEqual(losing.trade_count, 3)
        self.assertAlmostEqual(losing.total_pnl_usdt, -302.75, places=2)
        self.assertEqual(losing.win_rate_pct, 0.0)
        self.assertEqual(summary.by_setup["LONG|stretch_flat|atr_mid"].trade_count, 1)

    def test_records_without_a_setup_profile_are_skipped_not_crashed(self) -> None:
        summary = AttributionAnalyzer().summarize([
            {"symbol": "BTC/USDT", "direction": "LONG", "pnl_usdt": -1.0, "reason": "stop_loss"},
            {"symbol": "BTC/USDT", "direction": "LONG", "pnl_usdt": -1.0, "reason": "stop_loss", "factor_metadata": "not-a-dict"},
        ])
        self.assertEqual(summary.by_setup, {})

    def test_setup_bucket_is_exposed_in_the_serialised_summary(self) -> None:
        summary = AttributionAnalyzer().summarize([self._record("LONG|stretch_up|atr_high", -50.0)])
        self.assertIn("by_setup", summary.to_dict())


class SetupDiagnosisTests(unittest.TestCase):
    """Surface a repeatedly losing setup as a diagnosis card.

    Instrumentation alone only produces JSON. The card is what makes a losing
    entry shape legible before it has enough sample to become a hard gate.
    """

    def _records(self, setup_key: str, pnl: float, count: int) -> list[dict]:
        return [
            {
                "symbol": "BTC/USDT",
                "direction": "LONG",
                "pnl_usdt": pnl,
                "reason": "stop_loss",
                "factor_metadata": {"setup": {"setup_key": setup_key}},
            }
            for _ in range(count)
        ]

    def _setup_cards(self, records: list[dict]) -> list:
        summary = AttributionAnalyzer().summarize(records)
        return [c for c in DiagnosisGenerator().generate(summary) if c.title.startswith("Losing entry setup")]

    def test_repeated_losing_setup_is_flagged(self) -> None:
        cards = self._setup_cards(self._records("LONG|stretch_up|atr_mid", -100.0, 5))
        self.assertEqual(len(cards), 1)
        self.assertIn("LONG|stretch_up|atr_mid", cards[0].title)

    def test_small_sample_is_not_flagged_as_a_problem(self) -> None:
        # Two losses is noise, not a pattern; flagging it would invite acting on nothing.
        self.assertEqual(self._setup_cards(self._records("LONG|stretch_up|atr_mid", -100.0, 2)), [])

    def test_profitable_setup_is_not_flagged(self) -> None:
        self.assertEqual(self._setup_cards(self._records("LONG|stretch_flat|atr_mid", +40.0, 8)), [])
