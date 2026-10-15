"""Live calibration readers: AI entry latency and stop overshoot (spec 5.10, 5.2).

Research code; the bot never imports it. It reads the live bot's Supabase
records READ-ONLY through an injected ``get_rows`` coroutine (one PostgREST
page per call) and pages every query itself, at most ``MAX_PAGES`` pages.
``make_get_rows`` yields a reader that only GETs the three tables in
``READ_TABLES`` and closes the store's HTTP client afterwards. The two outputs calibrate the
simulator: the entry-call latency distribution and ``s*``, how far past the
stop level live stop-loss exits actually filled (the bot polls every 5 s and
closes with a market order).

Entry-decision payload shape (src/bot/ai/orchestration.py ``_record_ai_decision``
and ``CompactDecisionContextBuilder.build_entry_payload``)::

    payload_json.decision.stop_loss_pct
    payload_json.request.market_context.technicals  # [{symbol, last_price, atr_14, ...}]

Only those paths are selected (``json_select``; PostgREST ``alias:column->key``,
null when a path is absent) and ``payload_from_row`` rebuilds the payload.
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import statistics
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .spec import FillConfig
from .timeparse import parse_iso_datetime

GetRows = Callable[[str, "list[tuple[str, str]]"], Awaitable["list[dict]"]]

PAGE_SIZE = 1000  # PostgREST server cap per request
DECISION_PAGE_SIZE = 500  # decision rows carry JSON; smaller pages
MAX_PAGES = 200  # a server that keeps answering full pages is refused, not followed
READ_TABLES = frozenset({"trades", "ai_decisions", "ai_call_metrics"})
# PostgREST JSON paths of ai_decisions.payload_json, selected under these aliases.
JSON_PATHS = {
    "pj_decision": "payload_json->decision",
    "pj_technicals": "payload_json->request->market_context->technicals",
    "pj_ai_call_id": "payload_json->ai_call_id",
}
_ORDER = "created_at.asc,id.asc"
DECISION_WINDOW_SECONDS = 180
_DECISION_LOOKBACK = timedelta(days=1)
_ACTION_FOR_DIRECTION = {"LONG": "OPEN_LONG", "SHORT": "OPEN_SHORT"}

_FILL = FillConfig()


def _day_start_iso(day: str) -> str:
    """Midnight of a ``FillConfig`` date in its offset (WIB)."""
    return f"{day}T00:00:00{_FILL.since_utc_offset}"


DEFAULT_LATENCY_SINCE = _day_start_iso(_FILL.latency_since)
DEFAULT_OVERSHOOT_SINCE = _day_start_iso(_FILL.overshoot_since)
# Exclusive window ends (pre-registered): calibrate reads the same rows on any later day.
DEFAULT_LATENCY_UNTIL = _day_start_iso(_FILL.latency_until)
DEFAULT_OVERSHOOT_UNTIL = _day_start_iso(_FILL.overshoot_until)


class TooManyPagesError(RuntimeError):
    """A query returned full pages ``MAX_PAGES`` times in a row."""


@dataclass(frozen=True)
class StopOvershootDetail:
    samples: Tuple[float, ...]
    skipped: int


@dataclass(frozen=True)
class Calibration:
    s_star_bps: float
    n_stops: int
    latencies_ms: Tuple[float, ...]


def _positive_finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


async def fetch_all(
    get_rows: GetRows,
    table: str,
    params: Sequence[Tuple[str, str]],
    *,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> List[dict]:
    """Page with limit/offset until a page returns fewer than ``page_size`` rows;
    ``TooManyPagesError`` after ``max_pages`` full pages."""
    rows: List[dict] = []
    for page_no in range(max_pages):
        offset = page_no * page_size
        page = await get_rows(
            table,
            [*params, ("order", _ORDER), ("limit", str(page_size)), ("offset", str(offset))],
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows
    raise TooManyPagesError(f"{table}: more than {max_pages} pages of {page_size} rows")


def json_select(*aliases: str) -> str:
    """``alias:payload_json->path`` select items for the given ``JSON_PATHS`` aliases."""
    return ",".join(f"{alias}:{JSON_PATHS[alias]}" for alias in aliases)


def payload_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """The decision payload of a row: its full ``payload_json`` (dict or JSON text) when
    present, else the parts rebuilt from the ``JSON_PATHS`` columns it carries."""
    raw = row.get("payload_json")
    if raw is not None:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return {}
        return raw if isinstance(raw, dict) else {}
    payload: Dict[str, Any] = {}
    if "pj_decision" in row:
        payload["decision"] = row["pj_decision"]
    if "pj_technicals" in row:
        payload["request"] = {"market_context": {"technicals": row["pj_technicals"]}}
    if "pj_ai_call_id" in row:
        payload["ai_call_id"] = row["pj_ai_call_id"]
    return payload


async def load_entry_latencies_ms(
    get_rows: GetRows,
    table: str = _FILL.latency_table,
    since_iso: str = DEFAULT_LATENCY_SINCE,
    until_iso: str = DEFAULT_LATENCY_UNTIL,
) -> List[float]:
    """Successful entry-call latencies in ms (finite and >= 0) created in
    [``since_iso``, ``until_iso``)."""
    rows = await fetch_all(
        get_rows,
        table,
        [
            ("select", "created_at,latency_ms"),
            ("purpose", f"eq.{_FILL.latency_purpose}"),
            ("success", "eq.true"),
            ("created_at", f"gte.{since_iso}"),
            ("created_at", f"lt.{until_iso}"),
        ],
    )
    latencies: List[float] = []
    for row in rows:
        value = row.get("latency_ms")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if math.isfinite(number) and number >= 0:
            latencies.append(number)
    return latencies


def _entry_context(payload: Any, symbol: str) -> Optional[Tuple[float, float]]:
    """(AI stop %, ATR %) from one decision payload, or None when unusable."""
    if not isinstance(payload, dict):
        return None
    decision = payload.get("decision")
    request = payload.get("request")
    if not isinstance(decision, dict) or not isinstance(request, dict):
        return None
    ai_stop = _positive_finite(decision.get("stop_loss_pct"))
    market_context = request.get("market_context")
    technicals = market_context.get("technicals") if isinstance(market_context, dict) else None
    if ai_stop is None or not isinstance(technicals, list):
        return None
    for item in technicals:
        if not isinstance(item, dict) or item.get("symbol") != symbol:
            continue
        atr = _positive_finite(item.get("atr_14"))
        last = _positive_finite(item.get("last_price"))
        if atr is None or last is None:
            return None
        return ai_stop, atr / last * 100.0
    return None


def _stop_pct(ai_stop: float, atr_pct: float) -> float:
    return min(max(ai_stop, atr_pct * _FILL.stop_atr_multiple), _FILL.stop_cap_pct)


def _overshoot_bps(trade: dict, stop_pct: float) -> Optional[float]:
    entry = _positive_finite(trade.get("entry_price"))
    exit_ = _positive_finite(trade.get("exit_price"))
    if entry is None or exit_ is None:
        return None
    if trade.get("direction") == "LONG":
        adverse = (entry - exit_) / entry * 100.0
    else:
        adverse = (exit_ - entry) / entry * 100.0
    return (adverse - stop_pct) * 100.0


class _DecisionIndex:
    """Entry decisions grouped by (symbol, action), sorted by creation time."""

    def __init__(self, rows: Sequence[dict]) -> None:
        grouped: dict = {}
        for row in rows:
            created = parse_iso_datetime(row.get("created_at"))
            if created is None:
                continue
            key = (row.get("symbol"), row.get("action"))
            grouped.setdefault(key, []).append((created, row))
        self._times = {}
        self._rows = {}
        for key, items in grouped.items():
            items.sort(key=lambda pair: pair[0])
            self._times[key] = [created for created, _ in items]
            self._rows[key] = [row for _, row in items]

    def latest_before(self, symbol: Any, action: str, opened_at: datetime) -> Optional[dict]:
        """Latest decision with ``opened_at - 180 s <= created_at <= opened_at``."""
        key = (symbol, action)
        times = self._times.get(key)
        if not times:
            return None
        pos = bisect.bisect_right(times, opened_at) - 1
        if pos < 0:
            return None
        if opened_at - times[pos] > timedelta(seconds=DECISION_WINDOW_SECONDS):
            return None
        return self._rows[key][pos]


async def stop_overshoot_detail(
    get_rows: GetRows,
    trades_table: str,
    decisions_table: str,
    since_iso: str = DEFAULT_OVERSHOOT_SINCE,
    until_iso: str = DEFAULT_OVERSHOOT_UNTIL,
) -> StopOvershootDetail:
    """Overshoot (bps) of every reconstructable live stop-loss exit opened in
    [``since_iso``, ``until_iso``), plus the skipped count."""
    trades = await fetch_all(
        get_rows,
        trades_table,
        [
            ("select", "id,created_at,symbol,direction,entry_price,exit_price,opened_at"),
            ("reason", "eq.stop_loss"),
            ("exchange", "eq.bingx"),
            ("opened_at", f"gte.{since_iso}"),
            ("opened_at", f"lt.{until_iso}"),
        ],
    )
    since = parse_iso_datetime(since_iso)
    lookback_iso = (since - _DECISION_LOOKBACK).isoformat() if since else since_iso
    decisions = await fetch_all(
        get_rows,
        decisions_table,
        [
            ("select", "id,created_at,symbol,action," + json_select("pj_decision", "pj_technicals")),
            ("action", "in.(OPEN_LONG,OPEN_SHORT)"),
            ("created_at", f"gte.{lookback_iso}"),
            ("created_at", f"lt.{until_iso}"),  # a matched decision precedes its trade
        ],
        page_size=DECISION_PAGE_SIZE,
    )
    index = _DecisionIndex(decisions)
    samples: List[float] = []
    skipped = 0
    for trade in trades:
        opened_at = parse_iso_datetime(trade.get("opened_at"))
        action = _ACTION_FOR_DIRECTION.get(trade.get("direction"))
        overshoot: Optional[float] = None
        if opened_at is not None and action is not None:
            symbol = trade.get("symbol")
            match = index.latest_before(symbol, action, opened_at)
            context = _entry_context(payload_from_row(match), symbol) if match else None
            if context is not None:
                overshoot = _overshoot_bps(trade, _stop_pct(*context))
        if overshoot is None:
            skipped += 1
        else:
            samples.append(overshoot)
    return StopOvershootDetail(samples=tuple(samples), skipped=skipped)


async def stop_overshoot_samples(
    get_rows: GetRows,
    trades_table: str,
    decisions_table: str,
    since_iso: str = DEFAULT_OVERSHOOT_SINCE,
    until_iso: str = DEFAULT_OVERSHOOT_UNTIL,
) -> List[float]:
    detail = await stop_overshoot_detail(get_rows, trades_table, decisions_table, since_iso,
                                         until_iso)
    return list(detail.samples)


def s_star(
    samples: Sequence[float],
    min_samples: int = _FILL.min_samples,
    fallback: float = _FILL.fallback_bps,
) -> float:
    """Median overshoot rounded UP to the next 0.5 bp, never below ``FillConfig.floor_bps``
    (0: a stop never fills better than its level); ``fallback`` below ``min_samples``."""
    if len(samples) < min_samples:
        return fallback
    step = _FILL.round_up_bps
    return max(_FILL.floor_bps, math.ceil(statistics.median(samples) / step) * step)


async def load_calibration(
    get_rows: GetRows,
    trades_table: str,
    decisions_table: str,
    latency_table: str = _FILL.latency_table,
) -> Calibration:
    samples = await stop_overshoot_samples(get_rows, trades_table, decisions_table)
    latencies = await load_entry_latencies_ms(get_rows, latency_table)
    return Calibration(
        s_star_bps=s_star(samples), n_stops=len(samples), latencies_ms=tuple(latencies)
    )


def read_only(get_rows: GetRows) -> GetRows:
    """A reader of ``READ_TABLES`` only; any other table is a ``ValueError``."""

    async def read(table: str, params: "list[tuple[str, str]]") -> "list[dict]":
        if table not in READ_TABLES:
            raise ValueError(f"table {table!r} is not readable here")
        return await get_rows(table, list(params))

    return read


@asynccontextmanager
async def make_get_rows(
    settings: Any, store_factory: Optional[Callable[[Any], Any]] = None
) -> AsyncIterator[GetRows]:
    """``async with make_get_rows(settings) as get_rows``: a ``read_only`` reader over the
    bot's Supabase store (GET only; the store itself is never handed out), whose HTTP
    client is closed on exit. ``store_factory`` is for tests."""
    store = (store_factory or _bot_store)(settings)
    try:
        yield read_only(store._get_rows)
    finally:
        await store.close()


def _bot_store(settings: Any) -> Any:  # pragma: no cover - builds the live store
    from bot.storage.supabase_trade_store import SupabaseTradeStore

    return SupabaseTradeStore(settings, logging.getLogger("research.pine_gate.calibrate"))
