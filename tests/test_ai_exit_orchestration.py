from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from bot.ai.compact_context import CompactDecisionContextBuilder
from bot.config import Settings
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
        ai_min_confidence=0.75,
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

    async def test_close_below_point_75_is_rejected_after_the_hold_period(self) -> None:
        brain = _FakeBrain(SimpleNamespace(action="CLOSE", confidence=0.74, reason="weak"))
        orchestrator = _orchestrator(brain, _FakeIntelligence(), _settings())

        signal = await orchestrator.exit_signal(_position(), 60000.0, 3601.0)

        self.assertFalse(signal.close_now)
        # Labelled apart from the policy's own fallback hold: this verdict came
        # from the model and was rejected by the confidence gate, and the record
        # of it has to say so.
        self.assertEqual(signal.reason, "hold_low_confidence")
        self.assertTrue(signal.ai_called)
        self.assertEqual(signal.confidence, 0.74)

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



class ReservedBudgetSkipsContextBuildTests(unittest.IsolatedAsyncioTestCase):
    """Once only reserved calls remain, a quiet tick must not build market context.

    With the routine budget closed, a flow change can no longer earn a call, so
    building the context just to probe for one wastes an exchange round trip on
    every monitor tick.
    """

    async def test_no_context_is_built_when_only_reserved_calls_remain(self) -> None:
        intelligence = _FakeIntelligence()
        settings = _settings(ai_exit_max_calls_per_trade=4, ai_exit_event_reserve_calls=4)
        orchestrator = _orchestrator(_FakeBrain(), intelligence, settings)
        position = _position()

        signal = await orchestrator.exit_signal(position, 60000.0, 120.0)

        self.assertFalse(signal.close_now)
        self.assertFalse(signal.ai_called)
        self.assertEqual(intelligence.builds, 0)



class MinimumHoldBeforeAIExitTests(unittest.IsolatedAsyncioTestCase):
    """Exits the model takes in the first minutes of a trade cost money.

    Cycle from 2026-09-14, 32 AI exits compared with the same trades exited by the
    deterministic rules alone: the 8 taken within three minutes lost 98.76 more
    (three of them would have reached the +40 floor), while the 24 taken later
    saved 282.89. The first call fires seconds after entry, when net is already
    about -18 from fees, so the model reads "underwater" and the -25 cut honours
    it after a 0.04% move. Stop loss, take profit and the floor keep running.
    """

    def _brain(self) -> _FakeBrain:
        return _FakeBrain(SimpleNamespace(action="CLOSE", confidence=0.9, reason="position is underwater"))

    async def test_the_model_is_not_asked_before_the_minimum_hold(self) -> None:
        brain, intelligence = self._brain(), _FakeIntelligence()
        orchestrator = _orchestrator(brain, intelligence, _settings(ai_exit_min_hold_seconds=180))

        signal = await orchestrator.exit_signal(_position(), 60000.0, 60.0)

        self.assertFalse(signal.close_now)
        self.assertFalse(signal.ai_called)
        self.assertEqual(brain.calls, 0)
        self.assertEqual(intelligence.builds, 0)

    async def test_the_model_is_asked_once_the_minimum_hold_has_passed(self) -> None:
        brain = self._brain()
        orchestrator = _orchestrator(brain, _FakeIntelligence(), _settings(ai_exit_min_hold_seconds=180))

        signal = await orchestrator.exit_signal(_position(), 60000.0, 181.0)

        self.assertEqual(brain.calls, 1)
        self.assertTrue(signal.close_now)

    async def test_zero_keeps_the_previous_behaviour(self) -> None:
        # Characterisation guard: an unchanged .env still asks the model immediately.
        brain = self._brain()
        orchestrator = _orchestrator(brain, _FakeIntelligence(), _settings())

        await orchestrator.exit_signal(_position(), 60000.0, 5.0)

        self.assertEqual(brain.calls, 1)


class MinimumHoldSettingTests(unittest.TestCase):
    def _settings(self, **env) -> Settings:
        return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", STRATEGY_MODE="volume", **env)

    def test_defaults_to_off_and_reads_the_env(self) -> None:
        self.assertEqual(self._settings().ai_exit_min_hold_seconds, 0)
        self.assertEqual(self._settings(AI_EXIT_MIN_HOLD_SECONDS="180").ai_exit_min_hold_seconds, 180)

    def test_negative_values_are_rejected(self) -> None:
        with self.assertRaises(Exception):
            self._settings(AI_EXIT_MIN_HOLD_SECONDS="-1")


if __name__ == "__main__":
    unittest.main()
