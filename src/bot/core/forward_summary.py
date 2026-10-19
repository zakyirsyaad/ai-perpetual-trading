"""Summary of the LONG forward test's closed positions, and the CSV that carries them.

Pure: no settings, no store, no network. ``report_forward_test`` prints the summary and writes the CSV
(``export_csv``); the offline mode backtest reads the CSV back (``read_trades_csv``) and puts the same
summary beside mode L's backtest of the same week (research may import ``bot``, never the reverse).
Keeping both sides here keeps one definition of the columns, the exit buckets and the per-1M maths.

The window is [first_day 00:00, last_day 00:00) in WIB; a position belongs to the window of its
``closed_at``. Hasil per 1 juta is pooled: 1,000,000 x sum(pnl) / sum((entry + exit) x quantity).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

WIB = ZoneInfo("Asia/Jakarta")
PER_MILLION = 1_000_000.0
DROPPED_TOP_TRADES = 3

CSV_COLUMNS = (
    "opened_at",
    "closed_at",
    "direction",
    "entry_price",
    "exit_price",
    "quantity",
    "pnl_usdt",
    "reason",
    "volume_usdt",
)
DIRECTIONS = ("LONG", "SHORT")

EXIT_TP = "tp"
EXIT_SL = "sl"
EXIT_TIME = "waktu"
EXIT_AI = "ai"
EXIT_OTHER = "lain"
EXIT_BUCKETS = (EXIT_TP, EXIT_SL, EXIT_TIME, EXIT_AI, EXIT_OTHER)
# The reason strings TradeEngine._monitor_position returns and stores; strategy exits carry a suffix
# ("strategy_exit:<model reason>"), so they match by prefix.
_EXACT_EXIT_BUCKETS = {
    "neutral_exit": EXIT_TP,
    "take_profit": EXIT_TP,
    "stop_loss": EXIT_SL,
    "time_stop": EXIT_TIME,
}
_STRATEGY_EXIT_PREFIX = "strategy_exit"
_VOLUME_REL_TOLERANCE = 1e-9  # the CSV's volume column against (entry + exit) x quantity


class ClosedPosition(Protocol):
    """What the summary reads of a closed position: ``ClosedTrade`` and ``ForwardTrade`` both fit."""

    entry_price: float
    exit_price: float
    quantity: float
    pnl_usdt: float
    reason: str
    closed_at: datetime


@dataclass(frozen=True, slots=True)
class ForwardTrade:
    """One row of the trade CSV, as ``export_csv`` wrote it."""

    opened_at: datetime
    closed_at: datetime
    direction: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl_usdt: float
    reason: str
    volume_usdt: float


@dataclass(frozen=True, slots=True)
class ForwardSummary:
    posisi: int
    entry_per_hari_kalender: float
    untung_pct: float
    exits: dict[str, int]
    hasil_per_1_juta: Optional[float]
    hasil_tanpa_teratas: Optional[float]
    pnl_usdt: float
    volume_usdt: float
    non_long: int = 0  # positions in the window whose direction is not LONG; the forward bot is LONG-only


def direction_name(trade: object) -> str:
    """`LONG`/`SHORT` of a ``ClosedTrade`` (enum) or a ``ForwardTrade`` (text)."""
    direction = getattr(trade, "direction", None)
    return str(getattr(direction, "value", direction))


def non_long_warning(count: int) -> Optional[str]:
    """The line that must accompany any summary holding a position that is not LONG, or None."""
    if count <= 0:
        return None
    return (
        f"PERINGATAN: {count} posisi bukan LONG di dalam jendela. Bot uji maju hanya boleh LONG, jadi "
        "hasil gabungan di bawah memuat sisi lain: periksa penyaring arah sebelum memakai angkanya."
    )


def classify_exit(reason: str) -> str:
    """Bucket of a stored exit reason; anything unknown counts under `lain` rather than being dropped."""
    text = (reason or "").strip()
    if text in _EXACT_EXIT_BUCKETS:
        return _EXACT_EXIT_BUCKETS[text]
    if text == _STRATEGY_EXIT_PREFIX or text.startswith(_STRATEGY_EXIT_PREFIX + ":"):
        return EXIT_AI
    return EXIT_OTHER


def trade_volume(trade: ClosedPosition) -> float:
    """Traded volume of a position, both legs; the definition SupabaseTradeStore._sum_trade_volume uses."""
    return (trade.entry_price + trade.exit_price) * trade.quantity


def window_bounds(first_day: date, last_day: date) -> tuple[datetime, datetime]:
    """[first_day 00:00, last_day 00:00) in WIB. Refuses an empty or reversed window."""
    if last_day <= first_day:
        raise ValueError(f"empty or reversed window: from {first_day} to {last_day} (to is exclusive)")
    return (
        datetime.combine(first_day, time.min, tzinfo=WIB),
        datetime.combine(last_day, time.min, tzinfo=WIB),
    )


def trades_in_window(trades: Sequence[ClosedPosition], first_day: date, last_day: date) -> list:
    start, end = window_bounds(first_day, last_day)
    return [trade for trade in trades if start <= trade.closed_at < end]


def _pooled_per_1m(trades: Sequence[ClosedPosition]) -> Optional[float]:
    volume = sum(trade_volume(trade) for trade in trades)
    if volume <= 0:
        return None
    return PER_MILLION * sum(trade.pnl_usdt for trade in trades) / volume


def summarize(
    trades: Sequence[ClosedPosition],
    first_day: date,
    last_day: date,
    drop_top: int = DROPPED_TOP_TRADES,
) -> ForwardSummary:
    """Summary of the trades closed inside the WIB window [first_day, last_day); ``drop_top`` best
    positions are left out of ``hasil_tanpa_teratas``."""
    window = trades_in_window(trades, first_day, last_day)
    days = (last_day - first_day).days
    exits = {bucket: 0 for bucket in EXIT_BUCKETS}
    for trade in window:
        exits[classify_exit(trade.reason)] += 1
    ranked = sorted(window, key=lambda trade: trade.pnl_usdt, reverse=True)
    wins = sum(1 for trade in window if trade.pnl_usdt > 0)
    return ForwardSummary(
        posisi=len(window),
        entry_per_hari_kalender=len(window) / days,
        untung_pct=(100.0 * wins / len(window)) if window else 0.0,
        exits=exits,
        hasil_per_1_juta=_pooled_per_1m(window),
        hasil_tanpa_teratas=_pooled_per_1m(ranked[drop_top:]),
        pnl_usdt=sum(trade.pnl_usdt for trade in window),
        volume_usdt=sum(trade_volume(trade) for trade in window),
        non_long=sum(1 for trade in window if direction_name(trade) != "LONG"),
    )


def export_csv(trades: Sequence, path: Path) -> None:
    """Write ``ClosedTrade`` or ``ForwardTrade`` rows to `path`, refusing (FileExistsError) to overwrite.

    Columns are an interface."""
    with open(path, "x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for trade in trades:
            writer.writerow(
                [
                    trade.opened_at.astimezone(timezone.utc).isoformat(),
                    trade.closed_at.astimezone(timezone.utc).isoformat(),
                    direction_name(trade),
                    trade.entry_price,
                    trade.exit_price,
                    trade.quantity,
                    trade.pnl_usdt,
                    trade.reason,
                    trade_volume(trade),
                ]
            )


def _aware_time(text: str, column: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{column} is not an ISO-8601 time: {text!r}") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{column} has no timezone: {text!r}")
    return moment


def _finite(text: str, column: str, positive: bool) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{column} is not a number: {text!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{column} is not finite: {text!r}")
    if positive and value <= 0:
        raise ValueError(f"{column} must be above 0, got {text!r}")
    return value


def _parse_row(row: Sequence[str]) -> ForwardTrade:
    if len(row) != len(CSV_COLUMNS):
        raise ValueError(f"{len(row)} fields, expected {len(CSV_COLUMNS)}")
    cells = dict(zip(CSV_COLUMNS, row))
    opened_at = _aware_time(cells["opened_at"], "opened_at")
    closed_at = _aware_time(cells["closed_at"], "closed_at")
    if closed_at < opened_at:
        raise ValueError(f"closed_at {cells['closed_at']} is before opened_at {cells['opened_at']}")
    if cells["direction"] not in DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}, got {cells['direction']!r}")
    trade = ForwardTrade(
        opened_at=opened_at,
        closed_at=closed_at,
        direction=cells["direction"],
        entry_price=_finite(cells["entry_price"], "entry_price", positive=True),
        exit_price=_finite(cells["exit_price"], "exit_price", positive=True),
        quantity=_finite(cells["quantity"], "quantity", positive=True),
        pnl_usdt=_finite(cells["pnl_usdt"], "pnl_usdt", positive=False),
        reason=cells["reason"],
        volume_usdt=_finite(cells["volume_usdt"], "volume_usdt", positive=True),
    )
    if not math.isclose(trade.volume_usdt, trade_volume(trade), rel_tol=_VOLUME_REL_TOLERANCE):
        raise ValueError(
            f"volume_usdt {trade.volume_usdt!r} is not (entry_price + exit_price) x quantity "
            f"= {trade_volume(trade)!r}"
        )
    return trade


def read_trades_csv(path: Path) -> list[ForwardTrade]:
    """The rows ``export_csv`` wrote to ``path``, in file order.

    Anything the export never writes raises ``ValueError`` naming the file and the line: a header
    other than ``CSV_COLUMNS``, a row of another length, a time without a timezone, a closing time
    before the opening, a direction other than LONG or SHORT, a price or quantity that is not a finite
    number above 0, a PnL that is not finite, and a volume that is not both legs. A missing file
    raises ``FileNotFoundError``."""
    path = Path(path)
    with open(path, newline="", encoding="utf-8") as handle:
        try:
            rows = list(csv.reader(handle))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ValueError(f"{path}: not a UTF-8 trade CSV: {exc}") from exc
    if not rows or tuple(rows[0]) != CSV_COLUMNS:
        found = rows[0] if rows else "nothing"
        raise ValueError(f"{path}: the header must be {','.join(CSV_COLUMNS)}, found {found}")
    trades = []
    for number, row in enumerate(rows[1:], start=2):
        try:
            trades.append(_parse_row(row))
        except ValueError as exc:
            raise ValueError(f"{path} line {number}: {exc}") from exc
    return trades
