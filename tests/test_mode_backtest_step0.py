"""Tests for research.mode_backtest.step0 and its table: the Pine holdout replayed, checked
field by field against the holdout file, and split by side and by the selector's condition.

The real Pine data and files live outside the repository, so the replay runs on a small
synthetic data directory (23 days at the end of the holdout) loaded with ``start_day``: the
tests patch ``step0.load_dataset`` to return it.
The reference trades are simulated directly with ``simulate_holdout`` and the spec settings,
not through the code under test.
"""

import dataclasses
import hashlib
import json
import math
import random
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bot.strategy.modes.config import config_sha256, load_modes_config
from bot.strategy.modes.regime import NAIK, SIDEWAYS, TIDAK_ENTRY, TURUN
from research.mode_backtest import lock as mode_lock
from research.mode_backtest import report, step0
from research.mode_backtest.data import PairData, load_pair
from research.mode_backtest.frames import HourlyFrame
from research.mode_backtest.step0 import (
    LABEL_FIRST_DAY,
    LABEL_LAST_DAY,
    PINE_FILES,
    SEED,
    ReproductionError,
    Step0Row,
    check_inputs,
    condition_lookup,
    label_lookup,
    replay_main_runs,
    replayed_metrics,
    reproduction_mismatches,
    run_step0,
    split_rows,
)
from research.pine_gate.bars import (
    bars_from_rows,
    day_path,
    funding_path,
    warmup_path,
    wib_day,
    wib_day_start_ms,
    write_day,
)
from research.pine_gate.calibrate import Calibration
from research.pine_gate.dataset import SYMBOLS, Dataset, load_dataset
from research.pine_gate.fileio import write_exclusive
from research.pine_gate.holdout_replay import (
    Holdout,
    HoldoutPeriod,
    build_contenders,
    holdout_markets,
    simulate_holdout,
    symbol_metrics,
    trades_of,
)
from research.pine_gate.lock import latencies_sha256
from research.pine_gate.metalabel import FEATURES, MetaModel
from research.pine_gate.pipeline import purge_tick_minutes
from research.pine_gate.simulate import Trade
from research.pine_gate.spec import ExecConfig, GateConfig, GuardConfig

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "config" / "modes.yaml"
BTC, ETH = SYMBOLS
MINUTE = 60_000
HOUR = 60 * MINUTE
H4 = 4 * HOUR
DAY = 24 * HOUR
PERIOD = HoldoutPeriod.spec()
CALIB = Calibration(s_star_bps=1.0, n_stops=20, latencies_ms=(20_000.0, 30_000.0, 40_000.0))
NOW = datetime(2026, 10, 17, 18, 30, tzinfo=timezone.utc)  # 01:30 WIB on 18 October
STAMP = "20261018"
HEADER = (
    "| Kontender | Koin | Arah | Kondisi | Posisi | Menang | Rata-rata bps | Hasil per 1 juta |"
)


def json_form(value):
    return json.dumps(value, sort_keys=True)


def stub_holdout():
    """A holdout with the spec period, days and configs and no market: enough for
    ``symbol_metrics``, which reads nothing else."""
    exec_cfg = ExecConfig()
    return Holdout(
        frames={}, markets=(), period=PERIOD, calib=CALIB, seed=0, exec_cfg=exec_cfg,
        guard_cfg=GuardConfig(), gate_cfg=GateConfig(), days=tuple(PERIOD.days()),
        last20_days=tuple(PERIOD.last20_days()),
        purge_tick_minutes=purge_tick_minutes(CALIB, exec_cfg),
    )


def make_trade(symbol=BTC, side=1, kind="TP", gross_bps=18.0, net_usdt=27.5, day=0, minute=0,
               both=False):
    """A trade whose signal is at 08:00 WIB + ``minute`` on holdout day ``day``."""
    signal_ms = PERIOD.start_ms + day * DAY + 8 * HOUR + minute * MINUTE
    entry_ms = signal_ms + MINUTE
    return Trade(symbol, side, signal_ms, entry_ms, entry_ms + 20 * MINUTE, kind, gross_bps,
                 net_usdt, wib_day(entry_ms), both)


def sample_trades():
    return {
        "c1": [
            make_trade(BTC, 1, "TP", 18.0, 27.5, day=0),
            make_trade(BTC, -1, "SL", -22.0, -72.5, day=1),
            make_trade(ETH, 1, "TIME", 2.5, -11.25, day=40),
        ],
        "c2": [
            make_trade(ETH, -1, "TP", 18.0, 27.5, day=50, both=True),
            make_trade(BTC, 1, "SL", -21.0, -70.0, day=60),
        ],
        "c3": [],
    }


def metrics_of(trades_by_contender):
    """``symbol_metrics`` per contender and symbol, computed here without the code under test."""
    h = stub_holdout()
    return {
        name: {s: symbol_metrics(trades_of(trades, s), h) for s in SYMBOLS}
        for name, trades in trades_by_contender.items()
    }


def contenders_doc(metrics):
    """The ``contenders`` block of a holdout file: the metrics plus keys step 0 never reads."""
    return {
        name: {
            "rule": None,
            "symbols": {
                s: {**m, "robustness": {"wr_overshoot": 50.0}, "verdict": {"passed": False}}
                for s, m in by_symbol.items()
            },
        }
        for name, by_symbol in metrics.items()
    }


def as_written(doc):
    """``doc`` as it reads back from a JSON file."""
    return json.loads(json.dumps(doc))


# --------------------------------------------------------------------------- Pine fixture

FIRST_DAY = date(2026, 9, 21)  # a Monday inside the holdout
LAST_DAY = date(2026, 10, 13)  # the last holdout day
RULE = "H1|P40|OFF|OFF|OFF"
BASE_PRICE = {BTC: 60_000.0, ETH: 3_000.0}


def walk_rows(rng, start_ms, n, interval_ms, price, step):
    rows = []
    for k in range(n):
        o = price
        c = o * math.exp(rng.gauss(0.0, step))
        hi = max(o, c) * (1 + abs(rng.gauss(0.0, step / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0.0, step / 2)))
        rows.append((start_ms + k * interval_ms, o, hi, lo, c, rng.uniform(1.0, 10.0)))
        price = c
    return rows


def write_pine_data(root):
    """Random-walk day files, warm-ups and funding times for both symbols, laid out as the
    Pine gate stores them; the warm-ups end with the bar holding the minute before FIRST_DAY."""
    first_ms = wib_day_start_ms(FIRST_DAY)
    for i, symbol in enumerate(SYMBOLS):
        rng = random.Random(600 + i)
        price = BASE_PRICE[symbol]
        write_day(warmup_path(root, symbol, "1h"),
                  walk_rows(rng, first_ms - 1000 * HOUR, 1000, HOUR, price * 0.97, 0.003))
        last_4h = first_ms // H4 * H4
        write_day(warmup_path(root, symbol, "4h"),
                  walk_rows(rng, last_4h - 499 * H4, 500, H4, price * 0.95, 0.006))
        day = FIRST_DAY
        while day <= LAST_DAY:
            rows = walk_rows(rng, wib_day_start_ms(day), 1440, MINUTE, price, 0.0004)
            write_day(day_path(root, "prod", symbol, "1m", day), rows)
            price = rows[-1][4]
            day += timedelta(days=1)
        end_ms = wib_day_start_ms(LAST_DAY + timedelta(days=1))
        times = list(range(first_ms // (8 * HOUR) * (8 * HOUR), end_ms + 2 * DAY, 8 * HOUR))
        path = funding_path(root, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(times), encoding="utf-8")


def selection_doc(**override):
    coef = [0.0] * len(FEATURES)
    coef[FEATURES.index("hour_sin")] = 2.0  # c3 keeps WIB hours 0-11
    model = MetaModel(coef=tuple(coef), intercept=0.0, means=(0.0,) * len(FEATURES),
                      stds=(1.0,) * len(FEATURES), threshold=0.5, C=0.1)
    doc = {"contender1": RULE, "primary_rule": RULE, "meta_model": model.to_dict(), "seed": 0,
           "s_star_bps": CALIB.s_star_bps, "n_latencies": len(CALIB.latencies_ms),
           "latencies_sha256": latencies_sha256(CALIB.latencies_ms)}
    doc.update(override)
    return doc


def calibration_doc(calib=CALIB):
    return {"s_star_bps": calib.s_star_bps, "n_stops": calib.n_stops,
            "latencies_ms": list(calib.latencies_ms),
            "latencies_sha256": latencies_sha256(calib.latencies_ms)}


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def input_record(selection_path, data_sha):
    """The holdout file's fields that ``check_inputs`` compares, as the test stage writes them."""
    return {
        "selection_sha256": hashlib.sha256(selection_path.read_bytes()).hexdigest(),
        "data_sha256": data_sha,
        "seed": 0,
        "configs": {"exec": asdict(ExecConfig()), "guard": asdict(GuardConfig()),
                    "gate": asdict(GateConfig())},
        "period": PERIOD.to_dict(),
        "calibration": {"s_star_bps": CALIB.s_star_bps, "n_stops": CALIB.n_stops,
                        "n_latencies": len(CALIB.latencies_ms)},
        "code_sha256": "c" * 64,
    }


def write_pine_dir(pine_dir, data_sha, contenders, selection=None, calibration=None, **override):
    """The three Pine Phase 1 files; returns the holdout document as written."""
    write_json(pine_dir / PINE_FILES["selection"], selection or selection_doc())
    write_json(pine_dir / PINE_FILES["calibration"], calibration or calibration_doc())
    doc = {**input_record(pine_dir / PINE_FILES["selection"], data_sha),
           "contenders": contenders, **override}
    write_json(pine_dir / PINE_FILES["holdout"], doc)
    return as_written(doc)


def reference_trades(dataset):
    """Each contender's main run simulated directly with the spec settings and seed 0."""
    contenders, _skipped = build_contenders(selection_doc(), dataset.frames, PERIOD)
    exec_cfg = ExecConfig()
    return {
        c.name: simulate_holdout(c.signal, holdout_markets(dataset, exec_cfg), PERIOD, CALIB,
                                 seed=0, exec_cfg=exec_cfg, guard_cfg=GuardConfig())
        for c in contenders
    }


class _Shared:
    built = False


def shared():
    if not _Shared.built:
        tmp = Path(tempfile.mkdtemp(prefix="mode_step0_"))
        try:
            data_dir = tmp / "data"
            write_pine_data(data_dir)
            _Shared.dataset = load_dataset(data_dir, None, start_day=FIRST_DAY)
            _Shared.reference = reference_trades(_Shared.dataset)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)  # no temp dir left behind
            raise
        _Shared.tmp, _Shared.data_dir, _Shared.built = tmp, data_dir, True
    return _Shared


def tearDownModule():
    if _Shared.built:
        shutil.rmtree(_Shared.tmp, ignore_errors=True)


@contextmanager
def small_data():
    """``load_dataset`` returns the shared dataset and records what it was asked for."""
    calls = []

    def fake_load_dataset(data_dir, until_ms, **kwargs):
        calls.append(("load_dataset", Path(data_dir), until_ms, kwargs))
        return shared().dataset

    with mock.patch.object(step0, "load_dataset", side_effect=fake_load_dataset):
        yield calls


def files_in(path):
    return sorted(p.name for p in path.iterdir()) if path.exists() else []


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        self.pine = self.root / "pine"
        self.out = self.root / "out"


# --------------------------------------------------------------------------- reproduction


class ReproductionTests(unittest.TestCase):
    def setUp(self):
        self.replayed = metrics_of(sample_trades())
        self.doc = as_written({"contenders": contenders_doc(self.replayed)})

    def stored(self, name, symbol):
        return self.doc["contenders"][name]["symbols"][symbol]

    def test_identical_metrics_have_no_mismatch(self):
        self.assertEqual(reproduction_mismatches(self.replayed, self.doc), [])

    def test_one_changed_field_is_reported(self):
        replayed_wr = self.replayed["c1"][BTC]["wr"]
        self.stored("c1", BTC)["wr"] = 48.2

        self.assertEqual(reproduction_mismatches(self.replayed, self.doc),
                         [f"c1 BTC-USDT wr: {json_form(replayed_wr)} != 48.2"])

    def test_every_symbol_metrics_field_is_compared(self):
        keys = list(self.replayed["c1"][BTC])
        self.assertIn("halves", keys)
        self.assertIn("wr_upper_both_as_wins", keys)
        for key in keys:
            with self.subTest(key=key):
                doc = as_written(self.doc)
                value = doc["contenders"]["c1"]["symbols"][BTC][key]
                changed = [value, 1] if isinstance(value, list) else "changed"
                doc["contenders"]["c1"]["symbols"][BTC][key] = changed

                found = reproduction_mismatches(self.replayed, doc)

                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].startswith(f"c1 BTC-USDT {key}: "), found)

    def test_a_number_of_another_type_or_none_is_a_difference(self):
        self.assertIsInstance(self.replayed["c1"][BTC]["n"], int)
        self.stored("c1", BTC)["n"] = float(self.replayed["c1"][BTC]["n"])
        self.assertIsNone(self.replayed["c3"][ETH]["wr"])
        self.stored("c3", ETH)["wr"] = 0

        self.assertEqual(reproduction_mismatches(self.replayed, self.doc),
                         ["c1 BTC-USDT n: 2 != 2.0", "c3 ETH-USDT wr: null != 0"])

    def test_a_tiny_float_difference_is_a_difference(self):
        gross = self.replayed["c2"][ETH]["gross_bps"]
        self.stored("c2", ETH)["gross_bps"] = math.nextafter(gross, math.inf)

        self.assertEqual(len(reproduction_mismatches(self.replayed, self.doc)), 1)

    def test_keys_the_metrics_do_not_have_are_ignored(self):
        self.stored("c1", ETH)["verdict"] = {"passed": True}
        self.stored("c1", ETH)["new_key"] = 1

        self.assertEqual(reproduction_mismatches(self.replayed, self.doc), [])

    def test_a_missing_field_symbol_or_contender_is_a_difference(self):
        del self.stored("c1", BTC)["supply"]
        del self.doc["contenders"]["c2"]["symbols"][ETH]
        del self.doc["contenders"]["c3"]

        found = reproduction_mismatches(self.replayed, self.doc)

        self.assertEqual(found, [
            f"c1 BTC-USDT supply: {json_form(self.replayed['c1'][BTC]['supply'])} != (missing)",
            "c2 ETH-USDT: replayed, but not in the holdout file",
            "c3: replayed, but not in the holdout file",
        ])

    def test_a_contender_or_symbol_that_was_not_replayed_is_a_difference(self):
        del self.replayed["c3"]
        del self.replayed["c2"][BTC]

        self.assertEqual(reproduction_mismatches(self.replayed, self.doc), [
            "c2 BTC-USDT: in the holdout file, but not replayed",
            "c3: in the holdout file, but not replayed",
        ])

    def test_a_holdout_file_without_contenders_or_symbols_is_a_difference(self):
        self.assertEqual(reproduction_mismatches(self.replayed, {}),
                         ["the holdout file has no contenders block"])
        self.doc["contenders"]["c2"] = {"rule": None}
        found = reproduction_mismatches(self.replayed, self.doc)
        self.assertEqual(found, ["c2: the holdout file has no symbols block"])

    def test_nothing_to_compare_never_passes(self):
        nothing = ["nothing was compared: the replay and the holdout file share no contender "
                   "and symbol"]
        self.assertEqual(reproduction_mismatches({}, {"contenders": {}}), nothing)
        empty_c1 = {"contenders": {"c1": {"symbols": {}}}}
        self.assertEqual(reproduction_mismatches({"c1": {}}, empty_c1), nothing)

    def test_a_symbol_without_metrics_is_a_difference(self):
        self.doc["contenders"]["c1"]["symbols"][ETH] = None

        self.assertEqual(reproduction_mismatches(self.replayed, self.doc),
                         ["c1 ETH-USDT: the holdout file holds no metrics"])

    def test_replayed_metrics_are_symbol_metrics_per_symbol(self):
        trades = sample_trades()
        h = stub_holdout()

        got = replayed_metrics(trades, h)

        self.assertEqual(list(got), ["c1", "c2", "c3"])
        for name in got:
            self.assertEqual(list(got[name]), list(SYMBOLS))
            for symbol in SYMBOLS:
                self.assertEqual(got[name][symbol],
                                 symbol_metrics(trades_of(trades[name], symbol), h))


# --------------------------------------------------------------------------- inputs


class CheckInputsTests(TempDirTestCase):
    DATA_SHA = "d" * 64

    def setUp(self):
        super().setUp()
        self.doc = write_pine_dir(self.pine, self.DATA_SHA, {})
        self.dataset = Dataset(frames={}, markets={}, data_sha256=self.DATA_SHA, loaded_until_ms=0)

    def problems(self, doc=None, dataset=None):
        with self.assertRaises(ReproductionError) as caught:
            check_inputs(self.pine, doc or self.doc, dataset or self.dataset)
        return caught.exception.problems

    def test_matching_inputs_pass(self):
        self.assertIsNone(check_inputs(self.pine, self.doc, self.dataset))

    def test_check_inputs_refuses_a_different_selection_file(self):
        write_json(self.pine / PINE_FILES["selection"],
                   selection_doc(contender1="H4|P40|ADX|OFF|OFF"))

        problems = self.problems()

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("selection", problems[0])
        self.assertIn(self.doc["selection_sha256"], problems[0])

    def test_check_inputs_refuses_configs_other_than_the_spec_defaults(self):
        for name, field, value in (("exec", "bracket_pct", 0.25), ("guard", "max_entries", 35),
                                   ("gate", "offsets", 3)):
            with self.subTest(config=name):
                doc = as_written(self.doc)
                doc["configs"][name][field] = value

                problems = self.problems(doc)

                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f"configs.{name}", problems[0])

    def test_a_missing_configs_block_is_refused(self):
        doc = as_written(self.doc)
        del doc["configs"]

        self.assertEqual(len(self.problems(doc)), 3)

    def test_check_inputs_refuses_other_data(self):
        other = Dataset(frames={}, markets={}, data_sha256="e" * 64, loaded_until_ms=0)

        problems = self.problems(dataset=other)

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("data_sha256", problems[0])
        self.assertIn("e" * 64, problems[0])

    def test_check_inputs_refuses_another_seed_period_or_calibration(self):
        changes = {
            "seed": 2,
            "period": {**self.doc["period"], "start_ms": self.doc["period"]["start_ms"] + 1},
            "calibration": {**self.doc["calibration"], "n_latencies": 4},
        }
        for key, value in changes.items():
            with self.subTest(key=key):
                doc = {**self.doc, key: value}

                problems = self.problems(doc)

                self.assertEqual(len(problems), 1, problems)
                self.assertIn(key, problems[0])

    def test_a_latency_sample_other_than_the_selections_is_refused(self):
        other = Calibration(1.0, 20, (20_000.0, 30_000.0, 41_000.0))
        write_json(self.pine / PINE_FILES["calibration"], calibration_doc(other))

        problems = self.problems()

        self.assertEqual(len(problems), 1, problems)
        self.assertIn("latency", problems[0])

    def test_every_problem_is_reported_at_once(self):
        doc = {**self.doc, "seed": 1, "selection_sha256": "f" * 64}
        other = Dataset(frames={}, markets={}, data_sha256="e" * 64, loaded_until_ms=0)

        with self.assertRaises(ReproductionError) as caught:
            check_inputs(self.pine, doc, other)

        self.assertEqual(len(caught.exception.problems), 3)
        for problem in caught.exception.problems:
            self.assertIn(problem, str(caught.exception))


# --------------------------------------------------------------------------- the replay


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = shared()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.pine = Path(tmp.name) / "pine"
        write_pine_dir(cls.pine, cls.s.dataset.data_sha256, {})
        with small_data() as calls:
            cls.trades, cls.h = replay_main_runs(cls.s.data_dir, cls.pine)
        cls.calls = calls

    def test_the_whole_dataset_is_loaded_as_the_test_stage_loads_it(self):
        self.assertEqual(self.calls, [("load_dataset", self.s.data_dir, None, {})])

    def test_the_holdout_has_the_spec_settings_and_seed_0(self):
        h = self.h
        self.assertEqual(SEED, 0)
        self.assertEqual(h.seed, 0)
        self.assertEqual(h.period, HoldoutPeriod.spec())
        self.assertEqual((h.exec_cfg, h.guard_cfg, h.gate_cfg),
                         (ExecConfig(), GuardConfig(), GateConfig()))
        self.assertEqual(h.calib, CALIB)
        self.assertEqual(h.days, tuple(PERIOD.days()))
        self.assertEqual(h.last20_days, tuple(PERIOD.last20_days()))
        self.assertEqual(h.purge_tick_minutes, purge_tick_minutes(CALIB, ExecConfig()))
        self.assertEqual([m.symbol for m in h.markets], list(SYMBOLS))
        self.assertIs(h.frames, self.s.dataset.frames)

    def test_every_contender_is_replayed_as_a_direct_simulation(self):
        self.assertEqual(list(self.trades), ["c1", "c2", "c3"])
        for name, expected in self.s.reference.items():
            with self.subTest(contender=name):
                self.assertGreater(len(expected), 0)  # the fixture trades every contender
                self.assertEqual(self.trades[name], expected)

    def test_inputs_that_differ_stop_the_replay_before_any_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            pine = Path(tmp) / "pine"
            write_pine_dir(pine, "0" * 64, {})
            with small_data(), \
                    mock.patch.object(Holdout, "run", side_effect=AssertionError("ran")):
                with self.assertRaises(ReproductionError) as caught:
                    replay_main_runs(self.s.data_dir, pine)
        self.assertIn("data_sha256", str(caught.exception))


# --------------------------------------------------------------------------- run_step0


class RunStep0Tests(unittest.TestCase):
    """One full step 0 on the synthetic data, checked against the reference trades."""

    @classmethod
    def setUpClass(cls):
        cls.s = shared()
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        root = Path(tmp.name)
        cls.pine, cls.out = root / "pine", root / "out"
        cls.holdout_doc = write_pine_dir(cls.pine, cls.s.dataset.data_sha256,
                                         contenders_doc(metrics_of(cls.s.reference)))
        with small_data() as calls:
            cls.path = run_step0(cls.s.data_dir, cls.pine, CONFIG, cls.out, NOW)
        cls.calls = calls
        cls.doc = json.loads(cls.path.read_text(encoding="utf-8"))
        cls.markdown = (cls.out / f"step0_{STAMP}.md").read_text(encoding="utf-8")
        cls.rows = [Step0Row(**row) for row in cls.doc["rows"]]

    def test_writes_the_json_and_the_markdown_named_by_the_wib_day(self):
        self.assertEqual(self.path, self.out / f"step0_{STAMP}.json")
        self.assertEqual(files_in(self.out), [f"step0_{STAMP}.json", f"step0_{STAMP}.md"])

    def test_labels_come_from_the_dataset_the_replay_loaded(self):
        # The day files are read once: the labels use the replay's own 1h and 4h series.
        self.assertEqual(LABEL_FIRST_DAY, date(2025, 8, 1))
        self.assertEqual(LABEL_LAST_DAY, date(2026, 10, 13))
        self.assertEqual([call[0] for call in self.calls], ["load_dataset"])

    def test_rows_count_every_trade_once_by_side_and_condition(self):
        cfg = load_modes_config(CONFIG)
        frames = {s: HourlyFrame.build(load_pair(self.s.data_dir, s, FIRST_DAY, LAST_DAY),
                                       cfg.pemilih_mode) for s in SYMBOLS}
        expected = Counter()
        for name, trades in self.s.reference.items():
            for t in trades:
                kondisi = frames[t.symbol].context_at(t.signal_ms).kondisi
                expected[(name, t.symbol, "LONG" if t.side == 1 else "SHORT", kondisi)] += 1

        got = Counter({(r.contender, r.symbol, r.sisi, r.kondisi): r.posisi for r in self.rows})

        self.assertEqual(got, expected)
        self.assertEqual(len(got), len(self.rows))  # one row per group
        self.assertGreater(len({r.kondisi for r in self.rows}), 1)
        self.assertEqual({r.sisi for r in self.rows}, {"LONG", "SHORT"})

    def test_the_rows_are_split_rows_of_the_replayed_trades(self):
        cfg = load_modes_config(CONFIG)
        pairs = [load_pair(self.s.data_dir, s, FIRST_DAY, LAST_DAY) for s in SYMBOLS]

        expected = split_rows(self.s.reference, condition_lookup(pairs, cfg.pemilih_mode))

        self.assertEqual(self.rows, expected)

    def test_the_json_records_inputs_reproduction_and_caveat(self):
        doc = self.doc
        inputs = doc["inputs"]
        self.assertEqual(inputs["holdout_file"], PINE_FILES["holdout"])
        for kind in ("holdout", "selection", "calibration"):
            raw = (self.pine / PINE_FILES[kind]).read_bytes()
            self.assertEqual(inputs[f"{kind}_sha256"], hashlib.sha256(raw).hexdigest(), kind)
        self.assertEqual(inputs["selection_sha256"], self.holdout_doc["selection_sha256"])
        self.assertEqual(inputs["data_sha256"], self.s.dataset.data_sha256)
        self.assertEqual(inputs["config_sha256"], config_sha256(CONFIG))
        self.assertEqual(inputs["holdout_code_sha256"], "c" * 64)
        # The mode lock's fingerprint: it covers regime.py and config.py, which make the labels.
        self.assertEqual(inputs["code_sha256"], mode_lock.code_sha256())
        self.assertEqual(inputs["python_version"], f"{sys.version_info[0]}.{sys.version_info[1]}")
        self.assertEqual(doc["seed"], 0)
        self.assertEqual(doc["created_at"], NOW.isoformat())
        self.assertEqual(doc["label_days"], ["2025-08-01", "2026-10-13"])
        self.assertEqual(doc["reproduction"]["contenders"], ["c1", "c2", "c3"])
        self.assertEqual(doc["reproduction"]["symbols"], list(SYMBOLS))
        self.assertEqual(doc["reproduction"]["fields"],
                         sorted(symbol_metrics([], stub_holdout())))
        self.assertEqual(doc["reproduction"]["differences"], 0)
        self.assertEqual(doc["exec"], asdict(ExecConfig()))

    def test_the_markdown_holds_the_table_and_the_caveat(self):
        self.assertIn(report.step0_table(self.rows), self.markdown)
        self.assertIn(PINE_FILES["holdout"], self.markdown)
        self.assertIn("TP 0,20% dan SL 0,20%", self.markdown)
        self.assertIn("60 menit", self.markdown)
        self.assertIn("2026-08-05 sampai 2026-10-13", self.markdown)
        self.assertIn("seed 0", self.markdown)
        self.assertIn("25.000 USDT", self.markdown)

    def test_the_label_lookup_labels_as_the_day_files_do_without_reading_them(self):
        selector = load_modes_config(CONFIG).pemilih_mode
        pairs = [load_pair(self.s.data_dir, s, FIRST_DAY, LAST_DAY) for s in SYMBOLS]
        from_days = condition_lookup(pairs, selector)

        with small_data() as calls:
            kondisi_at = label_lookup(self.s.dataset.frames, selector)

        self.assertEqual(calls, [])
        trades = [t for ts in self.s.reference.values() for t in ts]
        self.assertGreater(len(trades), 20)
        for t in trades:
            self.assertEqual(kondisi_at(t.symbol, t.signal_ms), from_days(t.symbol, t.signal_ms))
        self.assertGreater(len({kondisi_at(t.symbol, t.signal_ms) for t in trades}), 1)
        first = self.s.dataset.frames[BTC].bars_1m.open_ms[0]
        with self.assertRaisesRegex(ValueError, "outside the loaded 1m bars"):
            kondisi_at(BTC, first - 1)

    def test_a_second_run_never_overwrites(self):
        with mock.patch.object(step0, "replay_main_runs") as replay:
            with self.assertRaises(FileExistsError):
                run_step0(self.s.data_dir, self.pine, CONFIG, self.out, NOW)
        replay.assert_not_called()


class RunStep0RefusalTests(TempDirTestCase):
    def write_sample_pine_dir(self, change=None):
        """The three Pine files whose holdout metrics are those of ``sample_trades``,
        with ``change(doc)`` applied to the holdout document."""
        doc = contenders_doc(metrics_of(sample_trades()))
        doc = as_written(doc)
        if change is not None:
            change(doc)
        write_pine_dir(self.pine, "d" * 64, doc)

    @contextmanager
    def sample_replay(self):
        with mock.patch.object(step0, "replay_main_runs",
                               return_value=(sample_trades(), stub_holdout())) as replay, \
                mock.patch.object(step0, "label_lookup",
                                  side_effect=AssertionError("labelled")) as labels:
            yield replay, labels

    def test_the_sample_reproduces(self):
        """The control for the next test: unchanged, the sample replay passes the check."""
        self.write_sample_pine_dir()
        with self.sample_replay(), \
                mock.patch.object(step0, "label_lookup",
                                  return_value=lambda symbol, t: NAIK) as labels:
            path = run_step0(self.data, self.pine, CONFIG, self.out, NOW)
        self.assertEqual(files_in(self.out), [path.name, f"step0_{STAMP}.md"])
        labels.assert_called_once_with({}, load_modes_config(CONFIG).pemilih_mode)  # its frames

    def test_one_changed_field_is_reported_and_stops_step0(self):
        replayed = metrics_of(sample_trades())["c2"][ETH]["gross_bps"]

        def change(doc):
            doc["c2"]["symbols"][ETH]["gross_bps"] = replayed + 0.5

        self.write_sample_pine_dir(change)
        with self.sample_replay() as (replay, labels):
            with self.assertRaises(ReproductionError) as caught:
                run_step0(self.data, self.pine, CONFIG, self.out, NOW)

        self.assertEqual(caught.exception.problems, (
            f"c2 ETH-USDT gross_bps: {json_form(replayed)} != {json_form(replayed + 0.5)}",
        ))
        self.assertIn("c2 ETH-USDT gross_bps", str(caught.exception))
        replay.assert_called_once_with(self.data, self.pine)
        labels.assert_not_called()
        self.assertEqual(files_in(self.out), [])

    def test_an_existing_output_refuses_before_the_replay(self):
        self.write_sample_pine_dir()
        for suffix in (".json", ".md"):
            with self.subTest(suffix=suffix):
                existing = self.out / f"step0_{STAMP}{suffix}"
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_text("earlier run\n", encoding="utf-8")
                with self.sample_replay() as (replay, _labels):
                    with self.assertRaises(FileExistsError):
                        run_step0(self.data, self.pine, CONFIG, self.out, NOW)
                replay.assert_not_called()
                self.assertEqual(existing.read_text(encoding="utf-8"), "earlier run\n")
                existing.unlink()

    def test_a_missing_pine_file_is_named_before_the_replay(self):
        self.write_sample_pine_dir()
        (self.pine / PINE_FILES["calibration"]).unlink()
        (self.pine / PINE_FILES["holdout"]).unlink()
        with self.sample_replay() as (replay, _labels):
            with self.assertRaises(FileNotFoundError) as caught:
                run_step0(self.data, self.pine, CONFIG, self.out, NOW)
        replay.assert_not_called()
        message = str(caught.exception)
        self.assertIn(PINE_FILES["calibration"], message)
        self.assertIn(PINE_FILES["holdout"], message)
        self.assertNotIn(PINE_FILES["selection"], message)

    def test_a_holdout_file_that_is_not_an_object_refuses_before_the_replay(self):
        self.write_sample_pine_dir()
        (self.pine / PINE_FILES["holdout"]).write_text("[]", encoding="utf-8")
        with self.sample_replay() as (replay, _labels):
            with self.assertRaisesRegex(ValueError, "expected a JSON object"):
                run_step0(self.data, self.pine, CONFIG, self.out, NOW)
        replay.assert_not_called()

    def test_a_broken_config_refuses_before_the_replay(self):
        self.write_sample_pine_dir()
        broken = self.root / "modes.yaml"
        broken.write_text(CONFIG.read_text(encoding="utf-8").replace("ema_4h: 50", "ema_4h: 0"),
                          encoding="utf-8")
        with self.sample_replay() as (replay, _labels):
            with self.assertRaises(ValueError):
                run_step0(self.data, self.pine, broken, self.out, NOW)
        replay.assert_not_called()

    def test_a_clock_without_a_zone_is_refused(self):
        self.write_sample_pine_dir()
        with self.sample_replay() as (replay, _labels):
            with self.assertRaises(ValueError):
                run_step0(self.data, self.pine, CONFIG, self.out, datetime(2026, 10, 17, 18, 30))
        replay.assert_not_called()

    def test_the_file_date_is_the_wib_day_of_the_clock(self):
        self.write_sample_pine_dir()
        late_utc = datetime(2026, 10, 17, 16, 59, tzinfo=timezone.utc)  # 23:59 WIB
        with self.sample_replay(), \
                mock.patch.object(step0, "label_lookup", return_value=lambda symbol, t: NAIK):
            path = run_step0(self.data, self.pine, CONFIG, self.out, late_utc)
        self.assertEqual(path.name, "step0_20261017.json")

    def test_a_missing_or_unused_warm_up_stops_step0_before_the_replay(self):
        # A spec run had every 1h/4h warm-up, and the labels read the series they start.
        s = shared()
        write_pine_dir(self.pine, s.dataset.data_sha256, contenders_doc(metrics_of(s.reference)))
        broken = dataclasses.replace(s.dataset, missing_warmups=("BTC-USDT 4h (unused)",))
        with mock.patch.object(step0, "load_dataset", return_value=broken), \
                mock.patch.object(step0, "build_contenders") as contenders:
            with self.assertRaisesRegex(ValueError, "warm-up.*BTC-USDT 4h"):
                run_step0(s.data_dir, self.pine, CONFIG, self.out, NOW)
        contenders.assert_not_called()
        self.assertEqual(files_in(self.out), [])

    def test_inputs_that_differ_write_nothing(self):
        s = shared()
        write_pine_dir(self.pine, "0" * 64, contenders_doc(metrics_of(s.reference)))
        with small_data():
            with self.assertRaises(ReproductionError):
                run_step0(s.data_dir, self.pine, CONFIG, self.out, NOW)
        self.assertEqual(files_in(self.out), [])

    def test_a_failed_second_write_leaves_no_first_file(self):
        self.write_sample_pine_dir()
        written = []

        def second_write_fails(path, data, label="output"):
            written.append(Path(path).name)
            if len(written) == 2:
                raise OSError("disk full")
            write_exclusive(path, data, label)

        with self.sample_replay(), \
                mock.patch.object(step0, "label_lookup", return_value=lambda symbol, t: NAIK), \
                mock.patch.object(step0, "write_exclusive", side_effect=second_write_fails):
            with self.assertRaisesRegex(OSError, "disk full"):
                run_step0(self.data, self.pine, CONFIG, self.out, NOW)
        self.assertEqual(sorted(written), [f"step0_{STAMP}.json", f"step0_{STAMP}.md"])
        self.assertEqual(files_in(self.out), [])


# --------------------------------------------------------------------------- the split


class SplitTests(unittest.TestCase):
    def test_split_rows_groups_by_side_and_condition(self):
        trades = {
            "c1": [
                make_trade(ETH, 1, "TIME", 2.0, -12.5, minute=9),
                make_trade(BTC, -1, "TP", 18.0, 27.5, minute=8),
                make_trade(BTC, 1, "SL", -22.0, -72.5, minute=2),
                make_trade(BTC, 1, "TP", 18.0, 27.5, minute=1),
                make_trade(BTC, 1, "TIME", 2.0, -12.5, minute=3),
                make_trade(BTC, 1, "TP", 18.0, 27.5, minute=4),
            ],
            "c2": [make_trade(BTC, 1, "SL", -22.0, -72.5, minute=5)],
        }
        kondisi = {1: NAIK, 2: NAIK, 3: NAIK, 4: TURUN, 5: TIDAK_ENTRY, 8: TURUN, 9: SIDEWAYS}

        def kondisi_at(symbol, t_ms):
            return kondisi[(t_ms - PERIOD.start_ms - 8 * HOUR) // MINUTE]

        rows = split_rows(trades, kondisi_at)

        self.assertEqual([(r.contender, r.symbol, r.sisi, r.kondisi, r.posisi) for r in rows], [
            ("c1", BTC, "LONG", NAIK, 3),
            ("c1", BTC, "LONG", TURUN, 1),
            ("c1", BTC, "SHORT", TURUN, 1),
            ("c1", ETH, "LONG", SIDEWAYS, 1),
            ("c2", BTC, "LONG", TIDAK_ENTRY, 1),
        ])
        naik = rows[0]
        self.assertEqual(naik.menang_pct, 50.0)
        self.assertAlmostEqual(naik.gross_bps, (18.0 - 22.0 + 2.0) / 3)
        self.assertAlmostEqual(naik.hasil_per_1_juta, 1e6 * (27.5 - 72.5 - 12.5) / (3 * 2 * 25_000))
        self.assertEqual(rows[1].menang_pct, 100.0)
        self.assertAlmostEqual(rows[1].hasil_per_1_juta, 550.0)
        self.assertEqual(rows[4].menang_pct, 0.0)
        self.assertAlmostEqual(rows[4].hasil_per_1_juta, -1450.0)

    def test_menang_leaves_time_exits_out_and_is_none_without_tp_or_sl(self):
        trades = {"c1": [make_trade(kind="TIME", minute=1), make_trade(kind="TIME", minute=2)],
                  "c2": [make_trade(kind="TP", minute=1), make_trade(kind="TIME", minute=2),
                         make_trade(kind="SL", minute=3), make_trade(kind="SL", minute=4)]}

        rows = split_rows(trades, lambda symbol, t: NAIK)

        self.assertIsNone(rows[0].menang_pct)
        self.assertEqual(rows[0].posisi, 2)
        self.assertAlmostEqual(rows[1].menang_pct, 100.0 / 3)

    def test_hasil_per_1_juta_uses_the_exec_notional(self):
        trades = {"c1": [make_trade(net_usdt=10.0, minute=1), make_trade(net_usdt=-40.0, minute=2)]}

        default = split_rows(trades, lambda symbol, t: NAIK)
        other = split_rows(trades, lambda symbol, t: NAIK, notional_usdt=10_000.0)

        self.assertEqual(ExecConfig().notional_usdt, 25_000.0)
        self.assertAlmostEqual(default[0].hasil_per_1_juta, -300.0)
        self.assertAlmostEqual(other[0].hasil_per_1_juta, -750.0)

    def test_conditions_follow_the_table_order_within_a_side(self):
        by_minute = {1: TIDAK_ENTRY, 2: SIDEWAYS, 3: TURUN, 4: NAIK}
        trades = {"c1": [make_trade(minute=m) for m in by_minute]}

        def kondisi_at(symbol, t_ms):
            return by_minute[(t_ms - PERIOD.start_ms - 8 * HOUR) // MINUTE]

        rows = split_rows(trades, kondisi_at)

        self.assertEqual([r.kondisi for r in rows], [NAIK, TURUN, SIDEWAYS, TIDAK_ENTRY])

    def test_the_condition_is_read_at_each_signal_time_of_its_symbol(self):
        asked = []
        trades = {"c1": [make_trade(ETH, minute=7), make_trade(BTC, -1, minute=3)]}

        split_rows(trades, lambda symbol, t: asked.append((symbol, t)) or TURUN)

        self.assertEqual(asked, [(ETH, trades["c1"][0].signal_ms),
                                 (BTC, trades["c1"][1].signal_ms)])

    def test_no_trades_give_no_rows(self):
        self.assertEqual(split_rows({"c1": [], "c2": []}, lambda symbol, t: NAIK), [])

    def test_an_unknown_side_condition_or_symbol_raises_naming_it(self):
        cases = {
            "side 0 ": ({"c1": [make_trade(side=0)]}, lambda symbol, t: NAIK),
            "condition 'NAIK '": ({"c1": [make_trade()]}, lambda symbol, t: "NAIK "),
            "SOL-USDT trade": ({"c1": [make_trade(symbol="SOL-USDT")]}, lambda symbol, t: NAIK),
        }
        for expected, (trades, kondisi_at) in cases.items():
            with self.subTest(case=expected):
                with self.assertRaisesRegex(ValueError, expected):
                    split_rows(trades, kondisi_at)

    def test_a_notional_that_is_not_positive_raises(self):
        for notional in (0.0, -25_000.0, math.inf, math.nan):
            with self.subTest(notional=notional):
                with self.assertRaises(ValueError):
                    split_rows({"c1": [make_trade()]}, lambda symbol, t: NAIK,
                               notional_usdt=notional)


class ConditionLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = shared()
        cls.selector = load_modes_config(CONFIG).pemilih_mode
        cls.pairs = [load_pair(cls.s.data_dir, s, FIRST_DAY, LAST_DAY) for s in SYMBOLS]
        cls.kondisi_at = staticmethod(condition_lookup(cls.pairs, cls.selector))

    def test_kondisi_is_the_hourly_frame_condition_at_the_time(self):
        start = wib_day_start_ms(FIRST_DAY)
        for pair in self.pairs:
            frame = HourlyFrame.build(pair, self.selector)
            for t in range(start, wib_day_start_ms(LAST_DAY + timedelta(days=1)), 37 * MINUTE):
                self.assertEqual(self.kondisi_at(pair.symbol, t), frame.context_at(t).kondisi)

    def test_a_time_outside_the_loaded_days_raises(self):
        start = wib_day_start_ms(FIRST_DAY)
        end = wib_day_start_ms(LAST_DAY + timedelta(days=1))
        self.assertIn(self.kondisi_at(BTC, start), (NAIK, TURUN, SIDEWAYS, TIDAK_ENTRY))
        self.assertIn(self.kondisi_at(BTC, end - 1), (NAIK, TURUN, SIDEWAYS, TIDAK_ENTRY))
        for t in (start - 1, end, end + 30 * DAY):
            with self.subTest(t=t):
                with self.assertRaises(ValueError):
                    self.kondisi_at(BTC, t)

    def test_an_unknown_symbol_raises(self):
        with self.assertRaises(ValueError):
            self.kondisi_at("SOL-USDT", wib_day_start_ms(FIRST_DAY) + DAY)

    def test_a_pair_given_twice_or_without_bars_raises(self):
        with self.assertRaisesRegex(ValueError, "given twice"):
            condition_lookup([self.pairs[0], self.pairs[0]], self.selector)
        first = self.pairs[0]
        empty = PairData(first.symbol, bars_from_rows([], MINUTE), first.warmup_1h,
                         first.warmup_4h)
        with self.assertRaisesRegex(ValueError, "no 1m bars"):
            condition_lookup([empty], self.selector)


# --------------------------------------------------------------------------- the table


class Step0TableTests(unittest.TestCase):
    def row(self, **override):
        values = dict(contender="c1", symbol=BTC, sisi="LONG", kondisi=NAIK, posisi=12,
                      menang_pct=48.148148, gross_bps=-3.254, hasil_per_1_juta=-398.6)
        values.update(override)
        return Step0Row(**values)

    def test_the_header_is_the_brief_one(self):
        lines = report.step0_table([]).splitlines()
        self.assertEqual(lines, [HEADER, "|---|---|---|---|---|---|---|---|"])

    def test_numbers_use_the_decimal_comma(self):
        lines = report.step0_table([self.row(), self.row(symbol=ETH, sisi="SHORT")]).splitlines()

        self.assertEqual(lines[2], "| c1 | BTC | LONG | NAIK | 12 | 48,1% | -3,25 | -399 |")
        self.assertEqual(lines[3], "| c1 | ETH | SHORT | NAIK | 12 | 48,1% | -3,25 | -399 |")
        self.assertEqual(len(lines), 4)

    def test_menang_without_tp_or_sl_is_a_dash(self):
        line = report.step0_table([self.row(menang_pct=None)]).splitlines()[2]
        self.assertEqual(line.split(" | ")[5], "–")

    def test_thousands_and_negative_zero(self):
        line = report.step0_table([self.row(posisi=1234, gross_bps=-0.001,
                                            hasil_per_1_juta=-1450.4)]).splitlines()[2]
        self.assertEqual(line, "| c1 | BTC | LONG | NAIK | 1.234 | 48,1% | 0,00 | -1.450 |")

    def test_number_helpers(self):
        self.assertEqual(report.format_number(1234567.891, 2), "1.234.567,89")
        self.assertEqual(report.format_number(-0.4, 0), "0")
        self.assertEqual(report.format_number(None, 1), "–")
        self.assertEqual(report.format_number(math.nan, 1), "–")
        self.assertEqual(report.format_number(True, 1), "–")
        self.assertEqual(report.format_percent(57.0), "57,0%")
        self.assertEqual(report.format_percent(None), "–")


if __name__ == "__main__":
    unittest.main()
