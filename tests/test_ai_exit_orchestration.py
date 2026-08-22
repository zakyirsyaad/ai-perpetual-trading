from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.ai.compact_context import CompactDecisionContextBuilder
from bot.ai.exit_policy import AIExitPolicy
from bot.ai.orchestration import AIOrchestrator
from bot.models import OpenPosition, PositionDirection


class _FakeBrain:
    def __init__(self, decision=None) -> None:
        self.calls = 0
        self._decision = decision

    async def exit_decision(self, payload: dict):
        self.calls += 1
        return self._decision


class _FakeIntelligence:
    def __init__(self, regime: str = "normal", flow: list | None = None) -> None:
        self.builds = 0
        self.regime = regime
        self.flow = flow if flow is not None else []

    async def build_context(self, symbols=None):
        self.builds += 1
        regime = self.regime
        flow = list(self.flow)
        return SimpleNamespace(
            to_dict=lambda: {
                "technicals": [{"symbol": "BTC/USDT", "last_price": 60000.0, "rsi_14": 50.0}],
                "external_signals": {},
                "intelligence": {"regime": regime, "risk_flags": [], "symbol_scores": {}},
                "factor_snapshots": [],
                "active_flow_signals": flow,
            }
        )


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        strategy_mode="ai_volume",
        symbols=["BTC/USDT"],
        taker_fee_pct=0.05,
        maker_fee_pct=-0.02,
        leverage=100,
        position_hold_minutes=60,
        ai_min_confidence=0.5,
        ai_kline_interval="3m",
        ai_exit_check_interval_seconds=30,
        ai_exit_price_move_threshold_pct=0.08,
        ai_exit_max_calls_per_trade=12,
        ai_compact_max_factors_per_symbol=6,
        ai_compact_max_flow_signals=5,
        ai_compact_max_memory_lessons=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _position() -> OpenPosition:
    return OpenPosition(
        symbol="BTC/USDT",
        direction=PositionDirection.LONG,
        entry_price=60000.0,
        quantity=0.01,
        exchange_size=0.01,
        exchange_order_id="order-1",
        opened_at=datetime.now(timezone.utc),
    )


def _orchestrator(brain, intelligence, settings) -> AIOrchestrator:
    orchestrator = object.__new__(AIOrchestrator)
    orchestrator._settings = settings
    orchestrator._brain = brain
    orchestrator._intelligence = intelligence
    orchestrator._logger = logging.getLogger("test.exit_orchestration")
    orchestrator._compact_context = CompactDecisionContextBuilder(settings)
    orchestrator._exit_policy = AIExitPolicy(settings)
    orchestrator._active_exit_symbol = None
    return orchestrator


class EventDrivenExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_ticks_without_an_event_never_call_the_ai(self) -> None:
        brain = _FakeBrain()
        intelligence = _FakeIntelligence()
        orchestrator = _orchestrator(brain, intelligence, _settings())
        position = _position()

        # First tick establishes the trade and fires on the current candle.
        await orchestrator.exit_signal(position, 60000.0, 5.0)
        first_calls = brain.calls

        # 5-second monitor ticks inside the same candle with no adverse move.
        for _ in range(20):
            signal = await orchestrator.exit_signal(position, 60000.0, 10.0)
            self.assertFalse(signal.close_now)

        self.assertEqual(brain.calls, first_calls)
        self.assertEqual(intelligence.builds, first_calls)

    async def test_no_market_context_is_built_when_the_gate_blocks(self) -> None:
        brain = _FakeBrain()
        intelligence = _FakeIntelligence()
        orchestrator = _orchestrator(brain, intelligence, _settings())
        position = _position()
        await orchestrator.exit_signal(position, 60000.0, 5.0)
        builds_after_first = intelligence.builds
        await orchestrator.exit_signal(position, 60000.0, 6.0)
        self.assertEqual(intelligence.builds, builds_after_first)

    async def test_exit_call_budget_is_capped_per_trade(self) -> None:
        brain = _FakeBrain()
        orchestrator = _orchestrator(brain, _FakeIntelligence(), _settings(ai_exit_check_interval_seconds=0))
        position = _position()
        price = 60000.0
        for index in range(40):
            # Each tick moves adversely enough to be an event.
            price *= 0.999
            await orchestrator.exit_signal(position, price, float(index))
        self.assertLessEqual(brain.calls, 12)

    async def test_deterministic_time_stop_still_fires_without_ai(self) -> None:
        brain = _FakeBrain()
        settings = _settings(ai_exit_max_calls_per_trade=0)
        orchestrator = _orchestrator(brain, _FakeIntelligence(), settings)
        signal = await orchestrator.exit_signal(_position(), 60000.0, 3600.0)
        self.assertTrue(signal.close_now)
        self.assertEqual(signal.reason, "time_stop")
        self.assertEqual(brain.calls, 0)

    async def test_hold_when_budget_exhausted_and_not_past_time_stop(self) -> None:
        orchestrator = _orchestrator(_FakeBrain(), _FakeIntelligence(), _settings(ai_exit_max_calls_per_trade=0))
        signal = await orchestrator.exit_signal(_position(), 60000.0, 10.0)
        self.assertFalse(signal.close_now)
        self.assertEqual(signal.reason, "hold")

    async def test_close_decision_is_returned(self) -> None:
        brain = _FakeBrain(SimpleNamespace(action="CLOSE", confidence=0.9, reason="trend invalidated"))
        orchestrator = _orchestrator(brain, _FakeIntelligence(), _settings())
        signal = await orchestrator.exit_signal(_position(), 60000.0, 10.0)
        self.assertTrue(signal.close_now)
        self.assertEqual(signal.reason, "trend invalidated")

    async def test_low_confidence_close_is_ignored(self) -> None:
        brain = _FakeBrain(SimpleNamespace(action="CLOSE", confidence=0.1, reason="weak"))
        orchestrator = _orchestrator(brain, _FakeIntelligence(), _settings())
        signal = await orchestrator.exit_signal(_position(), 60000.0, 10.0)
        self.assertFalse(signal.close_now)

    async def test_new_trade_resets_the_exit_budget(self) -> None:
        brain = _FakeBrain()
        orchestrator = _orchestrator(brain, _FakeIntelligence(), _settings(ai_exit_check_interval_seconds=0))
        position = _position()
        price = 60000.0
        for index in range(20):
            price *= 0.999
            await orchestrator.exit_signal(position, price, float(index))
        self.assertEqual(orchestrator.exit_policy_state()["calls_made"], 12)

        second = OpenPosition(
            symbol="BTC/USDT",
            direction=PositionDirection.LONG,
            entry_price=50000.0,
            quantity=0.01,
            exchange_size=0.01,
            exchange_order_id="order-2",
            opened_at=datetime.now(timezone.utc),
        )
        await orchestrator.exit_signal(second, 50000.0, 1.0)
        self.assertEqual(orchestrator.exit_policy_state()["calls_made"], 1)

    async def test_regime_change_alone_can_open_the_gate(self) -> None:
        # Regression: this trigger was unreachable because evaluate() ran before
        # the flow signature existed, so only candle/adverse events could fire.
        brain = _FakeBrain()
        intelligence = _FakeIntelligence(regime="calm")
        orchestrator = _orchestrator(brain, intelligence, _settings(ai_exit_check_interval_seconds=0))
        position = _position()

        await orchestrator.exit_signal(position, 60000.0, 10.0)
        calls_after_first = brain.calls

        # Same candle, no adverse move: only the regime changed.
        orchestrator._exit_policy._last_candle_ms = orchestrator._exit_policy._last_candle_ms
        intelligence.regime = "volatile"
        await orchestrator.exit_signal(position, 60000.0, 11.0)

        self.assertEqual(brain.calls, calls_after_first + 1)

    async def test_new_flow_trigger_alone_can_open_the_gate(self) -> None:
        brain = _FakeBrain()
        intelligence = _FakeIntelligence()
        orchestrator = _orchestrator(brain, intelligence, _settings(ai_exit_check_interval_seconds=0))
        position = _position()

        await orchestrator.exit_signal(position, 60000.0, 10.0)
        calls_after_first = brain.calls

        intelligence.flow = [{"symbol": "BTC/USDT", "name": "oi_surge", "direction": "LONG", "strength": 0.8}]
        await orchestrator.exit_signal(position, 60000.0, 11.0)

        self.assertEqual(brain.calls, calls_after_first + 1)

    async def test_unchanged_regime_does_not_call_the_ai(self) -> None:
        brain = _FakeBrain()
        intelligence = _FakeIntelligence()
        orchestrator = _orchestrator(brain, intelligence, _settings(ai_exit_check_interval_seconds=0))
        position = _position()

        await orchestrator.exit_signal(position, 60000.0, 10.0)
        calls_after_first = brain.calls

        await orchestrator.exit_signal(position, 60000.0, 11.0)

        self.assertEqual(brain.calls, calls_after_first)

    async def test_probes_are_rate_limited_to_the_minimum_interval(self) -> None:
        # A probe builds context; without rate-limiting it would do so on every
        # five-second monitor tick.
        brain = _FakeBrain()
        intelligence = _FakeIntelligence()
        orchestrator = _orchestrator(brain, intelligence, _settings())
        position = _position()

        await orchestrator.exit_signal(position, 60000.0, 10.0)
        builds_after_first = intelligence.builds

        for _ in range(10):
            await orchestrator.exit_signal(position, 60000.0, 11.0)

        self.assertEqual(intelligence.builds, builds_after_first)

    async def test_identical_payload_is_deduplicated(self) -> None:
        brain = _FakeBrain()
        orchestrator = _orchestrator(brain, _FakeIntelligence(), _settings(ai_exit_check_interval_seconds=0))
        position = _position()
        await orchestrator.exit_signal(position, 60000.0, 10.0)
        self.assertEqual(brain.calls, 1)
        # Force a fresh candle event so the gate opens again; the compact payload is
        # byte-identical (same price, same elapsed, same static context), so the
        # request must be deduplicated instead of re-sent.
        orchestrator._exit_policy._last_candle_ms = -1
        await orchestrator.exit_signal(position, 60000.0, 10.0)
        self.assertEqual(brain.calls, 1)
        self.assertGreaterEqual(orchestrator.exit_policy_state()["deduplicated_calls"], 1)


if __name__ == "__main__":
    unittest.main()
