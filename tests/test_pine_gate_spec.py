import json
import unittest
from datetime import datetime, timedelta

from research.pine_gate.spec import (
    BLOCKS,
    CSCV_BLOCKS,
    ENTRIES,
    HOLDOUT_END,
    HOLDOUT_START,
    LAST20_END,
    LAST20_START,
    STRENGTHS,
    TRAIN_END,
    TRAIN_START,
    TRENDS,
    VOLS,
    VPS,
    WIB,
    ExecConfig,
    GateConfig,
    GuardConfig,
    ModelConfig,
    RuleSpec,
    SelectionConfig,
    ms,
    spec_document,
    spec_sha256,
    stage1_neighbours,
    stage1_rules,
    stage2_variants,
)


class SegmentTests(unittest.TestCase):
    def test_boundaries_are_wib_midnights_and_blocks_tile_training(self):
        self.assertEqual(TRAIN_START, datetime(2025, 8, 1, tzinfo=WIB))
        self.assertEqual(BLOCKS[0][0], TRAIN_START)
        self.assertEqual(BLOCKS[-1][1], TRAIN_END)
        self.assertEqual(len(BLOCKS), 4)
        self.assertTrue(all(a[1] == b[0] for a, b in zip(BLOCKS, BLOCKS[1:])))
        self.assertEqual(HOLDOUT_START, TRAIN_END)
        self.assertEqual(HOLDOUT_END, datetime(2026, 10, 14, tzinfo=WIB))
        self.assertEqual(LAST20_START, datetime(2026, 9, 16, tzinfo=WIB))
        self.assertEqual(LAST20_END, datetime(2026, 10, 13, tzinfo=WIB))
        self.assertEqual(CSCV_BLOCKS, 8)
        for edge in (TRAIN_START, TRAIN_END, HOLDOUT_END, *[b[1] for b in BLOCKS]):
            self.assertEqual(edge.utcoffset(), timedelta(hours=7))
            self.assertEqual((edge.hour, edge.minute), (0, 0))

    def test_training_is_369_days_and_holdout_70(self):
        self.assertEqual((TRAIN_END - TRAIN_START).days, 369)
        self.assertEqual((HOLDOUT_END - HOLDOUT_START).days, 70)

    def test_ms_is_utc_epoch_milliseconds(self):
        self.assertEqual(ms(datetime(2025, 8, 1, tzinfo=WIB)), 1753981200000)
        self.assertIsInstance(ms(TRAIN_START), int)


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_spec(self):
        e = ExecConfig()
        self.assertEqual((e.bracket_pct, e.fee_pct, e.time_stop_min), (0.20, 0.035, 60))
        self.assertEqual((e.tick_s, e.idle_tick_s, e.order_delay_s), (180, 2, 5))
        self.assertEqual(e.notional_usdt, 25_000.0)
        g = GuardConfig()
        self.assertEqual((g.max_stops, g.max_entries, g.max_volume_usdt), (12, 34, 1_000_000.0))
        s = SelectionConfig()
        self.assertEqual((s.min_tp_sl, s.top_n, s.neighbour_min), (1000, 5, 4))
        self.assertEqual(ModelConfig().c_grid, (0.01, 0.1, 1.0))
        gate = GateConfig()
        self.assertEqual((gate.min_wr, gate.offsets, gate.bootstrap_n), (57.0, 20, 10_000))

    def test_configs_are_frozen(self):
        with self.assertRaises(Exception):
            ExecConfig().bracket_pct = 1.0


class RuleGridTests(unittest.TestCase):
    def test_stage1_has_36_unique_rules_with_filters_off(self):
        rules = stage1_rules()
        self.assertEqual(len(rules), 36)
        self.assertEqual(len(set(rules)), 36)
        self.assertTrue(all(r.vol == "OFF" and r.vp == "OFF" for r in rules))
        self.assertEqual({r.trend for r in rules}, set(TRENDS))
        self.assertEqual({r.entry for r in rules}, set(ENTRIES))
        self.assertEqual({r.strength for r in rules}, set(STRENGTHS))

    def test_key_format(self):
        self.assertEqual(RuleSpec("H1", "P35", "ADX_DI").key(), "H1|P35|ADX_DI|OFF|OFF")

    def test_rules_are_ordered(self):
        self.assertLess(RuleSpec("E200", "P35", "OFF"), RuleSpec("H1", "P35", "OFF"))

    def test_stage2_variants_are_six_and_include_base(self):
        base = RuleSpec("H4", "B12", "ADX")
        variants = stage2_variants(base)
        self.assertEqual(len(variants), 6)
        self.assertEqual(len(set(variants)), 6)
        self.assertIn(base, variants)
        self.assertEqual({(v.vol, v.vp) for v in variants},
                         {(a, b) for a in VOLS for b in VPS})
        self.assertTrue(all((v.trend, v.entry, v.strength) == ("H4", "B12", "ADX")
                            for v in variants))

    def test_every_stage1_rule_has_seven_neighbours(self):
        for rule in stage1_rules():
            neighbours = stage1_neighbours(rule)
            self.assertEqual(len(neighbours), 7, rule.key())
            self.assertEqual(len(set(neighbours)), 7)
            self.assertNotIn(rule, neighbours)
            for n in neighbours:
                differing = sum(
                    getattr(n, f) != getattr(rule, f) for f in ("trend", "entry", "strength")
                )
                self.assertEqual(differing, 1)
                self.assertEqual((n.vol, n.vp), ("OFF", "OFF"))


class SpecDocumentTests(unittest.TestCase):
    def test_document_is_json_serialisable_data_with_required_sections(self):
        doc = spec_document()
        json.dumps(doc)
        for key in ("version", "exec", "guards", "selection", "model", "gate", "grid",
                    "segments", "contender2", "contender3", "reference", "controls"):
            self.assertIn(key, doc)
        self.assertEqual(doc["gate"]["min_wr"], 57.0)
        self.assertEqual(doc["segments"]["purge_minutes"], 60)
        self.assertEqual(doc["segments"]["embargo_minutes"], 60)
        self.assertEqual(doc["segments"]["train_start"], TRAIN_START.isoformat())
        self.assertEqual(len(doc["segments"]["blocks"]), 4)
        self.assertEqual(doc["contender3"]["features"], [
            "trend_strength", "rsi_side", "adx", "di_diff_side",
            "vol_pct", "poc_dist_side", "hour_sin", "hour_cos"])
        self.assertEqual(doc["controls"]["c1_seeds"], 200)
        self.assertEqual(doc["controls"]["permutation_shifts"], 1000)

    def test_each_call_returns_a_fresh_document(self):
        first = spec_document()
        first["gate"]["min_wr"] = 1.0
        self.assertEqual(spec_document()["gate"]["min_wr"], 57.0)

    def test_spec_sha_is_deterministic_and_sensitive(self):
        doc = spec_document()
        self.assertEqual(spec_sha256(doc), spec_sha256(spec_document()))
        self.assertEqual(spec_sha256(), spec_sha256(spec_document()))
        doc["gate"]["min_wr"] = 56.0
        self.assertNotEqual(spec_sha256(doc), spec_sha256())
        self.assertEqual(len(spec_sha256()), 64)


if __name__ == "__main__":
    unittest.main()
