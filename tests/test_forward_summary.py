"""Tests for ``bot.core.forward_summary``: the forward test's summary and its trade CSV.

The summary maths is tested through ``report_forward_test`` in ``tests/test_report_forward_test.py``;
here the module is checked to be importable on its own and the CSV reader to be the exact inverse of
the writer, refusing anything else.
"""

from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bot.core import forward_summary as fs
from bot.models import ClosedTrade, PositionDirection

REPO = Path(__file__).resolve().parents[1]
WIB = timezone(timedelta(hours=7))
FROM, TO = date(2026, 10, 20), date(2026, 10, 23)


def closed_trade(pnl=1.0, reason="neutral_exit", closed=None, entry=100.0, exit_=101.0, qty=10.0,
                 direction=PositionDirection.LONG):
    closed = closed or datetime(2026, 10, 21, 12, 0, tzinfo=WIB)
    return ClosedTrade(
        symbol="BTC/USDT", direction=direction, entry_price=entry, exit_price=exit_,
        quantity=qty, pnl_usdt=pnl, reason=reason, entry_reason="ai_volume",
        opened_at=closed - timedelta(minutes=20), closed_at=closed,
        exchange_entry_order_id="e", exchange_exit_order_id="x",
    )


class NonLongTests(unittest.TestCase):
    """The forward bot is LONG-only; a row of another side must be counted and named, never pooled silently."""

    def test_counts_rows_of_either_shape_inside_the_window(self):
        in_window = datetime(2026, 10, 21, 12, 0, tzinfo=WIB)
        trades = [
            closed_trade(),
            closed_trade(direction=PositionDirection.SHORT),
            closed_trade(direction=PositionDirection.SHORT, closed=datetime(2026, 10, 30, tzinfo=WIB)),
            fs.ForwardTrade(in_window - timedelta(minutes=5), in_window, "SHORT", 100.0, 99.0, 1.0,
                            1.0, "stop_loss", 199.0),
        ]

        summary = fs.summarize(trades, FROM, TO)

        self.assertEqual(summary.posisi, 3)
        self.assertEqual(summary.non_long, 2)  # the one closed after the window is not counted

    def test_all_long_has_none(self):
        self.assertEqual(fs.summarize([closed_trade()], FROM, TO).non_long, 0)
        self.assertIsNone(fs.non_long_warning(0))

    def test_the_warning_names_the_count(self):
        text = fs.non_long_warning(2)

        self.assertTrue(text.startswith("PERINGATAN: 2 posisi bukan LONG"), text)


class ReadTradesCsvTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "forward_trades.csv"

    def write_rows(self, rows, header=fs.CSV_COLUMNS):
        with self.path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            if header is not None:
                writer.writerow(header)
            writer.writerows(rows)

    def good_row(self, **change):
        row = {
            "opened_at": "2026-10-21T04:40:00+00:00",
            "closed_at": "2026-10-21T05:00:00+00:00",
            "direction": "LONG",
            "entry_price": "100.0",
            "exit_price": "101.0",
            "quantity": "10.0",
            "pnl_usdt": "1.5",
            "reason": "strategy_exit:fade",
            "volume_usdt": "2010.0",
        }
        row.update(change)
        return [row[name] for name in fs.CSV_COLUMNS]

    def test_reads_back_what_export_csv_wrote(self):
        trades = [
            closed_trade(pnl=10.0, reason="neutral_exit"),
            closed_trade(pnl=-4.25, reason="strategy_exit:momentum faded", entry=100.0,
                         exit_=99.5, qty=0.3, closed=datetime(2026, 10, 22, 1, 0, tzinfo=WIB)),
        ]
        fs.export_csv(trades, self.path)

        read = fs.read_trades_csv(self.path)

        self.assertEqual(len(read), 2)
        for got, want in zip(read, trades):
            self.assertEqual(got.opened_at, want.opened_at)
            self.assertEqual(got.closed_at, want.closed_at)
            self.assertIsNotNone(got.closed_at.tzinfo)
            self.assertEqual(got.direction, "LONG")
            self.assertEqual((got.entry_price, got.exit_price, got.quantity, got.pnl_usdt),
                             (want.entry_price, want.exit_price, want.quantity, want.pnl_usdt))
            self.assertEqual(got.reason, want.reason)
            self.assertEqual(got.volume_usdt, fs.trade_volume(want))
        # the summary takes the rows read back as it takes the store's trades
        self.assertEqual(fs.summarize(read, FROM, TO), fs.summarize(trades, FROM, TO))

    def test_rows_read_back_export_again_unchanged(self):
        fs.export_csv([closed_trade(pnl=10.0, reason="neutral_exit")], self.path)
        again = self.path.with_name("again.csv")

        fs.export_csv(fs.read_trades_csv(self.path), again)

        self.assertEqual(again.read_text(encoding="utf-8"), self.path.read_text(encoding="utf-8"))

    def test_an_empty_export_reads_as_no_trades(self):
        fs.export_csv([], self.path)
        self.assertEqual(fs.read_trades_csv(self.path), [])

    def test_refuses_what_the_export_never_writes(self):
        cases = {
            "no header": (None, [self.good_row()]),
            "other header": (("opened_at", "closed_at"), [self.good_row()]),
            "a column short": (fs.CSV_COLUMNS, [self.good_row()[:-1]]),
            "naive opened_at": (fs.CSV_COLUMNS, [self.good_row(opened_at="2026-10-21T04:40:00")]),
            "naive closed_at": (fs.CSV_COLUMNS, [self.good_row(closed_at="2026-10-21T05:00:00")]),
            "not a time": (fs.CSV_COLUMNS, [self.good_row(closed_at="yesterday")]),
            "closed before opened": (fs.CSV_COLUMNS,
                                     [self.good_row(closed_at="2026-10-21T04:00:00+00:00")]),
            "direction": (fs.CSV_COLUMNS, [self.good_row(direction="long")]),
            "price not a number": (fs.CSV_COLUMNS, [self.good_row(entry_price="abc")]),
            "price not finite": (fs.CSV_COLUMNS, [self.good_row(exit_price="nan")]),
            "quantity zero": (fs.CSV_COLUMNS, [self.good_row(quantity="0")]),
            "pnl infinite": (fs.CSV_COLUMNS, [self.good_row(pnl_usdt="inf")]),
            "volume not the legs": (fs.CSV_COLUMNS, [self.good_row(volume_usdt="2000.0")]),
        }
        for name, (header, rows) in cases.items():
            with self.subTest(name):
                self.path.unlink(missing_ok=True)
                self.write_rows(rows, header=header)
                with self.assertRaises(ValueError) as raised:
                    fs.read_trades_csv(self.path)
                self.assertIn(str(self.path), str(raised.exception))

    def test_an_empty_file_is_refused(self):
        self.path.write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            fs.read_trades_csv(self.path)

    def test_bytes_that_are_not_utf8_are_refused_naming_the_file(self):
        self.path.write_bytes(b"\xff\xfe not a csv")
        with self.assertRaises(ValueError) as raised:
            fs.read_trades_csv(self.path)
        self.assertIn(str(self.path), str(raised.exception))

    def test_a_missing_file_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            fs.read_trades_csv(self.path)

    def test_summarize_trims_the_given_number_of_best_trades(self):
        trades = [closed_trade(pnl=p, entry=100.0, exit_=100.0, qty=10.0) for p in (9.0, 5.0, -1.0)]
        one = fs.summarize(trades, FROM, TO, drop_top=1)
        self.assertAlmostEqual(one.hasil_tanpa_teratas, 1_000_000 * 4.0 / 4000.0)
        self.assertIsNone(fs.summarize(trades, FROM, TO).hasil_tanpa_teratas)  # 3 of 3 dropped


class ImportTests(unittest.TestCase):
    def test_importing_it_loads_no_settings_storage_or_network_client(self):
        code = (
            "import sys, bot.core.forward_summary\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
            "('httpx', 'pydantic_settings', 'supabase') or m in ('bot.config', "
            "'bot.storage.supabase_trade_store'))\n"
            "print(bad)\n"
        )
        done = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                              env={"PYTHONPATH": str(REPO / "src")},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
