"""Composition for the dashboard feed: meta, hub, server and poller as one unit.

`main.py` builds this once and calls `start()` and `stop()`. Both are written so that
nothing about the feed can stop the bot from trading: a server that cannot bind is
logged and skipped, and a crashed poller leaves the server serving what it has.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Protocol

from bot.config import Settings
from bot.exchange.contracts import FuturesExchangeProtocol
from bot.telemetry.book_ticker import BookTickerFeed
from bot.telemetry.bus import TelemetryBus
from bot.telemetry.hub import TelemetryHub
from bot.telemetry.observer import NULL_OBSERVER, EngineObserver, EngineTelemetry
from bot.telemetry.poller import MarketDataPoller, coin_of, pair_of
from bot.telemetry.server import TelemetryServer
from bot.telemetry.wire import Meta, SleeveMeta

_AI_STRATEGIES = {"grok", "ai_volume"}


def build_meta(settings: Settings, *, now_ms: int | None = None) -> Meta:
    """Dashboard header data. Carries no wallet and no credential of any kind."""
    sleeves = [SleeveMeta(coin=coin_of(s), pair=pair_of(s), label=coin_of(s), wallet=None) for s in settings.symbols]
    first = sleeves[0]
    return Meta(
        model=settings.xai_model if settings.strategy_mode in _AI_STRATEGIES else "volume",
        wallet=None,
        dry_run=False,
        market=first.pair,
        started_at=now_ms if now_ms is not None else int(time.time() * 1000),
        venue="bingx",
        coin=first.coin,
        pair=first.pair,
        explorer_tx="",
        tick_ms=settings.entry_interval_minutes * 60 * 1000,
        sleeves=sleeves,
    )


class _Runs(Protocol):
    async def run(self) -> None: ...


class TelemetryRuntime:
    def __init__(
        self,
        hub: TelemetryHub,
        server: TelemetryServer,
        poller: MarketDataPoller,
        logger: logging.Logger,
        observer: EngineObserver = NULL_OBSERVER,
        stream: _Runs | None = None,
    ) -> None:
        self._hub = hub
        self._server = server
        self._poller = poller
        self._logger = logger
        self._observer = observer
        self._stream = stream
        self._poller_task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._running = False
        self._poller_crashed = False
        self._stream_crashed = False

    @property
    def stream(self) -> _Runs | None:
        """The optional websocket price feed; None while it is switched off."""
        return self._stream

    @property
    def stream_stopped(self) -> bool:
        return self._stream_crashed

    @property
    def hub(self) -> TelemetryHub:
        return self._hub

    @property
    def observer(self) -> EngineObserver:
        """The hooks `TradeEngine` calls. Handing this over is the only coupling to the engine."""
        return self._observer

    @property
    def running(self) -> bool:
        return self._running

    @property
    def poller_stopped(self) -> bool:
        return self._poller_crashed

    async def start(self) -> bool:
        """Start the server and poller. Returns False, never raises, if the server cannot start."""
        if self._running:
            return True
        try:
            await self._server.start()
        except Exception:
            self._logger.warning(
                "Telemetry server failed to start; trading continues without the dashboard feed.",
                exc_info=True,
            )
            return False
        self._poller_task = asyncio.create_task(self._supervise_poller(), name="telemetry-poller")
        if self._stream is not None:
            self._stream_task = asyncio.create_task(self._supervise_stream(), name="telemetry-price-stream")
        self._running = True
        self._logger.info("Telemetry dashboard feed listening on loopback port %s.", self._server.bound_port)
        return True

    async def stop(self) -> None:
        """Stop the poller and server. Never raises: it runs in the bot's shutdown path."""
        self._running = False
        tasks = [t for t in (self._poller_task, self._stream_task) if t is not None]
        self._poller_task = self._stream_task = None
        try:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._server.stop()
        except Exception:
            self._logger.warning("Telemetry shutdown was not clean.", exc_info=True)

    async def _supervise_poller(self) -> None:
        try:
            await self._poller.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._poller_crashed = True
            self._logger.error(
                "Telemetry poller stopped unexpectedly; the dashboard price feed is frozen.",
                exc_info=True,
            )

    async def _supervise_stream(self) -> None:
        assert self._stream is not None
        try:
            await self._stream.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._stream_crashed = True
            self._logger.error(
                "Telemetry price stream stopped unexpectedly; the dashboard falls back to polling.",
                exc_info=True,
            )


def build_telemetry(
    settings: Settings,
    client: FuturesExchangeProtocol,
    logger: logging.Logger,
) -> TelemetryRuntime | None:
    """Build the dashboard feed, or None when it is disabled or cannot be built."""
    if not settings.telemetry_enabled:
        return None
    try:
        hub = TelemetryHub(build_meta(settings), TelemetryBus())
        server = TelemetryServer(
            hub,
            host=settings.telemetry_host,
            port=settings.telemetry_port,
            allowed_origin=settings.telemetry_allowed_origin,
        )
        poller = MarketDataPoller(
            hub,
            client,
            settings.symbols,
            interval_seconds=settings.telemetry_price_interval_seconds,
        )
        stream = (
            BookTickerFeed([pair_of(symbol) for symbol in settings.symbols], poller.on_stream_tick)
            if settings.telemetry_price_stream
            else None
        )
        observer = EngineTelemetry(
            hub,
            settings.symbols,
            account_balance_usdt=settings.account_balance_usdt,
        )
    except Exception:
        logger.warning("Telemetry could not be built; trading continues without it.", exc_info=True)
        return None
    return TelemetryRuntime(hub, server, poller, logger, observer, stream)
