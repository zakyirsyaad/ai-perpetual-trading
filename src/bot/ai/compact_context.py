"""Compact AI decision context.

The full market context stays available to deterministic execution, factor
attribution, reporting, and audit storage. Only the *AI representation* is
compacted here: duplicated factor collections are collapsed into aggregates
plus one small ranked list per symbol, flow signals are capped, and external
provider payloads become one-line summaries.

Engineering budgets (see plan §4):
- two-symbol entry input at or below ~4,000 estimated tokens
- one-symbol exit input at or below ~1,500 estimated tokens
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from bot.ai.usage import estimate_tokens

# Prompt/contract version stamped on every AI call record.
COMPACT_PROMPT_VERSION = "compact-v1"

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
    ) -> dict[str, Any]:
        technicals = _as_list(market_context.get("technicals"))
        intelligence = _as_dict(market_context.get("intelligence"))
        factors_by_symbol = self._factors_by_symbol(market_context)
        composite_scores = {
            symbol: round(_float(scores.get("composite_score")), 4)
            for symbol, scores in _as_dict(intelligence.get("symbol_scores")).items()
        }

        symbols: list[dict[str, Any]] = []
        for features in technicals:
            symbol = str(features.get("symbol") or "")
            if not symbol:
                continue
            symbol_factors = factors_by_symbol.get(symbol, [])
            symbols.append(
                {
                    "symbol": symbol,
                    "technicals": self._compact_technicals(features),
                    "factor_aggregates": self._factor_aggregates(symbol_factors),
                    "top_factors": self._ranked_factors(symbol_factors),
                    "composite_score": composite_scores.get(symbol, 0.0),
                }
            )

        return {
            "task": "entry_decision",
            "execution_environment": {
                "exchange": str(getattr(self._settings, "exchange_name", "bingx")),
                "mode": "vst_demo",
                "margin_mode": str(getattr(self._settings, "margin_mode", "isolated")),
                "position_mode": str(getattr(self._settings, "position_mode", "hedge")),
            },
            "allowed_symbols": [str(item) for item in getattr(self._settings, "symbols", ())],
            "strategy_objective": self._compact_objective(strategy_objective),
            "market": {
                "regime": str(intelligence.get("regime") or "unknown"),
                "risk_flags": [str(flag) for flag in _as_list(intelligence.get("risk_flags"))[:8]],
                "composite_scores": composite_scores,
            },
            "symbols": symbols,
            "flow_signals": self._compact_flow_signals(market_context),
            "external_signals": self._compact_external_signals(market_context),
            "risk_state": self._compact_risk_state(daily_state),
            "fees": self._fee_assumptions(),
            "recent_performance": self._compact_recent_performance(recent_performance),
            "trade_memory": self._compact_trade_memory(trade_memory),
            "output_rules": {
                "one_decision_per_allowed_symbol": True,
                "open_requires_stop_loss_pct_take_profit_pct": True,
                "risk_review_required": True,
                "risk_review_approved_false_vetoes_the_candidate": True,
            },
        }

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
        technicals = _as_list(market_context.get("technicals"))
        features = next((item for item in technicals if str(item.get("symbol")) == symbol), None)
        intelligence = _as_dict(market_context.get("intelligence"))
        symbol_factors = self._factors_by_symbol(market_context).get(symbol, [])
        return {
            "task": "exit_decision",
            "position": {key: _round_value(value, 6) for key, value in position.items()},
            "market": {
                "regime": str(intelligence.get("regime") or "unknown"),
                "risk_flags": [str(flag) for flag in _as_list(intelligence.get("risk_flags"))[:5]],
                "composite_score": round(
                    _float(_as_dict(_as_dict(intelligence.get("symbol_scores")).get(symbol)).get("composite_score")),
                    4,
                ),
            },
            "technicals": self._compact_technicals(features or {}),
            "factor_aggregates": self._factor_aggregates(symbol_factors),
            "top_factors": self._ranked_factors(symbol_factors, limit=4),
            "flow_signals": self._compact_flow_signals(market_context, limit=3),
            "constraints": {
                "position_hold_minutes": int(getattr(self._settings, "position_hold_minutes", 60)),
                "round_trip_taker_fee_pct": self._round_trip_fee_pct(),
            },
        }

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
