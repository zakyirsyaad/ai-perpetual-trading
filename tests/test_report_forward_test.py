from __future__ import annotations

import asyncio
import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import report_forward_test as report
from bot.core.forward_guard import CycleTotals
from bot.models import ClosedTrade, PositionDirection

WIB = timezone(timedelta(hours=7))


def _trade(
    pnl: float = 1.0,
    reason: str = "neutral_exit",
    closed: datetime | None = None,
    entry: float = 100.0,
    exit_: float = 101.0,
    qty: float = 10.0,
    direction: PositionDirection = PositionDirection.LONG,
) -> ClosedTrade:
    closed = closed or datetime(2026, 10, 12, 12, 0, tzinfo=WIB)
    return ClosedTrade(
        symbol="BTC/USDT",
        direction=direction,
        entry_price=entry,
        exit_price=exit_,
        quantity=qty,
        pnl_usdt=pnl,
        reason=reason,
        entry_reason="ai_volume",
        opened_at=closed - timedelta(minutes=20),
        closed_at=closed,
        exchange_entry_order_id="e",
        exchange_exit_order_id="x",
    )


FROM, TO = date(2026, 10, 12), date(2026, 10, 19)


class SummaryTests(unittest.TestCase):
    def test_summary_numbers(self) -> None:
        trades = [
            _trade(pnl=10.0, entry=100.0, exit_=100.0, qty=10.0),  # volume 2000
            _trade(pnl=-4.0, entry=100.0, exit_=100.0, qty=10.0),  # volume 2000
            _trade(pnl=0.0, entry=100.0, exit_=100.0, qty=5.0),  # volume 1000, flat is not a win
        ]
        s = report.summarize(trades, FROM, TO)
        self.assertEqual(s.posisi, 3)
        self.assertAlmostEqual(s.entry_per_hari_kalender, 3 / 7)
        self.assertAlmostEqual(s.untung_pct, 100 / 3)
        self.assertAlmostEqual(s.hasil_per_1_juta, 1_000_000 * 6.0 / 5000.0)

    def test_empty_window_has_no_result(self) -> None:
        s = report.summarize([], FROM, TO)
        self.assertEqual(s.posisi, 0)
        self.assertIsNone(s.untung_pct)  # no positions: the win rate is not measurable, not 0 %
        self.assertIsNone(s.hasil_per_1_juta)
        self.assertIsNone(s.hasil_tanpa_teratas)

    def test_empty_window_renders_a_dash_for_the_win_rate(self) -> None:
        text = report.render_summary(report.summarize([], FROM, TO), FROM, TO)
        line = next(row for row in text.splitlines() if row.startswith("untung"))
        self.assertEqual(line.split(":", 1)[1].strip(), "-")
        self.assertIn("33.3%", report.render_summary(
            report.summarize([_trade(pnl=1.0), _trade(pnl=-1.0), _trade(pnl=-2.0)], FROM, TO), FROM, TO))

    def test_zero_volume_gives_none(self) -> None:
        s = report.summarize([_trade(pnl=3.0, qty=0.0)], FROM, TO)
        self.assertIsNone(s.hasil_per_1_juta)

    def test_exit_mapping(self) -> None:
        reasons = [
            "neutral_exit",
            "take_profit",
            "stop_loss",
            "time_stop",
            "strategy_exit:thesis_broken",
            "strategy_exit",
            "liquidation_guard",
            "",
        ]
        s = report.summarize([_trade(reason=r) for r in reasons], FROM, TO)
        self.assertEqual(s.exits, {"tp": 2, "sl": 1, "waktu": 1, "ai": 2, "lain": 2})
        self.assertEqual(sum(s.exits.values()), s.posisi)

    def test_top_three_dropped(self) -> None:
        pnls = [50.0, 40.0, 30.0, -10.0, -20.0]
        trades = [_trade(pnl=p, entry=100.0, exit_=100.0, qty=10.0) for p in pnls]  # volume 2000 each
        s = report.summarize(trades, FROM, TO)
        self.assertAlmostEqual(s.hasil_tanpa_teratas, 1_000_000 * (-30.0) / 4000.0)
        self.assertAlmostEqual(s.hasil_per_1_juta, 1_000_000 * 90.0 / 10000.0)

    def test_three_or_fewer_trades_leave_nothing_after_the_drop(self) -> None:
        s = report.summarize([_trade(pnl=p) for p in (1.0, 2.0, 3.0)], FROM, TO)
        self.assertIsNone(s.hasil_tanpa_teratas)

    def test_window_is_wib_and_end_exclusive(self) -> None:
        inside_start = _trade(closed=datetime(2026, 10, 12, 0, 0, tzinfo=WIB))
        just_before = _trade(closed=datetime(2026, 10, 11, 23, 59, 59, tzinfo=WIB))
        last_inside = _trade(closed=datetime(2026, 10, 18, 23, 59, 59, tzinfo=WIB))
        at_end = _trade(closed=datetime(2026, 10, 19, 0, 0, tzinfo=WIB))
        # 17:00 UTC on 11 Oct is already 00:00 WIB on 12 Oct.
        utc_edge = _trade(closed=datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc))
        utc_late = _trade(closed=datetime(2026, 10, 18, 17, 0, tzinfo=timezone.utc))  # 19 Oct 00:00 WIB
        trades = [inside_start, just_before, last_inside, at_end, utc_edge, utc_late]
        picked = report.trades_in_window(trades, FROM, TO)
        self.assertEqual(picked, [inside_start, last_inside, utc_edge])
        self.assertEqual(report.summarize(trades, FROM, TO).posisi, 3)

    def test_empty_or_reversed_window_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            report.summarize([], FROM, FROM)
        with self.assertRaises(ValueError):
            report.summarize([], TO, FROM)


class ExportTests(unittest.TestCase):
    def test_csv_columns_and_exclusive_write(self) -> None:
        trade = _trade(pnl=2.5, reason="stop_loss", entry=100.0, exit_=101.0, qty=3.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            report.export_csv([trade], path)
            rows = list(csv.DictReader(io.StringIO(path.read_text())))
            self.assertEqual(
                list(rows[0].keys()),
                [
                    "opened_at", "closed_at", "direction", "entry_price", "exit_price",
                    "quantity", "pnl_usdt", "reason", "volume_usdt",
                ],
            )
            row = rows[0]
            self.assertEqual(row["direction"], "LONG")
            self.assertEqual(row["reason"], "stop_loss")
            self.assertAlmostEqual(float(row["volume_usdt"]), (100.0 + 101.0) * 3.0)
            self.assertAlmostEqual(float(row["pnl_usdt"]), 2.5)
            self.assertIsNotNone(datetime.fromisoformat(row["closed_at"]).tzinfo)
            self.assertIsNotNone(datetime.fromisoformat(row["opened_at"]).tzinfo)
            with self.assertRaises(FileExistsError):
                report.export_csv([trade], path)


class _FakeStore:
    def __init__(self, trades: list[ClosedTrade], totals: CycleTotals) -> None:
        self._trades = trades
        self._totals = totals
        self.closed = False

    async def closed_trades_since_cycle(self) -> list[ClosedTrade]:
        return list(self._trades)

    async def cycle_trade_totals(self) -> CycleTotals:
        return self._totals

    async def close(self) -> None:
        self.closed = True


def _settings(**overrides):
    base = dict(forward_kill_after_positions=60, forward_kill_below_per_1m=-500.0, forward_review_at_positions=150)
    base.update(overrides)
    return SimpleNamespace(**base)


class CliTests(unittest.TestCase):
    def _run(self, argv: list[str], store: _FakeStore, settings=None) -> tuple[int, str, str]:
        args = report.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = asyncio.run(report.execute(args, store, settings or _settings()))
        return code, out.getvalue(), err.getvalue()

    def test_prints_summary_guard_and_writes_the_window_csv(self) -> None:
        trades = [_trade(pnl=5.0), _trade(pnl=-2.0, closed=datetime(2026, 10, 30, tzinfo=WIB))]
        store = _FakeStore(trades, CycleTotals(count=12, pnl_usdt=-30.0, volume_usdt=100_000.0))
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._run(
                ["--from", "2026-10-12", "--to", "2026-10-19", "--out-dir", tmp], store
            )
            self.assertEqual(code, 0, err)
            self.assertIn("posisi", out.lower())
            self.assertIn("12", out)
            self.assertIn("60", out)
            self.assertIn("150", out)
            self.assertIn("aktif", out)
            csv_path = Path(tmp) / "forward_trades_2026-10-12_2026-10-19.csv"
            rows = list(csv.DictReader(io.StringIO(csv_path.read_text())))
            self.assertEqual(len(rows), 1)
        self.assertTrue(store.closed)

    def test_guard_reason_is_shown_when_tripped(self) -> None:
        store = _FakeStore([], CycleTotals(count=150, pnl_usdt=0.0, volume_usdt=1_000_000.0))
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self._run(["--from", "2026-10-12", "--to", "2026-10-19", "--out-dir", tmp], store)
        self.assertEqual(code, 0)
        self.assertIn("forward_review", out)

    def test_a_short_row_is_flagged_in_the_summary_and_still_exported(self) -> None:
        store = _FakeStore(
            [_trade(pnl=5.0), _trade(pnl=-2.0, direction=PositionDirection.SHORT)],
            CycleTotals(count=2, pnl_usdt=3.0, volume_usdt=4000.0),
        )
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._run(["--from", "2026-10-12", "--to", "2026-10-19", "--out-dir", tmp], store)
            rows = list(csv.DictReader(io.StringIO((Path(tmp) / "forward_trades_2026-10-12_2026-10-19.csv").read_text())))
        self.assertEqual(code, 0, err)
        self.assertIn("PERINGATAN: 1 posisi bukan LONG", out)
        self.assertEqual(len(rows), 2)

    def test_an_all_long_week_prints_no_warning(self) -> None:
        store = _FakeStore([_trade()], CycleTotals(count=1, pnl_usdt=1.0, volume_usdt=2000.0))
        with tempfile.TemporaryDirectory() as tmp:
            _, out, _ = self._run(["--from", "2026-10-12", "--to", "2026-10-19", "--out-dir", tmp], store)
        self.assertNotIn("PERINGATAN", out)

    def test_prints_the_cycle_start_and_warns_when_the_window_begins_before_it(self) -> None:
        cycle = datetime(2026, 10, 14, 7, 0, tzinfo=WIB)
        store = _FakeStore([_trade()], CycleTotals(count=1, pnl_usdt=1.0, volume_usdt=2000.0))
        with tempfile.TemporaryDirectory() as tmp:
            _, out, _ = self._run(
                ["--from", "2026-10-12", "--to", "2026-10-19", "--out-dir", tmp], store,
                _settings(volume_cycle_started_at=cycle),
            )
        self.assertIn("2026-10-14", out)  # VOLUME_CYCLE_STARTED_AT is printed
        self.assertIn("PERINGATAN: --from 2026-10-12 lebih awal dari VOLUME_CYCLE_STARTED_AT", out)

    def test_no_cycle_warning_when_the_window_begins_at_or_after_it(self) -> None:
        cycle = datetime(2026, 10, 12, 0, 0, tzinfo=WIB)
        store = _FakeStore([_trade()], CycleTotals(count=1, pnl_usdt=1.0, volume_usdt=2000.0))
        with tempfile.TemporaryDirectory() as tmp:
            _, out, _ = self._run(
                ["--from", "2026-10-12", "--to", "2026-10-19", "--out-dir", tmp], store,
                _settings(volume_cycle_started_at=cycle),
            )
        self.assertIn("VOLUME_CYCLE_STARTED_AT", out)
        self.assertNotIn("PERINGATAN", out)

    def test_a_settings_without_a_cycle_start_prints_tidak_diatur(self) -> None:
        store = _FakeStore([], CycleTotals(0, 0.0, 0.0))
        with tempfile.TemporaryDirectory() as tmp:
            _, out, _ = self._run(["--from", "2026-10-12", "--to", "2026-10-19", "--out-dir", tmp], store)
        self.assertIn("VOLUME_CYCLE_STARTED_AT", out)
        self.assertIn("tidak diatur", out)

    def test_reversed_window_exits_2_with_one_error_line(self) -> None:
        store = _FakeStore([], CycleTotals(0, 0.0, 0.0))
        code, _, err = self._run(["--from", "2026-10-19", "--to", "2026-10-12"], store)
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertTrue(err.startswith("error:"))
        self.assertTrue(store.closed)

    def test_existing_output_exits_1_and_is_not_overwritten(self) -> None:
        store = _FakeStore([_trade()], CycleTotals(1, 1.0, 2000.0))
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "forward_trades_2026-10-12_2026-10-19.csv"
            existing.write_text("keep")
            code, _, err = self._run(["--from", "2026-10-12", "--to", "2026-10-19", "--out-dir", tmp], store)
            self.assertEqual(code, 1)
            self.assertEqual(len(err.strip().splitlines()), 1)
            self.assertEqual(existing.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
