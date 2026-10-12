from __future__ import annotations

import logging
import re
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from bot.ai.usage import AICallMetrics
from bot.config import Settings
from bot.models import ClosedTrade, OpenPosition, PositionDirection


_FRACTION = re.compile(r"\.(\d+)")


def _normalise_iso(value: Any) -> str:
    """Make a Supabase timestamp parseable on Python 3.10, which wants fractions of 3 or 6 digits.

    Supabase writes 5 or 6 digits. Only the fraction is rewritten (padded or truncated to 6); the
    UTC offset after it is left alone, and a `Z` suffix becomes `+00:00`.
    """
    text = str(value or "").strip().replace("Z", "+00:00")
    return _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)


def _parse_timestamp(value: Any) -> datetime | None:
    text = _normalise_iso(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class SupabaseTradeStore:
    """Primary trade store backed by Supabase REST.

    This intentionally does not keep local file state. Open positions are
    reconciled from the exchange on startup by the trade engine.
    """

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._url = (settings.supabase_url or "").rstrip("/")
        self._key = settings.supabase_service_role_key
        self._schema = settings.supabase_schema
        self._trades_table = settings.supabase_trades_table
        self._ai_decisions_table = settings.supabase_ai_decisions_table
        self._ai_exit_verdicts_table = settings.supabase_ai_exit_verdicts_table
        self._ai_call_metrics_table = settings.supabase_ai_call_metrics_table
        self._open_positions_table = settings.supabase_open_positions_table
        self._exchange = settings.exchange_name
        self._volume_cycle_started_at = getattr(settings, "volume_cycle_started_at", None)
        self._logger = logger
        self._client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        self._trading_tz = ZoneInfo(settings.trading_timezone)

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
        """Persist open-position lifecycle state so a restart can resume it.

        The exchange remains the authority on size and price; this record exists
        so a restart knows which position the bot itself opened, with which
        SL/TP intent and order ids, instead of treating it as orphan exposure.
        """
        try:
            await self._upsert(
                self._open_positions_table,
                {
                    "exchange": self._exchange,
                    "symbol": position.symbol,
                    "status": "open",
                    "direction": position.direction.value,
                    "entry_price": position.entry_price,
                    "quantity": position.quantity,
                    "exchange_size": position.exchange_size,
                    "exchange_order_id": position.exchange_order_id,
                    "position_id": position.position_id,
                    "stop_loss_pct": position.stop_loss_pct,
                    "take_profit_pct": position.take_profit_pct,
                    "entry_reason": position.entry_reason,
                    "opened_at": position.opened_at.isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="exchange,symbol",
            )
        except Exception as exc:
            # Position-state persistence must never abort an in-flight trade.
            self._logger.warning("Could not persist open position state for %s: %s", position.symbol, exc)

    async def remove_open_position(self, symbol: str) -> None:
        try:
            await self._delete(
                self._open_positions_table,
                [("exchange", f"eq.{self._exchange}"), ("symbol", f"eq.{symbol}")],
            )
        except Exception as exc:
            self._logger.warning("Could not clear open position state for %s: %s", symbol, exc)

    async def list_open_positions(self) -> list[OpenPosition]:
        try:
            rows = await self._get_rows(
                self._open_positions_table,
                [
                    ("select", "*"),
                    ("exchange", f"eq.{self._exchange}"),
                    ("status", "eq.open"),
                    ("limit", "50"),
                ],
            )
        except Exception as exc:
            self._logger.warning("Could not read persisted open positions: %s", exc)
            return []
        positions: list[OpenPosition] = []
        for row in rows:
            try:
                positions.append(
                    OpenPosition(
                        symbol=str(row["symbol"]),
                        direction=PositionDirection(str(row["direction"]).upper()),
                        entry_price=float(row.get("entry_price") or 0.0),
                        quantity=float(row.get("quantity") or 0.0),
                        exchange_size=float(row.get("exchange_size") or 0.0),
                        exchange_order_id=str(row.get("exchange_order_id") or ""),
                        opened_at=datetime.fromisoformat(str(row["opened_at"]).replace("Z", "+00:00")),
                        entry_reason=str(row.get("entry_reason") or "unknown"),
                        position_id=(
                            str(row["position_id"]) if row.get("position_id") not in (None, "") else None
                        ),
                        stop_loss_pct=self._optional_float(row.get("stop_loss_pct")),
                        take_profit_pct=self._optional_float(row.get("take_profit_pct")),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                self._logger.warning("Skipping unreadable persisted open position row: %s", exc)
        return positions

    async def save_closed_trade(self, trade: ClosedTrade) -> None:
        payload = asdict(trade)
        payload["direction"] = trade.direction.value
        payload["opened_at"] = trade.opened_at.isoformat()
        payload["closed_at"] = trade.closed_at.isoformat()
        payload["factor_metadata"] = trade.factor_metadata or {}
        payload["trigger_metadata"] = trade.trigger_metadata or {}
        payload["exchange"] = self._exchange
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
        ai_call_id: str | None = None,
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
                "exchange": self._exchange,
                "ai_call_id": ai_call_id,
            },
        )

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
    ) -> None:
        """Persist one model exit verdict and whether the engine acted on it."""
        await self._insert(
            self._ai_exit_verdicts_table,
            {
                "symbol": symbol,
                "direction": direction,
                "close_now": close_now,
                "reason": reason,
                "confidence": confidence,
                "honored": honored,
                "skip_reason": skip_reason,
                "elapsed_seconds": elapsed_seconds,
                "entry_price": entry_price,
                "current_price": current_price,
                "gross_usdt": gross_usdt,
                "fee_usdt": fee_usdt,
                "net_after_fee_usdt": net_after_fee_usdt,
                "exchange": self._exchange,
            },
        )

    async def save_ai_call_metrics(self, metrics: AICallMetrics) -> None:
        """Persist one telemetry record per provider request.

        Observability only: a storage failure is logged and swallowed so token
        accounting can never stop trading.
        """
        record = metrics.to_record()
        record["exchange"] = record.get("exchange") or self._exchange
        try:
            await self._insert(self._ai_call_metrics_table, record)
        except Exception as exc:
            self._logger.warning("Could not persist AI call metrics (%s): %s", metrics.call_id, exc)

    async def recent_ai_call_metrics(self, *, limit: int = 1000, since: str | None = None) -> list[dict[str, Any]]:
        params: list[tuple[str, str]] = [
            ("select", "*"),
            ("order", "created_at.desc"),
            ("limit", str(limit)),
        ]
        if since:
            params.append(("created_at", f"gte.{since}"))
        return await self._get_rows(self._ai_call_metrics_table, params)

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
                ("exchange", f"eq.{self._exchange}"),
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
        rows = await self._closed_trade_rows_for_day(
            day,
            "entry_price,exit_price,quantity",
            apply_cycle_start=True,
        )
        return self._sum_trade_volume(rows)

    async def daily_volume_series(self) -> list[tuple[str, float]]:
        """Traded volume per trading-timezone day since the cycle start, oldest first (dashboard only)."""
        rows = await self._all_closed_trade_rows("closed_at,entry_price,exit_price,quantity")
        by_day: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            closed = _parse_timestamp(row.get("closed_at"))
            if closed is None:
                continue
            by_day.setdefault(closed.astimezone(self._trading_tz).date().isoformat(), []).append(row)
        return [(day, self._sum_trade_volume(group)) for day, group in sorted(by_day.items())]

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
                ("exchange", f"eq.{self._exchange}"),
                ("order", "closed_at.desc"),
                ("limit", str(limit)),
            ],
        )

    async def _closed_trade_rows_for_day(
        self,
        day: date | None,
        select: str,
        *,
        apply_cycle_start: bool = False,
    ) -> list[dict[str, Any]]:
        target_day = day or datetime.now(self._trading_tz).date()
        start_local = datetime.combine(target_day, datetime.min.time(), tzinfo=self._trading_tz)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc)
        if apply_cycle_start and self._volume_cycle_started_at is not None:
            start_utc = max(start_utc, self._volume_cycle_started_at.astimezone(timezone.utc))
        start = start_utc.isoformat()
        end = end_local.astimezone(timezone.utc).isoformat()
        return await self._get_rows(
            self._trades_table,
            [
                ("select", select),
                ("exchange", f"eq.{self._exchange}"),
                ("closed_at", f"gte.{start}"),
                ("closed_at", f"lt.{end}"),
                ("order", "closed_at.desc"),
                ("limit", "1000"),
            ],
        )

    async def _all_closed_trade_rows(self, select: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_size = 1000
        offset = 0
        while True:
            params = [
                ("select", select),
                ("exchange", f"eq.{self._exchange}"),
                ("order", "closed_at.asc"),
                ("limit", str(page_size)),
                ("offset", str(offset)),
            ]
            if self._volume_cycle_started_at is not None:
                cycle_start = self._volume_cycle_started_at.astimezone(timezone.utc).isoformat()
                params.insert(2, ("closed_at", f"gte.{cycle_start}"))
            page = await self._get_rows(
                self._trades_table,
                params,
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

    async def _upsert(self, table: str, payload: dict[str, Any], *, on_conflict: str) -> None:
        response = await self._client.post(
            f"{self._url}/rest/v1/{table}",
            params=[("on_conflict", on_conflict)],
            json=payload,
            headers={
                **self._headers(),
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
                "Content-Profile": self._schema,
            },
        )
        self._raise_for_status(response, table)

    async def _delete(self, table: str, params: list[tuple[str, str]]) -> None:
        response = await self._client.request(
            "DELETE",
            f"{self._url}/rest/v1/{table}",
            params=params,
            headers={**self._headers(), "Prefer": "return=minimal", "Content-Profile": self._schema},
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
    def _optional_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

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
