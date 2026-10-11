from __future__ import annotations

import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from bot.core.trade_engine import TradeEngine
from bot.exchange.contracts import (
    FundingRateInfo,
    MaintenanceMarginTier,
    OrderBookSnapshot,
    OrderFill,
    PositionDetails,
)
from bot.models import ClosedTrade, OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import DailyRiskState
from bot.strategy.base import ExitSignal
from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.observer import NULL_OBSERVER, EngineTelemetry
from bot.telemetry.wire import Meta, PriceMessage

META = Meta.model_validate(json.loads((Path(__file__).parent / "fixtures" / "jev_wire_samples.json").read_text())["meta"])
T0 = 1_791_661_170_000


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

    def __init__(self, *, price: float = 100.0) -> None:
        self.price = price
        self.position_size = 0.0
        self.orders: list[dict] = []

    async def get_last_price(self, symbol: str) -> float:
        self.price *= 1.001  # drift up so the monitor always reaches a real exit
        return self.price

    async def get_order_book(self, symbol: str, depth: int = 20) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            symbol=symbol,
            best_bid=self.price * 0.9999,
            best_ask=self.price * 1.0001,
            mid_price=self.price,
            spread_bps=2.0,
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
            liquidation_price=None,
            pos_side="LONG",
            raw={},
            position_id="p-live",
        )

    async def has_open_position(self, symbol: str) -> bool:
        return self.position_size > 0

    async def has_any_open_position(self, symbols) -> bool:
        return self.position_size > 0


class FakeStore:
    def __init__(self) -> None:
        self.closed: list[ClosedTrade] = []
        self.open_positions: list[OpenPosition] = []

    async def save_open_position(self, position: OpenPosition) -> None:
        self.open_positions.append(position)

    async def remove_open_position(self, symbol: str) -> None:
        return None

    async def save_closed_trade(self, trade: ClosedTrade) -> None:
        self.closed.append(trade)

    async def save_exit_verdict(self, **verdict) -> None:
        return None

    async def total_volume_for_day(self, day=None) -> float:
        return 0.0


class FakeRisk:
    def __init__(self, allowed: bool = True) -> None:
        self.state = DailyRiskState(
            day=datetime.now(timezone.utc).date(),
            stop_losses=0,
            closed_trades=0,
            total_pnl_usdt=0.0,
            consecutive_losses=0,
            allowed_to_trade=allowed,
        )

    async def get_daily_state(self) -> DailyRiskState:
        return self.state


class FakeStrategy:
    def __init__(self, plan: TradePlan | None = None) -> None:
        self.plan = plan
        self.next_calls = 0

    async def next_trade_plan(self):
        self.next_calls += 1
        return self.plan

    async def exit_signal(self, position, price, elapsed) -> ExitSignal:
        return ExitSignal(close_now=True, reason="ai_close")


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


def _price(mid: float = 100.0) -> PriceMessage:
    return PriceMessage(coin="BTC", ts=T0, mid=mid, best_bid=mid - 0.5, best_ask=mid + 0.5, spread_bps=1.0)


class RecordingObserver:
    def __init__(self, client: FakeClient | None = None) -> None:
        self._client = client
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str, **fields) -> None:
        orders = len(self._client.orders) if self._client is not None else 0
        self.calls.append((name, {**fields, "orders_so_far": orders}))

    def decided(self, plan, *, latency_ms):
        self._record("decided", plan=plan, latency_ms=latency_ms)

    def entry_quoted(self, plan, *, price, size, order_id):
        self._record("entry_quoted", plan=plan, price=price, size=size, order_id=order_id)

    def entry_filled(self, position, *, leverage, fee_usdt):
        self._record("entry_filled", position=position, leverage=leverage, fee_usdt=fee_usdt)

    def position_resumed(self, position, *, leverage):
        self._record("position_resumed", position=position, leverage=leverage)

    def position_marked(self, position, *, net_usdt):
        self._record("position_marked", position=position, net_usdt=net_usdt)

    def position_closed(self, trade, *, fee_usdt):
        self._record("position_closed", trade=trade, fee_usdt=fee_usdt)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def first(self, name: str) -> dict:
        return next(fields for call, fields in self.calls if call == name)


def _engine(client, store, strategy, risk, observer=None) -> TradeEngine:
    kwargs = {} if observer is None else {"telemetry": observer}
    return TradeEngine(_settings(), logging.getLogger("test.engine.telemetry"), client, strategy, risk, store, **kwargs)


class TickHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_hold_tick_reports_no_plan_and_how_long_the_strategy_took(self) -> None:
        observer = RecordingObserver()
        engine = _engine(FakeClient(), FakeStore(), FakeStrategy(None), FakeRisk(), observer)

        await engine._tick()

        self.assertEqual(observer.names(), ["decided"])
        fields = observer.first("decided")
        self.assertIsNone(fields["plan"])
        self.assertGreaterEqual(fields["latency_ms"], 0)

    async def test_a_tick_stopped_by_a_gate_never_reaches_the_observer(self) -> None:
        observer = RecordingObserver()
        strategy = FakeStrategy(_plan())
        engine = _engine(FakeClient(), FakeStore(), strategy, FakeRisk(allowed=False), observer)

        await engine._tick()

        self.assertEqual(strategy.next_calls, 0)
        self.assertEqual(observer.calls, [])

    async def test_the_decision_is_reported_before_any_order_is_sent(self) -> None:
        client = FakeClient()
        observer = RecordingObserver(client)
        engine = _engine(client, FakeStore(), FakeStrategy(_plan()), FakeRisk(), observer)

        await engine._tick()

        self.assertEqual(observer.first("decided")["orders_so_far"], 0)
        self.assertEqual(observer.first("decided")["plan"].symbol, "BTC/USDT")


class LifecycleHookTests(unittest.IsolatedAsyncioTestCase):
    async def run_trade(self) -> tuple[RecordingObserver, FakeClient, FakeStore]:
        client = FakeClient()
        store = FakeStore()
        observer = RecordingObserver(client)
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk(), observer)
        await engine._tick()
        return observer, client, store

    async def test_a_full_trade_reports_decision_quote_fill_marks_and_close_in_order(self) -> None:
        observer, _, store = await self.run_trade()

        names = observer.names()
        self.assertEqual(names[:3], ["decided", "entry_quoted", "entry_filled"])
        self.assertEqual(names[-1], "position_closed")
        self.assertIn("position_marked", names[3:-1])
        self.assertEqual(len(store.closed), 1)

    async def test_the_quote_carries_the_order_that_was_actually_sent(self) -> None:
        observer, client, _ = await self.run_trade()

        quote = observer.first("entry_quoted")
        self.assertEqual(quote["order_id"], "o1")
        self.assertEqual(quote["size"], client.orders[0]["size"])
        self.assertAlmostEqual(quote["price"], 100.0, delta=0.5)
        self.assertEqual(quote["orders_so_far"], 1)

    async def test_the_fill_carries_the_persisted_position_its_leverage_and_fee(self) -> None:
        observer, _, store = await self.run_trade()

        fill = observer.first("entry_filled")
        self.assertIs(fill["position"], store.open_positions[0])
        self.assertEqual(fill["leverage"], 100)
        self.assertEqual(fill["fee_usdt"], 0.01)

    async def test_the_close_carries_the_stored_trade_and_the_fee_that_was_charged(self) -> None:
        observer, _, store = await self.run_trade()

        closed = observer.first("position_closed")
        self.assertIs(closed["trade"], store.closed[0])
        self.assertAlmostEqual(closed["fee_usdt"], 0.02)

    async def test_marks_carry_the_open_position_and_a_finite_net(self) -> None:
        observer, _, store = await self.run_trade()

        mark = observer.first("position_marked")
        self.assertIs(mark["position"], store.open_positions[0])
        self.assertIsInstance(mark["net_usdt"], float)

    async def test_a_position_resumed_after_a_restart_is_reported_then_closed(self) -> None:
        client = FakeClient()
        client.position_size = 1.8
        store = FakeStore()
        observer = RecordingObserver(client)
        engine = _engine(client, store, FakeStrategy(None), FakeRisk(), observer)
        engine._resumable_positions = [
            OpenPosition(
                symbol="BTC/USDT",
                direction=PositionDirection.LONG,
                entry_price=100.0,
                quantity=1.8,
                exchange_size=1.8,
                exchange_order_id="order-1",
                opened_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                entry_reason="ai_volume:OPEN_LONG",
                stop_loss_pct=0.5,
                take_profit_pct=1.2,
            )
        ]

        await engine._resume_persisted_positions()

        self.assertEqual(observer.names()[0], "position_resumed")
        self.assertEqual(observer.first("position_resumed")["leverage"], 100)
        self.assertEqual(observer.names()[-1], "position_closed")


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_engine_without_a_feed_uses_the_do_nothing_observer(self) -> None:
        engine = _engine(FakeClient(), FakeStore(), FakeStrategy(None), FakeRisk())

        self.assertIs(engine._telemetry, NULL_OBSERVER)

    async def test_a_broken_observer_cannot_stop_a_trade_from_opening_and_closing(self) -> None:
        client = FakeClient()
        store = FakeStore()
        observer = EngineTelemetry(TelemetryHub(META, TelemetryBus()), ("BTC/USDT",), account_balance_usdt=1500.0)
        observer._hub = None  # type: ignore[assignment]  # every hook fails inside
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk(), observer)

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            await engine._tick()

        self.assertEqual(len(store.closed), 1)
        self.assertEqual(len(client.orders), 2)  # entry and exit both went out
        self.assertEqual(client.position_size, 0.0)

    async def test_a_broken_hub_cannot_stop_a_trade_from_opening_and_closing(self) -> None:
        client = FakeClient()
        store = FakeStore()
        hub = TelemetryHub(META, TelemetryBus())
        hub.record_price(_price())
        hub._history["BTC"] = None  # type: ignore[assignment]
        observer = EngineTelemetry(hub, ("BTC/USDT",), account_balance_usdt=1500.0)
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk(), observer)

        with self.assertLogs("volume_bot.telemetry", level=logging.WARNING):
            await engine._tick()

        self.assertEqual(len(store.closed), 1)

    async def test_a_live_feed_sees_the_whole_trade(self) -> None:
        client = FakeClient()
        store = FakeStore()
        hub = TelemetryHub(META, TelemetryBus())
        hub.record_price(_price())
        observer = EngineTelemetry(hub, ("BTC/USDT",), account_balance_usdt=1500.0, mark_interval_seconds=0.0)
        engine = _engine(client, store, FakeStrategy(_plan()), FakeRisk(), observer)

        await engine._tick()

        history = hub.history_body()["BTC"]
        self.assertEqual([b["decision"]["intent"] for b in history], ["open", "close"])
        self.assertEqual(history[0]["quote"]["status"], "placed")
        self.assertEqual(history[0]["fill"]["dir"], "open")
        self.assertEqual(history[1]["position"]["side"], "flat")
        self.assertEqual(history[1]["fill"]["dir"], "close")
        self.assertEqual(history[1]["totals"]["fills"], 2)
        self.assertEqual(history[1]["totals"]["realizedUsd"], store.closed[0].pnl_usdt)


if __name__ == "__main__":
    unittest.main()
