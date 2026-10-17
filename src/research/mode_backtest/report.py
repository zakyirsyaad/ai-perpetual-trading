"""Indonesian tables and reports of the mode backtest.

Research code; the bot never imports it. Reports are in Indonesian; code and repo docs stay in
English. Every table writes numbers in Indonesian notation: a decimal comma and a dot between
thousands (``1.234,56``). A missing or non-finite value is "–", as in the Pine report. Menang is a
percentage with one decimal, the mean gross return in bps has two decimals and Hasil per 1 juta is
a whole number of USDT.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, List, Sequence

from research.pine_gate.report_format import NA, as_number
from research.pine_gate.spec import ExecConfig

if TYPE_CHECKING:  # pragma: no cover - typing only; step0 imports this module
    from .step0 import Step0Row

# Python's "1,234.56" becomes "1.234,56".
_INDONESIAN = str.maketrans({",": ".", ".": ","})

STEP0_HEADER = (
    "Kontender", "Koin", "Arah", "Kondisi", "Posisi", "Menang", "Rata-rata bps",
    "Hasil per 1 juta",
)


def format_number(value: Any, decimals: int) -> str:
    """``value`` rounded to ``decimals`` places in Indonesian notation; "–" when it is missing,
    not a number (a bool is not one) or not finite. A value that rounds to zero has no sign."""
    number = as_number(value)
    if number is None:
        return NA
    text = f"{number:,.{decimals}f}"
    if text.startswith("-") and float(text[1:].replace(",", "")) == 0:
        text = text[1:]  # never "-0" or "-0,00"
    return text.translate(_INDONESIAN)


def format_percent(value: Any, decimals: int = 1) -> str:
    """A percentage such as "48,1%"; "–" when missing."""
    text = format_number(value, decimals)
    return text if text == NA else f"{text}%"


def _coin(symbol: str) -> str:
    """``BTC-USDT`` -> ``BTC``."""
    return symbol.split("-", 1)[0]


def _table_line(cells: Sequence[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def step0_table(rows: Sequence["Step0Row"]) -> str:
    """The step-0 table: one line per contender, coin, side and condition, in the rows' order."""
    lines: List[str] = [_table_line(STEP0_HEADER), "|" + "---|" * len(STEP0_HEADER)]
    for row in rows:
        lines.append(_table_line((
            row.contender,
            _coin(row.symbol),
            row.sisi,
            row.kondisi,
            format_number(row.posisi, 0),
            format_percent(row.menang_pct, 1),
            format_number(row.gross_bps, 2),
            format_number(row.hasil_per_1_juta, 0),
        )))
    return "\n".join(lines) + "\n"


def step0_markdown(
    rows: Sequence["Step0Row"],
    *,
    holdout_file: str,
    first_day: date,
    last_day: date,
    seed: int,
    exec_cfg: ExecConfig,
) -> str:
    """The step-0 report: what was replayed, the caveat of spec section 8 and the table.

    ``first_day`` and ``last_day`` are the holdout's first and last WIB days. The bracket, the
    time stop and the notional come from ``exec_cfg``, the execution the Pine run used."""
    bracket = format_percent(exec_cfg.bracket_pct, 2)
    notional = format_number(exec_cfg.notional_usdt, 0)
    return "\n".join([
        "# Langkah 0: holdout Pine per arah dan kondisi",
        "",
        f"Posisi holdout Pine Fase 1 ({first_day.isoformat()} sampai {last_day.isoformat()} WIB) "
        f"diputar ulang dengan seed {seed} dari `{holdout_file}`. Setiap angka `symbol_metrics` "
        "hasil putar ulang sama persis dengan file itu, per kontender dan koin. Setiap posisi "
        "lalu dibagi menurut arahnya dan menurut kondisi pemilih mode (NAIK, TURUN, SIDEWAYS, "
        "TIDAK_ENTRY) pada waktu sinyalnya.",
        "",
        f"Catatan: posisi ini memakai bracket Pine (TP {bracket} dan SL {bracket}, batas waktu "
        f"{exec_cfg.time_stop_min} menit), jam tick dengan latensi AI dan penjaga harian Pine, "
        "bukan exit mode baru. Langkah 0 hanya membaca posisi yang sudah ada: tidak ada aturan "
        "yang diuji dan tidak ada keputusan yang berubah.",
        "",
        step0_table(rows),
        "Menang = TP / (TP + SL): exit karena batas waktu tidak dihitung, dan \"–\" berarti tidak "
        "ada TP maupun SL. Rata-rata bps = rata-rata hasil kotor per posisi, sebelum fee. Hasil "
        "per 1 juta = hasil bersih setelah fee per 1 juta USDT volume (setiap posisi 2 × "
        f"{notional} USDT).",
        "",
    ])
