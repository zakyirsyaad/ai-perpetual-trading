from __future__ import annotations

import json
import logging
import time
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, Field, ValidationError

from bot.config import Settings


class EntryDecision(BaseModel):
    symbol: str = Field(description='Internal symbol, for example "BTC/USDT"')
    action: Literal["OPEN_LONG", "OPEN_SHORT", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    market_trend: Literal["Bullish", "Bearish", "Sideways"]
    key_support: list[float] = Field(default_factory=list)
    key_resistance: list[float] = Field(default_factory=list)
    stop_loss_pct: float | None = Field(default=None, ge=0.0)
    take_profit_pct: float | None = Field(default=None, ge=0.0)
    risk_reward_ratio: float | None = Field(default=None, ge=0.0)
    systemic_risk: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=600)
    learning_note: str = Field(default="", max_length=500)


class EntryDecisionBatch(BaseModel):
    decisions: list[EntryDecision] = Field(
        min_length=1,
        description="One independent entry decision for each allowed symbol.",
    )


class PreTradeCheck(BaseModel):
    approved: bool
    confirmations: str = Field(min_length=1, max_length=500)
    invalidation_trigger: str = Field(min_length=1, max_length=300)


class ExitDecision(BaseModel):
    action: Literal["CLOSE", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)


class ArenaMarketReview(BaseModel):
    regime: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=800)
    risk_flags: list[str] = Field(default_factory=list, max_length=12)


class ArenaSignalReview(BaseModel):
    approved_triggers: list[str] = Field(default_factory=list, max_length=12)
    blocked_triggers: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=1, max_length=800)


class ArenaDiagnosis(BaseModel):
    cards: list[dict[str, Any]] = Field(default_factory=list, max_length=8)
    summary: str = Field(min_length=1, max_length=800)


SchemaT = TypeVar("SchemaT", bound=BaseModel)


ENTRY_SYSTEM_PROMPT = """You are Layer 1 Brain for an OKX demo crypto futures trading system.
You are a disciplined quant scalper and risk manager. You evaluate only live market data and live external signals.

Principles:
- Protect capital and avoid chaotic/noisy markets.
- Prefer HOLD when trend, momentum, liquidity, and external signals conflict, but do not use recent losses
  as a blanket veto when the current setup is clearly different and passes risk controls.
- Use OKX technicals as the execution truth; use CoinMarketCap, Dune, and TradingView-derived signals as supporting context.
- Learn from recent executed demo-trade outcomes. Avoid repeating patterns with negative average PnL.
- Never invent missing data. If an external provider is empty, mark that in your reason and reduce confidence.
- High leverage can liquidate quickly. Keep stop-loss wider than ordinary one-minute noise and take-profit
  far enough to beat taker fees with positive fee-adjusted risk/reward.
- For OPEN_LONG or OPEN_SHORT, provide explicit stop_loss_pct, take_profit_pct, and risk_reward_ratio.
- Return one decision for every allowed symbol in decisions. Do not omit ETH/USDT.
- Evaluate each symbol independently. Do not default to BTC/USDT when ETH/USDT has the cleaner setup.
"""


EXIT_SYSTEM_PROMPT = """You are Layer 1 Brain for an OKX demo futures bot. Decide whether to close an already open position.
Use live OKX technicals and external signals. Prefer closing when trend invalidates, volatility expands against the position,
or the original setup has decayed. Return a strict structured decision."""


class GrokBrain:
    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        self._settings = settings
        self._logger = logger
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.xai_timeout_seconds),
            headers={
                "Authorization": f"Bearer {settings.xai_api_key}",
                "Content-Type": "application/json",
            },
        )
        self._rate_limited_until: float | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def entry_decision(self, payload: dict[str, Any]) -> EntryDecision | None:
        return await self._ask_grok(
            system_prompt=ENTRY_SYSTEM_PROMPT,
            payload=payload,
            schema=EntryDecision,
            reasoning_effort=self._settings.xai_reasoning_effort,
        )

    async def entry_decisions(self, payload: dict[str, Any]) -> list[EntryDecision] | None:
        response = await self._ask_grok(
            system_prompt=ENTRY_SYSTEM_PROMPT,
            payload=payload,
            schema=EntryDecisionBatch,
            reasoning_effort=self._settings.xai_reasoning_effort,
        )
        if response is None:
            return None
        return response.decisions

    async def pre_trade_check(self, payload: dict[str, Any]) -> PreTradeCheck | None:
        system_prompt = (
            "You are a skeptical pre-trade risk controller. Approve only if the planned OKX demo "
            "trade has clear live-market confirmations, realistic invalidation, and no obvious data gaps."
        )
        return await self._ask_grok(
            system_prompt=system_prompt,
            payload=payload,
            schema=PreTradeCheck,
            reasoning_effort="low",
        )

    async def exit_decision(self, payload: dict[str, Any]) -> ExitDecision | None:
        return await self._ask_grok(
            system_prompt=EXIT_SYSTEM_PROMPT,
            payload=payload,
            schema=ExitDecision,
            reasoning_effort="low",
        )

    async def arena_market_review(self, payload: dict[str, Any]) -> ArenaMarketReview | None:
        return await self._ask_grok(
            system_prompt="You are MarketAnalyzer. Review only supplied market/factor context and return concise JSON.",
            payload=payload,
            schema=ArenaMarketReview,
            reasoning_effort="low",
        )

    async def arena_signal_review(self, payload: dict[str, Any]) -> ArenaSignalReview | None:
        return await self._ask_grok(
            system_prompt="You are SignalDesigner. Approve only robust supplied market-flow triggers.",
            payload=payload,
            schema=ArenaSignalReview,
            reasoning_effort="low",
        )

    async def arena_diagnosis(self, payload: dict[str, Any]) -> ArenaDiagnosis | None:
        return await self._ask_grok(
            system_prompt="You are AttributionDiagnoser. Use only supplied realized metrics.",
            payload=payload,
            schema=ArenaDiagnosis,
            reasoning_effort="low",
        )

    async def _ask_grok(
        self,
        *,
        system_prompt: str,
        payload: dict[str, Any],
        schema: type[SchemaT],
        reasoning_effort: str,
    ) -> SchemaT | None:
        now_ts = time.time()
        if self._rate_limited_until is not None and now_ts < self._rate_limited_until:
            self._logger.warning(
                "Skipping Grok request due to recent xAI rate limit (%.0fs remaining).",
                self._rate_limited_until - now_ts,
            )
            return None

        body = {
            "model": self._settings.xai_model,
            "reasoning": {"effort": reasoning_effort},
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": True,
                }
            },
        }

        use_chat = str(getattr(self._settings, "xai_api_style", "responses")).strip().lower() == "chat"
        if use_chat:
            # OpenAI-compatible /chat/completions with json_object. The provider
            # doesn't support strict json_schema, so we embed the schema in the
            # system prompt and rely on pydantic validation + retry to enforce it.
            schema_hint = (
                "Respond ONLY with a single JSON object, no markdown, matching this JSON Schema:\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=True)
            )
            body = {
                "model": self._settings.xai_model,
                "messages": [
                    {"role": "system", "content": system_prompt + "\n\n" + schema_hint},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
                ],
                "response_format": {"type": "json_object"},
            }
            endpoint = "/chat/completions"
        else:
            endpoint = "/responses"

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._client.post(
                    f"{self._settings.xai_base_url.rstrip('/')}{endpoint}",
                    json=body,
                )
                response.raise_for_status()
                raw_text = self._extract_response_text(response.json())
                parsed = json.loads(raw_text)
                return schema.model_validate(parsed)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code if exc.response is not None else None
                if status == 429:
                    cooldown = 1800.0
                    self._rate_limited_until = time.time() + cooldown
                    self._logger.warning("xAI rate limited. Backing off for %.0fs.", cooldown)
                    return None
                body_text = exc.response.text[:500] if exc.response is not None else ""
                self._logger.warning(
                    "xAI HTTP error on attempt %d: status=%s body=%s",
                    attempt + 1,
                    status,
                    body_text,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                self._logger.warning(
                    "Grok request timed out on attempt %d after %ss.",
                    attempt + 1,
                    self._settings.xai_timeout_seconds,
                )
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError) as exc:
                last_error = exc
                self._logger.warning(
                    "Invalid Grok structured response on attempt %d: %s: %s",
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                )
            except Exception as exc:
                last_error = exc
                self._logger.warning(
                    "Grok request failed on attempt %d: %s: %s",
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                )
        if last_error is not None:
            self._logger.warning("Grok failed after retries: %s: %s", type(last_error).__name__, last_error)
        return None

    @staticmethod
    def _extract_response_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str):
            return str(payload["output_text"])
        for item in payload.get("output", []):
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []):
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return str(content["text"])
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            if isinstance(message.get("content"), str):
                return str(message["content"])
        raise KeyError("No text output found in xAI response")
