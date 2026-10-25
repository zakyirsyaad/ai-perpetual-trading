"""The lean AI entry request (`AI_ENTRY_PAYLOAD=lean`), as a mixin of `AIOrchestrator`.

`LeanPayloadMixin` builds the request the lean profile sends instead of the legacy one: the sides it offers,
its stretch rule, its objective and the payload itself (`CompactDecisionContextBuilder.build_lean_entry_payload`).
It expects the class it is mixed into to provide `_settings`, `_logger`, `_compact_context`, `_strategy_mode`,
`_features_for_symbol`, `_current_gate_context`, `_regime_gate_mode`, `_mapped_side`, `_live_sides` and
`_ledger_lines`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from bot.ai.compact_context import (
    LEAN_OBJECTIVE_VOLUME,
    LEAN_PRIMARY_GOAL,
    LEAN_VOLUME_PRIMARY_GOAL,
    PAYLOAD_PROFILE_LEAN,
    PAYLOAD_PROFILE_LEGACY,
    STRETCH_RULE_TEXT,
    lean_market_regime,
    lean_objective,
)
from bot.arena.setup_profile import build_setup_profile
from bot.core.setup_ledger import BucketLedger

if TYPE_CHECKING:
    from bot.risk.risk_manager import DailyRiskState


class LeanPayloadMixin:
    """The lean entry request and the profile switch that selects it."""

    def _payload_profile(self) -> str:
        """`AI_ENTRY_PAYLOAD`: `lean`, or `legacy` for anything else (an unset setting included)."""
        profile = str(getattr(self._settings, "ai_entry_payload", PAYLOAD_PROFILE_LEGACY) or "").strip().lower()
        return PAYLOAD_PROFILE_LEAN if profile == PAYLOAD_PROFILE_LEAN else PAYLOAD_PROFILE_LEGACY

    def _lean_sides_offered(self) -> list[str]:
        """`_lean_allowed_sides`, with one INFO line when there is none: the tick then holds without a call."""
        sides = self._lean_allowed_sides()
        if not sides:
            context = self._current_gate_context() or {}
            self._logger.info(
                "Lean entry payload has no side to offer (regime gate %s, reason %s); holding without an AI call.",
                self._regime_gate_mode(),
                context.get("reason") or "no gate context",
            )
        return sides

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

    def _primary_symbol(self) -> str:
        symbols = list(getattr(self._settings, "symbols", ()) or ())
        return str(symbols[0]) if symbols else ""

    def _lean_strategy_objective_payload(self) -> dict[str, Any]:
        """The lean objective. `edge` (the default): an entry needs an edge beyond the fee, HOLD is the usual
        answer. `volume` (`AI_LEAN_OBJECTIVE`): open the offered side on a pullback into the mean, HOLD for a
        named reason. Either way the "every tick" and "do not sit out" notes are gone and the volume profile
        stays as information."""
        goal = (
            LEAN_VOLUME_PRIMARY_GOAL if lean_objective(self._settings) == LEAN_OBJECTIVE_VOLUME else LEAN_PRIMARY_GOAL
        )
        if self._strategy_mode != "ai_volume":
            return {"mode": "directional_ai", "primary_goal": goal}
        return {
            "mode": "ai_volume",
            "primary_goal": goal,
            "volume_profile": self._settings.volume_profile.as_log_fields(),
        }
