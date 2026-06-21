from __future__ import annotations

import unittest
from pathlib import Path

from bot.config import Settings
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
            ROOT / "src" / "bot" / "exchange" / "bingx_client.py",
            ROOT / "src" / "bot" / "core" / "paper_trade_engine.py",
            ROOT / "src" / "bot" / "strategy" / "openai_strategy.py",
            ROOT / "src" / "bot" / "perps",
            ROOT / "src" / "bot" / "backtest",
            ROOT / "config" / "perps.safe-defaults.json",
            ROOT / "docs" / "perps.md",
        ]
        legacy_fields = {
            "bingx_api_key",
            "bingx_secret_key",
            "bingx_base_url",
            "openai_api_key",
            "openai_model",
            "perp_exchange_api_name",
            "perp_max_spread_bps",
            "perp_hard_max_leverage",
        }

        self.assertEqual([path for path in removed_paths if path.exists()], [])
        self.assertTrue(legacy_fields.isdisjoint(Settings.model_fields))

    def test_arena_report_uses_supabase_arguments_not_db(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()

        self.assertNotIn("--db", help_text)
        self.assertIn("--supabase-url", help_text)
        self.assertIn("--service-role-key", help_text)


if __name__ == "__main__":
    unittest.main()
