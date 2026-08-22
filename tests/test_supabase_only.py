from __future__ import annotations

from datetime import date
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.config import Settings
from bot.storage.supabase_trade_store import SupabaseTradeStore
from report_arena import build_parser


ROOT = Path(__file__).resolve().parents[1]


class SupabaseOnlyTests(unittest.TestCase):
    def test_runtime_sources_do_not_depend_on_sqlite(self) -> None:
        forbidden = ("sqlite3", "aiosqlite", "bot_data/trades.db", "DB_PATH")
        paths = [
            *ROOT.joinpath("src").rglob("*.py"),
            ROOT / "requirements.txt",
            ROOT / "pyproject.toml",
            ROOT / "README.md",
        ]
        offenders: list[str] = []
        for path in paths:
            if path.name == "tradingview_relay.py":
                continue
            text = path.read_text(encoding="utf-8")
            for needle in forbidden:
                if needle in text:
                    offenders.append(f"{path.relative_to(ROOT)} contains {needle}")

        self.assertEqual(offenders, [])

    def test_settings_has_no_database_path(self) -> None:
        self.assertFalse(hasattr(Settings, "database_path"))
        self.assertNotIn("db_path", Settings.model_fields)

    def test_legacy_runtime_modules_are_removed(self) -> None:
        removed_paths = [
            ROOT / "src" / "bot" / "core" / "paper_trade_engine.py",
            ROOT / "src" / "bot" / "strategy" / "openai_strategy.py",
            ROOT / "src" / "bot" / "perps",
            ROOT / "src" / "bot" / "backtest",
            ROOT / "config" / "perps.safe-defaults.json",
            ROOT / "docs" / "perps.md",
        ]
        # BINGX_* is no longer legacy: BingX VST is the runtime venue. The OKX
        # runtime configuration is what must be gone from Settings.
        legacy_fields = {
            "okx_api_key",
            "okx_secret_key",
            "okx_passphrase",
            "okx_base_url",
            "okx_demo_trading",
            "okx_margin_mode",
            "okx_position_mode",
            "okx_instrument_type",
            "okx_max_spread_bps",
            "okx_hard_max_leverage",
            "okx_venue_max_leverage",
            "ai_pre_trade_check",
            "openai_api_key",
            "openai_model",
            "perp_exchange_api_name",
            "perp_max_spread_bps",
            "perp_hard_max_leverage",
        }

        self.assertEqual([path for path in removed_paths if path.exists()], [])
        self.assertTrue(legacy_fields.isdisjoint(Settings.model_fields))
        self.assertIn("bingx_api_key", Settings.model_fields)
        self.assertIn("max_spread_bps", Settings.model_fields)
        self.assertIn("hard_max_leverage", Settings.model_fields)

    def test_arena_report_uses_supabase_arguments_not_db(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()

        self.assertNotIn("--db", help_text)
        self.assertIn("--supabase-url", help_text)
        self.assertIn("--service-role-key", help_text)

    def test_daily_trade_window_uses_configured_trading_timezone(self) -> None:
        store = object.__new__(SupabaseTradeStore)
        store._trades_table = "trades"
        store._trading_tz = ZoneInfo("Asia/Jakarta")
        captured: dict[str, object] = {}

        async def fake_get_rows(table, params):
            captured["table"] = table
            captured["params"] = params
            return []

        store._get_rows = fake_get_rows

        async def run() -> None:
            await store._closed_trade_rows_for_day(date(2026, 6, 23), "id")

        import asyncio

        asyncio.run(run())

        self.assertEqual(captured["table"], "trades")
        self.assertIn(("closed_at", "gte.2026-06-22T17:00:00+00:00"), captured["params"])
        self.assertIn(("closed_at", "lt.2026-06-23T17:00:00+00:00"), captured["params"])


if __name__ == "__main__":
    unittest.main()
