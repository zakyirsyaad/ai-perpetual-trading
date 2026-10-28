"""Shadow report of exit variants: what a profit lock, a lower floor or a trailing stop would have changed on the real trades.

Analysis only and read-only: it GETs closed trades and the model's OPEN decisions through the store, reads 1m candles
from a local cache file and prints text. It never writes to Supabase, places or changes an order, touches pm2 or prints a
setting. For every closed trade it replays the live exits and a few pre-registered variants over the same candles and
reports the paired difference per trade (``research.exit_variants.study``); the printed labels are Indonesian because the
manager reads them. Run it from a bot's directory so ``Settings()`` reads that bot's ``.env`` and so the schema is that
bot's (account 1 ``public``, account 2 ``long_forward``):

    cd /home/ubuntu/ai-perpetual-trading
    PYTHONPATH=src python src/report_exit_variants.py --since 2026-10-26T00:00:00+07:00 [--until ISO] [--klines FILE]

``--klines`` is the cache the VPS cron refreshes, ``{"k1": {"<bar open ms>": [open, high, low, close]}}`` for the VST
BTC-USDT pair (default ``~/replay/edge_klines.json``). Trades whose candles are not all in the cache, or whose stop cannot
be derived, are listed and skipped.

The report ends with the pre-registered decision rule as text. It is never an instruction: nothing here enables anything.

Exit codes: 0 ok, 1 expected failure (one ``error:`` line: unusable cache, unmodelled live exits, a failed read), 2 usage
error (a naive time, ``--until`` not after ``--since``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import ValidationError

from bot.config import Settings
from bot.core.forward_summary import MISSING, WIB, format_number, format_percent
from bot.storage.supabase_trade_store import SupabaseTradeStore, parse_timestamp
from research.exit_variants import study
from research.exit_variants.walker import Cols

__all__ = [
    "DEFAULT_KLINES",
    "KlinesError",
    "ReportStore",
    "build_parser",
    "execute",
    "load_klines",
    "main",
    "render_block",
    "render_calibration",
    "render_candidate",
    "render_parameters",
    "render_report",
    "render_rule",
    "render_sample",
    "settings_problem",
    "signed",
]

DEFAULT_KLINES = Path.home() / "replay" / "edge_klines.json"
_DECISION_COLUMNS = "created_at,symbol,action,confidence,payload_json->decision->>stop_loss_pct"
_ERROR_CHARS = 600  # an error line is one line and not a page
_DECISION_END_SLACK = timedelta(seconds=1)  # the window is half open, and a decision may carry the second of the open
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

_SKIP_TEXT = {
    study.SKIP_UNREADABLE: "baris posisi tidak terbaca",
    study.SKIP_SYMBOL: f"bukan {study.TRADED_SYMBOL}",
    study.SKIP_ATR: "tanpa setup.atr_pct",
    study.SKIP_DECISION: "tanpa keputusan OPEN yang cocok (simbol, sisi, keyakinan, 15 menit sebelum buka)",
    study.SKIP_CANDLES: "candle 1m tidak lengkap",
}
_CONVENTION_TEXT = {
    "C+1bp": "bacaan realistis dengan 1 bps slippage tambahan pada keluaran kunci (paling dekat live)",
    "C": "bacaan realistis: kunci yang dipasang sebuah candle berlaku di candle itu bila ia menutup di bawahnya",
    "B": "batas terburuk: kunci dipasang dan kena di candle yang sama (bukan perkiraan)",
}
_SUBSETS = (("Semua posisi", None), ("Posisi tenang", study.calm_subset))
_CLOSING = (
    "Laporan ini tidak mengubah satu pun trade: hanya membaca (Supabase GET dan berkas candle), "
    "tidak menulis apa pun selain layar."
)


class KlinesError(ValueError):
    """The kline cache cannot be used; the message is one line naming the file."""


def _bar(path: Path, key: str, value: Any) -> tuple[int, float, float, float]:
    try:
        if not isinstance(value, list):  # a string such as "1234" would otherwise iterate into four "prices"
            raise TypeError("a bar is a list")
        open_ms = int(key)
        prices = [float(price) for price in value]
    except (TypeError, ValueError) as exc:
        raise KlinesError(f"the kline cache {path} has a bar that is not [open, high, low, close]: {key!r}") from exc
    if len(prices) != 4 or not all(math.isfinite(price) and price > 0 for price in prices):
        raise KlinesError(f"the kline cache {path} has a bar that is not four positive prices: {key!r}")
    return open_ms, prices[0], prices[1], prices[2]


def load_klines(path: Path) -> Cols:
    """The 1m bars of the cache at ``path`` in time order. Raises ``KlinesError`` (one line) for anything unusable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise KlinesError(f"cannot read the kline cache {path}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise KlinesError(f"the kline cache {path} is not valid JSON: {exc}") from exc
    bars = raw.get("k1") if isinstance(raw, dict) else None
    if not isinstance(bars, dict) or not bars:
        raise KlinesError(f'the kline cache {path} has no 1m bars under "k1"')
    rows = sorted(_bar(path, key, value) for key, value in bars.items())
    return Cols([row[0] for row in rows], [row[1] for row in rows], [row[2] for row in rows], [row[3] for row in rows])


def _utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware, got {moment.isoformat()}")
    return moment.astimezone(timezone.utc).isoformat()


class ReportStore(SupabaseTradeStore):
    """The trade store plus the one extra read this report needs, a GET like all of its reads."""

    async def open_decision_rows_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """The model's OPEN decisions made in [start, end), oldest first, paged past the 1000-row cap.

        Only the scalars the study reads come back: the request payload of a decision is tens of kilobytes, so the stop is
        extracted on the server (``payload_json->decision->>stop_loss_pct``, returned under the key ``stop_loss_pct``).
        Scoped to this store's schema and exchange, so each bot reads its own decisions."""
        return await self._page_all(
            self._ai_decisions_table,
            [
                ("select", _DECISION_COLUMNS),
                ("exchange", f"eq.{self._exchange}"),
                ("action", "in.(OPEN_LONG,OPEN_SHORT)"),
                ("created_at", f"gte.{_utc_iso(start)}"),
                ("created_at", f"lt.{_utc_iso(end)}"),
                ("order", "created_at.asc,id.asc"),
            ],
        )


def signed(value: Optional[float], decimals: int) -> str:
    """``value`` in Indonesian notation with a ``+`` on a positive figure, and "–" when missing or not finite."""
    text = format_number(value, decimals)
    if value is None or text == MISSING or text.startswith("-") or float(f"{value:.{decimals}f}") == 0:
        return text
    return "+" + text


def _wib(moment: datetime) -> str:
    return moment.astimezone(WIB).isoformat(timespec="minutes")


def _within(
    rows: Iterable[Mapping[str, Any]], since: datetime, until: datetime
) -> tuple[list[Mapping[str, Any]], int]:
    """Rows closed in [since, until), and how many rows had no readable close time at all."""
    kept: list[Mapping[str, Any]] = []
    unreadable = 0
    for row in rows:
        closed_at = parse_timestamp(row.get("closed_at"))
        if closed_at is None:
            unreadable += 1
        elif since <= closed_at < until:
            kept.append(row)
    return kept, unreadable


def render_header(settings: Any, since: datetime, until: datetime, unreadable: int) -> str:
    lines = [
        "Varian keluar bayangan: selisih berpasangan terhadap aturan live. Hanya analisis; tidak mengubah satu pun trade.",
        f"Skema Supabase: {settings.supabase_schema} | bursa: {settings.exchange_name}",
        f"Dibaca: posisi tertutup sejak {_wib(since)} sampai {_wib(until)} (WIB, akhir tidak termasuk)",
    ]
    if unreadable:
        lines.append(f"PERINGATAN: {unreadable} posisi tanpa waktu tutup yang terbaca dilewati.")
    return "\n".join(lines)


def _skip_order(skip: study.Skip) -> tuple[bool, datetime]:
    """Chronological by open time, whichever stage dropped the trade; a trade with no readable open time goes last."""
    return skip.opened_at is None, skip.opened_at or _EPOCH


def render_sample(result: study.Study) -> str:
    used = len(result.simulated)
    lines = [
        "=== Sampel ===",
        f"posisi tertutup dalam jendela: {result.rows_read}",
        f"dipakai: {used} | dilewati: {len(result.skipped)}",
    ]
    if used:
        opened = [sim.position.opened_at.astimezone(WIB).date().isoformat() for sim in result.simulated]
        lines.append(f"periode posisi dipakai (dibuka, WIB): {min(opened)} sampai {max(opened)}")
    if result.skipped:
        lines.append("dilewati:")
        for skip in sorted(result.skipped, key=_skip_order):
            when = _wib(skip.opened_at) if skip.opened_at is not None else "waktu tak terbaca"
            lines.append(f"  {when}  {skip.side:<6}{_SKIP_TEXT[skip.code]}")
    return "\n".join(lines)


def render_calibration(result: study.Study) -> str:
    lines = ["=== Kalibrasi ==="]
    cal = study.calibration(result.simulated)
    if cal.ratio is None:
        lines.append("belum ada posisi yang disimulasikan")
        return "\n".join(lines)
    percent = format_percent(100.0 * cal.ratio, 1)
    other = sum(1 for sim in result.simulated if sim.position.actual_exit == "")
    lines.append(f"alasan keluar simulasi aturan live sama dengan yang sebenarnya: {cal.matches}/{cal.total} ({percent})")
    lines.append(
        "(neutral_exit dan take_profit dihitung TP, stop_loss SL, time_stop WAKTU; keluar lain, seperti keluar AI, "
        f"tidak pernah cocok: {other} posisi)"
    )
    if cal.ratio < study.MIN_CALIBRATION:
        lines.append(
            f"PERINGATAN: kalibrasi {percent} di bawah {format_percent(100.0 * study.MIN_CALIBRATION, 1)}: simulasi aturan "
            "live belum meniru posisi nyata, jadi selisih di bawah tidak boleh dibaca sebagai efek pada posisi nyata."
        )
    return "\n".join(lines)


def render_parameters(rules: study.LiveRules) -> str:
    costs = study.LIVE_COSTS
    names = [f"{v.name} (utama)" if v.name == study.PRIMARY_VARIANT else v.name for v in study.VARIANTS]
    lines = [
        "=== Parameter (didaftarkan sebelumnya, tidak disetel) ===",
        f"biaya taker {format_number(rules.fee_pct, 4)}% per sisi (TAKER_FEE_PCT) | lantai live net "
        f"+{format_number(rules.floor_net, 0)} USDT | batas waktu {rules.hold_minutes} menit",
        f"slippage keluar pasar {format_number(costs.exit_market_bps, 1)} bps, target {format_number(costs.tp_bps, 1)} bps; "
        "baris C+1bp menambah 1,0 bps pada keluaran kunci",
        f"posisi tenang: setup.atr_pct di bawah {format_number(study.CALM_ATR_PCT, 3)}%",
        "varian: " + ", ".join(names),
        "selisih/posisi = net varian dikurangi net aturan live pada posisi yang sama (USDT, setelah biaya); "
        "positif berarti varian lebih baik",
    ]
    if rules.floor_net != study.REGISTERED_FLOOR_NET:
        lines.append(
            f"PERINGATAN: lantai live net +{format_number(rules.floor_net, 0)} USDT bukan "
            f"+{format_number(study.REGISTERED_FLOOR_NET, 0)} yang dipakai saat varian didaftarkan: angka varian dirancang "
            "untuk lantai itu."
        )
    if rules.venue_fee_rates:
        lines.append(
            "PERINGATAN: USE_EXCHANGE_FEE_RATES=true: bot menghitung net live dengan tarif bursa, laporan ini memakai "
            "TAKER_FEE_PCT, jadi lantai dan hasilnya bisa berbeda dari yang dijalankan bot."
        )
    return "\n".join(lines)


def _interval_text(interval: Optional[tuple[float, float]]) -> str:
    return MISSING if interval is None else f"[{signed(interval[0], 2)}; {signed(interval[1], 2)}]"


def render_block(subset: str, simulated: Sequence[study.Simulated], convention: study.Convention) -> str:
    """One table per subset and convention: a row per variant, then the difference by WIB ISO week."""
    lines = [
        f"=== {subset} | {convention.name}: {_CONVENTION_TEXT[convention.name]} ===",
        f"{'varian':<14}{'n':>5}{'selisih/posisi':>16}{'SE':>9}{'t':>8}{'IK95 hari':>20}{'per 1 juta':>12}{'berubah':>9}",
    ]
    summaries = {v.name: study.summarize(simulated, v.name, convention.name) for v in study.VARIANTS}
    for name, s in summaries.items():
        lines.append(
            f"{name:<14}{s.n:>5}{signed(s.mean, 2):>16}{format_number(s.se, 2):>9}{format_number(s.t, 1):>8}"
            f"{_interval_text(s.interval):>20}{signed(s.per_million, 0):>12}{s.changed:>9}"
        )
    primary = summaries[study.PRIMARY_VARIANT]
    lines.append("selisih rata-rata per pekan ISO (WIB), USDT per posisi:")
    if not primary.weeks:
        lines.append("belum ada posisi")
        return "\n".join(lines)
    lines.append(f"{'pekan':<10}{'n':>5}" + "".join(f"{name:>14}" for name in summaries))
    by_week = {name: {week.week: week.mean for week in s.weeks} for name, s in summaries.items()}
    for week in primary.weeks:
        cells = "".join(f"{signed(by_week[name][week.week], 2):>14}" for name in summaries)
        lines.append(f"{week.week:<10}{week.n:>5}{cells}")
    return "\n".join(lines)


def _criterion_text(criterion: study.Criterion) -> str:
    value, needed = criterion.value, criterion.needed
    if criterion.name == "n":
        return f"jumlah posisi: {value:.0f} (perlu >= {needed:.0f})"
    if criterion.name == "mean":
        return f"rata-rata selisih: {signed(value, 2)} USDT/posisi (perlu >= {signed(needed, 2)})"
    if criterion.name == "t":
        return f"t: {format_number(value, 1)} (perlu >= {format_number(needed, 1)})"
    if criterion.name == "weeks":
        need = f"perlu >= {needed:.0f} dari {study.WEEKS_CONSIDERED}"
        if not criterion.of:
            return f"pekan searah: belum ada pekan ({need})"
        short = f" (baru {criterion.of} pekan)" if criterion.of < study.WEEKS_CONSIDERED else ""
        return f"pekan searah: {value:.0f} dari {criterion.of} pekan terakhir positif ({need}){short}"
    return f"kalibrasi: {format_percent(None if value is None else 100.0 * value, 1)} (perlu >= {format_percent(100.0 * needed, 1)})"


def render_candidate(candidate: study.Candidate, verdict: study.Verdict) -> str:
    """One candidate of the decision rule: its criteria, ticked or open, and the conclusion."""
    claim = f"n >= {candidate.min_trades}, rata-rata >= {signed(candidate.min_mean, 2)} USDT/posisi"
    if candidate.name == study.CALM_POSITIONS.name:
        heading = f"kandidat posisi tenang (setup.atr_pct < {format_number(study.CALM_ATR_PCT, 3)}%; {claim}):"
    else:
        heading = f"kandidat semua posisi ({claim}):"
    lines = [heading]
    lines += [f"  [{'x' if criterion.met else ' '}] {_criterion_text(criterion)}" for criterion in verdict.criteria]
    if verdict.passed:
        lines.append("  -> layak jadi lengan live")
        lines.append("     (hasil aturan, bukan perintah untuk mengaktifkan apa pun: keputusan tetap di tangan manajer)")
    else:
        lines.append("  -> belum cukup")
    return "\n".join(lines)


def render_rule(result: study.Study) -> str:
    ratio = study.calibration(result.simulated).ratio
    lines = [
        "=== Aturan keputusan yang didaftarkan sebelumnya ===",
        f"varian utama: {study.PRIMARY_VARIANT}, dibaca pada konvensi {study.DECISION_CONVENTION}; varian lain hanya "
        "deskriptif (tanpa vonis, karena tidak ada koreksi untuk banyaknya varian yang dicoba)",
    ]
    for candidate in (study.ALL_POSITIONS, study.CALM_POSITIONS):
        pool = list(result.simulated) if candidate is study.ALL_POSITIONS else study.calm_subset(result.simulated)
        summary = study.summarize(pool, study.PRIMARY_VARIANT, study.DECISION_CONVENTION)
        lines.append(render_candidate(candidate, study.evaluate_rule(candidate, summary, ratio)))
    return "\n".join(lines)


def render_report(
    result: study.Study,
    rules: study.LiveRules,
    settings: Any,
    since: datetime,
    until: datetime,
    unreadable: int = 0,
) -> str:
    parts = [
        render_header(settings, since, until, unreadable),
        render_sample(result),
        render_calibration(result),
        render_parameters(rules),
    ]
    for title, pick in _SUBSETS:
        pool = list(result.simulated) if pick is None else pick(result.simulated)
        parts += [render_block(title, pool, convention) for convention in study.CONVENTIONS]
    parts += [render_rule(result), _CLOSING]
    return "\n\n".join(parts)


def settings_problem(exc: ValidationError) -> str:
    """One line naming the settings fields that failed to validate. Never a value: a bad secret would otherwise be echoed."""
    fields = sorted({".".join(str(part) for part in error["loc"]) for error in exc.errors()})
    return "invalid settings in .env or the environment (fields: " + ", ".join(fields) + ")"


def _parse_moment(text: str) -> datetime:
    """An ISO-8601 time; a trailing `Z` means UTC. A time without an offset stays naive so `execute` can refuse it."""
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 time: {text!r}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Shadow report of exit variants: pre-registered exits replayed on the real trades. Changes no trade."
    )
    parser.add_argument(
        "--since", required=True, type=_parse_moment, metavar="ISO",
        help="Start of the window, timezone-aware ISO-8601: closed trades from here on.",
    )
    parser.add_argument(
        "--until", type=_parse_moment, default=None, metavar="ISO",
        help="End of the window, timezone-aware ISO-8601, exclusive (default: now).",
    )
    parser.add_argument(
        "--klines", type=Path, default=DEFAULT_KLINES, metavar="FILE",
        help=f"1m kline cache, JSON {{\"k1\": {{\"<bar open ms>\": [open, high, low, close]}}}} (default: {DEFAULT_KLINES}).",
    )
    return parser


def _error(message: str) -> None:
    print(f"error: {' '.join(str(message).split())[:_ERROR_CHARS]}", file=sys.stderr)


async def _read(
    store: Any, since: datetime, until: datetime
) -> tuple[list[Mapping[str, Any]], int, list[Mapping[str, Any]]]:
    """The trades closed in the window, how many rows had no close time, and the decisions that could have opened them."""
    trades, unreadable = _within(await store.closed_trade_rows_since(since), since, until)
    opened = [moment for moment in (parse_timestamp(row.get("opened_at")) for row in trades) if moment is not None]
    if not opened:
        return trades, unreadable, []
    decisions = await store.open_decision_rows_between(
        min(opened) - study.DECISION_WINDOW, max(opened) + _DECISION_END_SLACK
    )
    return trades, unreadable, decisions


async def execute(args: argparse.Namespace, store: Any, settings: Any) -> int:
    """Run the report against `store`; exit codes: 0 ok, 1 expected failure, 2 usage error."""
    try:
        return await _execute(args, store, settings)
    finally:
        await store.close()


async def _execute(args: argparse.Namespace, store: Any, settings: Any) -> int:
    since = args.since
    until = args.until if args.until is not None else datetime.now(timezone.utc)
    if since.tzinfo is None or until.tzinfo is None:
        _error("--since and --until must carry a timezone (for example +07:00 or Z)")
        return 2
    if until <= since:
        _error(f"--until {until.isoformat()} is not after --since {since.isoformat()}")
        return 2
    try:
        rules = study.live_rules_from_settings(settings)
        cols = load_klines(args.klines.expanduser())
        trades, unreadable, decisions = await _read(store, since, until)
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
        _error(str(exc))
        return 1
    result = study.run_study(trades, decisions, cols, rules)
    print(render_report(result, rules, settings, since, until, unreadable))
    return 0


async def _run(args: argparse.Namespace) -> int:
    try:
        settings = Settings()
    except ValidationError as exc:
        _error(settings_problem(exc))
        return 1
    # Not setup_logging(): it opens logs/bot.log in the working directory, which for a bot's directory is that bot's own
    # rotating log. A report writes nothing but stdout; a plain logger leaves a store warning to Python's stderr default.
    logger = logging.getLogger("report_exit_variants")
    return await execute(args, ReportStore(settings, logger), settings)


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
