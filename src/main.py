from __future__ import annotations

import asyncio

from bot.ai.brain import GrokBrain
from bot.ai.orchestration import AIOrchestrator
from bot.config import Settings
from bot.core.trade_engine import TradeEngine
from bot.data.providers import ExternalSignalAggregator
from bot.exchange.okx_client import OKXAPIError, OKXFuturesClient
from bot.intelligence.market_intelligence import MarketIntelligence
from bot.logger import setup_logging
from bot.risk.risk_manager import RiskManager
from bot.storage.supabase_journal import SupabaseJournal
from bot.storage.trade_store import TradeStore
from bot.strategy.grok_strategy import GrokStrategy


async def run() -> None:
    settings = Settings()
    logger = setup_logging(settings.log_level)
    logger.info(
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
    signal_aggregator = ExternalSignalAggregator.from_settings(settings, logger)
    intelligence = MarketIntelligence(settings, client, signal_aggregator, logger)
    brain = GrokBrain(settings, logger)
    supabase_journal = SupabaseJournal(settings, logger)
    if supabase_journal.enabled:
        logger.info("Supabase mirror enabled.")
    orchestrator = AIOrchestrator(settings, brain, intelligence, store, logger, supabase_journal)
    strategy = GrokStrategy(orchestrator)
    engine = TradeEngine(settings, logger, client, strategy, risk, store, supabase_journal)

    try:
        await engine.run_forever()
    except (OKXAPIError, RuntimeError, ValueError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None
    finally:
        await strategy.close()
        await supabase_journal.close()
        await client.close()


if __name__ == "__main__":
    asyncio.run(run())
