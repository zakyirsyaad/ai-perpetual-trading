"""The hourly frame of the mode selector: the market condition at any time, and NAIK days.

Research code; the bot never imports it. ``HourlyFrame`` joins the native 1h and 4h warm-ups to
the candles aggregated from the 1m bars, computes the selector's series once at build time and
answers ``context_at(t)`` by looking them up.

* D1: candles are UTC-epoch buckets, as BingX and TradingView build them, so 4h candles close at
  03, 07, 11, 15, 19 and 23 WIB.
* D2: at ``t`` only candles with ``open + interval <= t`` are visible. A 15m candle closing at
  10:00 WIB sees the 1h candle 09:00-10:00 and never the one from 10:00 that is still forming.
* D3: a value that is not computed yet is ``None`` and makes the condition TIDAK_ENTRY.

The bars are validated finite when they are read and the series are plain averages and medians of
them, so every number in a context is finite or ``None``. That matters because ``pilih_mode``
guards ``None`` only: a NaN would not stop it.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import FrozenSet, Optional, Tuple

from bot.core.entry_window import WeeklyWindow, window_is_open
from bot.strategy.modes.config import SelectorConfig
from bot.strategy.modes.regime import NAIK, pilih_mode
from bot.strategy.pine_indicators import atr, ema, rolling_median, sma
from research.pine_gate.bars import Bars, last_closed_index, wib_day
from research.pine_gate.features import INTERVAL_1H, INTERVAL_4H, htf_bars
from research.pine_gate.spec import WIB

from .data import PairData

ATR_PERIOD_1H = 14  # the document's ATR14 (1h)
ATR_MEDIAN_HOURS = 720  # 30 days of closed hours, from the manager's document

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

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


def _wib_moment(t_ms: int) -> datetime:
    """The WIB wall-clock datetime of a UTC millisecond timestamp, as the weekly window reads it.

    Exact integer arithmetic. WIB is the fixed +07:00 of ``research.pine_gate.spec``:
    Asia/Jakarta has no daylight saving time, so it is the same zone."""
    return (_EPOCH + timedelta(milliseconds=t_ms)).astimezone(WIB)


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
        self, start_ms: int, end_ms: int, window: Optional[WeeklyWindow]
    ) -> FrozenSet[date]:
        """The WIB days with an hour in which Mode A could enter: NAIK, inside the entry window.

        A closed 1h candle counts when its close lies in ``[start_ms, end_ms)`` (the start is
        included, the end is not), ``window`` is open at that close (read as a WIB time; ``None``
        means always open) and ``context_at(close).kondisi`` is NAIK. The day of a candle is the
        WIB day of its close, so the candle ending at 00:00 WIB belongs to the new day. Warm-up
        candles count when the range reaches them. An empty range gives no days and a reversed
        one raises ``ValueError``."""
        if end_ms < start_ms:
            raise ValueError(f"end_ms {end_ms} is before start_ms {start_ms}")
        opens = self._bars_1h.open_ms
        first = bisect_left(opens, start_ms - INTERVAL_1H)
        last = bisect_left(opens, end_ms - INTERVAL_1H)
        days = set()
        for i in range(first, last):
            close_ms = opens[i] + INTERVAL_1H
            if not window_is_open(window, _wib_moment(close_ms)):
                continue
            if self.context_at(close_ms).kondisi == NAIK:
                days.add(wib_day(close_ms))
        return frozenset(days)
