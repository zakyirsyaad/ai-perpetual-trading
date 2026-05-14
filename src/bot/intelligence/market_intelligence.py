from __future__ import annotations

import logging
from statistics import mean
from typing import Any

from bot.config import Settings
from bot.data.market_signals import ExternalMarketSignals, MarketContext, TechnicalFeatures
from bot.data.providers import ExternalSignalAggregator
from bot.exchange.okx_client import OKXFuturesClient
from bot.strategy.indicators import atr, ema, macd, pct_change, rsi


class MarketIntelligence:
    """Layer 4: convert raw market data and external signals into AI-ready context."""

    def __init__(
        self,
        settings: Settings,
        client: OKXFuturesClient,
        external_signals: ExternalSignalAggregator,
        logger: logging.Logger,
    ) -> None:
        self._settings = settings
        self._client = client
        self._external_signals = external_signals
        self._logger = logger

    async def build_context(self, symbols: tuple[str, ...] | None = None) -> MarketContext:
        pairs = symbols or self._settings.symbols
        technicals: list[TechnicalFeatures] = []
        for symbol in pairs:
            features = await self._build_symbol_features(symbol)
            if features is not None:
                technicals.append(features)

        external = await self._external_signals.fetch()
        intelligence = self._summarize_market(technicals, external)
        return MarketContext(
            technicals=technicals,
            external_signals=external,
            intelligence=intelligence,
        )

    async def close(self) -> None:
        await self._external_signals.close()

    async def _build_symbol_features(self, symbol: str) -> TechnicalFeatures | None:
        klines = await self._client.get_klines(
            symbol=symbol,
            interval=self._settings.ai_kline_interval,
            limit=self._settings.ai_kline_limit,
        )
        closes = [float(row["close"]) for row in klines]
        highs = [float(row["high"]) for row in klines]
        lows = [float(row["low"]) for row in klines]
        volumes = [float(row.get("volume", 0.0)) for row in klines]
        if not closes:
            self._logger.warning("No OKX candles returned for %s.", symbol)
            return None

        macd_line, macd_signal, macd_hist = macd(closes, 12, 26, 9)
        try:
            funding_rate = await self._client.get_funding_rate(symbol)
        except Exception as exc:
            self._logger.debug("Funding rate fetch failed for %s: %s", symbol, exc)
            funding_rate = 0.0
        try:
            open_interest = await self._client.get_open_interest(symbol)
        except Exception as exc:
            self._logger.debug("Open interest fetch failed for %s: %s", symbol, exc)
            open_interest = 0.0

        return TechnicalFeatures(
            symbol=symbol,
            last_price=closes[-1],
            ema_fast=ema(closes, 9),
            ema_slow=ema(closes, 21),
            ema_21=ema(closes, 21),
            rsi_14=rsi(closes, 14),
            atr_14=atr(highs, lows, closes, 14),
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_hist=macd_hist,
            change_pct_1=pct_change(closes, 1),
            change_pct_5=pct_change(closes, 5),
            volume=volumes[-1] if volumes else 0.0,
            volume_change_pct=pct_change(volumes, 5) if len(volumes) > 5 else 0.0,
            funding_rate=funding_rate,
            open_interest=open_interest,
        )

    def _summarize_market(
        self,
        technicals: list[TechnicalFeatures],
        external: ExternalMarketSignals,
    ) -> dict[str, Any]:
        if not technicals:
            return {
                "regime": "unknown",
                "risk_flags": ["missing_okx_market_data"],
                "symbol_scores": {},
            }

        symbol_scores: dict[str, Any] = {}
        risk_flags: list[str] = []
        for item in technicals:
            trend_score = 0.0
            if item.ema_fast > item.ema_slow:
                trend_score += 1.0
            elif item.ema_fast < item.ema_slow:
                trend_score -= 1.0
            trend_score += max(min(item.macd_hist, 1.0), -1.0)
            momentum_score = (item.rsi_14 - 50.0) / 50.0
            volume_score = max(min(item.volume_change_pct / 100.0, 1.0), -1.0)
            funding_penalty = -abs(item.funding_rate) * 100.0
            composite = trend_score + momentum_score + (volume_score * 0.5) + funding_penalty

            if item.atr_14 > 0 and item.last_price > 0:
                atr_pct = (item.atr_14 / item.last_price) * 100
                if atr_pct > self._settings.stop_loss_pct * 3:
                    risk_flags.append(f"{item.symbol}:high_atr_pct={atr_pct:.3f}")

            symbol_scores[item.symbol] = {
                "composite_score": composite,
                "trend_bias": "bullish" if composite > 0.35 else "bearish" if composite < -0.35 else "mixed",
                "rsi": item.rsi_14,
                "funding_rate": item.funding_rate,
                "open_interest": item.open_interest,
            }

        avg_abs_change = mean(abs(item.change_pct_5) for item in technicals)
        regime = "volatile" if avg_abs_change >= 0.8 else "calm" if avg_abs_change < 0.2 else "normal"
        if not external.coinmarketcap:
            risk_flags.append("coinmarketcap_unavailable")
        if self._settings.dune_query_ids and not external.dune:
            risk_flags.append("dune_unavailable")
        if self._settings.tradingview_signal_url and not external.tradingview:
            risk_flags.append("tradingview_signal_unavailable")

        return {
            "regime": regime,
            "risk_flags": risk_flags,
            "symbol_scores": symbol_scores,
        }
