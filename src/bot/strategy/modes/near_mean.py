"""Mode D's trigger: SHORT while the 3m price sits near its mean, measured as the live gate does.

``stretch_from_window`` is the measurement of the live entry stretch gate: the distance of the last
close from the EMA of the window, in ATRs, taken from ``bot.strategy.indicators`` and
``build_setup_profile``, so it is the number that decides a live entry. Like the profile it rounds
to four decimals. The side passed to the profile is ``LONG`` because the stretch is signed by
price, not by side: positive above the mean, negative below it.

``near_mean_short`` is the trigger on that number. The band is inclusive, as the live gate blocks
only when ``abs(stretch) > max``. A stretch that could not be measured is ``None`` (a flat window
has an ATR of 0, a window of fewer than ``atr_period + 1`` bars has none) and is no signal: the
live gate lets such an entry through, the trigger of a backtest needs a measured value.

The module takes numbers only: no I/O, no bar series and no Pine indicator code. No live module
imports it.
"""

from __future__ import annotations

from typing import Optional, Sequence

from bot.arena.setup_profile import build_setup_profile
from bot.strategy.indicators import atr, ema

__all__ = ["near_mean_short", "stretch_from_window"]

_STRETCH_SIDE = "LONG"  # the stretch is signed by price, not by side


def stretch_from_window(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    ema_period: int,
    atr_period: int,
) -> Optional[float]:
    """The stretch of the window's last close, in ATRs from the EMA, as the live gate measures it.

    ``highs``, ``lows`` and ``closes`` are the bars of the window, oldest first, one entry per bar;
    the live gate reads the last 39 closed 3m bars with an EMA of 21 and an ATR of 14. The result
    is ``(close - EMA) / ATR`` rounded to four decimals, or ``None`` when the profile cannot
    measure it (an ATR of 0). The live indicators are kept as they are: with fewer closes than
    ``ema_period`` the EMA is the last close and the stretch is 0.0, with fewer than
    ``atr_period + 1`` the ATR is 0 and there is no stretch.

    Bars that do not match (different lengths, or none) raise ``ValueError``: a window longer
    on one side would otherwise be measured on misaligned bars without a word."""
    if not closes or len(highs) != len(closes) or len(lows) != len(closes):
        raise ValueError(
            "highs, lows and closes must hold the same, non-zero number of bars, "
            f"got {len(highs)}, {len(lows)} and {len(closes)}"
        )
    features = {
        "last_price": closes[-1],
        # The profile reads these two keys whatever the periods are.
        "ema_21": ema(closes, ema_period),
        "atr_14": atr(highs, lows, closes, atr_period),
    }
    return build_setup_profile(features, _STRETCH_SIDE)["stretch_atr"]


def near_mean_short(stretch_atr: Optional[float], max_abs: float) -> bool:
    """Whether the price sits within ``max_abs`` ATRs of its mean: Mode D's SHORT trigger.

    Inclusive on both sides of the mean. ``None``, NaN and infinity are not near it."""
    return stretch_atr is not None and abs(stretch_atr) <= max_abs
