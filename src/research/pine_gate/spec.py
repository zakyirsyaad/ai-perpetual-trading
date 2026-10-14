"""Pre-registered constants for the Pine-adapted gate research (Phase 1).

Every number the evaluation depends on lives here. ``spec_sha256()`` is
recorded before any outcome is computed and later locks the holdout: the
holdout test refuses to run unless the selection file carries the same hash.
Research code; the bot never imports it. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple

SPEC_VERSION = "pine-gate-phase1-v1"

WIB = timezone(timedelta(hours=7))


def _wib(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=WIB)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def ms(dt: datetime) -> int:
    """UTC epoch milliseconds of a timezone-aware datetime (exact integer arithmetic)."""
    return (dt - _EPOCH) // timedelta(milliseconds=1)


def _freeze(value: Any) -> Any:
    """Recursively wrap mappings read-only and lists as tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _plain(value: Any) -> Any:
    """Recursively convert to JSON-native dicts and lists (a fresh copy)."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


# Segment convention: every [start, end) range is half-open. A trade belongs to
# a segment when its entry is >= start and < end; each end is the next start.
TRAIN_START = _wib(2025, 8, 1)
TRAIN_END = _wib(2026, 8, 5)
BLOCKS: Tuple[Tuple[datetime, datetime], ...] = (
    (TRAIN_START, _wib(2025, 11, 1)),
    (_wib(2025, 11, 1), _wib(2026, 2, 1)),
    (_wib(2026, 2, 1), _wib(2026, 5, 4)),
    (_wib(2026, 5, 4), TRAIN_END),
)
CSCV_BLOCKS = 8
HOLDOUT_START = TRAIN_END
HOLDOUT_END = _wib(2026, 10, 14)
# Wed 2026-09-16 .. Tue 2026-10-13 inclusive = 20 Mon-Fri WIB days.
LAST20_START = _wib(2026, 9, 16)
LAST20_END = HOLDOUT_END
PURGE_MINUTES = 60
EMBARGO_MINUTES = 60
# The one seed of every random draw (latency samples, controls, bootstraps) in a spec
# run; train and test refuse any other seed under the spec sha256.
SEED = 0


@dataclass(frozen=True)
class ExecConfig:
    bracket_pct: float = 0.20
    entry_slip_bps: float = 0.25
    tp_slip_bps: float = 0.25
    time_stop_min: int = 60
    fee_pct: float = 0.035
    tick_s: int = 180
    idle_tick_s: int = 2
    order_delay_s: int = 5
    cooldown_min: int = 5
    notional_usdt: float = 25_000.0
    funding_blackout_s: int = 600


@dataclass(frozen=True)
class GuardConfig:
    max_stops: int = 12
    max_consecutive_losses: int = 15
    max_drawdown_usdt: float = 1500.0
    max_entries: int = 34
    max_volume_usdt: float = 1_000_000.0


@dataclass(frozen=True)
class SelectionConfig:
    min_tp_sl: int = 1000
    min_supply_per_day: float = 10.0
    z: float = 2.0
    blocks_above_half: int = 3
    block_min_wr: float = 50.0
    # A block thinner than this neither votes nor bounds (lower bound -inf): its
    # day-clustered SE is unreliable with few exits.
    block_min_tp_sl: int = 60
    block_min_exit_days: int = 10
    top_n: int = 5
    neighbour_min: int = 4
    neighbour_tolerance_pts: float = 3.0
    pbo_max: float = 0.5
    vol_high_pct: float = 67.0
    vol_lookback_1h: int = 720
    vp_bin_bps: float = 5.0
    trend_band_atr: float = 0.25


@dataclass(frozen=True)
class ModelConfig:
    c_grid: Tuple[float, ...] = (0.01, 0.1, 1.0)
    threshold_hi: float = 0.70
    threshold_lo: float = 0.50
    threshold_step: float = 0.01
    fallback_threshold: float = 0.5
    threshold_min_supply_per_day: float = 10.0


@dataclass(frozen=True)
class GateConfig:
    min_tp_sl: int = 60
    alpha: float = 0.05
    min_wr: float = 57.0
    half_min_wr: float = 50.0
    robust_overshoot_bps: float = 2.0
    robust_min_wr: float = 55.0
    offsets: int = 20
    offsets_min_pass: int = 15
    min_supply_per_day: float = 10.0
    bootstrap_n: int = 10_000
    permutation_shifts: int = 1000
    c1_seeds: int = 200
    holm_family: int = 3


@dataclass(frozen=True)
class FillConfig:
    """Fill-calibration, latency and sensitivity policy (spec 5.10, 5.2)."""

    overshoot_source: str = "live stop_loss exits"
    overshoot_since: str = "2026-09-12"
    stop_level_inputs: Tuple[str, ...] = ("ai_stop", "atr_at_decision", "cap")
    stop_atr_multiple: float = 1.5
    stop_cap_pct: float = 0.4
    overshoot_statistic: str = "median"
    round_up_bps: float = 0.5
    floor_bps: float = 0.0  # after rounding: a stop never fills better than its level
    min_samples: int = 15
    fallback_bps: float = 1.0
    overshoot_sensitivity_bps: Tuple[float, ...] = (0.0, 1.0, 2.0)
    fee_sensitivity_pct: float = 0.05
    both_touch_report_pct: float = 2.0
    both_touch_upper_bound: str = "report with both-touch trades as wins when above threshold"
    eth_first_sensitivity: bool = True
    latency_table: str = "ai_call_metrics"
    latency_purpose: str = "entry"
    latency_success_only: bool = True
    latency_since: str = "2026-09-25"
    # Both calibration windows end at the holdout end (HOLDOUT_END, WIB midnight), so
    # a later calibrate run reads the same rows: [since, until) on created_at/opened_at.
    latency_until: str = "2026-10-14"
    overshoot_until: str = "2026-10-14"
    # the since and until dates are WIB calendar days (midnight at this offset), like
    # the decision-level view's 2026-08-31
    since_utc_offset: str = "+07:00"


@dataclass(frozen=True)
class DataConfig:
    """Stored-data checks (spec 5.2): funding times must come every
    ``funding_interval_s`` (BingX BTC/ETH: 8 h), with at most this much slack, over the
    whole loaded range (a day file holds exactly its own WIB day: ``bars.load_days``)."""

    funding_interval_s: int = 8 * 3600
    funding_gap_tolerance_s: int = 600


TRENDS = ("E200", "H1", "H4")
ENTRIES = ("P35", "P40", "B12", "B24")
STRENGTHS = ("OFF", "ADX", "ADX_DI")
VOLS = ("OFF", "HIGH", "NOT_HIGH")
VPS = ("OFF", "ON")

# What each rule label means. Rule and feature code reads these values; they
# are hashed through spec_document() so a changed parameter changes the spec.
SIGNAL_TIMEFRAME = "5m"

_SEL = SelectionConfig()

ENTRY_PARAMS = _freeze({
    "P35": {"kind": "pullback", "rsi_len": 14, "lower": 35, "upper": 65},
    "P40": {"kind": "pullback", "rsi_len": 14, "lower": 40, "upper": 60},
    "B12": {"kind": "breakout", "bars": 12},
    "B24": {"kind": "breakout", "bars": 24},
})
STRENGTH_PARAMS = _freeze({
    "OFF": {"enabled": False},
    "ADX": {"enabled": True, "dmi_len": 14, "adx_smoothing": 14, "adx_min": 20,
            "di_agrees": False},
    "ADX_DI": {"enabled": True, "dmi_len": 14, "adx_smoothing": 14, "adx_min": 20,
               "di_agrees": True},
})
TREND_PARAMS = _freeze({
    "E200": {"timeframe": "5m", "kind": "close_vs_ema", "ema_len": 200, "band_atr": 0.0},
    "H1": {"timeframe": "1h", "kind": "ema_cross", "ema_fast": 20, "ema_slow": 50,
           "atr_len": 14, "band_atr": _SEL.trend_band_atr},
    "H4": {"timeframe": "4h", "kind": "ema_cross", "ema_fast": 20, "ema_slow": 50,
           "atr_len": 14, "band_atr": _SEL.trend_band_atr},
})
VOL_PARAMS = _freeze({
    "OFF": {"enabled": False},
    "HIGH": {"enabled": True, "timeframe": "1h", "measure": "ATR14 / close",
             "atr_len": 14, "rank": "percent rank over previous closed bars",
             "lookback_1h": _SEL.vol_lookback_1h,
             "percentile": _SEL.vol_high_pct, "comparison": ">="},
    "NOT_HIGH": {"enabled": True, "timeframe": "1h", "measure": "ATR14 / close",
                 "atr_len": 14, "rank": "percent rank over previous closed bars",
                 "lookback_1h": _SEL.vol_lookback_1h,
                 "percentile": _SEL.vol_high_pct, "comparison": "<"},
})
VP_PARAMS = _freeze({
    "OFF": {"enabled": False},
    "ON": {"enabled": True, "window_hours": 24, "source": "production 1m volume",
           "bin_bps": _SEL.vp_bin_bps,
           "volume_spread": "evenly over the bins the bar's high-low range covers",
           "poc": "busiest bin", "poc_ties": "nearest bin to price",
           "price": "last closed 5m close",
           "pullback": {"long": "price <= POC", "short": "price >= POC"},
           "breakout": {"long": "price >= POC", "short": "price <= POC"}},
})

CONTENDER_PREFERENCE = ("c2", "c1", "c3")

CONTENDER3_FEATURES = (
    "trend_strength", "rsi_side", "adx", "di_diff_side",
    "vol_pct", "poc_dist_side", "hour_sin", "hour_cos",
)


@dataclass(frozen=True, order=True)
class RuleSpec:
    trend: str
    entry: str
    strength: str
    vol: str = "OFF"
    vp: str = "OFF"

    def key(self) -> str:
        return "|".join((self.trend, self.entry, self.strength, self.vol, self.vp))


def stage1_rules() -> Tuple[RuleSpec, ...]:
    """The 36 base rules, filters off."""
    return tuple(
        RuleSpec(trend, entry, strength)
        for trend in TRENDS
        for entry in ENTRIES
        for strength in STRENGTHS
    )


def stage2_variants(base: RuleSpec) -> Tuple[RuleSpec, ...]:
    """The 3 x 2 volatility x volume-profile variants of ``base`` (includes it)."""
    return tuple(
        RuleSpec(base.trend, base.entry, base.strength, vol, vp)
        for vol in VOLS
        for vp in VPS
    )


def stage1_neighbours(base: RuleSpec) -> Tuple[RuleSpec, ...]:
    """Rules differing from ``base`` in exactly one Stage-1 dimension (7)."""
    out = []
    for trend in TRENDS:
        if trend != base.trend:
            out.append(RuleSpec(trend, base.entry, base.strength))
    for entry in ENTRIES:
        if entry != base.entry:
            out.append(RuleSpec(base.trend, entry, base.strength))
    for strength in STRENGTHS:
        if strength != base.strength:
            out.append(RuleSpec(base.trend, base.entry, strength))
    return tuple(out)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _policies() -> Dict[str, Any]:
    return {
        "eligibility": "at least min_tp_sl take-profit + stop-loss trades over training and "
                       "combined supply of at least min_supply_per_day per trading day; "
                       "if no rule is eligible, drop the supply condition and report supply-limited",
        "selection_rank": "median over B1-B4 of WR - 2 * SE (SE clustered by WIB day); "
                          "WR > 50 in at least 3 of 4 blocks; a block with fewer than "
                          "block_min_tp_sl TP+SL trades or fewer than block_min_exit_days WIB "
                          "days with a TP or SL exit counts as failing (no WR vote, lower "
                          "bound -inf)",
        "stage_flow": "stage 1 -> 5 best base rules -> stage 2 variants -> best variant by the same ranking",
        "stable_area": "at least 4 of 7 stage-1 neighbours within 3 WR points below the chosen "
                       "base rule, else the next in rank",
        "overfitting": "PBO by CSCV on 8 CSCV blocks over the evaluated rules with at least "
                       "min_tp_sl TP+SL trades must be < 0.5; with fewer than two such rules "
                       "PBO is undefined and criterion 9 fails for c1 and c3; "
                       "Hansen SPA of each evaluated rule's direction value is reported: per "
                       "training trading day the mean over its trades of gross bps (rule side) "
                       "minus gross bps (seeded random side, one draw per trade, same entry "
                       "bar and bracket), 0 on a day without trades",
        "model_threshold": "highest p* at which the out-of-fold (B2-B4) one-position simulation "
                           "still delivers threshold_min_supply_per_day; none -> fallback_threshold",
        "meta_model_objective": "L2 logistic minimising sum(w * logloss) / sum(w) + "
                                "|beta|^2 / (2C), intercept unpenalised, w = uniqueness "
                                "weights, so C means the same at every sample size; the "
                                "selection records the B2-B4 supply at p* for the out-of-fold "
                                "and the final model's training probabilities",
        "contender_choice_order": ["most symbols passed", "higher pooled WR lower bound",
                                   "simpler (c2, c1, c3)"],
        "c1_c3_must_beat_c2_holdout_wr": True,
        "holm": "Holm correction across the 3 contenders within each symbol; an absent "
                "contender counts with p = 1",
        "c0_training": "always long and always short per symbol on training (same clock, "
                       "offset 0); outside c0 tolerances the test stage refuses to run",
        "holdout_lock": "under the spec sha256 the test runs once, next to its selection file "
                        "(links resolved), on the code (code_sha256) and the training data "
                        "(train_data_sha256) and the Python major.minor (python_version) the "
                        "selection was made with, with the spec seed; every data check "
                        "that can refuse (the files checked without computing anything, the "
                        "training part hashed from a training-only load) comes before the "
                        "exclusive run marker <data_dir>/holdout_runs/<spec_sha256>.json, "
                        "which is written before the holdout is loaded and refuses every "
                        "later run",
        "contender3_primary_fallback": "every Stage-1 rule ranking -inf: median block lower "
                                       "bound without the vote (none last) -> pooled WR -> "
                                       "Stage-1 order",
        "eth_first_sensitivity": True,
    }


def _segments() -> Dict[str, Any]:
    return {
        "train_start": _iso(TRAIN_START),
        "train_end": _iso(TRAIN_END),
        "blocks": [[_iso(a), _iso(b)] for a, b in BLOCKS],
        "cscv_blocks": CSCV_BLOCKS,
        "holdout_start": _iso(HOLDOUT_START),
        "holdout_end": _iso(HOLDOUT_END),
        "last20_start": _iso(LAST20_START),
        "last20_end": _iso(LAST20_END),
        "purge_minutes": PURGE_MINUTES,
        "embargo_minutes": EMBARGO_MINUTES,
    }


def _contenders_and_controls() -> Dict[str, Any]:
    """The contender 2 / 3, reference and control sections of the document."""
    return {
        "contender2": {
            "near_mean_max_abs_stretch_atr": 0.75,
            "closed_3m_bars": 39,
            "ema_period": 21,
            "atr_period": 14,
            "implementation": "bot.strategy.indicators + build_setup_profile",
            "direction": {
                "source": "H1",
                "ema_fast": 20,
                "ema_slow": 50,
                "band_atr": 0.25,
                "atr_period": 14,
                "neutral": "no entry",
            },
            "tuning": "none",
        },
        "contender3": {
            "primary_rule": "chosen trend + entry of contender 1, strength OFF, no filters",
            "fallback_primary": "best-ranked Stage-1 base rule; when every Stage-1 rule "
                                "ranks -inf: the highest median block lower bound without the "
                                "vote (none last), then the higher pooled WR, then the simpler "
                                "rule (Stage-1 order); no primary only when no Stage-1 rule has "
                                "any TP+SL",
            "features": list(CONTENDER3_FEATURES),
            "label": "bracket trade gross return > 0",
            "model": "L2 logistic regression",
            "sample_weights": "uniqueness (inverse average concurrency)",
            "standardisation": "training means and standard deviations",
        },
        "reference": {
            "trend": "EMA125 on 5m",
            "rsi": {"period": 5, "long_below": 40, "short_above": 55},
            "adx": {"length": 11, "smoothing": 11, "min": 20, "via": "ta.dmi(11, 11)"},
        },
        "controls": {
            "c0": "always long and always short on the same tick clock",
            "c0_wr_tolerance_pts": 3.0,
            "c0_gross_tolerance_bps": 2.0,
            "c1": "contender entry times with a random side",
            "c1_seeds": GateConfig().c1_seeds,
            "permutation": "trend-label series shifted by whole days",
            "permutation_shifts": GateConfig().permutation_shifts,
            "permutation_k_min": 7,
            "permutation_k_max": "D - 7",
        },
    }


def spec_document() -> Dict[str, Any]:
    """Every pre-registered constant as plain JSON data; a fresh dict per call."""
    doc = {
        "version": SPEC_VERSION,
        "seed": SEED,
        "exec": asdict(ExecConfig()),
        "guards": asdict(GuardConfig()),
        "selection": asdict(SelectionConfig()),
        "model": asdict(ModelConfig()),
        "gate": asdict(GateConfig()),
        "fill": asdict(FillConfig()),
        "data": asdict(DataConfig()),
        "rules": {
            "signal_timeframe": SIGNAL_TIMEFRAME,
            "entry": ENTRY_PARAMS,
            "strength": STRENGTH_PARAMS,
            "trend": TREND_PARAMS,
            "vol": VOL_PARAMS,
            "vp": VP_PARAMS,
        },
        "policies": _policies(),
        "grid": {
            "trends": list(TRENDS),
            "entries": list(ENTRIES),
            "strengths": list(STRENGTHS),
            "vols": list(VOLS),
            "vps": list(VPS),
            "stage1_rules": len(stage1_rules()),
            "stage2_variants_per_base": len(VOLS) * len(VPS),
        },
        "segments": _segments(),
        **_contenders_and_controls(),
    }
    return _plain(doc)


def spec_sha256(doc: Optional[Dict[str, Any]] = None) -> str:
    """sha256 of the canonical JSON of ``doc`` (default: the current spec)."""
    if doc is None:
        doc = spec_document()
    payload = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
