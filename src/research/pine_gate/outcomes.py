"""First-touch take-profit / stop-loss bracket outcomes on 1m bars.

Research code; the bot never imports it. Standard library only.

The bracket is set from the entry fill (which already carries the adverse
entry slip) and walked from the bar after the entry. Pessimism rules: a bar
that touches both levels is a stop, and a bar that opens beyond a level fills
at its open.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .bars import Bars
from .spec import ExecConfig

_BPS = 1e4


@dataclass(frozen=True)
class Outcome:
    kind: str  # "TP" | "SL" | "TIME"
    entry_idx: int
    exit_idx: int
    entry_price: float
    raw_exit_price: float
    gapped: bool
    both_touched: bool = False  # deciding bar touched TP and SL; resolved as SL


def bracket_outcome(
    bars_1m: Bars, entry_idx: int, side: int, cfg: ExecConfig
) -> Optional[Outcome]:
    """Resolve the bracket opened at ``entry_idx``; ``None`` if the data ends first."""
    if side not in (1, -1):
        raise ValueError(f"side must be +1 or -1, got {side!r}")
    n = len(bars_1m)
    if not 0 <= entry_idx < n:
        raise ValueError(f"entry_idx {entry_idx} outside 0..{n - 1}")

    entry = bars_1m.close[entry_idx] * (1 + side * cfg.entry_slip_bps / _BPS)
    take = entry * (1 + side * cfg.bracket_pct / 100.0)
    stop = entry * (1 - side * cfg.bracket_pct / 100.0)
    last_idx = entry_idx + cfg.time_stop_min

    def done(kind: str, idx: int, raw: float, gapped: bool, both: bool = False):
        return Outcome(kind, entry_idx, idx, entry, raw, gapped, both)

    for i in range(entry_idx + 1, min(last_idx, n - 1) + 1):
        o = bars_1m.open[i]
        if side * (o - stop) <= 0:
            return done("SL", i, o, True)
        if side * (o - take) >= 0:
            return done("TP", i, o, True)
        hit_stop = _touched(bars_1m, i, side, stop, is_stop=True)
        hit_take = _touched(bars_1m, i, side, take, is_stop=False)
        if hit_stop:
            return done("SL", i, stop, False, both=hit_take)
        if hit_take:
            return done("TP", i, take, False)
    if last_idx <= n - 1:
        return done("TIME", last_idx, bars_1m.close[last_idx], False)
    return None


def _touched(bars: Bars, i: int, side: int, level: float, is_stop: bool) -> bool:
    """Whether bar ``i`` trades through ``level`` (stop is adverse, take is favourable)."""
    adverse_low = (side == 1) == is_stop
    if adverse_low:
        return bars.low[i] <= level
    return bars.high[i] >= level


def gross_bps(o: Outcome, side: int, overshoot_bps: float, cfg: ExecConfig) -> float:
    """Gross return in bps from the entry fill after exit slippage.

    Stops lose ``overshoot_bps`` beyond their raw exit; take-profit and time
    exits give up ``cfg.tp_slip_bps``. The entry slip is already in the fill.
    """
    slip = overshoot_bps if o.kind == "SL" else cfg.tp_slip_bps
    exit_price = o.raw_exit_price * (1 - side * slip / _BPS)
    return side * (exit_price / o.entry_price - 1) * _BPS


class OutcomeCache:
    """Memoised ``bracket_outcome`` for one bar series and execution config."""

    def __init__(self, bars_1m: Bars, cfg: ExecConfig) -> None:
        self._bars = bars_1m
        self._cfg = cfg
        self._memo: Dict[Tuple[int, int], Optional[Outcome]] = {}

    def get(self, entry_idx: int, side: int) -> Optional[Outcome]:
        key = (entry_idx, side)
        if key not in self._memo:
            self._memo[key] = bracket_outcome(self._bars, entry_idx, side, self._cfg)
        return self._memo[key]
