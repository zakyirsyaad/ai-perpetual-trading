from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

import aiosqlite
import httpx
from pydantic import BaseModel, Field, ValidationError

from bot.config import Settings
from bot.exchange.bingx_client import BingXFuturesClient
from bot.models import OpenPosition, PositionDirection, TradePlan
from bot.risk.risk_manager import DailyRiskState, RiskManager
from bot.storage.trade_store import TradeStore
from bot.strategy.base import BaseStrategy, ExitSignal
from bot.strategy.indicators import MarketFeatures, atr, ema, macd, pct_change, rsi

# --- Analyst-style output (professional high-leverage scalper prompt) ---
ANALYST_SYSTEM_PROMPT = """You are a professional crypto futures scalper and risk manager
specialized in high-leverage (100x–200x) trading on low timeframes (1m–15m).

You MUST assume:
- Leverage is VERY high (≈150x).
- Timeframe is 5m for entries, with 15m/1h as higher-timeframe context (trend filter).
- Survival and drawdown control matter more than number of trades.

You MUST analyze each specified market for:
- Price action and recent structure (swings, breakouts, fakeouts)
- Volume and volatility (including volume spikes vs low-liquidity chop)
- Fast and slow EMAs (e.g. EMA 5/13/21)
- RSI (short lookback, e.g. 7–9) for momentum/overbought/oversold
- MACD (fast settings) for momentum shifts
- Funding rate and open interest (crowded positioning, squeeze risk)
- Optional: band/volatility context (e.g. distance to recent high/low)

RISK PRINCIPLES (MUST FOLLOW):
- NEVER recommend a trade if conditions are chaotic, illiquid, or structurally unclear.
- Avoid entries very close to obvious support/resistance where risk/reward is poor.
- Do NOT chase extended moves (parabolic candles, extreme RSI) unless there is a very clear, asymmetric edge.
- Always think in risk/reward terms, assuming stop loss can be hit often.

LIQUIDATION & LEVERAGE AWARENESS:
- Assume liquidation price is relatively close because of high leverage.
- ALWAYS ensure the estimated liquidation distance is at least 2–3x the planned stop-loss distance.
- If this cannot be satisfied, set advise_no_trade=true and recommended_action="HOLD".

TRADE FREQUENCY:
- The bot aims for regular, good-quality scalps (e.g. multiple trades per day), NOT zero trades.
- Only recommend BUY/SELL when:
  - Trend + momentum + structure align in one direction,
  - Risk/reward is acceptable (R:R >= about 1.5),
  - Liquidity and volatility are normal (not extreme chaos).
- When in doubt or when signals conflict, choose HOLD.

OUTPUT SCHEMA (per market):
1. market (string): Market symbol (e.g., "BTCUSDT").
2. market_trend ("Bullish"|"Bearish"|"Sideways"): Assessed trend.
3. key_support (number[]): Key support levels.
4. key_resistance (number[]): Key resistance levels.
5. entry_zone (number[]|null): Suggested entry zone, or null if no entry.
6. stop_loss_level (number|null): Suggested stop loss, or null if no trade.
7. take_profit_targets (number[]|null): Profit targets, or null if no trade.
8. risk_reward_ratio (number|null): Expected R/R ratio, or null if no trade.
9. confidence_level (integer 1–10): Subjective confidence.
10. risk_warning (string): Brief risk warning.
11. systemic_risk (integer 1–5): Drawdown/volatility risk (5 = highest).
12. advise_no_trade (boolean): true if unclear/no trade recommended.
13. recommended_action ("BUY"|"SELL"|"HOLD"): "HOLD" if no trade; else "BUY" or "SELL".

If analysis is not possible or data is missing, set advise_no_trade=true and recommended_action="HOLD".
Populate all fields; use nulls for non-applicable entries.
If indicator input/data is missing, set the relevant field to null and briefly note this in risk_warning.
Output must be a single JSON object (or an array for multiple markets) using the exact field order above.
Interpret "BUY" as opening a long position and "SELL" as opening a short position on USDT-margined futures."""


class AnalystEntryResponse(BaseModel):
    """Structured analyst output; mapped to bot entry/NO TRADE."""

    market: str = Field(description='Market symbol, e.g. "BTCUSDT"')
    market_trend: Literal["Bullish", "Bearish", "Sideways"] = Field(
        description="Assessed trend"
    )
    key_support: list[float] = Field(
        default_factory=list,
        description="Key support levels",
    )
    key_resistance: list[float] = Field(
        default_factory=list,
        description="Key resistance levels",
    )
    entry_zone: list[float] | None = Field(
        default=None,
        description="Suggested entry zone or null if no entry",
    )
    stop_loss_level: float | None = Field(
        default=None,
        description="Suggested stop loss price or null if no trade",
    )
    take_profit_targets: list[float] | None = Field(
        default=None,
        description="Profit targets or null if no trade",
    )
    risk_reward_ratio: float | None = Field(
        default=None,
        description="Expected risk/reward ratio or null if no trade",
    )
    confidence_level: int = Field(ge=1, le=10, description="Confidence 1–10")
    risk_warning: str = Field(
        default="",
        description="Risk warning (free-form text)",
    )
    systemic_risk: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Systemic risk rating 1-5; 5 = highest vulnerability to drawdown/spike",
    )
    advise_no_trade: bool = Field(
        default=False,
        description="True if conditions are unclear or no setup",
    )
    recommended_action: Literal["BUY", "SELL", "HOLD"] = Field(
        description='Trade decision: "BUY", "SELL", or "HOLD" (no trade)'
    )


class PreTradeCheckResponse(BaseModel):
    """Skeptical risk manager: confirmations and invalidation before entry."""

    approved: bool = Field(description="True only if trade passes pre-trade integrity check")
    confirmations: str = Field(
        min_length=1,
        max_length=400,
        description="2-3 non-price confirmations required for the trade to be valid",
    )
    invalidation_trigger: str = Field(
        min_length=1,
        max_length=200,
        description="One condition that, if met, means exit immediately",
    )


class ExitDecision(BaseModel):
    action: Literal["CLOSE", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1)


@dataclass(slots=True)
class SetupAssessment:
    direction: PositionDirection
    score: float
    regime: str
    size_multiplier: float
    notes: list[str]


@dataclass(slots=True)
class ExitCheckCache:
    checked_at: float
    reference_price: float
    signal: ExitSignal


class OpenAIStrategy(BaseStrategy):
    def __init__(
        self,
        settings: Settings,
        client: BingXFuturesClient,
        logger: logging.Logger,
        trade_store: TradeStore,
        risk_manager: RiskManager,
    ) -> None:
        xai_key = settings.xai_api_key or settings.openai_api_key
        if not xai_key:
            raise ValueError("XAI_API_KEY is required when STRATEGY_MODE=ai")

        self._settings = settings
        self._client = client
        self._logger = logger
        self._trade_store = trade_store
        self._risk_manager = risk_manager
        # Use a more generous timeout for xAI (reasoning models can be slow).
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                float(settings.request_timeout_seconds or 0) or 120.0,
                connect=15.0,
                read=float(settings.request_timeout_seconds or 0) or 110.0,
            ),
            headers={
                "Authorization": f"Bearer {xai_key}",
                "Content-Type": "application/json",
            },
        )
        self._rate_limited_until: float | None = None
        self._exit_check_cache: dict[str, ExitCheckCache] = {}

    async def next_trade_plan(self) -> TradePlan | None:
        now_ts = time.time()
        if self._rate_limited_until is not None and now_ts < self._rate_limited_until:
            self._logger.warning(
                "Skipping AI entry decision due to recent xAI rate limit (%.0fs remaining).",
                self._rate_limited_until - now_ts,
            )
            return None

        features = await self._collect_features()
        if not features:
            self._logger.warning("No market features available for AI entry decision.")
            return None

        daily_state = await self._risk_manager.get_daily_state()
        recent_perf = await self._summarize_recent_performance(limit=50)
        payload = self._build_analyst_entry_payload(features, daily_state, recent_perf)
        analysis = await self._ask_openai(
            payload,
            AnalystEntryResponse,
            system_prompt=ANALYST_SYSTEM_PROMPT,
            reasoning_effort="medium",
            verbosity="medium",
        )
        if analysis is None:
            self._logger.warning(
                "AI entry analysis failed (no valid response); holding for this cycle."
            )
            return None

        symbol = self._normalize_ai_symbol(analysis.market)
        action = analysis.recommended_action
        confidence = analysis.confidence_level / 10.0
        if symbol not in self._settings.symbols:
            self._logger.warning("AI returned unsupported symbol '%s'.", symbol)
            return None
        if action not in {"BUY", "SELL", "HOLD"}:
            self._logger.warning("AI returned unsupported action '%s'.", action)
            return None

        if analysis.advise_no_trade or action == "HOLD":
            self._logger.info(
                "AI advises HOLD for %s. trend=%s reason=%s",
                symbol,
                analysis.market_trend,
                analysis.risk_warning,
            )
            return None

        if confidence < self._settings.ai_min_confidence:
            self._logger.info(
                "AI entry ignored for %s (confidence %d/10 < %.2f).",
                symbol,
                analysis.confidence_level,
                self._settings.ai_min_confidence,
            )
            return None

        if analysis.systemic_risk >= self._settings.ai_systemic_risk_no_trade_threshold:
            self._logger.info(
                "AI entry skipped for %s: systemic_risk=%d >= threshold=%d.",
                symbol,
                analysis.systemic_risk,
                self._settings.ai_systemic_risk_no_trade_threshold,
            )
            return None

        direction = PositionDirection.LONG if action == "BUY" else PositionDirection.SHORT
        if not self._trend_matches_direction(analysis.market_trend, direction):
            self._logger.info(
                "AI entry skipped for %s: action=%s conflicts with trend=%s.",
                symbol,
                action,
                analysis.market_trend,
            )
            return None

        feature = next((item for item in features if item.symbol == symbol), None)
        if feature is None:
            self._logger.warning("No collected features found for AI-selected symbol '%s'.", symbol)
            return None

        assessment = self._assess_setup(feature, direction)
        if assessment.regime in {"chaotic", "stagnant"}:
            self._logger.info(
                "AI entry skipped for %s due to regime=%s (%s).",
                symbol,
                assessment.regime,
                ", ".join(assessment.notes[:2]) or "low-quality environment",
            )
            return None
        if assessment.score < self._settings.ai_min_setup_score:
            self._logger.info(
                "AI entry skipped for %s: setup_score=%.2f < %.2f (%s).",
                symbol,
                assessment.score,
                self._settings.ai_min_setup_score,
                ", ".join(assessment.notes[:3]) or "weak technical alignment",
            )
            return None
        if self._entry_zone_conflicts(feature, direction, analysis):
            self._logger.info(
                "AI entry skipped for %s because the suggested zone is too extended for %s.",
                symbol,
                direction.value,
            )
            return None

        rr = analysis.risk_reward_ratio
        if rr is not None and rr < 1.2:
            self._logger.info(
                "AI entry skipped for %s due to low reported risk/reward %.2f.",
                symbol,
                rr,
            )
            return None

        pre_trade: PreTradeCheckResponse | None = None
        if self._settings.ai_pre_trade_check:
            plan_summary = {
                "symbol": symbol,
                "direction": action,
                "entry_zone": analysis.entry_zone or "market",
                "stop_loss_level": analysis.stop_loss_level,
                "take_profit_targets": analysis.take_profit_targets,
                "market_trend": analysis.market_trend,
                "confidence_level": analysis.confidence_level,
                "setup_score": round(assessment.score, 4),
                "regime": assessment.regime,
                "technical_notes": assessment.notes[:4],
            }
            pre_trade = await self._ask_pre_trade_check(plan_summary)
            if pre_trade is None or not pre_trade.approved:
                self._logger.info(
                    "Pre-trade check rejected %s %s. %s",
                    symbol,
                    direction.value,
                    getattr(pre_trade, "invalidation_trigger", "no response"),
                )
                return None

        sl_pct, tp_pct, effective_rr = self._resolve_trade_targets(direction, feature, analysis)
        if effective_rr < 1.25:
            self._logger.info(
                "AI entry skipped for %s because effective risk/reward %.2f is too low.",
                symbol,
                effective_rr,
            )
            return None

        usdt_size = self._size_position(
            stop_loss_pct=sl_pct,
            confidence=confidence,
            setup=assessment,
            effective_rr=effective_rr,
            systemic_risk=analysis.systemic_risk,
        )

        reason_parts = [
            f"trend={analysis.market_trend}",
            f"conf={analysis.confidence_level}/10",
            f"setup={assessment.score:.2f}",
            f"regime={assessment.regime}",
            f"rr={effective_rr:.2f}",
        ]
        if analysis.entry_zone:
            reason_parts.append(f"zone={analysis.entry_zone[:2]}")
        if analysis.risk_warning:
            reason_parts.append(f"risk={analysis.risk_warning[:60]}")
        if assessment.notes:
            reason_parts.append(f"notes={'; '.join(assessment.notes[:2])[:70]}")
        if pre_trade is not None:
            reason_parts.append(f"inv={pre_trade.invalidation_trigger[:40]}")
        decision_reason = "ai:" + " | ".join(reason_parts)

        return TradePlan(
            symbol=symbol,
            direction=direction,
            usdt_size=usdt_size,
            leverage=self._settings.leverage,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            decision_reason=decision_reason,
            confidence=confidence,
            setup_score=assessment.score,
            max_hold_minutes=self._settings.position_hold_minutes,
        )

    async def exit_signal(
        self,
        position: OpenPosition,
        current_price: float,
        elapsed_seconds: float,
    ) -> ExitSignal:
        max_hold_seconds = self._settings.position_hold_minutes * 60
        if elapsed_seconds >= max_hold_seconds:
            return ExitSignal(close_now=True, reason="time_stop")

        cache_key = f"{position.symbol}:{position.exchange_order_id}"
        cached = self._exit_check_cache.get(cache_key)
        if cached is not None and not self._should_refresh_exit_check(cached, current_price):
            return cached.signal

        features = await self._collect_features(symbols=(position.symbol,))
        if not features:
            signal = ExitSignal(close_now=False, reason="hold")
            self._exit_check_cache[cache_key] = ExitCheckCache(
                checked_at=time.time(),
                reference_price=current_price,
                signal=signal,
            )
            return signal

        feature = features[0]
        quant_signal = self._rule_based_exit_signal(
            position=position,
            current_price=current_price,
            elapsed_seconds=elapsed_seconds,
            feature=feature,
        )
        if quant_signal.close_now:
            self._exit_check_cache[cache_key] = ExitCheckCache(
                checked_at=time.time(),
                reference_price=current_price,
                signal=quant_signal,
            )
            return quant_signal

        payload = self._build_exit_payload(position, current_price, elapsed_seconds, feature)
        decision = await self._ask_openai(
            payload,
            ExitDecision,
            reasoning_effort="low",
            verbosity="low",
        )
        if decision is None:
            signal = ExitSignal(close_now=False, reason="hold")
        else:
            min_conf = self._settings.ai_min_confidence
            if elapsed_seconds >= max_hold_seconds * 0.75:
                min_conf = max(0.45, min_conf - 0.1)
            if decision.confidence < min_conf:
                signal = ExitSignal(close_now=False, reason="hold")
            else:
                signal = ExitSignal(
                    close_now=decision.action == "CLOSE",
                    reason=decision.reason if decision.action == "CLOSE" else "hold",
                )

        self._exit_check_cache[cache_key] = ExitCheckCache(
            checked_at=time.time(),
            reference_price=current_price,
            signal=signal,
        )
        return signal

    async def close(self) -> None:
        await self._http.aclose()

    async def _collect_features(self, symbols: tuple[str, ...] | None = None) -> list[MarketFeatures]:
        pairs = symbols or self._settings.symbols
        gathered = await asyncio.gather(
            *(self._collect_symbol_features(symbol) for symbol in pairs),
            return_exceptions=True,
        )
        result: list[MarketFeatures] = []
        for symbol, item in zip(pairs, gathered):
            if isinstance(item, Exception):
                self._logger.warning("Failed to collect features for %s: %s", symbol, item)
                continue
            if item is not None:
                result.append(item)
        return result

    async def _collect_symbol_features(self, symbol: str) -> MarketFeatures | None:
        klines = await self._client.get_klines(
            symbol=symbol,
            interval=self._settings.ai_kline_interval,
            limit=self._settings.ai_kline_limit,
        )
        closes = [float(item["close"]) for item in klines]
        highs = [float(item["high"]) for item in klines]
        lows = [float(item["low"]) for item in klines]
        volumes = [float(item.get("volume", 0)) for item in klines]
        if not closes:
            return None

        macd_line, macd_signal, macd_hist = macd(closes, 12, 26, 9)
        atr_value = atr(highs, lows, closes, 14)
        last_price = closes[-1]
        recent_high = max(highs[-20:]) if highs else last_price
        recent_low = min(lows[-20:]) if lows else last_price
        range_position = 0.5
        if recent_high > recent_low:
            range_position = (last_price - recent_low) / (recent_high - recent_low)
            range_position = max(0.0, min(range_position, 1.0))
        volatility_pct = (atr_value / last_price) * 100.0 if last_price > 0 else 0.0

        try:
            funding_rate, open_interest = await asyncio.gather(
                self._client.get_funding_rate(symbol),
                self._client.get_open_interest(symbol),
            )
        except Exception as exc:
            self._logger.debug("Funding/OI fetch failed for %s: %s", symbol, exc)
            funding_rate = 0.0
            open_interest = 0.0

        return MarketFeatures(
            symbol=symbol,
            last_price=last_price,
            ema_fast=ema(closes, 9),
            ema_slow=ema(closes, 21),
            ema_21=ema(closes, 21),
            rsi_14=rsi(closes, 14),
            atr_14=atr_value,
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_hist=macd_hist,
            change_pct_1=pct_change(closes, 1),
            change_pct_5=pct_change(closes, 5),
            volume=volumes[-1] if volumes else 0.0,
            volume_change_pct=pct_change(volumes, 5) if len(volumes) > 5 else 0.0,
            funding_rate=funding_rate,
            open_interest=open_interest,
            recent_high_20=recent_high,
            recent_low_20=recent_low,
            range_position_20=range_position,
            volatility_pct=volatility_pct,
        )

    def _assess_setup(
        self,
        feature: MarketFeatures,
        direction: PositionDirection,
    ) -> SetupAssessment:
        score = 0.5
        regime = "trend"
        notes: list[str] = []
        volatility_low = max(0.03, self._settings.stop_loss_pct * 0.20)
        volatility_high = max(volatility_low * 4.0, self._settings.stop_loss_pct * 2.0)

        if feature.volatility_pct < volatility_low:
            score -= 0.12
            regime = "stagnant"
            notes.append("volatility too low relative to fees")
        elif feature.volatility_pct > volatility_high:
            score -= 0.14
            regime = "chaotic"
            notes.append("volatility spike is too large")

        crowded_long = feature.funding_rate > 0.0010
        crowded_short = feature.funding_rate < -0.0010

        if direction == PositionDirection.LONG:
            if feature.last_price >= feature.ema_fast:
                score += 0.09
            else:
                score -= 0.11
                notes.append("price is below fast EMA")
            if feature.ema_fast >= feature.ema_slow:
                score += 0.14
            else:
                score -= 0.16
                notes.append("fast EMA is below slow EMA")
            if feature.macd_hist >= 0:
                score += 0.11
            else:
                score -= 0.12
                notes.append("MACD momentum is negative")
            if 52 <= feature.rsi_14 <= 68:
                score += 0.11
            elif 68 < feature.rsi_14 <= 75:
                score += 0.03
                regime = "stretched" if regime == "trend" else regime
                notes.append("RSI is getting extended")
            elif feature.rsi_14 < 45:
                score -= 0.13
                notes.append("RSI lacks bullish momentum")
            if feature.change_pct_5 > 0:
                score += 0.07
            else:
                score -= 0.08
            if feature.volume_change_pct > 0:
                score += 0.06
            elif feature.volume_change_pct < -20:
                score -= 0.07
                notes.append("volume participation is fading")
            if feature.range_position_20 > 0.92:
                score -= 0.10
                regime = "stretched"
                notes.append("price sits too close to recent high")
            elif feature.range_position_20 < 0.35:
                score += 0.03
            if crowded_long:
                score -= 0.05
                notes.append("positive funding suggests crowded longs")
        else:
            if feature.last_price <= feature.ema_fast:
                score += 0.09
            else:
                score -= 0.11
                notes.append("price is above fast EMA")
            if feature.ema_fast <= feature.ema_slow:
                score += 0.14
            else:
                score -= 0.16
                notes.append("fast EMA is above slow EMA")
            if feature.macd_hist <= 0:
                score += 0.11
            else:
                score -= 0.12
                notes.append("MACD momentum is positive")
            if 32 <= feature.rsi_14 <= 48:
                score += 0.11
            elif 25 <= feature.rsi_14 < 32:
                score += 0.03
                regime = "stretched" if regime == "trend" else regime
                notes.append("RSI is getting extended")
            elif feature.rsi_14 > 55:
                score -= 0.13
                notes.append("RSI lacks bearish momentum")
            if feature.change_pct_5 < 0:
                score += 0.07
            else:
                score -= 0.08
            if feature.volume_change_pct > 0:
                score += 0.06
            elif feature.volume_change_pct < -20:
                score -= 0.07
                notes.append("volume participation is fading")
            if feature.range_position_20 < 0.08:
                score -= 0.10
                regime = "stretched"
                notes.append("price sits too close to recent low")
            elif feature.range_position_20 > 0.65:
                score += 0.03
            if crowded_short:
                score -= 0.05
                notes.append("negative funding suggests crowded shorts")

        if abs(feature.change_pct_1) > max(feature.volatility_pct * 1.5, self._settings.stop_loss_pct * 0.9):
            score -= 0.05
            regime = "stretched" if regime == "trend" else regime
            notes.append("latest candle looks overextended")

        if (
            feature.volatility_pct >= volatility_low
            and regime == "trend"
            and (
                (direction == PositionDirection.LONG and feature.ema_fast < feature.ema_slow and feature.macd_hist < 0)
                or (
                    direction == PositionDirection.SHORT
                    and feature.ema_fast > feature.ema_slow
                    and feature.macd_hist > 0
                )
            )
        ):
            regime = "chop"
            score -= 0.05
            notes.append("trend and momentum are not aligned")

        score = max(0.0, min(score, 1.0))
        size_multiplier = max(0.70, min(1.20, 0.85 + ((score - 0.5) * 0.9)))
        if regime in {"chaotic", "stagnant"}:
            size_multiplier = min(size_multiplier, 0.80)

        return SetupAssessment(
            direction=direction,
            score=score,
            regime=regime,
            size_multiplier=size_multiplier,
            notes=notes[:5],
        )

    def _resolve_trade_targets(
        self,
        direction: PositionDirection,
        feature: MarketFeatures,
        analysis: AnalystEntryResponse,
    ) -> tuple[float, float, float]:
        base_sl = self._settings.stop_loss_pct
        base_tp = self._settings.take_profit_pct
        atr_pct = feature.volatility_pct
        atr_sl = atr_pct * self._settings.atr_sl_multiplier if atr_pct > 0 else base_sl
        ai_sl_pct = self._price_distance_pct(
            direction=direction,
            current_price=feature.last_price,
            level=analysis.stop_loss_level,
            for_take_profit=False,
        )

        min_sl = max(0.04, min(base_sl, atr_sl * 0.75 if atr_pct > 0 else base_sl))
        max_sl = max(base_sl * 1.35, min_sl * 1.5)
        if ai_sl_pct is not None and atr_pct > 0:
            stop_loss_pct = (ai_sl_pct * 0.65) + (atr_sl * 0.35)
        elif ai_sl_pct is not None:
            stop_loss_pct = ai_sl_pct
        elif atr_pct > 0:
            stop_loss_pct = atr_sl
        else:
            stop_loss_pct = base_sl
        stop_loss_pct = min(max(stop_loss_pct, min_sl), max_sl)

        ai_tp_pct: float | None = None
        for target in analysis.take_profit_targets or []:
            candidate = self._price_distance_pct(
                direction=direction,
                current_price=feature.last_price,
                level=target,
                for_take_profit=True,
            )
            if candidate is not None:
                ai_tp_pct = candidate
                break

        rr_target = analysis.risk_reward_ratio if analysis.risk_reward_ratio is not None else 1.8
        rr_target = min(max(rr_target, 1.35), 3.5)
        take_profit_pct = max(base_tp, stop_loss_pct * rr_target)
        if ai_tp_pct is not None:
            take_profit_pct = max(base_tp, min(ai_tp_pct, stop_loss_pct * 3.5))
            take_profit_pct = max(take_profit_pct, stop_loss_pct * 1.25)
        if atr_pct > 0:
            take_profit_pct = max(take_profit_pct, max(base_tp, atr_pct * 0.9))

        effective_rr = take_profit_pct / stop_loss_pct if stop_loss_pct > 0 else 0.0
        return stop_loss_pct, take_profit_pct, effective_rr

    def _size_position(
        self,
        stop_loss_pct: float,
        confidence: float,
        setup: SetupAssessment,
        effective_rr: float,
        systemic_risk: int,
    ) -> float:
        base_size = self._settings.position_usdt
        if stop_loss_pct <= 0 or self._settings.leverage <= 0:
            return base_size

        risk_usdt = (
            self._settings.account_balance_usdt * self._settings.max_risk_pct_per_trade
        ) / 100.0
        risk_limited_size = risk_usdt / ((stop_loss_pct / 100.0) * self._settings.leverage)
        risk_limited_size = max(risk_limited_size, 0.0)
        if risk_limited_size <= 0:
            return base_size * 0.35

        cap = min(risk_limited_size, base_size * 1.6)
        floor = min(base_size * 0.35, cap)
        confidence_scale = 0.85 + max(0.0, confidence - self._settings.ai_min_confidence) * 0.9
        setup_scale = 0.85 + max(0.0, setup.score - self._settings.ai_min_setup_score) * 1.0
        rr_scale = 0.95
        if effective_rr >= 2.0:
            rr_scale = 1.05
        elif effective_rr >= 1.5:
            rr_scale = 1.0
        systemic_scale = max(0.70, 1.0 - max(0, systemic_risk - 1) * 0.08)

        raw_size = (
            min(base_size, cap)
            * confidence_scale
            * setup.size_multiplier
            * setup_scale
            * rr_scale
            * systemic_scale
        )
        sized = max(floor, min(raw_size, cap))
        return sized

    def _entry_zone_conflicts(
        self,
        feature: MarketFeatures,
        direction: PositionDirection,
        analysis: AnalystEntryResponse,
    ) -> bool:
        if not analysis.entry_zone:
            return False
        entry_mid = sum(analysis.entry_zone) / float(len(analysis.entry_zone))
        support = analysis.key_support[:2] or [feature.recent_low_20]
        resistance = analysis.key_resistance[:2] or [feature.recent_high_20]
        support_mid = sum(support) / float(len(support))
        resistance_mid = sum(resistance) / float(len(resistance))
        if resistance_mid <= support_mid:
            return False
        frac = (entry_mid - support_mid) / (resistance_mid - support_mid)
        if direction == PositionDirection.LONG:
            return frac > 0.72
        return frac < 0.28

    def _rule_based_exit_signal(
        self,
        position: OpenPosition,
        current_price: float,
        elapsed_seconds: float,
        feature: MarketFeatures,
    ) -> ExitSignal:
        late_trade = elapsed_seconds >= (self._settings.position_hold_minutes * 60 * 0.60)
        if position.direction == PositionDirection.LONG:
            if current_price < feature.ema_fast and feature.macd_hist < 0 and feature.rsi_14 < 48:
                return ExitSignal(close_now=True, reason="quant_reversal")
            if late_trade and feature.change_pct_1 < 0 and feature.rsi_14 < 52:
                return ExitSignal(close_now=True, reason="late_momentum_fade")
        else:
            if current_price > feature.ema_fast and feature.macd_hist > 0 and feature.rsi_14 > 52:
                return ExitSignal(close_now=True, reason="quant_reversal")
            if late_trade and feature.change_pct_1 > 0 and feature.rsi_14 > 48:
                return ExitSignal(close_now=True, reason="late_momentum_fade")
        return ExitSignal(close_now=False, reason="hold")

    def _should_refresh_exit_check(
        self,
        cached: ExitCheckCache,
        current_price: float,
    ) -> bool:
        if cached.signal.close_now:
            return False
        if time.time() - cached.checked_at >= self._settings.ai_exit_check_interval_seconds:
            return True
        if cached.reference_price <= 0:
            return True
        move_pct = abs((current_price - cached.reference_price) / cached.reference_price) * 100.0
        return move_pct >= self._settings.ai_exit_price_move_threshold_pct

    def _build_analyst_entry_payload(
        self,
        features: list[MarketFeatures],
        daily_state: DailyRiskState,
        recent_perf: dict[str, Any],
    ) -> str:
        data = {
            "task": "entry_decision",
            "instruction": (
                "Analyze the provided markets and return the single best actionable setup. "
                "If no setup is high quality, return HOLD. "
                "Use the supplied market_data, recent performance, and account_risk_state. "
                "Prefer trades where trend, momentum, volatility, and structure align. "
                "Avoid chaotic, overextended, or low-liquidity conditions."
            ),
            "market_data": [asdict(x) for x in features],
            "constraints": {
                "allowed_symbols": list(self._settings.symbols),
                "must_return_json": True,
                "consider_taker_fee_pct": self._settings.taker_fee_pct,
                "target_trades_per_day": self._settings.trade_target_per_day,
                "decision_interval_minutes": self._settings.entry_interval_minutes,
                "min_confidence_level_for_entry": 7,
            },
            "account_risk_state": {
                "day": str(daily_state.day),
                "closed_trades_today": daily_state.closed_trades,
                "stop_losses_today": daily_state.stop_losses,
                "consecutive_losses_today": daily_state.consecutive_losses,
                "total_pnl_today_usdt": daily_state.total_pnl_usdt,
                "max_daily_drawdown_usdt": self._settings.max_daily_drawdown_usdt,
                "allowed_to_trade": daily_state.allowed_to_trade,
            },
            "recent_entry_performance": recent_perf,
            "output_schema": {
                "market": "string, e.g. BTCUSDT",
                "market_trend": "Bullish|Bearish|Sideways",
                "key_support": "number[]",
                "key_resistance": "number[]",
                "entry_zone": "number[] or null",
                "stop_loss_level": "number or null",
                "take_profit_targets": "number[] or null",
                "risk_reward_ratio": "number or null",
                "confidence_level": "integer 1-10",
                "risk_warning": "string",
                "systemic_risk": "integer 1-5 (5=highest systemic risk)",
                "advise_no_trade": "boolean",
                "recommended_action": "BUY|SELL|HOLD",
            },
        }
        return json.dumps(data)

    def _build_pre_trade_check_payload(self, plan_summary: dict[str, Any]) -> str:
        return json.dumps(
            {
                "task": "pre_trade_integrity_check",
                "instruction": (
                    "Act as a skeptical risk manager. Validate the proposed setup, list the key "
                    "confirmations, and define one invalidation trigger. Approve only if the setup "
                    "has clear structural support."
                ),
                "planned_trade": plan_summary,
                "output_schema": {
                    "approved": "boolean",
                    "confirmations": "string",
                    "invalidation_trigger": "string",
                },
            }
        )

    async def _ask_pre_trade_check(self, plan_summary: dict[str, Any]) -> PreTradeCheckResponse | None:
        payload = self._build_pre_trade_check_payload(plan_summary)
        system = (
            "You are a skeptical crypto futures risk manager. "
            "Return only strict JSON matching the requested schema."
        )
        return await self._ask_openai(
            payload,
            PreTradeCheckResponse,
            system_prompt=system,
            reasoning_effort="low",
            verbosity="low",
        )

    def _build_exit_payload(
        self,
        position: OpenPosition,
        current_price: float,
        elapsed_seconds: float,
        features: MarketFeatures,
    ) -> str:
        data = {
            "task": "exit_decision",
            "objective": "close positions when risk-reward worsens or trend invalidates",
            "position": {
                "symbol": position.symbol,
                "direction": position.direction.value,
                "entry_price": position.entry_price,
                "current_price": current_price,
                "quantity": position.quantity,
                "elapsed_seconds": elapsed_seconds,
            },
            "features": asdict(features),
            "constraints": {
                "actions": ["CLOSE", "HOLD"],
                "must_return_json": True,
                "consider_taker_fee_pct": self._settings.taker_fee_pct,
            },
            "output_schema": {
                "action": "CLOSE|HOLD",
                "confidence": "0..1",
                "reason": "short reason",
            },
        }
        return json.dumps(data)

    async def _ask_openai(
        self,
        task_payload: str,
        schema: type[BaseModel],
        system_prompt: str | None = None,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
    ) -> BaseModel | None:
        del reasoning_effort, verbosity
        system = system_prompt if system_prompt is not None else (
            "You are a quant trading decision assistant for crypto futures. "
            "Always return only strict JSON that matches the requested schema."
        )
        body: dict[str, Any] = {
            "model": self._settings.xai_model or self._settings.openai_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": task_payload},
            ],
            "temperature": 0.1,
        }

        last_error: Exception | None = None
        for attempt in range(2):
            parsed: Any | None = None
            content: str = ""
            try:
                base = (self._settings.xai_base_url or "https://api.x.ai/v1").rstrip("/")
                resp = await self._http.post(f"{base}/chat/completions", json=body)
                resp.raise_for_status()
                payload = resp.json()
                content = (
                    payload.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                parsed = self._parse_json(content)
                if parsed is None:
                    self._logger.warning(
                        "Failed to parse AI JSON response (attempt %d). Raw content (truncated): %s",
                        attempt + 1,
                        (content or "")[:400],
                    )
                    continue
                if schema is AnalystEntryResponse:
                    parsed = self._normalize_analyst_response(parsed)
                return schema.model_validate(parsed)
            except ValidationError as exc:
                last_error = exc
                self._logger.warning(
                    "xAI returned invalid schema (attempt %d): %s. Content (truncated): %s",
                    attempt + 1,
                    exc,
                    (content or "")[:400],
                )
            except ValueError as exc:
                last_error = exc
                self._logger.warning(
                    "xAI returned unusable content (attempt %d): %s. Content (truncated): %s",
                    attempt + 1,
                    exc,
                    (content or "")[:400],
                )
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code if exc.response is not None else None
                if status == 429:
                    cooldown = 1800.0
                    self._rate_limited_until = time.time() + cooldown
                    self._logger.warning(
                        "xAI rate limited (429). Backing off for %.0fs with no retry.",
                        cooldown,
                    )
                    return None
                self._logger.warning("xAI HTTP error (attempt %d): %s", attempt + 1, exc)
            except httpx.RequestError as exc:
                last_error = exc
                url = str(getattr(exc.request, "url", "")) if getattr(exc, "request", None) is not None else ""
                self._logger.warning(
                    "xAI request error (attempt %d): %s %r url=%s",
                    attempt + 1,
                    exc.__class__.__name__,
                    exc,
                    url,
                )
            except Exception as exc:
                last_error = exc
                self._logger.warning(
                    "xAI request failed (attempt %d): %s %r",
                    attempt + 1,
                    exc.__class__.__name__,
                    exc,
                )
        if last_error:
            self._logger.warning("xAI failed after retries: %s %r", last_error.__class__.__name__, last_error)
        return None

    async def _summarize_recent_performance(self, limit: int = 50) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "total_trades": 0,
            "total_pnl_usdt": 0.0,
            "by_entry_reason_sample": [],
        }
        try:
            async with aiosqlite.connect(self._settings.database_path) as db:  # type: ignore[arg-type]
                cursor = await db.execute(
                    """
                    SELECT entry_reason, pnl_usdt
                    FROM closed_trades
                    ORDER BY closed_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = await cursor.fetchall()
        except Exception as exc:
            self._logger.debug("Failed to load recent performance for AI context: %s", exc)
            return summary

        if not rows:
            return summary

        summary["total_trades"] = len(rows)
        total_pnl = 0.0
        reason_stats: dict[str, dict[str, Any]] = {}
        for reason, pnl in rows:
            r = str(reason or "unknown")
            p = float(pnl or 0.0)
            total_pnl += p
            stats = reason_stats.setdefault(r, {"trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
            stats["trades"] += 1
            if p > 0:
                stats["wins"] += 1
            elif p < 0:
                stats["losses"] += 1
            stats["total_pnl"] += p

        summary["total_pnl_usdt"] = total_pnl
        for reason, stats in reason_stats.items():
            trades = stats["trades"]
            wins = stats["wins"]
            win_rate = (wins / trades * 100.0) if trades else 0.0
            avg_pnl = stats["total_pnl"] / trades if trades else 0.0
            summary["by_entry_reason_sample"].append(
                {
                    "entry_reason": reason,
                    "trades": trades,
                    "wins": wins,
                    "losses": stats["losses"],
                    "win_rate_pct": win_rate,
                    "avg_pnl_usdt": avg_pnl,
                }
            )
        return summary

    def _normalize_analyst_response(self, parsed: Any) -> dict[str, Any]:
        if isinstance(parsed, list):
            candidates = [
                item
                for item in parsed
                if isinstance(item, dict)
                and "market_trend" in item
                and "recommended_action" in item
            ]
            if not candidates:
                raise ValueError("AI response did not contain a valid analyst object.")
            candidates.sort(key=self._score_analyst_candidate, reverse=True)
            return candidates[0]
        if not isinstance(parsed, dict):
            raise ValueError("AI response is not a valid JSON object.")
        return parsed

    @staticmethod
    def _parse_json(content: str) -> Any | None:
        text = content.strip()
        if not text:
            return None
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()
        text = text.strip()
        if text and text[0] in "{[":
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            start = text.find("[")
            end = text.rfind("]")
            if start < 0 or end <= start:
                return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None

    def _score_analyst_candidate(self, candidate: dict[str, Any]) -> float:
        symbol = self._normalize_ai_symbol(str(candidate.get("market") or ""))
        supported_bonus = 1.0 if symbol in self._settings.symbols else -2.0
        action = str(candidate.get("recommended_action") or "HOLD").upper()
        advise_no_trade = bool(candidate.get("advise_no_trade"))
        action_bonus = 2.0 if action in {"BUY", "SELL"} and not advise_no_trade else 0.0
        confidence = self._safe_float(candidate.get("confidence_level"))
        rr = self._safe_float(candidate.get("risk_reward_ratio"))
        systemic_risk = self._safe_float(candidate.get("systemic_risk"), default=5.0)
        return action_bonus + supported_bonus + (confidence * 0.30) + (min(rr, 4.0) * 0.25) - (systemic_risk * 0.40)

    @staticmethod
    def _normalize_ai_symbol(symbol: str) -> str:
        normalized = symbol.upper().replace("-", "/").strip()
        if "USDT" in normalized and "/" not in normalized:
            base = normalized[:-4]
            quote = normalized[-4:]
            return f"{base}/{quote}"
        return normalized

    @staticmethod
    def _trend_matches_direction(
        market_trend: Literal["Bullish", "Bearish", "Sideways"],
        direction: PositionDirection,
    ) -> bool:
        if market_trend == "Sideways":
            return True
        if direction == PositionDirection.LONG:
            return market_trend == "Bullish"
        return market_trend == "Bearish"

    @staticmethod
    def _price_distance_pct(
        direction: PositionDirection,
        current_price: float,
        level: float | None,
        for_take_profit: bool,
    ) -> float | None:
        if current_price <= 0 or level is None or level <= 0:
            return None
        if direction == PositionDirection.LONG:
            if for_take_profit and level > current_price:
                return ((level - current_price) / current_price) * 100.0
            if not for_take_profit and level < current_price:
                return ((current_price - level) / current_price) * 100.0
            return None
        if for_take_profit and level < current_price:
            return ((current_price - level) / current_price) * 100.0
        if not for_take_profit and level > current_price:
            return ((level - current_price) / current_price) * 100.0
        return None

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

