from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.core.trade_engine import TradeEngine
from bot.exchange.contracts import FundingRateInfo, OrderBookSnapshot, OrderFill, PositionDetails
from bot.models import ClosedTrade, OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import DailyRiskState
from bot.strategy.base import ExitSignal


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        bingx_env="vst",
        exchange_name="bingx",
        symbols=("BTC/USDT",),
        position_usdt=180.0,
        leverage=100,
        stop_loss_pct=0.1,
        take_profit_pct=0.2,
        position_hold_minutes=60,
        position_monitor_interval_seconds=0,
        order_fill_wait_seconds=0,
        entry_interval_minutes=15,
        trade_target_per_day=28,
        max_stop_loss_per_day=2,
        max_consecutive_losses=3,
        max_daily_drawdown_usdt=100.0,
        cooldown_minutes_after_entry=0,
        cooldown_minutes_after_stop=0,
        strategy_mode="ai_volume",
        volume_sides_per_entry=2,
        volume_daily_target_usdt=0.0,
        volume_target_usdt=0.0,
        volume_stop_at_target_enabled=False,
        volume_daily_fee_budget_usdt=0.0,
        volume_attempt_target_per_day=34,
        volume_neutral_exit_enabled=False,
        volume_neutral_exit_min_seconds=0,
        volume_neutral_exit_min_net_usdt=0.0,
        volume_neutral_exit_band_usdt=0.0,
        volume_breakeven_exit_enabled=False,
        volume_execution_mode="market",
        execution_guards_enabled=True,
        execution_entry_order_type="market",
        execution_post_only_timeout_seconds=1.0,
        execution_post_only_max_reprices=1,
        execution_post_only_fallback_to_market=True,
        maker_entry_ttl_seconds=1.0,
        maker_exit_ttl_seconds=1.0,
        maker_max_reprices=1,
        maker_allow_taker_emergency_exit=True,
        orderbook_depth_levels=20,
        min_orderbook_depth_multiplier=0.0,
        max_abs_funding_rate=0.01,
        max_spread_bps=50.0,
        funding_blackout_minutes=0,
        min_liquidation_distance_bps=0.0,
        margin_mode="isolated",
        position_mode="hedge",
        taker_fee_pct=0.05,
        maker_fee_pct=-0.02,
        use_exchange_fee_rates=True,
        bingx_require_set_leverage=False,
        prevent_new_entries_if_exchange_position_open=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeClient:
    venue = "bingx"

    def __init__(self, *, price: float = 100.0, spread_bps: float = 2.0, price_drift: float = 1.001) -> None:
        self.price = price
        self.spread_bps = spread_bps
        # Default upward drift so any test that reaches the monitor loop terminates
        # on a real exit condition instead of holding below the fee floor forever.
        self.price_drift = price_drift
        self.position_size = 0.0
        self.orders: list[dict] = []
        self.cancelled: list[str] = []
        self.margin_modes: list[tuple[str, str]] = []
        self.position_modes: list[str] = []
        self.leverages: list[tuple[str, int]] = []
        self.fee_error = False
        self.liquidation_price: float | None = None
        self.effective_position_mode = "hedge"
        self.effective_margin_mode = "isolated"
        self.effective_leverage = (100, 100)

    async def get_last_price(self, symbol: str) -> float:
        self.price *= self.price_drift
        return self.price

    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            symbol=symbol,
            best_bid=self.price * 0.9999,
            best_ask=self.price * 1.0001,
            mid_price=self.price,
            spread_bps=self.spread_bps,
            bid_depth_notional=10_000_000.0,
            ask_depth_notional=10_000_000.0,
            timestamp=datetime.now(timezone.utc),
            raw={},
        )

    async def get_funding_rate_info(self, symbol: str) -> FundingRateInfo:
        return FundingRateInfo(symbol=symbol, funding_rate=0.0, next_funding_time=None, raw={})

    async def get_trade_fee_rates(self, symbol: str):
        if self.fee_error:
            raise RuntimeError("fee endpoint down")
        return SimpleNamespace(maker_fee_pct=-0.02, taker_fee_pct=0.05, raw={})

    async def calculate_order_size(self, symbol: str, base_quantity: float) -> float:
        return round(base_quantity, 6)

    async def estimate_base_quantity(self, symbol: str, order_size: float) -> float:
        return float(order_size)

    async def place_market_order(self, symbol, direction, size, reduce_only=False):
        self.orders.append({"type": "market", "symbol": symbol, "size": size, "reduce_only": reduce_only})
        self.position_size = 0.0 if reduce_only else float(size)
        return {"data": {"order": {"orderId": f"o{len(self.orders)}"}}}

    async def place_limit_order(self, symbol, direction, size, price, reduce_only=False, post_only=False):
        self.orders.append({"type": "limit", "symbol": symbol, "size": size, "post_only": post_only})
        self.position_size = 0.0 if reduce_only else float(size)
        return {"data": {"order": {"orderId": f"o{len(self.orders)}"}}}

    async def cancel_order(self, symbol: str, order_id: str):
        self.cancelled.append(order_id)
        return {}

    async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
        return OrderFill(
            order_id=order_id,
            avg_price=self.price,
            filled_size=abs(self.position_size) or 1.8,
            base_quantity=abs(self.position_size) or 1.8,
            fee_usdt=0.01,
            pnl_usdt=0.0,
            state="filled",
            raw={},
        )

    async def get_symbol_position_snapshot(self, symbol: str):
        return self.position_size, (self.price if self.position_size else None)

    async def get_symbol_position_details(self, symbol: str) -> PositionDetails:
        return PositionDetails(
            symbol=symbol,
            exchange_size=self.position_size,
            base_quantity=self.position_size,
            avg_price=self.price,
            mark_price=self.price,
            liquidation_price=self.liquidation_price,
            pos_side="LONG",
            raw={},
        )

    async def has_open_position(self, symbol: str) -> bool:
        return self.position_size > 0

    async def has_any_open_position(self, symbols) -> bool:
        return self.position_size > 0

    async def set_margin_mode(self, symbol: str, margin_mode: str):
        self.margin_modes.append((symbol, margin_mode))
        return {"msg": "ok"}

    async def set_position_mode(self, position_mode: str):
        self.position_modes.append(position_mode)
        return {"msg": "ok"}

    async def set_leverage(self, symbol: str, leverage: int):
        self.leverages.append((symbol, leverage))
        return {"msg": "ok"}

    async def get_position_mode(self) -> str:
        return self.effective_position_mode

    async def get_margin_mode(self, symbol: str) -> str:
        return self.effective_margin_mode

    async def get_effective_leverage(self, symbol: str):
        return self.effective_leverage


class FakeStore:
    def __init__(self) -> None:
        self.closed: list[ClosedTrade] = []
        self.open_positions: list[OpenPosition] = []
        self.removed: list[str] = []
        self.daily_volume = 0.0

    async def initialize(self) -> None:
        return None

    async def save_open_position(self, position: OpenPosition) -> None:
        self.open_positions.append(position)

    async def remove_open_position(self, symbol: str) -> None:
        self.removed.append(symbol)

    async def list_open_positions(self):
        return []

    async def save_closed_trade(self, trade: ClosedTrade) -> None:
        self.closed.append(trade)

    async def total_volume_for_day(self, day=None) -> float:
        return self.daily_volume

    async def total_volume_all_time(self) -> float:
        return self.daily_volume


class FakeRisk:
    def __init__(self, allowed: bool = True, closed_trades: int = 0) -> None:
        self.state = DailyRiskState(
            day=datetime.now(timezone.utc).date(),
            stop_losses=0,
            closed_trades=closed_trades,
            total_pnl_usdt=0.0,
            consecutive_losses=0,
            allowed_to_trade=allowed,
        )

    async def get_daily_state(self) -> DailyRiskState:
        return self.state


class FakeStrategy:
    def __init__(self, plan: TradePlan | None = None, exit_after: int = 1) -> None:
        self.plan = plan
        self._calls = 0
        self._exit_after = exit_after
        self.next_calls = 0

    async def next_trade_plan(self):
        self.next_calls += 1
        return self.plan

    async def exit_signal(self, position, price, elapsed) -> ExitSignal:
        self._calls += 1
        if self._calls >= self._exit_after:
            return ExitSignal(close_now=True, reason="ai_close")
        return ExitSignal(close_now=False, reason="hold")


def _plan(**overrides) -> TradePlan:
    base = dict(
        symbol="BTC/USDT",
        direction=PositionDirection.LONG,
        usdt_size=180.0,
        leverage=100,
        stop_loss_pct=0.1,
        take_profit_pct=0.2,
        decision_reason="ai_volume:OPEN_LONG",
        confidence=0.8,
    )
    base.update(overrides)
    return TradePlan(**base)


def _engine(client, store, strategy, risk, settings=None) -> TradeEngine:
    engine = TradeEngine(settings or _settings(), logging.getLogger("test.engine"), client, strategy, risk, store)
    return engine


class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_trade_lifecycle_records_a_closed_trade(self) -> None:
        client = FakeClient()
        store = FakeStore()
        strategy = FakeStrategy(_plan())
        engine = _engine(client, store, strategy, FakeRisk())

        await engine._execute_single_trade(_plan())

        self.assertEqual(len(store.closed), 1)
        trade = store.closed[0]
        self.assertEqual(trade.symbol, "BTC/USDT")
        self.assertEqual(trade.direction, PositionDirection.LONG)
        self.assertTrue(trade.reason.startswith("strategy_exit") or trade.reason in {"take_profit", "time_stop"})
        self.assertEqual(store.removed, ["BTC/USDT"])
        self.assertEqual(store.open_positions[0].symbol, "BTC/USDT")

    async def test_take_profit_exit_is_detected(self) -> None:
        store = FakeStore()

        client = FakeClient(price_drift=1.01)
        strategy = FakeStrategy(_plan(), exit_after=999)
        engine = _engine(client, store, strategy, FakeRisk())

        await engine._execute_single_trade(_plan())

        self.assertEqual(store.closed[0].reason, "take_profit")

    async def test_stop_loss_exit_sets_cooldown(self) -> None:
        client = FakeClient(price_drift=0.99)
        store = FakeStore()
        engine = _engine(client, store, FakeStrategy(_plan(), exit_after=999), FakeRisk())

        await engine._execute_single_trade(_plan())

        self.assertEqual(store.closed[0].reason, "stop_loss")
        self.assertIsNotNone(engine._last_stop_loss_ts)

    async def test_wide_spread_blocks_the_entry(self) -> None:
        client = FakeClient(spread_bps=999.0)
        store = FakeStore()
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk())

        await engine._execute_single_trade(_plan())

        self.assertEqual(store.closed, [])
        self.assertEqual(client.orders, [])

    async def test_insufficient_depth_blocks_the_entry(self) -> None:
        client = FakeClient()
        store = FakeStore()
        engine = _engine(
            client, store, FakeStrategy(_plan()), FakeRisk(), _settings(min_orderbook_depth_multiplier=1e9)
        )

        await engine._execute_single_trade(_plan())

        self.assertEqual(client.orders, [])

    async def test_excessive_funding_blocks_the_entry(self) -> None:
        class HighFundingClient(FakeClient):
            async def get_funding_rate_info(self, symbol: str) -> FundingRateInfo:
                return FundingRateInfo(symbol=symbol, funding_rate=0.5, next_funding_time=None, raw={})

        client = HighFundingClient()
        engine = _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk())

        await engine._execute_single_trade(_plan())

        self.assertEqual(client.orders, [])

    async def test_funding_blackout_blocks_the_entry(self) -> None:
        class SoonFundingClient(FakeClient):
            async def get_funding_rate_info(self, symbol: str) -> FundingRateInfo:
                return FundingRateInfo(
                    symbol=symbol,
                    funding_rate=0.0,
                    next_funding_time=datetime.now(timezone.utc) + timedelta(minutes=1),
                    raw={},
                )

        client = SoonFundingClient()
        engine = _engine(
            client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), _settings(funding_blackout_minutes=10)
        )

        await engine._execute_single_trade(_plan())

        self.assertEqual(client.orders, [])

    async def test_guards_disabled_skips_the_order_book_fetch(self) -> None:
        client = FakeClient(spread_bps=999.0)
        engine = _engine(
            client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), _settings(execution_guards_enabled=False)
        )

        allowed, book = await engine._pre_entry_execution_checks(_plan(), 1000.0)

        self.assertTrue(allowed)
        self.assertIsNone(book)

    async def test_size_below_the_venue_minimum_is_skipped(self) -> None:
        # The venue's own metadata is the authority: calculate_order_size returns 0
        # when tradeMinQuantity / tradeMinUSDT rejects the size.
        class BelowMinimumClient(FakeClient):
            async def calculate_order_size(self, symbol: str, base_quantity: float) -> float:
                return 0.0

        client = BelowMinimumClient()
        engine = _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk())

        await engine._execute_single_trade(_plan())

        self.assertEqual(client.orders, [])

    async def test_sub_one_btc_quantity_is_allowed(self) -> None:
        # Regression: 180 USDT margin at 100x is ~0.3 BTC. The removed
        # MIN_ORDER_CONTRACTS=1 floor silently blocked every BTC entry.
        client = FakeClient(price=60000.0)
        store = FakeStore()
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk())

        await engine._execute_single_trade(_plan())

        self.assertTrue(client.orders, "a 0.3 BTC entry must reach the exchange")
        self.assertLess(client.orders[0]["size"], 1.0)

    async def test_post_only_entry_is_used_when_configured(self) -> None:
        client = FakeClient()
        engine = _engine(
            client,
            FakeStore(),
            FakeStrategy(_plan()),
            FakeRisk(),
            _settings(execution_entry_order_type="post_only"),
        )

        await engine._execute_single_trade(_plan())

        self.assertEqual(client.orders[0]["type"], "limit")
        self.assertTrue(client.orders[0]["post_only"])

    async def test_liquidation_guard_closes_immediately(self) -> None:
        client = FakeClient()
        client.liquidation_price = 99.999
        store = FakeStore()
        engine = _engine(
            client, store, FakeStrategy(_plan()), FakeRisk(), _settings(min_liquidation_distance_bps=5000.0)
        )

        await engine._execute_single_trade(_plan())

        self.assertEqual(store.closed[0].reason, "liquidation_guard")


class TickGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_risk_guard_pauses_entries(self) -> None:
        strategy = FakeStrategy(_plan())
        engine = _engine(FakeClient(), FakeStore(), strategy, FakeRisk(allowed=False))

        await engine._tick()

        self.assertEqual(strategy.next_calls, 0)

    async def test_daily_target_pauses_entries(self) -> None:
        strategy = FakeStrategy(_plan())
        engine = _engine(FakeClient(), FakeStore(), strategy, FakeRisk(closed_trades=999))

        await engine._tick()

        self.assertEqual(strategy.next_calls, 0)

    async def test_cooldown_after_entry_pauses_entries(self) -> None:
        strategy = FakeStrategy(_plan())
        engine = _engine(
            FakeClient(), FakeStore(), strategy, FakeRisk(), _settings(cooldown_minutes_after_entry=60)
        )
        engine._last_entry_ts = datetime.now(timezone.utc).timestamp()

        await engine._tick()

        self.assertEqual(strategy.next_calls, 0)

    async def test_cooldown_after_stop_pauses_entries(self) -> None:
        strategy = FakeStrategy(_plan())
        engine = _engine(
            FakeClient(), FakeStore(), strategy, FakeRisk(), _settings(cooldown_minutes_after_stop=60)
        )
        engine._last_stop_loss_ts = datetime.now(timezone.utc).timestamp()

        await engine._tick()

        self.assertEqual(strategy.next_calls, 0)

    async def test_hold_plan_opens_nothing(self) -> None:
        client = FakeClient()
        engine = _engine(client, FakeStore(), FakeStrategy(None), FakeRisk())

        await engine._tick()

        self.assertEqual(client.orders, [])

    async def test_existing_exchange_position_blocks_a_new_entry(self) -> None:
        client = FakeClient()
        client.position_size = 1.0
        engine = _engine(
            client,
            FakeStore(),
            FakeStrategy(_plan()),
            FakeRisk(),
            _settings(prevent_new_entries_if_exchange_position_open=True),
        )

        await engine._tick()

        self.assertEqual(client.orders, [])

    async def test_daily_volume_target_pauses_entries(self) -> None:
        store = FakeStore()
        store.daily_volume = 5_000_000.0
        strategy = FakeStrategy(_plan())
        engine = _engine(
            FakeClient(),
            store,
            strategy,
            FakeRisk(),
            _settings(volume_daily_target_usdt=1_000_000.0, volume_stop_at_target_enabled=True),
        )

        await engine._tick()

        self.assertEqual(strategy.next_calls, 0)

    async def test_fee_budget_pauses_entries(self) -> None:
        store = FakeStore()
        store.daily_volume = 10_000_000.0
        strategy = FakeStrategy(_plan())
        engine = _engine(
            FakeClient(),
            store,
            strategy,
            FakeRisk(),
            _settings(volume_daily_fee_budget_usdt=1.0),
        )

        await engine._tick()

        self.assertEqual(strategy.next_calls, 0)


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_leverage_init_sets_margin_position_mode_and_leverage(self) -> None:
        client = FakeClient()
        engine = _engine(client, FakeStore(), FakeStrategy(), FakeRisk())

        await engine._initialize_leverage()

        self.assertEqual(client.margin_modes, [("BTC/USDT", "isolated")])
        self.assertEqual(client.position_modes, ["hedge"])
        self.assertEqual(client.leverages, [("BTC/USDT", 100)])

    async def test_wrong_effective_margin_mode_fails_startup(self) -> None:
        client = FakeClient()
        client.effective_margin_mode = "crossed"
        engine = _engine(client, FakeStore(), FakeStrategy(), FakeRisk())

        with self.assertRaises(RuntimeError) as ctx:
            await engine._initialize_leverage()
        self.assertIn("margin mode", str(ctx.exception))

    async def test_wrong_effective_position_mode_fails_startup(self) -> None:
        client = FakeClient()
        client.effective_position_mode = "one_way"
        engine = _engine(client, FakeStore(), FakeStrategy(), FakeRisk())

        with self.assertRaises(RuntimeError):
            await engine._initialize_leverage()

    async def test_wrong_effective_leverage_fails_startup(self) -> None:
        client = FakeClient()
        client.effective_leverage = (100, 20)
        engine = _engine(client, FakeStore(), FakeStrategy(), FakeRisk())

        with self.assertRaises(RuntimeError) as ctx:
            await engine._initialize_leverage()
        self.assertIn("leverage", str(ctx.exception))

    async def test_unreadable_effective_state_fails_closed(self) -> None:
        class BlindClient(FakeClient):
            async def get_effective_leverage(self, symbol: str):
                raise RuntimeError("endpoint down")

        engine = _engine(BlindClient(), FakeStore(), FakeStrategy(), FakeRisk())

        with self.assertRaises(RuntimeError) as ctx:
            await engine._initialize_leverage()
        self.assertIn("could not read", str(ctx.exception))

    async def test_failed_set_calls_still_pass_when_effective_state_is_correct(self) -> None:
        # The venue rejects "no change needed" on some endpoints; what matters is
        # the verified effective state, not whether the setter returned an error.
        class StubbornClient(FakeClient):
            async def set_leverage(self, symbol: str, leverage: int):
                raise RuntimeError("no need to change leverage")

        engine = _engine(StubbornClient(), FakeStore(), FakeStrategy(), FakeRisk())

        await engine._initialize_leverage()  # must not raise

    async def test_fee_refresh_uses_exchange_rates(self) -> None:
        client = FakeClient()
        engine = _engine(client, FakeStore(), FakeStrategy(), FakeRisk())

        await engine._refresh_exchange_fee_rates()

        self.assertAlmostEqual(engine._taker_fee_pct("BTC/USDT"), 0.05)

    async def test_fee_refresh_failure_falls_back_to_configured_rate(self) -> None:
        client = FakeClient()
        client.fee_error = True
        engine = _engine(client, FakeStore(), FakeStrategy(), FakeRisk())

        await engine._refresh_exchange_fee_rates()

        self.assertAlmostEqual(engine._taker_fee_pct("BTC/USDT"), 0.05)

    async def test_fee_refresh_is_skipped_when_disabled(self) -> None:
        client = FakeClient()
        engine = _engine(
            client, FakeStore(), FakeStrategy(), FakeRisk(), _settings(use_exchange_fee_rates=False)
        )

        await engine._refresh_exchange_fee_rates()

        self.assertEqual(engine._maker_fee_pct_by_symbol, {})


if __name__ == "__main__":
    unittest.main()
