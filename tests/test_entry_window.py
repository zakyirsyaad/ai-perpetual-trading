"""Weekly entry window: no new entries outside Monday 07:00 - Friday 24:00 (trading timezone).

The operator wants weekdays only. Open positions must keep being managed, so only the
decision to *open* is gated, and the model is not even called while the window is closed.
"""
from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bot.core.entry_window import parse_weekly_window, window_is_open

try:  # `python -m unittest tests.x` (package) and `discover -s tests` (top-level) both work
    from tests.test_trade_engine_execution import (
        FakeClient, FakeRisk, FakeStore, FakeStrategy, _engine, _plan, _settings,
    )
except ImportError:  # pragma: no cover - depends on how unittest was launched
    from test_trade_engine_execution import (
        FakeClient, FakeRisk, FakeStore, FakeStrategy, _engine, _plan, _settings,
    )

WIB = ZoneInfo("Asia/Jakarta")
WINDOW = "mon 07:00-sat 00:00"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-10-05 is a Monday, so day 0 = Mon ... day 6 = Sun, day 7 = the next Monday.
    return datetime(2026, 10, 5 + day, hour, minute, tzinfo=WIB)


class ParseWeeklyWindowTests(unittest.TestCase):
    def test_empty_means_always_open(self) -> None:
        self.assertIsNone(parse_weekly_window(""))
        self.assertIsNone(parse_weekly_window("   "))
        self.assertTrue(window_is_open(None, at(5, 12)))

    def test_friday_24_is_saturday_midnight(self) -> None:
        self.assertEqual(parse_weekly_window("mon 07:00-fri 24:00"), parse_weekly_window(WINDOW))

    def test_is_case_and_space_tolerant(self) -> None:
        self.assertEqual(parse_weekly_window(" MON 7:00 - Sat 00:00 "), parse_weekly_window(WINDOW))

    def test_rejects_malformed_windows(self) -> None:
        for bad in ("mon 07:00", "mon 07:00-mon 07:00", "fri 12:00-mon 07:00", "funday 07:00-sat 00:00",
                    "mon 25:00-sat 00:00", "mon 07:00-fri 24:30", "mon 07:60-sat 00:00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_weekly_window(bad)


class WindowIsOpenTests(unittest.TestCase):
    def test_boundaries(self) -> None:
        window = parse_weekly_window(WINDOW)
        cases = [
            (at(0, 6, 59), False),    # Monday just before 07:00
            (at(0, 7, 0), True),      # Monday 07:00
            (at(2, 12), True),        # Wednesday noon
            (at(4, 23, 59), True),    # Friday 23:59
            (at(5, 0, 0), False),     # Saturday 00:00
            (at(6, 12), False),       # Sunday
        ]
        for moment, expected in cases:
            with self.subTest(moment=moment.isoformat()):
                self.assertEqual(window_is_open(window, moment), expected)


class TickWindowTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, now: datetime, strategy: FakeStrategy):
        engine = _engine(FakeClient(), FakeStore(), strategy, FakeRisk(), _settings(entry_weekly_window=WINDOW))
        engine._now_in_trading_tz = lambda: now
        return engine

    async def test_closed_window_pauses_entries_and_never_calls_the_strategy(self) -> None:
        strategy = FakeStrategy(_plan())
        engine = self._engine(at(5, 12), strategy)       # Saturday noon

        await engine._tick()

        self.assertEqual(strategy.next_calls, 0)

    async def test_open_window_lets_the_strategy_run(self) -> None:
        strategy = FakeStrategy(_plan())
        engine = self._engine(at(2, 12), strategy)       # Wednesday noon

        await engine._tick()

        self.assertEqual(strategy.next_calls, 1)

    async def test_no_window_configured_changes_nothing(self) -> None:
        strategy = FakeStrategy(_plan())
        engine = _engine(FakeClient(), FakeStore(), strategy, FakeRisk(), _settings())
        engine._now_in_trading_tz = lambda: at(5, 12)

        await engine._tick()

        self.assertEqual(strategy.next_calls, 1)

    async def test_closing_is_logged_once_not_every_tick(self) -> None:
        engine = self._engine(at(5, 12), FakeStrategy(_plan()))
        with self.assertLogs("test.engine", level="INFO") as captured:
            for _ in range(3):
                await engine._tick()
        paused = [line for line in captured.output if "entry window" in line.lower()]
        self.assertEqual(len(paused), 1)

    async def test_reopening_is_logged_and_entries_resume(self) -> None:
        strategy = FakeStrategy(_plan())
        engine = self._engine(at(5, 12), strategy)
        await engine._tick()
        engine._now_in_trading_tz = lambda: at(7, 7, 0)   # the following Monday 07:00
        with self.assertLogs("test.engine", level="INFO") as captured:
            await engine._tick()
        self.assertTrue(any("entry window" in line.lower() and "open" in line.lower() for line in captured.output))
        self.assertEqual(strategy.next_calls, 1)

    async def test_now_uses_the_configured_trading_timezone(self) -> None:
        engine = _engine(FakeClient(), FakeStore(), FakeStrategy(None), FakeRisk(),
                         _settings(trading_timezone="Asia/Jakarta"))
        self.assertEqual(str(engine._now_in_trading_tz().tzinfo), "Asia/Jakarta")


class WeeklyWindowSettingTests(unittest.TestCase):
    def test_default_is_disabled_and_value_is_validated(self) -> None:
        from bot.config import Settings

        self.assertEqual(Settings.model_fields["entry_weekly_window"].default, "")
        self.assertEqual(Settings.normalize_entry_weekly_window(" MON 7:00 - fri 24:00 "), "mon 07:00-sat 00:00")
        with self.assertRaises(ValueError):
            Settings.normalize_entry_weekly_window("fri 12:00-mon 07:00")


if __name__ == "__main__":
    unittest.main()
