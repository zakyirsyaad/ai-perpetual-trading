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

from .bars import Bars, entry_index_for, wib_day, wib_day_start_ms
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


class _DayGuards:
    """The live bot's daily guards, keyed on the WIB calendar day of a time.

    ``roll`` resets every counter when the day changes; ``allows`` says whether a
    new entry may open; ``record`` books a filled trade on its entry's day."""

    def __init__(self, cfg: GuardConfig, trade_volume: float) -> None:
        self._cfg = cfg
        self._trade_volume = trade_volume
        self._day: Optional[int] = None
        self._reset()

    def _reset(self) -> None:
        self.stops = self.losses = self.entries = 0
        self.day_net = self.volume = 0.0

    def roll(self, t_ms: int) -> None:
        day = (t_ms + _WIB_OFFSET_MS) // _MS_PER_DAY
        if day != self._day:
            self._day = day
            self._reset()

    def allows(self) -> bool:
        cfg = self._cfg
        return (self.stops < cfg.max_stops
                and self.losses < cfg.max_consecutive_losses
                and self.day_net > -cfg.max_drawdown_usdt
                and self.entries < cfg.max_entries
                and self.volume + self._trade_volume <= cfg.max_volume_usdt)

    def record(self, trade: "Trade") -> None:
        self.roll(trade.entry_ms)
        self.entries += 1
        self.volume += self._trade_volume
        self.day_net += trade.net_usdt
        if trade.kind == "SL":
            self.stops += 1
        self.losses = self.losses + 1 if trade.net_usdt < 0 else 0


@dataclass(frozen=True)
class _Fill:
    """What an entry needs besides the signal: costs, latency sample and slippage."""

    exec_cfg: ExecConfig
    latencies: Tuple[int, ...]
    overshoot_bps: float
    round_trip_fee_bps: float


def _choose(signal: SignalFn, markets: Sequence[Market], t: int,
            blackout_s: int) -> Optional[Tuple[Market, int]]:
    """The first market (in tick order) outside a funding blackout that signals at ``t``."""
    for market in markets:
        if in_funding_blackout(t, market.funding_ms, blackout_s):
            continue
        side = signal(market.symbol, t)
        if side == 0:
            continue
        if side not in (1, -1):
            raise ValueError(f"signal for {market.symbol} returned {side!r}")
        return market, side
    return None


def _try_entry(market: Market, side: int, t: int, rng: random.Random,
               fill: _Fill) -> Optional[Trade]:
    """The trade a tick at ``t`` opens; ``None`` when the order bar is missing or the
    data ends before the bracket resolves (the run stops there)."""
    latency = rng.choice(fill.latencies) if fill.latencies else 0
    order_ms = t + latency + fill.exec_cfg.order_delay_s * 1000
    entry_idx = entry_index_for(market.bars_1m, order_ms)
    if entry_idx is None:
        return None
    outcome = market.outcomes.get(entry_idx, side)
    if outcome is None:
        return None
    open_ms = market.bars_1m.open_ms
    entry_ms = open_ms[entry_idx] + _MS_PER_MIN
    exit_ms = open_ms[outcome.exit_idx] + (0 if outcome.gapped else _MS_PER_MIN)
    gross = gross_bps(outcome, side, fill.overshoot_bps, fill.exec_cfg)
    net = fill.exec_cfg.notional_usdt * (gross - fill.round_trip_fee_bps) / _BPS
    return Trade(market.symbol, side, t, entry_ms, exit_ms, outcome.kind, gross, net,
                 wib_day(entry_ms), outcome.both_touched)


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
    blocked_starts, blocked_ends = _merge_windows(blocked_entry_windows)
    fee = exec_cfg.fee_pct if fee_pct is None else fee_pct
    fill = _Fill(exec_cfg, tuple(int(x) for x in latencies_ms), overshoot_bps, 2 * fee * 100.0)
    guards = _DayGuards(guard_cfg, 2 * exec_cfg.notional_usdt)
    idle_step = (exec_cfg.idle_tick_s + exec_cfg.tick_s) * 1000
    cooldown = exec_cfg.cooldown_min * _MS_PER_MIN

    trades: List[Trade] = []
    next_entry_ms = None  # cooldown end
    t = start_ms + offset_s * 1000
    while t < end_ms:
        guards.roll(t)
        chosen = None
        if (in_entry_window(t) and not _in_windows(t, blocked_starts, blocked_ends)
                and (next_entry_ms is None or t >= next_entry_ms) and guards.allows()):
            chosen = _choose(signal, markets, t, exec_cfg.funding_blackout_s)
        if chosen is None:
            t += idle_step
            continue
        trade = _try_entry(chosen[0], chosen[1], t, rng, fill)
        if trade is None:
            break
        trades.append(trade)
        guards.record(trade)
        next_entry_ms = trade.entry_ms + cooldown
        t = trade.exit_ms + exec_cfg.tick_s * 1000
    return trades


def _in_windows(t_ms: int, starts: List[int], ends: List[int]) -> bool:
    i = bisect_right(starts, t_ms) - 1
    return i >= 0 and t_ms < ends[i]
