from __future__ import annotations

import unittest
from types import SimpleNamespace

from bot.core.trade_engine import TradeEngine
from bot.models import PositionDirection
from bot.volume import VolumeBuildProfile


class _Store:
    def __init__(self, volume: float, total_volume: float | None = None) -> None:
        self.volume = volume
        self.total_volume = volume if total_volume is None else total_volume

    async def total_volume_for_day(self) -> float:
        return self.volume

    async def total_volume_all_time(self) -> float:
        return self.total_volume


class ArenaVolumeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_maker_only_entry_never_falls_back_to_taker_market(self) -> None:
        engine = object.__new__(TradeEngine)
        engine._settings = SimpleNamespace(
            volume_execution_mode="maker_only",
            execution_entry_order_type="market",
            execution_post_only_fallback_to_market=True,
        )
        engine._logger = SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)
        market_orders: list[str] = []

        async def fake_post_only_entry(plan, exchange_size, order_book):
            return None

        class _Client:
            async def place_market_order(self, **kwargs):
                market_orders.append("called")
                return {"data": [{"ordId": "market-order"}]}

        engine._client = _Client()
        engine._place_post_only_entry = fake_post_only_entry
        plan = SimpleNamespace(symbol="BTC/USDT", direction=PositionDirection.LONG)

        order_id = await engine._place_entry_order(plan, exchange_size=1.0, order_book=None)

        self.assertIsNone(order_id)
        self.assertEqual(market_orders, [])

    async def test_taker_exit_is_allowed_only_for_risk_emergencies_in_maker_only_mode(self) -> None:
        engine = object.__new__(TradeEngine)
        engine._settings = SimpleNamespace(
            volume_execution_mode="maker_only",
            maker_allow_taker_emergency_exit=True,
        )

        self.assertTrue(engine._exit_allows_taker("stop_loss"))
        self.assertTrue(engine._exit_allows_taker("liquidation_guard"))
        self.assertTrue(engine._exit_allows_taker("liquidation_guard_unverified"))
        self.assertFalse(engine._exit_allows_taker("breakeven_exit"))
        self.assertFalse(engine._exit_allows_taker("neutral_exit"))
        self.assertFalse(engine._exit_allows_taker("take_profit"))
        self.assertFalse(engine._exit_allows_taker("time_stop"))

    def test_volume_target_math_for_100x_maker_profile(self) -> None:
        profile = VolumeBuildProfile(
            account_equity_usdt=1500,
            margin_per_entry_usdt=180,
            leverage=100,
            sides_per_entry=2,
            target_entries_per_day=28,
            target_volume_usdt=30_000_000,
        )

        self.assertEqual(float(profile.volume_per_entry_usdt), 36_000.0)
        self.assertEqual(float(profile.daily_volume_usdt), 1_008_000.0)

    async def test_volume_target_guard_blocks_entries_after_target_reached(self) -> None:
        engine = object.__new__(TradeEngine)
        engine._settings = SimpleNamespace(
            strategy_mode="ai_volume",
            volume_stop_at_target_enabled=True,
            volume_daily_target_usdt=0,
            volume_target_usdt=10_000,
            volume_daily_fee_budget_usdt=0,
            taker_fee_pct=0.05,
        )
        engine._trade_store = _Store(volume=1_000, total_volume=10_500)

        reason = await engine._volume_runtime_pause_reason()

        self.assertEqual(reason, "volume_target_reached")

    async def test_total_volume_target_does_not_use_only_today_volume(self) -> None:
        engine = object.__new__(TradeEngine)
        engine._settings = SimpleNamespace(
            strategy_mode="ai_volume",
            volume_stop_at_target_enabled=True,
            volume_daily_target_usdt=0,
            volume_target_usdt=10_000,
            volume_daily_fee_budget_usdt=0,
            taker_fee_pct=0.05,
        )
        engine._trade_store = _Store(volume=9_999, total_volume=9_999)

        reason = await engine._volume_runtime_pause_reason()

        self.assertIsNone(reason)

    async def test_volume_fee_budget_blocks_entries_when_estimated_fee_spent(self) -> None:
        engine = object.__new__(TradeEngine)
        engine._settings = SimpleNamespace(
            strategy_mode="ai_volume",
            volume_stop_at_target_enabled=False,
            volume_target_usdt=0,
            volume_daily_fee_budget_usdt=10,
            taker_fee_pct=0.05,
        )
        engine._trade_store = _Store(volume=25_000)

        reason = await engine._volume_runtime_pause_reason()

        self.assertEqual(reason, "volume_fee_budget_reached")


if __name__ == "__main__":
    unittest.main()
