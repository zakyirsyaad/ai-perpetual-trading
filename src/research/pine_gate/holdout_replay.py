"""Holdout replay: the period, the one-position clock, the contenders' signals
and the per-symbol metrics the test stage reads (spec 5.9, 5.10).

Research code; the bot never imports it. Standard library only.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .calibrate import Calibration
from .features import FeatureFrame
from .metalabel import MetaModel, candidate_features
from .pipeline import (
    PURGE_TICK_MINUTES,
    Dataset,
    _training_markets,
    contender2_fn,
    rule_signal_fn,
)
from .rules import contender2_signal, rule_signal, trend_label
from .simulate import Market, SignalFn, Trade, simulate, trading_days
from .spec import (
    ENTRIES,
    HOLDOUT_END,
    HOLDOUT_START,
    LAST20_END,
    LAST20_START,
    STRENGTHS,
    TRENDS,
    VOLS,
    VPS,
    ExecConfig,
    FillConfig,
    GateConfig,
    GuardConfig,
    RuleSpec,
    SelectionConfig,
    ms,
    spec_document,
)
from .stats import day_aggregates, mean_gross_bps, net_per_1m, win_rate, wr_lower_bound

_MS_PER_MIN = 60_000
_MS_PER_DAY = 24 * 60 * _MS_PER_MIN
_PCT = 100.0

PERMUTATION_K_MIN: int = spec_document()["controls"]["permutation_k_min"]  # k in [7, D - 7]
LB_Z = SelectionConfig().z
_FILL = FillConfig()
FEE_SENSITIVITY_PCT = _FILL.fee_sensitivity_pct
BOTH_TOUCH_REPORT_PCT = _FILL.both_touch_report_pct


# --------------------------------------------------------------------------- period


@dataclass(frozen=True)
class HoldoutPeriod:
    """The holdout [start, end) and its last-20-trading-days range, in UTC ms."""

    start_ms: int
    end_ms: int
    last20_start_ms: int
    last20_end_ms: int

    def __post_init__(self) -> None:
        if self.end_ms <= self.start_ms:
            raise ValueError(f"holdout {self.start_ms}..{self.end_ms} is empty")
        if not self.start_ms <= self.last20_start_ms < self.last20_end_ms <= self.end_ms:
            raise ValueError("the last-20-days range must lie inside the holdout")
        if not self.days() or not self.last20_days():
            raise ValueError("the holdout and its last-20-days range need a trading day each")

    @classmethod
    def spec(cls) -> "HoldoutPeriod":
        return cls(ms(HOLDOUT_START), ms(HOLDOUT_END), ms(LAST20_START), ms(LAST20_END))

    @property
    def mid_ms(self) -> int:
        return self.start_ms + (self.end_ms - self.start_ms) // 2

    def length_days(self) -> int:
        return (self.end_ms - self.start_ms) // _MS_PER_DAY

    def blocked_windows(self) -> Tuple[Tuple[int, int], ...]:
        """No entry tick in the final 65 minutes, so every trade exits before the end."""
        return ((self.end_ms - PURGE_TICK_MINUTES * _MS_PER_MIN, self.end_ms),)

    def days(self) -> List[date]:
        return trading_days(self.start_ms, self.end_ms)

    def last20_days(self) -> List[date]:
        return trading_days(self.last20_start_ms, self.last20_end_ms)

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


def shifted_time(t_ms: int, start_ms: int, days: int, k: int) -> int:
    """``t`` moved k whole days back, wrapped circularly into [start, start + days)."""
    return start_ms + (t_ms - start_ms - k * _MS_PER_DAY) % (days * _MS_PER_DAY)


def permutation_ks(seed: int, n: int, days: int) -> List[int]:
    """``n`` whole-day shifts drawn uniformly from [7, D - 7] with ``random.Random(seed)``."""
    high = days - PERMUTATION_K_MIN
    if high < PERMUTATION_K_MIN:
        raise ValueError(f"a {days}-day holdout leaves no shift in [7, D - 7]")
    rng = random.Random(seed)
    return [rng.randint(PERMUTATION_K_MIN, high) for _ in range(n)]


# --------------------------------------------------------------------------- simulation


def holdout_markets(dataset: Dataset, exec_cfg: ExecConfig) -> Tuple[Market, ...]:
    """Both markets in tick order with fresh outcome caches on ``exec_cfg``."""
    return _training_markets(dataset, exec_cfg)


def simulate_holdout(
    signal: SignalFn,
    markets: Sequence[Market],
    period: HoldoutPeriod,
    calib: Calibration,
    *,
    seed: int,
    exec_cfg: ExecConfig,
    guard_cfg: GuardConfig,
    overshoot_bps: Optional[float] = None,
    offset_s: int = 0,
    latencies_ms: Optional[Sequence[float]] = None,
) -> List[Trade]:
    """The one-position replay over the holdout with the final-65-minute block."""
    return simulate(
        signal, markets, period.start_ms, period.end_ms,
        exec_cfg=exec_cfg, guard_cfg=guard_cfg,
        latencies_ms=calib.latencies_ms if latencies_ms is None else latencies_ms,
        seed=seed,
        overshoot_bps=calib.s_star_bps if overshoot_bps is None else overshoot_bps,
        offset_s=offset_s, blocked_entry_windows=period.blocked_windows(),
    )


@dataclass(frozen=True)
class Holdout:
    """One holdout stage's inputs; ``run`` replays a signal with its defaults."""

    frames: Mapping[str, FeatureFrame]
    markets: Tuple[Market, ...]  # SYMBOLS order
    period: HoldoutPeriod
    calib: Calibration
    seed: int
    exec_cfg: ExecConfig
    guard_cfg: GuardConfig
    gate_cfg: GateConfig
    days: Tuple[date, ...]
    last20_days: Tuple[date, ...]

    def run(self, signal: SignalFn, **kwargs: Any) -> List[Trade]:
        """``simulate_holdout`` with this stage's defaults; ``kwargs`` override them."""
        markets = kwargs.pop("markets", self.markets)
        options = {"seed": self.seed, "exec_cfg": self.exec_cfg, "guard_cfg": self.guard_cfg}
        options.update(kwargs)
        return simulate_holdout(signal, markets, self.period, self.calib, **options)


def trades_of(trades: Sequence[Trade], symbol: str) -> List[Trade]:
    return [t for t in trades if t.symbol == symbol]


# --------------------------------------------------------------------------- contenders


def parse_rule(key: str) -> RuleSpec:
    """A ``RuleSpec`` from its ``key()`` form "trend|entry|strength|vol|vp"."""
    parts = str(key).split("|")
    allowed = (TRENDS, ENTRIES, STRENGTHS, VOLS, VPS)
    if len(parts) != len(allowed) or any(p not in a for p, a in zip(parts, allowed)):
        raise ValueError(f"not a rule key: {key!r}")
    return RuleSpec(*parts)


def contender3_fn(
    primary: RuleSpec, model: MetaModel, frames: Mapping[str, FeatureFrame]
) -> SignalFn:
    """The primary rule's side where the frozen model's probability >= its threshold."""

    def signal(symbol: str, t: int) -> int:
        frame = frames[symbol]
        s = frame.snapshot(t)
        side = rule_signal(primary, s, lambda: frame.poc_24h(t))
        if side == 0:
            return 0
        x = candidate_features(s, side, primary.trend, frame.poc_24h(t), t)
        if x is None:
            return 0
        return side if model.prob(x) >= model.threshold else 0

    return signal


def _shifted_label(frame: FeatureFrame, trend: str, t: int, period: HoldoutPeriod, k: int) -> int:
    return trend_label(frame.snapshot(shifted_time(t, period.start_ms, period.length_days(), k)),
                       trend)


def _shifted_rule(rule: RuleSpec, frames: Mapping[str, FeatureFrame], period: HoldoutPeriod):
    def make(k: int) -> SignalFn:
        def signal(symbol: str, t: int) -> int:
            frame = frames[symbol]
            label = _shifted_label(frame, rule.trend, t, period, k)
            return rule_signal(rule, frame.snapshot(t), lambda: frame.poc_24h(t), label)

        return signal

    return make


def _shifted_c2(frames: Mapping[str, FeatureFrame], period: HoldoutPeriod):
    def make(k: int) -> SignalFn:
        def signal(symbol: str, t: int) -> int:
            frame = frames[symbol]
            label = _shifted_label(frame, "H1", t, period, k)
            return contender2_signal(frame.snapshot(t), trend_override=label)

        return signal

    return make


@dataclass(frozen=True)
class Contender:
    name: str
    rule: Optional[RuleSpec]  # c1's frozen rule or c3's primary rule
    signal: SignalFn
    shifted: Optional[Callable[[int], SignalFn]] = None  # c1, c2: trend label k days back
    primary: Optional[SignalFn] = None  # c3: its primary rule on its own


def build_contenders(
    selection: Mapping[str, Any], frames: Mapping[str, FeatureFrame], period: HoldoutPeriod
) -> Tuple[List[Contender], List[str]]:
    """c1, c2, c3 in that order; an absent contender 1 or meta-model is skipped."""
    out: List[Contender] = []
    skipped: List[str] = []
    if selection.get("contender1") is not None:
        rule = parse_rule(selection["contender1"])
        out.append(Contender("c1", rule, rule_signal_fn(rule, frames),
                              _shifted_rule(rule, frames, period)))
    else:
        skipped.append("c1")
    out.append(Contender("c2", None, contender2_fn(frames), _shifted_c2(frames, period)))
    if selection.get("meta_model") is not None and selection.get("primary_rule") is not None:
        primary = parse_rule(selection["primary_rule"])
        model = MetaModel.from_dict(selection["meta_model"])
        out.append(Contender("c3", primary, contender3_fn(primary, model, frames),
                              primary=rule_signal_fn(primary, frames)))
    else:
        skipped.append("c3")
    return out, skipped


# --------------------------------------------------------------------------- metrics


def _both_touch(trades: Sequence[Trade], tp: int, sl: int) -> Dict[str, Any]:
    """Share of trades whose deciding bar touched both levels (resolved as stops);
    above 2 % also the WR upper bound with them counted as wins."""
    both = sum(1 for t in trades if t.both_touched)
    share = both / len(trades) if trades else None
    report = share is not None and _PCT * share > BOTH_TOUCH_REPORT_PCT
    upper = _PCT * (tp + both) / (tp + sl) if report and tp + sl else None
    return {"both_touch": both, "both_touch_share": share, "wr_upper_both_as_wins": upper}


def symbol_metrics(trades: Sequence[Trade], h: Holdout) -> Dict[str, Any]:
    """One symbol's holdout numbers from its trades in a run."""
    tp = sum(1 for t in trades if t.kind == "TP")
    sl = sum(1 for t in trades if t.kind == "SL")
    gross = mean_gross_bps(trades)
    p = h.period
    last20 = [t for t in trades if p.last20_start_ms <= t.entry_ms < p.last20_end_ms]
    return {
        "n": len(trades), "tp": tp, "sl": sl, "time": len(trades) - tp - sl,
        "wr": win_rate(trades),
        "lower_bound": wr_lower_bound(day_aggregates(trades, h.days), LB_Z),
        "gross_bps": gross,
        "net_per_1m": None if gross is None else net_per_1m(gross, h.exec_cfg.fee_pct),
        "net_per_1m_fee_sensitivity": (None if gross is None
                                       else net_per_1m(gross, FEE_SENSITIVITY_PCT)),
        "supply": len(trades) / len(h.days),
        "supply_last20": len(last20) / len(h.last20_days),
        "halves": [win_rate([t for t in trades if t.entry_ms < p.mid_ms]),
                   win_rate([t for t in trades if t.entry_ms >= p.mid_ms])],
        **_both_touch(trades, tp, sl),
    }


def brief_metrics(trades: Sequence[Trade], h: Holdout) -> Dict[str, Any]:
    return {"n": len(trades), "wr": win_rate(trades), "gross_bps": mean_gross_bps(trades),
            "supply": len(trades) / len(h.days)}


def mean_or_none(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else (a + b) / 2
