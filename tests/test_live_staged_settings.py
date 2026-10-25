"""Settings for the staged live-bot fixes: the map gate, the lean entry payload and the setup ledger."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from bot.ai.compact_context import lean_objective
from bot.config import Settings

# In the past: a SETUP_LEDGER_SINCE more than five minutes ahead is refused.
AWARE_SINCE = "2026-10-20T00:00:00+07:00"
# The shadow walk models the floor neutral exit, so map and the ledger need the live exit to be that one.
FLOOR_EXIT = {
    "VOLUME_NEUTRAL_EXIT_ENABLED": "true",
    "VOLUME_NEUTRAL_EXIT_MODE": "floor",
}
MAP_COMPANIONS = {
    "ENTRY_STRETCH_GATE_MODE": "enforce",
    "STRATEGY_MODE": "ai_volume",
    "XAI_API_KEY": "test-xai",
    **FLOOR_EXIT,
}


def build_settings(**env) -> Settings:
    env.setdefault("STRATEGY_MODE", "volume")
    return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", **env)


def mapped(**override) -> Settings:
    return build_settings(**{**MAP_COMPANIONS, "ENTRY_REGIME_GATE": "map", **override})


# What a lean payload needs before it may tell the model "an entry with |stretch_atr| above this
# limit is rejected in code": a stretch gate that enforces, a limit to enforce, and no behind-schedule bypass.
LEAN_COMPANIONS = {
    "ENTRY_STRETCH_GATE_MODE": "enforce",
    "ENTRY_STRETCH_GATE_MAX_ATR": "0.75",
    "ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR": "0",
}


def lean(**override) -> Settings:
    return build_settings(**{**LEAN_COMPANIONS, "AI_ENTRY_PAYLOAD": "lean", **override})


class StagedSettingsTests(unittest.TestCase):
    def test_defaults_are_legacy_and_off(self):
        settings = build_settings()

        self.assertEqual(settings.entry_regime_gate, "off")
        self.assertEqual(settings.ai_entry_payload, "legacy")
        self.assertEqual(settings.setup_ledger_mode, "off")
        self.assertIsNone(settings.setup_ledger_since)
        self.assertEqual(settings.setup_ledger_min_samples, 60)
        self.assertEqual(settings.setup_ledger_hurdle_bps, 4.0)

    def test_map_boots_with_each_direction(self):
        for direction in ("any", "short", "long"):
            with self.subTest(direction=direction):
                settings = mapped(ENTRY_GATE_DIRECTION=direction)

                self.assertEqual(settings.entry_regime_gate, "map")
                self.assertEqual(settings.entry_gate_direction, direction)
                self.assertEqual(settings.entry_stretch_gate_mode, "enforce")

    def test_map_is_normalised(self):
        self.assertEqual(mapped(ENTRY_REGIME_GATE=" MAP ").entry_regime_gate, "map")

    def test_map_refuses_without_enforce(self):
        for mode in ("off", "shadow"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError) as caught:
                    mapped(ENTRY_STRETCH_GATE_MODE=mode)
                self.assertIn("ENTRY_STRETCH_GATE_MODE", str(caught.exception))

    def test_map_refuses_the_volume_strategy(self):
        with self.assertRaises(ValueError) as caught:
            mapped(STRATEGY_MODE="volume")
        self.assertIn("STRATEGY_MODE", str(caught.exception))

    def test_map_accepts_ai_volume_and_refuses_grok(self):
        # The shadow floor is sized from the ai_volume margin, so map needs ai_volume, not just an AI strategy.
        self.assertEqual(mapped(STRATEGY_MODE="ai_volume").strategy_mode, "ai_volume")
        with self.assertRaises(ValueError) as caught:
            mapped(STRATEGY_MODE="grok")
        self.assertIn("STRATEGY_MODE=ai_volume", str(caught.exception))

    def test_naik_still_demands_long(self):
        companions = {**MAP_COMPANIONS, "ENTRY_REGIME_GATE": "naik"}
        for direction in ("any", "short"):
            with self.subTest(direction=direction):
                with self.assertRaises(ValueError) as caught:
                    build_settings(**companions, ENTRY_GATE_DIRECTION=direction)
                self.assertIn(
                    "ENTRY_REGIME_GATE is on, which makes this a LONG-only bot; it needs "
                    "ENTRY_GATE_DIRECTION=long (the direction filter only runs in an AI strategy "
                    "with the stretch gate enforcing).",
                    str(caught.exception),
                )

        self.assertEqual(
            build_settings(**companions, ENTRY_GATE_DIRECTION="long").entry_regime_gate, "naik"
        )

    def test_payload_and_ledger_values_are_normalised_and_unknown_refused(self):
        settings = mapped(AI_ENTRY_PAYLOAD=" LEAN ")
        self.assertEqual(settings.ai_entry_payload, "lean")
        self.assertEqual(
            mapped(SETUP_LEDGER_MODE=" Shadow ", SETUP_LEDGER_SINCE=AWARE_SINCE).setup_ledger_mode,
            "shadow",
        )
        self.assertEqual(
            mapped(SETUP_LEDGER_MODE="ENFORCE", SETUP_LEDGER_SINCE=AWARE_SINCE).setup_ledger_mode,
            "enforce",
        )

        for name in ("AI_ENTRY_PAYLOAD", "SETUP_LEDGER_MODE"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    mapped(**{name: "foo"})
                self.assertIn(name, str(caught.exception))

    def test_ledger_on_needs_a_regime_gate(self):
        for mode in ("shadow", "enforce"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError) as caught:
                    build_settings(SETUP_LEDGER_MODE=mode, SETUP_LEDGER_SINCE=AWARE_SINCE)
                self.assertIn("ENTRY_REGIME_GATE", str(caught.exception))

    def test_ledger_on_boots_with_either_gate_and_an_aware_since(self):
        long_companions = {**MAP_COMPANIONS, "ENTRY_GATE_DIRECTION": "long"}
        for gate, companions in (("naik", long_companions), ("map", MAP_COMPANIONS)):
            with self.subTest(gate=gate):
                settings = build_settings(
                    **companions, ENTRY_REGIME_GATE=gate,
                    SETUP_LEDGER_MODE="shadow", SETUP_LEDGER_SINCE=AWARE_SINCE,
                )

                self.assertEqual(settings.setup_ledger_mode, "shadow")
                self.assertEqual(
                    settings.setup_ledger_since, datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)
                )

    def test_ledger_on_needs_an_aware_since(self):
        for since in ("2026-10-22T00:00:00", ""):
            with self.subTest(since=since):
                with self.assertRaises(ValueError) as caught:
                    mapped(SETUP_LEDGER_MODE="enforce", SETUP_LEDGER_SINCE=since)
                self.assertIn("SETUP_LEDGER_SINCE", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            mapped(SETUP_LEDGER_MODE="shadow")
        self.assertIn("SETUP_LEDGER_SINCE", str(caught.exception))

    def test_a_since_more_than_five_minutes_ahead_is_refused(self):
        # A future start would leave both ledger reads empty: every bucket `learning`, nothing ever blocked,
        # and no log line, since the reads succeed. Same rule as VOLUME_CYCLE_STARTED_AT.
        ahead = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with self.assertRaises(ValueError) as caught:
            mapped(SETUP_LEDGER_MODE="enforce", SETUP_LEDGER_SINCE=ahead)
        self.assertIn("SETUP_LEDGER_SINCE cannot be more than 5 minutes in the future", str(caught.exception))

        now = datetime.now(timezone.utc).isoformat()
        self.assertIsNotNone(mapped(SETUP_LEDGER_MODE="enforce", SETUP_LEDGER_SINCE=now).setup_ledger_since)

    def test_a_naive_since_is_refused_even_with_the_ledger_off(self):
        with self.assertRaises(ValueError) as caught:
            mapped(SETUP_LEDGER_SINCE="2026-10-22T00:00:00")
        self.assertIn("SETUP_LEDGER_SINCE", str(caught.exception))

    def test_min_samples_below_two_refused(self):
        for value in (-1, 0, 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    mapped(SETUP_LEDGER_MIN_SAMPLES=value)

        self.assertEqual(mapped(SETUP_LEDGER_MIN_SAMPLES=2).setup_ledger_min_samples, 2)

    def test_hurdle_is_a_float_and_can_be_overridden(self):
        self.assertEqual(mapped(SETUP_LEDGER_HURDLE_BPS="5.5").setup_ledger_hurdle_bps, 5.5)

    def test_a_non_finite_hurdle_is_refused(self):
        # nan or inf would read every sampled bucket as blocked (or never), so the field refuses both.
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    mapped(SETUP_LEDGER_HURDLE_BPS=value)
                self.assertIn("SETUP_LEDGER_HURDLE_BPS", str(caught.exception))


class ShadowWalkExitSettingsTests(unittest.TestCase):
    """Shadow outcomes are walked with the floor neutral exit (`shadow_eval.walk_exit`), so a Settings
    that writes or judges them (`ENTRY_REGIME_GATE=map`, or the setup ledger on) needs the live neutral
    exit on and in `floor` mode. `naik` with the ledger off (the pinned forward bot) is not checked."""

    NEEDS_BOTH = ("VOLUME_NEUTRAL_EXIT_ENABLED=true", "VOLUME_NEUTRAL_EXIT_MODE=floor")

    def test_map_refuses_without_the_floor_neutral_exit(self):
        for override, missing in (
            ({"VOLUME_NEUTRAL_EXIT_ENABLED": "false"}, "VOLUME_NEUTRAL_EXIT_ENABLED=true"),
            ({"VOLUME_NEUTRAL_EXIT_MODE": "window"}, "VOLUME_NEUTRAL_EXIT_MODE=floor"),
            ({"VOLUME_NEUTRAL_EXIT_MODE": "trailing"}, "VOLUME_NEUTRAL_EXIT_MODE=floor"),
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError) as caught:
                    mapped(**override)
                message = str(caught.exception)
                self.assertIn("ENTRY_REGIME_GATE=map", message)
                self.assertIn(missing, message)
                for other in self.NEEDS_BOTH:
                    if other != missing:
                        self.assertNotIn(other, message)

    def test_one_error_names_both_missing_keys(self):
        # the defaults: neutral exit off, window mode
        with self.assertRaises(ValueError) as caught:
            build_settings(
                **{key: value for key, value in MAP_COMPANIONS.items() if key not in FLOOR_EXIT},
                ENTRY_REGIME_GATE="map",
            )
        message = str(caught.exception)
        for need in self.NEEDS_BOTH:
            self.assertIn(need, message)

    def test_the_ledger_on_refuses_without_the_floor_neutral_exit_even_under_naik(self):
        naik_long = {**MAP_COMPANIONS, "ENTRY_REGIME_GATE": "naik", "ENTRY_GATE_DIRECTION": "long"}
        for mode in ("shadow", "enforce"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError) as caught:
                    build_settings(
                        **{**naik_long, "VOLUME_NEUTRAL_EXIT_MODE": "window"},
                        SETUP_LEDGER_MODE=mode, SETUP_LEDGER_SINCE=AWARE_SINCE,
                    )
                message = str(caught.exception)
                self.assertIn(f"SETUP_LEDGER_MODE={mode}", message)
                self.assertIn("VOLUME_NEUTRAL_EXIT_MODE=floor", message)
                self.assertNotIn("ENTRY_REGIME_GATE=map", message)

    def test_map_and_the_ledger_refuse_the_gross_neutral_exit_basis(self):
        # The shadow floor is 100 x VOLUME_NEUTRAL_EXIT_MIN_NET_USDT / notional plus the fee: a net figure.
        naik_long = {**MAP_COMPANIONS, "ENTRY_REGIME_GATE": "naik", "ENTRY_GATE_DIRECTION": "long"}
        for name, build in (
            ("map", lambda: mapped(VOLUME_NEUTRAL_EXIT_BASIS="gross")),
            ("ledger", lambda: build_settings(
                **naik_long, VOLUME_NEUTRAL_EXIT_BASIS="gross", SETUP_LEDGER_MODE="shadow", SETUP_LEDGER_SINCE=AWARE_SINCE
            )),
        ):
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    build()
                message = str(caught.exception)
                self.assertIn("VOLUME_NEUTRAL_EXIT_BASIS=net", message)
                for other in (*self.NEEDS_BOTH, "STRATEGY_MODE=ai_volume"):
                    self.assertNotIn(other, message)

    def test_map_and_the_ledger_refuse_the_grok_strategy(self):
        # The shadow floor's notional is the ai_volume margin x leverage; grok sizes from risk instead.
        naik_long = {**MAP_COMPANIONS, "ENTRY_REGIME_GATE": "naik", "ENTRY_GATE_DIRECTION": "long"}
        for name, build in (
            ("map", lambda: mapped(STRATEGY_MODE="grok")),
            ("ledger", lambda: build_settings(
                **{**naik_long, "STRATEGY_MODE": "grok"}, SETUP_LEDGER_MODE="enforce", SETUP_LEDGER_SINCE=AWARE_SINCE
            )),
        ):
            with self.subTest(name):
                with self.assertRaises(ValueError) as caught:
                    build()
                message = str(caught.exception)
                self.assertIn("STRATEGY_MODE=ai_volume", message)
                for other in (*self.NEEDS_BOTH, "VOLUME_NEUTRAL_EXIT_BASIS=net"):
                    self.assertNotIn(other, message)

    def test_one_error_names_everything_the_shadow_walk_needs(self):
        with self.assertRaises(ValueError) as caught:
            build_settings(
                **{
                    **{key: value for key, value in MAP_COMPANIONS.items() if key not in FLOOR_EXIT},
                    "ENTRY_REGIME_GATE": "map",
                    "STRATEGY_MODE": "grok",
                    "VOLUME_NEUTRAL_EXIT_BASIS": "gross",
                }
            )
        message = str(caught.exception)
        for need in (*self.NEEDS_BOTH, "VOLUME_NEUTRAL_EXIT_BASIS=net", "STRATEGY_MODE=ai_volume"):
            self.assertIn(need, message)

    def test_map_and_the_ledger_boot_with_the_floor_neutral_exit(self):
        settings = mapped(SETUP_LEDGER_MODE="enforce", SETUP_LEDGER_SINCE=AWARE_SINCE)

        self.assertTrue(settings.volume_neutral_exit_enabled)
        self.assertEqual(settings.volume_neutral_exit_mode, "floor")
        # account 1's live values: the net basis (the default) and ai_volume
        self.assertEqual(settings.volume_neutral_exit_basis, "net")
        self.assertEqual(settings.strategy_mode, "ai_volume")

    def test_naik_with_the_ledger_off_and_the_gate_off_are_not_checked(self):
        naik_long = {
            **{key: value for key, value in MAP_COMPANIONS.items() if key not in FLOOR_EXIT},
            "ENTRY_REGIME_GATE": "naik",
            "ENTRY_GATE_DIRECTION": "long",
        }
        for name, env in (
            ("naik, ledger off", naik_long),
            ("naik, ledger off, grok", {**naik_long, "STRATEGY_MODE": "grok"}),
            ("gate off, ledger off", {}),
        ):
            with self.subTest(name):
                settings = build_settings(**env, VOLUME_NEUTRAL_EXIT_MODE="window", VOLUME_NEUTRAL_EXIT_BASIS="gross")
                self.assertEqual(settings.volume_neutral_exit_mode, "window")
                self.assertEqual(settings.volume_neutral_exit_basis, "gross")
                self.assertFalse(settings.volume_neutral_exit_enabled)



class LeanPayloadSettingsTests(unittest.TestCase):
    """`AI_ENTRY_PAYLOAD=lean` states the stretch rule and narrows the offered sides as if the code
    enforced them, so Settings refuses it unless the stretch gate really does. Legacy is never checked."""

    def test_lean_boots_with_an_enforcing_stretch_gate(self):
        for name, settings in (("gate off", lean()), ("map", mapped(AI_ENTRY_PAYLOAD="lean"))):
            with self.subTest(name):
                self.assertEqual(settings.ai_entry_payload, "lean")
                self.assertEqual(settings.entry_stretch_gate_mode, "enforce")
                self.assertEqual(settings.entry_stretch_gate_max_atr, 0.75)
                self.assertEqual(settings.entry_stretch_gate_behind_schedule_entries_per_hour, 0.0)

    def test_lean_refuses_a_stretch_gate_that_does_not_enforce(self):
        for mode in ("off", "shadow"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError) as caught:
                    lean(ENTRY_STRETCH_GATE_MODE=mode)
                message = str(caught.exception)
                self.assertIn("AI_ENTRY_PAYLOAD=lean", message)
                self.assertIn("ENTRY_STRETCH_GATE_MODE=enforce", message)
                self.assertNotIn("ENTRY_STRETCH_GATE_MAX_ATR", message)
                self.assertNotIn("ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR", message)

    def test_lean_refuses_a_zero_stretch_limit(self):
        with self.assertRaises(ValueError) as caught:
            lean(ENTRY_STRETCH_GATE_MAX_ATR="0")
        message = str(caught.exception)
        self.assertIn("ENTRY_STRETCH_GATE_MAX_ATR above 0", message)
        self.assertNotIn("ENTRY_STRETCH_GATE_MODE", message)

    def test_lean_refuses_an_infinite_stretch_limit(self):
        # inf passes the field (ge=0) but no stretch ever exceeds it, so the rule sentence would be false.
        with self.assertRaises(ValueError) as caught:
            lean(ENTRY_STRETCH_GATE_MAX_ATR="inf")
        message = str(caught.exception)
        self.assertIn("a finite ENTRY_STRETCH_GATE_MAX_ATR above 0", message)
        self.assertNotIn("ENTRY_STRETCH_GATE_MODE", message)

    def test_lean_refuses_the_behind_schedule_relaxation(self):
        for value in ("0.5", "3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    lean(ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=value)
                message = str(caught.exception)
                self.assertIn("ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0", message)
                self.assertNotIn("ENTRY_STRETCH_GATE_MODE", message)

    def test_lean_names_every_missing_requirement_at_once(self):
        with self.assertRaises(ValueError) as caught:
            lean(
                ENTRY_STRETCH_GATE_MODE="shadow",
                ENTRY_STRETCH_GATE_MAX_ATR="0",
                ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR="2",
            )
        message = str(caught.exception)
        for need in (
            "ENTRY_STRETCH_GATE_MODE=enforce",
            "ENTRY_STRETCH_GATE_MAX_ATR above 0",
            "ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0",
        ):
            self.assertIn(need, message)

    def test_lean_under_map_still_needs_the_stretch_limit_and_no_relaxation(self):
        with self.assertRaises(ValueError) as caught:
            mapped(AI_ENTRY_PAYLOAD="lean", ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR="1")
        self.assertIn("ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR=0", str(caught.exception))

    def test_legacy_is_never_checked(self):
        for payload in ({}, {"AI_ENTRY_PAYLOAD": "legacy"}):
            with self.subTest(payload=payload):
                settings = build_settings(
                    **payload,
                    ENTRY_STRETCH_GATE_MODE="off",
                    ENTRY_STRETCH_GATE_MAX_ATR="0",
                    ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR="3",
                )
                self.assertEqual(settings.ai_entry_payload, "legacy")

    def test_legacy_keeps_accepting_an_infinite_stretch_limit(self):
        # the lean guard checks finiteness; the field itself is unchanged, so a legacy .env boots as before
        settings = build_settings(ENTRY_STRETCH_GATE_MAX_ATR="inf")
        self.assertEqual(settings.ai_entry_payload, "legacy")
        self.assertEqual(settings.entry_stretch_gate_max_atr, float("inf"))


class LeanObjectiveSettingsTests(unittest.TestCase):
    """`AI_LEAN_OBJECTIVE=volume` swaps the lean prompt and goal for the volume ones. It describes a trade
    in the direction of the hourly regime, so Settings refuses it without the lean payload (which carries the
    regime block and offers one side) or without a regime gate. `edge`, the default, is never checked."""

    def test_the_default_objective_is_edge(self):
        for name, settings in (("legacy", build_settings()), ("lean", lean()), ("map", mapped())):
            with self.subTest(name):
                self.assertEqual(settings.ai_lean_objective, "edge")

    def test_the_objective_is_normalised(self):
        settings = mapped(AI_ENTRY_PAYLOAD="lean", AI_LEAN_OBJECTIVE=" VOLUME ")
        self.assertEqual(settings.ai_lean_objective, "volume")

    def test_an_unknown_objective_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            mapped(AI_ENTRY_PAYLOAD="lean", AI_LEAN_OBJECTIVE="aggressive")
        self.assertIn("AI_LEAN_OBJECTIVE must be one of: edge, volume", str(caught.exception))

    def test_volume_boots_with_the_lean_payload_and_a_regime_gate(self):
        settings = mapped(AI_ENTRY_PAYLOAD="lean", AI_LEAN_OBJECTIVE="volume")
        self.assertEqual((settings.ai_entry_payload, settings.entry_regime_gate), ("lean", "map"))
        self.assertEqual(settings.ai_lean_objective, "volume")
        # the helper the brain and the lean payload read the setting through agrees with the field
        self.assertEqual(lean_objective(settings), "volume")
        self.assertEqual(lean_objective(mapped(AI_ENTRY_PAYLOAD="lean")), "edge")
        naik = mapped(
            ENTRY_REGIME_GATE="naik", ENTRY_GATE_DIRECTION="long", AI_ENTRY_PAYLOAD="lean", AI_LEAN_OBJECTIVE="volume"
        )
        self.assertEqual((naik.entry_regime_gate, naik.ai_lean_objective), ("naik", "volume"))

    def test_volume_refuses_the_legacy_payload(self):
        with self.assertRaises(ValueError) as caught:
            mapped(AI_LEAN_OBJECTIVE="volume")
        message = str(caught.exception)
        self.assertIn("AI_LEAN_OBJECTIVE=volume", message)
        self.assertIn("AI_ENTRY_PAYLOAD=lean", message)
        self.assertNotIn("ENTRY_REGIME_GATE", message)

    def test_volume_refuses_without_a_regime_gate(self):
        with self.assertRaises(ValueError) as caught:
            lean(AI_LEAN_OBJECTIVE="volume")
        message = str(caught.exception)
        self.assertIn("AI_LEAN_OBJECTIVE=volume", message)
        self.assertIn("ENTRY_REGIME_GATE", message)
        self.assertNotIn("AI_ENTRY_PAYLOAD=lean", message)

    def test_volume_refuses_a_strategy_other_than_ai_volume(self):
        with self.assertRaises(ValueError) as caught:
            mapped(
                AI_ENTRY_PAYLOAD="lean", AI_LEAN_OBJECTIVE="volume", ENTRY_REGIME_GATE="naik",
                ENTRY_GATE_DIRECTION="long", STRATEGY_MODE="grok",
            )
        message = str(caught.exception)
        self.assertIn("STRATEGY_MODE=ai_volume", message)
        self.assertNotIn("AI_ENTRY_PAYLOAD=lean", message)

    def test_volume_refuses_a_raised_confidence_floor(self):
        # the prompt tells the model an OPEN needs 0.75; a higher AI_MIN_CONFIDENCE would drop answers it invited
        with self.assertRaises(ValueError) as caught:
            mapped(AI_ENTRY_PAYLOAD="lean", AI_LEAN_OBJECTIVE="volume", AI_MIN_CONFIDENCE="0.8")
        self.assertIn("AI_MIN_CONFIDENCE=0.75", str(caught.exception))
        # edge never told the model a number, so a raised floor stays legal there
        self.assertEqual(mapped(AI_ENTRY_PAYLOAD="lean", AI_MIN_CONFIDENCE="0.8").ai_min_confidence, 0.8)

    def test_volume_names_every_missing_requirement_at_once(self):
        with self.assertRaises(ValueError) as caught:
            build_settings(AI_LEAN_OBJECTIVE="volume", AI_MIN_CONFIDENCE="0.9")
        message = str(caught.exception)
        for need in ("AI_ENTRY_PAYLOAD=lean", "ENTRY_REGIME_GATE", "STRATEGY_MODE=ai_volume", "AI_MIN_CONFIDENCE=0.75"):
            self.assertIn(need, message)

    def test_an_empty_objective_means_edge_and_a_blank_one_is_refused(self):
        self.assertEqual(lean(AI_LEAN_OBJECTIVE="").ai_lean_objective, "edge")
        with self.assertRaises(ValueError) as caught:
            lean(AI_LEAN_OBJECTIVE="   ")
        self.assertIn("AI_LEAN_OBJECTIVE must be one of: edge, volume", str(caught.exception))

    def test_lean_objective_reads_any_settings_object(self):
        class Namespace:
            pass

        for value, expected in (("volume", "volume"), (" VOLUME ", "volume"), ("edge", "edge"), ("", "edge"),
                                ("junk", "edge"), (None, "edge")):
            with self.subTest(value=value):
                namespace = Namespace()
                namespace.ai_lean_objective = value
                self.assertEqual(lean_objective(namespace), expected)
        self.assertEqual(lean_objective(Namespace()), "edge")

    def test_edge_is_never_checked(self):
        settings = build_settings(
            AI_LEAN_OBJECTIVE="edge",
            ENTRY_STRETCH_GATE_MODE="off",
            ENTRY_STRETCH_GATE_BEHIND_SCHEDULE_ENTRIES_PER_HOUR="3",
        )
        self.assertEqual((settings.ai_entry_payload, settings.ai_lean_objective), ("legacy", "edge"))


if __name__ == "__main__":
    unittest.main()
