from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class MarketFeatures:
    symbol: str
    last_price: float
    ema_fast: float
    ema_slow: float
    ema_21: float
    rsi_14: float
    atr_14: float
    macd_line: float
    macd_signal: float
    macd_hist: float
    change_pct_1: float
    change_pct_5: float
    volume: float
    volume_change_pct: float
    funding_rate: float
    open_interest: float
    recent_high_20: float
    recent_low_20: float
    range_position_20: float
    volatility_pct: float


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return values[-1]
    k = 2 / (period + 1)
    current = values[0]
    for v in values[1:]:
        current = (v * k) + (current * (1 - k))
    return current


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 0.0
    true_ranges: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)
    return sum(true_ranges[-period:]) / period


def pct_change(values: list[float], lookback: int) -> float:
    if len(values) <= lookback:
        return 0.0
    prev = values[-1 - lookback]
    if prev == 0:
        return 0.0
    return ((values[-1] - prev) / prev) * 100


def macd(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[float, float, float]:
    """Returns (macd_line, macd_signal, macd_hist) for the last candle."""
    if len(closes) < slow + signal_period:
        return 0.0, 0.0, 0.0
    ema_fast_vals: list[float] = []
    ema_slow_vals: list[float] = []
    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)
    cur_fast = closes[0]
    cur_slow = closes[0]
    for c in closes[1:]:
        cur_fast = (c * k_fast) + (cur_fast * (1 - k_fast))
        cur_slow = (c * k_slow) + (cur_slow * (1 - k_slow))
        ema_fast_vals.append(cur_fast)
        ema_slow_vals.append(cur_slow)
    macd_line_vals = [f - s for f, s in zip(ema_fast_vals, ema_slow_vals)]
    if len(macd_line_vals) < signal_period:
        return 0.0, 0.0, 0.0
    macd_line = macd_line_vals[-1]
    signal = ema(macd_line_vals, signal_period)
    hist = macd_line - signal
    return macd_line, signal, hist

