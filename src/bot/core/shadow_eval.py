"""Shadow evaluation: what a hypothetical entry would have earned under the live exit rules.

A shadow row is an entry the bot did not take (a side the regime map does not allow, or a candidate the
setup ledger recorded without trading). Once it is older than the longest hold plus one minute, `walk_exit`
replays the 1m candles that followed it with the live exits, and `evaluate_pending` writes the result back:

* the stop is a percentage of the reference price and is checked first, so a candle that touches both
  levels counts as a stop; it fills one basis point worse than its level;
* the floor is the profit level and fills exactly at its level;
* neither level touched inside the hold: a time exit at the last close of the hold, with no slippage.

Everything is gross (before fees), in basis points, signed so that a profit is positive for either side.
The module is pure apart from the two awaited calls (the store and the venue client are passed in) and
imports nothing from the bot: it must stay free of `pine_indicators` and `strategy.modes`.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

STOP_SLIP_BPS = 1.0
MAX_ROWS_PER_TICK = 3
# The venue serves at most this many 1m candles in one request; an older row can no longer be walked.
CANDLE_WINDOW = 1000
# POSITION_HOLD_MINUTES default: the longest hold a row can carry, unless the caller passes the setting.
DEFAULT_MAX_HOLD_MINUTES = 60

REASON_STOP = "stop"
REASON_FLOOR = "floor"
REASON_TIME = "time"
REASON_EXPIRED = "expired"

BPS_PER_PERCENT = 100.0
BPS_PER_UNIT = 10_000.0
MINUTE_MS = 60_000
# Newest candles asked for beyond the minutes since the decision: the decision minute's own candle
# (skipped) and one spare for a venue that lags a bar.
CANDLE_SLACK = 2

SIDE_LONG = "long"
SIDE_SHORT = "short"

# Outcome of one row: (gross bps or None for an expired row, reason). None means "not yet".
Outcome = tuple[Optional[float], str]

_ISO = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[Tt ](\d{2}:\d{2}:\d{2})(?:\.(\d+))?([Zz]|[+-]\d{2}(?::?\d{2})?)$"
)


class ShadowStore(Protocol):
    async def pending_shadow_entries(self, decided_before: datetime, limit: int) -> list[dict[str, Any]]: ...

    async def complete_shadow_entry(self, entry_id: int, gross_bps: float | None, exit_reason: str) -> None: ...


class KlineClient(Protocol):
    async def get_klines(
        self, symbol: str, interval: str = "1m", limit: int = 120
    ) -> Sequence[Mapping[str, float]]: ...


def walk_exit(
    candles: Sequence[Mapping[str, float]],
    side: str,
    ref_price: float,
    stop_loss_pct: float,
    floor_pct: float,
    hold_minutes: int,
) -> tuple[float, str]:
    """Replay the live exits over 1m candles; return (gross bps, "stop" | "floor" | "time").

    `candles` start with the first candle after the decision. Only the first `hold_minutes` of them are
    walked. A stop or floor inside the candles given decides at once; reaching a time exit needs the full
    hold, so a shorter series without a touch raises `ValueError` instead of guessing a close.
    """
    if side not in (SIDE_LONG, SIDE_SHORT):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    if ref_price <= 0 or stop_loss_pct <= 0 or floor_pct <= 0 or hold_minutes <= 0:
        raise ValueError("ref_price, stop_loss_pct, floor_pct and hold_minutes must be positive")
    walked = candles[:hold_minutes]
    if not walked:
        raise ValueError("no candles to walk")
    is_long = side == SIDE_LONG
    direction = 1.0 if is_long else -1.0
    stop_level = ref_price * (1.0 - direction * stop_loss_pct / 100.0)
    floor_level = ref_price * (1.0 + direction * floor_pct / 100.0)
    for candle in walked:
        adverse = candle["low"] if is_long else candle["high"]
        favourable = candle["high"] if is_long else candle["low"]
        stop_hit = adverse <= stop_level if is_long else adverse >= stop_level
        if stop_hit:
            return -stop_loss_pct * BPS_PER_PERCENT - STOP_SLIP_BPS, REASON_STOP
        floor_hit = favourable >= floor_level if is_long else favourable <= floor_level
        if floor_hit:
            return floor_pct * BPS_PER_PERCENT, REASON_FLOOR
    if len(walked) < hold_minutes:
        raise ValueError(f"only {len(walked)} of {hold_minutes} hold candles are available")
    last_close = walked[-1]["close"]
    return direction * (last_close - ref_price) / ref_price * BPS_PER_UNIT, REASON_TIME


async def evaluate_pending(
    store: ShadowStore,
    client: KlineClient,
    *,
    now: datetime,
    logger: logging.Logger,
    max_hold_minutes: int = DEFAULT_MAX_HOLD_MINUTES,
) -> int:
    """Complete up to `MAX_ROWS_PER_TICK` shadow rows older than the longest hold plus one minute.

    Returns how many rows were completed. A row that cannot be evaluated now (venue error, failed write,
    not enough candles yet) is logged where it is an error and stays pending for the next call; it never
    stops the rows after it. A failing pending read raises, for the caller to catch.
    """
    cutoff = now - timedelta(minutes=max_hold_minutes + 1)
    rows = await store.pending_shadow_entries(cutoff, MAX_ROWS_PER_TICK)
    completed = 0
    for row in rows[:MAX_ROWS_PER_TICK]:
        try:
            outcome = await _outcome(client, row, now)
            if outcome is None:
                continue
            await store.complete_shadow_entry(int(row["id"]), outcome[0], outcome[1])
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the others or the trading loop
            logger.warning(
                "Shadow entry %s could not be evaluated (%s: %s); it stays pending.",
                row.get("id"), type(exc).__name__, exc,
            )
            continue
        completed += 1
    if completed:
        logger.info("Shadow evaluation completed %d row(s).", completed)
    return completed


async def _outcome(client: KlineClient, row: Mapping[str, Any], now: datetime) -> Optional[Outcome]:
    """The result for one row, or None while its hold has not fully played out on closed candles."""
    decision_minute = _parse_utc(row["decided_at"]).replace(second=0, microsecond=0)
    first_open_ms = int((decision_minute + timedelta(minutes=1)).timestamp()) * 1000
    hold_minutes = int(row["hold_minutes"])
    minutes_since = max(0, int((now - decision_minute).total_seconds() // 60))
    limit = min(CANDLE_WINDOW, minutes_since + CANDLE_SLACK)
    candles = await client.get_klines(str(row["symbol"]), "1m", limit=limit)
    if not candles:
        raise RuntimeError("the venue returned no candles")
    if int(candles[0]["timestamp"]) > first_open_ms:
        return None, REASON_EXPIRED  # the window no longer reaches back to the decision
    after = [candle for candle in candles if int(candle["timestamp"]) >= first_open_ms]
    if len(after) < hold_minutes:
        return None
    return walk_exit(
        after,
        str(row["side"]),
        float(row["ref_price"]),
        float(row["stop_loss_pct"]),
        float(row["floor_pct"]),
        hold_minutes,
    )


def _parse_utc(value: Any) -> datetime:
    """A timezone-aware UTC datetime from a store timestamp, on Python 3.10 as on 3.11.

    `datetime.fromisoformat` takes only 3 or 6 fraction digits and no `Z` before 3.11, while Postgres trims
    trailing zeros; the text is normalised first. A timestamp without an offset is refused, not read as UTC.
    """
    if isinstance(value, datetime):
        moment = value
    else:
        match = _ISO.match(str(value).strip())
        if match is None:
            raise ValueError(f"not an ISO-8601 timestamp with an offset: {value!r}")
        day, clock, fraction, offset = match.groups()
        fraction = (fraction or "0")[:6].ljust(6, "0")
        moment = datetime.fromisoformat(f"{day}T{clock}.{fraction}{_offset(offset)}")
    if moment.tzinfo is None:
        raise ValueError(f"timestamp has no timezone: {value!r}")
    return moment.astimezone(timezone.utc)


def _offset(text: str) -> str:
    if text in ("Z", "z"):
        return "+00:00"
    if ":" in text:
        return text
    return text[:3] + ":" + (text[3:] or "00")
