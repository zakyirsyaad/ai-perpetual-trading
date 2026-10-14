"""Live calibration readers: AI entry latency and stop overshoot (spec 5.10, 5.2).

Research code; the bot never imports it. It reads the live bot's Supabase
records READ-ONLY through an injected ``get_rows`` coroutine (one PostgREST
page per call) and pages every query itself. The two outputs calibrate the
simulator: the entry-call latency distribution and ``s*``, how far past the
stop level live stop-loss exits actually filled (the bot polls every 5 s and
closes with a market order).

Entry-decision payload shape (src/bot/ai/orchestration.py ``_record_ai_decision``
and ``CompactDecisionContextBuilder.build_entry_payload``)::

    payload_json.decision.stop_loss_pct
    payload_json.request.market_context.technicals  # [{symbol, last_price, atr_14, ...}]
"""

from __future__ import annotations

import bisect
import logging
import math
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Tuple

from .spec import FillConfig

GetRows = Callable[[str, "list[tuple[str, str]]"], Awaitable["list[dict]"]]

PAGE_SIZE = 1000  # PostgREST server cap per request
_ORDER = "created_at.asc,id.asc"
DECISION_WINDOW_SECONDS = 180
_DECISION_LOOKBACK = timedelta(days=1)
_ACTION_FOR_DIRECTION = {"LONG": "OPEN_LONG", "SHORT": "OPEN_SHORT"}

_FILL = FillConfig()


def _day_start_iso(day: str) -> str:
    return f"{day}T00:00:00+00:00"


DEFAULT_LATENCY_SINCE = _day_start_iso(_FILL.latency_since)
DEFAULT_OVERSHOOT_SINCE = _day_start_iso(_FILL.overshoot_since)

_FRACTION = re.compile(r"\.(\d+)")


@dataclass(frozen=True)
class StopOvershootDetail:
    samples: Tuple[float, ...]
    skipped: int


@dataclass(frozen=True)
class Calibration:
    s_star_bps: float
    n_stops: int
    latencies_ms: Tuple[float, ...]


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a PostgREST timestamptz (``Z`` suffix, 1-6 fraction digits) on Python 3.10."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _positive_finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


async def fetch_all(
    get_rows: GetRows, table: str, params: Sequence[Tuple[str, str]]
) -> List[dict]:
    """Page with limit/offset until a page returns fewer than ``PAGE_SIZE`` rows."""
    rows: List[dict] = []
    offset = 0
    while True:
        page = await get_rows(
            table,
            [*params, ("order", _ORDER), ("limit", str(PAGE_SIZE)), ("offset", str(offset))],
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


async def load_entry_latencies_ms(
    get_rows: GetRows,
    table: str = _FILL.latency_table,
    since_iso: str = DEFAULT_LATENCY_SINCE,
) -> List[float]:
    """Successful entry-call latencies in ms (finite and >= 0) since ``since_iso``."""
    rows = await fetch_all(
        get_rows,
        table,
        [
            ("select", "created_at,latency_ms"),
            ("purpose", f"eq.{_FILL.latency_purpose}"),
            ("success", "eq.true"),
            ("created_at", f"gte.{since_iso}"),
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
            created = _parse_ts(row.get("created_at"))
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
) -> StopOvershootDetail:
    """Overshoot (bps) of every reconstructable live stop-loss exit, plus the skipped count."""
    trades = await fetch_all(
        get_rows,
        trades_table,
        [
            ("select", "id,created_at,symbol,direction,entry_price,exit_price,opened_at"),
            ("reason", "eq.stop_loss"),
            ("exchange", "eq.bingx"),
            ("opened_at", f"gte.{since_iso}"),
        ],
    )
    since = _parse_ts(since_iso)
    lookback_iso = (since - _DECISION_LOOKBACK).isoformat() if since else since_iso
    decisions = await fetch_all(
        get_rows,
        decisions_table,
        [
            ("select", "id,created_at,symbol,action,payload_json"),
            ("action", "in.(OPEN_LONG,OPEN_SHORT)"),
            ("created_at", f"gte.{lookback_iso}"),
        ],
    )
    index = _DecisionIndex(decisions)
    samples: List[float] = []
    skipped = 0
    for trade in trades:
        opened_at = _parse_ts(trade.get("opened_at"))
        action = _ACTION_FOR_DIRECTION.get(trade.get("direction"))
        overshoot: Optional[float] = None
        if opened_at is not None and action is not None:
            symbol = trade.get("symbol")
            match = index.latest_before(symbol, action, opened_at)
            context = _entry_context(match.get("payload_json"), symbol) if match else None
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
) -> List[float]:
    detail = await stop_overshoot_detail(get_rows, trades_table, decisions_table, since_iso)
    return list(detail.samples)


def s_star(
    samples: Sequence[float],
    min_samples: int = _FILL.min_samples,
    fallback: float = _FILL.fallback_bps,
) -> float:
    """Median overshoot rounded UP to the next 0.5 bp; ``fallback`` below ``min_samples``."""
    if len(samples) < min_samples:
        return fallback
    step = _FILL.round_up_bps
    return math.ceil(statistics.median(samples) / step) * step


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


def make_get_rows(settings: Any) -> GetRows:  # pragma: no cover
    """Wrap the bot's ``SupabaseTradeStore._get_rows`` (read-only; no URLs or keys are logged)."""
    from bot.storage.supabase_trade_store import SupabaseTradeStore

    store = SupabaseTradeStore(settings, logging.getLogger("research.pine_gate.calibrate"))
    return store._get_rows
