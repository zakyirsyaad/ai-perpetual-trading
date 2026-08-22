from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.core.trade_engine import TradeEngine
from bot.exchange.contracts import PositionDetails
from bot.models import OpenPosition, PositionDirection


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        bingx_env="vst",
        exchange_name="bingx",
        symbols=("BTC/USDT", "ETH/USDT"),
        position_usdt=180.0,
        leverage=100,
        stop_loss_pct=0.08,
        take_profit_pct=0.12,
        position_hold_minutes=60,
        max_stop_loss_per_day=2,
        max_consecutive_losses=3,
        max_spread_bps=6.0,
        funding_blackout_minutes=10,
        min_liquidation_distance_bps=250.0,
        margin_mode="isolated",
        position_mode="hedge",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeClient:
    venue = "bingx"

    def __init__(self, open_symbols: set[str], *, details: dict | None = None) -> None:
        self._open = set(open_symbols)
        self.flattened: list[str] = []
        self._details = details or {}
        self.details_error = False

    async def has_open_position(self, symbol: str) -> bool:
        return symbol in self._open

    async def get_symbol_position_details(self, symbol: str) -> PositionDetails:
        if self.details_error:
            raise RuntimeError("positions endpoint down")
        override = self._details.get(symbol, {})
        size = 0.01 if symbol in self._open else 0.0
        return PositionDetails(
            symbol=symbol,
            exchange_size=override.get("exchange_size", size),
            base_quantity=override.get("exchange_size", size),
            avg_price=override.get("avg_price", 60000.0 if size else None),
            mark_price=None,
            liquidation_price=None,
            pos_side=override.get("pos_side", "LONG" if size else None),
            raw={},
            position_id=override.get("position_id"),
        )

    async def flatten_position(self, symbol: str):
        self.flattened.append(symbol)
        self._open.discard(symbol)
        return {"ok": True}


class _FakeStore:
    def __init__(self, positions: list[OpenPosition]) -> None:
        self._positions = list(positions)
        self.removed: list[str] = []

    async def list_open_positions(self) -> list[OpenPosition]:
        return list(self._positions)

    async def remove_open_position(self, symbol: str) -> None:
        self.removed.append(symbol)
        self._positions = [item for item in self._positions if item.symbol != symbol]


def _position(symbol: str = "BTC/USDT") -> OpenPosition:
    return OpenPosition(
        symbol=symbol,
        direction=PositionDirection.LONG,
        entry_price=60000.0,
        quantity=0.01,
        exchange_size=0.01,
        exchange_order_id="order-1",
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        entry_reason="ai_volume:OPEN_LONG",
    )


def _engine(client, store, settings=None) -> TradeEngine:
    engine = object.__new__(TradeEngine)
    engine._settings = settings or _settings()
    engine._logger = logging.getLogger("test.reconcile")
    engine._client = client
    engine._trade_store = store
    engine._entries_blocked_reason = None
    engine._resumable_positions = []
    engine._last_entry_ts = None
    engine._last_stop_loss_ts = None
    return engine


class ReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_opposite_side_is_treated_as_unknown_exposure(self) -> None:
        # A persisted LONG must never be matched to an exchange SHORT.
        client = _FakeClient({"BTC/USDT"}, details={"BTC/USDT": {"pos_side": "SHORT"}})
        engine = _engine(client, _FakeStore([_position()]))

        await engine._reconcile_local_open_positions()

        self.assertIsNotNone(engine._entries_blocked_reason)
        self.assertIn("SHORT", engine._entries_blocked_reason)
        self.assertEqual(engine._resumable_positions, [])
        self.assertEqual(client.flattened, [])

    async def test_size_drift_is_treated_as_unknown_exposure(self) -> None:
        client = _FakeClient({"BTC/USDT"}, details={"BTC/USDT": {"exchange_size": 0.5}})
        engine = _engine(client, _FakeStore([_position()]))

        await engine._reconcile_local_open_positions()

        self.assertIsNotNone(engine._entries_blocked_reason)
        self.assertIn("drift", engine._entries_blocked_reason)
        self.assertEqual(engine._resumable_positions, [])

    async def test_entry_price_drift_is_treated_as_unknown_exposure(self) -> None:
        client = _FakeClient({"BTC/USDT"}, details={"BTC/USDT": {"avg_price": 90000.0}})
        engine = _engine(client, _FakeStore([_position()]))

        await engine._reconcile_local_open_positions()

        self.assertIsNotNone(engine._entries_blocked_reason)
        self.assertEqual(engine._resumable_positions, [])

    async def test_small_partial_fill_drift_still_resumes(self) -> None:
        client = _FakeClient({"BTC/USDT"}, details={"BTC/USDT": {"exchange_size": 0.0101}})
        engine = _engine(client, _FakeStore([_position()]))

        await engine._reconcile_local_open_positions()

        self.assertIsNone(engine._entries_blocked_reason)
        self.assertEqual([item.symbol for item in engine._resumable_positions], ["BTC/USDT"])

    async def test_unreadable_position_state_blocks_entries(self) -> None:
        client = _FakeClient({"BTC/USDT"})
        client.details_error = True
        engine = _engine(client, _FakeStore([_position()]))

        await engine._reconcile_local_open_positions()

        self.assertIsNotNone(engine._entries_blocked_reason)
        self.assertEqual(client.flattened, [])

    async def test_unmatched_exchange_position_blocks_entries_and_is_not_flattened(self) -> None:
        client = _FakeClient({"ETH/USDT"})
        engine = _engine(client, _FakeStore([]))

        await engine._reconcile_local_open_positions()

        self.assertIsNotNone(engine._entries_blocked_reason)
        self.assertIn("ETH/USDT", engine._entries_blocked_reason)
        self.assertEqual(client.flattened, [], "unknown exposure must never be auto-flattened")

    async def test_persisted_position_confirmed_by_exchange_is_resumed(self) -> None:
        client = _FakeClient({"BTC/USDT"})
        engine = _engine(client, _FakeStore([_position()]))

        await engine._reconcile_local_open_positions()

        self.assertIsNone(engine._entries_blocked_reason)
        self.assertEqual([item.symbol for item in engine._resumable_positions], ["BTC/USDT"])

    async def test_stale_persisted_position_is_removed(self) -> None:
        client = _FakeClient(set())
        store = _FakeStore([_position()])
        engine = _engine(client, store)

        await engine._reconcile_local_open_positions()

        self.assertEqual(store.removed, ["BTC/USDT"])
        self.assertEqual(engine._resumable_positions, [])
        self.assertIsNone(engine._entries_blocked_reason)

    async def test_clean_startup_allows_trading(self) -> None:
        engine = _engine(_FakeClient(set()), _FakeStore([]))
        await engine._reconcile_local_open_positions()
        self.assertIsNone(engine._entries_blocked_reason)

    async def test_blocked_engine_skips_the_tick_entirely(self) -> None:
        engine = _engine(_FakeClient(set()), _FakeStore([]))
        engine._entries_blocked_reason = "unmatched position"
        engine._risk_manager = SimpleNamespace()  # would raise if touched

        await engine._tick()  # must return before any risk-state work

    async def test_resumed_plan_uses_configured_risk_envelope(self) -> None:
        engine = _engine(_FakeClient({"BTC/USDT"}), _FakeStore([]))
        plan = engine._plan_from_resumed_position(_position())
        self.assertEqual(plan.symbol, "BTC/USDT")
        self.assertEqual(plan.direction, PositionDirection.LONG)
        self.assertEqual(plan.stop_loss_pct, 0.08)
        self.assertEqual(plan.take_profit_pct, 0.12)
        self.assertIn("resumed_after_restart", plan.decision_reason)


class StartupSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_vst_environment_fails_closed(self) -> None:
        engine = _engine(_FakeClient(set()), _FakeStore([]), _settings(bingx_env="live"))
        with self.assertRaises(RuntimeError):
            await engine._startup_safety_checks()

    async def test_venue_label_prefers_the_client(self) -> None:
        engine = _engine(_FakeClient(set()), _FakeStore([]))
        self.assertEqual(engine._venue_label, "bingx")


class DeterministicGuardTests(unittest.TestCase):
    """The deterministic guards must behave exactly as before the migration."""

    def test_stop_loss_thresholds(self) -> None:
        self.assertTrue(TradeEngine._hit_stop_loss(PositionDirection.LONG, 100.0, 99.9, 0.1))
        self.assertFalse(TradeEngine._hit_stop_loss(PositionDirection.LONG, 100.0, 99.95, 0.1))
        self.assertTrue(TradeEngine._hit_stop_loss(PositionDirection.SHORT, 100.0, 100.1, 0.1))

    def test_take_profit_thresholds(self) -> None:
        self.assertTrue(TradeEngine._hit_take_profit(PositionDirection.LONG, 100.0, 100.2, 0.2))
        self.assertFalse(TradeEngine._hit_take_profit(PositionDirection.LONG, 100.0, 100.1, 0.2))
        self.assertTrue(TradeEngine._hit_take_profit(PositionDirection.SHORT, 100.0, 99.8, 0.2))

    def test_planned_stop_price(self) -> None:
        self.assertAlmostEqual(TradeEngine._planned_stop_price(PositionDirection.LONG, 100.0, 1.0), 99.0)
        self.assertAlmostEqual(TradeEngine._planned_stop_price(PositionDirection.SHORT, 100.0, 1.0), 101.0)

    def test_round_trip_fee_estimate(self) -> None:
        fee = TradeEngine._estimate_total_taker_fee(100.0, 110.0, 1.0, 0.05)
        self.assertAlmostEqual(fee, (100.0 * 0.0005) + (110.0 * 0.0005))

    def test_gross_pnl(self) -> None:
        self.assertAlmostEqual(TradeEngine._estimate_gross_pnl(PositionDirection.LONG, 100.0, 110.0, 2.0), 20.0)
        self.assertAlmostEqual(TradeEngine._estimate_gross_pnl(PositionDirection.SHORT, 100.0, 90.0, 2.0), 20.0)

    def test_quantity_from_notional(self) -> None:
        self.assertAlmostEqual(TradeEngine._calculate_quantity(180.0, 100, 60000.0), 0.3)

    def test_stop_loss_and_liquidation_exits_always_allow_taker(self) -> None:
        engine = object.__new__(TradeEngine)
        engine._settings = _settings(volume_execution_mode="maker_only", maker_allow_taker_emergency_exit=True)
        self.assertTrue(engine._exit_allows_taker("stop_loss"))
        self.assertTrue(engine._exit_allows_taker("liquidation_guard"))
        self.assertFalse(engine._exit_allows_taker("take_profit"))


if __name__ == "__main__":
    unittest.main()
