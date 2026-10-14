import hashlib
import json
import math
from dataclasses import asdict
import random
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from research.pine_gate import lock, pipeline
from research.pine_gate.bars import (
    day_path,
    funding_path,
    read_day,
    wib_day_start_ms,
    warmup_path,
    write_day,
)
from research.pine_gate.calibrate import Calibration
from research.pine_gate.features import FeatureFrame
from research.pine_gate.metalabel import FEATURES, MetaModel
from research.pine_gate.holdout_gate import c0_side_ok
from research.pine_gate.outcomes import bracket_outcome, gross_bps
from research.pine_gate.pipeline import (
    SYMBOLS,
    TRAIN_DATA_UNTIL_MS,
    HoldoutLockError,
    Segments,
    contender2_fn,
    load_dataset,
    load_selection_checked,
    purge_windows,
    regime_counts,
    rule_signal_fn,
    train_stage,
)
from research.pine_gate.rules import contender2_signal, rule_signal
from research.pine_gate.selection import Selection, summarize
from research.pine_gate.simulate import in_entry_window
from research.pine_gate.stats import mean_gross_bps, win_rate
from research.pine_gate.spec import (
    BLOCKS,
    TRAIN_END,
    TRAIN_START,
    WIB,
    ExecConfig,
    GuardConfig,
    ModelConfig,
    RuleSpec,
    SelectionConfig,
    ms,
    spec_sha256,
    stage1_rules,
)

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
H4 = 4 * HOUR

# Synthetic data: 1m bars on WIB days 2026-07-20 .. 2026-08-07, across the
# spec's training boundary (2026-08-05 00:00 WIB, a Wednesday).
FIRST_DAY = date(2026, 7, 20)
LAST_DAY = date(2026, 8, 7)
BASE_PRICE = {"BTC-USDT": 60_000.0, "ETH-USDT": 3_000.0}


def wib(year, month, day, hour=0):
    return ms(datetime(year, month, day, hour, tzinfo=WIB))


# Training for the tests: Mon 2026-07-27 -> Wed 2026-08-05 (the spec end), four
# blocks of about two trading days each, eight equal CSCV ranges.
TEST_TRAIN_START = wib(2026, 7, 27)
TEST_BLOCK_EDGES = (
    TEST_TRAIN_START, wib(2026, 7, 29), wib(2026, 7, 31), wib(2026, 8, 4), ms(TRAIN_END),
)
TEST_SEGMENTS = Segments(
    train_start_ms=TEST_TRAIN_START,
    train_end_ms=ms(TRAIN_END),
    block_bounds_ms=tuple(zip(TEST_BLOCK_EDGES, TEST_BLOCK_EDGES[1:])),
    cscv_bounds_ms=pipeline.equal_ranges(TEST_TRAIN_START, ms(TRAIN_END), 8),
)
LENIENT_SEL = replace(
    SelectionConfig(),
    min_tp_sl=1,
    min_supply_per_day=0.0,
    blocks_above_half=0,
    block_min_tp_sl=0,
    block_min_exit_days=0,
    neighbour_min=0,
)
# Daily guards off, so an always-long clock reaches the late-evening purge windows.
NO_GUARDS = GuardConfig(
    max_stops=10_000, max_consecutive_losses=10_000, max_drawdown_usdt=1e12,
    max_entries=10_000, max_volume_usdt=1e12,
)
# Non-spec runs (test segments, lenient selection, fake trainer) must not carry the
# real spec sha256; train_stage refuses that combination.
DUMMY_SHA = "0" * 64


def spec_run_record():
    """configs / segments / meta_trainer as a spec run records them (JSON-normalised)."""
    record = {
        "configs": {
            "exec": asdict(ExecConfig()),
            "guard": asdict(GuardConfig()),
            "selection": asdict(SelectionConfig()),
            "model": asdict(ModelConfig()),
        },
        "segments": Segments.spec().to_dict(),
        "meta_trainer": "train_meta",
    }
    return json.loads(json.dumps(record))


CALIB = Calibration(s_star_bps=1.0, n_stops=20, latencies_ms=(20_000.0, 30_000.0, 40_000.0))


def _walk_rows(rng, start_ms, n, interval_ms, price, step):
    rows = []
    for k in range(n):
        o = price
        c = o * math.exp(rng.gauss(0.0, step))
        hi = max(o, c) * (1 + abs(rng.gauss(0.0, step / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0.0, step / 2)))
        rows.append((start_ms + k * interval_ms, o, hi, lo, c, rng.uniform(1.0, 10.0)))
        price = c
    return rows


def write_data_dir(root, last_day=LAST_DAY, with_warmup=True, with_funding=True):
    first_ms = wib_day_start_ms(FIRST_DAY)
    for i, symbol in enumerate(SYMBOLS):
        rng = random.Random(100 + i)
        price = BASE_PRICE[symbol]
        if with_warmup:
            rows_1h = _walk_rows(rng, first_ms - 500 * HOUR, 500, HOUR, price * 0.97, 0.003)
            write_day(warmup_path(root, symbol, "1h"), rows_1h)
            last_4h = first_ms // H4 * H4
            rows_4h = _walk_rows(rng, last_4h - 499 * H4, 500, H4, price * 0.95, 0.006)
            write_day(warmup_path(root, symbol, "4h"), rows_4h)
        day = FIRST_DAY
        while day <= last_day:
            rows = _walk_rows(rng, wib_day_start_ms(day), 1440, MIN, price, 0.0004)
            write_day(day_path(root, "prod", symbol, "1m", day), rows)
            price = rows[-1][4]
            day += timedelta(days=1)
        if with_funding:
            end_ms = wib_day_start_ms(last_day + timedelta(days=1))
            times = list(range(first_ms // (8 * HOUR) * (8 * HOUR), end_ms + 2 * DAY, 8 * HOUR))
            path = funding_path(root, symbol)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(times))


class FakeMetaTrainer:
    """Stands in for train_meta, whose folds are fixed to spec.BLOCKS dates."""

    def __init__(self):
        self.candidates = None
        self.supply = {}

    def __call__(self, candidates, supply_for, cfg):
        self.candidates = list(candidates)
        everyone = {(c.symbol, c.t_ms): 1.0 for c in self.candidates}
        self.supply["all"] = supply_for(everyone, 0.5)
        self.supply["none"] = supply_for({}, 0.5)
        at = {k: 0.5 for k in everyone}
        below = {k: math.nextafter(0.5, 0.0) for k in everyone}
        self.supply["at"] = supply_for(at, 0.5)
        self.supply["below"] = supply_for(below, 0.5)
        model = MetaModel(
            coef=(0.0,) * len(FEATURES), intercept=0.0, means=(0.0,) * len(FEATURES),
            stds=(1.0,) * len(FEATURES), threshold=0.6, C=0.1,
        )
        diagnostics = {
            "cv_logloss": {0.1: [0.69, 0.69, 0.69]},
            "chosen_C": 0.1,
            "oof": [["BTC-USDT", 0, 0.5, 1]],
            "reliability": [(0.5, 0.5, 1)],
            "supply_oof_at_threshold": 11.5,
            "supply_final_at_threshold": 12.25,
        }
        return model, diagnostics


class _Shared:
    """One synthetic data dir and one train_stage run for the whole module."""

    built = False


def _build_shared():
    if _Shared.built:
        return
    _Shared.tmp = Path(tempfile.mkdtemp(prefix="pine_pipeline_"))
    try:
        _fill_shared()
    except BaseException:
        shutil.rmtree(_Shared.tmp, ignore_errors=True)  # no temp dir left behind
        raise
    _Shared.built = True


def _fill_shared():
    _Shared.data_dir = _Shared.tmp / "data"
    write_data_dir(_Shared.data_dir)
    _Shared.dataset = load_dataset(
        _Shared.data_dir, until_ms=TRAIN_DATA_UNTIL_MS, start_day=FIRST_DAY
    )
    _Shared.trainer = FakeMetaTrainer()
    _Shared.sha = DUMMY_SHA
    _Shared.out_dir = _Shared.tmp / "out"
    _Shared.selection_path = train_stage(
        _Shared.dataset, CALIB, _Shared.out_dir, _Shared.sha, seed=3,
        segments=TEST_SEGMENTS, sel_cfg=LENIENT_SEL, spa_n=200,
        meta_trainer=_Shared.trainer,
    )
    _Shared.selection = json.loads(_Shared.selection_path.read_text())


def tearDownModule():
    if _Shared.built:
        shutil.rmtree(_Shared.tmp, ignore_errors=True)


class PipelineFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _build_shared()
        for name in ("tmp", "data_dir", "dataset", "trainer", "sha", "out_dir",
                     "selection_path", "selection"):
            setattr(cls, name, getattr(_Shared, name))


class LoadDatasetTests(PipelineFixture):
    def test_train_stage_never_reads_bars_after_boundary_plus_hour(self):
        ds = load_dataset(self.data_dir, until_ms=TRAIN_DATA_UNTIL_MS, start_day=FIRST_DAY)
        for m in ds.markets.values():
            self.assertLessEqual(m.bars_1m.open_ms[-1] + 60_000, TRAIN_DATA_UNTIL_MS)
        for f in ds.frames.values():
            self.assertLessEqual(f.bars_1m.open_ms[-1] + 60_000, TRAIN_DATA_UNTIL_MS)
        self.assertEqual(ds.loaded_until_ms, TRAIN_DATA_UNTIL_MS)

    def test_boundary_is_spec_train_end_plus_one_hour(self):
        self.assertEqual(TRAIN_DATA_UNTIL_MS, ms(TRAIN_END) + HOUR)

    def test_bars_reach_the_boundary_and_funding_is_cut(self):
        for symbol in SYMBOLS:
            market = self.dataset.markets[symbol]
            self.assertEqual(market.bars_1m.open_ms[-1] + MIN, TRAIN_DATA_UNTIL_MS)
            self.assertEqual(market.bars_1m.open_ms[0], wib_day_start_ms(FIRST_DAY))
            self.assertTrue(market.funding_ms)
            self.assertLess(max(market.funding_ms), TRAIN_DATA_UNTIL_MS)
            self.assertEqual(list(market.funding_ms), sorted(market.funding_ms))
        self.assertEqual(tuple(self.dataset.markets), SYMBOLS)

    def test_data_sha_is_stable_and_changes_with_a_file(self):
        again = load_dataset(self.data_dir, until_ms=TRAIN_DATA_UNTIL_MS, start_day=FIRST_DAY)
        self.assertEqual(again.data_sha256, self.dataset.data_sha256)
        self.assertRegex(again.data_sha256, r"^[0-9a-f]{64}$")
        copy = self.tmp / "data_copy"
        shutil.copytree(self.data_dir, copy)
        day = date(2026, 7, 30)
        path = day_path(copy, "prod", "ETH-USDT", "1m", day)
        rows = _walk_rows(random.Random(9), wib_day_start_ms(day), 1440, MIN, 3_000.0, 0.0004)
        write_day(path, rows)
        changed = load_dataset(copy, until_ms=TRAIN_DATA_UNTIL_MS, start_day=FIRST_DAY)
        self.assertNotEqual(changed.data_sha256, self.dataset.data_sha256)

    def test_data_sha_ignores_files_past_the_until_day(self):
        copy = self.tmp / "data_late"
        shutil.copytree(self.data_dir, copy)
        late = day_path(copy, "prod", "BTC-USDT", "1m", date(2026, 8, 7))
        late.unlink()
        ds = load_dataset(copy, until_ms=TRAIN_DATA_UNTIL_MS, start_day=FIRST_DAY)
        self.assertEqual(ds.data_sha256, self.dataset.data_sha256)

    def test_train_data_sha_covers_only_the_sliced_arrays(self):
        self.assertRegex(self.dataset.train_data_sha256, r"^[0-9a-f]{64}$")
        self.assertNotEqual(self.dataset.train_data_sha256, self.dataset.data_sha256)
        # A bar after the cut (2026-08-05 02:00 WIB) changes the file hash only.
        copy = self.tmp / "data_after_cut"
        shutil.copytree(self.data_dir, copy)
        day = date(2026, 8, 5)
        path = day_path(copy, "prod", "BTC-USDT", "1m", day)
        rows = read_day(path)
        k = 120
        t, o, h, l, c, v = rows[k]
        rows[k] = (t, o, h * 1.001, l, c, v)
        write_day(path, rows)
        after = load_dataset(copy, until_ms=TRAIN_DATA_UNTIL_MS, start_day=FIRST_DAY)
        self.assertNotEqual(after.data_sha256, self.dataset.data_sha256)
        self.assertEqual(after.train_data_sha256, self.dataset.train_data_sha256)
        # A bar before the cut (00:30 WIB) changes both.
        k = 30
        t, o, h, l, c, v = rows[k]
        rows[k] = (t, o, h * 1.001, l, c, v)
        write_day(path, rows)
        before = load_dataset(copy, until_ms=TRAIN_DATA_UNTIL_MS, start_day=FIRST_DAY)
        self.assertNotEqual(before.train_data_sha256, self.dataset.train_data_sha256)

    def test_missing_funding_file_raises(self):
        root = self.tmp / "no_funding"
        write_data_dir(root, last_day=date(2026, 7, 22), with_funding=False)
        with self.assertRaises(FileNotFoundError):
            load_dataset(root, until_ms=wib(2026, 7, 22, 12), start_day=FIRST_DAY)

    def test_missing_warmup_is_recorded_not_fatal(self):
        root = self.tmp / "no_warmup"
        write_data_dir(root, last_day=date(2026, 7, 22), with_warmup=False)
        ds = load_dataset(root, until_ms=wib(2026, 7, 22, 12), start_day=FIRST_DAY)
        self.assertEqual(len(ds.missing_warmups), 4)
        self.assertEqual(self.dataset.missing_warmups, ())

    def test_markets_use_the_given_exec_config(self):
        cfg = replace(ExecConfig(), bracket_pct=0.30)
        ds = load_dataset(
            self.data_dir, until_ms=wib(2026, 7, 22, 12), start_day=FIRST_DAY, exec_cfg=cfg
        )
        market = ds.markets["BTC-USDT"]
        indexes = range(100, 1100, 10)
        for i in indexes:
            self.assertEqual(
                market.outcomes.get(i, 1), bracket_outcome(market.bars_1m, i, 1, cfg)
            )
        default = [bracket_outcome(market.bars_1m, i, 1, ExecConfig()) for i in indexes]
        self.assertNotEqual([market.outcomes.get(i, 1) for i in indexes], default)


class SignalTests(PipelineFixture):
    def test_rule_signal_fn_matches_rule_signal_on_snapshots(self):
        rule = RuleSpec("H1", "B12", "OFF", "OFF", "ON")
        fn = rule_signal_fn(rule, self.dataset.frames)
        frame = self.dataset.frames["BTC-USDT"]
        fired = 0
        for t in range(TEST_TRAIN_START, TEST_TRAIN_START + DAY, 182_000):
            expected = rule_signal(rule, frame.snapshot(t), lambda: frame.poc_24h(t))
            self.assertEqual(fn("BTC-USDT", t), expected)
            fired += expected != 0
        self.assertGreater(fired, 0)

    def test_contender2_fn_matches_contender2_signal(self):
        fn = contender2_fn(self.dataset.frames)
        frame = self.dataset.frames["ETH-USDT"]
        for t in range(TEST_TRAIN_START, TEST_TRAIN_START + 6 * HOUR, 182_000):
            self.assertEqual(fn("ETH-USDT", t), contender2_signal(frame.snapshot(t)))


class SegmentTests(unittest.TestCase):
    def test_spec_segments(self):
        seg = Segments.spec()
        self.assertEqual(seg.train_start_ms, ms(TRAIN_START))
        self.assertEqual(seg.train_end_ms, ms(TRAIN_END))
        self.assertEqual(seg.block_bounds_ms, tuple((ms(a), ms(b)) for a, b in BLOCKS))
        self.assertEqual(len(seg.cscv_bounds_ms), 8)
        self.assertEqual(seg.cscv_bounds_ms[0][0], ms(TRAIN_START))
        self.assertEqual(seg.cscv_bounds_ms[-1][1], ms(TRAIN_END))
        for (_, end), (start, _) in zip(seg.cscv_bounds_ms, seg.cscv_bounds_ms[1:]):
            self.assertEqual(end, start)

    def test_purge_windows_are_65_minutes_before_every_block_end(self):
        windows = purge_windows(Segments.spec())
        self.assertEqual(
            windows, tuple((ms(b) - 65 * MIN, ms(b)) for _, b in BLOCKS)
        )

    def test_segments_must_tile_training(self):
        seg = Segments.spec()
        with self.assertRaises(ValueError):
            replace(seg, block_bounds_ms=seg.block_bounds_ms[:3])
        gap = list(seg.block_bounds_ms)
        gap[1] = (gap[1][0] + MIN, gap[1][1])
        with self.assertRaises(ValueError):
            replace(seg, block_bounds_ms=tuple(gap))
        three = pipeline.equal_ranges(seg.train_start_ms, seg.train_end_ms, 3)
        with self.assertRaises(ValueError):
            replace(seg, cscv_bounds_ms=three)

    def test_equal_ranges_tile_and_last_absorbs_rounding(self):
        ranges = pipeline.equal_ranges(0, 103, 4)
        self.assertEqual(ranges, ((0, 25), (25, 50), (50, 75), (75, 103)))


class TrainStageTests(PipelineFixture):
    def test_selection_file_records_spec_sha_and_choice(self):
        sel = self.selection
        self.assertEqual(sel["spec_sha256"], self.sha)
        self.assertEqual(sel["data_sha256"], self.dataset.data_sha256)
        self.assertEqual(sel["train_data_sha256"], self.dataset.train_data_sha256)
        self.assertEqual(sel["meta_trainer"], "<injected>")
        self.assertIsNotNone(sel["contender1"])
        self.assertIn(sel["contender1"], sel["rules"])
        trend, entry = sel["contender1"].split("|")[:2]
        self.assertEqual(sel["primary_rule"], f"{trend}|{entry}|OFF|OFF|OFF")
        self.assertTrue(sel["stable"])
        self.assertLessEqual(len(sel["top"]), LENIENT_SEL.top_n)
        for key in sel["top"]:
            self.assertIn(key, sel["rules"])
        self.assertEqual(sel["pbo_pass"], sel["pbo"] < LENIENT_SEL.pbo_max)
        self.assertGreaterEqual(sel["spa_p"], 0.0)
        self.assertLessEqual(sel["spa_p"], 1.0)
        self.assertEqual(sel["s_star_bps"], CALIB.s_star_bps)
        self.assertEqual(sel["n_stops"], CALIB.n_stops)
        self.assertEqual(sel["n_latencies"], len(CALIB.latencies_ms))
        self.assertEqual(sel["meta_model"]["threshold"], 0.6)
        self.assertEqual(sel["cv_logloss"], {"0.1": [0.69, 0.69, 0.69]})
        self.assertEqual(sel["chosen_C"], 0.1)
        self.assertEqual(sel["reliability"], [[0.5, 0.5, 1]])
        self.assertEqual(sel["supply_oof_at_threshold"], 11.5)
        self.assertEqual(sel["supply_final_at_threshold"], 12.25)
        self.assertEqual(sel["configs"]["selection"]["min_tp_sl"], 1)
        self.assertEqual(sel["segments"]["train_end_ms"], ms(TRAIN_END))
        self.assertIn("created_at", sel)

    def test_file_name_carries_the_wib_creation_date(self):
        created = datetime.fromisoformat(self.selection["created_at"])
        stamp = created.astimezone(WIB).strftime("%Y%m%d")
        self.assertEqual(self.selection_path.name, f"pine_gate_selection_{stamp}.json")
        self.assertEqual(self.selection_path.parent, self.out_dir)
        self.assertLess(self.selection_path.stat().st_size, 5_000_000)
        self.assertEqual(list(self.out_dir.glob("*.tmp")), [])

    def test_every_evaluated_rule_has_training_metrics(self):
        rules = self.selection["rules"]
        self.assertGreaterEqual(len(rules), 36)
        for key, m in rules.items():
            for name in ("n_trades", "n_tp_sl", "supply_per_day", "wr", "lower_bound",
                         "block_wr", "block_lb", "block_counts", "cscv_counts", "rank_value",
                         "mean_gross_bps", "eligible"):
                self.assertIn(name, m, key)
            self.assertEqual(len(m["block_counts"]), 4)
            self.assertEqual(len(m["cscv_counts"]), 8)
        self.assertTrue(any(m["n_trades"] > 0 for m in rules.values()))

    def test_contender3_candidates_are_inside_training_and_outside_purge(self):
        candidates = self.trainer.candidates
        self.assertTrue(candidates)
        windows = purge_windows(TEST_SEGMENTS)
        keys = set()
        for c in candidates:
            self.assertGreaterEqual(c.t_ms, TEST_SEGMENTS.train_start_ms)
            self.assertLess(c.t_ms, TEST_SEGMENTS.train_end_ms)
            self.assertEqual(c.t_ms % (5 * MIN), 0)
            self.assertTrue(in_entry_window(c.t_ms))
            self.assertFalse(any(a <= c.t_ms < b for a, b in windows))
            start, end = TEST_SEGMENTS.block_bounds_ms[c.block]
            self.assertTrue(start <= c.t_ms < end)
            self.assertLessEqual(c.end_ms, end)
            self.assertGreater(c.start_ms, c.t_ms)
            keys.add((c.symbol, c.t_ms))
        self.assertEqual(len(keys), len(candidates))
        self.assertEqual(self.selection["n_candidates"], len(candidates))

    def test_contender3_candidates_follow_the_primary_rule(self):
        trend, entry = self.selection["primary_rule"].split("|")[:2]
        primary = RuleSpec(trend, entry, "OFF")
        for c in self.trainer.candidates[:50]:
            frame = self.dataset.frames[c.symbol]
            side = rule_signal(primary, frame.snapshot(c.t_ms), lambda: frame.poc_24h(c.t_ms))
            self.assertEqual(c.side, side)

    def test_supply_for_admits_only_candidates_at_or_above_threshold(self):
        self.assertGreater(self.trainer.supply["all"], 0.0)
        self.assertEqual(self.trainer.supply["none"], 0.0)
        self.assertEqual(self.trainer.supply["at"], self.trainer.supply["all"])
        self.assertEqual(self.trainer.supply["below"], 0.0)

    def test_regime_counts_cover_each_symbol_and_trend(self):
        regimes = self.selection["regime_counts"]
        self.assertEqual(sorted(regimes), sorted(SYMBOLS))
        for symbol in SYMBOLS:
            self.assertEqual(sorted(regimes[symbol]), ["E200", "H1", "H4"])

    def test_train_stage_refuses_data_loaded_past_the_boundary(self):
        late = load_dataset(
            self.data_dir, until_ms=TRAIN_DATA_UNTIL_MS + DAY, start_day=FIRST_DAY
        )
        with self.assertRaises(ValueError):
            train_stage(late, CALIB, self.tmp / "late", self.sha,
                        segments=TEST_SEGMENTS, meta_trainer=FakeMetaTrainer())
        self.assertFalse((self.tmp / "late").exists())

    def test_train_stage_refuses_a_dataset_with_bars_past_the_boundary(self):
        forged = replace(self.dataset, loaded_until_ms=TRAIN_DATA_UNTIL_MS)
        late = load_dataset(
            self.data_dir, until_ms=TRAIN_DATA_UNTIL_MS + DAY, start_day=FIRST_DAY
        )
        forged = replace(forged, markets=late.markets)
        with self.assertRaises(ValueError):
            train_stage(forged, CALIB, self.tmp / "forged", self.sha,
                        segments=TEST_SEGMENTS, meta_trainer=FakeMetaTrainer())

    def test_injected_segments_need_an_injected_meta_trainer(self):
        with self.assertRaises(ValueError):
            train_stage(self.dataset, CALIB, self.tmp / "real", self.sha,
                        segments=TEST_SEGMENTS)

    def test_train_stage_refuses_latencies_the_purge_cannot_cover(self):
        slow = replace(CALIB, latencies_ms=(30_000.0, 5 * 60_000.0))
        with self.assertRaises(ValueError):
            train_stage(self.dataset, slow, self.tmp / "slow", self.sha,
                        segments=TEST_SEGMENTS, meta_trainer=FakeMetaTrainer())
        live_worst = replace(CALIB, latencies_ms=(64_000.0,))
        pipeline.check_purge_covers(live_worst, ExecConfig())  # live max: fits

    def test_shared_fixture_file_is_refused_under_the_real_spec_sha(self):
        real = spec_sha256()
        with self.assertRaises(HoldoutLockError):
            load_selection_checked(self.selection_path, real)
        # Even relabelled with the real sha, its injected settings are refused.
        doc = dict(self.selection, spec_sha256=real)
        path = self.tmp / "relabelled.json"
        path.write_text(json.dumps(doc))
        with self.assertRaises(HoldoutLockError):
            load_selection_checked(path, real)

    def test_train_stage_refuses_the_real_sha_with_injected_settings(self):
        real = spec_sha256()
        cases = {
            "segments": dict(segments=TEST_SEGMENTS, meta_trainer=FakeMetaTrainer()),
            "selection": dict(sel_cfg=LENIENT_SEL),
            "exec": dict(exec_cfg=replace(ExecConfig(), fee_pct=0.05)),
            "guard": dict(guard_cfg=NO_GUARDS),
            "model": dict(model_cfg=replace(ModelConfig(), threshold_hi=0.8)),
            "meta_trainer": dict(meta_trainer=FakeMetaTrainer()),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as ctx:
                    train_stage(self.dataset, CALIB, self.tmp / f"real_{name}", real, **kwargs)
                self.assertIn(name, str(ctx.exception))
                self.assertFalse((self.tmp / f"real_{name}").exists())

    def test_train_stage_refuses_to_overwrite_a_selection_file(self):
        out = self.tmp / "existing"
        out.mkdir()
        stamp = datetime.now(WIB).strftime("%Y%m%d")
        existing = out / f"pine_gate_selection_{stamp}.json"
        existing.write_text("{}")
        with self.assertRaises(FileExistsError) as ctx:
            train_stage(self.dataset, CALIB, out, self.sha, segments=TEST_SEGMENTS,
                        sel_cfg=LENIENT_SEL, meta_trainer=FakeMetaTrainer())
        self.assertIn(str(existing), str(ctx.exception))
        self.assertEqual(existing.read_text(), "{}")

    def test_selection_records_the_latency_sample_hash(self):
        payload = json.dumps([float(x) for x in CALIB.latencies_ms], allow_nan=False)
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        self.assertEqual(self.selection["latencies_sha256"], expected)
        self.assertEqual(pipeline.latencies_sha256(CALIB.latencies_ms), expected)
        self.assertNotEqual(pipeline.latencies_sha256((1.0, 2.0, 3.0)), expected)

    def test_train_stage_refuses_a_frame_with_hourly_bars_past_the_boundary(self):
        late = load_dataset(
            self.data_dir, until_ms=TRAIN_DATA_UNTIL_MS + DAY, start_day=FIRST_DAY
        )
        frames = dict(self.dataset.frames)
        good, bad = frames["ETH-USDT"], late.frames["ETH-USDT"]
        # 1m bars end at the limit; only the derived 5m/1h/4h/3m series run past it.
        frames["ETH-USDT"] = FeatureFrame("ETH-USDT", good.bars_1m, bad._bars_5m, bad._tables)
        forged = replace(self.dataset, frames=frames)
        with self.assertRaises(ValueError) as ctx:
            train_stage(forged, CALIB, self.tmp / "forged_htf", self.sha,
                        segments=TEST_SEGMENTS, meta_trainer=FakeMetaTrainer())
        self.assertIn("ETH-USDT", str(ctx.exception))

    def test_train_stage_rejects_a_malformed_spec_sha(self):
        with self.assertRaises(ValueError):
            train_stage(self.dataset, CALIB, self.tmp / "badsha", "abc",
                        segments=TEST_SEGMENTS, meta_trainer=FakeMetaTrainer())


class IntegrityRecordTests(PipelineFixture):
    """What the selection file records for the holdout to check (A1, A4, A5, A7)."""

    def test_selection_records_the_code_sha256(self):
        self.assertEqual(self.selection["code_sha256"], lock.code_sha256())
        self.assertRegex(self.selection["code_sha256"], r"^[0-9a-f]{64}$")

    def test_recorded_code_sha_is_the_one_taken_before_the_run(self):
        first, later = "a" * 64, "b" * 64
        current = [first]
        real_context = pipeline._context

        def run_then_change_the_code(*args, **kwargs):
            current[0] = later  # the code changes once the long run has started
            return real_context(*args, **kwargs)

        with mock.patch.object(pipeline, "code_sha256", side_effect=lambda: current[0]), \
                mock.patch.object(pipeline, "_context", run_then_change_the_code):
            path = train_stage(self.dataset, CALIB, self.tmp / "fingerprint_once", self.sha,
                               segments=TEST_SEGMENTS, sel_cfg=LENIENT_SEL, spa_n=200,
                               meta_trainer=FakeMetaTrainer())
        self.assertEqual(json.loads(path.read_text())["code_sha256"], first)

    def test_training_c0_is_recorded_per_symbol_and_side(self):
        c0 = self.selection["c0_training"]
        self.assertEqual(sorted(c0), sorted(SYMBOLS))
        markets = {s: self.dataset.markets[s] for s in SYMBOLS}
        verdict = True
        for symbol in SYMBOLS:
            for name, side in (("long", 1), ("short", -1)):
                trades = pipeline.simulate_training(
                    lambda s, t, side=side: side, (markets[symbol],), TEST_SEGMENTS, CALIB,
                    seed=3, exec_cfg=ExecConfig(), guard_cfg=GuardConfig(),
                )
                cell = c0[symbol][name]
                self.assertGreater(cell["n"], 0)
                self.assertEqual(cell, {"n": len(trades), "wr": win_rate(trades),
                                        "gross_bps": mean_gross_bps(trades)})
                verdict = verdict and c0_side_ok(cell["wr"], cell["gross_bps"])
        self.assertIs(self.selection["c0_training_pass"], verdict)

    def test_spa_series_is_the_rule_side_minus_a_random_side_on_the_same_entries(self):
        rule = RuleSpec("H1", "B12", "OFF")
        markets = tuple(pipeline.fresh_markets(self.dataset, ExecConfig()))
        trades = pipeline.simulate_training(
            rule_signal_fn(rule, self.dataset.frames), markets, TEST_SEGMENTS, CALIB, seed=3,
            exec_cfg=ExecConfig(), guard_cfg=GuardConfig(),
        )
        self.assertTrue(trades)
        days = pipeline.trading_days(TEST_SEGMENTS.train_start_ms, TEST_SEGMENTS.train_end_ms)
        rng = random.Random(3)
        by_day = {}
        for t in trades:
            market = next(m for m in markets if m.symbol == t.symbol)
            idx = list(market.bars_1m.open_ms).index(t.entry_ms - MIN)
            side = rng.choice((1, -1))
            o = market.outcomes.get(idx, side)
            other = gross_bps(o, side, CALIB.s_star_bps, ExecConfig())
            by_day.setdefault(t.wib_day, []).append(t.gross_bps - other)
        expected = [sum(by_day[d]) / len(by_day[d]) if d in by_day else 0.0 for d in days]
        series = pipeline.direction_series(trades, markets, days, seed=3,
                                           overshoot_bps=CALIB.s_star_bps, exec_cfg=ExecConfig())
        self.assertEqual(len(series), len(days))
        for got, want in zip(series, expected):
            self.assertAlmostEqual(got, want, places=9)
        self.assertTrue(any(v != 0.0 for v in series))
        self.assertEqual(pipeline.direction_series([], markets, days, seed=3,
                                                   overshoot_bps=1.0, exec_cfg=ExecConfig()),
                         [0.0] * len(days))

    def test_training_part_hash_of_a_longer_load_equals_the_training_hash(self):
        self.assertEqual(self.dataset.train_part_sha256, self.dataset.train_data_sha256)
        longer = load_dataset(self.data_dir, until_ms=wib(2026, 8, 7, 12), start_day=FIRST_DAY)
        self.assertNotEqual(longer.train_data_sha256, self.dataset.train_data_sha256)
        self.assertEqual(longer.train_part_sha256, self.dataset.train_data_sha256)


class TrainingSimulationTests(PipelineFixture):
    def test_no_training_trade_enters_in_a_purge_window_or_crosses_a_block_end(self):
        always_long = lambda symbol, t: 1  # noqa: E731
        markets = [self.dataset.markets[s] for s in SYMBOLS]
        trades = pipeline.simulate_training(
            always_long, markets, TEST_SEGMENTS, CALIB, seed=1,
            exec_cfg=ExecConfig(), guard_cfg=NO_GUARDS,
        )
        self.assertTrue(trades)
        # Without the purge the clock would enter inside these windows.
        late = [t for t in trades if any(b - 2 * HOUR <= t.signal_ms < b
                                         for _, b in TEST_SEGMENTS.block_bounds_ms)]
        self.assertTrue(late)
        windows = purge_windows(TEST_SEGMENTS)
        for t in trades:
            self.assertFalse(any(a <= t.signal_ms < b for a, b in windows))
            for start, end in TEST_SEGMENTS.block_bounds_ms:
                if start <= t.entry_ms < end:
                    self.assertLessEqual(t.exit_ms, end)
                    self.assertLessEqual(t.entry_ms, end - 60 * MIN)


def _empty_results(sel_cfg):
    seg = TEST_SEGMENTS
    block_days = [pipeline.trading_days(a, b) for a, b in seg.block_bounds_ms]
    days = pipeline.trading_days(seg.train_start_ms, seg.train_end_ms)
    return {
        r.key(): summarize(r, [], seg.block_bounds_ms, seg.cscv_bounds_ms, block_days, days,
                           z=sel_cfg.z)
        for r in stage1_rules()
    }


# Blocks above SelectionConfig's thin-block floor (60 TP+SL on 10 exit days).
THICK = dict(block_counts=((60, 40),) * 4, block_exit_days=(20,) * 4)


def _selection(choice, results):
    return Selection(choice=choice, top=(), stable=choice is not None, pbo=0.0,
                     supply_limited=False, results=results)


class PboRecordTests(unittest.TestCase):
    def test_pbo_passes_below_the_maximum(self):
        cfg = SelectionConfig()
        self.assertEqual(pipeline.pbo_record(0.2, cfg),
                         {"pbo": 0.2, "pbo_pass": True, "pbo_reason": None})
        self.assertFalse(pipeline.pbo_record(0.5, cfg)["pbo_pass"])

    def test_undefined_pbo_fails_with_its_reason(self):
        record = pipeline.pbo_record(None, SelectionConfig())
        self.assertIsNone(record["pbo"])
        self.assertIs(record["pbo_pass"], False)
        self.assertIn("fewer than two rules with at least 1000 TP+SL", record["pbo_reason"])


class PrimaryRuleTests(unittest.TestCase):
    def test_primary_is_contender1_trend_and_entry_with_strength_off(self):
        choice = RuleSpec("H4", "B24", "ADX_DI", "HIGH", "ON")
        primary = pipeline._primary_rule(_selection(choice, {}), SelectionConfig())
        self.assertEqual(primary, RuleSpec("H4", "B24", "OFF"))

    def test_fallback_is_the_best_ranked_stage1_rule_ties_to_the_simpler(self):
        cfg = SelectionConfig()
        results = _empty_results(cfg)
        good = dict(block_wr=(60.0,) * 4, block_lb=(55.0,) * 4, **THICK)
        for key in ("H1|B12|ADX|OFF|OFF", "E200|P40|OFF|OFF|OFF"):
            results[key] = replace(results[key], **good)
        primary = pipeline._primary_rule(_selection(None, results), cfg)
        self.assertEqual(primary, RuleSpec("E200", "P40", "OFF"))

    def test_fallback_primary_forces_strength_off(self):
        cfg = SelectionConfig()
        results = _empty_results(cfg)
        key = "H1|B12|ADX_DI|OFF|OFF"
        results[key] = replace(results[key], block_wr=(60.0,) * 4, block_lb=(55.0,) * 4, **THICK)
        primary = pipeline._primary_rule(_selection(None, results), cfg)
        self.assertEqual(primary, RuleSpec("H1", "B12", "OFF", "OFF", "OFF"))

    def test_without_a_ranked_rule_the_best_median_block_bound_is_primary(self):
        cfg = SelectionConfig()
        results = _empty_results(cfg)
        no_vote = dict(block_wr=(45.0,) * 4, **THICK)  # every rule ranks -inf
        for key, lb, wr in (("H1|B12|ADX|OFF|OFF", 40.0, 45.0), ("H4|P35|OFF|OFF|OFF", 42.0, 44.0),
                            ("E200|B24|OFF|OFF|OFF", None, 60.0)):
            results[key] = replace(results[key], block_lb=(lb,) * 4, wr=wr, **no_vote)
        primary = pipeline._primary_rule(_selection(None, results), cfg)
        self.assertEqual(primary, RuleSpec("H4", "P35", "OFF"))

    def test_without_any_block_bound_pooled_wr_then_the_simpler_rule_decides(self):
        cfg = SelectionConfig()
        results = _empty_results(cfg)
        for key, wr in (("H4|B24|ADX|OFF|OFF", 48.0), ("H1|P40|OFF|OFF|OFF", 49.0),
                        ("E200|B12|ADX_DI|OFF|OFF", 49.0)):
            results[key] = replace(results[key], wr=wr)
        primary = pipeline._primary_rule(_selection(None, results), cfg)
        self.assertEqual(primary, RuleSpec("E200", "B12", "OFF"))  # tie -> Stage-1 order

    def test_no_rule_with_any_tp_sl_means_no_primary_and_no_contender3(self):
        cfg = SelectionConfig()
        self.assertIsNone(pipeline._primary_rule(_selection(None, _empty_results(cfg)), cfg))
        out = pipeline._contender3(None, None, FakeMetaTrainer())
        self.assertIsNone(out["meta_model"])
        self.assertEqual(out["n_candidates"], 0)


class RegimeCountTests(PipelineFixture):
    def test_counts_per_month_with_insufficient_cells(self):
        start = TEST_TRAIN_START
        out = regime_counts(self.dataset.frames, start, start + 4 * HOUR)
        cell = out["BTC-USDT"]["H1"]["2026-07"]
        self.assertEqual(cell["up"] + cell["down"] + cell["neutral"], cell["n"])
        self.assertEqual(cell["n"] + cell["unknown"], 48)
        self.assertTrue(cell["insufficient"])
        full = regime_counts(self.dataset.frames, start, start + DAY)
        cell = full["ETH-USDT"]["E200"]["2026-07"]
        self.assertEqual(cell["n"], 288)
        self.assertFalse(cell["insufficient"])
        shares = cell["share_up"] + cell["share_down"] + cell["share_neutral"]
        self.assertAlmostEqual(shares, 1.0)

    def test_months_split_on_wib_calendar(self):
        out = regime_counts(self.dataset.frames, wib(2026, 7, 31, 23), wib(2026, 8, 1, 1))
        months = out["BTC-USDT"]["H4"]
        self.assertEqual(months["2026-07"]["n"] + months["2026-07"]["unknown"], 12)
        self.assertEqual(months["2026-08"]["n"] + months["2026-08"]["unknown"], 12)


def spec_doc(sha, **extra):
    doc = {"spec_sha256": sha, "contender1": "H1|P35|OFF|OFF|OFF", **spec_run_record()}
    doc.update(extra)
    return doc


class HoldoutLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pine_lock_"))
        self.path = self.tmp / "sel.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, doc):
        self.path.write_text(json.dumps(doc))

    def test_load_selection_refuses_missing_file(self):
        with self.assertRaises(HoldoutLockError):
            load_selection_checked(self.tmp / "none.json", "abc")

    def test_load_selection_refuses_sha_mismatch(self):
        self.write(spec_doc("a" * 64))
        with self.assertRaises(HoldoutLockError) as ctx:
            load_selection_checked(self.path, "b" * 64)
        self.assertIn(str(self.path), str(ctx.exception))

    def test_load_selection_refuses_invalid_json(self):
        self.path.write_text("{not json")
        with self.assertRaises(HoldoutLockError):
            load_selection_checked(self.path, "a" * 64)

    def test_load_selection_refuses_missing_or_non_string_sha(self):
        for doc in ({"contender1": None}, [1, 2], spec_doc(None)):
            self.write(doc)
            with self.assertRaises(HoldoutLockError):
                load_selection_checked(self.path, "a" * 64)

    def test_load_selection_returns_the_document_when_everything_matches(self):
        doc = spec_doc("c" * 64)
        self.write(doc)
        self.assertEqual(load_selection_checked(self.path, "c" * 64), doc)

    def test_load_selection_refuses_one_changed_config_field(self):
        for section, field, value in (("exec", "fee_pct", 0.05), ("guard", "max_entries", 35),
                                      ("selection", "min_tp_sl", 1),
                                      ("model", "threshold_hi", 0.8)):
            doc = spec_doc("c" * 64)
            doc["configs"][section][field] = value
            self.write(doc)
            with self.subTest(section):
                with self.assertRaises(HoldoutLockError) as ctx:
                    load_selection_checked(self.path, "c" * 64)
                self.assertIn("configs", str(ctx.exception))

    def test_load_selection_refuses_non_spec_segments_or_trainer(self):
        segments = spec_run_record()["segments"]
        segments["train_end_ms"] -= DAY
        for field, value in (("segments", segments), ("meta_trainer", "<injected>"),
                             ("configs", None)):
            self.write(spec_doc("c" * 64, **{field: value}))
            with self.subTest(field):
                with self.assertRaises(HoldoutLockError) as ctx:
                    load_selection_checked(self.path, "c" * 64)
                self.assertIn(field, str(ctx.exception))
        doc = spec_doc("c" * 64)
        del doc["meta_trainer"]
        self.write(doc)
        with self.assertRaises(HoldoutLockError):
            load_selection_checked(self.path, "c" * 64)


if __name__ == "__main__":
    unittest.main()
