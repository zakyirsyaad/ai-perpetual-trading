"""Pine-compatible indicator series for offline research.

Every function takes plain lists and returns a list aligned to its input, with
``None`` wherever the value is not yet valid. All functions are causal: the
output at index ``i`` depends only on inputs ``0..i``, so computing on a prefix
gives the same value as computing on the full series.

Recursive smoothers (RMA, EMA, RSI, ATR, DMI) seed with the simple mean of their
first ``n`` valid values, as TradingView does, and report ``None`` until
``min_bars`` bars exist. ``min_bars=None`` means ``DEFAULT_WARMUP_PERIODS * n``,
which lets the seed's influence decay. Output at index ``i`` is valid once
``i >= min_bars - 1``.

Window statistics (``sma``, ``stdev``, ``rolling_median``) are ``None`` until ``n``
values exist and for as long as the last ``n`` values include a ``None``, for
instance an indicator that is still warming up. ``stdev`` is the population
deviation, as in TradingView's ``ta.stdev``.

Not wired into the live bot.
"""

from __future__ import annotations

from statistics import fmean, median, pstdev
from typing import Optional, Sequence

DEFAULT_WARMUP_PERIODS = 8
RSI_NEUTRAL = 50.0

Series = list[Optional[float]]


def _warmup(n: int, min_bars: Optional[int]) -> int:
    return DEFAULT_WARMUP_PERIODS * n if min_bars is None else min_bars


def _require_period(n: int) -> None:
    if n < 1:
        raise ValueError(f"period must be >= 1, got {n}")


def _mask(raw: Sequence[Optional[float]], min_bars: int) -> Series:
    """Blank every value before index ``min_bars - 1``."""
    first_valid = max(min_bars, 1) - 1
    return [value if i >= first_valid else None for i, value in enumerate(raw)]


def _recursive(values: Sequence[Optional[float]], n: int, alpha: float) -> Series:
    """Mean-seeded exponential smoother; leading ``None`` inputs are skipped."""
    out: Series = [None] * len(values)
    seed_sum = 0.0
    seen = 0
    prev: Optional[float] = None
    for i, value in enumerate(values):
        if value is None:
            continue
        if prev is None:
            seed_sum += value
            seen += 1
            if seen == n:
                prev = seed_sum / n
                out[i] = prev
        else:
            prev = prev + alpha * (value - prev)
            out[i] = prev
    return out


def _rma_raw(values: Sequence[Optional[float]], n: int) -> Series:
    _require_period(n)
    return _recursive(values, n, 1.0 / n)


def rma(values: Sequence[Optional[float]], n: int, min_bars: Optional[int] = None) -> Series:
    """Wilder moving average (alpha = 1/n)."""
    return _mask(_rma_raw(values, n), _warmup(n, min_bars))


def ema(values: Sequence[Optional[float]], n: int, min_bars: Optional[int] = None) -> Series:
    """Exponential moving average (alpha = 2/(n+1))."""
    _require_period(n)
    return _mask(_recursive(values, n, 2.0 / (n + 1)), _warmup(n, min_bars))


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        # No losses -> 100; no movement at all is undefined, report neutral.
        return 100.0 if avg_gain > 0.0 else RSI_NEUTRAL
    if avg_gain == 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(closes: Sequence[float], n: int, min_bars: Optional[int] = None) -> Series:
    """Relative strength index; changes start at the second close.

    avg loss == 0 -> 100.0; avg gain == 0 with losses -> 0.0; both zero -> 50.0.
    """
    _require_period(n)
    gains: Series = [None] * len(closes)
    losses: Series = [None] * len(closes)
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)
    avg_gain = _rma_raw(gains, n)
    avg_loss = _rma_raw(losses, n)
    raw: Series = [
        None if g is None or l is None else _rsi_from_averages(g, l)
        for g, l in zip(avg_gain, avg_loss)
    ]
    return _mask(raw, _warmup(n, min_bars))


def true_range(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float]:
    """True range; the first bar has no previous close, so it is high - low."""
    out: list[float] = []
    for i, (high, low) in enumerate(zip(highs, lows)):
        if i == 0:
            out.append(high - low)
        else:
            prev_close = closes[i - 1]
            out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    n: int,
    min_bars: Optional[int] = None,
) -> Series:
    """Average true range (Wilder)."""
    return rma(true_range(highs, lows, closes), n, min_bars)


def dmi(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    di_len: int,
    adx_len: int,
    min_bars: Optional[int] = None,
) -> tuple[Series, Series, Series]:
    """Directional movement index per Pine v5: returns (+DI, -DI, ADX).

    up = high - prev high, down = prev low - low;
    +DM = up if up > down and up > 0 else 0 (-DM mirrored);
    +DI = 100 * rma(+DM) / rma(TR); DX = 100 * |+DI - -DI| / ((+DI + -DI) or 1);
    ADX = rma(DX, adx_len). TR starts at bar 1 here (Pine's ta.dmi uses ta.tr, na on bar 0),
    while ``atr`` keeps bar 0 = high - low. A zero true range gives DI 0.0, so flat bars
    give ADX 0.0.
    Default warm-up is 8 * max(di_len, adx_len) for all three outputs.
    """
    _require_period(di_len)
    _require_period(adx_len)
    count = len(highs)
    plus_dm: Series = [None] * count
    minus_dm: Series = [None] * count
    for i in range(1, count):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0.0 else 0.0
        minus_dm[i] = down if down > up and down > 0.0 else 0.0

    # Pine's ta.dmi smooths the built-in ta.tr, which is na on bar 0 (unlike ta.atr),
    # so TR and both DM series all start at bar 1 and seed at the same index.
    tr_from_bar_one: Series = [None, *true_range(highs, lows, closes)[1:]][:count]
    tr_avg = _rma_raw(tr_from_bar_one, di_len)
    plus_avg = _rma_raw(plus_dm, di_len)
    minus_avg = _rma_raw(minus_dm, di_len)

    plus_di: Series = [None] * count
    minus_di: Series = [None] * count
    dx: Series = [None] * count
    for i in range(count):
        tr, plus, minus = tr_avg[i], plus_avg[i], minus_avg[i]
        if tr is None or plus is None or minus is None:
            continue
        p = 100.0 * plus / tr if tr else 0.0
        m = 100.0 * minus / tr if tr else 0.0
        plus_di[i] = p
        minus_di[i] = m
        dx[i] = 100.0 * abs(p - m) / ((p + m) or 1.0)
    adx = _rma_raw(dx, adx_len)

    warmup = _warmup(max(di_len, adx_len), min_bars)
    return _mask(plus_di, warmup), _mask(minus_di, warmup), _mask(adx, warmup)


def _rolling(values: Sequence[Optional[float]], n: int, pick) -> Series:
    _require_period(n)
    return [
        pick(values[i - n + 1 : i + 1]) if i >= n - 1 else None
        for i in range(len(values))
    ]


def rolling_max(values: Sequence[float], n: int) -> Series:
    """Maximum over the last ``n`` values including the current one."""
    return _rolling(values, n, max)


def rolling_min(values: Sequence[float], n: int) -> Series:
    """Minimum over the last ``n`` values including the current one."""
    return _rolling(values, n, min)


def _rolling_complete(values: Sequence[Optional[float]], n: int, pick) -> Series:
    """``_rolling`` that also gives ``None`` while the window holds a ``None``."""
    return _rolling(values, n, lambda window: None if None in window else pick(window))


def sma(values: Sequence[Optional[float]], n: int) -> Series:
    """Simple moving average of the last ``n`` values including the current one."""
    return _rolling_complete(values, n, fmean)


def stdev(values: Sequence[Optional[float]], n: int) -> Series:
    """Population standard deviation over the last ``n`` values, like ``ta.stdev``."""
    return _rolling_complete(values, n, pstdev)


def rolling_median(values: Sequence[Optional[float]], n: int) -> Series:
    """Median of the last ``n`` values; the mean of the middle two when ``n`` is even."""
    return _rolling_complete(values, n, median)


def percent_rank(values: Sequence[float], n: int) -> Series:
    """Pine ``ta.percentrank``: % of the previous ``n`` values <= the current one."""
    _require_period(n)
    out: Series = [None] * len(values)
    for i in range(n, len(values)):
        current = values[i]
        at_or_below = sum(1 for prior in values[i - n : i] if prior <= current)
        out[i] = 100.0 * at_or_below / n
    return out
