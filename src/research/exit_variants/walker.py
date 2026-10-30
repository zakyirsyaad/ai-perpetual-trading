"""Exit-rule variants walked over 1m bars: the live walk of ``research.mode_backtest.live_sim`` plus a profit lock.

Research code; the bot never imports it. With the null rule (no lock, no trail, the live floor as the target)
``walk`` gives the exit reason, bar and raw price of ``live_sim.resolve_live_exit``; a test pins that on random
bars of both sides. Every function takes plain numbers and reads no clock, file or setting.

Bar convention (the same as ``resolve_live_exit``): the position enters at the open of ``entry_idx``. Inside a
bar the order is the time exit at the open of the limit bar, the open gapping through the stop and then through
the target, the bar's worst price through the stop and, last, its best price through the target, so a bar that
touches both is a stop (a 1m bar does not say which came first).

A profit lock is a stop that moves to a net level once the best net seen reaches an arm level; a trailing stop
follows the peak at a fixed distance once it is armed. Nets are the bot's own estimate (gross minus ``fee_pct``
of both notionals, the number its neutral exit reads), so the lock levels are comparable with the live floor.
A lock never loosens the hard stop. Three conventions book a lock inside the 1m bar that arms or ratchets it:

* A (``same_bar`` False, ``close_check`` False) arms from the next bar: a bar's own best never arms its own stop.
  It books a bar that retraced through the new level at the next bar's open, which is worse than the level, so A
  is not a bound.
* B (``same_bar`` True) lets one bar arm the lock and stop out on it: the stop-biased reading, an extreme bound.
  The arming best is capped at the target, which would have closed the trade first.
* C (``close_check`` True) is A plus the realistic middle: a bar that arms or ratchets the stop and closes beyond
  it (read as the next bar's open) is stopped inside the bar, at the level.

The bad-case bound is B and the realistic reading C; the money of a settled walk is ``settle``.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Mapping, NamedTuple, Optional, Sequence

from bot.strategy.modes.checks import (
    LONG,
    SHORT,
    check_side,
    is_finite,
    is_whole,
    require_not_negative,
    require_positive,
)
from research.mode_backtest.simulate import reaches_sl, reaches_tp
from research.pine_gate.features import INTERVAL_1M

__all__ = [
    "LOCK",
    "LONG",
    "SHORT",
    "SL",
    "TP",
    "WAKTU",
    "Cols",
    "Costs",
    "ExitRule",
    "Money",
    "Outcome",
    "day_bootstrap",
    "group_by_day",
    "mean_se",
    "net_for_price",
    "price_for_net",
    "settle",
    "walk",
]

TP, SL, WAKTU, LOCK = "TP", "SL", "WAKTU", "LOCK"  # the exit reasons; WAKTU is the time exit (Indonesian: time)
_BPS_PER_UNIT = 10_000.0  # 1 bp = 1 / 10,000
_PERCENT = 100.0
_BOOTSTRAP_LOW, _BOOTSTRAP_HIGH = 0.025, 0.975


class Cols(NamedTuple):
    """The four 1m columns the walk reads, aligned by index; ``open_ms`` is the bar open in UTC milliseconds."""

    open_ms: Sequence[int]
    open: Sequence[float]
    high: Sequence[float]
    low: Sequence[float]


class Outcome(NamedTuple):
    """How a walked position ended: ``reason`` is TP, SL, WAKTU or LOCK (a profit lock or a trailing stop).

    ``raw_price`` is the exit price before slippage: the level for a touch, the bar's open for a gap and for the
    time exit. ``peak_net`` is the best net (USDT) seen before the exit bar, ``-inf`` for an exit on the first bar.
    """

    reason: str
    idx: int
    raw_price: float
    gapped: bool
    peak_net: float


@dataclass(frozen=True)
class ExitRule:
    """One exit rule: the live bracket plus an optional profit lock or trailing stop.

    ``tp_net`` is the live neutral floor as the net the bot's estimate must reach; ``limit_min`` the time limit in
    bars after the entry bar. ``lock_steps`` are ``(arm_net, lock_net)`` pairs: once the peak net reaches ``arm_net``
    the stop sits at ``lock_net`` (the highest armed step wins). ``trail_arm`` arms a stop ``trail_dist`` USDT of net
    behind the peak. ``same_bar`` and ``close_check`` choose the booking convention (module docstring)."""

    name: str
    tp_net: float = 56.0
    limit_min: int = 60
    lock_steps: tuple[tuple[float, float], ...] = ()
    trail_arm: Optional[float] = None
    trail_dist: float = 0.0
    same_bar: bool = False
    close_check: bool = False


class Costs(NamedTuple):
    """Slippage assumptions in bps, all adverse: a market exit (stop, lock, time) gives up ``exit_market_bps``, the
    take-profit ``tp_bps`` (0 when the target rests as a limit order) and a lock exit ``lock_extra_bps`` more."""

    exit_market_bps: float
    tp_bps: float
    lock_extra_bps: float


class Money(NamedTuple):
    """The money of one settled walk: ``net`` in USDT after the fee and slippage, ``gross_bps`` of the entry notional
    before the fee, the exit fill and the bars held after the entry bar."""

    net: float
    gross_bps: float
    exit_fill: float
    hold_min: int


def price_for_net(entry: float, side: int, notional: float, fee_pct: float, net: float) -> float:
    """The price at which the bot's net estimate (gross minus ``fee_pct`` percent of both notionals) equals ``net``.

    With ``r = fee_pct / 100`` and ``q = notional / entry``: a LONG is ``(entry (1 + r) + net / q) / (1 - r)`` and a
    SHORT ``(entry (1 - r) - net / q) / (1 + r)``; the inverse of ``net_for_price``. Raises ``ValueError`` for a
    price that is not above 0 (a SHORT target too large for the notional)."""
    check_side(side)
    require_positive("entry", entry)
    require_positive("notional", notional)
    require_not_negative("fee_pct", fee_pct)
    if not is_finite(net):
        raise ValueError(f"net must be a finite number, got {net!r}")
    rate, qty = fee_pct / _PERCENT, notional / entry
    if side == LONG:
        price = (entry * (1 + rate) + net / qty) / (1 - rate)
    else:
        price = (entry * (1 - rate) - net / qty) / (1 + rate)
    if not price > 0:
        raise ValueError(f"a net of {net!r} USDT needs a price of {price!r}, which is not above 0")
    return price


def net_for_price(entry: float, side: int, notional: float, fee_pct: float, price: float) -> float:
    """The bot's net estimate (USDT) of a position entered at ``entry`` if it closed at ``price``."""
    qty, rate = notional / entry, fee_pct / _PERCENT
    return side * qty * (price - entry) - rate * qty * (entry + price)


def _stop_net(rule: ExitRule, peak: float) -> Optional[float]:
    """The net level of the stop the rule has armed at ``peak``, or ``None`` while nothing is armed."""
    level: Optional[float] = None
    for arm, lock in rule.lock_steps:
        if peak >= arm:
            level = lock if level is None else max(level, lock)
    if rule.trail_arm is not None and peak >= rule.trail_arm:
        trailed = peak - rule.trail_dist
        level = trailed if level is None else max(level, trailed)
    return level


def _stop_level(
    entry: float, side: int, notional: float, fee_pct: float, hard_stop: float, stop_net: Optional[float]
) -> tuple[float, str]:
    """The stop in force as ``(price, kind)``: the lock level once armed, never looser than the hard stop."""
    if stop_net is None:
        return hard_stop, SL
    lock = price_for_net(entry, side, notional, fee_pct, stop_net)
    tighter = lock <= hard_stop if side == SHORT else lock >= hard_stop
    return (lock, LOCK) if tighter else (hard_stop, SL)


def _check_walk(
    cols: Cols,
    entry_idx: int,
    side: int,
    entry_fill: float,
    sl_pct: float,
    notional: float,
    fee_pct: float,
    rule: ExitRule,
) -> None:
    check_side(side)
    require_positive("entry_fill", entry_fill)
    require_positive("sl_pct", sl_pct)
    require_positive("notional", notional)
    require_not_negative("fee_pct", fee_pct)
    require_positive("tp_net", rule.tp_net)
    require_not_negative("trail_dist", rule.trail_dist)
    if not is_whole(entry_idx) or not 0 <= entry_idx < len(cols.open_ms):
        raise ValueError(f"entry_idx must be a bar index from 0 to {len(cols.open_ms) - 1}, got {entry_idx!r}")
    if not is_whole(rule.limit_min) or rule.limit_min < 1:
        raise ValueError(f"limit_min must be a whole number of minutes of at least 1, got {rule.limit_min!r}")


def walk(
    cols: Cols,
    entry_idx: int,
    side: int,
    entry_fill: float,
    sl_pct: float,
    notional: float,
    fee_pct: float,
    rule: ExitRule,
) -> Optional[Outcome]:
    """How a position entered at the open of bar ``entry_idx`` ends under ``rule``; ``None`` if the data ends first.

    ``entry_fill`` is the real fill, from which the hard stop (``sl_pct`` percent away) and the nets are measured,
    ``notional`` the position size in USDT at that fill. A bar that is not one minute after the one before it is a hole
    in the data and ends the walk as the end of the data does. Raises ``ValueError`` for numbers that cannot give an
    answer (a side that is not LONG or SHORT, a non-positive price, stop distance or notional, a bad index or limit)."""
    _check_walk(cols, entry_idx, side, entry_fill, sl_pct, notional, fee_pct, rule)
    hard_stop = entry_fill * (1 - side * sl_pct / _PERCENT)
    target = price_for_net(entry_fill, side, notional, fee_pct, rule.tp_net)
    limit_idx = entry_idx + rule.limit_min
    last = len(cols.open_ms) - 1
    peak = -math.inf

    def stop_for(peak_net: float) -> tuple[float, str]:
        return _stop_level(entry_fill, side, notional, fee_pct, hard_stop, _stop_net(rule, peak_net))

    for i in range(entry_idx, min(limit_idx, last) + 1):
        if i > entry_idx and cols.open_ms[i] != cols.open_ms[i - 1] + INTERVAL_1M:
            return None
        bar_open = cols.open[i]
        if i == limit_idx:
            return Outcome(WAKTU, i, bar_open, False, peak)
        worst, best = (cols.low[i], cols.high[i]) if side == LONG else (cols.high[i], cols.low[i])
        best_net = net_for_price(entry_fill, side, notional, fee_pct, best)
        stop_open = stop_for(peak)  # armed before this bar
        if reaches_sl(side, bar_open, stop_open[0]):
            return Outcome(stop_open[1], i, bar_open, True, peak)
        if reaches_tp(side, bar_open, target):
            return Outcome(TP, i, bar_open, True, peak)
        # B: this bar's own best may arm the stop, capped at the target, which would have closed the trade first
        stop_bar = stop_for(max(peak, min(best_net, rule.tp_net))) if rule.same_bar else stop_open
        if reaches_sl(side, worst, stop_bar[0]):
            return Outcome(stop_bar[1], i, stop_bar[0], False, peak)
        if reaches_tp(side, best, target):
            return Outcome(TP, i, target, False, peak)
        armed_before = _stop_net(rule, peak)
        peak = max(peak, best_net)
        closes_next = i + 1 <= last and cols.open_ms[i + 1] == cols.open_ms[i] + INTERVAL_1M
        if rule.close_check and not rule.same_bar and closes_next:
            armed_now = _stop_net(rule, peak)
            if armed_now is not None and (armed_before is None or armed_now > armed_before):
                level, kind = stop_for(peak)  # only a stop this bar armed or ratcheted
                if kind == LOCK and reaches_sl(side, cols.open[i + 1], level):  # the bar closed back beyond it
                    return Outcome(LOCK, i, level, False, peak)
    return None


def settle(
    entry_fill: float,
    side: int,
    outcome: Outcome,
    entry_idx: int,
    notional: float,
    fee_pct: float,
    costs: Costs,
) -> Money:
    """The money of a position whose entry already filled at ``entry_fill`` (slippage included).

    The same formulas as ``live_sim.settle_live``: the exit fill gives up ``costs.tp_bps`` for a take-profit and
    ``costs.exit_market_bps`` for every other exit, plus ``costs.lock_extra_bps`` for a lock exit; ``qty`` is
    ``notional / entry_fill``, the fee ``fee_pct`` percent of both legs and the net the gross minus that fee.
    Raises ``ValueError`` for numbers that cannot give an answer."""
    check_side(side)
    require_positive("entry_fill", entry_fill)
    require_positive("raw exit price", outcome.raw_price)
    require_positive("notional", notional)
    require_not_negative("fee_pct", fee_pct)
    for name, slippage in costs._asdict().items():
        require_not_negative(name, slippage)
    bps = costs.tp_bps if outcome.reason == TP else costs.exit_market_bps
    if outcome.reason == LOCK:
        bps += costs.lock_extra_bps
    exit_fill = outcome.raw_price * (1 - side * bps / _BPS_PER_UNIT)  # adverse: a LONG sells lower, a SHORT buys higher
    qty = notional / entry_fill
    fee = fee_pct / _PERCENT * (qty * entry_fill + qty * exit_fill)
    gross = side * qty * (exit_fill - entry_fill)
    return Money(gross - fee, gross / notional * _BPS_PER_UNIT, exit_fill, outcome.idx - entry_idx)


# ----------------------------------------------------------------------------- statistics


def mean_se(values: Sequence[float]) -> tuple[float, float]:
    """The mean and the standard error of the mean; ``nan`` for the error of one value and for both of none."""
    count = len(values)
    if count == 0:
        return math.nan, math.nan
    mean = sum(values) / count
    if count < 2:
        return mean, math.nan
    variance = sum((value - mean) ** 2 for value in values) / (count - 1)
    return mean, math.sqrt(variance / count)


def group_by_day(values: Sequence[float], days: Sequence[str]) -> dict[str, list[float]]:
    """``values`` grouped by the day label at the same index, each group in the order given."""
    grouped: dict[str, list[float]] = defaultdict(list)
    for value, day in zip(values, days):
        grouped[day].append(value)
    return dict(grouped)


def day_bootstrap(
    by_day: Mapping[str, Sequence[float]], draws: int = 2000, seed: int = 7
) -> tuple[float, float, float]:
    """The mean per trade with a 95 % interval from resampling whole days (the trades of a day are not independent).

    Returns ``(point, low, high)``; all ``nan`` when no day has a trade. The resampling is seeded, so a rerun on the
    same data gives the same interval."""
    days = [day for day, values in by_day.items() if values]
    if not days:
        return math.nan, math.nan, math.nan
    totals = [sum(by_day[day]) for day in days]
    counts = [len(by_day[day]) for day in days]
    point = sum(totals) / sum(counts)
    rng = random.Random(seed)
    means = []
    for _ in range(draws):
        picks = [rng.randrange(len(days)) for _ in days]
        means.append(sum(totals[k] for k in picks) / sum(counts[k] for k in picks))
    means.sort()
    return point, means[int(_BOOTSTRAP_LOW * draws)], means[int(_BOOTSTRAP_HIGH * draws) - 1]
