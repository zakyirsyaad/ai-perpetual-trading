"""Rule signals: map a feature ``Snapshot`` to a trade side.

Research code; the bot never imports it. Standard library only.

Every function is pure and returns +1 (long), -1 (short) or 0 (no trade). All
comparisons are strict as written in spec 5.5, 5.7 and 5.9, and any ``None``
input a condition needs makes that condition fail, so the result is 0 and
nothing raises. Thresholds come from ``spec`` so a changed parameter changes
both the spec hash and the rule.
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

from .features import Snapshot
from .spec import (
    ENTRY_PARAMS,
    STRENGTH_PARAMS,
    TREND_PARAMS,
    VOL_PARAMS,
    VP_PARAMS,
    RuleSpec,
    spec_document,
)

PocFn = Callable[[], Optional[float]]

_SPEC = spec_document()
# Spec 5.7: the live near-mean gate. Inclusive (<=), mirroring the live check
# in bot.ai.orchestration._stretch_gate_verdict, which blocks only when
# abs(stretch) > ENTRY_STRETCH_GATE_MAX_ATR (orchestration.py line 587).
_NEAR_MEAN_MAX_ABS_STRETCH = _SPEC["contender2"]["near_mean_max_abs_stretch_atr"]
_REFERENCE = _SPEC["reference"]
_REF_RSI_LONG_BELOW = _REFERENCE["rsi"]["long_below"]
_REF_RSI_SHORT_ABOVE = _REFERENCE["rsi"]["short_above"]
_REF_ADX_MIN = _REFERENCE["adx"]["min"]


def _sign(diff: float) -> int:
    return 1 if diff > 0 else (-1 if diff < 0 else 0)


def _ema_cross_label(
    ema_fast: Optional[float],
    ema_slow: Optional[float],
    atr: Optional[float],
    band_atr: float,
) -> int:
    if ema_fast is None or ema_slow is None or atr is None:
        return 0
    band = band_atr * atr
    diff = ema_fast - ema_slow
    if diff > band:
        return 1
    if diff < -band:
        return -1
    return 0


def trend_label(s: Snapshot, trend: str) -> int:
    """+1 up, -1 down, 0 neutral or unknown for "E200", "H1" or "H4"."""
    if trend == "E200":
        if s.close_5m is None or s.ema200_5m is None:
            return 0
        return _sign(s.close_5m - s.ema200_5m)
    if trend == "H1":
        return _ema_cross_label(
            s.h1_ema20, s.h1_ema50, s.h1_atr14, TREND_PARAMS["H1"]["band_atr"]
        )
    if trend == "H4":
        return _ema_cross_label(
            s.h4_ema20, s.h4_ema50, s.h4_atr14, TREND_PARAMS["H4"]["band_atr"]
        )
    raise ValueError(f"unknown trend source: {trend!r}")


def _pullback_ok(s: Snapshot, side: int, params) -> bool:
    rsi = s.rsi14_5m
    if rsi is None:
        return False
    return rsi < params["lower"] if side > 0 else rsi > params["upper"]


def _breakout_ok(s: Snapshot, side: int, params) -> bool:
    if s.close_5m is None:
        return False
    if params["bars"] == ENTRY_PARAMS["B12"]["bars"]:
        high, low = s.high12_prev, s.low12_prev
    else:
        high, low = s.high24_prev, s.low24_prev
    if side > 0:
        return high is not None and s.close_5m > high
    return low is not None and s.close_5m < low


def _entry_ok(s: Snapshot, side: int, entry: str) -> bool:
    params = ENTRY_PARAMS[entry]
    if params["kind"] == "pullback":
        return _pullback_ok(s, side, params)
    return _breakout_ok(s, side, params)


def _strength_ok(s: Snapshot, side: int, strength: str) -> bool:
    params = STRENGTH_PARAMS[strength]
    if not params["enabled"]:
        return True
    if s.adx14_5m is None or not s.adx14_5m > params["adx_min"]:
        return False
    if not params["di_agrees"]:
        return True
    if s.di_plus14_5m is None or s.di_minus14_5m is None:
        return False
    if side > 0:
        return s.di_plus14_5m > s.di_minus14_5m
    return s.di_minus14_5m > s.di_plus14_5m


def _vol_ok(s: Snapshot, vol: str) -> bool:
    params = VOL_PARAMS[vol]
    if not params["enabled"]:
        return True
    if s.h1_vol_pct is None:
        return False
    if vol == "HIGH":
        return s.h1_vol_pct >= params["percentile"]
    return s.h1_vol_pct < params["percentile"]


def _vp_ok(s: Snapshot, side: int, entry: str, poc: PocFn) -> bool:
    """Volume-profile condition; ``poc()`` is only called here, once."""
    if s.close_5m is None:
        return False
    level = poc()
    if level is None:
        return False
    # pullback long / breakout short: price <= POC; the other two: price >= POC
    at_or_below = (side > 0) == (ENTRY_PARAMS[entry]["kind"] == "pullback")
    return s.close_5m <= level if at_or_below else s.close_5m >= level


def rule_signal(
    rule: RuleSpec,
    s: Snapshot,
    poc: PocFn,
    trend_override: Optional[int] = None,
) -> int:
    """Side of ``rule`` at ``s`` (+1, -1, 0).

    ``trend_override`` replaces the trend label (day-shift permutation test).
    ``poc`` is called at most once, and only after every other condition held.
    """
    side = trend_override if trend_override is not None else trend_label(s, rule.trend)
    if side == 0:
        return 0
    if not _entry_ok(s, side, rule.entry):
        return 0
    if not _strength_ok(s, side, rule.strength):
        return 0
    if not _vol_ok(s, rule.vol):
        return 0
    if VP_PARAMS[rule.vp]["enabled"] and not _vp_ok(s, side, rule.entry, poc):
        return 0
    return side



_TREND_FIELDS = {
    "E200": ("close_5m", "ema200_5m"),
    "H1": ("h1_ema20", "h1_ema50", "h1_atr14"),
    "H4": ("h4_ema20", "h4_ema50", "h4_atr14"),
}


def _dedupe(fields: Tuple[str, ...]) -> Tuple[str, ...]:
    return tuple(dict.fromkeys(fields))


def required_fields(rule: RuleSpec) -> Tuple[str, ...]:
    """Snapshot fields ``rule_signal(rule, ...)`` may read, for either side.

    With all of them set the rule can signal; each one is read on at least one
    side, where ``None`` makes the signal 0 (a breakout reads the prior high when
    long and the prior low when short). A volume-profile rule also needs the
    POC, which is not a snapshot field.
    """
    entry = ENTRY_PARAMS[rule.entry]
    fields = _TREND_FIELDS[rule.trend]
    if entry["kind"] == "pullback":
        fields += ("rsi14_5m",)
    elif entry["bars"] == ENTRY_PARAMS["B12"]["bars"]:
        fields += ("close_5m", "high12_prev", "low12_prev")
    else:
        fields += ("close_5m", "high24_prev", "low24_prev")
    strength = STRENGTH_PARAMS[rule.strength]
    if strength["enabled"]:
        fields += ("adx14_5m",)
        if strength["di_agrees"]:
            fields += ("di_plus14_5m", "di_minus14_5m")
    if VOL_PARAMS[rule.vol]["enabled"]:
        fields += ("h1_vol_pct",)
    if VP_PARAMS[rule.vp]["enabled"]:
        fields += ("close_5m",)
    return _dedupe(fields)


def contender2_required_fields() -> Tuple[str, ...]:
    """Snapshot fields ``contender2_signal`` reads (no trend override)."""
    return _TREND_FIELDS["H1"] + ("stretch_3m",)


def reference_required_fields() -> Tuple[str, ...]:
    """Snapshot fields ``reference_signal`` reads."""
    return ("close_5m", "ema125_5m", "rsi5_5m", "adx11_5m")


def contender2_signal(s: Snapshot, trend_override: Optional[int] = None) -> int:
    """H1 trend side when price sits within the near-mean band, else 0."""
    side = trend_override if trend_override is not None else trend_label(s, "H1")
    if side == 0 or s.stretch_3m is None:
        return 0
    if abs(s.stretch_3m) <= _NEAR_MEAN_MAX_ABS_STRETCH:
        return side
    return 0


def reference_signal(s: Snapshot) -> int:
    """Pine reference: EMA125 trend, RSI5 40/55 pullback, ADX(11) > 20."""
    if s.close_5m is None or s.ema125_5m is None:
        return 0
    side = _sign(s.close_5m - s.ema125_5m)
    if side == 0 or s.rsi5_5m is None:
        return 0
    if s.adx11_5m is None or not s.adx11_5m > _REF_ADX_MIN:
        return 0
    if side > 0 and s.rsi5_5m < _REF_RSI_LONG_BELOW:
        return 1
    if side < 0 and s.rsi5_5m > _REF_RSI_SHORT_ABOVE:
        return -1
    return 0
