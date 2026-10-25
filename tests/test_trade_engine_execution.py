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
        self.exit_verdicts: list[dict] = []

    async def initialize(self) -> None:
        return None

    async def daily_volume_series(self) -> list:
        return []

    async def daily_ai_usage_series(self) -> list:
        return []

    async def save_open_position(self, position: OpenPosition) -> None:
        self.open_positions.append(position)

    async def remove_open_position(self, symbol: str) -> None:
        self.removed.append(symbol)

    async def list_open_positions(self):
        return []

    async def save_closed_trade(self, trade: ClosedTrade) -> None:
        self.closed.append(trade)

    async def save_exit_verdict(self, **verdict) -> None:
        self.exit_verdicts.append(verdict)

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

    async def test_fee_refresh_hands_the_venue_rates_to_the_strategy(self) -> None:
        # The shadow floor must net the same taker rate the live neutral exit does.
        class VenueFeeClient(FakeClient):
            async def get_trade_fee_rates(self, symbol: str):
                return SimpleNamespace(maker_fee_pct=-0.02, taker_fee_pct=0.035, raw={})

        class FeeStrategy(FakeStrategy):
            def __init__(self) -> None:
                super().__init__()
                self.fees: list[dict] = []

            def set_taker_fee_pcts(self, fees: dict) -> None:
                self.fees.append(fees)

        strategy = FeeStrategy()
        engine = _engine(VenueFeeClient(), FakeStore(), strategy, FakeRisk())

        await engine._refresh_exchange_fee_rates()

        self.assertEqual(strategy.fees, [{"BTC/USDT": 0.035}])
        self.assertAlmostEqual(engine._taker_fee_pct("BTC/USDT"), 0.035)

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


class AIExitVerdictRecordingTests(unittest.IsolatedAsyncioTestCase):
    """Every model exit verdict is persisted, honoured or overruled.

    Nothing records what the model asked for while a position is open:
    `ai_decisions` holds entry decisions only, and the engine logs the trigger
    rather than the verdict. Without that record the `gross >= fee` gate can
    never be judged against the outcome it produced.
    """

    class _VerdictStrategy:
        """Returns model-backed verdicts instead of the policy's gated fallback."""

        def __init__(self, *, close_now: bool, ai_called: bool = True) -> None:
            self._close_now = close_now
            self._ai_called = ai_called

        async def next_trade_plan(self):
            return None

        async def exit_signal(self, position, price, elapsed) -> ExitSignal:
            return ExitSignal(
                close_now=self._close_now,
                reason="ai_close" if self._close_now else "hold",
                ai_called=self._ai_called,
                confidence=0.81,
            )

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

    async def _run(self, strategy, *, price_drift: float, **overrides):
        settings = _settings(position_hold_minutes=60, **overrides)
        client = FakeClient(price=100.0, price_drift=price_drift)
        store = FakeStore()
        engine = _engine(client, store, strategy, FakeRisk(), settings)
        # Wide envelope so the verdict path is reached before TP or SL.
        reason = await engine._monitor_position(self._position(), _plan(stop_loss_pct=10.0, take_profit_pct=10.0))
        return reason, store

    async def test_close_request_the_engine_overrules_is_recorded_with_its_pnl(self) -> None:
        reason, store = await self._run(
            self._VerdictStrategy(close_now=True), price_drift=0.99, ai_exit_adverse_cut_usdt=0.0
        )
        self.assertEqual(reason, "stop_loss")
        overruled = [v for v in store.exit_verdicts if v["close_now"] and not v["honored"]]
        self.assertTrue(overruled)
        self.assertEqual(overruled[0]["symbol"], "BTC/USDT")
        self.assertEqual(overruled[0]["skip_reason"], "below_fee_and_above_cut")
        self.assertEqual(overruled[0]["confidence"], 0.81)
        self.assertLess(overruled[0]["net_after_fee_usdt"], 0.0)

    async def test_honoured_close_request_is_recorded_as_honoured(self) -> None:
        reason, store = await self._run(
            self._VerdictStrategy(close_now=True), price_drift=1.01, ai_exit_adverse_cut_usdt=5.0
        )
        self.assertEqual(reason, "strategy_exit:ai_close")
        self.assertTrue(store.exit_verdicts[-1]["honored"])
        self.assertIsNone(store.exit_verdicts[-1]["skip_reason"])

    async def test_gated_fallback_without_a_model_call_is_not_recorded(self) -> None:
        # Most monitor ticks return the exit policy's deterministic hold. Storing
        # those would bury the handful of real verdicts under thousands of rows.
        reason, store = await self._run(
            self._VerdictStrategy(close_now=False, ai_called=False), price_drift=0.99
        )
        self.assertEqual(reason, "stop_loss")
        self.assertEqual(store.exit_verdicts, [])


class MonitorSurvivesVenueErrorsTests(unittest.IsolatedAsyncioTestCase):
    """A venue hiccup must never leave a live position unmonitored.

    Observed 2026-09-12 15:03 UTC: one `quote/price` read failed with an SSL
    `ConnectError` twelve minutes into a LONG. The exception escaped
    `_monitor_position`, reached the catch-all in `run_forever`, and the engine
    went back to the entry loop. The position stayed open for hours with no stop
    loss, no time stop and no exit evaluation.
    """

    class _FlakyPriceClient(FakeClient):
        """Fails the listed price reads, then keeps drifting against a LONG."""

        def __init__(self, *, fail_on: set[int], **kwargs) -> None:
            super().__init__(**kwargs)
            self._fail_on = fail_on
            self.price_calls = 0

        async def get_last_price(self, symbol: str) -> float:
            self.price_calls += 1
            if self.price_calls in self._fail_on:
                raise ExchangeAPIError(
                    "BingX request failed for /openApi/swap/v2/quote/price: ConnectError: "
                    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
                    venue="bingx",
                )
            return await super().get_last_price(symbol)

    class _HoldStrategy:
        async def next_trade_plan(self):
            return None

        async def exit_signal(self, position, price, elapsed) -> ExitSignal:
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

    def _engine(self, client, store=None):
        settings = _settings(position_hold_minutes=60)
        return _engine(client, store or FakeStore(), self._HoldStrategy(), FakeRisk(), settings)

    async def test_a_failed_price_read_does_not_abandon_the_position(self) -> None:
        client = self._FlakyPriceClient(fail_on={2}, price=100.0, price_drift=0.999)
        engine = self._engine(client)

        reason = await engine._monitor_position(self._position(), _plan(stop_loss_pct=0.40, take_profit_pct=10.0))

        self.assertEqual(reason, "stop_loss")
        self.assertGreater(client.price_calls, 2)

    async def test_repeated_price_failures_are_escalated_to_error(self) -> None:
        client = self._FlakyPriceClient(fail_on={2, 3, 4}, price=100.0, price_drift=0.999)
        engine = self._engine(client)

        with self.assertLogs("test.engine", level="ERROR") as logs:
            reason = await engine._monitor_position(self._position(), _plan(stop_loss_pct=0.40, take_profit_pct=10.0))

        self.assertEqual(reason, "stop_loss")
        self.assertTrue(any("consecutive" in line for line in logs.output))

    async def test_unexpected_monitor_failure_keeps_managing_the_position(self) -> None:
        client = FakeClient(price=100.0, price_drift=0.999)
        client.position_size = 100.0
        store = FakeStore()
        engine = self._engine(client, store)
        calls = {"n": 0}

        async def monitor_once_broken(position, plan):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("unexpected failure inside the monitor loop")
            return "stop_loss"

        engine._monitor_position = monitor_once_broken
        await engine._manage_open_position(
            self._position(),
            _plan(stop_loss_pct=0.40, take_profit_pct=10.0),
            entry_order_id="entry-1",
            entry_fill=None,
            entry_fee_usdt=0.0,
            record_entry_cooldown=False,
        )

        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(store.closed), 1)
        self.assertEqual(store.closed[0].reason, "stop_loss")


class PositionManagementFailureLatchTests(unittest.IsolatedAsyncioTestCase):
    """A failure in the close phase must not silently hand control back to entries.

    Once an exit order has been sent, retrying it automatically could duplicate an
    order, which the order-idempotency rules forbid. What must never happen is the
    engine returning to the entry loop as if the position were gone. So: check the
    venue. Flat means nothing is exposed (for example a Supabase 504 after a clean
    close) and trading continues; open or unreadable latches entries loudly.
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
            stop_loss_pct=0.40,
            take_profit_pct=10.0,
        )

    def _engine(self, client):
        return _engine(client, FakeStore(), FakeStrategy(), FakeRisk(), _settings())

    @staticmethod
    def _failing_manage(message: str):
        async def manage(position, plan, **kwargs):
            raise RuntimeError(message)
        return manage

    async def _run_guarded(self, engine) -> None:
        await engine._manage_open_position_guarded(
            self._position(),
            _plan(stop_loss_pct=0.40, take_profit_pct=10.0),
            entry_order_id="entry-1",
            entry_fill=None,
            entry_fee_usdt=0.0,
            record_entry_cooldown=False,
        )

    async def test_a_failed_exit_with_the_position_still_open_blocks_new_entries(self) -> None:
        client = FakeClient()
        client.position_size = 100.0
        engine = self._engine(client)
        engine._manage_open_position = self._failing_manage("Exit order placed but position still open for BTC/USDT.")

        await self._run_guarded(engine)

        self.assertIsNotNone(engine._entries_blocked_reason)
        self.assertIn("BTC/USDT", engine._entries_blocked_reason)

    async def test_a_failure_after_the_venue_is_flat_does_not_block_entries(self) -> None:
        client = FakeClient()
        client.position_size = 0.0
        engine = self._engine(client)
        engine._manage_open_position = self._failing_manage("Supabase request failed for table trades: 504")

        await self._run_guarded(engine)

        self.assertIsNone(engine._entries_blocked_reason)

    async def test_an_unreadable_venue_after_a_failure_blocks_new_entries(self) -> None:
        class _Unreadable(FakeClient):
            async def has_open_position(self, symbol: str) -> bool:
                raise ExchangeAPIError("positions endpoint unreachable", venue="bingx")

        engine = self._engine(_Unreadable())
        engine._manage_open_position = self._failing_manage("exit order rejected")

        await self._run_guarded(engine)

        self.assertIsNotNone(engine._entries_blocked_reason)

    async def test_a_fresh_entry_whose_management_fails_blocks_new_entries(self) -> None:
        # Wiring for the call site that had no guard at all: _execute_single_trade.
        client = FakeClient()
        engine = _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk())
        engine._manage_open_position = self._failing_manage("exit order rejected")

        await engine._execute_single_trade(_plan())

        self.assertGreater(client.position_size, 0.0)
        self.assertIsNotNone(engine._entries_blocked_reason)

    async def test_a_resumed_position_whose_management_fails_blocks_new_entries(self) -> None:
        # The resume path used to log the exception and move on, orphaning the position.
        client = FakeClient()
        client.position_size = 100.0
        engine = self._engine(client)
        engine._resumable_positions = [self._position()]
        engine._manage_open_position = self._failing_manage("exit order rejected")

        await engine._resume_persisted_positions()

        self.assertIsNotNone(engine._entries_blocked_reason)


class NeutralExitProfitLockTests(unittest.IsolatedAsyncioTestCase):
    """The neutral exit must lock a profit that jumps over its window.

    Observed on trade #574 (2026-09-14): net after fees went +10 -> +85 within
    one minute, sat at +78..+122 for twenty minutes, then fell back to +0 within
    one minute and closed on the time stop at -0.84. The legacy check only exits
    while `min_net <= net <= min_net + band` on a 5 s poll, so a move that skips
    the 20 USDT window in either direction is never taken. Every exit replay so
    far modelled this exit as a floor, so live was quietly worse than measured.
    """

    class _ScriptedPriceClient(FakeClient):
        def __init__(self, prices, **kwargs) -> None:
            super().__init__(**kwargs)
            self._prices = list(prices)

        async def get_last_price(self, symbol: str) -> float:
            if len(self._prices) > 1:
                self.price = self._prices.pop(0)
            else:
                self.price = self._prices[0]
            return self.price

    class _HoldStrategy:
        async def next_trade_plan(self):
            return None

        async def exit_signal(self, position, price, elapsed) -> ExitSignal:
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

    def _engine(self, prices, **overrides):
        settings = _settings(
            position_hold_minutes=60,
            volume_neutral_exit_enabled=True,
            volume_neutral_exit_min_seconds=0,
            volume_neutral_exit_min_net_usdt=40.0,
            volume_neutral_exit_band_usdt=20.0,
            **overrides,
        )
        client = self._ScriptedPriceClient(prices, price=100.0)
        return _engine(client, FakeStore(), self._HoldStrategy(), FakeRisk(), settings), client

    def _price_for_net(self, engine, net: float) -> float:
        # Solve with the engine's own PnL and fee model so the test never drifts from it.
        position = self._position()
        lo, hi = 80.0, 120.0
        for _ in range(80):
            mid = (lo + hi) / 2
            value = engine._estimate_gross_pnl(position.direction, 100.0, mid, 100.0) - engine._estimate_total_taker_fee(
                100.0, mid, 100.0, engine._taker_fee_pct("BTC/USDT")
            )
            lo, hi = (mid, hi) if value < net else (lo, mid)
        return (lo + hi) / 2

    async def _run(self, nets, **overrides):
        probe, _ = self._engine([100.0], **overrides)
        prices = [self._price_for_net(probe, n) for n in nets] + [89.0]  # 89 breaches the 10% stop
        engine, client = self._engine(prices, **overrides)
        reason = await engine._monitor_position(self._position(), _plan(stop_loss_pct=10.0, take_profit_pct=10.0))
        net_at_exit = engine._estimate_gross_pnl(PositionDirection.LONG, 100.0, client.price, 100.0) - engine._estimate_total_taker_fee(
            100.0, client.price, 100.0, engine._taker_fee_pct("BTC/USDT")
        )
        return reason, net_at_exit

    async def test_window_mode_keeps_the_legacy_behaviour_by_default(self) -> None:
        # Characterisation guard: an unchanged .env must keep today's exits.
        reason, _ = await self._run([10, 85, 100, 122, 95])
        self.assertEqual(reason, "stop_loss")

    async def test_floor_mode_takes_a_profit_that_jumped_over_the_window(self) -> None:
        reason, net = await self._run([10, 85, 100, 122, 95], volume_neutral_exit_mode="floor")
        self.assertEqual(reason, "neutral_exit")
        self.assertAlmostEqual(net, 85.0, delta=0.5)

    async def test_trailing_mode_rides_a_runner_and_exits_on_the_pullback(self) -> None:
        reason, net = await self._run(
            [10, 85, 100, 122, 95], volume_neutral_exit_mode="trailing", volume_neutral_exit_trail_usdt=20.0
        )
        self.assertEqual(reason, "neutral_exit")
        self.assertAlmostEqual(net, 95.0, delta=0.5)

    async def test_trailing_mode_never_lets_an_armed_profit_fall_below_the_floor(self) -> None:
        # Peak 45 with a 20 trail would put the lock at 25; the floor keeps it at 40.
        reason, net = await self._run(
            [10, 45, 42, 39], volume_neutral_exit_mode="trailing", volume_neutral_exit_trail_usdt=20.0
        )
        self.assertEqual(reason, "neutral_exit")
        self.assertAlmostEqual(net, 39.0, delta=0.5)


class NeutralExitModeSettingTests(unittest.TestCase):
    """A mistyped mode must refuse to boot instead of silently meaning `window`.

    `window` is the legacy behaviour that misses profits jumping over the band, so
    a typo such as `trail` falling back to it would quietly disable the fix.
    """

    def _settings(self, **env) -> Settings:
        return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", STRATEGY_MODE="volume", **env)

    def test_modes_are_accepted_case_insensitively(self) -> None:
        self.assertEqual(self._settings().volume_neutral_exit_mode, "window")
        self.assertEqual(self._settings(VOLUME_NEUTRAL_EXIT_MODE="Trailing").volume_neutral_exit_mode, "trailing")
        self.assertEqual(self._settings(VOLUME_NEUTRAL_EXIT_MODE=" floor ").volume_neutral_exit_mode, "floor")

    def test_an_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self._settings(VOLUME_NEUTRAL_EXIT_MODE="trail")
        self.assertIn("VOLUME_NEUTRAL_EXIT_MODE", str(ctx.exception))


class BingXViewTests(unittest.IsolatedAsyncioTestCase):
    """What BingX shows and what the bot decides on must be reconcilable.

    BingX shows unrealised PnL before fees; the neutral exit used net after both
    taker fees. On trade #587 the screen showed about +50 while the bot saw +32,
    below its +40 floor, so the position that "looked profitable" was left open.
    """

    _Client = NeutralExitProfitLockTests._ScriptedPriceClient
    _Hold = NeutralExitProfitLockTests._HoldStrategy

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

    async def _run(self, grosses, **overrides):
        # For this position gross PnL is (price - 100) * 100, so price = 100 + gross / 100.
        prices = [100.0 + g / 100.0 for g in grosses] + [89.0]
        settings = _settings(
            position_hold_minutes=60,
            volume_neutral_exit_enabled=True,
            volume_neutral_exit_min_seconds=0,
            volume_neutral_exit_min_net_usdt=40.0,
            volume_neutral_exit_band_usdt=20.0,
            volume_neutral_exit_mode="floor",
            **overrides,
        )
        client = self._Client(prices, price=100.0)
        engine = _engine(client, FakeStore(), self._Hold(), FakeRisk(), settings)
        reason = await engine._monitor_position(self._position(), _plan(stop_loss_pct=10.0, take_profit_pct=10.0))
        return reason, client

    async def test_net_basis_stays_the_default(self) -> None:
        # Gross +45 is about +35 net here: below a +40 net floor, so no exit.
        reason, _ = await self._run([10, 45])
        self.assertEqual(reason, "stop_loss")

    async def test_gross_basis_applies_the_floor_to_the_number_bingx_shows(self) -> None:
        reason, client = await self._run([10, 45], volume_neutral_exit_basis="gross")
        self.assertEqual(reason, "neutral_exit")
        self.assertAlmostEqual((client.price - 100.0) * 100.0, 45.0, delta=0.01)

    async def test_status_line_shows_the_bingx_view_next_to_net(self) -> None:
        with self.assertLogs("test.engine", level="INFO") as logs:
            await self._run([30], position_status_log_seconds=60)
        status = [line for line in logs.output if "BingX" in line]
        self.assertTrue(status, "no status line reconciling the BingX view")
        self.assertIn("net", status[0])
        self.assertIn("fee", status[0])


class NeutralExitBasisSettingTests(unittest.TestCase):
    def _settings(self, **env) -> Settings:
        return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", STRATEGY_MODE="volume", **env)

    def test_basis_defaults_to_net_and_accepts_gross(self) -> None:
        self.assertEqual(self._settings().volume_neutral_exit_basis, "net")
        self.assertEqual(self._settings(VOLUME_NEUTRAL_EXIT_BASIS=" Gross ").volume_neutral_exit_basis, "gross")

    def test_an_unknown_basis_is_rejected(self) -> None:
        with self.assertRaises(Exception) as ctx:
            self._settings(VOLUME_NEUTRAL_EXIT_BASIS="screen")
        self.assertIn("VOLUME_NEUTRAL_EXIT_BASIS", str(ctx.exception))


class ExitRejectionKeepsThePositionManagedTests(unittest.IsolatedAsyncioTestCase):
    """A close the venue refuses outright is not unknown exposure.

    BingX answered every close with `109403 risk forbidden` from 2026-09-25 on:
    the single refusal escaped the monitor, latched entries and killed the loop
    that owns the stop, so two positions drifted ~1% into liquidation (about
    −250 each) where the 0.35% stop would have cost ~−90. A refused request
    placed no order, so re-sending it later cannot duplicate anything. Only an
    *uncertain* submission — where an order may exist — must still latch.
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
            stop_loss_pct=0.40,
            take_profit_pct=10.0,
        )

    def _engine_for(self, client, store):
        return _engine(client, store, FakeStrategy(), FakeRisk(), _settings(exit_rejection_retry_seconds=0))

    @staticmethod
    def _client_rejecting(error: Exception, *, times: int):
        client = FakeClient(price=100.0, price_drift=0.999)
        client.position_size = 100.0
        original = client.place_market_order
        calls = {"n": 0}

        async def place_market_order(symbol, direction, size, reduce_only=False):
            calls["n"] += 1
            if calls["n"] <= times:
                raise error
            return await original(symbol, direction, size, reduce_only=reduce_only)

        client.place_market_order = place_market_order
        return client, calls

    @staticmethod
    async def _always_stop(position, plan):
        return "stop_loss"

    async def _run(self, engine) -> None:
        await engine._manage_open_position_guarded(
            self._position(),
            _plan(stop_loss_pct=0.40, take_profit_pct=10.0),
            entry_order_id="entry-1",
            entry_fill=None,
            entry_fee_usdt=0.0,
            record_entry_cooldown=False,
        )

    async def test_a_refused_exit_is_retried_and_never_latches_entries(self) -> None:
        refusal = ExchangeAPIError(
            "BingX API error on /openApi/swap/v2/trade/order: code=109403 msg=risk forbidden",
            code="109403",
            retryable=False,
            venue="bingx",
        )
        client, calls = self._client_rejecting(refusal, times=1)
        store = FakeStore()
        engine = self._engine_for(client, store)
        engine._monitor_position = self._always_stop

        await self._run(engine)

        self.assertEqual(calls["n"], 2, "the refused close must be retried on the next monitor pass")
        self.assertIsNone(engine._entries_blocked_reason)
        self.assertEqual(len(store.closed), 1)
        self.assertEqual(store.closed[0].reason, "stop_loss")

    async def test_an_uncertain_exit_submission_still_latches_entries(self) -> None:
        client, _ = self._client_rejecting(
            UncertainOrderSubmissionError(
                "exit order may have been accepted",
                symbol="BTC/USDT",
                client_order_id="apt-exit-1",
                venue="bingx",
            ),
            times=99,
        )
        engine = self._engine_for(client, FakeStore())
        engine._monitor_position = self._always_stop

        await self._run(engine)

        self.assertIsNotNone(engine._entries_blocked_reason)

    async def test_a_local_validation_failure_still_latches_entries(self) -> None:
        # No venue code: the request never reached BingX, so nothing proves the
        # venue refused it, and an operator has to look at it.
        client, _ = self._client_rejecting(
            ExchangeAPIError("Order quantity 0.0 is below tradeMinQuantity", retryable=False, venue="bingx"),
            times=99,
        )
        engine = self._engine_for(client, FakeStore())
        engine._monitor_position = self._always_stop

        await self._run(engine)

        self.assertIsNotNone(engine._entries_blocked_reason)


class ExitRejectionRetrySettingTests(unittest.TestCase):
    """The retry spacing is a knob, and a negative value must refuse to boot."""

    def _settings(self, **env) -> Settings:
        return Settings(_env_file=None, BINGX_API_KEY="test-key", BINGX_SECRET_KEY="test-secret", STRATEGY_MODE="volume", **env)

    def test_default_spaces_retries_without_disabling_them(self) -> None:
        self.assertEqual(self._settings().exit_rejection_retry_seconds, 15.0)
        self.assertEqual(self._settings(EXIT_REJECTION_RETRY_SECONDS="0").exit_rejection_retry_seconds, 0.0)

    def test_a_negative_delay_is_refused(self) -> None:
        with self.assertRaises(Exception):
            self._settings(EXIT_REJECTION_RETRY_SECONDS="-1")
