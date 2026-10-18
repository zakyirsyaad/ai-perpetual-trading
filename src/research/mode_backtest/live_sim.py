"""Mode L's 1m exit walk and the money of its trades (the part of the live-exit backtest that is
pure arithmetic; the event loop is added to this module by a later task).

Research code; the bot never imports it. ``resolve_live_exit`` is the walk of ``simulate.resolve_exit``
with the loss cut of version (ii) added; ``settle_live`` is ``simulate.settle`` with the slippage
chosen by the kind of exit. Both take plain numbers and bars and read no clock, file or setting.

The walk (spec section 4). Order inside one bar:

1. the time exit: the bar ``limit_min`` minutes after the entry bar exits at its open, before any
   of its levels are looked at;
2. open beyond the stop: SL at the open (``gapped``);
3. open beyond the active cut: POTONG at the open (``gapped``);
4. open beyond the take-profit: TP at the open (``gapped``);
5. the bar's worst price through the stop: SL at the stop price;
6. else through the active cut: POTONG at the cut price;
7. else the bar's best price through the take-profit: TP at the take-profit price.

A loss level (SL or POTONG) therefore wins a bar that also reached the take-profit, because a 1m bar
does not say which came first; ``both_touched`` records that case. The cut is active on the bars
with index ``entry_idx + cut_from_min`` or later, and only when ``levels.cut`` is not ``None``
(version i has none). The walk needs every minute: a bar that is not one minute after the one
before it ends the walk as the end of the data does, with ``None``.

Money (spec section 5): the entry fills ``entry_bps`` against the position at the entry open; a take-profit
exit uses ``slippage_tp_bps`` (0 when the target rests as a limit order, so it fills at its price) and
every other exit uses ``slippage_exit_market_bps``.
"""

from __future__ import annotations

from typing import Optional

from bot.strategy.modes.checks import (
    LONG,
    check_side,
    is_whole,
    require_not_negative,
    require_positive,
)
from bot.strategy.modes.config import CostConfig
from bot.strategy.modes.live_exit import LiveExitLevels
from bot.strategy.modes.position_rules import adverse_fill
from research.pine_gate.bars import Bars
from research.pine_gate.features import INTERVAL_1M

from .simulate import (
    EXIT_POTONG,
    EXIT_SL,
    EXIT_TP,
    EXIT_WAKTU,
    ExitOutcome,
    Money,
    check_walk,
    reaches_sl,
    reaches_tp,
)

__all__ = ["resolve_live_exit", "settle_live"]

_BPS_PER_PERCENT = 100.0  # 1 percent = 100 bps


def _check_cut(side: int, levels: LiveExitLevels, cut_from_min: int) -> None:
    if not is_whole(cut_from_min) or cut_from_min < 1:
        raise ValueError(
            f"cut_from_min must be a whole number of minutes of at least 1, got {cut_from_min!r}"
        )
    if levels.cut is None:
        return
    require_positive("cut", levels.cut)
    # the cut lies between the stop and the take-profit, on the stop's side of the entry
    between = levels.sl < levels.cut < levels.tp if side == LONG else levels.tp < levels.cut < levels.sl
    if not between:
        raise ValueError(
            f"the cut must lie between the stop-loss and the take-profit, got sl {levels.sl!r}, "
            f"cut {levels.cut!r}, tp {levels.tp!r}"
        )


def resolve_live_exit(
    bars_1m: Bars,
    entry_idx: int,
    side: int,
    levels: LiveExitLevels,
    cut_from_min: int,
    limit_min: int,
) -> Optional[ExitOutcome]:
    """How the mode L position that entered at the open of bar ``entry_idx`` ends, or ``None``.

    ``levels`` carries the stop, take-profit and (version ii) cut prices; ``cut_from_min`` is the
    minute from which the cut acts and ``limit_min`` the time limit. The walk goes bar by bar in the
    order of the module docstring and stops at the first bar that decides. It returns ``None`` when
    the data ends first, which includes a hole in the minutes and a missing time-exit bar.

    Raises ``ValueError`` for bars that are not 1m, a ``side`` that is not ``LONG`` or ``SHORT``, an
    ``entry_idx`` outside the bars, a ``limit_min`` or ``cut_from_min`` that is not a whole number
    of at least 1, and levels that are not finite positive numbers or are not in order (the cut
    between the stop and the take-profit)."""
    check_walk(bars_1m, entry_idx, side, levels.tp, levels.sl, limit_min)
    _check_cut(side, levels, cut_from_min)
    sl, tp, cut = levels.sl, levels.tp, levels.cut
    cut_idx = entry_idx + cut_from_min
    limit_idx = entry_idx + limit_min
    open_ms, opens, highs, lows = bars_1m.open_ms, bars_1m.open, bars_1m.high, bars_1m.low
    for i in range(entry_idx, min(limit_idx, len(bars_1m) - 1) + 1):
        if i > entry_idx and open_ms[i] != open_ms[i - 1] + INTERVAL_1M:
            return None
        bar_open = opens[i]
        if i == limit_idx:
            return ExitOutcome(EXIT_WAKTU, i, bar_open, False, False)
        active_cut = cut if cut is not None and i >= cut_idx else None
        if reaches_sl(side, bar_open, sl):
            return ExitOutcome(EXIT_SL, i, bar_open, True, False)
        if active_cut is not None and reaches_sl(side, bar_open, active_cut):
            return ExitOutcome(EXIT_POTONG, i, bar_open, True, False)
        if reaches_tp(side, bar_open, tp):
            return ExitOutcome(EXIT_TP, i, bar_open, True, False)
        worst, best = (lows[i], highs[i]) if side == LONG else (highs[i], lows[i])
        if reaches_sl(side, worst, sl):
            return ExitOutcome(EXIT_SL, i, float(sl), False, reaches_tp(side, best, tp))
        if active_cut is not None and reaches_sl(side, worst, active_cut):
            return ExitOutcome(EXIT_POTONG, i, float(active_cut), False, reaches_tp(side, best, tp))
        if reaches_tp(side, best, tp):
            return ExitOutcome(EXIT_TP, i, float(tp), False, False)
    return None


def settle_live(
    entry_open: float,
    outcome: ExitOutcome,
    side: int,
    notional: float,
    fee_pct: float,
    costs: CostConfig,
) -> Money:
    """The money of a mode L trade that entered at ``entry_open`` and ended as ``outcome``.

    ``fee_pct`` is a percent (0.035 in the document) and ``notional`` the position size in USDT at
    the entry fill. With ``entry_fill = adverse_fill(entry_open, side, entry_bps / 100, closing=False)``
    and ``exit_fill = adverse_fill(outcome.raw_price, side, bps / 100, closing=True)``, where ``bps``
    is ``slippage_tp_bps`` for a take-profit and ``slippage_exit_market_bps`` for every other exit:

    * ``qty = notional / entry_fill``;
    * ``volume = qty x entry_fill + qty x exit_fill``;
    * ``fee = fee_pct / 100 x volume``;
    * ``pnl = side x qty x (exit_fill - entry_fill) - fee``;
    * ``slippage = qty x |entry_fill - entry_open| + qty x |raw - exit_fill|``.

    Raises ``ValueError`` for a ``side`` that is not ``LONG`` or ``SHORT``, a price that is not a
    finite number above 0, a ``notional`` that is not a finite number above 0, and a ``fee_pct`` or
    slippage that is not a finite number of at least 0."""
    check_side(side)
    require_positive("notional", notional)
    require_not_negative("fee_pct", fee_pct)
    exit_bps = costs.slippage_tp_bps if outcome.reason == EXIT_TP else costs.slippage_exit_market_bps
    raw_exit = outcome.raw_price
    entry_fill = adverse_fill(entry_open, side, costs.slippage_entry_bps / _BPS_PER_PERCENT, False)
    exit_fill = adverse_fill(raw_exit, side, exit_bps / _BPS_PER_PERCENT, True)
    qty = notional / entry_fill
    volume = qty * entry_fill + qty * exit_fill
    fee = fee_pct / 100 * volume
    pnl = side * qty * (exit_fill - entry_fill) - fee
    slippage = qty * abs(entry_fill - entry_open) + qty * abs(raw_exit - exit_fill)
    return Money(entry_fill, exit_fill, qty, fee, slippage, pnl, volume)
