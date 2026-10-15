import json
import tempfile
import unittest
from pathlib import Path

from research.pine_gate.bars import BarGapError, bars_from_rows
from research.pine_gate.checks import (
    Agreement,
    DecisionRow,
    decision_buckets,
    load_edge_klines_vst,
    parse_decision,
    vst_agreement,
)
from research.pine_gate.features import FeatureFrame
from research.pine_gate.spec import ExecConfig

MIN = 60_000
CFG = ExecConfig()
START = 1_700_000_000_000 - (1_700_000_000_000 % MIN)
SPACING = 61  # minutes between ticks, so each 60-bar bracket window is private
TP, SL = "TP", "SL"
HOUR = 3_600_000


def entry_times(n):
    return [START + i * SPACING * MIN for i in range(n)]


def build_frame(kinds, symbol="BTC-USDT"):
    """Flat 100.0 bars; entry i gets a spike two bars after its entry bar.

    ``kinds[i]`` is TP (high spike), SL (low spike) or None (no spike -> TIME).
    """
    n = len(kinds) * SPACING + 70
    rows = [(START + i * MIN, 100.0, 100.0, 100.0, 100.0, 1.0) for i in range(n)]
    for i, kind in enumerate(kinds):
        j = i * SPACING + 2
        t, o, h, l, c, v = rows[j]
        if kind == TP:
            h = 100.3
        elif kind == SL:
            l = 99.7
        rows[j] = (t, o, h, l, c, v)
    return FeatureFrame.build(symbol, bars_from_rows(rows, MIN))


def long_signal(diff_times=(), diff_side=-1):
    """Production always +1; VST differs (diff_side) on ``diff_times``."""
    diff = set(diff_times)

    def signal_for(frame, poc, t):
        if t in diff and frame.bars_1m is not poc.bars_1m:
            return diff_side
        return 1

    return signal_for


class VstAgreementTests(unittest.TestCase):
    def check(self, prod_kinds, vst_kinds, signal_for=None, times=None):
        prod = build_frame(prod_kinds)
        vst = build_frame(vst_kinds)
        return vst_agreement(
            prod, vst, signal_for or long_signal(),
            entry_times(len(prod_kinds)) if times is None else times, CFG
        )

    def test_identical_frames_agree_fully(self):
        kinds = [TP, SL] * 10
        times = entry_times(20)
        prod = build_frame(kinds)
        vst = build_frame(kinds)
        seen = []

        def signal_for(frame, poc, t):
            seen.append((frame, poc))
            return 0 if t == times[3] else 1

        result = vst_agreement(prod, vst, signal_for, times, CFG)
        self.assertIsInstance(result, Agreement)
        self.assertEqual(result.n, 20)
        self.assertEqual(result.side_agree, 1.0)
        self.assertEqual(result.outcome_agree, 1.0)
        # tick 3 has no production signal, so one SL drops out of the WR
        self.assertAlmostEqual(result.wr_prod, 100.0 * 10 / 19)
        self.assertEqual(result.wr_vst, result.wr_prod)
        self.assertTrue(result.passed)
        # volume-profile rules read the production POC on both sides
        self.assertEqual(len(seen), 40)
        self.assertTrue(all(poc is prod for _, poc in seen))
        self.assertEqual(sum(1 for f, _ in seen if f is vst), 20)

    def test_agreement_thresholds(self):
        base = [TP, SL] * 50  # 100 entries, WR 50
        ts = entry_times(100)

        with self.subTest("side 97% passes"):
            r = self.check(base, base, long_signal(ts[:3]), ts)
            self.assertEqual(r.side_agree, 0.97)
            self.assertEqual((r.n_either, r.n_both, r.n_prod, r.n_vst), (100, 97, 100, 100))
            # VST's WR is taken at its own ticks with its own side: the three shorts turn
            # TP, SL, TP spikes into SL, TP, SL
            self.assertEqual((r.wr_prod, r.wr_vst), (50.0, 49.0))
            self.assertTrue(r.passed)
        with self.subTest("side 96% fails"):
            r = self.check(base, base, long_signal(ts[:4]), ts)
            self.assertEqual(r.side_agree, 0.96)
            self.assertFalse(r.passed)
        with self.subTest("flat vs long counts as a side disagreement"):
            r = self.check(base, base, long_signal(ts[:4], diff_side=0), ts)
            self.assertEqual(r.side_agree, 0.96)

        def swapped(k):  # k TP->SL and k SL->TP: WR unchanged, 2k kinds differ
            out = list(base)
            for idx in range(k):
                out[2 * idx], out[2 * idx + 1] = SL, TP
            return out

        with self.subTest("outcome 96% passes"):
            r = self.check(base, swapped(2), times=ts)
            self.assertEqual(r.outcome_agree, 0.96)
            self.assertEqual(r.wr_prod, r.wr_vst)
            self.assertTrue(r.passed)
        with self.subTest("outcome 94% fails"):
            r = self.check(base, swapped(3), times=ts)
            self.assertEqual(r.outcome_agree, 0.94)
            self.assertFalse(r.passed)

        def lost_wins(d):  # d TP->SL on VST: outcome 1-d/100, WR 50 -> 50-d
            out = list(base)
            for idx in range(d):
                out[2 * idx] = SL
            return out

        with self.subTest("WR gap of 2 points passes"):
            r = self.check(base, lost_wins(2), times=ts)
            self.assertEqual((r.wr_prod, r.wr_vst), (50.0, 48.0))
            self.assertTrue(r.passed)
        with self.subTest("WR gap of 3 points fails"):
            r = self.check(base, lost_wins(3), times=ts)
            self.assertEqual(r.outcome_agree, 0.97)
            self.assertFalse(r.passed)

    def test_no_comparable_result_never_passes(self):
        with self.subTest("no times"):
            r = self.check([TP, SL], [TP, SL], times=[])
            self.assertEqual(r.n, 0)
            self.assertIsNone(r.side_agree)
            self.assertFalse(r.passed)
        with self.subTest("neither side ever signals"):
            r = self.check([TP, SL], [TP, SL], signal_for=lambda f, p, t: 0)
            self.assertEqual((r.n, r.n_either), (2, 0))
            self.assertIsNone(r.side_agree)
            self.assertIsNone(r.outcome_agree)
            self.assertFalse(r.passed)
        with self.subTest("no TP or SL, only time stops"):
            r = self.check([None, None], [None, None])
            self.assertEqual(r.outcome_agree, 1.0)
            self.assertIsNone(r.wr_prod)
            self.assertFalse(r.passed)

    def test_signals_that_never_overlap_fail(self):
        """Production signals at tick 10 only, VST at tick 50 only: the flat ticks on
        both sides must not carry the side agreement (the reviewer's counterexample)."""
        kinds = [TP, SL] * 50
        ts = entry_times(100)

        def signal_for(frame, poc, t):
            own = ts[50] if frame.bars_1m is not poc.bars_1m else ts[10]
            return 1 if t == own else 0

        r = self.check(kinds, kinds, signal_for, ts)
        self.assertEqual(r.n, 100)
        self.assertEqual((r.n_either, r.n_both, r.n_prod, r.n_vst), (2, 0, 1, 1))
        self.assertEqual(r.side_agree, 0.0)
        self.assertIsNone(r.outcome_agree)
        self.assertEqual((r.wr_prod, r.wr_vst), (100.0, 100.0))
        self.assertFalse(r.passed)

    def test_vst_only_signals_count_against_the_side_agreement(self):
        kinds = [TP, SL] * 50
        ts = entry_times(100)

        def signal_for(frame, poc, t):  # production silent on the first 4 ticks VST trades
            return 0 if t in ts[:4] and frame.bars_1m is poc.bars_1m else 1

        r = self.check(kinds, kinds, signal_for, ts)
        self.assertEqual((r.n_either, r.n_both, r.n_prod, r.n_vst), (100, 96, 96, 100))
        self.assertEqual(r.side_agree, 0.96)
        self.assertFalse(r.passed)

    def test_unresolved_outcomes_are_skipped(self):
        kinds = [TP, SL]
        prod = build_frame(kinds)
        vst = build_frame(kinds)
        last_bar = prod.bars_1m.open_ms[-1]
        times = entry_times(2) + [last_bar]  # last bar: bracket runs off the data
        r = vst_agreement(prod, vst, long_signal(), times, CFG)
        self.assertEqual(r.n, 3)
        self.assertEqual((r.n_either, r.n_both, r.n_prod, r.n_vst), (3, 2, 2, 2))
        self.assertEqual(r.side_agree, 1.0)
        self.assertEqual(r.outcome_agree, 1.0)
        self.assertEqual(r.wr_prod, 50.0)


class LoadVstKlinesTests(unittest.TestCase):
    def write(self, payload):
        return self.write_text(json.dumps(payload))

    def write_text(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "edge.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_sorted_with_zero_volume_and_ignores_other_keys(self):
        path = self.write(
            {
                "k1": {str(MIN * 2): [3, 4, 2, 3.5], "0": [1, 2, 0.5, 1.5], str(MIN): [2, 3, 1, 2.5]},
                "k5": {"0": [9, 9, 9, 9]},
                "note": "x",
            }
        )
        bars = load_edge_klines_vst(path)
        self.assertEqual(bars.interval_ms, MIN)
        self.assertEqual(list(bars.open_ms), [0, MIN, 2 * MIN])
        self.assertEqual(list(bars.close), [1.5, 2.5, 3.5])
        self.assertEqual(list(bars.high), [2.0, 3.0, 4.0])
        self.assertEqual(list(bars.volume), [0.0, 0.0, 0.0])

    def test_bad_values_and_malformed_rows_raise_value_error(self):
        raw_cases = {
            "nan": '{"k1": {"0": [1, NaN, 1, 1]}}',
            "infinity": '{"k1": {"0": [1, Infinity, 1, 1]}}',
            "minus infinity": '{"k1": {"0": [1, 1, -Infinity, 1]}}',
        }
        for name, text in raw_cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    load_edge_klines_vst(self.write_text(text))
        cases = {
            "zero price": {"0": [0, 1, 0, 1]},
            "negative price": {"0": [-1, 1, -2, 1]},
            "high below close": {"0": [1, 1.5, 1, 2]},
            "low above open": {"0": [1, 2, 1.5, 1.8]},
            "three values": {"0": [1, 1, 1]},
            "not a list": {"0": "1,1,1,1"},
            "string price": {"0": ["a", 1, 1, 1]},
            "bool price": {"0": [True, 1, 1, 1]},
            "non-integer key": {"x": [1, 1, 1, 1]},
            "negative key": {"-60000": [1, 1, 1, 1]},
        }
        for name, k1 in cases.items():
            with self.subTest(name):
                with self.assertRaises(ValueError) as ctx:
                    load_edge_klines_vst(self.write({"k1": k1}))
                self.assertNotIsInstance(ctx.exception, (TypeError, KeyError))

    def test_gap_raises_and_missing_k1_raises(self):
        with self.assertRaises(BarGapError):
            load_edge_klines_vst(self.write({"k1": {"0": [1, 1, 1, 1], str(2 * MIN): [1, 1, 1, 1]}}))
        with self.assertRaises(ValueError):
            load_edge_klines_vst(self.write({"k5": {}}))


def row(symbol="BTC/USDT", action="OPEN_LONG", confidence=0.8, created="2026-09-01T03:00:00+00:00",
        approved=True, call_id="c1", risk_review=True):
    decision = {}
    if risk_review:
        decision["risk_review"] = {"approved": approved}
    return {
        "symbol": symbol,
        "action": action,
        "confidence": confidence,
        "created_at": created,
        "payload_json": {"decision": decision, "ai_call_id": call_id},
    }


class ParseDecisionTests(unittest.TestCase):
    def test_parse_decision_keeps_hold_as_zero_side(self):
        hold = parse_decision(row(action="HOLD", confidence=0.4, approved=False))
        self.assertEqual(
            hold,
            DecisionRow("BTC-USDT", 1_788_231_600_000, 0, 0.4, False, "c1"),
        )
        self.assertEqual(parse_decision(row(action="OPEN_LONG")).side, 1)
        self.assertEqual(parse_decision(row(action="OPEN_SHORT")).side, -1)

    def test_other_actions_and_broken_rows_are_none(self):
        self.assertIsNone(parse_decision(row(action="CLOSE")))
        self.assertIsNone(parse_decision(row(confidence="high")))
        self.assertIsNone(parse_decision(row(created="yesterday")))
        self.assertIsNone(parse_decision({"symbol": "BTC/USDT"}))
        self.assertIsNone(parse_decision(row(symbol=None)))
        for action in (["OPEN_LONG"], {"a": 1}, 1, None):
            with self.subTest(action=action):
                self.assertIsNone(parse_decision(row(action=action)))

    def test_timestamps_accept_z_and_odd_fraction_lengths(self):
        base = parse_decision(row(created="2026-09-01T03:00:00+00:00")).created_ms
        for text, extra in [
            ("2026-09-01T03:00:00Z", 0),
            ("2026-09-01T03:00:00.5Z", 500),
            ("2026-09-01T03:00:00.12345+00:00", 123),
            ("2026-09-01T03:00:00.123456789Z", 123),
            ("2026-09-01 10:00:00.250+07:00", 250),
            ("2026-09-01T03:00:00", 0),
        ]:
            with self.subTest(text):
                self.assertEqual(parse_decision(row(created=text)).created_ms, base + extra)

    def test_approval_and_call_id_defaults(self):
        self.assertFalse(parse_decision(row(risk_review=False)).approved)
        self.assertFalse(parse_decision(row(approved="yes")).approved)
        self.assertIsNone(parse_decision(row(call_id=None)).ai_call_id)
        bare = row()
        bare["payload_json"] = None
        parsed = parse_decision(bare)
        self.assertFalse(parsed.approved)
        self.assertIsNone(parsed.ai_call_id)


DAY1 = 1_788_231_600_000  # 2026-09-01 10:00 WIB
DAY2 = DAY1 + 24 * HOUR


def drow(created, side, conf, approved):
    return DecisionRow("BTC-USDT", created, side, conf, approved, None)


class BucketTests(unittest.TestCase):
    def setUp(self):
        t = lambda k: DAY1 + k * 1000  # noqa: E731
        self.rows = [
            drow(t(1), 1, 0.80, True),    # A, signal +1 -> B
            drow(t(2), 1, 0.90, True),    # A, signal -1 -> not B
            drow(t(3), -1, 0.75, True),   # A (boundary), no signal
            drow(t(4), 1, 0.74, True),    # confidence too low
            drow(t(5), 1, 0.95, False),   # not approved
            drow(t(6), 0, 0.30, False),   # HOLD, signal +1 -> E
            drow(t(7), 0, 0.30, False),   # HOLD, signal -1 -> E (TIME)
            drow(t(8), 0, 0.30, False),   # HOLD, no signal
            drow(DAY2 + 1000, 1, 0.80, True),  # A on day 2, unresolved outcome
        ]
        self.signals = {
            ("BTC-USDT", t(1)): 1,
            ("BTC-USDT", t(2)): -1,
            ("BTC-USDT", t(4)): 1,
            ("BTC-USDT", t(5)): -1,
            ("BTC-USDT", t(6)): 1,
            ("BTC-USDT", t(7)): -1,
        }
        self.outcomes = {
            ("BTC-USDT", t(1), 1): (TP, 19.0),
            ("BTC-USDT", t(2), 1): (SL, -21.0),
            ("BTC-USDT", t(3), -1): ("TIME", 3.0),
            ("BTC-USDT", t(4), 1): (TP, 19.0),
            ("BTC-USDT", t(5), -1): (SL, -21.0),
            ("BTC-USDT", t(6), 1): (TP, 19.0),
            ("BTC-USDT", t(7), -1): ("TIME", -5.0),
            ("BTC-USDT", t(2), -1): (TP, 19.0),
        }
        self.calls = []

        def outcome_for(symbol, t_ms, side):
            self.calls.append((symbol, t_ms, side))
            return self.outcomes.get((symbol, t_ms, side))

        self.buckets = decision_buckets(
            self.rows,
            lambda r: r.created_ms,
            lambda symbol, t_ms: self.signals.get((symbol, t_ms), 0),
            outcome_for,
        )

    def test_bucket_definitions(self):
        b = self.buckets
        self.assertEqual(set(b), {"A", "B", "E", "F"})
        # A: rows 1, 2, 3 resolve (AI side); day-2 row has no outcome
        self.assertEqual((b["A"]["n"], b["A"]["tp"], b["A"]["sl"], b["A"]["time"]), (3, 1, 1, 1))
        self.assertEqual(b["A"]["wr"], 50.0)
        self.assertAlmostEqual(b["A"]["gross"], (19.0 - 21.0 + 3.0) / 3)
        # B: only row 1 has a same-side signal
        self.assertEqual((b["B"]["n"], b["B"]["tp"], b["B"]["sl"]), (1, 1, 0))
        self.assertEqual(b["B"]["wr"], 100.0)
        self.assertEqual(b["B"]["gross"], 19.0)
        # E: HOLD rows 6 and 7; outcome taken with the signal's side
        self.assertEqual((b["E"]["n"], b["E"]["tp"], b["E"]["time"]), (2, 1, 1))
        self.assertEqual(b["E"]["wr"], 100.0)
        self.assertAlmostEqual(b["E"]["gross"], 7.0)
        # F: every signal tick (rows 1, 2, 4, 5, 6, 7) with the signal's side
        self.assertEqual((b["F"]["n"], b["F"]["tp"], b["F"]["sl"], b["F"]["time"]), (6, 4, 1, 1))
        self.assertEqual(b["F"]["wr"], 80.0)
        self.assertAlmostEqual(b["F"]["gross"], (19 + 19 + 19 - 21 + 19 - 5) / 6)
        # the AI side drives A, the signal side drives E and F
        self.assertIn(("BTC-USDT", DAY1 + 2000, 1), self.calls)
        self.assertIn(("BTC-USDT", DAY1 + 2000, -1), self.calls)

    def test_trades_per_day_uses_distinct_wib_days_of_all_rows(self):
        # rows fall on 2 WIB days; A resolved 3 -> 1.5/day
        self.assertEqual(self.buckets["A"]["trades_per_day"], 1.5)
        self.assertEqual(self.buckets["F"]["trades_per_day"], 3.0)

    def test_empty_bucket_has_no_rates(self):
        empty = decision_buckets([], lambda r: r.created_ms, lambda s, t: 0, lambda s, t, d: None)
        for name in "ABEF":
            self.assertEqual(empty[name]["n"], 0)
            self.assertIsNone(empty[name]["wr"])
            self.assertIsNone(empty[name]["gross"])
            self.assertEqual(empty[name]["trades_per_day"], 0.0)


if __name__ == "__main__":
    unittest.main()
