"""Provider-neutral AI call telemetry and retry classification.

One `AICallMetrics` record is produced per provider request so token spend,
latency, retries, and structured-response validity are measurable per call and
per day. Reporting on these records is observability-only: exceeding a daily
token amount must never stop trading.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

import httpx

# Rough provider-agnostic fallback used only when the provider does not report
# usage. Marked `estimated=True` everywhere it is surfaced.
_CHARS_PER_TOKEN = 4


class ErrorCategory(str, Enum):
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    AUTH = "auth"
    PERMANENT_CLIENT = "permanent_client"
    SCHEMA = "schema"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ErrorClassification:
    category: ErrorCategory
    retryable: bool
    repairable: bool = False


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False
    reasoning_tokens: int = 0
    cached_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "estimated": self.estimated,
        }


@dataclass(slots=True)
class AICallMetrics:
    purpose: str
    provider: str
    model: str
    api_style: str
    prompt_version: str
    reasoning_effort: str
    request_hash: str
    request_bytes: int
    response_bytes: int
    latency_ms: float
    attempts: int
    success: bool
    usage: TokenUsage | None = None
    error_category: str | None = None
    error_message: str | None = None
    deduplicated: bool = False
    repaired: bool = False
    input_cost_per_million: float | None = None
    output_cost_per_million: float | None = None
    exchange: str | None = None
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def estimated_cost_usd(self) -> float | None:
        if self.usage is None:
            return None
        if self.input_cost_per_million is None and self.output_cost_per_million is None:
            return None
        cost = 0.0
        if self.input_cost_per_million is not None:
            cost += (self.usage.input_tokens / 1_000_000) * self.input_cost_per_million
        if self.output_cost_per_million is not None:
            cost += (self.usage.output_tokens / 1_000_000) * self.output_cost_per_million
        return cost

    def to_record(self) -> dict[str, Any]:
        usage = self.usage or TokenUsage()
        return {
            "call_id": self.call_id,
            "created_at": self.created_at,
            "purpose": self.purpose,
            "provider": self.provider,
            "model": self.model,
            "api_style": self.api_style,
            "prompt_version": self.prompt_version,
            "reasoning_effort": self.reasoning_effort,
            "request_hash": self.request_hash,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "latency_ms": round(self.latency_ms, 3),
            "attempts": self.attempts,
            "success": self.success,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cached_tokens": usage.cached_tokens,
            "total_tokens": usage.total_tokens,
            "tokens_estimated": usage.estimated,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "deduplicated": self.deduplicated,
            "repaired": self.repaired,
            "estimated_cost_usd": self.estimated_cost_usd,
            "exchange": self.exchange,
        }


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


def parse_provider_usage(payload: dict[str, Any] | None) -> TokenUsage | None:
    """Parse usage from responses / chat-completions / messages dialects."""
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None

    input_tokens = _first_int(usage, ("input_tokens", "prompt_tokens"))
    output_tokens = _first_int(usage, ("output_tokens", "completion_tokens"))
    total = _first_int(usage, ("total_tokens",))
    if total == 0:
        total = input_tokens + output_tokens

    reasoning = _first_int(usage, ("reasoning_tokens",))
    details = usage.get("output_tokens_details")
    if reasoning == 0 and isinstance(details, dict):
        reasoning = _first_int(details, ("reasoning_tokens",))
    completion_details = usage.get("completion_tokens_details")
    if reasoning == 0 and isinstance(completion_details, dict):
        reasoning = _first_int(completion_details, ("reasoning_tokens",))

    cached = _first_int(usage, ("cache_read_input_tokens", "cached_tokens"))
    for key in ("input_tokens_details", "prompt_tokens_details"):
        block = usage.get(key)
        if cached == 0 and isinstance(block, dict):
            cached = _first_int(block, ("cached_tokens",))

    if input_tokens == 0 and output_tokens == 0 and total == 0:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        estimated=False,
        reasoning_tokens=reasoning,
        cached_tokens=cached,
    )


def classify_provider_error(exc: BaseException, *, schema_violation: bool = False) -> ErrorClassification:
    """Retry only transient transport failures; fail fast on permanent errors."""
    if schema_violation:
        # A structurally invalid response gets ONE repair request carrying the
        # validation problem — never a blind resend of the same large payload.
        return ErrorClassification(ErrorCategory.SCHEMA, retryable=False, repairable=True)
    if isinstance(exc, httpx.TimeoutException):
        return ErrorClassification(ErrorCategory.TIMEOUT, retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else None
        if status == 429:
            return ErrorClassification(ErrorCategory.RATE_LIMIT, retryable=True)
        if status in {401, 403}:
            return ErrorClassification(ErrorCategory.AUTH, retryable=False)
        if status is not None and 400 <= status < 500:
            if status in {408, 409, 425}:
                return ErrorClassification(ErrorCategory.SERVER, retryable=True)
            return ErrorClassification(ErrorCategory.PERMANENT_CLIENT, retryable=False)
        if status is not None and status >= 500:
            return ErrorClassification(ErrorCategory.SERVER, retryable=True)
        return ErrorClassification(ErrorCategory.UNKNOWN, retryable=False)
    if isinstance(exc, httpx.TransportError):
        return ErrorClassification(ErrorCategory.CONNECTION, retryable=True)
    return ErrorClassification(ErrorCategory.UNKNOWN, retryable=False)


def summarize_metrics(metrics: Iterable[AICallMetrics | dict[str, Any]]) -> dict[str, Any]:
    """Aggregate call records for the usage report (observability only)."""
    rows = [item.to_record() if isinstance(item, AICallMetrics) else dict(item) for item in metrics]
    totals = {
        "calls": len(rows),
        "successes": 0,
        "failures": 0,
        "attempts": 0,
        "retried_calls": 0,
        "repaired_calls": 0,
        "deduplicated_calls": 0,
        "estimated_token_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
    }
    by_purpose: dict[str, dict[str, Any]] = {}
    errors: dict[str, int] = {}

    for row in rows:
        purpose = str(row.get("purpose") or "unknown")
        bucket = by_purpose.setdefault(
            purpose,
            {"calls": 0, "successes": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        bucket["calls"] += 1
        totals["attempts"] += int(row.get("attempts") or 0)
        if int(row.get("attempts") or 0) > 1:
            totals["retried_calls"] += 1
        if row.get("repaired"):
            totals["repaired_calls"] += 1
        if row.get("deduplicated"):
            totals["deduplicated_calls"] += 1
        if row.get("tokens_estimated"):
            totals["estimated_token_calls"] += 1
        if row.get("success"):
            totals["successes"] += 1
            bucket["successes"] += 1
        else:
            totals["failures"] += 1
            bucket["failures"] += 1
            category = str(row.get("error_category") or "unknown")
            errors[category] = errors.get(category, 0) + 1
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = int(row.get(key) or 0)
            totals[key] += value
            bucket[key] += value
        totals["reasoning_tokens"] += int(row.get("reasoning_tokens") or 0)
        totals["cached_tokens"] += int(row.get("cached_tokens") or 0)
        cost = row.get("estimated_cost_usd")
        if cost:
            totals["estimated_cost_usd"] += float(cost)

    totals["success_rate_pct"] = (totals["successes"] / totals["calls"] * 100.0) if totals["calls"] else 0.0
    totals["estimated_cost_usd"] = round(totals["estimated_cost_usd"], 6)
    return {"totals": totals, "by_purpose": by_purpose, "errors": errors}


def _first_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0
