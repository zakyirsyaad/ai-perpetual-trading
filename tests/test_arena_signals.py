from __future__ import annotations

import unittest

from pydantic import ValidationError

from bot.arena.signals import NormalizedSignal, normalize_legacy_entry_decision


class ArenaSignalTests(unittest.TestCase):
    def test_valid_signal_contract(self) -> None:
        signal = NormalizedSignal.model_validate(
            {
                "symbol": "btc-usdt",
                "timeframe": "1m",
                "side": "long",
                "confidence": 0.72,
                "expected_move_bps": 85,
                "stop_bps": 50,
                "take_bps": 95,
                "max_hold_minutes": 12,
                "rationale_codes": ["trend:bullish", "spread_ok"],
                "invalidation_reason": "breaks intraday support",
                "model_latency_ms": 2500,
            }
        )

        self.assertEqual(signal.symbol, "BTC/USDT")
        self.assertEqual(signal.side, "long")

    def test_rejects_non_contract_payload(self) -> None:
        with self.assertRaises(ValidationError):
            NormalizedSignal.model_validate(
                {
                    "symbol": "SOL/USDT",
                    "timeframe": "15m",
                    "side": "long",
                    "confidence": 0.9,
                    "expected_move_bps": 0,
                    "stop_bps": 0,
                    "take_bps": 0,
                    "max_hold_minutes": 10,
                    "rationale_codes": ["bad code with spaces"],
                    "invalidation_reason": "x",
                    "model_latency_ms": 1,
                    "extra": True,
                }
            )

    def test_legacy_open_requires_stop_and_take(self) -> None:
        with self.assertRaises(ValueError):
            normalize_legacy_entry_decision(
                {
                    "symbol": "BTC/USDT",
                    "action": "OPEN_LONG",
                    "confidence": 0.8,
                    "market_trend": "Bullish",
                    "reason": "missing stops",
                },
                timeframe="1m",
                model_latency_ms=100,
                default_max_hold_minutes=10,
            )


if __name__ == "__main__":
    unittest.main()
