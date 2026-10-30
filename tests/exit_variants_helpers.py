"""Shared fixtures of the exit-variants tests: closed-trade and decision rows as the store returns them, and 1m bars.

Tests import it as ``from tests.exit_variants_helpers import ...`` and run from the repository root. Everything is
synthetic: bars are described in net space (the bot's own net estimate after both taker fees) and priced with the
walker's ``price_for_net``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from research.exit_variants.study import LiveRules, Position
from research.exit_variants.walker import SHORT, Cols, price_for_net

UTC = timezone.utc
STEP = 60_000
ENTRY, QTY, FEE_PCT = 100_000.0, 0.25, 0.05
NOTIONAL = ENTRY * QTY
OPENED = datetime(2026, 10, 26, 0, 15, 12, 974567, tzinfo=UTC)
ENTRY_BAR_MS = int(datetime(2026, 10, 26, 0, 16, tzinfo=UTC).timestamp()) * 1000  # the first full bar after OPENED
RULES = LiveRules(
    fee_pct=FEE_PCT,
    floor_net=56.0,
    hold_minutes=60,
    stop_loss_pct=0.35,
    atr_sl_multiplier=1.5,
    max_stop_loss_pct=0.40,
    take_profit_pct=0.70,
    atr_tp_multiplier=3.0,
    min_fee_adjusted_rr=1.35,
    venue_fee_rates=False,
)


def trade_row(**overrides: Any) -> dict[str, Any]:
    """A closed SHORT as the store returns it: opened at ``OPENED``, a time stop 60 minutes later."""
    row: dict[str, Any] = {
        "symbol": "BTC/USDT",
        "direction": "SHORT",
        "entry_price": ENTRY,
        "exit_price": 99_950.0,
        "quantity": QTY,
        "pnl_usdt": 1.0,
        "reason": "time_stop",
        "confidence": 0.78,
        "opened_at": OPENED.isoformat(),
        "closed_at": (OPENED + timedelta(minutes=60)).isoformat(),
        "factor_metadata": {"setup": {"atr_pct": 0.12}},
    }
    row.update(overrides)
    return row


def decision_row(created: Optional[datetime] = None, **overrides: Any) -> dict[str, Any]:
    """The OPEN decision that opened ``trade_row``: four seconds before it, the model's stop as PostgREST gives it."""
    row: dict[str, Any] = {
        "created_at": (created or OPENED - timedelta(seconds=4)).isoformat(),
        "symbol": "BTC/USDT",
        "action": "OPEN_SHORT",
        "confidence": 0.78,
        "stop_loss_pct": "0.30",  # a ->> extraction comes back as text
        "take_profit_pct": "0.80",  # far above the floor's 0.32 %, so by default the floor is the live target
    }
    row.update(overrides)
    return row


def cols_for(side: int, path: list[tuple[float, float, float]], start_ms: int = ENTRY_BAR_MS) -> Cols:
    """1m bars from the entry bar on, each given in net space as (open, best, worst)."""
    opens, highs, lows = [], [], []
    for open_net, best_net, worst_net in path:
        o, b, w = (price_for_net(ENTRY, side, NOTIONAL, FEE_PCT, net) for net in (open_net, best_net, worst_net))
        opens.append(o)
        highs.append(max(o, b, w))
        lows.append(min(o, b, w))
    return Cols([start_ms + i * STEP for i in range(len(path))], opens, highs, lows)


FLAT = (0.0, 0.5, -0.5)
LOCK_CATCHES = [FLAT, (0.0, 47.0, -2.0), (30.0, 31.0, 18.0)] + [(5.0, 5.5, 4.5)] * 60  # peaks at +47, ends at +5
NOTHING_HAPPENS = [FLAT] + [(2.0, 3.0, 1.0)] * 61


def position(**overrides: Any) -> Position:
    fields: dict[str, Any] = dict(
        opened_at=OPENED,
        closed_at=OPENED + timedelta(minutes=60),
        side=SHORT,
        entry_fill=ENTRY,
        notional=NOTIONAL,
        sl_pct=0.30,
        tp_pct=0.80,
        atr_pct=0.12,
        actual_exit="WAKTU",
        volume_usdt=50_000.0,
    )
    fields.update(overrides)
    return Position(**fields)
