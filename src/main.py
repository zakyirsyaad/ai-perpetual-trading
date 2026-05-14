from __future__ import annotations

import asyncio

from bot.ai.brain import GrokBrain
from bot.ai.orchestration import AIOrchestrator
from bot.config import Settings
from bot.core.trade_engine import TradeEngine
<<<<<<< HEAD
from bot.data.providers import ExternalSignalAggregator
from bot.exchange.okx_client import OKXFuturesClient
from bot.intelligence.market_intelligence import MarketIntelligence
=======
from bot.exchange.bingx_client import BingXAPIError, BingXFuturesClient
>>>>>>> c78be0d66ab75cb6b8447245488aab66339bae13
from bot.logger import setup_logging
from bot.risk.risk_manager import RiskManager
from bot.storage.supabase_journal import SupabaseJournal
from bot.storage.trade_store import TradeStore
from bot.strategy.grok_strategy import GrokStrategy


async def run() -> None:
    settings = Settings()
    logger = setup_logging(settings.log_level)
    logger.info(
<<<<<<< HEAD
        "OKX endpoint: %s (%s)",
        settings.okx_base_url,
        "DEMO" if settings.okx_demo_trading else "LIVE",
    )
    logger.info("AI model: xAI %s", settings.xai_model)

    client = OKXFuturesClient(
        api_key=settings.okx_api_key,
        secret_key=settings.okx_secret_key,
        passphrase=settings.okx_passphrase,
        base_url=settings.okx_base_url,
        demo_trading=settings.okx_demo_trading,
        margin_mode=settings.okx_margin_mode,
        instrument_type=settings.okx_instrument_type,
=======
        "BingX endpoint: %s (%s)",
        settings.bingx_base_url,
        "DEMO/VST" if settings.bingx_is_demo_endpoint else "LIVE",
    )
    if settings.enable_live_trading and (not settings.bingx_api_key or not settings.bingx_secret_key):
        raise ValueError("BINGX_API_KEY and BINGX_SECRET_KEY are required when live trading is enabled.")
    if settings.enable_live_trading and settings.bingx_is_demo_endpoint:
        logger.warning(
            "ENABLE_LIVE_TRADING=true while using the BingX DEMO/VST endpoint. "
            "Only VST API keys work there; live account keys will be rejected."
        )

    client = BingXFuturesClient(
        api_key=settings.bingx_api_key or "",
        secret_key=settings.bingx_secret_key or "",
        base_url=settings.bingx_base_url,
>>>>>>> c78be0d66ab75cb6b8447245488aab66339bae13
        timeout_seconds=settings.request_timeout_seconds,
        insecure_ssl=settings.okx_insecure_ssl,
        debug_http=settings.okx_debug_http,
    )
    store = TradeStore(settings.database_path)
    risk = RiskManager(
        store,
        max_stop_losses_per_day=settings.max_stop_loss_per_day,
        max_daily_drawdown_usdt=settings.max_daily_drawdown_usdt,
        max_consecutive_losses=settings.max_consecutive_losses,
    )
<<<<<<< HEAD
    signal_aggregator = ExternalSignalAggregator.from_settings(settings, logger)
    intelligence = MarketIntelligence(settings, client, signal_aggregator, logger)
    brain = GrokBrain(settings, logger)
    supabase_journal = SupabaseJournal(settings, logger)
    if supabase_journal.enabled:
        logger.info("Supabase mirror enabled.")
    orchestrator = AIOrchestrator(settings, brain, intelligence, store, logger, supabase_journal)
    strategy = GrokStrategy(orchestrator)
    engine = TradeEngine(settings, logger, client, strategy, risk, store, supabase_journal)
=======
    if settings.strategy_mode == "ai":
        logger.info("Strategy mode: AI (xAI Grok)")
        strategy = OpenAIStrategy(settings, client, logger, store, risk)
    else:
        logger.info("Strategy mode: RULE (deterministic)")
        strategy = VolumeStrategy(settings)
    if settings.enable_live_trading:
        if settings.bingx_is_demo_endpoint:
            logger.info("Mode: DEMO/VST trading")
        else:
            logger.info("Mode: LIVE trading")
        engine = TradeEngine(settings, logger, client, strategy, risk, store)
    else:
        logger.info("Mode: PAPER trading")
        engine = PaperTradeEngine(settings, logger, client, strategy, risk, store)
>>>>>>> c78be0d66ab75cb6b8447245488aab66339bae13

    try:
        await engine.run_forever()
    except (BingXAPIError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None
    finally:
        await strategy.close()
        await supabase_journal.close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(run())
