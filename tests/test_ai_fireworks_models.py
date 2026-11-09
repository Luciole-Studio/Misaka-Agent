"""Translated from pi's ``packages/ai/test/fireworks-models.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.ai.api.anthropic_messages import stream as streamAnthropic
from misaka.ai.compat import getModel
from misaka.ai.env_api_keys import findEnvKeys, getEnvApiKey
from misaka.ai.models import getSupportedThinkingLevels
from misaka.ai.types import Context, Model, Tool, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.values import read_field
from tests.http_capture import captureServer
from tests.payload_capture import captureSimplePayload


def plain(value: Any) -> Any:
    """``toEqual`` ignores undefined properties; a pydantic compat is compared by its set fields."""
    return value.model_dump(exclude_none=True) if hasattr(value, "model_dump") else value


def userContext() -> Context:
    return Context(messages=[UserMessage(content="test", timestamp=0)])


# --- Fireworks models ---------------------------------------------------------------------------


def test_registers_non_glm_non_kimi_k3_models_via_anthropic_compatible_messages_api():
    model = getModel("fireworks", "accounts/fireworks/models/deepseek-v4p1-flash")

    assert model is not None
    assert model.api == "anthropic-messages"
    assert model.provider == "fireworks"
    assert model.baseUrl == "https://api.fireworks.ai/inference"
    assert model.reasoning is True
    assert model.input == ["text", "image"]


def test_aligns_glm_5_3_fast_with_glm_5_3_openai_compatible_config():
    base = getModel("fireworks", "accounts/fireworks/models/glm-5p3")
    fast = getModel("fireworks", "accounts/fireworks/routers/glm-5p3-fast")

    assert fast.api == base.api
    assert fast.baseUrl == base.baseUrl
    assert plain(fast.compat) == plain(base.compat)
    assert fast.thinkingLevelMap == base.thinkingLevelMap


@pytest.mark.parametrize("modelId", ["accounts/fireworks/models/glm-5p3", "accounts/fireworks/routers/glm-5p3-fast"])
async def test_omits_unsupported_long_cache_retention(modelId: str):
    model = getModel("fireworks", modelId)
    payload = await captureSimplePayload(
        model, userContext(), {"cacheRetention": "long", "sessionId": "test-fireworks-session"}
    )

    assert payload is not None
    # None is pi's undefined here: split_sdk_params drops it before the request.
    assert payload.get("prompt_cache_retention") is None


async def test_routes_kimi_k3_through_the_openai_compatible_api_with_native_effort_controls():
    base = getModel("fireworks", "accounts/fireworks/models/kimi-k3")
    fast = getModel("fireworks", "accounts/fireworks/routers/kimi-k3-fast")
    compat = {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsStrictMode": True,
        "requiresReasoningContentOnAssistantMessages": True,
        "thinkingFormat": "openai",
        "supportsMidConvoSystemMessages": True,
        "supportsMidConvoToolAdditions": True,
        "sendSessionAffinityHeaders": True,
        "supportsLongCacheRetention": False,
    }
    thinkingLevelMap = {
        "off": None,
        "minimal": None,
        "low": "low",
        "medium": None,
        "high": "high",
        "xhigh": None,
        "max": "max",
    }

    assert base.api == "openai-completions"
    assert base.baseUrl == "https://api.fireworks.ai/inference/v1"
    assert plain(base.compat) == compat
    assert base.thinkingLevelMap == thinkingLevelMap
    assert fast.api == base.api
    assert fast.baseUrl == base.baseUrl
    assert plain(fast.compat) == compat
    assert fast.thinkingLevelMap == thinkingLevelMap

    payload = await captureSimplePayload(base, userContext(), {"reasoning": "max"})

    assert payload.get("reasoning_effort") == "max"


# Regression for #9323: native effort must reach Messages without budget-based fallback.
@pytest.mark.parametrize(
    ("modelId", "levels"),
    [
        ("accounts/fireworks/models/deepseek-v4p1-flash", ["off", "low", "high", "max"]),
        ("accounts/fireworks/models/qwen3p8-max", ["off", "low", "medium", "xhigh"]),
        ("accounts/fireworks/models/qwen3p8-2p4t-a95b", ["off", "low", "medium", "xhigh"]),
    ],
)
async def test_sends_native_messages_effort_levels(modelId: str, levels: list[str]):
    model = getModel("fireworks", modelId)
    assert model.api == "anthropic-messages"
    assert read_field(model.compat, "forceAdaptiveThinking") is True
    assert getSupportedThinkingLevels(model) == levels

    for level in levels:
        payload = await captureSimplePayload(
            model, userContext(), {} if level == "off" else {"reasoning": level}
        )
        assert payload is not None
        assert payload.get("thinking") == (
            {"type": "disabled"} if level == "off" else {"type": "adaptive", "display": "summarized"}
        )
        assert payload.get("output_config") == (None if level == "off" else {"effort": level})


# Regression for #9323: accepted aliases are not distinct native effort levels.
@pytest.mark.parametrize(
    ("modelId", "levels"),
    [
        ("accounts/fireworks/models/glm-5p3", ["low", "high", "max"]),
        ("accounts/fireworks/routers/glm-5p3-fast", ["low", "high", "max"]),
        ("accounts/fireworks/models/kimi-k3", ["low", "high", "max"]),
        ("accounts/fireworks/routers/kimi-k3-fast", ["low", "high", "max"]),
    ],
)
def test_exposes_distinct_native_effort_levels(modelId: str, levels: list[str]):
    assert getSupportedThinkingLevels(getModel("fireworks", modelId)) == levels


async def test_keeps_toggle_only_messages_models_without_a_verified_fallback_on_budget_based_thinking():
    model = getModel("fireworks", "accounts/fireworks/models/nemotron-3-ultra-nvfp4")
    assert read_field(model.compat, "forceAdaptiveThinking") is None

    payload = await captureSimplePayload(model, userContext(), {"reasoning": "high"})

    assert payload.get("thinking") == {"type": "enabled", "budget_tokens": 16384, "display": "summarized"}
    assert payload.get("output_config") is None


def test_resolves_fireworks_api_key_from_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-fireworks-key")

    assert findEnvKeys("fireworks") == ["FIREWORKS_API_KEY"]
    assert getEnvApiKey("fireworks") == "test-fireworks-key"


def test_sets_fireworks_specific_compat_for_session_affinity_and_unsupported_tool_fields():
    model = getModel("fireworks", "accounts/fireworks/models/nemotron-3-ultra-nvfp4")

    assert model.compat is not None
    assert read_field(model.compat, "sendSessionAffinityHeaders") is True
    assert read_field(model.compat, "supportsEagerToolInputStreaming") is False
    assert read_field(model.compat, "supportsCacheControlOnTools") is False
    assert read_field(model.compat, "supportsLongCacheRetention") is False
    assert read_field(model.compat, "allowEmptySignature") is True


# --- Integration tests for Fireworks Anthropic session affinity and tool compat ---

tool = Tool(
    name="lookup",
    description="Look up a value",
    parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
)

FIREWORKS_ANTHROPIC_COMPAT = {
    "allowEmptySignature": True,
    "sendSessionAffinityHeaders": True,
    "supportsEagerToolInputStreaming": False,
    "supportsCacheControlOnTools": False,
    "supportsLongCacheRetention": False,
}


def createFireworksModel(compat: dict[str, Any] | None = FIREWORKS_ANTHROPIC_COMPAT) -> Model:
    return Model.model_validate({
        "id": "accounts/fireworks/models/kimi-k2p6",
        "name": "Kimi K2.6",
        "api": "anthropic-messages",
        "provider": "fireworks",
        "baseUrl": "http://127.0.0.1:0",  # overridden by captureAnthropicRequest
        "reasoning": True,
        "input": ["text", "image"],
        "cost": {"input": 0.95, "output": 4, "cacheRead": 0.16, "cacheWrite": 0},
        "contextWindow": 262000,
        "maxTokens": 262000,
        **({"compat": compat} if compat is not None else {}),
    })


def createAnthropicModel() -> Model:
    return Model.model_validate({
        "id": "claude-opus-4-8",
        "name": "Claude Opus 4.8",
        "api": "anthropic-messages",
        "provider": "anthropic",
        "baseUrl": "http://127.0.0.1:0",  # overridden by captureAnthropicRequest
        "reasoning": True,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 200000,
        "maxTokens": 32000,
    })


def createOpenRouterModel() -> Model:
    return createAnthropicModel().model_copy(
        update={"id": "anthropic/claude-opus-4.8", "provider": "openrouter", "baseUrl": "https://openrouter.ai/api"}
    )


def createContext(tools: list[Tool] | None = None) -> Context:
    tools = [tool] if tools is None else tools
    return Context(
        messages=[UserMessage(content="Use the tool", timestamp=int(time.time() * 1000))],
        **({"tools": tools} if len(tools) > 0 else {}),
    )


async def captureAnthropicRequest(model: Model, context: Context, **options: Any) -> Any:
    with captureServer() as server:
        # Override the model's baseUrl to point to the local test server
        localModel = model.model_copy(update={"baseUrl": server.baseUrl})

        stream = streamAnthropic(
            localModel,
            normalizeContext(context),
            {
                "apiKey": "test-key",
                "cacheRetention": options.get("cacheRetention") or "short",
                "sessionId": options.get("sessionId"),
            },
        )
        async for event in stream:
            if event.type in ("done", "error"):
                break

    if not server.requests:
        raise RuntimeError("Anthropic request was not captured")
    return server.requests[0]


def getTools(body: dict[str, Any]) -> list[dict[str, Any]]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        raise TypeError("Expected tools in request body")
    return tools


async def test_sends_x_session_affinity_header_for_fireworks_models():
    request = await captureAnthropicRequest(createFireworksModel(), createContext(), sessionId="fireworks-session-1")

    assert request.headers.get("x-session-affinity") == "fireworks-session-1"


async def test_omits_x_session_affinity_header_for_native_anthropic_models():
    request = await captureAnthropicRequest(createAnthropicModel(), createContext(), sessionId="anthropic-session-1")

    assert "x-session-affinity" not in request.headers


async def test_omits_x_session_affinity_header_when_cache_retention_is_none():
    request = await captureAnthropicRequest(
        createFireworksModel(), createContext(), sessionId="fireworks-session-2", cacheRetention="none"
    )

    assert "x-session-affinity" not in request.headers


# Regression test for https://github.com/earendil-works/pi/issues/9102
async def test_sends_only_x_session_id_for_openrouter_models():
    request = await captureAnthropicRequest(createOpenRouterModel(), createContext(), sessionId="openrouter-session-1")

    assert request.headers.get("x-session-id") == "openrouter-session-1"
    assert "x-session-affinity" not in request.headers


async def test_omits_openrouter_session_headers_when_cache_retention_is_none():
    request = await captureAnthropicRequest(
        createOpenRouterModel(), createContext(), sessionId="openrouter-session-2", cacheRetention="none"
    )

    assert "x-session-id" not in request.headers
    assert "x-session-affinity" not in request.headers


async def test_allows_openrouter_session_headers_to_be_disabled():
    model = createOpenRouterModel().model_copy(update={"compat": {"sendSessionAffinityHeaders": False}})
    model = Model.model_validate(model.model_dump(exclude_none=True))
    request = await captureAnthropicRequest(model, createContext(), sessionId="openrouter-session-3")

    assert "x-session-id" not in request.headers


async def test_omits_cache_control_on_tools_for_fireworks_models():
    request = await captureAnthropicRequest(createFireworksModel(), createContext())

    tools = getTools(request.body)
    assert "cache_control" not in tools[-1]


async def test_omits_eager_input_streaming_on_tools_for_fireworks_models():
    request = await captureAnthropicRequest(createFireworksModel(), createContext())

    for t in getTools(request.body):
        assert "eager_input_streaming" not in t


async def test_sends_cache_control_on_tools_for_native_anthropic_models():
    request = await captureAnthropicRequest(createAnthropicModel(), createContext())

    lastTool = getTools(request.body)[-1]
    assert lastTool.get("cache_control") is not None
    assert lastTool["cache_control"]["type"] == "ephemeral"


async def test_sends_eager_input_streaming_on_tools_for_native_anthropic_models():
    request = await captureAnthropicRequest(createAnthropicModel(), createContext())

    assert getTools(request.body)[0]["eager_input_streaming"] is True
