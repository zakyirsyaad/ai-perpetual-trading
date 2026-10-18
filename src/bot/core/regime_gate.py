"""Regime and volatility gate of the LONG forward bot.

The same hourly selector as the offline backtest (``research.mode_backtest.frames.HourlyFrame``),
read from the venue's closed candles: 1h SMA20 and SMA50, 1h ATR14 (Wilder), the 4h EMA50, and
``pilih_mode`` of ``bot.strategy.modes.regime``. The maths is reused, not copied. The numbers the
backtest keeps in ``frames.py`` and ``config/modes.yaml`` are repeated below because live code may
not import ``research``; ``tests/test_regime_gate.py`` pins each of them to its source.

This is the one live module allowed to import ``bot.strategy.pine_indicators`` and
``bot.strategy.modes`` (the structure tests name it).

The gate answers one question per closed hour: may an entry go through now?

* mode ``off``: yes, and no network call is made;
* mode ``naik``: only while the condition is NAIK, and, when the volatility multiple is above zero,
  only while ATR14 (1h) does not exceed that multiple of its median over the last 720 hours. An
  unknown median (fewer than 720 ATR values) blocks while the guard is on.

The verdict is cached until the next 1h close, so the venue is read once per closed hour. A venue
error, a gap or a non-finite price in the candles blocks with reason ``data_error`` and is retried on
the next call. A blocked verdict for a data reason is logged at WARNING once per state change.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from bot.strategy.modes.regime import NAIK, pilih_mode
from bot.strategy.pine_indicators import atr, ema, rolling_median, sma

ATR_PERIOD_1H = 14  # frames.ATR_PERIOD_1H
ATR_MEDIAN_HOURS = 720  # frames.ATR_MEDIAN_HOURS: 30 days of closed hours
SMA_FAST_PERIOD_1H = 20  # config/modes.yaml pemilih_mode.sma_cepat_1h
SMA_SLOW_PERIOD_1H = 50  # config/modes.yaml pemilih_mode.sma_lambat_1h
EMA_PERIOD_4H = 50  # config/modes.yaml pemilih_mode.ema_4h

KLINES_1H = 1000  # ATR14 needs 112 candles to settle and its 720-hour median 719 more
KLINES_4H = 500  # the 4h EMA50 needs 400 candles to settle

HOUR_MS = 3_600_000
FOUR_HOUR_MS = 4 * HOUR_MS

MODE_OFF = "off"
MODE_NAIK = "naik"
MODES = frozenset({MODE_OFF, MODE_NAIK})

REASON_OFF = "off"
REASON_NAIK = "naik"
REASON_VOLATILITY = "volatility"
REASON_VOLATILITY_UNKNOWN = "volatility_unknown"
REASON_HISTORY_SHORT = "history_short"
REASON_DATA_ERROR = "data_error"
_REASON_NOT_NAIK = "not_naik:"

_WARNING_REASONS = frozenset({REASON_VOLATILITY_UNKNOWN, REASON_HISTORY_SHORT, REASON_DATA_ERROR})

Candles = List[Dict[str, float]]


@dataclass(frozen=True)
class RegimeVerdict:
    """Whether an entry may go through, and the numbers behind the answer.

    ``kondisi`` is what ``pilih_mode`` read (``""`` when it was not evaluated). ``reason`` is
    ``off``, ``naik``, ``not_naik:<KONDISI>``, ``volatility``, ``volatility_unknown``,
    ``history_short`` or ``data_error``. ``hour_close_ms`` is the close time of the last 1h candle
    the verdict rests on."""

    kondisi: str
    allowed: bool
    reason: str
    atr14_1h: Optional[float]
    atr_median: Optional[float]
    hour_close_ms: Optional[int]


class CandleDataError(ValueError):
    """The venue's candles cannot be trusted: a hole, a disorder or a non-finite price."""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _checked(rows: Candles, interval_ms: int) -> Candles:
    """``rows`` when each candle is finite and follows the one before by one interval."""
    previous: Optional[int] = None
    for row in rows:
        stamp = int(row["timestamp"])
        if previous is not None and stamp != previous + interval_ms:
            raise CandleDataError(f"candle at {stamp} does not follow {previous} by {interval_ms} ms")
        if not all(math.isfinite(float(row[key])) for key in ("high", "low", "close")):
            raise CandleDataError(f"candle at {stamp} has a non-finite price")
        previous = stamp
    return rows


def _last(series: Sequence[Optional[float]]) -> Optional[float]:
    return series[-1] if series else None


class RegimeGate:
    """Reads the venue's closed 1h and 4h candles once per closed hour and answers ``verdict()``."""

    def __init__(
        self,
        client: Any,
        symbol: str,
        mode: str,
        volatility_mult: float,
        sideways_mult: float,
        logger: logging.Logger,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"regime gate mode must be one of {sorted(MODES)}, got {mode!r}")
        self._client = client
        self._symbol = symbol
        self._mode = mode
        self._volatility_mult = float(volatility_mult)
        self._sideways_mult = float(sideways_mult)
        self._logger = logger
        self._clock_ms = clock_ms
        self._cached: Optional[RegimeVerdict] = None
        self._last_logged_reason: Optional[str] = None
        self._last_verdict: Optional[RegimeVerdict] = None
        self._detail = ""

    async def verdict(self) -> RegimeVerdict:
        if self._mode == MODE_OFF:
            return RegimeVerdict("", True, REASON_OFF, None, None, None)
        if self._cached is not None and self._cache_is_current(self._cached):
            return self._cached
        verdict = await self._evaluate()
        self._note(verdict)
        # a data error is not cached: the next call asks the venue again
        self._cached = None if verdict.reason == REASON_DATA_ERROR else verdict
        self._last_verdict = verdict
        return verdict

    def as_payload(self) -> dict:
        """The last verdict as a plain dict for the decision payload; ``{}`` before the first."""
        return dataclasses.asdict(self._last_verdict) if self._last_verdict is not None else {}

    def _cache_is_current(self, cached: RegimeVerdict) -> bool:
        return cached.hour_close_ms is not None and self._clock_ms() < cached.hour_close_ms + HOUR_MS

    async def _evaluate(self) -> RegimeVerdict:
        try:
            rows_1h = _checked(await self._client.get_klines(self._symbol, "1h", KLINES_1H), HOUR_MS)
            rows_4h = _checked(await self._client.get_klines(self._symbol, "4h", KLINES_4H), FOUR_HOUR_MS)
        except Exception as exc:  # noqa: BLE001 - the gate fails closed on any read problem
            self._detail = f"{type(exc).__name__}: {exc}"
            return RegimeVerdict("", False, REASON_DATA_ERROR, None, None, None)
        if not rows_1h:
            return RegimeVerdict("", False, REASON_HISTORY_SHORT, None, None, None)
        hour_close_ms = int(rows_1h[-1]["timestamp"]) + HOUR_MS
        # the last 4h candle closed at or before that hour close, as the backtest reads it
        closed_4h = [r for r in rows_4h if int(r["timestamp"]) + FOUR_HOUR_MS <= hour_close_ms]
        return self._classify(rows_1h, closed_4h, hour_close_ms)

    def _classify(self, rows_1h: Candles, rows_4h: Candles, hour_close_ms: int) -> RegimeVerdict:
        highs = [float(r["high"]) for r in rows_1h]
        lows = [float(r["low"]) for r in rows_1h]
        closes = [float(r["close"]) for r in rows_1h]
        atr_1h = atr(highs, lows, closes, ATR_PERIOD_1H)
        closes_4h = [float(r["close"]) for r in rows_4h]
        atr14 = _last(atr_1h)
        median = _last(rolling_median(atr_1h, ATR_MEDIAN_HOURS))
        sma_fast = _last(sma(closes, SMA_FAST_PERIOD_1H))
        sma_slow = _last(sma(closes, SMA_SLOW_PERIOD_1H))
        close_4h = closes_4h[-1] if closes_4h else None
        ema_4h = _last(ema(closes_4h, EMA_PERIOD_4H))
        kondisi = pilih_mode(sma_fast, sma_slow, atr14, close_4h, ema_4h, self._sideways_mult)

        def verdict(allowed: bool, reason: str) -> RegimeVerdict:
            return RegimeVerdict(kondisi, allowed, reason, atr14, median, hour_close_ms)

        if None in (sma_fast, sma_slow, atr14, close_4h, ema_4h):
            return verdict(False, REASON_HISTORY_SHORT)
        if kondisi != NAIK:
            return verdict(False, f"{_REASON_NOT_NAIK}{kondisi}")
        if self._volatility_mult > 0.0:
            if median is None:
                return verdict(False, REASON_VOLATILITY_UNKNOWN)
            if atr14 > self._volatility_mult * median:
                return verdict(False, REASON_VOLATILITY)
        return verdict(True, REASON_NAIK)

    def _note(self, verdict: RegimeVerdict) -> None:
        """Log a change of state once: WARNING for a data reason, INFO for the rest."""
        if verdict.reason == self._last_logged_reason:
            return
        self._last_logged_reason = verdict.reason
        level = logging.WARNING if verdict.reason in _WARNING_REASONS else logging.INFO
        self._logger.log(
            level,
            "Regime gate %s: %s | allowed=%s kondisi=%s atr14_1h=%s atr_median=%s%s",
            self._symbol, verdict.reason, verdict.allowed, verdict.kondisi or "-",
            verdict.atr14_1h, verdict.atr_median,
            f" | {self._detail}" if verdict.reason == REASON_DATA_ERROR else "",
        )


def build_regime_gate(settings: Any, client: Any, logger: logging.Logger) -> Optional[RegimeGate]:
    """The gate for ``main.py``: ``None`` while ``ENTRY_REGIME_GATE`` is off.

    The gate reads one symbol's candles, so a bot that trades several symbols could not tell which
    one a verdict is about: with the gate on, anything but a single symbol refuses to boot."""
    mode = getattr(settings, "entry_regime_gate", MODE_OFF)
    if mode == MODE_OFF:
        return None
    symbols = tuple(settings.symbols)
    if len(symbols) != 1:
        raise ValueError(
            f"ENTRY_REGIME_GATE={mode} needs exactly one symbol in SYMBOLS, "
            f"got {len(symbols)}: {', '.join(symbols) or 'none'}. Run one bot per symbol."
        )
    return RegimeGate(
        client,
        symbols[0],
        mode,
        settings.entry_volatility_guard_mult,
        settings.regime_sideways_atr_mult,
        logger,
    )
