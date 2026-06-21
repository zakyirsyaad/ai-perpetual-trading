from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bot.arena.attribution import AttributionSummary, DiagnosisGenerator
from bot.volume import VolumeBuildProfile


@dataclass(frozen=True, slots=True)
class ArenaReview:
    role: str
    summary: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "summary": self.summary, "payload": self.payload}


@dataclass(frozen=True, slots=True)
class AgentRoute:
    primary_agent: str
    reason: str
    supporting_agents: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_agent": self.primary_agent,
            "reason": self.reason,
            "supporting_agents": list(self.supporting_agents),
        }


@dataclass(frozen=True, slots=True)
class SignalConditionConfig:
    daily_volume_target_usdt: float
    breakeven_required: bool
    max_loss_mode: str
    trigger_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "daily_volume_target_usdt": self.daily_volume_target_usdt,
            "breakeven_required": self.breakeven_required,
            "max_loss_mode": self.max_loss_mode,
            "trigger_names": list(self.trigger_names),
        }


class HyperAI:
    """Backend-only router for arena workflows."""

    def route(self, goal: str) -> AgentRoute:
        text = goal.lower()
        if any(token in text for token in ("volume", "trigger", "signal", "flow")):
            return AgentRoute(
                primary_agent="SignalAI",
                reason="Goal is dominated by market-flow trigger and volume target configuration.",
                supporting_agents=("PromptAI", "ProgramAI", "AttributionAI"),
            )
        if "program" in text or "python" in text:
            return AgentRoute(
                primary_agent="ProgramAI",
                reason="Goal asks for deterministic program strategy logic.",
                supporting_agents=("SignalAI", "AttributionAI"),
            )
        if "diagnos" in text or "performance" in text or "pnl" in text:
            return AgentRoute(
                primary_agent="AttributionAI",
                reason="Goal asks for performance diagnosis.",
                supporting_agents=("SignalAI", "PromptAI"),
            )
        return AgentRoute(
            primary_agent="PromptAI",
            reason="Goal is best handled by strategy-prompt refinement.",
            supporting_agents=("SignalAI", "AttributionAI"),
        )


class SignalAI:
    """Generate backend signal settings from an operator goal."""

    def design_conditions(self, goal: str) -> SignalConditionConfig:
        target = max(_numbers_from_goal(goal, floor=1_000.0) or [1_000_000.0])
        if "daily" in goal.lower() or "perhari" in goal.lower() or "per day" in goal.lower():
            daily_target = target
        else:
            daily_target = max(target, 1_000_000.0)
        return SignalConditionConfig(
            daily_volume_target_usdt=daily_target,
            breakeven_required="breakeven" in goal.lower() or "break even" in goal.lower(),
            max_loss_mode="fee-aware breakeven with hard daily fee budget",
            trigger_names=(
                "order_flow_imbalance",
                "open_interest_surge",
                "funding_extreme",
                "factor_percentile_breakout",
                "volume_spike",
            ),
        )


class PromptAI:
    """Build structured AI-trader instructions for GrokBrain."""

    def build_prompt(self, goal: str) -> str:
        return (
            "Prioritize capital preservation while pursuing the requested volume goal. "
            "Only approve entries when factor quality, market-flow triggers, spread, depth, "
            "funding, liquidation distance, and cooldown checks are aligned. "
            "Prefer breakeven exits after round-trip fees; reject churn when expected edge "
            f"does not cover fees. Operator goal: {goal}"
        )


class ProgramAI:
    """Emit a Program Trader skeleton that returns this repo's signal shape."""

    def strategy_template(self, goal: str) -> str:
        return f'''"""Program Trader strategy skeleton generated from goal: {goal}."""

def build_signal(context):
    factors = context.get("factors", [])
    triggers = set(context.get("trigger_names", []))
    if "factor_percentile_breakout" not in triggers:
        return {{"action": "HOLD", "confidence": 0.0, "reason": "No factor percentile edge"}}
    top = max(factors, key=lambda item: abs(item.get("score", 0.0)), default={{}})
    direction = top.get("direction", "MIXED")
    if direction not in {{"LONG", "SHORT"}}:
        return {{"action": "HOLD", "confidence": 0.0, "reason": "No directional factor"}}
    return {{
        "action": direction,
        "confidence": min(abs(float(top.get("score", 0.0))), 0.95),
        "stop_loss_pct": 0.12,
        "take_profit_pct": 0.16,
        "reason": "Factor percentile edge with tight breakeven-capable exits",
    }}
'''


class AttributionAI:
    """Generate deterministic diagnosis prompts from stored metrics only."""

    def diagnosis_checklist(self, goal: str) -> list[str]:
        return [
            "Factor PnL and IC/ICIR by factor name before increasing exposure.",
            "Trigger quality by order-flow, OI, funding, volume, and percentile breakout.",
            "Breakeven exit rate, neutral-exit slippage, and fee spend versus daily budget.",
            "Hourly PnL and loss clusters against the operator goal: " + goal,
        ]


class MarketAnalyzer:
    def review(self, market_context: dict[str, Any]) -> ArenaReview:
        intelligence = market_context.get("intelligence") or {}
        factors = intelligence.get("factor_intelligence") or {}
        top_factors = factors.get("top_factors") or []
        risk_flags = intelligence.get("risk_flags") or []
        return ArenaReview(
            role="MarketAnalyzer",
            summary=f"Regime={intelligence.get('regime', 'unknown')} top_factors={top_factors[:4]}",
            payload={"top_factors": top_factors[:8], "risk_flags": risk_flags},
        )


def _numbers_from_goal(goal: str, *, floor: float = 0.0) -> list[float]:
    values: list[float] = []
    for raw in re.findall(r"\d[\d,_.]*", goal):
        normalized = raw.replace(",", "").replace("_", "")
        try:
            value = float(normalized)
        except ValueError:
            continue
        if value >= floor:
            values.append(value)
    return values


class SignalDesigner:
    def review(self, market_context: dict[str, Any]) -> ArenaReview:
        flow = (market_context.get("intelligence") or {}).get("market_flow_signals") or {}
        triggers = flow.get("trigger_names") or []
        return ArenaReview(
            role="SignalDesigner",
            summary=f"Active market-flow triggers: {', '.join(triggers) if triggers else 'none'}",
            payload=flow,
        )


class VolumeRiskReviewer:
    def review(
        self,
        profile: VolumeBuildProfile,
        *,
        daily_volume_usdt: float,
        estimated_fee_spent_usdt: float,
    ) -> ArenaReview:
        fields = profile.as_log_fields()
        remaining = max(0.0, float(profile.target_volume_usdt) - daily_volume_usdt)
        return ArenaReview(
            role="VolumeRiskReviewer",
            summary=(
                f"daily_volume={daily_volume_usdt:.2f} remaining={remaining:.2f} "
                f"fee_estimate={estimated_fee_spent_usdt:.2f}"
            ),
            payload={
                "volume_profile": fields,
                "daily_volume_usdt": daily_volume_usdt,
                "remaining_volume_usdt": remaining,
                "estimated_fee_spent_usdt": estimated_fee_spent_usdt,
            },
        )


class AttributionDiagnoser:
    def review(self, summary: AttributionSummary) -> ArenaReview:
        cards = DiagnosisGenerator().generate(summary)
        return ArenaReview(
            role="AttributionDiagnoser",
            summary=f"Generated {len(cards)} attribution diagnosis card(s).",
            payload={"cards": [card.to_dict() for card in cards]},
        )
