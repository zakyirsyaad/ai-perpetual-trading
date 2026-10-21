from __future__ import annotations

import asyncio
import logging
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from bot.ai.brain import EntryDecision, RiskReview
from bot.ai.compact_context import CompactDecisionContextBuilder, compact_json
from bot.ai.trade_memory import build_trade_decision_memory
from bot.arena.agents import MarketAnalyzer, SignalDesigner
from bot.ai.orchestration import AIOrchestrator, EntryCandidate, ResolvedTradeTargets
from bot.models import PositionDirection
from bot.risk.risk_manager import DailyRiskState


def _approved_review() -> RiskReview:
    return RiskReview(
        approved=True,
        confirmations="live confirmations present",
        invalidation_trigger="trend structure breaks",
    )


def _bare_orchestrator() -> AIOrchestrator:
    """An orchestrator built without `__init__`, holding the in-memory state `__init__` sets (no gate
    context, no shadow row times, no ledger snapshot); each test adds the collaborators it needs."""
    orchestrator = object.__new__(AIOrchestrator)
    orchestrator._gate_context = None
    orchestrator._shadow_rows_written_at = {}
    orchestrator._shadow_seeded_keys = frozenset()
    orchestrator._last_ledger_snapshot = None
    orchestrator._last_ledger_read_at = None
    return orchestrator


def _volume_run_settings() -> SimpleNamespace:
    """Settings mirroring the live volume-building run where trade-suppressing
    gates are removed (confidence/systemic/RR floors no longer veto)."""
    return SimpleNamespace(
        strategy_mode="ai_volume",
        entry_regime_gate="off",
        entry_gate_direction="any",
        symbols=["BTC/USDT", "ETH/USDT"],
        stop_loss_pct=0.08,
        take_profit_pct=0.12,
        atr_sl_multiplier=1.5,
        atr_tp_multiplier=3.0,
        taker_fee_pct=0.05,
        maker_fee_pct=-0.02,
        leverage=100,
        ai_min_fee_adjusted_rr=0.1,
        ai_min_confidence=0.75,
        ai_compact_max_factors_per_symbol=6,
        ai_compact_max_flow_signals=5,
        ai_compact_max_memory_lessons=3,
    )


class EntryGateRemovalTests(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self) -> AIOrchestrator:
        orchestrator = _bare_orchestrator()
        orchestrator._settings = _volume_run_settings()
        orchestrator._logger = logging.getLogger("test.entry_gate_removal")
        return orchestrator

    def test_low_confidence_decision_is_rejected_even_for_ai_volume(self) -> None:
        orchestrator = self._orchestrator()
        decision = EntryDecision(
            symbol="BTC/USDT",
            action="OPEN_LONG",
            confidence=0.10,
            market_trend="Sideways",
            stop_loss_pct=0.08,
            take_profit_pct=0.12,
            risk_reward_ratio=0.5,
            systemic_risk=5,
            reason="weak/choppy setup that the old guards would reject",
            risk_review=_approved_review(),
        )

        self.assertIsNone(orchestrator._build_entry_candidate(decision, {"technicals": []}))

    def test_hold_decision_is_skipped(self) -> None:
        # HOLD is no longer offered to the model, but if one slips through it must be
        # skipped rather than crash.
        orchestrator = self._orchestrator()
        decision = EntryDecision(
            symbol="BTC/USDT",
            action="HOLD",
            confidence=0.9,
            market_trend="Sideways",
            systemic_risk=1,
            reason="model returned hold despite the volume mandate",
        )

        self.assertIsNone(orchestrator._build_entry_candidate(decision, {"technicals": []}))


class AIOrchestrationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_payload_uses_configured_history_limits(self) -> None:
        class FakeTradeStore:
            def __init__(self) -> None:
                self.performance_limit: int | None = None
                self.memory_limit: int | None = None

            async def recent_performance_summary(self, limit: int = 50) -> dict:
                self.performance_limit = limit
                return {"sample_size": limit}

            async def recent_closed_trade_records(self, limit: int = 100) -> list[dict]:
                self.memory_limit = limit
                return []

        class FakeBrain:
            async def entry_decisions(self, payload: dict) -> None:
                return None

        class FakeIntelligence:
            async def build_context(self) -> SimpleNamespace:
                return SimpleNamespace(
                    to_dict=lambda: {
                        "technicals": [],
                        "external_signals": {},
                        "intelligence": {},
                        "factor_snapshots": [],
                        "active_flow_signals": [],
                    }
                )

        store = FakeTradeStore()
        orchestrator = _bare_orchestrator()
        orchestrator._settings = SimpleNamespace(
            strategy_mode="grok",
            okx_instrument_type="SWAP",
            okx_margin_mode="isolated",
            symbols=["BTC/USDT", "ETH/USDT"],
            trade_target_per_day=28,
            max_stop_loss_per_day=2,
            max_consecutive_losses=3,
            max_daily_drawdown_usdt=100.0,
            leverage=100,
            position_usdt=180.0,
            ai_min_fee_adjusted_rr=0.1,
            taker_fee_pct=0.05,
            ai_recent_performance_limit=12,
            ai_trade_memory_limit=24,
            maker_fee_pct=-0.02,
            exchange_name="bingx",
            margin_mode="isolated",
            position_mode="hedge",
            ai_compact_max_factors_per_symbol=6,
            ai_compact_max_flow_signals=5,
            ai_compact_max_memory_lessons=3,
        )
        orchestrator._brain = FakeBrain()
        orchestrator._trade_store = store
        orchestrator._intelligence = FakeIntelligence()
        orchestrator._market_analyzer = SimpleNamespace(review=lambda context: SimpleNamespace(to_dict=lambda: {}))
        orchestrator._signal_designer = SimpleNamespace(review=lambda context: SimpleNamespace(to_dict=lambda: {}))
        orchestrator._compact_context = CompactDecisionContextBuilder(orchestrator._settings)

        plan = await orchestrator.next_trade_plan(
            DailyRiskState(
                day=date(2026, 6, 27),
                stop_losses=0,
                closed_trades=0,
                total_pnl_usdt=0.0,
                consecutive_losses=0,
                allowed_to_trade=True,
            )
        )

        self.assertIsNone(plan)
        self.assertEqual(store.performance_limit, 12)
        self.assertEqual(store.memory_limit, 24)

    async def test_candidate_to_trade_plan_uses_candidate_direction(self) -> None:
        orchestrator = _bare_orchestrator()
        orchestrator._settings = SimpleNamespace(
            strategy_mode="grok",
            account_balance_usdt=60.0,
            max_risk_pct_per_trade=0.5,
            leverage=3,
            position_usdt=20.0,
            taker_fee_pct=0.05,
        )

        candidate = EntryCandidate(
            decision=EntryDecision(
                symbol="ETH/USDT",
                action="OPEN_SHORT",
                confidence=0.81,
                market_trend="Bearish",
                stop_loss_pct=0.5,
                take_profit_pct=1.0,
                risk_reward_ratio=2.0,
                systemic_risk=2,
                reason="bearish continuation setup",
                risk_review=_approved_review(),
            ),
            direction=PositionDirection.SHORT,
            target=ResolvedTradeTargets(
                stop_loss_pct=0.5,
                take_profit_pct=1.0,
                atr_pct=0.1,
                fee_adjusted_rr=1.5,
                round_trip_fee_pct=0.1,
            ),
            score=2.0,
        )

        plan = await orchestrator._candidate_to_trade_plan(candidate, {"technicals": []})

        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, PositionDirection.SHORT)
        self.assertEqual(plan.symbol, "ETH/USDT")
        self.assertEqual(plan.stop_loss_pct, 0.5)
        self.assertEqual(plan.take_profit_pct, 1.0)

    async def test_ai_volume_plan_uses_volume_margin_size(self) -> None:
        orchestrator = _bare_orchestrator()
        orchestrator._settings = SimpleNamespace(
            strategy_mode="ai_volume",
            account_balance_usdt=1500.0,
            max_risk_pct_per_trade=0.5,
            leverage=100,
            position_usdt=20.0,
            taker_fee_pct=0.05,
            volume_profile=SimpleNamespace(
                margin_per_entry_usdt=180.0,
                volume_per_entry_usdt=36_000.0,
            ),
        )

        candidate = EntryCandidate(
            decision=EntryDecision(
                symbol="BTC/USDT",
                action="OPEN_LONG",
                confidence=0.84,
                market_trend="Bullish",
                stop_loss_pct=0.5,
                take_profit_pct=1.2,
                risk_reward_ratio=2.4,
                systemic_risk=2,
                reason="bullish continuation with clean liquidity",
                risk_review=_approved_review(),
            ),
            direction=PositionDirection.LONG,
            target=ResolvedTradeTargets(
                stop_loss_pct=0.5,
                take_profit_pct=1.2,
                atr_pct=0.1,
                fee_adjusted_rr=1.8,
                round_trip_fee_pct=0.1,
            ),
            score=2.0,
        )

        plan = await orchestrator._candidate_to_trade_plan(candidate, {"technicals": []})

        self.assertIsNotNone(plan)
        self.assertEqual(plan.usdt_size, 180.0)
        self.assertEqual(plan.leverage, 100)
        self.assertIn("ai_volume:OPEN_LONG", plan.decision_reason)
        self.assertIn("entry_volume=36000.00", plan.decision_reason)


if __name__ == "__main__":
    unittest.main()


class MergedRiskReviewGateTests(unittest.TestCase):
    def _orchestrator(self) -> AIOrchestrator:
        orchestrator = _bare_orchestrator()
        orchestrator._settings = _volume_run_settings()
        orchestrator._logger = logging.getLogger("test.risk_review")
        return orchestrator

    def _decision(self, **overrides) -> EntryDecision:
        base = dict(
            symbol="BTC/USDT",
            action="OPEN_LONG",
            confidence=0.8,
            market_trend="Bullish",
            stop_loss_pct=0.08,
            take_profit_pct=0.12,
            systemic_risk=2,
            reason="clean breakout",
            risk_review=_approved_review(),
        )
        base.update(overrides)
        return EntryDecision(**base)

    def test_approved_review_allows_candidate(self) -> None:
        candidate = self._orchestrator()._build_entry_candidate(
            self._decision(),
            {
                "technicals": [],
                "liquidation_constraints": {
                    "BTC/USDT": {"status": "available", "max_long_stop_loss_pct": 0.5}
                },
            },
        )
        self.assertIsNotNone(candidate)

    def test_stop_at_or_beyond_estimated_liquidation_is_rejected(self) -> None:
        decision = self._decision(stop_loss_pct=0.5)
        context = {
            "technicals": [],
            "liquidation_constraints": {
                "BTC/USDT": {"status": "available", "max_long_stop_loss_pct": 0.5}
            },
        }
        self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, context))

    def test_non_finite_stop_or_liquidation_limit_is_rejected(self) -> None:
        for stop_loss_pct, maximum in ((float("nan"), 0.5), (float("inf"), 0.5), (0.1, float("nan")), (0.1, float("inf"))):
            with self.subTest(stop_loss_pct=stop_loss_pct, maximum=maximum):
                decision = self._decision()
                decision.stop_loss_pct = stop_loss_pct
                context = {
                    "technicals": [],
                    "liquidation_constraints": {
                        "BTC/USDT": {"status": "available", "max_long_stop_loss_pct": maximum}
                    },
                }
                self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, context))

    def test_vetoed_review_blocks_candidate(self) -> None:
        decision = self._decision(
            risk_review=RiskReview(
                approved=False,
                confirmations="thin confirmations",
                invalidation_trigger="no real level to lean on",
            )
        )
        self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, {"technicals": []}))

    def test_missing_review_blocks_candidate_instead_of_failing_open(self) -> None:
        decision = self._decision(risk_review=None)
        self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, {"technicals": []}))

    def test_structurally_invalid_review_blocks_candidate(self) -> None:
        decision = EntryDecision.model_validate(
            {
                "symbol": "BTC/USDT",
                "action": "OPEN_SHORT",
                "confidence": 0.7,
                "market_trend": "Bearish",
                "stop_loss_pct": 0.08,
                "take_profit_pct": 0.12,
                "systemic_risk": 2,
                "reason": "breakdown",
                "risk_review": {"approved": True},
            }
        )
        self.assertIsNone(self._orchestrator()._build_entry_candidate(decision, {"technicals": []}))


class StopLossCeilingTests(unittest.TestCase):
    """MAX_STOP_LOSS_PCT restores the hard loss bound that the removed
    liquidation_guard exit used to provide implicitly.

    Without a ceiling `sl_pct = max(base_sl, atr_pct * ATR_SL_MULTIPLIER)` grows
    without limit, so a volatile entry opens with a stop far wider than the
    profit-taking band and a single adverse move costs several winners.
    """

    def _orchestrator(self, **overrides) -> AIOrchestrator:
        settings = _volume_run_settings()
        for key, value in overrides.items():
            setattr(settings, key, value)
        orchestrator = _bare_orchestrator()
        orchestrator._settings = settings
        orchestrator._logger = logging.getLogger("test.stop_loss_ceiling")
        return orchestrator

    def _decision(self, **overrides) -> EntryDecision:
        base = dict(
            symbol="BTC/USDT",
            action="OPEN_LONG",
            confidence=0.9,
            market_trend="Bullish",
            stop_loss_pct=0.08,
            take_profit_pct=0.12,
            risk_reward_ratio=2.0,
            systemic_risk=1,
            reason="ceiling test",
            risk_review=_approved_review(),
        )
        base.update(overrides)
        return EntryDecision(**base)

    def _context(self, atr_14: float, *, max_stop: float = 5.0) -> dict:
        return {
            "technicals": [{"symbol": "BTC/USDT", "atr_14": atr_14, "last_price": 100.0}],
            "liquidation_constraints": {
                "BTC/USDT": {"status": "available", "max_long_stop_loss_pct": max_stop}
            },
        }

    def test_atr_scaled_stop_is_capped(self) -> None:
        # atr_pct 0.8% * 1.5 = 1.20%, ceiling 0.35% must win.
        target = self._orchestrator(max_stop_loss_pct=0.35)._resolve_trade_targets(
            self._decision(), self._context(0.8)
        )
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target.stop_loss_pct, 0.35, places=6)

    def test_ceiling_is_disabled_by_default(self) -> None:
        target = self._orchestrator(max_stop_loss_pct=0.0)._resolve_trade_targets(
            self._decision(), self._context(0.8)
        )
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target.stop_loss_pct, 1.20, places=6)

    def test_ceiling_never_widens_an_already_tighter_stop(self) -> None:
        # atr_pct 0.1% * 1.5 = 0.15% stays 0.15%; the ceiling is a maximum, not a floor.
        target = self._orchestrator(max_stop_loss_pct=0.35)._resolve_trade_targets(
            self._decision(), self._context(0.1)
        )
        self.assertIsNotNone(target)
        self.assertAlmostEqual(target.stop_loss_pct, 0.15, places=6)

    def test_capping_improves_fee_adjusted_rr(self) -> None:
        capped = self._orchestrator(max_stop_loss_pct=0.35)._resolve_trade_targets(
            self._decision(), self._context(0.8)
        )
        uncapped = self._orchestrator(max_stop_loss_pct=0.0)._resolve_trade_targets(
            self._decision(), self._context(0.8)
        )
        self.assertGreater(capped.fee_adjusted_rr, uncapped.fee_adjusted_rr)

    def test_ceiling_does_not_bypass_the_liquidation_guard(self) -> None:
        # A ceiling at or beyond the liquidation limit must still reject the entry.
        target = self._orchestrator(max_stop_loss_pct=0.35)._resolve_trade_targets(
            self._decision(), self._context(0.8, max_stop=0.30)
        )
        self.assertIsNone(target)


class SetupMetadataTests(unittest.TestCase):
    """The setup profile must be persisted at entry, otherwise attribution can
    never learn which *shape* of entry loses money."""

    def _context(self, **overrides) -> dict:
        features = {"symbol": "BTC/USDT", "last_price": 100.4, "ema_21": 100.0, "atr_14": 0.25, "rsi_14": 72.0}
        features.update(overrides)
        return {"technicals": [features], "factor_snapshots": []}

    def test_entry_metadata_carries_the_setup_profile(self) -> None:
        meta = AIOrchestrator._factor_metadata_for_symbol(self._context(), "BTC/USDT", "LONG")
        self.assertEqual(meta["setup"]["setup_key"], "LONG|stretch_up|atr_mid")

    def test_setup_profile_survives_a_symbol_with_no_factor_snapshots(self) -> None:
        meta = AIOrchestrator._factor_metadata_for_symbol(self._context(), "BTC/USDT", "SHORT")
        self.assertEqual(meta["top_factors"], [])
        self.assertEqual(meta["setup"]["direction"], "SHORT")

    def test_missing_technicals_still_yield_a_recorded_setup(self) -> None:
        meta = AIOrchestrator._factor_metadata_for_symbol({"technicals": []}, "BTC/USDT", "LONG")
        self.assertEqual(meta["setup"]["setup_key"], "LONG|unknown|unknown")


class EntryPacingRecordTests(unittest.IsolatedAsyncioTestCase):
    """Volume pacing is recorded beside each entry decision, not sent to the model.

    This step is instrumentation only. The numbers a pacing prompt would need
    must exist in history before any prompt change is allowed to touch live
    behaviour, so the request the model receives has to stay unchanged.
    """

    async def test_pacing_state_is_recorded_without_reaching_the_model_request(self) -> None:
        class FakeTradeStore:
            def __init__(self) -> None:
                self.decisions: list[dict] = []

            async def recent_performance_summary(self, limit: int = 50) -> dict:
                return {"sample_size": 0}

            async def recent_closed_trade_records(self, limit: int = 100) -> list[dict]:
                return []

            async def total_volume_for_day(self, day=None) -> float:
                return 360_000.0

            async def save_ai_decision(self, **kwargs) -> None:
                self.decisions.append(kwargs)

        class FakeBrain:
            last_call_metrics = None

            def __init__(self) -> None:
                self.payload: dict | None = None

            async def entry_decisions(self, payload: dict):
                self.payload = payload
                return [
                    EntryDecision(
                        symbol="BTC/USDT",
                        action="HOLD",
                        confidence=0.5,
                        market_trend="Sideways",
                        systemic_risk=2,
                        reason="no edge worth the fees",
                    )
                ]

        class FakeIntelligence:
            async def build_context(self) -> SimpleNamespace:
                return SimpleNamespace(
                    to_dict=lambda: {
                        "technicals": [],
                        "external_signals": {},
                        "intelligence": {},
                        "factor_snapshots": [],
                        "active_flow_signals": [],
                    }
                )

        store = FakeTradeStore()
        brain = FakeBrain()
        orchestrator = _bare_orchestrator()
        orchestrator._settings = SimpleNamespace(
            strategy_mode="ai_volume",
            symbols=["BTC/USDT"],
            trade_target_per_day=28,
            max_stop_loss_per_day=2,
            max_consecutive_losses=3,
            max_daily_drawdown_usdt=100.0,
            leverage=100,
            position_usdt=180.0,
            ai_min_fee_adjusted_rr=0.1,
            ai_min_confidence=0.75,
            taker_fee_pct=0.05,
            maker_fee_pct=-0.02,
            ai_recent_performance_limit=12,
            ai_trade_memory_limit=24,
            exchange_name="bingx",
            margin_mode="isolated",
            position_mode="hedge",
            trading_timezone="Asia/Jakarta",
            ai_compact_max_factors_per_symbol=6,
            ai_compact_max_flow_signals=5,
            ai_compact_max_memory_lessons=3,
            volume_profile=SimpleNamespace(
                daily_volume_usdt=1_000_000.0,
                as_log_fields=lambda: {"daily_volume_usdt": 1_000_000.0},
            ),
        )
        orchestrator._logger = logging.getLogger("test.orchestrator")
        orchestrator._brain = brain
        orchestrator._trade_store = store
        orchestrator._supabase_journal = None
        orchestrator._intelligence = FakeIntelligence()
        orchestrator._market_analyzer = SimpleNamespace(review=lambda context: SimpleNamespace(to_dict=lambda: {}))
        orchestrator._signal_designer = SimpleNamespace(review=lambda context: SimpleNamespace(to_dict=lambda: {}))
        orchestrator._compact_context = CompactDecisionContextBuilder(orchestrator._settings)

        plan = await orchestrator.next_trade_plan(
            DailyRiskState(
                day=date(2026, 9, 12),
                stop_losses=0,
                closed_trades=10,
                total_pnl_usdt=-120.0,
                consecutive_losses=0,
                allowed_to_trade=True,
            )
        )

        self.assertIsNone(plan)
        pacing = store.decisions[-1]["payload"]["pacing"]
        self.assertEqual(pacing["volume_today_usdt"], 360_000.0)
        self.assertEqual(pacing["target_volume_per_day_usdt"], 1_000_000.0)
        self.assertEqual(pacing["volume_completion_pct"], 36.0)
        self.assertEqual(pacing["closed_trades_today"], 10)
        self.assertEqual(pacing["day_pnl_usdt"], -120.0)
        # Volume is accounted on the trading-timezone day, so the clock reported
        # here must be that same day's, not UTC's.
        self.assertGreaterEqual(pacing["hours_remaining_in_trading_day"], 0.0)
        self.assertLessEqual(pacing["hours_remaining_in_trading_day"], 24.0)
        self.assertNotIn("pacing", brain.payload)


class EntryStretchGateTests(unittest.TestCase):
    """Entries taken far from the mean are the measured loss source.

    Replay over 342 stored BTC decisions: keeping only near-mean entries moved
    gross/1M from -2 to +170 (cadence-faithful exit model) and from -230 to +113
    (gross-faithful). Prompt text does not move this — the pacing arm halved the
    entry rate without changing which ticks were picked — so the gate belongs in
    code, with a shadow mode that records what it would have blocked.
    """

    def _settings(self, **overrides) -> SimpleNamespace:
        data = dict(vars(_volume_run_settings()))
        # Mirrors production: Settings always exposes volume_profile in ai_volume mode.
        data.setdefault("volume_profile", SimpleNamespace(volume_per_entry_usdt=36_000.0,
                                                          margin_per_entry_usdt=180.0))
        data.setdefault("volume_sides_per_entry", 2)
        data.setdefault("entry_stretch_gate_mode", "off")
        data.setdefault("entry_stretch_gate_max_atr", 0.75)
        data.setdefault("entry_stretch_gate_behind_schedule_entries_per_hour", 0.0)
        data.update(overrides)
        return SimpleNamespace(**data)

    def _orchestrator(self, **overrides) -> AIOrchestrator:
        orchestrator = _bare_orchestrator()
        orchestrator._settings = self._settings(**overrides)
        orchestrator._logger = logging.getLogger("test.stretch_gate")
        return orchestrator

    def _decision(self, action: str = "OPEN_LONG") -> EntryDecision:
        return EntryDecision(
            symbol="BTC/USDT",
            action=action,
            confidence=0.8,
            market_trend="Bullish",
            stop_loss_pct=0.08,
            take_profit_pct=0.12,
            systemic_risk=2,
            reason="clean breakout",
            risk_review=_approved_review(),
        )

    def _context(self, *, ema_21: float) -> dict:
        # atr_14 = 0.1 on a price of 100 keeps the ATR-scaled stop inside the
        # liquidation limit; ema_21 alone sets how stretched the entry is.
        return {
            "technicals": [{"symbol": "BTC/USDT", "last_price": 100.0, "ema_21": ema_21, "atr_14": 0.1}],
            "liquidation_constraints": {"BTC/USDT": {"status": "available", "max_long_stop_loss_pct": 0.5,
                                                     "max_short_stop_loss_pct": 0.5}},
        }

    STRETCHED = 99.86   # (100 - 99.86) / 0.1 = 1.4 ATR above the mean
    NEAR_MEAN = 99.98   # 0.2 ATR

    def test_enforce_blocks_an_entry_far_from_the_mean(self) -> None:
        orchestrator = self._orchestrator(entry_stretch_gate_mode="enforce")
        context = self._context(ema_21=self.STRETCHED)
        verdict = orchestrator._stretch_gate_verdict(self._decision(), context, None)

        self.assertEqual(verdict["verdict"], "block")
        self.assertAlmostEqual(verdict["stretch_atr"], 1.4, places=2)
        self.assertIsNone(orchestrator._build_entry_candidate(self._decision(), context, verdict))

    def test_enforce_allows_an_entry_near_the_mean(self) -> None:
        orchestrator = self._orchestrator(entry_stretch_gate_mode="enforce")
        context = self._context(ema_21=self.NEAR_MEAN)
        verdict = orchestrator._stretch_gate_verdict(self._decision(), context, None)

        self.assertEqual(verdict["verdict"], "allow")
        self.assertIsNotNone(orchestrator._build_entry_candidate(self._decision(), context, verdict))

    def test_shadow_records_the_block_without_stopping_the_entry(self) -> None:
        orchestrator = self._orchestrator(entry_stretch_gate_mode="shadow")
        context = self._context(ema_21=self.STRETCHED)
        verdict = orchestrator._stretch_gate_verdict(self._decision(), context, None)

        self.assertEqual(verdict["mode"], "shadow")
        self.assertEqual(verdict["verdict"], "block")
        self.assertIsNotNone(orchestrator._build_entry_candidate(self._decision(), context, verdict))

    def test_disabled_by_default_changes_nothing(self) -> None:
        orchestrator = self._orchestrator()
        context = self._context(ema_21=self.STRETCHED)
        verdict = orchestrator._stretch_gate_verdict(self._decision(), context, None)

        self.assertEqual(verdict["mode"], "off")
        self.assertEqual(verdict["verdict"], "allow")
        self.assertIsNotNone(orchestrator._build_entry_candidate(self._decision(), context, verdict))

    def test_a_day_behind_schedule_relaxes_the_gate(self) -> None:
        # Volume is the mandate: when the remaining schedule needs more entries
        # per hour than the threshold, a stretched entry is better than no entry.
        orchestrator = self._orchestrator(
            entry_stretch_gate_mode="enforce",
            entry_stretch_gate_behind_schedule_entries_per_hour=2.0,
        )
        context = self._context(ema_21=self.STRETCHED)
        pacing = {"volume_today_usdt": 100_000.0, "target_volume_per_day_usdt": 1_008_000.0,
                  "hours_remaining_in_trading_day": 6.0}

        verdict = orchestrator._stretch_gate_verdict(self._decision(), context, pacing)

        self.assertTrue(verdict["behind_schedule"])
        self.assertEqual(verdict["verdict"], "allow")
        self.assertIsNotNone(orchestrator._build_entry_candidate(self._decision(), context, verdict))


class EntryDirectionFilterTests(EntryStretchGateTests):
    """One side only: SHORT near the mean is the only bucket with positive gross.

    Over 700 stored BTC decisions (31 Aug - 6 Oct) the SHORT near-mean bucket carried
    +162 gross per 1M in both phases while every other bucket was negative. The user
    chose lower volume over trading the buckets that lose, so the gate can restrict the
    side as well as the stretch. Inherits the helpers; the parent's own tests re-run
    unchanged, which doubles as the regression check that "any" changes nothing.
    """

    def test_other_side_is_blocked_even_when_it_sits_near_the_mean(self) -> None:
        orchestrator = self._orchestrator(entry_stretch_gate_mode="enforce", entry_gate_direction="short")
        context = self._context(ema_21=self.NEAR_MEAN)
        decision = self._decision("OPEN_LONG")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "block")
        self.assertEqual(verdict["reason"], "direction")
        self.assertIsNone(orchestrator._build_entry_candidate(decision, context, verdict))

    def test_chosen_side_near_the_mean_is_allowed(self) -> None:
        orchestrator = self._orchestrator(entry_stretch_gate_mode="enforce", entry_gate_direction="short")
        context = self._context(ema_21=self.NEAR_MEAN)
        decision = self._decision("OPEN_SHORT")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "allow")
        self.assertIsNotNone(orchestrator._build_entry_candidate(decision, context, verdict))

    def test_chosen_side_far_from_the_mean_is_still_blocked_by_stretch(self) -> None:
        orchestrator = self._orchestrator(entry_stretch_gate_mode="enforce", entry_gate_direction="short")
        context = self._context(ema_21=100.14)   # price 1.4 ATR below the mean
        decision = self._decision("OPEN_SHORT")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "block")
        self.assertNotEqual(verdict.get("reason"), "direction")

    def test_a_day_behind_schedule_cannot_bypass_the_side_filter(self) -> None:
        orchestrator = self._orchestrator(
            entry_stretch_gate_mode="enforce",
            entry_gate_direction="short",
            entry_stretch_gate_behind_schedule_entries_per_hour=2.0,
        )
        context = self._context(ema_21=self.NEAR_MEAN)
        pacing = {"volume_today_usdt": 0.0, "target_volume_per_day_usdt": 1_008_000.0,
                  "hours_remaining_in_trading_day": 6.0}
        decision = self._decision("OPEN_LONG")

        verdict = orchestrator._stretch_gate_verdict(decision, context, pacing)

        self.assertEqual(verdict["verdict"], "block")
        self.assertEqual(verdict["reason"], "direction")

    def test_shadow_mode_records_the_side_block_without_stopping_the_entry(self) -> None:
        orchestrator = self._orchestrator(entry_stretch_gate_mode="shadow", entry_gate_direction="short")
        context = self._context(ema_21=self.NEAR_MEAN)
        decision = self._decision("OPEN_LONG")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "block")
        self.assertEqual(verdict["reason"], "direction")
        self.assertIsNotNone(orchestrator._build_entry_candidate(decision, context, verdict))

    def test_off_mode_ignores_the_side_filter(self) -> None:
        orchestrator = self._orchestrator(entry_stretch_gate_mode="off", entry_gate_direction="short")
        verdict = orchestrator._stretch_gate_verdict(
            self._decision("OPEN_LONG"), self._context(ema_21=self.NEAR_MEAN), None
        )
        self.assertEqual(verdict["verdict"], "allow")


class RegimeGateSideDefenceTests(unittest.IsolatedAsyncioTestCase):
    """With the regime gate on, only the configured side may become a candidate, whatever the stretch mode.

    The LONG forward bot's invariant used to rest on the stretch gate enforcing; a misconfigured
    ENTRY_STRETCH_GATE_MODE must not let a SHORT through. The defence lives in the verdict, so the
    stored decision and the outcome agree. Gate off: nothing changes.
    """

    _settings = EntryStretchGateTests._settings
    _orchestrator = EntryStretchGateTests._orchestrator
    _decision = EntryStretchGateTests._decision
    _context = EntryStretchGateTests._context
    NEAR_MEAN = EntryStretchGateTests.NEAR_MEAN

    def _gated(self, **overrides) -> AIOrchestrator:
        options = dict(entry_regime_gate="naik", entry_gate_direction="long", entry_stretch_gate_mode="off")
        options.update(overrides)
        return self._orchestrator(**options)

    def test_a_short_gets_a_direction_block_even_when_the_stretch_gate_is_off(self) -> None:
        orchestrator = self._gated()
        context = self._context(ema_21=self.NEAR_MEAN)
        decision = self._decision("OPEN_SHORT")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "block")
        self.assertEqual(verdict["reason"], "direction")
        # the record keeps the configured stretch mode and names what enforced the side
        self.assertEqual(verdict["mode"], "off")
        self.assertEqual(verdict["forced_by"], "regime_gate")
        self.assertEqual(verdict["allowed_direction"], "long")
        # otherwise the same block the enforcing stretch gate returns
        reference = self._orchestrator(entry_stretch_gate_mode="enforce", entry_gate_direction="long")
        expected = reference._stretch_gate_verdict(decision, context, None)
        self.assertNotIn("forced_by", expected)
        self.assertEqual(verdict, {**expected, "mode": "off", "forced_by": "regime_gate"})

        with self.assertLogs("test.stretch_gate", level="INFO") as logs:
            candidate = orchestrator._build_entry_candidate(decision, context, verdict)
        self.assertIsNone(candidate)
        self.assertIn("lets through long entries only", "\n".join(logs.output))

    async def test_the_stored_decision_shows_the_block_the_outcome_follows(self) -> None:
        saved: list[dict] = []

        class Store:
            async def save_ai_decision(self, **kwargs) -> None:
                saved.append(kwargs)

        orchestrator = self._gated()
        orchestrator._trade_store = Store()
        orchestrator._brain = SimpleNamespace()
        orchestrator._supabase_journal = None
        orchestrator._gate_context = None
        context = self._context(ema_21=self.NEAR_MEAN)
        decision = self._decision("OPEN_SHORT")
        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        await orchestrator._record_ai_decision(decision, {}, None, verdict)

        gate = saved[0]["payload"]["gate"]
        self.assertEqual(
            (gate["verdict"], gate["reason"], gate["mode"], gate["forced_by"]),
            ("block", "direction", "off", "regime_gate"),
        )
        self.assertIsNone(orchestrator._build_entry_candidate(decision, context, verdict))

    def test_the_configured_side_still_passes_with_the_gate_on(self) -> None:
        orchestrator = self._gated()
        context = self._context(ema_21=self.NEAR_MEAN)
        decision = self._decision("OPEN_LONG")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "allow")
        self.assertIsNotNone(orchestrator._build_entry_candidate(decision, context, verdict))

    def test_with_the_gate_off_a_short_still_passes(self) -> None:
        orchestrator = self._gated(entry_regime_gate="off")
        context = self._context(ema_21=self.NEAR_MEAN)
        decision = self._decision("OPEN_SHORT")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "allow")
        self.assertEqual(verdict["mode"], "off")
        self.assertIsNotNone(orchestrator._build_entry_candidate(decision, context, verdict))


class EntryGateDirectionSettingTests(unittest.TestCase):
    def test_default_is_any_and_value_is_normalised(self) -> None:
        from bot.config import Settings

        self.assertEqual(Settings.model_fields["entry_gate_direction"].default, "any")
        self.assertEqual(Settings.normalize_entry_gate_direction(" SHORT "), "short")
        self.assertEqual(Settings.normalize_entry_gate_direction(""), "any")

    def test_unknown_value_is_rejected(self) -> None:
        from bot.config import Settings

        with self.assertRaises(ValueError):
            Settings.normalize_entry_gate_direction("both")


class MapSideTests(EntryStretchGateTests):
    """Under `ENTRY_REGIME_GATE=map` the hourly regime picks the one side an entry may take.

    NAIK maps to LONG and TURUN to SHORT. The mapped side is judged by the stretch check like any
    other entry; a side that `ENTRY_GATE_DIRECTION` does not make live is a shadow side (marked, not
    traded, evaluated later). Every other side, and every hour without a mapped side, is a direction
    block. Inherits the helpers; the parent's tests re-run with the gate off, which doubles as the
    regression check that the default path is unchanged.
    """

    def _mapped(self, kondisi: str, allowed_direction: str, **overrides) -> AIOrchestrator:
        options = dict(entry_regime_gate="map", entry_stretch_gate_mode="enforce", entry_gate_direction="any")
        options.update(overrides)
        orchestrator = self._orchestrator(**options)
        orchestrator.set_gate_context(
            {
                "kondisi": kondisi,
                "allowed": bool(allowed_direction),
                "reason": kondisi.lower(),
                "allowed_direction": allowed_direction,
            }
        )
        return orchestrator

    def test_map_mapped_side_live_passes_to_the_stretch_check(self) -> None:
        orchestrator = self._mapped("TURUN", "short", entry_gate_direction="short")
        context = self._context(ema_21=100.02)   # price 0.2 ATR below the mean
        decision = self._decision("OPEN_SHORT")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "allow")
        self.assertIs(verdict["shadow_side"], False)
        self.assertEqual(verdict["mapped_side"], "short")
        self.assertIsNotNone(orchestrator._build_entry_candidate(decision, context, verdict))

    def test_map_mapped_side_not_live_is_shadow(self) -> None:
        orchestrator = self._mapped("NAIK", "long", entry_gate_direction="short")
        context = self._context(ema_21=self.NEAR_MEAN)
        decision = self._decision("OPEN_LONG")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "allow")
        self.assertIs(verdict["shadow_side"], True)
        self.assertEqual(verdict["mapped_side"], "long")

    def test_map_shadow_side_still_gets_the_stretch_check(self) -> None:
        orchestrator = self._mapped("NAIK", "long", entry_gate_direction="short")
        context = self._context(ema_21=self.STRETCHED)
        decision = self._decision("OPEN_LONG")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "block")
        self.assertEqual(verdict["reason"], "stretch")
        self.assertIs(verdict["shadow_side"], True)

    def test_map_wrong_side_is_a_direction_block(self) -> None:
        orchestrator = self._mapped("TURUN", "short", entry_gate_direction="short")
        context = self._context(ema_21=self.NEAR_MEAN)
        decision = self._decision("OPEN_LONG")

        verdict = orchestrator._stretch_gate_verdict(decision, context, None)

        self.assertEqual(verdict["verdict"], "block")
        self.assertEqual(verdict["reason"], "direction")
        self.assertEqual(verdict["forced_by"], "regime_gate")
        self.assertEqual(verdict["allowed_direction"], "short")
        self.assertIsNone(orchestrator._build_entry_candidate(decision, context, verdict))

    def test_map_without_context_blocks_every_side(self) -> None:
        orchestrator = self._orchestrator(
            entry_regime_gate="map", entry_stretch_gate_mode="enforce", entry_gate_direction="any"
        )
        context = self._context(ema_21=self.NEAR_MEAN)
        for action in ("OPEN_LONG", "OPEN_SHORT"):
            with self.subTest(action=action):
                decision = self._decision(action)

                verdict = orchestrator._stretch_gate_verdict(decision, context, None)

                self.assertEqual(verdict["verdict"], "block")
                self.assertEqual(verdict["reason"], "direction")
                self.assertEqual(verdict["allowed_direction"], "")
                self.assertEqual(verdict["forced_by"], "regime_gate")
                self.assertIsNone(orchestrator._build_entry_candidate(decision, context, verdict))

    def test_map_a_hold_hour_blocks_both_sides(self) -> None:
        orchestrator = self._mapped("SIDEWAYS", "")
        context = self._context(ema_21=self.NEAR_MEAN)
        for action in ("OPEN_LONG", "OPEN_SHORT"):
            verdict = orchestrator._stretch_gate_verdict(self._decision(action), context, None)
            self.assertEqual((verdict["verdict"], verdict["reason"]), ("block", "direction"))

    def test_map_mapped_side_runs_the_stretch_check_whatever_the_stretch_mode(self) -> None:
        orchestrator = self._mapped("TURUN", "short", entry_stretch_gate_mode="off")
        context = self._context(ema_21=100.14)   # price 1.4 ATR below the mean
        verdict = orchestrator._stretch_gate_verdict(self._decision("OPEN_SHORT"), context, None)

        self.assertEqual(verdict["verdict"], "block")
        self.assertEqual(verdict["reason"], "stretch")

    def test_map_a_day_behind_schedule_cannot_bypass_the_mapped_side(self) -> None:
        orchestrator = self._mapped(
            "TURUN", "short", entry_stretch_gate_behind_schedule_entries_per_hour=2.0
        )
        pacing = {"volume_today_usdt": 0.0, "target_volume_per_day_usdt": 1_008_000.0,
                  "hours_remaining_in_trading_day": 6.0}
        verdict = orchestrator._stretch_gate_verdict(
            self._decision("OPEN_LONG"), self._context(ema_21=self.NEAR_MEAN), pacing
        )

        self.assertEqual((verdict["verdict"], verdict["reason"]), ("block", "direction"))

    def test_naik_and_off_verdicts_are_unchanged(self) -> None:
        # The side maps are ignored outside `map`, even with a TURUN context lying around.
        naik = self._orchestrator(
            entry_regime_gate="naik", entry_gate_direction="long", entry_stretch_gate_mode="off"
        )
        naik.set_gate_context({"kondisi": "TURUN", "allowed_direction": "short"})
        context = self._context(ema_21=self.NEAR_MEAN)

        blocked = naik._stretch_gate_verdict(self._decision("OPEN_SHORT"), context, None)
        passed = naik._stretch_gate_verdict(self._decision("OPEN_LONG"), context, None)

        self.assertEqual((blocked["verdict"], blocked["reason"]), ("block", "direction"))
        self.assertEqual(blocked["allowed_direction"], "long")
        self.assertEqual(blocked["forced_by"], "regime_gate")
        self.assertEqual(passed["verdict"], "allow")
        for verdict in (blocked, passed):
            self.assertNotIn("mapped_side", verdict)
            self.assertNotIn("shadow_side", verdict)

        off = self._orchestrator(entry_regime_gate="off", entry_gate_direction="any")
        off.set_gate_context({"kondisi": "TURUN", "allowed_direction": "short"})
        self.assertEqual(off._stretch_gate_verdict(self._decision("OPEN_LONG"), context, None)["verdict"], "allow")

    def test_mapped_side_and_live_sides_follow_the_configured_direction(self) -> None:
        cases = [
            # (regime_gate, direction, allowed_direction, expected mapped side, expected live sides)
            ("map", "any", "short", "short", {"short"}),
            ("map", "short", "short", "short", {"short"}),
            ("map", "short", "long", "long", set()),
            ("map", "long", "long", "long", {"long"}),
            ("map", "any", "", "", set()),
            ("naik", "long", "long", "", {"long"}),
            ("off", "any", "short", "", {"long", "short"}),
            ("off", "short", "", "", {"short"}),
        ]
        for gate, direction, allowed, mapped, live in cases:
            with self.subTest(gate=gate, direction=direction, allowed=allowed):
                orchestrator = self._orchestrator(entry_regime_gate=gate, entry_gate_direction=direction)
                orchestrator.set_gate_context({"kondisi": "X", "allowed_direction": allowed})

                self.assertEqual(orchestrator._mapped_side(), mapped)
                self.assertEqual(orchestrator._live_sides(), frozenset(live))

    def test_bucket_and_side_use_the_same_gate_context(self) -> None:
        orchestrator = self._mapped("TURUN", "short", entry_gate_direction="short")
        context = {
            "technicals": [
                {"symbol": "BTC/USDT", "last_price": 100.0, "ema_21": 100.02, "atr_14": 0.25, "rsi_14": 40.0}
            ],
            "factor_snapshots": [],
            "liquidation_constraints": {"BTC/USDT": {"status": "available", "max_long_stop_loss_pct": 0.5,
                                                     "max_short_stop_loss_pct": 0.5}},
        }
        decision = self._decision("OPEN_SHORT")
        verdict = orchestrator._stretch_gate_verdict(decision, context, None)
        candidate = orchestrator._build_entry_candidate(decision, context, verdict)
        self.assertIsNotNone(candidate)

        plan = asyncio.run(orchestrator._candidate_to_trade_plan(candidate, context))

        setup = plan.factor_metadata["setup"]
        self.assertEqual(verdict["mapped_side"], "short")
        self.assertEqual(setup["kondisi"], "TURUN")
        self.assertEqual(setup["atr_regime"], "atr_mid")
        self.assertEqual(setup["bucket_key"], "SHORT|TURUN|atr_mid")

    def test_setup_has_no_bucket_without_a_gate_context(self) -> None:
        meta = AIOrchestrator._factor_metadata_for_symbol(
            {"technicals": [{"symbol": "BTC/USDT", "last_price": 100.4, "ema_21": 100.0, "atr_14": 0.25}],
             "factor_snapshots": []},
            "BTC/USDT",
            "LONG",
        )
        self.assertEqual(meta["setup"]["kondisi"], "")
        self.assertEqual(meta["setup"]["bucket_key"], "")
        # the pre-existing keys are untouched
        self.assertEqual(meta["setup"]["setup_key"], "LONG|stretch_up|atr_mid")


class _LedgerStore:
    """Trade store fake for the entry path with the shadow and ledger methods (Task 4).

    Records every ledger read, every shadow row and every spacing seed read; `read_error` /
    `write_error` / `seed_error` make the corresponding calls raise. The seed read answers from the
    rows written to this store, as the real table would after a restart."""

    def __init__(
        self,
        trades: list[dict] | None = None,
        shadows: list[dict] | None = None,
        *,
        read_error: Exception | None = None,
        write_error: Exception | None = None,
        seed_error: Exception | None = None,
    ) -> None:
        self.trades = trades or []
        self.shadows = shadows or []
        self.read_error = read_error
        self.write_error = write_error
        self.seed_error = seed_error
        self.decisions: list[dict] = []
        self.shadow_rows: list[dict] = []
        self.ledger_reads: list[tuple] = []
        self.seed_reads: list[tuple] = []

    async def recent_performance_summary(self, limit: int = 50) -> dict:
        return {"sample_size": 0}

    async def recent_closed_trade_records(self, limit: int = 100) -> list[dict]:
        return []

    async def total_volume_for_day(self, day=None) -> float:
        return 0.0

    async def save_ai_decision(self, **kwargs) -> None:
        self.decisions.append(kwargs)

    async def closed_trade_rows_since(self, since) -> list[dict]:
        self.ledger_reads.append(("trades", since))
        if self.read_error is not None:
            raise self.read_error
        return self.trades

    async def shadow_outcomes_since(self, since, source=None) -> list[dict]:
        self.ledger_reads.append(("shadows", since, source))
        if self.read_error is not None:
            raise self.read_error
        return [row for row in self.shadows if source is None or row.get("source") == source]

    async def save_shadow_entry(self, row: dict) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.shadow_rows.append(row)

    async def latest_shadow_decided_at(self, symbol: str, side: str, source: str, bucket_key: str):
        self.seed_reads.append((symbol, side, source, bucket_key))
        if self.seed_error is not None:
            raise self.seed_error
        key = (symbol, side, source, bucket_key)
        times = [
            row["decided_at"]
            for row in self.shadow_rows
            if (row["symbol"], row["side"], row["source"], row["bucket_key"]) == key
        ]
        return max(times, default=None)


class LedgerAndShadowTests(unittest.IsolatedAsyncioTestCase):
    """A shadow side and, under `enforce`, a blocked ledger bucket are recorded but never traded.

    Under `ENTRY_REGIME_GATE=map` a mapped side that `ENTRY_GATE_DIRECTION` does not make live still
    goes through every entry check; a candidate that clears them becomes a `shadow_side` row and the
    tick holds. The setup ledger files every OPEN decision under `side|condition|atr_regime` and
    records its verdict at `payload_json.gate.ledger`; `enforce` turns a `blocked` bucket into a
    `ledger_block` row, `shadow` only records it. Defaults (ledger off, gate not `map`) touch none of
    the shadow or ledger store methods.
    """

    SINCE = datetime(2026, 10, 21, 0, 0, tzinfo=timezone.utc)
    NEAR_MEAN = 100.0     # stretch 0 ATR for either side
    STRETCHED = 99.5      # (100 - 99.5) / 0.25 = 2 ATR above the mean
    BUCKET = "SHORT|TURUN|atr_mid"

    def _settings(self, **overrides) -> SimpleNamespace:
        data = dict(vars(_volume_run_settings()))
        data.update(
            entry_regime_gate="map",
            entry_gate_direction="short",
            entry_stretch_gate_mode="enforce",
            entry_stretch_gate_max_atr=0.75,
            entry_stretch_gate_behind_schedule_entries_per_hour=0.0,
            setup_ledger_mode="off",
            setup_ledger_since=self.SINCE,
            setup_ledger_min_samples=3,
            setup_ledger_hurdle_bps=4.0,
            volume_neutral_exit_min_net_usdt=18.0,
            position_hold_minutes=60,
            cooldown_minutes_after_entry=10,
            position_usdt=180.0,
            volume_sides_per_entry=2,
            trading_timezone="Asia/Jakarta",
            ai_recent_performance_limit=12,
            ai_trade_memory_limit=24,
            exchange_name="bingx",
            volume_profile=SimpleNamespace(
                margin_per_entry_usdt=180.0,
                volume_per_entry_usdt=36_000.0,
                daily_volume_usdt=1_000_000.0,
                as_log_fields=lambda: {"margin_per_entry_usdt": 180.0},
            ),
        )
        data.update(overrides)
        return SimpleNamespace(**data)

    @staticmethod
    def _context(*, ema_21: float) -> dict:
        # atr_14 = 0.25 on a price of 100: atr_mid, an ATR-scaled stop of 0.375 % inside the 0.5 % limit.
        constraint = {"status": "available", "max_long_stop_loss_pct": 0.5, "max_short_stop_loss_pct": 0.5}
        return {
            "technicals": [
                {"symbol": "BTC/USDT", "last_price": 100.0, "ema_21": ema_21, "atr_14": 0.25, "rsi_14": 50.0},
                {"symbol": "ETH/USDT", "last_price": 100.0, "ema_21": 100.0, "atr_14": 0.25, "rsi_14": 50.0},
            ],
            "external_signals": {},
            "intelligence": {},
            "factor_snapshots": [],
            "active_flow_signals": [],
            "liquidation_constraints": {"BTC/USDT": dict(constraint), "ETH/USDT": dict(constraint)},
        }

    @staticmethod
    def _decision(action: str, symbol: str = "BTC/USDT") -> EntryDecision:
        return EntryDecision(
            symbol=symbol,
            action=action,
            confidence=0.8,
            market_trend="Bearish",
            stop_loss_pct=0.08,
            take_profit_pct=0.12,
            systemic_risk=2,
            reason="clean setup",
            risk_review=_approved_review() if action != "HOLD" else None,
        )

    def _orchestrator(
        self,
        store: _LedgerStore,
        decisions: list[EntryDecision],
        *,
        gate_context: dict | None = None,
        ema_21: float = NEAR_MEAN,
        context: dict | None = None,
        **overrides,
    ) -> AIOrchestrator:
        settings = self._settings(**overrides)
        context = context if context is not None else self._context(ema_21=ema_21)

        class Brain:
            last_call_metrics = None
            calls = 0

            async def entry_decisions(self, payload: dict):
                self.calls += 1
                return decisions

        class Intelligence:
            async def build_context(self) -> SimpleNamespace:
                return SimpleNamespace(to_dict=lambda: context)

        orchestrator = _bare_orchestrator()
        orchestrator._settings = settings
        orchestrator._logger = logging.getLogger("test.ledger")
        orchestrator._brain = Brain()
        orchestrator._trade_store = store
        orchestrator._supabase_journal = None
        orchestrator._intelligence = Intelligence()
        orchestrator._market_analyzer = SimpleNamespace(review=lambda ctx: SimpleNamespace(to_dict=lambda: {}))
        orchestrator._signal_designer = SimpleNamespace(review=lambda ctx: SimpleNamespace(to_dict=lambda: {}))
        orchestrator._compact_context = CompactDecisionContextBuilder(settings)
        if gate_context is not None:
            orchestrator.set_gate_context(gate_context)
        return orchestrator

    @staticmethod
    def _turun() -> dict:
        return {"kondisi": "TURUN", "allowed": True, "reason": "turun", "allowed_direction": "short"}

    @staticmethod
    def _naik() -> dict:
        return {"kondisi": "NAIK", "allowed": True, "reason": "naik", "allowed_direction": "long"}

    @staticmethod
    def _state() -> DailyRiskState:
        return DailyRiskState(
            day=date(2026, 10, 21),
            stop_losses=0,
            closed_trades=0,
            total_pnl_usdt=0.0,
            consecutive_losses=0,
            allowed_to_trade=True,
        )

    def _losing_shorts(self) -> list[dict]:
        # -10, -20 and -15 bps: mean -15, SE 2.89, upper bound -11.3 < 4 -> blocked at n = 3.
        return [
            {
                "direction": "SHORT",
                "entry_price": 100.0,
                "exit_price": exit_price,
                "factor_metadata": {"setup": {"bucket_key": self.BUCKET}},
            }
            for exit_price in (100.1, 100.2, 100.15)
        ]

    def _block_row(self, gross: float | None, decided_at: datetime) -> dict:
        return {"source": "ledger_block", "bucket_key": self.BUCKET, "gross_bps": gross, "decided_at": decided_at.isoformat()}

    @staticmethod
    def _gate(store: _LedgerStore, symbol: str = "BTC/USDT") -> dict:
        return next(item["payload"]["gate"] for item in store.decisions if item["symbol"] == symbol)

    async def test_a_shadow_side_candidate_never_becomes_a_plan(self) -> None:
        store = _LedgerStore()
        orchestrator = self._orchestrator(store, [self._decision("OPEN_LONG")], gate_context=self._naik())

        plan = await orchestrator.next_trade_plan(self._state())

        self.assertIsNone(plan)
        self.assertEqual(len(store.shadow_rows), 1)
        row = store.shadow_rows[0]
        self.assertEqual(row["source"], "shadow_side")
        self.assertEqual(
            (row["symbol"], row["side"], row["bucket_key"], row["hold_minutes"]),
            ("BTC/USDT", "long", "LONG|NAIK|atr_mid", 60),
        )
        self.assertEqual(row["ref_price"], 100.0)
        # the candidate's ATR-scaled stop, and the neutral floor 100 x 18 / (180 x 100) + 0.10 below the TP
        self.assertAlmostEqual(row["stop_loss_pct"], 0.375)
        self.assertAlmostEqual(row["floor_pct"], 0.2)
        self.assertIsNotNone(row["decided_at"].tzinfo)
        self.assertLess(abs((datetime.now(timezone.utc) - row["decided_at"]).total_seconds()), 60)
        self.assertNotIn("exchange", row)
        self.assertIs(self._gate(store)["shadow_side"], True)
        # the ledger is off: nothing read, nothing recorded
        self.assertEqual(store.ledger_reads, [])
        self.assertNotIn("ledger", self._gate(store))

    async def test_a_stretched_shadow_side_writes_no_row(self) -> None:
        store = _LedgerStore()
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_LONG")], gate_context=self._naik(), ema_21=self.STRETCHED
        )

        plan = await orchestrator.next_trade_plan(self._state())

        self.assertIsNone(plan)
        self.assertEqual(store.shadow_rows, [])
        gate = self._gate(store)
        self.assertEqual((gate["verdict"], gate["reason"], gate["shadow_side"]), ("block", "stretch", True))

    async def test_ledger_off_reads_nothing_and_records_nothing(self) -> None:
        store = _LedgerStore(self._losing_shorts())
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_SHORT")], entry_regime_gate="off", entry_gate_direction="any",
            entry_stretch_gate_mode="off",
        )

        plan = await orchestrator.next_trade_plan(self._state())

        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, PositionDirection.SHORT)
        self.assertEqual(store.ledger_reads, [])
        self.assertEqual(store.shadow_rows, [])
        # the stored gate dict is exactly today's verdict
        expected = orchestrator._stretch_gate_verdict(
            self._decision("OPEN_SHORT"), self._context(ema_21=self.NEAR_MEAN), None
        )
        self.assertEqual(self._gate(store), expected)
        self.assertNotIn("ledger", self._gate(store))

    async def test_ledger_shadow_records_the_status_and_trades(self) -> None:
        store = _LedgerStore(self._losing_shorts())
        orchestrator = self._orchestrator(
            store,
            [self._decision("OPEN_SHORT"), self._decision("HOLD", symbol="ETH/USDT")],
            gate_context=self._turun(),
            setup_ledger_mode="shadow",
        )

        with self.assertLogs("test.ledger", level="INFO") as logs:
            plan = await orchestrator.next_trade_plan(self._state())

        self.assertIsNotNone(plan)
        self.assertEqual((plan.symbol, plan.direction), ("BTC/USDT", PositionDirection.SHORT))
        self.assertEqual(store.shadow_rows, [])
        self.assertEqual(store.ledger_reads, [("trades", self.SINCE), ("shadows", self.SINCE, "ledger_block")])
        ledger = self._gate(store)["ledger"]
        self.assertEqual(
            (ledger["mode"], ledger["bucket"], ledger["n"], ledger["status"]),
            ("shadow", self.BUCKET, 3, "blocked"),
        )
        self.assertAlmostEqual(ledger["mean_bps"], -15.0)
        self.assertLess(ledger["upper_bps"], 4.0)
        self.assertEqual(
            set(ledger),
            {
                "mode", "bucket", "n", "mean_bps", "upper_bps", "status",
                "shadow_run_n", "shadow_run_upper_bps", "since_block_n", "since_block_upper_bps",
            },
        )
        # no ledger_block row yet: both episode samples are empty
        episode = ("shadow_run_n", "shadow_run_upper_bps", "since_block_n", "since_block_upper_bps")
        self.assertEqual(tuple(ledger[key] for key in episode), (0, None, 0, None))
        self.assertIn("Setup ledger (shadow) would block BTC/USDT", "\n".join(logs.output))
        # a HOLD decision gets no ledger verdict
        self.assertNotIn("ledger", self._gate(store, "ETH/USDT"))

    async def test_ledger_enforce_turns_a_blocked_bucket_into_hold_and_a_ledger_block_row(self) -> None:
        store = _LedgerStore(self._losing_shorts())
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode="enforce"
        )

        plan = await orchestrator.next_trade_plan(self._state())

        self.assertIsNone(plan)
        self.assertEqual(len(store.shadow_rows), 1)
        row = store.shadow_rows[0]
        self.assertEqual(
            (row["source"], row["side"], row["bucket_key"], row["symbol"]),
            ("ledger_block", "short", self.BUCKET, "BTC/USDT"),
        )
        self.assertAlmostEqual(row["stop_loss_pct"], 0.375)
        self.assertAlmostEqual(row["floor_pct"], 0.2)
        self.assertEqual(self._gate(store)["ledger"]["status"], "blocked")
        self.assertEqual(self._gate(store)["ledger"]["mode"], "enforce")

    async def test_ledger_enforce_lets_a_reopened_bucket_trade(self) -> None:
        shadows = [self._block_row(value, datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc)) for value in (20.0, 25.0, 30.0)]
        store = _LedgerStore(self._losing_shorts(), shadows)
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode="enforce"
        )

        plan = await orchestrator.next_trade_plan(self._state())

        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, PositionDirection.SHORT)
        self.assertEqual(store.shadow_rows, [])
        self.assertEqual(self._gate(store)["ledger"]["status"], "reopened")

    async def test_ledger_enforce_blocks_a_reopened_bucket_that_loses_again(self) -> None:
        # Blocked on three losing SHORTs, reopened by three good ledger_block outcomes decided at 05:00; the
        # SHORTs opened after that last block row are judged again, and three more losers block it again.
        block_at = datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc)
        before = [dict(row, opened_at="2026-10-21T01:00:00+00:00") for row in self._losing_shorts()]
        shadows = [
            {"source": "ledger_block", "bucket_key": self.BUCKET, "gross_bps": value, "decided_at": block_at.isoformat()}
            for value in (20.0, 25.0, 30.0)
        ]
        for new_trades, status in ((3, "blocked"), (2, "reopened")):
            with self.subTest(new_trades=new_trades):
                after = [
                    dict(row, opened_at=(block_at + timedelta(minutes=10 * (i + 1))).isoformat())
                    for i, row in enumerate(self._losing_shorts()[:new_trades])
                ]
                store = _LedgerStore(before + after, shadows)
                orchestrator = self._orchestrator(
                    store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode="enforce"
                )

                plan = await orchestrator.next_trade_plan(self._state())

                self.assertEqual(self._gate(store)["ledger"]["status"], status)
                self.assertEqual(plan is None, status == "blocked")
                self.assertEqual(
                    [row["source"] for row in store.shadow_rows], ["ledger_block"] if status == "blocked" else []
                )

    async def test_ledger_enforce_keeps_a_re_blocked_bucket_blocked_until_a_fresh_run_reopens_it(self) -> None:
        # Blocked on three losers, reopened by three good ledger_block outcomes decided at 05:00, blocked again by
        # three losers opened after that. Its next block rows start a new run: the old three no longer count, an
        # expired row only marks time, and it takes three fresh good outcomes to reopen it.
        block_at = datetime(2026, 10, 21, 5, 0, tzinfo=timezone.utc)
        re_blocked_at = block_at + timedelta(hours=2)
        before = [dict(row, opened_at="2026-10-21T01:00:00+00:00") for row in self._losing_shorts()]
        after = [
            dict(row, opened_at=(block_at + timedelta(minutes=10 * (i + 1))).isoformat())
            for i, row in enumerate(self._losing_shorts())
        ]
        first_run = [self._block_row(value, block_at) for value in (20.0, 25.0, 30.0)]
        for new_values, status in (([None], "blocked"), ([20.0], "blocked"), ([20.0, 25.0, 30.0], "reopened")):
            with self.subTest(new_values=new_values):
                new_run = [
                    self._block_row(value, re_blocked_at + timedelta(minutes=70 * i)) for i, value in enumerate(new_values)
                ]
                store = _LedgerStore(before + after, first_run + new_run)
                orchestrator = self._orchestrator(
                    store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode="enforce"
                )

                plan = await orchestrator.next_trade_plan(self._state())

                ledger = self._gate(store)["ledger"]
                self.assertEqual(ledger["status"], status)
                self.assertEqual(plan is None, status == "blocked")
                self.assertEqual(
                    [row["source"] for row in store.shadow_rows], ["ledger_block"] if status == "blocked" else []
                )
                # the decision row says why: the fresh run's size, and no real trade since it yet
                finite = [value for value in new_values if value is not None]
                self.assertEqual((ledger["shadow_run_n"], ledger["since_block_n"]), (len(finite), 0))
                self.assertEqual(ledger["shadow_run_upper_bps"] is None, len(finite) < 2)

    async def test_a_ledger_read_failure_reuses_the_last_snapshot(self) -> None:
        # A good read files the bucket as blocked; a failed read on a later tick (past the cache age, so the
        # ledger is read again) keeps that snapshot, so a transient store error lets no entry into a blocked
        # bucket (whose trade would also end its run).
        store = _LedgerStore(self._losing_shorts())
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode="enforce"
        )
        self.assertIsNone(await self._tick_at(orchestrator, self.T0))
        store.read_error = RuntimeError("supabase down")

        with self.assertLogs("test.ledger", level="WARNING") as logs:
            plan = await self._tick_at(orchestrator, self.T0 + timedelta(minutes=10))

        self.assertIsNone(plan)
        self.assertIn(
            "Setup ledger could not be read (RuntimeError: supabase down); the last snapshot is used this tick.",
            "\n".join(logs.output),
        )
        ledger = store.decisions[-1]["payload"]["gate"]["ledger"]
        self.assertEqual((ledger["status"], ledger["n"]), ("blocked", 3))

    async def test_ledger_is_reused_within_the_cache_age(self) -> None:
        store = _LedgerStore(self._losing_shorts())
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode="shadow"
        )
        one_read = [("trades", self.SINCE), ("shadows", self.SINCE, "ledger_block")]

        await self._tick_at(orchestrator, self.T0)
        await self._tick_at(orchestrator, self.T0 + timedelta(seconds=299))
        self.assertEqual(store.ledger_reads, one_read)
        # both ticks still carry the cached verdict
        self.assertEqual([item["payload"]["gate"]["ledger"]["status"] for item in store.decisions], ["blocked"] * 2)

        # at 300 s the table is read again
        await self._tick_at(orchestrator, self.T0 + timedelta(seconds=300))
        self.assertEqual(store.ledger_reads, one_read * 2)

    async def test_a_failed_read_reuses_the_last_table(self) -> None:
        # Whatever its age: hours of failed reads keep the last good table, one WARNING per failed read.
        store = _LedgerStore(self._losing_shorts())
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode="enforce"
        )
        self.assertIsNone(await self._tick_at(orchestrator, self.T0))
        store.read_error = RuntimeError("supabase down")

        for hours in (1, 2, 3):
            with self.subTest(hours=hours):
                with self.assertLogs("test.ledger", level="WARNING") as logs:
                    plan = await self._tick_at(orchestrator, self.T0 + timedelta(hours=hours))

                self.assertIsNone(plan)
                failures = [record for record in logs.records if "could not be read" in record.getMessage()]
                self.assertEqual([record.levelno for record in failures], [logging.WARNING])
                self.assertIn("the last snapshot is used this tick", failures[0].getMessage())
                ledger = store.decisions[-1]["payload"]["gate"]["ledger"]
                self.assertEqual((ledger["status"], ledger["n"]), ("blocked", 3))

        # a failed read is never cached: the next tick reads again, and a good read replaces the table
        store.read_error = None
        store.trades = []
        await self._tick_at(orchestrator, self.T0 + timedelta(hours=3, seconds=1))
        self.assertEqual(store.decisions[-1]["payload"]["gate"]["ledger"]["status"], "learning")

    async def test_a_failed_first_read_falls_open_with_an_error_under_enforce(self) -> None:
        for mode, level in (("enforce", logging.ERROR), ("shadow", logging.WARNING)):
            with self.subTest(mode=mode):
                store = _LedgerStore(self._losing_shorts(), read_error=RuntimeError("supabase down"))
                orchestrator = self._orchestrator(
                    store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode=mode
                )

                with self.assertLogs("test.ledger", level="WARNING") as logs:
                    plan = await self._tick_at(orchestrator, self.T0)

                # no table was ever read: every bucket reads learning and the SHORT trades
                self.assertIsNotNone(plan)
                failures = [record for record in logs.records if "could not be read" in record.getMessage()]
                self.assertEqual([record.levelno for record in failures], [level])
                self.assertIn("every bucket reads as learning this tick", failures[0].getMessage())
                self.assertEqual(store.decisions[-1]["payload"]["gate"]["ledger"]["status"], "learning")

    async def test_a_ledger_read_failure_is_logged_and_treated_as_learning(self) -> None:
        store = _LedgerStore(self._losing_shorts(), read_error=RuntimeError("supabase down"))
        orchestrator = self._orchestrator(
            store,
            # the ETH LONG is the wrong side in TURUN: the regime gate still applies
            [self._decision("OPEN_SHORT"), self._decision("OPEN_LONG", symbol="ETH/USDT")],
            gate_context=self._turun(),
            setup_ledger_mode="enforce",
        )

        with self.assertLogs("test.ledger", level="WARNING") as logs:
            plan = await orchestrator.next_trade_plan(self._state())

        self.assertIn("Setup ledger could not be read (RuntimeError: supabase down)", "\n".join(logs.output))
        self.assertIsNotNone(plan)
        self.assertEqual((plan.symbol, plan.direction), ("BTC/USDT", PositionDirection.SHORT))
        self.assertEqual(store.shadow_rows, [])
        ledger = self._gate(store)["ledger"]
        self.assertEqual((ledger["status"], ledger["n"], ledger["mean_bps"]), ("learning", 0, None))
        eth = self._gate(store, "ETH/USDT")
        self.assertEqual((eth["verdict"], eth["reason"]), ("block", "direction"))
        self.assertEqual(eth["ledger"]["status"], "learning")

    async def test_a_ledger_read_failure_still_lets_the_stretch_gate_block(self) -> None:
        store = _LedgerStore(read_error=RuntimeError("supabase down"))
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode="enforce",
            ema_21=200.0 - self.STRETCHED,   # price 2 ATR below the mean
        )

        with self.assertLogs("test.ledger", level="WARNING") as logs:
            plan = await orchestrator.next_trade_plan(self._state())

        self.assertIn("Setup ledger could not be read", "\n".join(logs.output))
        self.assertIsNone(plan)
        self.assertEqual(store.shadow_rows, [])
        gate = self._gate(store)
        self.assertEqual((gate["verdict"], gate["reason"]), ("block", "stretch"))

    async def test_a_failed_shadow_write_still_drops_the_candidate(self) -> None:
        for gate_context, options in (
            (self._naik(), {}),
            (self._turun(), {"setup_ledger_mode": "enforce"}),
        ):
            with self.subTest(kondisi=gate_context["kondisi"]):
                store = _LedgerStore(self._losing_shorts(), write_error=RuntimeError("insert refused"))
                action = "OPEN_LONG" if gate_context["kondisi"] == "NAIK" else "OPEN_SHORT"
                orchestrator = self._orchestrator(
                    store, [self._decision(action)], gate_context=gate_context, **options
                )

                with self.assertLogs("test.ledger", level="WARNING") as logs:
                    plan = await orchestrator.next_trade_plan(self._state())

                self.assertIsNone(plan)
                self.assertIn("insert refused", "\n".join(logs.output))

    async def test_a_shadow_side_in_an_enforce_blocked_bucket_writes_one_shadow_side_row(self) -> None:
        losing_longs = [
            {
                "direction": "LONG",
                "entry_price": 100.0,
                "exit_price": exit_price,
                "factor_metadata": {"setup": {"bucket_key": "LONG|NAIK|atr_mid"}},
            }
            for exit_price in (99.9, 99.8, 99.85)
        ]
        store = _LedgerStore(losing_longs)
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_LONG")], gate_context=self._naik(), setup_ledger_mode="enforce"
        )

        plan = await orchestrator.next_trade_plan(self._state())

        self.assertIsNone(plan)
        gate = self._gate(store)
        self.assertIs(gate["shadow_side"], True)
        self.assertEqual((gate["ledger"]["status"], gate["ledger"]["mode"]), ("blocked", "enforce"))
        # one row, filed as the shadow side it is, not as a ledger block
        self.assertEqual([row["source"] for row in store.shadow_rows], ["shadow_side"])

    async def test_the_best_scored_candidate_is_dropped_and_a_lower_scored_live_one_trades(self) -> None:
        # BTC SHORT is in the blocked SHORT|TURUN|atr_mid bucket; ETH SHORT, with ATR 0.1 %, files under atr_low.
        context = self._context(ema_21=self.NEAR_MEAN)
        context["technicals"][1] = {
            "symbol": "ETH/USDT", "last_price": 100.0, "ema_21": 100.0, "atr_14": 0.1, "rsi_14": 50.0,
        }
        btc = self._decision("OPEN_SHORT").model_copy(update={"confidence": 0.95})
        eth = self._decision("OPEN_SHORT", symbol="ETH/USDT")
        store = _LedgerStore(self._losing_shorts())
        orchestrator = self._orchestrator(
            store, [btc, eth], gate_context=self._turun(), context=context, setup_ledger_mode="enforce"
        )
        scores = {
            decision.symbol: orchestrator._build_entry_candidate(
                decision, context, orchestrator._stretch_gate_verdict(decision, context, None)
            ).score
            for decision in (btc, eth)
        }
        self.assertGreater(scores["BTC/USDT"], scores["ETH/USDT"])

        plan = await orchestrator.next_trade_plan(self._state())

        self.assertIsNotNone(plan)
        self.assertEqual((plan.symbol, plan.direction), ("ETH/USDT", PositionDirection.SHORT))
        self.assertEqual(
            [(row["symbol"], row["source"]) for row in store.shadow_rows], [("BTC/USDT", "ledger_block")]
        )
        self.assertEqual(self._gate(store, "ETH/USDT")["ledger"]["bucket"], "SHORT|TURUN|atr_low")
        self.assertEqual(self._gate(store, "ETH/USDT")["ledger"]["status"], "learning")

    # Spacing: hold 60 + cooldown after entry 10 = 70 minutes between two rows of one (symbol, side, source).
    T0 = datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc)

    async def _tick_at(self, orchestrator: AIOrchestrator, moment: datetime):
        with mock.patch("bot.ai.orchestration._utc_now", return_value=moment):
            return await orchestrator.next_trade_plan(self._state())

    async def test_a_second_shadow_row_within_the_spacing_is_skipped_and_still_not_traded(self) -> None:
        # A live side in an enforce-blocked bucket: the AI is still asked every tick (the hour may trade),
        # and the spacing is applied when the row would be written. A shadow-only hour skips the call
        # instead (test_a_shadow_only_hour_inside_the_spacing_makes_no_ai_call).
        store = _LedgerStore(self._losing_shorts())
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_SHORT")], gate_context=self._turun(), setup_ledger_mode="enforce"
        )

        self.assertIsNone(await self._tick_at(orchestrator, self.T0))
        with self.assertLogs("test.ledger", level="INFO") as logs:
            plan = await self._tick_at(orchestrator, self.T0 + timedelta(minutes=30))

        # the candidate is still dropped: spacing never lets a recorded candidate trade
        self.assertIsNone(plan)
        self.assertEqual([row["decided_at"] for row in store.shadow_rows], [self.T0])
        skips = [line for line in logs.output if "no shadow row" in line]
        self.assertEqual(len(skips), 1)
        self.assertIn("INFO", skips[0])
        self.assertIn("40.0 min", skips[0])   # 70 - 30 minutes remain
        self.assertIn(self.BUCKET, skips[0])
        # both decisions are still recorded as the model's own OPEN_SHORT
        self.assertEqual([item["action"] for item in store.decisions], ["OPEN_SHORT", "OPEN_SHORT"])
        self.assertEqual(orchestrator._brain.calls, 2)

        # one second short of the spacing still skips; at 70 minutes the next row is written
        self.assertIsNone(await self._tick_at(orchestrator, self.T0 + timedelta(minutes=70, seconds=-1)))
        self.assertIsNone(await self._tick_at(orchestrator, self.T0 + timedelta(minutes=70)))
        self.assertEqual(
            [row["decided_at"] for row in store.shadow_rows], [self.T0, self.T0 + timedelta(minutes=70)]
        )

    async def test_a_shadow_only_hour_inside_the_spacing_makes_no_ai_call(self) -> None:
        # NAIK maps LONG, and only SHORT is live: nothing this hour can trade, so once the shadow row is
        # written, an AI call inside its spacing could change nothing and is not made.
        store = _LedgerStore()
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_LONG")], gate_context=self._naik(), symbols=["BTC/USDT"]
        )

        self.assertIsNone(await self._tick_at(orchestrator, self.T0))
        with self.assertLogs("test.ledger", level="INFO") as logs:
            plan = await self._tick_at(orchestrator, self.T0 + timedelta(minutes=30))

        self.assertIsNone(plan)
        self.assertEqual(orchestrator._brain.calls, 1)
        self.assertEqual([item["action"] for item in store.decisions], ["OPEN_LONG"])   # no second decision
        self.assertEqual([row["decided_at"] for row in store.shadow_rows], [self.T0])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("INFO", logs.output[0])
        self.assertIn("without an AI call", logs.output[0])
        self.assertIn("LONG|NAIK|atr_mid", logs.output[0])
        self.assertIn("40.0 min", logs.output[0])   # 70 - 30 minutes remain

    async def test_a_shadow_only_hour_outside_the_spacing_still_calls_the_ai(self) -> None:
        store = _LedgerStore()
        orchestrator = self._orchestrator(store, [self._decision("OPEN_LONG")], gate_context=self._naik())

        # the first tick of an hour has no earlier row: the AI is asked
        self.assertIsNone(await self._tick_at(orchestrator, self.T0))
        self.assertEqual(orchestrator._brain.calls, 1)
        # BTC's row is inside its spacing but ETH has none yet, so a row could still be written: asked again
        self.assertIsNone(await self._tick_at(orchestrator, self.T0 + timedelta(minutes=30)))
        self.assertEqual(orchestrator._brain.calls, 2)
        # at the end of the spacing it is asked again, and the next row is written
        self.assertIsNone(await self._tick_at(orchestrator, self.T0 + timedelta(minutes=70)))
        self.assertEqual(orchestrator._brain.calls, 3)
        self.assertEqual(
            [row["decided_at"] for row in store.shadow_rows], [self.T0, self.T0 + timedelta(minutes=70)]
        )

        # a live hour is never skipped, whatever the spacing of the shadow side
        orchestrator._settings = self._settings(symbols=["BTC/USDT"])
        orchestrator.set_gate_context(self._turun())
        plan = await self._tick_at(orchestrator, self.T0 + timedelta(minutes=71))
        self.assertEqual(orchestrator._brain.calls, 4)
        self.assertIsNone(plan)   # the model answered OPEN_LONG, the wrong side in TURUN

    async def test_spacing_is_per_bucket(self) -> None:
        # One symbol, side and source, two ATR regimes: each bucket keeps its own spacing.
        store = _LedgerStore()
        mid = self._context(ema_21=self.NEAR_MEAN)
        low = self._context(ema_21=self.NEAR_MEAN)
        low["technicals"][0] = dict(low["technicals"][0], atr_14=0.1)   # 0.1 % ATR: atr_low
        contexts = [mid, low, mid, low]

        class Intelligence:
            async def build_context(self) -> SimpleNamespace:
                context = contexts.pop(0)
                return SimpleNamespace(to_dict=lambda: context)

        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_LONG")], gate_context=self._naik(), symbols=["BTC/USDT"]
        )
        orchestrator._intelligence = Intelligence()
        for minutes in (0, 1, 2, 3):
            await self._tick_at(orchestrator, self.T0 + timedelta(minutes=minutes))

        self.assertEqual(
            [(row["bucket_key"], row["decided_at"]) for row in store.shadow_rows],
            [("LONG|NAIK|atr_mid", self.T0), ("LONG|NAIK|atr_low", self.T0 + timedelta(minutes=1))],
        )
        # the third and fourth ticks found their own bucket inside its spacing and made no AI call
        self.assertEqual(orchestrator._brain.calls, 2)

    async def test_spacing_survives_a_restart_via_the_store_seed(self) -> None:
        store = _LedgerStore()
        options = dict(gate_context=self._naik(), symbols=["BTC/USDT"])
        before = self._orchestrator(store, [self._decision("OPEN_LONG")], **options)
        self.assertIsNone(await self._tick_at(before, self.T0))
        self.assertEqual(len(store.shadow_rows), 1)

        # a new process: nothing in memory, so the last row of the key is read from the store once
        store.seed_reads.clear()
        after = self._orchestrator(store, [self._decision("OPEN_LONG")], **options)
        for minutes in (30, 40):
            self.assertIsNone(await self._tick_at(after, self.T0 + timedelta(minutes=minutes)))
        self.assertEqual(after._brain.calls, 0)
        self.assertEqual(store.seed_reads, [("BTC/USDT", "long", "shadow_side", "LONG|NAIK|atr_mid")])
        self.assertEqual(len(store.shadow_rows), 1)

        self.assertIsNone(await self._tick_at(after, self.T0 + timedelta(minutes=70)))
        self.assertEqual(after._brain.calls, 1)
        self.assertEqual(
            [row["decided_at"] for row in store.shadow_rows], [self.T0, self.T0 + timedelta(minutes=70)]
        )
        self.assertEqual(len(store.seed_reads), 1)

    async def test_a_failed_seed_read_writes_the_row_and_warns(self) -> None:
        store = _LedgerStore(seed_error=RuntimeError("supabase down"))
        orchestrator = self._orchestrator(
            store, [self._decision("OPEN_LONG")], gate_context=self._naik(), symbols=["BTC/USDT"]
        )

        with self.assertLogs("test.ledger", level="WARNING") as logs:
            plan = await self._tick_at(orchestrator, self.T0)

        self.assertIsNone(plan)
        self.assertEqual(orchestrator._brain.calls, 1)
        self.assertEqual([row["decided_at"] for row in store.shadow_rows], [self.T0])
        self.assertTrue(any("RuntimeError: supabase down" in line for line in logs.output))
        self.assertTrue(all(record.levelno == logging.WARNING for record in logs.records))
        # the written row starts the spacing in memory; no further seed read is needed
        store.seed_error = None
        reads = len(store.seed_reads)
        self.assertIsNone(await self._tick_at(orchestrator, self.T0 + timedelta(minutes=5)))
        self.assertEqual((orchestrator._brain.calls, len(store.seed_reads)), (1, reads))

    async def test_spacing_is_per_symbol_side_and_source(self) -> None:
        losing_longs = [
            {
                "direction": "LONG",
                "entry_price": 100.0,
                "exit_price": exit_price,
                "factor_metadata": {"setup": {"bucket_key": "LONG|NAIK|atr_mid"}},
            }
            for exit_price in (99.9, 99.8, 99.85)
        ]
        store = _LedgerStore(losing_longs)
        orchestrator = self._orchestrator(
            store,
            [self._decision("OPEN_LONG"), self._decision("OPEN_LONG", symbol="ETH/USDT")],
            gate_context=self._naik(),
            setup_ledger_mode="enforce",
        )
        minute = timedelta(minutes=1)

        # BTC and ETH LONG shadow sides in one tick: two symbols, two rows
        await self._tick_at(orchestrator, self.T0)
        # the same BTC LONG, now live (ENTRY_GATE_DIRECTION=any) in its blocked bucket: another source, a row
        orchestrator._settings = self._settings(entry_gate_direction="any", setup_ledger_mode="enforce")
        orchestrator._brain.entry_decisions = self._answer([self._decision("OPEN_LONG")])
        await self._tick_at(orchestrator, self.T0 + minute)
        # a BTC SHORT shadow side in TURUN (only LONG live): another side, a row
        orchestrator._settings = self._settings(entry_gate_direction="long")
        orchestrator._brain.entry_decisions = self._answer([self._decision("OPEN_SHORT")])
        orchestrator.set_gate_context(self._turun())
        await self._tick_at(orchestrator, self.T0 + 2 * minute)
        # the same three keys again inside the spacing: no row
        orchestrator.set_gate_context(self._naik())
        orchestrator._settings = self._settings(setup_ledger_mode="enforce")
        orchestrator._brain.entry_decisions = self._answer(
            [self._decision("OPEN_LONG"), self._decision("OPEN_LONG", symbol="ETH/USDT")]
        )
        await self._tick_at(orchestrator, self.T0 + 3 * minute)

        self.assertEqual(
            [(row["symbol"], row["side"], row["source"]) for row in store.shadow_rows],
            [
                ("BTC/USDT", "long", "shadow_side"),
                ("ETH/USDT", "long", "shadow_side"),
                ("BTC/USDT", "long", "ledger_block"),
                ("BTC/USDT", "short", "shadow_side"),
            ],
        )

    async def test_a_failed_write_does_not_start_the_spacing(self) -> None:
        store = _LedgerStore(write_error=RuntimeError("insert refused"))
        orchestrator = self._orchestrator(store, [self._decision("OPEN_LONG")], gate_context=self._naik())

        with self.assertLogs("test.ledger", level="WARNING"):
            self.assertIsNone(await self._tick_at(orchestrator, self.T0))
        store.write_error = None
        self.assertIsNone(await self._tick_at(orchestrator, self.T0 + timedelta(minutes=1)))

        self.assertEqual([row["decided_at"] for row in store.shadow_rows], [self.T0 + timedelta(minutes=1)])

    def test_a_fresh_orchestrator_starts_with_no_spacing_state(self) -> None:
        # in memory only: a restart forgets the last rows, so the first shadow row after it is always written
        orchestrator = AIOrchestrator(
            self._settings(), SimpleNamespace(), SimpleNamespace(), _LedgerStore(), logging.getLogger("test.ledger")
        )
        self.assertEqual(orchestrator._shadow_rows_written_at, {})

    @staticmethod
    def _answer(decisions: list[EntryDecision]):
        async def entry_decisions(payload: dict):
            return decisions

        return entry_decisions

    def test_shadow_row_floor_is_capped_at_the_take_profit(self) -> None:
        orchestrator = self._orchestrator(
            _LedgerStore(), [], gate_context=self._naik(), volume_neutral_exit_min_net_usdt=200.0
        )
        candidate = EntryCandidate(
            decision=self._decision("OPEN_LONG"),
            direction=PositionDirection.LONG,
            target=ResolvedTradeTargets(
                stop_loss_pct=0.3, take_profit_pct=0.5, atr_pct=0.25, fee_adjusted_rr=1.0, round_trip_fee_pct=0.1
            ),
            score=1.0,
        )

        row = orchestrator._shadow_row(
            candidate.decision, candidate, "shadow_side", self._context(ema_21=self.NEAR_MEAN)
        )

        # 100 x 200 / 18,000 + 0.10 = 1.21 % is beyond the TP, so the floor is the TP
        self.assertEqual(row["floor_pct"], 0.5)
        self.assertEqual(row["stop_loss_pct"], 0.3)

        # below the TP the floor carries the candidate's own round-trip fee (0.07 here), not the setting's 0.10
        cheap = EntryCandidate(
            decision=candidate.decision,
            direction=PositionDirection.LONG,
            target=ResolvedTradeTargets(
                stop_loss_pct=0.3, take_profit_pct=5.0, atr_pct=0.25, fee_adjusted_rr=1.0, round_trip_fee_pct=0.07
            ),
            score=1.0,
        )
        floor = orchestrator._shadow_row(
            cheap.decision, cheap, "shadow_side", self._context(ema_21=self.NEAR_MEAN)
        )["floor_pct"]
        self.assertAlmostEqual(floor, 100.0 * 200.0 / 18_000.0 + 0.07)
        with self.assertRaises(ValueError):
            orchestrator._shadow_row(candidate.decision, candidate, "shadow_side", {"technicals": []})


class _PayloadStore(_LedgerStore):
    """`_LedgerStore` that also records the trade-history reads only the legacy payload needs."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.memory_reads: list[str] = []

    async def recent_performance_summary(self, limit: int = 50) -> dict:
        self.memory_reads.append("performance")
        return {"sample_size": 0}

    async def recent_closed_trade_records(self, limit: int = 100) -> list[dict]:
        self.memory_reads.append("records")
        return []


class LeanEntryPayloadTests(unittest.IsolatedAsyncioTestCase):
    """`AI_ENTRY_PAYLOAD=lean` changes what the model is sent, never what the code enforces.

    The request offers only the sides that may be decided this tick (the mapped side under `map`,
    shadow included; the live sides otherwise), states the stretch rule with the stretch the gate
    will measure, shows the regime verdict and, with the ledger enforcing, each side's bucket status. With
    no side to offer there is no AI call at all. Every stored decision names its payload profile.
    """

    SINCE = LedgerAndShadowTests.SINCE
    BUCKET = LedgerAndShadowTests.BUCKET
    # The ledger tests' settings (map, SHORT live, enforce, max 0.75 ATR, ledger off) and losing SHORTs.
    _settings = LedgerAndShadowTests._settings
    _losing_shorts = LedgerAndShadowTests._losing_shorts
    LEAN_GOAL = (
        "Open a position only when the setup shows a directional edge larger than the round-trip fee; "
        "HOLD is the expected answer most ticks."
    )

    @staticmethod
    def _context(*, ema_21: float = 100.0) -> dict:
        context = LedgerAndShadowTests._context(ema_21=ema_21)
        context["lean_features"] = {
            "BTC/USDT": {"bar_interval": "3m", "stretch_atr": 0.4, "rsi_14": 50.0, "atr_pct": 0.25},
        }
        return context

    def _orchestrator(
        self,
        store: _LedgerStore,
        decisions: list[EntryDecision],
        *,
        gate_context: dict | None = None,
        context: dict | None = None,
        **overrides,
    ) -> tuple[AIOrchestrator, list[dict], list[str]]:
        """The orchestrator, the payloads its brain was sent, and its context builds."""
        settings = self._settings(**overrides)
        context = context if context is not None else self._context()
        payloads: list[dict] = []
        builds: list[str] = []

        class Brain:
            last_call_metrics = None

            def __init__(self) -> None:
                self.lean_flags: list[bool] = []

            async def entry_decisions(self, payload: dict, *, lean: bool = False):
                payloads.append(payload)
                self.lean_flags.append(lean)
                return decisions

        class Intelligence:
            async def build_context(self) -> SimpleNamespace:
                builds.append("context")
                return SimpleNamespace(to_dict=lambda: context)

        orchestrator = _bare_orchestrator()
        orchestrator._settings = settings
        orchestrator._logger = logging.getLogger("test.lean")
        orchestrator._brain = Brain()
        orchestrator._trade_store = store
        orchestrator._supabase_journal = None
        orchestrator._intelligence = Intelligence()
        orchestrator._market_analyzer = SimpleNamespace(review=lambda ctx: SimpleNamespace(to_dict=lambda: {"role": "MarketAnalyzer"}))
        orchestrator._signal_designer = SimpleNamespace(review=lambda ctx: SimpleNamespace(to_dict=lambda: {"role": "SignalDesigner"}))
        orchestrator._compact_context = CompactDecisionContextBuilder(settings)
        if gate_context is not None:
            orchestrator.set_gate_context(gate_context)
        return orchestrator, payloads, builds

    @staticmethod
    def _turun(**numbers) -> dict:
        return {**LedgerAndShadowTests._turun(), **numbers}

    async def test_lean_profile_is_recorded_and_legacy_is_default(self) -> None:
        for name, overrides in (("unset", {}), ("legacy", {"ai_entry_payload": "legacy"})):
            with self.subTest(name):
                store = _PayloadStore()
                orchestrator, payloads, _ = self._orchestrator(
                    store, [LedgerAndShadowTests._decision("OPEN_SHORT")], gate_context=self._turun(), **overrides
                )
                plan = await orchestrator.next_trade_plan(LedgerAndShadowTests._state())

                self.assertIsNotNone(plan)
                self.assertEqual(store.decisions[0]["payload"]["payload_profile"], "legacy")
                request = payloads[0]
                self.assertIs(store.decisions[0]["payload"]["request"], request)
                self.assertEqual([review["role"] for review in request["arena_reviews"]], ["MarketAnalyzer", "SignalDesigner"])
                self.assertIn("learned_trade_memory", request)
                self.assertEqual(request["risk_constraints"]["min_confidence"], 0.75)
                self.assertEqual(request["output_rules"]["allowed_actions"], ["OPEN_LONG", "OPEN_SHORT", "HOLD"])
                self.assertNotIn("features", request)
                self.assertNotIn("payload_profile", request)
                self.assertEqual(store.memory_reads, ["performance", "records"])
                # the legacy request goes out under the legacy system prompt
                self.assertEqual(orchestrator._brain.lean_flags, [False])

        store = _PayloadStore()
        orchestrator, payloads, _ = self._orchestrator(
            store, [LedgerAndShadowTests._decision("OPEN_SHORT")], gate_context=self._turun(), ai_entry_payload="lean"
        )
        plan = await orchestrator.next_trade_plan(LedgerAndShadowTests._state())

        # lean changes the request, not the outcome: the same SHORT still trades
        self.assertIsNotNone(plan)
        self.assertEqual(plan.direction, PositionDirection.SHORT)
        self.assertEqual(store.decisions[0]["payload"]["payload_profile"], "lean")
        request = payloads[0]
        self.assertIs(store.decisions[0]["payload"]["request"], request)
        for key in ("arena_reviews", "learned_trade_memory", "recent_executed_trade_performance", "payload_profile"):
            self.assertNotIn(key, request)
        self.assertNotIn("min_confidence", request["risk_constraints"])
        self.assertEqual(request["features"], self._context()["lean_features"])
        # the trade history fed only the legacy payload, so lean does not read it
        self.assertEqual(store.memory_reads, [])
        # and the lean request goes out under the lean system prompt
        self.assertEqual(orchestrator._brain.lean_flags, [True])

    async def test_the_legacy_request_is_the_builder_s_output_byte_for_byte(self) -> None:
        # The golden pins the builder; this pins the orchestrator's legacy path into it: the same
        # trade history, memory, objective and arena reviews, in the same bytes the brain is sent.
        performance = {"total_trades": 2, "total_pnl_usdt": -2.25}
        records = [
            {
                "symbol": "BTC/USDT", "direction": "SHORT", "pnl_usdt": -6.5, "reason": "stop_loss", "confidence": 0.78,
                "factor_metadata": {"setup": {"setup_key": "SHORT|stretch_up|atr_mid", "family_key": "SHORT|trend_led"}},
            },
            {"symbol": "BTC/USDT", "direction": "LONG", "pnl_usdt": 4.25, "reason": "neutral_exit", "confidence": 0.81},
        ]

        class Store(_PayloadStore):
            async def recent_performance_summary(self, limit: int = 50) -> dict:
                return performance

            async def recent_closed_trade_records(self, limit: int = 100) -> list[dict]:
                return records

        flow = {"symbol": "BTC/USDT", "name": "volume_spike", "direction": "SHORT", "strength": 0.7}
        factor = {"symbol": "BTC/USDT", "name": "trend_ema", "category": "trend", "score": -0.6, "direction": "SHORT"}
        context = self._context()
        context.update(
            external_signals={"dune": {"saved_queries": {}, "builtin_signals": {}}, "tradingview": {"summary": "SELL"}},
            intelligence={
                "regime": "normal",
                "risk_flags": ["coinmarketcap_unavailable"],
                "symbol_scores": {"BTC/USDT": {"composite_score": -0.4}},
                "factor_intelligence": {"snapshots": [factor], "top_factors": ["trend_ema"]},
                "market_flow_signals": {"active": [flow], "new_triggers": [], "trigger_names": ["volume_spike"]},
            },
            factor_snapshots=[factor],
            active_flow_signals=[flow],
        )
        state = LedgerAndShadowTests._state()
        orchestrator, payloads, _ = self._orchestrator(
            Store(), [LedgerAndShadowTests._decision("HOLD")], gate_context=self._turun(), context=context
        )
        orchestrator._market_analyzer = MarketAnalyzer()
        orchestrator._signal_designer = SignalDesigner()

        await orchestrator.next_trade_plan(state)

        expected = CompactDecisionContextBuilder(orchestrator._settings).build_entry_payload(
            market_context=context,
            daily_state=state,
            recent_performance=performance,
            trade_memory=build_trade_decision_memory(records),
            strategy_objective={
                "mode": "ai_volume",
                "primary_goal": "Build exchange volume by opening a directional trade every tick.",
                "volume_profile": {"margin_per_entry_usdt": 180.0},
                "execution_notes": {
                    "sizing_source": "volume_profile",
                    "ai_responsibility": "select the best symbol and direction every tick; do not sit out",
                    "fee_warning": "Entries/exits pay fees; prefer the side with cleaner directional lean.",
                },
            },
            arena_reviews=[MarketAnalyzer().review(context).to_dict(), SignalDesigner().review(context).to_dict()],
        )
        self.assertEqual(compact_json(payloads[0]), compact_json(expected))
        self.assertGreater(build_trade_decision_memory(records)["sample_size"], 0)   # a real memory, not the empty one

    async def test_no_mapped_side_under_lean_makes_no_ai_call(self) -> None:
        sideways = {"kondisi": "SIDEWAYS", "allowed": False, "reason": "not_mapped:SIDEWAYS", "allowed_direction": ""}
        for name, gate_context in (("sideways hour", sideways), ("no gate context", None)):
            with self.subTest(name):
                store = _PayloadStore(self._losing_shorts())
                orchestrator, payloads, builds = self._orchestrator(
                    store,
                    [LedgerAndShadowTests._decision("OPEN_SHORT")],
                    gate_context=gate_context,
                    ai_entry_payload="lean",
                    setup_ledger_mode="shadow",
                )

                with self.assertLogs("test.lean", level="INFO") as logs:
                    plan = await orchestrator.next_trade_plan(LedgerAndShadowTests._state())

                self.assertIsNone(plan)
                self.assertEqual(payloads, [])
                self.assertEqual(len(logs.output), 1)
                self.assertIn("no side to offer", logs.output[0])
                # nothing read, nothing recorded
                self.assertEqual(builds, [])
                self.assertEqual(store.decisions, [])
                self.assertEqual(store.shadow_rows, [])
                self.assertEqual(store.ledger_reads, [])
                self.assertEqual(store.memory_reads, [])

    async def test_lean_allowed_actions_follow_the_mapped_or_live_sides(self) -> None:
        naik = LedgerAndShadowTests._naik()
        for name, gate_context, overrides, actions in (
            ("map, NAIK, shadow LONG offered", naik, {}, ["OPEN_LONG", "HOLD"]),
            ("map, TURUN", self._turun(), {}, ["OPEN_SHORT", "HOLD"]),
            ("map, TURUN, any live", self._turun(), {"entry_gate_direction": "any"}, ["OPEN_SHORT", "HOLD"]),
            ("naik gate, LONG bot", naik, {"entry_regime_gate": "naik", "entry_gate_direction": "long"}, ["OPEN_LONG", "HOLD"]),
            ("gate off, any", None, {"entry_regime_gate": "off", "entry_gate_direction": "any"}, ["OPEN_LONG", "OPEN_SHORT", "HOLD"]),
            ("gate off, short", None, {"entry_regime_gate": "off", "entry_gate_direction": "short"}, ["OPEN_SHORT", "HOLD"]),
        ):
            with self.subTest(name):
                orchestrator, payloads, _ = self._orchestrator(
                    _PayloadStore(),
                    [LedgerAndShadowTests._decision("HOLD")],
                    gate_context=gate_context,
                    ai_entry_payload="lean",
                    **overrides,
                )
                await orchestrator.next_trade_plan(LedgerAndShadowTests._state())
                self.assertEqual(payloads[0]["output_rules"]["allowed_actions"], actions)

    async def test_lean_entry_rules_carry_the_stretch_the_gate_measures(self) -> None:
        store = _PayloadStore()
        orchestrator, payloads, _ = self._orchestrator(
            store,
            [LedgerAndShadowTests._decision("OPEN_SHORT")],
            gate_context=self._turun(),
            context=self._context(ema_21=99.9),   # (100 - 99.9) / 0.25 = 0.4 ATR above the mean
            ai_entry_payload="lean",
            symbols=["BTC/USDT"],
        )
        await orchestrator.next_trade_plan(LedgerAndShadowTests._state())

        self.assertEqual(
            payloads[0]["entry_rules"],
            {"stretch_atr": 0.4, "max_stretch_atr": 0.75, "rule": "an entry with |stretch_atr| above this limit is rejected in code"},
        )
        self.assertEqual(store.decisions[0]["payload"]["gate"]["stretch_atr"], payloads[0]["entry_rules"]["stretch_atr"])

    async def test_lean_entry_rules_hold_one_stretch_per_symbol_with_several_symbols(self) -> None:
        orchestrator, payloads, _ = self._orchestrator(
            _PayloadStore(),
            [LedgerAndShadowTests._decision("HOLD")],
            context=self._context(ema_21=99.9),
            ai_entry_payload="lean",
            entry_regime_gate="off",
            entry_gate_direction="any",
        )
        await orchestrator.next_trade_plan(LedgerAndShadowTests._state())

        self.assertEqual(payloads[0]["entry_rules"]["stretch_atr"], {"BTC/USDT": 0.4, "ETH/USDT": 0.0})

    async def test_lean_market_regime_comes_from_the_gate_context(self) -> None:
        numbers = {"atr14_1h": 0.5, "atr_median": 0.4, "hour_close_ms": 1_792_000_000_000, "sma20_1h": 101.0,
                   "sma50_1h": 102.0, "close_4h": 100.5, "ema50_4h": 101.5}
        orchestrator, payloads, _ = self._orchestrator(
            _PayloadStore(),
            [LedgerAndShadowTests._decision("HOLD")],
            gate_context=self._turun(**numbers),
            ai_entry_payload="lean",
            symbols=["BTC/USDT"],
        )
        await orchestrator.next_trade_plan(LedgerAndShadowTests._state())

        self.assertEqual(
            payloads[0]["market_regime"],
            {"kondisi": "TURUN", "reason": "turun", "allowed_direction": "short", **numbers,
             "price_vs_sma20_1h_atr": -2.0},   # (100 - 101) / 0.5, from the 3m last price
        )

        orchestrator, payloads, _ = self._orchestrator(
            _PayloadStore(), [LedgerAndShadowTests._decision("HOLD")], ai_entry_payload="lean",
            entry_regime_gate="off", entry_gate_direction="any",
        )
        await orchestrator.next_trade_plan(LedgerAndShadowTests._state())
        self.assertNotIn("market_regime", payloads[0])

    async def test_lean_ledger_lines_reuse_the_one_ledger_read(self) -> None:
        store = _PayloadStore(self._losing_shorts())
        orchestrator, payloads, _ = self._orchestrator(
            store,
            [LedgerAndShadowTests._decision("OPEN_SHORT")],
            gate_context=self._turun(),
            ai_entry_payload="lean",
            setup_ledger_mode="enforce",
            symbols=["BTC/USDT"],
        )
        plan = await orchestrator.next_trade_plan(LedgerAndShadowTests._state())

        self.assertEqual(
            payloads[0]["setup_ledger"],
            [{"side": "short", "bucket": self.BUCKET, "n": 3, "mean_bps": -15.0, "status": "blocked"}],
        )
        # the decision's ledger verdict and the payload line come from one read of each table
        self.assertEqual(store.ledger_reads, [("trades", self.SINCE), ("shadows", self.SINCE, "ledger_block")])
        self.assertEqual(store.decisions[0]["payload"]["gate"]["ledger"]["status"], "blocked")
        # the status the model was shown is the one the code enforced
        self.assertIsNone(plan)
        self.assertEqual([row["source"] for row in store.shadow_rows], ["ledger_block"])

    async def test_lean_omits_the_ledger_block_unless_enforcing(self) -> None:
        # A `shadow` ledger records its verdict but trades a "blocked" bucket anyway, so the model must not be
        # told the bucket is blocked: the lean request carries `setup_ledger` only while the ledger enforces.
        for mode, shown in (("off", False), ("shadow", False), ("enforce", True)):
            with self.subTest(mode=mode):
                store = _PayloadStore(self._losing_shorts())
                orchestrator, payloads, _ = self._orchestrator(
                    store,
                    [LedgerAndShadowTests._decision("OPEN_SHORT")],
                    gate_context=self._turun(),
                    ai_entry_payload="lean",
                    setup_ledger_mode=mode,
                    symbols=["BTC/USDT"],
                )
                plan = await orchestrator.next_trade_plan(LedgerAndShadowTests._state())

                self.assertEqual("setup_ledger" in payloads[0], shown)
                gate = store.decisions[0]["payload"]["gate"]
                if mode == "off":
                    self.assertNotIn("ledger", gate)
                else:
                    # the verdict is still recorded in both modes; only enforce acts on it
                    self.assertEqual((gate["ledger"]["mode"], gate["ledger"]["status"]), (mode, "blocked"))
                self.assertEqual(plan is None, mode == "enforce")

    async def test_lean_ledger_lines_cover_a_shadow_side_and_are_absent_while_the_ledger_is_off(self) -> None:
        store = _PayloadStore()
        orchestrator, payloads, _ = self._orchestrator(
            store, [LedgerAndShadowTests._decision("HOLD")], gate_context=LedgerAndShadowTests._naik(),
            ai_entry_payload="lean", setup_ledger_mode="enforce", symbols=["BTC/USDT"],
        )
        await orchestrator.next_trade_plan(LedgerAndShadowTests._state())
        self.assertEqual(
            payloads[0]["setup_ledger"],
            [{"side": "long", "bucket": "LONG|NAIK|atr_mid", "n": 0, "mean_bps": None, "status": "learning"}],
        )

        orchestrator, payloads, _ = self._orchestrator(
            _PayloadStore(), [LedgerAndShadowTests._decision("HOLD")], gate_context=self._turun(),
            ai_entry_payload="lean", symbols=["BTC/USDT"],
        )
        await orchestrator.next_trade_plan(LedgerAndShadowTests._state())
        self.assertNotIn("setup_ledger", payloads[0])

    async def test_lean_objective_drops_the_every_tick_mandate_and_keeps_the_volume_profile(self) -> None:
        orchestrator, payloads, _ = self._orchestrator(
            _PayloadStore(), [LedgerAndShadowTests._decision("HOLD")], gate_context=self._turun(),
            ai_entry_payload="lean",
        )
        await orchestrator.next_trade_plan(LedgerAndShadowTests._state())

        lean = {"mode": "ai_volume", "primary_goal": self.LEAN_GOAL, "volume_profile": {"margin_per_entry_usdt": 180.0}}
        self.assertEqual(orchestrator._lean_strategy_objective_payload(), lean)
        self.assertEqual(payloads[0]["strategy_objective"], lean)
        # the legacy objective is today's, word for word
        self.assertEqual(
            orchestrator._strategy_objective_payload(),
            {
                "mode": "ai_volume",
                "primary_goal": "Build exchange volume by opening a directional trade every tick.",
                "volume_profile": {"margin_per_entry_usdt": 180.0},
                "execution_notes": {
                    "sizing_source": "volume_profile",
                    "ai_responsibility": "select the best symbol and direction every tick; do not sit out",
                    "fee_warning": "Entries/exits pay fees; prefer the side with cleaner directional lean.",
                },
            },
        )

        grok, _, _ = self._orchestrator(_PayloadStore(), [], strategy_mode="grok")
        self.assertEqual(
            grok._lean_strategy_objective_payload(), {"mode": "directional_ai", "primary_goal": self.LEAN_GOAL}
        )
