from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main as main_module
from bot.exchange.leverage import LeverageResolution


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        log_level="INFO",
        bingx_base_url="https://open-api-vst.bingx.com",
        bingx_env="vst",
        bingx_api_key="k",
        bingx_secret_key="s",
        bingx_margin_mode="isolated",
        bingx_position_mode="hedge",
        bingx_recv_window_ms=5000,
        bingx_source_key="src",
        bingx_route_min_interval_seconds=0.0,
        bingx_market_cache_seconds=0.0,
        bingx_insecure_ssl=False,
        bingx_debug_http=False,
        strategy_mode="ai_volume",
        xai_model="deepseek-v4-flash",
        supabase_url="https://x.supabase.co",
        supabase_service_role_key="key",
        symbols=("BTC/USDT",),
        leverage=100,
        request_timeout_seconds=5,
        max_stop_loss_per_day=2,
        max_daily_drawdown_usdt=100.0,
        max_consecutive_losses=3,
        trading_timezone="Asia/Jakarta",
        volume_profile=SimpleNamespace(as_log_fields=lambda: {}),
        venue_max_leverage=100,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Recorder:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.venue = "bingx"
        self.sink = None

    def set_metrics_sink(self, sink) -> None:
        self.sink = sink

    async def close(self) -> None:
        return None

    async def save_ai_call_metrics(self, metrics) -> None:
        return None


def _resolution(was_capped: bool = False, resolved: int = 100) -> LeverageResolution:
    return LeverageResolution(
        symbols=("BTC/USDT",),
        configured_leverage=100,
        effective_max_leverage=100,
        resolved_leverage=resolved,
        symbol_max_leverage={"BTC/USDT": 100},
        source="bingx_position_tiers",
        was_capped=was_capped,
        reason="ok",
    )


class _Engine:
    instances: list["_Engine"] = []

    def __init__(self, *args, **kwargs) -> None:
        _Engine.instances.append(self)
        self.args = args

    async def run_forever(self) -> None:
        return None


def _patched(settings, resolution=None):
    async def resolve(*args, **kwargs):
        return resolution or _resolution()

    return [
        patch.object(main_module, "Settings", lambda: settings),
        patch.object(main_module, "BingXFuturesClient", _Recorder),
        patch.object(main_module, "resolve_venue_leverage", resolve),
        patch.object(main_module, "SupabaseTradeStore", _Recorder),
        patch.object(main_module, "RiskManager", _Recorder),
        patch.object(main_module, "ExternalSignalAggregator", SimpleNamespace(from_settings=lambda s, l: _Recorder())),
        patch.object(main_module, "MarketIntelligence", _Recorder),
        patch.object(main_module, "GrokBrain", _Recorder),
        patch.object(main_module, "AIOrchestrator", _Recorder),
        patch.object(main_module, "GrokStrategy", _Recorder),
        patch.object(main_module, "VolumeStrategy", _Recorder),
        patch.object(main_module, "TradeEngine", _Engine),
    ]


class CompositionRootTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, settings, resolution=None) -> None:
        _Engine.instances.clear()
        patches = _patched(settings, resolution)
        for item in patches:
            item.start()
        try:
            await main_module.run()
        finally:
            for item in patches:
                item.stop()

    async def test_ai_volume_mode_wires_the_brain_and_engine(self) -> None:
        await self._run(_settings())
        self.assertEqual(len(_Engine.instances), 1)

    async def test_volume_mode_skips_the_ai_brain(self) -> None:
        await self._run(_settings(strategy_mode="volume"))
        self.assertEqual(len(_Engine.instances), 1)

    async def test_missing_supabase_credentials_fail_fast(self) -> None:
        with self.assertRaises(RuntimeError):
            await self._run(_settings(supabase_url=None))

    async def test_capped_leverage_is_applied_to_settings(self) -> None:
        settings = _settings()
        await self._run(settings, _resolution(was_capped=True, resolved=50))
        self.assertEqual(settings.leverage, 50)

    async def test_venue_max_leverage_is_recorded(self) -> None:
        settings = _settings()
        await self._run(settings)
        self.assertEqual(settings.venue_max_leverage, 100)


if __name__ == "__main__":
    unittest.main()
