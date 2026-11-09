"""Translated from pi's ``packages/ai/test/cache-retention.test.ts`` (v1.1.0): the OpenAI Responses and OpenAI Completions
blocks (the Anthropic block is ``tests/test_ai_cache_retention.py``; the two ``OPENAI_API_KEY``-gated cases are not
ported)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.api.openai_completions import stream as streamOpenAICompletions
from misaka.ai.api.openai_responses import stream as streamOpenAIResponses
from misaka.ai.compat import getModel
from misaka.ai.types import Context, Model, SystemMessage, Tool, UserMessage
from misaka.ai.utils.transcript import normalizeContext


class PayloadCaptured(Exception):
    pass


def stopAfterPayload(captured: list[Any]):
    def onPayload(payload: Any, _model: Any) -> Any:
        captured.append(payload)
        raise PayloadCaptured("payload captured")

    return onPayload


@pytest.fixture(autouse=True)
def _noCacheRetentionEnv(monkeypatch):
    monkeypatch.delenv("PI_CACHE_RETENTION", raising=False)


context = normalizeContext(
    Context(
        systemPrompt="You are a helpful assistant.",
        messages=[UserMessage(content="Hello", timestamp=time.time_ns() // 1_000_000)],
    )
)


async def _drain(eventStream: Any) -> None:
    async for event in eventStream:
        if event.type == "error":
            break


def _with(model: Model, **fields: Any) -> Model:
    return Model.model_validate({**model.model_dump(), **fields})


# --- OpenAI Responses Provider ------------------------------------------------------------------


@pytest.mark.parametrize("modelId", ["gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-astra", "gpt-6-luna", "gpt-6-sol"])
def test_does_not_enable_cache_warming_from_the_documented_ttl_alone(modelId):
    assert getModel("openai", modelId).promptCache is None


async def test_responses_should_set_prompt_cache_retention_for_non_api_openai_com_base_url_by_default(monkeypatch):
    monkeypatch.setenv("PI_CACHE_RETENTION", "long")
    # Create a model with a different baseUrl (simulating a proxy)
    proxyModel = _with(getModel("openai", "gpt-4o-mini"), baseUrl="https://my-proxy.example.com/v1")
    captured: list[Any] = []

    await _drain(streamOpenAIResponses(proxyModel, context, {"apiKey": "sk-fake-key", "onPayload": stopAfterPayload(captured)}))

    assert captured
    assert captured[0]["prompt_cache_retention"] == "24h"


async def test_responses_should_omit_prompt_cache_retention_when_supports_long_cache_retention_is_false():
    model = _with(getModel("openai", "gpt-4o-mini"), compat={"supportsLongCacheRetention": False})
    captured: list[Any] = []

    await _drain(
        streamOpenAIResponses(
            model,
            context,
            {
                "apiKey": "sk-fake-key",
                "cacheRetention": "long",
                "sessionId": "session-compat-false",
                "onPayload": stopAfterPayload(captured),
            },
        )
    )

    assert captured
    assert captured[0].get("prompt_cache_retention") is None


async def test_responses_should_omit_prompt_cache_key_and_disable_implicit_writes_when_cache_retention_is_none():
    captured: list[Any] = []

    await _drain(
        streamOpenAIResponses(
            getModel("openai", "gpt-5.6-sol"),
            context,
            {"apiKey": "sk-fake-key", "cacheRetention": "none", "sessionId": "session-1", "onPayload": stopAfterPayload(captured)},
        )
    )

    assert captured
    assert captured[0].get("prompt_cache_key") is None
    assert captured[0].get("prompt_cache_retention") is None
    assert captured[0]["prompt_cache_options"] == {"mode": "explicit"}


async def test_responses_should_omit_prompt_cache_options_for_models_that_reject_it():
    captured: list[Any] = []

    await _drain(
        streamOpenAIResponses(
            getModel("openai", "gpt-4o-mini"),
            context,
            {"apiKey": "sk-fake-key", "cacheRetention": "none", "sessionId": "session-1", "onPayload": stopAfterPayload(captured)},
        )
    )

    assert captured
    assert captured[0].get("prompt_cache_key") is None
    assert captured[0].get("prompt_cache_options") is None


@pytest.mark.parametrize(
    ("modelId", "retention", "cacheOptions"),
    [
        ("gpt-4o-mini", "24h", None),
        ("gpt-6-astra", None, {"ttl": "30m"}),
        ("gpt-6-sol", None, {"ttl": "30m"}),
        ("gpt-6-luna", None, {"ttl": "30m"}),
    ],
)
async def test_responses_should_use_the_supported_long_cache_field(modelId, retention, cacheOptions):
    captured: list[Any] = []

    await _drain(
        streamOpenAIResponses(
            getModel("openai", modelId),
            context,
            {"apiKey": "sk-fake-key", "cacheRetention": "long", "sessionId": "session-2", "onPayload": stopAfterPayload(captured)},
        )
    )

    assert captured[0]["prompt_cache_key"] == "session-2"
    assert captured[0].get("prompt_cache_retention") == retention
    assert captured[0].get("prompt_cache_options") == cacheOptions


# --- OpenAI Completions Provider ----------------------------------------------------------------


def createCompletionsModel(compat: dict[str, Any] | None = None) -> Model:
    return Model.model_validate(
        {
            "id": "test-model",
            "name": "Test Model",
            "api": "openai-completions",
            "provider": "test-openai-completions",
            "baseUrl": "https://my-proxy.example.com/v1",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 4096,
            "compat": compat,
        }
    )


async def test_completions_should_set_prompt_cache_retention_for_non_api_openai_com_base_url_by_default():
    captured: list[Any] = []

    await _drain(
        streamOpenAICompletions(
            createCompletionsModel(),
            context,
            {
                "apiKey": "fake-key",
                "cacheRetention": "long",
                "sessionId": "session-completions",
                "onPayload": stopAfterPayload(captured),
            },
        )
    )

    assert captured
    assert captured[0]["prompt_cache_key"] == "session-completions"
    assert captured[0]["prompt_cache_retention"] == "24h"


async def test_completions_should_omit_prompt_cache_retention_when_supports_long_cache_retention_is_false():
    captured: list[Any] = []

    await _drain(
        streamOpenAICompletions(
            createCompletionsModel({"supportsLongCacheRetention": False}),
            context,
            {
                "apiKey": "fake-key",
                "cacheRetention": "long",
                "sessionId": "session-completions-false",
                "onPayload": stopAfterPayload(captured),
            },
        )
    )

    assert captured
    assert captured[0].get("prompt_cache_key") is None
    assert captured[0].get("prompt_cache_retention") is None


@pytest.mark.parametrize("modelId", ["deepseek-v4-flash", "deepseek-v4-pro", "kimi-k2.5", "kimi-k2.6", "minimax-m2.7"])
async def test_completions_should_omit_long_cache_retention_for_opencode(modelId):
    model = getModel("opencode", modelId)
    captured: list[Any] = []

    await _drain(
        streamOpenAICompletions(
            model,
            context,
            {
                "apiKey": "fake-key",
                "cacheRetention": "long",
                "sessionId": "session-opencode-long-cache-unsupported",
                "onPayload": stopAfterPayload(captured),
            },
        )
    )

    assert model.compat.supportsLongCacheRetention is False
    assert captured
    assert captured[0].get("prompt_cache_key") is None
    assert captured[0].get("prompt_cache_retention") is None


@pytest.mark.parametrize("modelId", ["gpt-oss-120b", "qwen-3.8-27b"])
async def test_completions_should_omit_strict_field_on_tools_for_cerebras(modelId):
    model = getModel("cerebras", modelId)
    contextWithTools = Context(
        messages=[
            SystemMessage(
                content="test",
                toolsAdded=[
                    Tool(
                        name="t1",
                        description="strict tool",
                        parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
                        constrainedSampling={"type": "json_schema"},
                    ),
                    Tool(
                        name="t2",
                        description="non-strict tool",
                        parameters={"type": "object", "properties": {"y": {"type": "string"}}, "required": ["y"]},
                    ),
                ],
                timestamp=0,
            ),
            UserMessage(content="hello", timestamp=1),
        ]
    )
    captured: list[Any] = []

    await _drain(
        streamOpenAICompletions(
            model,
            normalizeContext(contextWithTools),
            {"apiKey": "fake-key", "sessionId": "test", "onPayload": stopAfterPayload(captured)},
        )
    )

    assert model.compat is None or model.compat.supportsStrictMode is None
    assert captured
    tools = captured[0].get("tools")
    assert tools is not None
    for tool in tools:
        assert "strict" not in tool["function"]
