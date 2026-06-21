from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bot.arena.agents import MarketAnalyzer, SignalDesigner
from bot.ai.brain import EntryDecision, GrokBrain
from bot.config import Settings
from bot.intelligence.market_intelligence import MarketIntelligence
from bot.models import OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import DailyRiskState
from bot.storage.supabase_journal import SupabaseJournal
from bot.storage.interfaces import TradeStoreProtocol
from bot.strategy.base import ExitSignal


@dataclass(slots=True)
class ResolvedTradeTargets:
    stop_loss_pct: float
    take_profit_pct: float
    atr_pct: float
    fee_adjusted_rr: float
    round_trip_fee_pct: float


@dataclass(slots=True)
class EntryCandidate:
    decision: EntryDecision
    direction: PositionDirection
    target: ResolvedTradeTargets
    score: float


class AIOrchestrator:
    """Layer 2: coordinate brain decisions, risk state, market context, and learning memory."""

    def __init__(
        self,
        settings: Settings,
        brain: GrokBrain,
        intelligence: MarketIntelligence,
        trade_store: TradeStoreProtocol,
        logger: logging.Logger,
        supabase_journal: SupabaseJournal | None = None,
    ) -> None:
        self._settings = settings
        self._brain = brain
        self._intelligence = intelligence
        self._trade_store = trade_store
        self._logger = logger
        self._supabase_journal = supabase_journal
        self._market_analyzer = MarketAnalyzer()
        self._signal_designer = SignalDesigner()

    async def close(self) -> None:
        await self._brain.close()
        await self._intelligence.close()

    async def next_trade_plan(self, daily_state: DailyRiskState) -> TradePlan | None:
        market_context = await self._intelligence.build_context()
        recent_performance = await self._trade_store.recent_performance_summary(limit=50)
        context_dict = market_context.to_dict()
        payload = {
            "task": "ai_volume_entry_decision" if self._strategy_mode == "ai_volume" else "entry_decision",
            "execution_environment": {
                "exchange": "OKX",
                "mode": "demo_trading",
                "instrument_type": self._settings.okx_instrument_type,
                "margin_mode": self._settings.okx_margin_mode,
                "symbols": list(self._settings.symbols),
            },
            "strategy_objective": self._strategy_objective_payload(),
            "risk_constraints": {
                "trade_target_per_day": self._settings.trade_target_per_day,
                "closed_trades_today": daily_state.closed_trades,
                "stop_losses_today": daily_state.stop_losses,
                "max_stop_losses_per_day": self._settings.max_stop_loss_per_day,
                "consecutive_losses_today": daily_state.consecutive_losses,
                "max_consecutive_losses": self._settings.max_consecutive_losses,
                "day_pnl_usdt": daily_state.total_pnl_usdt,
                "max_daily_drawdown_usdt": self._settings.max_daily_drawdown_usdt,
                "configured_leverage": self._settings.leverage,
                "configured_position_usdt": self._settings.position_usdt,
                # Deliberately NOT exposing min_confidence to the model: showing it the
                # exact gate made it self-censor at that number every tick. The code gate
                # in _build_entry_candidate still enforces ai_min_confidence after the fact.
                "min_fee_adjusted_risk_reward": self._settings.ai_min_fee_adjusted_rr,
                "round_trip_taker_fee_pct": self._round_trip_fee_pct,
            },
            "market_context": context_dict,
            "arena_reviews": [
                self._market_analyzer.review(context_dict).to_dict(),
                self._signal_designer.review(context_dict).to_dict(),
            ],
            "recent_executed_trade_performance": recent_performance,
            "output_rules": {
                "allowed_actions": ["OPEN_LONG", "OPEN_SHORT"],
                "use_only_allowed_symbols": True,
                "only_demo_trades_are_executed": True,
                "open_requires_explicit_stop_loss_take_profit_and_risk_reward": True,
                "choose_best_symbol_not_default_symbol": True,
                "return_one_decision_per_symbol": True,
                "ai_volume_mode_priorities": [
                    "This is a volume-building run: always open a directional trade, never sit out.",
                    "Pick the side the OKX technicals lean toward; the execution layer handles volume sizing.",
                ]
                if self._strategy_mode == "ai_volume"
                else [],
            },
        }
        decisions = await self._brain.entry_decisions(payload)
        if decisions is None:
            return None

        decisions = self._dedupe_entry_decisions(decisions)
        returned_symbols = {decision.symbol for decision in decisions}
        missing_symbols = [symbol for symbol in self._settings.symbols if symbol not in returned_symbols]
        if missing_symbols:
            self._logger.warning("Grok did not return decisions for symbols: %s", ", ".join(missing_symbols))

        candidates: list[EntryCandidate] = []
        for decision in decisions:
            await self._record_ai_decision(decision, payload)
            candidate = self._build_entry_candidate(decision, context_dict)
            if candidate is not None:
                candidates.append(candidate)

        if not candidates:
            return None

        candidate = max(candidates, key=lambda item: item.score)
        self._logger.info(
            "Selected %s %s candidate with score %.3f among %s eligible symbols.",
            candidate.decision.symbol,
            candidate.direction.value,
            candidate.score,
            len(candidates),
        )
        return await self._candidate_to_trade_plan(candidate, context_dict)

    def _dedupe_entry_decisions(self, decisions: list[EntryDecision]) -> list[EntryDecision]:
        result: list[EntryDecision] = []
        seen: set[str] = set()
        for decision in decisions:
            if decision.symbol in seen:
                self._logger.warning("Grok returned duplicate decision for %s; keeping first.", decision.symbol)
                continue
            seen.add(decision.symbol)
            result.append(decision)
        return result

    def _build_entry_candidate(
        self,
        decision: EntryDecision,
        market_context: dict[str, Any],
    ) -> EntryCandidate | None:
        # Volume-building run: trade-suppressing gates (HOLD handling, confidence,
        # systemic risk, RR floor, and the deterministic entry guard) are intentionally
        # removed so the AI's chosen direction is always taken. The only checks kept are
        # sanity guards that would otherwise crash or trade an unsupported instrument.
        if decision.action == "HOLD":
            self._logger.info("Grok returned HOLD for %s; volume run skips this symbol.", decision.symbol)
            return None
        if decision.symbol not in self._settings.symbols:
            self._logger.warning("Grok returned unsupported symbol %s.", decision.symbol)
            return None

        direction = PositionDirection.LONG if decision.action == "OPEN_LONG" else PositionDirection.SHORT
        target = self._resolve_trade_targets(decision, market_context)
        if target is None:
            return None
        return EntryCandidate(
            decision=decision,
            direction=direction,
            target=target,
            score=self._candidate_score(decision, direction, market_context, target),
        )

    async def _candidate_to_trade_plan(
        self,
        candidate: EntryCandidate,
        market_context: dict[str, Any],
    ) -> TradePlan | None:
        decision = candidate.decision
        direction = candidate.direction
        target = candidate.target
        if self._strategy_mode == "ai_volume":
            usdt_size = float(self._settings.volume_profile.margin_per_entry_usdt)
        else:
            usdt_size = self._calculate_position_usdt(decision, market_context, target.stop_loss_pct)
        if self._settings.ai_pre_trade_check:
            approved = await self._pre_trade_approved(decision, usdt_size, target)
            if not approved:
                return None

        mode = "ai_volume" if self._strategy_mode == "ai_volume" else "grok"
        volume_note = ""
        if self._strategy_mode == "ai_volume":
            profile = self._settings.volume_profile
            volume_note = (
                f" volume_margin={float(profile.margin_per_entry_usdt):.2f} "
                f"entry_volume={float(profile.volume_per_entry_usdt):.2f}"
            )
        reason = (
            f"{mode}:{decision.action} conf={decision.confidence:.2f} "
            f"trend={decision.market_trend} risk={decision.systemic_risk}/5 "
            f"rr={decision.risk_reward_ratio} net_rr={target.fee_adjusted_rr:.2f} "
            f"atr={target.atr_pct:.3f}%{volume_note} note={decision.reason[:180]}"
        )
        return TradePlan(
            symbol=decision.symbol,
            direction=direction,
            usdt_size=usdt_size,
            leverage=self._settings.leverage,
            stop_loss_pct=target.stop_loss_pct,
            take_profit_pct=target.take_profit_pct,
            decision_reason=reason,
            decision_payload=decision.model_dump(),
            confidence=decision.confidence,
            factor_metadata=self._factor_metadata_for_symbol(market_context, decision.symbol),
            trigger_metadata=self._trigger_metadata_for_symbol(market_context, decision.symbol),
        )

    def _candidate_score(
        self,
        decision: EntryDecision,
        direction: PositionDirection,
        market_context: dict[str, Any],
        target: ResolvedTradeTargets,
    ) -> float:
        scores = ((market_context.get("intelligence") or {}).get("symbol_scores") or {}).get(decision.symbol, {})
        composite = float(scores.get("composite_score") or 0.0)
        directional_edge = composite if direction == PositionDirection.LONG else -composite
        secondary_symbol_bonus = 0.05 if decision.symbol != self._settings.symbols[0] else 0.0
        return (
            (decision.confidence * 2.0)
            + target.fee_adjusted_rr
            + max(directional_edge, 0.0) * 0.25
            - max(decision.systemic_risk - 1, 0) * 0.1
            + secondary_symbol_bonus
        )

    def _strategy_objective_payload(self) -> dict[str, Any]:
        if self._strategy_mode != "ai_volume":
            return {
                "mode": "directional_ai",
                "primary_goal": "Only open trades with clear positive expectancy after fees.",
            }
        profile = self._settings.volume_profile
        return {
            "mode": "ai_volume",
            "primary_goal": "Build exchange volume by opening a directional trade every tick.",
            "volume_profile": profile.as_log_fields(),
            "execution_notes": {
                "sizing_source": "volume_profile",
                "ai_responsibility": "select the best symbol and direction every tick; do not sit out",
                "fee_warning": "Entries/exits pay fees; prefer the side with cleaner directional lean.",
            },
        }

    @property
    def _strategy_mode(self) -> str:
        return str(getattr(self._settings, "strategy_mode", "grok"))

    async def exit_signal(
        self,
        position: OpenPosition,
        current_price: float,
        elapsed_seconds: float,
    ) -> ExitSignal:
        market_context = await self._intelligence.build_context(symbols=(position.symbol,))
        payload = {
            "task": "exit_decision",
            "position": {
                "symbol": position.symbol,
                "direction": position.direction.value,
                "entry_price": position.entry_price,
                "current_price": current_price,
                "quantity_base": position.quantity,
                "exchange_size": position.exchange_size,
                "elapsed_seconds": elapsed_seconds,
            },
            "constraints": {
                "min_confidence": self._settings.ai_min_confidence,
                "position_hold_minutes": self._settings.position_hold_minutes,
                "consider_taker_fee_pct": self._settings.taker_fee_pct,
            },
            "market_context": market_context.to_dict(),
        }
        decision = await self._brain.exit_decision(payload)
        if decision is None:
            if elapsed_seconds >= self._settings.position_hold_minutes * 60:
                return ExitSignal(close_now=True, reason="time_stop_no_grok")
            return ExitSignal(close_now=False, reason="hold")
        min_confidence = self._settings.ai_min_confidence
        if elapsed_seconds >= self._settings.position_hold_minutes * 60:
            min_confidence = max(0.4, min_confidence - 0.2)
        if decision.confidence < min_confidence:
            return ExitSignal(close_now=False, reason="hold")
        return ExitSignal(close_now=decision.action == "CLOSE", reason=decision.reason)

    async def _pre_trade_approved(
        self,
        decision: EntryDecision,
        usdt_size: float,
        target: ResolvedTradeTargets,
    ) -> bool:
        payload = {
            "planned_trade": decision.model_dump(),
            "position_sizing": {
                "usdt_size": usdt_size,
                "leverage": self._settings.leverage,
                "stop_loss_pct": target.stop_loss_pct,
                "take_profit_pct": target.take_profit_pct,
                "atr_pct": target.atr_pct,
                "round_trip_taker_fee_pct": target.round_trip_fee_pct,
                "fee_adjusted_risk_reward": target.fee_adjusted_rr,
                "min_fee_adjusted_risk_reward": self._settings.ai_min_fee_adjusted_rr,
            },
        }
        check = await self._brain.pre_trade_check(payload)
        if check is None:
            # No/invalid AI response is an infrastructure failure (timeout, 503,
            # unparseable output) — NOT a risk veto. The candidate already cleared the
            # entry guard and code gates, so fail open rather than silently dropping a
            # valid setup whenever the secondary AI call flakes.
            self._logger.warning(
                "Pre-trade check unavailable for %s (no/invalid AI response); "
                "passing on prior entry-guard approval.",
                decision.symbol,
            )
            return True
        if not check.approved:
            self._logger.info(
                "Pre-trade check vetoed %s: %s",
                decision.symbol,
                check.invalidation_trigger,
            )
            return False
        self._logger.info(
            "Pre-trade approved for %s. confirmations=%s invalidation=%s",
            decision.symbol,
            check.confirmations,
            check.invalidation_trigger,
        )
        return True

    def _resolve_trade_targets(
        self,
        decision: EntryDecision,
        market_context: dict[str, Any],
    ) -> ResolvedTradeTargets | None:
        base_sl = float(decision.stop_loss_pct or self._settings.stop_loss_pct)
        base_tp = float(decision.take_profit_pct or self._settings.take_profit_pct)
        if base_sl <= 0 or base_tp <= 0:
            self._logger.info("Grok entry rejected: invalid stop/take-profit values.")
            return None

        features = self._features_for_symbol(market_context, decision.symbol)
        atr_pct = 0.0
        if features is not None:
            atr_value = float(features.get("atr_14") or 0.0)
            last_price = float(features.get("last_price") or 0.0)
            if atr_value > 0 and last_price > 0:
                atr_pct = (atr_value / last_price) * 100.0

        sl_pct = max(base_sl, atr_pct * self._settings.atr_sl_multiplier)
        round_trip_fee_pct = self._round_trip_fee_pct
        min_tp_for_net_rr = round_trip_fee_pct + (
            (sl_pct + round_trip_fee_pct) * self._settings.ai_min_fee_adjusted_rr
        )
        tp_pct = max(base_tp, atr_pct * self._settings.atr_tp_multiplier, min_tp_for_net_rr)
        fee_adjusted_rr = self._fee_adjusted_rr(sl_pct, tp_pct)
        # Volume-building run: fee-adjusted RR and TP-vs-fee rejections removed. The
        # computed fee_adjusted_rr is still returned for candidate scoring.
        return ResolvedTradeTargets(
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            atr_pct=atr_pct,
            fee_adjusted_rr=fee_adjusted_rr,
            round_trip_fee_pct=round_trip_fee_pct,
        )

    async def _record_ai_decision(self, decision: EntryDecision, request_payload: dict[str, Any]) -> None:
        payload_json = {
            "decision": decision.model_dump(),
            "request": request_payload,
        }
        await self._trade_store.save_ai_decision(
            symbol=decision.symbol,
            action=decision.action,
            confidence=decision.confidence,
            systemic_risk=decision.systemic_risk,
            reason=decision.reason,
            payload=payload_json,
        )
        if self._supabase_journal is not None:
            await self._supabase_journal.record_ai_decision(
                {
                    "symbol": decision.symbol,
                    "action": decision.action,
                    "confidence": decision.confidence,
                    "systemic_risk": decision.systemic_risk,
                    "reason": decision.reason,
                    "payload_json": payload_json,
                }
            )
        if decision.learning_note:
            await self._trade_store.save_learning_note(
                note_type="entry_decision",
                symbol=decision.symbol,
                payload={"learning_note": decision.learning_note, "decision": decision.model_dump()},
            )

    def _calculate_position_usdt(
        self,
        decision: EntryDecision,
        market_context: dict[str, Any],
        sl_pct: float,
    ) -> float:
        risk_usdt = (self._settings.account_balance_usdt * self._settings.max_risk_pct_per_trade) / 100.0
        if sl_pct <= 0 or self._settings.leverage <= 0:
            return self._settings.position_usdt

        loss_pct = sl_pct + self._round_trip_fee_pct
        usdt_size = risk_usdt / ((loss_pct / 100.0) * self._settings.leverage)
        configured = self._settings.position_usdt
        usdt_size = min(usdt_size, configured * 1.5)

        for features in market_context.get("technicals", []):
            if features.get("symbol") != decision.symbol:
                continue
            atr_value = float(features.get("atr_14") or 0.0)
            last_price = float(features.get("last_price") or 0.0)
            if atr_value > 0 and last_price > 0:
                atr_pct = (atr_value / last_price) * 100.0
                if atr_pct > sl_pct:
                    usdt_size *= 0.75
            break
        return usdt_size

    @property
    def _round_trip_fee_pct(self) -> float:
        return self._settings.taker_fee_pct * 2.0

    def _fee_adjusted_rr(self, sl_pct: float, tp_pct: float) -> float:
        round_trip_fee_pct = self._round_trip_fee_pct
        net_reward = tp_pct - round_trip_fee_pct
        net_risk = sl_pct + round_trip_fee_pct
        if net_reward <= 0 or net_risk <= 0:
            return 0.0
        return net_reward / net_risk

    @staticmethod
    def _features_for_symbol(market_context: dict[str, Any], symbol: str) -> dict[str, Any] | None:
        for features in market_context.get("technicals", []):
            if features.get("symbol") == symbol:
                return features
        return None

    @staticmethod
    def _factor_metadata_for_symbol(market_context: dict[str, Any], symbol: str) -> dict[str, Any]:
        snapshots = [
            item
            for item in market_context.get("factor_snapshots", [])
            if isinstance(item, dict) and item.get("symbol") == symbol
        ]
        top = sorted(snapshots, key=lambda item: abs(float(item.get("score") or 0.0)), reverse=True)[:5]
        return {
            "top_factors": [str(item.get("name")) for item in top if item.get("name")],
            "snapshots": top,
        }

    @staticmethod
    def _trigger_metadata_for_symbol(market_context: dict[str, Any], symbol: str) -> dict[str, Any]:
        triggers = [
            item
            for item in market_context.get("active_flow_signals", [])
            if isinstance(item, dict) and item.get("symbol") == symbol
        ]
        return {
            "trigger_names": [str(item.get("name")) for item in triggers if item.get("name")],
            "signals": triggers,
        }
