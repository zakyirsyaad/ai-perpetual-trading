from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from bot.models import ClosedTrade, OpenPosition, PositionDirection


class TradeStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS closed_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    pnl_usdt REAL NOT NULL,
                    reason TEXT NOT NULL,
                    entry_reason TEXT NOT NULL DEFAULT '',
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    exchange_entry_order_id TEXT NOT NULL,
                    exchange_exit_order_id TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS open_positions (
                    symbol TEXT PRIMARY KEY,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    quantity REAL NOT NULL,
                    exchange_size REAL NOT NULL DEFAULT 0,
                    exchange_order_id TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    entry_reason TEXT NOT NULL DEFAULT ''
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    systemic_risk INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS learning_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    note_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            await self._ensure_column(db, "closed_trades", "entry_reason", "TEXT NOT NULL DEFAULT ''")
            await self._ensure_column(db, "open_positions", "entry_reason", "TEXT NOT NULL DEFAULT ''")
<<<<<<< HEAD
            await self._ensure_column(db, "open_positions", "exchange_size", "REAL NOT NULL DEFAULT 0")
=======
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_closed_trades_closed_at
                ON closed_trades(closed_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_closed_trades_reason_closed_at
                ON closed_trades(reason, closed_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_closed_trades_entry_reason
                ON closed_trades(entry_reason)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_open_positions_opened_at
                ON open_positions(opened_at)
                """
            )
>>>>>>> c78be0d66ab75cb6b8447245488aab66339bae13
            await db.commit()

    async def _ensure_column(
        self,
        db: aiosqlite.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        columns = {str(r[1]) for r in rows}
        if column in columns:
            return
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def save_open_position(self, position: OpenPosition) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO open_positions
                (symbol, direction, entry_price, quantity, exchange_size, exchange_order_id, opened_at, entry_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position.symbol,
                    position.direction.value,
                    position.entry_price,
                    position.quantity,
                    position.exchange_size,
                    position.exchange_order_id,
                    position.opened_at.isoformat(),
                    position.entry_reason,
                ),
            )
            await db.commit()

    async def remove_open_position(self, symbol: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("DELETE FROM open_positions WHERE symbol = ?", (symbol,))
            await db.commit()

    async def save_closed_trade(self, trade: ClosedTrade) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO closed_trades
                (symbol, direction, entry_price, exit_price, quantity, pnl_usdt, reason,
                 entry_reason, opened_at, closed_at, exchange_entry_order_id, exchange_exit_order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.symbol,
                    trade.direction.value,
                    trade.entry_price,
                    trade.exit_price,
                    trade.quantity,
                    trade.pnl_usdt,
                    trade.reason,
                    trade.entry_reason,
                    trade.opened_at.isoformat(),
                    trade.closed_at.isoformat(),
                    trade.exchange_entry_order_id,
                    trade.exchange_exit_order_id,
                ),
            )
            await db.commit()

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
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO ai_decisions
                (created_at, symbol, action, confidence, systemic_risk, reason, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    symbol,
                    action,
                    confidence,
                    systemic_risk,
                    reason,
                    json.dumps(payload, ensure_ascii=True),
                ),
            )
            await db.commit()

    async def save_learning_note(
        self,
        *,
        note_type: str,
        symbol: str,
        payload: dict[str, Any],
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO learning_notes
                (created_at, note_type, symbol, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    note_type,
                    symbol,
                    json.dumps(payload, ensure_ascii=True),
                ),
            )
            await db.commit()

    async def recent_performance_summary(self, limit: int = 50) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "total_trades": 0,
            "total_pnl_usdt": 0.0,
            "by_entry_reason_sample": [],
        }
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                SELECT entry_reason, pnl_usdt
                FROM closed_trades
                ORDER BY closed_at DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()

        if not rows:
            return summary

        reason_stats: dict[str, dict[str, Any]] = {}
        total_pnl = 0.0
        for reason, pnl in rows:
            key = str(reason or "unknown")
            value = float(pnl or 0.0)
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

    async def count_closed_trades_for_day(self, day: date | None = None) -> int:
        target_day = day or datetime.now(timezone.utc).date()
        start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        end = datetime.combine(target_day, datetime.max.time(), tzinfo=timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM closed_trades WHERE closed_at BETWEEN ? AND ?", (start, end)
            )
            row = await cursor.fetchone()
            return int(row[0] if row else 0)

    async def count_stop_losses_for_day(self, day: date | None = None) -> int:
        target_day = day or datetime.now(timezone.utc).date()
        start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        end = datetime.combine(target_day, datetime.max.time(), tzinfo=timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*)
                FROM closed_trades
                WHERE closed_at BETWEEN ? AND ? AND reason = 'stop_loss'
                """,
                (start, end),
            )
            row = await cursor.fetchone()
            return int(row[0] if row else 0)

    async def total_volume_for_day(self, day: date | None = None) -> float:
        target_day = day or datetime.now(timezone.utc).date()
        start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        end = datetime.combine(target_day, datetime.max.time(), tzinfo=timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                SELECT COALESCE(SUM((entry_price * quantity) + (exit_price * quantity)), 0)
                FROM closed_trades
                WHERE closed_at BETWEEN ? AND ?
                """,
                (start, end),
            )
            row = await cursor.fetchone()
            return float(row[0] if row else 0.0)

    async def total_pnl_for_day(self, day: date | None = None) -> float:
        target_day = day or datetime.now(timezone.utc).date()
        start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        end = datetime.combine(target_day, datetime.max.time(), tzinfo=timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                SELECT COALESCE(SUM(pnl_usdt), 0)
                FROM closed_trades
                WHERE closed_at BETWEEN ? AND ?
                """,
                (start, end),
            )
            row = await cursor.fetchone()
            return float(row[0] if row else 0.0)

    async def consecutive_losses_for_day(self, day: date | None = None) -> int:
        target_day = day or datetime.now(timezone.utc).date()
        start = datetime.combine(target_day, datetime.min.time(), tzinfo=timezone.utc).isoformat()
        end = datetime.combine(target_day, datetime.max.time(), tzinfo=timezone.utc).isoformat()

        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                SELECT pnl_usdt
                FROM closed_trades
                WHERE closed_at BETWEEN ? AND ?
                ORDER BY closed_at DESC
                """,
                (start, end),
            )
            rows = await cursor.fetchall()

        streak = 0
        for row in rows:
            pnl = float(row[0])
            if pnl < 0:
                streak += 1
            else:
                break
        return streak

    async def list_open_positions(self) -> list[OpenPosition]:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                """
                SELECT symbol, direction, entry_price, quantity, exchange_size, exchange_order_id, opened_at, entry_reason
                FROM open_positions
                """
            )
            rows = await cursor.fetchall()

        result: list[OpenPosition] = []
        for row in rows:
            result.append(
                OpenPosition(
                    symbol=str(row[0]),
                    direction=PositionDirection(str(row[1])),
                    entry_price=float(row[2]),
                    quantity=float(row[3]),
                    exchange_size=float(row[4] or row[3]),
                    exchange_order_id=str(row[5]),
                    opened_at=datetime.fromisoformat(str(row[6])),
                    entry_reason=str(row[7]) if len(row) > 7 else "",
                )
            )
        return result

