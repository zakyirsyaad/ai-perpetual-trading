"""Tests for the Mode A table of research.mode_backtest.report: ``ReportRow`` and ``mode_table``.

The header is held to the design document itself (``spec_table_header`` reads it out of spec
section 2.9) and to a copy written out here. Every expected line is written out by hand in
Indonesian notation (a decimal comma, a dot between thousands, "-" for a missing value), so the
tests never format a number with the code under test. The metrics of a row are written out by
hand too, except in the last class, which renders what ``stage_metrics`` makes of real trades.
"""

import dataclasses
import unittest

from research.mode_backtest.metrics import StageMetrics, stage_metrics
from bot.strategy.modes.config import load_modes_config
from pathlib import Path

from research.mode_backtest.metrics import GAGAL
from research.mode_backtest.report import (
    NO_TRADING,
    LiveRow,
    ReportRow,
    live_no_trading_row,
    live_status_line,
    live_table,
    mode_table,
)
from research.mode_backtest.simulate import EXIT_SL, EXIT_TP, EXIT_WAKTU
from tests.mode_backtest_helpers import MINUTE_MS
from tests.mode_backtest_trades import FIRST_ENTRY_MS, make_trade, spec_table_header

DOCUMENT_HEADER = (
    "| Mode | Pair | Tahap | Posisi | Entry per hari aktif | Menang | Hasil per 1 juta | "
    "Keluar TP / SL / waktu | Rata-rata lama posisi |"
)
SEPARATOR = "|---|---|---|---|---|---|---|---|---|"
BTC, ETH = "BTC-USDT", "ETH-USDT"


def metrics(
    posisi=100,
    entry=2.0,
    menang=50.0,
    hasil=-100.0,
    tp=40,
    sl=40,
    waktu=20,
    rata=45.5,
    tanpa=-200.0,
    hari_naik=50,
):
    """A metrics object written out by hand."""
    return StageMetrics(
        posisi=posisi,
        entry_per_hari_aktif=entry,
        menang_pct=menang,
        hasil_per_1_juta=hasil,
        tp=tp,
        sl=sl,
        waktu=waktu,
        rata_lama_menit=rata,
        hasil_tanpa_teratas=tanpa,
        hari_naik=hari_naik,
    )


def row(mode="A", pair=BTC, tahap="Latihan", **fields):
    return ReportRow(mode, pair, tahap, metrics(**fields))


def lines(rows):
    return mode_table(rows).splitlines()


class HeaderTests(unittest.TestCase):
    def test_table_header_equals_the_document(self):
        header, separator = lines([])

        self.assertEqual(header, DOCUMENT_HEADER)
        self.assertEqual(separator, SEPARATOR)

    def test_the_header_cells_are_those_of_the_spec(self):
        header = lines([])[0]

        self.assertEqual([c.strip() for c in header.strip("|").split("|")], spec_table_header())

    def test_any_iterable_of_rows_gives_the_same_table(self):
        rows = [row("A"), row("D di NAIK", hasil=-300.0)]

        self.assertEqual(mode_table(iter(rows)), mode_table(rows))
        self.assertEqual(mode_table(tuple(rows)), mode_table(rows))
        self.assertEqual(len(lines(r for r in rows)), 5)  # header, separator, two rows, baseline

    def test_a_table_without_rows_is_the_header_alone(self):
        self.assertEqual(mode_table([]), DOCUMENT_HEADER + "\n" + SEPARATOR + "\n")

    def test_the_separator_has_one_cell_per_column(self):
        self.assertEqual(SEPARATOR.count("---"), len(spec_table_header()))


class RowTests(unittest.TestCase):
    def test_a_row_is_written_in_indonesian_notation(self):
        m = metrics(
            posisi=1234,
            entry=2.5,
            menang=48.07,
            hasil=-1234.4,
            tp=600,
            sl=500,
            waktu=134,
            rata=45.5,
        )

        table = lines([ReportRow("A", BTC, "Latihan", m)])

        self.assertEqual(
            table[2],
            "| A | BTC-USDT | Latihan | 1.234 | 2,50 | 48,1% | -1.234 | "
            "600 / 500 / 134 | 45,5 menit |",
        )

    def test_a_missing_value_is_a_dash(self):
        m = metrics(posisi=0, entry=None, menang=None, hasil=None, tp=0, sl=0, waktu=0, rata=None)

        table = lines([ReportRow("A", BTC, "Ujian", m)])

        self.assertEqual(table[2], "| A | BTC-USDT | Ujian | 0 | – | – | – | 0 / 0 / 0 | – |")

    def test_a_loss_that_rounds_to_zero_has_no_sign(self):
        table = lines([row(hasil=-0.4)])

        self.assertIn("| 0 |", table[2])
        self.assertNotIn("-0", table[2])

    def test_the_mode_pair_and_tahap_are_written_as_given(self):
        table = lines([ReportRow("D di NAIK", ETH, "Ujian", metrics())])

        self.assertTrue(table[2].startswith("| D di NAIK | ETH-USDT | Ujian | "))

    def test_every_line_has_nine_cells(self):
        for line in lines([row(), row(mode="D")]):
            with self.subTest(line=line):
                self.assertEqual(line.count("|"), 10)

    def test_the_table_ends_with_one_newline(self):
        text = mode_table([row()])

        self.assertTrue(text.endswith("|\n"))
        self.assertFalse(text.endswith("\n\n"))

    def test_the_counts_of_exits_keep_their_order_and_a_thousands_dot(self):
        table = lines([row(posisi=3000, tp=1234, sl=1500, waktu=266)])

        self.assertIn("| 1.234 / 1.500 / 266 |", table[2])

    def test_the_hold_is_in_minutes_with_one_decimal(self):
        self.assertIn("| 90,0 menit |", lines([row(rata=90.0)])[2])
        self.assertIn("| 1,5 menit |", lines([row(rata=1.5)])[2])

    def test_menang_has_one_decimal_and_a_percent_sign(self):
        self.assertIn("| 50,0% |", lines([row(menang=50.0)])[2])
        self.assertIn("| 0,0% |", lines([row(menang=0.0)])[2])

    def test_entry_per_hari_aktif_has_two_decimals(self):
        self.assertIn("| 2,99 |", lines([row(entry=2.99)])[2])
        self.assertIn("| 3,00 |", lines([row(entry=3.0)])[2])

    def test_a_row_is_a_frozen_record_of_four_fields(self):
        r = row()

        self.assertEqual(
            [f.name for f in dataclasses.fields(ReportRow)], ["mode", "pair", "tahap", "metrics"]
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.mode = "D"


class NoTradingTests(unittest.TestCase):
    def test_the_label_is_tidak_trading(self):
        self.assertEqual(NO_TRADING, "Tidak trading")

    def test_a_tidak_trading_row_follows_the_comparator_of_each_tahap(self):
        rows = [
            row("A", tahap="Latihan"),
            row("D di NAIK", tahap="Latihan", hasil=-300.0),
            row("A", tahap="Ujian"),
            row("D di NAIK", tahap="Ujian", hasil=-250.0),
        ]

        table = lines(rows)

        self.assertEqual(
            [line.split(" | ")[0].lstrip("| ") for line in table[2:]],
            ["A", "D di NAIK", "Tidak trading", "A", "D di NAIK", "Tidak trading"],
        )
        self.assertEqual(
            table[4], "| Tidak trading | BTC-USDT | Latihan | 0 | 0,00 | – | 0 | 0 / 0 / 0 | – |"
        )
        self.assertEqual(
            table[7], "| Tidak trading | BTC-USDT | Ujian | 0 | 0,00 | – | 0 | 0 / 0 / 0 | – |"
        )

    def test_a_tahap_with_one_row_still_gets_its_tidak_trading_row(self):
        table = lines([row("A", tahap="Ujian")])

        self.assertEqual(len(table), 4)
        self.assertTrue(table[3].startswith("| Tidak trading | BTC-USDT | Ujian | 0 |"))

    def test_the_rows_keep_the_order_given_and_the_extra_row_closes_the_last_of_its_tahap(self):
        rows = [
            row("A", tahap="Latihan"),
            row("A", tahap="Ujian"),
            row("D di NAIK", tahap="Latihan"),
            row("D di NAIK", tahap="Ujian"),
        ]

        table = lines(rows)

        self.assertEqual(
            [tuple(c.strip() for c in line.strip("|").split("|")[:3]) for line in table[2:]],
            [
                ("A", BTC, "Latihan"),
                ("A", BTC, "Ujian"),
                ("D di NAIK", BTC, "Latihan"),
                ("Tidak trading", BTC, "Latihan"),
                ("D di NAIK", BTC, "Ujian"),
                ("Tidak trading", BTC, "Ujian"),
            ],
        )

    def test_the_entry_per_day_of_it_is_a_dash_when_the_tahap_has_no_naik_day(self):
        table = lines([row("A", entry=None, hari_naik=0, posisi=0, hasil=None)])

        self.assertEqual(
            table[3], "| Tidak trading | BTC-USDT | Latihan | 0 | – | – | 0 | 0 / 0 / 0 | – |"
        )

    def test_each_pair_of_a_tahap_gets_its_own_tidak_trading_row(self):
        rows = [row("A", pair=BTC), row("A", pair=ETH)]

        table = lines(rows)

        self.assertEqual(len(table), 6)
        self.assertTrue(table[3].startswith("| Tidak trading | BTC-USDT | Latihan |"))
        self.assertTrue(table[5].startswith("| Tidak trading | ETH-USDT | Latihan |"))

    def test_the_row_has_no_positions_and_a_result_of_zero(self):
        m = metrics(posisi=77, hasil=-999.0, hari_naik=12)

        tidak_trading = lines([ReportRow("A", BTC, "Latihan", m)])[3].split(" | ")

        self.assertEqual(tidak_trading[3], "0")  # Posisi
        self.assertEqual(tidak_trading[6], "0")  # Hasil per 1 juta

    def test_no_rows_means_no_tidak_trading_row(self):
        self.assertEqual(len(lines([])), 2)


class RealTradesTests(unittest.TestCase):
    def test_a_table_from_trades_is_the_same_as_one_from_the_numbers(self):
        def trade(pnl, reason, minute, hold):
            return make_trade(
                entry_ms=FIRST_ENTRY_MS + minute * MINUTE_MS,
                hold_min=hold,
                pnl_usdt=pnl,
                exit_reason=reason,
            )

        trades = [
            trade(10.0, EXIT_TP, 0, 30),
            trade(-40.0, EXIT_SL, 100, 60),
            trade(0.0, EXIT_WAKTU, 200, 90),
        ]
        m = stage_metrics(trades, hari_naik=2, drop_top=3)

        table = lines([ReportRow("A", BTC, "Ujian", m)])

        # 3 positions over 2 NAIK days; 1 TP of 2 decided; -30 on 150,000 is -200 per 1M;
        # holds of 30, 60 and 90 minutes
        self.assertEqual(
            table[2], "| A | BTC-USDT | Ujian | 3 | 1,50 | 50,0% | -200 | 1 / 1 / 1 | 60,0 menit |"
        )


LIVE_HEADER = (
    "| Mode | Versi | Pair | Tahap | Posisi | Entry per hari aktif | Entry per hari kalender | "
    "Menang | Hasil per 1 juta | Keluar TP / SL / potong / waktu | Rata-rata lama posisi |"
)
UJI = load_modes_config(Path(__file__).resolve().parents[1] / "config" / "modes.yaml").uji


def live_metrics(**fields):
    base = dict(
        posisi=123, entry_per_hari_aktif=2.345, menang_pct=47.61, hasil_per_1_juta=-1234.56,
        tp=50, sl=40, waktu=13, rata_lama_menit=41.26, hasil_tanpa_teratas=-1500.0, hari_naik=52,
        potong=20, entry_per_hari_kalender=1.005,
    )
    base.update(fields)
    return StageMetrics(**base)


class LiveTableTests(unittest.TestCase):
    def test_live_table_rows_and_rounding(self):
        table = live_table([
            LiveRow("L", "ii", BTC, "Ujian", live_metrics()),
            LiveRow("S", "i", BTC, "Ujian", live_metrics(entry_per_hari_kalender=None,
                                                        menang_pct=None, rata_lama_menit=None)),
        ]).splitlines()

        self.assertEqual(table[0], LIVE_HEADER)
        self.assertEqual(table[1], "|" + "---|" * 11)
        self.assertEqual(
            table[2],
            "| L | ii | BTC-USDT | Ujian | 123 | 2,35 | 1,00 | 47,6% | -1.235 | "
            "50 / 40 / 20 / 13 | 41,3 menit |",
        )
        self.assertEqual(
            table[3],
            "| S | i | BTC-USDT | Ujian | 123 | 2,35 | – | – | -1.235 | "
            "50 / 40 / 20 / 13 | – |",
        )
        self.assertEqual(len(table), 4)

    def test_the_no_trading_row_of_a_live_stage(self):
        row = live_no_trading_row(BTC, "Ujian", 52, 70)

        self.assertEqual((row.mode, row.versi, row.tahap), (NO_TRADING, "–", "Ujian"))
        self.assertEqual((row.metrics.posisi, row.metrics.hasil_per_1_juta), (0, 0.0))
        self.assertEqual(
            live_table([row]).splitlines()[2],
            "| Tidak trading | – | BTC-USDT | Ujian | 0 | 0,00 | 0,00 | – | 0 | 0 / 0 / 0 / 0 | – |",
        )
        none = live_no_trading_row(BTC, "Ujian", 0, 0).metrics
        self.assertEqual((none.entry_per_hari_aktif, none.entry_per_hari_kalender), (None, None))

    def test_an_empty_live_table_is_the_header_alone(self):
        self.assertEqual(len(live_table([]).splitlines()), 2)

    def test_the_mode_a_table_is_untouched(self):
        self.assertEqual(lines([row()])[0], DOCUMENT_HEADER)


class LiveStatusLineTests(unittest.TestCase):
    def test_live_status_line_names_the_version(self):
        m = live_metrics(posisi=37, hasil_per_1_juta=-190.5, hasil_tanpa_teratas=-260.0)

        line = live_status_line("ii", m, UJI, "BTC-USDT")

        self.assertEqual(
            line,
            "Ujian mode L versi ii BTC-USDT: BELUM CUKUP SAMPEL. Posisi 37 (minimal 40); "
            "Hasil per 1 juta = -190.5 (lolos bila > -150.0); tanpa 3 posisi teratas = -260.0 "
            "(lolos bila > -250.0).",
        )

    def test_a_failing_stage_and_the_comparator(self):
        m = live_metrics(posisi=60, hasil_per_1_juta=-190.5, hasil_tanpa_teratas=-260.0)

        line = live_status_line("i", m, UJI, "BTC-USDT", mode="S")

        self.assertTrue(line.startswith("Ujian mode S versi i BTC-USDT: GAGAL. Posisi 60 (minimal 40);"))
        self.assertIn(GAGAL, line)


if __name__ == "__main__":
    unittest.main()
