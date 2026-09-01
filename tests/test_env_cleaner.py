from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from clean_env import clean_env_file


class EnvCleanerTests(unittest.TestCase):
    def test_preserves_allowed_secrets_removes_legacy_keys_and_adds_live_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            example = root / ".env.example"
            env = root / ".env"
            example.write_text(
                "\n".join(
                    [
                        "# BingX Perpetual Futures VST",
                        "BINGX_API_KEY=your_bingx_vst_api_key",
                        "BINGX_SECRET_KEY=your_bingx_vst_secret_key",
                        "BINGX_ENV=live",
                        "BINGX_BASE_URL=https://open-api.bingx.com",
                        "",
                        "# OpenAI-compatible AI brain",
                        "XAI_API_KEY=your_xai_api_key",
                        "XAI_BASE_URL=https://api.x.ai/v1",
                        "XAI_MODEL=grok-4.3",
                        "XAI_API_STYLE=responses",
                        "",
                        "# Required Supabase primary store",
                        "SUPABASE_URL=",
                        "SUPABASE_SERVICE_ROLE_KEY=",
                        "",
                        "# Bot behavior",
                        "STRATEGY_MODE=ai_volume",
                        "POSITION_USDT=180",
                        "LEVERAGE=100",
                        "VOLUME_EXECUTION_MODE=maker_only",
                        "VOLUME_BREAKEVEN_EXIT_ENABLED=true",
                        "VOLUME_ATTEMPT_TARGET_PER_DAY=34",
                        "VOLUME_MIN_FILLED_ENTRIES_PER_DAY=28",
                        "",
                        "# Risk and timing",
                        "ENTRY_INTERVAL_MINUTES=15",
                        "POSITION_HOLD_MINUTES=20",
                        "EXECUTION_ENTRY_ORDER_TYPE=post_only",
                        "EXECUTION_POST_ONLY_FALLBACK_TO_MARKET=false",
                        "MAKER_ENTRY_TTL_SECONDS=3",
                        "MAKER_EXIT_TTL_SECONDS=3",
                        "MAKER_MAX_REPRICES=5",
                        "MAKER_ALLOW_TAKER_EMERGENCY_EXIT=true",
                        "AI_KLINE_INTERVAL=1m",
                        "CONFLUENCE_ENABLED=false",
                        "CONFLUENCE_MIN_AGREEMENT=0.2",
                        "CONFLUENCE_MIN_ATR_PCT=0.01",
                        "CONFLUENCE_MIN_SCORE=0.01",
                        "CONFLUENCE_ATR_TP_MULT=2.5",
                        "CONFLUENCE_ATR_SL_MULT=1.0",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            env.write_text(
                "\n".join(
                    [
                        "BINGX_API_KEY=real-bingx-key",
                        "BINGX_SECRET_KEY=real-bingx-secret",
                        "XAI_API_KEY=real-xai-key",
                        "SUPABASE_URL=https://example.supabase.co",
                        "SUPABASE_SERVICE_ROLE_KEY=real-supabase-key",
                        "LEVERAGE=150",
                        "EXECUTION_ENTRY_ORDER_TYPE=market",
                        "OKX_API_KEY=legacy-okx",
                        "OKX_PASSPHRASE=legacy-okx-passphrase",
                        "AI_PRE_TRADE_CHECK=true",
                        "PERP_ENABLED=true",
                        "OPENAI_API_KEY=legacy-openai",
                        "USE_TESTNET=true",
                        "ENABLE_LIVE_TRADING=true",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = clean_env_file(env_path=env, example_path=example)

            cleaned = env.read_text(encoding="utf-8")
            self.assertTrue(result.backup_path.exists())
            self.assertIn("BINGX_API_KEY=real-bingx-key", cleaned)
            self.assertIn("BINGX_SECRET_KEY=real-bingx-secret", cleaned)
            # VST-only defaults are re-applied even when .env drifted to live values.
            self.assertIn("BINGX_ENV=vst", cleaned)
            self.assertIn("BINGX_BASE_URL=https://open-api-vst.bingx.com", cleaned)
            self.assertIn("XAI_API_KEY=real-xai-key", cleaned)
            self.assertIn("XAI_BASE_URL=https://api.freemodel.dev/v1", cleaned)
            self.assertIn("XAI_MODEL=gpt-5.4", cleaned)
            self.assertIn("XAI_API_STYLE=chat", cleaned)
            self.assertIn("SUPABASE_URL=https://example.supabase.co", cleaned)
            self.assertIn("SUPABASE_SERVICE_ROLE_KEY=real-supabase-key", cleaned)
            self.assertIn("STRATEGY_MODE=ai_volume", cleaned)
            self.assertIn("LEVERAGE=100", cleaned)
            self.assertIn("VOLUME_EXECUTION_MODE=market", cleaned)
            self.assertIn("VOLUME_BREAKEVEN_EXIT_ENABLED=false", cleaned)
            self.assertIn("VOLUME_ATTEMPT_TARGET_PER_DAY=34", cleaned)
            self.assertIn("VOLUME_MIN_FILLED_ENTRIES_PER_DAY=28", cleaned)
            self.assertIn("POSITION_HOLD_MINUTES=60", cleaned)
            self.assertIn("ENTRY_INTERVAL_MINUTES=3", cleaned)
            self.assertIn("AI_KLINE_INTERVAL=3m", cleaned)
            self.assertIn("CONFLUENCE_ENABLED=true", cleaned)
            self.assertIn("CONFLUENCE_MIN_AGREEMENT=0.55", cleaned)
            self.assertIn("CONFLUENCE_MIN_ATR_PCT=0.05", cleaned)
            self.assertIn("CONFLUENCE_MIN_SCORE=0.05", cleaned)
            self.assertIn("CONFLUENCE_ATR_TP_MULT=3.0", cleaned)
            self.assertIn("CONFLUENCE_ATR_SL_MULT=1.0", cleaned)
            self.assertIn("EXECUTION_ENTRY_ORDER_TYPE=market", cleaned)
            self.assertIn("EXECUTION_POST_ONLY_FALLBACK_TO_MARKET=false", cleaned)
            self.assertIn("MAKER_ALLOW_TAKER_EMERGENCY_EXIT=true", cleaned)
            self.assertNotIn("OKX_API_KEY", cleaned)
            self.assertNotIn("OKX_PASSPHRASE", cleaned)
            self.assertNotIn("AI_PRE_TRADE_CHECK", cleaned)
            self.assertNotIn("PERP_ENABLED", cleaned)
            self.assertNotIn("OPENAI_API_KEY", cleaned)
            self.assertNotIn("USE_TESTNET", cleaned)
            self.assertNotIn("ENABLE_LIVE_TRADING", cleaned)
            self.assertLess(
                cleaned.index("# BingX Perpetual Futures VST"),
                cleaned.index("# OpenAI-compatible AI brain"),
            )
            self.assertLess(
                cleaned.index("# OpenAI-compatible AI brain"),
                cleaned.index("# Required Supabase primary store"),
            )


if __name__ == "__main__":
    unittest.main()
