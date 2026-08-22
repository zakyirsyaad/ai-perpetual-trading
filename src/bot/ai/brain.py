from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Literal, TypeVar

import httpx
from pydantic import BaseModel, Field, ValidationError, ValidationInfo, field_validator

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

    @field_validator("confirmations", "invalidation_trigger", mode="before")
    @classmethod
    def _clamp_length(cls, value: Any, info: ValidationInfo) -> Any:
        # The chat-mode provider cannot enforce maxLength server-side, so a verbose
        # but otherwise valid approval must not be discarded on a length technicality
        # (which previously turned guard-passed entries into "no response" rejections).
        if not isinstance(value, str):
            return value
        limit = 500 if info.field_name == "confirmations" else 300
        return value[:limit]


class ExitDecision(BaseModel):
    action: Literal["CLOSE", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason", mode="before")
    @classmethod
    def _clamp_reason(cls, value: Any) -> Any:
        # Same chat-mode length guard as PreTradeCheck: a verbose-but-valid exit
        # decision must not raise ValidationError -> None and strand an open position
        # on the time-stop fallback.
        if not isinstance(value, str):
            return value
        return value[:500]


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
You are a decisive quant scalper running a volume-building mandate. You evaluate only live market data and live external signals.

Principles:
- This is a volume-building run on OKX demo, but repeated bad decisions destroy net PnL.
- Use recent_executed_trade_performance and learned_trade_memory as direct feedback. Avoid repeating patterns that
  recently ended in stop_loss or liquidation_guard, and favor patterns that recently produced positive net exits.
- HOLD is valid when no allowed symbol has enough edge for the expected gross move to beat round-trip taker fees
  plus a buffer. Prefer fewer cleaner entries over forced entries.
- Use OKX technicals as the execution truth; CoinMarketCap, Dune, and TradingView are OPTIONAL supporting context.
- Never invent missing data. If an optional external provider is empty, note it in your reason, but do not
  reduce confidence on that basis — the OKX live technicals are sufficient to act.
- High leverage can liquidate quickly. Keep stop-loss wider than ordinary one-minute noise and take-profit
  far enough to beat taker fees.
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
        # The Anthropic-style /messages dialect authenticates with x-api-key +
        # anthropic-version instead of a Bearer token. Send both so a single client
        # works across "responses"/"chat" (Bearer) and "messages" (x-api-key) providers.
        _headers = {
            "Authorization": f"Bearer {settings.xai_api_key}",
            "Content-Type": "application/json",
        }
        if str(getattr(settings, "xai_api_style", "")).strip().lower() == "messages":
            _headers["x-api-key"] = settings.xai_api_key or ""
            _headers["anthropic-version"] = "2023-06-01"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.xai_timeout_seconds),
            headers=_headers,
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
                "Skipping AI provider request due to recent rate limit "
                "(remaining_seconds=%.0f model=%s).",
                self._rate_limited_until - now_ts,
                self._settings.xai_model,
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

        style = str(getattr(self._settings, "xai_api_style", "responses")).strip().lower()
        if style in {"chat", "messages"}:
            # Neither dialect supports strict json_schema, so embed the schema in the
            # system prompt and rely on pydantic validation + retry to enforce it.
            schema_hint = (
                "Respond ONLY with a single JSON object, no markdown, matching this JSON Schema:\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=True)
            )
            system_full = system_prompt + "\n\n" + schema_hint
            if style == "messages":
                # Anthropic-style /messages: top-level `system`, mandatory `max_tokens`.
                # Reasoning models (e.g. deepseek-v4-flash) also spend max_tokens on a
                # thinking block, which _extract_response_text skips.
                body = {
                    "model": self._settings.xai_model,
                    "max_tokens": int(getattr(self._settings, "xai_max_tokens", 4096)),
                    "system": system_full,
                    "messages": [
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
                    ],
                    # Some gateways default to SSE streaming; force a single JSON body
                    # so response.json() can parse it.
                    "stream": False,
                }
                endpoint = "/messages"
            else:
                # OpenAI-compatible /chat/completions with json_object.
                body = {
                    "model": self._settings.xai_model,
                    "max_tokens": int(getattr(self._settings, "xai_max_tokens", 4096)),
                    "messages": [
                        {"role": "system", "content": system_full},
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
                    ],
                    "response_format": {"type": "json_object"},
                    # Some gateways default to SSE streaming; force a single JSON body
                    # so response.json() can parse it.
                    "stream": False,
                }
                endpoint = "/chat/completions"
        else:
            endpoint = "/responses"

        last_error: Exception | None = None
        max_attempts = 3
        for attempt in range(max_attempts):
            raw_text = ""
            try:
                response = await self._client.post(
                    f"{self._settings.xai_base_url.rstrip('/')}{endpoint}",
                    json=body,
                )
                response.raise_for_status()
                raw_text = self._extract_response_text(response.json())
                parsed = self._load_json_object(raw_text)
                return schema.model_validate(parsed)
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code if exc.response is not None else None
                if status == 429:
                    retry_after = exc.response.headers.get("Retry-After") if exc.response is not None else None
                    cooldown = self._retry_after_seconds(retry_after) or min(2.0 * (attempt + 1), 10.0)
                    if cooldown > 10.0:
                        self._rate_limited_until = time.time() + cooldown
                        self._logger.warning(
                            "AI provider rate limited: status=429 retry_after=%s "
                            "cooldown_seconds=%.0f endpoint=%s model=%s body=%s",
                            retry_after,
                            cooldown,
                            endpoint,
                            self._settings.xai_model,
                            self._response_body_preview(exc.response),
                        )
                        return None
                    if attempt < max_attempts - 1:
                        self._logger.warning(
                            "AI provider rate limited on attempt %d: status=429 retry_after=%s "
                            "retrying_after_seconds=%.0f endpoint=%s model=%s body=%s",
                            attempt + 1,
                            retry_after,
                            cooldown,
                            endpoint,
                            self._settings.xai_model,
                            self._response_body_preview(exc.response),
                        )
                        await asyncio.sleep(cooldown)
                        continue
                    self._rate_limited_until = time.time() + cooldown
                    self._logger.warning(
                        "AI provider rate limited: status=429 retry_after=%s "
                        "cooldown_seconds=%.0f endpoint=%s model=%s body=%s",
                        retry_after,
                        cooldown,
                        endpoint,
                        self._settings.xai_model,
                        self._response_body_preview(exc.response),
                    )
                    return None
                self._logger.warning(
                    "AI provider HTTP error on attempt %d: status=%s endpoint=%s model=%s body=%s",
                    attempt + 1,
                    status,
                    endpoint,
                    self._settings.xai_model,
                    self._response_body_preview(exc.response),
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                self._logger.warning(
                    "AI provider request timed out on attempt %d after %ss "
                    "(endpoint=%s model=%s).",
                    attempt + 1,
                    self._settings.xai_timeout_seconds,
                    endpoint,
                    self._settings.xai_model,
                )
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError) as exc:
                last_error = exc
                self._logger.warning(
                    "Invalid AI provider structured response on attempt %d: %s: %s "
                    "(endpoint=%s model=%s raw_preview=%s).",
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                    endpoint,
                    self._settings.xai_model,
                    raw_text[:300],
                )
            except Exception as exc:
                last_error = exc
                self._logger.warning(
                    "AI provider request failed on attempt %d: %s: %s "
                    "(endpoint=%s model=%s).",
                    attempt + 1,
                    type(exc).__name__,
                    exc,
                    endpoint,
                    self._settings.xai_model,
                )
        if last_error is not None:
            self._logger.warning(
                "AI provider failed after retries: %s: %s (endpoint=%s model=%s)",
                type(last_error).__name__,
                last_error,
                endpoint,
                self._settings.xai_model,
            )
        return None

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value.strip())
        except ValueError:
            return None
        return seconds if seconds > 0 else None

    @staticmethod
    def _response_body_preview(response: httpx.Response | None) -> str:
        if response is None:
            return ""
        return response.text[:500]

    @staticmethod
    def _load_json_object(text: str) -> dict[str, Any]:
        stripped = text.strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            if stripped.startswith("```"):
                stripped = stripped.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = GrokBrain._load_embedded_json_object(text)
            else:
                parsed = GrokBrain._load_embedded_json_object(text)
        if not isinstance(parsed, dict):
            raise TypeError("AI provider response JSON must be an object")
        return parsed

    @staticmethod
    def _load_embedded_json_object(text: str) -> dict[str, Any]:
        decoder = json.JSONDecoder()
        stripped = text.strip()
        for index, char in enumerate(stripped):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        raise json.JSONDecodeError("No JSON object found in AI provider response", text, 0)

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
        # Anthropic-style /messages: top-level content[] with text/thinking blocks.
        # Reasoning models emit a "thinking" block first; take the first "text" block.
        top_content = payload.get("content")
        if isinstance(top_content, list):
            for content in top_content:
                if not isinstance(content, dict):
                    continue
                if content.get("type") == "text" and isinstance(content.get("text"), str):
                    return str(content["text"])
        raise KeyError("No text output found in xAI response")
