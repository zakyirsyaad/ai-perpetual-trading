import unittest

from research.pine_gate.bars import bars_from_rows
from research.pine_gate.outcomes import Outcome, OutcomeCache, bracket_outcome, gross_bps
from research.pine_gate.spec import ExecConfig

MIN = 60_000
CFG = ExecConfig()
BASE = 100.0
ENTRY_LONG = BASE * (1 + 0.25e-4)
ENTRY_SHORT = BASE * (1 - 0.25e-4)
TP_LONG = ENTRY_LONG * 1.002
SL_LONG = ENTRY_LONG * 0.998
TP_SHORT = ENTRY_SHORT * 0.998
SL_SHORT = ENTRY_SHORT * 1.002


def flat(n, price=BASE):
    return [(i * MIN, price, price, price, price, 1.0) for i in range(n)]


def with_bar(rows, idx, o=None, h=None, l=None, c=None):
    t, ro, rh, rl, rc, v = rows[idx]
    rows = list(rows)
    rows[idx] = (
        t,
        ro if o is None else o,
        rh if h is None else h,
        rl if l is None else l,
        rc if c is None else c,
        v,
    )
    return rows


def bars_of(rows):
    return bars_from_rows(rows, MIN)


class BracketOutcomeTests(unittest.TestCase):
    def test_long_tp_first(self):
        rows = with_bar(flat(80), 5, h=TP_LONG + 0.01)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        self.assertEqual(o.kind, "TP")
        self.assertEqual(o.exit_idx, 5)
        self.assertAlmostEqual(o.entry_price, ENTRY_LONG, places=9)
        self.assertAlmostEqual(o.raw_exit_price, TP_LONG, places=9)
        self.assertFalse(o.gapped)
        self.assertFalse(o.both_touched)

    def test_long_sl_first_is_clean_stop(self):
        rows = with_bar(flat(80), 3, l=SL_LONG - 0.01)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        self.assertEqual(o.kind, "SL")
        self.assertEqual(o.exit_idx, 3)
        self.assertAlmostEqual(o.raw_exit_price, SL_LONG, places=9)
        self.assertFalse(o.gapped)
        self.assertFalse(o.both_touched)

    def test_same_bar_touch_is_stop(self):
        rows = with_bar(flat(80), 4, h=TP_LONG + 0.01, l=SL_LONG - 0.01)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        self.assertEqual(o.kind, "SL")
        self.assertEqual(o.exit_idx, 4)
        self.assertAlmostEqual(o.raw_exit_price, SL_LONG, places=9)
        self.assertTrue(o.both_touched)

    def test_gap_through_stop_fills_at_open_minus_overshoot(self):
        gap_open = SL_LONG - 0.30
        rows = with_bar(flat(80), 2, o=gap_open, h=gap_open, l=gap_open - 0.1, c=gap_open)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        self.assertEqual(o.kind, "SL")
        self.assertTrue(o.gapped)
        self.assertFalse(o.both_touched)
        self.assertAlmostEqual(o.raw_exit_price, gap_open, places=9)
        bps = gross_bps(o, 1, 1.0, CFG)
        expected = (gap_open * (1 - 1e-4) / ENTRY_LONG - 1) * 1e4
        self.assertAlmostEqual(bps, expected, places=9)

    def test_gap_through_target_fills_at_open(self):
        gap_open = TP_LONG + 0.30
        rows = with_bar(flat(80), 2, o=gap_open, h=gap_open + 0.1, l=gap_open, c=gap_open)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        self.assertEqual(o.kind, "TP")
        self.assertTrue(o.gapped)
        self.assertAlmostEqual(o.raw_exit_price, gap_open, places=9)

    def test_time_stop_exits_at_bar_60_close(self):
        rows = with_bar(flat(80), 60, h=100.05, c=100.05)  # a valid bar: high >= close
        # a touch on bar 61 must not matter
        rows = with_bar(rows, 61, h=TP_LONG + 1.0)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        self.assertEqual(o.kind, "TIME")
        self.assertEqual(o.exit_idx, 60)
        self.assertAlmostEqual(o.raw_exit_price, 100.05, places=9)

    def test_touch_on_bar_60_still_decides(self):
        rows = with_bar(flat(80), 60, h=TP_LONG + 0.01)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        self.assertEqual(o.kind, "TP")
        self.assertEqual(o.exit_idx, 60)

    def test_none_when_data_ends_first(self):
        self.assertIsNone(bracket_outcome(bars_of(flat(40)), 0, 1, CFG))
        self.assertIsNone(bracket_outcome(bars_of(flat(60)), 0, 1, CFG))

    def test_touch_before_data_ends_is_decided(self):
        rows = with_bar(flat(40), 7, h=TP_LONG + 0.01)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        self.assertEqual(o.kind, "TP")

    def test_short_mirrors_long(self):
        rows = with_bar(flat(80), 5, l=TP_SHORT - 0.01)
        o = bracket_outcome(bars_of(rows), 0, -1, CFG)
        self.assertEqual(o.kind, "TP")
        self.assertAlmostEqual(o.entry_price, ENTRY_SHORT, places=9)
        self.assertAlmostEqual(o.raw_exit_price, TP_SHORT, places=9)

        rows = with_bar(flat(80), 3, h=SL_SHORT + 0.01)
        o = bracket_outcome(bars_of(rows), 0, -1, CFG)
        self.assertEqual(o.kind, "SL")
        self.assertAlmostEqual(o.raw_exit_price, SL_SHORT, places=9)

        rows = with_bar(flat(80), 4, h=SL_SHORT + 0.01, l=TP_SHORT - 0.01)
        o = bracket_outcome(bars_of(rows), 0, -1, CFG)
        self.assertEqual(o.kind, "SL")
        self.assertTrue(o.both_touched)

        gap_open = SL_SHORT + 0.30
        rows = with_bar(flat(80), 2, o=gap_open, h=gap_open + 0.1, l=gap_open, c=gap_open)
        o = bracket_outcome(bars_of(rows), 0, -1, CFG)
        self.assertEqual(o.kind, "SL")
        self.assertTrue(o.gapped)
        self.assertAlmostEqual(o.raw_exit_price, gap_open, places=9)

        gap_open = TP_SHORT - 0.30
        rows = with_bar(flat(80), 2, o=gap_open, h=gap_open, l=gap_open - 0.1, c=gap_open)
        o = bracket_outcome(bars_of(rows), 0, -1, CFG)
        self.assertEqual(o.kind, "TP")
        self.assertTrue(o.gapped)

    def test_uses_config_bracket_and_time_stop(self):
        cfg = ExecConfig(bracket_pct=0.10, time_stop_min=10)
        tp = ENTRY_LONG * 1.001
        rows = with_bar(flat(30), 5, h=tp + 0.001)
        self.assertEqual(bracket_outcome(bars_of(rows), 0, 1, cfg).kind, "TP")
        o = bracket_outcome(bars_of(flat(30)), 0, 1, cfg)
        self.assertEqual((o.kind, o.exit_idx), ("TIME", 10))

    def test_rejects_bad_arguments(self):
        bars = bars_of(flat(80))
        with self.assertRaises(ValueError):
            bracket_outcome(bars, 0, 0, CFG)
        with self.assertRaises(ValueError):
            bracket_outcome(bars, -1, 1, CFG)
        with self.assertRaises(ValueError):
            bracket_outcome(bars, 80, 1, CFG)


class GrossBpsTests(unittest.TestCase):
    # gross_bps is measured from the entry FILL (already slipped); levels are set
    # from that fill, so the entry slip is not charged a second time.
    def test_gross_bps_applies_exit_slip_only(self):
        rows = with_bar(flat(80), 5, h=TP_LONG + 0.01)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        expected = (1.002 * (1 - 0.25e-4) - 1) * 1e4
        self.assertAlmostEqual(gross_bps(o, 1, 1.0, CFG), expected, places=9)
        self.assertAlmostEqual(expected, 19.7495, places=4)
        rows = with_bar(flat(80), 5, l=TP_SHORT - 0.01)
        o = bracket_outcome(bars_of(rows), 0, -1, CFG)
        expected = -(0.998 * (1 + 0.25e-4) - 1) * 1e4
        self.assertAlmostEqual(gross_bps(o, -1, 1.0, CFG), expected, places=9)

    def test_clean_stop_includes_overshoot(self):
        rows = with_bar(flat(80), 3, l=SL_LONG - 0.01)
        o = bracket_outcome(bars_of(rows), 0, 1, CFG)
        expected = (0.998 * (1 - 1e-4) - 1) * 1e4
        self.assertAlmostEqual(gross_bps(o, 1, 1.0, CFG), expected, places=9)
        self.assertAlmostEqual(expected, -20.998, places=3)
        self.assertAlmostEqual(
            gross_bps(o, 1, 0.0, CFG), (0.998 - 1) * 1e4, places=9
        )
        rows = with_bar(flat(80), 3, h=SL_SHORT + 0.01)
        o = bracket_outcome(bars_of(rows), 0, -1, CFG)
        expected = -(1.002 * (1 + 1e-4) - 1) * 1e4
        self.assertAlmostEqual(gross_bps(o, -1, 1.0, CFG), expected, places=9)

    def test_time_exit_applies_tp_slip(self):
        o = bracket_outcome(bars_of(flat(80)), 0, 1, CFG)
        self.assertEqual(o.kind, "TIME")
        expected = ((1 - 0.25e-4) * BASE / ENTRY_LONG - 1) * 1e4
        self.assertAlmostEqual(gross_bps(o, 1, 1.0, CFG), expected, places=9)


class OutcomeCacheTests(unittest.TestCase):
    def test_memoises_and_matches_direct_call(self):
        bars = bars_of(with_bar(flat(80), 5, h=TP_LONG + 0.01))
        cache = OutcomeCache(bars, CFG)
        first = cache.get(0, 1)
        self.assertIs(cache.get(0, 1), first)
        self.assertEqual(first, bracket_outcome(bars, 0, 1, CFG))
        self.assertIsNot(cache.get(0, -1), first)

    def test_caches_none_and_outcome_is_immutable(self):
        cache = OutcomeCache(bars_of(flat(40)), CFG)
        self.assertIsNone(cache.get(0, 1))
        self.assertIsNone(cache.get(0, 1))
        o = OutcomeCache(bars_of(flat(80)), CFG).get(0, 1)
        with self.assertRaises(Exception):
            o.kind = "SL"


if __name__ == "__main__":
    unittest.main()
