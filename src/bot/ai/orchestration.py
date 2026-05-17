from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from bot.ai.brain import EntryDecision, GrokBrain
from bot.config import Settings
from bot.intelligence.market_intelligence import MarketIntelligence
from bot.models import OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import DailyRiskState
from bot.storage.supabase_journal import SupabaseJournal
from bot.storage.trade_store import TradeStore
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
        trade_store: TradeStore,
        logger: logging.Logger,
        supabase_journal: SupabaseJournal | None = None,
    ) -> None:
        self._settings = settings
        self._brain = brain
        self._intelligence = intelligence
        self._trade_store = trade_store
        self._logger = logger
        self._supabase_journal = supabase_journal

    async def close(self) -> None:
        await self._brain.close()
        await self._intelligence.close()

    async def next_trade_plan(self, daily_state: DailyRiskState) -> TradePlan | None:
        market_context = await self._intelligence.build_context()
        recent_performance = await self._trade_store.recent_performance_summary(limit=50)
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
                "min_confidence": self._settings.ai_min_confidence,
                "min_fee_adjusted_risk_reward": self._settings.ai_min_fee_adjusted_rr,
                "round_trip_taker_fee_pct": self._round_trip_fee_pct,
            },
            "market_context": market_context.to_dict(),
            "recent_executed_trade_performance": recent_performance,
            "output_rules": {
                "allowed_actions": ["OPEN_LONG", "OPEN_SHORT", "HOLD"],
                "use_only_allowed_symbols": True,
                "never_trade_if_data_is_missing_or_unclear": True,
                "only_demo_trades_are_executed": True,
                "open_requires_explicit_stop_loss_take_profit_and_risk_reward": True,
                "choose_best_symbol_not_default_symbol": True,
                "return_one_decision_per_symbol": True,
                "ai_volume_mode_priorities": [
                    "Only choose trades with enough directional edge to offset taker fees.",
                    "Prefer HOLD over forced volume when trend is mixed, choppy, or liquidity is poor.",
                    "The execution layer handles volume sizing; the brain should choose timing and direction.",
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
        context_dict = market_context.to_dict()
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
        if decision.action == "HOLD":
            self._logger.info("Grok entry decision HOLD for %s: %s", decision.symbol, decision.reason)
            return None
        if decision.symbol not in self._settings.symbols:
            self._logger.warning("Grok returned unsupported symbol %s.", decision.symbol)
            return None
        if decision.confidence < self._settings.ai_min_confidence:
            self._logger.info(
                "Grok entry rejected for %s: confidence %.2f < %.2f. %s",
                decision.symbol,
                decision.confidence,
                self._settings.ai_min_confidence,
                decision.reason,
            )
            return None
        if decision.systemic_risk >= self._settings.ai_systemic_risk_no_trade_threshold:
            self._logger.info(
                "Grok entry rejected for %s: systemic_risk=%s >= threshold=%s. %s",
                decision.symbol,
                decision.systemic_risk,
                self._settings.ai_systemic_risk_no_trade_threshold,
                decision.reason,
            )
            return None
        if decision.risk_reward_ratio is not None and decision.risk_reward_ratio < 1.2:
            self._logger.info(
                "Grok entry rejected for %s: risk/reward %.2f too low.",
                decision.symbol,
                decision.risk_reward_ratio,
            )
            return None

        direction = PositionDirection.LONG if decision.action == "OPEN_LONG" else PositionDirection.SHORT
        target = self._resolve_trade_targets(decision, market_context)
        if target is None:
            return None
        if not self._entry_guard_approved(decision, direction, market_context, target):
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
            "primary_goal": "Build exchange volume while minimizing realized loss and avoiding low-edge forced entries.",
            "volume_profile": profile.as_log_fields(),
            "execution_notes": {
                "sizing_source": "volume_profile",
                "ai_responsibility": "select symbol, direction, and timing; return HOLD when edge is unclear",
                "fee_warning": "Market entries/exits pay taker fees; avoid tiny moves that cannot cover round-trip fees.",
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
        if check is None or not check.approved:
            self._logger.info(
                "Pre-trade check rejected %s: %s",
                decision.symbol,
                getattr(check, "invalidation_trigger", "no response"),
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
        if fee_adjusted_rr < self._settings.ai_min_fee_adjusted_rr:
            self._logger.info(
                "Grok entry rejected: fee-adjusted RR %.2f < %.2f (sl=%.3f%% tp=%.3f%% fee=%.3f%%).",
                fee_adjusted_rr,
                self._settings.ai_min_fee_adjusted_rr,
                sl_pct,
                tp_pct,
                round_trip_fee_pct,
            )
            return None
        if tp_pct <= round_trip_fee_pct:
            self._logger.info(
                "Grok entry rejected: take-profit %.3f%% cannot beat round-trip taker fee %.3f%%.",
                tp_pct,
                round_trip_fee_pct,
            )
            return None
        return ResolvedTradeTargets(
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            atr_pct=atr_pct,
            fee_adjusted_rr=fee_adjusted_rr,
            round_trip_fee_pct=round_trip_fee_pct,
        )

    def _entry_guard_approved(
        self,
        decision: EntryDecision,
        direction: PositionDirection,
        market_context: dict[str, Any],
        target: ResolvedTradeTargets,
    ) -> bool:
        features = self._features_for_symbol(market_context, decision.symbol)
        if features is None:
            self._logger.info("Grok entry rejected: missing technical features for %s.", decision.symbol)
            return False

        last_price = float(features.get("last_price") or 0.0)
        ema_fast = float(features.get("ema_fast") or 0.0)
        ema_slow = float(features.get("ema_slow") or 0.0)
        macd_hist = float(features.get("macd_hist") or 0.0)
        rsi_14 = float(features.get("rsi_14") or 50.0)
        change_pct_1 = float(features.get("change_pct_1") or 0.0)
        change_pct_5 = float(features.get("change_pct_5") or 0.0)
        volume_change_pct = float(features.get("volume_change_pct") or 0.0)
        scores = ((market_context.get("intelligence") or {}).get("symbol_scores") or {}).get(decision.symbol, {})
        trend_bias = str(scores.get("trend_bias") or "mixed")
        composite = float(scores.get("composite_score") or 0.0)

        if direction == PositionDirection.LONG:
            blocked = [
                (last_price <= ema_slow, "price_not_above_slow_ema"),
                (ema_fast <= ema_slow, "fast_ema_not_above_slow_ema"),
                (macd_hist <= 0, "macd_hist_not_positive"),
                (trend_bias != "bullish", f"trend_bias={trend_bias}"),
                (composite < 0.9, f"composite_too_low={composite:.2f}"),
                (rsi_14 >= 76, f"rsi_overheated={rsi_14:.1f}"),
                (change_pct_1 < -0.05 and change_pct_5 < 0, "short_term_reversal_down"),
                (volume_change_pct <= -85, f"volume_collapse={volume_change_pct:.1f}%"),
            ]
        else:
            blocked = [
                (last_price >= ema_slow, "price_not_below_slow_ema"),
                (ema_fast >= ema_slow, "fast_ema_not_below_slow_ema"),
                (macd_hist >= 0, "macd_hist_not_negative"),
                (trend_bias != "bearish", f"trend_bias={trend_bias}"),
                (composite > -0.9, f"composite_not_bearish_enough={composite:.2f}"),
                (rsi_14 <= 24, f"rsi_oversold={rsi_14:.1f}"),
                (change_pct_1 > 0.05 and change_pct_5 > 0, "short_term_reversal_up"),
                (volume_change_pct <= -85, f"volume_collapse={volume_change_pct:.1f}%"),
            ]

        reasons = [reason for condition, reason in blocked if condition]
        if reasons:
            self._logger.info(
                "Grok entry rejected by deterministic guard for %s %s: %s",
                decision.symbol,
                direction.value,
                ", ".join(reasons[:4]),
            )
            return False

        self._logger.info(
            "Entry guard passed for %s %s: sl=%.3f%% tp=%.3f%% net_rr=%.2f atr=%.3f%% rsi=%.1f change1=%.3f change5=%.3f.",
            decision.symbol,
            direction.value,
            target.stop_loss_pct,
            target.take_profit_pct,
            target.fee_adjusted_rr,
            target.atr_pct,
            rsi_14,
            change_pct_1,
            change_pct_5,
        )
        return True

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
