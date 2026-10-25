"""The entry gate's side and stretch checks, as a mixin of `AIOrchestrator`.

The side filter (`ENTRY_GATE_DIRECTION`, narrowed by the regime gate under `ENTRY_REGIME_GATE=map`) and the
stretch gate (`ENTRY_STRETCH_GATE_*`) judge each OPEN decision before it can become a candidate; the
verdict is recorded at `ai_decisions.payload_json.gate`. `EntryGateMixin` expects the class it is mixed
into to provide `_settings`, `_features_for_symbol` and `_current_gate_context`.
"""

from __future__ import annotations

from typing import Any

from bot.ai.brain import EntryDecision
from bot.ai.shadow_ledger import OPEN_ACTIONS
from bot.arena.setup_profile import build_setup_profile

_SIDES = frozenset({"long", "short"})


class EntryGateMixin:
    """The side filter, the regime gate's side mapping and the stretch gate."""

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
        mode = self._stretch_gate_mode()
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
        side_fields, allowed_side, other_side = self._resolve_gate_side(decision)
        verdict.update(side_fields)
        if allowed_side is None:
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

    def _stretch_gate_mode(self) -> str:
        return str(getattr(self._settings, "entry_stretch_gate_mode", "off") or "off").strip().lower()

    def _resolve_gate_side(self, decision: EntryDecision) -> tuple[dict[str, Any], str | None, bool]:
        """The side half of the entry gate: (verdict fields, allowed side, whether the decision is on another side).

        The allowed side is `None` when the verdict is already final: a HOLD, or the stretch gate off with no
        regime gate to force the side filter. The fields come in the verdict's key order: `mapped_side` and
        `shadow_side` under `map`, then `forced_by` when the regime gate forces the side filter."""
        fields: dict[str, Any] = {}
        # Under `map` the regime picks the side. The gate context is read once, here, so the side
        # and the decision's record come from the same hour.
        mapped_gate = self._regime_gate_mode() == "map"
        mapped_side = self._mapped_side() if mapped_gate else ""
        if mapped_gate:
            fields["mapped_side"] = mapped_side
            fields["shadow_side"] = False
        if decision.action not in OPEN_ACTIONS:
            return fields, None, False
        if mapped_gate:
            # The mapped side goes on to the stretch check whatever ENTRY_STRETCH_GATE_MODE says; every
            # other side, and an hour with no mapped side, is a direction block. A mapped side that
            # ENTRY_GATE_DIRECTION does not make live is a shadow side: marked here, never traded.
            other_side = decision.action != f"OPEN_{mapped_side.upper()}"
            if other_side:
                fields["forced_by"] = "regime_gate"
            else:
                fields["shadow_side"] = mapped_side not in self._live_sides(mapped_side)
            return fields, mapped_side, other_side
        allowed_side = str(self._settings.entry_gate_direction or "any").strip().lower()
        other_side = allowed_side != "any" and decision.action != f"OPEN_{allowed_side.upper()}"
        if other_side and self._settings.entry_regime_gate != "off":
            # The LONG-only forward bot: with the regime gate on, the side filter enforces whatever
            # ENTRY_STRETCH_GATE_MODE says. `mode` stays as configured; `forced_by` records why it enforced.
            fields["forced_by"] = "regime_gate"
        elif self._stretch_gate_mode() == "off":
            return fields, None, False
        return fields, allowed_side, other_side

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
