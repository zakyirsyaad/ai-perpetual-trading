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
        # These rows carry no factor_metadata.setup, so there is no entry shape to
        # report. Exit reasons moved to exit_performance: an exit is a consequence,
        # not a decision, and keying patterns on it produced a tautology.
        self.assertEqual(memory["winning_patterns"], [])
        self.assertEqual(memory["losing_patterns"], [])
        self.assertIn("neutral_exit", memory["exit_performance"])
        self.assertIn("stop_loss", memory["exit_performance"])
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


class EntryShapeMemoryTests(unittest.TestCase):
    """Lessons must describe which ENTRIES lose, not which exits fired.

    Deriving patterns from the exit reason yields a tautology with this data —
    "neutral_exit wins, stop_loss loses" — which tells the model nothing about
    what to open. That is why the same LONG at 78,29x could be repeated three
    times despite the memory being in every prompt.
    """

    def _row(self, setup_key: str, pnl: float, *, reason: str = "stop_loss") -> dict:
        return {
            "pnl_usdt": pnl,
            "reason": reason,
            "direction": "LONG",
            "confidence": 0.8,
            "entry_reason": "trend continuation",
            "factor_metadata": {
                "setup": {"setup_key": setup_key, "family_key": "LONG|trend_led"}
            },
        }

    def test_patterns_are_keyed_on_entry_setup(self) -> None:
        memory = build_trade_decision_memory(
            [self._row("LONG|stretch_up|atr_mid", -100.0) for _ in range(3)]
            + [self._row("LONG|stretch_flat|atr_low", +40.0, reason="neutral_exit")]
        )
        losing = [p["pattern"] for p in memory["losing_patterns"]]
        winning = [p["pattern"] for p in memory["winning_patterns"]]
        self.assertIn("LONG|stretch_up|atr_mid", losing)
        self.assertIn("LONG|stretch_flat|atr_low", winning)
        self.assertNotIn("stop_loss", losing)
        self.assertNotIn("neutral_exit", winning)

    def test_exit_reason_is_still_reported_but_demoted(self) -> None:
        memory = build_trade_decision_memory([self._row("LONG|stretch_up|atr_mid", -100.0)])
        self.assertIn("exit_performance", memory)
        self.assertIn("stop_loss", memory["exit_performance"])

    def test_factor_family_performance_is_reported(self) -> None:
        memory = build_trade_decision_memory([self._row("LONG|stretch_up|atr_mid", -100.0)])
        self.assertIn("LONG|trend_led", memory["family_performance"])

    def test_rows_without_a_setup_yield_no_invented_entry_lesson(self) -> None:
        # Trades written before setup instrumentation must not be dressed up as
        # entry-shape evidence.
        memory = build_trade_decision_memory([
            {"pnl_usdt": -100.0, "reason": "stop_loss", "direction": "LONG", "confidence": 0.8}
        ])
        self.assertEqual(memory["losing_patterns"], [])
        self.assertEqual(memory["winning_patterns"], [])
        self.assertTrue(any("no entry-shape" in str(x).lower() for x in memory["lessons"]))
