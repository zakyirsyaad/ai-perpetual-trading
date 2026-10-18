"""Mode L's exit levels: the live bot's stop and take-profit, and the version (ii) cut.

All three are fixed at entry from the entry fill ``e``, and from the quantity ``q = notional / e``.
With ``side`` +1 for LONG and -1 for SHORT:

- the stop is ``e x (1 - side x sl_pct / 100)``, with ``sl_pct`` the ATR percentage times
  ``sl_kali_atr`` clamped between ``sl_minimal_persen`` and ``sl_maksimal_persen``;
- the take-profit is the price at which the bot's own estimate of the net (gross minus
  ``fee_perkiraan_persen`` on the entry leg and on the exit leg) reaches ``tp_net_usdt``;
- the cut (version ii only) is ``e x (1 - side x potong_rugi_persen / 100)``.

The module takes numbers only: no bars, no I/O. No live module imports it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bot.strategy.modes.checks import LONG, check_side, require_positive
from bot.strategy.modes.config import LiveExitModeConfig

__all__ = ["LiveExitLevels", "live_exit_levels", "net_target_price", "stop_pct"]

_PERCENT = 100.0


@dataclass(frozen=True)
class LiveExitLevels:
    """The prices of one position: ``cut`` is ``None`` outside version (ii); ``sl_pct`` is the
    stop distance, in percent of the entry fill."""

    sl: float
    tp: float
    cut: Optional[float]
    sl_pct: float


def stop_pct(atr_pct: float, rules: LiveExitModeConfig) -> float:
    """The stop distance in percent: ``sl_kali_atr x atr_pct`` clamped to the configured band.

    ``atr_pct`` is ``ATR / close x 100`` of the signal candle. It must be a finite number above 0:
    a window without an ATR has no stretch, hence no signal and no levels, so a missing one is
    refused (``ValueError``) and never replaced by the lower bound."""
    require_positive("atr_pct", atr_pct)
    return min(
        max(rules.sl_minimal_persen, rules.sl_kali_atr * atr_pct), rules.sl_maksimal_persen
    )


def net_target_price(entry_fill: float, side: int, notional: float, rules: LiveExitModeConfig) -> float:
    """The exit price at which the bot's estimated net reaches ``rules.tp_net_usdt``.

    With ``r = fee_perkiraan_persen / 100``, ``N = tp_net_usdt`` and ``q = notional / entry_fill``:
    LONG ``(e(1 + r) + N/q) / (1 - r)``, SHORT ``(e(1 - r) - N/q) / (1 + r)``."""
    require_positive("entry_fill", entry_fill)
    check_side(side)
    require_positive("notional", notional)
    fee = rules.fee_perkiraan_persen / _PERCENT
    per_unit = rules.tp_net_usdt / (notional / entry_fill)
    if side == LONG:
        return (entry_fill * (1 + fee) + per_unit) / (1 - fee)
    return (entry_fill * (1 - fee) - per_unit) / (1 + fee)


def live_exit_levels(
    entry_fill: float,
    side: int,
    atr_pct: float,
    notional: float,
    rules: LiveExitModeConfig,
    with_cut: bool,
) -> LiveExitLevels:
    """The stop, take-profit and (when ``with_cut``) cut prices for an entry at ``entry_fill``."""
    pct = stop_pct(atr_pct, rules)  # checks atr_pct
    tp = net_target_price(entry_fill, side, notional, rules)  # checks the rest
    sl = entry_fill * (1 - side * pct / _PERCENT)
    cut = entry_fill * (1 - side * rules.potong_rugi_persen / _PERCENT) if with_cut else None
    return LiveExitLevels(sl=sl, tp=tp, cut=cut, sl_pct=pct)
