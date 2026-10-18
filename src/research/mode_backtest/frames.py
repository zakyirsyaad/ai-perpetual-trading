"""The frames of a pair for the mode backtest: the hourly condition, trigger candles, stretch.

Research code; the bot never imports it. ``HourlyFrame`` joins the native 1h and 4h warm-ups to
the candles aggregated from the 1m bars, computes the selector's series once at build time and
answers ``context_at(t)`` by looking them up. ``trigger_series`` and ``stretch_series`` give the
candles the event loop walks, each with its numbers, and ``build_pair_frames`` gathers the three
for one pair in a ``PairFrames``.

* D1: candles are UTC-epoch buckets, as BingX and TradingView build them, so 4h candles close at
  03, 07, 11, 15, 19 and 23 WIB. A bucket missing a minute is dropped.
* D2: at ``t`` only candles with ``open + interval <= t`` are visible. A 15m candle closing at
  10:00 WIB sees the 1h candle 09:00-10:00 and never the one from 10:00 that is still forming.
  A value of a trigger series or of the stretch at bar ``i`` is computed through the close of
  bar ``i``, so it exists from ``open_ms[i] + interval_ms`` and not before.
* D3: a value that is not computed yet is ``None`` and makes the condition TIDAK_ENTRY.

The bars are validated finite when they are read and the series are plain averages and medians of
them, so every number in a context is finite or ``None``. That matters because ``pilih_mode``
guards ``None`` only: a NaN would not stop it.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import FrozenSet, List, Mapping, Optional, Tuple

from bot.core.entry_window import WeeklyWindow
from bot.strategy.modes.config import ModesConfig, SelectorConfig
from bot.strategy.modes.near_mean import stretch_from_window
from bot.strategy.modes.position_rules import entry_window_open
from bot.strategy.modes.regime import NAIK, pilih_mode
from bot.strategy.pine_indicators import atr, ema, rolling_median, sma
from research.pine_gate.bars import Bars, aggregate, interval_to_ms, last_closed_index, wib_day
from research.pine_gate.features import INTERVAL_1H, INTERVAL_1M, INTERVAL_3M, INTERVAL_4H, htf_bars

from .data import PairData

ATR_PERIOD_1H = 14  # the document's ATR14 (1h)
ATR_MEDIAN_HOURS = 720  # 30 days of closed hours, from the manager's document
TRIGGER_TIMEFRAMES = ("15m", "5m")  # Mode A's trigger candles; 5m is the pre-registered switch

Column = Tuple[Optional[float], ...]


@dataclass(frozen=True)
class HourlyContext:
    """The selector's inputs at one time, from closed candles only, and the condition they give.

    ``sma20_1h`` and ``sma50_1h`` are the fast and the slow 1h SMA of the selector config (20 and
    50 in the document, and named after them). ``atr_median`` is the median of the last 720
    closed-hour ATR14 values, the latest one included; it feeds the volatility guard, not the
    selector. A value that is not computed yet is ``None``. ``kondisi`` is what ``pilih_mode``
    answers for the five selector inputs: NAIK, TURUN, SIDEWAYS or TIDAK_ENTRY."""

    sma20_1h: Optional[float]
    sma50_1h: Optional[float]
    atr14_1h: Optional[float]
    atr_median: Optional[float]
    close_4h: Optional[float]
    ema50_4h: Optional[float]
    kondisi: str


def _at(column: Column, index: int) -> Optional[float]:
    """``column[index]``, or ``None`` when no candle has closed yet (index -1 is not the last)."""
    return column[index] if index >= 0 else None


class HourlyFrame:
    """The 1h and 4h series of the mode selector for one pair, read through ``context_at``.

    Build it with ``HourlyFrame.build``. The constructor takes the joined 1h and 4h candle
    series and refuses any other interval."""

    __slots__ = (
        "_bars_1h",
        "_bars_4h",
        "_sma_fast",
        "_sma_slow",
        "_atr",
        "_atr_median",
        "_close_4h",
        "_ema_4h",
        "_sideways_multiple",
    )

    def __init__(self, bars_1h: Bars, bars_4h: Bars, sel: SelectorConfig) -> None:
        if (bars_1h.interval_ms, bars_4h.interval_ms) != (INTERVAL_1H, INTERVAL_4H):
            raise ValueError(
                f"expected 1h and 4h candles, got {bars_1h.interval_ms} ms and "
                f"{bars_4h.interval_ms} ms"
            )
        highs, lows, closes = list(bars_1h.high), list(bars_1h.low), list(bars_1h.close)
        atr_1h = atr(highs, lows, closes, ATR_PERIOD_1H)
        closes_4h = list(bars_4h.close)
        self._bars_1h = bars_1h
        self._bars_4h = bars_4h
        self._sma_fast = tuple(sma(closes, sel.sma_cepat_1h))
        self._sma_slow = tuple(sma(closes, sel.sma_lambat_1h))
        self._atr = tuple(atr_1h)
        self._atr_median = tuple(rolling_median(atr_1h, ATR_MEDIAN_HOURS))
        self._close_4h = tuple(closes_4h)
        self._ema_4h = tuple(ema(closes_4h, sel.ema_4h))
        self._sideways_multiple = sel.sideways_jarak_kali_atr

    @classmethod
    def build(cls, pair: PairData, sel: SelectorConfig) -> "HourlyFrame":
        """Join each warm-up to the candles aggregated from the 1m bars, and compute the series.

        A hole between a warm-up and the first aggregated candle raises ``BarGapError`` (from
        ``htf_bars``), so it stops the build instead of shortening the series."""
        return cls(
            htf_bars(pair.bars_1m, pair.warmup_1h, INTERVAL_1H),
            htf_bars(pair.bars_1m, pair.warmup_4h, INTERVAL_4H),
            sel,
        )

    def context_at(self, t_ms: int) -> HourlyContext:
        """The selector's inputs and the condition at ``t_ms``: the last closed 1h and 4h candles.

        A candle is closed when ``open + interval <= t_ms``, so a candle closing exactly at
        ``t_ms`` is visible and the one opening then is not."""
        i = last_closed_index(self._bars_1h, t_ms)
        j = last_closed_index(self._bars_4h, t_ms)
        sma20_1h, sma50_1h, atr14_1h = (
            _at(self._sma_fast, i),
            _at(self._sma_slow, i),
            _at(self._atr, i),
        )
        close_4h, ema50_4h = _at(self._close_4h, j), _at(self._ema_4h, j)
        return HourlyContext(
            sma20_1h,
            sma50_1h,
            atr14_1h,
            _at(self._atr_median, i),
            close_4h,
            ema50_4h,
            pilih_mode(sma20_1h, sma50_1h, atr14_1h, close_4h, ema50_4h, self._sideways_multiple),
        )

    def naik_days(
        self,
        start_ms: int,
        end_ms: int,
        window: Optional[WeeklyWindow],
        min_naik_hours: int,
    ) -> FrozenSet[date]:
        """The WIB days with at least ``min_naik_hours`` hours in which Mode A could enter.

        An hour counts when its closed 1h candle's close lies in ``[start_ms, end_ms)`` (the
        start is included, the end is not), ``window`` is open at that close (read as a WIB time;
        ``None`` means always open) and ``context_at(close).kondisi`` is NAIK. The hours are
        counted for the WIB day of the close, so the candle ending at 00:00 WIB counts for the new
        day, and a day is kept when it has at least ``min_naik_hours`` of them. Warm-up candles
        count when the range reaches them.

        ``min_naik_hours`` is the config's ``uji.hari_naik_minimal_jam_naik`` (1 in
        ``config/modes.yaml``: one NAIK hour makes a NAIK day). It has no default, so no caller
        can leave the config value out, and a value below 1 raises ``ValueError``. An empty range
        gives no days and a reversed one raises ``ValueError``."""
        if end_ms < start_ms:
            raise ValueError(f"end_ms {end_ms} is before start_ms {start_ms}")
        if min_naik_hours < 1:
            raise ValueError(f"min_naik_hours must be at least 1, got {min_naik_hours}")
        opens = self._bars_1h.open_ms
        first = bisect_left(opens, start_ms - INTERVAL_1H)
        last = bisect_left(opens, end_ms - INTERVAL_1H)
        naik_hours = Counter()
        for i in range(first, last):
            close_ms = opens[i] + INTERVAL_1H
            if not entry_window_open(window, close_ms):
                continue
            if self.context_at(close_ms).kondisi == NAIK:
                naik_hours[wib_day(close_ms)] += 1
        return frozenset(day for day, hours in naik_hours.items() if hours >= min_naik_hours)


def _require_1m(bars: Bars) -> None:
    """The series below are built from 1m bars; 3m bars would aggregate to 15m without a word."""
    if bars.interval_ms != INTERVAL_1M:
        raise ValueError(f"expected 1m bars, got {bars.interval_ms} ms")


@dataclass(frozen=True)
class TriggerSeries:
    """The closed trigger candles of one timeframe and the EMA through each candle's close.

    ``ema[i]`` belongs to candle ``i`` of ``bars`` and is the EMA of the closes ``0..i``: a candle
    is compared with the EMA that includes its own close (D4) and nothing later. It is known when
    the candle closes, ``bars.open_ms[i] + bars.interval_ms``, and is ``None`` until 8 x period
    candles exist (D3)."""

    bars: Bars
    ema: Column

    def first_known_ms(self) -> Optional[int]:
        """The close of the first candle whose EMA is known, or ``None`` when none has one: from
        then on Mode A can signal on these candles."""
        for i, value in enumerate(self.ema):
            if value is not None:
                return self.bars.open_ms[i] + self.bars.interval_ms
        return None


def trigger_series(bars_1m: Bars, tf: str, ema_period: int) -> TriggerSeries:
    """The ``tf`` candles (``"15m"`` or ``"5m"``) of the 1m bars and their EMA of ``ema_period``.

    The candles are UTC-epoch buckets and a bucket that misses a minute is dropped (``aggregate``).
    The EMA is the mean-seeded one of ``pine_indicators``. Any other ``tf``, and bars that are not
    1m, raise ``ValueError``."""
    if tf not in TRIGGER_TIMEFRAMES:
        raise ValueError(
            f"trigger timeframe must be one of {', '.join(TRIGGER_TIMEFRAMES)}, got {tf!r}"
        )
    _require_1m(bars_1m)
    bars = aggregate(bars_1m, interval_to_ms(tf))
    return TriggerSeries(bars, tuple(ema(list(bars.close), ema_period)))


def _require_measurable(window: int, ema_period: int, atr_period: int) -> None:
    """The live EMA needs ``ema_period`` closes and the live ATR ``atr_period + 1`` of them.

    With fewer, the EMA is the last close (a stretch of 0.0, near the mean on every candle) or the
    ATR is 0 (no stretch at all), and the series would look like data."""
    if ema_period < 1 or atr_period < 1:
        raise ValueError(f"periods must be at least 1, got EMA {ema_period} and ATR {atr_period}")
    needed = max(ema_period, atr_period + 1)
    if window < needed:
        raise ValueError(
            f"a window of {window} candles is too short for an EMA of {ema_period} and an ATR of "
            f"{atr_period}: it needs {needed}"
        )


def stretch_series(
    bars_1m: Bars, window: int, ema_period: int, atr_period: int
) -> Tuple[Bars, Column]:
    """The 3m candles of the 1m bars and the stretch of each, as the live gate measures it.

    The stretch of candle ``i`` is ``near_mean.stretch_from_window`` over the ``window`` candles
    ending with candle ``i``, so it uses closes up to and including ``i`` and is known when the
    candle closes. It is ``None`` for the first ``window - 1`` candles and wherever the window has
    no ATR (a flat market). The loop is the one that builds ``stretch_3m`` in
    ``research.pine_gate.features``, and a test holds the two equal on every candle. A window the
    live maths cannot measure (see ``_require_measurable``) raises ``ValueError``."""
    _require_1m(bars_1m)
    _require_measurable(window, ema_period, atr_period)
    bars = aggregate(bars_1m, INTERVAL_3M)
    highs, lows, closes = list(bars.high), list(bars.low), list(bars.close)
    out: List[Optional[float]] = []
    for end in range(1, len(closes) + 1):
        if end < window:
            out.append(None)
            continue
        start = end - window
        out.append(
            stretch_from_window(
                highs[start:end], lows[start:end], closes[start:end], ema_period, atr_period
            )
        )
    return bars, tuple(out)


@dataclass(frozen=True)
class PairFrames:
    """Everything the event loop reads for one pair.

    ``hourly`` answers the market condition at any time. ``triggers`` maps ``"15m"`` and ``"5m"``
    to Mode A's candles with their EMA. ``stretch_bars`` are Mode D's 3m candles and ``stretch``
    holds the stretch of each, index for index. ``triggers`` is read-only. ``cfg`` is the config
    the frames were built under: its periods are in the series, so ``simulate_run`` refuses to
    run them under another one."""

    symbol: str
    bars_1m: Bars
    hourly: HourlyFrame
    triggers: Mapping[str, TriggerSeries]
    stretch_bars: Bars
    stretch: Column
    cfg: ModesConfig


def build_pair_frames(pair: PairData, cfg: ModesConfig) -> PairFrames:
    """The frames of one pair under ``cfg``.

    The hourly frame takes the selector's periods (``pemilih_mode``). Both trigger timeframes are
    built with Mode A's EMA period (``mode_a.ema_pemicu``), so a run can pick 15m or the 5m
    switch. The stretch takes Mode D's window and periods (``candle_jendela``, ``ema_periode``,
    ``atr_periode``)."""
    stretch_bars, stretch = stretch_series(
        pair.bars_1m, cfg.mode_d.candle_jendela, cfg.mode_d.ema_periode, cfg.mode_d.atr_periode
    )
    triggers = {
        tf: trigger_series(pair.bars_1m, tf, cfg.mode_a.ema_pemicu) for tf in TRIGGER_TIMEFRAMES
    }
    return PairFrames(
        pair.symbol,
        pair.bars_1m,
        HourlyFrame.build(pair, cfg.pemilih_mode),
        MappingProxyType(triggers),
        stretch_bars,
        stretch,
        cfg,
    )
