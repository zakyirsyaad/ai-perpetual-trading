import hashlib
import json
import math
import random
import shutil
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from research.pine_gate.bars import (
    day_path,
    funding_path,
    wib_day_start_ms,
    warmup_path,
    write_day,
)
from research.pine_gate.calibrate import Calibration
from research.pine_gate import holdout, lock
from research.pine_gate.holdout import test_stage as run_test_stage
from research.pine_gate.holdout_replay import (
    HoldoutPeriod,
    contender3_fn,
    holdout_markets,
    parse_rule,
    permutation_ks,
    shifted_time,
    simulate_holdout,
)
from research.pine_gate.holdout_gate import (
    EVIDENCE_KEYS,
    GateVerdict,
    choose_contender,
    gate_symbol,
    traded_symbols,
)
from research.pine_gate.metalabel import FEATURES, MetaModel
from research.pine_gate.pipeline import (
    SYMBOLS,
    HoldoutLockError,
    Segments,
    contender2_fn,
    load_dataset,
    rule_signal_fn,
)
from research.pine_gate.spec import (
    HOLDOUT_END,
    HOLDOUT_START,
    LAST20_END,
    LAST20_START,
    WIB,
    ExecConfig,
    GateConfig,
    GuardConfig,
    ModelConfig,
    SelectionConfig,
    ms,
    spec_sha256,
)

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
H4 = 4 * HOUR
DUMMY_SHA = "0" * 64
BTC, ETH = SYMBOLS


def wib(year, month, day, hour=0):
    return ms(datetime(year, month, day, hour, tzinfo=WIB))


# --------------------------------------------------------------------------- gate


def passing_evidence(contender="c1", symbol=BTC, **override):
    evidence = {
        "contender": contender,
        "symbol": symbol,
        "tp": 70,
        "sl": 30,
        "gross_bps": 3.0,
        "lower_bound": 58.0,
        "p_holm": 0.01,
        "wr_halves": [69.0, 71.0],
        "wr_overshoot": 66.0,
        "wr_offsets": [60.0] * 20,
        "source_p": 0.01,
        "supply_holdout": 12.0,
        "supply_last20": 11.0,
        "c0_long_wr": 49.0,
        "c0_short_wr": 51.0,
        "c0_long_gross": -1.0,
        "c0_short_gross": -1.5,
        "pbo_pass": True,
    }
    evidence.update(override)
    return evidence


class GateTests(unittest.TestCase):
    cfg = GateConfig()

    def test_passing_evidence_passes(self):
        for contender in ("c1", "c2", "c3"):
            verdict = gate_symbol(passing_evidence(contender), self.cfg)
            self.assertTrue(verdict.passed, contender)
            self.assertEqual(verdict.failed_criteria, ())
            self.assertEqual((verdict.contender, verdict.symbol), (contender, BTC))
            self.assertAlmostEqual(verdict.wr, 70.0)
            self.assertEqual((verdict.tp, verdict.sl), (70, 30))
            self.assertEqual(verdict.lower_bound, 58.0)
            self.assertEqual(verdict.gross_bps, 3.0)
            self.assertEqual(verdict.p_holm, 0.01)

    def test_gate_fails_on_each_single_criterion(self):
        breaks = [
            (1, {"tp": 40, "sl": 19}),  # 59 TP+SL, WR still 67.8
            (2, {"p_holm": 0.05}),
            (3, {"tp": 56, "sl": 44}),  # WR 56
            (4, {"gross_bps": 0.0}),
            (4, {"gross_bps": None}),
            (5, {"wr_halves": [50.0, 70.0]}),
            (5, {"wr_halves": [70.0, None]}),
            (6, {"wr_overshoot": 54.9}),
            (6, {"wr_overshoot": None}),
            (6, {"wr_offsets": [60.0] * 14 + [50.0] * 6}),  # 50 is not > 50
            (6, {"wr_offsets": [60.0] * 14 + [None] * 6}),
            (6, {"wr_offsets": [60.0] * 14}),  # missing offset runs count as failing
            (7, {"source_p": 0.05}),
            (8, {"supply_holdout": 9.99}),
            (8, {"supply_last20": 9.0}),
            (9, {"c0_long_wr": 46.9}),
            (9, {"c0_short_wr": 53.1}),
            (9, {"c0_long_gross": -2.01}),
            (9, {"c0_short_gross": 2.01}),
            (9, {"c0_long_wr": None}),
            (9, {"pbo_pass": False}),
        ]
        for number, change in breaks:
            with self.subTest(number=number, change=change):
                verdict = gate_symbol(passing_evidence("c1", **change), self.cfg)
                self.assertFalse(verdict.passed)
                self.assertEqual(verdict.failed_criteria, (number,))

    def test_boundaries_that_pass(self):
        for change in ({"tp": 35, "sl": 25},  # exactly 60, WR 58.3
                       {"tp": 57, "sl": 43},  # WR exactly 57
                       {"wr_overshoot": 55.0},
                       {"wr_offsets": [60.0] * 15 + [40.0] * 5},
                       {"supply_holdout": 10.0, "supply_last20": 10.0},
                       {"c0_long_wr": 47.0, "c0_short_wr": 53.0},
                       {"c0_long_gross": -2.0, "c0_short_gross": 2.0}):
            with self.subTest(change=change):
                self.assertTrue(gate_symbol(passing_evidence(**change), self.cfg).passed)

    def test_pbo_applies_to_c1_and_c3_only(self):
        for contender, expected in (("c1", (9,)), ("c2", ()), ("c3", (9,))):
            for pbo in (False, None):
                verdict = gate_symbol(passing_evidence(contender, pbo_pass=pbo), self.cfg)
                self.assertEqual(verdict.failed_criteria, expected, (contender, pbo))

    def test_failures_are_sorted_and_all_reported(self):
        verdict = gate_symbol(
            passing_evidence(tp=0, sl=0, source_p=1.0, supply_last20=0.0), self.cfg
        )
        self.assertEqual(verdict.failed_criteria, (1, 3, 7, 8))
        self.assertIsNone(verdict.wr)

    def test_missing_key_or_unknown_contender_is_refused(self):
        evidence = passing_evidence()
        del evidence["source_p"]
        with self.assertRaises(ValueError) as ctx:
            gate_symbol(evidence, self.cfg)
        self.assertIn("source_p", str(ctx.exception))
        with self.assertRaises(ValueError):
            gate_symbol(passing_evidence("c4"), self.cfg)
        self.assertEqual(set(EVIDENCE_KEYS), set(passing_evidence()))

    def test_gate_reads_its_thresholds_from_the_config(self):
        cfg = replace(self.cfg, min_tp_sl=200, offsets_min_pass=21, alpha=0.001)
        verdict = gate_symbol(passing_evidence(), cfg)
        self.assertEqual(verdict.failed_criteria, (1, 2, 6, 7))


def verdict(contender, symbol, passed, tp=70, sl=30):
    wr = 100.0 * tp / (tp + sl) if tp + sl else None
    return GateVerdict(contender, symbol, passed, () if passed else (3,), wr, None, 1.0,
                       0.01, tp, sl)


class ChoiceTests(unittest.TestCase):
    def test_choice_prefers_more_symbols_then_lower_bound_then_simpler(self):
        # more symbols wins over a higher lower bound
        vs = [verdict("c2", BTC, True, 60, 40), verdict("c2", ETH, False, 60, 40),
              verdict("c3", BTC, True), verdict("c3", ETH, True)]
        self.assertEqual(choose_contender(vs, {"c2": 70.0, "c3": 55.0}), "c3")
        # same count: the higher pooled lower bound
        vs = [verdict("c1", BTC, True, 70, 30), verdict("c2", BTC, True, 60, 40),
              verdict("c1", ETH, False), verdict("c2", ETH, False)]
        self.assertEqual(choose_contender(vs, {"c1": 58.0, "c2": 55.0}), "c1")
        self.assertEqual(choose_contender(vs, {"c1": 54.0, "c2": 55.0}), "c2")
        # a defined lower bound beats an undefined one
        self.assertEqual(choose_contender(vs, {"c1": 50.0, "c2": None}), "c1")
        # full tie: the simpler contender, c2 then c1 then c3
        self.assertEqual(choose_contender(vs, {"c1": 55.0, "c2": 55.0}), "c2")
        vs = [verdict("c1", BTC, True, 70, 30), verdict("c3", BTC, True, 70, 30),
              verdict("c2", BTC, False, 60, 40)]
        self.assertEqual(choose_contender(vs, {"c1": 55.0, "c3": 55.0}), "c1")

    def test_c1_or_c3_must_beat_c2(self):
        # c1 passes on more symbols, but c2's holdout WR over the same symbols is higher
        vs = [verdict("c1", BTC, True, 60, 40), verdict("c1", ETH, True, 60, 40),
              verdict("c2", BTC, True, 65, 35), verdict("c2", ETH, False, 65, 35)]
        self.assertEqual(choose_contender(vs, {"c1": 58.0, "c2": 50.0}), "c2")
        # equal WR is not beating c2
        vs = [verdict("c3", BTC, True, 60, 40), verdict("c2", BTC, False, 60, 40)]
        self.assertIsNone(choose_contender(vs, {"c3": 58.0}))
        # c2 compared over the contender's passing symbols only
        vs = [verdict("c1", BTC, True, 70, 30), verdict("c1", ETH, False, 10, 90),
              verdict("c2", BTC, False, 60, 40), verdict("c2", ETH, False, 90, 10)]
        self.assertEqual(choose_contender(vs, {"c1": 58.0}), "c1")

    def test_c2_without_trades_counts_as_50(self):
        for tp, sl, expected in ((51, 49, "c1"), (50, 50, None)):
            vs = [verdict("c1", BTC, True, tp, sl), verdict("c2", BTC, False, 0, 0)]
            self.assertEqual(choose_contender(vs, {"c1": 45.0}), expected)
        self.assertEqual(choose_contender([verdict("c3", BTC, True, 55, 45)], {}), "c3")

    def test_nothing_passes_means_no_choice(self):
        vs = [verdict(c, s, False) for c in ("c1", "c2", "c3") for s in SYMBOLS]
        self.assertIsNone(choose_contender(vs, {}))
        self.assertEqual(traded_symbols(vs, None), ())

    def test_failing_symbol_is_dropped(self):
        vs = [verdict("c2", BTC, True), verdict("c2", ETH, False),
              verdict("c1", ETH, True, 50, 50), verdict("c1", BTC, False, 50, 50)]
        chosen = choose_contender(vs, {"c2": 55.0, "c1": 60.0})
        self.assertEqual(chosen, "c2")
        self.assertEqual(traded_symbols(vs, chosen), (BTC,))

    def test_unknown_contender_is_refused(self):
        with self.assertRaises(ValueError):
            choose_contender([verdict("c9", BTC, True)], {})

    def test_verdict_counts_are_required(self):
        with self.assertRaises(TypeError):
            GateVerdict("c2", BTC, True, (), 70.0, None, 1.0, 0.01)


# --------------------------------------------------------------------------- helpers


class PermutationTests(unittest.TestCase):
    def test_shifted_time_is_circular_whole_days_back(self):
        start = wib(2026, 8, 5)
        self.assertEqual(shifted_time(start + 10 * DAY + 5, start, 70, 7), start + 3 * DAY + 5)
        self.assertEqual(shifted_time(start + 2 * DAY, start, 70, 7), start + 65 * DAY)
        for t in range(start, start + 70 * DAY, 7 * HOUR + 13):
            s = shifted_time(t, start, 70, 30)
            self.assertTrue(start <= s < start + 70 * DAY)
            self.assertEqual((s - t) % DAY, 0)

    def test_k_is_drawn_in_7_to_d_minus_7_with_its_own_seed(self):
        ks = permutation_ks(seed=4, n=1000, days=70)
        self.assertEqual(len(ks), 1000)
        self.assertEqual((min(ks), max(ks)), (7, 63))
        self.assertEqual(ks, permutation_ks(seed=4, n=1000, days=70))
        self.assertNotEqual(ks, permutation_ks(seed=5, n=1000, days=70))
        self.assertEqual(set(permutation_ks(seed=0, n=50, days=14)), {7})
        with self.assertRaises(ValueError):
            permutation_ks(seed=0, n=10, days=13)


class PeriodTests(unittest.TestCase):
    def test_spec_period(self):
        p = HoldoutPeriod.spec()
        self.assertEqual((p.start_ms, p.end_ms), (ms(HOLDOUT_START), ms(HOLDOUT_END)))
        self.assertEqual((p.last20_start_ms, p.last20_end_ms), (ms(LAST20_START), ms(LAST20_END)))
        self.assertEqual(len(p.last20_days()), 20)
        self.assertEqual(p.length_days(), 70)
        self.assertEqual(p.blocked_windows(), ((ms(HOLDOUT_END) - 65 * MIN, ms(HOLDOUT_END)),))
        self.assertEqual(p.mid_ms, ms(HOLDOUT_START) + 35 * DAY)

    def test_period_must_be_ordered_and_contain_last20(self):
        start, end = wib(2026, 9, 28), wib(2026, 10, 14)
        for args in ((end, start, start, end), (start, end, start - DAY, end),
                     (start, end, start, end + DAY),
                     (start, end, wib(2026, 10, 3), wib(2026, 10, 5))):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    HoldoutPeriod(*args)


class SupplyCriterionTests(unittest.TestCase):
    """Criterion 8 counts the combined supply of the symbols passing 1-7 and 9."""

    def evidence(self, symbol, **override):
        ev = passing_evidence("c2", symbol, **override)
        del ev["supply_holdout"], ev["supply_last20"]
        return ev

    def test_passing_symbols_pool_their_supply(self):
        metrics = {BTC: {"supply": 6.0, "supply_last20": 5.0},
                   ETH: {"supply": 5.0, "supply_last20": 5.0}}
        evidence = {BTC: self.evidence(BTC), ETH: self.evidence(ETH)}
        full, verdicts = holdout._gate_contender(evidence, metrics, GateConfig())
        self.assertTrue(verdicts[BTC].passed and verdicts[ETH].passed)
        self.assertEqual((full[BTC]["supply_holdout"], full[BTC]["supply_last20"]), (11.0, 10.0))
        self.assertEqual(full[BTC]["supply_set"], [BTC, ETH])
        # ETH fails criterion 3: BTC alone no longer supplies 10 a day
        evidence[ETH] = self.evidence(ETH, tp=50, sl=50)
        full, verdicts = holdout._gate_contender(evidence, metrics, GateConfig())
        self.assertEqual(verdicts[BTC].failed_criteria, (8,))
        self.assertEqual(verdicts[ETH].failed_criteria, (3,))
        self.assertEqual(full[ETH]["supply_holdout"], 11.0)  # the passing set plus itself
        self.assertEqual(full[BTC]["supply_holdout"], 6.0)
        self.assertEqual((full[BTC]["supply_set"], full[ETH]["supply_set"]), ([BTC], [BTC, ETH]))

    def test_pooled_lower_bound_uses_passing_symbols_only(self):
        from types import SimpleNamespace
        from research.pine_gate.simulate import Trade
        days = [date(2026, 9, 28) + timedelta(days=k) for k in range(4)]
        trades = []
        for d in days:
            for kind in ("TP", "TP", "SL"):
                trades.append(Trade(BTC, 1, 0, 0, 0, kind, 1.0, 0.0, d))
            trades.append(Trade(ETH, 1, 0, 0, 0, "SL", -1.0, 0.0, d))
        h = SimpleNamespace(days=days)
        runs = SimpleNamespace(main=tuple(trades))
        both = {BTC: verdict("c2", BTC, True), ETH: verdict("c2", ETH, True)}
        only_btc = {BTC: verdict("c2", BTC, True), ETH: verdict("c2", ETH, False)}
        none = {BTC: verdict("c2", BTC, False), ETH: verdict("c2", ETH, False)}
        lb_btc = holdout._pooled_lower_bound(h, runs, only_btc)
        self.assertAlmostEqual(lb_btc, 100 * 2 / 3)  # identical days: SE 0
        self.assertAlmostEqual(holdout._pooled_lower_bound(h, runs, both), 50.0)
        self.assertIsNone(holdout._pooled_lower_bound(h, runs, none))


class HolmFamilyTests(unittest.TestCase):
    """Holm runs over the spec's family of three even when a contender is absent (A3)."""

    def test_absent_contenders_count_with_p_one(self):
        p = {"c2": {BTC: 0.01, ETH: 0.40}, "c3": {BTC: 0.02, ETH: 0.03}}
        out = holdout._holm_by_symbol(p, family=3)
        self.assertAlmostEqual(out["c2"][BTC], 0.03)  # 3 x 0.01, not 2 x 0.01
        self.assertAlmostEqual(out["c3"][BTC], 0.04)  # 2 x 0.02
        self.assertAlmostEqual(out["c3"][ETH], 0.09)
        self.assertAlmostEqual(out["c2"][ETH], 0.80)
        self.assertEqual(sorted(out), ["c2", "c3"])
        alone = holdout._holm_by_symbol({"c2": {BTC: 0.2, ETH: 0.5}}, family=3)
        self.assertEqual(alone, {"c2": {BTC: 0.6000000000000001, ETH: 1.0}})
        full = {n: {BTC: 0.01, ETH: 0.01} for n in ("c1", "c2", "c3")}
        self.assertAlmostEqual(holdout._holm_by_symbol(full, family=3)["c1"][BTC], 0.03)


class SmallHelperTests(unittest.TestCase):
    def test_permutation_p_is_conservative_about_missing_wr(self):
        self.assertEqual(holdout._shifted_p(None, [10.0, 20.0]), 1.0)
        self.assertAlmostEqual(holdout._shifted_p(60.0, [50.0, None, 70.0]), 3 / 4)

    def test_parse_rule_round_trips_and_refuses_bad_keys(self):
        self.assertEqual(parse_rule(RULE).key(), RULE)
        for key in ("H1|P40|OFF|OFF", "H2|P40|OFF|OFF|OFF", "H1|P40|OFF|OFF|OFF|X", None):
            with self.assertRaises(ValueError):
                parse_rule(key)

    def test_quantile_interpolates(self):
        self.assertIsNone(holdout._quantile([], 0.5))
        self.assertEqual(holdout._quantile([1.0, 3.0], 0.5), 2.0)
        self.assertEqual(holdout._quantile([4.0], 0.95), 4.0)


# --------------------------------------------------------------------------- stage

FIRST_DAY = date(2026, 9, 21)
LAST_DAY = date(2026, 10, 13)
TEST_PERIOD = HoldoutPeriod(wib(2026, 9, 28), ms(HOLDOUT_END), wib(2026, 10, 6), ms(HOLDOUT_END))
BASE_PRICE = {BTC: 60_000.0, ETH: 3_000.0}
RULE = "H1|P40|OFF|OFF|OFF"
CALIB = Calibration(s_star_bps=1.0, n_stops=20, latencies_ms=(20_000.0, 30_000.0, 40_000.0))
NO_GUARDS = GuardConfig(max_stops=10_000, max_consecutive_losses=10_000, max_drawdown_usdt=1e12,
                        max_entries=10_000, max_volume_usdt=1e12)
SMALL = {"offsets": 3, "permutation_shifts": 20, "c1_seeds": 5, "bootstrap_n": 200}


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


def write_data_dir(root):
    first_ms = wib_day_start_ms(FIRST_DAY)
    for i, symbol in enumerate(SYMBOLS):
        rng = random.Random(200 + i)
        price = BASE_PRICE[symbol]
        write_day(warmup_path(root, symbol, "1h"),
                  _walk_rows(rng, first_ms - 1000 * HOUR, 1000, HOUR, price * 0.97, 0.003))
        last_4h = first_ms // H4 * H4
        write_day(warmup_path(root, symbol, "4h"),
                  _walk_rows(rng, last_4h - 499 * H4, 500, H4, price * 0.95, 0.006))
        day = FIRST_DAY
        while day <= LAST_DAY:
            rows = _walk_rows(rng, wib_day_start_ms(day), 1440, MIN, price, 0.0004)
            write_day(day_path(root, "prod", symbol, "1m", day), rows)
            price = rows[-1][4]
            day += timedelta(days=1)
        end_ms = wib_day_start_ms(LAST_DAY + timedelta(days=1))
        times = list(range(first_ms // (8 * HOUR) * (8 * HOUR), end_ms + 2 * DAY, 8 * HOUR))
        path = funding_path(root, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(times))


def spec_run_record():
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


def meta_model_dict():
    coef = [0.0] * len(FEATURES)
    coef[FEATURES.index("hour_sin")] = 2.0  # c3 keeps WIB hours 0-11
    return MetaModel(coef=tuple(coef), intercept=0.0, means=(0.0,) * len(FEATURES),
                     stds=(1.0,) * len(FEATURES), threshold=0.5, C=0.1).to_dict()


def latencies_sha256(latencies):
    """The selection file's binding of the latency sample (explicit formula)."""
    payload = json.dumps([float(x) for x in latencies], allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_selection(path, sha, **override):
    doc = {"spec_sha256": sha, **spec_run_record(), "contender1": RULE, "primary_rule": RULE,
           "pbo": 0.2, "pbo_pass": True, "s_star_bps": 1.0, "n_stops": 20, "n_latencies": 3,
           "meta_model": meta_model_dict(), "seed": 0,
           "latencies_sha256": latencies_sha256(CALIB.latencies_ms),
           "code_sha256": lock.code_sha256(), "c0_training_pass": True,
           "train_data_sha256": "e" * 64}
    doc.update(override)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))
    return path


class _Shared:
    built = False


def _build_shared():
    if _Shared.built:
        return
    _Shared.tmp = Path(tempfile.mkdtemp(prefix="pine_holdout_"))
    write_data_dir(_Shared.tmp / "data")
    _Shared.dataset = load_dataset(_Shared.tmp / "data", until_ms=None, start_day=FIRST_DAY)
    _Shared.selection_path = write_selection(_Shared.tmp / "sel" / "selection.json", DUMMY_SHA)
    _Shared.out_dir = _Shared.tmp / "out"
    _Shared.path = run_test_stage(
        _Shared.dataset, _Shared.selection_path, CALIB, _Shared.out_dir, DUMMY_SHA, seed=2,
        period=TEST_PERIOD, guard_cfg=NO_GUARDS, **SMALL,
    )
    _Shared.doc = json.loads(_Shared.path.read_text())
    _Shared.built = True


def tearDownModule():
    if _Shared.built:
        shutil.rmtree(_Shared.tmp, ignore_errors=True)


class _Untouchable:
    """A dataset stand-in that fails the test if anything reads it."""

    def __getattr__(self, name):
        raise AssertionError(f"dataset.{name} read before the lock refused")


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pine_holdout_lock_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_stage(self, selection, calib=CALIB, sha=DUMMY_SHA, out_dir=None, dataset=None,
                  **kwargs):
        out = self.tmp / "out" if out_dir is None else out_dir
        data = _Untouchable() if dataset is None else dataset
        return run_test_stage(data, selection, calib, out, sha, **kwargs)

    def test_lock_is_checked_before_any_holdout_data_is_read(self):
        with self.assertRaises(HoldoutLockError):
            self.run_stage(self.tmp / "missing.json")
        path = write_selection(self.tmp / "sel.json", "a" * 64)
        with self.assertRaises(HoldoutLockError):
            self.run_stage(path)
        path = write_selection(self.tmp / "sel2.json", DUMMY_SHA, meta_trainer="<injected>")
        with self.assertRaises(HoldoutLockError):
            self.run_stage(path)
        self.assertFalse((self.tmp / "out").exists())

    def test_calibration_must_match_the_selection(self):
        path = write_selection(self.tmp / "sel.json", DUMMY_SHA)
        for calib in (replace(CALIB, s_star_bps=1.5), replace(CALIB, latencies_ms=(1.0,))):
            with self.subTest(calib=calib):
                with self.assertRaises(HoldoutLockError):
                    self.run_stage(path, calib=calib)

    def test_real_spec_sha_refuses_injected_settings(self):
        sha = spec_sha256()
        path = write_selection(self.tmp / "sel.json", sha)
        injections = [{"offsets": 3}, {"permutation_shifts": 20}, {"c1_seeds": 5},
                      {"bootstrap_n": 200}, {"guard_cfg": NO_GUARDS},
                      {"exec_cfg": replace(ExecConfig(), fee_pct=0.05)},
                      {"gate_cfg": replace(GateConfig(), min_wr=50.0)}, {"period": TEST_PERIOD}]
        for kwargs in injections:
            with self.subTest(kwargs=sorted(kwargs)):
                with self.assertRaises(ValueError) as ctx:
                    self.run_stage(path, sha=sha, **kwargs)
                self.assertIn("spec run", str(ctx.exception))

    def test_any_existing_holdout_file_refuses_the_run(self):
        path = write_selection(self.tmp / "sel.json", DUMMY_SHA)
        old = self.tmp / "out" / "pine_gate_holdout_20260101.json"
        old.parent.mkdir(parents=True)
        old.write_text("{}")
        with self.assertRaises(FileExistsError) as ctx:
            self.run_stage(path)
        self.assertIn(old.name, str(ctx.exception))
        self.assertEqual(old.read_text(), "{}")

    def test_real_spec_sha_requires_the_selection_seed(self):
        sha = spec_sha256()
        for recorded, seed in ((0, 1), (3, 0), (None, 0)):
            path = write_selection(self.tmp / f"sel_{recorded}_{seed}.json", sha, seed=recorded)
            with self.subTest(recorded=recorded, seed=seed):
                with self.assertRaises(ValueError) as ctx:
                    self.run_stage(path, sha=sha, seed=seed)
                self.assertIn("seed", str(ctx.exception))
        # the recorded seed passes this check and goes on to read the dataset
        path = write_selection(self.tmp / "sel_ok.json", sha, seed=3)
        with self.assertRaises(AssertionError):
            self.run_stage(path, sha=sha, seed=3, out_dir=self.tmp)
        # a dummy sha may pick any seed
        path = write_selection(self.tmp / "sel_dummy.json", DUMMY_SHA, seed=3)
        with self.assertRaises(AssertionError):
            self.run_stage(path, seed=7)

    def test_latency_sample_must_be_the_selection_one(self):
        path = write_selection(self.tmp / "sel.json", DUMMY_SHA)
        same_size = replace(CALIB, latencies_ms=(21_000.0, 30_000.0, 40_000.0))
        with self.assertRaises(HoldoutLockError) as ctx:
            self.run_stage(path, calib=same_size)
        self.assertIn("latenc", str(ctx.exception))
        path = write_selection(self.tmp / "sel_old.json", DUMMY_SHA, latencies_sha256=None)
        with self.assertRaises(HoldoutLockError):
            self.run_stage(path)

    def test_real_spec_sha_refuses_a_selection_made_by_other_code(self):
        sha = spec_sha256()
        for code in ("f" * 64, None):
            path = write_selection(self.tmp / f"sel_{code}.json", sha, code_sha256=code)
            with self.subTest(code=code):
                with self.assertRaises(HoldoutLockError) as ctx:
                    self.run_stage(path, sha=sha, out_dir=self.tmp)
                self.assertIn("code sha256", str(ctx.exception))
        # a dummy sha is a test run: the code hash is recorded, not enforced
        path = write_selection(self.tmp / "sel_dummy.json", DUMMY_SHA, code_sha256="f" * 64)
        with self.assertRaises(AssertionError):
            self.run_stage(path)

    def test_real_spec_sha_refuses_a_failed_training_c0(self):
        sha = spec_sha256()
        for value in (False, None):
            path = write_selection(self.tmp / f"sel_{value}.json", sha, c0_training_pass=value)
            with self.subTest(value=value):
                with self.assertRaises(HoldoutLockError) as ctx:
                    self.run_stage(path, sha=sha, out_dir=self.tmp)
                self.assertIn("fill model invalid on training C0; fix before test",
                              str(ctx.exception))

    def test_real_spec_sha_writes_the_holdout_next_to_its_selection(self):
        sha = spec_sha256()
        path = write_selection(self.tmp / "sel" / "selection.json", sha)
        with self.assertRaises(HoldoutLockError) as ctx:
            self.run_stage(path, sha=sha, out_dir=self.tmp / "elsewhere")
        self.assertIn("next to its selection file", str(ctx.exception))
        with self.assertRaises(AssertionError):  # same folder: goes on to the data
            self.run_stage(path, sha=sha, out_dir=self.tmp / "sel" / ".")

    def test_a_holdout_file_next_to_the_selection_refuses_any_out_dir(self):
        path = write_selection(self.tmp / "sel" / "selection.json", DUMMY_SHA)
        old = self.tmp / "sel" / "pine_gate_holdout_20260101.json"
        old.write_text("{}")
        with self.assertRaises(FileExistsError) as ctx:
            self.run_stage(path, out_dir=self.tmp / "fresh_out")
        self.assertIn(old.name, str(ctx.exception))
        self.assertFalse((self.tmp / "fresh_out").exists())

    def test_real_spec_sha_refuses_holdout_data_whose_training_part_differs(self):
        _build_shared()
        sha = spec_sha256()
        path = write_selection(self.tmp / "sel.json", sha)  # train_data_sha256 "eee..."
        with self.assertRaises(HoldoutLockError) as ctx:
            self.run_stage(path, sha=sha, out_dir=self.tmp, dataset=_Shared.dataset)
        self.assertIn("training part", str(ctx.exception))
        self.assertEqual(list(self.tmp.glob("pine_gate_holdout_*.json")), [])

    def test_counts_must_be_positive_integers(self):
        path = write_selection(self.tmp / "sel.json", DUMMY_SHA)
        for kwargs in ({"offsets": 0}, {"bootstrap_n": 2.5}, {"c1_seeds": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    self.run_stage(path, **kwargs)

    def test_malformed_sha_is_refused(self):
        path = write_selection(self.tmp / "sel.json", "XYZ")
        with self.assertRaises(ValueError):
            self.run_stage(path, sha="XYZ")


class StageFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _build_shared()
        for name in ("tmp", "dataset", "selection_path", "out_dir", "path", "doc"):
            setattr(cls, name, getattr(_Shared, name))


class TestStageTests(StageFixture):
    def test_file_name_and_provenance(self):
        self.assertTrue(self.path.name.startswith("pine_gate_holdout_"))
        self.assertRegex(self.path.name, r"^pine_gate_holdout_\d{8}\.json$")
        doc = self.doc
        self.assertEqual(doc["spec_sha256"], DUMMY_SHA)
        self.assertEqual(doc["selection_sha256"],
                         hashlib.sha256(self.selection_path.read_bytes()).hexdigest())
        self.assertEqual(doc["data_sha256"], self.dataset.data_sha256)
        self.assertEqual(doc["data_content_sha256"], self.dataset.train_data_sha256)
        self.assertEqual(doc["counts"], SMALL)
        self.assertEqual(doc["configs"]["guard"], asdict(NO_GUARDS))
        self.assertEqual(doc["configs"]["gate"]["offsets"], 3)
        self.assertEqual(doc["period"], TEST_PERIOD.to_dict())
        self.assertEqual(doc["seed"], 2)
        self.assertEqual(doc["calibration"]["s_star_bps"], 1.0)
        self.assertEqual(doc["code_sha256"], lock.code_sha256())
        self.assertEqual(doc["train_part_sha256"], self.dataset.train_part_sha256)

    def test_never_overwrites_the_holdout_file(self):
        with self.assertRaises(FileExistsError):
            run_test_stage(self.dataset, self.selection_path, CALIB, self.out_dir, DUMMY_SHA,
                           seed=2, period=TEST_PERIOD, guard_cfg=NO_GUARDS, **SMALL)

    def test_every_contender_and_symbol_has_a_verdict(self):
        contenders = self.doc["contenders"]
        self.assertEqual(sorted(contenders), ["c1", "c2", "c3"])
        self.assertEqual(contenders["c1"]["rule"], RULE)
        self.assertEqual(contenders["c3"]["rule"], RULE)
        for name, block in contenders.items():
            self.assertEqual(sorted(block["symbols"]), sorted(SYMBOLS))
            for symbol, row in block["symbols"].items():
                for key in ("n", "tp", "sl", "time", "wr", "lower_bound", "gross_bps",
                            "net_per_1m", "net_per_1m_fee_sensitivity", "supply",
                            "supply_last20", "halves", "robustness", "source", "p_c0",
                            "p_holm", "evidence", "verdict"):
                    self.assertIn(key, row, (name, symbol, key))
                self.assertEqual(row["n"], row["tp"] + row["sl"] + row["time"])
                self.assertEqual(len(row["robustness"]["wr_offsets"]), 3)

    def test_recorded_verdicts_follow_from_the_recorded_evidence(self):
        cfg = GateConfig(**self.doc["configs"]["gate"])
        verdicts = []
        for name, block in self.doc["contenders"].items():
            for symbol, row in block["symbols"].items():
                v = gate_symbol(row["evidence"], cfg)
                self.assertEqual(list(v.failed_criteria), row["verdict"]["failed_criteria"])
                self.assertEqual(v.passed, row["verdict"]["passed"])
                verdicts.append(v)
        lbs = {n: b["pooled_lower_bound"] for n, b in self.doc["contenders"].items()}
        chosen = choose_contender(verdicts, lbs)
        self.assertEqual(self.doc["chosen"], chosen)
        self.assertEqual(self.doc["symbols_to_trade"], list(traded_symbols(verdicts, chosen)))
        # 3 offset runs can never reach the 15 the gate needs
        self.assertTrue(all(not v.passed for v in verdicts))
        self.assertIsNone(self.doc["chosen"])

    def test_holm_is_per_symbol_across_contenders(self):
        for symbol in SYMBOLS:
            raw = {n: b["symbols"][symbol]["p_c0"] for n, b in self.doc["contenders"].items()}
            ordered = sorted(raw.items(), key=lambda kv: (kv[1], kv[0]))
            running = 0.0
            for i, (name, p) in enumerate(ordered):
                running = max(running, min(1.0, (3 - i) * p))
                self.assertAlmostEqual(
                    self.doc["contenders"][name]["symbols"][symbol]["p_holm"], running)

    def test_per_symbol_metrics_come_from_the_joint_run(self):
        markets = holdout_markets(self.dataset, ExecConfig())
        trades = simulate_holdout(contender2_fn(self.dataset.frames), markets, TEST_PERIOD,
                                  CALIB, seed=2, exec_cfg=ExecConfig(), guard_cfg=NO_GUARDS)
        rows = self.doc["contenders"]["c2"]["symbols"]
        self.assertGreater(len(trades), 0)
        for symbol in SYMBOLS:
            mine = [t for t in trades if t.symbol == symbol]
            self.assertEqual(rows[symbol]["n"], len(mine))
            self.assertEqual(rows[symbol]["tp"], sum(t.kind == "TP" for t in mine))
        self.assertEqual(rows[BTC]["evidence"]["wr_offsets"][0], rows[BTC]["wr"])

    def test_c0_runs_each_symbol_on_its_own(self):
        c0 = self.doc["controls"]["c0"]
        for symbol in SYMBOLS:
            self.assertGreater(c0[symbol]["long"]["n"], 0, symbol)
            self.assertGreater(c0[symbol]["short"]["n"], 0, symbol)
            long_wr, short_wr = c0[symbol]["long"]["wr"], c0[symbol]["short"]["wr"]
            self.assertAlmostEqual(c0[symbol]["wr"], (long_wr + short_wr) / 2)
            evidence = self.doc["contenders"]["c2"]["symbols"][symbol]["evidence"]
            self.assertEqual(evidence["c0_long_wr"], long_wr)
            self.assertEqual(evidence["c0_short_gross"], c0[symbol]["short"]["gross_bps"])

    def test_c1_control_reports_a_distribution(self):
        c1 = self.doc["controls"]["c1"]
        self.assertEqual(sorted(c1), ["c1", "c2", "c3"])
        row = c1["c2"][BTC]
        self.assertEqual(row["n_seeds"], 5)
        self.assertEqual(row["n_entries"], self.doc["contenders"]["c2"]["symbols"][BTC]["n"])
        self.assertGreater(row["n_entries"], 0)
        self.assertIsNotNone(row["mean"])
        self.assertLessEqual(row["p5"], row["mean"])
        self.assertLessEqual(row["mean"], row["p95"])

    def test_source_of_the_edge(self):
        for name in ("c1", "c2"):
            source = self.doc["contenders"][name]["symbols"][BTC]["source"]
            self.assertEqual(source["kind"], "permutation")
            self.assertEqual(source["shifts"], 20)
            self.assertTrue(set(source["distinct_k"]) <= {7, 8, 9})
            self.assertEqual(source["distinct_k"], sorted(source["distinct_k"]))
            self.assertTrue(0.0 < source["p"] <= 1.0)
        source = self.doc["contenders"]["c3"]["symbols"][BTC]["source"]
        self.assertEqual(source["kind"], "paired_vs_primary")
        self.assertIn("primary_wr", source)

    def test_c3_enters_only_where_its_primary_signals_and_the_model_agrees(self):
        frames = self.dataset.frames
        rule = parse_rule(RULE)
        model = MetaModel.from_dict(meta_model_dict())
        c3 = contender3_fn(rule, model, frames)
        primary = rule_signal_fn(rule, frames)
        fired = {"c3": 0, "primary": 0}
        for symbol in SYMBOLS:
            for t in range(TEST_PERIOD.start_ms, TEST_PERIOD.end_ms, 7 * MIN):
                side, base = c3(symbol, t), primary(symbol, t)
                fired["c3"] += side != 0
                fired["primary"] += base != 0
                if side:
                    self.assertEqual(side, base)
                    # hour_sin >= 0; sin(pi) is a tiny positive float, so hour 12 enters
                    self.assertLessEqual((t // HOUR + 7) % 24, 12)
        self.assertGreater(fired["c3"], 0)
        self.assertLess(fired["c3"], fired["primary"])
        primary_rows = self.doc["contenders"]["c3"]["primary"]
        self.assertEqual(sorted(primary_rows), sorted(SYMBOLS))

    def test_sensitivities_are_reported(self):
        sens = self.doc["sensitivities"]
        for key in ("eth_first", "cadence", "guards_off", "reference"):
            self.assertIn(key, sens)
        for name in ("c1", "c2", "c3"):
            co = sens["eth_first"][name]["co_signal"]
            self.assertGreater(co["evaluated_ticks"], 0)
            self.assertTrue(0.0 <= co["share"] <= 1.0)
            self.assertIn("combined_supply", sens["guards_off"][name])
            self.assertEqual(sorted(sens["cadence"][name]), sorted(SYMBOLS))
        self.assertEqual(sorted(sens["reference"]), sorted(SYMBOLS))
        row = self.doc["contenders"]["c2"]["symbols"][BTC]
        self.assertIn("both_touch_share", row)
        self.assertAlmostEqual(row["net_per_1m_fee_sensitivity"] - row["net_per_1m"],
                               -50 * 2 * 100 * (0.05 - 0.035))


# Every criterion threshold relaxed so a synthetic contender passes. The C0
# tolerances stay the spec's: the synthetic always-long/short WRs are 47.8-50.9
# and gross -1.5..-0.3 bps, inside 50 +- 3 and +-2 bps.
RELAXED_GATE = replace(GateConfig(), min_tp_sl=1, alpha=1.01, min_wr=0.0, half_min_wr=-1.0,
                       robust_min_wr=0.0, offsets_min_pass=0, min_supply_per_day=0.0)


class RelaxedGateStageTests(unittest.TestCase):
    def test_a_passing_contender_is_chosen_with_its_passing_symbols(self):
        _build_shared()
        out = Path(tempfile.mkdtemp(prefix="pine_holdout_relaxed_"))
        try:
            sel = write_selection(out / "sel.json", DUMMY_SHA)
            path = run_test_stage(_Shared.dataset, sel, CALIB, out / "o", DUMMY_SHA, seed=2,
                                  period=TEST_PERIOD, guard_cfg=NO_GUARDS,
                                  gate_cfg=RELAXED_GATE, **SMALL)
            doc = json.loads(path.read_text())
        finally:
            shutil.rmtree(out, ignore_errors=True)
        chosen = doc["chosen"]
        self.assertIsNotNone(chosen)
        block = doc["contenders"][chosen]
        self.assertTrue(block["symbols_passed"])
        self.assertEqual(doc["symbols_to_trade"], block["symbols_passed"])
        self.assertIsNotNone(block["pooled_lower_bound"])
        verdicts = []
        for name, b in doc["contenders"].items():
            passed = [s for s in SYMBOLS if b["symbols"][s]["verdict"]["passed"]]
            self.assertEqual(passed, b["symbols_passed"])
            for s in SYMBOLS:
                row = b["symbols"][s]
                expected = passed if row["verdict"]["passed"] else [
                    x for x in SYMBOLS if x in set(passed) | {s}]
                self.assertEqual(row["evidence"]["supply_set"], expected)
                self.assertAlmostEqual(row["evidence"]["supply_holdout"],
                                       sum(b["symbols"][x]["supply"] for x in expected))
                verdicts.append(gate_symbol(row["evidence"], RELAXED_GATE))
        lbs = {n: b["pooled_lower_bound"] for n, b in doc["contenders"].items()}
        self.assertEqual(choose_contender(verdicts, lbs), chosen)


class StageSkipTests(unittest.TestCase):
    def test_absent_contenders_are_skipped(self):
        _build_shared()
        out = Path(tempfile.mkdtemp(prefix="pine_holdout_skip_"))
        try:
            sel = write_selection(out / "sel.json", DUMMY_SHA, contender1=None, meta_model=None)
            small = dict(SMALL, permutation_shifts=1)
            path = run_test_stage(_Shared.dataset, sel, CALIB, out / "o", DUMMY_SHA,
                                  period=TEST_PERIOD, guard_cfg=NO_GUARDS, **small)
            doc = json.loads(path.read_text())
            self.assertEqual(sorted(doc["contenders"]), ["c2"])
            self.assertEqual(doc["skipped"], ["c1", "c3"])
            for symbol in SYMBOLS:  # Holm still counts a family of three
                row = doc["contenders"]["c2"]["symbols"][symbol]
                self.assertAlmostEqual(row["p_holm"], min(1.0, 3 * row["p_c0"]))
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_dataset_must_reach_the_holdout_end(self):
        _build_shared()
        out = Path(tempfile.mkdtemp(prefix="pine_holdout_short_"))
        try:
            sel = write_selection(out / "sel.json", DUMMY_SHA)
            short = replace(_Shared.dataset, loaded_until_ms=ms(HOLDOUT_END) - DAY)
            with self.assertRaises(ValueError):
                run_test_stage(short, sel, CALIB, out / "o", DUMMY_SHA, period=TEST_PERIOD,
                               **SMALL)
        finally:
            shutil.rmtree(out, ignore_errors=True)


class HoldoutSimulationTests(StageFixture):
    def test_no_entry_in_the_final_65_minutes_and_none_before_the_start(self):
        markets = holdout_markets(self.dataset, ExecConfig())
        trades = simulate_holdout(lambda s, t: 1, markets, TEST_PERIOD, CALIB, seed=0,
                                  exec_cfg=ExecConfig(), guard_cfg=NO_GUARDS)
        self.assertGreater(len(trades), 100)
        self.assertTrue(all(t.symbol == BTC for t in trades))
        self.assertTrue(all(t.signal_ms >= TEST_PERIOD.start_ms for t in trades))
        self.assertTrue(all(t.signal_ms < TEST_PERIOD.end_ms - 65 * MIN for t in trades))
        self.assertTrue(all(t.exit_ms <= TEST_PERIOD.end_ms for t in trades))


if __name__ == "__main__":
    unittest.main()
