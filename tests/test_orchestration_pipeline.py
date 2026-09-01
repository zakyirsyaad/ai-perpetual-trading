from __future__ import annotations

import logging
import unittest
from datetime import date
from types import SimpleNamespace

from bot.arena.agents import MarketAnalyzer, SignalDesigner
from bot.ai.brain import EntryDecision, RiskReview
from bot.ai.compact_context import CompactDecisionContextBuilder, compact_json, estimate_payload_tokens
from bot.ai.exit_policy import AIExitPolicy
from bot.ai.orchestration import AIOrchestrator
from bot.models import PositionDirection
from bot.risk.risk_manager import DailyRiskState


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        strategy_mode="ai_volume",
        symbols=("BTC/USDT", "ETH/USDT"),
        exchange_name="bingx",
        margin_mode="isolated",
        position_mode="hedge",
        taker_fee_pct=0.05,
        maker_fee_pct=-0.02,
        leverage=100,
        position_usdt=180.0,
        account_balance_usdt=1500.0,
        max_risk_pct_per_trade=0.5,
        trade_target_per_day=28,
        max_stop_loss_per_day=2,
        max_consecutive_losses=3,
        max_daily_drawdown_usdt=100.0,
        stop_loss_pct=0.08,
        take_profit_pct=0.12,
        atr_sl_multiplier=1.5,
        atr_tp_multiplier=3.0,
        ai_min_fee_adjusted_rr=0.1,
        ai_min_confidence=0.75,
        ai_recent_performance_limit=10,
        ai_trade_memory_limit=10,
        ai_kline_interval="3m",
        ai_exit_check_interval_seconds=30,
        ai_exit_price_move_threshold_pct=0.08,
        ai_exit_max_calls_per_trade=12,
        ai_compact_max_factors_per_symbol=6,
        ai_compact_max_flow_signals=5,
        ai_compact_max_memory_lessons=3,
        position_hold_minutes=60,
        volume_profile=SimpleNamespace(
            margin_per_entry_usdt=180.0,
            volume_per_entry_usdt=36_000.0,
            as_log_fields=lambda: {"margin_per_entry_usdt": 180.0},
        ),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _context() -> dict:
    factors = [
        {
            "symbol": symbol,
            "name": f"factor_{index}",
            "category": "trend",
            "score": 0.5 - index * 0.1,
            "direction": "LONG",
            "percentile": 0.8,
            "value": 1.0,
            "reason": "x",
        }
        for symbol in ("BTC/USDT", "ETH/USDT")
        for index in range(10)
    ]
    return {
        "technicals": [
            {"symbol": "BTC/USDT", "last_price": 60000.0, "atr_14": 60.0, "rsi_14": 55.0},
            {"symbol": "ETH/USDT", "last_price": 3000.0, "atr_14": 3.0, "rsi_14": 52.0},
        ],
        "external_signals": {"tradingview": {"summary": "BUY"}},
        "intelligence": {
            "regime": "normal",
            "risk_flags": [],
            "symbol_scores": {
                "BTC/USDT": {"composite_score": 0.6},
                "ETH/USDT": {"composite_score": 0.2},
            },
        },
        "factor_snapshots": factors,
        "active_flow_signals": [{"symbol": "BTC/USDT", "name": "volume_spike", "direction": "LONG", "strength": 0.6}],
        "liquidation_constraints": {
            "BTC/USDT": {"status": "available", "max_long_stop_loss_pct": 1.0, "max_short_stop_loss_pct": 1.0},
            "ETH/USDT": {"status": "available", "max_long_stop_loss_pct": 1.0, "max_short_stop_loss_pct": 1.0},
        },
    }


class _FakeBrain:
    def __init__(self, decisions) -> None:
        self.decisions = decisions
        self.payloads: list[dict] = []
        self.last_call_metrics = SimpleNamespace(call_id="call-1")

    async def entry_decisions(self, payload: dict):
        self.payloads.append(payload)
        return self.decisions


class _FakeIntelligence:
    async def build_context(self, symbols=None):
        return SimpleNamespace(to_dict=_context)


class _FakeStore:
    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.notes: list[dict] = []

    async def recent_performance_summary(self, limit: int = 50) -> dict:
        return {"total_trades": 5, "total_pnl_usdt": 1.0}

    async def recent_closed_trade_records(self, limit: int = 100) -> list[dict]:
        return []

    async def save_ai_decision(self, **kwargs) -> None:
        self.decisions.append(kwargs)

    async def save_learning_note(self, **kwargs) -> None:
        self.notes.append(kwargs)


def _decision(symbol: str, action: str = "OPEN_LONG", approved: bool = True, **overrides) -> EntryDecision:
    base = dict(
        symbol=symbol,
        action=action,
        confidence=0.8,
        market_trend="Bullish",
        stop_loss_pct=0.08,
        take_profit_pct=0.3,
        systemic_risk=2,
        reason="clean setup",
        risk_review=RiskReview(
            approved=approved,
            confirmations="live confirmations",
            invalidation_trigger="structure breaks",
        ),
    )
    base.update(overrides)
    return EntryDecision(**base)


def _orchestrator(brain, store, settings=None) -> AIOrchestrator:
    settings = settings or _settings()
    orchestrator = object.__new__(AIOrchestrator)
    orchestrator._settings = settings
    orchestrator._brain = brain
    orchestrator._trade_store = store
    orchestrator._intelligence = _FakeIntelligence()
    orchestrator._logger = logging.getLogger("test.pipeline")
    orchestrator._supabase_journal = None
    orchestrator._market_analyzer = SimpleNamespace(review=lambda ctx: SimpleNamespace(to_dict=lambda: {}))
    orchestrator._signal_designer = SimpleNamespace(review=lambda ctx: SimpleNamespace(to_dict=lambda: {}))
    orchestrator._compact_context = CompactDecisionContextBuilder(settings)
    orchestrator._exit_policy = AIExitPolicy(settings)
    orchestrator._active_exit_symbol = None
    return orchestrator


def _state() -> DailyRiskState:
    return DailyRiskState(
        day=date(2026, 8, 22),
        stop_losses=0,
        closed_trades=2,
        total_pnl_usdt=1.0,
        consecutive_losses=0,
        allowed_to_trade=True,
    )


class EntryPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_best_candidate_becomes_a_trade_plan(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT"), _decision("ETH/USDT")])
        store = _FakeStore()
        plan = await _orchestrator(brain, store).next_trade_plan(_state())

        self.assertIsNotNone(plan)
        self.assertIn(plan.symbol, {"BTC/USDT", "ETH/USDT"})
        self.assertEqual(plan.direction, PositionDirection.LONG)
        self.assertEqual(plan.usdt_size, 180.0)
        self.assertEqual(len(store.decisions), 2)

    async def test_entry_payload_has_context_parity_and_liquidation_rules(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT")])
        await _orchestrator(brain, _FakeStore()).next_trade_plan(_state())
        payload = brain.payloads[0]
        self.assertIn("market_context", payload)
        self.assertIn("risk_constraints", payload)
        self.assertIn("arena_reviews", payload)
        self.assertEqual(payload["risk_constraints"]["min_confidence"], 0.75)
        self.assertTrue(payload["output_rules"]["open_requires_final_stop_strictly_before_estimated_liquidation_or_hold"])

    async def test_entry_payload_keeps_each_new_flow_record_once_across_context_and_arena_review(self) -> None:
        trigger = {
            "symbol": "BTC/USDT",
            "name": "fresh_btc_trigger",
            "direction": "LONG",
            "strength": 0.9,
            "state": "new",
        }
        context = _context()
        context["new_flow_signals"] = [trigger]
        context["intelligence"]["market_flow_signals"] = {
            "active": [trigger],
            "new_triggers": [trigger],
            "trigger_names": ["fresh_btc_trigger"],
        }

        class _FlowIntelligence:
            async def build_context(self, symbols=None):
                return SimpleNamespace(to_dict=lambda: context)

        brain = _FakeBrain([_decision("BTC/USDT")])
        orchestrator = _orchestrator(brain, _FakeStore())
        orchestrator._intelligence = _FlowIntelligence()
        orchestrator._signal_designer = SignalDesigner()

        await orchestrator.next_trade_plan(_state())

        payload = brain.payloads[0]
        serialized = compact_json(payload)
        self.assertEqual(serialized.count(compact_json(trigger)), 1)
        self.assertEqual(serialized.count("fresh_btc_trigger"), 1)
        review = next(item for item in payload["arena_reviews"] if item.get("role") == "SignalDesigner")
        self.assertNotIn("new_triggers", review["payload"])
        self.assertNotIn("trigger_names", review["payload"])
        self.assertNotIn("fresh_btc_trigger", review["summary"])

    async def test_entry_payload_keeps_risk_flags_once_across_context_and_arena_review(self) -> None:
        context = _context()
        context["intelligence"]["risk_flags"] = ["liquidity_thin"]

        class _RiskIntelligence:
            async def build_context(self, symbols=None):
                return SimpleNamespace(to_dict=lambda: context)

        brain = _FakeBrain([_decision("BTC/USDT")])
        orchestrator = _orchestrator(brain, _FakeStore())
        orchestrator._intelligence = _RiskIntelligence()
        orchestrator._market_analyzer = MarketAnalyzer()

        await orchestrator.next_trade_plan(_state())

        payload = brain.payloads[0]
        self.assertEqual(compact_json(payload).count("liquidity_thin"), 1)
        review = next(item for item in payload["arena_reviews"] if item.get("role") == "MarketAnalyzer")
        self.assertNotIn("risk_flags", review["payload"])

    async def test_stored_decision_carries_the_ai_call_id(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT")])
        store = _FakeStore()
        await _orchestrator(brain, store).next_trade_plan(_state())
        self.assertEqual(store.decisions[0]["ai_call_id"], "call-1")

    async def test_no_ai_response_yields_no_plan(self) -> None:
        plan = await _orchestrator(_FakeBrain(None), _FakeStore()).next_trade_plan(_state())
        self.assertIsNone(plan)

    async def test_all_vetoed_yields_no_plan(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT", approved=False), _decision("ETH/USDT", approved=False)])
        self.assertIsNone(await _orchestrator(brain, _FakeStore()).next_trade_plan(_state()))

    async def test_duplicate_symbol_decisions_are_deduplicated(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT"), _decision("BTC/USDT")])
        store = _FakeStore()
        await _orchestrator(brain, store).next_trade_plan(_state())
        self.assertEqual(len(store.decisions), 1)

    async def test_unsupported_symbol_is_ignored(self) -> None:
        brain = _FakeBrain([_decision("DOGE/USDT")])
        self.assertIsNone(await _orchestrator(brain, _FakeStore()).next_trade_plan(_state()))

    async def test_hold_decisions_yield_no_plan(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT", action="HOLD")])
        self.assertIsNone(await _orchestrator(brain, _FakeStore()).next_trade_plan(_state()))

    async def test_learning_note_is_persisted(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT", learning_note="watch the funding flip")])
        store = _FakeStore()
        await _orchestrator(brain, store).next_trade_plan(_state())
        self.assertEqual(store.notes[0]["note_type"], "entry_decision")

    async def test_invalid_stop_and_take_profit_reject_the_candidate(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT", stop_loss_pct=0.0, take_profit_pct=0.0)])
        orchestrator = _orchestrator(
            brain, _FakeStore(), _settings(stop_loss_pct=0.0, take_profit_pct=0.0)
        )
        self.assertIsNone(await orchestrator.next_trade_plan(_state()))

    async def test_grok_mode_sizes_from_risk_not_volume_profile(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT")])
        orchestrator = _orchestrator(brain, _FakeStore(), _settings(strategy_mode="grok"))
        plan = await orchestrator.next_trade_plan(_state())
        self.assertIsNotNone(plan)
        self.assertNotEqual(plan.usdt_size, 180.0)
        self.assertIn("grok:OPEN_LONG", plan.decision_reason)

    async def test_plan_carries_factor_and_trigger_metadata(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT")])
        plan = await _orchestrator(brain, _FakeStore()).next_trade_plan(_state())
        self.assertIn("top_factors", plan.factor_metadata)
        self.assertIn("trigger_names", plan.trigger_metadata)

    async def test_targets_respect_the_atr_floor(self) -> None:
        brain = _FakeBrain([_decision("BTC/USDT")])
        plan = await _orchestrator(brain, _FakeStore()).next_trade_plan(_state())
        # ATR is 0.1% of price; the SL floor is atr_pct * ATR_SL_MULTIPLIER.
        self.assertGreaterEqual(plan.stop_loss_pct, 0.1 * 1.5 - 1e-9)


if __name__ == "__main__":
    unittest.main()
