"""The Indonesian report for the manager (spec 5.14, 5.9, 5.11 last paragraph).

Research code; the bot never imports it. Standard library only.

``render_markdown`` turns the selection file (train stage), the holdout file
(test stage) and the check outputs (VST agreement, AI decision buckets) into a
Markdown report; ``write_report`` writes it next to a JSON copy. Code, names and
tests are English; only the rendered text is Indonesian. Any missing part renders
as "belum tersedia" and never crashes the report.

The report is deliberately honest: passing the gate means "an edge is present and
consistent with the target", not "WR 60% proven" (spec 5.11). ``verdict_sentence``,
the paragraph the manager reads, is the user's own contribution.

Number formats: WR with one decimal and "%", bps with two decimals, net per 1M as
an integer, a missing or non-finite value as "–". Net per 1M is always shown next
to WR: ``net/1M = 50 * (gross bps - 7)`` at 0.035% per side (spec section 4).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .checks import OUTCOME_AGREE_MIN, SIDE_AGREE_MIN, WR_DIFF_MAX_PTS
from .fileio import write_exclusive
from .holdout_gate import C0_CENTRE_WR, C0_GROSS_TOLERANCE_BPS, C0_WR_TOLERANCE_PTS
from .pipeline import REGIME_MIN_N, SYMBOLS
from .spec import WIB, GateConfig
from .stats import net_per_1m

REPORT_PREFIX = "pine_gate_edge_"
NA = "–"
NOT_AVAILABLE = "belum tersedia"
LIVE_BASELINE_PER_1M = -190  # spec section 2, "Money"
MIN_TP_SL = GateConfig().min_tp_sl
TOP_RULES = 5

SECTION_TITLES: Tuple[str, ...] = (
    "Ringkasan",
    "Integritas data dan sha256",
    "Langkah 0 (sebaran rezim)",
    "Tahap latih (grid, 5 teratas, PBO, SPA)",
    "Model meta-labeling (log-loss, kalibrasi)",
    "Periode uji per kontender dan simbol",
    "Gerbang (kriteria 1–9)",
    "Kontrol dan permutasi",
    "VST vs produksi",
    "Keputusan AI (bucket A/B/E/F)",
    "Biaya kadens dan guard",
    "Kesimpulan",
)
SHA_KEYS: Tuple[str, ...] = ("spec", "data", "data_holdout", "selection", "holdout", "code")
CONTENDER_LABELS = {
    "c1": "Kontender 1 (struktur Pine)",
    "c2": "Kontender 2 (logika bot)",
    "c3": "Kontender 3 (meta-labeling)",
}
CRITERIA_LEGEND: Tuple[str, ...] = (
    "Minimal 60 transaksi TP+SL pada simbol itu.",
    "p Holm < 0.05, satu sisi: WR di atas C0 (edge ada).",
    "WR ≥ 57%.",
    "Gross > 0 bps (setelah slippage, sebelum fee).",
    "WR > 50% di tiap setengah periode uji.",
    "Ketahanan: WR ≥ 55% dengan overshoot stop 2 bps dan WR > 50% di minimal 15 dari 20 offset tick.",
    "Sumber edge: permutasi tren p < 0.05 (kontender 1 dan 2); kontender 3 lebih tinggi dari "
    "aturan primernya sendiri, p < 0.05.",
    "Pasokan gabungan simbol yang lulus ≥ 10 transaksi per hari (periode uji dan 20 hari terakhir).",
    "Kontrol wajar (C0 50 ± 3% dengan |gross| ≤ 2 bps; PBO latih < 0.5 untuk kontender 1 dan 3).",
)

Lines = List[str]


# --------------------------------------------------------------------------- formatting


def _num(value: Any) -> Optional[float]:
    """``value`` as a finite float, else ``None`` (bool and non-numbers are not numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def fmt_wr(value: Any) -> str:
    v = _num(value)
    return NA if v is None else f"{v:.1f}%"


def fmt_share(value: Any) -> str:
    """A share in 0..1 as a percentage with one decimal."""
    v = _num(value)
    return NA if v is None else f"{100.0 * v:.1f}%"


def fmt_bps(value: Any) -> str:
    v = _num(value)
    return NA if v is None else f"{v:.2f}"


def fmt_money(value: Any) -> str:
    v = _num(value)
    return NA if v is None else f"{round(v)}"


def fmt_p(value: Any) -> str:
    v = _num(value)
    return NA if v is None else f"{v:.3f}"


def fmt_dec(value: Any, digits: int = 1) -> str:
    v = _num(value)
    return NA if v is None else f"{v:.{digits}f}"


def fmt_int(value: Any) -> str:
    v = _num(value)
    return NA if v is None else f"{round(v)}"


def _net(gross_bps: Any) -> Optional[float]:
    v = _num(gross_bps)
    return None if v is None else net_per_1m(v)


def _label(name: str) -> str:
    return CONTENDER_LABELS.get(name, name)


def _cell(value: Any) -> str:
    """A table cell; a pipe in a rule key would split the row."""
    return str(value).replace("|", "/").replace("\n", " ")


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> Lines:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return out + [""]


def _get(doc: Any, *keys: Any) -> Any:
    """Nested lookup that yields ``None`` through anything that is not a mapping."""
    for key in keys:
        if not isinstance(doc, Mapping):
            return None
        doc = doc.get(key)
    return doc


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _section(title: str, body: Lines) -> Lines:
    return [f"## {title}", ""] + body


def _unavailable(what: str = "Bagian ini") -> Lines:
    return [f"{what} {NOT_AVAILABLE}.", ""]


# --------------------------------------------------------------------------- holdout access


def _contenders(holdout: Any) -> Mapping[str, Any]:
    return _mapping(_get(holdout, "contenders"))


def _symbol_rows(holdout: Any) -> Iterator[Tuple[str, str, Mapping[str, Any]]]:
    """(contender, symbol, per-symbol holdout block) in contender then symbol order."""
    for name in sorted(_contenders(holdout)):
        symbols = _mapping(_get(holdout, "contenders", name, "symbols"))
        for symbol in [s for s in SYMBOLS if s in symbols] + sorted(set(symbols) - set(SYMBOLS)):
            yield name, symbol, _mapping(symbols[symbol])


def c0_failed_symbols(holdout: Any) -> List[str]:
    """Symbols whose C0 control broke spec 5.9: always-long or always-short WR outside
    50 ± 3 or |gross| above 2 bps. A control without a number counts as failed."""
    failed = []
    for symbol, cell in sorted(_mapping(_get(holdout, "controls", "c0")).items()):
        for side in ("long", "short"):
            wr, gross = _num(_get(cell, side, "wr")), _num(_get(cell, side, "gross_bps"))
            if (wr is None or gross is None
                    or abs(wr - C0_CENTRE_WR) > C0_WR_TOLERANCE_PTS
                    or abs(gross) > C0_GROSS_TOLERANCE_BPS):
                failed.append(symbol)
                break
    return failed


# --------------------------------------------------------------------------- the verdict


def _verdict_row(block: Mapping[str, Any]) -> Dict[str, Any]:
    v = _mapping(block.get("verdict"))
    return {"passed": v.get("passed") is True,
            "failed_criteria": list(v.get("failed_criteria") or []),
            "wr": _num(v.get("wr")), "lower_bound": _num(v.get("lower_bound")),
            "tp": int(_num(v.get("tp")) or 0), "sl": int(_num(v.get("sl")) or 0)}


def _per_contender(holdout: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for name, symbol, block in _symbol_rows(holdout):
        out.setdefault(name, {})[symbol] = _verdict_row(block)
    return out


def _enough_trades(per: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> bool:
    return any(row["tp"] + row["sl"] >= MIN_TP_SL for rows in per.values() for row in rows.values())


def summarize_for_verdict(results: Mapping[str, Any]) -> Dict[str, Any]:
    """The summary dict ``verdict_sentence`` receives; JSON-native.

    - ``outcome``: "passed" (a contender passed the gate on at least one symbol),
      "failed" (the holdout ran with enough trades and nothing passed) or
      "insufficient" (no holdout, or no contender has 60 TP+SL trades on any symbol);
    - ``contender``: the chosen contender ("c1" | "c2" | "c3"), else ``None``;
    - ``symbols``: the symbols to trade (passed); empty unless "passed";
    - ``failed_symbols``: judged symbols that did not pass (under the chosen
      contender when one passed, otherwise every judged symbol);
    - ``wr``, ``lower_bound``, ``n_tp_sl``: per symbol for the chosen contender
      (percent / percent / count), empty when none was chosen;
    - ``c0_valid``: ``False`` when a C0 control failed (fill model invalid, results
      not to be read), ``None`` when there are no controls;
    - ``per_contender``: {contender: {symbol: {passed, failed_criteria, wr,
      lower_bound, tp, sl}}} for every contender and symbol judged.
    """
    holdout = results.get("holdout")
    per = _per_contender(holdout)
    chosen = _get(holdout, "chosen")
    to_trade = [s for s in (_get(holdout, "symbols_to_trade") or [])]
    passed = bool(per) and chosen in per and bool(to_trade)
    if not per or (not passed and not _enough_trades(per)):
        outcome = "insufficient"
    else:
        outcome = "passed" if passed else "failed"
    judged = [s for s in SYMBOLS if any(s in rows for rows in per.values())]
    mine = per.get(chosen, {}) if passed else {}
    c0_symbols = _mapping(_get(holdout, "controls", "c0"))
    return {
        "outcome": outcome,
        "contender": chosen if passed else None,
        "symbols": to_trade if passed else [],
        "failed_symbols": [s for s in judged if s not in to_trade] if passed else judged,
        "wr": {s: r["wr"] for s, r in mine.items()},
        "lower_bound": {s: r["lower_bound"] for s, r in mine.items()},
        "n_tp_sl": {s: r["tp"] + r["sl"] for s, r in mine.items()},
        "c0_valid": (not c0_failed_symbols(holdout)) if c0_symbols else None,
        "per_contender": per,
    }


def verdict_sentence(summary: dict) -> str:
    """The one paragraph the manager reads (Kesimpulan), in Indonesian. USER CONTRIBUTION.

    ``summary`` comes from ``summarize_for_verdict``:

    - ``outcome``: "passed" | "failed" | "insufficient";
    - ``contender``: "c1" | "c2" | "c3" | None (the contender chosen for both symbols);
    - ``symbols``: symbols to trade; ``failed_symbols``: symbols judged and not passed;
    - ``wr`` / ``lower_bound`` / ``n_tp_sl``: per symbol, for the chosen contender;
    - ``c0_valid``: False when the fill model's control failed; ``per_contender``: all.

    It must return a short, honest text with one of three outcomes:

    - gate passed: must contain "lulus gerbang" and "belum bukti 60%";
    - gate failed: must contain "tidak lulus";
    - not enough data: must contain "data belum cukup".
    """
    # TODO(user): write the three sentences (5-10 lines). Guidance:
    #   - passed: the edge is present and consistent with the target, but a true 60% has a
    #     95% interval of about 54-66% at 250-500 trades; the 60% verdict comes later.
    #   - failed: the bot is unchanged and K2 continues; name the symbols that failed.
    #   - insufficient: say what is missing (holdout not run, or under 60 TP+SL trades).
    raise NotImplementedError("verdict_sentence is the user's contribution (TODO(user))")


# --------------------------------------------------------------------------- header and summary


def _sha_header(results: Mapping[str, Any]) -> Lines:
    sha = _mapping(results.get("sha256"))
    lines = ["# Laporan evaluasi gerbang Pine (Fase 1)", "",
             "Evaluasi offline atas data riwayat; tidak ada order yang ditempatkan dan bot "
             "tidak diubah.", ""]
    for key in SHA_KEYS:
        value = sha.get(key)
        lines.append(f"- sha256 {key}: " + (f"`{value}`" if value else NOT_AVAILABLE))
    return lines + [""]


def _c0_warning(holdout: Any) -> Lines:
    failed = c0_failed_symbols(holdout)
    if not failed:
        return []
    return [
        f"**PERINGATAN: model fill tidak valid untuk {', '.join(failed)}** (kontrol C0 gagal: WR "
        "selalu-long atau selalu-short di luar 50 ± 3% atau |gross| di atas 2 bps). Hasil "
        "kontender di bawah jangan dibaca sebagai bukti apa pun; model fill harus diperbaiki "
        "lebih dulu (spec 5.9).", ""]


def _economics_lines() -> Lines:
    return [
        "Rumus: net per 1M = 50 × (gross bps − 7) pada fee 0.035% per sisi. Acuan: bot live "
        f"saat ini {LIVE_BASELINE_PER_1M} per 1M volume.", ""]


def _summary_table(holdout: Mapping[str, Any]) -> Lines:
    rows = []
    for name, symbol, b in _symbol_rows(holdout):
        v = _mapping(b.get("verdict"))
        rows.append([_label(name), symbol, fmt_wr(v.get("wr")), fmt_money(b.get("net_per_1m")),
                     fmt_dec(b.get("supply")), "lulus" if v.get("passed") is True else "gagal"])
    headers = ["Kontender", "Simbol", "WR", "net/1M", "transaksi/hari", "gerbang"]
    return _table(headers, rows) if rows else []


def _summary_outcome(summary: Mapping[str, Any]) -> Lines:
    """The Ringkasan outcome line, from the same summary the Kesimpulan sentence gets."""
    if not summary["per_contender"]:
        return [f"Hasil kontender {NOT_AVAILABLE}.", ""]
    if summary["outcome"] == "passed":
        trade = ", ".join(summary["symbols"]) or NA
        return [f"Kontender terpilih: {_label(summary['contender'])}; simbol yang lolos: {trade}.",
                ""]
    if summary["outcome"] == "insufficient":
        return [f"Data belum cukup untuk menilai gerbang (kurang dari {MIN_TP_SL} transaksi "
                "TP+SL pada setiap kontender dan simbol); bot tidak diubah.", ""]
    return ["Tidak ada kontender yang lulus gerbang; bot tidak diubah dan tabel WR "
            "terhadap pasokan per kontender dan simbol ada di bawah.", ""]


def _honesty_lines() -> Lines:
    return [
        "Lulus gerbang berarti \"edge ada dan konsisten dengan target\", bukan bukti WR 60%: "
        "dengan sekitar 250–500 transaksi per simbol, WR sebenarnya 60% punya interval 95% "
        "sekitar 54–66%. Keputusan 60% dibuat per simbol setelah sekitar 600 transaksi.", ""]


def _ringkasan(results: Mapping[str, Any]) -> Lines:
    holdout = results.get("holdout")
    lines = _provenance_warning(results) + _c0_warning(holdout)
    if not isinstance(holdout, Mapping) or not holdout:
        return lines + _unavailable("Hasil periode uji") + _economics_lines() + _honesty_lines()
    # a failed C0 voids the results
    outcome = [] if c0_failed_symbols(holdout) else _summary_outcome(summarize_for_verdict(results))
    return lines + outcome + _economics_lines() + _summary_table(holdout) + _honesty_lines()


# --------------------------------------------------------------------------- data integrity


def _match(a: Any, b: Any) -> str:
    if not a or not b:
        return NOT_AVAILABLE
    return "cocok" if a == b else "TIDAK COCOK"


def _integrity(results: Mapping[str, Any]) -> Lines:
    sha, sel = _mapping(results.get("sha256")), _mapping(results.get("selection"))
    hold = _mapping(results.get("holdout"))
    rows = [[key, _sha_cell(sha.get(key))] for key in SHA_KEYS]
    rows += [["kode di file seleksi", _sha_cell(sel.get("code_sha256"))],
             ["kode di file uji", _sha_cell(hold.get("code_sha256"))]]
    checks = [
        ["spec di file seleksi = spec saat ini", _match(sel.get("spec_sha256"), sha.get("spec"))],
        ["spec di file uji = spec saat ini", _match(hold.get("spec_sha256"), sha.get("spec"))],
        ["file seleksi yang dipakai uji = file seleksi", _match(hold.get("selection_sha256"),
                                                                sha.get("selection"))],
        ["kode file seleksi = kode file uji", _match(sel.get("code_sha256"),
                                                     hold.get("code_sha256"))],
        ["kode saat ini = kode file uji", _match(sha.get("code"), hold.get("code_sha256"))],
    ]
    facts = [["s* (overshoot stop, bps)", fmt_bps(sel.get("s_star_bps"))],
             ["jumlah stop untuk s*", fmt_int(sel.get("n_stops"))],
             ["jumlah sampel latensi", fmt_int(sel.get("n_latencies"))],
             ["seed", fmt_int(sel.get("seed"))],
             ["data dimuat sampai (ms UTC)", fmt_int(sel.get("data_loaded_until_ms"))],
             ["periode uji (ms UTC)", _period_text(hold.get("period"))]]
    return (_table(["Berkas", "sha256"], rows) + _table(["Pemeriksaan", "Hasil"], checks)
            + _table(["Parameter", "Nilai"], facts))


def _sha_cell(value: Any) -> str:
    return f"`{value}`" if value else NOT_AVAILABLE


def _period_text(period: Any) -> str:
    p = _mapping(period)
    if _num(p.get("start_ms")) is None or _num(p.get("end_ms")) is None:
        return NOT_AVAILABLE
    return f"{fmt_int(p['start_ms'])} sampai {fmt_int(p['end_ms'])}"


# --------------------------------------------------------------------------- step 0 and training


def _regime_rows(counts: Mapping[str, Any]) -> List[List[Any]]:
    rows = []
    for symbol in sorted(counts):
        for trend in sorted(_mapping(counts[symbol])):
            for month, c in sorted(_mapping(counts[symbol][trend]).items()):
                n = _num(_get(c, "n"))
                short = _get(c, "insufficient") is True or (n is not None and n < REGIME_MIN_N)
                status = f"tidak cukup (n kurang dari {REGIME_MIN_N})" if short else "cukup"
                rows.append([symbol, trend, month, fmt_share(_get(c, "share_up")),
                             fmt_share(_get(c, "share_down")), fmt_share(_get(c, "share_neutral")),
                             fmt_int(n), status])
    return rows


def _regime(selection: Any) -> Lines:
    rows = _regime_rows(_mapping(_get(selection, "regime_counts")))
    if not rows:
        return _unavailable()
    note = ["Sebaran naik/turun/netral per sumber tren dan bulan WIB, dicuplik di tiap penutupan "
            f"bar 5m. Sel dengan n kurang dari {REGIME_MIN_N} ditandai \"tidak cukup\", bukan \"gagal\".", ""]
    return note + _table(["Simbol", "Tren", "Bulan", "naik", "turun", "netral", "n", "Status"], rows)


def _rule_row(key: str, metrics: Mapping[str, Any]) -> List[Any]:
    return [key, fmt_int(metrics.get("n_tp_sl")), fmt_wr(metrics.get("wr")),
            fmt_wr(metrics.get("lower_bound")), fmt_bps(metrics.get("mean_gross_bps")),
            fmt_money(_net(metrics.get("mean_gross_bps"))), fmt_dec(metrics.get("supply_per_day")),
            "ya" if metrics.get("eligible") is True else "tidak"]


PBO_UNDEFINED = ("; tak terdefinisi: kurang dari dua aturan dengan ≥ 1.000 TP+SL, jadi kriteria 9 "
                 "gagal untuk kontender 1 dan 3")


def _training(selection: Any) -> Lines:
    sel = _mapping(selection)
    if not sel.get("rules") and not sel.get("top"):
        return _unavailable()
    rules = _mapping(sel.get("rules"))
    rows = [_rule_row(key, _mapping(rules.get(key))) for key in list(sel.get("top") or [])[:TOP_RULES]]
    facts = [
        f"- Kontender 1 terpilih: `{sel.get('contender1') or NA}`; aturan primer kontender 3: "
        f"`{sel.get('primary_rule') or NA}`.",
        f"- Aturan dinilai: {len(rules)}; stabil: {_yes_no(sel.get('stable'))}; pasokan "
        f"membatasi: {_yes_no(sel.get('supply_limited'))}.",
        f"- PBO (CSCV, aturan dengan ≥ 1.000 TP+SL): {fmt_dec(sel.get('pbo'), 2)} (lulus jika "
        f"< 0.5: {_yes_no(sel.get('pbo_pass'))}){PBO_UNDEFINED if sel.get('pbo_reason') else ''}.",
        f"- SPA p: {fmt_p(sel.get('spa_p'))} ({fmt_int(sel.get('spa_n'))} replikasi; nilai arah vs "
        "sisi acak pada entri yang sama).", ""]
    headers = ["Aturan", "n TP+SL", "WR", "batas bawah", "gross bps", "net/1M", "transaksi/hari",
               "memenuhi syarat"]
    return (facts + ["Lima aturan teratas pada data latih (WR, net/1M, pasokan):", ""]
            + _table(headers, rows) + _training_c0(sel))


def _training_c0(sel: Mapping[str, Any]) -> Lines:
    """C0 on training: always long / always short per symbol and the verdict."""
    rows = [[f"{s} {side}", fmt_wr(_get(c, side, "wr")), fmt_bps(_get(c, side, "gross_bps")),
             fmt_int(_get(c, side, "n"))]
            for s, c in sorted(_mapping(sel.get("c0_training")).items())
            for side in ("long", "short")]
    if not rows:
        return [f"C0 pada data latih: {NOT_AVAILABLE}.", ""]
    line = ("C0 pada data latih wajar (50 ± 3% WR, |gross| ≤ 2 bps)."
            if sel.get("c0_training_pass") is True else
            "**PERINGATAN: model fill tidak valid pada data latih** (C0 di luar 50 ± 3% WR atau "
            "|gross| di atas 2 bps); uji tidak boleh dijalankan sebelum diperbaiki.")
    return [line, ""] + _table(["C0 pada data latih", "WR", "gross bps", "n"], rows)


def _yes_no(value: Any) -> str:
    return NA if value is None else ("ya" if value else "tidak")


def _metalabel(selection: Any) -> Lines:
    sel = _mapping(selection)
    if sel.get("meta_model") is None and not sel.get("cv_logloss"):
        return ["Tidak ada model meta-labeling (aturan primer tidak tersedia atau data belum "
                f"tersedia): {NOT_AVAILABLE}.", ""]
    losses = _mapping(sel.get("cv_logloss"))
    sizes = sel.get("cv_fold_sizes")
    loss_rows = [[c, ", ".join(fmt_dec(x, 4) for x in v), fmt_dec(_pooled_loss(v, sizes), 4)]
                 for c, v in sorted(losses.items(), key=lambda kv: float(kv[0]))]
    rel_rows = [[fmt_dec(r[0], 3), fmt_dec(r[1], 3), fmt_int(r[2])]
                for r in (sel.get("reliability") or []) if len(r) == 3]
    head = [f"- C terpilih: {fmt_dec(sel.get('chosen_C'), 3)}; ambang probabilitas: "
            f"{fmt_dec(_get(sel, 'meta_model', 'threshold'), 2)}; kandidat: "
            f"{fmt_int(sel.get('n_candidates'))}; prediksi di luar sampel: {fmt_int(sel.get('n_oof'))}.",
            f"- pasokan pada ambang (trade/hari, B2–B4): luar sampel "
            f"{fmt_dec(sel.get('supply_oof_at_threshold'))}; model akhir "
            f"{fmt_dec(sel.get('supply_final_at_threshold'))}.",
            "", "Log-loss validasi per lipatan (jendela memanjang B1–B4); rata-rata dibobot "
            "ukuran lipatan, sama dengan pemilihan C:", ""]
    return (head + _table(["C", "log-loss per lipatan", "rata-rata"], loss_rows)
            + ["Kalibrasi (prediksi vs frekuensi teramati):", ""]
            + _table(["rata-rata prediksi", "frekuensi teramati", "n"], rel_rows))


def _pooled_loss(losses: Any, sizes: Any) -> Optional[float]:
    """Fold-size weighted mean log-loss (as ``metalabel._best_C``); ``None`` without sizes."""
    if not isinstance(sizes, list) or not isinstance(losses, list) or len(sizes) != len(losses):
        return None
    if (not sizes or any(_num(n) is None or n <= 0 for n in sizes)
            or any(_num(x) is None for x in losses)):
        return None
    return sum(n * x for n, x in zip(sizes, losses)) / sum(sizes)


# --------------------------------------------------------------------------- holdout


def _holdout_rows(holdout: Mapping[str, Any]) -> List[List[Any]]:
    rows = []
    for name, symbol, b in _symbol_rows(holdout):
        v = _mapping(b.get("verdict"))
        halves = list(b.get("halves") or [None, None])
        rows.append([_label(name), symbol, fmt_int(b.get("tp")), fmt_int(b.get("sl")),
                     fmt_int(b.get("time")), fmt_wr(v.get("wr")), fmt_wr(b.get("lower_bound")),
                     fmt_bps(b.get("gross_bps")), fmt_money(b.get("net_per_1m")),
                     fmt_money(b.get("net_per_1m_fee_sensitivity")),
                     f"{fmt_wr(halves[0])} / {fmt_wr(halves[-1])}", fmt_dec(b.get("supply")),
                     fmt_dec(b.get("supply_last20")), "lulus" if v.get("passed") is True else "gagal"])
    return rows


def _reference_rows(holdout: Mapping[str, Any]) -> List[List[Any]]:
    ref = _mapping(_get(holdout, "sensitivities", "reference"))
    return [[s, fmt_int(m.get("n")), fmt_wr(m.get("wr")), fmt_bps(m.get("gross_bps")),
             fmt_money(m.get("net_per_1m"))] for s, m in sorted(ref.items()) if isinstance(m, Mapping)]


def _holdout_section(holdout: Any) -> Lines:
    rows = _holdout_rows(holdout) if isinstance(holdout, Mapping) else []
    skipped = _get(holdout, "skipped")
    skipped_line = [f"Kontender dilewati: {', '.join(map(str, skipped))}.", ""] if skipped else []
    if not rows:
        return skipped_line + _unavailable("Hasil kontender") if skipped else _unavailable()
    headers = ["Kontender", "Simbol", "TP", "SL", "time stop", "WR", "batas bawah", "gross bps",
               "net/1M", "net/1M (fee 0.05%)", "WR separuh 1 / 2", "transaksi/hari",
               "transaksi/hari 20 hari", "gerbang"]
    lines = ["WR = TP / (TP + SL); time stop dilaporkan terpisah. Gross dihitung atas semua "
             f"transaksi. Acuan bot live: {LIVE_BASELINE_PER_1M} per 1M. Entri diisi pada "
             "harga penutupan bar 1m yang memuat waktu order (tick + latensi AI + 5 detik, "
             "spec 5.10), bukan pada harga saat order dikirim.", ""]
    lines += _table(headers, rows)
    ref = _reference_rows(holdout)
    if ref:
        lines += ["Referensi (skrip Pine apa adanya, hanya dilaporkan):", ""]
        lines += _table(["Simbol", "n", "WR", "gross bps", "net/1M"], ref)
    return lines + skipped_line


def _gate_cell(verdict: Mapping[str, Any]) -> str:
    if verdict.get("passed") is True:
        return "lulus"
    failed = ", ".join(str(n) for n in verdict.get("failed_criteria") or [])
    return f"gagal: {failed}" if failed else NA


def _gate_section(holdout: Any) -> Lines:
    rows = [[_label(n), s, fmt_p(b.get("p_holm")), fmt_wr(_get(b, "verdict", "wr")),
             _gate_cell(_mapping(b.get("verdict")))] for n, s, b in _symbol_rows(holdout)]
    if not rows:
        return _unavailable()
    lines = ["Kriteria (spec 5.11), per kontender dan simbol:", ""]
    lines += [f"{i}. {text}" for i, text in enumerate(CRITERIA_LEGEND, start=1)] + [""]
    lines += _table(["Kontender", "Simbol", "p Holm", "WR", "Hasil"], rows)
    chosen = _get(holdout, "chosen")
    trade = ", ".join(_get(holdout, "symbols_to_trade") or []) or NA
    pick = _label(chosen) if chosen else "tidak ada (bot tidak diubah)"
    return lines + [f"Kontender terpilih: {pick}. Simbol diperdagangkan: {trade}.", ""]


# --------------------------------------------------------------------------- controls


def _c0_rows(holdout: Any) -> List[List[Any]]:
    bad = set(c0_failed_symbols(holdout))
    rows = []
    for s, cell in sorted(_mapping(_get(holdout, "controls", "c0")).items()):
        rows.append([s, fmt_wr(_get(cell, "long", "wr")), fmt_bps(_get(cell, "long", "gross_bps")),
                     fmt_wr(_get(cell, "short", "wr")), fmt_bps(_get(cell, "short", "gross_bps")),
                     "GAGAL" if s in bad else "wajar"])
    return rows


def _c1_rows(holdout: Any) -> List[List[Any]]:
    rows = []
    for name, symbols in sorted(_mapping(_get(holdout, "controls", "c1")).items()):
        for s, c in sorted(_mapping(symbols).items()):
            mine = _get(holdout, "contenders", name, "symbols", s, "verdict", "wr")
            rows.append([_label(name), s, fmt_wr(mine), fmt_wr(c.get("mean")), fmt_wr(c.get("p5")),
                         fmt_wr(c.get("p95")), fmt_int(c.get("n_entries"))])
    return rows


def _source_text(source: Mapping[str, Any]) -> str:
    if source.get("kind") == "permutation":
        return (f"permutasi tren, {fmt_int(source.get('shifts'))} pergeseran; WR rata-rata "
                f"tergeser {fmt_wr(source.get('mean_shifted_wr'))}")
    return (f"dibanding aturan primer; WR aturan primer {fmt_wr(source.get('primary_wr'))} "
            f"(n {fmt_int(source.get('primary_n'))})")


def _robust_text(rob: Mapping[str, Any]) -> str:
    offsets = [w for w in rob.get("wr_offsets") or [] if (_num(w) or 0) > 50.0]
    total = len(rob.get("wr_offsets") or [])
    return (f"WR dengan overshoot {fmt_dec(rob.get('overshoot_bps'))} bps "
            f"{fmt_wr(rob.get('wr_overshoot'))}; offset dengan WR > 50%: {len(offsets)} dari {total}")


def _source_rows(holdout: Any) -> List[List[Any]]:
    return [[_label(n), s, fmt_p(_get(b, "source", "p")), _source_text(_mapping(b.get("source"))),
             _robust_text(_mapping(b.get("robustness")))] for n, s, b in _symbol_rows(holdout)]


def _controls_section(holdout: Any) -> Lines:
    c0, c1, src = _c0_rows(holdout), _c1_rows(holdout), _source_rows(holdout)
    if not (c0 or c1 or src):
        return _unavailable()
    lines = ["C0: selalu-long dan selalu-short pada jam tick yang sama; harus 50 ± 3% WR dan gross "
             "dalam ± 2 bps.", ""]
    lines += _table(["Simbol", "WR long", "gross long", "WR short", "gross short", "Hasil"], c0)
    lines += ["C1: waktu entri kontender dengan sisi acak (nilai arah):", ""]
    lines += _table(["Kontender", "Simbol", "WR kontender", "C1 rata-rata", "p5", "p95", "n entri"], c1)
    lines += ["Sumber edge (kriteria 7) dan ketahanan (kriteria 6):", ""]
    return lines + _table(["Kontender", "Simbol", "p", "Sumber", "Ketahanan"], src)


# --------------------------------------------------------------------------- checks


def _provenance_problem(doc: Any, results: Mapping[str, Any]) -> Optional[str]:
    """Why a check output does not belong to this report's holdout, else ``None``.

    vst-check and decisions record the ``contender`` they scored and the
    ``selection_sha256`` they were run under; both must match the holdout's chosen
    contender and the selection file of this report (spec 5.12, 5.13)."""
    chosen, contender = _get(results.get("holdout"), "chosen"), _get(doc, "contender")
    if contender is None or contender != chosen:
        return (f"kontender {contender or NA} bukan kontender pilihan periode uji "
                f"({chosen or NA})")
    selection_sha = _get(results.get("sha256"), "selection")
    if not selection_sha or _get(doc, "selection_sha256") != selection_sha:
        return "sha256 file seleksi berbeda dari file seleksi laporan ini"
    return None


def _checked_vst(results: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """VST outputs that belong to this report, and ``{symbol: reason}`` for the rest."""
    kept: Dict[str, Any] = {}
    skipped: Dict[str, str] = {}
    for symbol, doc in sorted(_mapping(results.get("vst")).items()):
        problem = _provenance_problem(doc, results)
        if problem is None:
            kept[symbol] = doc
        else:
            skipped[symbol] = problem
    return kept, skipped


def _decisions_problem(results: Mapping[str, Any]) -> Optional[str]:
    doc = results.get("decisions")
    return None if doc is None else _provenance_problem(doc, results)


def _provenance_warning(results: Mapping[str, Any]) -> Lines:
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


def _vst_section(results: Mapping[str, Any]) -> Lines:
    kept, skipped = _checked_vst(results)
    rows = []
    for symbol, a in kept.items():
        diff = None
        if _num(_get(a, "wr_prod")) is not None and _num(_get(a, "wr_vst")) is not None:
            diff = abs(a["wr_prod"] - a["wr_vst"])
        rows.append([_label(str(_get(a, "contender"))), symbol, fmt_int(_get(a, "n")),
                     fmt_share(_get(a, "side_agree")), fmt_share(_get(a, "outcome_agree")),
                     fmt_wr(_get(a, "wr_prod")), fmt_wr(_get(a, "wr_vst")), fmt_dec(diff),
                     _yes_no(_get(a, "passed"))])
    skipped_lines = _skipped_lines([f"VST {s}: {why}" for s, why in skipped.items()])
    if not rows:
        return skipped_lines + _unavailable()
    note = [f"Ambang (spec 5.12): arah sinyal sama ≥ {SIDE_AGREE_MIN:.0%}, hasil bracket sama "
            f"≥ {OUTCOME_AGREE_MIN:.0%}, selisih WR ≤ {WR_DIFF_MAX_PTS:.0f} poin. Jika gagal, "
            "pembacaan maju di VST yang menentukan.", ""]
    headers = ["Kontender", "Simbol", "tick", "arah sama", "hasil sama", "WR produksi",
               "WR VST", "selisih WR (poin)", "lulus"]
    return skipped_lines + note + _table(headers, rows)


BUCKET_NOTES = {
    "A": "semua kandidat AI (OPEN, keyakinan ≥ 0.75, tinjauan risiko disetujui)",
    "B": "kandidat AI dengan sinyal aturan searah",
    "E": "ada sinyal aturan, AI menjawab HOLD",
    "F": "semua sinyal pada waktu tick tersimpan",
}


def _decisions_section(results: Mapping[str, Any]) -> Lines:
    problem = _decisions_problem(results)
    if problem is not None:
        return _skipped_lines([f"keputusan AI: {problem}"]) + _unavailable()
    doc = results.get("decisions")
    contender = _label(str(_get(doc, "contender")))
    rows = []
    for key in ("A", "B", "E", "F"):
        b = _mapping(_get(doc, "buckets", key))
        if b:
            rows.append([contender, key, BUCKET_NOTES[key], fmt_int(b.get("n")),
                         fmt_int(b.get("tp")), fmt_int(b.get("sl")), fmt_int(b.get("time")),
                         fmt_wr(b.get("wr")), fmt_bps(b.get("gross")),
                         fmt_money(_net(b.get("gross"))), fmt_dec(b.get("trades_per_day"))])
    if not rows:
        return _unavailable()
    headers = ["Kontender", "Bucket", "Definisi", "n", "TP", "SL", "time", "WR", "gross bps",
               "net/1M", "transaksi/hari (belum dikoreksi)"]
    return ["Keputusan AI BTC tersimpan, dengan bracket 0.20% / 0.20%. Transaksi/hari belum "
            "dikoreksi untuk tick saat posisi terbuka (spec 5.13).", ""] + _table(headers, rows)


# --------------------------------------------------------------------------- cadence and guards


def _cadence_rows(holdout: Any) -> List[List[Any]]:
    cadence = _mapping(_get(holdout, "sensitivities", "cadence"))
    rows = []
    for name, symbols in sorted(cadence.items()):
        for s, m in sorted(_mapping(symbols).items()):
            main = _get(holdout, "contenders", name, "symbols", s, "verdict", "wr")
            rows.append([_label(name), s, fmt_wr(main), fmt_wr(m.get("wr")),
                         fmt_bps(m.get("gross_bps")), fmt_money(_net(m.get("gross_bps"))),
                         fmt_dec(m.get("supply"))])
    return rows


def _guard_rows(holdout: Any) -> List[List[Any]]:
    off = _mapping(_get(holdout, "sensitivities", "guards_off"))
    first = _mapping(_get(holdout, "sensitivities", "eth_first"))
    rows = []
    for name in sorted(set(off) | set(first)):
        rows.append([_label(name), fmt_dec(_get(off, name, "combined_supply")),
                     fmt_share(_get(first, name, "co_signal", "share")),
                     fmt_int(_get(first, name, "co_signal", "both_signal"))])
    return rows


def _guard_symbol_rows(holdout: Any) -> List[List[Any]]:
    off = _mapping(_get(holdout, "sensitivities", "guards_off"))
    return [[_label(name), s, fmt_int(_get(m, "n")), fmt_wr(_get(m, "wr")),
             fmt_bps(_get(m, "gross_bps")), fmt_dec(_get(m, "supply"))]
            for name in sorted(off) for s, m in sorted(_mapping(_get(off, name, "symbols")).items())]


def _cadence_section(holdout: Any) -> Lines:
    cad, guards = _cadence_rows(holdout), _guard_rows(holdout)
    if not (cad or guards):
        return _unavailable()
    lines = ["Kadens: jam satu menit yang hanya bertindak pada penutupan 5m, tanpa latensi dan "
             "tanpa jeda order. Dibandingkan dengan jalannya utama:", ""]
    lines += _table(["Kontender", "Simbol", "WR utama", "WR kadens", "gross bps", "net/1M",
                     "transaksi/hari"], cad)
    lines += ["Guard dimatikan (pasokan tanpa batas harian) dan urutan ETH lebih dulu "
              "(bagian tick dengan sinyal di kedua simbol):", ""]
    lines += _table(["Kontender", "pasokan tanpa guard", "sinyal kedua simbol",
                     "tick sinyal ganda"], guards)
    lines += ["Per simbol dengan guard dimatikan:", ""]
    lines += _table(["Kontender", "Simbol", "n", "WR", "gross bps", "transaksi/hari"],
                    _guard_symbol_rows(holdout))
    lines += ["Sensitivitas overshoot stop (hanya dilaporkan; 2 bps ada di kriteria 6):", ""]
    return lines + _table(["Kontender", "overshoot bps", "Simbol", "WR", "gross bps", "net/1M"],
                          _overshoot_rows(holdout))


def _overshoot_rows(holdout: Any) -> List[List[Any]]:
    runs = _mapping(_get(holdout, "sensitivities", "overshoot"))
    return [[_label(name), bps, s, fmt_wr(_get(m, "wr")), fmt_bps(_get(m, "gross_bps")),
             fmt_money(_net(_get(m, "gross_bps")))]
            for name in sorted(runs) for bps, symbols in sorted(_mapping(runs[name]).items())
            for s, m in sorted(_mapping(symbols).items())]


def _kesimpulan(verdict: str) -> Lines:
    return [verdict, "", "Angka di atas adalah hasil simulasi offline; keputusan atas bot nyata "
            "tetap menunggu persetujuan terpisah.", ""]


# --------------------------------------------------------------------------- public API


def _render(results: Mapping[str, Any], verdict: str) -> str:
    selection, holdout = results.get("selection"), results.get("holdout")
    bodies = (
        _ringkasan(results), _integrity(results), _regime(selection), _training(selection),
        _metalabel(selection), _holdout_section(holdout), _gate_section(holdout),
        _controls_section(holdout), _vst_section(results),
        _decisions_section(results), _cadence_section(holdout),
        _kesimpulan(verdict),
    )
    lines = _sha_header(results)
    for title, body in zip(SECTION_TITLES, bodies, strict=True):
        lines += _section(title, body)
    return "\n".join(lines).rstrip("\n") + "\n"


def render_markdown(results: Mapping[str, Any]) -> str:
    """The report, sections in ``SECTION_TITLES`` order under the sha256 header.

    ``results``: {"selection", "holdout" (or None), "vst" ({symbol: vst-check output} or
    {}), "decisions" (the decisions output with "buckets", or None), "sha256" ({"spec",
    "data", "selection", "holdout", "code"})}. A VST or decisions output whose
    ``contender`` is not the holdout's ``chosen``, or whose ``selection_sha256`` is not
    ``sha256["selection"]``, is skipped with a warning at the top of Ringkasan. The Kesimpulan paragraph is ``verdict_sentence`` over
    ``summarize_for_verdict(results)``. ``results`` is not modified."""
    return _render(results, verdict_sentence(summarize_for_verdict(results)))


def write_report(
    results: Mapping[str, Any], out_dir: Path, now: Optional[datetime] = None
) -> Tuple[Path, Path]:
    """Write ``pine_gate_edge_<YYYYMMDD>.md`` and ``.json`` (WIB date); returns both paths.

    The JSON copy is ``results`` plus ``summary`` (``summarize_for_verdict``) and
    ``verdict`` (the Kesimpulan text); it is strict JSON (NaN refused). Nothing is
    written when either file exists (``FileExistsError``) or the report cannot be
    rendered or serialised. ``now`` is for tests."""
    verdict = verdict_sentence(summarize_for_verdict(results))
    markdown = _render(results, verdict)
    document = {**results, "summary": summarize_for_verdict(results), "verdict": verdict}
    text = json.dumps(document, sort_keys=True, indent=1, allow_nan=False)
    stamp = (now or datetime.now(timezone.utc)).astimezone(WIB)
    out = Path(out_dir)
    md_path, json_path = (out / f"{REPORT_PREFIX}{stamp:%Y%m%d}{ext}" for ext in (".md", ".json"))
    for path in (md_path, json_path):
        if path.exists():
            raise FileExistsError(f"{path}: report already exists; remove it deliberately")
    out.mkdir(parents=True, exist_ok=True)
    write_exclusive(md_path, markdown, "report")
    try:
        write_exclusive(json_path, text + "\n", "report")
    except BaseException:
        md_path.unlink()
        raise
    return md_path, json_path
