"""The rescore stage: Mode A's trade logs repriced with the costs of the live-exit spec.

Research code; the bot never imports it. Spec section 8: the existing trade logs of Mode A and of
the Phase 1 comparator are repriced with the costs of section 5, keeping each trade's exit event
(its reason, its entry and exit times and its side). The result is labelled a cost sensitivity,
never a pass or a fail. The stage reads the trade-log CSVs it is given and the two configuration
files; it opens no bar file and writes through ``research.pine_gate.fileio`` only, so an output is
never overwritten.

How a row is repriced. A Phase 1 log holds the fills, which carry the old slippage
(``umum.slippage_persen``, applied by ``adverse_fill``): the entry fill is ``open x (1 + side x
old/100)`` and the exit fill is ``raw x (1 - side x old/100)`` for every kind of exit. Inverting
those gives the raw entry open and the raw exit:

* ``raw_entry = harga_entry / (1 + side x old/100)``;
* ``raw_exit = harga_exit / (1 - side x old/100)``.

The raw exit is recovered from the fill, never taken from ``harga_sl`` or ``harga_tp``: a stop
that gapped was filled at the bar's open, not at its level, and the inverse gives that open (a
touch gives the level). The new money is then ``settle_live`` on those numbers with the row's own
exit reason and notional, so the formulas of spec section 5 live in one place.

A row whose own ``fee_usdt``, ``pnl_usdt`` and ``slippage_usdt`` the config's fee and old slippage do not reproduce
(``simulate.settle`` on the recovered raw prices) is refused, naming the trade: the log was priced
with other costs and the inversion would be wrong.

Only the 23 columns of a Phase 1 log are accepted. A mode L log already carries the new costs,
and repricing it again would count them twice, so a log with any other columns is refused.

The summary per log gives the old and the new result per 1M of volume and per 1M without the
``uji.lolos_buang_posisi_teratas`` best positions, with ``metrics.stage_metrics`` (the Phase 1
helper) on both. The old numbers are the log's own ``pnl_usdt`` and ``volume_usdt``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Union

from bot.strategy.modes.config import (
    CostConfig,
    load_live_exit_config_with_sha256,
    load_modes_config_with_sha256,
)
from bot.strategy.modes.position_rules import LONG, SHORT
from research.pine_gate.fileio import write_exclusive, write_json_exclusive
from research.pine_gate.spec import WIB

from .frames import HourlyContext
from .live_sim import settle_live
from .metrics import StageMetrics, stage_metrics
from .report import format_number
from .simulate import EXIT_POTONG, EXIT_SL, EXIT_TP, EXIT_WAKTU, ExitOutcome, Trade, settle
from .stages import refuse_existing, stamp
from .tradelog import COLUMNS

__all__ = ["LABEL", "RescoredTrade", "rescore_rows", "rescore_stage"]

PathLike = Union[str, Path]

LABEL = "sensitivitas biaya, bukan uji"
_SIDES = {"LONG": LONG, "SHORT": SHORT}
_REASONS = (EXIT_TP, EXIT_SL, EXIT_WAKTU, EXIT_POTONG)
_NO_CONTEXT = HourlyContext(None, None, None, None, None, None, "")
_PERCENT = 100.0
_MONEY_REL_TOL = 1e-9
_MONEY_ABS_TOL = 1e-6


@dataclass(frozen=True)
class RescoredTrade:
    """One logged trade and the same trade repriced.

    ``old`` is the trade as the log holds it and ``new`` the same exit event with the new costs
    (same side, reason, times and levels; new fills, quantity, fee, slippage, profit and volume).
    ``raw_entry_open`` and ``raw_exit`` are the prices before any slippage, recovered from the
    log's fills."""

    old: Trade
    new: Trade
    raw_entry_open: float
    raw_exit: float


def _number(row: Mapping[str, str], name: str, n: int) -> float:
    try:
        value = float(row[name])
    except KeyError:
        raise ValueError(f"trade {n}: the row has no {name} column") from None
    except (TypeError, ValueError):
        raise ValueError(f"trade {n}: {name} must be a number, got {row[name]!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"trade {n}: {name} must be finite, got {row[name]!r}")
    return value


def _time_ms(row: Mapping[str, str], name: str, n: int) -> int:
    try:
        text = row[name]
    except KeyError:
        raise ValueError(f"trade {n}: the row has no {name} column") from None
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise ValueError(f"trade {n}: {name} must be an ISO 8601 time, got {text!r}") from None
    if moment.tzinfo is None:
        raise ValueError(f"trade {n}: {name} must carry a UTC offset, got {text!r}")
    return int(round(moment.timestamp() * 1000))


def _old_trade(row: Mapping[str, str], n: int) -> Trade:
    sisi = row.get("sisi")
    if sisi not in _SIDES:
        raise ValueError(f"trade {n}: sisi must be LONG or SHORT, got {sisi!r}")
    reason = row.get("alasan_exit")
    if reason not in _REASONS:
        raise ValueError(
            f"trade {n}: alasan_exit must be one of {', '.join(_REASONS)}, got {reason!r}"
        )
    entry_fill = _number(row, "harga_entry", n)
    exit_fill = _number(row, "harga_exit", n)
    notional = _number(row, "notional_usdt", n)
    for name, value in (("harga_entry", entry_fill), ("harga_exit", exit_fill),
                        ("notional_usdt", notional)):
        if value <= 0:
            raise ValueError(f"trade {n}: {name} must be above 0, got {value!r}")
    return Trade(
        mode=row.get("mode", ""), pair=row.get("pair", ""), side=_SIDES[sisi],
        kondisi=row.get("kondisi", ""), context=_NO_CONTEXT, faktor_k=0.0,
        signal_ms=_time_ms(row, "waktu_sinyal", n), entry_ms=_time_ms(row, "waktu_entry", n),
        entry_fill=entry_fill, tp=_number(row, "harga_tp", n), sl=_number(row, "harga_sl", n),
        exit_ms=_time_ms(row, "waktu_exit", n), exit_fill=exit_fill, exit_reason=reason,
        gapped=False, both_touched=False, qty=notional / entry_fill, notional_usdt=notional,
        fee_usdt=_number(row, "fee_usdt", n), slippage_usdt=_number(row, "slippage_usdt", n),
        pnl_usdt=_number(row, "pnl_usdt", n), volume_usdt=_number(row, "volume_usdt", n),
    )


def _check_old_money(old: Trade, raw_entry: float, raw_exit: float, fee_pct: float,
                     old_slippage_pct: float) -> None:
    """Refuse a row whose own fee, profit and slippage the given fee and slippage do not reproduce.

    The slippage column is part of the check because ``settle`` on prices recovered with a wrong
    slippage returns the logged fills back, so fee and profit alone would agree anyway."""
    money = settle(raw_entry, raw_exit, old.side, old.notional_usdt, fee_pct, old_slippage_pct)
    for name, logged, recomputed in (("fee_usdt", old.fee_usdt, money.fee_usdt),
                                     ("pnl_usdt", old.pnl_usdt, money.pnl_usdt),
                                     ("slippage_usdt", old.slippage_usdt, money.slippage_usdt)):
        if not math.isclose(logged, recomputed, rel_tol=_MONEY_REL_TOL, abs_tol=_MONEY_ABS_TOL):
            raise ValueError(
                f"the log's {name} {logged!r} is not what the fee {fee_pct}% and the slippage "
                f"{old_slippage_pct}% of the config give ({recomputed!r}); the log was priced "
                "with other costs"
            )


def _reprice(old: Trade, costs: CostConfig, fee_pct: float, old_slippage_pct: float):
    """The new ``Trade`` of ``old`` with the raw prices recovered from its fills."""
    side = old.side
    raw_entry = old.entry_fill / (1 + side * old_slippage_pct / _PERCENT)
    raw_exit = old.exit_fill / (1 - side * old_slippage_pct / _PERCENT)
    _check_old_money(old, raw_entry, raw_exit, fee_pct, old_slippage_pct)
    outcome = ExitOutcome(old.exit_reason, 0, raw_exit, False, False)
    money = settle_live(raw_entry, outcome, side, old.notional_usdt, fee_pct, costs)
    new = replace(
        old, entry_fill=money.entry_fill, exit_fill=money.exit_fill, qty=money.qty,
        fee_usdt=money.fee_usdt, slippage_usdt=money.slippage_usdt, pnl_usdt=money.pnl_usdt,
        volume_usdt=money.volume_usdt,
    )
    return new, raw_entry, raw_exit


def rescore_rows(
    rows: Iterable[Mapping[str, str]],
    costs: CostConfig,
    fee_pct: float,
    old_slippage_pct: float,
) -> List[RescoredTrade]:
    """Reprice the rows of a Phase 1 trade log, in the order given.

    ``fee_pct`` is the taker fee in percent (``umum.fee_taker_persen``) and ``old_slippage_pct``
    the slippage the log was priced with (``umum.slippage_persen``). Each exit event is kept.
    Raises ``ValueError`` (naming the trade by its position) for a row with a missing or
    malformed cell, an unknown side or exit reason, or a fill or notional that is not above 0."""
    results = []
    for n, row in enumerate(rows, start=1):
        old = _old_trade(row, n)
        try:
            new, raw_entry, raw_exit = _reprice(old, costs, fee_pct, old_slippage_pct)
        except ValueError as exc:
            raise ValueError(f"trade {n}: {exc}") from None
        results.append(RescoredTrade(old, new, raw_entry, raw_exit))
    return results


def _read_log(path: Path) -> tuple[List[Dict[str, str]], str]:
    """The rows of the CSV at ``path`` and the sha256 of the bytes they were read from."""
    data = Path(path).read_bytes()
    text = data.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != list(COLUMNS):
        raise ValueError(
            f"{path}: the columns are not the 23 columns of a Phase 1 trade log; "
            f"got {reader.fieldnames!r}"
        )
    return list(reader), hashlib.sha256(data).hexdigest()


def _metrics_doc(m: StageMetrics, trades: Sequence[Trade]) -> Dict[str, Any]:
    return {
        "posisi": m.posisi,
        "tp": m.tp, "sl": m.sl, "waktu": m.waktu, "potong": m.potong,
        "menang_pct": m.menang_pct,
        "hasil_per_1_juta": m.hasil_per_1_juta,
        "hasil_tanpa_teratas": m.hasil_tanpa_teratas,
        "pnl_usdt": math.fsum(t.pnl_usdt for t in trades),
        "volume_usdt": math.fsum(t.volume_usdt for t in trades),
        "fee_usdt": math.fsum(t.fee_usdt for t in trades),
    }


def _log_doc(path: Path, rows: Sequence[Mapping[str, str]], sha: str, costs: CostConfig,
             fee_pct: float, old_pct: float, drop_top: int) -> Dict[str, Any]:
    results = rescore_rows(rows, costs, fee_pct, old_pct)
    old = [r.old for r in results]
    new = [r.new for r in results]
    return {
        "path": str(path),
        "sha256": sha,
        "old": _metrics_doc(stage_metrics(old, 0, drop_top), old),
        "new": _metrics_doc(stage_metrics(new, 0, drop_top), new),
    }


def _delta(new: Any, old: Any) -> Any:
    return None if new is None or old is None else new - old


def _report(doc: Mapping[str, Any]) -> str:
    drop = doc["buang_posisi_teratas"]
    costs = doc["biaya"]
    lines = [
        "# Rescore: log Mode A dengan biaya baru",
        "",
        f"**{LABEL.capitalize()}.** Ini bukan uji: tidak ada lolos atau gagal. Setiap transaksi "
        "keluar pada kejadian yang sama seperti di lognya (alasan, waktu, sisi); hanya biayanya "
        "yang dihitung ulang.",
        "",
        "Biaya baru (spec bagian 5): slippage entry "
        f"{format_number(costs['slippage_entry_bps'], 2)} bps, exit pasar "
        f"{format_number(costs['slippage_exit_market_bps'], 2)} bps, TP "
        f"{format_number(costs['slippage_tp_bps'], 2)} bps; fee "
        f"{format_number(doc['fee_persen'], 3)}% per sisi. Biaya lama: slippage "
        f"{format_number(doc['slippage_lama_persen'], 3)}%. Hanya log yang diberikan yang dibaca; "
        "tidak ada data candle yang dibuka.",
        "",
        f"| Log | Posisi | Hasil per 1 juta lama | Hasil per 1 juta baru | Selisih | "
        f"Tanpa {drop} teratas lama | Tanpa {drop} teratas baru | Selisih |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for entry in doc["logs"]:
        old, new = entry["old"], entry["new"]
        lines.append(
            f"| {Path(entry['path']).name} | {new['posisi']} "
            f"| {format_number(old['hasil_per_1_juta'], 2)} "
            f"| {format_number(new['hasil_per_1_juta'], 2)} "
            f"| {format_number(_delta(new['hasil_per_1_juta'], old['hasil_per_1_juta']), 2)} "
            f"| {format_number(old['hasil_tanpa_teratas'], 2)} "
            f"| {format_number(new['hasil_tanpa_teratas'], 2)} "
            f"| {format_number(_delta(new['hasil_tanpa_teratas'], old['hasil_tanpa_teratas']), 2)} |"
        )
    lines += ["", "Satuan: USDT per 1 juta USDT volume.", ""]
    return "\n".join(lines)


def rescore_stage(
    trade_logs: Sequence[PathLike],
    live_config_path: PathLike,
    config_path: PathLike,
    out_dir: PathLike,
    now: datetime,
) -> Path:
    """Reprice each log of ``trade_logs`` and write ``rescore_<YYYYMMDD>_<HHMMSS>.json`` and
    ``.md`` (the WIB time of ``now``) in ``out_dir``; returns the JSON path.

    The costs come from ``live_config_path`` (``biaya``); the fee, the old slippage and the number
    of best positions to leave out come from ``config_path``. Everything is computed before
    anything is written, and an existing output (the same second) raises ``FileExistsError`` before the first write.
    Raises ``ValueError`` for no log, a log whose columns are not Phase 1's or whose row cannot be
    priced, and a naive ``now``."""
    if not trade_logs:
        raise ValueError("rescore needs at least one trade log")
    day = stamp(now)
    live, live_sha = load_live_exit_config_with_sha256(Path(live_config_path))
    cfg, cfg_sha = load_modes_config_with_sha256(Path(config_path))
    fee_pct, old_pct = cfg.umum.fee_taker_persen, cfg.umum.slippage_persen
    drop_top = cfg.uji.lolos_buang_posisi_teratas
    logs = []
    for path in trade_logs:
        rows, sha = _read_log(Path(path))
        logs.append(_log_doc(Path(path), rows, sha, live.biaya, fee_pct, old_pct, drop_top))
    out = Path(out_dir)
    json_path = out / f"rescore_{day}_{now.astimezone(WIB).strftime('%H%M%S')}.json"
    md_path = json_path.with_suffix(".md")
    refuse_existing([json_path, md_path])
    doc = {
        "label": LABEL,
        "tanggal": day,
        "biaya": asdict(live.biaya),
        "fee_persen": fee_pct,
        "slippage_lama_persen": old_pct,
        "buang_posisi_teratas": drop_top,
        "config_sha256": cfg_sha,
        "live_config_sha256": live_sha,
        "logs": logs,
        "files": [md_path.name],
    }
    write_exclusive(md_path, _report(doc), "rescore report")
    write_json_exclusive(json_path, doc, "rescore result")
    return json_path
