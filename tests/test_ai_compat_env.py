"""Translated from pi's ``packages/ai/test/compat-env.test.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any

import pytest

from misaka.ai.compat import (
    ApiProvider,
    complete,
    registerApiProvider,
    resetApiProviders,
)
from misaka.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    Model,
    StartEvent,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.utils.values import read_field

context = Context(messages=[UserMessage(content="hi", timestamp=0)])

model = Model.model_validate(
    {
        "id": "test-model",
        "name": "Test Model",
        "api": "openai-responses",
        "provider": "custom-openai",
        "baseUrl": "https://example.test/v1",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 128000,
        "maxTokens": 4096,
    }
)


def message() -> AssistantMessage:
    return AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "api": model.api,
            "provider": model.provider,
            "model": model.id,
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "stop",
            "timestamp": 0,
        }
    )


@pytest.fixture(autouse=True)
def _reset():
    yield
    resetApiProviders()


async def test_dispatches_unknown_providers_through_the_legacy_api_registry():
    captured: dict[str, Any] = {}

    def respond(_model: Model, _context: Any, options: Any = None) -> AssistantMessageEventStream:
        captured["apiKey"] = read_field(options, "apiKey")
        stream = AssistantMessageEventStream()
        output = message()
        stream.push(StartEvent(type="start", partial=output))
        stream.push(DoneEvent(type="done", reason="stop", message=output))
        stream.end(output)
        return stream

    registerApiProvider(ApiProvider(api="openai-responses", stream=respond, streamSimple=respond))

    await complete(model, context, {"apiKey": "request-key"})

    assert captured["apiKey"] == "request-key"
