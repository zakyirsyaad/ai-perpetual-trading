from __future__ import annotations

import asyncio
import logging
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.core.trade_engine import TradeEngine
from bot.config import Settings
from bot.exchange.contracts import (
    ExchangeAPIError,
    FundingRateInfo,
    MaintenanceMarginTier,
    OrderBookSnapshot,
    OrderFill,
    PositionDetails,
    UncertainOrderSubmissionError,
)
from decimal import Decimal
from bot.models import ClosedTrade, OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import DailyRiskState
from bot.strategy.base import ExitSignal
from bot.strategy.volume_strategy import VolumeStrategy


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        bingx_env="vst",
        exchange_name="bingx",
        symbols=("BTC/USDT",),
        position_usdt=180.0,
        account_balance_usdt=1500.0,
        volume_margin_per_entry_usdt=180.0,
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

    async def get_fresh_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        return await self.get_order_book(symbol, depth)

    async def get_funding_rate_info(self, symbol: str) -> FundingRateInfo:
        return FundingRateInfo(symbol=symbol, funding_rate=0.0, next_funding_time=None, raw={})

    async def get_trade_fee_rates(self, symbol: str):
        if self.fee_error:
            raise RuntimeError("fee endpoint down")
        return SimpleNamespace(maker_fee_pct=-0.02, taker_fee_pct=0.05, raw={})

    async def get_maintenance_margin_tiers(self, symbol: str):
        return [MaintenanceMarginTier(Decimal("0"), Decimal("50000"), Decimal("0.004"), Decimal("0"))]

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
            position_id="p-live",
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
    async def test_real_settings_default_routes_entries_as_market_orders(self) -> None:
        settings = Settings(
            _env_file=None,
            BINGX_API_KEY="test-key",
            BINGX_SECRET_KEY="test-secret",
            STRATEGY_MODE="volume",
        )
        client = FakeClient()
        engine = _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)

        await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)

        self.assertEqual(settings.execution_entry_order_type, "market")
        self.assertEqual(settings.entry_interval_minutes, 3)
        self.assertEqual(client.orders[0]["type"], "market")

    async def test_market_entry_has_a_last_mile_liquidation_gate_at_the_ask_price(self) -> None:
        class RecordingGateEngine(TradeEngine):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.gate_attempts: list[tuple[float, float]] = []

            async def _passes_pre_order_liquidation_gate(self, plan, entry_price, quantity_base):
                self.gate_attempts.append((entry_price, quantity_base))
                return await super()._passes_pre_order_liquidation_gate(plan, entry_price, quantity_base)

        class TrackingClient(FakeClient):
            async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
                return OrderBookSnapshot(symbol, 99.0, 101.0, 100.0, 2.0, 1_000_000.0, 1_000_000.0, None, {})

        client = TrackingClient()
        engine = RecordingGateEngine(_settings(), logging.getLogger("test.last_mile.market"), client, FakeStrategy(_plan()), FakeRisk(), FakeStore())

        await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)

        self.assertEqual(engine.gate_attempts, [(101.0, 1.0)])
        self.assertEqual(client.orders[0]["type"], "market")

    async def test_market_entry_proof_uses_uncached_book_instead_of_a_stale_cached_book(self) -> None:
        class RecordingGateEngine(TradeEngine):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.gate_attempts: list[float] = []

            async def _passes_pre_order_liquidation_gate(self, plan, entry_price, quantity_base):
                self.gate_attempts.append(entry_price)
                return await super()._passes_pre_order_liquidation_gate(plan, entry_price, quantity_base)

        class StaleBookClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.fresh_calls = 0

            async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
                return OrderBookSnapshot(symbol, 90.0, 91.0, 90.5, 2.0, 1_000_000.0, 1_000_000.0, None, {})

            async def get_fresh_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
                self.fresh_calls += 1
                return OrderBookSnapshot(symbol, 99.0, 102.0, 100.5, 2.0, 1_000_000.0, 1_000_000.0, None, {})

        client = StaleBookClient()
        engine = RecordingGateEngine(_settings(), logging.getLogger("test.fresh.market"), client, FakeStrategy(_plan()), FakeRisk(), FakeStore())
        await client.get_order_book("BTC/USDT")

        await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)

        self.assertEqual(engine.gate_attempts, [102.0])
        self.assertEqual(client.fresh_calls, 1)
        self.assertTrue(client.orders)

    async def test_fresh_entry_quote_failure_fails_closed_without_order(self) -> None:
        class FreshQuoteFailureClient(FakeClient):
            async def get_fresh_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
                raise ExchangeAPIError("fresh depth unavailable")

        client = FreshQuoteFailureClient()
        engine = _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk())

        order_id = await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)

        self.assertIsNone(order_id)
        self.assertEqual(client.orders, [])

    async def test_post_only_initial_reprice_and_market_fallback_each_have_last_mile_gates(self) -> None:
        class RecordingGateEngine(TradeEngine):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.gate_attempts: list[tuple[float, float]] = []

            async def _passes_pre_order_liquidation_gate(self, plan, entry_price, quantity_base):
                self.gate_attempts.append((entry_price, quantity_base))
                return await super()._passes_pre_order_liquidation_gate(plan, entry_price, quantity_base)

        class RetryingClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.book_calls = 0
                self.fresh_calls = 0

            async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
                self.book_calls += 1
                bid = 99.0 - self.book_calls
                return OrderBookSnapshot(symbol, bid, 101.0 + self.book_calls, 100.0, 2.0, 1_000_000.0, 1_000_000.0, None, {})

            async def get_fresh_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
                self.fresh_calls += 1
                return await self.get_order_book(symbol, depth)

            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                return OrderFill(order_id, None, 0.0, 0.0, 0.0, 0.0, "canceled", {})

        client = RetryingClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )
        engine = RecordingGateEngine(settings, logging.getLogger("test.last_mile.post_only"), client, FakeStrategy(_plan()), FakeRisk(), FakeStore())

        await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)

        self.assertEqual([item["type"] for item in client.orders], ["limit", "limit", "market"])
        self.assertEqual(engine.gate_attempts, [(98.0, 1.0), (97.0, 1.0), (104.0, 1.0)])
        self.assertEqual(client.fresh_calls, 3)

    async def test_post_only_cancel_exception_blocks_reprice_and_market_fallback(self) -> None:
        class CancelFailureClient(FakeClient):
            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                return None

            async def cancel_order(self, symbol: str, order_id: str):
                raise ExchangeAPIError("cancel unavailable")

        client = CancelFailureClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.0,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )
        order_id = await _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)._place_entry_order(
            _plan(), exchange_size=1.0, order_book=None
        )

        self.assertIsNone(order_id)
        self.assertEqual([item["type"] for item in client.orders], ["limit"])

    async def test_uncertain_post_only_order_blocks_future_entry_ticks_until_restart(self) -> None:
        class CancelFailureClient(FakeClient):
            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                return None

            async def cancel_order(self, symbol: str, order_id: str):
                raise ExchangeAPIError("cancel unavailable")

        client = CancelFailureClient()
        strategy = FakeStrategy(_plan())
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.0,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )
        engine = _engine(client, FakeStore(), strategy, FakeRisk(), settings)

        order_id = await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)
        first_orders = list(client.orders)
        await engine._tick()

        self.assertIsNone(order_id)
        self.assertIsNotNone(engine._entries_blocked_reason)
        self.assertIn("manual reconciliation", engine._entries_blocked_reason.lower())
        self.assertIn("restart", engine._entries_blocked_reason.lower())
        self.assertEqual(client.orders, first_orders)
        self.assertEqual(strategy.next_calls, 0)

    async def test_post_only_poll_exception_latches_uncertainty_and_blocks_future_tick(self) -> None:
        class PollFailureClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.fill_calls = 0

            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                self.fill_calls += 1
                if self.fill_calls == 1:
                    raise ExchangeAPIError("poll unavailable")
                return None

        client = PollFailureClient()
        strategy = FakeStrategy(_plan())
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.001,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )
        engine = _engine(client, FakeStore(), strategy, FakeRisk(), settings)

        order_id = await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)
        first_orders = list(client.orders)
        await engine._tick()

        self.assertIsNone(order_id)
        self.assertEqual([item["type"] for item in client.orders], ["limit"])
        self.assertEqual(client.orders, first_orders)
        self.assertIsNotNone(engine._entries_blocked_reason)
        self.assertEqual(strategy.next_calls, 0)

    async def test_post_only_poll_exception_then_canceled_partial_fill_returns_original_order(self) -> None:
        class PollFailureThenCanceledPartialClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.fill_calls = 0
                self.cancelled_once = False

            async def cancel_order(self, symbol: str, order_id: str):
                self.cancelled_once = True
                return await super().cancel_order(symbol, order_id)

            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                self.fill_calls += 1
                if self.fill_calls == 1:
                    raise ExchangeAPIError("poll unavailable")
                return OrderFill(order_id, self.price, 0.5, 0.5, 0.0, 0.0, "mmp_canceled", {})

        client = PollFailureThenCanceledPartialClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.001,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )

        order_id = await _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)._place_entry_order(
            _plan(), exchange_size=1.0, order_book=None
        )

        self.assertEqual(order_id, "o1")
        self.assertEqual([item["type"] for item in client.orders], ["limit"])
        self.assertEqual(client.cancelled, ["o1"])

    async def test_post_only_partial_fill_canceled_remainder_returns_original_order(self) -> None:
        class PartialFillClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled_once = False

            async def cancel_order(self, symbol: str, order_id: str):
                self.cancelled_once = True
                return await super().cancel_order(symbol, order_id)

            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                state = "canceled" if self.cancelled_once else "partially_filled"
                return OrderFill(order_id, self.price, 0.5, 0.5, 0.0, 0.0, state, {})

        client = PartialFillClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.001,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )

        order_id = await _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)._place_entry_order(
            _plan(), exchange_size=1.0, order_book=None
        )

        self.assertEqual(order_id, "o1")
        self.assertEqual([item["type"] for item in client.orders], ["limit"])
        self.assertEqual(client.cancelled, ["o1"])

    async def test_post_only_partial_fill_cancel_failure_latches_without_duplicate(self) -> None:
        class PartialFillCancelFailureClient(FakeClient):
            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                return OrderFill(order_id, self.price, 0.5, 0.5, 0.0, 0.0, "partially_filled", {})

            async def cancel_order(self, symbol: str, order_id: str):
                raise ExchangeAPIError("cancel unavailable")

        client = PartialFillCancelFailureClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.001,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )
        engine = _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)

        order_id = await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)

        self.assertIsNone(order_id)
        self.assertEqual([item["type"] for item in client.orders], ["limit"])
        self.assertIsNotNone(engine._entries_blocked_reason)

    async def test_post_only_partial_fill_ambiguous_remainder_latches_without_duplicate(self) -> None:
        class PartialFillAmbiguousClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled_once = False

            async def cancel_order(self, symbol: str, order_id: str):
                self.cancelled_once = True
                return await super().cancel_order(symbol, order_id)

            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                if self.cancelled_once:
                    return None
                return OrderFill(order_id, self.price, 0.5, 0.5, 0.0, 0.0, "partially_filled", {})

        client = PartialFillAmbiguousClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.001,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )
        engine = _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)

        order_id = await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)

        self.assertIsNone(order_id)
        self.assertEqual([item["type"] for item in client.orders], ["limit"])
        self.assertIsNotNone(engine._entries_blocked_reason)

    async def test_uncertain_market_entry_submission_blocks_all_future_ticks(self) -> None:
        class UncertainSubmissionClient(FakeClient):
            async def place_market_order(self, symbol, direction, size, reduce_only=False):
                self.orders.append({"type": "market", "symbol": symbol, "size": size, "reduce_only": reduce_only})
                raise UncertainOrderSubmissionError(
                    "order acknowledgement was lost",
                    symbol=symbol,
                    client_order_id="entry-uncertain-1",
                    venue="bingx",
                )

        client = UncertainSubmissionClient()
        strategy = FakeStrategy(_plan())
        engine = _engine(client, FakeStore(), strategy, FakeRisk(), _settings(execution_entry_order_type="market"))

        order_id = await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)
        first_orders = list(client.orders)
        await engine._tick()

        self.assertIsNone(order_id)
        self.assertEqual(client.orders, first_orders)
        self.assertEqual(len(client.orders), 1)
        self.assertIsNotNone(engine._entries_blocked_reason)
        self.assertIn("manual reconciliation", engine._entries_blocked_reason.lower())
        self.assertIn("restart", engine._entries_blocked_reason.lower())
        self.assertEqual(strategy.next_calls, 0)

    async def test_post_only_ambiguous_post_cancel_status_blocks_reprice_and_fallback(self) -> None:
        class AmbiguousCancelClient(FakeClient):
            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                return None

        client = AmbiguousCancelClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.0,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )
        order_id = await _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)._place_entry_order(
            _plan(), exchange_size=1.0, order_book=None
        )

        self.assertIsNone(order_id)
        self.assertEqual([item["type"] for item in client.orders], ["limit"])

    async def test_post_only_malformed_canceled_fill_latches_without_reprice_or_fallback(self) -> None:
        class MalformedCanceledFillClient(FakeClient):
            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                return OrderFill("other-order", None, float("nan"), 0.0, 0.0, 0.0, "canceled", {})

        client = MalformedCanceledFillClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.001,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )
        engine = _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)

        order_id = await engine._place_entry_order(_plan(), exchange_size=1.0, order_book=None)

        self.assertIsNone(order_id)
        self.assertEqual([item["type"] for item in client.orders], ["limit"])
        self.assertIsNotNone(engine._entries_blocked_reason)

    async def test_post_only_confirmed_cancellation_allows_reprice(self) -> None:
        class ConfirmedCancelClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled_once = False

            async def cancel_order(self, symbol: str, order_id: str):
                self.cancelled_once = True
                return await super().cancel_order(symbol, order_id)

            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                if order_id == "o1":
                    if self.cancelled_once:
                        return OrderFill(order_id, None, 0.0, 0.0, 0.0, 0.0, "canceled", {})
                    return None
                return OrderFill(order_id, self.price, 1.0, 1.0, 0.0, 0.0, "filled", {})

        client = ConfirmedCancelClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.0,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=False,
        )
        order_id = await _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)._place_entry_order(
            _plan(), exchange_size=1.0, order_book=None
        )

        self.assertEqual(order_id, "o2")
        self.assertEqual([item["type"] for item in client.orders], ["limit", "limit"])

    async def test_post_only_late_fill_after_cancel_returns_original_order_without_duplicate(self) -> None:
        class LateFillClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.cancelled_once = False

            async def cancel_order(self, symbol: str, order_id: str):
                self.cancelled_once = True
                return await super().cancel_order(symbol, order_id)

            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                if self.cancelled_once:
                    return OrderFill(order_id, self.price, 0.5, 0.5, 0.0, 0.0, "filled", {})
                return None

        client = LateFillClient()
        settings = _settings(
            execution_entry_order_type="post_only",
            execution_post_only_timeout_seconds=0.0,
            execution_post_only_max_reprices=1,
            execution_post_only_fallback_to_market=True,
        )
        order_id = await _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), settings)._place_entry_order(
            _plan(), exchange_size=1.0, order_book=None
        )

        self.assertEqual(order_id, "o1")
        self.assertEqual([item["type"] for item in client.orders], ["limit"])

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

    async def test_entry_captures_the_venue_position_id(self) -> None:
        client = FakeClient()
        store = FakeStore()
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk())

        await engine._execute_single_trade(_plan())

        self.assertEqual(store.open_positions[0].position_id, "p-live")

    async def test_entry_persists_the_effective_risk_envelope(self) -> None:
        client = FakeClient()
        store = FakeStore()
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk())

        await engine._execute_single_trade(_plan(stop_loss_pct=0.5, take_profit_pct=1.2))

        self.assertEqual(store.open_positions[0].stop_loss_pct, 0.5)
        self.assertEqual(store.open_positions[0].take_profit_pct, 1.2)

    async def test_entry_persists_position_when_fill_lookup_fails(self) -> None:
        class FillLookupFailingClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.fill_calls = 0

            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                self.fill_calls += 1
                if self.fill_calls == 1:
                    raise ExchangeAPIError("BingX order lookup rejected", retryable=False, venue=self.venue)
                return await super().get_order_fill(symbol, order_id)

        client = FillLookupFailingClient()
        store = FakeStore()
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk())

        await engine._execute_single_trade(_plan())

        self.assertEqual(store.open_positions[0].exchange_order_id, "o1")
        self.assertEqual(store.open_positions[0].position_id, "p-live")
        self.assertGreaterEqual(client.fill_calls, 2)

    async def test_entry_fill_fallback_rejects_opposite_side_position(self) -> None:
        class OppositeSideClient(FakeClient):
            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                raise ExchangeAPIError("BingX order lookup rejected", retryable=False, venue=self.venue)

            async def get_symbol_position_details(self, symbol: str) -> PositionDetails:
                details = await super().get_symbol_position_details(symbol)
                return replace(details, pos_side="SHORT")

        client = OppositeSideClient()
        store = FakeStore()
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk())

        with self.assertRaisesRegex(RuntimeError, "opposite-side position"):
            await engine._execute_single_trade(_plan())

        self.assertEqual(store.open_positions, [])

    async def test_entry_fill_fallback_rejects_oversized_position(self) -> None:
        class OversizedPositionClient(FakeClient):
            async def get_order_fill(self, symbol: str, order_id: str) -> OrderFill | None:
                raise ExchangeAPIError("BingX order lookup rejected", retryable=False, venue=self.venue)

            async def get_symbol_position_details(self, symbol: str) -> PositionDetails:
                details = await super().get_symbol_position_details(symbol)
                oversized = details.exchange_size * 1.5
                return replace(details, exchange_size=oversized, base_quantity=oversized)

        client = OversizedPositionClient()
        store = FakeStore()
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk())

        with self.assertRaisesRegex(RuntimeError, "larger than submitted entry size"):
            await engine._execute_single_trade(_plan())

        self.assertEqual(store.open_positions, [])

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

    async def test_post_entry_liquidation_guard_never_closes_a_valid_new_position(self) -> None:
        client = FakeClient()
        client.liquidation_price = 99.999
        store = FakeStore()
        engine = _engine(
            client, store, FakeStrategy(_plan()), FakeRisk()
        )

        await engine._execute_single_trade(_plan())

        self.assertNotEqual(store.closed[0].reason, "liquidation_guard")

    async def test_volume_plan_fails_closed_before_order_when_maintenance_tiers_are_unavailable(self) -> None:
        class MissingTierClient(FakeClient):
            get_maintenance_margin_tiers = None

        client = MissingTierClient()
        plan = VolumeStrategy(_settings()).next_trade()
        await _engine(client, FakeStore(), FakeStrategy(plan), FakeRisk())._execute_single_trade(plan)
        self.assertEqual(client.orders, [])

    async def test_volume_plan_fails_closed_before_order_when_stop_reaches_liquidation(self) -> None:
        client = FakeClient()
        await _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk())._execute_single_trade(_plan(stop_loss_pct=99.0))
        self.assertEqual(client.orders, [])

    async def test_volume_plan_places_order_when_pre_order_liquidation_constraint_is_valid(self) -> None:
        client = FakeClient()
        plan = VolumeStrategy(_settings()).next_trade()
        await _engine(client, FakeStore(), FakeStrategy(plan), FakeRisk())._execute_single_trade(plan)
        self.assertTrue(client.orders)


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


class AIExitAdverseCutTests(unittest.IsolatedAsyncioTestCase):
    """AI_EXIT_ADVERSE_CUT_USDT lets the AI close a losing position.

    The pre-existing `gross >= fee` guard only honours close_now once the trade
    is already profitable, so the sole exits available to a losing trade are the
    full stop loss and the time stop. That is what makes losses run to their
    maximum while winners are clipped early by the neutral-exit band.
    """

    def _position(self) -> OpenPosition:
        return OpenPosition(
            symbol="BTC/USDT",
            direction=PositionDirection.LONG,
            entry_price=100.0,
            quantity=100.0,
            exchange_size=100.0,
            exchange_order_id="entry-1",
            opened_at=datetime.now(timezone.utc),
        )

    def _plan(self) -> TradePlan:
        # Deliberately wide envelope so neither take profit nor stop loss fires
        # before the AI is consulted.
        return _plan(stop_loss_pct=10.0, take_profit_pct=10.0)

    async def _run(self, **overrides) -> str:
        settings = _settings(position_hold_minutes=60, **overrides)
        client = FakeClient(price=100.0, price_drift=0.99)  # LONG moving against us
        strategy = FakeStrategy(exit_after=1)  # AI says close immediately
        engine = _engine(client, FakeStore(), strategy, FakeRisk(), settings)
        return await engine._monitor_position(self._position(), self._plan())

    async def test_ai_may_cut_a_losing_position_once_past_the_threshold(self) -> None:
        reason = await self._run(ai_exit_adverse_cut_usdt=5.0)
        self.assertEqual(reason, "strategy_exit:ai_close")

    async def test_disabled_by_default_keeps_the_existing_hold_behaviour(self) -> None:
        # With the cut disabled the AI is ignored while losing and the trade is
        # only released by the full stop loss.
        reason = await self._run(ai_exit_adverse_cut_usdt=0.0)
        self.assertEqual(reason, "stop_loss")

    async def test_small_adverse_drift_below_the_threshold_is_still_held(self) -> None:
        # A loss smaller than the threshold must not trigger a fee-paying exit.
        settings = _settings(position_hold_minutes=60, ai_exit_adverse_cut_usdt=10_000.0)
        client = FakeClient(price=100.0, price_drift=0.99)
        engine = _engine(client, FakeStore(), FakeStrategy(exit_after=1), FakeRisk(), settings)
        reason = await engine._monitor_position(self._position(), self._plan())
        self.assertEqual(reason, "stop_loss")

    async def test_profitable_ai_exit_is_unaffected_by_the_new_threshold(self) -> None:
        settings = _settings(position_hold_minutes=60, ai_exit_adverse_cut_usdt=5.0)
        client = FakeClient(price=100.0, price_drift=1.01)  # LONG moving in our favour
        engine = _engine(client, FakeStore(), FakeStrategy(exit_after=1), FakeRisk(), settings)
        reason = await engine._monitor_position(self._position(), self._plan())
        self.assertEqual(reason, "strategy_exit:ai_close")


class ExitCallDoesNotBlockStopLossTests(unittest.IsolatedAsyncioTestCase):
    """The AI exit call must not stall stop-loss detection.

    `exit_signal` is a network round trip to the model provider. Measured on the
    live VPS over 732 calls: median 3.9s, p90 17.9s, p99 78.9s, max 316.9s.
    Awaiting it inline leaves `_hit_stop_loss` unevaluated for that whole
    duration, so a breach is only noticed once the model answers. That is why 12
    of 19 stop losses overshot the 0.40% cap, by 31% up to 106%, and why ETH
    (median ATR 0.299%) was hit far harder than BTC (0.130%).
    """

    class _CountingClient(FakeClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.price_calls = 0

        async def get_last_price(self, symbol: str) -> float:
            self.price_calls += 1
            return await super().get_last_price(symbol)

    class _SlowStrategy:
        """Stands in for a model call that spans many monitor intervals."""

        def __init__(self, client, delay: float = 0.1) -> None:
            self.client = client
            self._delay = delay
            self.calls_at_start = -1
            self.calls_at_end = -1

        async def next_trade_plan(self):
            return None

        async def exit_signal(self, position, price, elapsed) -> ExitSignal:
            self.calls_at_start = self.client.price_calls
            await asyncio.sleep(self._delay)
            self.calls_at_end = self.client.price_calls
            self.client.price = 99.0  # breach only after the model has answered
            return ExitSignal(close_now=False, reason="hold")

    def _position(self) -> OpenPosition:
        return OpenPosition(
            symbol="BTC/USDT",
            direction=PositionDirection.LONG,
            entry_price=100.0,
            quantity=100.0,
            exchange_size=100.0,
            exchange_order_id="entry-1",
            opened_at=datetime.now(timezone.utc),
        )

    def _engine_for(self, strategy, client):
        # A real, small interval: interval 0 makes the loop hot-spin for the whole
        # simulated model call, which costs ~57s of CPU under coverage for no gain.
        settings = _settings(position_hold_minutes=60, position_monitor_interval_seconds=0.01)
        return _engine(client, FakeStore(), strategy, FakeRisk(), settings)

    async def test_price_is_still_polled_while_the_model_is_thinking(self) -> None:
        client = self._CountingClient(price=100.0, price_drift=1.0)
        strategy = self._SlowStrategy(client)
        engine = self._engine_for(strategy, client)
        reason = await engine._monitor_position(self._position(), _plan(stop_loss_pct=0.40, take_profit_pct=10.0))
        self.assertEqual(reason, "stop_loss")
        self.assertGreaterEqual(strategy.calls_at_start, 1)
        self.assertGreater(
            strategy.calls_at_end,
            strategy.calls_at_start,
            "no price was fetched while exit_signal was in flight: the stop loss was unwatched",
        )

    async def test_a_breach_during_the_call_is_caught_without_waiting_for_the_model(self) -> None:
        class _BreachesImmediately(ExitCallDoesNotBlockStopLossTests._SlowStrategy):
            async def exit_signal(self, position, price, elapsed) -> ExitSignal:
                self.calls_at_start = self.client.price_calls
                self.client.price = 99.0  # past the 0.40% stop at 99.6
                await asyncio.sleep(self._delay)
                self.calls_at_end = self.client.price_calls
                return ExitSignal(close_now=False, reason="hold")

        client = self._CountingClient(price=100.0, price_drift=1.0)
        strategy = _BreachesImmediately(client, delay=0.3)
        engine = self._engine_for(strategy, client)
        reason = await engine._monitor_position(self._position(), _plan(stop_loss_pct=0.40, take_profit_pct=10.0))
        self.assertEqual(reason, "stop_loss")
        # The monitor must have returned while the model was still answering.
        # Awaiting the call inline makes that impossible.
        self.assertEqual(strategy.calls_at_end, -1, "the monitor waited for the model before acting on the stop")

    async def test_a_failing_exit_call_does_not_abandon_the_position(self) -> None:
        # Previously the exception propagated out of _monitor_position, up through
        # _tick to run_forever, leaving the open position unmanaged.
        class _Raising(ExitCallDoesNotBlockStopLossTests._SlowStrategy):
            async def exit_signal(self, position, price, elapsed) -> ExitSignal:
                raise RuntimeError("provider exploded")

        client = self._CountingClient(price=100.0, price_drift=0.999)
        strategy = _Raising(client)
        engine = self._engine_for(strategy, client)
        reason = await engine._monitor_position(self._position(), _plan(stop_loss_pct=0.40, take_profit_pct=10.0))
        self.assertEqual(reason, "stop_loss")
