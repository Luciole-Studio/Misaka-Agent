"""Translated from pi's ``packages/ai/test/openai-completions-prompt-cache.test.ts`` (v1.1.0)."""

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
    monkeypatch.delenv("PI_CACHE_RETENTION", raising=False)
    return installFakeOpenAI(monkeypatch, DEFAULT_CHUNKS)


def createModel(**overrides: Any) -> Model:
    baseModel = getModel("openai", "gpt-4o-mini").model_dump(exclude={"compat"})
    return Model.model_validate({**baseModel, "api": "openai-completions", **overrides})


async def captureRequest(mockState: Any, options: dict[str, Any] | None = None, model: Model | None = None):
    await streamOpenAICompletions(
        model if model is not None else createModel(),
        normalizeContext(Context(systemPrompt="sys", messages=[UserMessage(content="hi", timestamp=time.time_ns() // 1_000_000)])),
        {"apiKey": "test-key", **(options or {})},
    ).result()

    return mockState.lastParams, (mockState.lastClientOptions or {}).get("defaultHeaders") or {}


async def test_sets_prompt_cache_key_for_direct_openai_requests_when_caching_is_enabled(mockState):
    payload, _headers = await captureRequest(mockState, {"sessionId": "session-123"})

    assert payload["prompt_cache_key"] == "session-123"
    assert payload.get("prompt_cache_retention") is None


async def test_sets_prompt_cache_retention_to_24h_for_direct_openai_requests_when_cache_retention_is_long(mockState):
    payload, _headers = await captureRequest(mockState, {"cacheRetention": "long", "sessionId": "session-456"})

    assert payload["prompt_cache_key"] == "session-456"
    assert payload["prompt_cache_retention"] == "24h"


async def test_clamps_prompt_cache_key_to_openais_64_character_limit(mockState):
    payload, _headers = await captureRequest(mockState, {"sessionId": "x" * 67})

    assert payload["prompt_cache_key"] == "x" * 64


async def test_omits_prompt_cache_fields_when_cache_retention_is_none(mockState):
    payload, _headers = await captureRequest(mockState, {"cacheRetention": "none", "sessionId": "session-789"})

    assert payload.get("prompt_cache_key") is None
    assert payload.get("prompt_cache_retention") is None


async def test_omits_prompt_cache_fields_for_non_openai_base_urls_without_compatible_long_retention(mockState):
    model = createModel(baseUrl="https://proxy.example.com/v1", compat={"supportsLongCacheRetention": False})
    payload, _headers = await captureRequest(mockState, {"cacheRetention": "long", "sessionId": "session-proxy"}, model)

    assert payload.get("prompt_cache_key") is None
    assert payload.get("prompt_cache_retention") is None


async def test_uses_pi_cache_retention_for_direct_openai_requests(mockState, monkeypatch):
    monkeypatch.setenv("PI_CACHE_RETENTION", "long")
    payload, _headers = await captureRequest(mockState, {"sessionId": "session-env"})

    assert payload["prompt_cache_key"] == "session-env"
    assert payload["prompt_cache_retention"] == "24h"


async def test_sends_known_session_affinity_headers_when_compat_send_session_affinity_headers_is_enabled(mockState):
    model = createModel(baseUrl="https://proxy.example.com/v1", compat={"sendSessionAffinityHeaders": True})
    _payload, headers = await captureRequest(mockState, {"sessionId": "session-affinity"}, model)

    assert headers["session_id"] == "session-affinity"
    assert headers["x-client-request-id"] == "session-affinity"
    assert headers["x-session-affinity"] == "session-affinity"


@pytest.mark.parametrize("modelId", ["accounts/fireworks/models/glm-5p3", "accounts/fireworks/routers/glm-5p3-fast"])
async def test_sends_fireworks_session_affinity(mockState, modelId):
    _payload, headers = await captureRequest(mockState, {"sessionId": "fireworks-session"}, getModel("fireworks", modelId))

    assert headers["x-session-affinity"] == "fireworks-session"


async def test_sends_baseten_session_affinity_for_built_in_catalog_models(mockState):
    _payload, headers = await captureRequest(
        mockState, {"sessionId": "baseten-catalog-session"}, getModel("baseten", "zai-org/GLM-5.2")
    )

    assert headers["x-session-affinity"] == "baseten-catalog-session"
    assert headers["x-client-request-id"] == "baseten-catalog-session"


async def test_uses_openai_no_session_format_when_configured(mockState):
    model = createModel(compat={"sendSessionAffinityHeaders": True, "sessionAffinityFormat": "openai-nosession"})
    payload, headers = await captureRequest(mockState, {"sessionId": "session-nosession"}, model)

    assert "session_id" not in payload
    assert payload["prompt_cache_key"] == "session-nosession"
    assert "session_id" not in headers
    assert headers["x-client-request-id"] == "session-nosession"
    assert headers["x-session-affinity"] == "session-nosession"
    assert "x-session-id" not in headers


async def test_uses_openrouter_session_affinity_header_when_configured(mockState):
    model = createModel(
        baseUrl="https://proxy.example.com/v1",
        compat={"sendSessionAffinityHeaders": True, "sessionAffinityFormat": "openrouter"},
    )
    payload, headers = await captureRequest(mockState, {"sessionId": "session-proxy"}, model)

    assert "session_id" not in payload
    assert payload.get("prompt_cache_key") is None
    assert headers["x-session-id"] == "session-proxy"
    assert "session_id" not in headers
    assert "x-client-request-id" not in headers
    assert "x-session-affinity" not in headers


async def test_sends_openrouter_session_affinity_header_by_default_for_built_in_openrouter_models(mockState):
    payload, headers = await captureRequest(mockState, {"sessionId": "session-openrouter"}, getModel("openrouter", "auto"))

    assert "session_id" not in payload
    assert payload.get("prompt_cache_key") is None
    assert headers["x-session-id"] == "session-openrouter"
    assert "session_id" not in headers
    assert "x-client-request-id" not in headers
    assert "x-session-affinity" not in headers


async def test_omits_openrouter_session_affinity_data_when_disabled(mockState):
    model = createModel(
        provider="openrouter", baseUrl="https://openrouter.ai/api/v1", compat={"sendSessionAffinityHeaders": False}
    )
    payload, headers = await captureRequest(mockState, {"sessionId": "session-openrouter"}, model)

    assert "session_id" not in payload
    assert payload.get("prompt_cache_key") is None
    assert "x-session-id" not in headers


async def test_omits_session_affinity_headers_when_cache_retention_is_none(mockState):
    model = createModel(baseUrl="https://proxy.example.com/v1", compat={"sendSessionAffinityHeaders": True})
    _payload, headers = await captureRequest(mockState, {"cacheRetention": "none", "sessionId": "session-affinity"}, model)

    assert "session_id" not in headers
    assert "x-client-request-id" not in headers
    assert "x-session-affinity" not in headers


async def test_lets_explicit_headers_override_generated_session_affinity_headers(mockState):
    model = createModel(baseUrl="https://proxy.example.com/v1", compat={"sendSessionAffinityHeaders": True})
    _payload, headers = await captureRequest(
        mockState,
        {
            "sessionId": "session-affinity",
            "headers": {
                "session_id": "override-session",
                "x-client-request-id": "override-request",
                "x-session-affinity": "override-affinity",
            },
        },
        model,
    )

    assert headers["session_id"] == "override-session"
    assert headers["x-client-request-id"] == "override-request"
    assert headers["x-session-affinity"] == "override-affinity"
