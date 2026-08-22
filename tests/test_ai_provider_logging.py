from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from pydantic import BaseModel

from bot.ai.brain import GrokBrain


class _Result(BaseModel):
    ok: bool


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str, *args) -> None:
        self.messages.append(message % args if args else message)


class _RateLimitedClient:
    async def post(self, *args, **kwargs):
        request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
        response = httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "0.01"},
            text='{"error":"too many requests"}',
        )
        raise httpx.HTTPStatusError("too many requests", request=request, response=response)


class _RateLimitThenSuccessClient:
    def __init__(self) -> None:
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
        if self.calls == 1:
            response = httpx.Response(
                429,
                request=request,
                headers={"Retry-After": "0.01"},
                text='{"error":"retry shortly"}',
            )
            raise httpx.HTTPStatusError("rate limited", request=request, response=response)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )


class _LongRateLimitedClient:
    async def post(self, *args, **kwargs):
        request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
        response = httpx.Response(
            429,
            request=request,
            headers={"Retry-After": "1800"},
            text='{"error":"retry much later"}',
        )
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)


class _CaptureClient:
    def __init__(self) -> None:
        self.json_body = None

    async def post(self, *args, **kwargs):
        self.json_body = kwargs.get("json")
        request = httpx.Request("POST", "https://provider.test/v1/chat/completions")
        response = httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": '{"ok": true}'}}]},
        )
        return response


class AIProviderLoggingTests(unittest.TestCase):
    def test_rate_limit_log_includes_status_retry_after_endpoint_and_model(self) -> None:
        logger = _Logger()
        brain = GrokBrain(
            SimpleNamespace(
                xai_api_key="test-key",
                xai_api_style="chat",
                xai_timeout_seconds=10,
                xai_base_url="https://provider.test/v1",
                xai_model="ag/gemini-pro-agent",
            ),
            logger,
        )
        brain._client = _RateLimitedClient()

        result = asyncio.run(
            brain._ask_grok(
                system_prompt="Return JSON.",
                payload={"ping": True},
                schema=_Result,
                reasoning_effort="low",
            )
        )

        self.assertIsNone(result)
        self.assertTrue(any("status=429" in item for item in logger.messages))
        self.assertTrue(any("retry_after=0.01" in item for item in logger.messages))
        self.assertTrue(any("endpoint=/chat/completions" in item for item in logger.messages))
        self.assertTrue(any("model=ag/gemini-pro-agent" in item for item in logger.messages))

    def test_short_rate_limit_is_retried_before_returning_hold(self) -> None:
        logger = _Logger()
        brain = GrokBrain(
            SimpleNamespace(
                xai_api_key="test-key",
                xai_api_style="chat",
                xai_timeout_seconds=10,
                xai_base_url="https://provider.test/v1",
                xai_model="DeepSeek-V4-Flash",
            ),
            logger,
        )
        client = _RateLimitThenSuccessClient()
        brain._client = client

        result = asyncio.run(
            brain._ask_grok(
                system_prompt="Return JSON.",
                payload={"ping": True},
                schema=_Result,
                reasoning_effort="low",
            )
        )

        self.assertEqual(result, _Result(ok=True))
        self.assertEqual(client.calls, 2)
        self.assertTrue(any("retrying_after_seconds" in item for item in logger.messages))

    def test_long_rate_limit_returns_hold_without_retry_sleep(self) -> None:
        logger = _Logger()
        brain = GrokBrain(
            SimpleNamespace(
                xai_api_key="test-key",
                xai_api_style="chat",
                xai_timeout_seconds=10,
                xai_base_url="https://provider.test/v1",
                xai_model="DeepSeek-V4-Flash",
            ),
            logger,
        )
        brain._client = _LongRateLimitedClient()
        slept_for: list[float] = []

        async def record_sleep(seconds: float) -> None:
            slept_for.append(seconds)

        with patch("bot.ai.brain.asyncio.sleep", new=record_sleep):
            result = asyncio.run(
                brain._ask_grok(
                    system_prompt="Return JSON.",
                    payload={"ping": True},
                    schema=_Result,
                    reasoning_effort="low",
                )
            )

        self.assertIsNone(result)
        self.assertEqual(slept_for, [])
        self.assertIsNotNone(brain._rate_limited_until)
        self.assertGreater(brain._rate_limited_until or 0.0, time.time() + 1790.0)

    def test_chat_completion_request_includes_max_tokens(self) -> None:
        logger = _Logger()
        brain = GrokBrain(
            SimpleNamespace(
                xai_api_key="test-key",
                xai_api_style="chat",
                xai_timeout_seconds=10,
                xai_base_url="https://provider.test/v1",
                xai_model="DeepSeek-V4-Flash",
                xai_max_tokens=1234,
            ),
            logger,
        )
        client = _CaptureClient()
        brain._client = client

        result = asyncio.run(
            brain._ask_grok(
                system_prompt="Return JSON.",
                payload={"ping": True},
                schema=_Result,
                reasoning_effort="low",
            )
        )

        self.assertEqual(result, _Result(ok=True))
        self.assertEqual(client.json_body["max_tokens"], 1234)

    def test_load_json_object_accepts_markdown_wrapped_json(self) -> None:
        self.assertEqual(GrokBrain._load_json_object('```json\n{"ok": true}\n```'), {"ok": True})
        self.assertEqual(GrokBrain._load_json_object('Result:\n{"ok": true}\nDone.'), {"ok": True})


if __name__ == "__main__":
    unittest.main()
