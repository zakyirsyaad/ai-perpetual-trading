"""Engine wiring of the LONG forward bot: the regime gate, the pause after any exit and the decision payload.

Every new setting defaults to off, so the first test pins that an unconfigured engine ticks exactly as
before: the same strategy call, no regime call and no gate context.
"""
from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.ai.orchestration import AIOrchestrator
from bot.config import Settings
from bot.core.regime_gate import RegimeGate, RegimeVerdict, build_regime_gate
from bot.core.trade_engine import TradeEngine
from bot.models import ClosedTrade, PositionDirection
from bot.strategy.grok_strategy import GrokStrategy

try:  # `python -m unittest tests.x` (package) and `discover -s tests` (top-level) both work
    from tests.test_trade_engine_execution import (
        FakeClient, FakeRisk, FakeStore, FakeStrategy, _plan, _settings,
    )
except ImportError:  # pragma: no cover - depends on how unittest was launched
    from test_trade_engine_execution import (
        FakeClient, FakeRisk, FakeStore, FakeStrategy, _plan, _settings,
    )

LOG_NAME = "test.engine"
NAIK_VERDICT = RegimeVerdict("NAIK", True, "naik", 1.0, 0.8, 1_000)
BLOCKED_VERDICT = RegimeVerdict("TURUN", False, "not_naik:TURUN", 1.0, 0.8, 1_000)


class SpyRegimeGate:
    """Stands in for RegimeGate: serves scripted verdicts and counts the calls."""

    def __init__(self, *verdicts: RegimeVerdict) -> None:
        self._verdicts = list(verdicts)
        self.calls = 0

    async def verdict(self) -> RegimeVerdict:
        self.calls += 1
        index = min(self.calls, len(self._verdicts)) - 1
        return self._verdicts[index]

    def as_payload(self) -> dict:
        index = min(max(self.calls, 1), len(self._verdicts)) - 1
        return {"kondisi": self._verdicts[index].kondisi, "reason": self._verdicts[index].reason}


class ContextStrategy(FakeStrategy):
    """A FakeStrategy that records the gate context the engine hands it before each decision."""

    def __init__(self, plan=None) -> None:
        super().__init__(plan)
        self.contexts: list[dict] = []

    def set_gate_context(self, context: dict) -> None:
        self.contexts.append(context)


def _engine(strategy, settings=None, regime_gate=None, store=None, risk=None) -> TradeEngine:
    return TradeEngine(
        settings or _settings(), logging.getLogger(LOG_NAME), FakeClient(), strategy,
        risk or FakeRisk(), store or FakeStore(), regime_gate=regime_gate,
    )


class DefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_leave_the_tick_path_unchanged(self) -> None:
        strategy = ContextStrategy(_plan())
        engine = _engine(strategy)

        await engine._tick()

        self.assertEqual(strategy.next_calls, 1)
        self.assertEqual(strategy.contexts, [])

    def test_every_new_setting_defaults_off(self) -> None:
        settings = Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", STRATEGY_MODE="volume")
        self.assertEqual(settings.cooldown_minutes_after_exit, 0)
        self.assertEqual(settings.entry_regime_gate, "off")
        self.assertEqual(settings.forward_kill_after_positions, 0)
        self.assertEqual(settings.forward_review_at_positions, 0)
        self.assertEqual(settings.entry_volatility_guard_mult, 0.0)
        self.assertIsNone(build_regime_gate(settings, FakeClient(), logging.getLogger(LOG_NAME)))

    def test_negative_pause_after_exit_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            Settings(
                _env_file=None, BINGX_API_KEY="k", BINGX_SECRET_KEY="s", STRATEGY_MODE="volume",
                COOLDOWN_MINUTES_AFTER_EXIT=-1,
            )

    async def test_a_zero_pause_never_blocks_after_a_close(self) -> None:
        strategy = ContextStrategy(_plan())
        engine = _engine(strategy, _settings(cooldown_minutes_after_exit=0))
        engine._last_exit_ts = datetime.now(timezone.utc).timestamp()

        await engine._tick()

        self.assertEqual(strategy.next_calls, 1)


class RegimeGateTickTests(unittest.IsolatedAsyncioTestCase):
    async def test_regime_gate_blocks_before_the_strategy_and_costs_no_ai_call(self) -> None:
        strategy = ContextStrategy(_plan())
        gate = SpyRegimeGate(BLOCKED_VERDICT)
        engine = _engine(strategy, regime_gate=gate)

        await engine._tick()

        self.assertEqual(gate.calls, 1)
        self.assertEqual(strategy.next_calls, 0)

    async def test_an_allowed_verdict_hands_the_context_over_before_the_strategy_runs(self) -> None:
        strategy = ContextStrategy(_plan())
        engine = _engine(strategy, regime_gate=SpyRegimeGate(NAIK_VERDICT))

        await engine._tick()

        self.assertEqual(strategy.next_calls, 1)
        self.assertEqual(strategy.contexts, [{"kondisi": "NAIK", "reason": "naik"}])

    async def test_a_data_error_verdict_blocks_like_any_other_refusal(self) -> None:
        strategy = ContextStrategy(_plan())
        gate = SpyRegimeGate(RegimeVerdict("", False, "data_error", None, None, None))
        await _engine(strategy, regime_gate=gate)._tick()
        self.assertEqual(strategy.next_calls, 0)

    async def test_an_earlier_gate_stops_the_tick_before_the_regime_gate_is_read(self) -> None:
        gate = SpyRegimeGate(NAIK_VERDICT)
        engine = _engine(ContextStrategy(_plan()), regime_gate=gate, risk=FakeRisk(closed_trades=999))

        await engine._tick()

        self.assertEqual(gate.calls, 0)

    async def test_the_block_is_logged_once_per_state_change(self) -> None:
        gate = SpyRegimeGate(BLOCKED_VERDICT, BLOCKED_VERDICT, BLOCKED_VERDICT, NAIK_VERDICT)
        engine = _engine(ContextStrategy(None), regime_gate=gate)
        with self.assertLogs(LOG_NAME, level="INFO") as captured:
            for _ in range(4):
                await engine._tick()
        blocked = [line for line in captured.output if "regime gate" in line.lower() and "blocks" in line.lower()]
        resumed = [line for line in captured.output if "regime gate" in line.lower() and "allows" in line.lower()]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(len(resumed), 1)

    async def test_the_gate_context_reaches_the_grok_strategy(self) -> None:
        received: list[dict] = []

        class RecordingOrchestrator:
            def set_gate_context(self, context: dict) -> None:
                received.append(context)

            async def next_trade_plan(self, daily_state):
                return None

        strategy = GrokStrategy(RecordingOrchestrator())
        engine = _engine(strategy, regime_gate=SpyRegimeGate(NAIK_VERDICT))

        await engine._tick()

        self.assertEqual(received, [{"kondisi": "NAIK", "reason": "naik"}])


class PauseAfterExitTests(unittest.IsolatedAsyncioTestCase):
    async def _closed_by(self, reason: str):
        store = FakeStore()
        strategy = ContextStrategy(_plan())
        engine = _engine(strategy, _settings(cooldown_minutes_after_exit=15), store=store)

        async def monitor(position, plan):
            return reason

        engine._monitor_position = monitor
        await engine._execute_single_trade(_plan())
        return engine, store, strategy

    async def test_pause_after_exit_blocks_for_15_minutes_after_any_close(self) -> None:
        for reason in ("stop_loss", "neutral_exit", "strategy_exit", "take_profit", "time_stop"):
            with self.subTest(reason=reason):
                engine, store, strategy = await self._closed_by(reason)
                self.assertEqual(store.closed[0].reason, reason)
                self.assertIsNotNone(engine._last_exit_ts)

                await engine._tick()

                self.assertEqual(strategy.next_calls, 0)

    async def test_the_pause_lapses_after_the_configured_minutes(self) -> None:
        strategy = ContextStrategy(_plan())
        engine = _engine(strategy, _settings(cooldown_minutes_after_exit=15))
        engine._last_exit_ts = (datetime.now(timezone.utc) - timedelta(minutes=16)).timestamp()

        await engine._tick()

        self.assertEqual(strategy.next_calls, 1)

    async def test_the_pause_is_logged_once_and_the_resume_once(self) -> None:
        engine = _engine(ContextStrategy(None), _settings(cooldown_minutes_after_exit=15))
        engine._last_exit_ts = datetime.now(timezone.utc).timestamp()
        with self.assertLogs(LOG_NAME, level="INFO") as captured:
            for _ in range(3):
                await engine._tick()
            engine._last_exit_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).timestamp()
            await engine._tick()
        paused = [line for line in captured.output if "pause after exit" in line.lower() and "active" in line.lower()]
        over = [line for line in captured.output if "pause after exit" in line.lower() and "over" in line.lower()]
        self.assertEqual(len(paused), 1)
        self.assertEqual(len(over), 1)

    async def test_pause_runs_after_the_stop_and_entry_cooldowns_and_before_the_regime_gate(self) -> None:
        gate = SpyRegimeGate(NAIK_VERDICT)
        engine = _engine(
            ContextStrategy(_plan()), _settings(cooldown_minutes_after_exit=15), regime_gate=gate
        )
        engine._last_exit_ts = datetime.now(timezone.utc).timestamp()

        await engine._tick()

        self.assertEqual(gate.calls, 0)


class PauseSeedTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _trade(closed_at: datetime) -> ClosedTrade:
        return ClosedTrade(
            symbol="BTC/USDT", direction=PositionDirection.LONG, entry_price=100.0, exit_price=100.1,
            quantity=1.0, pnl_usdt=0.1, reason="neutral_exit", entry_reason="x",
            opened_at=closed_at - timedelta(minutes=5), closed_at=closed_at,
            exchange_entry_order_id="e", exchange_exit_order_id="x", confidence=0.8,
        )

    def _store(self, trades=None, error: Exception | None = None):
        class Store(FakeStore):
            requested: list[int] = []

            async def recent_closed_trades(self, limit: int = 100):
                self.requested.append(limit)
                if error is not None:
                    raise error
                return trades or []

        return Store()

    async def test_pause_after_exit_is_seeded_from_the_last_closed_trade(self) -> None:
        closed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        store = self._store([self._trade(closed_at)])
        strategy = ContextStrategy(_plan())
        engine = _engine(strategy, _settings(cooldown_minutes_after_exit=15), store=store)

        await engine._seed_last_exit()
        await engine._tick()

        self.assertEqual(store.requested, [1])
        self.assertAlmostEqual(engine._last_exit_ts, closed_at.timestamp(), places=3)
        self.assertEqual(strategy.next_calls, 0)

    async def test_an_old_last_close_does_not_pause(self) -> None:
        closed_at = datetime.now(timezone.utc) - timedelta(hours=3)
        strategy = ContextStrategy(_plan())
        engine = _engine(strategy, _settings(cooldown_minutes_after_exit=15), store=self._store([self._trade(closed_at)]))

        await engine._seed_last_exit()
        await engine._tick()

        self.assertEqual(strategy.next_calls, 1)

    async def test_no_closed_trade_leaves_the_pause_unset(self) -> None:
        engine = _engine(ContextStrategy(None), _settings(cooldown_minutes_after_exit=15), store=self._store([]))
        await engine._seed_last_exit()
        self.assertIsNone(engine._last_exit_ts)

    async def test_a_failing_store_is_logged_and_never_crashes_startup(self) -> None:
        engine = _engine(
            ContextStrategy(None), _settings(cooldown_minutes_after_exit=15),
            store=self._store(error=RuntimeError("supabase down")),
        )
        with self.assertLogs(LOG_NAME, level="WARNING"):
            await engine._seed_last_exit()
        self.assertIsNone(engine._last_exit_ts)

    async def test_nothing_is_read_while_the_pause_is_off(self) -> None:
        store = self._store([self._trade(datetime.now(timezone.utc))])
        engine = _engine(ContextStrategy(None), _settings(cooldown_minutes_after_exit=0), store=store)
        await engine._seed_last_exit()
        self.assertEqual(store.requested, [])
        self.assertIsNone(engine._last_exit_ts)


class _RecordingStore:
    def __init__(self) -> None:
        self.decisions: list[dict] = []

    async def save_ai_decision(self, **kwargs) -> None:
        self.decisions.append(kwargs)

    async def save_learning_note(self, **kwargs) -> None:
        return None


def _decision():
    from bot.ai.brain import EntryDecision
    return EntryDecision(
        symbol="BTC/USDT", action="HOLD", confidence=0.5, market_trend="Sideways",
        systemic_risk=2, reason="no edge",
    )


class DecisionPayloadTests(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self, store) -> AIOrchestrator:
        orchestrator = object.__new__(AIOrchestrator)
        orchestrator._settings = SimpleNamespace(exchange_name="bingx")
        orchestrator._brain = SimpleNamespace(last_call_metrics=None)
        orchestrator._trade_store = store
        orchestrator._supabase_journal = None
        return orchestrator

    async def test_ai_decision_payload_carries_the_regime_verdict(self) -> None:
        store = _RecordingStore()
        orchestrator = self._orchestrator(store)
        stretch = {"mode": "shadow", "verdict": "allow"}
        regime = {"kondisi": "NAIK", "allowed": True, "reason": "naik"}

        orchestrator.set_gate_context(regime)
        await orchestrator._record_ai_decision(_decision(), {}, None, dict(stretch))

        gate = store.decisions[0]["payload"]["gate"]
        self.assertEqual(gate["regime"], regime)
        self.assertEqual(gate["mode"], "shadow")      # the stretch-gate verdict is kept
        self.assertEqual(gate["verdict"], "allow")

    async def test_the_regime_verdict_is_recorded_even_without_a_stretch_gate_dict(self) -> None:
        store = _RecordingStore()
        orchestrator = self._orchestrator(store)
        orchestrator.set_gate_context({"reason": "naik"})
        await orchestrator._record_ai_decision(_decision(), {}, None, None)
        self.assertEqual(store.decisions[0]["payload"]["gate"], {"regime": {"reason": "naik"}})

    async def test_without_a_regime_gate_the_payload_is_unchanged(self) -> None:
        store = _RecordingStore()
        orchestrator = self._orchestrator(store)
        stretch = {"mode": "off", "verdict": "allow"}

        await orchestrator._record_ai_decision(_decision(), {}, None, dict(stretch))
        orchestrator.set_gate_context({})
        await orchestrator._record_ai_decision(_decision(), {}, None, dict(stretch))
        await orchestrator._record_ai_decision(_decision(), {}, None, None)

        self.assertEqual(store.decisions[0]["payload"]["gate"], stretch)
        self.assertEqual(store.decisions[1]["payload"]["gate"], stretch)
        self.assertIsNone(store.decisions[2]["payload"]["gate"])
        self.assertNotIn("regime", store.decisions[0]["payload"]["gate"])

    async def test_the_caller_s_gate_dict_is_not_mutated(self) -> None:
        orchestrator = self._orchestrator(_RecordingStore())
        orchestrator.set_gate_context({"reason": "naik"})
        stretch = {"mode": "off"}
        await orchestrator._record_ai_decision(_decision(), {}, None, stretch)
        self.assertEqual(stretch, {"mode": "off"})


class BuildRegimeGateTests(unittest.TestCase):
    def _settings(self, **env):
        if env.get("ENTRY_REGIME_GATE", "off") != "off":  # the companions Settings demands with the gate on
            env = {"ENTRY_GATE_DIRECTION": "long", "ENTRY_STRETCH_GATE_MODE": "enforce",
                   "STRATEGY_MODE": "ai_volume", "XAI_API_KEY": "test-xai", **env}
        env.setdefault("STRATEGY_MODE", "volume")
        return Settings(_env_file=None, BINGX_API_KEY="k", BINGX_SECRET_KEY="s", **env)

    def test_the_gate_is_built_for_the_single_symbol(self) -> None:
        settings = self._settings(ENTRY_REGIME_GATE="naik", SYMBOLS="BTC/USDT", ENTRY_VOLATILITY_GUARD_MULT="2.0")
        gate = build_regime_gate(settings, FakeClient(), logging.getLogger(LOG_NAME))
        self.assertIsInstance(gate, RegimeGate)

    def test_more_than_one_symbol_refuses_to_boot(self) -> None:
        settings = self._settings(ENTRY_REGIME_GATE="naik", SYMBOLS="BTC/USDT,ETH/USDT")
        with self.assertRaises(ValueError) as caught:
            build_regime_gate(settings, FakeClient(), logging.getLogger(LOG_NAME))
        self.assertIn("one symbol", str(caught.exception))

    def test_several_symbols_are_fine_while_the_gate_is_off(self) -> None:
        settings = self._settings(SYMBOLS="BTC/USDT,ETH/USDT")
        self.assertIsNone(build_regime_gate(settings, FakeClient(), logging.getLogger(LOG_NAME)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
