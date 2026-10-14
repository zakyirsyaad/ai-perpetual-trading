"""Contender 3: a meta-labeling logistic model over a primary rule's candidates.

Spec 5.8. Each candidate is a tick where the primary rule would enter; its
label is 1 when that candidate's bracket trade ends with a positive gross
return. An L2 logistic regression on eight side-signed features, weighted by
uniqueness, decides which candidates may enter.

Leakage controls (spec 5.3): the penalty C is chosen by expanding-window
validation (fit on blocks before k, validate on block k, for k = B2..B4). Each
fold fits its own scaler on its own training candidates only, drops training
trades that end after the validation block starts (purge), and skips
validation candidates in the first ``EMBARGO_MINUTES`` of the block (embargo).

The frozen ``MetaModel`` is plain data: in the bot it is a dot product and a
sigmoid. Block numbers are 0-based indexes into ``spec.BLOCKS`` (0 = B1).
Research code; the bot never imports it. Standard library only.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .bars import wib_hour
from .features import Snapshot
from .spec import BLOCKS, CONTENDER3_FEATURES, EMBARGO_MINUTES, TRENDS, ModelConfig, ms

FEATURES: Tuple[str, ...] = CONTENDER3_FEATURES

_MS_PER_MIN = 60_000
_LOGLOSS_EPS = 1e-12
_MAX_HALVINGS = 40
# The threshold of a model inside cross-validation is never read; train_meta
# replaces it with the scanned value.
_UNSCANNED_THRESHOLD = 0.5

Key = Tuple[str, int]


# --------------------------------------------------------------------------- features


def _trend_strength(s: Snapshot, trend: str) -> Optional[float]:
    if trend == "E200":
        diff = _diff(s.close_5m, s.ema200_5m)
        atr = s.atr14_5m
    elif trend == "H1":
        diff = _diff(s.h1_ema20, s.h1_ema50)
        atr = s.h1_atr14
    else:
        diff = _diff(s.h4_ema20, s.h4_ema50)
        atr = s.h4_atr14
    if diff is None or atr is None or atr <= 0:
        return None
    return diff / atr


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a - b


def candidate_features(
    s: Snapshot, side: int, trend: str, poc: Optional[float], t_ms: int
) -> Optional[Tuple[float, ...]]:
    """The 8 features of ``FEATURES`` for a candidate, signed by ``side``.

    ``None`` when any needed input is missing or an ATR is not positive.
    """
    if side not in (1, -1):
        raise ValueError(f"side must be 1 or -1, got {side!r}")
    if trend not in TRENDS:
        raise ValueError(f"unknown trend source {trend!r}")
    strength = _trend_strength(s, trend)
    inputs = (s.close_5m, s.atr14_5m, s.rsi14_5m, s.adx14_5m,
              s.di_plus14_5m, s.di_minus14_5m, s.h1_vol_pct, poc)
    if strength is None or any(v is None for v in inputs) or s.atr14_5m <= 0:
        return None
    angle = 2.0 * math.pi * wib_hour(t_ms) / 24.0
    return (
        side * strength,
        s.rsi14_5m if side > 0 else 100.0 - s.rsi14_5m,
        s.adx14_5m,
        side * (s.di_plus14_5m - s.di_minus14_5m),
        s.h1_vol_pct,
        side * (s.close_5m - poc) / s.atr14_5m,
        math.sin(angle),
        math.cos(angle),
    )


@dataclass(frozen=True)
class Candidate:
    """One primary-rule entry candidate with its bracket-trade label."""

    symbol: str
    t_ms: int
    side: int
    x: Tuple[float, ...]
    label: int
    start_ms: int  # entry fill time
    end_ms: int  # exit time
    block: int  # 0-based index into spec.BLOCKS

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError(f"side must be 1 or -1, got {self.side!r}")
        if self.label not in (0, 1):
            raise ValueError(f"label must be 0 or 1, got {self.label!r}")
        if len(self.x) != len(FEATURES):
            raise ValueError(f"x needs {len(FEATURES)} features, got {len(self.x)}")
        if not 0 <= self.block < len(BLOCKS):
            raise ValueError(f"block must be in 0..{len(BLOCKS) - 1}, got {self.block!r}")
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms is before start_ms")
        object.__setattr__(self, "x", tuple(float(v) for v in self.x))


# --------------------------------------------------------------------------- weights


def uniqueness_weights(intervals: Sequence[Tuple[int, int]]) -> List[float]:
    """Mean over each interval's minutes of 1 / concurrency.

    Intervals are half-open ``[start, end)`` in ms, floored to whole minutes.
    An interval with no minutes gets weight 1.0 and adds to no concurrency.
    """
    spans = []
    for start, end in intervals:
        if end < start:
            raise ValueError(f"interval ends before it starts: {(start, end)}")
        spans.append((start // _MS_PER_MIN, end // _MS_PER_MIN))
    starts = sorted(a for a, b in spans if b > a)
    ends = sorted(b for a, b in spans if b > a)
    points = sorted(set(starts) | set(ends))
    # cum[i] = sum of 1/concurrency over minutes before points[i].
    cum = [0.0]
    for left, right in zip(points, points[1:]):
        active = bisect_right(starts, left) - bisect_right(ends, left)
        cum.append(cum[-1] + ((right - left) / active if active > 0 else 0.0))
    index = {p: i for i, p in enumerate(points)}
    return [
        (cum[index[b]] - cum[index[a]]) / (b - a) if b > a else 1.0
        for a, b in spans
    ]


# --------------------------------------------------------------------------- logistic


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _softplus(z: float) -> float:
    return max(z, 0.0) + math.log1p(math.exp(-abs(z)))


def _log_loss(prob: float, label: int) -> float:
    p = min(max(prob, _LOGLOSS_EPS), 1.0 - _LOGLOSS_EPS)
    return -math.log(p) if label == 1 else -math.log(1.0 - p)


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(u * v for u, v in zip(a, b))


def _objective(rows, y, w, C, theta) -> float:
    """sum(w * logloss) / sum(w) + |beta|^2 / (2C); the intercept (last) is unpenalised.

    The data term is a weighted mean, so C regularises a fold-sized and a full-sized
    fit alike (``w`` holds the weights pre-divided by their sum, see ``fit_logistic``)."""
    data = sum(wi * (_softplus(z) - yi * z)
               for z, yi, wi in ((_dot(r, theta), yi, wi) for r, yi, wi in zip(rows, y, w)))
    return data + sum(b * b for b in theta[:-1]) / (2.0 * C)


def _gradient_and_hessian(rows, y, w, C, theta):
    """Gradient and Hessian of ``_objective`` (``w`` already divided by its sum)."""
    n = len(theta)
    grad = [0.0] * n
    hess = [[0.0] * n for _ in range(n)]
    for r, yi, wi in zip(rows, y, w):
        p = _sigmoid(_dot(r, theta))
        g = wi * (p - yi)
        h = wi * p * (1.0 - p)
        for i in range(n):
            ri = r[i]
            grad[i] += g * ri
            hrow = hess[i]
            hri = h * ri
            for j in range(i + 1):
                hrow[j] += hri * r[j]
    for i in range(n):
        for j in range(i):
            hess[j][i] = hess[i][j]
    for i in range(n - 1):
        grad[i] += theta[i] / C
        hess[i][i] += 1.0 / C
    return grad, hess


def _solve(a: List[List[float]], b: List[float]) -> List[float]:
    """Gaussian elimination with partial pivoting (copies its inputs)."""
    n = len(b)
    m = [list(row) + [rhs] for row, rhs in zip(a, b)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if m[pivot][col] == 0.0:
            raise ArithmeticError("singular Newton system")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            factor = m[r][col] / m[col][col]
            if factor:
                for c in range(col, n + 1):
                    m[r][c] -= factor * m[col][c]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (m[r][n] - _dot(m[r][r + 1:n], x[r + 1:])) / m[r][r]
    return x


def fit_logistic(
    X: Sequence[Sequence[float]],
    y: Sequence[int],
    w: Sequence[float],
    C: float,
    max_iter: int = 50,
    tol: float = 1e-8,
) -> Tuple[Tuple[float, ...], float]:
    """Newton (IRLS) fit of an L2 logistic regression; returns (coef, intercept).

    Minimises sum(w * logloss) / sum(w) + |beta|^2 / (2C) with the intercept
    unpenalised, so C means the same at every sample size and weight scale. Each
    Newton step is halved until the objective does not increase; the fit stops when
    the largest parameter change is below ``tol``.
    """
    if not X:
        raise ValueError("no samples")
    if len(y) != len(X) or len(w) != len(X):
        raise ValueError("X, y and w must have the same length")
    if not C > 0:
        raise ValueError(f"C must be positive, got {C!r}")
    width = len(X[0])
    if any(len(row) != width for row in X):
        raise ValueError("every row of X needs the same number of features")
    total = sum(w)
    if not (total > 0 and math.isfinite(total)) or any(not wi >= 0 for wi in w):
        raise ValueError("weights must be non-negative with a positive finite sum")
    w = [wi / total for wi in w]  # the data term becomes the weighted mean log-loss
    rows = [tuple(float(v) for v in row) + (1.0,) for row in X]
    theta = [0.0] * (width + 1)
    current = _objective(rows, y, w, C, theta)
    for _ in range(max_iter):
        grad, hess = _gradient_and_hessian(rows, y, w, C, theta)
        step = _solve(hess, grad)
        scale = 1.0
        for _ in range(_MAX_HALVINGS):
            trial = [t - scale * s for t, s in zip(theta, step)]
            value = _objective(rows, y, w, C, trial)
            if value <= current:
                break
            scale /= 2.0
        else:
            break  # no step decreases the objective: at the optimum numerically
        theta, current = trial, value
        if max(abs(scale * s) for s in step) < tol:
            break
    return tuple(theta[:-1]), theta[-1]


# --------------------------------------------------------------------------- model


@dataclass(frozen=True)
class MetaModel:
    """Frozen meta-labeling model: scaler, coefficients, penalty and threshold."""

    coef: Tuple[float, ...]
    intercept: float
    means: Tuple[float, ...]
    stds: Tuple[float, ...]
    threshold: float
    C: float

    def __post_init__(self) -> None:
        for name in ("coef", "means", "stds"):
            values = tuple(float(v) for v in getattr(self, name))
            if len(values) != len(FEATURES):
                raise ValueError(f"{name} needs {len(FEATURES)} values, got {len(values)}")
            object.__setattr__(self, name, values)
        if any(s < 0 for s in self.stds):
            raise ValueError("stds must be non-negative")
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1], got {self.threshold!r}")
        if not self.C > 0:
            raise ValueError(f"C must be positive, got {self.C!r}")
        object.__setattr__(self, "intercept", float(self.intercept))
        object.__setattr__(self, "threshold", float(self.threshold))
        object.__setattr__(self, "C", float(self.C))

    def prob(self, x: Sequence[float]) -> float:
        """P(label = 1) for raw features ``x``; a zero std scales by 1."""
        if len(x) != len(FEATURES):
            raise ValueError(f"x needs {len(FEATURES)} features, got {len(x)}")
        z = self.intercept + sum(
            b * (v - m) / (s if s > 0 else 1.0)
            for b, v, m, s in zip(self.coef, x, self.means, self.stds)
        )
        return _sigmoid(z)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "features": list(FEATURES),
            "coef": list(self.coef),
            "intercept": self.intercept,
            "means": list(self.means),
            "stds": list(self.stds),
            "threshold": self.threshold,
            "C": self.C,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "MetaModel":
        if list(d["features"]) != list(FEATURES):
            raise ValueError(f"feature order {d['features']!r} differs from {list(FEATURES)!r}")
        return cls(
            coef=tuple(d["coef"]),
            intercept=d["intercept"],
            means=tuple(d["means"]),
            stds=tuple(d["stds"]),
            threshold=d["threshold"],
            C=d["C"],
        )


def _fit_candidates(train: Sequence[Candidate], C: float) -> MetaModel:
    """Scaler and uniqueness weights from ``train`` alone, then the L2 fit."""
    n = len(train)
    cols = list(zip(*(c.x for c in train)))
    means = [sum(col) / n for col in cols]
    stds = [math.sqrt(sum((v - m) ** 2 for v in col) / n) for col, m in zip(cols, means)]
    scale = [s if s > 0 else 1.0 for s in stds]
    X = [[(v - m) / s for v, m, s in zip(c.x, means, scale)] for c in train]
    w = uniqueness_weights([(c.start_ms, c.end_ms) for c in train])
    coef, intercept = fit_logistic(X, [c.label for c in train], w, C)
    return MetaModel(coef, intercept, tuple(means), tuple(stds), _UNSCANNED_THRESHOLD, C)


# --------------------------------------------------------------------------- validation


def _fold(candidates: Sequence[Candidate], k: int) -> Tuple[List[Candidate], List[Candidate]]:
    """Training (blocks < k, purged) and validation (block k, embargoed) of fold k."""
    start = ms(BLOCKS[k][0])
    train = [c for c in candidates if c.block < k and c.end_ms <= start]
    embargo_end = start + EMBARGO_MINUTES * _MS_PER_MIN
    valid = [c for c in candidates if c.block == k and c.t_ms >= embargo_end]
    if not train or not valid:
        raise ValueError(f"fold {k}: {len(train)} training, {len(valid)} validation candidates")
    return train, valid


def _cross_validate(candidates: Sequence[Candidate], c_grid: Sequence[float]):
    """Per-C fold log-losses, fold sizes and out-of-fold (candidate, prob) pairs."""
    losses: Dict[float, List[float]] = {C: [] for C in c_grid}
    oof: Dict[float, List[Tuple[Candidate, float]]] = {C: [] for C in c_grid}
    sizes: List[int] = []
    for k in range(1, len(BLOCKS)):
        train, valid = _fold(candidates, k)
        sizes.append(len(valid))
        for C in c_grid:
            model = _fit_candidates(train, C)
            probs = [model.prob(c.x) for c in valid]
            losses[C].append(sum(_log_loss(p, c.label) for p, c in zip(probs, valid)) / len(valid))
            oof[C].extend(zip(valid, probs))
    return losses, sizes, oof


def _validated_grid(c_grid: Sequence[float]) -> Tuple[float, ...]:
    grid = tuple(c_grid)
    if not grid or any(not C > 0 for C in grid):
        raise ValueError(f"c_grid needs positive values, got {grid!r}")
    return grid


def _best_C(losses: Mapping[float, Sequence[float]], sizes: Sequence[int]) -> float:
    """Lowest pooled (size-weighted) log-loss; ties go to the smaller C. ``ValueError``
    when no C has a finite score."""
    total = sum(sizes)
    best, best_score = None, math.inf
    for C in sorted(losses):
        score = sum(n * v for n, v in zip(sizes, losses[C])) / total
        if math.isfinite(score) and score < best_score:
            best, best_score = C, score
    if best is None:
        raise ValueError(f"no penalty C has a finite validation log-loss: {dict(losses)!r}")
    return best


def choose_C(
    candidates: Sequence[Candidate], c_grid: Sequence[float]
) -> Tuple[float, Dict[float, List[float]]]:
    """Penalty by expanding-window validation over B1-B4; also the per-fold losses."""
    grid = _validated_grid(c_grid)
    losses, sizes, _ = _cross_validate(candidates, grid)
    return _best_C(losses, sizes), losses


def reliability(
    probs: Sequence[float], labels: Sequence[int], bins: int = 10
) -> List[Tuple[float, float, int]]:
    """(mean predicted, observed rate, n) per non-empty equal-width bin on [0, 1]."""
    if len(probs) != len(labels):
        raise ValueError("probs and labels must have the same length")
    if bins < 1:
        raise ValueError("bins must be at least 1")
    sums = [[0.0, 0, 0] for _ in range(bins)]
    for p, label in zip(probs, labels):
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"probability out of [0, 1]: {p!r}")
        if label not in (0, 1):
            raise ValueError(f"label must be 0 or 1, got {label!r}")
        cell = sums[min(int(p * bins), bins - 1)]
        cell[0] += p
        cell[1] += label
        cell[2] += 1
    return [(s / n, hits / n, n) for s, hits, n in sums if n]


def choose_threshold(supply_at: Callable[[float], float], cfg: ModelConfig) -> float:
    """First p from threshold_hi down to threshold_lo whose supply meets the floor."""
    if cfg.threshold_step <= 0 or cfg.threshold_hi < cfg.threshold_lo:
        raise ValueError("threshold scan needs hi >= lo and a positive step")
    steps = int(round((cfg.threshold_hi - cfg.threshold_lo) / cfg.threshold_step))
    for i in range(steps + 1):
        p = round(cfg.threshold_hi - i * cfg.threshold_step, 10)
        if supply_at(p) >= cfg.threshold_min_supply_per_day:
            return p
    return cfg.fallback_threshold


def train_meta(
    candidates: Sequence[Candidate],
    supply_for: Callable[[Mapping[Key, float], float], float],
    cfg: ModelConfig,
) -> Tuple[MetaModel, Dict[str, Any]]:
    """Choose C, scan the threshold on out-of-fold probabilities, refit on all.

    ``supply_for(oof_probs, p)`` returns the trades per trading day of the
    caller's one-position simulation when only out-of-fold candidates with
    probability >= p may enter. The diagnostics also record that supply at the
    chosen threshold for the final model's own probabilities on its training
    candidates (no labels), so the refit's supply sits next to the validated one.
    """
    grid = _validated_grid(cfg.c_grid)
    keys = [(c.symbol, c.t_ms) for c in candidates]
    if len(set(keys)) != len(keys):
        raise ValueError("candidates must have unique (symbol, t_ms) keys")
    losses, sizes, oof_by_c = _cross_validate(candidates, grid)
    chosen = _best_C(losses, sizes)
    pairs = oof_by_c[chosen]
    oof = MappingProxyType({(c.symbol, c.t_ms): p for c, p in pairs})
    threshold = choose_threshold(lambda p: supply_for(oof, p), cfg)
    model = replace(_fit_candidates(list(candidates), chosen), threshold=threshold)
    final = MappingProxyType({(c.symbol, c.t_ms): model.prob(c.x) for c in candidates})
    diagnostics = {
        "cv_logloss": losses,
        "fold_sizes": list(sizes),
        "chosen_C": chosen,
        "oof": [[c.symbol, c.t_ms, p, c.label] for c, p in pairs],
        "reliability": reliability([p for _, p in pairs], [c.label for c, _ in pairs]),
        "supply_oof_at_threshold": supply_for(oof, threshold),
        "supply_final_at_threshold": supply_for(final, threshold),
    }
    return model, diagnostics
