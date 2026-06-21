from __future__ import annotations

import unittest

from bot.arena.program_trader import ProgramStrategyRunner, validate_strategy_code


class ArenaProgramTraderTests(unittest.TestCase):
    def test_runner_accepts_strategy_returning_normalized_signal(self) -> None:
        code = """
def build_signal(context):
    return {
        "symbol": context["symbol"],
        "timeframe": "1m",
        "side": "long",
        "confidence": 0.82,
        "expected_move_bps": 95,
        "stop_bps": 45,
        "take_bps": 110,
        "max_hold_minutes": 12,
        "rationale_codes": ["program:trend"],
        "invalidation_reason": "trend failed",
        "model_latency_ms": 0,
    }
"""
        result = ProgramStrategyRunner(timeout_seconds=1).run_signal(code, {"symbol": "BTC/USDT"})

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.signal.symbol, "BTC/USDT")
        self.assertEqual(result.signal.side, "long")

    def test_validator_rejects_imports_and_missing_stop_take(self) -> None:
        unsafe = validate_strategy_code("import os\ndef build_signal(context):\n    return {}")
        missing_risk = ProgramStrategyRunner(timeout_seconds=1).run_signal(
            """
def build_signal(context):
    return {
        "symbol": "BTC/USDT",
        "timeframe": "1m",
        "side": "long",
        "confidence": 0.8,
        "expected_move_bps": 90,
        "stop_bps": 0,
        "take_bps": 0,
        "max_hold_minutes": 10,
        "rationale_codes": ["program:test"],
        "invalidation_reason": "none",
        "model_latency_ms": 0,
    }
""",
            {},
        )

        self.assertFalse(unsafe.is_valid)
        self.assertIn("Import", unsafe.errors[0])
        self.assertFalse(missing_risk.success)
        self.assertIn("Directional signals require positive", missing_risk.error or "")


if __name__ == "__main__":
    unittest.main()
