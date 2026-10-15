"""The check sections of the Indonesian report: VST vs production (spec 5.12) and
the AI decision buckets (5.13), with the provenance check that skips an output
not scored on this report's contender and selection file.

Research code; the bot never imports it. Standard library only.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .checks import OUTCOME_AGREE_MIN, SIDE_AGREE_MIN, WR_DIFF_MAX_PTS
from .report_format import (
    NA,
    Lines,
    dig,
    contender_label,
    as_mapping,
    net_or_none,
    as_number,
    md_table,
    unavailable,
    yes_no,
    fmt_bps,
    fmt_dec,
    fmt_int,
    fmt_money,
    fmt_points,
    fmt_share,
    fmt_wr,
)

# The counts behind each VST share: ticks where either side signals, where both
# signal the same side, and each side's signal ticks with a resolved outcome.
VST_COUNT_KEYS = ("n_either", "n_both", "n_prod", "n_vst")


def _provenance_problem(doc: Any, results: Mapping[str, Any]) -> Optional[str]:
    """Why a check output does not belong to this report's holdout, else ``None``.

    vst-check and decisions record the ``contender`` they scored and the
    ``selection_sha256`` they were run under; both must match the holdout's chosen
    contender and the selection file of this report (spec 5.12, 5.13)."""
    chosen, contender = dig(results.get("holdout"), "chosen"), dig(doc, "contender")
    if contender is None or contender != chosen:
        return (f"kontender {contender or NA} bukan kontender pilihan periode uji "
                f"({chosen or NA})")
    selection_sha = dig(results.get("sha256"), "selection")
    if not selection_sha or dig(doc, "selection_sha256") != selection_sha:
        return "sha256 file seleksi berbeda dari file seleksi laporan ini"
    return None


def _checked_vst(results: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """VST outputs that belong to this report, and ``{symbol: reason}`` for the rest."""
    kept: Dict[str, Any] = {}
    skipped: Dict[str, str] = {}
    for symbol, doc in sorted(as_mapping(results.get("vst")).items()):
        problem = _provenance_problem(doc, results)
        if problem is None:
            kept[symbol] = doc
        else:
            skipped[symbol] = problem
    return kept, skipped


def _decisions_problem(results: Mapping[str, Any]) -> Optional[str]:
    doc = results.get("decisions")
    return None if doc is None else _provenance_problem(doc, results)


def provenance_warning(results: Mapping[str, Any]) -> Lines:
    reasons = [f"VST {symbol}: {why}" for symbol, why in _checked_vst(results)[1].items()]
    problem = _decisions_problem(results)
    if problem is not None:
        reasons.append(f"Keputusan AI: {problem}")
    if not reasons:
        return []
    return [f"**PERINGATAN: hasil pemeriksaan dilewati** ({'; '.join(reasons)}). Bagian itu "
            "tidak ditampilkan karena tidak berasal dari kontender dan file seleksi periode "
            "uji ini.", ""]


def _skipped_lines(reasons: Sequence[str]) -> Lines:
    return [f"Hasil dilewati — {why}." for why in reasons] + [""] if reasons else []


def vst_section(results: Mapping[str, Any]) -> Lines:
    kept, skipped = _checked_vst(results)
    rows = []
    for symbol, a in kept.items():
        diff = None
        if as_number(dig(a, "wr_prod")) is not None and as_number(dig(a, "wr_vst")) is not None:
            diff = abs(a["wr_prod"] - a["wr_vst"])
        rows.append([contender_label(str(dig(a, "contender"))), symbol,
                     *(fmt_int(dig(a, key)) for key in VST_COUNT_KEYS),
                     fmt_share(dig(a, "side_agree")), fmt_share(dig(a, "outcome_agree")),
                     fmt_wr(dig(a, "wr_prod")), fmt_wr(dig(a, "wr_vst")),
                     fmt_points(diff, near=(WR_DIFF_MAX_PTS,)), yes_no(dig(a, "passed"))])
    skipped_lines = _skipped_lines([f"VST {s}: {why}" for s, why in skipped.items()])
    if not rows:
        return skipped_lines + unavailable()
    note = [f"Ambang (spec 5.12): arah sinyal sama ≥ {SIDE_AGREE_MIN:.0%} dari tick saat salah "
            f"satu sisi bersinyal, hasil bracket sama ≥ {OUTCOME_AGREE_MIN:.0%} dari tick saat "
            f"keduanya bersinyal searah, selisih WR ≤ {WR_DIFF_MAX_PTS:.0f} poin (WR tiap sisi "
            "pada tick sinyalnya sendiri). Tick saat kedua sisi datar (tanpa sinyal) tidak "
            "dihitung. Jika gagal, pembacaan maju di VST yang menentukan.",
            ""]
    headers = ["Kontender", "Simbol", "tick salah satu sinyal", "tick sinyal searah",
               "sinyal produksi", "sinyal VST", "arah sama", "hasil sama", "WR produksi",
               "WR VST", "selisih WR (poin)", "lulus"]
    return skipped_lines + note + md_table(headers, rows)


BUCKET_NOTES = {
    "A": "semua kandidat AI (OPEN, keyakinan ≥ 0.75, tinjauan risiko disetujui)",
    "B": "kandidat AI dengan sinyal aturan searah",
    "E": "ada sinyal aturan, AI menjawab HOLD",
    "F": "semua sinyal pada waktu tick tersimpan",
}


def decisions_section(results: Mapping[str, Any]) -> Lines:
    problem = _decisions_problem(results)
    if problem is not None:
        return _skipped_lines([f"keputusan AI: {problem}"]) + unavailable()
    doc = results.get("decisions")
    contender = contender_label(str(dig(doc, "contender")))
    rows = []
    for key in ("A", "B", "E", "F"):
        b = as_mapping(dig(doc, "buckets", key))
        if b:
            rows.append([contender, key, BUCKET_NOTES[key], fmt_int(b.get("n")),
                         fmt_int(b.get("tp")), fmt_int(b.get("sl")), fmt_int(b.get("time")),
                         fmt_wr(b.get("wr")), fmt_bps(b.get("gross")),
                         fmt_money(net_or_none(b.get("gross"))), fmt_dec(b.get("trades_per_day"))])
    if not rows:
        return unavailable()
    headers = ["Kontender", "Bucket", "Definisi", "n", "TP", "SL", "time", "WR", "gross bps",
               "net/1M", "transaksi/hari (belum dikoreksi)"]
    return ["Keputusan AI BTC tersimpan, dengan bracket 0.20% / 0.20%. Transaksi/hari belum "
            "dikoreksi untuk tick saat posisi terbuka (spec 5.13).", ""] + md_table(headers, rows)
