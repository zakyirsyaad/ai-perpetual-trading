"""Tick-clock replay of the live bot's execution with its daily guards.

Research code; the bot never imports it. Standard library only.

One position across all markets. A tick evaluates the markets in the given
order (callers pass BTC first) at the tick time ``t``; the signal function must
read only data closed by ``t``. A tick without an entry costs ``idle_tick_s``
and the next starts ``tick_s`` later. An entry draws an AI latency, sends the
order ``order_delay_s`` after it, fills at the close of the 1m bar containing
the order time, and holds until the bracket outcome; the next tick starts
``tick_s`` after the exit. Daily guards are keyed on WIB calendar days.
"""

from __future__ import annotations

import random
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from .bars import Bars, wib_day, wib_day_start_ms
from .outcomes import OutcomeCache, gross_bps
from .spec import ExecConfig, GuardConfig

SignalFn = Callable[[str, int], int]

_MS_PER_MIN = 60_000
_MS_PER_HOUR = 60 * _MS_PER_MIN
_MS_PER_DAY = 24 * _MS_PER_HOUR
_WIB_OFFSET_MS = 7 * _MS_PER_HOUR
_EPOCH_WEEKDAY = 3  # 1970-01-01 was a Thursday (Monday = 0)
_MONDAY, _SATURDAY = 0, 5
_MONDAY_OPEN_HOUR = 7
_BPS = 1e4


@dataclass(frozen=True)
class Market:
    symbol: str
    bars_1m: Bars
    outcomes: OutcomeCache
    funding_ms: Tuple[int, ...]


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: int
    signal_ms: int
    entry_ms: int
    exit_ms: int
    kind: str
    gross_bps: float
    net_usdt: float
    wib_day: date
    both_touched: bool = False


def in_entry_window(t_ms: int) -> bool:
    """WIB time in [Mon 07:00, Sat 00:00): Mon from 07:00, Tue-Fri all day."""
    local = t_ms + _WIB_OFFSET_MS
    weekday = (local // _MS_PER_DAY + _EPOCH_WEEKDAY) % 7
    if weekday == _MONDAY:
        return local % _MS_PER_DAY >= _MONDAY_OPEN_HOUR * _MS_PER_HOUR
    return weekday < _SATURDAY


def in_funding_blackout(t_ms: int, funding_ms: Sequence[int], blackout_s: int = 600) -> bool:
    """Whether the next funding time at or after ``t_ms`` is at most ``blackout_s`` away."""
    i = bisect_left(funding_ms, t_ms)
    return i < len(funding_ms) and funding_ms[i] - t_ms <= blackout_s * 1000


def trading_days(start_ms: int, end_ms: int) -> List[date]:
    """Mon-Fri WIB days whose 00:00 WIB lies in [start_ms, end_ms)."""
    day = wib_day(start_ms)
    if wib_day_start_ms(day) < start_ms:
        day += timedelta(days=1)
    out = []
    while wib_day_start_ms(day) < end_ms:
        if day.weekday() < _SATURDAY:
            out.append(day)
        day += timedelta(days=1)
    return out


def _merge_windows(windows: Iterable[Tuple[int, int]]) -> Tuple[List[int], List[int]]:
    """Sorted, non-overlapping half-open windows as parallel start/end lists."""
    starts: List[int] = []
    ends: List[int] = []
    for a, b in sorted((int(a), int(b)) for a, b in windows if b > a):
        if ends and a <= ends[-1]:
            ends[-1] = max(ends[-1], b)
        else:
            starts.append(a)
            ends.append(b)
    return starts, ends


def simulate(
    signal: SignalFn,
    markets: Sequence[Market],
    start_ms: int,
    end_ms: int,
    *,
    exec_cfg: ExecConfig,
    guard_cfg: GuardConfig,
    latencies_ms: Sequence[int],
    seed: int,
    overshoot_bps: float,
    offset_s: int = 0,
    blocked_entry_windows: Iterable[Tuple[int, int]] = (),
    fee_pct: Optional[float] = None,
) -> List[Trade]:
    """Replay ticks in [start_ms + offset_s, end_ms) and return the closed trades.

    The run stops early, returning the trades so far, when the order bar does
    not exist or the data ends before the bracket resolves.
    """
    rng = random.Random(seed)
    latencies = tuple(int(x) for x in latencies_ms)
    blocked_starts, blocked_ends = _merge_windows(blocked_entry_windows)

    fee = exec_cfg.fee_pct if fee_pct is None else fee_pct
    round_trip_fee_bps = 2 * fee * 100.0
    notional = exec_cfg.notional_usdt
    trade_volume = 2 * notional
    idle_step = (exec_cfg.idle_tick_s + exec_cfg.tick_s) * 1000
    after_exit = exec_cfg.tick_s * 1000
    order_delay = exec_cfg.order_delay_s * 1000
    cooldown = exec_cfg.cooldown_min * _MS_PER_MIN
    blackout_s = exec_cfg.funding_blackout_s

    max_stops = guard_cfg.max_stops
    max_losses = guard_cfg.max_consecutive_losses
    min_net = -guard_cfg.max_drawdown_usdt
    max_entries = guard_cfg.max_entries
    max_volume = guard_cfg.max_volume_usdt

    trades: List[Trade] = []
    next_entry_ms = None  # cooldown end
    day = None  # WIB day index of the guard counters
    stops = losses = entries = 0
    day_net = volume = 0.0

    t = start_ms + offset_s * 1000
    while t < end_ms:
        tick_day = (t + _WIB_OFFSET_MS) // _MS_PER_DAY
        if tick_day != day:
            day = tick_day
            stops = losses = entries = 0
            day_net = volume = 0.0

        chosen = None
        if (
            in_entry_window(t)
            and not _in_windows(t, blocked_starts, blocked_ends)
            and (next_entry_ms is None or t >= next_entry_ms)
            and stops < max_stops
            and losses < max_losses
            and day_net > min_net
            and entries < max_entries
            and volume + trade_volume <= max_volume
        ):
            for market in markets:
                if in_funding_blackout(t, market.funding_ms, blackout_s):
                    continue
                side = signal(market.symbol, t)
                if side == 0:
                    continue
                if side not in (1, -1):
                    raise ValueError(f"signal for {market.symbol} returned {side!r}")
                chosen = (market, side)
                break

        if chosen is None:
            t += idle_step
            continue

        market, side = chosen
        latency = rng.choice(latencies) if latencies else 0
        order_ms = t + latency + order_delay
        open_ms = market.bars_1m.open_ms
        entry_idx = bisect_right(open_ms, order_ms) - 1
        if entry_idx < 0 or order_ms >= open_ms[entry_idx] + _MS_PER_MIN:
            break
        outcome = market.outcomes.get(entry_idx, side)
        if outcome is None:
            break

        entry_ms = open_ms[entry_idx] + _MS_PER_MIN
        exit_ms = open_ms[outcome.exit_idx] + (0 if outcome.gapped else _MS_PER_MIN)
        gross = gross_bps(outcome, side, overshoot_bps, exec_cfg)
        net = notional * (gross - round_trip_fee_bps) / _BPS
        trades.append(
            Trade(
                market.symbol, side, t, entry_ms, exit_ms, outcome.kind, gross, net,
                wib_day(entry_ms), outcome.both_touched,
            )
        )

        entry_day = (entry_ms + _WIB_OFFSET_MS) // _MS_PER_DAY
        if entry_day != day:
            day = entry_day
            stops = losses = entries = 0
            day_net = volume = 0.0
        entries += 1
        volume += trade_volume
        day_net += net
        if outcome.kind == "SL":
            stops += 1
        losses = losses + 1 if net < 0 else 0

        next_entry_ms = entry_ms + cooldown
        t = exit_ms + after_exit
    return trades


def _in_windows(t_ms: int, starts: List[int], ends: List[int]) -> bool:
    i = bisect_right(starts, t_ms) - 1
    return i >= 0 and t_ms < ends[i]
