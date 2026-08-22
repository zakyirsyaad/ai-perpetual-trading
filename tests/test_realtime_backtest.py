from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from bot.exchange.okx_client import OKXFundingRateInfo, OKXOrderBook
from realtime_backtest import (
    BacktestCandle,
    _estimate_round_trip_fee,
    build_parser,
    simulate_realtime_backtest,
)


class RealtimeBacktestTests(unittest.TestCase):
    def test_backtest_cli_rejects_removed_regression_strategy(self) -> None:
        parser = build_parser()

        with self.assertRaises(SystemExit):
            with redirect_stderr(StringIO()):
                parser.parse_args(["--strategy", "regression_alpha"])

    def test_backtest_cli_accepts_ai_volume_strategy(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["--strategy", "ai_volume"])

        self.assertEqual(args.strategy, "ai_volume")

    def test_simulation_reports_volume_pnl_and_projection(self) -> None:
        settings = SimpleNamespace(
            symbols=("BTC/USDT", "ETH/USDT"),
            account_balance_usdt=1_500,
            volume_margin_per_entry_usdt=100,
            position_usdt=100,
            leverage=10,
            volume_sides_per_entry=2,
            trade_target_per_day=4,
            volume_target_usdt=10_000,
            volume_daily_target_usdt=4_000,
            position_hold_minutes=2,
            entry_interval_minutes=1,
            stop_loss_pct=0.5,
            take_profit_pct=0.5,
            taker_fee_pct=0.05,
            volume_breakeven_exit_enabled=True,
            volume_breakeven_min_seconds=5,
            volume_breakeven_min_profit_usdt=0,
            execution_guards_enabled=True,
            confluence_enabled=False,
            okx_max_spread_bps=6,
            min_orderbook_depth_multiplier=1.2,
            max_abs_funding_rate=0.001,
        )
        candles = {
            "BTC/USDT": [
                BacktestCandle(i, 100, 101, 99.9, 100 + i * 0.2, 1_000)
                for i in range(8)
            ],
            "ETH/USDT": [
                BacktestCandle(i, 100, 100.1, 99, 100 - i * 0.2, 1_000)
                for i in range(8)
            ],
        }
        books = {
            symbol: OKXOrderBook(
                symbol=symbol,
                best_bid=99.99,
                best_ask=100.01,
                mid_price=100,
                spread_bps=2,
                bid_depth_notional=100_000,
                ask_depth_notional=100_000,
                timestamp=datetime.now(timezone.utc),
                raw={},
            )
            for symbol in settings.symbols
        }
        funding = {
            symbol: OKXFundingRateInfo(symbol=symbol, funding_rate=0.0001, next_funding_time=None, raw={})
            for symbol in settings.symbols
        }

        summary = simulate_realtime_backtest(
            settings,
            candles,
            order_books=books,
            funding=funding,
            interval_minutes=1,
        )

        self.assertGreater(summary.simulated_trades, 0)
        self.assertGreater(summary.window_notional_volume_usdt, 0)
        self.assertGreater(summary.projected_daily_volume_usdt, 0)
        self.assertEqual(summary.configured_daily_entries_to_target, 2)

    def test_volume_simulation_holds_when_confluence_rejects_entry(self) -> None:
        settings = SimpleNamespace(
            symbols=("BTC/USDT",),
            account_balance_usdt=1_500,
            volume_margin_per_entry_usdt=180,
            position_usdt=180,
            leverage=100,
            volume_sides_per_entry=2,
            trade_target_per_day=28,
            volume_target_usdt=10_000_000,
            volume_daily_target_usdt=1_000_000,
            position_hold_minutes=5,
            entry_interval_minutes=3,
            stop_loss_pct=0.08,
            take_profit_pct=0.12,
            taker_fee_pct=0.05,
            volume_breakeven_exit_enabled=False,
            volume_breakeven_min_seconds=0,
            volume_breakeven_min_profit_usdt=0,
            execution_guards_enabled=False,
            confluence_enabled=True,
            confluence_min_agreement=0.55,
            confluence_min_atr_pct=0.05,
            confluence_min_score=0.05,
            confluence_atr_tp_mult=3.0,
            confluence_atr_sl_mult=1.0,
        )
        candles = {
            "BTC/USDT": [
                BacktestCandle(
                    timestamp_ms=i * 60_000,
                    open=100 + i * 0.01,
                    high=100.1 + i * 0.01,
                    low=99.9 + i * 0.01,
                    close=100 + i * 0.01,
                    volume=1_000,
                )
                for i in range(80)
            ]
        }

        with patch("realtime_backtest.FactorRegistry") as registry_class:
            registry_class.return_value.score_symbol.return_value = []
            summary = simulate_realtime_backtest(
                settings,
                candles,
                order_books={"BTC/USDT": None},
                funding={"BTC/USDT": None},
                interval_minutes=1,
                strategy="volume",
            )

        self.assertEqual(summary.simulated_trades, 0)
        self.assertGreater(summary.skipped_entries, 0)

    def test_maker_only_uses_maker_fee_on_non_forced_exits(self) -> None:
        maker_only = SimpleNamespace(
            volume_execution_mode="maker_only",
            taker_fee_pct=0.05,
            maker_fee_pct=0.02,
            maker_allow_taker_emergency_exit=True,
        )
        taker = SimpleNamespace(
            volume_execution_mode="market",
            taker_fee_pct=0.05,
            maker_fee_pct=0.02,
            maker_allow_taker_emergency_exit=True,
        )

        # Both legs maker on a breakeven exit: 2 * 0.02% of 1000 notional = 0.4
        maker_breakeven = _estimate_round_trip_fee(maker_only, 100.0, 100.0, 10.0, "breakeven_exit")
        self.assertAlmostEqual(maker_breakeven, 0.4, places=6)

        # Forced stop exit crosses the book: entry maker (0.02%) + exit taker (0.05%) = 0.7
        maker_stop = _estimate_round_trip_fee(maker_only, 100.0, 100.0, 10.0, "stop_loss")
        self.assertAlmostEqual(maker_stop, 0.7, places=6)

        # Non-maker mode pays taker on both legs: 2 * 0.05% of 1000 = 1.0
        taker_breakeven = _estimate_round_trip_fee(taker, 100.0, 100.0, 10.0, "breakeven_exit")
        self.assertAlmostEqual(taker_breakeven, 1.0, places=6)

        self.assertLess(maker_breakeven, taker_breakeven)


if __name__ == "__main__":
    unittest.main()
