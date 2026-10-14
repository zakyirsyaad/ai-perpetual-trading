"""Win-rate statistics for rule selection, the holdout gate and the report.

Research code; the bot never imports it. Standard library only.

Win rate (WR) is take-profit / (take-profit + stop-loss) in percent; time
stops carry no win/loss verdict and are left out of WR but counted in supply
and mean gross. Uncertainty is clustered by WIB day: trades on one day share
a regime, so days, not trades, are the independent units. Every random
procedure takes an explicit seed and draws from its own ``random.Random``.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date
from itertools import combinations
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .simulate import Trade

_PCT = 100.0
_BPS_PER_PCT = 100.0
_NET_SCALE = 50.0  # USDT per 1M volume per bp of gross (two sides of 0.5M notional)
_DEFAULT_FEE_PCT = 0.035
# A bootstrap std this small relative to the data scale is rounding noise
# from a constant series, not variation; such a rule is left out of the SPA max.
_OMEGA_REL_TOL = 1e-9


def _pooled_wr(tp: int, sl: int) -> Optional[float]:
    total = tp + sl
    return _PCT * tp / total if total > 0 else None


def win_rate(trades: Iterable[Trade]) -> Optional[float]:
    """TP / (TP + SL) in percent; None when no trade ended on the bracket."""
    tp = sl = 0
    for t in trades:
        if t.kind == "TP":
            tp += 1
        elif t.kind == "SL":
            sl += 1
    return _pooled_wr(tp, sl)


def mean_gross_bps(trades: Sequence[Trade]) -> Optional[float]:
    """Mean gross return in bps over every trade, time stops included."""
    if not trades:
        return None
    return sum(t.gross_bps for t in trades) / len(trades)


def net_per_1m(gross_bps: float, fee_pct: float = _DEFAULT_FEE_PCT) -> float:
    """Net USDT per 1M volume: 50 * (gross - round-trip fee in bps)."""
    return _NET_SCALE * (gross_bps - 2 * _BPS_PER_PCT * fee_pct)


def trades_per_trading_day(trades: Sequence[Trade], days: Sequence[date]) -> float:
    """All trades (whatever their day) divided by the number of trading days."""
    if not days:
        raise ValueError("trades_per_trading_day needs at least one trading day")
    return len(trades) / len(days)


@dataclass(frozen=True)
class DayAgg:
    day: date
    tp: int
    sl: int
    n: int  # every trade that day, time stops included
    gross_sum: float


def day_aggregates(trades: Iterable[Trade], days: Iterable[date]) -> List[DayAgg]:
    """One DayAgg per listed day (zero counts allowed) plus one per trade day
    not listed (e.g. a rare Saturday fill), sorted by day; nothing is dropped."""
    acc: Dict[date, List[float]] = {d: [0, 0, 0, 0.0] for d in days}
    for t in trades:
        row = acc.setdefault(t.wib_day, [0, 0, 0, 0.0])
        if t.kind == "TP":
            row[0] += 1
        elif t.kind == "SL":
            row[1] += 1
        row[2] += 1
        row[3] += t.gross_bps
    return [
        DayAgg(d, int(r[0]), int(r[1]), int(r[2]), float(r[3]))
        for d, r in sorted(acc.items())
    ]


def _informative(aggs: Iterable[DayAgg]) -> List[Tuple[int, int]]:
    """(tp, tp + sl) for days with at least one bracket exit."""
    return [(a.tp, a.tp + a.sl) for a in aggs if a.tp + a.sl > 0]


def wr_se_clustered(aggs: Iterable[DayAgg]) -> Optional[float]:
    """Day-clustered standard error of the pooled WR, in percentage points.

    Ratio estimator: with WR = sum(tp) / sum(m), m_d = tp_d + sl_d and
    e_d = tp_d - WR * m_d, var = D / (D - 1) * sum(e_d^2) / sum(m_d)^2.
    D counts only days with m_d > 0: a day without bracket exits has e_d = 0
    and carries no information, so it must not inflate the D / (D - 1) factor.
    None with fewer than two such days.
    """
    rows = _informative(aggs)
    if len(rows) < 2:
        return None
    total_m = sum(m for _, m in rows)
    wr = sum(tp for tp, _ in rows) / total_m
    sum_e2 = sum((tp - wr * m) ** 2 for tp, m in rows)
    d = len(rows)
    var = d / (d - 1) * sum_e2 / total_m**2
    return _PCT * math.sqrt(var)


def wr_lower_bound(aggs: Sequence[DayAgg], z: float = 2.0) -> Optional[float]:
    """Pooled WR - z * clustered SE; None if either is undefined."""
    se = wr_se_clustered(aggs)
    wr = _pooled_wr(sum(a.tp for a in aggs), sum(a.sl for a in aggs))
    if se is None or wr is None:
        return None
    return wr - z * se


def bootstrap_p_above(
    aggs: Sequence[DayAgg], baseline_wr: float, n: int = 10_000, seed: int = 0
) -> float:
    """One-sided day-block bootstrap p for H0: WR <= baseline_wr (percent).

    Days with bracket exits are resampled with replacement; the pooled WR of
    each resample is compared to the baseline, p = (1 + #{WR* <= baseline}) /
    (1 + n). Without any informative day there is no evidence: p = 1.
    """
    rows = _informative(aggs)
    if not rows:
        return 1.0
    rng = random.Random(seed)
    k = len(rows)
    hits = 0
    for _ in range(n):
        sample = rng.choices(rows, k=k)
        tp = sum(r[0] for r in sample)
        m = sum(r[1] for r in sample)
        if _PCT * tp / m <= baseline_wr:
            hits += 1
    return (1 + hits) / (1 + n)


def bootstrap_paired_p(
    a_aggs: Sequence[DayAgg], b_aggs: Sequence[DayAgg], n: int, seed: int
) -> float:
    """One-sided paired day-block bootstrap p for H0: WR_a <= WR_b.

    Both sides are aligned by day (a day missing on one side has zero counts
    there); days with no bracket exit on either side carry nothing and are
    dropped. The same day indices are resampled for both sides, p = (1 +
    #{WR_a* - WR_b* <= 0}) / (1 + n). A resample in which either side has no
    bracket exit counts as <= 0 (conservative).
    """
    by_day: Dict[date, List[int]] = {}
    for col, aggs in ((0, a_aggs), (2, b_aggs)):
        for x in aggs:
            row = by_day.setdefault(x.day, [0, 0, 0, 0])
            row[col] += x.tp
            row[col + 1] += x.tp + x.sl
    rows = [tuple(r) for _, r in sorted(by_day.items()) if r[1] + r[3] > 0]
    if not rows:
        return 1.0
    rng = random.Random(seed)
    k = len(rows)
    hits = 0
    for _ in range(n):
        sample = rng.choices(rows, k=k)
        tp_a = sum(r[0] for r in sample)
        m_a = sum(r[1] for r in sample)
        tp_b = sum(r[2] for r in sample)
        m_b = sum(r[3] for r in sample)
        if m_a == 0 or m_b == 0 or tp_a / m_a - tp_b / m_b <= 0:
            hits += 1
    return (1 + hits) / (1 + n)


def bootstrap_paired_block_p(
    a_aggs: Sequence[DayAgg], b_aggs: Sequence[DayAgg], n: int, seed: int, block_days: int
) -> float:
    """``bootstrap_paired_p`` with circular blocks of ``block_days`` consecutive days.

    Both sides are aligned on every listed day in calendar order (days without a
    bracket exit stay, so a block spans real consecutive days); each resample joins
    blocks starting at uniformly drawn days, wrapping at the end, cut to the series
    length. Trend regimes last weeks, so this is the sensitivity of a day bootstrap
    that treats days as independent (report only). Same p and conventions."""
    if type(block_days) is not int or block_days < 1:
        raise ValueError(f"block_days must be a positive integer, got {block_days!r}")
    by_day: Dict[date, List[int]] = {}
    for col, aggs in ((0, a_aggs), (2, b_aggs)):
        for x in aggs:
            row = by_day.setdefault(x.day, [0, 0, 0, 0])
            row[col] += x.tp
            row[col + 1] += x.tp + x.sl
    rows = [tuple(r) for _, r in sorted(by_day.items())]
    if not any(r[1] + r[3] for r in rows):
        return 1.0
    rng = random.Random(seed)
    k = len(rows)
    starts_per_sample = -(-k // block_days)
    hits = 0
    for _ in range(n):
        tp_a = m_a = tp_b = m_b = 0
        taken = 0
        for _ in range(starts_per_sample):
            start = rng.randrange(k)
            for j in range(min(block_days, k - taken)):
                r = rows[(start + j) % k]
                tp_a, m_a, tp_b, m_b = tp_a + r[0], m_a + r[1], tp_b + r[2], m_b + r[3]
            taken += block_days
        if m_a == 0 or m_b == 0 or tp_a / m_a - tp_b / m_b <= 0:
            hits += 1
    return (1 + hits) / (1 + n)


def holm(p: Mapping[str, float]) -> Dict[str, float]:
    """Holm step-down adjusted p-values (monotone, capped at 1), same keys."""
    for key, value in p.items():
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"p-value for {key!r} outside [0, 1]: {value}")
    ordered = sorted(p.items(), key=lambda kv: (kv[1], kv[0]))
    m = len(ordered)
    out: Dict[str, float] = {}
    running = 0.0
    for i, (key, value) in enumerate(ordered):
        running = max(running, min(1.0, (m - i) * value))
        out[key] = running
    return out


def permutation_p(observed: float, stats: Sequence[float]) -> float:
    """(1 + #{s >= observed}) / (1 + len(stats))."""
    return (1 + sum(1 for s in stats if s >= observed)) / (1 + len(stats))


def _validate_blocks(block_counts: Mapping[str, Sequence[Tuple[int, int]]]) -> int:
    if len(block_counts) < 2:
        raise ValueError("pbo_cscv needs at least two rules")
    lengths = {len(v) for v in block_counts.values()}
    if len(lengths) != 1:
        raise ValueError(f"every rule needs the same number of blocks, got {sorted(lengths)}")
    s = lengths.pop()
    if s < 2 or s % 2:
        raise ValueError(f"the number of blocks must be even and >= 2, got {s}")
    for key, blocks in block_counts.items():
        if any(tp < 0 or sl < 0 for tp, sl in blocks):
            raise ValueError(f"negative block count for rule {key!r}")
    return s


def _halves_wr(blocks: Sequence[Tuple[int, int]], idx: Iterable[int]) -> Optional[float]:
    tp = sl = 0
    for i in idx:
        tp += blocks[i][0]
        sl += blocks[i][1]
    return _pooled_wr(tp, sl)


def pbo_cscv(block_counts: Mapping[str, Sequence[Tuple[int, int]]]) -> float:
    """Probability of backtest overfitting by combinatorially symmetric
    cross-validation (Bailey, Borwein, Lopez de Prado & Zhu, 2017).

    ``block_counts`` maps each rule to its (tp, sl) counts per block, S blocks
    (even). For every split of the blocks into an in-sample (IS) and an
    out-of-sample (OOS) half, the rule with the best pooled IS WR is chosen
    (rules without IS bracket exits rank last; ties -> smallest key). Its OOS
    rank among all N rules (1 = worst; no OOS exits = lowest; ties -> average
    rank) gives omega = rank / (N + 1) and lambda = ln(omega / (1 - omega)).
    PBO is the share of splits with lambda <= 0.
    """
    s = _validate_blocks(block_counts)
    keys = sorted(block_counts)
    n_rules = len(keys)
    all_blocks = range(s)
    splits = list(combinations(all_blocks, s // 2))
    overfit = 0
    for is_idx in splits:
        oos_idx = [i for i in all_blocks if i not in is_idx]
        is_wr = {k: _halves_wr(block_counts[k], is_idx) for k in keys}
        best = min(keys, key=lambda k: (is_wr[k] is None, -(is_wr[k] or 0.0), k))
        oos = {}
        for k in keys:
            wr = _halves_wr(block_counts[k], oos_idx)
            oos[k] = -math.inf if wr is None else wr
        below = sum(1 for k in keys if oos[k] < oos[best])
        tied = sum(1 for k in keys if oos[k] == oos[best])  # includes best
        rank = below + (tied + 1) / 2
        omega = rank / (n_rules + 1)
        lam = math.log(omega / (1 - omega))
        if lam <= 0:
            overfit += 1
    return overfit / len(splits)


def _stationary_path(rng: random.Random, t: int, mean_block: float) -> List[Tuple[int, int]]:
    """One Politis-Romano stationary-bootstrap path of length t as (start,
    length) segments over the circularly wrapped series: each position starts
    a new block at a uniform index with probability 1 / mean_block, else
    continues the current block (geometric block lengths, wrap-around)."""
    p_new = 1.0 / mean_block
    segments: List[Tuple[int, int]] = []
    start, length = rng.randrange(t), 1
    for _ in range(1, t):
        if rng.random() < p_new:
            segments.append((start, length))
            start, length = rng.randrange(t), 1
        else:
            length += 1
    segments.append((start, length))
    return segments


def _validate_spa(daily_diff: Mapping[str, Sequence[float]], n: int, mean_block: float) -> int:
    if not daily_diff:
        raise ValueError("spa_pvalue needs at least one rule")
    lengths = {len(v) for v in daily_diff.values()}
    if len(lengths) != 1:
        raise ValueError(f"every series needs the same length, got {sorted(lengths)}")
    t = lengths.pop()
    if t < 2:
        raise ValueError(f"series length must be >= 2, got {t}")
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if mean_block < 1.0:
        raise ValueError(f"mean_block must be >= 1, got {mean_block}")
    return t


def _boot_means(series: Sequence[Sequence[float]], n: int, mean_block: float,
                seed: int) -> List[List[float]]:
    """``n`` stationary-bootstrap means per series, one shared index path per draw."""
    t = len(series[0])
    prefixes = []
    for x in series:
        acc = [0.0]
        for v in list(x) + list(x):
            acc.append(acc[-1] + v)
        prefixes.append(acc)
    rng = random.Random(seed)
    boot = [[0.0] * n for _ in series]
    for b in range(n):
        path = _stationary_path(rng, t, mean_block)
        for k, pre in enumerate(prefixes):
            boot[k][b] = sum(pre[s + length] - pre[s] for s, length in path) / t
    return boot


def _spa_active(series: Sequence[Sequence[float]], means: Sequence[float],
                boot: Sequence[Sequence[float]]) -> List[Tuple[int, float, float]]:
    """(k, omega_k, g_k) of every rule that is not numerically constant."""
    t, n = len(series[0]), len(boot[0])
    root_t = math.sqrt(t)
    threshold = math.sqrt(2 * math.log(math.log(t))) if t >= 3 else 0.0
    active = []
    for k, x in enumerate(series):
        omega = math.sqrt(sum((root_t * (v - means[k])) ** 2 for v in boot[k]) / n)
        scale = root_t * max(1.0, max(abs(v) for v in x))
        if omega <= _OMEGA_REL_TOL * scale:
            continue
        g = means[k] if root_t * means[k] / omega >= -threshold else 0.0
        active.append((k, omega, g))
    return active


def spa_pvalue(
    daily_diff: Mapping[str, Sequence[float]],
    n: int = 2000,
    mean_block: float = 5.0,
    seed: int = 0,
) -> float:
    """Hansen's consistent test for superior predictive ability.

    Hansen, P. R. (2005), "A Test for Superior Predictive Ability", Journal of
    Business & Economic Statistics 23(4), 365-380. ``daily_diff[k]`` is rule
    k's daily performance minus the benchmark's over the same T days; H0: no
    rule beats the benchmark (every E[d_k] <= 0).

    - d_k = mean of the series; a stationary bootstrap (Politis & Romano,
      1994; mean block ``mean_block``, wrap-around) draws n index paths with
      ``random.Random(seed)``, the same path applied to every rule.
    - omega_k^2 = (1/n) * sum_b (sqrt(T) * (d*_kb - d_k))^2, Hansen's bootstrap
      variance estimator. A rule with omega_k numerically zero (a constant
      series) is excluded from the maxima.
    - T_SPA = max(0, max_k sqrt(T) * d_k / omega_k).
    - Consistent recentring: g_k = d_k * 1{sqrt(T) * d_k / omega_k >=
      -sqrt(2 ln ln T)} (threshold 0 when T < 3), null draw Z*_kb = d*_kb - g_k:
      Hansen's d* - d + mu^c with mu^c = d * 1{sqrt(T) d / omega < -sqrt(2 ln ln T)}.
    - T*_b = max(0, max_k sqrt(T) * (d*_kb - g_k) / omega_k);
      p = (1 + #{T*_b >= T_SPA}) / (1 + n), never 0, like every other p here.
    """
    t = _validate_spa(daily_diff, n, mean_block)
    series = [list(map(float, daily_diff[k])) for k in sorted(daily_diff)]
    means = [sum(x) / t for x in series]
    boot = _boot_means(series, n, mean_block, seed)
    active = _spa_active(series, means, boot)
    root_t = math.sqrt(t)
    observed = max([0.0] + [root_t * means[k] / omega for k, omega, _ in active])
    hits = 0
    for b in range(n):
        stat = max([0.0] + [root_t * (boot[k][b] - g) / omega for k, omega, g in active])
        if stat >= observed:
            hits += 1
    return (1 + hits) / (1 + n)
