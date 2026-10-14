"""Two-stage rule selection on training data (spec section 5.6).

Stage 1 evaluates the 36 base rules; the best five by block-consistent rank
become the pool for Stage 2 (volatility x volume-profile variants). A rule is
only ever chosen when it is eligible (enough bracket exits, enough supply),
consistent across blocks, and sits in a stable area of the rule grid. PBO is
computed over every evaluated rule so the report can show how much the search
itself overfits. Research code; the bot never imports it. Standard library only.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from .simulate import Trade
from .spec import (
    RuleSpec,
    SelectionConfig,
    stage1_neighbours,
    stage1_rules,
    stage2_variants,
)
from .stats import (
    day_aggregates,
    pbo_cscv,
    trades_per_trading_day,
    win_rate,
    wr_lower_bound,
)

Counts = Tuple[Tuple[int, int], ...]
TimeRange = Tuple[int, int]


@dataclass(frozen=True)
class RuleResult:
    rule: RuleSpec
    trades: Tuple[Trade, ...]
    block_counts: Counts  # (tp, sl) per block B1-B4
    cscv_counts: Counts  # (tp, sl) per CSCV block
    n_tp_sl: int
    supply_per_day: float
    wr: Optional[float]
    lower_bound: Optional[float]
    block_wr: Tuple[Optional[float], ...]  # one per B1-B4
    block_lb: Tuple[Optional[float], ...]  # one per B1-B4


@dataclass(frozen=True)
class Selection:
    choice: Optional[RuleSpec]
    top: Tuple[RuleSpec, ...]
    stable: bool
    pbo: float
    supply_limited: bool
    results: Mapping[str, RuleResult]


def _counts_in(trades: Sequence[Trade], bounds: Sequence[TimeRange]) -> Counts:
    """(tp, sl) per half-open range; a trade belongs to the range holding its entry."""
    out = []
    for start, end in bounds:
        tp = sl = 0
        for t in trades:
            if start <= t.entry_ms < end:
                if t.kind == "TP":
                    tp += 1
                elif t.kind == "SL":
                    sl += 1
        out.append((tp, sl))
    return tuple(out)


def summarize(
    rule: RuleSpec,
    trades: Sequence[Trade],
    block_bounds_ms: Sequence[TimeRange],
    cscv_bounds_ms: Sequence[TimeRange],
    block_days: Sequence[Sequence[date]],
    trading_days: Sequence[date],
    z: float = SelectionConfig.z,
) -> RuleResult:
    """Reduce one rule's training trades to the numbers selection needs."""
    if len(block_bounds_ms) != len(block_days):
        raise ValueError("block_bounds_ms and block_days must have the same length")
    all_trades = tuple(trades)
    block_wr = []
    block_lb = []
    for (start, end), days in zip(block_bounds_ms, block_days):
        inside = [t for t in all_trades if start <= t.entry_ms < end]
        block_wr.append(win_rate(inside))
        block_lb.append(wr_lower_bound(day_aggregates(inside, days), z))
    block_counts = _counts_in(all_trades, block_bounds_ms)
    n_tp_sl = sum(1 for t in all_trades if t.kind in ("TP", "SL"))
    return RuleResult(
        rule=rule,
        trades=all_trades,
        block_counts=block_counts,
        cscv_counts=_counts_in(all_trades, cscv_bounds_ms),
        n_tp_sl=n_tp_sl,
        supply_per_day=trades_per_trading_day(all_trades, trading_days),
        wr=win_rate(all_trades),
        lower_bound=wr_lower_bound(day_aggregates(all_trades, trading_days), z),
        block_wr=tuple(block_wr),
        block_lb=tuple(block_lb),
    )


def eligible(r: RuleResult, cfg: SelectionConfig) -> bool:
    return r.n_tp_sl >= cfg.min_tp_sl and r.supply_per_day >= cfg.min_supply_per_day


def _sample_ok(r: RuleResult, cfg: SelectionConfig) -> bool:
    """Eligibility with the supply condition dropped (supply-limited fallback)."""
    return r.n_tp_sl >= cfg.min_tp_sl


def rank_value(r: RuleResult, cfg: SelectionConfig) -> float:
    """Median over B1-B4 of the block WR lower bound; -inf unless at least
    ``blocks_above_half`` blocks have WR above ``block_min_wr``. A block
    without a WR or lower bound counts as failing."""
    passing = sum(1 for wr in r.block_wr if wr is not None and wr > cfg.block_min_wr)
    if passing < cfg.blocks_above_half:
        return -math.inf
    bounds = [-math.inf if lb is None else lb for lb in r.block_lb]
    return statistics.median(bounds)


def _stable(
    base: RuleSpec, results: Mapping[str, RuleResult], cfg: SelectionConfig
) -> bool:
    base_wr = results[base.key()].wr
    if base_wr is None:
        return False
    floor = base_wr - cfg.neighbour_tolerance_pts
    near = 0
    for n in stage1_neighbours(base):
        wr = results[n.key()].wr
        if wr is not None and wr >= floor:
            near += 1
    return near >= cfg.neighbour_min


def _base_of(rule: RuleSpec) -> RuleSpec:
    return RuleSpec(rule.trend, rule.entry, rule.strength)


def select(evaluate: Callable[[RuleSpec], RuleResult], cfg: SelectionConfig) -> Selection:
    """Pick contender 1's rule on training data; see the module docstring."""
    cache: Dict[str, RuleResult] = {}

    def run(rule: RuleSpec) -> RuleResult:
        key = rule.key()
        if key not in cache:
            cache[key] = evaluate(rule)
        return cache[key]

    def ranked(rules: Sequence[RuleSpec]) -> list:
        scored = [(rank_value(cache[r.key()], cfg), r) for r in rules]
        scored = [(v, r) for v, r in scored if v != -math.inf]
        scored.sort(key=lambda vr: (-vr[0], vr[1]))
        return scored

    stage1 = stage1_rules()
    for rule in stage1:
        run(rule)

    pool = [r for r in stage1 if eligible(cache[r.key()], cfg)]
    supply_limited = not pool
    if supply_limited:
        pool = list(stage1)
        passes = _sample_ok
    else:
        passes = eligible

    top = tuple(r for _, r in ranked(pool)[: cfg.top_n])
    candidates = list(top)
    for base in top:
        for variant in stage2_variants(base):
            if variant != base:
                run(variant)
                candidates.append(variant)
    candidates = [r for r in candidates if passes(cache[r.key()], cfg)]

    choice: Optional[RuleSpec] = None
    for _, rule in ranked(candidates):
        if _stable(_base_of(rule), cache, cfg):
            choice = rule
            break

    pbo = pbo_cscv({k: r.cscv_counts for k, r in cache.items()})
    return Selection(
        choice=choice,
        top=top,
        stable=choice is not None,
        pbo=pbo,
        supply_limited=supply_limited,
        results=MappingProxyType(dict(cache)),
    )
