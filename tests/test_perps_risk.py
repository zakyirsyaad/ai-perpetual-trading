from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from bot.perps.config import PerpsConfig
from bot.perps.exchange_adapter import HealthStatus, InstrumentMetadata, MarketSnapshot
from bot.perps.risk import EntryRiskContext, PerpsRiskEngine, RiskLedger
from bot.perps.signal import NormalizedSignal


class PerpsRiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PerpsConfig()
        self.risk = PerpsRiskEngine(self.config)
        self.instrument = InstrumentMetadata(
            symbol="BTC/USDT",
            base_asset="BTC",
            quote_asset="USDT",
            price_tick=Decimal("0.1"),
            qty_step=Decimal("0.00001"),
            min_qty=Decimal("0.00001"),
            min_notional=Decimal("5"),
            max_leverage=5,
        )
        self.snapshot = MarketSnapshot(
            symbol="BTC/USDT",
            bid=100_000,
            ask=100_010,
            last=100_005,
            mark=100_005,
            timestamp=datetime.now(timezone.utc),
        )

    def test_sizing_uses_fee_budget_risk_and_exchange_rounding(self) -> None:
        signal = self._signal(expected_move_bps=85)
        sizing = self.risk.size_order(
            signal=signal,
            instrument=self.instrument,
            snapshot=self.snapshot,
            equity_usdt=Decimal("60"),
            requested_leverage=3,
        )

        self.assertTrue(sizing.accepted, sizing.rejected_reason)
        self.assertAlmostEqual(float(sizing.n_fee), 18.0, places=6)
        self.assertAlmostEqual(float(sizing.n_risk), 43.2692307, places=5)
        self.assertLessEqual(sizing.n_final, sizing.n_fee)
        self.assertEqual(sizing.leverage, 3)
        self.assertGreater(sizing.projected_fee, Decimal("0"))
        self.assertGreater(sizing.projected_loss_at_stop, sizing.projected_fee)
        self.assertGreater(sizing.projected_net_profit_at_target, Decimal("0"))

    def test_sizing_rejects_move_that_cannot_clear_costs(self) -> None:
        sizing = self.risk.size_order(
            signal=self._signal(expected_move_bps=50),
            instrument=self.instrument,
            snapshot=self.snapshot,
            equity_usdt=Decimal("60"),
            requested_leverage=3,
        )

        self.assertFalse(sizing.accepted)
        self.assertIn("expected_move_bps", sizing.rejected_reason or "")

    def test_volume_sizing_uses_configured_margin_per_entry(self) -> None:
        config = PerpsConfig(
            starting_equity_usdt=1500,
            target_trades_per_day=16,
            sizing_mode="volume",
            target_margin_per_entry_usdt=180,
            default_leverage=100,
            leverage_cap=100,
            hard_max_leverage=100,
            venue_max_leverage=100,
            min_liquidation_distance_bps=10,
        )
        risk = PerpsRiskEngine(config)
        instrument = InstrumentMetadata(
            symbol="BTC/USDT",
            base_asset="BTC",
            quote_asset="USDT",
            price_tick=Decimal("0.1"),
            qty_step=Decimal("0.00001"),
            min_qty=Decimal("0.00001"),
            min_notional=Decimal("5"),
            max_leverage=100,
        )

        sizing = risk.size_order(
            signal=self._signal(expected_move_bps=1, stop_bps=5, take_bps=8),
            instrument=instrument,
            snapshot=self.snapshot,
            equity_usdt=Decimal("1500"),
            requested_leverage=100,
        )

        self.assertTrue(sizing.accepted, sizing.rejected_reason)
        self.assertEqual(sizing.leverage, 100)
        self.assertEqual(sizing.n_margin, Decimal("18000"))
        self.assertAlmostEqual(float(sizing.margin_used), 180.0, places=2)
        self.assertAlmostEqual(float(sizing.projected_round_trip_volume), 36_000.0, delta=2.0)

    def test_kill_switches_block_entries(self) -> None:
        signal = self._signal(expected_move_bps=85)
        result = self.risk.evaluate_entry(
            EntryRiskContext(
                signal=signal,
                snapshot=self.snapshot,
                health=HealthStatus(websocket_healthy=False, rate_limit_remaining_pct=0.02),
                ledger=RiskLedger(
                    equity_usdt=Decimal("60"),
                    realized_pnl_today=Decimal("-2"),
                    rolling_pnl=Decimal("-4"),
                    consecutive_losses=3,
                    fee_spent_today=Decimal("2"),
                ),
            )
        )

        self.assertFalse(result.allowed)
        self.assertIn("websocket_unhealthy", result.reasons)
        self.assertIn("daily_realized_drawdown_breach", result.reasons)
        self.assertIn("rolling_drawdown_breach", result.reasons)
        self.assertIn("too_many_consecutive_losses", result.reasons)
        self.assertIn("fee_budget_breach", result.reasons)

    @staticmethod
    def _signal(expected_move_bps: float, stop_bps: float = 50, take_bps: float = 95) -> NormalizedSignal:
        return NormalizedSignal.model_validate(
            {
                "symbol": "BTC/USDT",
                "timeframe": "1m",
                "side": "long",
                "confidence": 0.75,
                "expected_move_bps": expected_move_bps,
                "stop_bps": stop_bps,
                "take_bps": take_bps,
                "max_hold_minutes": 12,
                "rationale_codes": ["trend:bullish"],
                "invalidation_reason": "breaks support",
                "model_latency_ms": 500,
            }
        )


if __name__ == "__main__":
    unittest.main()
