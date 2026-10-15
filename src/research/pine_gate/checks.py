"""VST-versus-production check (spec 5.12) and AI decision-level view (spec 5.13).

Both are reports; neither places orders. Research code; the bot never imports
it. Standard library only.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .bars import Bars, bars_from_rows, entry_index_for, wib_day
from .calibrate import payload_from_row
from .features import FeatureFrame
from .outcomes import OutcomeCache
from .simulate import SignalFn
from .spec import ExecConfig
from .timeparse import parse_iso_ms

_MS_1M = 60_000
_MS_PER_SECOND = 1000

# Spec 5.12: VST must be a valid stand-in for production. Over the ticks where either
# side signals, the side agrees on at least 97%; over the ticks where both signal the
# same side, the bracket outcome kind agrees on at least 95%; each side's win rate is
# taken at its own signal ticks with its own side, and the two differ by at most 2 points.
SIDE_AGREE_MIN = 0.97
OUTCOME_AGREE_MIN = 0.95
WR_DIFF_MAX_PTS = 2.0
_EPS = 1e-9

# Spec 5.13: bucket A counts AI candidates with confidence >= 0.75 (the bot's
# AI_MIN_CONFIDENCE) that the risk review approved.
AI_MIN_CONFIDENCE = 0.75

_SIDES = {"OPEN_LONG": 1, "OPEN_SHORT": -1, "HOLD": 0}

OutcomeFor = Callable[[str, int, int], Optional[Tuple[str, float]]]


# --- VST klines ------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite JSON number {name}")


def _edge_row(path: Path, key: Any, value: Any) -> Tuple[int, float, float, float, float, float]:
    """One ``"<open_ms>": [o, h, l, c]`` entry as a row; ``ValueError`` when malformed."""
    if not isinstance(key, str) or not key.isdigit():
        raise ValueError(f"{path}: k1 key {key!r} is not a non-negative integer of ms")
    if (not isinstance(value, list) or len(value) != 4
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in value)):
        raise ValueError(f"{path}: k1[{key}] is not a list of four numbers")
    o, h, low, c = (float(x) for x in value)
    return (int(key), o, h, low, c, 0.0)


def load_edge_klines_vst(path: Path) -> Bars:
    """Load 1m VST bars from ``{"k1": {"<open_ms>": [o, h, l, c], ...}, ...}``.

    Other keys are ignored and volume is 0.0. NaN/Infinity, a malformed entry and
    any value ``bars_from_rows`` refuses (non-positive price, broken OHLC) raise
    ``ValueError``; a gap raises ``BarGapError``.
    """
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh, parse_constant=_reject_constant)
    k1 = payload.get("k1") if isinstance(payload, dict) else None
    if not isinstance(k1, dict):
        raise ValueError(f"{path}: expected a JSON object with a 'k1' mapping")
    rows = sorted(_edge_row(path, key, value) for key, value in k1.items())
    return bars_from_rows(rows, _MS_1M)


# --- VST agreement ---------------------------------------------------------


@dataclass(frozen=True)
class Agreement:
    n: int  # ticks compared
    n_either: int  # ticks where production or VST signals
    n_both: int  # ticks where both signal the same side and both outcomes resolve
    n_prod: int  # production signal ticks with a resolved production outcome
    n_vst: int  # VST signal ticks with a resolved VST outcome
    side_agree: Optional[float]  # same side / n_either (0..1)
    outcome_agree: Optional[float]  # same kind / n_both (0..1)
    wr_prod: Optional[float]  # TP / (TP + SL) in percent, production at its own ticks
    wr_vst: Optional[float]  # the same for VST at its own ticks and side
    passed: bool


def _wr(kinds: Sequence[str]) -> Optional[float]:
    tp = sum(1 for k in kinds if k == "TP")
    sl = sum(1 for k in kinds if k == "SL")
    return 100.0 * tp / (tp + sl) if tp + sl else None


def _outcome_kind(frame: FeatureFrame, side: int, t: int, cfg: ExecConfig,
                  cache: OutcomeCache) -> Optional[str]:
    """Bracket outcome kind on ``frame``'s own 1m bars for an order at ``t`` + order
    delay (no latency) on ``side``; ``None`` when flat or unresolved."""
    if side == 0:
        return None
    i = entry_index_for(frame.bars_1m, t + cfg.order_delay_s * _MS_PER_SECOND)
    o = cache.get(i, side) if i is not None else None
    return None if o is None else o.kind


def _passed(n_either: int, same_side: int, pairs: Sequence[Tuple[str, str]],
            wr_prod: Optional[float], wr_vst: Optional[float]) -> bool:
    same_kind = sum(1 for a, b in pairs if a == b)
    return (
        n_either > 0 and len(pairs) > 0 and wr_prod is not None and wr_vst is not None
        and same_side * 100 >= SIDE_AGREE_MIN * 100 * n_either - _EPS
        and same_kind * 100 >= OUTCOME_AGREE_MIN * 100 * len(pairs) - _EPS
        and abs(wr_prod - wr_vst) <= WR_DIFF_MAX_PTS + _EPS
    )


def vst_agreement(
    prod: FeatureFrame,
    vst: FeatureFrame,
    signal_for: Callable[[FeatureFrame, FeatureFrame, int], int],
    times: Sequence[int],
    cfg: ExecConfig,
) -> Agreement:
    """Compare signal sides and bracket outcomes on VST and production data.

    ``signal_for(features, poc_frame, t)`` is called as ``(prod, prod, t)`` and
    ``(vst, prod, t)`` so volume-profile rules read the production POC on both
    sides. Ticks where neither side signals are left out. Each side's outcomes and
    WR come from its own signal ticks, side and bars; outcome kinds are compared
    where both signal the same side. Outcome kinds do not depend on the stop
    overshoot. Without a tick where either side signals the check never passes.
    """
    caches = (OutcomeCache(prod.bars_1m, cfg), OutcomeCache(vst.bars_1m, cfg))
    n_either = same_side = 0
    prod_kinds: List[str] = []
    vst_kinds: List[str] = []
    pairs: List[Tuple[str, str]] = []
    for t in times:
        side_prod, side_vst = signal_for(prod, prod, t), signal_for(vst, prod, t)
        if side_prod == 0 and side_vst == 0:
            continue
        n_either += 1
        same_side += side_prod == side_vst
        kind_prod = _outcome_kind(prod, side_prod, t, cfg, caches[0])
        kind_vst = _outcome_kind(vst, side_vst, t, cfg, caches[1])
        prod_kinds += [kind_prod] if kind_prod is not None else []
        vst_kinds += [kind_vst] if kind_vst is not None else []
        if side_prod == side_vst and kind_prod is not None and kind_vst is not None:
            pairs.append((kind_prod, kind_vst))
    wr_prod, wr_vst = _wr(prod_kinds), _wr(vst_kinds)
    same_kind = sum(1 for a, b in pairs if a == b)
    return Agreement(n=len(times), n_either=n_either, n_both=len(pairs), n_prod=len(prod_kinds),
                     n_vst=len(vst_kinds), side_agree=same_side / n_either if n_either else None,
                     outcome_agree=same_kind / len(pairs) if pairs else None,
                     wr_prod=wr_prod, wr_vst=wr_vst,
                     passed=_passed(n_either, same_side, pairs, wr_prod, wr_vst))


# --- AI decisions ----------------------------------------------------------


@dataclass(frozen=True)
class DecisionRow:
    symbol: str  # venue form, e.g. "BTC-USDT"
    created_ms: int
    side: int  # +1 OPEN_LONG, -1 OPEN_SHORT, 0 HOLD
    confidence: float
    approved: bool  # payload_json.decision.risk_review.approved is True
    ai_call_id: Optional[str]


def parse_decision(row: Dict[str, Any]) -> Optional[DecisionRow]:
    """Turn an ``ai_decisions`` row into a ``DecisionRow``; ``None`` if unusable.

    HOLD rows are kept with ``side == 0``; any other action is unusable.
    """
    if not isinstance(row, dict):
        return None
    symbol = row.get("symbol")
    action = row.get("action")
    side = _SIDES.get(action) if isinstance(action, str) else None
    created_ms = parse_iso_ms(row.get("created_at"))
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
    payload = payload_from_row(row)
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
