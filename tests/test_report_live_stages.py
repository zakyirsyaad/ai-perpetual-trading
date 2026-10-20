from __future__ import annotations

import asyncio
import io
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import report_live_stages as report

UTC = timezone.utc
DAY0 = datetime(2026, 10, 21, 0, 0, tzinfo=UTC)
DAY1 = DAY0 + timedelta(days=1)
DAY2 = DAY0 + timedelta(days=2)
DAY4 = DAY0 + timedelta(days=4)
BUCKET = "SHORT|TURUN|atr_mid"


def _row(
    closed: datetime,
    pnl: float = 1.0,
    direction: str = "SHORT",
    entry: float = 100.0,
    exit_: float = 99.0,
    qty: float = 10.0,
    bucket: str | None = None,
) -> dict[str, Any]:
    setup = {"setup": {"bucket_key": bucket}} if bucket else {}
    return {
        "symbol": "BTC/USDT",
        "direction": direction,
        "entry_price": entry,
        "exit_price": exit_,
        "quantity": qty,
        "pnl_usdt": pnl,
        "reason": "neutral_exit",
        "closed_at": closed.isoformat(),
        "factor_metadata": setup,
    }


def _shadow(
    side: str = "long",
    bucket: str = "LONG|NAIK|atr_mid",
    gross: float | None = 5.0,
    source: str = "shadow_side",
    exit_reason: str = "floor",
) -> dict[str, Any]:
    return {
        "decided_at": DAY0.isoformat(),
        "symbol": "BTC/USDT",
        "side": side,
        "bucket_key": bucket,
        "source": source,
        "gross_bps": gross,
        "exit_reason": exit_reason,
        "evaluated_at": DAY1.isoformat(),
    }


class StageRowsTests(unittest.TestCase):
    def test_stages_split_at_each_start(self) -> None:
        rows = [
            _row(DAY0 - timedelta(seconds=1)),  # before the first stage: ignored
            _row(DAY0),  # a start belongs to the stage that opens there
            _row(DAY1 - timedelta(seconds=1)),
            _row(DAY1),
            _row(DAY1 + timedelta(hours=3)),
            _row(DAY1 + timedelta(hours=4)),
            _row(DAY2),
            _row(DAY4),  # at --until: excluded
            _row(DAY4 + timedelta(hours=1)),  # after --until: excluded
        ]
        stages = report.stage_rows(rows, [DAY0, DAY1, DAY2], DAY4)
        self.assertEqual([(s.start, s.end) for s in stages], [(DAY0, DAY1), (DAY1, DAY2), (DAY2, DAY4)])
        self.assertEqual([s.n for s in stages], [2, 3, 1])

    def test_starts_given_out_of_order_are_sorted(self) -> None:
        stages = report.stage_rows([_row(DAY1)], [DAY2, DAY0, DAY1], DAY4)
        self.assertEqual([s.start for s in stages], [DAY0, DAY1, DAY2])
        self.assertEqual([s.n for s in stages], [0, 1, 0])

    def test_net_per_1m_uses_both_legs(self) -> None:
        rows = [
            _row(DAY0 + timedelta(hours=1), pnl=12.5, entry=100.0, exit_=101.0, qty=3.0),
            _row(DAY0 + timedelta(hours=2), pnl=-4.0, entry=200.0, exit_=198.0, qty=5.0),
        ]
        (stage,) = report.stage_rows(rows, [DAY0], DAY1)
        volume = (100.0 + 101.0) * 3.0 + (200.0 + 198.0) * 5.0
        self.assertAlmostEqual(stage.net_per_1m, 1e6 * (12.5 - 4.0) / volume)

    def test_volume_per_day_divides_by_the_stage_length(self) -> None:
        rows = [_row(DAY0 + timedelta(hours=1), entry=100.0, exit_=100.0, qty=10.0)]  # volume 2000
        (stage,) = report.stage_rows(rows, [DAY0], DAY0 + timedelta(days=2))
        self.assertAlmostEqual(stage.volume_per_day, 1000.0)
        (half,) = report.stage_rows(rows, [DAY0], DAY0 + timedelta(hours=12))
        self.assertAlmostEqual(half.volume_per_day, 4000.0)

    def test_win_rate_is_the_share_of_positive_pnl(self) -> None:
        rows = [_row(DAY0 + timedelta(hours=h), pnl=p) for h, p in enumerate([5.0, -1.0, 0.0, 2.0])]
        (stage,) = report.stage_rows(rows, [DAY0], DAY1)
        self.assertAlmostEqual(stage.win_rate, 0.5)

    def test_gross_mean_and_se_come_from_the_entry_and_exit_prices(self) -> None:
        rows = [
            _row(DAY0 + timedelta(hours=1), direction="LONG", entry=100.0, exit_=101.0),  # +100 bps
            _row(DAY0 + timedelta(hours=2), direction="SHORT", entry=100.0, exit_=101.0),  # -100 bps
            _row(DAY0 + timedelta(hours=3), direction="LONG", entry=100.0, exit_=101.0),  # +100 bps
        ]
        (stage,) = report.stage_rows(rows, [DAY0], DAY1)
        self.assertAlmostEqual(stage.mean_gross_bps, 100.0 / 3)
        # sample stdev of (100, -100, 100) is 115.47, so the SE is 115.47 / sqrt(3) = 66.67
        self.assertAlmostEqual(stage.se_gross_bps, 200.0 / 3, places=6)

    def test_an_empty_stage_has_no_statistics(self) -> None:
        (stage,) = report.stage_rows([], [DAY0], DAY1)
        self.assertEqual(stage.n, 0)
        self.assertIsNone(stage.win_rate)
        self.assertIsNone(stage.mean_gross_bps)
        self.assertIsNone(stage.se_gross_bps)
        self.assertIsNone(stage.net_per_1m)
        self.assertEqual(stage.volume_per_day, 0.0)

    def test_a_single_trade_has_a_mean_but_no_se(self) -> None:
        (stage,) = report.stage_rows([_row(DAY0 + timedelta(hours=1))], [DAY0], DAY1)
        self.assertEqual(stage.n, 1)
        self.assertAlmostEqual(stage.mean_gross_bps, 100.0)
        self.assertIsNone(stage.se_gross_bps)

    def test_a_row_that_cannot_be_priced_counts_as_a_trade_but_not_in_the_gross(self) -> None:
        bad = _row(DAY0 + timedelta(hours=1), direction="FLAT")
        good = _row(DAY0 + timedelta(hours=2))
        (stage,) = report.stage_rows([bad, good], [DAY0], DAY1)
        self.assertEqual(stage.n, 2)
        self.assertAlmostEqual(stage.mean_gross_bps, 100.0)

    def test_a_row_without_prices_has_no_gross_and_no_volume(self) -> None:
        row = _row(DAY0 + timedelta(hours=1), entry=0.0, exit_=0.0, qty=10.0)
        (stage,) = report.stage_rows([row], [DAY0], DAY1)
        self.assertEqual(stage.n, 1)
        self.assertIsNone(stage.mean_gross_bps)
        self.assertIsNone(stage.net_per_1m)

    def test_rows_without_a_readable_close_time_are_left_out(self) -> None:
        broken = _row(DAY0)
        broken["closed_at"] = "not a time"
        missing = _row(DAY0)
        del missing["closed_at"]
        (stage,) = report.stage_rows([broken, missing, _row(DAY0 + timedelta(hours=1))], [DAY0], DAY1)
        self.assertEqual(stage.n, 1)

    def test_supabase_timestamps_with_five_digit_fractions_are_read(self) -> None:
        row = _row(DAY0)
        row["closed_at"] = "2026-10-21T05:00:00.12345+00:00"
        (stage,) = report.stage_rows([row], [DAY0], DAY1)
        self.assertEqual(stage.n, 1)

    def test_a_stage_that_starts_at_or_after_until_is_not_listed(self) -> None:
        stages = report.stage_rows([_row(DAY0)], [DAY0, DAY2, DAY4], DAY2)
        self.assertEqual([(s.start, s.end) for s in stages], [(DAY0, DAY2)])

    def test_until_not_after_the_first_start_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            report.stage_rows([], [DAY1], DAY0)
        with self.assertRaises(ValueError):
            report.stage_rows([], [DAY1], DAY1)

    def test_naive_times_duplicate_starts_and_no_starts_are_refused(self) -> None:
        naive = datetime(2026, 10, 21, 0, 0)
        with self.assertRaises(ValueError):
            report.stage_rows([], [naive], DAY4)
        with self.assertRaises(ValueError):
            report.stage_rows([], [DAY0], naive.replace(day=25))
        with self.assertRaises(ValueError):
            report.stage_rows([], [DAY0, DAY0], DAY4)
        with self.assertRaises(ValueError):
            report.stage_rows([], [], DAY4)


class ShadowSummaryTests(unittest.TestCase):
    def test_groups_by_side_and_bucket_over_both_sources(self) -> None:
        rows = [
            _shadow("long", "LONG|NAIK|atr_mid", 4.0, "shadow_side"),
            _shadow("long", "LONG|NAIK|atr_mid", 8.0, "ledger_block"),
            _shadow("short", "SHORT|NAIK|atr_mid", -3.0, "shadow_side"),
            _shadow("long", "LONG|TURUN|atr_high", 1.0, "ledger_block"),
        ]
        self.assertEqual(
            report.shadow_summary(rows),
            [
                ("LONG", "LONG|NAIK|atr_mid", 2, 6.0),
                ("LONG", "LONG|TURUN|atr_high", 1, 1.0),
                ("SHORT", "SHORT|NAIK|atr_mid", 1, -3.0),
            ],
        )

    def test_rows_without_a_result_are_not_averaged(self) -> None:
        rows = [
            _shadow(gross=None, exit_reason="expired"),
            _shadow(gross=float("nan")),
            _shadow(gross=2.0),
        ]
        self.assertEqual(report.shadow_summary(rows), [("LONG", "LONG|NAIK|atr_mid", 1, 2.0)])

    def test_the_unpriced_rows_are_counted_by_exit_reason(self) -> None:
        rows = [
            _shadow(gross=None, exit_reason="expired"),
            _shadow(gross=None, exit_reason="expired"),
            _shadow(gross=None, exit_reason="invalid"),
            _shadow(gross=2.0),
        ]
        self.assertEqual(report.shadow_unpriced(rows), {"expired": 2, "invalid": 1})

    def test_no_rows_gives_an_empty_summary(self) -> None:
        self.assertEqual(report.shadow_summary([]), [])
        self.assertEqual(report.shadow_unpriced([]), {})


class _FakeStore:
    """Only the two reads the report may use; any other attribute raises AttributeError."""

    def __init__(self, trades: list[dict], shadows: list[dict] | None = None) -> None:
        self._trades = trades
        self._shadows = shadows or []
        self.trade_since: list[datetime] = []
        self.shadow_since: list[datetime] = []
        self.closed = False

    async def closed_trade_rows_since(self, since: datetime) -> list[dict]:
        self.trade_since.append(since)
        return list(self._trades)

    async def shadow_outcomes_since(self, since: datetime) -> list[dict]:
        self.shadow_since.append(since)
        return list(self._shadows)

    async def close(self) -> None:
        self.closed = True


def _settings(**overrides: Any) -> SimpleNamespace:
    base = dict(setup_ledger_min_samples=2, setup_ledger_hurdle_bps=4.0)
    base.update(overrides)
    return SimpleNamespace(**base)


def _table_row(out: str, stage: str) -> list[str]:
    """The whitespace-split cells of a stage-table row: stage, start, end, n, win, gross, SE, net per 1M, volume per day."""
    line = next(row for row in out.splitlines() if row.startswith(f"{stage} "))
    return line.split()


class CliTests(unittest.TestCase):
    def _run(self, argv: list[str], store: _FakeStore, settings: Any = None) -> tuple[int, str, str]:
        args = report.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = asyncio.run(report.execute(args, store, settings or _settings()))
        return code, out.getvalue(), err.getvalue()

    def test_output_has_the_three_headings(self) -> None:
        store = _FakeStore([_row(DAY0 + timedelta(hours=1), bucket=BUCKET)], [_shadow()])
        code, out, err = self._run(
            ["--stage-start", DAY0.isoformat(), "--until", DAY1.isoformat()], store
        )
        self.assertEqual(code, 0, err)
        self.assertIn("Ringkasan per tahap", out)
        self.assertIn("Shadow per sisi dan kelompok", out)
        self.assertIn("Ledger", out)
        self.assertTrue(store.closed)

    def test_naive_stage_start_is_refused_exit_2(self) -> None:
        store = _FakeStore([])
        code, out, err = self._run(["--stage-start", "2026-10-21T00:00:00"], store)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertTrue(err.startswith("error:"))
        self.assertEqual(store.trade_since, [])  # nothing was read
        self.assertTrue(store.closed)

    def test_naive_until_is_refused_exit_2(self) -> None:
        store = _FakeStore([])
        code, _, err = self._run(["--stage-start", DAY0.isoformat(), "--until", "2026-10-25T00:00:00"], store)
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error:"))
        self.assertEqual(store.trade_since, [])

    def test_until_before_the_first_start_exits_2(self) -> None:
        store = _FakeStore([])
        code, _, err = self._run(
            ["--stage-start", DAY2.isoformat(), "--until", DAY1.isoformat()], store
        )
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(store.trade_since, [])

    def test_no_stage_start_exits_2(self) -> None:
        store = _FakeStore([])
        args = Namespace(stage_starts=[], until=None)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = asyncio.run(report.execute(args, store, _settings()))
        self.assertEqual(code, 2)
        self.assertTrue(err.getvalue().startswith("error:"))

    def test_the_parser_requires_a_stage_start(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            report.build_parser().parse_args([])
        self.assertEqual(raised.exception.code, 2)

    def test_the_parser_refuses_text_that_is_not_a_time(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            report.build_parser().parse_args(["--stage-start", "yesterday"])
        self.assertEqual(raised.exception.code, 2)

    def test_the_parser_reads_z_and_offsets(self) -> None:
        args = report.build_parser().parse_args(
            ["--stage-start", "2026-10-21T00:00:00Z", "--stage-start", "2026-10-22T07:00:00+07:00"]
        )
        self.assertEqual(args.stage_starts, [DAY0, DAY1])
        self.assertIsNone(args.until)

    def test_it_reads_since_the_earliest_start_and_drops_what_closed_at_or_after_until(self) -> None:
        until = DAY1 + timedelta(hours=1)
        trades = [
            _row(DAY0 + timedelta(hours=1), bucket=BUCKET),
            _row(DAY1 + timedelta(minutes=30), bucket=BUCKET),
            _row(until, bucket=BUCKET),  # at --until: out of the stages and out of the ledger
            _row(until + timedelta(hours=1), bucket=BUCKET),
        ]
        store = _FakeStore(trades)
        code, out, err = self._run(
            [
                "--stage-start", DAY1.isoformat(),
                "--stage-start", DAY0.isoformat(),
                "--until", until.isoformat(),
            ],
            store,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(store.trade_since, [DAY0])
        self.assertEqual(store.shadow_since, [DAY0])
        self.assertIn("posisi berkelompok: 2 dari 2", out)
        self.assertEqual([_table_row(out, "1")[3], _table_row(out, "2")[3]], ["1", "1"])

    def test_until_defaults_to_now(self) -> None:
        now = datetime.now(UTC)
        store = _FakeStore([_row(now - timedelta(hours=1))])
        code, out, err = self._run(["--stage-start", (now - timedelta(days=2)).isoformat()], store)
        self.assertEqual(code, 0, err)
        self.assertEqual(_table_row(out, "1")[3], "1")  # one position

    def test_the_stage_table_shows_the_figures(self) -> None:
        rows = [
            _row(DAY0 + timedelta(hours=1), pnl=10.0, entry=100.0, exit_=100.0, qty=10.0),  # volume 2000
            _row(DAY0 + timedelta(hours=2), pnl=-4.0, entry=100.0, exit_=100.0, qty=10.0),  # volume 2000
        ]
        code, out, err = self._run(
            ["--stage-start", DAY0.isoformat(), "--until", DAY1.isoformat()], _FakeStore(rows)
        )
        self.assertEqual(code, 0, err)
        cells = _table_row(out, "1")
        self.assertEqual(cells[3], "2")  # positions
        self.assertEqual(cells[4], "50,0%")  # win rate
        self.assertEqual(cells[7], "1.500")  # 1e6 * 6 / 4000
        self.assertEqual(cells[8], "4.000")  # volume per day

    def test_a_stage_that_has_not_started_is_noted_not_listed(self) -> None:
        code, out, err = self._run(
            ["--stage-start", DAY0.isoformat(), "--stage-start", DAY4.isoformat(), "--until", DAY1.isoformat()],
            _FakeStore([]),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("belum mulai", out)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("2 ")]), 0)

    def test_the_shadow_section_lists_groups_and_the_unpriced_rows(self) -> None:
        shadows = [
            _shadow("long", "LONG|NAIK|atr_mid", 4.0),
            _shadow("long", "LONG|NAIK|atr_mid", 8.0),
            _shadow(gross=None, exit_reason="expired"),
        ]
        _, out, _ = self._run(
            ["--stage-start", DAY0.isoformat(), "--until", DAY1.isoformat()], _FakeStore([], shadows)
        )
        shadow_part = out.split("Shadow per sisi dan kelompok", 1)[1].split("Ledger", 1)[0]
        self.assertIn("LONG|NAIK|atr_mid", shadow_part)
        self.assertIn("expired=1", shadow_part)

    def test_the_ledger_lists_each_bucket_with_its_status(self) -> None:
        trades = [
            _row(DAY0 + timedelta(hours=1), direction="SHORT", entry=100.0, exit_=100.5, bucket=BUCKET),
            _row(DAY0 + timedelta(hours=2), direction="SHORT", entry=100.0, exit_=100.6, bucket=BUCKET),
            _row(DAY0 + timedelta(hours=3)),  # no bucket: not in the ledger
        ]
        shadows = [_shadow("short", BUCKET, 20.0, "ledger_block"), _shadow("short", BUCKET, 22.0, "ledger_block")]
        _, out, _ = self._run(
            ["--stage-start", DAY0.isoformat(), "--until", DAY1.isoformat()], _FakeStore(trades, shadows)
        )
        ledger_part = out.split("Ledger", 1)[1]
        line = next(row for row in ledger_part.splitlines() if row.startswith(BUCKET))
        self.assertIn("reopened", line)  # real bound far below 4 bps, shadow bound far above it
        self.assertIn("posisi berkelompok: 2 dari 3", ledger_part)

    def test_an_empty_run_still_prints_all_sections(self) -> None:
        code, out, err = self._run(
            ["--stage-start", DAY0.isoformat(), "--until", DAY1.isoformat()], _FakeStore([])
        )
        self.assertEqual(code, 0, err)
        for heading in ("Ringkasan per tahap", "Shadow per sisi dan kelompok", "Ledger"):
            self.assertIn(heading, out)

    def test_rows_without_a_readable_time_are_reported(self) -> None:
        broken = _row(DAY0)
        broken["closed_at"] = None
        _, out, _ = self._run(
            ["--stage-start", DAY0.isoformat(), "--until", DAY1.isoformat()], _FakeStore([broken])
        )
        self.assertIn("PERINGATAN", out)


if __name__ == "__main__":
    unittest.main()
