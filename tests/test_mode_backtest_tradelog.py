"""Tests for research.mode_backtest.tradelog: the per-trade CSV of spec section 2.9.

The column list is held to the design document itself (``spec_log_columns`` reads it out of the
spec) and to a copy written out here. Every field of a test trade gets a number of its own, so a
column fed from the wrong field cannot pass. Times are WIB wall-clock readings turned into UTC
milliseconds by hand, not by the code under test.
"""

import csv
import io
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bot.strategy.modes.position_rules import LONG, SHORT
from bot.strategy.modes.regime import NAIK, TURUN
from research.mode_backtest.simulate import EXIT_SL, EXIT_TP, EXIT_WAKTU, MODE_D
from research.mode_backtest.tradelog import COLUMNS, trade_log_text, wib_iso, write_trade_log
from research.pine_gate.bars import wib_day_start_ms
from tests.mode_backtest_helpers import MINUTE_MS
from tests.mode_backtest_trades import make_context, make_trade, spec_log_columns

# The columns of section 2.9, in its order (typed here from the document).
DOCUMENT_COLUMNS = (
    "trade_id,pair,mode,sisi,kondisi,sma20_1h,sma50_1h,ema50_4h,atr14_1h,faktor_k,"
    "waktu_sinyal,waktu_entry,harga_entry,harga_tp,harga_sl,waktu_exit,harga_exit,"
    "alasan_exit,fee_usdt,slippage_usdt,pnl_usdt,notional_usdt,volume_usdt"
).split(",")

WED = date(2026, 8, 5)  # a Wednesday
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def wib_ms(day, hour=0, minute=0, second=0):
    """UTC milliseconds of a WIB wall-clock reading."""
    return wib_day_start_ms(day) + ((hour * 60 + minute) * 60 + second) * 1000


def rows_of(text):
    """The CSV text as a list of dicts, one per trade."""
    return list(csv.DictReader(io.StringIO(text)))


def long_trade(**fields):
    """A LONG with a number of its own in every field the log writes.

    The signal is 15 minutes before the entry and the exit 45 minutes after it, which the real
    loop never does (it enters at the signal's close) but which tells the two time columns apart."""
    entry_ms = wib_ms(WED, 7, 15)
    values = dict(
        mode="A",
        pair="BTC-USDT",
        side=LONG,
        kondisi=NAIK,
        context=make_context(
            sma20_1h=101.5,
            sma50_1h=99.25,
            atr14_1h=2.5,
            atr_median=1.75,
            close_4h=102.0,
            ema50_4h=100.75,
            kondisi=NAIK,
        ),
        faktor_k=1.0,
        signal_ms=entry_ms - 15 * MINUTE_MS,
        entry_ms=entry_ms,
        entry_fill=100.01,
        tp=100.41004,
        sl=99.759975,
        exit_ms=entry_ms + 45 * MINUTE_MS,
        exit_fill=100.39959,
        exit_reason=EXIT_TP,
        qty=249.9,
        notional_usdt=25_000.0,
        fee_usdt=17.5,
        slippage_usdt=5.0,
        pnl_usdt=12.25,
        volume_usdt=50_000.0,
    )
    values.update(fields)
    return make_trade(**values)


LONG_ROW = (
    "1,BTC-USDT,A,LONG,NAIK,101.5,99.25,100.75,2.5,1.0,"
    "2026-08-05T07:00:00+07:00,2026-08-05T07:15:00+07:00,100.01,100.41004,99.759975,"
    "2026-08-05T08:00:00+07:00,100.39959,"
    "TP,17.5,5.0,12.25,25000.0,50000.0"
)


class ColumnTests(unittest.TestCase):
    def test_log_header_equals_the_document(self):
        self.assertEqual(list(COLUMNS), DOCUMENT_COLUMNS)
        self.assertEqual(list(COLUMNS), spec_log_columns())

    def test_there_are_23_distinct_columns(self):
        self.assertEqual(len(COLUMNS), 23)
        self.assertEqual(len(set(COLUMNS)), 23)

    def test_the_columns_are_an_immutable_tuple(self):
        self.assertIsInstance(COLUMNS, tuple)

    def test_a_log_without_trades_is_the_header_alone(self):
        self.assertEqual(trade_log_text([]), ",".join(DOCUMENT_COLUMNS) + "\n")

    def test_the_header_is_the_first_line_of_every_log(self):
        text = trade_log_text([long_trade()])

        self.assertEqual(text.splitlines()[0], ",".join(DOCUMENT_COLUMNS))


class WibTimeTests(unittest.TestCase):
    def test_log_times_are_wib(self):
        # 2026-08-05 00:15 UTC is 07:15 in Jakarta
        self.assertEqual(wib_iso(1_785_888_900_000), "2026-08-05T07:15:00+07:00")

    def test_a_wib_midnight_is_the_17_00_before_it_in_utc(self):
        self.assertEqual(wib_iso(wib_ms(WED)), "2026-08-05T00:00:00+07:00")
        self.assertEqual(wib_iso(wib_ms(WED) - 1000), "2026-08-04T23:59:59+07:00")

    def test_the_epoch_is_seven_in_the_morning(self):
        self.assertEqual(wib_iso(0), "1970-01-01T07:00:00+07:00")

    def test_the_offset_is_always_plus_07_00_and_never_z(self):
        for t_ms in (0, wib_ms(WED, 12), wib_ms(date(2026, 1, 1), 23, 59, 59)):
            with self.subTest(t_ms=t_ms):
                self.assertTrue(wib_iso(t_ms).endswith("+07:00"))

    def test_a_time_that_is_not_a_whole_second_is_refused_and_not_cut(self):
        for t_ms in (1_785_888_900_001, -1500):
            with self.subTest(t_ms=t_ms):
                with self.assertRaisesRegex(ValueError, "whole second"):
                    wib_iso(t_ms)

    def test_a_time_that_is_not_whole_milliseconds_is_refused(self):
        for bad in (1_785_888_900_000.0, True, None, "1785888900000"):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(ValueError, "whole milliseconds"):
                    wib_iso(bad)

    def test_the_time_columns_of_a_row_are_wib(self):
        row = rows_of(trade_log_text([long_trade()]))[0]

        self.assertEqual(row["waktu_sinyal"], "2026-08-05T07:00:00+07:00")
        self.assertEqual(row["waktu_entry"], "2026-08-05T07:15:00+07:00")
        self.assertEqual(row["waktu_exit"], "2026-08-05T08:00:00+07:00")


class RowTests(unittest.TestCase):
    def test_a_long_row_is_written_field_by_field(self):
        text = trade_log_text([long_trade()])

        self.assertEqual(text, ",".join(DOCUMENT_COLUMNS) + "\n" + LONG_ROW + "\n")

    def test_every_column_comes_from_the_field_the_document_means(self):
        row = rows_of(trade_log_text([long_trade()]))[0]

        self.assertEqual(
            row,
            {
                "trade_id": "1",
                "pair": "BTC-USDT",
                "mode": "A",
                "sisi": "LONG",
                "kondisi": "NAIK",
                "sma20_1h": "101.5",
                "sma50_1h": "99.25",
                "ema50_4h": "100.75",
                "atr14_1h": "2.5",
                "faktor_k": "1.0",
                "waktu_sinyal": "2026-08-05T07:00:00+07:00",
                "waktu_entry": "2026-08-05T07:15:00+07:00",
                "harga_entry": "100.01",
                "harga_tp": "100.41004",
                "harga_sl": "99.759975",
                "waktu_exit": "2026-08-05T08:00:00+07:00",
                "harga_exit": "100.39959",
                "alasan_exit": "TP",
                "fee_usdt": "17.5",
                "slippage_usdt": "5.0",
                "pnl_usdt": "12.25",
                "notional_usdt": "25000.0",
                "volume_usdt": "50000.0",
            },
        )

    def test_a_short_row_says_short_and_carries_its_own_mode_and_condition(self):
        short = long_trade(
            mode=MODE_D,
            side=SHORT,
            kondisi=TURUN,
            context=make_context(kondisi=TURUN, sma20_1h=98.0, sma50_1h=99.5),
            exit_reason=EXIT_SL,
            pnl_usdt=-30.0,
        )

        row = rows_of(trade_log_text([short]))[0]

        self.assertEqual(
            (row["mode"], row["sisi"], row["kondisi"], row["alasan_exit"], row["pnl_usdt"]),
            ("D", "SHORT", "TURUN", "SL", "-30.0"),
        )
        self.assertEqual((row["sma20_1h"], row["sma50_1h"]), ("98.0", "99.5"))

    def test_the_condition_is_the_trades_and_not_the_context(self):
        row = rows_of(trade_log_text([long_trade(kondisi=TURUN)]))[0]

        self.assertEqual(row["kondisi"], "TURUN")

    def test_the_exit_reason_is_written_as_tp_sl_or_waktu(self):
        trades = [long_trade(exit_reason=reason) for reason in (EXIT_TP, EXIT_SL, EXIT_WAKTU)]

        reasons = [row["alasan_exit"] for row in rows_of(trade_log_text(trades))]

        self.assertEqual(reasons, ["TP", "SL", "WAKTU"])

    def test_the_prices_are_the_fills_with_their_slippage(self):
        row = rows_of(trade_log_text([long_trade(entry_fill=100.01, exit_fill=99.74)]))[0]

        self.assertEqual((row["harga_entry"], row["harga_exit"]), ("100.01", "99.74"))

    def test_a_side_that_is_neither_long_nor_short_is_refused(self):
        for side in (0, 2, 1.0, -1.0, True, None, "LONG"):
            with self.subTest(side=side):
                with self.assertRaisesRegex(ValueError, "side"):
                    trade_log_text([long_trade(side=side)])

    def test_a_missing_indicator_is_an_empty_cell_and_not_a_lost_trade(self):
        ctx = make_context(sma50_1h=None, ema50_4h=None)

        row = rows_of(trade_log_text([long_trade(context=ctx)]))[0]

        self.assertEqual((row["sma50_1h"], row["ema50_4h"]), ("", ""))
        self.assertEqual((row["sma20_1h"], row["atr14_1h"]), ("100.0", "1.0"))

    def test_the_close_and_the_median_of_the_context_are_not_columns(self):
        header = trade_log_text([long_trade()]).splitlines()[0]

        self.assertNotIn("close_4h", header)
        self.assertNotIn("atr_median", header)


class TradeIdAndOrderTests(unittest.TestCase):
    def test_trade_id_is_the_one_based_row_number(self):
        trades = [long_trade(pnl_usdt=float(i)) for i in range(5)]

        rows = rows_of(trade_log_text(trades))

        self.assertEqual([row["trade_id"] for row in rows], ["1", "2", "3", "4", "5"])

    def test_trade_id_starts_over_in_every_log(self):
        first = rows_of(trade_log_text([long_trade(), long_trade()]))
        second = rows_of(trade_log_text([long_trade()]))

        self.assertEqual([row["trade_id"] for row in first], ["1", "2"])
        self.assertEqual([row["trade_id"] for row in second], ["1"])

    def test_the_rows_keep_the_order_of_the_trades(self):
        trades = [long_trade(pnl_usdt=pnl) for pnl in (3.0, -1.0, 7.0, 0.5)]

        rows = rows_of(trade_log_text(trades))

        self.assertEqual([row["pnl_usdt"] for row in rows], ["3.0", "-1.0", "7.0", "0.5"])

    def test_any_iterable_of_trades_is_written(self):
        trades = [long_trade(pnl_usdt=1.0), long_trade(pnl_usdt=2.0)]

        self.assertEqual(trade_log_text(t for t in trades), trade_log_text(trades))
        self.assertEqual(trade_log_text(tuple(trades)), trade_log_text(trades))


class FormatTests(unittest.TestCase):
    def test_every_line_ends_with_a_newline_and_nothing_else(self):
        text = trade_log_text([long_trade(), long_trade()])

        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\r", text)
        self.assertEqual(len(text.split("\n")), 4)  # header, two rows, the empty tail

    def test_every_row_has_23_cells(self):
        text = trade_log_text([long_trade(), long_trade(context=make_context(sma50_1h=None))])

        for line in csv.reader(io.StringIO(text)):
            self.assertEqual(len(line), 23)

    def test_numbers_use_a_decimal_point_and_no_thousands_separator(self):
        row = rows_of(
            trade_log_text([long_trade(notional_usdt=1_250_000.5, volume_usdt=2_500_001.0)])
        )[0]

        self.assertEqual((row["notional_usdt"], row["volume_usdt"]), ("1250000.5", "2500001.0"))

    def test_floats_round_trip_exactly(self):
        ctx = make_context(
            sma20_1h=0.1 + 0.2,
            sma50_1h=1 / 3,
            atr14_1h=1e-7,
            ema50_4h=123456789.123456789,
        )
        t = long_trade(
            context=ctx,
            faktor_k=1.2345678901234567,
            entry_fill=67890.12345678912,
            tp=2.5e-05,
            sl=99999.99999999999,
            exit_fill=0.30000000000000004,
            fee_usdt=17.499999999999996,
            slippage_usdt=4.440892098500626e-16,
            pnl_usdt=-12.000000000000002,
            notional_usdt=25_000.0,
            volume_usdt=49_999.99999999999,
        )

        row = rows_of(trade_log_text([t]))[0]

        wanted = {
            "sma20_1h": ctx.sma20_1h,
            "sma50_1h": ctx.sma50_1h,
            "atr14_1h": ctx.atr14_1h,
            "ema50_4h": ctx.ema50_4h,
            "faktor_k": t.faktor_k,
            "harga_entry": t.entry_fill,
            "harga_tp": t.tp,
            "harga_sl": t.sl,
            "harga_exit": t.exit_fill,
            "fee_usdt": t.fee_usdt,
            "slippage_usdt": t.slippage_usdt,
            "pnl_usdt": t.pnl_usdt,
            "notional_usdt": t.notional_usdt,
            "volume_usdt": t.volume_usdt,
        }
        for column, value in wanted.items():
            with self.subTest(column=column):
                self.assertEqual(float(row[column]), value)

    def test_the_times_parse_back_to_the_same_instant(self):
        t = long_trade()

        row = rows_of(trade_log_text([t]))[0]

        for column, ms in (
            ("waktu_sinyal", t.signal_ms),
            ("waktu_entry", t.entry_ms),
            ("waktu_exit", t.exit_ms),
        ):
            with self.subTest(column=column):
                parsed = datetime.fromisoformat(row[column])
                self.assertEqual((parsed - EPOCH) // timedelta(milliseconds=1), ms)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_the_file_holds_the_log_text(self):
        trades = [long_trade(), long_trade(pnl_usdt=-3.5)]
        path = self.dir / "mode_A_trades.csv"

        write_trade_log(path, trades)

        self.assertEqual(path.read_text(encoding="utf-8"), trade_log_text(trades))

    def test_a_text_path_is_accepted(self):
        path = self.dir / "text_path.csv"

        write_trade_log(str(path), [long_trade()])

        self.assertTrue(path.exists())

    def test_the_folder_is_made_when_it_is_missing(self):
        path = self.dir / "out" / "deeper" / "log.csv"

        write_trade_log(path, [long_trade()])

        self.assertTrue(path.exists())

    def test_an_existing_log_is_never_overwritten(self):
        path = self.dir / "log.csv"
        write_trade_log(path, [long_trade(pnl_usdt=1.0)])
        original = path.read_bytes()

        with self.assertRaisesRegex(FileExistsError, "trade log file already exists"):
            write_trade_log(path, [long_trade(pnl_usdt=2.0)])

        self.assertEqual(path.read_bytes(), original)

    def test_a_log_that_cannot_be_built_leaves_no_file(self):
        path = self.dir / "never.csv"

        with self.assertRaises(ValueError):
            write_trade_log(path, [long_trade(side=0)])

        self.assertFalse(path.exists())
        self.assertEqual(list(self.dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
