"""The LONG forward bot's own stop: a kill on the pooled result and a review stop at a position count.

Both default off, so the first tests pin that nothing changes unconfigured. The engine tests drive a fake
store, never Supabase, and prove the latch is recomputed from the store on every tick (so a restart holds it).
"""
from __future__ import annotations

import logging
import unittest
from datetime import datetime, timezone

from bot.config import Settings
from bot.core.forward_guard import CycleTotals, forward_guard_reason
from bot.core.trade_engine import TradeEngine
from bot.models import OpenPosition, PositionDirection
from bot.storage.supabase_trade_store import SupabaseTradeStore

try:  # `python -m unittest tests.x` (package) and `discover -s tests` (top-level) both work
    from tests.test_trade_engine_execution import FakeClient, FakeRisk, FakeStore, FakeStrategy, _plan, _settings
except ImportError:  # pragma: no cover - depends on how unittest was launched
    from test_trade_engine_execution import FakeClient, FakeRisk, FakeStore, FakeStrategy, _plan, _settings

LOG_NAME = "test.engine"
KILL_AFTER = 60
KILL_BELOW = -500.0
REVIEW_AT = 150
MILLION = 1_000_000.0
NOW = datetime(2026, 10, 19, 1, 0, tzinfo=timezone.utc)


def _totals(count: int, per_1m: float, volume: float = MILLION) -> CycleTotals:
    """Totals whose pooled result is exactly `per_1m` per 1M of volume."""
    return CycleTotals(count=count, pnl_usdt=per_1m * volume / MILLION, volume_usdt=volume)


def _reason(totals: CycleTotals, kill_after=KILL_AFTER, kill_below=KILL_BELOW, review_at=REVIEW_AT):
    return forward_guard_reason(totals, kill_after, kill_below, review_at)


class GuardDecisionTests(unittest.TestCase):
    def test_off_by_default(self) -> None:
        self.assertIsNone(forward_guard_reason(_totals(500, -9999.0), 0, -500.0, 0))
        settings = Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", STRATEGY_MODE="volume")
        self.assertEqual(settings.forward_kill_after_positions, 0)
        self.assertEqual(settings.forward_kill_below_per_1m, -500.0)
        self.assertEqual(settings.forward_review_at_positions, 0)

    def test_nothing_before_60_positions_even_if_bad(self) -> None:
        self.assertIsNone(_reason(_totals(59, -5000.0)))

    def test_kill_at_and_after_60_when_below_minus_500(self) -> None:
        reason = _reason(_totals(60, -501.0))
        self.assertIsNotNone(reason)
        self.assertTrue(reason.startswith("forward_kill: 60 positions at -501"), reason)
        self.assertIn("limit -500", reason)
        self.assertIsNone(_reason(_totals(61, -499.0)))

    def test_below_is_strict(self) -> None:
        self.assertIsNone(_reason(_totals(60, -500.0)))

    def test_review_latches_at_150_regardless_of_result(self) -> None:
        for per_1m in (800.0, 0.0, -200.0):
            with self.subTest(per_1m=per_1m):
                self.assertEqual(
                    _reason(_totals(150, per_1m)),
                    "forward_review: 150 positions reached, the manager decides",
                )
        self.assertIsNone(_reason(_totals(149, 800.0)))

    def test_review_fires_even_without_volume_and_kill_wins_when_both_apply(self) -> None:
        self.assertTrue(_reason(CycleTotals(150, 0.0, 0.0)).startswith("forward_review"))
        self.assertTrue(_reason(_totals(150, -900.0)).startswith("forward_kill"))

    def test_zero_volume_has_nothing_to_judge(self) -> None:
        self.assertIsNone(_reason(CycleTotals(60, -50.0, 0.0)))

    def test_result_per_1m_is_pooled(self) -> None:
        # One small trade at -2000/1M and 59 big trades at -400/1M: the mean of ratios is far below -500
        # (about -427 is the pooled figure), so a mean-of-ratios guard would wrongly kill.
        small = (-0.2, 100.0)
        big_pnl, big_volume = -40.0, 100_000.0
        pnl = small[0] + 59 * big_pnl
        volume = small[1] + 59 * big_volume
        self.assertGreater(pnl / volume * MILLION, KILL_BELOW)
        self.assertIsNone(_reason(CycleTotals(60, pnl, volume)))
        # And the opposite: pooled worse than the limit latches.
        self.assertIsNotNone(_reason(CycleTotals(60, -60.0, 100_000.0)))  # -600 per 1M

    def test_the_reason_reports_the_pooled_figure(self) -> None:
        self.assertIn("at -600 per 1M", _reason(CycleTotals(60, -60.0, 100_000.0)))


class StoreTotalsTests(unittest.IsolatedAsyncioTestCase):
    async def test_cycle_trade_totals_pools_count_pnl_and_the_volume_definition(self) -> None:
        store = object.__new__(SupabaseTradeStore)

        async def rows(select: str):
            self.selected = select
            return [
                {"pnl_usdt": -10.0, "entry_price": 100.0, "exit_price": 101.0, "quantity": 2.0},
                {"pnl_usdt": 4.0, "entry_price": 50.0, "exit_price": 49.0, "quantity": 1.0},
                {"pnl_usdt": None, "entry_price": None, "exit_price": None, "quantity": None},
            ]

        store._all_closed_trade_rows = rows  # type: ignore[method-assign]
        totals = await store.cycle_trade_totals()

        self.assertEqual(self.selected, "pnl_usdt,entry_price,exit_price,quantity")
        self.assertEqual(totals.count, 3)
        self.assertAlmostEqual(totals.pnl_usdt, -6.0)
        self.assertAlmostEqual(totals.volume_usdt, 100 * 2 + 101 * 2 + 50 + 49)


class GuardStore(FakeStore):
    def __init__(self, totals: CycleTotals | Exception) -> None:
        super().__init__()
        self.totals = totals
        self.total_calls = 0

    async def cycle_trade_totals(self) -> CycleTotals:
        self.total_calls += 1
        if isinstance(self.totals, Exception):
            raise self.totals
        return self.totals


def _engine(store, strategy=None, **settings) -> tuple[TradeEngine, FakeStrategy]:
    strategy = strategy or FakeStrategy(_plan())
    engine = TradeEngine(_settings(**settings), logging.getLogger(LOG_NAME), FakeClient(), strategy, FakeRisk(), store)
    return engine, strategy


GUARD_ON = dict(forward_kill_after_positions=KILL_AFTER, forward_kill_below_per_1m=KILL_BELOW, forward_review_at_positions=REVIEW_AT)


class EngineGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_guard_off_makes_no_store_call_and_the_strategy_runs(self) -> None:
        store = GuardStore(_totals(500, -9999.0))
        engine, strategy = _engine(store)

        await engine._tick()

        self.assertEqual(store.total_calls, 0)
        self.assertEqual(strategy.next_calls, 1)

    async def test_a_kill_blocks_before_the_strategy_and_logs_error_with_the_reason(self) -> None:
        store = GuardStore(_totals(60, -501.0))
        engine, strategy = _engine(store, **GUARD_ON)

        with self.assertLogs(LOG_NAME, level="ERROR") as captured:
            await engine._tick()

        self.assertEqual(strategy.next_calls, 0)
        self.assertIn("forward_kill: 60 positions", "\n".join(captured.output))
        self.assertIsNone(engine._entries_blocked_reason)  # the supervisor's latch stays untouched

    async def test_a_review_blocks_regardless_of_result(self) -> None:
        engine, strategy = _engine(GuardStore(_totals(150, 900.0)), **GUARD_ON)
        with self.assertLogs(LOG_NAME, level="ERROR"):
            await engine._tick()
        self.assertEqual(strategy.next_calls, 0)

    async def test_a_good_result_below_the_review_count_lets_the_tick_run(self) -> None:
        engine, strategy = _engine(GuardStore(_totals(61, -499.0)), **GUARD_ON)
        await engine._tick()
        self.assertEqual(strategy.next_calls, 1)

    async def test_the_guard_runs_before_the_window_gate(self) -> None:
        store = GuardStore(_totals(60, -501.0))
        engine, _ = _engine(store, entry_weekly_window="Mon 00:00-00:01", **GUARD_ON)
        with self.assertLogs(LOG_NAME, level="ERROR"):
            await engine._tick()
        self.assertEqual(store.total_calls, 1)

    async def test_guard_is_recomputed_after_a_restart(self) -> None:
        store = GuardStore(_totals(60, -501.0))
        first, _ = _engine(store, **GUARD_ON)
        with self.assertLogs(LOG_NAME, level="ERROR"):
            await first._tick()

        fresh, strategy = _engine(store, **GUARD_ON)  # a new process against the same store
        with self.assertLogs(LOG_NAME, level="ERROR"):
            await fresh._tick()

        self.assertEqual(strategy.next_calls, 0)
        self.assertEqual(store.total_calls, 2)

    async def test_the_guard_lifts_when_the_store_no_longer_shows_the_breach(self) -> None:
        store = GuardStore(_totals(60, -501.0))
        engine, strategy = _engine(store, **GUARD_ON)
        with self.assertLogs(LOG_NAME, level="ERROR"):
            await engine._tick()
        store.totals = _totals(61, -499.0)
        await engine._tick()
        self.assertEqual(strategy.next_calls, 1)

    async def test_an_unreadable_store_fails_closed_without_crashing_the_loop(self) -> None:
        store = GuardStore(RuntimeError("supabase down"))
        engine, strategy = _engine(store, **GUARD_ON)

        with self.assertLogs(LOG_NAME, level="ERROR") as captured:
            await engine._tick()

        self.assertEqual(strategy.next_calls, 0)
        self.assertIn("forward guard", "\n".join(captured.output).lower())

    async def test_open_position_is_still_managed_while_latched(self) -> None:
        engine, strategy = _engine(GuardStore(_totals(150, -900.0)), **GUARD_ON)
        position = OpenPosition(
            symbol="BTC/USDT", direction=PositionDirection.LONG, entry_price=100.0, quantity=1.0,
            exchange_size=1.0, exchange_order_id="1", opened_at=NOW, stop_loss_pct=0.3, take_profit_pct=0.5,
        )
        managed: list[str] = []

        async def manage(pos, plan, **kwargs):
            managed.append(pos.symbol)

        engine._manage_open_position_guarded = manage  # type: ignore[method-assign]
        engine._plan_from_resumed_position = lambda pos: _plan()  # type: ignore[method-assign]
        engine._resumable_positions = [position]

        await engine._resume_persisted_positions()
        with self.assertLogs(LOG_NAME, level="ERROR"):
            await engine._tick()

        self.assertEqual(managed, ["BTC/USDT"])  # resume never consults the guard
        self.assertEqual(strategy.next_calls, 0)  # while new entries stay latched


if __name__ == "__main__":
    unittest.main()
