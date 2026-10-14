import json
import re
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from research.pine_gate import report
from research.pine_gate.report import (
    NOT_AVAILABLE,
    SECTION_TITLES,
    render_markdown,
    summarize_for_verdict,
    verdict_sentence,
    write_report,
)

BTC, ETH = "BTC-USDT", "ETH-USDT"
RULE = "E200|P35|ADX|OFF|OFF"
SHA = {name: str(i) * 64 for i, name in enumerate(("spec", "data", "selection", "holdout", "code"))}
STUB_VERDICT = "KALIMAT-VONIS-UJI"


def verdict(contender, symbol, passed, failed=(), tp=120, sl=80):
    return {"contender": contender, "symbol": symbol, "passed": passed,
            "failed_criteria": list(failed), "wr": 100.0 * tp / (tp + sl),
            "lower_bound": 51.2, "gross_bps": 3.1, "p_holm": 0.01, "tp": tp, "sl": sl}


def symbol_block(contender, symbol, passed, failed=(), tp=120, sl=80, gross=3.1):
    wr = 100.0 * tp / (tp + sl) if tp + sl else None
    return {
        "n": tp + sl + 10, "tp": tp, "sl": sl, "time": 10, "wr": wr, "lower_bound": 51.2,
        "gross_bps": gross, "net_per_1m": 50 * (gross - 7),
        "net_per_1m_fee_sensitivity": 50 * (gross - 10), "supply": 6.0, "supply_last20": 5.5,
        "halves": [57.0, 58.0],
        "robustness": {"wr_overshoot": 56.0, "overshoot_bps": 2.0,
                       "wr_offsets": [55.0, 49.0, 51.0], "offsets_s": [0, 9, 18]},
        "source": {"kind": "permutation", "p": 0.02, "shifts": 1000,
                   "mean_shifted_wr": 50.4, "shifted_without_tp_sl": 0},
        "p_c0": 0.01, "p_holm": 0.02, "evidence": {},
        "verdict": verdict(contender, symbol, passed, failed, tp, sl),
    }


def contender_block(name, passed_symbols, tp=120, sl=80):
    symbols = {}
    for s in (BTC, ETH):
        ok = s in passed_symbols
        symbols[s] = symbol_block(name, s, ok, () if ok else (3, 8), tp, sl)
    block = {"rule": RULE if name != "c2" else None, "symbols": symbols,
             "pooled_lower_bound": 52.0 if passed_symbols else None,
             "symbols_passed": list(passed_symbols)}
    if name == "c3":
        block["primary"] = {s: {"wr": 52.0, "n": 400} for s in (BTC, ETH)}
        for s in (BTC, ETH):
            symbols[s]["source"] = {"kind": "paired_vs_primary", "p": 0.03,
                                    "primary_wr": 52.0, "primary_n": 400}
    return block


def c0_cell(wr=50.5, gross=0.4):
    side = {"wr": wr, "gross_bps": gross, "n": 300, "tp": 150, "sl": 150}
    return {"long": dict(side), "short": dict(side), "wr": wr}


def make_holdout(passed=True, c0_ok=True, tp=120, sl=80):
    names = ("c1", "c2", "c3")
    winners = [BTC] if passed else []
    contenders = {n: contender_block(n, winners if n == "c2" else [], tp, sl) for n in names}
    c0 = {s: c0_cell() for s in (BTC, ETH)}
    if not c0_ok:
        c0[ETH] = c0_cell(wr=61.0, gross=0.4)
    brief = {"n": 300, "wr": 52.0, "gross_bps": 1.0, "supply": 6.0}
    return {
        "contenders": contenders, "skipped": [],
        "controls": {"c0": c0,
                     "c1": {n: {s: {"n_entries": 300, "n_seeds": 200, "mean": 50.1,
                                    "p5": 46.0, "p95": 54.0} for s in (BTC, ETH)}
                            for n in names}},
        "sensitivities": {
            "eth_first": {n: {"symbols": {s: dict(brief) for s in (BTC, ETH)},
                              "co_signal": {"evaluated_ticks": 1000, "both_signal": 50,
                                            "share": 0.05}} for n in names},
            "cadence": {n: {s: dict(brief) for s in (BTC, ETH)} for n in names},
            "guards_off": {n: {"symbols": {s: dict(brief) for s in (BTC, ETH)},
                               "combined_supply": 11.0} for n in names},
            "reference": {s: symbol_block("ref", s, False) for s in (BTC, ETH)},
            "fee_pct": 0.05,
        },
        "chosen": "c2" if passed else None, "symbols_to_trade": winners,
        "period": {"start_ms": 1, "end_ms": 2, "last20_start_ms": 1, "last20_end_ms": 2},
        "spec_sha256": SHA["spec"], "selection_sha256": SHA["selection"],
        "created_at": "2026-10-14T00:00:00+00:00",
    }


def rule_metrics(wr=55.0, gross=2.0):
    return {"n_trades": 900, "n_tp_sl": 800, "supply_per_day": 7.5, "wr": wr,
            "lower_bound": 52.0, "block_wr": [55.0] * 4, "block_lb": [51.0] * 4,
            "block_counts": [[10, 8]] * 4, "cscv_counts": [[10, 8]] * 8,
            "rank_value": 52.0, "mean_gross_bps": gross, "eligible": True}


def regime_cell(n=500):
    return {"up": n // 2, "down": n // 4, "neutral": n - n // 2 - n // 4, "unknown": 3,
            "n": n, "share_up": 0.5, "share_down": 0.25, "share_neutral": 0.25,
            "insufficient": n < 60}


def make_selection():
    top = [f"E200|P35|ADX|{vol}|OFF" for vol in ("OFF", "HIGH", "NOT_HIGH")]
    top += ["H1|B12|OFF|OFF|OFF", "H4|B24|ADX_DI|OFF|OFF"]
    return {
        "contender1": RULE, "top": top, "stable": True, "pbo": 0.31, "pbo_pass": True,
        "supply_limited": False, "primary_rule": RULE, "s_star_bps": 1.0, "n_stops": 20,
        "n_latencies": 100, "seed": 0, "spa_p": 0.04, "spa_n": 2000,
        "rules": {k: rule_metrics() for k in top},
        "regime_counts": {s: {t: {"2025-08": regime_cell(), "2025-09": regime_cell(40)}
                              for t in ("E200", "H1", "H4")} for s in (BTC, ETH)},
        "meta_model": {"threshold": 0.6, "C": 0.1}, "chosen_C": 0.1,
        "cv_logloss": {"0.1": [0.69, 0.68, 0.67, 0.69], "1.0": [0.70, 0.69, 0.68, 0.70]},
        "reliability": [[0.52, 0.50, 100], [0.61, 0.58, 80]], "n_oof": 180,
        "n_candidates": 200, "spec_sha256": SHA["spec"],
    }


def agreement(passed=True):
    return {"n": 5000, "side_agree": 0.982, "outcome_agree": 0.961, "wr_prod": 54.0,
            "wr_vst": 53.0, "passed": passed}


def buckets():
    base = {"n": 100, "tp": 40, "sl": 40, "time": 20, "wr": 50.0, "gross": 0.5,
            "trades_per_day": 2.5}
    return {k: dict(base) for k in ("A", "B", "E", "F")}


def make_results(passed=True, c0_ok=True):
    return {"selection": make_selection(), "holdout": make_holdout(passed, c0_ok),
            "vst": {BTC: agreement()}, "decisions": buckets(), "sha256": dict(SHA)}


def headings(text):
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def section(text, title):
    parts = re.split(r"^## ", text, flags=re.M)
    return next(p for p in parts if p.startswith(title))


@mock.patch.object(report, "verdict_sentence", return_value=STUB_VERDICT)
class RenderMarkdownTests(unittest.TestCase):
    def test_markdown_has_all_sections_in_order_and_sha_header(self, _):
        text = render_markdown(make_results())

        self.assertEqual(headings(text), list(SECTION_TITLES))
        first_heading = text.index("## Ringkasan")
        for name, sha in SHA.items():
            self.assertIn(sha, text[:first_heading], name)
            self.assertIn(name, text[:first_heading].lower())

    def test_section_titles_are_the_required_indonesian_headings(self, _):
        self.assertEqual(SECTION_TITLES, (
            "Ringkasan", "Integritas data dan sha256", "Langkah 0 (sebaran rezim)",
            "Tahap latih (grid, 5 teratas, PBO, SPA)",
            "Model meta-labeling (log-loss, kalibrasi)",
            "Periode uji per kontender dan simbol", "Gerbang (kriteria 1–9)",
            "Kontrol dan permutasi", "VST vs produksi", "Keputusan AI (bucket A/B/E/F)",
            "Biaya kadens dan guard", "Kesimpulan"))

    def test_failed_c0_control_starts_ringkasan_with_fill_model_warning(self, _):
        text = render_markdown(make_results(c0_ok=False))

        body = section(text, "Ringkasan").split("\n", 1)[1].strip()
        self.assertTrue(body.startswith("**PERINGATAN"), body[:80])
        self.assertIn("model fill tidak valid", body[:300])
        self.assertIn("jangan dibaca", body[:400])
        self.assertIn(ETH, body[:400])
        self.assertNotIn(BTC, body[:400])

    def test_valid_c0_controls_give_no_warning(self, _):
        text = render_markdown(make_results())

        self.assertNotIn("PERINGATAN", text)

    def test_c0_gross_outside_two_bps_also_warns(self, _):
        results = make_results()
        results["holdout"]["controls"]["c0"][BTC]["short"]["gross_bps"] = -2.5

        self.assertIn("PERINGATAN", render_markdown(results))

    def test_missing_parts_render_not_available_without_crashing(self, _):
        results = {"selection": None, "holdout": None, "vst": {}, "decisions": None,
                   "sha256": {}}

        text = render_markdown(results)

        self.assertEqual(headings(text), list(SECTION_TITLES))
        self.assertIn(NOT_AVAILABLE, section(text, "Periode uji"))
        self.assertIn(NOT_AVAILABLE, section(text, "VST vs produksi"))
        self.assertIn(NOT_AVAILABLE, section(text, "Keputusan AI"))
        self.assertIn(NOT_AVAILABLE, section(text, "Tahap latih"))
        self.assertIn(STUB_VERDICT, section(text, "Kesimpulan"))

    def test_sha_mismatch_between_selection_used_and_selection_file_is_flagged(self, _):
        results = make_results()
        results["holdout"]["selection_sha256"] = "f" * 64

        integritas = section(render_markdown(results), "Integritas")

        self.assertIn("TIDAK COCOK", integritas)
        self.assertNotIn("TIDAK COCOK", section(render_markdown(make_results()), "Integritas"))

    def test_results_without_any_key_still_render(self, _):
        self.assertEqual(headings(render_markdown({})), list(SECTION_TITLES))

    def test_partial_selection_and_holdout_do_not_crash(self, _):
        results = {"selection": {"contender1": None, "rules": {}}, "holdout": {"contenders": {}},
                   "vst": {BTC: {"n": 0, "side_agree": None, "outcome_agree": None,
                                 "wr_prod": None, "wr_vst": None, "passed": False}},
                   "decisions": {"A": {"n": 0, "wr": None, "gross": None}},
                   "sha256": {"spec": "a" * 64}}

        text = render_markdown(results)

        self.assertEqual(headings(text), list(SECTION_TITLES))
        self.assertIn(NOT_AVAILABLE, text.split("## Ringkasan")[0])  # missing sha256 entries

    def test_wr_is_shown_with_net_per_1m_and_the_live_baseline(self, _):
        text = render_markdown(make_results())

        ringkasan = section(text, "Ringkasan")
        self.assertIn("60.0%", ringkasan)  # BTC c2 120/200
        self.assertIn("-195", ringkasan)  # 50 * (3.1 - 7)
        self.assertIn("-190", ringkasan)
        self.assertIn("50 × (gross bps − 7)", ringkasan)

    def test_summary_states_the_gate_is_not_a_60_percent_proof(self, _):
        ringkasan = section(render_markdown(make_results()), "Ringkasan")

        self.assertIn("bukan bukti WR 60%", ringkasan)
        self.assertIn("54", ringkasan)
        self.assertIn("66", ringkasan)

    def test_rule_keys_do_not_break_markdown_tables(self, _):
        text = render_markdown(make_results())

        for line in section(text, "Tahap latih").splitlines():
            if line.startswith("| E200"):
                self.assertEqual(line.count("|"), 9, line)

    def test_top_five_rules_are_listed(self, _):
        tahap = section(render_markdown(make_results()), "Tahap latih")

        for key in make_selection()["top"]:
            self.assertIn(key.replace("|", "/"), tahap)
        self.assertIn("0.31", tahap)  # PBO
        self.assertIn("0.040", tahap)  # SPA p

    def test_regime_cells_with_small_n_are_marked_insufficient(self, _):
        langkah0 = section(render_markdown(make_results()), "Langkah 0")

        self.assertIn("kurang dari 60", langkah0)
        self.assertIn("2025-09", langkah0)

    def test_gate_table_lists_failed_criteria_per_contender_and_symbol(self, _):
        gerbang = section(render_markdown(make_results()), "Gerbang")

        self.assertIn("Kontender 2", gerbang)
        self.assertIn("gagal: 3, 8", gerbang)
        self.assertIn("lulus", gerbang)
        for number in range(1, 10):
            self.assertIn(f"{number}.", gerbang)

    def test_vst_section_shows_thresholds_and_verdict(self, _):
        vst = section(render_markdown(make_results()), "VST vs produksi")

        self.assertIn("98.2%", vst)
        self.assertIn("96.1%", vst)
        self.assertIn("97%", vst)

    def test_ai_buckets_show_all_four_with_net_per_1m(self, _):
        ai = section(render_markdown(make_results()), "Keputusan AI")

        for bucket in "ABEF":
            self.assertIn(f"| {bucket} |", ai)
        self.assertIn("-325", ai)  # 50 * (0.5 - 7)

    def test_controls_section_reports_c0_c1_and_permutation(self, _):
        kontrol = section(render_markdown(make_results()), "Kontrol dan permutasi")

        self.assertIn("C0", kontrol)
        self.assertIn("C1", kontrol)
        self.assertIn("50.4%", kontrol)  # mean shifted WR
        self.assertIn("0.030", kontrol)  # paired p of contender 3

    def test_cadence_section_compares_with_the_main_run(self, _):
        biaya = section(render_markdown(make_results()), "Biaya kadens dan guard")

        self.assertIn("11.0", biaya)  # guards-off combined supply
        self.assertIn("5.0%", biaya)  # co-signal share

    def test_kesimpulan_gets_the_sentence_for_the_summary(self, mocked):
        results = make_results()

        text = render_markdown(results)

        self.assertIn(STUB_VERDICT, section(text, "Kesimpulan"))
        mocked.assert_called_once_with(summarize_for_verdict(results))

    def test_render_does_not_mutate_results(self, _):
        results = make_results()
        before = deepcopy(results)

        render_markdown(results)

        self.assertEqual(results, before)

    def test_non_finite_numbers_render_as_dash(self, _):
        results = make_results()
        results["holdout"]["contenders"]["c1"]["symbols"][BTC]["wr"] = float("nan")

        text = render_markdown(results)

        self.assertIsNone(re.search(r"\bnan\b", text.lower()))


class FormatTests(unittest.TestCase):
    def test_number_formats(self):
        self.assertEqual(report.fmt_wr(55.55), "55.5%")
        self.assertEqual(report.fmt_wr(None), "–")
        self.assertEqual(report.fmt_bps(2.345), "2.35")
        self.assertEqual(report.fmt_bps(None), "–")
        self.assertEqual(report.fmt_money(-194.6), "-195")
        self.assertEqual(report.fmt_money(None), "–")
        self.assertEqual(report.fmt_share(0.982), "98.2%")
        self.assertEqual(report.fmt_bps(float("inf")), "–")


class SummarizeForVerdictTests(unittest.TestCase):
    def test_passed_outcome_names_contender_symbols_and_wr(self):
        summary = summarize_for_verdict(make_results(passed=True))

        self.assertEqual(summary["outcome"], "passed")
        self.assertEqual(summary["contender"], "c2")
        self.assertEqual(summary["symbols"], [BTC])
        self.assertEqual(summary["failed_symbols"], [ETH])
        self.assertAlmostEqual(summary["wr"][BTC], 60.0)
        self.assertEqual(summary["lower_bound"][BTC], 51.2)
        self.assertEqual(summary["n_tp_sl"][BTC], 200)
        self.assertTrue(summary["c0_valid"])

    def test_failed_outcome_when_nothing_passes(self):
        summary = summarize_for_verdict(make_results(passed=False))

        self.assertEqual(summary["outcome"], "failed")
        self.assertIsNone(summary["contender"])
        self.assertEqual(summary["symbols"], [])
        self.assertEqual(summary["failed_symbols"], [BTC, ETH])
        self.assertEqual(sorted(summary["per_contender"]), ["c1", "c2", "c3"])

    def test_insufficient_when_holdout_is_missing(self):
        results = make_results()
        results["holdout"] = None

        summary = summarize_for_verdict(results)

        self.assertEqual(summary["outcome"], "insufficient")
        self.assertEqual(summary["symbols"], [])
        self.assertEqual(summary["per_contender"], {})

    def test_insufficient_when_no_contender_has_sixty_tp_sl_trades(self):
        results = make_results(passed=False)
        results["holdout"] = make_holdout(passed=False, tp=20, sl=15)

        self.assertEqual(summarize_for_verdict(results)["outcome"], "insufficient")

    def test_failed_not_insufficient_when_one_symbol_has_enough_trades(self):
        results = make_results(passed=False)
        block = results["holdout"]["contenders"]["c1"]["symbols"][BTC]
        block["verdict"]["tp"], block["verdict"]["sl"] = 40, 30

        self.assertEqual(summarize_for_verdict(results)["outcome"], "failed")

    def test_c0_validity_is_reported(self):
        summary = summarize_for_verdict(make_results(passed=False, c0_ok=False))

        self.assertFalse(summary["c0_valid"])
        self.assertEqual(summary["outcome"], "failed")

    def test_summary_is_json_native(self):
        json.dumps(summarize_for_verdict(make_results()), allow_nan=False)


class WriteReportTests(unittest.TestCase):
    NOW = datetime(2026, 10, 14, 18, 30, tzinfo=timezone.utc)  # 15 Oct 01:30 WIB

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / "out"
        patcher = mock.patch.object(report, "verdict_sentence", return_value=STUB_VERDICT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_files_are_named_with_the_wib_date(self):
        md, js = write_report(make_results(), self.out, now=self.NOW)

        self.assertEqual(md.name, "pine_gate_edge_20261015.md")
        self.assertEqual(js.name, "pine_gate_edge_20261015.json")
        self.assertEqual(md.parent, self.out)

    def test_markdown_file_equals_render_markdown(self):
        results = make_results()

        md, _ = write_report(results, self.out, now=self.NOW)

        self.assertEqual(md.read_text(encoding="utf-8"), render_markdown(results))

    def test_json_roundtrip(self):
        results = make_results()

        _, js = write_report(results, self.out, now=self.NOW)

        loaded = json.loads(js.read_text(encoding="utf-8"))
        self.assertEqual(loaded["verdict"], STUB_VERDICT)
        self.assertEqual(loaded["summary"], summarize_for_verdict(results))
        for key, value in results.items():
            self.assertEqual(loaded[key], json.loads(json.dumps(value)), key)

    def test_existing_report_is_never_overwritten(self):
        md, js = write_report(make_results(), self.out, now=self.NOW)
        before = (md.read_bytes(), js.read_bytes())

        with self.assertRaises(FileExistsError):
            write_report(make_results(passed=False), self.out, now=self.NOW)

        self.assertEqual((md.read_bytes(), js.read_bytes()), before)

    def test_existing_json_alone_refuses_and_leaves_no_markdown(self):
        self.out.mkdir(parents=True)
        (self.out / "pine_gate_edge_20261015.json").write_text("{}")

        with self.assertRaises(FileExistsError):
            write_report(make_results(), self.out, now=self.NOW)

        self.assertFalse((self.out / "pine_gate_edge_20261015.md").exists())

    def test_nan_in_results_is_refused_and_leaves_no_files(self):
        results = make_results()
        results["decisions"]["A"]["gross"] = float("nan")

        with self.assertRaises(ValueError):
            write_report(results, self.out, now=self.NOW)

        self.assertEqual(sorted(p.name for p in self.out.glob("*")) if self.out.exists() else [],
                         [])

    def test_no_temp_files_are_left_behind(self):
        write_report(make_results(), self.out, now=self.NOW)

        self.assertEqual(sorted(p.suffix for p in self.out.iterdir()), [".json", ".md"])


@unittest.skip("TODO(user): verdict_sentence is the user's contribution")
class VerdictSentenceTests(unittest.TestCase):
    def summary(self, outcome, **extra):
        base = {"outcome": outcome, "contender": None, "symbols": [], "failed_symbols": [],
                "wr": {}, "lower_bound": {}, "n_tp_sl": {}, "c0_valid": True,
                "per_contender": {}}
        return {**base, **extra}

    def test_verdict_sentence_three_outcomes(self):
        passed = verdict_sentence(self.summary(
            "passed", contender="c2", symbols=[BTC], failed_symbols=[ETH],
            wr={BTC: 58.2}, lower_bound={BTC: 52.1}, n_tp_sl={BTC: 260}))
        failed = verdict_sentence(self.summary("failed", failed_symbols=[BTC, ETH]))
        insufficient = verdict_sentence(self.summary("insufficient"))

        self.assertIn("lulus gerbang", passed)
        self.assertIn("belum bukti 60%", passed)
        self.assertIn(BTC, passed)
        self.assertIn("tidak lulus", failed)
        self.assertNotIn("lulus gerbang", failed)
        self.assertIn("data belum cukup", insufficient)

    def test_passed_sentence_names_the_symbols_that_failed(self):
        passed = verdict_sentence(self.summary(
            "passed", contender="c2", symbols=[BTC], failed_symbols=[ETH],
            wr={BTC: 58.2}, lower_bound={BTC: 52.1}, n_tp_sl={BTC: 260}))

        self.assertIn(ETH, passed)

    def test_failed_sentence_says_the_bot_is_unchanged(self):
        failed = verdict_sentence(self.summary("failed", failed_symbols=[BTC, ETH]))

        self.assertIn("tidak diubah", failed)


class VerdictSentenceStubTests(unittest.TestCase):
    def test_stub_documents_its_contract(self):
        doc = verdict_sentence.__doc__ or ""

        for needle in ("lulus gerbang", "belum bukti 60%", "tidak lulus", "data belum cukup",
                       "outcome", "contender", "symbols", "wr", "lower_bound"):
            self.assertIn(needle, doc)


if __name__ == "__main__":
    unittest.main()
