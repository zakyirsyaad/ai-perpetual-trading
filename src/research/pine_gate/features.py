"""Look-ahead-free feature snapshots and the 24-hour volume-profile POC.

Research code; the bot never imports it. Standard library only.

``FeatureFrame.build`` aggregates 1m bars to 3m and 5m, prepends native 1h/4h
warm-up bars to the 1h/4h bars aggregated from 1m, and computes every
indicator series once. ``snapshot(t)`` then only looks up, per timeframe, the
last bar closed at ``t`` (``open + interval <= t``), so a forming bar can never
leak into a feature. Every series is causal, so building from
``slice_until(bars_1m, t)`` gives the same snapshot at ``t`` as the full frame.

POC anchor: the 24-hour window ends at the close of the last closed 5m bar,
which is ``t`` itself whenever ``t`` sits on a 5m boundary. Anchoring there
makes the POC a function of the 5m bar alone (price reference and window), so
it is memoised per 5m bar and a full window of exactly 1,440 bars is reachable
at any tick time, not only at whole minutes.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from bot.arena.setup_profile import build_setup_profile
from bot.strategy import indicators as live
from bot.strategy import pine_indicators as pine

from .bars import BarGapError, Bars, aggregate, concat, last_closed_index
from .spec import (
    ENTRY_PARAMS,
    STRENGTH_PARAMS,
    TREND_PARAMS,
    VOL_PARAMS,
    VP_PARAMS,
    spec_document,
)

_MIN_MS = 60_000
_HOUR_MS = 60 * _MIN_MS
INTERVAL_1M = _MIN_MS
INTERVAL_3M = 3 * _MIN_MS
INTERVAL_5M = 5 * _MIN_MS
INTERVAL_1H = _HOUR_MS
INTERVAL_4H = 4 * _HOUR_MS
_BPS = 10_000.0
_STRETCH_SIDE = "LONG"  # stretch_atr is signed by price, not by side

_SPEC = spec_document()
_CONTENDER2 = _SPEC["contender2"]
_REFERENCE = _SPEC["reference"]

EMA_TREND_LEN = TREND_PARAMS["E200"]["ema_len"]
EMA_REFERENCE_LEN = 125  # spec reference: "EMA125 on 5m"
ATR_5M_LEN = 14  # spec 5.8: distance to the POC in 5m ATR14 units
RSI_LEN = ENTRY_PARAMS["P35"]["rsi_len"]
RSI_REFERENCE_LEN = _REFERENCE["rsi"]["period"]
DMI_LEN = STRENGTH_PARAMS["ADX"]["dmi_len"]
ADX_SMOOTHING = STRENGTH_PARAMS["ADX"]["adx_smoothing"]
DMI_REFERENCE_LEN = _REFERENCE["adx"]["length"]
ADX_REFERENCE_SMOOTHING = _REFERENCE["adx"]["smoothing"]
BREAKOUT_SHORT_BARS = ENTRY_PARAMS["B12"]["bars"]
BREAKOUT_LONG_BARS = ENTRY_PARAMS["B24"]["bars"]
HTF_EMA_FAST = TREND_PARAMS["H1"]["ema_fast"]
HTF_EMA_SLOW = TREND_PARAMS["H1"]["ema_slow"]
HTF_ATR_LEN = TREND_PARAMS["H1"]["atr_len"]
VOL_LOOKBACK_1H = VOL_PARAMS["HIGH"]["lookback_1h"]
STRETCH_BARS_3M = _CONTENDER2["closed_3m_bars"]
STRETCH_EMA_LEN = _CONTENDER2["ema_period"]
STRETCH_ATR_LEN = _CONTENDER2["atr_period"]
POC_BIN_BPS = VP_PARAMS["ON"]["bin_bps"]
POC_WINDOW_MS = VP_PARAMS["ON"]["window_hours"] * _HOUR_MS
POC_BARS = POC_WINDOW_MS // INTERVAL_1M

PRICE_REL_TOL = 1e-9
VOLUME_REL_TOL = 1e-6

Column = Tuple[Optional[float], ...]


@dataclass(frozen=True)
class Snapshot:
    """Features at ``t_ms`` from closed bars only; ``None`` where not yet valid."""

    t_ms: int
    close_5m: Optional[float]
    ema200_5m: Optional[float]
    ema125_5m: Optional[float]
    atr14_5m: Optional[float]
    rsi14_5m: Optional[float]
    rsi5_5m: Optional[float]
    adx14_5m: Optional[float]
    di_plus14_5m: Optional[float]
    di_minus14_5m: Optional[float]
    adx11_5m: Optional[float]
    high12_prev: Optional[float]
    low12_prev: Optional[float]
    high24_prev: Optional[float]
    low24_prev: Optional[float]
    h1_ema20: Optional[float]
    h1_ema50: Optional[float]
    h1_atr14: Optional[float]
    h1_vol_pct: Optional[float]
    h4_ema20: Optional[float]
    h4_ema50: Optional[float]
    h4_atr14: Optional[float]
    stretch_3m: Optional[float]


@dataclass(frozen=True)
class _Table:
    """Columns aligned to ``bars``; each is read at the last closed bar."""

    bars: Bars
    columns: Mapping[str, Column]


def _previous(series: Sequence[Optional[float]]) -> Column:
    """Value of the bar before: index ``i`` holds ``series[i - 1]``."""
    return (None, *series[:-1]) if series else ()


def _columns_5m(bars: Bars) -> Dict[str, Column]:
    highs, lows, closes = list(bars.high), list(bars.low), list(bars.close)
    plus_di, minus_di, adx = pine.dmi(highs, lows, closes, DMI_LEN, ADX_SMOOTHING)
    adx_reference = pine.dmi(
        highs, lows, closes, DMI_REFERENCE_LEN, ADX_REFERENCE_SMOOTHING
    )[2]
    return {
        "close_5m": tuple(closes),
        "ema200_5m": tuple(pine.ema(closes, EMA_TREND_LEN)),
        "ema125_5m": tuple(pine.ema(closes, EMA_REFERENCE_LEN)),
        "atr14_5m": tuple(pine.atr(highs, lows, closes, ATR_5M_LEN)),
        "rsi14_5m": tuple(pine.rsi(closes, RSI_LEN)),
        "rsi5_5m": tuple(pine.rsi(closes, RSI_REFERENCE_LEN)),
        "adx14_5m": tuple(adx),
        "di_plus14_5m": tuple(plus_di),
        "di_minus14_5m": tuple(minus_di),
        "adx11_5m": tuple(adx_reference),
        "high12_prev": _previous(pine.rolling_max(highs, BREAKOUT_SHORT_BARS)),
        "low12_prev": _previous(pine.rolling_min(lows, BREAKOUT_SHORT_BARS)),
        "high24_prev": _previous(pine.rolling_max(highs, BREAKOUT_LONG_BARS)),
        "low24_prev": _previous(pine.rolling_min(lows, BREAKOUT_LONG_BARS)),
    }


def _volatility_percentile(
    atr_series: Sequence[Optional[float]], closes: Sequence[float]
) -> Column:
    """``percent_rank(ATR / close, lookback)``, ranked from the first valid ATR on."""
    ratios = [None if a is None else a / c for a, c in zip(atr_series, closes)]
    first = next((i for i, r in enumerate(ratios) if r is not None), len(ratios))
    return tuple([None] * first + pine.percent_rank(ratios[first:], VOL_LOOKBACK_1H))


def _columns_htf(bars: Bars, prefix: str, with_vol_pct: bool) -> Dict[str, Column]:
    highs, lows, closes = list(bars.high), list(bars.low), list(bars.close)
    atr_series = pine.atr(highs, lows, closes, HTF_ATR_LEN)
    columns = {
        f"{prefix}_ema20": tuple(pine.ema(closes, HTF_EMA_FAST)),
        f"{prefix}_ema50": tuple(pine.ema(closes, HTF_EMA_SLOW)),
        f"{prefix}_atr14": tuple(atr_series),
    }
    if with_vol_pct:
        columns[f"{prefix}_vol_pct"] = _volatility_percentile(atr_series, closes)
    return columns


def _stretch_column(bars: Bars) -> Column:
    """Live near-mean stretch over the last ``STRETCH_BARS_3M`` closed 3m bars."""
    highs, lows, closes = list(bars.high), list(bars.low), list(bars.close)
    out: List[Optional[float]] = []
    for end in range(1, len(closes) + 1):
        if end < STRETCH_BARS_3M:
            out.append(None)
            continue
        start = end - STRETCH_BARS_3M
        window = closes[start:end]
        features = {
            "last_price": window[-1],
            "ema_21": live.ema(window, STRETCH_EMA_LEN),
            "atr_14": live.atr(highs[start:end], lows[start:end], window, STRETCH_ATR_LEN),
        }
        out.append(build_setup_profile(features, _STRETCH_SIDE)["stretch_atr"])
    return tuple(out)


def _head(bars: Bars, end: int) -> Bars:
    return Bars(
        bars.interval_ms,
        bars.open_ms[:end],
        bars.open[:end],
        bars.high[:end],
        bars.low[:end],
        bars.close[:end],
        bars.volume[:end],
    )


def htf_bars(bars_1m: Bars, warmup: Optional[Bars], interval_ms: int) -> Bars:
    """Native warm-up bars before the first aggregated bar, then the aggregated bars.

    ``aggregate`` drops a partial leading bucket (1m data starting mid-bucket), so
    the native bar of that bucket is kept and fills the seam. The last kept native
    bar must open exactly one interval before the first aggregated bar, else
    ``BarGapError``. With no aggregated bar yet the whole warm-up is used;
    ``last_closed_index`` still hides any bar not closed at the snapshot time.
    """
    aggregated = aggregate(bars_1m, interval_ms)
    if warmup is None:
        return aggregated
    if warmup.interval_ms != interval_ms:
        raise ValueError(
            f"warm-up interval {warmup.interval_ms} ms, expected {interval_ms} ms"
        )
    if not len(aggregated):
        return warmup
    first = aggregated.open_ms[0]
    keep = bisect_left(warmup.open_ms, first)
    if keep and warmup.open_ms[keep - 1] + interval_ms != first:
        raise BarGapError(
            f"warm-up ends at {warmup.open_ms[keep - 1]}, not one {interval_ms} ms bar before "
            f"the first aggregated bar at {first}"
        )
    return concat(_head(warmup, keep), aggregated)


def warmup_unused(bars_1m: Bars, warmup: Optional[Bars], interval_ms: int) -> bool:
    """Whether a present warm-up adds nothing to ``htf_bars``: it is empty or starts at
    or after the first aggregated bar (so long EMAs stay ``None`` for weeks)."""
    if warmup is None:
        return False
    aggregated = aggregate(bars_1m, interval_ms)
    if not len(aggregated):
        return not len(warmup)
    return bisect_left(warmup.open_ms, aggregated.open_ms[0]) == 0


def _poc(
    lows: Sequence[float], highs: Sequence[float], volumes: Sequence[float], price: float
) -> Optional[float]:
    """Centre of the busiest bin; ties go to the bin nearest ``price``, then the lower bin."""
    width = price * POC_BIN_BPS / _BPS
    profile: Dict[int, float] = {}
    for low, high, volume in zip(lows, highs, volumes):
        first, last = math.floor(low / width), math.floor(high / width)
        share = volume / (last - first + 1)
        for k in range(first, last + 1):
            profile[k] = profile.get(k, 0.0) + share
    if sum(profile.values()) <= 0.0:
        return None
    best_k: Optional[int] = None
    best_key: Tuple[float, float] = (0.0, 0.0)
    for k in sorted(profile):
        key = (-profile[k], abs((k + 0.5) * width - price))
        if best_k is None or key < best_key:
            best_k, best_key = k, key
    return (best_k + 0.5) * width


class FeatureFrame:
    """Precomputed feature series for one symbol; read through ``snapshot``."""

    __slots__ = ("_symbol", "_bars_1m", "_bars_5m", "_tables", "_poc_cache")

    def __init__(self, symbol: str, bars_1m: Bars, bars_5m: Bars, tables: Tuple[_Table, ...]):
        self._symbol = symbol
        self._bars_1m = bars_1m
        self._bars_5m = bars_5m
        self._tables = tables
        self._poc_cache: Dict[int, Optional[float]] = {}

    @classmethod
    def build(
        cls,
        symbol: str,
        bars_1m: Bars,
        warmup_1h: Optional[Bars] = None,
        warmup_4h: Optional[Bars] = None,
    ) -> "FeatureFrame":
        if bars_1m.interval_ms != INTERVAL_1M:
            raise ValueError(f"expected 1m bars, got {bars_1m.interval_ms} ms")
        bars_3m = aggregate(bars_1m, INTERVAL_3M)
        bars_5m = aggregate(bars_1m, INTERVAL_5M)
        bars_1h = htf_bars(bars_1m, warmup_1h, INTERVAL_1H)
        bars_4h = htf_bars(bars_1m, warmup_4h, INTERVAL_4H)
        tables = (
            _Table(bars_5m, _columns_5m(bars_5m)),
            _Table(bars_1h, _columns_htf(bars_1h, "h1", with_vol_pct=True)),
            _Table(bars_4h, _columns_htf(bars_4h, "h4", with_vol_pct=False)),
            _Table(bars_3m, {"stretch_3m": _stretch_column(bars_3m)}),
        )
        return cls(symbol, bars_1m, bars_5m, tables)

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def bars_1m(self) -> Bars:
        return self._bars_1m

    def series(self) -> Tuple[Bars, ...]:
        """Every bar series the frame reads: 1m, then its 5m, 1h, 4h and 3m tables."""
        return (self._bars_1m, *(table.bars for table in self._tables))

    def snapshot(self, t_ms: int) -> Snapshot:
        values: Dict[str, object] = {"t_ms": t_ms}
        for table in self._tables:
            i = last_closed_index(table.bars, t_ms)
            for name, column in table.columns.items():
                values[name] = column[i] if i >= 0 else None
        return Snapshot(**values)

    def poc_24h(self, t_ms: int) -> Optional[float]:
        """POC of the 24 hours of 1m bars ending at the last closed 5m bar's close."""
        i5 = last_closed_index(self._bars_5m, t_ms)
        if i5 < 0:
            return None
        if i5 not in self._poc_cache:
            self._poc_cache[i5] = self._compute_poc(i5)
        return self._poc_cache[i5]

    def _compute_poc(self, i5: int) -> Optional[float]:
        anchor = self._bars_5m.open_ms[i5] + self._bars_5m.interval_ms
        price = self._bars_5m.close[i5]
        bars = self._bars_1m
        last = last_closed_index(bars, anchor)
        first = bisect_left(bars.open_ms, anchor - POC_WINDOW_MS)
        if last - first + 1 != POC_BARS or price <= 0.0:
            return None
        end = last + 1
        return _poc(bars.low[first:end], bars.high[first:end], bars.volume[first:end], price)


@dataclass(frozen=True)
class HtfAgreement:
    """Native vs aggregated higher-timeframe bars over their shared open times.

    OHLC is what the 1h/4h features read; volume is reported apart because no
    feature reads 1h/4h volume, but it is the only cross-check of the 1m volume
    the POC reads. ``worst_volume_rel`` is NaN when any pair's is.
    """

    bars: int
    ohlc_share: float
    volume_share: float
    worst_volume_rel: float


def _ohlc_agrees(native: Bars, i: int, aggregated: Bars, j: int) -> bool:
    prices = (
        (native.open[i], aggregated.open[j]),
        (native.high[i], aggregated.high[j]),
        (native.low[i], aggregated.low[j]),
        (native.close[i], aggregated.close[j]),
    )
    return all(math.isclose(a, b, rel_tol=PRICE_REL_TOL, abs_tol=0.0) for a, b in prices)


def _relative_difference(a: float, b: float) -> float:
    """|a - b| over the larger magnitude, the measure ``math.isclose`` compares."""
    scale = max(abs(a), abs(b))
    return 0.0 if scale == 0.0 else abs(a - b) / scale


def htf_agreement(native: Bars, aggregated: Bars) -> HtfAgreement:
    """OHLC and volume agreement of the open times present in both."""
    if native.interval_ms != aggregated.interval_ms:
        raise ValueError(
            f"interval mismatch: {native.interval_ms} ms vs {aggregated.interval_ms} ms"
        )
    index = {t: j for j, t in enumerate(aggregated.open_ms)}
    pairs = [(i, index[t]) for i, t in enumerate(native.open_ms) if t in index]
    if not pairs:
        raise ValueError("native and aggregated bars share no open time")
    ohlc = sum(1 for i, j in pairs if _ohlc_agrees(native, i, aggregated, j))
    rel = [_relative_difference(native.volume[i], aggregated.volume[j]) for i, j in pairs]
    volume = sum(1 for r in rel if r <= VOLUME_REL_TOL)
    worst = math.nan if any(math.isnan(r) for r in rel) else max(rel)  # max() skips a NaN
    return HtfAgreement(len(pairs), ohlc / len(pairs), volume / len(pairs), worst)
