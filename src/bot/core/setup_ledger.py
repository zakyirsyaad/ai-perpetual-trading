"""The setup ledger: a per-bucket record of how entries filed under a setup actually came out.

An entry is filed under the side it takes, the regime condition the gate read for that hour and
the volatility regime of its setup (`bucket_key`). The ledger folds the realized gross outcome of
every closed trade in a bucket (in basis points of the entry price) into a mean, a standard error
and a one-sided 90 % upper bound, and compares that bound with a hurdle. A bucket whose upper bound
sits below the hurdle with enough samples is `blocked`; shadow outcomes of the entries refused in its
latest blocked episode can `reopen` it, and the real trades opened after its latest `ledger_block` row
can block it again. Everything here is pure maths over rows already fetched: no I/O, no clock.

This module imports neither `bot.strategy.pine_indicators` nor `bot.strategy.modes`; only
`bot/core/regime_gate.py` may.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Store timestamps are read the way the shadow evaluator reads `decided_at`: offset required, UTC out.
from bot.core.shadow_eval import _parse_utc

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


EMPTY_STATS = BucketStats(0, None, None, None)


@dataclass(frozen=True)
class BucketLedger:
    """One bucket's three samples.

    - `real`: every counted closed trade;
    - `shadow_run`: the `ledger_block` shadow outcomes of the bucket's latest blocked episode (the
      entries the ledger refused since its last real trade before its latest block row), which can
      reopen it;
    - `real_since_block`: the real trades opened after the bucket's latest `ledger_block` row
      (empty while it has none), on which a reopened bucket is judged again.
    """

    real: BucketStats
    shadow_run: BucketStats
    real_since_block: BucketStats


# What a bucket with no counted row reads: `learning`.
EMPTY_BUCKET_LEDGER = BucketLedger(EMPTY_STATS, EMPTY_STATS, EMPTY_STATS)


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


def ledger_status(bucket: BucketLedger, min_samples: int, hurdle_bps: float) -> str:
    """`learning`, `open`, `blocked` or `reopened` for one bucket.

    - `learning`: fewer than `min_samples` real outcomes (or no upper bound yet);
    - `open`: the real upper bound reaches the hurdle;
    - `blocked`: the real bound is below the hurdle and the latest run of shadow outcomes does not
      clear it;
    - `reopened`: the real bound is below the hurdle, but at least `min_samples` shadow outcomes of
      the latest run have an upper bound at or above it, and the real trades opened since the latest
      block row have not failed again: once those reach `min_samples` with an upper bound below the
      hurdle, the bucket is `blocked` again, and only a fresh run can reopen it.
    """
    real = bucket.real
    if real.n < min_samples or real.upper_bps is None:
        return STATUS_LEARNING
    if real.upper_bps >= hurdle_bps:
        return STATUS_OPEN
    if not _clears(bucket.shadow_run, min_samples, hurdle_bps):
        return STATUS_BLOCKED
    if _fails(bucket.real_since_block, min_samples, hurdle_bps):
        return STATUS_BLOCKED
    return STATUS_REOPENED


def _clears(stats: BucketStats, min_samples: int, hurdle_bps: float) -> bool:
    return stats.n >= min_samples and stats.upper_bps is not None and stats.upper_bps >= hurdle_bps


def _fails(stats: BucketStats, min_samples: int, hurdle_bps: float) -> bool:
    return stats.n >= min_samples and stats.upper_bps is not None and stats.upper_bps < hurdle_bps


def ledger_table(
    trade_rows: Iterable[Mapping[str, Any]],
    shadow_rows: Iterable[Mapping[str, Any]],
) -> dict[str, BucketLedger]:
    """`{bucket_key: BucketLedger}` for every bucket with a counted trade or a counted shadow outcome.

    A trade row counts when `factor_metadata.setup.bucket_key` is set and its prices give a gross
    result. A shadow row is a block row only with `source == "ledger_block"` and a readable
    `decided_at`; it counts as an outcome only with a finite `gross_bps` (expired and invalid rows
    carry none, so they only mark time). A bucket seen in only one source gets empty stats (`n == 0`)
    for the others; readers take `EMPTY_BUCKET_LEDGER` for a bucket that is not a key.

    Per bucket, the counted trades (by `opened_at`) and the block rows (by `decided_at`) share one
    timeline; times need an offset, as the store writes them. `shadow_run` holds the outcomes of the
    block rows decided strictly after the last trade opened at or before the latest block row (all of
    them when there is no such trade; a tie ends the run), and `real_since_block` the trades opened
    strictly after the latest block row. A trade without a readable `opened_at` counts in `real` only.
    """
    trades: dict[str, list[tuple[float, datetime | None]]] = {}
    for row in trade_rows:
        key = _trade_bucket_key(row)
        value = _trade_gross_bps(row) if key else None
        if key and value is not None:
            trades.setdefault(key, []).append((value, _moment(row.get("opened_at"))))

    blocks: dict[str, list[tuple[datetime, float | None]]] = {}
    for row in shadow_rows:
        key = _text(row.get("bucket_key"))
        decided_at = _moment(row.get("decided_at")) if row.get("source") == SHADOW_BLOCK_SOURCE else None
        if key and decided_at is not None:
            blocks.setdefault(key, []).append((decided_at, _finite(row.get("gross_bps"))))

    ledgers = {
        key: _bucket_ledger(trades.get(key, []), blocks.get(key, []))
        for key in (*trades, *(k for k in blocks if k not in trades))
    }
    return {key: ledger for key, ledger in ledgers.items() if ledger != EMPTY_BUCKET_LEDGER}


def _bucket_ledger(
    trades: Sequence[tuple[float, datetime | None]],
    blocks: Sequence[tuple[datetime, float | None]],
) -> BucketLedger:
    """One bucket's samples from its `(gross_bps, opened_at)` trades and `(decided_at, gross_bps)` block rows."""
    run: list[float] = []
    since_block: list[float] = []
    if blocks:
        latest = max(decided_at for decided_at, _ in blocks)
        # The last trade opened before the latest block row ends every earlier run of block rows; a trade at
        # the same instant ends it too, so a tie never leaves an old run standing.
        boundary = max(
            (opened_at for _, opened_at in trades if opened_at is not None and opened_at <= latest), default=None
        )
        run = [
            gross for decided_at, gross in blocks if gross is not None and (boundary is None or decided_at > boundary)
        ]
        since_block = [value for value, opened_at in trades if opened_at is not None and opened_at > latest]
    return BucketLedger(
        real=bucket_stats([value for value, _ in trades]),
        shadow_run=bucket_stats(run),
        real_since_block=bucket_stats(since_block),
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _moment(value: Any) -> datetime | None:
    """A UTC time from a store timestamp, or `None` when it is missing, unreadable or has no offset."""
    if value is None or value == "":
        return None
    try:
        return _parse_utc(value)
    except (TypeError, ValueError):
        return None


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
