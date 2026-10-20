"""Settings for the staged live-bot fixes: the map gate, the lean entry payload and the setup ledger."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from bot.config import Settings

AWARE_SINCE = "2026-10-22T00:00:00+07:00"
MAP_COMPANIONS = {
    "ENTRY_STRETCH_GATE_MODE": "enforce",
    "STRATEGY_MODE": "ai_volume",
    "XAI_API_KEY": "test-xai",
}


def build_settings(**env) -> Settings:
    env.setdefault("STRATEGY_MODE", "volume")
    return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", **env)


def mapped(**override) -> Settings:
    return build_settings(**{**MAP_COMPANIONS, "ENTRY_REGIME_GATE": "map", **override})


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

    def test_map_accepts_either_ai_strategy(self):
        for strategy in ("grok", "ai_volume"):
            with self.subTest(strategy=strategy):
                self.assertEqual(mapped(STRATEGY_MODE=strategy).strategy_mode, strategy)

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
                    settings.setup_ledger_since, datetime(2026, 10, 21, 17, 0, tzinfo=timezone.utc)
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


if __name__ == "__main__":
    unittest.main()
