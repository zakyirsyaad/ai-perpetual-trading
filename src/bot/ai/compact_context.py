"""Parity AI decision context with structural duplicate removal only."""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from typing import Any, Iterable, Mapping, Sequence

from bot.ai.usage import estimate_tokens

# Prompt/contract version stamped on every AI call record.
COMPACT_PROMPT_VERSION = "okx-context-parity-v3"
# The version stamped instead on an entry call that sends the lean payload under the lean system prompt
# (`ENTRY_SYSTEM_PROMPT_LEAN`), so `ai_call_metrics` can tell the two profiles apart.
LEAN_PROMPT_VERSION = "lean-entry-v1"
# The version stamped instead when `AI_LEAN_OBJECTIVE=volume` swaps in the volume prompt
# (`ENTRY_SYSTEM_PROMPT_LEAN_VOLUME`).
LEAN_VOLUME_PROMPT_VERSION = "lean-entry-volume-v1"

# The free-text length contract for an entry decision. It lives here because this
# module builds the prompt that states the limits; `ai/brain.py` imports the same
# numbers to enforce them, so the prompt and the validator can never drift apart.
ENTRY_REASON_MAX_CHARS = 600
ENTRY_LEARNING_NOTE_MAX_CHARS = 500

# `AI_ENTRY_PAYLOAD`: which entry request the model is sent. Recorded on every decision.
PAYLOAD_PROFILE_LEGACY = "legacy"
PAYLOAD_PROFILE_LEAN = "lean"
# The lean objective (spec section 4): HOLD is the expected answer, not a failure to trade.
LEAN_PRIMARY_GOAL = (
    "Open a position only when the setup shows a directional edge larger than the round-trip fee; "
    "HOLD is the expected answer most ticks."
)
# `AI_LEAN_OBJECTIVE`: the two readings of the lean objective. `edge` is the goal above. `volume` trades the
# side the hourly regime offers on a pullback into the mean (the bounce half of what the stretch gate lets
# through) and holds only for a reason the model can name, so the code's gates and the daily cap, not the
# model's caution, set the pace.
LEAN_OBJECTIVE_EDGE = "edge"
LEAN_OBJECTIVE_VOLUME = "volume"
LEAN_VOLUME_PRIMARY_GOAL = (
    "Build volume at the lowest cost per volume: open the allowed side on a pullback into the mean within "
    "the stretch limit; HOLD only for a reason you can name."
)
# `entry_rules.rule`: the stretch gate runs in code after the model has answered, on the absolute
# stretch (a signed -0.9 is beyond a 0.75 limit). Settings only allows lean while that gate enforces.
STRETCH_RULE_TEXT = "an entry with |stretch_atr| above this limit is rejected in code"
# What the lean payload leaves out of `market_context`: the arena's factor and flow records and
# its per-symbol scores. The arena still runs and is still recorded; only the model stops seeing it.
_LEAN_DROPPED_CONTEXT_KEYS = frozenset({"factor_snapshots", "active_flow_signals", "new_flow_signals"})
_LEAN_DROPPED_INTELLIGENCE_KEYS = frozenset({"symbol_scores", "factor_intelligence", "market_flow_signals"})
_LEAN_SIDES = frozenset({"long", "short"})
_LEAN_DIGITS = 4

_TECHNICAL_FIELDS: tuple[tuple[str, int], ...] = (
    ("last_price", 4),
    ("ema_fast", 4),
    ("ema_slow", 4),
    ("rsi_14", 2),
    ("atr_14", 4),
    ("macd_hist", 4),
    ("change_pct_1", 4),
    ("change_pct_5", 4),
    ("volume_change_pct", 2),
    ("funding_rate", 6),
    ("open_interest_change_pct", 3),
    ("spread_bps", 2),
    ("bid_depth_notional", 0),
    ("ask_depth_notional", 0),
)

_FACTOR_FIELDS = ("name", "category", "score", "direction", "percentile")


def compact_json(payload: Any) -> str:
    """Compact, stable-key-ordered JSON — the exact bytes sent and hashed."""
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True, default=str)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(compact_json(payload).encode("utf-8")).hexdigest()


def estimate_payload_tokens(payload: Any) -> int:
    return estimate_tokens(compact_json(payload))


class CompactDecisionContextBuilder:
    """Builds the compact entry/exit payloads handed to the AI brain."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    # ------------------------------------------------------------------
    # entry
    # ------------------------------------------------------------------
    def build_entry_payload(
        self,
        *,
        market_context: dict[str, Any],
        daily_state: Any,
        recent_performance: dict[str, Any],
        trade_memory: dict[str, Any],
        strategy_objective: dict[str, Any],
        arena_reviews: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """The legacy entry request (`AI_ENTRY_PAYLOAD=legacy`); a golden test pins it byte for byte."""
        return {
            "task": self._entry_task(),
            "execution_environment": self._execution_environment(),
            "strategy_objective": deepcopy(_as_dict(strategy_objective)),
            "risk_constraints": self._entry_risk_constraints(daily_state),
            "market_context": self._deduplicated_market_context(market_context),
            "arena_reviews": self._deduplicated_entry_arena_reviews(arena_reviews),
            "recent_executed_trade_performance": deepcopy(_as_dict(recent_performance)),
            "learned_trade_memory": deepcopy(_as_dict(trade_memory)),
            "output_rules": self._entry_output_rules(["OPEN_LONG", "OPEN_SHORT", "HOLD"]),
        }

    def build_lean_entry_payload(
        self,
        *,
        market_context: dict[str, Any],
        daily_state: Any,
        strategy_objective: dict[str, Any],
        allowed_sides: Sequence[str],
        entry_rules: dict[str, Any],
        market_regime: dict[str, Any] | None,
        ledger_lines: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        """The lean entry request (`AI_ENTRY_PAYLOAD=lean`, spec section 4).

        Against the legacy request: no arena reviews, trade memory, recent performance or
        `min_confidence` (the code still enforces it); `market_context` without the factor and flow
        records or `intelligence.symbol_scores`; added `features` (the 3m lean features per symbol),
        `entry_rules`, `market_regime` (only when given) and `setup_ledger` (only with lines, which the
        orchestrator passes only while the ledger enforces).
        `allowed_actions` offers one OPEN per side in `allowed_sides`, then HOLD. Raises `ValueError`
        on an empty side list, an unknown side or a repeated one, so the model is never offered
        nothing but HOLD."""
        sides = [str(side).strip().lower() for side in allowed_sides]
        if not sides or not set(sides) <= _LEAN_SIDES or len(set(sides)) != len(sides):
            raise ValueError(f"lean entry payload needs distinct sides among long/short, got {list(allowed_sides)!r}")
        payload: dict[str, Any] = {
            "task": self._entry_task(),
            "execution_environment": self._execution_environment(),
            "strategy_objective": deepcopy(_as_dict(strategy_objective)),
            "risk_constraints": {
                key: value
                for key, value in self._entry_risk_constraints(daily_state).items()
                if key != "min_confidence"
            },
            "market_context": self._lean_market_context(market_context),
            "features": _rounded_features(_as_dict(market_context).get("lean_features")),
            "entry_rules": deepcopy(_as_dict(entry_rules)),
            "output_rules": self._entry_output_rules([f"OPEN_{side.upper()}" for side in sides] + ["HOLD"]),
        }
        if market_regime:
            payload["market_regime"] = deepcopy(_as_dict(market_regime))
        if ledger_lines:
            payload["setup_ledger"] = [deepcopy(_as_dict(line)) for line in ledger_lines]
        return payload

    def _entry_task(self) -> str:
        return "ai_volume_entry_decision" if str(getattr(self._settings, "strategy_mode", "")) == "ai_volume" else "entry_decision"

    def _execution_environment(self) -> dict[str, Any]:
        return {
            "exchange": str(getattr(self._settings, "exchange_name", "bingx")),
            "mode": "vst_demo",
            "margin_mode": str(getattr(self._settings, "margin_mode", "isolated")),
            "position_mode": str(getattr(self._settings, "position_mode", "hedge")),
            "symbols": [str(item) for item in getattr(self._settings, "symbols", ())],
        }

    @staticmethod
    def _entry_output_rules(allowed_actions: list[str]) -> dict[str, Any]:
        return {
            "allowed_actions": allowed_actions,
            "use_only_allowed_symbols": True,
            "only_vst_demo_trades_are_executed": True,
            "return_one_decision_per_symbol": True,
            "open_requires_explicit_stop_loss_take_profit_and_risk_reward": True,
            "risk_review_required": True,
            "risk_review_approved_false_vetoes_the_candidate": True,
            "open_requires_final_stop_strictly_before_estimated_liquidation_or_hold": True,
            "reason_max_characters": ENTRY_REASON_MAX_CHARS,
            "learning_note_max_characters": ENTRY_LEARNING_NOTE_MAX_CHARS,
        }

    def _lean_market_context(self, market_context: dict[str, Any]) -> dict[str, Any]:
        """The legacy dedupe, less the arena's factor and flow records and per-symbol scores."""
        deduplicated = self._deduplicated_market_context(market_context)
        context = {key: value for key, value in deduplicated.items() if key not in _LEAN_DROPPED_CONTEXT_KEYS}
        context["intelligence"] = {
            key: value
            for key, value in _as_dict(deduplicated.get("intelligence")).items()
            if key not in _LEAN_DROPPED_INTELLIGENCE_KEYS
        }
        return context

    # ------------------------------------------------------------------
    # exit
    # ------------------------------------------------------------------
    def build_exit_payload(
        self,
        *,
        position: dict[str, Any],
        market_context: dict[str, Any],
    ) -> dict[str, Any]:
        symbol = str(position.get("symbol") or "")
        return {
            "task": "exit_decision",
            "position": deepcopy(_as_dict(position)),
            "market_context": self._deduplicated_market_context(market_context, symbol=symbol),
            "constraints": {
                "min_confidence": float(getattr(self._settings, "ai_min_confidence", 0.75)),
                "position_hold_minutes": int(getattr(self._settings, "position_hold_minutes", 60)),
                "taker_fee_pct": float(getattr(self._settings, "taker_fee_pct", 0.0)),
                "round_trip_taker_fee_pct": self._round_trip_fee_pct(),
            },
        }

    def _entry_risk_constraints(self, daily_state: Any) -> dict[str, Any]:
        return {
            "trade_target_per_day": int(getattr(self._settings, "trade_target_per_day", 0)),
            "closed_trades_today": int(getattr(daily_state, "closed_trades", 0)),
            "stop_losses_today": int(getattr(daily_state, "stop_losses", 0)),
            "max_stop_losses_per_day": int(getattr(self._settings, "max_stop_loss_per_day", 0)),
            "consecutive_losses_today": int(getattr(daily_state, "consecutive_losses", 0)),
            "max_consecutive_losses": int(getattr(self._settings, "max_consecutive_losses", 0)),
            "day_pnl_usdt": float(getattr(daily_state, "total_pnl_usdt", 0.0)),
            "max_daily_drawdown_usdt": float(getattr(self._settings, "max_daily_drawdown_usdt", 0.0)),
            "configured_leverage": int(getattr(self._settings, "leverage", 1)),
            "configured_position_usdt": float(getattr(self._settings, "position_usdt", 0.0)),
            "min_confidence": float(getattr(self._settings, "ai_min_confidence", 0.75)),
            "min_fee_adjusted_risk_reward": float(getattr(self._settings, "ai_min_fee_adjusted_rr", 0.0)),
            "round_trip_taker_fee_pct": self._round_trip_fee_pct(),
        }

    def _deduplicated_market_context(self, market_context: dict[str, Any], symbol: str | None = None) -> dict[str, Any]:
        """Keep complete values once, removing only mirrored factor/flow arrays."""
        context = deepcopy(_as_dict(market_context))
        # Lean features belong to the lean entry payload alone. Dropping them here keeps
        # the legacy entry payload and the exit payload exactly as they were.
        context.pop("lean_features", None)
        # A provider whose sub-requests all failed still returns a skeleton (Dune)
        # or an empty dict (TradingView). Forwarding those verbatim shows the model
        # a provider that is present but carries nothing, which the entry prompt
        # penalises as an empty provider. Providers that returned data keep full
        # parity, and non-provider metadata such as collected_at is untouched.
        external = _as_dict(context.get("external_signals"))
        if external:
            for provider in _EXTERNAL_PROVIDER_KEYS:
                if provider in external and not _has_signal_content(external[provider]):
                    external.pop(provider)
            context["external_signals"] = external
        if symbol:
            context["technicals"] = [
                item for item in _as_list(context.get("technicals")) if _as_dict(item).get("symbol") == symbol
            ]
            context["factor_snapshots"] = [
                item for item in _as_list(context.get("factor_snapshots")) if _as_dict(item).get("symbol") == symbol
            ]
            context["active_flow_signals"] = [
                item for item in _as_list(context.get("active_flow_signals")) if _as_dict(item).get("symbol") == symbol
            ]
            context["new_flow_signals"] = [
                item for item in _as_list(context.get("new_flow_signals")) if _as_dict(item).get("symbol") == symbol
            ]
            constraints = _as_dict(context.get("liquidation_constraints"))
            context["liquidation_constraints"] = {symbol: deepcopy(constraints[symbol])} if symbol in constraints else {}
        intelligence = _as_dict(context.get("intelligence"))
        if symbol:
            symbol_scores = _as_dict(intelligence.get("symbol_scores"))
            intelligence["symbol_scores"] = (
                {symbol: deepcopy(symbol_scores[symbol])} if symbol in symbol_scores else {}
            )
        factor_intelligence = _as_dict(intelligence.get("factor_intelligence"))
        for key in ("snapshots", "by_symbol", "top_factor_details", "top_factors"):
            factor_intelligence.pop(key, None)
        if factor_intelligence:
            intelligence["factor_intelligence"] = factor_intelligence
        else:
            intelligence.pop("factor_intelligence", None)
        flow = _as_dict(intelligence.get("market_flow_signals"))
        for key in ("active", "new_triggers", "by_symbol", "trigger_names"):
            flow.pop(key, None)
        if flow:
            intelligence["market_flow_signals"] = flow
        else:
            intelligence.pop("market_flow_signals", None)
        new_flows = _as_list(context.get("new_flow_signals"))
        new_flow_records = {compact_json(item) for item in new_flows}
        context["active_flow_signals"] = [
            item for item in _as_list(context.get("active_flow_signals")) if compact_json(item) not in new_flow_records
        ]
        context["intelligence"] = intelligence
        return context

    @staticmethod
    def _deduplicated_entry_arena_reviews(arena_reviews: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        """Keep arena conclusions while excluding flow records already in market_context."""
        reviews = deepcopy(arena_reviews or [])
        for review in reviews:
            payload = _as_dict(_as_dict(review).get("payload"))
            if _as_dict(review).get("role") == "SignalDesigner":
                for key in ("active", "new_triggers", "by_symbol", "trigger_names"):
                    payload.pop(key, None)
                review["summary"] = "Market-flow review complete; trigger records are in market_context."
            if _as_dict(review).get("role") == "MarketAnalyzer":
                payload.pop("risk_flags", None)
            review["payload"] = payload
        return reviews

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _factors_by_symbol(market_context: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """Read factor snapshots from exactly ONE source collection.

        The runtime context repeats the same snapshots under
        `factor_snapshots`, `intelligence.factor_intelligence.snapshots`,
        `.by_symbol`, and `.top_factor_details`. Reading one and grouping it
        here is what removes the duplication from the AI payload.
        """
        snapshots = _as_list(market_context.get("factor_snapshots"))
        if not snapshots:
            factor_intelligence = _as_dict(_as_dict(market_context.get("intelligence")).get("factor_intelligence"))
            snapshots = _as_list(factor_intelligence.get("snapshots"))
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in snapshots:
            if not isinstance(item, dict):
                continue
            grouped.setdefault(str(item.get("symbol") or ""), []).append(item)
        return grouped

    @staticmethod
    def _compact_technicals(features: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {}
        for field, digits in _TECHNICAL_FIELDS:
            value = features.get(field)
            if value in (None, ""):
                continue
            compact[field] = _round_value(value, digits)
        return compact

    @staticmethod
    def _factor_aggregates(factors: list[dict[str, Any]]) -> dict[str, Any]:
        if not factors:
            return {
                "factor_count": 0,
                "category_scores": {},
                "directional_agreement": 0.0,
                "mean_score": 0.0,
                "strongest_positive": None,
                "strongest_negative": None,
            }
        by_category: dict[str, list[float]] = {}
        longs = 0
        shorts = 0
        total = 0.0
        for item in factors:
            score = _float(item.get("score"))
            total += score
            by_category.setdefault(str(item.get("category") or "other"), []).append(score)
            direction = str(item.get("direction") or "").upper()
            if direction == "LONG":
                longs += 1
            elif direction == "SHORT":
                shorts += 1
        directional = longs + shorts
        agreement = ((longs - shorts) / directional) if directional else 0.0
        ranked = sorted(factors, key=lambda item: _float(item.get("score")))
        return {
            "factor_count": len(factors),
            "category_scores": {
                category: round(sum(values) / len(values), 4) for category, values in sorted(by_category.items())
            },
            "directional_agreement": round(agreement, 4),
            "mean_score": round(total / len(factors), 4),
            "strongest_negative": _factor_label(ranked[0]),
            "strongest_positive": _factor_label(ranked[-1]),
        }

    def _ranked_factors(self, factors: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
        cap = limit if limit is not None else int(
            getattr(self._settings, "ai_compact_max_factors_per_symbol", 6)
        )
        ranked = sorted(factors, key=lambda item: abs(_float(item.get("score"))), reverse=True)[:cap]
        return [
            {
                "name": str(item.get("name") or ""),
                "category": str(item.get("category") or ""),
                "score": round(_float(item.get("score")), 4),
                "direction": str(item.get("direction") or ""),
                "percentile": round(_float(item.get("percentile")), 3),
            }
            for item in ranked
        ]

    def _compact_flow_signals(self, market_context: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
        cap = limit if limit is not None else int(getattr(self._settings, "ai_compact_max_flow_signals", 5))
        intelligence = _as_dict(market_context.get("intelligence"))
        flow = _as_dict(intelligence.get("market_flow_signals"))
        # New triggers first: an edge-triggered signal carries more information
        # than one that has been active for many ticks.
        candidates = _as_list(flow.get("new_triggers")) + _as_list(market_context.get("active_flow_signals"))
        if not candidates:
            candidates = _as_list(flow.get("active"))
        seen: set[tuple[str, str]] = set()
        compact: list[dict[str, Any]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("symbol") or ""), str(item.get("name") or ""))
            if key in seen:
                continue
            seen.add(key)
            compact.append(
                {
                    "symbol": key[0],
                    "name": key[1],
                    "direction": str(item.get("direction") or ""),
                    "strength": round(_float(item.get("strength")), 3),
                }
            )
            if len(compact) >= cap:
                break
        return compact

    @staticmethod
    def _compact_external_signals(market_context: dict[str, Any]) -> dict[str, Any]:
        external = _as_dict(market_context.get("external_signals"))
        compact: dict[str, Any] = {}
        for provider in ("coinmarketcap", "dune", "tradingview"):
            payload = external.get(provider)
            # Only available providers are represented at all; an absent provider
            # is simply omitted rather than sent as an empty object.
            if not payload:
                continue
            compact[provider] = _summarize_provider(payload)
        if external.get("collected_at"):
            compact["collected_at"] = str(external["collected_at"])
        return compact

    @staticmethod
    def _compact_risk_state(daily_state: Any) -> dict[str, Any]:
        return {
            "closed_trades_today": int(getattr(daily_state, "closed_trades", 0) or 0),
            "stop_losses_today": int(getattr(daily_state, "stop_losses", 0) or 0),
            "consecutive_losses_today": int(getattr(daily_state, "consecutive_losses", 0) or 0),
            "day_pnl_usdt": round(_float(getattr(daily_state, "total_pnl_usdt", 0.0)), 4),
        }

    def _fee_assumptions(self) -> dict[str, Any]:
        return {
            "taker_fee_pct": round(_float(getattr(self._settings, "taker_fee_pct", 0.0)), 5),
            "maker_fee_pct": round(_float(getattr(self._settings, "maker_fee_pct", 0.0)), 5),
            "round_trip_taker_fee_pct": self._round_trip_fee_pct(),
            "leverage": int(getattr(self._settings, "leverage", 1)),
        }

    def _round_trip_fee_pct(self) -> float:
        return round(_float(getattr(self._settings, "taker_fee_pct", 0.0)) * 2.0, 5)

    @staticmethod
    def _compact_objective(strategy_objective: dict[str, Any]) -> dict[str, Any]:
        objective = _as_dict(strategy_objective)
        compact = {
            "mode": str(objective.get("mode") or "ai_volume"),
            "primary_goal": str(objective.get("primary_goal") or "")[:240],
        }
        profile = _as_dict(objective.get("volume_profile"))
        if profile:
            compact["margin_per_entry_usdt"] = _round_value(
                profile.get("margin_per_entry_usdt") or profile.get("margin_usdt"), 2
            )
        return compact

    @staticmethod
    def _compact_recent_performance(recent_performance: dict[str, Any]) -> dict[str, Any]:
        performance = _as_dict(recent_performance)
        return {
            "total_trades": int(_float(performance.get("total_trades"))),
            "total_pnl_usdt": round(_float(performance.get("total_pnl_usdt")), 4),
        }

    def _compact_trade_memory(self, trade_memory: dict[str, Any]) -> dict[str, Any]:
        memory = _as_dict(trade_memory)
        cap = int(getattr(self._settings, "ai_compact_max_memory_lessons", 3))
        return {
            "sample_size": int(_float(memory.get("sample_size"))),
            "net_pnl_usdt": round(_float(memory.get("net_pnl_usdt")), 4),
            "fee_impact_usdt": round(_float(memory.get("fee_impact_usdt")), 4),
            "lessons": [str(item)[:200] for item in _as_list(memory.get("lessons"))[:cap]],
            "winning_patterns": _compact_patterns(_as_list(memory.get("winning_patterns")), cap),
            "losing_patterns": _compact_patterns(_as_list(memory.get("losing_patterns")), cap),
        }


def _compact_patterns(patterns: Iterable[Any], cap: int) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in list(patterns)[:cap]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "pattern": str(item.get("pattern") or item.get("name") or "")[:80],
                "trades": int(_float(item.get("trades"))),
                "net_usdt": round(_float(item.get("net") if "net" in item else item.get("net_usdt")), 4),
            }
        )
    return compact


def lean_objective(settings: Any) -> str:
    """`AI_LEAN_OBJECTIVE`: `volume`, or `edge` for anything else (a settings object without it included)."""
    value = str(getattr(settings, "ai_lean_objective", LEAN_OBJECTIVE_EDGE) or "").strip().lower()
    return LEAN_OBJECTIVE_VOLUME if value == LEAN_OBJECTIVE_VOLUME else LEAN_OBJECTIVE_EDGE


def lean_market_regime(gate_context: Mapping[str, Any] | None, last_price: Any) -> dict[str, Any]:
    """The lean payload's `market_regime`: the regime gate's verdict without `allowed`, plus
    `price_vs_sma20_1h_atr` = (last price - 1h SMA20) / 1h ATR14, rounded to 4 places.

    `last_price` is the 3m last price of the gated symbol. The distance is None when any of its
    inputs is missing, non-finite or not positive (a zero ATR included). `{}` without a gate
    context, which the builder leaves out rather than send an empty block."""
    if not gate_context:
        return {}
    regime = {key: deepcopy(value) for key, value in gate_context.items() if key != "allowed"}
    last = _positive_or_none(last_price)
    sma20 = _positive_or_none(gate_context.get("sma20_1h"))
    atr = _positive_or_none(gate_context.get("atr14_1h"))
    regime["price_vs_sma20_1h_atr"] = (
        round((last - sma20) / atr, _LEAN_DIGITS) if last is not None and sma20 is not None and atr is not None else None
    )
    return regime


def _positive_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _rounded_features(lean_features: Any) -> dict[str, Any]:
    """Each symbol's lean features with every number rounded to 4 places; None and text unchanged."""
    return {
        str(symbol): {key: _round_value(value, _LEAN_DIGITS) for key, value in _as_dict(features).items()}
        for symbol, features in _as_dict(lean_features).items()
    }


def _factor_label(item: dict[str, Any]) -> dict[str, Any] | None:
    if not item:
        return None
    return {"name": str(item.get("name") or ""), "score": round(_float(item.get("score")), 4)}


# Keys that carry decision-relevant meaning. Counting a nested provider block as
# `BTC{10}` is compact but throws away exactly the signal the model needs, so
# these are projected through by name (matched case-insensitively on substrings).
_SEMANTIC_KEY_HINTS: tuple[str, ...] = (
    "bias",
    "direction",
    "signal",
    "recommendation",
    "summary",
    "trend",
    "regime",
    "confidence",
    "score",
    "state",
    "liquidity",
    "sentiment",
    "percent_change",
    "change_pct",
    "price",
    "volume",
    "dominance",
    "funding",
    "open_interest",
    "value",
    "rank",
)

_MAX_PROVIDER_ENTRIES = 6
_MAX_NESTED_ENTRIES = 5
# Dune nests provider -> builtin_signals -> <signal> -> rows -> row, so the
# limit must reach the row itself or the bias/confidence never arrive.
_MAX_PROVIDER_DEPTH = 5


_EXTERNAL_PROVIDER_KEYS = ("coinmarketcap", "dune", "tradingview")


def _has_signal_content(payload: Any) -> bool:
    """True when a provider payload carries at least one real value.

    Containers are transparent: only what they hold counts, so a skeleton of
    empty dicts reads as no content. Scalars count, including 0 and False, which
    are meaningful readings rather than emptiness.
    """
    if isinstance(payload, dict):
        return any(_has_signal_content(value) for value in payload.values())
    if isinstance(payload, (list, tuple, set)):
        return any(_has_signal_content(value) for value in payload)
    if payload is None:
        return False
    if isinstance(payload, str):
        return bool(payload.strip())
    return True


def _summarize_provider(payload: Any) -> Any:
    """Bounded semantic projection of one external provider's response.

    Keeps direction/bias/confidence/liquidity/price-change style fields — the
    things the model actually reasons over — while staying inside the payload
    budget. Only unnamed bulk collections degrade to a count.
    """
    return _project(payload, depth=0)


def _project(value: Any, *, depth: int) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return _round_value(value, 6)
    if isinstance(value, str):
        return value[:120]
    if value is None:
        return None
    if isinstance(value, dict):
        if depth >= _MAX_PROVIDER_DEPTH:
            return f"{{{len(value)}}}"
        limit = _MAX_PROVIDER_ENTRIES if depth == 0 else _MAX_NESTED_ENTRIES
        projected: dict[str, Any] = {}
        for key in _ranked_keys(value, limit):
            projected[key] = _project(value[key], depth=depth + 1)
        omitted = len(value) - len(projected)
        if omitted > 0:
            projected["_omitted"] = omitted
        return projected
    if isinstance(value, (list, tuple)):
        items = list(value)
        if not items:
            return []
        if depth >= _MAX_PROVIDER_DEPTH:
            return f"[{len(items)}]"
        head = [_project(item, depth=depth + 1) for item in items[:2]]
        if len(items) > 2:
            head.append(f"+{len(items) - 2} more")
        return head
    return str(value)[:120]


def _ranked_keys(payload: dict[str, Any], limit: int) -> list[str]:
    """Semantic keys first, then remaining scalars, capped at `limit`."""
    semantic: list[str] = []
    other: list[str] = []
    for key in sorted(payload):
        lowered = str(key).lower()
        if any(hint in lowered for hint in _SEMANTIC_KEY_HINTS):
            semantic.append(key)
        else:
            other.append(key)
    ranked = semantic + other
    return ranked[:limit]


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round_value(value: Any, digits: int) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if digits <= 0:
        return int(round(float(value)))
    return round(float(value), digits)
