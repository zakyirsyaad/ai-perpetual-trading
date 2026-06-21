"""Selective confluence entry signal — shared by live strategy and backtest.

Validated approach (taker 0.05%, 3m/5m timeframes): instead of mechanical
LONG/SHORT alternation, derive direction from the 86-factor consensus and only
enter when factors agree AND volatility can clear the fee floor. TP/SL scale to
ATR so targets track realized volatility. Walk-forward tested: gross >= ~50% of
fee across consecutive segments on BTC/ETH/SOL at 3m/5m (1m is too noisy).
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.data.market_signals import TechnicalFeatures
from bot.models import PositionDirection


@dataclass(frozen=True, slots=True)
class ConfluenceDecision:
    enter: bool
    direction: PositionDirection
    mean_score: float
    agreement: float
    atr_pct: float
    take_profit_pct: float
    stop_loss_pct: float
    reason: str


def evaluate_confluence(
    snapshots: list,
    *,
    atr_pct: float,
    min_agreement: float,
    min_atr_pct: float,
    min_score: float,
    atr_tp_mult: float,
    atr_sl_mult: float,
    tp_floor_pct: float,
    sl_floor_pct: float,
) -> ConfluenceDecision:
    """Decide entry from factor snapshots for ONE symbol.

    `snapshots` items may be FactorSnapshot objects or dicts (backtest passes the
    objects directly, the live context passes dicts). We read `direction` and
    `score` from either form.
    """
    directional = []
    score_sum = 0.0
    longs = 0
    shorts = 0
    for item in snapshots:
        direction = _get(item, "direction")
        score = float(_get(item, "score") or 0.0)
        if direction not in ("LONG", "SHORT"):
            continue
        directional.append(item)
        score_sum += score
        if direction == "LONG":
            longs += 1
        else:
            shorts += 1

    count = len(directional)
    if count == 0:
        return ConfluenceDecision(False, PositionDirection.LONG, 0.0, 0.0, atr_pct,
                                  tp_floor_pct, sl_floor_pct, "no_directional_factors")

    mean_score = score_sum / count
    agreement = max(longs, shorts) / count
    direction = PositionDirection.LONG if mean_score > 0 else PositionDirection.SHORT

    if agreement < min_agreement:
        return ConfluenceDecision(False, direction, mean_score, agreement, atr_pct,
                                  tp_floor_pct, sl_floor_pct, f"low_agreement={agreement:.2f}")
    if atr_pct < min_atr_pct:
        return ConfluenceDecision(False, direction, mean_score, agreement, atr_pct,
                                  tp_floor_pct, sl_floor_pct, f"low_volatility={atr_pct:.3f}%")
    if abs(mean_score) < min_score:
        return ConfluenceDecision(False, direction, mean_score, agreement, atr_pct,
                                  tp_floor_pct, sl_floor_pct, f"weak_score={mean_score:.3f}")

    tp = max(atr_pct * atr_tp_mult, tp_floor_pct)
    sl = max(atr_pct * atr_sl_mult, sl_floor_pct)
    return ConfluenceDecision(
        True, direction, mean_score, agreement, atr_pct, tp, sl,
        f"confluence agree={agreement:.2f} score={mean_score:.3f} atr={atr_pct:.3f}% "
        f"dir={direction.value} tp={tp:.3f}% sl={sl:.3f}%",
    )


def _get(item, key):
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)
