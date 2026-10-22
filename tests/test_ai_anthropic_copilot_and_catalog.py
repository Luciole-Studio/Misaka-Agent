"""Translated from pi's ``packages/ai/test/github-copilot-anthropic.test.ts`` and
``anthropic-adaptive-thinking-models.test.ts`` (v1.3.0).

MISAKA fork: upstream mocks the SDK constructor to read the client options; here the request goes to a local HTTP
server, and the headers it carries show the same choices (Bearer auth, no API key, the Copilot headers).
"""

from __future__ import annotations

import json
import re
import time

from misaka.ai.api.anthropic_messages import stream as streamAnthropic
from misaka.ai.compat import getModel, getModels, getProviders
from misaka.ai.models import getSupportedThinkingLevels
from misaka.ai.types import UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.http_capture import Reply, captureServer

# --- github-copilot-anthropic.test.ts --------------------------------------------------------

context = normalizeContext(
    {
        "systemPrompt": "You are a helpful assistant.",
        "messages": [UserMessage(content="Hello", timestamp=time.time_ns() // 1_000_000)],
    }
)


def _sse() -> bytes:
    events = [
        {"type": "message_start", "message": {"id": "msg_test", "usage": {"input_tokens": 10, "output_tokens": 0}}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
    ]
    return "\n".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n" for event in events).encode()


async def _copilotRequest(options: dict) -> tuple:
    model = getModel("github-copilot", "claude-sonnet-4.6")
    with captureServer(lambda _request: Reply(body=_sse())) as server:
        s = streamAnthropic(model.model_copy(update={"baseUrl": server.baseUrl}), context, options)
        async for event in s:
            if event.type == "error":
                break
    return model, server.requests[0]


def test_applies_copilot_specific_adaptive_thinking_effort_overrides():
    opus47 = getModel("github-copilot", "claude-opus-4.7")
    assert {key: opus47.thinkingLevelMap[key] for key in ("minimal", "xhigh", "max")} == {
        "minimal": "low",
        "xhigh": "xhigh",
        "max": "max",
    }
    assert "xhigh" in getSupportedThinkingLevels(opus47)
    assert "max" in getSupportedThinkingLevels(opus47)

    opus5 = getModel("github-copilot", "claude-opus-5")
    assert opus5.api == "anthropic-messages"
    assert opus5.contextWindow == 1000000
    assert {key: opus5.thinkingLevelMap[key] for key in ("minimal", "xhigh", "max")} == {
        "minimal": "low",
        "xhigh": "xhigh",
        "max": "max",
    }
    assert "xhigh" in getSupportedThinkingLevels(opus5)
    assert "max" in getSupportedThinkingLevels(opus5)

    opus55 = getModel("github-copilot", "claude-opus-5.5")
    assert opus55.api == "anthropic-messages"
    assert opus55.contextWindow == 1000000
    assert getSupportedThinkingLevels(opus55) == ["low", "medium", "high", "xhigh", "max"]

    sonnet46 = getModel("github-copilot", "claude-sonnet-4.6")
    assert {key: sonnet46.thinkingLevelMap[key] for key in ("minimal", "max")} == {"minimal": "low", "max": "max"}
    assert "max" in getSupportedThinkingLevels(sonnet46)
    assert "xhigh" not in getSupportedThinkingLevels(sonnet46)


async def test_uses_bearer_auth_copilot_headers_and_valid_anthropic_messages_payload():
    model, request = await _copilotRequest({"apiKey": "tid_copilot_session_test_token"})
    assert model.api == "anthropic-messages"

    # Auth: no API key, the token as Bearer auth.
    assert "x-api-key" not in request.headers
    assert request.headers["authorization"] == "Bearer tid_copilot_session_test_token"

    # Copilot static headers from model.headers
    assert "GitHubCopilotChat" in request.headers["user-agent"]
    assert request.headers["copilot-integration-id"] == "vscode-chat"

    # Dynamic headers
    assert request.headers["x-initiator"] == "user"
    assert request.headers["openai-intent"] == "conversation-edits"

    # Payload is valid Anthropic Messages format
    params = request.body
    assert "fine-grained-tool-streaming-2025-05-14" not in request.headers.get("anthropic-beta", "")
    assert params["model"] == "claude-sonnet-4.6"
    assert params["stream"] is True
    assert params["max_tokens"] == model.maxTokens
    assert isinstance(params["messages"], list)


async def test_omits_interleaved_thinking_beta_for_adaptive_thinking_models():
    _, request = await _copilotRequest({"apiKey": "tid_copilot_session_test_token", "interleavedThinking": True})

    assert "interleaved-thinking-2025-05-14" not in request.headers.get("anthropic-beta", "")


# --- anthropic-adaptive-thinking-models.test.ts ---------------------------------------------

EXPECTED_CURRENT_ADAPTIVE_THINKING_MODELS = [
    "anthropic/claude-fable-5",
    "anthropic/claude-haiku-5-5",
    "anthropic/claude-opus-4-8",
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
    "cloudflare-ai-gateway/claude-fable-5",
    "fireworks/accounts/fireworks/models/deepseek-v4p1-flash",
    "fireworks/accounts/fireworks/models/gpt-oss-120b",
    "fireworks/accounts/fireworks/models/qwen3p8-max",
    "opencode/claude-opus-4-8",
    "opencode/claude-opus-5",
    "vercel-ai-gateway/anthropic/claude-opus-4.8",
    "vercel-ai-gateway/anthropic/claude-opus-5",
    "vercel-ai-gateway/anthropic/claude-sonnet-5",
]


def getAllModels() -> list:
    return [model for provider in getProviders() for model in getModels(provider)]


def test_marks_built_in_anthropic_messages_models_that_use_adaptive_thinking():
    flaggedModels = sorted(
        f"{model.provider}/{model.id}"
        for model in getAllModels()
        if model.api == "anthropic-messages"
        and model.compat is not None
        and model.compat.forceAdaptiveThinking is True
    )

    assert set(EXPECTED_CURRENT_ADAPTIVE_THINKING_MODELS) <= set(flaggedModels)
    # Regression for #9323: Fireworks uses catalog effort metadata and
    # verified fallbacks, not a fixed set of adaptive model names.
    pattern = re.compile(r"(opus[-.](4[-.][678]|5)|sonnet[-.]4[-.]6|sonnet[-.]5|haiku[-.]5[-.]5|fable[-.]5)")
    assert flaggedModels == [
        modelId for modelId in flaggedModels if modelId.startswith("fireworks/") or pattern.search(modelId)
    ]
