"""Translated from pi's ``packages/ai/test/openai-completions-{raw-stop-reason,vllm-priority}.test.ts`` (v1.1.0).

``openai-completions-provider-stream-event.test.ts`` lives in ``tests/test_pi_099_port.py``
(``test_completions_exposes_provider_chunks_including_openrouter_metadata``).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.api.openai_completions import stream as streamOpenAICompletions
from misaka.ai.compat import getModel
from misaka.ai.types import Context, Model, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.openai_fake import installFakeOpenAI

DEFAULT_CHUNKS = [
    {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }
]


@pytest.fixture
def mockState(monkeypatch):
    return installFakeOpenAI(monkeypatch, DEFAULT_CHUNKS)


# --- openai-completions-raw-stop-reason.test.ts -------------------------------------------------

model = Model.model_validate(
    {
        "id": "test-model",
        "name": "Test Model",
        "api": "openai-completions",
        "provider": "openai",
        "baseUrl": "https://api.openai.com/v1",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 128_000,
        "maxTokens": 4096,
    }
)

context = normalizeContext(Context(messages=[UserMessage(content="hello", timestamp=time.time_ns() // 1_000_000)]))


async def test_preserves_raw_finish_reasons_for_successful_stops(mockState):
    mockState.chunks = [{"id": "chatcmpl-1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]

    message = await streamOpenAICompletions(model, context, {"apiKey": "test"}).result()

    assert message.stopReason == "stop"
    assert message.rawStopReason == "stop"
    assert message.errorMessage is None


async def test_preserves_raw_finish_reasons_for_provider_error_stops(mockState):
    mockState.chunks = [{"id": "chatcmpl-2", "choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]}]

    message = await streamOpenAICompletions(model, context, {"apiKey": "test"}).result()

    assert message.stopReason == "error"
    assert message.rawStopReason == "content_filter"
    assert message.errorMessage == "Provider finish_reason: content_filter"


# --- openai-completions-vllm-priority.test.ts ---------------------------------------------------


def createModel(**overrides: Any) -> Model:
    baseModel = getModel("openai", "gpt-4o-mini").model_dump(exclude={"compat"})
    return Model.model_validate({**baseModel, "api": "openai-completions", **overrides})


async def captureRequest(mockState: Any, model: Model) -> Any:
    await streamOpenAICompletions(
        model,
        normalizeContext(Context(systemPrompt="sys", messages=[UserMessage(content="hi", timestamp=time.time_ns() // 1_000_000)])),
        {"apiKey": "test-key"},
    ).result()

    return mockState.lastParams


async def test_sends_compat_vllm_priority_as_the_top_level_priority_request_field(mockState):
    payload = await captureRequest(mockState, createModel(compat={"vllmPriority": 10}))

    assert payload["priority"] == 10


async def test_omits_priority_when_vllm_priority_is_not_set(mockState):
    payload = await captureRequest(mockState, createModel())

    assert "priority" not in payload
