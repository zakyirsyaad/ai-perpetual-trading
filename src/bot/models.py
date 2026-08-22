from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class PositionDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(slots=True)
class TradePlan:
    symbol: str
    direction: PositionDirection
    usdt_size: float
    leverage: int
    stop_loss_pct: float
    take_profit_pct: float
    decision_reason: str = "rule"
    decision_payload: dict[str, Any] | None = None
    confidence: float = 0.0
    setup_score: float = 0.0
    max_hold_minutes: int | None = None
    factor_metadata: dict[str, Any] | None = None
    trigger_metadata: dict[str, Any] | None = None


@dataclass(slots=True)
class OpenPosition:
    symbol: str
    direction: PositionDirection
    entry_price: float
    quantity: float
    exchange_size: float
    exchange_order_id: str
    opened_at: datetime
    entry_reason: str = "unknown"
    # Venue position identifier, captured at entry so a restart can verify it is
    # the same position rather than merely the same symbol.
    position_id: str | None = None


@dataclass(slots=True)
class ClosedTrade:
    symbol: str
    direction: PositionDirection
    entry_price: float
    exit_price: float
    quantity: float
    pnl_usdt: float
    reason: str
    entry_reason: str
    opened_at: datetime
    closed_at: datetime
    exchange_entry_order_id: str
    exchange_exit_order_id: str
    confidence: float = 0.0
    factor_metadata: dict[str, Any] | None = None
    trigger_metadata: dict[str, Any] | None = None
