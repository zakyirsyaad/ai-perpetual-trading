"""The 1m exit walk, the money of a simulated trade and the event loop of the mode backtest.

Research code; the bot never imports it. ``resolve_exit`` walks 1m bars from the bar a position
entered on and says how it ended; ``settle`` turns the entry open and the raw exit price into the
fills, the fee and the profit of the trade. Both take plain numbers and bars and read no clock,
file or setting, so the same call always gives the same answer. ``simulate_run`` drives Mode A or
Mode D over the closed candles of one segment and turns each signal that passes every gate into a
``Trade`` with those two (see "The event loop" below).

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

The event loop (spec section 9.1, decisions D2, D4, D5 and D10):

* The clock is the close ``t`` of each trigger candle inside the run, ``start_ms <= t < end_ms``:
  the 15m candles of Mode A (or the pre-registered 5m ones) and the 3m candles of Mode D. A candle
  closing before the start, or at or after the end, never reaches the rule, and every candle in
  between must be there, because Mode A's window counts candles: a missing one raises
  ``RuntimeError``.
* The condition is read only through ``frames.hourly.context_at(t)``: the last closed 1h and 4h
  candles at ``t`` (D2). Mode A sends every candle to a ``PullbackTracker`` with the candle's own
  EMA and the SMA20 and ATR14 (1h) of that context, also while a position is open and also before
  the EMA exists (D4). Mode D signals when the candle's stretch is near the mean
  (``near_mean``). Mode A is LONG and Mode D SHORT.
* A signal meets the gates in this order, and the first that fails is counted under its key in
  ``RunResult.blocked``: ``window`` (the weekly window is closed at ``t``), ``segment_edge`` (D10:
  ``t`` plus the time limit is at or after the end, where the time-exit bar would open in the next
  segment), ``kondisi`` (the condition at ``t`` is not the run's ``kondisi_gate``),
  ``position_open`` (``t`` is before the exit of the last position), ``pause`` (less than
  ``jeda_menit`` minutes after that exit), ``daily_cap`` (the entries already opened on the WIB day
  of ``t``; a run trades one pair, so its count is the pair's and the total) and ``volatility``
  (the 1h ATR14 above the guard, or no median yet).
* A signal that passes opens at the open of the 1m bar that opens at ``t``: the fill, the bracket
  from the fill (D6) with the mode's percents times the pair's ``faktor_k``, the walk, the money.
  Only an entry uses up a Mode A touch (D5): a blocked signal leaves it for a later confirmation
  inside its window.
* The exit time is the open of the exit bar for a time exit and a gap, and the end of that minute
  for a touch, which is only known to the minute. The position is open until then, and the pause
  runs from then.
* A signal that passes every gate always becomes a trade. No 1m bar opening at ``t``, or a walk
  that cannot end (a missing minute, or the data ending first), raises ``RuntimeError`` naming the
  signal time instead of dropping the trade.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Iterator, List, Mapping, Optional, Protocol, Tuple, Union

from bot.core.entry_window import parse_weekly_window
from bot.strategy.modes.checks import (
    check_side,
    is_whole,
    require_not_negative,
    require_positive,
)
from bot.strategy.modes.config import GeneralConfig, ModesConfig, PullbackConfig
from bot.strategy.modes.near_mean import near_mean
from bot.strategy.modes.position_rules import (
    LONG,
    SHORT,
    adverse_fill,
    bracket_prices,
    entry_window_open,
    pause_over,
    under_daily_caps,
    volatility_ok,
)
from bot.strategy.modes.pullback import Candle, PullbackTracker
from bot.strategy.modes.regime import NAIK, TURUN
from research.pine_gate.bars import Bars, entry_index_for, interval_to_ms, wib_day
from research.pine_gate.features import INTERVAL_1M
from research.pine_gate.spec import WIB

from .frames import TRIGGER_TIMEFRAMES, FrameParams, HourlyContext, PairFrames, TriggerSeries

__all__ = [
    "BLOCK_REASONS",
    "Book",
    "EXIT_POTONG",
    "EXIT_SL",
    "EXIT_TP",
    "EXIT_WAKTU",
    "MODE_A",
    "MODE_D",
    "ExitOutcome",
    "Gates",
    "Money",
    "NearMeanTrigger",
    "RunResult",
    "RunSpec",
    "Trade",
    "candle_closes",
    "check_column",
    "check_inputs",
    "check_walk",
    "moment",
    "outcome_exit_ms",
    "reaches_sl",
    "reaches_tp",
    "resolve_exit",
    "settle",
    "simulate_run",
]

EXIT_TP = "TP"
EXIT_SL = "SL"
EXIT_WAKTU = "WAKTU"
EXIT_POTONG = "POTONG"  # Mode L version (ii): the loss cut, see ``live_sim``

MODE_A = "A"  # the pullback LONG
MODE_D = "D"  # the near-mean SHORT

# The gates of a signal in the order of spec section 9.1, as the keys of ``RunResult.blocked``.
BLOCK_REASONS = (
    "window",
    "segment_edge",
    "kondisi",
    "position_open",
    "pause",
    "daily_cap",
    "volatility",
)
_WINDOW, _SEGMENT_EDGE, _KONDISI, _POSITION_OPEN, _PAUSE, _DAILY_CAP, _VOLATILITY = BLOCK_REASONS

# The runs Phase 1 has: Mode A in NAIK; Mode D in TURUN, and in NAIK as the comparator.
_TIMEFRAMES = {MODE_A: TRIGGER_TIMEFRAMES, MODE_D: ("3m",)}
_GATES = {MODE_A: (NAIK,), MODE_D: (TURUN, NAIK)}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ExitOutcome:
    """How a position ended.

    ``reason`` is ``EXIT_TP``, ``EXIT_SL`` or ``EXIT_WAKTU`` (``EXIT_POTONG`` only from mode L's
    ``live_sim`` walk). ``exit_idx`` is the index, in the
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


def reaches_sl(side: int, price: float, sl: float) -> bool:
    """Whether ``price`` is at or beyond the stop-loss.

    A LONG's stop is below its entry, so beyond it is at or below; a SHORT's is the mirror."""
    return price <= sl if side == LONG else price >= sl


def reaches_tp(side: int, price: float, tp: float) -> bool:
    """Whether ``price`` is at or beyond the take-profit.

    A LONG's target is above its entry, so beyond it is at or above; a SHORT's is the mirror."""
    return price >= tp if side == LONG else price <= tp


def check_walk(
    bars_1m: Bars, entry_idx: int, side: int, tp: float, sl: float, limit_min: int
) -> None:
    if bars_1m.interval_ms != INTERVAL_1M:
        raise ValueError(f"expected 1m bars, got {bars_1m.interval_ms} ms")
    check_side(side)
    if not is_whole(entry_idx) or not 0 <= entry_idx < len(bars_1m):
        raise ValueError(
            f"entry_idx must be a bar index from 0 to {len(bars_1m) - 1}, got {entry_idx!r}"
        )
    if not is_whole(limit_min) or limit_min < 1:
        raise ValueError(
            f"limit_min must be a whole number of minutes of at least 1, got {limit_min!r}"
        )
    require_positive("tp", tp)
    require_positive("sl", sl)
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
    check_walk(bars_1m, entry_idx, side, tp, sl, limit_min)
    open_ms, opens, highs, lows = bars_1m.open_ms, bars_1m.open, bars_1m.high, bars_1m.low
    limit_idx = entry_idx + limit_min
    for i in range(entry_idx, min(limit_idx, len(bars_1m) - 1) + 1):
        if i > entry_idx and open_ms[i] != open_ms[i - 1] + INTERVAL_1M:
            return None
        bar_open = opens[i]
        if i == limit_idx:
            return ExitOutcome(EXIT_WAKTU, i, bar_open, False, False)
        if reaches_sl(side, bar_open, sl):
            return ExitOutcome(EXIT_SL, i, bar_open, True, False)
        if reaches_tp(side, bar_open, tp):
            return ExitOutcome(EXIT_TP, i, bar_open, True, False)
        worst, best = (lows[i], highs[i]) if side == LONG else (highs[i], lows[i])
        if reaches_sl(side, worst, sl):
            return ExitOutcome(EXIT_SL, i, float(sl), False, reaches_tp(side, best, tp))
        if reaches_tp(side, best, tp):
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
    check_side(side)
    require_positive("notional", notional)
    require_not_negative("fee_pct", fee_pct)
    entry_fill = adverse_fill(entry_open, side, slippage_pct, False)
    exit_fill = adverse_fill(raw_exit, side, slippage_pct, True)
    qty = notional / entry_fill
    volume = qty * entry_fill + qty * exit_fill
    fee = fee_pct / 100 * volume
    pnl = side * qty * (exit_fill - entry_fill) - fee
    slippage = qty * abs(entry_fill - entry_open) + qty * abs(raw_exit - exit_fill)
    return Money(entry_fill, exit_fill, qty, fee, slippage, pnl, volume)


@dataclass(frozen=True)
class RunSpec:
    """One run of the loop: ``mode`` on ``pair`` over the segment ``[start_ms, end_ms)``.

    ``mode`` is ``MODE_A``, the pullback LONG on ``"15m"`` candles or the pre-registered ``"5m"``
    switch, or ``MODE_D``, the near-mean SHORT on ``"3m"`` candles. ``kondisi_gate`` is the
    condition a signal needs at its close: NAIK for Mode A; TURUN for Mode D, or NAIK for the
    comparator "current SHORT in NAIK". The times are UTC milliseconds and the end is not part of
    the segment. Any other run raises ``ValueError``, as do times that are not whole numbers and an
    end before the start; a start equal to the end is an empty run."""

    mode: str
    pair: str
    tf: str
    kondisi_gate: str
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        if self.mode not in (MODE_A, MODE_D):
            raise ValueError(f"mode must be {MODE_A!r} or {MODE_D!r}, got {self.mode!r}")
        if self.tf not in _TIMEFRAMES[self.mode]:
            raise ValueError(
                f"Mode {self.mode} runs on {' or '.join(_TIMEFRAMES[self.mode])} candles, "
                f"got {self.tf!r}"
            )
        if self.kondisi_gate not in _GATES[self.mode]:
            raise ValueError(
                f"Mode {self.mode} runs in {' or '.join(_GATES[self.mode])}, "
                f"got {self.kondisi_gate!r}"
            )
        if not (is_whole(self.start_ms) and is_whole(self.end_ms)):
            raise ValueError(
                f"start_ms and end_ms must be whole milliseconds, got {self.start_ms!r} and "
                f"{self.end_ms!r}"
            )
        if self.end_ms < self.start_ms:
            raise ValueError(f"end_ms {self.end_ms} is before start_ms {self.start_ms}")


@dataclass(frozen=True)
class Trade:
    """One simulated position, with every field of the trade log (spec section 2.9).

    ``side`` is ``LONG`` or ``SHORT``; ``kondisi`` is the condition at the signal and ``context``
    the whole hourly context it came from (the log's SMA20, SMA50, EMA50 4h and ATR14 columns).
    ``signal_ms`` is the close of the signal candle and ``entry_ms`` the open of the 1m bar the
    position fills on: the same instant. ``tp`` and ``sl`` are the levels set from ``entry_fill``,
    ``exit_ms`` is when the position is closed (see the module docstring) and ``exit_reason`` is
    ``EXIT_TP``, ``EXIT_SL`` or ``EXIT_WAKTU``. ``gapped`` and ``both_touched`` are those of the
    walk's ``ExitOutcome``; the two fills, ``qty``, the fee, the slippage, ``pnl_usdt`` and
    ``volume_usdt`` are those of ``settle``'s ``Money``, and ``notional_usdt`` is the configured
    size the position opened with. The last three belong to mode L and comparator S and stay
    unset for Mode A and Mode D: ``versi`` is the exit version ("i" or "ii"), ``sl_pct`` the stop
    distance in percent of the entry fill and ``potong`` the cut price (version ii only)."""

    mode: str
    pair: str
    side: int
    kondisi: str
    context: HourlyContext
    faktor_k: float
    signal_ms: int
    entry_ms: int
    entry_fill: float
    tp: float
    sl: float
    exit_ms: int
    exit_fill: float
    exit_reason: str
    gapped: bool
    both_touched: bool
    qty: float
    notional_usdt: float
    fee_usdt: float
    slippage_usdt: float
    pnl_usdt: float
    volume_usdt: float
    versi: str = ""
    sl_pct: Optional[float] = None
    potong: Optional[float] = None


@dataclass(frozen=True)
class RunResult:
    """The trades of a run, in time order, and how many signals each gate blocked.

    ``blocked`` is read-only, holds every key of ``BLOCK_REASONS`` in gate order (0 included) and
    counts a blocked signal once, under the first gate it fails."""

    trades: Tuple[Trade, ...]
    blocked: Mapping[str, int]


class _Conditions(Protocol):
    """What the loop reads of the hourly frame; a test may pass any object that has it."""

    def context_at(self, t_ms: int) -> HourlyContext: ...


@dataclass(frozen=True)
class _Terms:
    """What a run's positions trade on: the side, the bracket percents (already times
    ``faktor_k``) and the time limit in minutes."""

    side: int
    tp_pct: float
    sl_pct: float
    limit_min: int
    faktor_k: float


def moment(t_ms: int) -> str:
    """``t_ms`` as a WIB time and in milliseconds, for a message."""
    wib = (_EPOCH + timedelta(milliseconds=t_ms)).astimezone(WIB)
    return f"{wib.isoformat()} ({t_ms} ms)"


def check_column(bars: Bars, column: Tuple, interval_ms: int, what: str, name: str) -> None:
    """The candles of a signal source must be of the run's interval, one value of ``name`` each."""
    if bars.interval_ms != interval_ms:
        raise ValueError(f"the {what} are {bars.interval_ms} ms candles, expected {interval_ms} ms")
    if len(column) != len(bars):
        raise ValueError(f"the {what} have {len(bars)} candles but {len(column)} {name} values")


def check_inputs(frames: PairFrames, cfg: ModesConfig, pair: str) -> None:
    """The frames and the config must be those of the run on ``pair``, and the loop holds one
    position."""
    if pair != frames.symbol:
        raise ValueError(f"the run is for {pair!r} but the frames are of {frames.symbol!r}")
    if pair not in cfg.pair:
        raise ValueError(f"{pair!r} is not a pair of the config ({', '.join(cfg.pair)})")
    if cfg.umum.maks_posisi_per_pair != 1:
        raise ValueError(
            "the loop holds one position per pair at a time, but umum.maks_posisi_per_pair is "
            f"{cfg.umum.maks_posisi_per_pair}"
        )
    if frames.bars_1m.interval_ms != INTERVAL_1M:
        raise ValueError(f"expected 1m bars, got {frames.bars_1m.interval_ms} ms")
    if frames.params != FrameParams.of(cfg):
        raise ValueError(
            "the frames were built under another config: its periods (FrameParams) are in their "
            "series and the rest would come from this one, so build them again with "
            "build_pair_frames"
        )


def _terms(cfg: ModesConfig, run: RunSpec) -> _Terms:
    """The mode's side, percents and limit; the percents scale with the pair's ``faktor_k``."""
    faktor_k = cfg.pair[run.pair].faktor_k
    if faktor_k is None:
        raise ValueError(
            f"pair.{run.pair}.faktor_k is 'otomatis', which Phase 1 cannot compute: "
            "the pair cannot run"
        )
    side, mode = (LONG, cfg.mode_a) if run.mode == MODE_A else (SHORT, cfg.mode_d)
    return _Terms(
        side, mode.tp_persen * faktor_k, mode.sl_persen * faktor_k, mode.batas_waktu_menit, faktor_k
    )


class _PullbackTrigger:
    """Mode A's signals. Every candle goes to the tracker with its own EMA and the SMA20 and
    ATR14 (1h) of the context at its close (D4)."""

    def __init__(self, series: TriggerSeries, hourly: _Conditions, rules: PullbackConfig) -> None:
        self.bars = series.bars
        self._ema = series.ema
        self._hourly = hourly
        self._tracker = PullbackTracker(
            rules.konfirmasi_dalam_candle,
            rules.maks_jarak_sma20_1h_kali_atr,
            series.bars.interval_ms,
        )

    def signal_at(self, i: int, t_ms: int) -> Optional[HourlyContext]:
        """The context at ``t_ms`` when candle ``i``, closing then, signals; else None."""
        bars = self.bars
        context = self._hourly.context_at(t_ms)
        candle = Candle(t_ms, bars.open[i], bars.high[i], bars.low[i], bars.close[i])
        signal = self._tracker.on_candle(candle, self._ema[i], context.sma20_1h, context.atr14_1h)
        return None if signal is None else context

    def entered(self) -> None:
        """The signal of the latest candle opened a position: it uses the touches up (D5)."""
        self._tracker.consume()


class NearMeanTrigger:
    """The signals of Mode D and of mode L: a candle whose stretch is within the band, whatever
    the side. It keeps no state."""

    def __init__(self, bars: Bars, stretch: Tuple, hourly: _Conditions, max_abs: float) -> None:
        self.bars = bars
        self._stretch = stretch
        self._hourly = hourly
        self._max_abs = max_abs

    def signal_at(self, i: int, t_ms: int) -> Optional[HourlyContext]:
        """The context at ``t_ms`` when candle ``i``, closing then, signals; else None."""
        if not near_mean(self._stretch[i], self._max_abs):
            return None
        return self._hourly.context_at(t_ms)

    def entered(self) -> None:
        """Nothing to use up."""


def _trigger(
    frames: PairFrames, cfg: ModesConfig, run: RunSpec
) -> Union[_PullbackTrigger, NearMeanTrigger]:
    """The signal source of the run's mode, after checking its candles and their column."""
    interval_ms = interval_to_ms(run.tf)
    if run.mode == MODE_A:
        series = frames.triggers.get(run.tf)
        if series is None:
            raise ValueError(f"the frames hold no {run.tf} trigger candles")
        check_column(series.bars, series.ema, interval_ms, f"{run.tf} trigger candles", "EMA")
        return _PullbackTrigger(series, frames.hourly, cfg.mode_a)
    check_column(frames.stretch_bars, frames.stretch, interval_ms, "3m candles", "stretch")
    return NearMeanTrigger(
        frames.stretch_bars, frames.stretch, frames.hourly, cfg.mode_d.maks_jarak_ke_ema_kali_atr
    )


def candle_closes(bars: Bars, start_ms: int, end_ms: int) -> Iterator[Tuple[int, int]]:
    """``(index, close)`` of every candle closing in ``[start_ms, end_ms)``, in time order.

    A candle missing between two of them raises ``RuntimeError``: the Mode A window counts the
    candles it is sent, so a hole would widen it without a word."""
    step = bars.interval_ms
    opens = bars.open_ms
    first = bisect_left(opens, start_ms - step)
    last = bisect_left(opens, end_ms - step)
    for i in range(first, last):
        if i > first and opens[i] != opens[i - 1] + step:
            raise RuntimeError(
                f"no trigger candle closes at {moment(opens[i - 1] + 2 * step)}: the candles of "
                "the run have a hole"
            )
        yield i, opens[i] + step


class Book:
    """What the run has done so far: its trades, the last exit, the entries per WIB day and the
    blocked signals per reason."""

    def __init__(self) -> None:
        self._trades: List[Trade] = []
        self._entries_per_day: Counter = Counter()
        self._blocked: Counter = Counter()
        self.last_exit_ms: Optional[int] = None

    def open_at(self, t_ms: int) -> bool:
        """Whether the last position is still open at ``t_ms``; it is closed at its exit time."""
        return self.last_exit_ms is not None and t_ms < self.last_exit_ms

    def entries_on(self, day: date) -> int:
        return self._entries_per_day[day]

    def block(self, reason: str) -> None:
        self._blocked[reason] += 1

    def enter(self, trade: Trade) -> None:
        self._trades.append(trade)
        self._entries_per_day[wib_day(trade.signal_ms)] += 1
        self.last_exit_ms = trade.exit_ms

    def result(self) -> RunResult:
        blocked = {reason: self._blocked[reason] for reason in BLOCK_REASONS}
        return RunResult(tuple(self._trades), MappingProxyType(blocked))


class Gates:
    """The gates of a signal (spec section 9.1) with the numbers of the config and the run: the
    condition the run needs, the end of its segment and the time limit of its positions."""

    def __init__(self, umum: GeneralConfig, kondisi_gate: str, end_ms: int, limit_min: int) -> None:
        self._window = parse_weekly_window(umum.jendela_entry_mingguan)
        self._limit_ms = limit_min * INTERVAL_1M
        self._end_ms = end_ms
        self._kondisi = kondisi_gate
        self._pause_min = umum.jeda_menit
        self._max_pair = umum.maks_entry_per_hari_per_pair
        self._max_total = umum.maks_entry_per_hari_total
        self._volatility_mult = umum.penjaga_volatilitas_kali_median_atr

    def first_failure(self, t_ms: int, context: HourlyContext, book: Book) -> Optional[str]:
        """The key of the first gate the signal at ``t_ms`` fails, or None when it passes all."""
        if not entry_window_open(self._window, t_ms):
            return _WINDOW
        if t_ms + self._limit_ms >= self._end_ms:
            return _SEGMENT_EDGE
        if context.kondisi != self._kondisi:
            return _KONDISI
        if book.open_at(t_ms):
            return _POSITION_OPEN
        if not pause_over(t_ms, book.last_exit_ms, self._pause_min):
            return _PAUSE
        today = book.entries_on(wib_day(t_ms))
        if not under_daily_caps(today, today, self._max_pair, self._max_total):
            return _DAILY_CAP
        if not volatility_ok(context.atr14_1h, context.atr_median, self._volatility_mult):
            return _VOLATILITY
        return None


def outcome_exit_ms(bars_1m: Bars, outcome: ExitOutcome) -> int:
    """The open of the exit bar for a time exit and a gap; the end of its minute for a touch."""
    at_open = outcome.reason == EXIT_WAKTU or outcome.gapped
    return bars_1m.open_ms[outcome.exit_idx] + (0 if at_open else INTERVAL_1M)


def _open_position(
    bars_1m: Bars,
    umum: GeneralConfig,
    run: RunSpec,
    terms: _Terms,
    t_ms: int,
    context: HourlyContext,
) -> Trade:
    """The trade of a signal at ``t_ms`` that passed every gate; it never comes back empty."""
    idx = entry_index_for(bars_1m, t_ms)
    if idx is None or bars_1m.open_ms[idx] != t_ms:
        raise RuntimeError(f"no 1m bar opens at the signal at {moment(t_ms)}")
    entry_open = bars_1m.open[idx]
    side, slippage = terms.side, umum.slippage_persen
    bracket = bracket_prices(
        adverse_fill(entry_open, side, slippage, False), side, terms.tp_pct, terms.sl_pct
    )
    outcome = resolve_exit(bars_1m, idx, side, bracket.tp, bracket.sl, terms.limit_min)
    if outcome is None:
        raise RuntimeError(
            f"the position of the signal at {moment(t_ms)} does not end inside the 1m bars: a "
            "minute is missing or the data ends before its exit"
        )
    money = settle(
        entry_open, outcome.raw_price, side, umum.notional_usdt, umum.fee_taker_persen, slippage
    )
    return Trade(
        mode=run.mode,
        pair=run.pair,
        side=side,
        kondisi=context.kondisi,
        context=context,
        faktor_k=terms.faktor_k,
        signal_ms=t_ms,
        entry_ms=t_ms,
        entry_fill=money.entry_fill,
        tp=bracket.tp,
        sl=bracket.sl,
        exit_ms=outcome_exit_ms(bars_1m, outcome),
        exit_fill=money.exit_fill,
        exit_reason=outcome.reason,
        gapped=outcome.gapped,
        both_touched=outcome.both_touched,
        qty=money.qty,
        notional_usdt=umum.notional_usdt,
        fee_usdt=money.fee_usdt,
        slippage_usdt=money.slippage_usdt,
        pnl_usdt=money.pnl_usdt,
        volume_usdt=money.volume_usdt,
    )


def simulate_run(frames: PairFrames, cfg: ModesConfig, run: RunSpec) -> RunResult:
    """Simulate ``run`` on the frames of its pair under ``cfg`` (see "The event loop" above).

    At each candle close inside the run the signal source is updated first; a signal then meets
    the gates in order, and one that passes them all opens a position whose whole life is read
    from the 1m bars before the next candle. The condition is read only through
    ``frames.hourly.context_at``.

    Raises ``ValueError`` when the frames or the config do not fit the run: frames of another pair,
    a pair the config does not have, an ``otomatis`` ``faktor_k``, more than one position per pair,
    bars of the wrong interval, frames built with other periods (``frames.params``), or a column
    (EMA, stretch) that does not match its candles. Raises
    ``RuntimeError`` for a hole in the data the run needs: a missing trigger candle, no 1m bar
    opening at a signal that passed every gate, or a walk that cannot end."""
    check_inputs(frames, cfg, run.pair)
    terms = _terms(cfg, run)
    trigger = _trigger(frames, cfg, run)
    gates = Gates(cfg.umum, run.kondisi_gate, run.end_ms, terms.limit_min)
    book = Book()
    for i, t_ms in candle_closes(trigger.bars, run.start_ms, run.end_ms):
        context = trigger.signal_at(i, t_ms)
        if context is None:
            continue
        reason = gates.first_failure(t_ms, context, book)
        if reason is not None:
            book.block(reason)
            continue
        book.enter(_open_position(frames.bars_1m, cfg.umum, run, terms, t_ms, context))
        trigger.entered()
    return book.result()
