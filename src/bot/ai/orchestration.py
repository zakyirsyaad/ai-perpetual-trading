from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Sequence

from bot.arena.agents import MarketAnalyzer, SignalDesigner
from bot.ai.brain import EntryDecision, GrokBrain
from bot.ai.compact_context import (
    LEAN_PRIMARY_GOAL,
    PAYLOAD_PROFILE_LEAN,
    PAYLOAD_PROFILE_LEGACY,
    STRETCH_RULE_TEXT,
    CompactDecisionContextBuilder,
    lean_market_regime,
    payload_hash,
)
from bot.ai.exit_policy import AIExitPolicy
from bot.ai.trade_memory import build_trade_decision_memory
from bot.arena.setup_profile import build_setup_profile
from bot.config import Settings
from bot.core.setup_ledger import (
    EMPTY_BUCKET_LEDGER,
    SHADOW_BLOCK_SOURCE,
    STATUS_BLOCKED,
    BucketLedger,
    bucket_key,
    ledger_status,
    ledger_table,
)
from bot.intelligence.market_intelligence import MarketIntelligence
from bot.models import OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import DailyRiskState
from bot.storage.supabase_journal import SupabaseJournal
from bot.storage.interfaces import TradeStoreProtocol
from bot.strategy.base import ExitSignal

_SIDES = frozenset({"long", "short"})
_OPEN_ACTIONS = ("OPEN_LONG", "OPEN_SHORT")
# `shadow_entries.source` of a mapped side that ENTRY_GATE_DIRECTION does not make live.
SHADOW_SIDE_SOURCE = "shadow_side"
# What one shadow row spacing is kept for: (symbol, side as stored, source, bucket_key).
ShadowKey = tuple[str, str, str, str]
# A setup-ledger snapshot younger than this is reused instead of read again (two paged GETs).
LEDGER_CACHE_SECONDS = 300


def _utc_now() -> datetime:
    """The wall clock that stamps a shadow row's `decided_at` (one seam, so tests can move it)."""
    return datetime.now(timezone.utc)


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
        # `decided_at` of the last shadow row per (symbol, side, source, bucket_key), as written here or
        # read from the store; the keys already read from the store this process (each is read once).
        self._shadow_rows_written_at: dict[ShadowKey, datetime] = {}
        self._shadow_seeded_keys: frozenset[ShadowKey] = frozenset()
        # The last setup-ledger snapshot read without error and when it was read; in memory only, `None`
        # until the first one. Reused for `LEDGER_CACHE_SECONDS`, and on a failed read whatever its age.
        self._last_ledger_snapshot: dict[str, BucketLedger] | None = None
        self._last_ledger_read_at: datetime | None = None

    def _current_gate_context(self) -> dict[str, Any] | None:
        return self._gate_context

    def set_gate_context(self, context: dict[str, Any]) -> None:
        """Keep the regime verdict; `_record_ai_decision` writes it under `payload_json.gate.regime`.

        Replaced, never mutated, so a stale verdict cannot leak into a later decision."""
        self._gate_context = dict(context)

    async def close(self) -> None:
        await self._brain.close()
        await self._intelligence.close()

    async def next_trade_plan(self, daily_state: DailyRiskState) -> TradePlan | None:
        lean = self._payload_profile() == PAYLOAD_PROFILE_LEAN
        # The sides the lean request offers come from the gate context set before the strategy ran,
        # the same one the stretch gate and the ledger bucket read below.
        lean_sides = self._lean_allowed_sides() if lean else []
        if lean and not lean_sides:
            context = self._current_gate_context() or {}
            self._logger.info(
                "Lean entry payload has no side to offer (regime gate %s, reason %s); holding without an AI call.",
                self._regime_gate_mode(),
                context.get("reason") or "no gate context",
            )
            return None
        market_context = await self._intelligence.build_context()
        context_dict = market_context.to_dict()
        if await self._shadow_only_hour_is_spaced(context_dict):
            return None
        # Read once per tick, and only while the ledger is on; `{}` otherwise.
        ledger = await self._ledger_snapshot()
        # The full context still feeds deterministic execution, attribution, and the
        # audit record below; only the AI representation is compacted.
        if lean:
            payload = self._lean_entry_payload(context_dict, daily_state, lean_sides, ledger)
            # The lean request goes out under the lean system prompt; legacy keeps today's call as it was.
            decisions = await self._brain.entry_decisions(payload, lean=True)
        else:
            payload = await self._legacy_entry_payload(context_dict, daily_state)
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
            gate_verdict = self._with_ledger_verdict(
                self._stretch_gate_verdict(decision, context_dict, pacing), decision, context_dict, ledger
            )
            await self._record_ai_decision(decision, payload, pacing, gate_verdict)
            candidate = self._build_entry_candidate(decision, context_dict, gate_verdict)
            if candidate is None:
                continue
            shadow_source = self._shadow_source(decision, gate_verdict)
            if shadow_source:
                # Recorded for the shadow evaluator and never traded, even when the row is skipped by the
                # spacing or cannot be saved.
                await self._record_shadow_entry(decision, candidate, shadow_source, context_dict)
                continue
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

    def _payload_profile(self) -> str:
        """`AI_ENTRY_PAYLOAD`: `lean`, or `legacy` for anything else (an unset setting included)."""
        profile = str(getattr(self._settings, "ai_entry_payload", PAYLOAD_PROFILE_LEGACY) or "").strip().lower()
        return PAYLOAD_PROFILE_LEAN if profile == PAYLOAD_PROFILE_LEAN else PAYLOAD_PROFILE_LEGACY

    async def _legacy_entry_payload(self, context_dict: dict[str, Any], daily_state: DailyRiskState) -> dict[str, Any]:
        """Today's request. Its trade history and arena reviews feed this request and nothing else."""
        recent_performance = await self._trade_store.recent_performance_summary(
            limit=self._settings.ai_recent_performance_limit,
        )
        recent_trade_records = await self._trade_store.recent_closed_trade_records(
            limit=self._settings.ai_trade_memory_limit,
        )
        return self._compact_context.build_entry_payload(
            market_context=context_dict,
            daily_state=daily_state,
            recent_performance=recent_performance,
            trade_memory=build_trade_decision_memory(recent_trade_records),
            strategy_objective=self._strategy_objective_payload(),
            arena_reviews=[
                self._market_analyzer.review(context_dict).to_dict(),
                self._signal_designer.review(context_dict).to_dict(),
            ],
        )

    def _lean_entry_payload(
        self,
        context_dict: dict[str, Any],
        daily_state: DailyRiskState,
        sides: Sequence[str],
        ledger: dict[str, BucketLedger],
    ) -> dict[str, Any]:
        """The lean request. The regime distance and the ledger lines read the first configured
        symbol, which is the only one whenever the regime gate (and so the ledger) is on."""
        features = self._features_for_symbol(context_dict, self._primary_symbol())
        return self._compact_context.build_lean_entry_payload(
            market_context=context_dict,
            daily_state=daily_state,
            strategy_objective=self._lean_strategy_objective_payload(),
            allowed_sides=sides,
            entry_rules=self._lean_entry_rules(context_dict),
            market_regime=lean_market_regime(self._current_gate_context(), (features or {}).get("last_price")),
            ledger_lines=self._ledger_lines(sides, features, ledger),
        )

    def _lean_allowed_sides(self) -> list[str]:
        """The sides the lean request offers: the mapped side under `map` (a shadow side included, so
        its calls are measured), none in an hour that maps no side; otherwise the live sides, sorted."""
        if self._regime_gate_mode() == "map":
            mapped = self._mapped_side()
            return [mapped] if mapped else []
        return sorted(self._live_sides())

    def _lean_entry_rules(self, context_dict: dict[str, Any]) -> dict[str, Any]:
        """The stretch rule the gate applies after the call, with the stretch it will measure.

        `stretch_atr` is the signed (price - EMA21) / ATR14 of `build_setup_profile`, the stretch
        gate's own number. One configured symbol gives one number; several give one per symbol."""
        symbols = [str(symbol) for symbol in self._settings.symbols]
        stretches = {
            symbol: build_setup_profile(self._features_for_symbol(context_dict, symbol), "")["stretch_atr"]
            for symbol in symbols
        }
        return {
            "stretch_atr": stretches[symbols[0]] if len(symbols) == 1 else stretches,
            "max_stretch_atr": float(getattr(self._settings, "entry_stretch_gate_max_atr", 0.0) or 0.0),
            "rule": STRETCH_RULE_TEXT,
        }

    def _ledger_lines(
        self,
        sides: Sequence[str],
        features: dict[str, Any] | None,
        ledger: dict[str, BucketLedger],
    ) -> list[dict[str, Any]]:
        """One line per offered side, from the tick's one ledger snapshot; `[]` unless the ledger enforces.

        A `shadow` ledger records its verdict but still trades a `blocked` bucket, so showing the model that
        status would describe a rule the code does not apply; the lean request then has no `setup_ledger`."""
        if self._ledger_mode() != "enforce":
            return []
        verdicts = [(side, self._ledger_verdict(side, features, ledger)) for side in sides]
        return [
            {
                "side": side,
                "bucket": verdict["bucket"],
                "n": verdict["n"],
                "mean_bps": round(verdict["mean_bps"], 2) if verdict["mean_bps"] is not None else None,
                "status": verdict["status"],
            }
            for side, verdict in verdicts
        ]

    def _primary_symbol(self) -> str:
        symbols = list(getattr(self._settings, "symbols", ()) or ())
        return str(symbols[0]) if symbols else ""

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
            if gate_verdict.get("mode") == "enforce" or gate_verdict.get("forced_by") == "regime_gate":
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
                market_context, decision.symbol, direction.value, self._current_gate_context()
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

    def _lean_strategy_objective_payload(self) -> dict[str, Any]:
        """The lean objective: an entry needs an edge beyond the fee, HOLD is the usual answer. The
        "every tick" and "do not sit out" notes are gone; the volume profile stays as information."""
        if self._strategy_mode != "ai_volume":
            return {"mode": "directional_ai", "primary_goal": LEAN_PRIMARY_GOAL}
        return {
            "mode": "ai_volume",
            "primary_goal": LEAN_PRIMARY_GOAL,
            "volume_profile": self._settings.volume_profile.as_log_fields(),
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
        # Under `map` the regime picks the side. The gate context is read once, here, so the side
        # and the decision's record come from the same hour.
        mapped_gate = self._regime_gate_mode() == "map"
        mapped_side = self._mapped_side() if mapped_gate else ""
        if mapped_gate:
            verdict["mapped_side"] = mapped_side
            verdict["shadow_side"] = False
        if decision.action not in ("OPEN_LONG", "OPEN_SHORT"):
            return verdict
        if mapped_gate:
            # The mapped side goes on to the stretch check whatever ENTRY_STRETCH_GATE_MODE says; every
            # other side, and an hour with no mapped side, is a direction block. A mapped side that
            # ENTRY_GATE_DIRECTION does not make live is a shadow side: marked here, never traded.
            allowed_side = mapped_side
            other_side = decision.action != f"OPEN_{mapped_side.upper()}"
            if other_side:
                verdict["forced_by"] = "regime_gate"
            else:
                verdict["shadow_side"] = mapped_side not in self._live_sides(mapped_side)
        else:
            allowed_side = str(self._settings.entry_gate_direction or "any").strip().lower()
            other_side = allowed_side != "any" and decision.action != f"OPEN_{allowed_side.upper()}"
            if other_side and self._settings.entry_regime_gate != "off":
                # The LONG-only forward bot: with the regime gate on, the side filter enforces whatever
                # ENTRY_STRETCH_GATE_MODE says. `mode` stays as configured; `forced_by` records why it enforced.
                verdict["forced_by"] = "regime_gate"
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

    def _regime_gate_mode(self) -> str:
        return str(getattr(self._settings, "entry_regime_gate", "off") or "off").strip().lower()

    def _mapped_side(self) -> str:
        """The one side the regime maps to this hour: `"long"`, `"short"`, or `""`.

        Only under `ENTRY_REGIME_GATE=map`; it is the gate context's `allowed_direction`, so a missing
        context, an hour with no mapped condition and every other gate mode give `""`."""
        if self._regime_gate_mode() != "map":
            return ""
        context = self._current_gate_context() or {}
        side = str(context.get("allowed_direction") or "").strip().lower()
        return side if side in _SIDES else ""

    def _live_sides(self, mapped: str | None = None) -> frozenset[str]:
        """The sides this bot may trade now; a mapped side outside this set is a shadow side.

        `ENTRY_GATE_DIRECTION` names the sides the account trades (`any` is both). Under `map` the
        regime narrows that to the mapped side, or to nothing when the hour maps no side that the
        account trades. Pass `mapped` to reuse a side already read from the gate context."""
        configured = str(getattr(self._settings, "entry_gate_direction", "any") or "any").strip().lower()
        configured_sides = frozenset({configured}) if configured in _SIDES else _SIDES
        if self._regime_gate_mode() != "map":
            return configured_sides
        side = self._mapped_side() if mapped is None else mapped
        return frozenset({side}) & configured_sides

    def _ledger_mode(self) -> str:
        return str(getattr(self._settings, "setup_ledger_mode", "off") or "off").strip().lower()

    async def _ledger_snapshot(self) -> dict[str, BucketLedger]:
        """Real and shadow outcome statistics per bucket since `SETUP_LEDGER_SINCE`; `{}` while the ledger is off.

        Only `ledger_block` shadow rows count, so only those are read; `ledger_table` puts them and the
        trades on one timeline, so only a bucket's latest run of block rows can reopen it. A snapshot read
        less than `LEDGER_CACHE_SECONDS` ago is reused without a read. A failed read is logged and reuses
        the last snapshot read without error, whatever its age, so a transient store error lets no entry
        into a blocked bucket (that trade would also end the bucket's reopening run). Before the first good
        read it gives `{}`: every bucket reads `learning` for the tick, so the ledger fails open there (an
        ERROR under `enforce`) and the regime and stretch gates still apply."""
        if self._ledger_mode() == "off":
            return {}
        now = _utc_now()
        last, read_at = self._last_ledger_snapshot, self._last_ledger_read_at
        if last is not None and read_at is not None and (now - read_at).total_seconds() < LEDGER_CACHE_SECONDS:
            return last
        since = self._settings.setup_ledger_since
        try:
            trades = await self._trade_store.closed_trade_rows_since(since)
            shadows = await self._trade_store.shadow_outcomes_since(since, source=SHADOW_BLOCK_SOURCE)
            snapshot = ledger_table(trades, shadows)
        except Exception as exc:  # noqa: BLE001 - a ledger read must never stop the entry path
            failing_open = last is None
            self._logger.log(
                logging.ERROR if failing_open and self._ledger_mode() == "enforce" else logging.WARNING,
                "Setup ledger could not be read (%s: %s); %s",
                type(exc).__name__,
                exc,
                "every bucket reads as learning this tick." if failing_open else "the last snapshot is used this tick.",
            )
            return {} if failing_open else last
        # Replaced, never mutated.
        self._last_ledger_snapshot, self._last_ledger_read_at = snapshot, now
        return snapshot

    def _ledger_bucket(self, side: str, features: dict[str, Any] | None) -> str:
        """`side|condition|atr_regime` for an entry on `side` now: the condition of the gate context set
        before the strategy ran, the ATR regime of the symbol's 3m technicals."""
        kondisi = str((self._current_gate_context() or {}).get("kondisi") or "")
        return bucket_key(side, kondisi, build_setup_profile(features, side)["atr_regime"])

    def _ledger_verdict(
        self,
        side: str,
        features: dict[str, Any] | None,
        ledger: dict[str, BucketLedger],
    ) -> dict[str, Any]:
        bucket = self._ledger_bucket(side, features)
        # `ledger_table` keys only buckets with a counted row; any other bucket has no outcomes yet.
        entry = ledger.get(bucket, EMPTY_BUCKET_LEDGER)
        return {
            "mode": self._ledger_mode(),
            "bucket": bucket,
            "n": entry.real.n,
            "mean_bps": entry.real.mean_bps,
            "upper_bps": entry.real.upper_bps,
            # The two samples the status turns on once the real bound is below the hurdle.
            "shadow_run_n": entry.shadow_run.n,
            "shadow_run_upper_bps": entry.shadow_run.upper_bps,
            "since_block_n": entry.real_since_block.n,
            "since_block_upper_bps": entry.real_since_block.upper_bps,
            "status": ledger_status(
                entry,
                int(self._settings.setup_ledger_min_samples),
                float(self._settings.setup_ledger_hurdle_bps),
            ),
        }

    def _with_ledger_verdict(
        self,
        gate_verdict: dict[str, Any],
        decision: EntryDecision,
        market_context: dict[str, Any],
        ledger: dict[str, BucketLedger],
    ) -> dict[str, Any]:
        """The gate verdict plus `ledger` for an OPEN decision while the ledger is on; otherwise unchanged.

        A new dict, so the stored decision (`payload_json.gate.ledger`) and the outcome read the same verdict."""
        if self._ledger_mode() == "off" or decision.action not in _OPEN_ACTIONS:
            return gate_verdict
        side = "LONG" if decision.action == "OPEN_LONG" else "SHORT"
        features = self._features_for_symbol(market_context, decision.symbol)
        return {**gate_verdict, "ledger": self._ledger_verdict(side, features, ledger)}

    def _shadow_source(self, decision: EntryDecision, gate_verdict: dict[str, Any]) -> str:
        """Why a candidate that cleared every entry check is recorded instead of traded; `""` trades it.

        A shadow side always; a `blocked` ledger bucket only under `enforce` (`shadow` logs it and trades)."""
        # Truthiness, not `is True`: on this path any doubt must end in "not traded".
        if gate_verdict.get("shadow_side"):
            return SHADOW_SIDE_SOURCE
        ledger = gate_verdict.get("ledger")
        if not isinstance(ledger, dict) or ledger.get("status") != STATUS_BLOCKED:
            return ""
        if ledger.get("mode") == "enforce":
            return SHADOW_BLOCK_SOURCE
        self._logger.info(
            "Setup ledger (shadow) would block %s: bucket %s has n=%s, upper bound %s bps below the hurdle.",
            decision.symbol,
            ledger.get("bucket"),
            ledger.get("n"),
            ledger.get("upper_bps"),
        )
        return ""

    def _shadow_row(
        self,
        decision: EntryDecision,
        candidate: EntryCandidate,
        source: str,
        market_context: dict[str, Any],
        decided_at: datetime | None = None,
    ) -> dict[str, Any]:
        """One `shadow_entries` row for a candidate that is recorded, not traded (the store adds `exchange`).

        The floor is the live neutral-exit level, `100 x VOLUME_NEUTRAL_EXIT_MIN_NET_USDT / notional` plus
        the round-trip fee, capped at the candidate's take-profit. `decided_at` defaults to now. Raises
        `ValueError` without a 3m price."""
        features = self._features_for_symbol(market_context, decision.symbol) or {}
        ref_price = float(features.get("last_price") or 0.0)
        if not math.isfinite(ref_price) or ref_price <= 0:
            raise ValueError(f"no 3m last price for {decision.symbol}")
        side = candidate.direction.value
        notional = float(self._settings.volume_profile.margin_per_entry_usdt) * float(self._settings.leverage)
        floor_pct = min(
            candidate.target.take_profit_pct,
            100.0 * float(self._settings.volume_neutral_exit_min_net_usdt) / notional
            + candidate.target.round_trip_fee_pct,
        )
        return {
            "decided_at": decided_at if decided_at is not None else _utc_now(),
            "symbol": decision.symbol,
            "side": side.lower(),
            "bucket_key": self._ledger_bucket(side, features),
            "source": source,
            "ref_price": ref_price,
            "stop_loss_pct": candidate.target.stop_loss_pct,
            "floor_pct": floor_pct,
            "hold_minutes": int(self._settings.position_hold_minutes),
        }

    def _shadow_spacing(self) -> timedelta:
        """The least time between two shadow rows of one (symbol, side, source, bucket).

        A shadow row stands for a trade that was not taken; a real trade holds up to
        `POSITION_HOLD_MINUTES` and is followed by `COOLDOWN_MINUTES_AFTER_ENTRY`, so rows closer than that
        would overlap their walk windows, count one move several times and understate the SE."""
        hold = int(getattr(self._settings, "position_hold_minutes", 0) or 0)
        cooldown = int(getattr(self._settings, "cooldown_minutes_after_entry", 0) or 0)
        return timedelta(minutes=hold + cooldown)

    def _last_shadow_rows(self) -> dict[ShadowKey, datetime]:
        return self._shadow_rows_written_at

    def _shadow_key(self, symbol: str, side: str, source: str, market_context: dict[str, Any]) -> ShadowKey:
        """The spacing key of a shadow row: its symbol, side (lower case, as stored), source and bucket."""
        features = self._features_for_symbol(market_context, symbol)
        return (symbol, side.lower(), source, self._ledger_bucket(side.upper(), features))

    async def _last_shadow_row_at(self, key: ShadowKey) -> datetime | None:
        """`decided_at` of the key's last row: from memory, else read from the store once per process.

        The store read seeds the spacing after a restart. A failed read logs a WARNING and counts as no
        earlier row (the row is written), and it is tried again the next time the key comes up."""
        last = self._last_shadow_rows().get(key)
        if last is not None or key in self._shadow_seeded_keys:
            return last
        try:
            last = await self._trade_store.latest_shadow_decided_at(*key)
        except Exception as exc:  # noqa: BLE001 - a seed read must never stop the entry path
            self._logger.warning(
                "Shadow row spacing for %s could not be read from the store (%s: %s); treated as no earlier row.",
                "|".join(key),
                type(exc).__name__,
                exc,
            )
            return None
        # Replaced, never mutated.
        self._shadow_seeded_keys = self._shadow_seeded_keys | {key}
        if last is not None:
            self._shadow_rows_written_at = {**self._last_shadow_rows(), key: last}
        return last

    async def _spacing_left(self, key: ShadowKey, now: datetime) -> tuple[datetime, timedelta] | None:
        """(last row's `decided_at`, time left) while the key's last row is inside the spacing; else None."""
        last = await self._last_shadow_row_at(key)
        if last is None:
            return None
        left = self._shadow_spacing() - (now - last)
        return (last, left) if left > timedelta(0) else None

    async def _shadow_only_hour_is_spaced(self, market_context: dict[str, Any]) -> bool:
        """True, after one INFO line, when this tick's AI call could change nothing.

        Under `map`, an hour whose mapped side `ENTRY_GATE_DIRECTION` does not make live can trade nothing:
        the mapped side becomes a `shadow_side` row and every other side is blocked. While every symbol's
        row for that side and bucket is inside its spacing, no row would be written either."""
        if self._regime_gate_mode() != "map":
            return False
        mapped = self._mapped_side()
        if not mapped or mapped in self._live_sides(mapped):
            return False
        now = _utc_now()
        waits: list[tuple[ShadowKey, datetime, timedelta]] = []
        for symbol in self._settings.symbols:
            key = self._shadow_key(str(symbol), mapped, SHADOW_SIDE_SOURCE, market_context)
            spaced = await self._spacing_left(key, now)
            if spaced is None:
                return False
            waits.append((key, *spaced))
        if not waits:
            return False
        key, last, left = min(waits, key=lambda wait: wait[2])
        self._logger.info(
            "Shadow-only hour (%s maps %s, which is not traded here): the last shadow row for bucket %s was "
            "decided at %s, %.1f min of the %.0f-minute spacing remain; holding without an AI call.",
            (self._current_gate_context() or {}).get("kondisi") or "?",
            mapped,
            key[3],
            last.isoformat(timespec="seconds"),
            left.total_seconds() / 60.0,
            self._shadow_spacing().total_seconds() / 60.0,
        )
        return True

    async def _record_shadow_entry(
        self,
        decision: EntryDecision,
        candidate: EntryCandidate,
        source: str,
        market_context: dict[str, Any],
    ) -> None:
        """Write the candidate's shadow row, at most one per (symbol, side, source, bucket) per spacing.

        A row inside the spacing is skipped (one INFO line) and a failed write is logged; the caller drops
        the candidate in every case. Only a written row starts the spacing. After a restart the spacing of
        a key is seeded once from the store's newest row for it."""
        key = self._shadow_key(decision.symbol, candidate.direction.value, source, market_context)
        now = _utc_now()
        spaced = await self._spacing_left(key, now)
        if spaced is not None:
            last, left = spaced
            self._logger.info(
                "Entry candidate %s %s is not traded (%s); no shadow row: the last one for bucket %s was decided "
                "at %s, %.1f min of the %.0f-minute spacing remain.",
                decision.symbol,
                candidate.direction.value,
                source,
                key[3],
                last.isoformat(timespec="seconds"),
                left.total_seconds() / 60.0,
                self._shadow_spacing().total_seconds() / 60.0,
            )
            return
        try:
            row = self._shadow_row(decision, candidate, source, market_context, decided_at=now)
            await self._trade_store.save_shadow_entry(row)
        except Exception as exc:  # noqa: BLE001 - the candidate is not traded whether or not the row is saved
            self._logger.warning(
                "Entry candidate %s %s is not traded (%s), but its shadow row could not be saved (%s: %s).",
                decision.symbol,
                candidate.direction.value,
                source,
                type(exc).__name__,
                exc,
            )
            return
        # Replaced, never mutated.
        self._shadow_rows_written_at = {**self._last_shadow_rows(), key: row["decided_at"]}
        self._logger.info(
            "Entry candidate %s %s is not traded (%s): shadow row for bucket %s, stop %.3f%%, floor %.3f%%.",
            decision.symbol,
            candidate.direction.value,
            source,
            row["bucket_key"],
            row["stop_loss_pct"],
            row["floor_pct"],
        )

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
        gate_context = self._current_gate_context()
        if gate_context:
            gate = {**(gate or {}), "regime": gate_context}
        payload_json = {
            "decision": decision.model_dump(),
            "request": request_payload,
            "ai_call_id": ai_call_id,
            "pacing": pacing,
            "gate": gate,
            # Beside the request, never inside it: the legacy request stays byte for byte today's.
            "payload_profile": self._payload_profile(),
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
        market_context: dict[str, Any],
        symbol: str,
        direction: str = "",
        gate_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshots = [
            item
            for item in market_context.get("factor_snapshots", [])
            if isinstance(item, dict) and item.get("symbol") == symbol
        ]
        top = sorted(snapshots, key=lambda item: abs(float(item.get("score") or 0.0)), reverse=True)[:5]
        setup = build_setup_profile(AIOrchestrator._features_for_symbol(market_context, symbol), direction, top)
        # The regime condition the gate read for this entry's hour, and the ledger bucket it files the
        # entry under; both are "" when no gate context was set.
        kondisi = str((gate_context or {}).get("kondisi") or "")
        return {
            "top_factors": [str(item.get("name")) for item in top if item.get("name")],
            "snapshots": top,
            # Persisted so attribution can bucket by the *shape* of the entry,
            # not just its symbol/direction/factors.
            "setup": {
                **setup,
                "kondisi": kondisi,
                "bucket_key": (
                    bucket_key(setup["direction"], kondisi, setup["atr_regime"]) if kondisi else ""
                ),
            },
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
