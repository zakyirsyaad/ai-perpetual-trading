from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol

from bot.ai.usage import DailyAiUsage
from bot.models import ClosedTrade, OpenPosition


class TradeStoreProtocol(Protocol):
    async def initialize(self) -> None: ...

    async def save_open_position(self, position: OpenPosition) -> None: ...

    async def remove_open_position(self, symbol: str) -> None: ...

    async def list_open_positions(self) -> list[OpenPosition]: ...

    async def save_closed_trade(self, trade: ClosedTrade) -> None: ...

    async def save_ai_decision(
        self,
        *,
        symbol: str,
        action: str,
        confidence: float,
        systemic_risk: int,
        reason: str,
        payload: dict[str, Any],
        ai_call_id: str | None = None,
    ) -> None: ...

    async def save_ai_call_metrics(self, metrics: Any) -> None: ...

    async def save_exit_verdict(
        self,
        *,
        symbol: str,
        direction: str,
        close_now: bool,
        reason: str,
        confidence: float | None,
        honored: bool,
        skip_reason: str | None,
        elapsed_seconds: float,
        entry_price: float,
        current_price: float,
        gross_usdt: float,
        fee_usdt: float,
        net_after_fee_usdt: float,
    ) -> None: ...

    async def save_learning_note(
        self,
        *,
        note_type: str,
        symbol: str,
        payload: dict[str, Any],
    ) -> None: ...

    async def recent_performance_summary(self, limit: int = 50) -> dict[str, Any]: ...

    async def count_closed_trades_for_day(self, day: date | None = None) -> int: ...

    async def count_stop_losses_for_day(self, day: date | None = None) -> int: ...

    async def total_volume_for_day(self, day: date | None = None) -> float: ...

    async def total_volume_all_time(self) -> float: ...

    async def daily_volume_series(self) -> list[tuple[str, float]]: ...

    async def daily_ai_usage_series(self) -> list[DailyAiUsage]: ...

    async def recent_closed_trades(self, limit: int = 100) -> list[ClosedTrade]: ...

    async def realized_pnl_since_cycle(self) -> float: ...

    async def total_pnl_for_day(self, day: date | None = None) -> float: ...

    async def consecutive_losses_for_day(self, day: date | None = None) -> int: ...

    async def recent_closed_trade_records(self, limit: int = 100) -> list[dict[str, Any]]: ...

    async def save_shadow_entry(self, row: dict[str, Any]) -> None: ...

    async def pending_shadow_entries(self, decided_before: datetime, limit: int) -> list[dict[str, Any]]: ...

    async def latest_shadow_decided_at(
        self, symbol: str, side: str, source: str, bucket_key: str
    ) -> datetime | None: ...

    async def complete_shadow_entry(self, entry_id: int, gross_bps: float | None, exit_reason: str) -> None: ...

    async def shadow_outcomes_since(self, since: datetime, source: str | None = None) -> list[dict[str, Any]]: ...

    async def closed_trade_rows_since(self, since: datetime) -> list[dict[str, Any]]: ...
