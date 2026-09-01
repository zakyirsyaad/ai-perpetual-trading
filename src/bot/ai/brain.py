from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Literal, TypeVar

import httpx
from pydantic import BaseModel, Field, ValidationError, ValidationInfo, field_validator

from bot.ai.compact_context import COMPACT_PROMPT_VERSION, compact_json, payload_hash
from bot.ai.usage import (
    AICallMetrics,
    TokenUsage,
    classify_provider_error,
    estimate_tokens,
    parse_provider_usage,
)
from bot.config import Settings


class RiskReview(BaseModel):
    """Skeptical risk confirmation, merged into the entry decision.

    This replaces the separate `PreTradeCheck` provider call. `approved=false`
    vetoes the candidate; a missing or structurally invalid review makes the
    candidate unusable (see `AIOrchestrator._build_entry_candidate`) rather than
    silently inventing approval.
    """

    approved: bool
    confirmations: str = Field(min_length=1, max_length=500)
    invalidation_trigger: str = Field(min_length=1, max_length=300)

    @field_validator("confirmations", "invalidation_trigger", mode="before")
    @classmethod
    def _clamp_length(cls, value: Any, info: ValidationInfo) -> Any:
        # Chat/messages providers cannot enforce maxLength server-side, so a
        # verbose but otherwise valid review must not be discarded on a length
        # technicality.
        if not isinstance(value, str):
            return value
        limit = 500 if info.field_name == "confirmations" else 300
        return value[:limit]


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
    risk_review: RiskReview | None = Field(
        default=None,
        description="Mandatory for OPEN_LONG/OPEN_SHORT: nested skeptical risk confirmation.",
    )
    learning_note: str = Field(default="", max_length=500)

    @field_validator("risk_review", mode="before")
    @classmethod
    def _drop_invalid_risk_review(cls, value: Any) -> Any:
        # A malformed review for ONE symbol must not invalidate the whole batch;
        # it becomes None, which the orchestrator treats as "unusable candidate".
        if value in (None, "", {}):
            return None
        if isinstance(value, RiskReview):
            return value
        if not isinstance(value, dict):
            return None
        try:
            return RiskReview.model_validate(value)
        except ValidationError:
            return None


class EntryDecisionBatch(BaseModel):
    decisions: list[EntryDecision] = Field(
        min_length=1,
        description="One independent entry decision for each allowed symbol.",
    )


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


ENTRY_SYSTEM_PROMPT = """You are Layer 1 Brain for a BingX VST perpetual-futures trading system.
You are a decisive quant scalper running a volume-building mandate. You evaluate only live market data and live external signals.

Principles:
- This is a volume-building run on BingX VST, but repeated bad decisions destroy net PnL.
- Use recent_performance and trade_memory as direct feedback. Avoid repeating patterns that recently ended in
  stop_loss or liquidation_guard, and favor patterns that recently produced positive net exits.
- HOLD is valid when no allowed symbol has enough edge for the expected gross move to beat round-trip taker fees
  plus a buffer. Prefer fewer cleaner entries over forced entries.
- Exchange technicals, factors, flow signals, and liquidation_constraints are the execution truth; CoinMarketCap, Dune, and TradingView are
  OPTIONAL supporting context. An external provider that is absent from external_signals is simply unavailable:
  note it in your reason, but do not reduce confidence on that basis.
- High leverage can liquidate quickly. Keep stop-loss wider than ordinary one-minute noise and take-profit
  far enough to beat taker fees.
- For OPEN_LONG or OPEN_SHORT, provide explicit stop_loss_pct and take_profit_pct.
- For OPEN_LONG or OPEN_SHORT, the final stop_loss_pct must be strictly smaller than that direction's
  max_*_stop_loss_pct in market_context.liquidation_constraints. If the constraint is unavailable or the stop
  would reach/exceed estimated liquidation, return HOLD.
- For OPEN_LONG or OPEN_SHORT you MUST also return risk_review with approved, confirmations, and
  invalidation_trigger. Act as your own skeptical pre-trade risk controller: set approved=false when the live
  confirmations are weak, the invalidation is unrealistic, or there is an obvious data gap. An omitted or
  malformed risk_review makes the candidate unusable — it is never treated as approval.
- Return one decision for every allowed symbol in decisions. Do not omit any allowed symbol.
- Evaluate each symbol independently. Do not default to the first symbol when the second has the cleaner setup.
- Keep every free-text field short. Long prose costs tokens and adds no decision value.
"""


EXIT_SYSTEM_PROMPT = """You are Layer 1 Brain for a BingX VST perpetual-futures bot. Decide whether to close an
already open position. Use the supplied one-symbol market context, including live technicals, factors, and flow signals. Prefer closing when the
trend invalidates, volatility expands against the position, or the original setup has decayed. Deterministic stop-loss,
take-profit, and time-stop protection runs independently every few seconds, so only call for a discretionary close.
Return a strict structured decision and keep the reason short."""


REPAIR_SYSTEM_PROMPT = """Your previous response did not satisfy the required JSON contract.
Return ONLY a corrected single JSON object matching the schema. Do not explain."""


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
        self._metrics_sink: Callable[[AICallMetrics], Awaitable[None]] | None = None
        self.last_call_metrics: AICallMetrics | None = None

    def set_metrics_sink(self, sink: Callable[[AICallMetrics], Awaitable[None]] | None) -> None:
        """Register the coroutine that persists one record per provider request."""
        self._metrics_sink = sink

    async def close(self) -> None:
        await self._client.aclose()

    async def entry_decision(self, payload: dict[str, Any]) -> EntryDecision | None:
        return await self._ask_grok(
            system_prompt=ENTRY_SYSTEM_PROMPT,
            payload=payload,
            schema=EntryDecision,
            reasoning_effort=self._settings.xai_reasoning_effort,
            purpose="entry",
        )

    async def entry_decisions(self, payload: dict[str, Any]) -> list[EntryDecision] | None:
        response = await self._ask_grok(
            system_prompt=ENTRY_SYSTEM_PROMPT,
            payload=payload,
            schema=EntryDecisionBatch,
            reasoning_effort=self._settings.xai_reasoning_effort,
            purpose="entry",
        )
        if response is None:
            return None
        return response.decisions

    async def exit_decision(self, payload: dict[str, Any]) -> ExitDecision | None:
        return await self._ask_grok(
            system_prompt=EXIT_SYSTEM_PROMPT,
            payload=payload,
            schema=ExitDecision,
            reasoning_effort="low",
            purpose="exit",
        )

    async def arena_market_review(self, payload: dict[str, Any]) -> ArenaMarketReview | None:
        return await self._ask_grok(
            system_prompt="You are MarketAnalyzer. Review only supplied market/factor context and return concise JSON.",
            payload=payload,
            schema=ArenaMarketReview,
            reasoning_effort="low",
            purpose="arena_review",
        )

    async def arena_signal_review(self, payload: dict[str, Any]) -> ArenaSignalReview | None:
        return await self._ask_grok(
            system_prompt="You are SignalDesigner. Approve only robust supplied market-flow triggers.",
            payload=payload,
            schema=ArenaSignalReview,
            reasoning_effort="low",
            purpose="arena_review",
        )

    async def arena_diagnosis(self, payload: dict[str, Any]) -> ArenaDiagnosis | None:
        return await self._ask_grok(
            system_prompt="You are AttributionDiagnoser. Use only supplied realized metrics.",
            payload=payload,
            schema=ArenaDiagnosis,
            reasoning_effort="low",
            purpose="arena_review",
        )

    async def _ask_grok(
        self,
        *,
        system_prompt: str,
        payload: dict[str, Any],
        schema: type[SchemaT],
        reasoning_effort: str,
        purpose: str = "unknown",
        deduplicated: bool = False,
    ) -> SchemaT | None:
        """One structured provider request, with telemetry and a strict retry policy.

        Retries only connection failures, timeouts, short 429s, and retryable 5xx.
        Authentication, authorization, validation, and other permanent 4xx errors
        fail immediately. A structurally invalid response gets at most ONE repair
        request carrying only the validation problem and the compact contract —
        the same large payload is never resent three times.
        """
        now_ts = time.time()
        request_hash = payload_hash(payload)
        started = time.perf_counter()
        if self._rate_limited_until is not None and now_ts < self._rate_limited_until:
            self._logger.warning(
                "Skipping AI provider request due to recent rate limit "
                "(remaining_seconds=%.0f model=%s).",
                self._rate_limited_until - now_ts,
                self._settings.xai_model,
            )
            await self._record_metrics(
                purpose=purpose,
                reasoning_effort=reasoning_effort,
                request_hash=request_hash,
                request_bytes=0,
                response_bytes=0,
                started=started,
                attempts=0,
                success=False,
                usage=None,
                error_category="rate_limit",
                error_message="skipped due to active rate-limit cooldown",
                repaired=False,
                deduplicated=deduplicated,
            )
            return None

        body, endpoint = self._build_request_body(
            system_prompt=system_prompt,
            payload=payload,
            schema=schema,
            reasoning_effort=reasoning_effort,
        )

        attempts = 0
        repaired = False
        repair_used = False
        # Accumulated across every attempt: a retry or repair genuinely spends the
        # tokens and bytes of each attempt, so recording only the last one would
        # understate real cost.
        request_bytes_total = 0
        response_bytes = 0
        usage: TokenUsage | None = None
        last_error: Exception | None = None
        last_category = "unknown"
        last_raw_text = ""
        max_transport_attempts = 3

        while attempts < max_transport_attempts:
            attempts += 1
            request_bytes_total += len(compact_json(body))
            # A dispatched attempt may already have been billed by the provider
            # even if we never see a usable response (a client timeout does not
            # prove inference did not run). Assume it was, conservatively, and
            # clear the flag only once the real response has been accounted.
            attempt_accounted = False
            raw_text = ""
            try:
                response = await self._client.post(
                    f"{self._settings.xai_base_url.rstrip('/')}{endpoint}",
                    json=body,
                )
                response.raise_for_status()
                response_payload = response.json()
                response_bytes += len(compact_json(response_payload))
                raw_text, extraction_error = self._extract_response_text_safe(response_payload)
                # Account for THIS attempt before anything can raise: an attempt
                # that produced an unusable response still spent its tokens.
                usage = self._add_attempt_usage(usage, response_payload, body, raw_text)
                attempt_accounted = True
                if extraction_error is not None:
                    raise extraction_error
                last_raw_text = raw_text
                parsed = self._load_json_object(raw_text)
                result = schema.model_validate(parsed)
                await self._record_metrics(
                    purpose=purpose,
                    reasoning_effort=reasoning_effort,
                    request_hash=request_hash,
                    request_bytes=request_bytes_total,
                    response_bytes=response_bytes,
                    started=started,
                    attempts=attempts,
                    success=True,
                    usage=usage if usage is not None else TokenUsage(estimated=True),
                    error_category=None,
                    error_message=None,
                    repaired=repaired,
                    deduplicated=deduplicated,
                )
                return result
            except (json.JSONDecodeError, ValidationError, KeyError, TypeError) as exc:
                last_error = exc
                if not attempt_accounted:
                    usage = self._add_dispatched_attempt_usage(usage, body)
                classification = classify_provider_error(exc, schema_violation=True)
                last_category = classification.category.value
                self._logger.warning(
                    "Invalid AI provider structured response on attempt %d: %s: %s "
                    "(endpoint=%s model=%s raw_preview=%s).",
                    attempts,
                    type(exc).__name__,
                    exc,
                    endpoint,
                    self._settings.xai_model,
                    raw_text[:300],
                )
                if repair_used:
                    break
                repair_used = True
                repaired = True
                body = self._build_repair_body(
                    schema=schema,
                    validation_error=str(exc)[:600],
                    invalid_output=last_raw_text[:600],
                    endpoint=endpoint,
                )
                continue
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if not attempt_accounted:
                    usage = self._add_dispatched_attempt_usage(usage, body)
                classification = classify_provider_error(exc)
                last_category = classification.category.value
                status = exc.response.status_code if exc.response is not None else None
                if status == 429:
                    handled = await self._handle_rate_limit(exc, attempts, max_transport_attempts, endpoint)
                    if handled:
                        continue
                    break
                self._logger.warning(
                    "AI provider HTTP error on attempt %d: status=%s endpoint=%s model=%s body=%s",
                    attempts,
                    status,
                    endpoint,
                    self._settings.xai_model,
                    self._response_body_preview(exc.response),
                )
                if not classification.retryable:
                    # Permanent 4xx (auth/authorization/validation): resending the
                    # same request cannot succeed and only burns tokens.
                    break
            except Exception as exc:
                last_error = exc
                if not attempt_accounted:
                    usage = self._add_dispatched_attempt_usage(usage, body)
                classification = classify_provider_error(exc)
                last_category = classification.category.value
                self._logger.warning(
                    "AI provider request failed on attempt %d: %s: %s "
                    "(endpoint=%s model=%s).",
                    attempts,
                    type(exc).__name__,
                    exc,
                    endpoint,
                    self._settings.xai_model,
                )
                if not classification.retryable:
                    break

        if last_error is not None:
            self._logger.warning(
                "AI provider failed after retries: %s: %s (endpoint=%s model=%s)",
                type(last_error).__name__,
                last_error,
                endpoint,
                self._settings.xai_model,
            )
        await self._record_metrics(
            purpose=purpose,
            reasoning_effort=reasoning_effort,
            request_hash=request_hash,
            request_bytes=request_bytes_total,
            response_bytes=response_bytes,
            started=started,
            attempts=attempts,
            success=False,
            usage=usage,
            error_category=last_category,
            error_message=str(last_error)[:400] if last_error is not None else None,
            repaired=repaired,
            deduplicated=deduplicated,
        )
        return None

    async def _handle_rate_limit(
        self,
        exc: httpx.HTTPStatusError,
        attempt: int,
        max_attempts: int,
        endpoint: str,
    ) -> bool:
        """Return True when the caller should retry, False to give up."""
        retry_after = exc.response.headers.get("Retry-After") if exc.response is not None else None
        cooldown = self._retry_after_seconds(retry_after) or min(2.0 * attempt, 10.0)
        if cooldown > 10.0 or attempt >= max_attempts:
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
            return False
        self._logger.warning(
            "AI provider rate limited on attempt %d: status=429 retry_after=%s "
            "retrying_after_seconds=%.0f endpoint=%s model=%s body=%s",
            attempt,
            retry_after,
            cooldown,
            endpoint,
            self._settings.xai_model,
            self._response_body_preview(exc.response),
        )
        await asyncio.sleep(cooldown)
        return True

    def _build_request_body(
        self,
        *,
        system_prompt: str,
        payload: dict[str, Any],
        schema: type[SchemaT],
        reasoning_effort: str,
    ) -> tuple[dict[str, Any], str]:
        user_content = compact_json(payload)
        style = str(getattr(self._settings, "xai_api_style", "responses")).strip().lower()
        if style not in {"chat", "messages"}:
            return (
                {
                    "model": self._settings.xai_model,
                    "reasoning": {"effort": reasoning_effort},
                    "input": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": schema.__name__,
                            "schema": schema.model_json_schema(),
                            "strict": True,
                        }
                    },
                },
                "/responses",
            )

        # Neither dialect supports strict json_schema, so embed the schema in the
        # system prompt and rely on pydantic validation + one repair to enforce it.
        system_full = system_prompt + "\n\n" + self._schema_hint(schema)
        max_tokens = int(getattr(self._settings, "xai_max_tokens", 4096))
        if style == "messages":
            # Anthropic-style /messages: top-level `system`, mandatory `max_tokens`.
            # Reasoning models also spend max_tokens on a thinking block, which
            # _extract_response_text skips.
            return (
                {
                    "model": self._settings.xai_model,
                    "max_tokens": max_tokens,
                    "system": system_full,
                    "messages": [{"role": "user", "content": user_content}],
                    # Some gateways default to SSE streaming; force a single JSON body.
                    "stream": False,
                },
                "/messages",
            )
        return (
            {
                "model": self._settings.xai_model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_full},
                    {"role": "user", "content": user_content},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
            },
            "/chat/completions",
        )

    def _build_repair_body(
        self,
        *,
        schema: type[SchemaT],
        validation_error: str,
        invalid_output: str,
        endpoint: str,
    ) -> dict[str, Any]:
        """Corrective request carrying ONLY the problem and the contract.

        Deliberately omits the original market payload: resending it would cost
        the full input tokens again for a response the model already produced.
        """
        repair_user = compact_json(
            {
                "validation_error": validation_error,
                "invalid_output_preview": invalid_output,
                "instruction": "Return only the corrected JSON object for the schema in the system prompt.",
            }
        )
        system_full = REPAIR_SYSTEM_PROMPT + "\n\n" + self._schema_hint(schema)
        max_tokens = int(getattr(self._settings, "xai_max_tokens", 4096))
        if endpoint == "/messages":
            return {
                "model": self._settings.xai_model,
                "max_tokens": max_tokens,
                "system": system_full,
                "messages": [{"role": "user", "content": repair_user}],
                "stream": False,
            }
        if endpoint == "/chat/completions":
            return {
                "model": self._settings.xai_model,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system_full},
                    {"role": "user", "content": repair_user},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
            }
        return {
            "model": self._settings.xai_model,
            "reasoning": {"effort": "low"},
            "input": [
                {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
                {"role": "user", "content": repair_user},
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

    @staticmethod
    def _schema_hint(schema: type[SchemaT]) -> str:
        return (
            "Respond ONLY with a single JSON object, no markdown, matching this JSON Schema:\n"
            + compact_json(schema.model_json_schema())
        )

    @classmethod
    def _add_attempt_usage(
        cls,
        running: TokenUsage | None,
        response_payload: dict[str, Any],
        body: dict[str, Any],
        raw_text: str,
    ) -> TokenUsage:
        """Fold one attempt's spend into the running total for this request.

        Uses the provider's own numbers when present and an explicitly-estimated
        figure otherwise — per attempt, not once at the end. Estimating only the
        final attempt made a large invalid response followed by a small repair
        look like the cost of the repair alone.
        """
        attempt = parse_provider_usage(response_payload)
        if attempt is None:
            attempt = cls._estimated_usage(body, raw_text)
        return attempt if running is None else running + attempt

    @classmethod
    def _add_dispatched_attempt_usage(
        cls,
        running: TokenUsage | None,
        body: dict[str, Any],
    ) -> TokenUsage:
        """Conservatively bill a dispatched attempt with an unknown outcome.

        The request left this process, so the provider may well have run and
        charged for it. Output length is genuinely unknown, so only the input is
        estimated; the result is always flagged estimated.
        """
        input_tokens = estimate_tokens(compact_json(body))
        attempt = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=0,
            total_tokens=input_tokens,
            estimated=True,
        )
        return attempt if running is None else running + attempt

    @staticmethod
    def _extract_response_text_safe(payload: dict[str, Any]) -> tuple[str, Exception | None]:
        """Extract output text without raising, so accounting can run first."""
        try:
            return GrokBrain._extract_response_text(payload), None
        except (KeyError, TypeError) as exc:
            return "", exc

    @staticmethod
    def _estimated_usage(body: dict[str, Any], raw_text: str) -> TokenUsage:
        """Fallback when the provider does not report usage. Always marked estimated."""
        input_tokens = estimate_tokens(compact_json(body))
        output_tokens = estimate_tokens(raw_text)
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            estimated=True,
        )

    async def _record_metrics(
        self,
        *,
        purpose: str,
        reasoning_effort: str,
        request_hash: str,
        request_bytes: int,
        response_bytes: int,
        started: float,
        attempts: int,
        success: bool,
        usage: TokenUsage | None,
        error_category: str | None,
        error_message: str | None,
        repaired: bool,
        deduplicated: bool,
    ) -> None:
        metrics = AICallMetrics(
            purpose=purpose,
            provider=str(self._settings.xai_base_url),
            model=str(self._settings.xai_model),
            api_style=str(getattr(self._settings, "xai_api_style", "responses")),
            prompt_version=COMPACT_PROMPT_VERSION,
            reasoning_effort=reasoning_effort,
            request_hash=request_hash,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            attempts=attempts,
            success=success,
            usage=usage,
            error_category=error_category,
            error_message=error_message,
            repaired=repaired,
            deduplicated=deduplicated,
            input_cost_per_million=_optional_float(
                getattr(self._settings, "ai_input_cost_per_million_tokens", None)
            ),
            output_cost_per_million=_optional_float(
                getattr(self._settings, "ai_output_cost_per_million_tokens", None)
            ),
            exchange=str(getattr(self._settings, "exchange_name", "") or "") or None,
        )
        self.last_call_metrics = metrics
        if self._metrics_sink is None:
            return
        try:
            await self._metrics_sink(metrics)
        except Exception as exc:  # telemetry must never break trading
            self._logger.warning("Could not persist AI call metrics: %s: %s", type(exc).__name__, exc)

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


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
