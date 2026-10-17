"""The 1m exit walk and the money of a simulated trade for the mode backtest.

Research code; the bot never imports it. ``resolve_exit`` walks 1m bars from the bar a position
entered on and says how it ended; ``settle`` turns the entry open and the raw exit price into the
fills, the fee and the profit of the trade. Both take plain numbers and bars and read no clock,
file or setting, so the same call always gives the same answer.

The walk (spec sections 2.6 and 9.1, decisions D6 and D7):

* The entry fills at the open of the entry bar, so the walk starts on that bar and its whole range
  counts. The caller sets ``tp`` and ``sl`` from the entry *fill*, slippage included (D6).
* Order inside a bar: the time limit, then an open beyond the SL, then an open beyond the TP, then
  a touch of the SL, then a touch of the TP. A LONG is beyond its SL at or below it and beyond its
  TP at or above it; a SHORT is the mirror image. A touch is the bar's range reaching the level
  (low <= sl and high >= tp for a LONG, high >= sl and low <= tp for a SHORT), the level itself
  included.
* A bar that opens beyond a level exits at its open (``gapped``). A bar that only reaches a level
  exits at the level. A bar that reaches both without opening beyond either is taken as the SL,
  because a 1m bar does not say which came first and the stop-first rule is the pessimistic one
  (``both_touched``).
* The time limit (D7) ends the position at the open of the bar ``limit_min`` minutes after the
  entry bar, before that bar's levels are looked at, so its range does not count.
* The walk needs every minute. A bar that is not exactly one minute after the one before it is a
  hole in the data and ends the walk as the end of the data does: ``resolve_exit`` returns
  ``None``, because what the price did inside the hole is not known.

The money (spec section 9.1) has no venue rounding: the quantity is the notional over the entry
fill. Both fills come from ``adverse_fill``, so a market order gives up ``slippage_pct`` percent
at the entry and again at the exit, and the take-profit and the stop-loss are market orders too.

A number that cannot give a meaningful answer raises ``ValueError`` and is never passed on as a
price: a ``side`` that is not ``LONG`` or ``SHORT``, a bar index or a limit that is not a whole
number in range, levels that are not finite or are on the wrong sides of each other, bars that are
not 1m, and a notional or a fee that is not a finite number in range.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from bot.strategy.modes.position_rules import LONG, SHORT, adverse_fill
from research.pine_gate.bars import Bars
from research.pine_gate.features import INTERVAL_1M

__all__ = [
    "EXIT_SL",
    "EXIT_TP",
    "EXIT_WAKTU",
    "ExitOutcome",
    "Money",
    "resolve_exit",
    "settle",
]

EXIT_TP = "TP"
EXIT_SL = "SL"
EXIT_WAKTU = "WAKTU"


@dataclass(frozen=True)
class ExitOutcome:
    """How a position ended.

    ``reason`` is ``EXIT_TP``, ``EXIT_SL`` or ``EXIT_WAKTU``. ``exit_idx`` is the index, in the
    walked bars, of the bar the exit happens in; a touch is only known to the minute. ``raw_price``
    is the exit price before its slippage: the level for a touch and the bar's open for a gap and
    for the time exit. ``gapped`` is True for an exit at the open of a bar that opened beyond a
    level. ``both_touched`` is True for a stop-loss taken at the SL price from a bar whose range
    also reached the take-profit: the one case the stop-first rule decided. A gap is not that case
    (the open comes first, so the order is known) and neither is a time exit."""

    reason: str
    exit_idx: int
    raw_price: float
    gapped: bool
    both_touched: bool


@dataclass(frozen=True)
class Money:
    """The money of one trade, in USDT unless it is a price or a quantity.

    ``entry_fill`` and ``exit_fill`` are the prices after slippage and ``qty`` the base-asset
    quantity. ``fee_usdt`` is both taker fees. ``slippage_usdt`` is the cost of the two slippage
    adjustments: informational and positive, already inside the fills and so inside
    ``pnl_usdt``, which is after fees. ``volume_usdt`` is the entry notional plus the exit
    notional."""

    entry_fill: float
    exit_fill: float
    qty: float
    fee_usdt: float
    slippage_usdt: float
    pnl_usdt: float
    volume_usdt: float


def _is_whole(value: object) -> bool:
    """Whether ``value`` is an ``int``; a bool is not one for this purpose."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite(value: object) -> bool:
    """Whether ``value`` is a finite number; None, text, a bool, NaN and infinity are not."""
    if isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):  # not a number at all; an int too large for a float
        return False


def _require_positive(name: str, value: object) -> None:
    if not (_is_finite(value) and value > 0):
        raise ValueError(f"{name} must be a finite number above 0, got {value!r}")


def _require_not_negative(name: str, value: object) -> None:
    if not (_is_finite(value) and value >= 0):
        raise ValueError(f"{name} must be a finite number of at least 0, got {value!r}")


def _check_side(side: object) -> None:
    if not _is_whole(side) or side not in (LONG, SHORT):
        raise ValueError(f"side must be LONG ({LONG}) or SHORT ({SHORT}), got {side!r}")


def _reaches_sl(side: int, price: float, sl: float) -> bool:
    """Whether ``price`` is at or beyond the stop-loss.

    A LONG's stop is below its entry, so beyond it is at or below; a SHORT's is the mirror."""
    return price <= sl if side == LONG else price >= sl


def _reaches_tp(side: int, price: float, tp: float) -> bool:
    """Whether ``price`` is at or beyond the take-profit.

    A LONG's target is above its entry, so beyond it is at or above; a SHORT's is the mirror."""
    return price >= tp if side == LONG else price <= tp


def _check_walk(
    bars_1m: Bars, entry_idx: int, side: int, tp: float, sl: float, limit_min: int
) -> None:
    if bars_1m.interval_ms != INTERVAL_1M:
        raise ValueError(f"expected 1m bars, got {bars_1m.interval_ms} ms")
    _check_side(side)
    if not _is_whole(entry_idx) or not 0 <= entry_idx < len(bars_1m):
        raise ValueError(
            f"entry_idx must be a bar index from 0 to {len(bars_1m) - 1}, got {entry_idx!r}"
        )
    if not _is_whole(limit_min) or limit_min < 1:
        raise ValueError(
            f"limit_min must be a whole number of minutes of at least 1, got {limit_min!r}"
        )
    _require_positive("tp", tp)
    _require_positive("sl", sl)
    if not (tp > sl if side == LONG else tp < sl):
        where = "above" if side == LONG else "below"
        raise ValueError(f"the take-profit must be {where} the stop-loss, got tp {tp!r}, sl {sl!r}")


def resolve_exit(
    bars_1m: Bars, entry_idx: int, side: int, tp: float, sl: float, limit_min: int
) -> Optional[ExitOutcome]:
    """How the position that entered at the open of bar ``entry_idx`` ends, or ``None``.

    ``tp`` and ``sl`` are the take-profit and the stop-loss prices (a LONG has ``sl < tp``, a
    SHORT ``tp < sl``) and ``limit_min`` the time limit in minutes. The walk goes from the entry
    bar on, bar by bar, in the order of the module docstring, and stops at the first bar that
    decides. It returns ``None`` when the data ends before that, which includes a hole in the
    minutes and the case where the bar of the time exit is not in the data.

    Raises ``ValueError`` for bars that are not 1m, a ``side`` that is not ``LONG`` or ``SHORT``,
    an ``entry_idx`` outside the bars, a ``limit_min`` that is not a whole number of at least 1,
    and levels that are not finite positive numbers or are on the wrong sides of each other."""
    _check_walk(bars_1m, entry_idx, side, tp, sl, limit_min)
    open_ms, opens, highs, lows = bars_1m.open_ms, bars_1m.open, bars_1m.high, bars_1m.low
    limit_idx = entry_idx + limit_min
    for i in range(entry_idx, min(limit_idx, len(bars_1m) - 1) + 1):
        if i > entry_idx and open_ms[i] != open_ms[i - 1] + INTERVAL_1M:
            return None
        bar_open = opens[i]
        if i == limit_idx:
            return ExitOutcome(EXIT_WAKTU, i, bar_open, False, False)
        if _reaches_sl(side, bar_open, sl):
            return ExitOutcome(EXIT_SL, i, bar_open, True, False)
        if _reaches_tp(side, bar_open, tp):
            return ExitOutcome(EXIT_TP, i, bar_open, True, False)
        worst, best = (lows[i], highs[i]) if side == LONG else (highs[i], lows[i])
        if _reaches_sl(side, worst, sl):
            return ExitOutcome(EXIT_SL, i, float(sl), False, _reaches_tp(side, best, tp))
        if _reaches_tp(side, best, tp):
            return ExitOutcome(EXIT_TP, i, float(tp), False, False)
    return None


def settle(
    entry_open: float,
    raw_exit: float,
    side: int,
    notional: float,
    fee_pct: float,
    slippage_pct: float,
) -> Money:
    """The money of a trade that entered at ``entry_open`` and exited at ``raw_exit``.

    ``fee_pct`` and ``slippage_pct`` are percents (0.035 and 0.01 in the document) and
    ``notional`` is the position size in USDT at the entry fill. With
    ``entry_fill = adverse_fill(entry_open, side, slippage_pct, closing=False)`` and
    ``exit_fill = adverse_fill(raw_exit, side, slippage_pct, closing=True)``:

    * ``qty = notional / entry_fill``;
    * ``volume = qty x entry_fill + qty x exit_fill``;
    * ``fee = fee_pct / 100 x volume``;
    * ``pnl = side x qty x (exit_fill - entry_fill) - fee``;
    * ``slippage`` is what the two adjustments cost: ``qty x |entry_fill - entry_open|`` plus
      ``qty x |raw_exit - exit_fill|``.

    Raises ``ValueError`` for a ``side`` that is not ``LONG`` or ``SHORT``, a price that is not a
    finite number above 0, a ``notional`` that is not a finite number above 0, and a ``fee_pct`` or
    ``slippage_pct`` that is not a finite number of at least 0."""
    _check_side(side)
    _require_positive("notional", notional)
    _require_not_negative("fee_pct", fee_pct)
    entry_fill = adverse_fill(entry_open, side, slippage_pct, False)
    exit_fill = adverse_fill(raw_exit, side, slippage_pct, True)
    qty = notional / entry_fill
    volume = qty * entry_fill + qty * exit_fill
    fee = fee_pct / 100 * volume
    pnl = side * qty * (exit_fill - entry_fill) - fee
    slippage = qty * abs(entry_fill - entry_open) + qty * abs(raw_exit - exit_fill)
    return Money(entry_fill, exit_fill, qty, fee, slippage, pnl, volume)
