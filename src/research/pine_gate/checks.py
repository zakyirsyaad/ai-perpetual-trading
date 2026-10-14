"""VST-versus-production check (spec 5.12) and AI decision-level view (spec 5.13).

Both are reports; neither places orders. Research code; the bot never imports
it. Standard library only.
"""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .bars import Bars, bars_from_rows, wib_day
from .features import FeatureFrame
from .outcomes import OutcomeCache
from .simulate import SignalFn
from .spec import ExecConfig

_MS_1M = 60_000
_MS_PER_SECOND = 1000

# Spec 5.12: VST must be a valid stand-in for production. The signal side agrees
# on at least 97% of ticks, the bracket outcome kind on at least 95% of entries,
# and the win rate differs by at most 2 percentage points.
SIDE_AGREE_MIN = 0.97
OUTCOME_AGREE_MIN = 0.95
WR_DIFF_MAX_PTS = 2.0
_EPS = 1e-9

# Spec 5.13: bucket A counts AI candidates with confidence >= 0.75 (the bot's
# AI_MIN_CONFIDENCE) that the risk review approved.
AI_MIN_CONFIDENCE = 0.75

_SIDES = {"OPEN_LONG": 1, "OPEN_SHORT": -1, "HOLD": 0}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ISO = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}(?::?\d{2})?)?$"
)

OutcomeFor = Callable[[str, int, int], Optional[Tuple[str, float]]]


# --- VST klines ------------------------------------------------------------


def load_edge_klines_vst(path: Path) -> Bars:
    """Load 1m VST bars from ``{"k1": {"<open_ms>": [o, h, l, c], ...}, ...}``.

    Other keys are ignored and volume is 0.0. Bars must be contiguous; a gap
    raises ``BarGapError``.
    """
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    k1 = payload.get("k1") if isinstance(payload, dict) else None
    if not isinstance(k1, dict):
        raise ValueError(f"{path}: expected a JSON object with a 'k1' mapping")
    rows = []
    for key in sorted(k1, key=int):
        o, h, l, c = k1[key]
        rows.append((int(key), float(o), float(h), float(l), float(c), 0.0))
    return bars_from_rows(rows, _MS_1M)


# --- VST agreement ---------------------------------------------------------


@dataclass(frozen=True)
class Agreement:
    n: int  # ticks compared
    side_agree: Optional[float]  # share of ticks with equal side (0..1)
    outcome_agree: Optional[float]  # share of compared entries with equal kind (0..1)
    wr_prod: Optional[float]  # TP / (TP + SL) in percent
    wr_vst: Optional[float]
    passed: bool


def _entry_index(bars: Bars, order_ms: int) -> Optional[int]:
    """Index of the 1m bar containing ``order_ms``; ``None`` when not covered."""
    i = bisect_right(bars.open_ms, order_ms) - 1
    if i < 0 or order_ms >= bars.open_ms[i] + bars.interval_ms:
        return None
    return i


def _wr(kinds: Sequence[str]) -> Optional[float]:
    tp = sum(1 for k in kinds if k == "TP")
    sl = sum(1 for k in kinds if k == "SL")
    return 100.0 * tp / (tp + sl) if tp + sl else None


def vst_agreement(
    prod: FeatureFrame,
    vst: FeatureFrame,
    signal_for: Callable[[FeatureFrame, FeatureFrame, int], int],
    times: Sequence[int],
    cfg: ExecConfig,
    *,
    overshoot_bps: float = 1.0,
) -> Agreement:
    """Compare signal sides and bracket outcomes on VST and production data.

    ``signal_for(features, poc_frame, t)`` is called as ``(prod, prod, t)`` and
    ``(vst, prod, t)`` so volume-profile rules read the production POC on both
    sides. Outcomes are compared at times where the production side is not 0:
    the order is placed at ``t + cfg.order_delay_s`` (no latency), enters on the
    1m bar containing it on each data set, and both walk the production side.
    A time is skipped when either outcome is unresolved; the win rates use the
    same compared entries. ``overshoot_bps`` is accepted for call-site symmetry
    with the simulator and does not affect outcome kinds.
    """
    del overshoot_bps
    prod_cache = OutcomeCache(prod.bars_1m, cfg)
    vst_cache = OutcomeCache(vst.bars_1m, cfg)
    same_side = 0
    prod_kinds: List[str] = []
    vst_kinds: List[str] = []
    for t in times:
        side_prod = signal_for(prod, prod, t)
        side_vst = signal_for(vst, prod, t)
        if side_prod == side_vst:
            same_side += 1
        if side_prod == 0:
            continue
        order_ms = t + cfg.order_delay_s * _MS_PER_SECOND
        i_prod = _entry_index(prod.bars_1m, order_ms)
        i_vst = _entry_index(vst.bars_1m, order_ms)
        if i_prod is None or i_vst is None:
            continue
        o_prod = prod_cache.get(i_prod, side_prod)
        o_vst = vst_cache.get(i_vst, side_prod)
        if o_prod is None or o_vst is None:
            continue
        prod_kinds.append(o_prod.kind)
        vst_kinds.append(o_vst.kind)

    n = len(times)
    compared = len(prod_kinds)
    same_kind = sum(1 for a, b in zip(prod_kinds, vst_kinds) if a == b)
    wr_prod = _wr(prod_kinds)
    wr_vst = _wr(vst_kinds)
    passed = (
        n > 0
        and compared > 0
        and wr_prod is not None
        and wr_vst is not None
        and same_side * 100 >= SIDE_AGREE_MIN * 100 * n - _EPS
        and same_kind * 100 >= OUTCOME_AGREE_MIN * 100 * compared - _EPS
        and abs(wr_prod - wr_vst) <= WR_DIFF_MAX_PTS + _EPS
    )
    return Agreement(
        n=n,
        side_agree=same_side / n if n else None,
        outcome_agree=same_kind / compared if compared else None,
        wr_prod=wr_prod,
        wr_vst=wr_vst,
        passed=passed,
    )


# --- AI decisions ----------------------------------------------------------


@dataclass(frozen=True)
class DecisionRow:
    symbol: str  # venue form, e.g. "BTC-USDT"
    created_ms: int
    side: int  # +1 OPEN_LONG, -1 OPEN_SHORT, 0 HOLD
    confidence: float
    approved: bool  # payload_json.decision.risk_review.approved is True
    ai_call_id: Optional[str]


def _parse_created_ms(text: Any) -> Optional[int]:
    """ISO-8601 to UTC ms; accepts ``Z`` and 1-6+ fractional digits (Python 3.10)."""
    if not isinstance(text, str):
        return None
    m = _ISO.match(text.strip())
    if m is None:
        return None
    day, clock, frac, offset = m.groups()
    if offset is None or offset == "Z":
        offset = "+00:00"
    elif ":" not in offset:
        offset = offset[:3] + ":" + (offset[3:] or "00")
    fraction = (frac or "0")[:6].ljust(6, "0")
    try:
        dt = datetime.fromisoformat(f"{day}T{clock}.{fraction}{offset}")
    except ValueError:
        return None
    delta = dt - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _payload(row: Dict[str, Any]) -> Dict[str, Any]:
    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return {}
    return payload if isinstance(payload, dict) else {}


def parse_decision(row: Dict[str, Any]) -> Optional[DecisionRow]:
    """Turn an ``ai_decisions`` row into a ``DecisionRow``; ``None`` if unusable.

    HOLD rows are kept with ``side == 0``; any other action is unusable.
    """
    if not isinstance(row, dict):
        return None
    symbol = row.get("symbol")
    side = _SIDES.get(row.get("action"))
    created_ms = _parse_created_ms(row.get("created_at"))
    confidence = row.get("confidence")
    if not isinstance(symbol, str) or not symbol or side is None or created_ms is None:
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float, str)):
        return None
    try:
        confidence = float(confidence)
    except ValueError:
        return None
    if not math.isfinite(confidence):
        return None
    payload = _payload(row)
    decision = payload.get("decision")
    review = decision.get("risk_review") if isinstance(decision, dict) else None
    approved = isinstance(review, dict) and review.get("approved") is True
    call_id = payload.get("ai_call_id")
    return DecisionRow(
        symbol=symbol.replace("/", "-"),
        created_ms=created_ms,
        side=side,
        confidence=confidence,
        approved=approved,
        ai_call_id=None if call_id is None else str(call_id),
    )


def _bucket_stats(
    resolved: Sequence[Tuple[str, float]], rows_days: int
) -> Dict[str, Any]:
    tp = sum(1 for kind, _ in resolved if kind == "TP")
    sl = sum(1 for kind, _ in resolved if kind == "SL")
    time_stops = sum(1 for kind, _ in resolved if kind == "TIME")
    n = len(resolved)
    return {
        "n": n,
        "tp": tp,
        "sl": sl,
        "time": time_stops,
        "wr": 100.0 * tp / (tp + sl) if tp + sl else None,
        "gross": sum(g for _, g in resolved) / n if n else None,
        "trades_per_day": n / rows_days if rows_days else 0.0,
    }


def decision_buckets(
    rows: Iterable[DecisionRow],
    tick_start_ms: Callable[[DecisionRow], int],
    signal: SignalFn,
    outcome_for: OutcomeFor,
) -> Dict[str, Dict[str, Any]]:
    """Spec 5.13 buckets A, B, E, F over stored AI decisions.

    ``outcome_for(symbol, t_ms, side)`` returns ``(kind, gross_bps)`` of the
    bracket trade an order at tick ``t_ms`` would get, or ``None`` when
    unresolved. Buckets:

    - A: ``side != 0``, ``confidence >= AI_MIN_CONFIDENCE`` and approved; the
      outcome is taken with the AI's side.
    - B: A rows whose ``signal(symbol, tick)`` equals the AI's side.
    - E: ``signal != 0`` and the AI said HOLD; outcome with the signal's side.
    - F: every row with ``signal != 0``; outcome with the signal's side.

    Per bucket: ``n`` (rows with a resolved outcome), ``tp``/``sl``/``time``
    counts, ``wr`` = TP / (TP + SL) in percent (``None`` without any; time stops
    are in ``n`` and ``gross`` but not in the WR), ``gross`` = mean gross bps
    over all resolved rows, and ``trades_per_day`` = ``n`` over the number of
    distinct WIB days the rows fall on (``created_ms``).
    """
    rows = list(rows)
    days = len({wib_day(r.created_ms) for r in rows})
    resolved: Dict[str, List[Tuple[str, float]]] = {"A": [], "B": [], "E": [], "F": []}

    def take(bucket: str, row: DecisionRow, tick: int, side: int) -> None:
        result = outcome_for(row.symbol, tick, side)
        if result is not None:
            resolved[bucket].append(result)

    for row in rows:
        tick = tick_start_ms(row)
        sig = signal(row.symbol, tick)
        is_candidate = (
            row.side != 0 and row.confidence >= AI_MIN_CONFIDENCE and row.approved
        )
        if is_candidate:
            take("A", row, tick, row.side)
            if sig == row.side:
                take("B", row, tick, row.side)
        if sig != 0:
            if row.side == 0:
                take("E", row, tick, sig)
            take("F", row, tick, sig)
    return {name: _bucket_stats(items, days) for name, items in resolved.items()}
