from __future__ import annotations

import logging
from statistics import mean
from typing import Any

from bot.arena.factors import FactorRegistry
from bot.arena.flow_signals import FactorDistributionTracker, MarketFlowSignalDetector, SignalRuleConfig
from bot.config import Settings
from bot.data.market_signals import ExternalMarketSignals, MarketContext, TechnicalFeatures
from bot.data.providers import ExternalSignalAggregator
from bot.exchange.contracts import FuturesExchangeProtocol, MaintenanceMarginTier, MaintenanceMarginTierProvider
from bot.exchange.liquidation import estimate_isolated_liquidation_constraint, select_maintenance_margin_tier
from bot.strategy.indicators import atr, ema, macd, pct_change, rsi

LEAN_EMA_PERIOD = 21
LEAN_ATR_PERIOD = 14
LEAN_RSI_PERIOD = 14
LEAN_SLOPE_BARS = 5
LEAN_SHORT_CHANGE_BARS = 5
LEAN_LONG_CHANGE_BARS = 20
# 21 closes make a real EMA21, and the slope compares it with the EMA21 of the series
# five bars earlier. The three ATR-scaled values share this warm-up.
LEAN_MIN_CLOSES = LEAN_EMA_PERIOD + LEAN_SLOPE_BARS


def _change_pct_or_none(closes: list[float], lookback: int) -> float | None:
    """`pct_change`, but None instead of its 0.0 fallback when the history is missing."""
    if len(closes) <= lookback or closes[-1 - lookback] == 0:
        return None
    return pct_change(closes, lookback)


def lean_features_from_candles(
    *,
    bar_interval: str,
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> dict[str, Any]:
    """The lean entry payload's numbers, from closed candles (oldest first).

    Every value is None when its input is missing, never a placeholder. The ATR-scaled
    values (`stretch_atr`, `ema21_slope_atr_5_bars`, `atr_pct`) need 26 closes and a
    non-zero ATR14. `rsi_14` and the two changes need only their own look-back
    (15, 6 and 21 closes), because `rsi` and `pct_change` would otherwise answer 50.0
    or 0.0 for a series too short to measure.
    """
    lean: dict[str, Any] = {
        "bar_interval": bar_interval,
        "stretch_atr": None,
        "ema21_slope_atr_5_bars": None,
        "rsi_14": rsi(closes, LEAN_RSI_PERIOD) if len(closes) > LEAN_RSI_PERIOD else None,
        "change_pct_5_bars": _change_pct_or_none(closes, LEAN_SHORT_CHANGE_BARS),
        "change_pct_20_bars": _change_pct_or_none(closes, LEAN_LONG_CHANGE_BARS),
        "atr_pct": None,
    }
    if len(closes) < LEAN_MIN_CLOSES:
        return lean
    atr_value = atr(highs, lows, closes, LEAN_ATR_PERIOD)
    last = closes[-1]
    if atr_value <= 0 or last <= 0:
        return lean
    ema_now = ema(closes, LEAN_EMA_PERIOD)
    ema_before = ema(closes[:-LEAN_SLOPE_BARS], LEAN_EMA_PERIOD)
    lean["stretch_atr"] = (last - ema_now) / atr_value
    lean["ema21_slope_atr_5_bars"] = (ema_now - ema_before) / atr_value
    lean["atr_pct"] = 100.0 * atr_value / last
    return lean


class MarketIntelligence:
    """Layer 4: convert raw market data and external signals into AI-ready context.

    Depends on `FuturesExchangeProtocol`, never on a specific venue client.
    """

    def __init__(
        self,
        settings: Settings,
        client: FuturesExchangeProtocol,
        external_signals: ExternalSignalAggregator,
        logger: logging.Logger,
    ) -> None:
        self._settings = settings
        self._client = client
        self._external_signals = external_signals
        self._logger = logger
        self._factor_registry = FactorRegistry()
        self._factor_distribution_tracker = FactorDistributionTracker()
        self._flow_detector = MarketFlowSignalDetector(
            self._signal_config_from_settings(settings),
            distribution_tracker=self._factor_distribution_tracker,
        )
        self._last_open_interest_by_symbol: dict[str, float] = {}
        self._tradingview_ever_available = False

    async def build_context(self, symbols: tuple[str, ...] | None = None) -> MarketContext:
        pairs = symbols or self._settings.symbols
        technicals: list[TechnicalFeatures] = []
        lean_features: dict[str, dict[str, Any]] = {}
        for symbol in pairs:
            built = await self._build_symbol_features(symbol)
            if built is not None:
                features, lean = built
                technicals.append(features)
                lean_features[symbol] = lean

        external = await self._external_signals.fetch()
        factor_payload: dict[str, Any] = {"snapshots": [], "top_factors": []}
        if self._settings.arena_factors_enabled:
            factor_payload = self._factor_registry.context_payload(technicals)
        flow_payload: dict[str, Any] = {"active": [], "new_triggers": [], "trigger_names": []}
        if self._settings.arena_flow_signals_enabled:
            factors = self._factor_registry.score_many(technicals) if self._settings.arena_factors_enabled else []
            for factor in factors:
                self._factor_distribution_tracker.add(factor.symbol, factor.name, factor.score)
            flow_payload = self._flow_detector.evaluate_many(technicals, factors)
        intelligence = self._summarize_market(technicals, external)
        intelligence["factor_intelligence"] = factor_payload
        intelligence["market_flow_signals"] = flow_payload
        liquidation_constraints = await self._build_liquidation_constraints(technicals)
        return MarketContext(
            technicals=technicals,
            external_signals=external,
            intelligence=intelligence,
            factor_snapshots=factor_payload.get("snapshots") or [],
            active_flow_signals=flow_payload.get("active") or [],
            new_flow_signals=flow_payload.get("new_triggers") or [],
            liquidation_constraints=liquidation_constraints,
            lean_features=lean_features,
        )

    async def close(self) -> None:
        await self._external_signals.close()

    async def _build_symbol_features(self, symbol: str) -> tuple[TechnicalFeatures, dict[str, Any]] | None:
        """The symbol's technicals and its lean features, or None when no candles came back."""
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
            self._logger.warning("No exchange candles returned for %s.", symbol)
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
        previous_open_interest = self._last_open_interest_by_symbol.get(symbol)
        open_interest_change_pct = 0.0
        if previous_open_interest and previous_open_interest > 0 and open_interest > 0:
            open_interest_change_pct = ((open_interest - previous_open_interest) / previous_open_interest) * 100.0
        if open_interest > 0:
            self._last_open_interest_by_symbol[symbol] = open_interest

        spread_bps = None
        bid_depth_notional = None
        ask_depth_notional = None
        if self._settings.arena_flow_signals_enabled or self._settings.arena_factors_enabled:
            try:
                order_book = await self._client.get_order_book(symbol, depth=self._settings.orderbook_depth_levels)
                spread_bps = order_book.spread_bps
                bid_depth_notional = order_book.bid_depth_notional
                ask_depth_notional = order_book.ask_depth_notional
            except Exception as exc:
                self._logger.debug("Order book enrichment failed for %s: %s", symbol, exc)

        lean = lean_features_from_candles(
            bar_interval=self._settings.ai_kline_interval,
            closes=closes,
            highs=highs,
            lows=lows,
        )
        features = TechnicalFeatures(
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
            open_interest_change_pct=open_interest_change_pct,
            spread_bps=spread_bps,
            bid_depth_notional=bid_depth_notional,
            ask_depth_notional=ask_depth_notional,
        )
        return features, lean

    async def _build_liquidation_constraints(
        self,
        technicals: list[TechnicalFeatures],
    ) -> dict[str, dict[str, Any]]:
        constraints: dict[str, dict[str, Any]] = {}
        for technical in technicals:
            try:
                if not isinstance(self._client, MaintenanceMarginTierProvider):
                    raise TypeError("exchange does not provide maintenance margin tiers")
                tiers = await self._client.get_maintenance_margin_tiers(technical.symbol)
                constraints[technical.symbol] = self._liquidation_constraint(technical, tiers)
            except Exception as exc:
                self._logger.warning("Liquidation tiers unavailable for %s: %s", technical.symbol, exc)
                constraints[technical.symbol] = {
                    "status": "unavailable",
                    "error": f"maintenance_margin_tier_lookup_failed:{type(exc).__name__}",
                }
        return constraints

    def _liquidation_constraint(
        self,
        technical: TechnicalFeatures,
        tiers: list[MaintenanceMarginTier],
    ) -> dict[str, Any]:
        planned_notional = self._planned_notional()
        close_fee_rate = float(self._settings.taker_fee_pct) / 100.0
        reference_price = float(technical.last_price)
        if reference_price <= 0:
            raise ValueError("invalid liquidation reference price")
        constraint = estimate_isolated_liquidation_constraint(
            reference_price=reference_price,
            quantity_base=planned_notional / reference_price,
            leverage=int(self._settings.leverage),
            taker_close_fee_rate=close_fee_rate,
            tiers=tiers,
        )
        return constraint.to_dict()

    def _planned_notional(self) -> float:
        profile = getattr(self._settings, "volume_profile", None)
        if str(getattr(self._settings, "strategy_mode", "")) == "ai_volume" and profile is not None:
            value = getattr(profile, "notional_per_side_usdt", None)
            if value is not None:
                return float(value)
        return float(self._settings.position_usdt) * float(self._settings.leverage)

    @staticmethod
    def _select_maintenance_tier(
        tiers: list[MaintenanceMarginTier],
        notional: float,
    ) -> MaintenanceMarginTier | None:
        return select_maintenance_margin_tier(tiers, notional)

    def _summarize_market(
        self,
        technicals: list[TechnicalFeatures],
        external: ExternalMarketSignals,
    ) -> dict[str, Any]:
        if not technicals:
            return {
                "regime": "unknown",
                "risk_flags": ["missing_exchange_market_data"],
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
        if external.tradingview:
            self._tradingview_ever_available = True
        # Only flag TradingView as unavailable if the feed has worked before (a real
        # transient outage). A feed that has never responded is treated as not
        # configured, so a structurally dead relay does not poison every decision.
        if (
            self._settings.tradingview_signal_url
            and self._tradingview_ever_available
            and not external.tradingview
        ):
            risk_flags.append("tradingview_signal_unavailable")

        return {
            "regime": regime,
            "risk_flags": risk_flags,
            "symbol_scores": symbol_scores,
        }

    @staticmethod
    def _signal_config_from_settings(settings: Settings) -> SignalRuleConfig:
        return SignalRuleConfig(
            volume_spike_pct=settings.arena_volume_spike_pct,
            volume_collapse_pct=settings.arena_volume_collapse_pct,
            price_momentum_bps=settings.arena_price_momentum_bps,
            open_interest_surge_pct=settings.arena_open_interest_surge_pct,
            funding_extreme_abs=settings.arena_funding_extreme_abs,
            factor_score_trigger=settings.arena_factor_score_trigger,
            factor_percentile_trigger=settings.arena_factor_percentile_trigger,
            order_flow_imbalance_pct=settings.arena_order_flow_imbalance_pct,
            max_spread_bps=settings.max_spread_bps,
            min_depth_notional=settings.arena_min_depth_notional,
        )
