"""Translated from pi's ``packages/ai/test/xai-responses.test.ts`` (v1.1.0).

MISAKA fork: ``vi.spyOn(globalThis, "fetch")`` becomes a patched ``httpx2.AsyncClient.send`` (the HTTP client the
openai SDK ships); the User-Agent is misaka's (``misaka.ai.utils.pi_user_agent``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")

from misaka.ai.api.openai_completions import stream as streamOpenAICompletions
from misaka.ai.api.openai_responses import stream as streamOpenAIResponses
from misaka.ai.models import getSupportedThinkingLevels
from misaka.ai.providers.xai import xaiProvider
from misaka.ai.providers.xai_models import XAI_MODELS
from misaka.ai.types import Context, Model, UserMessage
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.transcript import normalizeContext

PI_USER_AGENT = getPiUserAgent()


def completedResponse(request: httpx2.Request) -> httpx2.Response:
    event = {
        "type": "response.completed",
        "sequence_number": 0,
        "response": {
            "id": "resp_xai_test",
            "status": "completed",
            "output": [],
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
            },
        },
    }
    return httpx2.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode(),
        request=request,
    )


def matchesObject(actual: Any, expected: Any) -> bool:
    """`expect(actual).toMatchObject(expected)`: every expected key matches, recursively; extra keys are allowed."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and matchesObject(actual[key], value) for key, value in expected.items()
        )
    return actual == expected


customCompletionsModel = Model.model_validate(
    {
        "id": "grok-custom",
        "name": "Grok Custom",
        "api": "openai-completions",
        "provider": "xai",
        "baseUrl": "https://api.x.ai/v1",
        "reasoning": False,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 128000,
        "maxTokens": 16384,
    }
)


def stubSend(monkeypatch: pytest.MonkeyPatch, respond: Any) -> list[httpx2.Request]:
    requests: list[httpx2.Request] = []

    async def send(self: httpx2.AsyncClient, request: httpx2.Request, **_kwargs: Any) -> httpx2.Response:
        requests.append(request)
        return respond(request)

    monkeypatch.setattr(httpx2.AsyncClient, "send", send)
    return requests


async def captureCompletionsUserAgent(monkeypatch: pytest.MonkeyPatch, headers: dict[str, str] | None = None) -> str:
    def respond(request: httpx2.Request) -> httpx2.Response:
        chunks = [
            {"id": "chatcmpl-ua", "choices": [{"delta": {"content": "ok"}, "finish_reason": None, "index": 0}]},
            {
                "id": "chatcmpl-ua",
                "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        ]
        body = "\n\n".join(f"data: {json.dumps(chunk)}" for chunk in chunks) + "\n\ndata: [DONE]\n\n"
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode(), request=request)

    requests = stubSend(monkeypatch, respond)

    result = await streamOpenAICompletions(
        customCompletionsModel,
        normalizeContext(Context(messages=[UserMessage(content="hello", timestamp=1)])),
        {"apiKey": "xai-test-token", "headers": headers},
    ).result()

    assert result.stopReason == "stop", result.errorMessage
    return requests[-1].headers.get("user-agent")


async def captureRequest(monkeypatch: pytest.MonkeyPatch, model: Model, context: Context, options: dict[str, Any]):
    requests = stubSend(monkeypatch, completedResponse)

    result = await xaiProvider().stream(model, normalizeContext(context), options).result()
    assert result.stopReason == "stop", result.errorMessage
    assert requests
    request = requests[-1]
    return {"url": str(request.url), "headers": request.headers, "body": json.loads(request.content)}


def test_excludes_retired_and_redundant_models_from_the_built_in_catalog():
    for modelId in [
        "grok-3",
        "grok-3-fast",
        "grok-4.20-0309-non-reasoning",
        "grok-4.20-0309-reasoning",
        "grok-build-0.1",
        "grok-code-fast-1",
    ]:
        assert modelId not in XAI_MODELS


def test_routes_every_built_in_xai_model_through_responses():
    for model in XAI_MODELS.values():
        assert model.api == "openai-responses", model.id
    assert getSupportedThinkingLevels(XAI_MODELS["grok-4.5"]) == ["low", "medium", "high"]
    assert getSupportedThinkingLevels(XAI_MODELS["grok-4.6"]) == ["low", "medium", "high", "xhigh"]
    assert getSupportedThinkingLevels(XAI_MODELS["grok-4.7"]) == ["low", "medium", "high", "xhigh"]
    assert getSupportedThinkingLevels(XAI_MODELS["grok-4.3"]) == ["off", "low", "medium", "high"]


def test_includes_grok_4_7_capabilities_and_long_context_pricing():
    model = XAI_MODELS["grok-4.7"]
    assert (model.api, model.reasoning, model.input, model.contextWindow, model.maxTokens) == (
        "openai-responses",
        True,
        ["text", "image"],
        500000,
        500000,
    )
    assert model.cost.model_dump(exclude_none=True) == {
        "input": 2,
        "output": 6,
        "cacheRead": 0.5,
        "cacheWrite": 0,
        "tiers": [{"inputTokensAbove": 200000, "input": 4, "output": 12, "cacheRead": 1, "cacheWrite": 0}],
    }


async def test_uses_responses_with_bearer_auth_and_xai_compatible_request_fields(monkeypatch):
    captured = await captureRequest(
        monkeypatch,
        XAI_MODELS["grok-4.5"],
        Context(
            systemPrompt="You are a careful coding assistant.",
            messages=[UserMessage(content="hello", timestamp=1)],
        ),
        {
            "apiKey": "xai-test-token",
            "sessionId": "pi-session-123",
            "cacheRetention": "long",
            "reasoningEffort": "medium",
        },
    )

    assert captured["url"] == "https://api.x.ai/v1/responses"
    assert captured["headers"].get("authorization") == "Bearer xai-test-token"
    assert captured["headers"].get("user-agent") == PI_USER_AGENT
    assert captured["headers"].get("session_id") == "pi-session-123"
    body = captured["body"]
    assert matchesObject(
        body,
        {
            "model": "grok-4.5",
            "store": False,
            "stream": True,
            "prompt_cache_key": "pi-session-123",
            "reasoning": {"effort": "medium"},
            "include": ["reasoning.encrypted_content"],
        },
    )
    assert "prompt_cache_retention" not in body
    assert any(
        item.get("role") == "developer" and item.get("content") == "You are a careful coding assistant."
        for item in body["input"]
    )


async def test_requests_encrypted_reasoning_without_an_effort_override(monkeypatch):
    captured = await captureRequest(
        monkeypatch,
        XAI_MODELS["grok-4.5"],
        Context(messages=[UserMessage(content="hello", timestamp=1)]),
        {"apiKey": "xai-test-token"},
    )

    body = captured["body"]
    assert matchesObject(body, {"model": "grok-4.5", "store": False, "include": ["reasoning.encrypted_content"]})
    assert "reasoning" not in body


async def test_uses_responses_for_grok_4_7_with_xhigh_effort_and_encrypted_reasoning(monkeypatch):
    captured = await captureRequest(
        monkeypatch,
        XAI_MODELS["grok-4.7"],
        Context(
            systemPrompt="You are a careful coding assistant.",
            messages=[UserMessage(content="hello", timestamp=1)],
        ),
        {"apiKey": "xai-test-token", "reasoningEffort": "xhigh"},
    )

    assert captured["url"] == "https://api.x.ai/v1/responses"
    assert matchesObject(
        captured["body"],
        {
            "model": "grok-4.7",
            "store": False,
            "stream": True,
            "reasoning": {"effort": "xhigh"},
            "include": ["reasoning.encrypted_content"],
        },
    )


async def test_uses_responses_for_grok_4_3(monkeypatch):
    captured = await captureRequest(
        monkeypatch,
        XAI_MODELS["grok-4.3"],
        Context(messages=[UserMessage(content="hello", timestamp=1)]),
        {"apiKey": "xai-test-token", "reasoningEffort": "low"},
    )

    assert captured["url"] == "https://api.x.ai/v1/responses"
    assert matchesObject(
        captured["body"],
        {
            "model": "grok-4.3",
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": "low"},
        },
    )


async def test_uses_pis_user_agent_by_default_for_responses_requests(monkeypatch):
    requests = stubSend(monkeypatch, completedResponse)

    openaiModel = XAI_MODELS["grok-4.5"].model_copy(update={"provider": "openai", "baseUrl": "https://api.openai.com/v1"})
    result = await streamOpenAIResponses(
        openaiModel,
        normalizeContext(Context(messages=[UserMessage(content="hello", timestamp=1)])),
        {"apiKey": "test-token"},
    ).result()

    assert result.stopReason == "stop", result.errorMessage
    assert requests[-1].headers.get("user-agent") == PI_USER_AGENT


async def test_lets_explicit_headers_override_the_default_responses_user_agent(monkeypatch):
    captured = await captureRequest(
        monkeypatch,
        XAI_MODELS["grok-4.5"],
        Context(messages=[UserMessage(content="hello", timestamp=1)]),
        {"apiKey": "xai-test-token", "headers": {"User-Agent": "custom-agent"}},
    )

    assert captured["headers"].get("user-agent") == "custom-agent"


async def test_uses_pis_user_agent_by_default_for_completions_requests(monkeypatch):
    assert await captureCompletionsUserAgent(monkeypatch) == PI_USER_AGENT


async def test_lets_explicit_headers_override_the_default_completions_user_agent(monkeypatch):
    assert await captureCompletionsUserAgent(monkeypatch, {"User-Agent": "custom-agent"}) == "custom-agent"
