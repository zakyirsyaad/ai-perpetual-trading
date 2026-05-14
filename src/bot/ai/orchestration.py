from __future__ import annotations

import logging
from typing import Any

from bot.ai.brain import EntryDecision, GrokBrain
from bot.config import Settings
from bot.intelligence.market_intelligence import MarketIntelligence
from bot.models import OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import DailyRiskState
from bot.storage.supabase_journal import SupabaseJournal
from bot.storage.trade_store import TradeStore
from bot.strategy.base import ExitSignal


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
            "task": "entry_decision",
            "execution_environment": {
                "exchange": "OKX",
                "mode": "demo_trading",
                "instrument_type": self._settings.okx_instrument_type,
                "margin_mode": self._settings.okx_margin_mode,
                "symbols": list(self._settings.symbols),
            },
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
            },
            "market_context": market_context.to_dict(),
            "recent_executed_trade_performance": recent_performance,
            "output_rules": {
                "allowed_actions": ["OPEN_LONG", "OPEN_SHORT", "HOLD"],
                "use_only_allowed_symbols": True,
                "never_trade_if_data_is_missing_or_unclear": True,
                "only_demo_trades_are_executed": True,
            },
        }
        decision = await self._brain.entry_decision(payload)
        if decision is None:
            return None
        await self._record_ai_decision(decision, payload)

        if decision.action == "HOLD":
            self._logger.info("Grok entry decision HOLD for %s: %s", decision.symbol, decision.reason)
            return None
        if decision.symbol not in self._settings.symbols:
            self._logger.warning("Grok returned unsupported symbol %s.", decision.symbol)
            return None
        if decision.confidence < self._settings.ai_min_confidence:
            self._logger.info(
                "Grok entry rejected: confidence %.2f < %.2f. %s",
                decision.confidence,
                self._settings.ai_min_confidence,
                decision.reason,
            )
            return None
        if decision.systemic_risk >= self._settings.ai_systemic_risk_no_trade_threshold:
            self._logger.info(
                "Grok entry rejected: systemic_risk=%s >= threshold=%s. %s",
                decision.systemic_risk,
                self._settings.ai_systemic_risk_no_trade_threshold,
                decision.reason,
            )
            return None
        if decision.risk_reward_ratio is not None and decision.risk_reward_ratio < 1.2:
            self._logger.info("Grok entry rejected: risk/reward %.2f too low.", decision.risk_reward_ratio)
            return None

        direction = PositionDirection.LONG if decision.action == "OPEN_LONG" else PositionDirection.SHORT
        sl_pct = decision.stop_loss_pct or self._settings.stop_loss_pct
        tp_pct = decision.take_profit_pct or self._settings.take_profit_pct
        usdt_size = self._calculate_position_usdt(decision, market_context.to_dict(), sl_pct)

        if self._settings.ai_pre_trade_check:
            approved = await self._pre_trade_approved(decision, usdt_size, sl_pct, tp_pct)
            if not approved:
                return None

        reason = (
            f"grok:{decision.action} conf={decision.confidence:.2f} "
            f"trend={decision.market_trend} risk={decision.systemic_risk}/5 "
            f"rr={decision.risk_reward_ratio} note={decision.reason[:180]}"
        )
        return TradePlan(
            symbol=decision.symbol,
            direction=direction,
            usdt_size=usdt_size,
            leverage=self._settings.leverage,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            decision_reason=reason,
            decision_payload=decision.model_dump(),
        )

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
        sl_pct: float,
        tp_pct: float,
    ) -> bool:
        payload = {
            "planned_trade": decision.model_dump(),
            "position_sizing": {
                "usdt_size": usdt_size,
                "leverage": self._settings.leverage,
                "stop_loss_pct": sl_pct,
                "take_profit_pct": tp_pct,
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

        usdt_size = risk_usdt / ((sl_pct / 100.0) * self._settings.leverage)
        configured = self._settings.position_usdt
        usdt_size = max(min(usdt_size, configured * 1.5), configured * 0.3)

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
