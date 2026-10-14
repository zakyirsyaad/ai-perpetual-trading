"""The holdout gate (spec 5.11): the nine criteria per symbol and the choice.

Research code; the bot never imports it. Standard library only.

``gate_symbol`` is pure: the test stage computes every number first and hands
it over as an evidence mapping (keys in ``EVIDENCE_KEYS``, documented on
``gate_symbol``). ``choose_contender`` picks the one contender used for both
symbols; ``traded_symbols`` drops the symbols it fails on.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .spec import CONTENDER_PREFERENCE, GateConfig, spec_document

_CONTROLS = spec_document()["controls"]
C0_CENTRE_WR = 50.0
C0_WR_TOLERANCE_PTS: float = _CONTROLS["c0_wr_tolerance_pts"]
C0_GROSS_TOLERANCE_BPS: float = _CONTROLS["c0_gross_tolerance_bps"]
# Spec 5.11 criterion 9: "contender 1's training PBO was < 0.5" binds contender 1
# and contender 3, whose primary rule comes from the same grid; contender 2 is fixed.
PBO_CONTENDERS = ("c1", "c3")
# A contender without trades on the comparison symbols is compared to a coin flip.
C2_WR_WITHOUT_TRADES = 50.0
_PCT = 100.0

EVIDENCE_KEYS: Tuple[str, ...] = (
    "contender", "symbol", "tp", "sl", "gross_bps", "lower_bound", "p_holm",
    "wr_halves", "wr_overshoot", "wr_offsets", "source_p", "supply_holdout",
    "supply_last20", "c0_long_wr", "c0_short_wr", "c0_long_gross", "c0_short_gross",
    "pbo_pass",
)


@dataclass(frozen=True)
class GateVerdict:
    contender: str
    symbol: str
    passed: bool
    failed_criteria: Tuple[int, ...]  # spec 5.11 numbers, sorted
    wr: Optional[float]
    lower_bound: Optional[float]
    gross_bps: Optional[float]
    p_holm: float
    tp: int
    sl: int


def _wr(tp: int, sl: int) -> Optional[float]:
    return _PCT * tp / (tp + sl) if tp + sl > 0 else None


def _above(value: Optional[float], floor: float) -> bool:
    return value is not None and value > floor


def _at_least(value: Optional[float], floor: float) -> bool:
    return value is not None and value >= floor


def _within(value: Optional[float], centre: float, tolerance: float) -> bool:
    return value is not None and abs(value - centre) <= tolerance


def _robust(ev: Mapping[str, Any], cfg: GateConfig) -> bool:
    """WR >= 55 with 2 bps overshoot and WR > 50 in >= 15 of the 20 offset runs;
    a missing offset run or one without TP+SL counts as failing.

    Spec 5.11 uses the same 50 % floor in item 5 ("WR > 50% in each half") and in
    item 6 ("WR > 50% in at least 15 of 20 tick offsets"), so both read
    ``cfg.half_min_wr``; GateConfig has no separate offset floor."""
    passing = sum(1 for wr in ev["wr_offsets"] if _above(wr, cfg.half_min_wr))
    return _at_least(ev["wr_overshoot"], cfg.robust_min_wr) and passing >= cfg.offsets_min_pass


def c0_side_ok(wr: Optional[float], gross_bps: Optional[float]) -> bool:
    """One C0 side (always long or always short) is sane: WR 50 +- 3, |gross| <= 2 bps."""
    return (_within(wr, C0_CENTRE_WR, C0_WR_TOLERANCE_PTS)
            and _within(gross_bps, 0.0, C0_GROSS_TOLERANCE_BPS))


def _controls(ev: Mapping[str, Any]) -> bool:
    """C0 long and short each at 50 +- 3 WR and |gross| <= 2 bps; PBO for c1/c3."""
    c0_ok = all(c0_side_ok(ev[f"c0_{side}_wr"], ev[f"c0_{side}_gross"])
                for side in ("long", "short"))
    pbo_ok = ev["contender"] not in PBO_CONTENDERS or ev["pbo_pass"] is True
    return c0_ok and pbo_ok


def _criteria(ev: Mapping[str, Any], cfg: GateConfig) -> Dict[int, bool]:
    tp, sl = ev["tp"], ev["sl"]
    halves = list(ev["wr_halves"])
    return {
        1: tp + sl >= cfg.min_tp_sl,
        2: ev["p_holm"] < cfg.alpha,
        3: _at_least(_wr(tp, sl), cfg.min_wr),
        4: _above(ev["gross_bps"], 0.0),
        5: len(halves) == 2 and all(_above(wr, cfg.half_min_wr) for wr in halves),
        6: _robust(ev, cfg),
        7: ev["source_p"] < cfg.alpha,
        8: (ev["supply_holdout"] >= cfg.min_supply_per_day
            and ev["supply_last20"] >= cfg.min_supply_per_day),
        9: _controls(ev),
    }


def gate_symbol(evidence: Mapping[str, Any], cfg: GateConfig) -> GateVerdict:
    """One contender on one symbol against spec 5.11's criteria 1-9.

    Evidence keys (all required):

    - ``contender`` ("c1" | "c2" | "c3"), ``symbol``;
    - ``tp``, ``sl``: the symbol's take-profit / stop-loss counts in the main run
      (criterion 1 needs ``tp + sl >= min_tp_sl``; criterion 3 WR >= ``min_wr``);
    - ``p_holm``: Holm-adjusted (per symbol, across the contenders present) paired
      day-block bootstrap p of WR above C0 (criterion 2, ``< alpha``);
    - ``gross_bps``: mean gross bps after slippage, before fees (criterion 4, > 0);
    - ``wr_halves``: WR in [start, mid) and [mid, end) by entry (criterion 5, each > 50);
    - ``wr_overshoot``: WR of the 2 bps overshoot rerun; ``wr_offsets``: WR of each
      tick-offset run (criterion 6);
    - ``source_p``: trend permutation p (c1, c2) or paired bootstrap p against the
      primary rule alone (c3) (criterion 7, ``< alpha``);
    - ``supply_holdout``, ``supply_last20``: combined trades per trading day of the
      passing symbols over the holdout and its last 20 trading days (criterion 8);
    - ``c0_long_wr``, ``c0_short_wr``, ``c0_long_gross``, ``c0_short_gross``: the
      symbol's always-long / always-short controls; ``pbo_pass``: the selection's
      training PBO verdict, required for c1 and c3 (criterion 9).

    WR values are percent or ``None`` (no TP+SL); a ``None`` never passes.
    ``lower_bound`` is carried into the verdict only.
    """
    missing = [k for k in EVIDENCE_KEYS if k not in evidence]
    if missing:
        raise ValueError(f"gate evidence lacks {', '.join(missing)}")
    if evidence["contender"] not in CONTENDER_PREFERENCE:
        raise ValueError(f"unknown contender {evidence['contender']!r}")
    checks = _criteria(evidence, cfg)
    failed = tuple(sorted(n for n, ok in checks.items() if not ok))
    tp, sl = int(evidence["tp"]), int(evidence["sl"])
    return GateVerdict(
        contender=evidence["contender"], symbol=evidence["symbol"], passed=not failed,
        failed_criteria=failed, wr=_wr(tp, sl), lower_bound=evidence["lower_bound"],
        gross_bps=evidence["gross_bps"], p_holm=float(evidence["p_holm"]), tp=tp, sl=sl,
    )


# --------------------------------------------------------------------------- choice


def _by_contender(verdicts: Iterable[GateVerdict]) -> Dict[str, List[GateVerdict]]:
    out: Dict[str, List[GateVerdict]] = {}
    for v in verdicts:
        if v.contender not in CONTENDER_PREFERENCE:
            raise ValueError(f"unknown contender {v.contender!r}")
        out.setdefault(v.contender, []).append(v)
    return out


def _pooled_wr(rows: Sequence[GateVerdict]) -> Optional[float]:
    return _wr(sum(v.tp for v in rows), sum(v.sl for v in rows))


def _beats_c2(name: str, groups: Mapping[str, List[GateVerdict]], symbols: Sequence[str]) -> bool:
    """Pooled holdout WR over the contender's passing symbols above c2's WR there."""
    if name == "c2":
        return True
    mine = _pooled_wr([v for v in groups[name] if v.symbol in symbols])
    c2 = _pooled_wr([v for v in groups.get("c2", []) if v.symbol in symbols])
    return _above(mine, C2_WR_WITHOUT_TRADES if c2 is None else c2)


def _rank_key(
    name: str, n_passed: int, lower_bounds: Mapping[str, Optional[float]]
) -> Tuple[int, float, int]:
    lb = lower_bounds.get(name)
    return (-n_passed, -(lb if lb is not None else -math.inf), CONTENDER_PREFERENCE.index(name))


def choose_contender(
    verdicts: Iterable[GateVerdict], lower_bounds: Mapping[str, Optional[float]]
) -> Optional[str]:
    """The contender used for both symbols, or ``None`` when nothing passes.

    Most symbols passed, then the higher pooled WR lower bound over its passing
    symbols (``lower_bounds``; ``None`` ranks last), then the simpler (c2, c1, c3).
    Contender 1 or 3 is eligible only when its pooled holdout WR over its passing
    symbols beats contender 2's WR over the same symbols (50 if c2 has no TP+SL).
    """
    groups = _by_contender(verdicts)
    ranked = []
    for name, rows in groups.items():
        symbols = [v.symbol for v in rows if v.passed]
        if symbols and _beats_c2(name, groups, symbols):
            ranked.append((_rank_key(name, len(symbols), lower_bounds), name))
    return min(ranked)[1] if ranked else None


def traded_symbols(verdicts: Iterable[GateVerdict], contender: Optional[str]) -> Tuple[str, ...]:
    """Symbols the chosen contender passes on; a failing symbol is not traded."""
    if contender is None:
        return ()
    return tuple(v.symbol for v in verdicts if v.contender == contender and v.passed)
