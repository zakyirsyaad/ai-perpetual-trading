from __future__ import annotations

import unittest
from types import SimpleNamespace

from bot.ai.exit_policy import AIExitPolicy, ExitCallGate
from bot.models import PositionDirection


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        ai_exit_check_interval_seconds=30,
        ai_exit_price_move_threshold_pct=0.08,
        ai_exit_max_calls_per_trade=12,
        ai_kline_interval="3m",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class ExitPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = AIExitPolicy(_settings())
        self.policy.start_trade(entry_price=100.0, direction=PositionDirection.LONG, now=0.0)

    def _gate(self, now: float, price: float, **kwargs) -> ExitCallGate:
        return self.policy.evaluate(now=now, price=price, **kwargs)

    def test_first_evaluation_after_interval_on_new_candle_is_allowed(self) -> None:
        gate = self._gate(now=200.0, price=100.0, last_closed_candle_ms=180_000)
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "new_candle")

    def test_no_event_means_no_ai_call(self) -> None:
        self.policy.mark_called(now=0.0, price=100.0, last_closed_candle_ms=0, flow_signature="a")
        gate = self._gate(now=1000.0, price=100.0, last_closed_candle_ms=0, flow_signature="a")
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "no_exit_event")

    def test_minimum_interval_is_enforced(self) -> None:
        self.policy.mark_called(now=100.0, price=100.0, last_closed_candle_ms=0, flow_signature="a")
        gate = self._gate(now=120.0, price=100.0, last_closed_candle_ms=180_000, flow_signature="a")
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "min_interval")

    def test_interval_boundary_is_inclusive(self) -> None:
        self.policy.mark_called(now=100.0, price=100.0, last_closed_candle_ms=0, flow_signature="a")
        gate = self._gate(now=130.0, price=100.0, last_closed_candle_ms=180_000, flow_signature="a")
        self.assertTrue(gate.allowed)

    def test_adverse_move_triggers_evaluation(self) -> None:
        self.policy.mark_called(now=0.0, price=100.0, last_closed_candle_ms=0, flow_signature="a")
        gate = self._gate(now=60.0, price=99.9, last_closed_candle_ms=0, flow_signature="a")
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "adverse_move")

    def test_favourable_move_does_not_trigger(self) -> None:
        self.policy.mark_called(now=0.0, price=100.0, last_closed_candle_ms=0, flow_signature="a")
        gate = self._gate(now=60.0, price=100.5, last_closed_candle_ms=0, flow_signature="a")
        self.assertFalse(gate.allowed)

    def test_short_position_adverse_move_is_upward(self) -> None:
        policy = AIExitPolicy(_settings())
        policy.start_trade(entry_price=100.0, direction=PositionDirection.SHORT, now=0.0)
        policy.mark_called(now=0.0, price=100.0, last_closed_candle_ms=0, flow_signature="a")
        self.assertTrue(policy.evaluate(now=60.0, price=100.1, last_closed_candle_ms=0, flow_signature="a").allowed)
        policy.mark_called(now=60.0, price=100.1, last_closed_candle_ms=0, flow_signature="a")
        self.assertFalse(policy.evaluate(now=120.0, price=99.0, last_closed_candle_ms=0, flow_signature="a").allowed)

    def test_new_flow_signature_triggers_evaluation(self) -> None:
        self.policy.mark_called(now=0.0, price=100.0, last_closed_candle_ms=0, flow_signature="calm")
        gate = self._gate(now=60.0, price=100.0, last_closed_candle_ms=0, flow_signature="volatile|oi_surge")
        self.assertTrue(gate.allowed)
        self.assertEqual(gate.reason, "regime_or_flow_change")

    def test_max_calls_per_trade_is_enforced(self) -> None:
        for index in range(12):
            now = float(index * 60)
            gate = self._gate(now=now, price=100.0, last_closed_candle_ms=index * 180_000)
            self.assertTrue(gate.allowed, f"call {index} should be allowed")
            self.policy.mark_called(now=now, price=100.0, last_closed_candle_ms=index * 180_000, flow_signature="a")
        gate = self._gate(now=10_000.0, price=100.0, last_closed_candle_ms=99 * 180_000)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "exit_call_budget_exhausted")

    def test_start_trade_resets_budget(self) -> None:
        for index in range(12):
            self.policy.mark_called(now=float(index * 60), price=100.0, last_closed_candle_ms=index, flow_signature="a")
        self.assertEqual(self.policy.calls_made, 12)
        self.policy.start_trade(entry_price=50.0, direction=PositionDirection.LONG, now=0.0)
        self.assertEqual(self.policy.calls_made, 0)
        self.assertTrue(self._gate(now=100.0, price=50.0, last_closed_candle_ms=180_000).allowed)

    def test_identical_payload_hash_is_skipped(self) -> None:
        self.assertFalse(self.policy.is_duplicate_request("h1"))
        self.policy.mark_called(now=0.0, price=100.0, last_closed_candle_ms=0, flow_signature="a", request_hash="h1")
        self.assertTrue(self.policy.is_duplicate_request("h1"))
        self.assertFalse(self.policy.is_duplicate_request("h2"))

    def test_duplicate_hash_does_not_consume_budget(self) -> None:
        self.policy.note_deduplicated()
        self.assertEqual(self.policy.calls_made, 0)
        self.assertEqual(self.policy.deduplicated_calls, 1)

    def test_zero_threshold_disables_adverse_move_event(self) -> None:
        policy = AIExitPolicy(_settings(ai_exit_price_move_threshold_pct=0.0))
        policy.start_trade(entry_price=100.0, direction=PositionDirection.LONG, now=0.0)
        policy.mark_called(now=0.0, price=100.0, last_closed_candle_ms=0, flow_signature="a")
        gate = policy.evaluate(now=60.0, price=1.0, last_closed_candle_ms=0, flow_signature="a")
        self.assertFalse(gate.allowed)

    def test_flow_signature_helper_is_order_insensitive(self) -> None:
        context = {
            "intelligence": {"regime": "volatile"},
            "active_flow_signals": [{"name": "b", "symbol": "BTC/USDT"}, {"name": "a", "symbol": "BTC/USDT"}],
        }
        reversed_context = {
            "intelligence": {"regime": "volatile"},
            "active_flow_signals": [{"name": "a", "symbol": "BTC/USDT"}, {"name": "b", "symbol": "BTC/USDT"}],
        }
        self.assertEqual(
            AIExitPolicy.flow_signature(context, "BTC/USDT"),
            AIExitPolicy.flow_signature(reversed_context, "BTC/USDT"),
        )

    def test_flow_signature_changes_with_regime(self) -> None:
        calm = AIExitPolicy.flow_signature({"intelligence": {"regime": "calm"}}, "BTC/USDT")
        volatile = AIExitPolicy.flow_signature({"intelligence": {"regime": "volatile"}}, "BTC/USDT")
        self.assertNotEqual(calm, volatile)

    def test_candle_close_helper_floors_to_interval(self) -> None:
        self.assertEqual(AIExitPolicy.last_closed_candle_ms(1_700_000_400_000, "3m"), 1_700_000_280_000)

    def test_budget_state_is_reportable(self) -> None:
        self.policy.mark_called(now=0.0, price=100.0, last_closed_candle_ms=0, flow_signature="a")
        state = self.policy.state()
        self.assertEqual(state["calls_made"], 1)
        self.assertEqual(state["max_calls"], 12)
        self.assertFalse(state["budget_exhausted"])


if __name__ == "__main__":
    unittest.main()
