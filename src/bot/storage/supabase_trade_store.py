from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any

import httpx

from bot.config import Settings
from bot.models import ClosedTrade, OpenPosition


class SupabaseTradeStore:
    """Primary trade store backed by Supabase REST.

    This intentionally does not keep local file state. Open positions are
    reconciled from OKX on startup by the trade engine.
    """

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

    async def initialize(self) -> None:
        if not self.enabled:
            raise RuntimeError(
                "Supabase trade store is selected but SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is empty."
            )
        await self._get_rows(self._trades_table, [("select", "id"), ("limit", "1")])

    async def close(self) -> None:
        await self._client.aclose()

    async def save_open_position(self, position: OpenPosition) -> None:
        self._logger.debug(
            "Not persisting open position locally; OKX is the source of truth for %s.",
            position.symbol,
        )

    async def remove_open_position(self, symbol: str) -> None:
        self._logger.debug("No local open position state to remove for %s.", symbol)

    async def list_open_positions(self) -> list[OpenPosition]:
        return []

    async def save_closed_trade(self, trade: ClosedTrade) -> None:
        payload = asdict(trade)
        payload["direction"] = trade.direction.value
        payload["opened_at"] = trade.opened_at.isoformat()
        payload["closed_at"] = trade.closed_at.isoformat()
        payload["factor_metadata"] = trade.factor_metadata or {}
        payload["trigger_metadata"] = trade.trigger_metadata or {}
        await self._insert(self._trades_table, payload)

    async def save_ai_decision(
        self,
        *,
        symbol: str,
        action: str,
        confidence: float,
        systemic_risk: int,
        reason: str,
        payload: dict[str, Any],
    ) -> None:
        await self._insert(
            self._ai_decisions_table,
            {
                "symbol": symbol,
                "action": action,
                "confidence": confidence,
                "systemic_risk": systemic_risk,
                "reason": reason,
                "payload_json": payload,
            },
        )

    async def save_learning_note(
        self,
        *,
        note_type: str,
        symbol: str,
        payload: dict[str, Any],
    ) -> None:
        self._logger.debug(
            "Skipping learning note persistence because Supabase schema has no learning_notes table: %s %s.",
            note_type,
            symbol,
        )

    async def recent_performance_summary(self, limit: int = 50) -> dict[str, Any]:
        rows = await self._get_rows(
            self._trades_table,
            [
                ("select", "entry_reason,pnl_usdt,closed_at"),
                ("order", "closed_at.desc"),
                ("limit", str(limit)),
            ],
        )
        return self._summarize_rows(rows)

    async def count_closed_trades_for_day(self, day: date | None = None) -> int:
        rows = await self._closed_trade_rows_for_day(day, "id")
        return len(rows)

    async def count_stop_losses_for_day(self, day: date | None = None) -> int:
        rows = await self._closed_trade_rows_for_day(day, "reason")
        return sum(1 for row in rows if row.get("reason") == "stop_loss")

    async def total_volume_for_day(self, day: date | None = None) -> float:
        rows = await self._closed_trade_rows_for_day(day, "entry_price,exit_price,quantity")
        return self._sum_trade_volume(rows)

    async def total_volume_all_time(self) -> float:
        rows = await self._all_closed_trade_rows("entry_price,exit_price,quantity")
        return self._sum_trade_volume(rows)

    @staticmethod
    def _sum_trade_volume(rows: list[dict[str, Any]]) -> float:
        return sum(
            (float(row.get("entry_price") or 0.0) * float(row.get("quantity") or 0.0))
            + (float(row.get("exit_price") or 0.0) * float(row.get("quantity") or 0.0))
            for row in rows
        )

    async def total_pnl_for_day(self, day: date | None = None) -> float:
        rows = await self._closed_trade_rows_for_day(day, "pnl_usdt")
        return sum(float(row.get("pnl_usdt") or 0.0) for row in rows)

    async def consecutive_losses_for_day(self, day: date | None = None) -> int:
        rows = await self._closed_trade_rows_for_day(day, "pnl_usdt,closed_at")
        streak = 0
        for row in rows:
            if float(row.get("pnl_usdt") or 0.0) < 0:
                streak += 1
                continue
            break
        return streak

    async def recent_closed_trade_records(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._get_rows(
            self._trades_table,
            [
                (
                    "select",
                    "symbol,direction,entry_price,exit_price,quantity,pnl_usdt,reason,entry_reason,"
                    "opened_at,closed_at,exchange_entry_order_id,exchange_exit_order_id,confidence,"
                    "factor_metadata,trigger_metadata",
                ),
                ("order", "closed_at.desc"),
                ("limit", str(limit)),
            ],
        )

    async def _closed_trade_rows_for_day(self, day: date | None, select: str) -> list[dict[str, Any]]:
        target_day = day or datetime.now(timezone.utc).date()
        start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        end = datetime.combine(target_day, datetime.max.time(), tzinfo=timezone.utc).isoformat()
        return await self._get_rows(
            self._trades_table,
            [
                ("select", select),
                ("closed_at", f"gte.{start}"),
                ("closed_at", f"lte.{end}"),
                ("order", "closed_at.desc"),
                ("limit", "1000"),
            ],
        )

    async def _all_closed_trade_rows(self, select: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_size = 1000
        offset = 0
        while True:
            page = await self._get_rows(
                self._trades_table,
                [
                    ("select", select),
                    ("order", "closed_at.asc"),
                    ("limit", str(page_size)),
                    ("offset", str(offset)),
                ],
            )
            rows.extend(page)
            if len(page) < page_size:
                return rows
            offset += page_size

    async def _insert(self, table: str, payload: dict[str, Any]) -> None:
        response = await self._client.post(
            f"{self._url}/rest/v1/{table}",
            json=payload,
            headers={
                **self._headers(),
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
                "Content-Profile": self._schema,
            },
        )
        self._raise_for_status(response, table)

    async def _get_rows(self, table: str, params: list[tuple[str, str]]) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self._url}/rest/v1/{table}",
            params=params,
            headers=self._headers(),
        )
        self._raise_for_status(response, table)
        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected Supabase response for table {table}: {type(data).__name__}")
        return [row for row in data if isinstance(row, dict)]

    def _headers(self) -> dict[str, str]:
        return {
            "apikey": str(self._key),
            "Authorization": f"Bearer {self._key}",
            "Accept-Profile": self._schema,
        }

    @staticmethod
    def _raise_for_status(response: httpx.Response, table: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500] if exc.response is not None else ""
            raise RuntimeError(f"Supabase request failed for table {table}: {exc} body={body}") from exc

    @staticmethod
    def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "total_trades": 0,
            "total_pnl_usdt": 0.0,
            "by_entry_reason_sample": [],
        }
        if not rows:
            return summary

        reason_stats: dict[str, dict[str, Any]] = {}
        total_pnl = 0.0
        for row in rows:
            key = str(row.get("entry_reason") or "unknown")
            value = float(row.get("pnl_usdt") or 0.0)
            total_pnl += value
            stats = reason_stats.setdefault(
                key,
                {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0},
            )
            stats["trades"] += 1
            stats["total_pnl"] += value
            if value > 0:
                stats["wins"] += 1
            elif value < 0:
                stats["losses"] += 1

        summary["total_trades"] = len(rows)
        summary["total_pnl_usdt"] = total_pnl
        for reason, stats in reason_stats.items():
            trades = int(stats["trades"])
            wins = int(stats["wins"])
            summary["by_entry_reason_sample"].append(
                {
                    "entry_reason": reason,
                    "trades": trades,
                    "wins": wins,
                    "losses": int(stats["losses"]),
                    "win_rate_pct": (wins / trades * 100.0) if trades else 0.0,
                    "avg_pnl_usdt": float(stats["total_pnl"]) / trades if trades else 0.0,
                }
            )
        return summary
