import dataclasses
import json
import unittest
from datetime import datetime, timedelta

from research.pine_gate.spec import (
    BLOCKS,
    CONTENDER_PREFERENCE,
    CSCV_BLOCKS,
    ENTRIES,
    ENTRY_PARAMS,
    HOLDOUT_END,
    HOLDOUT_START,
    LAST20_END,
    LAST20_START,
    STRENGTHS,
    TRAIN_END,
    TRAIN_START,
    TRENDS,
    STRENGTH_PARAMS,
    TREND_PARAMS,
    VOL_PARAMS,
    VOLS,
    VP_PARAMS,
    VPS,
    WIB,
    ExecConfig,
    FillConfig,
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
        self.assertEqual(CSCV_BLOCKS, 8)
        for edge in (TRAIN_START, TRAIN_END, HOLDOUT_END, *[b[1] for b in BLOCKS]):
            self.assertEqual(edge.utcoffset(), timedelta(hours=7))
            self.assertEqual((edge.hour, edge.minute), (0, 0))

    def test_training_is_369_days_and_holdout_70(self):
        self.assertEqual((TRAIN_END - TRAIN_START).days, 369)
        self.assertEqual((HOLDOUT_END - HOLDOUT_START).days, 70)

    def test_last20_window_is_half_open_and_holds_20_weekdays(self):
        self.assertEqual(LAST20_START, datetime(2026, 9, 16, tzinfo=WIB))
        self.assertEqual(LAST20_END, HOLDOUT_END)
        day, weekdays = LAST20_START, 0
        while day < LAST20_END:
            weekdays += day.weekday() < 5
            day += timedelta(days=1)
        self.assertEqual(weekdays, 20)

    def test_ms_is_exact_for_sub_second_values(self):
        dt = datetime(2025, 8, 1, 0, 0, 0, 999000, tzinfo=WIB)
        self.assertEqual(ms(dt), 1753981200999)

    def test_ms_is_utc_epoch_milliseconds(self):
        self.assertEqual(ms(datetime(2025, 8, 1, tzinfo=WIB)), 1753981200000)
        self.assertIsInstance(ms(TRAIN_START), int)


class ConfigTests(unittest.TestCase):
    def test_defaults_match_the_spec(self):
        self.assertEqual(dataclasses.asdict(ExecConfig()), dict(
            bracket_pct=0.20, entry_slip_bps=0.25, tp_slip_bps=0.25, time_stop_min=60,
            fee_pct=0.035, tick_s=180, idle_tick_s=2, order_delay_s=5, cooldown_min=5,
            notional_usdt=25_000.0, funding_blackout_s=600))
        self.assertEqual(dataclasses.asdict(GuardConfig()), dict(
            max_stops=12, max_consecutive_losses=15, max_drawdown_usdt=1500.0,
            max_entries=34, max_volume_usdt=1_000_000.0))
        self.assertEqual(dataclasses.asdict(SelectionConfig()), dict(
            min_tp_sl=1000, min_supply_per_day=10.0, z=2.0, blocks_above_half=3,
            block_min_wr=50.0, block_min_tp_sl=60, block_min_exit_days=10, top_n=5,
            neighbour_min=4, neighbour_tolerance_pts=3.0,
            pbo_max=0.5, vol_high_pct=67.0, vol_lookback_1h=720, vp_bin_bps=5.0,
            trend_band_atr=0.25))
        self.assertEqual(dataclasses.asdict(ModelConfig()), dict(
            c_grid=(0.01, 0.1, 1.0), threshold_hi=0.70, threshold_lo=0.50,
            threshold_step=0.01, fallback_threshold=0.5, threshold_min_supply_per_day=10.0))
        self.assertEqual(dataclasses.asdict(GateConfig()), dict(
            min_tp_sl=60, alpha=0.05, min_wr=57.0, half_min_wr=50.0,
            robust_overshoot_bps=2.0, robust_min_wr=55.0, offsets=20, offsets_min_pass=15,
            min_supply_per_day=10.0, bootstrap_n=10_000, permutation_shifts=1000,
            c1_seeds=200, holm_family=3))
        fill = FillConfig()
        self.assertEqual(
            (fill.overshoot_since, fill.min_samples, fill.fallback_bps, fill.round_up_bps),
            ("2026-09-12", 15, 1.0, 0.5))
        self.assertEqual(fill.overshoot_sensitivity_bps, (0.0, 1.0, 2.0))
        self.assertEqual((fill.stop_atr_multiple, fill.stop_cap_pct), (1.5, 0.4))
        self.assertEqual((fill.fee_sensitivity_pct, fill.both_touch_report_pct), (0.05, 2.0))
        self.assertEqual((fill.latency_purpose, fill.latency_since), ("entry", "2026-09-25"))

    def test_configs_are_frozen(self):
        for config in (ExecConfig(), GuardConfig(), SelectionConfig(), ModelConfig(),
                       GateConfig(), FillConfig()):
            field = dataclasses.fields(config)[0].name
            with self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(config, field, 0)


class RuleParamTests(unittest.TestCase):
    def test_every_rule_label_has_parameters(self):
        self.assertEqual(set(ENTRY_PARAMS), set(ENTRIES))
        self.assertEqual(set(STRENGTH_PARAMS), set(STRENGTHS))
        self.assertEqual(set(TREND_PARAMS), set(TRENDS))
        self.assertEqual(set(VOL_PARAMS), set(VOLS))
        self.assertEqual(set(VP_PARAMS), set(VPS))

    def test_entry_parameters(self):
        self.assertEqual(dict(ENTRY_PARAMS["P35"]),
                         {"kind": "pullback", "rsi_len": 14, "lower": 35, "upper": 65})
        self.assertEqual(dict(ENTRY_PARAMS["P40"]),
                         {"kind": "pullback", "rsi_len": 14, "lower": 40, "upper": 60})
        self.assertEqual(ENTRY_PARAMS["B12"]["bars"], 12)
        self.assertEqual(ENTRY_PARAMS["B24"]["bars"], 24)
        self.assertEqual(ENTRY_PARAMS["B24"]["kind"], "breakout")

    def test_parameter_mappings_are_immutable(self):
        for mapping in (ENTRY_PARAMS, STRENGTH_PARAMS, TREND_PARAMS, VOL_PARAMS, VP_PARAMS):
            with self.assertRaises(TypeError):
                mapping["X"] = {}
        with self.assertRaises(TypeError):
            ENTRY_PARAMS["P35"]["lower"] = 1

    def test_trend_strength_and_filter_parameters(self):
        self.assertEqual(STRENGTH_PARAMS["ADX_DI"]["adx_min"], 20)
        self.assertTrue(STRENGTH_PARAMS["ADX_DI"]["di_agrees"])
        self.assertEqual((STRENGTH_PARAMS["ADX"]["dmi_len"], STRENGTH_PARAMS["ADX"]["adx_smoothing"]), (14, 14))
        self.assertEqual(TREND_PARAMS["E200"]["band_atr"], 0.0)
        self.assertEqual(TREND_PARAMS["H1"]["timeframe"], "1h")
        self.assertEqual(TREND_PARAMS["H4"]["timeframe"], "4h")
        self.assertEqual(TREND_PARAMS["H4"]["band_atr"], 0.25)
        self.assertEqual(VOL_PARAMS["HIGH"]["comparison"], ">=")
        self.assertEqual(VOL_PARAMS["NOT_HIGH"]["comparison"], "<")
        self.assertEqual(VP_PARAMS["ON"]["window_hours"], 24)
        self.assertEqual(VP_PARAMS["ON"]["bin_bps"], SelectionConfig().vp_bin_bps)


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

    def test_document_is_json_native(self):
        doc = spec_document()
        self.assertEqual(json.loads(json.dumps(doc)), doc)

    def test_document_hashes_rule_semantics(self):
        rules = spec_document()["rules"]
        self.assertEqual(rules["signal_timeframe"], "5m")
        self.assertEqual(rules["entry"]["P40"]["upper"], 60)
        self.assertEqual(rules["entry"]["B24"]["bars"], 24)
        self.assertEqual(rules["strength"]["ADX"]["adx_min"], 20)
        self.assertEqual(rules["trend"]["E200"]["ema_len"], 200)
        self.assertEqual(rules["trend"]["H1"]["band_atr"], 0.25)
        self.assertEqual(rules["vol"]["HIGH"]["lookback_1h"], 720)
        self.assertEqual(rules["vol"]["HIGH"]["percentile"], 67.0)
        self.assertEqual(rules["vp"]["ON"]["poc_ties"], "nearest bin to price")
        self.assertEqual(rules["vp"]["ON"]["pullback"], {"long": "price <= POC", "short": "price >= POC"})
        self.assertEqual(rules["vp"]["ON"]["breakout"], {"long": "price >= POC", "short": "price <= POC"})

    def test_document_hashes_policies_and_sensitivities(self):
        doc = spec_document()
        self.assertEqual(doc["fill"]["overshoot_sensitivity_bps"], [0.0, 1.0, 2.0])
        self.assertEqual(doc["fill"]["fallback_bps"], 1.0)
        self.assertEqual(doc["fill"]["min_samples"], 15)
        self.assertEqual(doc["fill"]["latency_table"], "ai_call_metrics")
        self.assertEqual(doc["gate"]["holm_family"], 3)
        self.assertEqual(doc["model"]["threshold_min_supply_per_day"], 10.0)
        policies = doc["policies"]
        self.assertEqual(policies["contender_choice_order"], [
            "most symbols passed", "higher pooled WR lower bound", "simpler (c2, c1, c3)"])
        self.assertEqual(CONTENDER_PREFERENCE, ("c2", "c1", "c3"))
        self.assertTrue(policies["c1_c3_must_beat_c2_holdout_wr"])
        self.assertTrue(policies["eth_first_sensitivity"])
        self.assertIn("WR - 2 * SE", policies["selection_rank"])
        self.assertIn("8 CSCV blocks", policies["overfitting"])
        self.assertIn("seeded random side", policies["overfitting"])
        self.assertIn("p = 1", policies["holm"])
        self.assertIn("c0_training", policies)
        self.assertIn("code_sha256", policies["holdout_lock"])
        self.assertIn("sum(w * logloss) / sum(w)", policies["meta_model_objective"])

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
