"""Weekly report of the LONG forward test, plus the trade export that feeds the backtest comparison.

Read-only: it GETs closed trades through SupabaseTradeStore and never writes to Supabase. Run it from the
forward bot's directory so Settings() reads that bot's .env:

    cd /home/ubuntu/ai-perp-long
    PYTHONPATH=src python src/report_forward_test.py --from 2026-10-12 --to 2026-10-19 --out-dir ~/forward_reports

The window is [from 00:00, to 00:00) in WIB (Asia/Jakarta); a position belongs to the window of its
closed_at. The printed labels are Indonesian because the manager reads them.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.core.forward_guard import CycleTotals, forward_guard_reason
from bot.logger import setup_logging
from bot.models import ClosedTrade
from bot.storage.supabase_trade_store import SupabaseTradeStore

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

EXIT_TP = "tp"
EXIT_SL = "sl"
EXIT_TIME = "waktu"
EXIT_AI = "ai"
EXIT_OTHER = "lain"
# The reason strings TradeEngine._monitor_position returns and stores; strategy exits carry a suffix
# ("strategy_exit:<model reason>"), so they match by prefix.
_EXACT_EXIT_BUCKETS = {
    "neutral_exit": EXIT_TP,
    "take_profit": EXIT_TP,
    "stop_loss": EXIT_SL,
    "time_stop": EXIT_TIME,
}
_STRATEGY_EXIT_PREFIX = "strategy_exit"


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


def classify_exit(reason: str) -> str:
    """Bucket of a stored exit reason; anything unknown counts under `lain` rather than being dropped."""
    text = (reason or "").strip()
    if text in _EXACT_EXIT_BUCKETS:
        return _EXACT_EXIT_BUCKETS[text]
    if text == _STRATEGY_EXIT_PREFIX or text.startswith(_STRATEGY_EXIT_PREFIX + ":"):
        return EXIT_AI
    return EXIT_OTHER


def trade_volume(trade: ClosedTrade) -> float:
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


def trades_in_window(trades: Sequence[ClosedTrade], first_day: date, last_day: date) -> list[ClosedTrade]:
    start, end = window_bounds(first_day, last_day)
    return [trade for trade in trades if start <= trade.closed_at < end]


def _pooled_per_1m(trades: Sequence[ClosedTrade]) -> Optional[float]:
    volume = sum(trade_volume(trade) for trade in trades)
    if volume <= 0:
        return None
    return PER_MILLION * sum(trade.pnl_usdt for trade in trades) / volume


def summarize(trades: Sequence[ClosedTrade], first_day: date, last_day: date) -> ForwardSummary:
    """Summary of the trades closed inside the WIB window [first_day, last_day)."""
    window = trades_in_window(trades, first_day, last_day)
    days = (last_day - first_day).days
    exits = {EXIT_TP: 0, EXIT_SL: 0, EXIT_TIME: 0, EXIT_AI: 0, EXIT_OTHER: 0}
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
        hasil_tanpa_teratas=_pooled_per_1m(ranked[DROPPED_TOP_TRADES:]),
        pnl_usdt=sum(trade.pnl_usdt for trade in window),
        volume_usdt=sum(trade_volume(trade) for trade in window),
    )


def export_csv(trades: Sequence[ClosedTrade], path: Path) -> None:
    """Write the trades to `path`, refusing (FileExistsError) to overwrite. Columns are an interface."""
    with open(path, "x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for trade in trades:
            writer.writerow(
                [
                    trade.opened_at.astimezone(timezone.utc).isoformat(),
                    trade.closed_at.astimezone(timezone.utc).isoformat(),
                    trade.direction.value,
                    trade.entry_price,
                    trade.exit_price,
                    trade.quantity,
                    trade.pnl_usdt,
                    trade.reason,
                    trade_volume(trade),
                ]
            )


def _fmt(value: Optional[float], digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_summary(summary: ForwardSummary, first_day: date, last_day: date) -> str:
    exits = summary.exits
    lines = [
        f"=== Uji maju LONG, {first_day} s/d {last_day} (WIB, akhir tidak termasuk) ===",
        f"posisi                    : {summary.posisi}",
        f"entry per hari kalender   : {summary.entry_per_hari_kalender:.2f}",
        f"untung (pnl > 0)          : {summary.untung_pct:.1f}%",
        f"keluar                    : tp={exits[EXIT_TP]} sl={exits[EXIT_SL]} "
        f"waktu={exits[EXIT_TIME]} ai={exits[EXIT_AI]} lain={exits[EXIT_OTHER]}",
        f"pnl bersih (USDT)         : {summary.pnl_usdt:.2f}",
        f"volume (USDT)             : {summary.volume_usdt:.0f}",
        f"hasil per 1 juta          : {_fmt(summary.hasil_per_1_juta)}",
        f"hasil tanpa {DROPPED_TOP_TRADES} teratas     : {_fmt(summary.hasil_tanpa_teratas)}",
    ]
    return "\n".join(lines)


def render_guard(totals: CycleTotals, settings: Any) -> str:
    kill_after = settings.forward_kill_after_positions
    review_at = settings.forward_review_at_positions
    reason = forward_guard_reason(
        totals, kill_after, settings.forward_kill_below_per_1m, review_at
    )
    return "\n".join(
        [
            "--- Penjaga uji maju (seluruh siklus) ---",
            f"posisi tertutup           : {totals.count} (kill dari {kill_after}, tinjau di {review_at})",
            f"hasil gabungan per 1 juta : {_fmt(totals.pnl_per_1m())}",
            f"status                    : {reason if reason else 'aktif'}",
        ]
    )


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Weekly LONG forward-test report and trade export.")
    parser.add_argument("--from", dest="first_day", type=_parse_day, required=True, help="First day, WIB (YYYY-MM-DD).")
    parser.add_argument("--to", dest="last_day", type=_parse_day, required=True, help="End day, WIB, exclusive (YYYY-MM-DD).")
    parser.add_argument("--out-dir", type=Path, default=Path("."), help="Directory for the CSV (default: current).")
    return parser


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


async def execute(args: argparse.Namespace, store: Any, settings: Any) -> int:
    """Run the report against `store`; exit codes: 0 ok, 1 expected failure, 2 usage error."""
    try:
        return await _execute(args, store, settings)
    finally:
        await store.close()


async def _execute(args: argparse.Namespace, store: Any, settings: Any) -> int:
    try:
        window_bounds(args.first_day, args.last_day)
    except ValueError as exc:
        _error(str(exc))
        return 2
    out_path = args.out_dir / f"forward_trades_{args.first_day}_{args.last_day}.csv"
    if out_path.exists():
        _error(f"{out_path} already exists; refusing to overwrite")
        return 1
    trades = await store.closed_trades_since_cycle()
    totals = await store.cycle_trade_totals()
    window = trades_in_window(trades, args.first_day, args.last_day)
    print(render_summary(summarize(trades, args.first_day, args.last_day), args.first_day, args.last_day))
    print()
    print(render_guard(totals, settings))
    try:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        export_csv(window, out_path)
    except OSError as exc:
        _error(f"cannot write {out_path}: {exc}")
        return 1
    print()
    print(f"CSV: {out_path} ({len(window)} posisi)")
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    logger = setup_logging(settings.log_level)
    return await execute(args, SupabaseTradeStore(settings, logger), settings)


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
