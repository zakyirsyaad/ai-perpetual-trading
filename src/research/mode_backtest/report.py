"""Indonesian tables and reports of the mode backtest.

Research code; the bot never imports it. Reports are in Indonesian; code and repo docs stay in
English. Every table writes numbers in Indonesian notation: a decimal comma and a dot between
thousands (``1.234,56``). A missing or non-finite value is "–", as in the Pine report. Menang is a
percentage with one decimal, the mean gross return in bps has two decimals and Hasil per 1 juta is
a whole number of USDT.

The Mode A table (``mode_table``, spec section 2.9) follows the same conventions: Entry per hari
aktif has two decimals, Keluar TP / SL / waktu is three counts, and Rata-rata lama posisi is the
mean hold in minutes with one decimal. The table rounds for reading; the exact numbers stay in
``StageMetrics``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Sequence

from research.pine_gate.report_format import NA, as_number
from research.pine_gate.spec import ExecConfig

from bot.strategy.modes.config import TestConfig

from .metrics import StageMetrics, pass_status

if TYPE_CHECKING:  # pragma: no cover - typing only; step0 imports this module
    from .step0 import Step0Row

# Python's "1,234.56" becomes "1.234,56".
_INDONESIAN = str.maketrans({",": ".", ".": ","})

STEP0_HEADER = (
    "Kontender", "Koin", "Arah", "Kondisi", "Posisi", "Menang", "Rata-rata bps",
    "Hasil per 1 juta",
)

# The header of the results table in spec section 2.9, cell for cell.
MODE_HEADER = (
    "Mode", "Pair", "Tahap", "Posisi", "Entry per hari aktif", "Menang", "Hasil per 1 juta",
    "Keluar TP / SL / waktu", "Rata-rata lama posisi",
)
NO_TRADING = "Tidak trading"  # the comparator that never opens a position

# The results table of mode L and comparator S (spec section 6): Mode A's, with the exit version,
# the entries per calendar day and the cut as a fourth kind of exit.
LIVE_HEADER = (
    "Mode", "Versi", "Pair", "Tahap", "Posisi", "Entry per hari aktif",
    "Entry per hari kalender", "Menang", "Hasil per 1 juta",
    "Keluar TP / SL / potong / waktu", "Rata-rata lama posisi",
)


def exact(value: Optional[float]) -> str:
    """A number as the rules compare it: ``repr`` of a float, a whole number as it is; "–" for
    None."""
    if value is None:
        return NA
    return str(value) if isinstance(value, int) else repr(float(value))


def status_figures(m: StageMetrics, uji: TestConfig) -> str:
    """The figures of a test status line, the part after the verdict: the positions against the
    minimum, then both results against their pass limits, exactly as the rule compares them."""
    return (
        f"Posisi {m.posisi} (minimal {uji.lolos_minimal_posisi}); Hasil per 1 juta = "
        f"{exact(m.hasil_per_1_juta)} (lolos bila > {exact(uji.lolos_hasil_per_1_juta_lebih_dari)}); "
        f"tanpa {uji.lolos_buang_posisi_teratas} posisi teratas = {exact(m.hasil_tanpa_teratas)} "
        f"(lolos bila > {exact(uji.lolos_hasil_tanpa_teratas_lebih_dari)})."
    )


def live_status_line(
    versi: str, m: StageMetrics, uji: TestConfig, pair: str, mode: str = "L"
) -> str:
    """The status line of one live-exit version over the test stage, e.g.
    "Ujian mode L versi ii BTC-USDT: GAGAL. Posisi 40 (minimal 40); ...".

    The verdict is ``pass_status(m, uji)``, so it is the Phase 1 pass rule applied to this
    version's own metrics; ``mode`` is "L" or the comparator "S"."""
    return f"Ujian mode {mode} versi {versi} {pair}: {pass_status(m, uji)}. {status_figures(m, uji)}"


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


@dataclass(frozen=True)
class ReportRow:
    """One line of the Mode A table.

    ``mode`` is the label shown in the Mode column ("A", or the comparator's own name), ``pair``
    the pair as the config names it (``BTC-USDT``), ``tahap`` the stage ("Latihan" or "Ujian") and
    ``metrics`` the numbers of that run over that stage."""

    mode: str
    pair: str
    tahap: str
    metrics: StageMetrics


def _minutes(value: Any) -> str:
    """A hold such as "45,5 menit"; "–" when missing."""
    text = format_number(value, 1)
    return text if text == NA else f"{text} menit"


def _mode_line(mode: str, pair: str, tahap: str, m: StageMetrics) -> str:
    return _table_line((
        mode,
        pair,
        tahap,
        format_number(m.posisi, 0),
        format_number(m.entry_per_hari_aktif, 2),
        format_percent(m.menang_pct, 1),
        format_number(m.hasil_per_1_juta, 0),
        " / ".join(format_number(count, 0) for count in (m.tp, m.sl, m.waktu)),
        _minutes(m.rata_lama_menit),
    ))


@dataclass(frozen=True)
class LiveRow:
    """One line of the mode L table: a ``ReportRow`` with the exit version ("i" or "ii")."""

    mode: str
    versi: str
    pair: str
    tahap: str
    metrics: StageMetrics


def live_table(rows: Iterable[LiveRow]) -> str:
    """The results table of mode L and comparator S: one line per row, in the rows' order.

    Hasil per 1 juta is a whole number, the entry rates have two decimals, Menang one decimal,
    and the hold is in minutes; a missing value is "–". An empty ``rows`` gives the header alone.
    The table rounds for reading; the exact numbers stay in ``StageMetrics``."""
    lines: List[str] = [_table_line(LIVE_HEADER), "|" + "---|" * len(LIVE_HEADER)]
    for row in rows:
        m = row.metrics
        lines.append(_table_line((
            row.mode,
            row.versi,
            row.pair,
            row.tahap,
            format_number(m.posisi, 0),
            format_number(m.entry_per_hari_aktif, 2),
            format_number(m.entry_per_hari_kalender, 2),
            format_percent(m.menang_pct, 1),
            format_number(m.hasil_per_1_juta, 0),
            " / ".join(format_number(count, 0) for count in (m.tp, m.sl, m.potong, m.waktu)),
            _minutes(m.rata_lama_menit),
        )))
    return "\n".join(lines) + "\n"


def _no_trading(hari_naik: int) -> StageMetrics:
    """The comparator that never trades: no position and a result of exactly 0.

    It has no TP or SL, so Menang is missing, and no hold. Its entries per NAIK day are 0 when the
    stage has a NAIK day and missing when it has none, as for every other row."""
    return StageMetrics(
        posisi=0,
        entry_per_hari_aktif=0.0 if hari_naik > 0 else None,
        menang_pct=None,
        hasil_per_1_juta=0.0,
        tp=0,
        sl=0,
        waktu=0,
        rata_lama_menit=None,
        hasil_tanpa_teratas=None,
        hari_naik=hari_naik,
    )


def mode_table(rows: Iterable[ReportRow]) -> str:
    """The results table of spec section 2.9: one line per row, in the rows' order.

    After the last row of each tahap and pair (the comparators come last) the table adds a
    "Tidak trading" row, the comparator that never opens a position: 0 positions and a result of
    0. Its NAIK days are those of the row it follows, since every row of a tahap covers the same
    NAIK days. An empty ``rows`` gives the header alone."""
    rows = tuple(rows)  # read twice below; an iterator would be spent by the first pass
    last = {(row.tahap, row.pair): index for index, row in enumerate(rows)}
    lines: List[str] = [_table_line(MODE_HEADER), "|" + "---|" * len(MODE_HEADER)]
    for index, row in enumerate(rows):
        lines.append(_mode_line(row.mode, row.pair, row.tahap, row.metrics))
        if last[(row.tahap, row.pair)] == index:
            baseline = _no_trading(row.metrics.hari_naik)
            lines.append(_mode_line(NO_TRADING, row.pair, row.tahap, baseline))
    return "\n".join(lines) + "\n"
