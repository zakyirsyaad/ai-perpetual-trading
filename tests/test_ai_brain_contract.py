from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unittest
from types import SimpleNamespace

import httpx
from pydantic import BaseModel, ValidationError

from bot.ai.brain import (
    ENTRY_LEARNING_NOTE_MAX_CHARS,
    ENTRY_REASON_MAX_CHARS,
    ENTRY_SYSTEM_PROMPT,
    ENTRY_SYSTEM_PROMPT_LEAN,
    ENTRY_SYSTEM_PROMPT_LEAN_VOLUME,
    EntryDecision,
    EntryDecisionBatch,
    GrokBrain,
    RiskReview,
)
from bot.ai.compact_context import COMPACT_PROMPT_VERSION, LEAN_PROMPT_VERSION, LEAN_VOLUME_PROMPT_VERSION
from bot.ai.usage import ErrorCategory
from bot.config import LEAN_VOLUME_MIN_CONFIDENCE
from bot.intelligence.market_intelligence import lean_features_from_candles


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

    def test_repair_without_provider_usage_estimates_both_attempts(self) -> None:
        # No `usage` object from the provider: each attempt must still be
        # estimated and summed, not estimated once at the end.
        big_payload = {"blob": "x" * 4000}
        brain, _, recorded = _brain([_ok_response("garbage"), _ok_response('{"ok": true}')])
        result = asyncio.run(
            brain._ask_grok(system_prompt="p", payload=big_payload, schema=_Result, reasoning_effort="low")
        )
        self.assertEqual(result, _Result(ok=True))
        metric = recorded[-1]
        self.assertTrue(metric.usage.estimated)
        self.assertEqual(metric.attempts, 2)
        # The first attempt alone carries the ~4000-char payload, so the total must
        # be far larger than an estimate of the small repair request by itself.
        self.assertGreater(metric.usage.total_tokens, 1000)

    def test_timed_out_attempt_is_still_billed_conservatively(self) -> None:
        # A client timeout does not prove the provider skipped inference, so a
        # dispatched attempt must be costed rather than assumed free.
        big_payload = {"blob": "y" * 4000}
        brain, _, recorded = _brain([httpx.ReadTimeout("slow"), _ok_response('{"ok": true}')])
        asyncio.run(
            brain._ask_grok(system_prompt="p", payload=big_payload, schema=_Result, reasoning_effort="low")
        )
        metric = recorded[-1]
        self.assertTrue(metric.usage.estimated)
        self.assertEqual(metric.attempts, 2)
        # Both dispatched requests carried the ~4000-char payload.
        self.assertGreater(metric.usage.total_tokens, 2000)

    def test_retryable_http_error_attempt_is_billed(self) -> None:
        big_payload = {"blob": "z" * 4000}
        brain, _, recorded = _brain([_status_error(503), _ok_response('{"ok": true}')])
        asyncio.run(
            brain._ask_grok(system_prompt="p", payload=big_payload, schema=_Result, reasoning_effort="low")
        )
        metric = recorded[-1]
        self.assertEqual(metric.attempts, 2)
        self.assertGreater(metric.usage.total_tokens, 2000)

    def test_permanent_error_attempt_is_billed(self) -> None:
        big_payload = {"blob": "q" * 4000}
        brain, _, recorded = _brain([_status_error(401)])
        asyncio.run(
            brain._ask_grok(system_prompt="p", payload=big_payload, schema=_Result, reasoning_effort="low")
        )
        metric = recorded[-1]
        self.assertFalse(metric.success)
        self.assertGreater(metric.usage.total_tokens, 900)

    def test_mixed_reported_and_estimated_attempts_are_summed(self) -> None:
        brain, _, recorded = _brain(
            [
                # First attempt: provider reports usage but the body is invalid.
                _ok_response("garbage", usage={"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050}),
                # Repair attempt: no usage reported, so it must be estimated.
                _ok_response('{"ok": true}'),
            ]
        )
        result = asyncio.run(brain._ask_grok(system_prompt="p", payload={"a": 1}, schema=_Result, reasoning_effort="low"))
        self.assertEqual(result, _Result(ok=True))
        metric = recorded[-1]
        self.assertGreater(metric.usage.total_tokens, 1050, "reported 1050 plus the estimated repair")
        self.assertTrue(metric.usage.estimated, "a partly-estimated total must be flagged estimated")

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


_HOLD_BATCH = json.dumps(
    {
        "decisions": [
            {"symbol": "BTC/USDT", "action": "HOLD", "confidence": 0.5, "market_trend": "Sideways",
             "systemic_risk": 2, "reason": "no edge"}
        ]
    }
)


class EntrySystemPromptTests(unittest.TestCase):
    """The lean payload gets its own system prompt and its own `prompt_version`; legacy is unchanged."""

    # sha256 of ENTRY_SYSTEM_PROMPT as it has been sent to the live bot; the legacy prompt must not move.
    LEGACY_PROMPT_SHA256 = "8e125a5c98e1a908a8cd6c9af2676077aa64e0320c22cd7bc34cc0e009634323"
    # sha256 of ENTRY_SYSTEM_PROMPT_LEAN as rollout step 2 was prepared; the edge objective must not move.
    LEAN_PROMPT_SHA256 = "b2cef6c084b597a2de12c780ba052d97e05a1ff106a445b0e338d0d57afada80"
    # sha256 of ENTRY_SYSTEM_PROMPT_LEAN_VOLUME as the operator reviewed it. `lean-entry-volume-v1` is the only
    # identity of this text in ai_call_metrics: a changed prompt needs a new version and a new pin.
    LEAN_VOLUME_PROMPT_SHA256 = "22297422db9f95d3a00a25951388d83d434fb19f612cadbf6bfc9e6beafcdfcc"

    @staticmethod
    def _system_text(body: dict) -> str:
        return body["messages"][0]["content"]

    def test_the_legacy_entry_prompt_and_version_are_unchanged(self) -> None:
        self.assertEqual(hashlib.sha256(ENTRY_SYSTEM_PROMPT.encode()).hexdigest(), self.LEGACY_PROMPT_SHA256)
        self.assertEqual(COMPACT_PROMPT_VERSION, "okx-context-parity-v3")
        brain, client, recorded = _brain([_ok_response(_HOLD_BATCH)])

        decisions = asyncio.run(brain.entry_decisions({"a": 1}))

        self.assertEqual([decision.action for decision in decisions], ["HOLD"])
        self.assertTrue(self._system_text(client.bodies[0]).startswith(ENTRY_SYSTEM_PROMPT + "\n\n"))
        self.assertEqual((recorded[-1].purpose, recorded[-1].prompt_version), ("entry", COMPACT_PROMPT_VERSION))

    def test_a_lean_call_uses_the_lean_prompt_and_records_the_lean_version(self) -> None:
        self.assertNotEqual(LEAN_PROMPT_VERSION, COMPACT_PROMPT_VERSION)
        brain, client, recorded = _brain([_ok_response(_HOLD_BATCH)])

        decisions = asyncio.run(brain.entry_decisions({"a": 1}, lean=True))

        self.assertEqual([decision.action for decision in decisions], ["HOLD"])
        self.assertTrue(self._system_text(client.bodies[0]).startswith(ENTRY_SYSTEM_PROMPT_LEAN + "\n\n"))
        self.assertNotIn(ENTRY_SYSTEM_PROMPT, self._system_text(client.bodies[0]))
        self.assertEqual((recorded[-1].purpose, recorded[-1].prompt_version), ("entry", LEAN_PROMPT_VERSION))
        self.assertEqual(recorded[-1].to_record()["prompt_version"], LEAN_PROMPT_VERSION)

    def test_a_failed_lean_call_still_records_the_lean_version(self) -> None:
        brain, _, recorded = _brain([_status_error(401)])

        self.assertIsNone(asyncio.run(brain.entry_decisions({"a": 1}, lean=True)))

        self.assertFalse(recorded[-1].success)
        self.assertEqual(recorded[-1].prompt_version, LEAN_PROMPT_VERSION)

    def test_a_lean_call_skipped_by_a_rate_limit_cooldown_records_the_lean_version(self) -> None:
        brain, client, recorded = _brain([])
        brain._rate_limited_until = time.time() + 60.0

        self.assertIsNone(asyncio.run(brain.entry_decisions({"a": 1}, lean=True)))

        self.assertEqual(client.bodies, [])   # nothing was sent
        self.assertEqual((recorded[-1].error_category, recorded[-1].attempts), ("rate_limit", 0))
        self.assertEqual(recorded[-1].prompt_version, LEAN_PROMPT_VERSION)

    def test_other_calls_keep_the_legacy_version(self) -> None:
        brain, _, recorded = _brain([_ok_response('{"action": "HOLD", "confidence": 0.5, "reason": "hold"}')])

        asyncio.run(brain.exit_decision({"a": 1}))

        self.assertEqual((recorded[-1].purpose, recorded[-1].prompt_version), ("exit", COMPACT_PROMPT_VERSION))

    def test_the_lean_prompt_drops_the_volume_mandate_and_names_the_lean_inputs(self) -> None:
        lean = ENTRY_SYSTEM_PROMPT_LEAN
        for absent in ("volume-building", "mandate", "recent_performance", "trade_memory", "factors", "flow signals"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, lean)
        for present in (
            "HOLD is the expected answer most ticks",
            "round-trip",
            "features",
            "entry_rules",
            "market_regime",
            "setup_ledger",
            "risk_review",
            "liquidation_constraints",
            "Return one decision for every allowed symbol",
        ):
            with self.subTest(present=present):
                self.assertIn(present, lean)
        # the same role as the legacy prompt
        self.assertEqual(lean.splitlines()[0], ENTRY_SYSTEM_PROMPT.splitlines()[0])

    def test_the_edge_lean_prompt_and_version_are_unchanged(self) -> None:
        self.assertEqual(hashlib.sha256(ENTRY_SYSTEM_PROMPT_LEAN.encode()).hexdigest(), self.LEAN_PROMPT_SHA256)
        self.assertEqual(LEAN_PROMPT_VERSION, "lean-entry-v1")

    def test_the_volume_objective_sends_its_own_lean_prompt_and_version(self) -> None:
        self.assertNotIn(LEAN_VOLUME_PROMPT_VERSION, (LEAN_PROMPT_VERSION, COMPACT_PROMPT_VERSION))
        brain, client, recorded = _brain([_ok_response(_HOLD_BATCH)], ai_lean_objective="volume")

        decisions = asyncio.run(brain.entry_decisions({"a": 1}, lean=True))

        self.assertEqual([decision.action for decision in decisions], ["HOLD"])
        system = self._system_text(client.bodies[0])
        self.assertTrue(system.startswith(ENTRY_SYSTEM_PROMPT_LEAN_VOLUME + "\n\n"))
        self.assertNotIn(ENTRY_SYSTEM_PROMPT_LEAN, system)
        self.assertNotIn(ENTRY_SYSTEM_PROMPT, system)
        self.assertEqual((recorded[-1].purpose, recorded[-1].prompt_version), ("entry", LEAN_VOLUME_PROMPT_VERSION))
        self.assertEqual(recorded[-1].to_record()["prompt_version"], LEAN_VOLUME_PROMPT_VERSION)

    def test_the_volume_prompt_and_version_are_pinned(self) -> None:
        self.assertEqual(
            hashlib.sha256(ENTRY_SYSTEM_PROMPT_LEAN_VOLUME.encode()).hexdigest(), self.LEAN_VOLUME_PROMPT_SHA256
        )
        self.assertEqual(LEAN_VOLUME_PROMPT_VERSION, "lean-entry-volume-v1")

    def test_a_failed_volume_call_still_records_the_volume_version(self) -> None:
        brain, _, recorded = _brain([_status_error(401)], ai_lean_objective="volume")

        self.assertIsNone(asyncio.run(brain.entry_decisions({"a": 1}, lean=True)))

        self.assertFalse(recorded[-1].success)
        self.assertEqual(recorded[-1].prompt_version, LEAN_VOLUME_PROMPT_VERSION)

    def test_the_edge_objective_and_an_unset_one_keep_the_lean_prompt(self) -> None:
        for name, overrides in (("unset", {}), ("edge", {"ai_lean_objective": "edge"})):
            with self.subTest(name):
                brain, client, recorded = _brain([_ok_response(_HOLD_BATCH)], **overrides)

                asyncio.run(brain.entry_decisions({"a": 1}, lean=True))

                self.assertTrue(self._system_text(client.bodies[0]).startswith(ENTRY_SYSTEM_PROMPT_LEAN + "\n\n"))
                self.assertEqual(recorded[-1].prompt_version, LEAN_PROMPT_VERSION)

    def test_the_objective_never_reaches_a_legacy_call(self) -> None:
        brain, client, recorded = _brain([_ok_response(_HOLD_BATCH)], ai_lean_objective="volume")

        asyncio.run(brain.entry_decisions({"a": 1}))

        self.assertTrue(self._system_text(client.bodies[0]).startswith(ENTRY_SYSTEM_PROMPT + "\n\n"))
        self.assertEqual(recorded[-1].prompt_version, COMPACT_PROMPT_VERSION)

    def test_the_volume_prompt_keeps_the_lean_contract_and_inputs(self) -> None:
        volume = ENTRY_SYSTEM_PROMPT_LEAN_VOLUME
        for absent in (
            "recent_performance", "trade_memory", "factors", "flow signals", "every tick", "do not sit out",
            "HOLD is the expected answer most ticks",
        ):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, volume)
        for present in (
            "features",
            "entry_rules",
            "market_regime",
            "setup_ledger",
            "risk_review",
            "liquidation_constraints",
            "stop_loss_pct",
            "take_profit_pct",
            "output_rules.allowed_actions",
            "Return one decision for every allowed symbol",
        ):
            with self.subTest(present=present):
                self.assertIn(present, volume)
        self.assertEqual(volume.splitlines()[0], ENTRY_SYSTEM_PROMPT.splitlines()[0])

    def test_the_volume_prompt_keeps_the_skeptical_risk_review_sentence(self) -> None:
        # the veto is a safety control the volume objective does not soften: the sentence is the lean one's
        sentence = "Act as your own skeptical pre-trade risk controller: set approved=false when the live"
        self.assertIn(sentence, ENTRY_SYSTEM_PROMPT_LEAN)
        self.assertIn(sentence, ENTRY_SYSTEM_PROMPT_LEAN_VOLUME)

    def test_the_volume_prompt_states_the_confidence_settings_enforce(self) -> None:
        self.assertEqual(LEAN_VOLUME_MIN_CONFIDENCE, 0.75)
        self.assertIn(f"only at {LEAN_VOLUME_MIN_CONFIDENCE} or", ENTRY_SYSTEM_PROMPT_LEAN_VOLUME)

    def test_every_feature_name_the_volume_prompt_cites_is_a_lean_feature(self) -> None:
        closes = [100.0 + 0.1 * (i % 7) for i in range(40)]
        features = lean_features_from_candles(
            bar_interval="3m", closes=closes, highs=[c + 0.2 for c in closes], lows=[c - 0.2 for c in closes]
        )
        cited = set(
            re.findall(
                r"\b(?:change_pct_\w+|ema21_slope_\w+|stretch_atr|rsi_14|atr_pct)\b", ENTRY_SYSTEM_PROMPT_LEAN_VOLUME
            )
        )
        self.assertTrue({"stretch_atr", "change_pct_5_bars", "ema21_slope_atr_5_bars"} <= cited)
        self.assertEqual(cited - set(features), set())

    def test_the_volume_prompt_describes_the_setup_the_gate_admits(self) -> None:
        volume = ENTRY_SYSTEM_PROMPT_LEAN_VOLUME
        for present in (
            "max_stretch_atr",
            "pullback",
            "chase",
            "HOLD only for a reason you can name",
            "0.75",
        ):
            with self.subTest(present=present):
                self.assertIn(present, volume)


class EntryTextLengthClampTests(unittest.TestCase):
    """A verbose-but-valid entry decision must survive the length contract.

    `RiskReview` and `ExitDecision.reason` already clamp instead of raising,
    because chat/messages providers cannot enforce maxLength server-side. The
    entry decision's own free text was the one place left unguarded: mimo-v2.6-pro
    writes longer prose than v2.5, so 4 of 5 live entry calls on 2026-09-25 failed
    on `decisions.0.reason` and burned a second repair call each time.
    """

    @staticmethod
    def _payload(**overrides: object) -> dict:
        decision: dict = {
            "symbol": "BTC/USDT",
            "action": "HOLD",
            "confidence": 0.8,
            "market_trend": "Sideways",
            "systemic_risk": 2,
            "reason": "short",
        }
        decision.update(overrides)
        return {"decisions": [decision]}

    def test_overlong_reason_is_clamped_not_rejected(self) -> None:
        batch = EntryDecisionBatch.model_validate(self._payload(reason="R" * 1500))
        self.assertEqual(len(batch.decisions[0].reason), ENTRY_REASON_MAX_CHARS)

    def test_overlong_learning_note_is_clamped(self) -> None:
        batch = EntryDecisionBatch.model_validate(self._payload(learning_note="L" * 1200))
        self.assertEqual(len(batch.decisions[0].learning_note), ENTRY_LEARNING_NOTE_MAX_CHARS)

    def test_structure_stays_strict(self) -> None:
        # Only the length technicality is forgiven; missing or empty fields still fail.
        with self.assertRaises(ValidationError):
            EntryDecisionBatch.model_validate(self._payload(reason=""))
        payload = self._payload()
        payload["decisions"][0].pop("symbol")
        with self.assertRaises(ValidationError):
            EntryDecisionBatch.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
