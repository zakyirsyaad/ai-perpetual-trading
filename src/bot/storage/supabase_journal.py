from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

import httpx

from bot.config import Settings
from bot.models import ClosedTrade


class SupabaseJournal:
    """Optional Supabase REST mirror for executed trades and AI decisions."""

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._url = (settings.supabase_url or "").rstrip("/")
        self._key = settings.supabase_service_role_key
        self._schema = settings.supabase_schema
        self._trades_table = settings.supabase_trades_table
        self._ai_decisions_table = settings.supabase_ai_decisions_table
        self._logger = logger
        self._client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)

    @property
    def enabled(self) -> bool:
        return bool(self._url and self._key)

    async def close(self) -> None:
        await self._client.aclose()

    async def record_closed_trade(self, trade: ClosedTrade) -> None:
        if not self.enabled:
            return
        payload = asdict(trade)
        payload["direction"] = trade.direction.value
        payload["opened_at"] = trade.opened_at.isoformat()
        payload["closed_at"] = trade.closed_at.isoformat()
        await self._insert(self._trades_table, payload)

    async def record_ai_decision(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        await self._insert(self._ai_decisions_table, payload)

    async def _insert(self, table: str, payload: dict[str, Any]) -> None:
        try:
            response = await self._client.post(
                f"{self._url}/rest/v1/{table}",
                json=payload,
                headers={
                    "apikey": str(self._key),
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                    "Accept-Profile": self._schema,
                    "Content-Profile": self._schema,
                },
            )
            response.raise_for_status()
        except Exception as exc:
            self._logger.warning("Supabase mirror insert failed for table %s: %s", table, exc)
