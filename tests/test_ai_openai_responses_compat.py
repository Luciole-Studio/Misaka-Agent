"""Translated from pi's ``packages/ai/test/openai-responses-compat.test.ts`` (v1.1.0).

pi mocks ``globalThis.fetch``; the Python SDK sends through its own transport, so the requests go to a local server
(``tests.http_capture``) and the model's ``baseUrl`` points there.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from misaka.ai.api.openai_responses import stream as streamOpenAIResponses
from misaka.ai.compat import getModel
from misaka.ai.types import Context, Model, Tool, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.http_capture import Reply, captureServer

DONE = Reply(body=b"data: [DONE]\n\n")


def _at(model: Model, baseUrl: str, **fields: Any) -> Model:
    return Model.model_validate({**model.model_dump(), "baseUrl": baseUrl, **fields})


def _hiContext() -> Context:
    return Context(systemPrompt="sys", messages=[UserMessage(content="hi", timestamp=time.time_ns() // 1_000_000)])


async def _drain(eventStream: Any) -> None:
    async for event in eventStream:
        if event.type in ("done", "error"):
            break


async def _capturePayload(model: Model, context: Context, options: dict[str, Any] | None = None) -> Any:
    captured: list[Any] = []
    with captureServer(lambda _request: DONE) as server:
        await _drain(
            streamOpenAIResponses(
                _at(model, server.baseUrl),
                normalizeContext(context),
                {"apiKey": "sk-test-key", **(options or {}), "onPayload": lambda payload, _model: captured.append(payload)},
            )
        )
    return captured[0] if captured else None


async def _captureOpenAIResponseHeaders(options: dict[str, Any], model: Model | None = None) -> dict[str, str | None]:
    model = model if model is not None else getModel("openai", "gpt-5.4")
    with captureServer(lambda _request: DONE) as server:
        await _drain(
            streamOpenAIResponses(
                _at(model, server.baseUrl),
                normalizeContext(_hiContext()),
                {"apiKey": "sk-test-key", **options},
            )
        )
        headers = server.requests[0].headers
    return {
        "sessionId": headers.get("session_id"),
        "clientRequestId": headers.get("x-client-request-id"),
        "xSessionId": headers.get("x-session-id"),
    }


# --- openai-responses provider defaults ---------------------------------------------------------


async def test_omits_reasoning_when_no_reasoning_is_requested():
    capturedPayload = await _capturePayload(getModel("github-copilot", "gpt-5-mini"), _hiContext())

    assert capturedPayload is not None
    assert "reasoning" not in capturedPayload


async def test_forwards_required_tool_choice():
    context = Context(
        messages=[
            UserMessage(content="Do not call ping. Respond with text instead.", timestamp=time.time_ns() // 1_000_000)
        ],
        tools=[
            Tool(
                name="ping",
                description="Ping",
                parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
            )
        ],
    )

    capturedPayload = await _capturePayload(getModel("openai", "gpt-5.4"), context, {"toolChoice": "required"})

    assert capturedPayload["tool_choice"] == "required"
    assert [tool["name"] for tool in capturedPayload["tools"]] == ["ping"]


async def test_sets_strict_mode_explicitly_for_cloudflare_openai_responses_tools():
    model = getModel("cloudflare-ai-gateway", "gpt-5.6-sol")
    context = Context(
        messages=[UserMessage(content="Use a tool.", timestamp=time.time_ns() // 1_000_000)],
        tools=[
            Tool(
                name="ordinary",
                description="An ordinary tool",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "offset": {"type": "number"}},
                    "required": ["path"],
                },
            ),
            Tool(
                name="constrained",
                description="A constrained tool",
                parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
                constrainedSampling={"type": "json_schema", "strict": "prefer"},
            ),
        ],
    )

    capturedPayload = await _capturePayload(model, context)

    assert model.compat.supportsStrictMode is True
    assert [(tool["name"], tool["strict"]) for tool in capturedPayload["tools"]] == [
        ("ordinary", False),
        ("constrained", True),
    ]


@pytest.mark.parametrize(
    "modelId",
    [
        "gpt-5.1",
        "gpt-5.2",
        "gpt-5.3-codex",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-6-sol",
        "gpt-6-luna",
    ],
)
async def test_sends_none_reasoning_effort_when_no_reasoning_is_requested(modelId):
    capturedPayload = await _capturePayload(getModel("openai", modelId), _hiContext())

    assert capturedPayload["reasoning"] == {"effort": "none"}


@pytest.mark.parametrize("modelId", ["gpt-5", "gpt-5-mini", "gpt-5-nano", "gpt-5-pro", "gpt-5.2-pro", "gpt-5.4-pro", "gpt-5.5-pro"])
async def test_omits_reasoning_effort_when_off_is_unsupported(modelId):
    capturedPayload = await _capturePayload(getModel("openai", modelId), _hiContext())

    assert "reasoning" not in capturedPayload


async def test_sets_cache_affinity_headers_for_official_openai_requests_with_a_session_id():
    captured = await _captureOpenAIResponseHeaders({"sessionId": "session-123"})

    assert captured["sessionId"] == "session-123"
    assert captured["clientRequestId"] == "session-123"


async def test_clamps_prompt_cache_key_to_openais_64_character_limit():
    capturedPayload = await _capturePayload(getModel("openai", "gpt-5.4"), _hiContext(), {"sessionId": "x" * 67})

    assert capturedPayload["prompt_cache_key"] == "x" * 64


async def test_sets_cache_affinity_headers_for_proxy_openai_requests_with_a_session_id():
    proxyModel = getModel("openai", "gpt-5.4").model_copy(update={"provider": "opencode"})

    captured = await _captureOpenAIResponseHeaders({"sessionId": "session-123"}, proxyModel)

    assert captured["sessionId"] == "session-123"
    assert captured["clientRequestId"] == "session-123"


async def _captureWithPayload(sessionId: str, model: Model) -> tuple[dict[str, str | None], Any]:
    payloads: list[Any] = []
    captured = await _captureOpenAIResponseHeaders(
        {"sessionId": sessionId, "onPayload": lambda payload, _model: payloads.append(payload)}, model
    )
    return captured, payloads[0]


async def test_uses_openrouter_session_affinity_header_when_configured():
    proxyModel = Model.model_validate(
        {**getModel("openai", "gpt-5.4").model_dump(), "provider": "proxy", "compat": {"sessionAffinityFormat": "openrouter"}}
    )

    captured, capturedPayload = await _captureWithPayload("session-proxy", proxyModel)

    assert captured == {"sessionId": None, "clientRequestId": None, "xSessionId": "session-proxy"}
    assert "session_id" not in capturedPayload
    assert capturedPayload["prompt_cache_key"] == "session-proxy"


async def test_auto_detects_openrouter_session_affinity_header_for_openrouter_endpoints():
    openRouterModel = getModel("openai", "gpt-5.4").model_copy(update={"provider": "openrouter"})

    captured, capturedPayload = await _captureWithPayload("session-openrouter", openRouterModel)

    assert captured == {"sessionId": None, "clientRequestId": None, "xSessionId": "session-openrouter"}
    assert "session_id" not in capturedPayload
    assert capturedPayload["prompt_cache_key"] == "session-openrouter"


async def test_uses_openai_no_session_format_when_configured():
    proxyModel = Model.model_validate(
        {
            **getModel("openai", "gpt-5.4").model_dump(),
            "provider": "proxy",
            "compat": {"sessionAffinityFormat": "openai-nosession"},
        }
    )

    captured, capturedPayload = await _captureWithPayload("session-proxy", proxyModel)

    assert captured == {"sessionId": None, "clientRequestId": "session-proxy", "xSessionId": None}
    assert "session_id" not in capturedPayload
    assert capturedPayload["prompt_cache_key"] == "session-proxy"


async def test_uses_openai_no_session_format_for_opencode_responses_models():
    model = getModel("opencode", "gpt-5.4")

    captured, capturedPayload = await _captureWithPayload("session-opencode", model)

    assert model.compat.sessionAffinityFormat == "openai-nosession"
    assert captured == {"sessionId": None, "clientRequestId": "session-opencode", "xSessionId": None}
    assert capturedPayload["prompt_cache_key"] == "session-opencode"


async def test_can_omit_openai_session_id_header_while_preserving_other_affinity_data():
    proxyModel = Model.model_validate(
        {
            **getModel("openai", "gpt-5.4").model_dump(),
            "provider": "opencode",
            "compat": {"sessionAffinityFormat": "openai-nosession"},
        }
    )

    captured, capturedPayload = await _captureWithPayload("session-123", proxyModel)

    assert captured["sessionId"] is None
    assert captured["clientRequestId"] == "session-123"
    assert capturedPayload["prompt_cache_key"] == "session-123"


async def test_lets_explicit_headers_override_the_default_openai_cache_affinity_headers():
    captured = await _captureOpenAIResponseHeaders(
        {
            "sessionId": "session-123",
            "headers": {"session_id": "override-session", "x-client-request-id": "override-request"},
        }
    )

    assert captured["sessionId"] == "override-session"
    assert captured["clientRequestId"] == "override-request"


async def test_omits_openai_cache_affinity_headers_when_cache_retention_is_none():
    captured = await _captureOpenAIResponseHeaders({"cacheRetention": "none", "sessionId": "session-123"})

    assert captured["sessionId"] is None
    assert captured["clientRequestId"] is None


@pytest.mark.parametrize(
    ("modelId", "serviceTier", "responseServiceTier", "multiplier"),
    [
        ("gpt-5.4", "priority", "priority", 2),
        ("gpt-5.5", "priority", "priority", 2.5),
        ("gpt-5.5", "flex", "flex", 0.5),
        # GPT-6 models report Fast mode as "fast" even when "priority" is requested (#10034)
        ("gpt-6-luna", "priority", "fast", 2),
        ("gpt-6-luna", "fast", "fast", 2),
    ],
)
async def test_applies_the_service_tier_cost_multiplier(modelId, serviceTier, responseServiceTier, multiplier):
    model = getModel("openai", modelId)
    tokenCount = 100_000
    tokenScale = tokenCount / 1_000_000
    completed = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "service_tier": responseServiceTier,
            "usage": {
                "input_tokens": tokenCount,
                "output_tokens": tokenCount,
                "total_tokens": tokenCount * 2,
                "input_tokens_details": {"cached_tokens": 0},
            },
        },
    }
    sse = f"data: {json.dumps(completed)}\n\n".encode()

    with captureServer(lambda _request: Reply(body=sse)) as server:
        result = await streamOpenAIResponses(
            _at(model, server.baseUrl),
            normalizeContext(_hiContext()),
            {"apiKey": "sk-test-key", "serviceTier": serviceTier},
        ).result()

    assert result.usage.cost.input == pytest.approx(model.cost.input * multiplier * tokenScale, abs=1e-12)
    assert result.usage.cost.output == pytest.approx(model.cost.output * multiplier * tokenScale, abs=1e-12)
    assert result.usage.cost.total == pytest.approx((model.cost.input + model.cost.output) * multiplier * tokenScale, abs=1e-12)


# --- openai-responses max_output_tokens compat --------------------------------------------------


async def test_sends_max_output_tokens_by_default():
    capturedPayload = await _capturePayload(getModel("openai", "gpt-5.4"), _hiContext(), {"maxTokens": 1024})

    assert capturedPayload["max_output_tokens"] == 1024


async def test_omits_max_output_tokens_when_supports_max_output_tokens_is_false():
    baseModel = getModel("openai", "gpt-5.4")
    model = Model.model_validate(
        {**baseModel.model_dump(), "compat": {**(baseModel.model_dump().get("compat") or {}), "supportsMaxOutputTokens": False}}
    )

    capturedPayload = await _capturePayload(model, _hiContext(), {"maxTokens": 1024})

    assert "max_output_tokens" not in capturedPayload
