"""Shadow evaluation: what a hypothetical entry would have earned under the live exit rules.

A shadow row is an entry the bot did not take (a side the regime map does not allow, or a candidate the
setup ledger recorded without trading). Once it is older than the longest hold plus one minute, `walk_exit`
replays the 1m candles that followed it with the live exits, and `evaluate_pending` writes the result back:

* the reference price is the close of the decision-minute candle (the candle whose open time is the minute
  of `decided_at`), which is the open of the first walked candle, so no move between the decision and the
  walk is lost; the row's stored `ref_price` (the 3m price the entry path saw, before the AI call) is kept
  as a record only;
* the stop is a percentage of the reference price and is checked first, so a candle that touches both
  levels counts as a stop; it fills one basis point worse than its level;
* the floor is the profit level and fills exactly at its level;
* neither level touched inside the hold: a time exit at the last close of the hold, with no slippage.

Everything is gross (before fees), in basis points, signed so that a profit is positive for either side.

`evaluate_pending` runs inside the trading tick, before the entry path, so it is bounded: one wall-clock
budget for the whole call, and the first venue failure stops the row loop (a down venue is down for every
row). A row that can never be evaluated (malformed fields, non-positive levels) is closed as `invalid`
instead of pending forever, so it cannot hold one of the three slots of every tick.

The only bot import is the cancellation-safe timeout helper (`bot.asyncutil`, not the telemetry package);
the module must stay free of `pine_indicators` and `strategy.modes`. `parse_utc` is the strict store
timestamp reader the setup ledger shares.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from bot.asyncutil import with_timeout

STOP_SLIP_BPS = 1.0
MAX_ROWS_PER_TICK = 3
# The venue serves at most this many 1m candles in one request; an older row can no longer be walked.
CANDLE_WINDOW = 1000
# Wall-clock budget for one `evaluate_pending` call. A venue request can take minutes in the worst case
# (two hosts, three attempts, 20 s each), and this runs before the entry path.
EVAL_BUDGET_SECONDS = 20.0
# POSITION_HOLD_MINUTES default: the longest hold a row can carry, unless the caller passes the setting.
DEFAULT_MAX_HOLD_MINUTES = 60

REASON_STOP = "stop"
REASON_FLOOR = "floor"
REASON_TIME = "time"
REASON_EXPIRED = "expired"
REASON_INVALID = "invalid"

BPS_PER_PERCENT = 100.0
BPS_PER_UNIT = 10_000.0
MINUTE_MS = 60_000
# Newest candles asked for beyond the minutes since the decision minute. With `minutes_since + 2` the window
# reaches back to one minute before the decision minute even when the venue includes the forming candle, so it
# always holds the decision-minute candle (the reference) while that is inside the venue's window.
CANDLE_SLACK = 2

SIDE_LONG = "long"
SIDE_SHORT = "short"

# Outcome of one row: (gross bps or None for an expired or invalid row, reason). None means "not yet".
Outcome = tuple[Optional[float], str]

# Errors that come from the row's own values (a missing key, a bad number, a non-positive level) and
# cannot go away by retrying.
_PERMANENT_ROW_ERRORS = (KeyError, TypeError, ValueError, OverflowError)

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

    `candles` start with the first candle after the decision minute, and `ref_price` is the close of the
    decision-minute candle (the open of the first walked one). Only the first `hold_minutes` of them are
    walked. A stop or floor inside the candles given decides at once; reaching a time exit needs the full
    hold, so a shorter series without a touch raises `ValueError` instead of guessing a close.
    """
    _check_inputs(side, ref_price, stop_loss_pct, floor_pct, hold_minutes)
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


def _check_inputs(side: str, ref_price: float, stop_loss_pct: float, floor_pct: float, hold_minutes: int) -> None:
    """Raise `ValueError` for values no walk can use; shared so a bad row is refused before any venue call."""
    if side not in (SIDE_LONG, SIDE_SHORT):
        raise ValueError(f"side must be 'long' or 'short', got {side!r}")
    if ref_price <= 0 or stop_loss_pct <= 0 or floor_pct <= 0 or hold_minutes <= 0:
        raise ValueError("ref_price, stop_loss_pct, floor_pct and hold_minutes must be positive")


class _VenueFailure(Exception):
    """The venue could not give candles (an error, an empty answer or a malformed candle): systemic, not per-row."""


async def evaluate_pending(
    store: ShadowStore,
    client: KlineClient,
    *,
    now: datetime,
    logger: logging.Logger,
    max_hold_minutes: int = DEFAULT_MAX_HOLD_MINUTES,
) -> int:
    """Complete up to `MAX_ROWS_PER_TICK` shadow rows older than the longest hold plus one minute.

    Returns how many rows were completed. The whole call has `EVAL_BUDGET_SECONDS`; at the budget it logs
    one WARNING, cancels what is in flight and returns what was completed (the rest stays pending, and a
    cancelled write is repeated harmlessly next time). A failing pending read raises, for the caller.
    Per row: a venue failure stops the loop and leaves this row and the rest pending (one WARNING); a
    failed write leaves that row pending and the loop goes on; a row that can never be evaluated is closed
    as `invalid` (ERROR) so it cannot starve the queue.
    """
    completed: list[int] = []
    try:
        await with_timeout(
            _evaluate_rows(store, client, now, logger, max_hold_minutes, completed), EVAL_BUDGET_SECONDS
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Shadow evaluation hit its %.0f s budget after %d row(s); the rest stays pending.",
            EVAL_BUDGET_SECONDS, len(completed),
        )
    if completed:
        logger.info("Shadow evaluation completed %d row(s).", len(completed))
    return len(completed)


async def _evaluate_rows(
    store: ShadowStore,
    client: KlineClient,
    now: datetime,
    logger: logging.Logger,
    max_hold_minutes: int,
    completed: list[int],
) -> None:
    if now.tzinfo is None:  # a caller error, checked here so it is never mistaken for a bad row
        raise ValueError("now must be timezone-aware")
    cutoff = now - timedelta(minutes=max_hold_minutes + 1)
    rows = await store.pending_shadow_entries(cutoff, MAX_ROWS_PER_TICK)
    for index, row in enumerate(rows[:MAX_ROWS_PER_TICK]):
        try:
            entry_id = int(row["id"])
        except _PERMANENT_ROW_ERRORS:
            logger.error("Shadow row without a usable id cannot be closed; skipped: %r", row.get("id"))
            continue
        try:
            outcome = await _outcome(client, row, now)
        except _VenueFailure as exc:
            logger.warning(
                "Shadow evaluation stopped at entry %s: %s; %d row(s) stay pending.",
                entry_id, exc, len(rows[:MAX_ROWS_PER_TICK]) - index,
            )
            return
        except _PERMANENT_ROW_ERRORS as exc:
            logger.error(
                "Shadow entry %s is invalid and is closed unevaluated (%s: %s).",
                entry_id, type(exc).__name__, exc,
            )
            outcome = (None, REASON_INVALID)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the others or the trading loop
            logger.warning(
                "Shadow entry %s could not be evaluated (%s: %s); it stays pending.",
                entry_id, type(exc).__name__, exc,
            )
            continue
        if outcome is None:
            continue
        try:
            await store.complete_shadow_entry(entry_id, outcome[0], outcome[1])
        except Exception as exc:  # noqa: BLE001 - a failed write is retried on the next tick
            logger.warning(
                "Shadow entry %s could not be saved (%s: %s); it stays pending.",
                entry_id, type(exc).__name__, exc,
            )
            continue
        completed.append(entry_id)


async def _outcome(client: KlineClient, row: Mapping[str, Any], now: datetime) -> Optional[Outcome]:
    """The result for one row, or None while its hold has not fully played out on closed candles.

    The walk starts from the close of the decision-minute candle. A full window (at least `limit - 1`
    candles) that no longer holds that candle completes the row as `expired`; a shorter one that does not
    reach it is a venue failure. Raises `_VenueFailure` when the venue gives no usable candles, and
    `KeyError`/`TypeError`/`ValueError` when the row itself is unusable. The stored `ref_price` is only
    checked as part of the row; it never prices the walk.
    """
    decision_minute = parse_utc(row["decided_at"]).replace(second=0, microsecond=0)
    symbol = str(row["symbol"])
    side = str(row["side"])
    ref_price = float(row["ref_price"])
    stop_loss_pct = float(row["stop_loss_pct"])
    floor_pct = float(row["floor_pct"])
    hold_minutes = int(row["hold_minutes"])
    _check_inputs(side, ref_price, stop_loss_pct, floor_pct, hold_minutes)
    decision_ms = int(decision_minute.timestamp()) * 1000
    first_open_ms = decision_ms + MINUTE_MS
    minutes_since = max(0, int((now - decision_minute).total_seconds() // 60))
    limit = min(CANDLE_WINDOW, minutes_since + CANDLE_SLACK)
    try:
        candles = await client.get_klines(symbol, "1m", limit=limit)
        if not candles:
            raise RuntimeError("the venue returned no candles")
        oldest_ms = int(candles[0]["timestamp"])
        reference = next((candle for candle in candles if int(candle["timestamp"]) == decision_ms), None)
        after = [candle for candle in candles if int(candle["timestamp"]) >= first_open_ms]
    except Exception as exc:  # noqa: BLE001 - any failure to obtain usable candles is the venue's
        raise _VenueFailure(f"klines for {symbol} failed ({type(exc).__name__}: {exc})") from exc
    if oldest_ms > decision_ms:
        # A full window (the forming candle may be dropped) that no longer reaches back to the decision-minute
        # candle has lost it for good. A shorter answer did not honour the limit and proves nothing.
        if len(candles) < limit - 1:
            raise _VenueFailure(
                f"klines for {symbol} returned {len(candles)} of the {limit} candles asked for, "
                "without the decision minute"
            )
        return None, REASON_EXPIRED
    if len(after) < hold_minutes:
        return None
    if reference is None:
        return None, REASON_EXPIRED  # a hole at the decision minute: no reference price will ever come
    walk_ref = _reference_close(reference, symbol)
    try:
        return walk_exit(after, side, walk_ref, stop_loss_pct, floor_pct, hold_minutes)
    except KeyError as exc:  # a candle without high, low or close is bad venue data, not a bad row
        raise _VenueFailure(f"malformed candle for {symbol} (missing {exc})") from exc


def _reference_close(candle: Mapping[str, Any], symbol: str) -> float:
    """The decision-minute close the walk is priced from; an unusable one is bad venue data, not a bad row."""
    try:
        close = float(candle["close"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _VenueFailure(f"malformed decision-minute candle for {symbol} ({type(exc).__name__}: {exc})") from exc
    if not math.isfinite(close) or close <= 0:
        raise _VenueFailure(f"decision-minute candle for {symbol} has no usable close ({close!r})")
    return close


def parse_utc(value: Any) -> datetime:
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


# The old private name, kept so nothing that imported it breaks.
_parse_utc = parse_utc


def _offset(text: str) -> str:
    if text in ("Z", "z"):
        return "+00:00"
    if ":" in text:
        return text
    return text[:3] + ":" + (text[3:] or "00")
