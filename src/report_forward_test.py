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
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from bot.config import Settings
from bot.core.forward_guard import CycleTotals, forward_guard_reason
from bot.core.forward_summary import (
    CSV_COLUMNS,
    DROPPED_TOP_TRADES,
    EXIT_AI,
    EXIT_OTHER,
    EXIT_SL,
    EXIT_TIME,
    EXIT_TP,
    WIB,
    ForwardSummary,
    classify_exit,
    export_csv,
    non_long_warning,
    summarize,
    trade_volume,
    trades_in_window,
    window_bounds,
)
from bot.logger import setup_logging
from bot.storage.supabase_trade_store import SupabaseTradeStore

# The summary, the exit buckets and the CSV live in bot.core.forward_summary, so the offline mode
# backtest can read the same CSV and reuse the same maths without importing the store; they are
# re-exported here under their old names.
__all__ = [
    "CSV_COLUMNS",
    "DROPPED_TOP_TRADES",
    "EXIT_AI",
    "EXIT_OTHER",
    "EXIT_SL",
    "EXIT_TIME",
    "EXIT_TP",
    "WIB",
    "ForwardSummary",
    "build_parser",
    "classify_exit",
    "execute",
    "export_csv",
    "main",
    "render_cycle",
    "render_guard",
    "render_summary",
    "summarize",
    "trade_volume",
    "trades_in_window",
    "window_bounds",
]


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
    warning = non_long_warning(summary.non_long)
    if warning:
        lines.append(warning)
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


def render_cycle(settings: Any, first_day: date) -> str:
    """The cycle start the store filters on, and a warning when the window begins before it.

    The store reads only trades closed since VOLUME_CYCLE_STARTED_AT, so an earlier day of the
    window would silently come back empty or short.
    """
    cycle_start = getattr(settings, "volume_cycle_started_at", None)
    if cycle_start is None:
        return "VOLUME_CYCLE_STARTED_AT    : tidak diatur (semua posisi di skema ini dihitung)"
    lines = [f"VOLUME_CYCLE_STARTED_AT    : {cycle_start.astimezone(WIB).isoformat()} (WIB)"]
    if first_day < cycle_start.astimezone(WIB).date():
        lines.append(
            f"PERINGATAN: --from {first_day} lebih awal dari VOLUME_CYCLE_STARTED_AT; posisi yang "
            "ditutup sebelum awal siklus tidak dibaca, jadi hari-hari awal jendela tampak kosong atau kurang."
        )
    return "\n".join(lines)


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
    print(render_cycle(settings, args.first_day))
    print()
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
