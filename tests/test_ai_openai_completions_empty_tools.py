"""Translated from pi's ``packages/ai/test/openai-completions-empty-tools.test.ts`` (v1.1.0).

Empty tools arrays must NOT be serialized as `tools: []` -- some OpenAI-compatible backends (e.g. DashScope / Aliyun Qwen
via compatible-mode) reject the request with `"[] is too short - 'tools'"` (HTTP 400) when `--no-tools` produces an
empty array. Regression for https://github.com/earendil-works/pi/issues/3649
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.compat import getModel, streamSimple
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
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


@pytest.fixture
def cloudflareEnv(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_KEY", "cf-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "account-id")
    monkeypatch.setenv("CLOUDFLARE_GATEWAY_ID", "gateway-id")


def _now() -> int:
    return time.time_ns() // 1_000_000


def _completionsModel(**fields: Any) -> Model:
    baseModel = getModel("openai", "gpt-4o-mini").model_dump(exclude={"compat"})
    return Model.model_validate({**baseModel, "api": "openai-completions", **fields})


def _hi(content: str = "hi", **context: Any) -> Context:
    return Context(messages=[UserMessage(content=content, timestamp=_now())], **context)


async def test_omits_tools_field_when_context_tools_is_an_empty_array(mockState):
    await streamSimple(_completionsModel(), _hi(tools=[]), {"apiKey": "test"}).result()

    assert "tools" not in mockState.lastParams


async def test_omits_tools_field_when_context_tools_is_undefined(mockState):
    await streamSimple(_completionsModel(), _hi(), {"apiKey": "test"}).result()

    assert "tools" not in mockState.lastParams


async def test_sends_default_max_tokens(mockState):
    model = _completionsModel()

    await streamSimple(model, _hi(), {"apiKey": "test"}).result()

    assert "max_tokens" not in mockState.lastParams
    assert mockState.lastParams["max_completion_tokens"] == model.maxTokens


async def test_sends_explicit_max_tokens(mockState):
    await streamSimple(_completionsModel(), _hi(), {"apiKey": "test", "maxTokens": 1234}).result()

    assert "max_tokens" not in mockState.lastParams
    assert mockState.lastParams["max_completion_tokens"] == 1234


async def test_clamps_default_max_tokens_to_remaining_context(mockState):
    model = _completionsModel(contextWindow=10000, maxTokens=8000)

    await streamSimple(model, _hi("x" * 8000), {"apiKey": "test"}).result()

    assert "max_tokens" not in mockState.lastParams
    assert mockState.lastParams["max_completion_tokens"] == 3618


async def test_clamps_explicit_max_tokens_to_remaining_context(mockState):
    model = _completionsModel(contextWindow=10000, maxTokens=8000)

    await streamSimple(model, _hi("x" * 8000), {"apiKey": "test", "maxTokens": 7000}).result()

    assert "max_tokens" not in mockState.lastParams
    assert mockState.lastParams["max_completion_tokens"] == 3618


async def test_uses_conservative_openai_compatible_fields_for_cloudflare_ai_gateway_compat_models(mockState, cloudflareEnv):
    model = getModel("cloudflare-ai-gateway", "workers-ai/@cf/moonshotai/kimi-k2.6")

    await streamSimple(
        model,
        Context(systemPrompt="You are helpful.", messages=[UserMessage(content="hi", timestamp=_now())]),
        {"maxTokens": 1234, "reasoning": "high"},
    ).result()

    params = mockState.lastParams
    assert params["messages"][0]["role"] == "system"
    assert params["max_tokens"] == 1234
    assert "max_completion_tokens" not in params
    assert "reasoning_effort" not in params
    assert "store" not in params

    clientOptions = mockState.lastClientOptions
    assert clientOptions["baseURL"] == "https://gateway.ai.cloudflare.com/v1/account-id/gateway-id/compat"
    assert "Authorization" in clientOptions["defaultHeaders"]
    assert clientOptions["defaultHeaders"]["Authorization"] is None
    assert clientOptions["defaultHeaders"]["cf-aig-authorization"] == "Bearer cf-token"


async def test_resolves_cloudflare_ai_gateway_base_url_through_provider_auth(mockState, cloudflareEnv):
    model = getModel("cloudflare-ai-gateway", "workers-ai/@cf/moonshotai/kimi-k2.6")

    await streamSimple(model, _hi()).result()

    assert mockState.lastClientOptions["baseURL"] == "https://gateway.ai.cloudflare.com/v1/account-id/gateway-id/compat"


async def test_preserves_inline_upstream_authorization_for_cloudflare_ai_gateway_byok_requests(mockState, cloudflareEnv):
    model = getModel("cloudflare-ai-gateway", "gpt-5.1")

    await streamSimple(model, _hi(), {"headers": {"Authorization": "Bearer upstream-token"}}).result()

    defaultHeaders = mockState.lastClientOptions["defaultHeaders"]
    assert defaultHeaders["Authorization"] == "Bearer upstream-token"
    assert defaultHeaders["cf-aig-authorization"] == "Bearer cf-token"


async def test_sends_session_affinity_headers_for_workers_ai_through_cloudflare_ai_gateway(mockState, cloudflareEnv):
    workersModel = getModel("cloudflare-ai-gateway", "workers-ai/@cf/moonshotai/kimi-k2.6")

    await streamSimple(workersModel, _hi(), {"sessionId": "session-1"}).result()

    defaultHeaders = mockState.lastClientOptions["defaultHeaders"]
    assert defaultHeaders["session_id"] == "session-1"
    assert defaultHeaders["x-client-request-id"] == "session-1"
    assert defaultHeaders["x-session-affinity"] == "session-1"


async def test_still_emits_empty_tools_for_anthropic_litellm_proxy_when_conversation_has_tool_history(mockState):
    context = Context(
        messages=[
            UserMessage(content="use the tool", timestamp=_now()),
            AssistantMessage(
                content=[ToolCall(id="t1", name="noop", arguments={})],
                stopReason="toolUse",
                usage=Usage(
                    input=0,
                    output=0,
                    cacheRead=0,
                    cacheWrite=0,
                    totalTokens=0,
                    cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
                ),
                api="openai-completions",
                provider="openai",
                model="gpt-4o-mini",
                timestamp=_now(),
            ),
            ToolResultMessage(toolCallId="t1", toolName="noop", content=[TextContent(text="done")], isError=False, timestamp=_now()),
        ],
        tools=[],
    )

    await streamSimple(_completionsModel(), context, {"apiKey": "test"}).result()

    assert isinstance(mockState.lastParams["tools"], list)
    assert mockState.lastParams["tools"] == []
