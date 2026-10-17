"""The mode selector of the LONG module: ``pilih_mode`` of spec section 2.2, as a pure function.

The inputs are numbers from the closed candles: the 1h SMA20 and SMA50, the 1h ATR14, and the
close and the EMA50 of the last closed 4h candle. A ``None`` in any of the five (a value not yet
computed, as during warm-up) gives ``TIDAK_ENTRY`` before anything is computed. The checks run in
the document's order: the sideways band first, then the up-trend, then the down-trend.
"""

from typing import Optional

SIDEWAYS = "SIDEWAYS"
NAIK = "NAIK"
TURUN = "TURUN"
TIDAK_ENTRY = "TIDAK_ENTRY"


def pilih_mode(
    sma20_1h: Optional[float],
    sma50_1h: Optional[float],
    atr14_1h: Optional[float],
    close_4h: Optional[float],
    ema50_4h: Optional[float],
    sideways_jarak_kali_atr: float,
) -> str:
    if (
        sma20_1h is None
        or sma50_1h is None
        or atr14_1h is None
        or close_4h is None
        or ema50_4h is None
    ):
        return TIDAK_ENTRY
    gap = abs(sma20_1h - sma50_1h)
    if gap < sideways_jarak_kali_atr * atr14_1h:
        return SIDEWAYS  # mode C
    if sma20_1h > sma50_1h and close_4h > ema50_4h:
        return NAIK  # mode A, B
    if sma20_1h < sma50_1h:
        return TURUN  # mode D, D2 (no 4h filter, on purpose)
    return TIDAK_ENTRY
