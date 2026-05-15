from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from bot.perps.backtester import BacktestCandle, BacktestSignal, ConservativePerpsBacktester
from bot.perps.config import PerpsConfig
from bot.perps.signal import NormalizedSignal


class PerpsBacktesterTests(unittest.TestCase):
    def test_backtester_includes_costs_and_metrics(self) -> None:
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        candles = [
            BacktestCandle(t0, "BTC/USDT", "1m", 100_000, 100_100, 99_900, 100_000, 10),
            BacktestCandle(t0 + timedelta(minutes=1), "BTC/USDT", "1m", 100_000, 101_100, 99_950, 100_900, 11),
            BacktestCandle(t0 + timedelta(minutes=2), "BTC/USDT", "1m", 100_900, 101_200, 100_800, 101_000, 12),
        ]
        signal = NormalizedSignal.model_validate(
            {
                "symbol": "BTC/USDT",
                "timeframe": "1m",
                "side": "long",
                "confidence": 0.8,
                "expected_move_bps": 95,
                "stop_bps": 50,
                "take_bps": 90,
                "max_hold_minutes": 5,
                "rationale_codes": ["trend:bullish"],
                "invalidation_reason": "breaks support",
                "model_latency_ms": 123,
            }
        )

        metrics = ConservativePerpsBacktester(PerpsConfig()).run(
            candles,
            [BacktestSignal(timestamp=t0, signal=signal)],
        )

        self.assertEqual(metrics.total_trades, 1)
        self.assertGreater(metrics.trades[0].open_fee, 0)
        self.assertGreater(metrics.trades[0].close_fee, 0)
        self.assertIn("BTC/USDT:1m", metrics.pnl_by_symbol_timeframe)
        self.assertGreaterEqual(metrics.slippage_distribution_bps["avg"], 0)

    def test_parameter_sweep_hook(self) -> None:
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        candles = [
            BacktestCandle(t0, "ETH/USDT", "1m", 4_000, 4_010, 3_990, 4_000, 100),
            BacktestCandle(t0 + timedelta(minutes=1), "ETH/USDT", "1m", 4_000, 4_050, 3_990, 4_040, 120),
        ]
        signal = NormalizedSignal.model_validate(
            {
                "symbol": "ETH/USDT",
                "timeframe": "1m",
                "side": "long",
                "confidence": 0.7,
                "expected_move_bps": 90,
                "stop_bps": 50,
                "take_bps": 90,
                "max_hold_minutes": 3,
                "rationale_codes": ["trend:bullish"],
                "invalidation_reason": "breaks support",
                "model_latency_ms": 123,
            }
        )

        results = ConservativePerpsBacktester(PerpsConfig()).sweep(
            candles,
            [BacktestSignal(timestamp=t0, signal=signal)],
            {"confidence_threshold": [0.65, 0.75], "max_spread_bps": [4, 6]},
        )

        self.assertEqual(len(results), 4)
        self.assertEqual(results[0].params["confidence_threshold"], 0.65)


if __name__ == "__main__":
    unittest.main()
