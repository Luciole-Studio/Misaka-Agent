"""Translated from pi's ``packages/ai/test/openai-completions-thinking-as-text.test.ts`` (v1.1.0)."""

from __future__ import annotations

import json
from typing import Any

from misaka.ai.api.openai_completions import convertMessages
from misaka.ai.api.openai_completions import stream as streamOpenAICompletions
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    TextContent,
    ThinkingContent,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.transcript import normalizeContext
from tests.http_capture import Reply, captureServer

emptyUsage = Usage(
    input=0,
    output=0,
    cacheRead=0,
    cacheWrite=0,
    totalTokens=0,
    cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
)

compat: dict[str, Any] = {
    "supportsStore": True,
    "supportsDeveloperRole": True,
    "supportsReasoningEffort": True,
    "supportsUsageInStreaming": True,
    "supportsFinishReason": True,
    "maxTokensField": "max_completion_tokens",
    "requiresToolResultName": False,
    "requiresAssistantAfterToolResult": False,
    "requiresThinkingAsText": True,
    "requiresReasoningContentOnAssistantMessages": False,
    "thinkingFormat": "openai",
    "openRouterRouting": {},
    "vercelGatewayRouting": {},
    "chatTemplateKwargs": {},
    "chatTemplateArgs": {},
    "zaiToolStream": False,
    "supportsThinkingTokenBudget": False,
    "thinkingTokenBudgetField": None,
    "supportsStrictMode": True,
    "supportsOpenAIGrammarTools": False,
    "supportsMidConvoSystemMessages": False,
    "supportsMidConvoToolAdditions": False,
    "cacheControlFormat": None,
    "sendSessionAffinityHeaders": False,
    "sessionAffinityFormat": "openai",
    "supportsLongCacheRetention": True,
}


def buildModel(baseUrl: str = "http://127.0.0.1:1") -> Model:
    return Model.model_validate(
        {
            "id": "repro-model",
            "name": "Repro Model",
            "api": "openai-completions",
            "provider": "repro-provider",
            "baseUrl": baseUrl,
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 4096,
            "compat": {key: value for key, value in compat.items() if value is not None},
        }
    )


def buildAssistant(content: list[Any]) -> AssistantMessage:
    return AssistantMessage(
        content=content,
        api="openai-completions",
        provider="repro-provider",
        model="repro-model",
        usage=emptyUsage,
        stopReason="stop",
        timestamp=2,
    )


def buildContext(assistant: AssistantMessage) -> Context:
    return Context(
        messages=[
            UserMessage(content="hello", timestamp=1),
            assistant,
            UserMessage(content="continue", timestamp=3),
        ]
    )


async def collectEvents(stream: Any) -> list[Any]:
    return [event async for event in stream]


def test_serializes_same_model_thinking_plus_text_replay_as_assistant_text_parts():
    messages = convertMessages(
        buildModel(),
        normalizeContext(
            buildContext(buildAssistant([ThinkingContent(thinking="internal reasoning"), TextContent(text="visible answer")]))
        ),
        compat,
    )

    assert messages[1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "internal reasoning"}, {"type": "text", "text": "visible answer"}],
    }


def test_serializes_same_model_thinking_only_replay_as_assistant_text_parts():
    messages = convertMessages(
        buildModel(),
        normalizeContext(buildContext(buildAssistant([ThinkingContent(thinking="internal reasoning")]))),
        compat,
    )

    assert messages[1] == {"role": "assistant", "content": [{"type": "text", "text": "internal reasoning"}]}


async def test_reaches_the_endpoint_when_replay_contains_both_thinking_and_text():
    def respond(request: Any) -> Reply:
        if request.method != "POST" or request.path != "/chat/completions":
            return Reply(status=404, headers={}, body=b"")
        first = {
            "id": "chatcmpl-repro",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "repro-model",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": None}],
        }
        last = {
            "id": "chatcmpl-repro",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "repro-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        body = f"data: {json.dumps(first)}\n\ndata: {json.dumps(last)}\n\ndata: [DONE]\n\n"
        return Reply(headers={"content-type": "text/event-stream", "cache-control": "no-cache"}, body=body.encode())

    with captureServer(respond) as server:
        events = await collectEvents(
            streamOpenAICompletions(
                buildModel(server.baseUrl),
                normalizeContext(
                    buildContext(
                        buildAssistant([ThinkingContent(thinking="internal reasoning"), TextContent(text="visible answer")])
                    )
                ),
                {"apiKey": "test-key"},
            )
        )
        requestBodies = [request.body for request in server.requests]

    assert len(requestBodies) == 1
    assert requestBodies[0]["messages"][1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "internal reasoning"}, {"type": "text", "text": "visible answer"}],
    }
    assert events[-1].type == "done"
