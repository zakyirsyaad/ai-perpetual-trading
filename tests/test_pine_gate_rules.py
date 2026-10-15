import unittest
from dataclasses import replace

from research.pine_gate.features import Snapshot
from research.pine_gate.rules import (
    contender2_signal,
    reference_signal,
    rule_signal,
    trend_label,
)
from research.pine_gate.spec import RuleSpec


def snap(**overrides) -> Snapshot:
    base = Snapshot(
        t_ms=0,
        close_5m=100.0, ema200_5m=99.0, ema125_5m=99.0, atr14_5m=1.0,
        rsi14_5m=50.0, rsi5_5m=50.0, adx14_5m=25.0, di_plus14_5m=30.0,
        di_minus14_5m=10.0, adx11_5m=25.0,
        high12_prev=101.0, low12_prev=99.0, high24_prev=102.0, low24_prev=98.0,
        h1_ema20=102.0, h1_ema50=100.0, h1_atr14=4.0, h1_vol_pct=50.0,
        h4_ema20=102.0, h4_ema50=100.0, h4_atr14=4.0,
        stretch_3m=0.0,
    )
    return replace(base, **overrides)


def never_called():
    raise AssertionError("poc must not be called")


class CountingPoc:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


def rule(entry="P35", trend="E200", strength="OFF", vol="OFF", vp="OFF"):
    return RuleSpec(trend, entry, strength, vol, vp)


class TrendLabelTests(unittest.TestCase):
    def test_trend_labels_respect_neutral_band(self):
        # H1 band = 0.25 * 4 = 1.0
        self.assertEqual(trend_label(snap(h1_ema20=101.5, h1_ema50=100.0), "H1"), 1)
        self.assertEqual(trend_label(snap(h1_ema20=101.0, h1_ema50=100.0), "H1"), 0)
        self.assertEqual(trend_label(snap(h1_ema20=100.5, h1_ema50=100.0), "H1"), 0)
        self.assertEqual(trend_label(snap(h1_ema20=98.5, h1_ema50=100.0), "H1"), -1)
        self.assertEqual(trend_label(snap(h1_ema20=99.0, h1_ema50=100.0), "H1"), 0)
        self.assertEqual(trend_label(snap(h4_ema20=98.0, h4_ema50=100.0), "H4"), -1)
        self.assertEqual(trend_label(snap(h4_ema20=102.0, h4_ema50=100.0), "H4"), 1)
        self.assertEqual(trend_label(snap(close_5m=100.0, ema200_5m=99.0), "E200"), 1)
        self.assertEqual(trend_label(snap(close_5m=98.0, ema200_5m=99.0), "E200"), -1)
        self.assertEqual(trend_label(snap(close_5m=99.0, ema200_5m=99.0), "E200"), 0)

    def test_trend_label_none_inputs_are_neutral(self):
        self.assertEqual(trend_label(snap(ema200_5m=None), "E200"), 0)
        self.assertEqual(trend_label(snap(close_5m=None), "E200"), 0)
        self.assertEqual(trend_label(snap(h1_atr14=None), "H1"), 0)
        self.assertEqual(trend_label(snap(h4_ema20=None), "H4"), 0)

    def test_unknown_trend_raises(self):
        with self.assertRaises(ValueError):
            trend_label(snap(), "E999")


class EntryTests(unittest.TestCase):
    def test_pullback_and_breakout_truth_table(self):
        up = dict(close_5m=100.0, ema200_5m=99.0)
        down = dict(close_5m=100.0, ema200_5m=101.0)
        poc = never_called
        # P35: strict < 35 long in uptrend, > 65 short in downtrend
        self.assertEqual(rule_signal(rule("P35"), snap(rsi14_5m=34.9, **up), poc), 1)
        self.assertEqual(rule_signal(rule("P35"), snap(rsi14_5m=35.0, **up), poc), 0)
        self.assertEqual(rule_signal(rule("P35"), snap(rsi14_5m=65.1, **down), poc), -1)
        self.assertEqual(rule_signal(rule("P35"), snap(rsi14_5m=65.0, **down), poc), 0)
        # RSI oversold in a downtrend (or overbought in an uptrend) is no signal
        self.assertEqual(rule_signal(rule("P35"), snap(rsi14_5m=30.0, **down), poc), 0)
        self.assertEqual(rule_signal(rule("P35"), snap(rsi14_5m=70.0, **up), poc), 0)
        # P40: 40 / 60
        self.assertEqual(rule_signal(rule("P40"), snap(rsi14_5m=39.9, **up), poc), 1)
        self.assertEqual(rule_signal(rule("P40"), snap(rsi14_5m=40.0, **up), poc), 0)
        self.assertEqual(rule_signal(rule("P40"), snap(rsi14_5m=60.1, **down), poc), -1)
        self.assertEqual(rule_signal(rule("P40"), snap(rsi14_5m=60.0, **down), poc), 0)
        # B12 / B24: strict break of the previous extreme
        self.assertEqual(rule_signal(rule("B12"), snap(close_5m=101.1, ema200_5m=99.0), poc), 1)
        self.assertEqual(rule_signal(rule("B12"), snap(close_5m=101.0, ema200_5m=99.0), poc), 0)
        self.assertEqual(rule_signal(rule("B12"), snap(close_5m=98.9, ema200_5m=100.0), poc), -1)
        self.assertEqual(rule_signal(rule("B12"), snap(close_5m=99.0, ema200_5m=100.0), poc), 0)
        # B24 uses the 24-bar extremes (102 / 98), not the 12-bar ones
        self.assertEqual(rule_signal(rule("B24"), snap(close_5m=101.5, ema200_5m=99.0), poc), 0)
        self.assertEqual(rule_signal(rule("B24"), snap(close_5m=102.1, ema200_5m=99.0), poc), 1)
        self.assertEqual(rule_signal(rule("B24"), snap(close_5m=97.9, ema200_5m=100.0), poc), -1)
        # breakout against the trend is no signal
        self.assertEqual(rule_signal(rule("B12"), snap(close_5m=98.0, ema200_5m=90.0), poc), 0)

    def test_neutral_trend_blocks_everything(self):
        s = snap(close_5m=99.0, ema200_5m=99.0, rsi14_5m=10.0)
        self.assertEqual(rule_signal(rule("P35"), s, never_called), 0)

    def test_none_inputs_fail_closed(self):
        up = dict(close_5m=100.0, ema200_5m=99.0)
        self.assertEqual(rule_signal(rule("P35"), snap(rsi14_5m=None, **up), never_called), 0)
        self.assertEqual(rule_signal(rule("B12"), snap(high12_prev=None, **up), never_called), 0)
        self.assertEqual(rule_signal(rule("B24"), snap(high24_prev=None, close_5m=103.0, ema200_5m=99.0), never_called), 0)


class StrengthTests(unittest.TestCase):
    def setUp(self):
        self.up = dict(close_5m=100.0, ema200_5m=99.0, rsi14_5m=30.0)
        self.down = dict(close_5m=100.0, ema200_5m=101.0, rsi14_5m=70.0)

    def test_adx_requires_strictly_above_minimum(self):
        r = rule(strength="ADX")
        self.assertEqual(rule_signal(r, snap(adx14_5m=20.1, **self.up), never_called), 1)
        self.assertEqual(rule_signal(r, snap(adx14_5m=20.0, **self.up), never_called), 0)
        self.assertEqual(rule_signal(r, snap(adx14_5m=None, **self.up), never_called), 0)

    def test_adx_di_requires_agreeing_di(self):
        r = rule(strength="ADX_DI")
        self.assertEqual(
            rule_signal(r, snap(di_plus14_5m=30, di_minus14_5m=10, **self.up), never_called), 1)
        self.assertEqual(
            rule_signal(r, snap(di_plus14_5m=10, di_minus14_5m=30, **self.up), never_called), 0)
        self.assertEqual(
            rule_signal(r, snap(di_plus14_5m=20, di_minus14_5m=20, **self.up), never_called), 0)
        self.assertEqual(
            rule_signal(r, snap(di_plus14_5m=10, di_minus14_5m=30, **self.down), never_called), -1)
        self.assertEqual(
            rule_signal(r, snap(di_plus14_5m=30, di_minus14_5m=10, **self.down), never_called), 0)
        self.assertEqual(
            rule_signal(r, snap(di_plus14_5m=None, **self.up), never_called), 0)


class VolTests(unittest.TestCase):
    def setUp(self):
        self.base = dict(close_5m=100.0, ema200_5m=99.0, rsi14_5m=30.0)

    def test_high_and_not_high_boundaries(self):
        high, not_high = rule(vol="HIGH"), rule(vol="NOT_HIGH")
        self.assertEqual(rule_signal(high, snap(h1_vol_pct=67.0, **self.base), never_called), 1)
        self.assertEqual(rule_signal(high, snap(h1_vol_pct=66.9, **self.base), never_called), 0)
        self.assertEqual(rule_signal(not_high, snap(h1_vol_pct=67.0, **self.base), never_called), 0)
        self.assertEqual(rule_signal(not_high, snap(h1_vol_pct=66.9, **self.base), never_called), 1)

    def test_vol_filter_none_blocks(self):
        s = snap(h1_vol_pct=None, **self.base)
        self.assertEqual(rule_signal(rule(vol="HIGH"), s, never_called), 0)
        self.assertEqual(rule_signal(rule(vol="NOT_HIGH"), s, never_called), 0)
        self.assertEqual(rule_signal(rule(vol="OFF"), s, never_called), 1)


class VolumeProfileTests(unittest.TestCase):
    def test_vp_rule_flips_with_entry_type(self):
        # pullback long needs price <= POC; breakout long needs price >= POC
        pull_long = snap(close_5m=100.0, ema200_5m=99.0, rsi14_5m=30.0)
        self.assertEqual(rule_signal(rule("P35", vp="ON"), pull_long, lambda: 100.5), 1)
        self.assertEqual(rule_signal(rule("P35", vp="ON"), pull_long, lambda: 100.0), 1)
        self.assertEqual(rule_signal(rule("P35", vp="ON"), pull_long, lambda: 99.5), 0)
        brk_long = snap(close_5m=102.0, ema200_5m=99.0)
        self.assertEqual(rule_signal(rule("B12", vp="ON"), brk_long, lambda: 101.5), 1)
        self.assertEqual(rule_signal(rule("B12", vp="ON"), brk_long, lambda: 102.0), 1)
        self.assertEqual(rule_signal(rule("B12", vp="ON"), brk_long, lambda: 102.5), 0)
        pull_short = snap(close_5m=100.0, ema200_5m=101.0, rsi14_5m=70.0)
        self.assertEqual(rule_signal(rule("P35", vp="ON"), pull_short, lambda: 99.5), -1)
        self.assertEqual(rule_signal(rule("P35", vp="ON"), pull_short, lambda: 100.5), 0)
        brk_short = snap(close_5m=98.0, ema200_5m=100.0)
        self.assertEqual(rule_signal(rule("B12", vp="ON"), brk_short, lambda: 98.5), -1)
        self.assertEqual(rule_signal(rule("B12", vp="ON"), brk_short, lambda: 97.5), 0)

    def test_vp_none_poc_fails(self):
        s = snap(close_5m=100.0, ema200_5m=99.0, rsi14_5m=30.0)
        self.assertEqual(rule_signal(rule("P35", vp="ON"), s, lambda: None), 0)

    def test_vp_none_close_fails(self):
        s = snap(close_5m=None, ema200_5m=99.0)
        self.assertEqual(rule_signal(rule("P35", vp="ON"), s, never_called), 0)

    def test_poc_not_called_when_earlier_conditions_fail(self):
        poc = CountingPoc(100.0)
        r = rule("P35", strength="ADX", vol="HIGH", vp="ON")
        base = dict(close_5m=100.0, ema200_5m=99.0, rsi14_5m=30.0, adx14_5m=25.0, h1_vol_pct=80.0)
        failing = (
            dict(ema200_5m=100.0),          # neutral trend
            dict(rsi14_5m=50.0),            # no entry
            dict(adx14_5m=10.0),            # weak
            dict(h1_vol_pct=10.0),          # vol
        )
        for override in failing:
            rule_signal(r, snap(**{**base, **override}), poc)
        self.assertEqual(poc.calls, 0)
        self.assertEqual(rule_signal(r, snap(**base), poc), 1)
        self.assertEqual(poc.calls, 1)

    def test_vp_off_never_calls_poc(self):
        s = snap(close_5m=100.0, ema200_5m=99.0, rsi14_5m=30.0)
        self.assertEqual(rule_signal(rule("P35"), s, never_called), 1)


class OverrideTests(unittest.TestCase):
    def test_trend_override_replaces_label(self):
        # actual E200 trend is up; override says down, so the short side is evaluated
        s = snap(close_5m=100.0, ema200_5m=99.0, rsi14_5m=70.0)
        self.assertEqual(rule_signal(rule("P35"), s, never_called), 0)
        self.assertEqual(rule_signal(rule("P35"), s, never_called, trend_override=-1), -1)
        # an override of 0 blocks even when the real label is non-zero
        s_up = snap(close_5m=100.0, ema200_5m=99.0, rsi14_5m=30.0)
        self.assertEqual(rule_signal(rule("P35"), s_up, never_called, trend_override=0), 0)
        # override can create a trend where the real label is neutral
        s_neutral = snap(close_5m=99.0, ema200_5m=99.0, rsi14_5m=30.0)
        self.assertEqual(rule_signal(rule("P35"), s_neutral, never_called, trend_override=1), 1)

    def test_contender2_override(self):
        s = snap(h1_ema20=102.0, h1_ema50=100.0, stretch_3m=0.1)
        self.assertEqual(contender2_signal(s), 1)
        self.assertEqual(contender2_signal(s, trend_override=-1), -1)
        self.assertEqual(contender2_signal(s, trend_override=0), 0)


class Contender2Tests(unittest.TestCase):
    def test_contender2_near_mean_follows_h1_trend(self):
        up = dict(h1_ema20=102.0, h1_ema50=100.0)
        down = dict(h1_ema20=98.0, h1_ema50=100.0)
        self.assertEqual(contender2_signal(snap(stretch_3m=0.0, **up)), 1)
        self.assertEqual(contender2_signal(snap(stretch_3m=0.75, **up)), 1)
        self.assertEqual(contender2_signal(snap(stretch_3m=-0.75, **down)), -1)
        self.assertEqual(contender2_signal(snap(stretch_3m=0.76, **up)), 0)
        self.assertEqual(contender2_signal(snap(stretch_3m=-0.76, **down)), 0)
        self.assertEqual(contender2_signal(snap(stretch_3m=None, **up)), 0)
        # neutral H1 label
        self.assertEqual(contender2_signal(snap(stretch_3m=0.0, h1_ema20=100.5, h1_ema50=100.0)), 0)
        self.assertEqual(contender2_signal(snap(stretch_3m=0.0, h1_atr14=None)), 0)


class ReferenceTests(unittest.TestCase):
    def test_reference_uses_pine_numbers(self):
        up = dict(close_5m=100.0, ema125_5m=99.0, adx11_5m=25.0)
        down = dict(close_5m=100.0, ema125_5m=101.0, adx11_5m=25.0)
        self.assertEqual(reference_signal(snap(rsi5_5m=39.9, **up)), 1)
        self.assertEqual(reference_signal(snap(rsi5_5m=40.0, **up)), 0)
        self.assertEqual(reference_signal(snap(rsi5_5m=55.1, **down)), -1)
        self.assertEqual(reference_signal(snap(rsi5_5m=55.0, **down)), 0)
        # asymmetric thresholds: RSI 45 is neither oversold nor overbought
        self.assertEqual(reference_signal(snap(rsi5_5m=45.0, **up)), 0)
        self.assertEqual(reference_signal(snap(rsi5_5m=45.0, **down)), 0)
        # wrong side for the trend
        self.assertEqual(reference_signal(snap(rsi5_5m=30.0, **down)), 0)
        self.assertEqual(reference_signal(snap(rsi5_5m=70.0, **up)), 0)
        # ADX(11) must be strictly above 20
        self.assertEqual(reference_signal(snap(rsi5_5m=30.0, **{**up, "adx11_5m": 20.0})), 0)
        self.assertEqual(reference_signal(snap(rsi5_5m=30.0, **{**up, "adx11_5m": None})), 0)
        # tie on the trend, None inputs
        self.assertEqual(reference_signal(snap(rsi5_5m=30.0, close_5m=100.0, ema125_5m=100.0, adx11_5m=25.0)), 0)
        self.assertEqual(reference_signal(snap(rsi5_5m=None, **up)), 0)
        self.assertEqual(reference_signal(snap(rsi5_5m=30.0, close_5m=100.0, ema125_5m=None, adx11_5m=25.0)), 0)



_LONG_VALUES = {
    "close_5m": 110.0, "ema200_5m": 100.0, "ema125_5m": 100.0, "atr14_5m": 1.0,
    "rsi14_5m": 10.0, "rsi5_5m": 10.0, "adx14_5m": 50.0, "di_plus14_5m": 30.0,
    "di_minus14_5m": 10.0, "adx11_5m": 50.0,
    "high12_prev": 105.0, "low12_prev": 90.0, "high24_prev": 105.0, "low24_prev": 90.0,
    "h1_ema20": 110.0, "h1_ema50": 100.0, "h1_atr14": 1.0,
    "h4_ema20": 110.0, "h4_ema50": 100.0, "h4_atr14": 1.0, "stretch_3m": 0.1,
}
_SHORT_VALUES = {
    **_LONG_VALUES,
    "close_5m": 90.0, "rsi14_5m": 90.0, "rsi5_5m": 90.0, "di_plus14_5m": 10.0,
    "di_minus14_5m": 30.0, "high12_prev": 110.0, "low12_prev": 95.0, "high24_prev": 110.0,
    "low24_prev": 95.0, "h1_ema20": 90.0, "h4_ema20": 90.0,
}
_ALL_FIELDS = tuple(f for f in Snapshot.__dataclass_fields__ if f != "t_ms")


def only(fields, values=_LONG_VALUES, **extra) -> Snapshot:
    """A snapshot with only ``fields`` set (from ``values``); every other field is None."""
    snapshot = {f: None for f in _ALL_FIELDS}
    snapshot.update({f: values.get(f) for f in fields})
    snapshot.update(extra)
    return Snapshot(t_ms=0, **snapshot)


class RequiredFieldsTests(unittest.TestCase):
    def all_rules(self):
        from itertools import product

        from research.pine_gate.spec import ENTRIES, STRENGTHS, TRENDS, VOLS, VPS

        return [RuleSpec(*combo) for combo in product(TRENDS, ENTRIES, STRENGTHS, VOLS, VPS)]

    def test_rule_signal_needs_exactly_the_required_fields(self):
        from research.pine_gate.rules import required_fields
        from research.pine_gate.spec import ENTRY_PARAMS

        rules = self.all_rules()
        self.assertEqual(len(rules), 216)
        for r in rules:
            fields = required_fields(r)
            pullback = ENTRY_PARAMS[r.entry]["kind"] == "pullback"
            vol = {"HIGH": 99.0, "NOT_HIGH": 1.0}.get(r.vol)
            extra = {"h1_vol_pct": vol} if "h1_vol_pct" in fields else {}
            setups = ((1, _LONG_VALUES, 200.0 if pullback else 50.0),
                      (-1, _SHORT_VALUES, 50.0 if pullback else 200.0))
            matters = set()
            for side, values, poc in setups:
                s = only(fields, values, **extra)
                self.assertEqual(rule_signal(r, s, lambda: poc), side, f"{r.key()} {side}")
                for name in fields:
                    if rule_signal(r, replace(s, **{name: None}), lambda: poc) == 0:
                        matters.add(name)
            self.assertEqual(matters, set(fields), r.key())

    def test_contender2_and_reference_need_exactly_their_fields(self):
        from research.pine_gate.rules import (
            contender2_required_fields,
            reference_required_fields,
        )

        for signal, fields in ((contender2_signal, contender2_required_fields()),
                               (reference_signal, reference_required_fields())):
            self.assertEqual(signal(only(fields)), 1, signal.__name__)
            for name in fields:
                self.assertEqual(signal(replace(only(fields), **{name: None})), 0,
                                 f"{signal.__name__} without {name}")


if __name__ == "__main__":
    unittest.main()
