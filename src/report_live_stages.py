"""Stage report of the live bot: how each rollout stage came out, the shadow evidence and the setup ledger.

Read-only: it GETs closed trades and evaluated shadow rows through SupabaseTradeStore and never
writes to Supabase, touches pm2 or prints a setting. Run it from the live bot's directory so
Settings() reads that bot's .env:

    cd /home/ubuntu/ai-perpetual-trading
    PYTHONPATH=src python src/report_live_stages.py \\
        --stage-start 2026-10-22T10:00:00+07:00 --stage-start 2026-10-25T10:00:00+07:00

Each `--stage-start` opens a stage that ends where the next one starts; the last one ends at
`--until` (default: now). A trade belongs to the stage that holds its `closed_at`; a trade closed at
or after `--until` is left out of every section. The shadow table keeps the two sources apart. The
ledger section reads the engine's window, every trade and `ledger_block` row since
`SETUP_LEDGER_SINCE` up to `--until`; with it unset (ledger off) it falls back to the stage rows, and
its heading says which. The printed labels are Indonesian because the manager reads them.

Exit codes: 0 ok, 2 usage error (a naive time, no stage start, `--until` not after the first start).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bot.config import Settings
from bot.core.forward_summary import PER_MILLION, WIB, format_number, format_percent
from bot.core.setup_ledger import (
    SHADOW_BLOCK_SOURCE,
    bucket_stats,
    finite_or_none,
    ledger_status,
    ledger_table,
    trade_gross_bps,
)
from bot.logger import setup_logging
from bot.storage.supabase_trade_store import SupabaseTradeStore, parse_timestamp

__all__ = [
    "StageSummary",
    "build_parser",
    "execute",
    "main",
    "shadow_summary",
    "shadow_unpriced",
    "stage_rows",
]

SECONDS_PER_DAY = 86_400.0


@dataclass(frozen=True, slots=True)
class StageSummary:
    """One stage, [start, end): `win_rate` is a share in 0..1, the gross figures are in bps of the entry price.

    `None` marks a figure the stage cannot measure (no trades, one trade for the SE, no volume).
    """

    start: datetime
    end: datetime
    n: int
    win_rate: float | None
    mean_gross_bps: float | None
    se_gross_bps: float | None
    net_per_1m: float | None
    volume_per_day: float


@dataclass(frozen=True, slots=True)
class _TradeFacts:
    closed_at: datetime
    pnl_usdt: float
    volume_usdt: float
    gross_bps: float | None


def stage_rows(
    trades: Iterable[Mapping[str, Any]],
    starts: Sequence[datetime],
    until: datetime,
) -> list[StageSummary]:
    """One summary per stage start, in time order; a stage ends where the next one starts.

    Trades are the rows `closed_trade_rows_since` returns. A trade is assigned by `closed_at`, a trade
    closed before the first start or at or after `until` is ignored, and a row whose `closed_at`
    cannot be read is skipped. A stage that starts at or after `until` has not begun and is not listed.

    Raises ValueError for no start, a naive time, a duplicate start or `until` not after the first start.
    """
    bounds = _stage_bounds(starts, until)
    facts = [fact for fact in map(_trade_facts, trades) if fact is not None]
    return [
        _summarize_stage(start, end, [fact for fact in facts if start <= fact.closed_at < end])
        for start, end in bounds
    ]


def shadow_summary(rows: Iterable[Mapping[str, Any]]) -> list[tuple[str, str, str, int, float]]:
    """`(source, side, bucket, n, mean_bps)` per group of evaluated shadow rows.

    The sources are kept apart: `shadow_side` rows measure a side that is not traded (the LONG go-live
    bar), `ledger_block` rows a bucket's way back. Rows without a finite `gross_bps` (expired and invalid
    ones) or without a source, side or bucket are not averaged; `shadow_unpriced` counts them.
    """
    groups: dict[tuple[str, str, str], list[float]] = {}
    for row in rows:
        key = _shadow_group(row)
        value = finite_or_none(row.get("gross_bps"))
        if key is not None and value is not None:
            groups.setdefault(key, []).append(value)
    return [
        (source, side, bucket, len(values), sum(values) / len(values))
        for (source, side, bucket), values in sorted(groups.items())
    ]


def shadow_unpriced(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Evaluated shadow rows `shadow_summary` leaves out, counted by `exit_reason` (`expired`, `invalid`, ...)."""
    counts: Counter[str] = Counter()
    for row in rows:
        if _shadow_group(row) is None or finite_or_none(row.get("gross_bps")) is None:
            counts[str(row.get("exit_reason") or "tanpa alasan")] += 1
    return dict(sorted(counts.items()))


def _stage_bounds(starts: Sequence[datetime], until: datetime) -> list[tuple[datetime, datetime]]:
    if not starts:
        raise ValueError("at least one --stage-start is required")
    if until.tzinfo is None or any(start.tzinfo is None for start in starts):
        raise ValueError("stage starts and --until must carry a timezone (for example +07:00 or Z)")
    ordered = sorted(starts)
    if len(set(ordered)) != len(ordered):
        raise ValueError("two stages start at the same moment")
    if until <= ordered[0]:
        raise ValueError(f"--until {until.isoformat()} is not after the first stage start {ordered[0].isoformat()}")
    begun = [start for start in ordered if start < until]
    ends = [*begun[1:], until]
    return list(zip(begun, ends))


def _trade_facts(row: Mapping[str, Any]) -> _TradeFacts | None:
    closed_at = parse_timestamp(row.get("closed_at"))
    if closed_at is None:
        return None
    entry = _number(row.get("entry_price"))
    exit_ = _number(row.get("exit_price"))
    return _TradeFacts(
        closed_at=closed_at,
        pnl_usdt=_number(row.get("pnl_usdt")),
        volume_usdt=(entry + exit_) * _number(row.get("quantity")),
        gross_bps=trade_gross_bps(row),
    )


def _summarize_stage(start: datetime, end: datetime, members: Sequence[_TradeFacts]) -> StageSummary:
    stats = bucket_stats([fact.gross_bps for fact in members if fact.gross_bps is not None])
    volume = sum(fact.volume_usdt for fact in members)
    days = (end - start).total_seconds() / SECONDS_PER_DAY
    return StageSummary(
        start=start,
        end=end,
        n=len(members),
        win_rate=(sum(1 for fact in members if fact.pnl_usdt > 0) / len(members)) if members else None,
        mean_gross_bps=stats.mean_bps,
        se_gross_bps=stats.se_bps,
        net_per_1m=(PER_MILLION * sum(fact.pnl_usdt for fact in members) / volume) if volume > 0 else None,
        volume_per_day=volume / days,
    )


def _shadow_group(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    source = str(row.get("source") or "").strip()
    side = str(row.get("side") or "").strip().upper()
    bucket = str(row.get("bucket_key") or "").strip()
    return (source, side, bucket) if source and side and bucket else None


def _number(value: Any) -> float:
    """A float; 0.0 for a missing or non-finite value, like SupabaseTradeStore's closed-trade parser."""
    number = finite_or_none(value)
    return 0.0 if number is None else number


def _within(
    rows: Iterable[Mapping[str, Any]], column: str, since: datetime, until: datetime
) -> tuple[list[Mapping[str, Any]], int]:
    """Rows whose `column` time is in [since, until), and how many rows had no readable time at all."""
    kept: list[Mapping[str, Any]] = []
    unreadable = 0
    for row in rows:
        moment = parse_timestamp(row.get(column))
        if moment is None:
            unreadable += 1
        elif since <= moment < until:
            kept.append(row)
    return kept, unreadable


def _wib(moment: datetime) -> str:
    return moment.astimezone(WIB).isoformat(timespec="minutes")


def render_stages(stages: Sequence[StageSummary], waiting: Sequence[datetime]) -> str:
    lines = [
        "=== Ringkasan per tahap (WIB) ===",
        f"{'tahap':<6}{'mulai':<26}{'akhir':<26}{'posisi':>7}{'menang':>9}"
        f"{'gross bps':>11}{'SE bps':>9}{'hasil/1 juta':>14}{'volume/hari':>14}",
    ]
    for index, stage in enumerate(stages, start=1):
        lines.append(
            f"{index:<6}{_wib(stage.start):<26}{_wib(stage.end):<26}{stage.n:>7}"
            f"{format_percent(None if stage.win_rate is None else 100.0 * stage.win_rate, 1):>9}"
            f"{format_number(stage.mean_gross_bps, 2):>11}{format_number(stage.se_gross_bps, 2):>9}"
            f"{format_number(stage.net_per_1m, 0):>14}{format_number(stage.volume_per_day, 0):>14}"
        )
    for moment in waiting:
        lines.append(f"tahap belum mulai (tidak dihitung): {_wib(moment)}")
    return "\n".join(lines)


def render_shadow(rows: Sequence[Mapping[str, Any]]) -> str:
    lines = ["=== Shadow per sisi dan kelompok ==="]
    summary = shadow_summary(rows)
    if summary:
        lines.append(f"{'sumber':<14}{'sisi':<7}{'kelompok':<34}{'n':>6}{'rata-rata bps':>15}")
        lines.extend(
            f"{source:<14}{side:<7}{bucket:<34}{n:>6}{format_number(mean, 2):>15}"
            for source, side, bucket, n, mean in summary
        )
    else:
        lines.append("belum ada baris shadow yang dievaluasi dengan hasil")
    unpriced = shadow_unpriced(rows)
    if unpriced:
        reasons = ", ".join(f"{reason}={count}" for reason, count in unpriced.items())
        lines.append(f"tanpa hasil (tidak dirata-ratakan): {sum(unpriced.values())} ({reasons})")
    return "\n".join(lines)


def render_ledger(
    trades: Sequence[Mapping[str, Any]],
    shadows: Sequence[Mapping[str, Any]],
    settings: Any,
    window: str = "",
    since: datetime | None = None,
) -> str:
    """The ledger table; `window` says which rows it was built from (printed in the heading), and `since`
    leaves out trades opened before it, as the engine's ledger does."""
    min_samples = settings.setup_ledger_min_samples
    hurdle = settings.setup_ledger_hurdle_bps
    table = ledger_table(trades, shadows, since=since)
    lines = [
        f"=== Ledger {window}(minimum {min_samples} sampel, ambang {format_number(hurdle, 2)} bps) ===",
        f"{'kelompok':<34}{'n nyata':>8}{'rata-rata':>11}{'batas atas':>12}"
        f"{'n shadow':>10}{'rata-rata':>11}{'batas atas':>12}"
        f"{'n sejak blok':>14}{'batas atas':>12}  status",
    ]
    for bucket, entry in sorted(table.items()):
        real, shadow, since = entry.real, entry.shadow_run, entry.real_since_block
        lines.append(
            f"{bucket:<34}{real.n:>8}{_bps(real.mean_bps):>11}{_bps(real.upper_bps):>12}"
            f"{shadow.n:>10}{_bps(shadow.mean_bps):>11}{_bps(shadow.upper_bps):>12}"
            f"{since.n:>14}{_bps(since.upper_bps):>12}"
            f"  {ledger_status(entry, min_samples, hurdle)}"
        )
    if not table:
        lines.append("belum ada posisi atau baris shadow berkelompok")
    lines.append(
        "n shadow: hasil ledger_block dari rangkaian blok terakhir kelompok itu (baris yang diputuskan setelah "
        "posisi nyata terakhir sebelum baris ledger_block terakhir); hanya rangkaian itu yang bisa membuka kembali "
        "kelompok, dan baris expired/invalid hanya penanda waktu"
    )
    lines.append(
        "n sejak blok: posisi nyata yang dibuka setelah baris ledger_block terakhir kelompok itu; "
        "kelompok reopened diblok lagi bila jumlahnya mencapai minimum dan batas atasnya di bawah ambang"
    )
    grouped = sum(entry.real.n for entry in table.values())
    lines.append(f"posisi berkelompok: {grouped} dari {len(trades)} posisi")
    return "\n".join(lines)


def _bps(value: float | None) -> str:
    return format_number(value, 2)


def _parse_moment(text: str) -> datetime:
    """An ISO-8601 time; a trailing `Z` means UTC. A time without an offset stays naive so `execute` can refuse it."""
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 time: {text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stage report of the live bot: stages, shadow evidence, setup ledger.")
    parser.add_argument(
        "--stage-start",
        dest="stage_starts",
        action="append",
        type=_parse_moment,
        required=True,
        metavar="ISO",
        help="Start of a stage, timezone-aware ISO-8601 (repeatable; a stage ends where the next one starts).",
    )
    parser.add_argument(
        "--until",
        type=_parse_moment,
        default=None,
        metavar="ISO",
        help="End of the last stage, timezone-aware ISO-8601, exclusive (default: now).",
    )
    return parser


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


async def execute(args: argparse.Namespace, store: Any, settings: Any) -> int:
    """Run the report against `store`; exit codes: 0 ok, 2 usage error."""
    try:
        return await _execute(args, store, settings)
    finally:
        await store.close()


async def _execute(args: argparse.Namespace, store: Any, settings: Any) -> int:
    until = args.until if args.until is not None else datetime.now(timezone.utc)
    starts = list(args.stage_starts or [])
    try:
        _stage_bounds(starts, until)
    except ValueError as exc:
        _error(str(exc))
        return 2
    first = min(starts)
    trade_rows, unreadable_trades = _within(await store.closed_trade_rows_since(first), "closed_at", first, until)
    shadow_rows, unreadable_shadows = _within(await store.shadow_outcomes_since(first), "decided_at", first, until)
    stages = stage_rows(trade_rows, starts, until)
    waiting = sorted(start for start in starts if start >= until)
    unreadable = [(unreadable_trades, "posisi"), (unreadable_shadows, "baris shadow")]

    # The engine's ledger counts every trade and ledger_block row since SETUP_LEDGER_SINCE, so the report's
    # ledger reads that window (cut at --until). Without it the ledger is off, and the stage rows stand in.
    ledger_since = settings.setup_ledger_since
    if ledger_since is None:
        ledger_trades, ledger_shadows = trade_rows, shadow_rows
        window = f"(SETUP_LEDGER_SINCE tidak diatur, ledger mati: dibaca sejak awal tahap pertama {_wib(first)}) "
    else:
        ledger_trades, unreadable_ledger_trades = _within(
            await store.closed_trade_rows_since(ledger_since), "closed_at", ledger_since, until
        )
        block_rows = await store.shadow_outcomes_since(ledger_since, source=SHADOW_BLOCK_SOURCE)
        ledger_shadows, unreadable_ledger_shadows = _within(block_rows, "decided_at", ledger_since, until)
        unreadable += [(unreadable_ledger_trades, "posisi ledger"), (unreadable_ledger_shadows, "baris shadow ledger")]
        window = f"sejak {_wib(ledger_since)} (SETUP_LEDGER_SINCE) sampai {_wib(until)} "

    print(f"Dibaca sejak {_wib(first)} sampai {_wib(until)} (WIB, akhir tidak termasuk)")
    for count, what in unreadable:
        if count:
            print(f"PERINGATAN: {count} {what} tanpa waktu yang terbaca dilewati.")
    print()
    print(render_stages(stages, waiting))
    print()
    print(render_shadow(shadow_rows))
    print()
    print(render_ledger(ledger_trades, ledger_shadows, settings, window, since=ledger_since))
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    logger = setup_logging(settings.log_level)
    return await execute(args, SupabaseTradeStore(settings, logger), settings)


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
