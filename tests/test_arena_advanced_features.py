from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from bot.arena.agents import AttributionAI, HyperAI, ProgramAI, PromptAI, SignalAI
from bot.arena.expression_engine import FactorExpressionEngine
from bot.arena.factors import FactorEffectivenessAnalyzer, FactorRegistry
from bot.arena.flow_signals import FactorDistributionTracker, MarketFlowSignalDetector, SignalRuleConfig
from bot.core.trade_engine import TradeEngine
from bot.data.market_signals import TechnicalFeatures
from bot.models import PositionDirection


class ArenaAdvancedFeatureTests(unittest.TestCase):
    def test_factor_library_has_86_definitions_and_institutional_metrics(self) -> None:
        registry = FactorRegistry()
        features = self._features()

        definitions = registry.definitions()
        snapshots = registry.score_symbol(features)
        metrics = FactorEffectivenessAnalyzer().summarize(
            [
                {
                    "symbol": "BTC/USDT",
                    "direction": "LONG",
                    "entry_price": 100,
                    "exit_price": 101,
                    "pnl_usdt": 1.0,
                    "closed_at": datetime(2026, 6, 13, 1, tzinfo=timezone.utc).isoformat(),
                    "factor_metadata": {"top_factors": ["price_momentum_5m"]},
                },
                {
                    "symbol": "BTC/USDT",
                    "direction": "LONG",
                    "entry_price": 100,
                    "exit_price": 99.5,
                    "pnl_usdt": -0.5,
                    "closed_at": datetime(2026, 6, 13, 2, tzinfo=timezone.utc).isoformat(),
                    "factor_metadata": {"top_factors": ["price_momentum_5m"]},
                },
                {
                    "symbol": "BTC/USDT",
                    "direction": "LONG",
                    "entry_price": 100,
                    "exit_price": 102,
                    "pnl_usdt": 2.0,
                    "closed_at": datetime(2026, 6, 13, 3, tzinfo=timezone.utc).isoformat(),
                    "factor_metadata": {"top_factors": ["price_momentum_5m"]},
                },
            ]
        )

        self.assertEqual(len(definitions), 86)
        self.assertEqual(len(snapshots), 86)
        self.assertEqual({item.category for item in definitions}, {"momentum", "trend", "volatility", "volume", "microstructure"})
        self.assertIn("price_momentum_5m", {item.name for item in snapshots})
        self.assertGreater(metrics["price_momentum_5m"].icir, 0)
        self.assertGreater(metrics["price_momentum_5m"].decay_half_life_trades, 0)

    def test_expression_engine_exposes_69_safe_functions(self) -> None:
        engine = FactorExpressionEngine()
        context = {
            "close": [98, 99, 100, 102, 103],
            "volume": [100, 110, 120, 160, 180],
            "funding": 0.0004,
        }

        value = engine.evaluate("scale(pct_change(close, 4), -10, 10) + gt(mean(volume), 100)", context)

        self.assertEqual(len(engine.function_names()), 69)
        self.assertGreater(value, 1.0)
        with self.assertRaises(ValueError):
            engine.evaluate("__import__('os').system('id')", context)

    def test_flow_monitor_supports_order_flow_and_percentile_thresholds(self) -> None:
        tracker = FactorDistributionTracker()
        now = datetime(2026, 6, 13, tzinfo=timezone.utc)
        for index, score in enumerate([-0.9, -0.3, 0.0, 0.25, 0.8, 0.95]):
            tracker.add("BTC/USDT", "price_momentum_5m", score, timestamp=now + timedelta(minutes=index))
        detector = MarketFlowSignalDetector(
            SignalRuleConfig(
                order_flow_imbalance_pct=20,
                factor_percentile_trigger=0.90,
            ),
            distribution_tracker=tracker,
        )

        result = detector.evaluate_symbol(self._features(bid_depth=300_000, ask_depth=100_000), [])

        names = {item.name for item in result.new_triggers}
        self.assertIn("order_flow_imbalance", names)
        self.assertIn("factor_percentile_breakout", names)

    def test_agent_helpers_generate_configs_from_goal(self) -> None:
        goal = "I need 1,000,000 USDT daily volume with tiny loss and breakeven exits"

        self.assertEqual(HyperAI().route(goal).primary_agent, "SignalAI")
        self.assertGreaterEqual(SignalAI().design_conditions(goal).daily_volume_target_usdt, 1_000_000)
        self.assertIn("breakeven", PromptAI().build_prompt(goal).lower())
        self.assertIn("build_signal", ProgramAI().strategy_template(goal))
        self.assertIn("factor", AttributionAI().diagnosis_checklist(goal)[0].lower())

    def test_trade_engine_uses_daily_volume_target_and_breakeven_price(self) -> None:
        engine = object.__new__(TradeEngine)
        engine._settings = SimpleNamespace(
            strategy_mode="ai_volume",
            volume_stop_at_target_enabled=True,
            volume_daily_target_usdt=1_000_000,
            volume_target_usdt=30_000_000,
            volume_daily_fee_budget_usdt=0,
            taker_fee_pct=0.05,
        )
        engine._trade_store = _Store(volume=1_010_000)

        self.assertEqual(
            self._run(engine._volume_runtime_pause_reason()),
            "daily_volume_target_reached",
        )
        breakeven = engine._breakeven_exit_price(
            direction=PositionDirection.LONG,
            entry_price=100.0,
            quantity=1.0,
            taker_fee_pct=0.05,
            target_net_usdt=0.0,
        )
        self.assertAlmostEqual(breakeven, 100.100050025, places=6)

    @staticmethod
    def _run(awaitable):
        import asyncio

        return asyncio.run(awaitable)

    @staticmethod
    def _features(bid_depth: float = 180_000, ask_depth: float = 170_000) -> TechnicalFeatures:
        return TechnicalFeatures(
            symbol="BTC/USDT",
            last_price=100_000,
            ema_fast=100_700,
            ema_slow=99_900,
            ema_21=99_900,
            rsi_14=64,
            atr_14=420,
            macd_line=120,
            macd_signal=80,
            macd_hist=0.72,
            change_pct_1=0.22,
            change_pct_5=1.4,
            volume=1250,
            volume_change_pct=86,
            funding_rate=0.0008,
            open_interest=2_500_000,
            open_interest_change_pct=9.5,
            spread_bps=2.1,
            bid_depth_notional=bid_depth,
            ask_depth_notional=ask_depth,
        )


class _Store:
    def __init__(self, volume: float, total_volume: float | None = None) -> None:
        self.volume = volume
        self.total_volume = volume if total_volume is None else total_volume

    async def total_volume_for_day(self) -> float:
        return self.volume

    async def total_volume_all_time(self) -> float:
        return self.total_volume


if __name__ == "__main__":
    unittest.main()
