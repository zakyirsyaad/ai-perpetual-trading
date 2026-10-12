"""History and Trades survive a bot restart: the feed is seeded from the trade store at startup.

The dashboard builds both tabs from fills on the chart tape, and the tape lives in memory, so every restart
used to leave them empty even though the day's trades were safely in the database. Each stored trade becomes
the two fills the live path would have produced (open, then close carrying the net PnL and the total fee).
"""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timezone

from bot.models import ClosedTrade, PositionDirection
from bot.storage.supabase_trade_store import SupabaseTradeStore
from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.observer import NULL_OBSERVER, EngineTelemetry
from bot.telemetry.wire import Meta, PricePointFill, PriceMessage

try:  # `python -m unittest tests.x` (package) and `discover -s tests` (top-level) both work
    from tests.test_trade_engine_execution import FakeClient, FakeRisk, FakeStore, FakeStrategy, _engine, _plan, _settings
    from tests.test_telemetry_server import SAMPLES
except ImportError:  # pragma: no cover - depends on how unittest was launched
    from test_trade_engine_execution import FakeClient, FakeRisk, FakeStore, FakeStrategy, _engine, _plan, _settings
    from test_telemetry_server import SAMPLES

META = Meta.model_validate(SAMPLES["meta"])
UTC = timezone.utc


def _trade(direction: PositionDirection, *, entry: float, exit_: float, qty: float, pnl: float,
           opened: datetime, closed: datetime, symbol: str = "BTC/USDT") -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol, direction=direction, entry_price=entry, exit_price=exit_, quantity=qty, pnl_usdt=pnl,
        reason="neutral_exit", entry_reason="test", opened_at=opened, closed_at=closed,
        exchange_entry_order_id="1", exchange_exit_order_id="2",
    )


T_OPEN = datetime(2026, 10, 12, 2, 0, 0, tzinfo=UTC)
T_CLOSE = datetime(2026, 10, 12, 2, 20, 0, tzinfo=UTC)


def _fills(hub: TelemetryHub) -> list[dict]:
    return [p for p in hub.tape_body()["BTC"] if p.get("fill")]


def _observer() -> tuple[EngineTelemetry, TelemetryHub]:
    hub = TelemetryHub(META, TelemetryBus())
    return EngineTelemetry(hub, ("BTC/USDT",), account_balance_usdt=1500.0), hub


class HubSeedFillsTests(unittest.TestCase):
    def test_seeded_fills_appear_on_the_tape_and_repeats_are_ignored(self) -> None:
        hub = TelemetryHub(META, TelemetryBus())
        fill = PricePointFill(side="buy", price=100.0, size=1.0, direction="open")
        hub.seed_fills("BTC", [(1_000, fill), (1_000, fill)])
        self.assertEqual(len(_fills(hub)), 1)

    def test_an_unknown_coin_is_dropped_without_raising(self) -> None:
        hub = TelemetryHub(META, TelemetryBus())
        hub.seed_fills("DOGE", [(1_000, PricePointFill(side="buy", price=1.0, size=1.0, direction="open"))])
        self.assertEqual(_fills(hub), [])


class ObserverHistoryTests(unittest.TestCase):
    def test_a_long_becomes_a_buy_open_then_a_sell_close_carrying_pnl_and_the_total_fee(self) -> None:
        observer, hub = _observer()
        # gross = (101 - 100) * 2 = 2.0, net pnl 0.5, so the total fee is 1.5
        observer.history_loaded([_trade(PositionDirection.LONG, entry=100.0, exit_=101.0, qty=2.0, pnl=0.5,
                                        opened=T_OPEN, closed=T_CLOSE)])
        opened, closed = _fills(hub)
        self.assertEqual((opened["ts"], opened["fill"]["side"], opened["fill"]["dir"], opened["fill"]["price"]),
                         (int(T_OPEN.timestamp() * 1000), "buy", "open", 100.0))
        self.assertEqual((closed["ts"], closed["fill"]["side"], closed["fill"]["dir"], closed["fill"]["price"]),
                         (int(T_CLOSE.timestamp() * 1000), "sell", "close", 101.0))
        self.assertEqual(closed["fill"]["closedPnl"], 0.5)
        self.assertAlmostEqual(closed["fill"]["feeUsd"], 1.5)
        self.assertEqual(opened["fill"]["size"], 2.0)

    def test_a_short_opens_with_a_sell_and_closes_with_a_buy(self) -> None:
        observer, hub = _observer()
        # gross = (100 - 99) * 1 = 1.0, net pnl -0.25, fee 1.25
        observer.history_loaded([_trade(PositionDirection.SHORT, entry=100.0, exit_=99.0, qty=1.0, pnl=-0.25,
                                        opened=T_OPEN, closed=T_CLOSE)])
        opened, closed = _fills(hub)
        self.assertEqual((opened["fill"]["side"], closed["fill"]["side"]), ("sell", "buy"))
        self.assertAlmostEqual(closed["fill"]["feeUsd"], 1.25)

    def test_a_malformed_trade_is_skipped_and_the_rest_still_load(self) -> None:
        observer, hub = _observer()
        bad = _trade(PositionDirection.LONG, entry=0.0, exit_=101.0, qty=1.0, pnl=0.0, opened=T_OPEN, closed=T_CLOSE)
        good = _trade(PositionDirection.LONG, entry=100.0, exit_=101.0, qty=1.0, pnl=0.5, opened=T_OPEN, closed=T_CLOSE)
        observer.history_loaded([bad, good])
        self.assertEqual(len(_fills(hub)), 2)

    def test_the_stored_realized_pnl_shows_in_the_totals_of_the_next_block(self) -> None:
        # The header reads realized/pnl from the latest block's totals, which restart from zero with the process.
        observer, hub = _observer()
        hub.record_price(PriceMessage(coin="BTC", ts=1_000, mid=100.0, best_bid=99.5, best_ask=100.5, spread_bps=1.0))
        observer.history_loaded([], realized_usd=86.76)
        observer.decided(None, latency_ms=1)                     # the first tick publishes a block
        totals = hub.root_body()["latestByCoin"]["BTC"]["totals"]
        self.assertAlmostEqual(totals["realizedUsd"], 86.76)
        self.assertAlmostEqual(totals["pnlUsd"], 86.76)

    def test_garbage_input_never_raises(self) -> None:
        observer, _ = _observer()
        observer.history_loaded(None)  # type: ignore[arg-type]


class StoreRecentClosedTradesTests(unittest.IsolatedAsyncioTestCase):
    async def test_rows_become_closed_trades_oldest_first_and_bad_rows_are_skipped(self) -> None:
        store = object.__new__(SupabaseTradeStore)
        import logging
        store._logger = logging.getLogger("test.store")
        store._volume_cycle_started_at = None

        async def records(limit: int):
            self.limit = limit
            return [  # newest first, as the query returns them
                {"symbol": "BTC/USDT", "direction": "SHORT", "entry_price": 100.0, "exit_price": 99.0, "quantity": 1.0,
                 "pnl_usdt": -0.25, "reason": "stop_loss", "entry_reason": "x", "opened_at": "2026-10-12T02:00:00.23315+00:00",
                 "closed_at": "2026-10-12T02:20:00.116863+00:00", "exchange_entry_order_id": None, "exchange_exit_order_id": "9"},
                {"symbol": "BTC/USDT", "direction": "nonsense", "entry_price": 1, "exit_price": 1, "quantity": 1,
                 "pnl_usdt": 0, "opened_at": "2026-10-12T01:00:00+00:00", "closed_at": "2026-10-12T01:10:00+00:00"},
                {"symbol": "BTC/USDT", "direction": "LONG", "entry_price": 100.0, "exit_price": 101.0, "quantity": 2.0,
                 "pnl_usdt": 0.5, "reason": "neutral_exit", "entry_reason": "y", "opened_at": "2026-10-11T02:00:00+00:00",
                 "closed_at": "2026-10-11T02:20:00+00:00", "exchange_entry_order_id": "1", "exchange_exit_order_id": "2"},
            ]

        store.recent_closed_trade_records = records  # type: ignore[method-assign]
        trades = await store.recent_closed_trades(60)
        self.assertEqual(self.limit, 60)
        self.assertEqual([t.direction for t in trades], [PositionDirection.LONG, PositionDirection.SHORT])
        self.assertEqual(trades[1].closed_at, datetime(2026, 10, 12, 2, 20, 0, 116863, tzinfo=UTC))
        self.assertEqual(trades[1].exchange_entry_order_id, "")


class StoreRealizedPnlTests(unittest.IsolatedAsyncioTestCase):
    async def test_sums_the_net_pnl_of_every_trade_since_the_cycle_start(self) -> None:
        store = object.__new__(SupabaseTradeStore)

        async def rows(select: str):
            self.selected = select
            return [{"pnl_usdt": -51.83}, {"pnl_usdt": 68.09}, {"pnl_usdt": 70.5}, {"pnl_usdt": None}]

        store._all_closed_trade_rows = rows  # type: ignore[method-assign]
        self.assertAlmostEqual(await store.realized_pnl_since_cycle(), 86.76)
        self.assertEqual(self.selected, "pnl_usdt")


class StoreCycleStartTests(unittest.IsolatedAsyncioTestCase):
    """The dashboard starts where the volume cycle starts: older trades are not shown."""

    def _store(self, cycle_start):
        import logging
        store = object.__new__(SupabaseTradeStore)
        store._logger = logging.getLogger("test.store")
        store._volume_cycle_started_at = cycle_start

        async def records(limit: int):
            return [
                {"symbol": "BTC/USDT", "direction": "SHORT", "entry_price": 100.0, "exit_price": 99.0, "quantity": 1.0,
                 "pnl_usdt": 1.0, "opened_at": "2026-10-12T02:00:00+00:00", "closed_at": "2026-10-12T02:20:00+00:00"},
                {"symbol": "BTC/USDT", "direction": "SHORT", "entry_price": 100.0, "exit_price": 99.0, "quantity": 1.0,
                 "pnl_usdt": 1.0, "opened_at": "2026-10-10T02:00:00+00:00", "closed_at": "2026-10-10T02:20:00+00:00"},
            ]

        store.recent_closed_trade_records = records  # type: ignore[method-assign]
        return store

    async def test_trades_closed_before_the_cycle_start_are_left_out(self) -> None:
        start = datetime(2026, 10, 11, 17, 0, tzinfo=UTC)          # 12 Oct 00:00 WIB
        trades = await self._store(start).recent_closed_trades(60)
        self.assertEqual([t.closed_at.day for t in trades], [12])

    async def test_without_a_cycle_start_every_stored_trade_is_kept(self) -> None:
        trades = await self._store(None).recent_closed_trades(60)
        self.assertEqual(len(trades), 2)


class _Recorder:
    def __init__(self) -> None:
        self.loaded: list = []
        self.realized: list = []

    def history_loaded(self, trades, *, realized_usd: float = 0.0) -> None:
        self.loaded.append(list(trades))
        self.realized.append(realized_usd)

    def __getattr__(self, name: str):
        return lambda *a, **k: None


class _HistoryStore(FakeStore):
    def __init__(self, trades=None, error: Exception | None = None) -> None:
        super().__init__()
        self.trades, self.error, self.reads = trades or [], error, 0

    realized = 0.0

    async def realized_pnl_since_cycle(self) -> float:
        return self.realized

    async def recent_closed_trades(self, limit: int = 100):
        self.reads += 1
        self.limit = limit
        if self.error:
            raise self.error
        return self.trades


class EngineSeedsHistoryTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, store: FakeStore):
        engine = _engine(FakeClient(), store, FakeStrategy(_plan()), FakeRisk(), _settings())
        engine._telemetry = _Recorder()
        return engine

    async def test_hands_the_observer_the_stored_trades(self) -> None:
        trade = _trade(PositionDirection.LONG, entry=100.0, exit_=101.0, qty=1.0, pnl=0.5, opened=T_OPEN, closed=T_CLOSE)
        store = _HistoryStore([trade])
        engine = self._engine(store)
        await engine._seed_dashboard_history()
        self.assertEqual(engine._telemetry.loaded, [[trade]])
        self.assertGreaterEqual(store.limit, 30)

    async def test_also_hands_over_the_realized_pnl_since_the_cycle_start(self) -> None:
        store = _HistoryStore([])
        store.realized = 86.76
        engine = self._engine(store)
        await engine._seed_dashboard_history()
        self.assertEqual(engine._telemetry.realized, [86.76])

    async def test_costs_nothing_when_the_dashboard_feed_is_off(self) -> None:
        store = _HistoryStore([])
        engine = self._engine(store)
        engine._telemetry = NULL_OBSERVER
        await engine._seed_dashboard_history()
        self.assertEqual(store.reads, 0)

    async def test_a_failing_store_read_never_stops_startup(self) -> None:
        engine = self._engine(_HistoryStore(error=RuntimeError("supabase is down")))
        with self.assertLogs("test.engine", level="WARNING"):
            await engine._seed_dashboard_history()
        self.assertEqual(engine._telemetry.loaded, [])

    async def test_startup_seeds_before_it_starts_watching_a_resumed_position(self) -> None:
        engine = self._engine(_HistoryStore([]))
        order: list[str] = []

        async def noop() -> None:
            return None

        async def seed() -> None:
            order.append("seed")

        async def resume() -> None:
            order.append("resume")

        async def stop() -> None:
            raise asyncio.CancelledError

        engine._startup_safety_checks = noop  # type: ignore[method-assign]
        engine._refresh_exchange_fee_rates = noop  # type: ignore[method-assign]
        engine._initialize_leverage = noop  # type: ignore[method-assign]
        engine._seed_dashboard_history = seed  # type: ignore[method-assign]
        engine._resume_persisted_positions = resume  # type: ignore[method-assign]
        engine._tick = stop  # type: ignore[method-assign]
        with self.assertRaises(asyncio.CancelledError):
            await engine.run_forever()
        self.assertEqual(order, ["seed", "resume"])


if __name__ == "__main__":
    unittest.main()
