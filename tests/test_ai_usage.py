from __future__ import annotations

import unittest

import httpx

from bot.ai.usage import (
    AICallMetrics,
    ErrorCategory,
    TokenUsage,
    classify_provider_error,
    estimate_tokens,
    parse_provider_usage,
    summarize_metrics,
)


class TokenEstimationTests(unittest.TestCase):
    def test_estimate_tokens_is_roughly_four_chars_per_token(self) -> None:
        self.assertEqual(estimate_tokens("a" * 400), 100)

    def test_empty_text_is_zero(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)


class UsageParsingTests(unittest.TestCase):
    def test_chat_completions_usage(self) -> None:
        usage = parse_provider_usage(
            {"usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}}
        )
        self.assertEqual(usage, TokenUsage(input_tokens=1200, output_tokens=300, total_tokens=1500, estimated=False))

    def test_responses_dialect_usage(self) -> None:
        usage = parse_provider_usage(
            {
                "usage": {
                    "input_tokens": 900,
                    "output_tokens": 120,
                    "total_tokens": 1020,
                    "output_tokens_details": {"reasoning_tokens": 80},
                    "input_tokens_details": {"cached_tokens": 400},
                }
            }
        )
        self.assertEqual(usage.input_tokens, 900)
        self.assertEqual(usage.reasoning_tokens, 80)
        self.assertEqual(usage.cached_tokens, 400)
        self.assertFalse(usage.estimated)

    def test_messages_dialect_usage(self) -> None:
        usage = parse_provider_usage(
            {
                "usage": {
                    "input_tokens": 500,
                    "output_tokens": 60,
                    "cache_read_input_tokens": 250,
                }
            }
        )
        self.assertEqual(usage.input_tokens, 500)
        self.assertEqual(usage.output_tokens, 60)
        self.assertEqual(usage.cached_tokens, 250)
        self.assertEqual(usage.total_tokens, 560)

    def test_missing_usage_returns_none(self) -> None:
        self.assertIsNone(parse_provider_usage({"choices": []}))

    def test_prompt_tokens_details_cached(self) -> None:
        usage = parse_provider_usage(
            {"usage": {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": {"cached_tokens": 4}}}
        )
        self.assertEqual(usage.cached_tokens, 4)


class ErrorClassificationTests(unittest.TestCase):
    def _status_error(self, status: int) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
        response = httpx.Response(status, request=request, text="{}")
        return httpx.HTTPStatusError("err", request=request, response=response)

    def test_timeout_is_retryable(self) -> None:
        result = classify_provider_error(httpx.ReadTimeout("slow"))
        self.assertEqual(result.category, ErrorCategory.TIMEOUT)
        self.assertTrue(result.retryable)

    def test_connection_error_is_retryable(self) -> None:
        result = classify_provider_error(httpx.ConnectError("down"))
        self.assertEqual(result.category, ErrorCategory.CONNECTION)
        self.assertTrue(result.retryable)

    def test_rate_limit_is_retryable(self) -> None:
        result = classify_provider_error(self._status_error(429))
        self.assertEqual(result.category, ErrorCategory.RATE_LIMIT)
        self.assertTrue(result.retryable)

    def test_server_error_is_retryable(self) -> None:
        result = classify_provider_error(self._status_error(503))
        self.assertEqual(result.category, ErrorCategory.SERVER)
        self.assertTrue(result.retryable)

    def test_auth_error_fails_fast(self) -> None:
        result = classify_provider_error(self._status_error(401))
        self.assertEqual(result.category, ErrorCategory.AUTH)
        self.assertFalse(result.retryable)

    def test_forbidden_fails_fast(self) -> None:
        self.assertFalse(classify_provider_error(self._status_error(403)).retryable)

    def test_bad_request_fails_fast(self) -> None:
        result = classify_provider_error(self._status_error(400))
        self.assertEqual(result.category, ErrorCategory.PERMANENT_CLIENT)
        self.assertFalse(result.retryable)

    def test_schema_violation_is_repairable_not_retryable(self) -> None:
        result = classify_provider_error(ValueError("bad json"), schema_violation=True)
        self.assertEqual(result.category, ErrorCategory.SCHEMA)
        self.assertFalse(result.retryable)
        self.assertTrue(result.repairable)


class MetricsTests(unittest.TestCase):
    def _metric(self, **kwargs) -> AICallMetrics:
        base = dict(
            purpose="entry",
            provider="openmodel",
            model="deepseek-v4-flash",
            api_style="messages",
            prompt_version="v2",
            reasoning_effort="high",
            request_hash="h1",
            request_bytes=100,
            response_bytes=50,
            latency_ms=120.0,
            attempts=1,
            success=True,
        )
        base.update(kwargs)
        return AICallMetrics(**base)

    def test_call_id_is_generated_and_unique(self) -> None:
        self.assertNotEqual(self._metric().call_id, self._metric().call_id)

    def test_to_record_marks_estimated_usage(self) -> None:
        metric = self._metric(usage=TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12, estimated=True))
        record = metric.to_record()
        self.assertTrue(record["tokens_estimated"])
        self.assertEqual(record["input_tokens"], 10)
        self.assertEqual(record["purpose"], "entry")

    def test_estimated_cost_uses_configured_pricing(self) -> None:
        metric = self._metric(
            usage=TokenUsage(input_tokens=1_000_000, output_tokens=0, total_tokens=1_000_000, estimated=False),
            input_cost_per_million=0.5,
            output_cost_per_million=1.5,
        )
        self.assertAlmostEqual(metric.estimated_cost_usd, 0.5)

    def test_cost_is_none_without_pricing(self) -> None:
        self.assertIsNone(self._metric().estimated_cost_usd)

    def test_summarize_groups_by_purpose_and_counts_dedupe(self) -> None:
        metrics = [
            self._metric(usage=TokenUsage(100, 10, 110, False)),
            self._metric(usage=TokenUsage(200, 20, 220, False)),
            self._metric(purpose="exit", success=False, error_category="timeout", attempts=2),
            self._metric(purpose="exit", deduplicated=True, usage=TokenUsage(50, 5, 55, True)),
        ]
        summary = summarize_metrics(metrics)
        self.assertEqual(summary["totals"]["calls"], 4)
        self.assertEqual(summary["totals"]["total_tokens"], 385)
        self.assertEqual(summary["totals"]["deduplicated_calls"], 1)
        self.assertEqual(summary["by_purpose"]["entry"]["calls"], 2)
        self.assertEqual(summary["by_purpose"]["entry"]["total_tokens"], 330)
        self.assertEqual(summary["by_purpose"]["exit"]["failures"], 1)
        self.assertEqual(summary["errors"]["timeout"], 1)
        self.assertAlmostEqual(summary["totals"]["success_rate_pct"], 75.0)
        self.assertEqual(summary["totals"]["estimated_token_calls"], 1)

    def test_summarize_handles_empty(self) -> None:
        summary = summarize_metrics([])
        self.assertEqual(summary["totals"]["calls"], 0)
        self.assertEqual(summary["by_purpose"], {})


if __name__ == "__main__":
    unittest.main()
