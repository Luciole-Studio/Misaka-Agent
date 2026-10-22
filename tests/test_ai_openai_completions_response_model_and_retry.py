"""Translated from pi's ``packages/ai/test/openai-completions-{response-model,retry}.test.ts`` (v1.1.0).

MISAKA fork: vitest's fake timers are stood in for by replacing the running loop's ``call_later`` with
``tests.fake_timers.FakeTimers`` (as ``tests/test_ai_provider_retry.py`` does).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from misaka.ai.api.openai_completions import stream as streamOpenAICompletions
from misaka.ai.compat import complete
from misaka.ai.types import Context, Model, TextContent, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.fake_timers import FakeTimers
from tests.openai_fake import installFakeOpenAI

# --- openai-completions-response-model.test.ts --------------------------------------------------
# Router/virtual ids (e.g. OpenRouter `auto`) keep `model` pinned to the
# requested id and surface the routed concrete id on `responseModel`.


@pytest.fixture
def mockState(monkeypatch):
    return installFakeOpenAI(monkeypatch)


def openRouterAuto() -> Model:
    return Model.model_validate(
        {
            "id": "openrouter/auto",
            "name": "OpenRouter Auto",
            "api": "openai-completions",
            "provider": "openrouter",
            "baseUrl": "https://openrouter.ai/api/v1",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 200_000,
            "maxTokens": 8192,
        }
    )


def _hi() -> Context:
    return Context(messages=[UserMessage(content="hi", timestamp=time.time_ns() // 1_000_000)])


def _usage(prompt: int, completion: int) -> dict[str, Any]:
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }


async def test_surfaces_routed_chunk_model_on_response_model_without_changing_model(mockState):
    mockState.chunks = [
        {"id": "chatcmpl-1", "model": "anthropic/claude-opus-4.8", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
        {
            "id": "chatcmpl-1",
            "model": "anthropic/claude-opus-4.8",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": _usage(10, 5),
        },
    ]

    message = await complete(openRouterAuto(), _hi(), {"apiKey": "test"})

    assert message.model == "openrouter/auto"
    assert message.responseModel == "anthropic/claude-opus-4.8"
    assert message.provider == "openrouter"
    assert message.stopReason == "stop"


async def test_leaves_response_model_undefined_when_chunks_echo_the_requested_id(mockState):
    mockState.chunks = [
        {"id": "chatcmpl-2", "model": "openrouter/auto", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
        {
            "id": "chatcmpl-2",
            "model": "openrouter/auto",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": _usage(1, 1),
        },
    ]

    message = await complete(openRouterAuto(), _hi(), {"apiKey": "test"})

    assert message.model == "openrouter/auto"
    assert message.responseModel is None


async def test_ignores_empty_or_missing_chunk_model(mockState):
    mockState.chunks = [
        {"id": "chatcmpl-3", "choices": [{"index": 0, "delta": {"content": "hi"}}]},
        {"id": "chatcmpl-3", "model": "", "choices": [{"index": 0, "delta": {"content": "!"}}]},
        {"id": "chatcmpl-3", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": _usage(1, 2)},
    ]

    message = await complete(openRouterAuto(), _hi(), {"apiKey": "test"})

    assert message.model == "openrouter/auto"
    assert message.responseModel is None


# --- openai-completions-retry.test.ts -----------------------------------------------------------


@pytest.fixture
def retryState(monkeypatch):
    fake = installFakeOpenAI(monkeypatch)
    fake.requestOptions = []
    fake.requestErrors = []

    def create(_resource: str, _params: Any, requestOptions: Any) -> list[Any]:
        fake.requestOptions.append(requestOptions)
        if fake.requestErrors:
            raise fake.requestErrors.pop(0)
        return [
            {"id": "chatcmpl-test", "choices": [{"index": 0, "delta": {"content": "ok"}}]},
            {"id": "chatcmpl-test", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]

    fake.create = create
    return fake


retryModel = Model.model_validate(
    {
        "id": "test-model",
        "name": "Test Model",
        "api": "openai-completions",
        "provider": "opencode-go",
        "baseUrl": "https://opencode.ai/zen/go/v1",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000,
        "maxTokens": 100,
    }
)

retryContext = normalizeContext(
    Context(systemPrompt="", messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)], tools=[])
)


def providerError(message: str, status: int, headers: dict[str, str]) -> Exception:
    error = Exception(message)
    error.status = status  # type: ignore[attr-defined]
    error.headers = httpx.Headers(headers)  # type: ignore[attr-defined]
    return error


async def consume(options: dict[str, Any] | None = None):
    stream = streamOpenAICompletions(retryModel, retryContext, {"apiKey": "test", **(options or {})})
    async for _event in stream:
        pass
    return await stream.result()


def useFakeTimers(monkeypatch: pytest.MonkeyPatch) -> FakeTimers:
    timers = FakeTimers()
    loop = asyncio.get_running_loop()

    def call_later(delay: float, callback: Any, *args: Any, context: Any = None) -> SimpleNamespace:
        timer = timers.setTimeout(lambda: callback(*args), delay * 1000)
        return SimpleNamespace(cancel=lambda: timers.clearTimeout(timer))

    monkeypatch.setattr(loop, "call_later", call_later)
    return timers


async def test_disables_sdk_retries_by_default(retryState):
    await consume()

    assert [options["maxRetries"] for options in retryState.requestOptions] == [0]


async def test_honors_provider_retries_while_keeping_sdk_retries_disabled(retryState, monkeypatch):
    timers = useFakeTimers(monkeypatch)
    retryState.requestErrors = [
        providerError("rate limited", 429, {"retry-after-ms": "100"}),
        providerError("server error", 500, {"retry-after-ms": "100"}),
    ]

    result = asyncio.ensure_future(consume({"maxRetries": 2, "maxRetryDelayMs": 100}))
    await timers.advanceAsync(0)
    assert len(retryState.requestOptions) == 1
    await timers.advanceAsync(99)
    assert len(retryState.requestOptions) == 1
    await timers.advanceAsync(1)
    assert len(retryState.requestOptions) == 2
    await timers.advanceAsync(99)
    assert len(retryState.requestOptions) == 2
    await timers.advanceAsync(1)
    await result
    assert [options["maxRetries"] for options in retryState.requestOptions] == [0, 0, 0]


async def test_fails_immediately_when_a_provider_requested_retry_delay_exceeds_the_limit(retryState):
    retryState.requestErrors = [providerError("rate limited", 429, {"retry-after": "277403"})]

    result = await consume({"maxRetries": 2, "maxRetryDelayMs": 1000})

    assert result.stopReason == "error"
    assert "Server requested 277403s retry delay (max: 1s)" in result.errorMessage
    assert "rate limited" in result.errorMessage
    assert [options["maxRetries"] for options in retryState.requestOptions] == [0]
