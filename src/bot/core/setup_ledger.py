"""The setup ledger: a per-bucket record of how entries filed under a setup actually came out.

An entry is filed under the side it takes, the regime condition the gate read for that hour and
the volatility regime of its setup (`bucket_key`). The ledger folds the realized gross outcome of
every closed trade in a bucket (in basis points of the entry price) into a mean, a standard error
and a one-sided 90 % upper bound, and compares that bound with a hurdle. A bucket whose upper bound
sits below the hurdle with enough samples is `blocked`; shadow outcomes of the blocked entries can
`reopen` it. Everything here is pure maths over rows already fetched: no I/O, no clock.

This module imports neither `bot.strategy.pine_indicators` nor `bot.strategy.modes`; only
`bot/core/regime_gate.py` may.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# One-sided 90 % normal quantile: the bound is mean + Z * SE.
Z_ONE_SIDED_90 = 1.28

BPS_PER_UNIT = 10_000.0
SHADOW_BLOCK_SOURCE = "ledger_block"

_LONG = "LONG"
_SHORT = "SHORT"

STATUS_LEARNING = "learning"
STATUS_OPEN = "open"
STATUS_BLOCKED = "blocked"
STATUS_REOPENED = "reopened"


def bucket_key(side: str, kondisi: str, atr_regime: str) -> str:
    """`"SHORT|TURUN|atr_mid"`: the side upper-cased, the condition and the ATR regime as given."""
    return f"{str(side).strip().upper()}|{kondisi}|{atr_regime}"


@dataclass(frozen=True)
class BucketStats:
    """Outcome statistics of one sample. `se_bps` and `upper_bps` are `None` below two values."""

    n: int
    mean_bps: float | None
    se_bps: float | None
    upper_bps: float | None


def gross_bps(direction: str, entry_price: float, exit_price: float) -> float:
    """Gross result in basis points of the entry price: positive when the position gained."""
    side = str(direction).strip().upper()
    if side == _LONG:
        move = exit_price - entry_price
    elif side == _SHORT:
        move = entry_price - exit_price
    else:
        raise ValueError(f"direction must be LONG or SHORT, got {direction!r}")
    return move / entry_price * BPS_PER_UNIT


def bucket_stats(values: Sequence[float]) -> BucketStats:
    """Mean, standard error (sample stdev / sqrt(n)) and `mean + Z_ONE_SIDED_90 * SE` of `values`.

    n = 0 has no mean; n = 1 has a mean but no spread, so no SE and no bound. Identical values give
    SE 0.0 and an upper bound equal to the mean, so a flat sample still decides.
    """
    n = len(values)
    if n == 0:
        return BucketStats(0, None, None, None)
    mean = statistics.mean(values)
    if n == 1:
        return BucketStats(1, mean, None, None)
    se = statistics.stdev(values) / math.sqrt(n)
    return BucketStats(n, mean, se, mean + Z_ONE_SIDED_90 * se)


def ledger_status(real: BucketStats, shadow: BucketStats, min_samples: int, hurdle_bps: float) -> str:
    """`learning`, `open`, `blocked` or `reopened` for one bucket.

    - `learning`: fewer than `min_samples` real outcomes (or no upper bound yet);
    - `open`: the real upper bound reaches the hurdle;
    - `reopened`: the real bound is below the hurdle, but at least `min_samples` shadow outcomes of
      the blocked entries have an upper bound at or above it;
    - `blocked`: the real bound is below the hurdle and the shadow evidence does not clear it.
    """
    if real.n < min_samples or real.upper_bps is None:
        return STATUS_LEARNING
    if real.upper_bps >= hurdle_bps:
        return STATUS_OPEN
    if shadow.n >= min_samples and shadow.upper_bps is not None and shadow.upper_bps >= hurdle_bps:
        return STATUS_REOPENED
    return STATUS_BLOCKED


def ledger_table(
    trade_rows: Iterable[Mapping[str, Any]],
    shadow_rows: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[BucketStats, BucketStats]]:
    """`{bucket_key: (real_stats, shadow_stats)}` for every bucket with at least one counted row.

    A trade row counts when `factor_metadata.setup.bucket_key` is set and its prices give a gross
    result. A shadow row counts only with `source == "ledger_block"` and a finite `gross_bps`
    (expired and invalid rows carry none). A bucket seen in only one source gets empty stats
    (`n == 0`) for the other.
    """
    real_values: dict[str, list[float]] = {}
    for row in trade_rows:
        key = _trade_bucket_key(row)
        value = _trade_gross_bps(row) if key else None
        if key and value is not None:
            real_values.setdefault(key, []).append(value)

    shadow_values: dict[str, list[float]] = {}
    for row in shadow_rows:
        key = _text(row.get("bucket_key"))
        value = _finite(row.get("gross_bps")) if row.get("source") == SHADOW_BLOCK_SOURCE else None
        if key and value is not None:
            shadow_values.setdefault(key, []).append(value)

    return {
        key: (bucket_stats(real_values.get(key, [])), bucket_stats(shadow_values.get(key, [])))
        for key in (*real_values, *(k for k in shadow_values if k not in real_values))
    }


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _trade_bucket_key(row: Mapping[str, Any]) -> str:
    metadata = row.get("factor_metadata")
    setup = metadata.get("setup") if isinstance(metadata, Mapping) else None
    return _text(setup.get("bucket_key")) if isinstance(setup, Mapping) else ""


def _trade_gross_bps(row: Mapping[str, Any]) -> float | None:
    """The row's gross result in bps, or `None` when its direction or prices cannot give one."""
    entry = _finite(row.get("entry_price"))
    exit_ = _finite(row.get("exit_price"))
    if entry is None or exit_ is None or entry <= 0 or exit_ <= 0:
        return None
    try:
        return gross_bps(str(row.get("direction") or ""), entry, exit_)
    except ValueError:
        return None
