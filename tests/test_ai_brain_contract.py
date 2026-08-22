from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

import httpx
from pydantic import BaseModel, ValidationError

from bot.ai.brain import EntryDecision, EntryDecisionBatch, GrokBrain, RiskReview
from bot.ai.usage import ErrorCategory


class _Result(BaseModel):
    ok: bool


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str, *args) -> None:
        self.messages.append(message % args if args else message)

    def info(self, message: str, *args) -> None:
        self.messages.append(message % args if args else message)


class _ScriptedClient:
    def __init__(self, items: list) -> None:
        self._items = list(items)
        self.bodies: list[dict] = []

    async def post(self, *args, **kwargs):
        self.bodies.append(kwargs.get("json"))
        item = self._items.pop(0) if self._items else _ok_response('{"ok": true}')
        if isinstance(item, Exception):
            raise item
        return item


def _ok_response(content: str, usage: dict | None = None) -> httpx.Response:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    body: dict = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, request=request, json=body)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
    response = httpx.Response(status, request=request, text='{"error":"nope"}')
    return httpx.HTTPStatusError("err", request=request, response=response)


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        xai_api_key="test-key",
        xai_api_style="chat",
        xai_timeout_seconds=10,
        xai_base_url="https://provider.test/v1",
        xai_model="deepseek-v4-flash",
        xai_max_tokens=1600,
        xai_reasoning_effort="high",
        ai_input_cost_per_million_tokens=None,
        ai_output_cost_per_million_tokens=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _brain(items: list, **overrides) -> tuple[GrokBrain, _ScriptedClient, list]:
    brain = GrokBrain(_settings(**overrides), _Logger())
    client = _ScriptedClient(items)
    brain._client = client
    recorded: list = []

    async def sink(metrics) -> None:
        recorded.append(metrics)

    brain.set_metrics_sink(sink)
    return brain, client, recorded


class MergedRiskReviewSchemaTests(unittest.TestCase):
    def test_entry_decision_accepts_nested_risk_review(self) -> None:
        decision = EntryDecision.model_validate(
            {
                "symbol": "BTC/USDT",
                "action": "OPEN_LONG",
                "confidence": 0.82,
                "market_trend": "Bullish",
                "stop_loss_pct": 0.08,
                "take_profit_pct": 0.12,
                "systemic_risk": 2,
                "reason": "Concise decision reason",
                "risk_review": {
                    "approved": True,
                    "confirmations": "Concise confirmation summary",
                    "invalidation_trigger": "Concise invalidation condition",
                },
            }
        )
        self.assertIsInstance(decision.risk_review, RiskReview)
        self.assertTrue(decision.risk_review.approved)

    def test_missing_risk_review_is_none_not_approved(self) -> None:
        decision = EntryDecision.model_validate(
            {
                "symbol": "BTC/USDT",
                "action": "OPEN_LONG",
                "confidence": 0.5,
                "market_trend": "Bullish",
                "systemic_risk": 2,
                "reason": "no review supplied",
            }
        )
        self.assertIsNone(decision.risk_review)

    def test_structurally_invalid_risk_review_is_dropped_not_approved(self) -> None:
        decision = EntryDecision.model_validate(
            {
                "symbol": "BTC/USDT",
                "action": "OPEN_LONG",
                "confidence": 0.5,
                "market_trend": "Bullish",
                "systemic_risk": 2,
                "reason": "garbage review",
                "risk_review": {"approved": "maybe"},
            }
        )
        self.assertIsNone(decision.risk_review)

    def test_veto_is_preserved(self) -> None:
        review = RiskReview(approved=False, confirmations="none", invalidation_trigger="trend broke")
        self.assertFalse(review.approved)

    def test_long_review_strings_are_clamped_not_rejected(self) -> None:
        review = RiskReview(approved=True, confirmations="x" * 900, invalidation_trigger="y" * 900)
        self.assertEqual(len(review.confirmations), 500)
        self.assertEqual(len(review.invalidation_trigger), 300)

    def test_batch_requires_at_least_one_decision(self) -> None:
        with self.assertRaises(ValidationError):
            EntryDecisionBatch.model_validate({"decisions": []})

    def test_pre_trade_check_call_is_removed(self) -> None:
        self.assertFalse(hasattr(GrokBrain, "pre_trade_check"))


class RetryPolicyTests(unittest.TestCase):
    def test_permanent_auth_error_fails_immediately(self) -> None:
        brain, client, recorded = _brain([_status_error(401), _ok_response('{"ok": true}')])
        result = asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        self.assertIsNone(result)
        self.assertEqual(len(client.bodies), 1)
        self.assertEqual(recorded[-1].error_category, ErrorCategory.AUTH.value)
        self.assertEqual(recorded[-1].attempts, 1)

    def test_bad_request_fails_immediately(self) -> None:
        brain, client, _ = _brain([_status_error(400), _ok_response('{"ok": true}')])
        self.assertIsNone(
            asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        )
        self.assertEqual(len(client.bodies), 1)

    def test_timeout_is_retried(self) -> None:
        brain, client, _ = _brain([httpx.ReadTimeout("slow"), _ok_response('{"ok": true}')])
        result = asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        self.assertEqual(result, _Result(ok=True))
        self.assertEqual(len(client.bodies), 2)

    def test_invalid_response_gets_exactly_one_repair_request(self) -> None:
        brain, client, recorded = _brain(
            [
                _ok_response("not json at all"),
                _ok_response("still not json"),
                _ok_response('{"ok": true}'),
            ]
        )
        result = asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        self.assertIsNone(result)
        self.assertEqual(len(client.bodies), 2, "one original request plus at most one repair")
        self.assertTrue(recorded[-1].repaired)

    def test_repair_request_does_not_resend_the_large_payload(self) -> None:
        big = {"blob": "x" * 5000}
        brain, client, _ = _brain([_ok_response("garbage"), _ok_response('{"ok": true}')])
        result = asyncio.run(brain._ask_grok(system_prompt="p", payload=big, schema=_Result, reasoning_effort="low"))
        self.assertEqual(result, _Result(ok=True))
        original = json.dumps(client.bodies[0])
        repair = json.dumps(client.bodies[1])
        self.assertIn("x" * 5000, original)
        self.assertNotIn("x" * 5000, repair)
        self.assertIn("validation", repair.lower())

    def test_successful_repair_is_flagged_in_metrics(self) -> None:
        brain, _, recorded = _brain([_ok_response("garbage"), _ok_response('{"ok": true}')])
        asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        self.assertTrue(recorded[-1].repaired)
        self.assertTrue(recorded[-1].success)
        self.assertEqual(recorded[-1].attempts, 2)


class UsageAccumulationTests(unittest.TestCase):
    """A retried or repaired call really spends every attempt's tokens."""

    def test_repair_accumulates_both_attempts(self) -> None:
        brain, _, recorded = _brain(
            [
                _ok_response("garbage", usage={"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050}),
                _ok_response('{"ok": true}', usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}),
            ]
        )
        result = asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        self.assertEqual(result, _Result(ok=True))
        metric = recorded[-1]
        self.assertEqual(metric.usage.total_tokens, 1160, "1050 (invalid) + 110 (repair)")
        self.assertEqual(metric.usage.input_tokens, 1100)
        self.assertEqual(metric.usage.output_tokens, 60)
        self.assertEqual(metric.attempts, 2)
        self.assertTrue(metric.repaired)

    def test_retry_accumulates_request_and_response_bytes(self) -> None:
        brain, _, recorded = _brain(
            [
                httpx.ReadTimeout("slow"),
                _ok_response('{"ok": true}', usage={"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}),
            ]
        )
        asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        metric = recorded[-1]
        # Both attempts sent the request body, so bytes must reflect both.
        self.assertGreater(metric.request_bytes, 0)
        self.assertEqual(metric.attempts, 2)

    def test_mixed_reported_and_estimated_usage_is_flagged_estimated(self) -> None:
        from bot.ai.usage import TokenUsage

        combined = TokenUsage(10, 1, 11, False) + TokenUsage(5, 1, 6, True)
        self.assertTrue(combined.estimated)
        self.assertEqual(combined.total_tokens, 17)

    def test_failed_call_still_records_the_tokens_it_spent(self) -> None:
        brain, _, recorded = _brain(
            [
                _ok_response("garbage", usage={"prompt_tokens": 900, "completion_tokens": 20, "total_tokens": 920}),
                _ok_response("still garbage", usage={"prompt_tokens": 40, "completion_tokens": 5, "total_tokens": 45}),
            ]
        )
        result = asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        self.assertIsNone(result)
        self.assertEqual(recorded[-1].usage.total_tokens, 965)


class MetricsTests(unittest.TestCase):
    def test_metrics_capture_provider_usage(self) -> None:
        brain, _, recorded = _brain(
            [_ok_response('{"ok": true}', usage={"prompt_tokens": 900, "completion_tokens": 40, "total_tokens": 940})]
        )
        asyncio.run(
            brain._ask_grok(
                system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low", purpose="entry"
            )
        )
        metric = recorded[-1]
        self.assertEqual(metric.purpose, "entry")
        self.assertEqual(metric.usage.input_tokens, 900)
        self.assertFalse(metric.usage.estimated)
        self.assertTrue(metric.success)
        self.assertGreater(metric.request_bytes, 0)
        self.assertTrue(metric.request_hash)

    def test_metrics_fall_back_to_estimated_tokens(self) -> None:
        brain, _, recorded = _brain([_ok_response('{"ok": true}')])
        asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        metric = recorded[-1]
        self.assertTrue(metric.usage.estimated)
        self.assertGreater(metric.usage.input_tokens, 0)

    def test_request_hash_is_stable_for_identical_payloads(self) -> None:
        brain_a, _, recorded_a = _brain([_ok_response('{"ok": true}')])
        brain_b, _, recorded_b = _brain([_ok_response('{"ok": true}')])
        asyncio.run(brain_a._ask_grok(system_prompt="p", payload={"a": 1, "b": 2}, schema=_Result, reasoning_effort="low"))
        asyncio.run(brain_b._ask_grok(system_prompt="p", payload={"b": 2, "a": 1}, schema=_Result, reasoning_effort="low"))
        self.assertEqual(recorded_a[-1].request_hash, recorded_b[-1].request_hash)

    def test_estimated_cost_when_pricing_configured(self) -> None:
        brain, _, recorded = _brain(
            [_ok_response('{"ok": true}', usage={"prompt_tokens": 1_000_000, "completion_tokens": 0})],
            ai_input_cost_per_million_tokens=0.25,
            ai_output_cost_per_million_tokens=1.0,
        )
        asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        self.assertAlmostEqual(recorded[-1].estimated_cost_usd, 0.25)


if __name__ == "__main__":
    unittest.main()
