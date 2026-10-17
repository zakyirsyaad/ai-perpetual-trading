"""Position rules of the LONG module: the price arithmetic of a position and the gates of an entry.

The event loop of the offline backtest applies these to every signal, and a later phase applies the
same functions to the live engine's, so each one is small and takes numbers (the weekly window is
the one other thing it takes). ``side`` is ``LONG`` (+1) or ``SHORT`` (-1).

Prices (spec section 2.6, decision D6):

* ``bracket_prices``: the take-profit is ``entry x (1 + side x tp / 100)`` and the stop-loss is
  ``entry x (1 - side x sl / 100)``, with the percents of the mode (Mode A LONG 0.40 and 0.25,
  Mode D SHORT 0.20 and 0.20). They are set from the entry *fill*, the slippage included.
* ``adverse_fill``: a market order fills ``slippage_pct`` percent against the position (0.01 in
  the document). An opening fill pays up (LONG x (1 + s), SHORT x (1 - s)), a closing fill gives up
  (LONG x (1 - s), SHORT x (1 + s)). The take-profit and the stop-loss exits are market orders too.

Gates (spec section 2.7), each answering "may a new position open?":

* ``entry_window_open``: the weekly window (``mon 07:00-sat 00:00``), read as a WIB wall-clock time
  through the live ``bot.core.entry_window``. ``None`` means always open.
* ``pause_over``: 15 minutes per pair after an exit. An entry exactly 15 minutes after the exit is
  allowed.
* ``under_daily_caps``: at most 6 entries per pair and 10 in total per WIB day. The caller counts
  the entries already opened that day, the one being checked not included.
* ``volatility_ok``: skip a signal when the 1h ATR14 is above twice its median. Equal passes.

A gate that has no usable number refuses the entry, so a missing input never lets one through:
``volatility_ok`` is False when either value is ``None`` or NaN. The price functions cannot say
"no", so they raise ``ValueError`` instead of returning a wrong price: for a ``side`` that is not
``LONG`` or ``SHORT``, a ``closing`` that is not a bool, and a price or percent that is not a
finite number above 0 (a slippage of 0 is allowed), because any of these would put a level on the
wrong side of the entry or fill in the position's favour without a word.

The module takes numbers only: no I/O, no bars, and no indicator or research code. No live module
imports it.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.core.entry_window import WeeklyWindow, window_is_open

__all__ = [
    "LONG",
    "SHORT",
    "Bracket",
    "adverse_fill",
    "bracket_prices",
    "entry_window_open",
    "pause_over",
    "under_daily_caps",
    "volatility_ok",
]

LONG = 1
SHORT = -1

# Asia/Jakarta (the config's ``zona_waktu``) has been UTC+7 without daylight saving time since
# 1964, and the research side reads WIB the same way, so a fixed offset is the same zone and needs
# no tzdata.
_WIB = timezone(timedelta(hours=7), "WIB")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MS_PER_MINUTE = 60_000


@dataclass(frozen=True)
class Bracket:
    """The take-profit and the stop-loss price of an open position."""

    tp: float
    sl: float


def _check_side(side: object) -> None:
    if isinstance(side, bool) or side not in (LONG, SHORT):
        raise ValueError(f"side must be LONG ({LONG}) or SHORT ({SHORT}), got {side!r}")


def _is_finite(value: object) -> bool:
    """Whether ``value`` is a finite number; None, text, NaN and infinity are not."""
    try:
        return math.isfinite(value)
    except (TypeError, OverflowError):  # not a number at all; an int too large for a float
        return False


def _require_positive(name: str, value: float) -> None:
    if not (_is_finite(value) and value > 0):
        raise ValueError(f"{name} must be a finite number above 0, got {value!r}")


def _require_not_negative(name: str, value: float) -> None:
    if not (_is_finite(value) and value >= 0):
        raise ValueError(f"{name} must be a finite number of at least 0, got {value!r}")


def bracket_prices(entry_fill: float, side: int, tp_pct: float, sl_pct: float) -> Bracket:
    """The take-profit and the stop-loss of a position that filled at ``entry_fill``.

    ``tp_pct`` and ``sl_pct`` are percents (0.40 is 0.40 %). A LONG takes profit above the fill and
    stops out below it; a SHORT the other way round. Raises ``ValueError`` for a ``side`` that is
    not ``LONG`` or ``SHORT`` and for a fill or percent that is not a finite number above 0."""
    _check_side(side)
    _require_positive("entry_fill", entry_fill)
    _require_positive("tp_pct", tp_pct)
    _require_positive("sl_pct", sl_pct)
    return Bracket(
        tp=entry_fill * (1 + side * tp_pct / 100),
        sl=entry_fill * (1 - side * sl_pct / 100),
    )


def adverse_fill(price: float, side: int, slippage_pct: float, closing: bool) -> float:
    """The fill of a market order at ``price``, ``slippage_pct`` percent against the position.

    ``closing`` is False for the order that opens the position and True for the one that closes it.
    Raises ``ValueError`` for a ``side`` that is not ``LONG`` or ``SHORT``, a ``closing`` that is
    not a bool, a ``price`` that is not a finite number above 0 and a ``slippage_pct`` that is not
    a finite number of at least 0."""
    _check_side(side)
    if not isinstance(closing, bool):
        raise ValueError(f"closing must be True (an exit) or False (an entry), got {closing!r}")
    _require_positive("price", price)
    _require_not_negative("slippage_pct", slippage_pct)
    against = -side if closing else side
    return price * (1 + against * slippage_pct / 100)


def _wib_moment(t_ms: int) -> datetime:
    """The WIB wall-clock datetime of a UTC millisecond timestamp, by exact integer arithmetic."""
    return (_EPOCH + timedelta(milliseconds=t_ms)).astimezone(_WIB)


def entry_window_open(window: Optional[WeeklyWindow], t_ms: int) -> bool:
    """Whether the weekly entry ``window`` is open at ``t_ms``, UTC milliseconds.

    The window is read in WIB, to the minute: its start is included and its end is not, so
    ``mon 07:00-sat 00:00`` is open at Monday 07:00:00.000 and closed at Saturday 00:00:00.000.
    ``None`` means always open."""
    return window_is_open(window, _wib_moment(t_ms))


def pause_over(t_ms: int, last_exit_ms: Optional[int], pause_min: float) -> bool:
    """Whether the pair's pause is over at ``t_ms``: ``pause_min`` minutes after its last exit.

    Exactly ``pause_min`` minutes after the exit it is over. A pair that has not exited yet
    (``last_exit_ms`` is ``None``) is never paused."""
    return last_exit_ms is None or t_ms >= last_exit_ms + pause_min * _MS_PER_MINUTE


def under_daily_caps(pair_today: int, total_today: int, max_pair: int, max_total: int) -> bool:
    """Whether one more entry fits under both daily caps.

    ``pair_today`` and ``total_today`` count the entries already opened on this WIB day, on the
    pair and in total; the entry being checked is not among them."""
    return pair_today < max_pair and total_today < max_total


def volatility_ok(atr14_1h: Optional[float], atr_median: Optional[float], mult: float) -> bool:
    """Whether the 1h ATR14 is at most ``mult`` times its median: the volatility guard.

    A value that is missing (``None``) or not a number (NaN) fails the guard, and so does an ATR
    above ``mult x atr_median``; an ATR exactly at the limit passes."""
    if atr14_1h is None or atr_median is None:
        return False
    return atr14_1h <= mult * atr_median
