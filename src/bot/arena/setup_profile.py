"""Classify an entry into stable buckets so attribution can see the *setup*.

Attribution already buckets closed trades by symbol, direction, factor, trigger,
hour and confidence band. None of those can express "opened while price was
already stretched away from the mean", which is the shape the BingX cycle
repeated: LONG at 78,293 / 78,309 / 78,301 in the same range, all stopped out,
and SHORT at the range low. The pattern was invisible to the feedback loop, so
nothing stopped the AI from taking it again.

The measurement is `(last_price - ema_21) / atr_14` — distance from the mean in
ATR units, which is comparable across symbols and volatility regimes in a way
that a raw price or percentage is not.

Raw measurements are kept alongside the buckets so thresholds can be retuned
against real trades later without re-instrumenting.
"""

from __future__ import annotations

from typing import Any, Mapping

UNKNOWN = "unknown"

# Beyond this many ATR from ema_21 the entry counts as stretched.
STRETCH_ATR_THRESHOLD = 0.75
# ATR as a percentage of price, splitting calm / normal / volatile regimes.
ATR_REGIME_MID_PCT = 0.15
ATR_REGIME_HIGH_PCT = 0.35
# Share of the top factors carrying category "microstructure". Retrospectively
# microstructure-led entries looked worse on OKX (n=281) but BingX had only 5
# such trades, so this is recorded for a prospective test, not acted on.
MICRO_LED_SHARE = 0.6
TREND_LED_MAX_MICRO_SHARE = 0.2
MICROSTRUCTURE = "microstructure"


def build_setup_profile(
    features: Mapping[str, Any] | None,
    direction: str,
    factor_snapshots: Any = None,
) -> dict[str, Any]:
    """Return the bucketed setup profile for one entry.

    Never raises: missing or unusable features degrade to `unknown` buckets so a
    trade is still recorded rather than dropped from attribution.
    """
    side = str(direction or "").strip().upper() or "UNKNOWN"
    atr_pct: float | None = None
    stretch: float | None = None
    rsi: float | None = None

    if features:
        last = _float(features.get("last_price"))
        atr = _float(features.get("atr_14"))
        ema = _float(features.get("ema_21"))
        rsi_value = _float(features.get("rsi_14"))
        rsi = rsi_value if rsi_value > 0 else None
        if last > 0 and atr > 0:
            atr_pct = atr / last * 100.0
            if ema > 0:
                stretch = (last - ema) / atr

    stretch_bucket = _stretch_bucket(stretch)
    atr_regime = _atr_regime(atr_pct)
    micro_share = _micro_share(factor_snapshots)
    family = _factor_family(micro_share)
    return {
        # Three axes only. Folding the factor family in as a fourth would triple
        # the bucket count and starve each one of sample; it is tracked separately.
        "setup_key": f"{side}|{stretch_bucket}|{atr_regime}",
        "family_key": f"{side}|{family}",
        "factor_family": family,
        "micro_share": round(micro_share, 4) if micro_share is not None else None,
        "direction": side,
        "stretch_bucket": stretch_bucket,
        "atr_regime": atr_regime,
        "stretch_atr": round(stretch, 4) if stretch is not None else None,
        "atr_pct": round(atr_pct, 4) if atr_pct is not None else None,
        "rsi_14": round(rsi, 4) if rsi is not None else None,
    }


def _micro_share(factor_snapshots: Any) -> float | None:
    """Fraction of the recorded top factors whose category is microstructure."""
    if not isinstance(factor_snapshots, (list, tuple)):
        return None
    categories = [
        str(item.get("category")).strip().lower()
        for item in factor_snapshots
        if isinstance(item, Mapping) and item.get("category")
    ]
    if not categories:
        return None
    return sum(1 for c in categories if c == MICROSTRUCTURE) / len(categories)


def _factor_family(micro_share: float | None) -> str:
    if micro_share is None:
        return UNKNOWN
    if micro_share >= MICRO_LED_SHARE:
        return "micro_led"
    if micro_share <= TREND_LED_MAX_MICRO_SHARE:
        return "trend_led"
    return "mixed"


def _stretch_bucket(stretch: float | None) -> str:
    if stretch is None:
        return UNKNOWN
    if stretch > STRETCH_ATR_THRESHOLD:
        return "stretch_up"
    if stretch < -STRETCH_ATR_THRESHOLD:
        return "stretch_down"
    return "stretch_flat"


def _atr_regime(atr_pct: float | None) -> str:
    if atr_pct is None:
        return UNKNOWN
    if atr_pct >= ATR_REGIME_HIGH_PCT:
        return "atr_high"
    if atr_pct >= ATR_REGIME_MID_PCT:
        return "atr_mid"
    return "atr_low"


def _float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result and abs(result) != float("inf") else 0.0
