"""Translated from pi's ``packages/ai/test/anthropic-mid-conversation-effort.test.ts`` (v1.3.0).

MISAKA fork: Python has no ``fetch`` override; the beta-header case answers from a local HTTP server instead.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from misaka.ai.api.anthropic_messages import stream
from misaka.ai.compat import getModel
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    ModelCost,
    TextContent,
    ThinkingContent,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.transcript import normalizeContext
from tests.http_capture import Reply, captureServer


def managedModel(provider: str = "anthropic", compat: dict[str, Any] | None = None) -> Model:
    return Model(
        id="claude-fable-5-1",
        name="Claude Fable 5.1",
        api="anthropic-messages",
        provider=provider,
        baseUrl="http://127.0.0.1:9",
        reasoning=True,
        thinkingLevelMap={"off": None, "minimal": "low", "low": "low", "medium": "medium", "high": "high", "max": "max"},
        input=["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=200000,
        maxTokens=32000,
        compat=compat if compat is not None else {"forceAdaptiveThinking": True, "supportsMidConvoEffort": True},
    )


def assistant(model: Model, level: str | None = None) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=[ThinkingContent(thinking="reasoning", thinkingSignature="signature"), TextContent(text="answer")],
        api="anthropic-messages",
        provider=model.provider,
        model=model.id,
        **({} if level is None else {"providerThinkingLevel": level}),
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason="stop",
        timestamp=1,
    )


async def capture(model: Model, context: Context, options: dict[str, Any] | None = None) -> tuple[Any, AssistantMessage]:
    """``options`` is ``Pick<AnthropicOptions, "effort" | "thinkingEnabled">``."""
    captured: list[Any] = []

    def onPayload(value: Any, _model: Any) -> Any:
        captured.append(value)
        raise RuntimeError("payload captured")

    result = stream(
        model,
        normalizeContext(context),
        {"apiKey": "test-key", "cacheRetention": "none", "thinkingEnabled": True, **(options or {}), "onPayload": onPayload},
    )
    message = await result.result()
    if not captured:
        raise RuntimeError("Expected payload capture")
    return captured[0], message


def user(text: str, timestamp: int) -> UserMessage:
    return UserMessage(content=text, timestamp=timestamp)


def effortMessages(payload: Any) -> list[Any]:
    return [message for message in payload["messages"] if message["role"] == "system"]


async def test_reconstructs_an_exact_historical_marker_prefix_and_appends_the_current_marker():
    model = managedModel()
    firstPayload, firstMessage = await capture(model, Context(messages=[user("one", 1)]), {"effort": "low"})
    secondPayload, _ = await capture(
        model, Context(messages=[user("one", 1), assistant(model, "low"), user("two", 2)]), {"effort": "high"}
    )

    assert firstPayload["messages"] == [
        {"role": "user", "content": "one"},
        {"role": "system", "content": [], "output_config": {"effort": "low"}},
    ]
    assert secondPayload["messages"][: len(firstPayload["messages"])] == firstPayload["messages"]
    assert secondPayload["messages"][-1] == {"role": "system", "content": [], "output_config": {"effort": "high"}}
    assert firstPayload["output_config"] == {"effort": "high"}
    assert secondPayload["output_config"] == {"effort": "high"}
    assert secondPayload["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
        "block_binding": {"prefix_mismatch_behavior": "drop_block"},
    }
    assert firstMessage.providerThinkingLevel == "low"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
async def test_preserves_native_effort(effort):
    model = managedModel()
    payload, message = await capture(model, Context(messages=[user("one", 1)]), {"effort": effort})
    assert effortMessages(payload) == [{"role": "system", "content": [], "output_config": {"effort": effort}}]
    assert message.providerThinkingLevel == effort


async def test_defaults_omitted_effort_to_high_and_still_enables_drop_block():
    payload, message = await capture(managedModel(), Context(messages=[user("one", 1)]))
    assert payload["messages"][-1] == {"role": "system", "content": [], "output_config": {"effort": "high"}}
    assert payload["thinking"]["block_binding"]["prefix_mismatch_behavior"] == "drop_block"
    assert message.providerThinkingLevel == "high"


async def test_does_not_invent_markers_for_legacy_or_other_provider_assistants():
    model = managedModel()
    legacy = assistant(model)
    otherProvider = assistant(model, "low").model_copy(update={"provider": "other-provider"})
    payload, _ = await capture(
        model,
        Context(messages=[user("one", 1), legacy, user("two", 2), otherProvider, user("three", 3)]),
        {"effort": "medium"},
    )
    assert effortMessages(payload) == [{"role": "system", "content": [], "output_config": {"effort": "medium"}}]


async def test_sends_plain_disabled_thinking_without_effort_when_off_is_supported():
    model = managedModel().model_copy(
        update={"thinkingLevelMap": {"minimal": None, "low": "low", "medium": "medium", "high": "high", "max": "max"}}
    )
    # Anthropic rejects block_binding with disabled thinking, and rejects effort markers
    # that change level (here low, then medium) while thinking is disabled.
    payload, message = await capture(
        model,
        Context(
            messages=[
                user("one", 1),
                assistant(model, "low"),
                user("two", 2),
                assistant(model, "medium"),
                user("three", 3),
            ]
        ),
        {"thinkingEnabled": False},
    )
    assert payload["thinking"] == {"type": "disabled"}
    assert "output_config" not in payload
    assert effortMessages(payload) == []
    assert message.providerThinkingLevel is None


async def test_keeps_adaptive_thinking_when_off_is_unsupported():
    payload, message = await capture(managedModel(), Context(messages=[user("one", 1)]), {"thinkingEnabled": False})
    assert payload["thinking"]["type"] == "adaptive"
    assert effortMessages(payload) == [{"role": "system", "content": [], "output_config": {"effort": "high"}}]
    assert message.providerThinkingLevel == "high"


async def test_leaves_unsupported_models_on_top_level_effort():
    model = managedModel(compat={"forceAdaptiveThinking": True})
    payload, message = await capture(model, Context(messages=[user("one", 1)]), {"effort": "low"})
    assert payload["messages"] == [{"role": "user", "content": "one"}]
    assert payload["output_config"] == {"effort": "low"}
    assert payload["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert message.providerThinkingLevel is None


async def test_sends_the_effort_and_binding_beta_headers():
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_test", "model": "claude-fable-5-1", "usage": {"input_tokens": 1, "output_tokens": 0}},
        },
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"input_tokens": 1, "output_tokens": 1}},
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()
    with captureServer(lambda _request: Reply(body=body)) as server:
        model = managedModel().model_copy(update={"baseUrl": server.baseUrl})
        result = await stream(
            model, normalizeContext(Context(messages=[user("one", 1)])), {"apiKey": "test-key", "cacheRetention": "none"}
        ).result()

    assert result.stopReason == "stop", result.errorMessage
    betaHeader = server.requests[0].headers.get("anthropic-beta", "")
    assert "mid-conversation-output-config-2026-07-01" in betaHeader
    assert "thinking-binding-controls-2026-08-01" in betaHeader


def test_generates_exact_model_and_transport_gates():
    direct = getModel("anthropic", "claude-fable-5-1")
    openRouter = getModel("openrouter", "anthropic/claude-fable-5.1")
    unsupported = getModel("anthropic", "claude-opus-4-8")
    assert direct.compat.supportsMidConvoEffort is True
    assert direct.thinkingLevelMap["off"] is None
    assert openRouter.api == "anthropic-messages"
    assert openRouter.baseUrl == "https://openrouter.ai/api"
    assert openRouter.compat.supportsMidConvoEffort is True
    assert (unsupported.compat.supportsMidConvoEffort if unsupported.compat else None) is None
    # OpenRouter rejects configuration_update on Opus 5 but accepts it on Fable 5.1.
    opus5 = getModel("openrouter", "anthropic/claude-opus-5")
    assert (opus5.compat.supportsMidConvoEffort if opus5.compat else None) is None
    anthropicOpus5 = getModel("anthropic", "claude-opus-5")
    assert (anthropicOpus5.compat.allowedFallbackModels if anthropicOpus5.compat else None) is None


def test_supports_off_only_on_managed_models_that_accept_disabled_thinking():
    for id in ["claude-opus-5", "claude-haiku-5-5"]:
        model = getModel("anthropic", id)
        assert model.compat.supportsMidConvoEffort is True
        # `thinkingLevelMap?.off` not null: absent or a value.
        assert (model.thinkingLevelMap or {}).get("off", "absent") is not None
    for id in ["claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"]:
        model = getModel("anthropic", id)
        assert "off" in model.thinkingLevelMap and model.thinkingLevelMap["off"] is None
