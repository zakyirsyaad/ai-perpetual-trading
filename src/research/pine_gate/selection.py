"""Two-stage rule selection on training data (spec section 5.6).

Stage 1 evaluates the 36 base rules; the best five by block-consistent rank
become the pool for Stage 2 (volatility x volume-profile variants). A rule is
only ever chosen when it is eligible (enough bracket exits, enough supply),
consistent across blocks, and sits in a stable area of the rule grid. PBO is
computed over every evaluated rule with enough bracket exits (``_sample_ok``) so
the report can show how much the search itself overfits; thin rules would win
in-sample halves by noise and inflate it. Research code; the bot never imports it. Standard library only.
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
    block_exit_days: Tuple[int, ...]  # WIB days with a TP or SL exit, per block B1-B4
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
    pbo: Optional[float]  # None with fewer than two rules passing _sample_ok
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
    *,
    z: float,
) -> RuleResult:
    """Reduce one rule's training trades to the numbers selection needs."""
    if len(block_bounds_ms) != len(block_days):
        raise ValueError("block_bounds_ms and block_days must have the same length")
    all_trades = tuple(trades)
    block_wr = []
    block_lb = []
    exit_days = []
    for (start, end), days in zip(block_bounds_ms, block_days):
        inside = [t for t in all_trades if start <= t.entry_ms < end]
        block_wr.append(win_rate(inside))
        block_lb.append(wr_lower_bound(day_aggregates(inside, days), z))
        exit_days.append(len({t.wib_day for t in inside if t.kind in ("TP", "SL")}))
    block_counts = _counts_in(all_trades, block_bounds_ms)
    n_tp_sl = sum(1 for t in all_trades if t.kind in ("TP", "SL"))
    return RuleResult(
        rule=rule,
        trades=all_trades,
        block_counts=block_counts,
        block_exit_days=tuple(exit_days),
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


def _thick_blocks(r: RuleResult, cfg: SelectionConfig) -> Tuple[bool, ...]:
    """Per block: at least ``block_min_tp_sl`` TP+SL trades on at least
    ``block_min_exit_days`` WIB days with a TP or SL exit."""
    return tuple(tp + sl >= cfg.block_min_tp_sl and days >= cfg.block_min_exit_days
                 for (tp, sl), days in zip(r.block_counts, r.block_exit_days))


def block_median_lb(r: RuleResult, cfg: SelectionConfig) -> float:
    """Median over B1-B4 of the block WR lower bound, without the WR vote; a thin
    block or one without a lower bound counts as -inf."""
    bounds = [lb if thick and lb is not None else -math.inf
              for lb, thick in zip(r.block_lb, _thick_blocks(r, cfg))]
    return statistics.median(bounds)


def rank_value(r: RuleResult, cfg: SelectionConfig) -> float:
    """``block_median_lb``; -inf unless at least ``blocks_above_half`` blocks have
    WR above ``block_min_wr``. A thin block (``_thick_blocks``) or one without a WR
    or lower bound counts as failing: no vote, lower bound -inf."""
    passing = sum(1 for wr, thick in zip(r.block_wr, _thick_blocks(r, cfg))
                  if thick and wr is not None and wr > cfg.block_min_wr)
    if passing < cfg.blocks_above_half:
        return -math.inf
    return block_median_lb(r, cfg)


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
            res = evaluate(rule)
            if res.rule != rule:
                raise ValueError(f"evaluate({key}) returned a result for {res.rule.key()}")
            cache[key] = res
        return cache[key]

    stage1 = stage1_rules()
    order = {r.key(): i for i, r in enumerate(stage1)}

    def ranked(rules: Sequence[RuleSpec]) -> list:
        """Best first; exact ties prefer the simpler rule (no filters, then
        Stage-1 order of the base, then RuleSpec order)."""
        scored = [(rank_value(cache[r.key()], cfg), r) for r in rules]
        scored = [(v, r) for v, r in scored if v != -math.inf]
        scored.sort(key=lambda vr: (
            -vr[0], vr[1].vol != "OFF", vr[1].vp != "OFF",
            order[_base_of(vr[1]).key()], vr[1],
        ))
        return scored

    for rule in stage1:
        run(rule)

    pool = [r for r in stage1 if eligible(cache[r.key()], cfg)]
    supply_limited = not pool
    if supply_limited:
        # Only the supply condition is dropped; the bracket-exit floor stays.
        pool = [r for r in stage1 if _sample_ok(cache[r.key()], cfg)]
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

    sampled = {k: r.cscv_counts for k, r in cache.items() if _sample_ok(r, cfg)}
    pbo = pbo_cscv(sampled) if len(sampled) >= 2 else None
    return Selection(
        choice=choice,
        top=top,
        stable=choice is not None,
        pbo=pbo,
        supply_limited=supply_limited,
        results=MappingProxyType(dict(cache)),
    )
