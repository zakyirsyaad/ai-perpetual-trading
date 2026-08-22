from __future__ import annotations

import asyncio

from bot.ai.brain import GrokBrain
from bot.ai.orchestration import AIOrchestrator
from bot.config import Settings
from bot.core.trade_engine import TradeEngine
from bot.data.providers import ExternalSignalAggregator
from bot.exchange.okx_client import OKXAPIError, OKXFuturesClient
from bot.exchange.leverage import resolve_okx_venue_leverage
from bot.intelligence.market_intelligence import MarketIntelligence
from bot.logger import setup_logging
from bot.risk.risk_manager import RiskManager
from bot.storage.supabase_trade_store import SupabaseTradeStore
from bot.strategy.grok_strategy import GrokStrategy
from bot.strategy.volume_strategy import VolumeStrategy


async def run() -> None:
    settings = Settings()
    logger = setup_logging(settings.log_level)
    logger.info(
        "OKX endpoint: %s (%s)",
        settings.okx_base_url,
        "DEMO" if settings.okx_demo_trading else "LIVE",
    )
    logger.info("Strategy mode: %s", settings.strategy_mode)
    if settings.strategy_mode in {"grok", "ai_volume"}:
        logger.info("AI model: xAI %s", settings.xai_model)
    if not (settings.supabase_url and settings.supabase_service_role_key):
        raise RuntimeError(
            "Supabase primary store is required. Fill SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY; "
            "local trade.db is disabled for this runtime."
        )
    logger.info("Supabase primary store enabled; local trade.db will not be used.")

    client = OKXFuturesClient(
        api_key=settings.okx_api_key,
        secret_key=settings.okx_secret_key,
        passphrase=settings.okx_passphrase,
        base_url=settings.okx_base_url,
        demo_trading=settings.okx_demo_trading,
        margin_mode=settings.okx_margin_mode,
        instrument_type=settings.okx_instrument_type,
        timeout_seconds=settings.request_timeout_seconds,
        insecure_ssl=settings.okx_insecure_ssl,
        debug_http=settings.okx_debug_http,
    )
    resolution = await resolve_okx_venue_leverage(
        client,
        symbols=settings.symbols,
        configured_leverage=settings.leverage,
        margin_mode=settings.okx_margin_mode,
    )
    settings.okx_venue_max_leverage = resolution.effective_max_leverage
    if resolution.was_capped:
        logger.warning(
            "Configured leverage %sx capped to %sx by OKX venue max %sx. source=%s",
            resolution.configured_leverage,
            resolution.resolved_leverage,
            resolution.effective_max_leverage,
            resolution.source,
        )
        settings.leverage = resolution.resolved_leverage
    else:
        logger.info(
            "OKX leverage cap resolved: configured=%sx venue_max=%sx source=%s",
            resolution.configured_leverage,
            resolution.effective_max_leverage,
            resolution.source,
        )
    if settings.strategy_mode in {"volume", "ai_volume"}:
        logger.info("Volume profile: %s", settings.volume_profile.as_log_fields())

    store = SupabaseTradeStore(settings, logger)
    risk = RiskManager(
        store,
        max_stop_losses_per_day=settings.max_stop_loss_per_day,
        max_daily_drawdown_usdt=settings.max_daily_drawdown_usdt,
        max_consecutive_losses=settings.max_consecutive_losses,
        trading_timezone=settings.trading_timezone,
    )
    signal_aggregator = ExternalSignalAggregator.from_settings(settings, logger)
    intelligence = MarketIntelligence(settings, client, signal_aggregator, logger)
    if settings.strategy_mode == "volume":
        strategy = VolumeStrategy(settings, intelligence)
    else:
        brain = GrokBrain(settings, logger)
        orchestrator = AIOrchestrator(settings, brain, intelligence, store, logger, supabase_journal=None)
        strategy = GrokStrategy(orchestrator)
    engine = TradeEngine(settings, logger, client, strategy, risk, store, supabase_journal=None)

    try:
        await engine.run_forever()
    except (OKXAPIError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None
    finally:
        strategy_close = getattr(strategy, "close", None)
        if strategy_close is not None:
            await strategy_close()
        store_close = getattr(store, "close", None)
        if store_close is not None:
            await store_close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(run())
