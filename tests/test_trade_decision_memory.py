from __future__ import annotations

import unittest

from bot.ai.trade_memory import build_trade_decision_memory


class TradeDecisionMemoryTests(unittest.TestCase):
    def test_summarizes_winning_and_losing_exit_patterns(self) -> None:
        memory = build_trade_decision_memory(
            [
                {
                    "symbol": "BTC/USDT",
                    "direction": "LONG",
                    "entry_price": 100.0,
                    "exit_price": 101.0,
                    "quantity": 10.0,
                    "pnl_usdt": 8.0,
                    "reason": "neutral_exit",
                    "confidence": 0.85,
                    "entry_reason": "ai_volume:OPEN_LONG conf=0.85 note=clean bullish continuation",
                },
                {
                    "symbol": "ETH/USDT",
                    "direction": "SHORT",
                    "entry_price": 100.0,
                    "exit_price": 102.0,
                    "quantity": 10.0,
                    "pnl_usdt": -22.0,
                    "reason": "stop_loss",
                    "confidence": 0.75,
                    "entry_reason": "ai_volume:OPEN_SHORT conf=0.75 note=oversold short chase",
                },
                {
                    "symbol": "ETH/USDT",
                    "direction": "LONG",
                    "entry_price": 100.0,
                    "exit_price": 99.8,
                    "quantity": 10.0,
                    "pnl_usdt": -4.0,
                    "reason": "liquidation_guard",
                    "confidence": 0.65,
                    "entry_reason": "ai_volume:OPEN_LONG conf=0.65 note=tight liquidation distance",
                },
            ]
        )

        self.assertEqual(memory["sample_size"], 3)
        self.assertLess(memory["net_pnl_usdt"], 0)
        self.assertEqual(memory["winning_patterns"][0]["pattern"], "neutral_exit")
        self.assertEqual(memory["losing_patterns"][0]["pattern"], "stop_loss")
        self.assertTrue(any("stop-loss trades" in lesson for lesson in memory["lessons"]))
        self.assertTrue(any("liquidation-guard exits" in lesson for lesson in memory["lessons"]))

    def test_empty_memory_is_advisory(self) -> None:
        memory = build_trade_decision_memory([])

        self.assertEqual(memory["sample_size"], 0)
        self.assertEqual(memory["winning_patterns"], [])
        self.assertEqual(memory["losing_patterns"], [])
        self.assertIn("No realized trade memory yet", memory["lessons"][0])


if __name__ == "__main__":
    unittest.main()
