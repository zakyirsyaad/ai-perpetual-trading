from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any

from bot.arena.agents import MarketAnalyzer, SignalDesigner
from bot.ai.brain import EntryDecision, GrokBrain
from bot.ai.compact_context import CompactDecisionContextBuilder, payload_hash
from bot.ai.exit_policy import AIExitPolicy
from bot.ai.trade_memory import build_trade_decision_memory
from bot.arena.setup_profile import build_setup_profile
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
        self._compact_context = CompactDecisionContextBuilder(settings)
        self._exit_policy = AIExitPolicy(settings)
        self._active_exit_symbol: str | None = None
        self._gate_context: dict[str, Any] | None = None

    def set_gate_context(self, context: dict[str, Any]) -> None:
        """Keep the regime verdict; `_record_ai_decision` writes it under `payload_json.gate.regime`.

        Replaced, never mutated, so a stale verdict cannot leak into a later decision."""
        self._gate_context = dict(context)

    async def close(self) -> None:
        await self._brain.close()
        await self._intelligence.close()

    async def next_trade_plan(self, daily_state: DailyRiskState) -> TradePlan | None:
        market_context = await self._intelligence.build_context()
        recent_performance = await self._trade_store.recent_performance_summary(
            limit=self._settings.ai_recent_performance_limit,
        )
        recent_trade_records = await self._trade_store.recent_closed_trade_records(
            limit=self._settings.ai_trade_memory_limit,
        )
        learned_trade_memory = build_trade_decision_memory(recent_trade_records)
        context_dict = market_context.to_dict()
        # The full context still feeds deterministic execution, attribution, and the
        # audit record below; only the AI representation is compacted.
        payload = self._compact_context.build_entry_payload(
            market_context=context_dict,
            daily_state=daily_state,
            recent_performance=recent_performance,
            trade_memory=learned_trade_memory,
            strategy_objective=self._strategy_objective_payload(),
            arena_reviews=[
                self._market_analyzer.review(context_dict).to_dict(),
                self._signal_designer.review(context_dict).to_dict(),
            ],
        )
        decisions = await self._brain.entry_decisions(payload)
        if decisions is None:
            return None

        decisions = self._dedupe_entry_decisions(decisions)
        returned_symbols = {decision.symbol for decision in decisions}
        missing_symbols = [symbol for symbol in self._settings.symbols if symbol not in returned_symbols]
        if missing_symbols:
            self._logger.warning("Grok did not return decisions for symbols: %s", ", ".join(missing_symbols))

        pacing = await self._pacing_state(daily_state)

        candidates: list[EntryCandidate] = []
        for decision in decisions:
            gate_verdict = self._stretch_gate_verdict(decision, context_dict, pacing)
            await self._record_ai_decision(decision, payload, pacing, gate_verdict)
            candidate = self._build_entry_candidate(decision, context_dict, gate_verdict)
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
        gate_verdict: dict[str, Any] | None = None,
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
        if decision.confidence < self._settings.ai_min_confidence:
            self._logger.info(
                "Entry candidate %s rejected: confidence %.3f is below minimum %.3f.",
                decision.symbol,
                decision.confidence,
                self._settings.ai_min_confidence,
            )
            return None

        # Merged risk review (replaces the separate PreTradeCheck call). A missing or
        # structurally invalid review makes the candidate unusable — never approved.
        review = decision.risk_review
        if review is None:
            self._logger.warning(
                "Entry candidate %s has no usable risk_review; treating as unusable rather than approved.",
                decision.symbol,
            )
            return None
        if not review.approved:
            self._logger.info(
                "Risk review vetoed %s: %s",
                decision.symbol,
                review.invalidation_trigger,
            )
            return None
        self._logger.info(
            "Risk review approved %s. confirmations=%s invalidation=%s",
            decision.symbol,
            review.confirmations,
            review.invalidation_trigger,
        )

        if gate_verdict and gate_verdict.get("verdict") == "block" and gate_verdict.get("reason") == "direction":
            allowed = gate_verdict.get("allowed_direction")
            if gate_verdict.get("mode") == "enforce":
                self._logger.info(
                    "Entry candidate %s rejected: the gate lets through %s entries only.", decision.symbol, allowed
                )
                return None
            self._logger.info(
                "Entry gate (shadow) would block %s: only %s entries are let through.", decision.symbol, allowed
            )
        elif gate_verdict and gate_verdict.get("verdict") == "block":
            stretch = gate_verdict.get("stretch_atr")
            limit = gate_verdict.get("max_stretch_atr")
            if gate_verdict.get("mode") == "enforce":
                self._logger.info(
                    "Entry candidate %s rejected: %.2f ATR from the mean exceeds the gate limit %.2f.",
                    decision.symbol,
                    float(stretch or 0.0),
                    float(limit or 0.0),
                )
                return None
            # Shadow mode records what enforcement would have cost without
            # changing a single live decision.
            self._logger.info(
                "Entry stretch gate (shadow) would block %s: %.2f ATR from the mean exceeds %.2f.",
                decision.symbol,
                float(stretch or 0.0),
                float(limit or 0.0),
            )

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
            factor_metadata=self._factor_metadata_for_symbol(
                market_context, decision.symbol, direction.value
            ),
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
        """Event-driven AI exit evaluation.

        Deterministic price/SL/TP/fee/liquidation/time-stop checks keep running on
        every five-second monitor iteration in `TradeEngine._monitor_position`.
        This method only decides whether an *AI* call is worth making, so a
        one-hour position costs a handful of calls instead of up to 720.
        """
        now = datetime.now(timezone.utc).timestamp()
        self._ensure_exit_trade_started(position, now)
        # Deterministic stop loss, take profit and the neutral floor keep running in
        # the monitor; only the model is held back until the position has had time
        # to move past its own fee cost.
        min_hold = float(getattr(self._settings, "ai_exit_min_hold_seconds", 0) or 0)
        if min_hold > 0 and elapsed_seconds < min_hold:
            self._logger.debug(
                "AI exit evaluation deferred for %s: held %.0fs of the %.0fs minimum.",
                position.symbol,
                elapsed_seconds,
                min_hold,
            )
            return self._deterministic_exit_fallback(elapsed_seconds, ai_called=False)
        candle_ms = AIExitPolicy.last_closed_candle_ms(
            int(now * 1000),
            str(getattr(self._settings, "ai_kline_interval", "3m")),
        )

        # Stage 1 — cheap gate. Budget, minimum interval, a newly closed candle,
        # and adverse price movement all resolve without any network call, so a
        # blocked monitor tick costs nothing.
        gate = self._exit_policy.evaluate(now=now, price=current_price, last_closed_candle_ms=candle_ms)
        # routine_budget_reserved: only price moves may spend the remaining calls,
        # and those were already checked above, so probing flow would be wasted.
        if not gate.allowed and gate.reason in {"exit_call_budget_exhausted", "min_interval", "routine_budget_reserved"}:
            self._logger.debug(
                "AI exit evaluation skipped for %s (%s); deterministic protection stays active.",
                position.symbol,
                gate.reason,
            )
            return self._deterministic_exit_fallback(elapsed_seconds, ai_called=False)

        market_context = await self._intelligence.build_context(symbols=(position.symbol,))
        context_dict = market_context.to_dict()
        flow_signature = AIExitPolicy.flow_signature(context_dict, position.symbol)

        if not gate.allowed:
            # Stage 2 — the cheap events did not fire, so the only remaining
            # trigger is a materially new regime or flow state, which can only be
            # judged once the context exists.
            gate = self._exit_policy.evaluate(
                now=now,
                price=current_price,
                last_closed_candle_ms=candle_ms,
                flow_signature=flow_signature,
            )
            if not gate.allowed:
                self._exit_policy.note_probe(now=now, flow_signature=flow_signature)
                self._logger.debug(
                    "AI exit evaluation skipped for %s (%s); deterministic protection stays active.",
                    position.symbol,
                    gate.reason,
                )
                return self._deterministic_exit_fallback(elapsed_seconds, ai_called=False)

        payload = self._compact_context.build_exit_payload(
            position={
                "symbol": position.symbol,
                "direction": position.direction.value,
                "entry_price": position.entry_price,
                "current_price": current_price,
                "quantity_base": position.quantity,
                "exchange_size": position.exchange_size,
                "elapsed_seconds": round(elapsed_seconds, 1),
            },
            market_context=context_dict,
        )
        request_hash = payload_hash(payload)
        if self._exit_policy.is_duplicate_request(request_hash):
            # Identical compact request: the model already answered this exact
            # state, so skip the provider call and keep the budget intact.
            self._exit_policy.note_deduplicated()
            self._exit_policy.note_probe(now=now, flow_signature=flow_signature)
            self._logger.debug(
                "Skipped duplicate AI exit request for %s (hash=%s).",
                position.symbol,
                request_hash[:12],
            )
            return self._deterministic_exit_fallback(elapsed_seconds, ai_called=False)

        self._exit_policy.mark_called(
            now=now,
            price=current_price,
            last_closed_candle_ms=candle_ms,
            flow_signature=flow_signature,
            request_hash=request_hash,
        )
        self._logger.info(
            "AI exit evaluation for %s triggered by %s (call %s/%s).",
            position.symbol,
            gate.reason,
            self._exit_policy.calls_made,
            self._exit_policy.state()["max_calls"],
        )

        decision = await self._brain.exit_decision(payload)
        if decision is None:
            return self._deterministic_exit_fallback(elapsed_seconds, ai_called=True)
        if decision.confidence < self._settings.ai_min_confidence:
            return ExitSignal(
                close_now=False,
                reason="hold_low_confidence",
                ai_called=True,
                confidence=decision.confidence,
            )
        return ExitSignal(
            close_now=decision.action == "CLOSE",
            reason=decision.reason,
            ai_called=True,
            confidence=decision.confidence,
        )

    def _ensure_exit_trade_started(self, position: OpenPosition, now: float) -> None:
        trade_key = f"{position.symbol}:{position.exchange_order_id}"
        if self._active_exit_symbol == trade_key:
            return
        self._active_exit_symbol = trade_key
        self._exit_policy.start_trade(
            entry_price=position.entry_price,
            direction=position.direction,
            now=now,
        )

    def _deterministic_exit_fallback(self, elapsed_seconds: float, *, ai_called: bool) -> ExitSignal:
        """No AI verdict: deterministic time-stop protection still applies."""
        if elapsed_seconds >= self._settings.position_hold_minutes * 60:
            return ExitSignal(close_now=True, reason="time_stop_no_grok" if ai_called else "time_stop")
        return ExitSignal(close_now=False, reason="hold")

    def exit_policy_state(self) -> dict[str, Any]:
        return self._exit_policy.state()

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
        # The ATR-scaled stop is otherwise unbounded. Clamp it before the TP floor
        # is derived so a capped stop also tightens the reward requirement.
        sl_ceiling = float(getattr(self._settings, "max_stop_loss_pct", 0.0) or 0.0)
        if sl_ceiling > 0:
            sl_pct = min(sl_pct, sl_ceiling)
        round_trip_fee_pct = self._round_trip_fee_pct
        min_tp_for_net_rr = round_trip_fee_pct + (
            (sl_pct + round_trip_fee_pct) * self._settings.ai_min_fee_adjusted_rr
        )
        tp_pct = max(base_tp, atr_pct * self._settings.atr_tp_multiplier, min_tp_for_net_rr)
        fee_adjusted_rr = self._fee_adjusted_rr(sl_pct, tp_pct)
        if not self._stop_is_strictly_before_liquidation(decision, sl_pct, market_context):
            return None
        # Volume-building run: fee-adjusted RR and TP-vs-fee rejections removed. The
        # computed fee_adjusted_rr is still returned for candidate scoring.
        return ResolvedTradeTargets(
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            atr_pct=atr_pct,
            fee_adjusted_rr=fee_adjusted_rr,
            round_trip_fee_pct=round_trip_fee_pct,
        )

    def _stop_is_strictly_before_liquidation(
        self,
        decision: EntryDecision,
        stop_loss_pct: float,
        market_context: dict[str, Any],
    ) -> bool:
        constraints = market_context.get("liquidation_constraints")
        constraint = constraints.get(decision.symbol) if isinstance(constraints, dict) else None
        if not isinstance(constraint, dict) or constraint.get("status") != "available":
            self._logger.warning("Entry candidate %s rejected: liquidation constraint unavailable.", decision.symbol)
            return False
        key = "max_long_stop_loss_pct" if decision.action == "OPEN_LONG" else "max_short_stop_loss_pct"
        try:
            maximum = float(constraint[key])
        except (KeyError, TypeError, ValueError):
            self._logger.warning("Entry candidate %s rejected: malformed liquidation constraint.", decision.symbol)
            return False
        if (
            not math.isfinite(stop_loss_pct)
            or not math.isfinite(maximum)
            or stop_loss_pct <= 0
            or maximum <= 0
            or stop_loss_pct >= maximum
        ):
            self._logger.warning(
                "Entry candidate %s rejected: stop_loss_pct=%.6f must be strictly below liquidation maximum %.6f.",
                decision.symbol,
                stop_loss_pct,
                maximum,
            )
            return False
        return True

    def _stretch_gate_verdict(
        self,
        decision: EntryDecision,
        market_context: dict[str, Any],
        pacing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Judge one entry by how far price sits from its own mean.

        Entries opened more than `ENTRY_STRETCH_GATE_MAX_ATR` from the 21-EMA are
        the measured loss source; keeping only near-mean entries moved gross/1M
        from -2 to +170 in replay. The volume mandate still wins when the day is
        behind schedule, so a stretched entry beats no entry then.
        """
        mode = str(getattr(self._settings, "entry_stretch_gate_mode", "off") or "off").strip().lower()
        max_stretch = float(getattr(self._settings, "entry_stretch_gate_max_atr", 0.0) or 0.0)
        verdict: dict[str, Any] = {
            "mode": mode,
            "verdict": "allow",
            "stretch_atr": None,
            "bucket": "unknown",
            "max_stretch_atr": max_stretch,
            "behind_schedule": False,
            "entries_needed_per_hour": None,
        }
        if decision.action not in ("OPEN_LONG", "OPEN_SHORT"):
            return verdict
        allowed_side = str(self._settings.entry_gate_direction or "any").strip().lower()
        other_side = allowed_side != "any" and decision.action != f"OPEN_{allowed_side.upper()}"
        if other_side and self._settings.entry_regime_gate != "off":
            # The LONG-only forward bot: with the regime gate on, the side filter enforces whatever
            # ENTRY_STRETCH_GATE_MODE says, so the stored verdict and the outcome agree.
            verdict["mode"] = "enforce"
        elif mode == "off":
            return verdict
        # The side filter comes before the schedule relaxation on purpose: being behind
        # on volume may excuse a stretched entry, never the side that has no edge.
        if other_side:
            verdict["verdict"] = "block"
            verdict["reason"] = "direction"
            verdict["allowed_direction"] = allowed_side
            return verdict

        if max_stretch <= 0:
            return verdict

        features = self._features_for_symbol(market_context, decision.symbol)
        setup = build_setup_profile(features, "LONG" if decision.action == "OPEN_LONG" else "SHORT")
        stretch = setup.get("stretch_atr")
        verdict["stretch_atr"] = stretch
        verdict["bucket"] = setup.get("stretch_bucket")

        needed = self._entries_needed_per_hour(pacing)
        verdict["entries_needed_per_hour"] = needed
        threshold = float(
            getattr(self._settings, "entry_stretch_gate_behind_schedule_entries_per_hour", 0.0) or 0.0
        )
        if threshold > 0 and needed is not None and needed >= threshold:
            verdict["behind_schedule"] = True
            return verdict

        # An unreadable setup is not evidence of a bad entry; only a measured
        # stretch beyond the limit blocks.
        if stretch is not None and abs(float(stretch)) > max_stretch:
            verdict["verdict"] = "block"
            verdict["reason"] = "stretch"
        return verdict

    def _entries_needed_per_hour(self, pacing: dict[str, Any] | None) -> float | None:
        if not isinstance(pacing, dict):
            return None
        target = float(pacing.get("target_volume_per_day_usdt") or 0.0)
        done = float(pacing.get("volume_today_usdt") or 0.0)
        hours = float(pacing.get("hours_remaining_in_trading_day") or 0.0)
        profile = getattr(self._settings, "volume_profile", None)
        per_entry = float(getattr(profile, "volume_per_entry_usdt", 0.0) or 0.0)
        if per_entry <= 0:
            margin = float(
                getattr(profile, "margin_per_entry_usdt", 0.0)
                or getattr(self._settings, "position_usdt", 0.0)
                or 0.0
            )
            per_entry = margin * float(getattr(self._settings, "leverage", 0) or 0) * float(
                getattr(self._settings, "volume_sides_per_entry", 2) or 2
            )
        if target <= 0 or hours <= 0 or per_entry <= 0:
            return None
        return max(0.0, target - done) / per_entry / hours

    async def _pacing_state(self, daily_state: DailyRiskState) -> dict[str, Any]:
        """How far the day is from its volume target, recorded beside the decision.

        Deliberately absent from the model request: this only builds the history a
        pacing prompt would need, so live behaviour stays unchanged until that
        prompt has been validated offline.
        """
        volume_today = float(await self._trade_store.total_volume_for_day())
        profile = getattr(self._settings, "volume_profile", None)
        target = float(getattr(profile, "daily_volume_usdt", 0.0) or 0.0)
        # Volume accounting keys the day on the trading timezone, so the clock that
        # matters here is that one rather than UTC.
        try:
            tz = ZoneInfo(str(getattr(self._settings, "trading_timezone", "UTC")))
        except Exception:  # noqa: BLE001 - a misconfigured zone must not stop trading
            tz = timezone.utc
        now = datetime.now(tz)
        hours_remaining = 24.0 - (now.hour + now.minute / 60.0 + now.second / 3600.0)
        completion = round(volume_today / target * 100.0, 2) if target > 0 else None
        state: dict[str, Any] = {
            "volume_today_usdt": round(volume_today, 2),
            "target_volume_per_day_usdt": round(target, 2),
            "volume_completion_pct": completion,
            "hours_remaining_in_trading_day": round(hours_remaining, 2),
            "closed_trades_today": int(daily_state.closed_trades),
            "day_pnl_usdt": float(daily_state.total_pnl_usdt),
        }
        self._logger.info(
            "Volume pacing: %.0f/%.0f USDT (%.1f%%), %.1fh left in the trading day, %d trades, day PnL %.2f.",
            volume_today,
            target,
            completion or 0.0,
            hours_remaining,
            state["closed_trades_today"],
            state["day_pnl_usdt"],
        )
        return state

    async def _record_ai_decision(
        self,
        decision: EntryDecision,
        request_payload: dict[str, Any],
        pacing: dict[str, Any] | None = None,
        gate: dict[str, Any] | None = None,
    ) -> None:
        # Link the stored decision to the provider call that produced it so token
        # spend can be attributed to individual decisions.
        call_metrics = getattr(self._brain, "last_call_metrics", None)
        ai_call_id = getattr(call_metrics, "call_id", None)
        if self._gate_context:
            gate = {**(gate or {}), "regime": self._gate_context}
        payload_json = {
            "decision": decision.model_dump(),
            "request": request_payload,
            "ai_call_id": ai_call_id,
            "pacing": pacing,
            "gate": gate,
        }
        await self._trade_store.save_ai_decision(
            symbol=decision.symbol,
            action=decision.action,
            confidence=decision.confidence,
            systemic_risk=decision.systemic_risk,
            reason=decision.reason,
            payload=payload_json,
            ai_call_id=ai_call_id,
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
                    "exchange": str(getattr(self._settings, "exchange_name", "")) or None,
                    "ai_call_id": ai_call_id,
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
    def _factor_metadata_for_symbol(
        market_context: dict[str, Any], symbol: str, direction: str = ""
    ) -> dict[str, Any]:
        snapshots = [
            item
            for item in market_context.get("factor_snapshots", [])
            if isinstance(item, dict) and item.get("symbol") == symbol
        ]
        top = sorted(snapshots, key=lambda item: abs(float(item.get("score") or 0.0)), reverse=True)[:5]
        return {
            "top_factors": [str(item.get("name")) for item in top if item.get("name")],
            "snapshots": top,
            # Persisted so attribution can bucket by the *shape* of the entry,
            # not just its symbol/direction/factors.
            "setup": build_setup_profile(
                AIOrchestrator._features_for_symbol(market_context, symbol), direction, top
            ),
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
