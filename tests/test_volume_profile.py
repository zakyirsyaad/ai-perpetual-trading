from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from pydantic import ValidationError

from bot.config import Settings
from bot.strategy.volume_strategy import VolumeStrategy
from bot.volume import VolumeBuildProfile


class VolumeProfileTests(unittest.TestCase):
    def test_volume_profile_matches_target_math(self) -> None:
        profile = VolumeBuildProfile(
            account_equity_usdt=Decimal("1500"),
            margin_per_entry_usdt=Decimal("180"),
            leverage=100,
            sides_per_entry=2,
            target_entries_per_day=16,
            target_volume_usdt=Decimal("10000000"),
        )

        self.assertEqual(profile.notional_per_side_usdt, Decimal("18000"))
        self.assertEqual(profile.volume_per_entry_usdt, Decimal("36000"))
        self.assertEqual(profile.daily_volume_usdt, Decimal("576000"))
        self.assertAlmostEqual(float(profile.target_days_exact), 17.361111, places=6)
        self.assertEqual(profile.target_days_conservative, 18)
        self.assertEqual(profile.margin_utilization_pct, Decimal("12.00"))

    def test_volume_strategy_uses_margin_profile(self) -> None:
        strategy = VolumeStrategy(
            SimpleNamespace(
                account_balance_usdt=1500.0,
                position_usdt=20.0,
                volume_margin_per_entry_usdt=180.0,
                leverage=100,
                volume_sides_per_entry=2,
                volume_target_usdt=10_000_000.0,
                trade_target_per_day=16,
                symbols=("BTC/USDT", "ETH/USDT"),
                stop_loss_pct=0.08,
                take_profit_pct=0.12,
                position_hold_minutes=20,
            )
        )

        first = strategy.next_trade()
        second = strategy.next_trade()

        self.assertEqual(first.symbol, "BTC/USDT")
        self.assertEqual(first.usdt_size, 180.0)
        self.assertEqual(first.leverage, 100)
        self.assertIn("volume_builder", first.decision_reason)
        self.assertEqual(first.decision_payload["volume_profile"]["volume_per_entry_usdt"], 36_000.0)
        self.assertEqual(second.symbol, "ETH/USDT")
        self.assertNotEqual(first.direction, second.direction)

    def test_volume_strategy_holds_when_confluence_rejects_entry(self) -> None:
        strategy = VolumeStrategy(
            SimpleNamespace(
                account_balance_usdt=1500.0,
                position_usdt=180.0,
                volume_margin_per_entry_usdt=180.0,
                leverage=100,
                volume_sides_per_entry=2,
                volume_target_usdt=10_000_000.0,
                trade_target_per_day=28,
                symbols=("BTC/USDT", "ETH/USDT"),
                stop_loss_pct=0.08,
                take_profit_pct=0.12,
                position_hold_minutes=60,
                confluence_enabled=True,
                confluence_min_agreement=0.55,
                confluence_min_atr_pct=0.05,
                confluence_min_score=0.05,
                confluence_atr_tp_mult=3.0,
                confluence_atr_sl_mult=1.0,
            )
        )
        context = {
            "factor_snapshots": [],
            "technicals": [
                {
                    "symbol": "BTC/USDT",
                    "last_price": 100_000.0,
                    "atr_14": 20.0,
                }
            ],
            "active_flow_signals": [],
        }

        plan = strategy._build_plan(context)

        self.assertIsNone(plan)

    def test_settings_volume_mode_does_not_require_xai_key(self) -> None:
        settings = Settings(
            _env_file=None,
            BINGX_API_KEY="vst-key",
            BINGX_SECRET_KEY="vst-secret",
            STRATEGY_MODE="volume",
            ACCOUNT_BALANCE_USDT=1500,
            POSITION_USDT=20,
            VOLUME_MARGIN_PER_ENTRY_USDT=180,
            LEVERAGE=100,
            HARD_MAX_LEVERAGE=100,
            VENUE_MAX_LEVERAGE=100,
            TRADE_TARGET_PER_DAY=16,
        )

        self.assertEqual(settings.strategy_mode, "volume")
        self.assertIsNone(settings.xai_api_key)
        self.assertEqual(settings.volume_profile.volume_per_entry_usdt, Decimal("36000"))

    def test_settings_ai_volume_mode_requires_and_uses_ai_key(self) -> None:
        settings = Settings(
            _env_file=None,
            BINGX_API_KEY="vst-key",
            BINGX_SECRET_KEY="vst-secret",
            XAI_API_KEY="xai-key",
            STRATEGY_MODE="ai_volume",
            ACCOUNT_BALANCE_USDT=1500,
            VOLUME_MARGIN_PER_ENTRY_USDT=180,
            LEVERAGE=100,
            HARD_MAX_LEVERAGE=100,
            VENUE_MAX_LEVERAGE=100,
            TRADE_TARGET_PER_DAY=16,
        )

        self.assertEqual(settings.strategy_mode, "ai_volume")
        self.assertEqual(settings.volume_profile.volume_per_entry_usdt, Decimal("36000"))

    def test_settings_rejects_pro_host_as_primary(self) -> None:
        # `.pro` is an internal fallback only; configuring it as the base URL
        # would bypass the mandatory `.com` primary on every request.
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                BINGX_API_KEY="vst-key",
                BINGX_SECRET_KEY="vst-secret",
                XAI_API_KEY="xai-key",
                STRATEGY_MODE="ai_volume",
                BINGX_BASE_URL="https://open-api-vst.bingx.pro",
            )

    def test_settings_accepts_the_com_primary_host(self) -> None:
        settings = Settings(
            _env_file=None,
            BINGX_API_KEY="vst-key",
            BINGX_SECRET_KEY="vst-secret",
            XAI_API_KEY="xai-key",
            STRATEGY_MODE="ai_volume",
            BINGX_BASE_URL="https://open-api-vst.bingx.com",
        )
        self.assertEqual(settings.bingx_base_url, "https://open-api-vst.bingx.com")

    def test_settings_parses_timezone_aware_volume_cycle_start(self) -> None:
        settings = Settings(
            _env_file=None,
            BINGX_API_KEY="vst-key",
            BINGX_SECRET_KEY="vst-secret",
            STRATEGY_MODE="volume",
            VOLUME_CYCLE_STARTED_AT="2026-08-31T19:00:00+07:00",
        )

        self.assertEqual(
            settings.volume_cycle_started_at,
            datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
        )

    def test_settings_rejects_timezone_naive_volume_cycle_start(self) -> None:
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                BINGX_API_KEY="vst-key",
                BINGX_SECRET_KEY="vst-secret",
                STRATEGY_MODE="volume",
                VOLUME_CYCLE_STARTED_AT="2026-08-31T19:00:00",
            )

    def test_settings_rejects_removed_regression_modes(self) -> None:
        base = {
            "_env_file": None,
            "BINGX_API_KEY": "vst-key",
            "BINGX_SECRET_KEY": "vst-secret",
            "XAI_API_KEY": "xai-key",
            "ACCOUNT_BALANCE_USDT": 1500,
            "VOLUME_MARGIN_PER_ENTRY_USDT": 180,
            "LEVERAGE": 100,
            "HARD_MAX_LEVERAGE": 100,
            "VENUE_MAX_LEVERAGE": 100,
        }

        for mode in ("regression_alpha", "regression_alpha_ai"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValidationError):
                    Settings(**base, STRATEGY_MODE=mode)


if __name__ == "__main__":
    unittest.main()
