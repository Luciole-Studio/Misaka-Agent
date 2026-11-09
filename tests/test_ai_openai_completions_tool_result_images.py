"""Translated from pi's ``packages/ai/test/openai-completions-tool-result-images.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

from misaka.ai.api.openai_completions import convertMessages
from misaka.ai.compat import getModel
from misaka.ai.types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.transcript import normalizeContext

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
    "requiresThinkingAsText": False,
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
    "cacheControlFormat": "anthropic",
    "sendSessionAffinityHeaders": False,
    "sessionAffinityFormat": "openai",
    "supportsLongCacheRetention": True,
}


def _now() -> int:
    return time.time_ns() // 1_000_000


def _imageModel() -> Model:
    baseModel = getModel("openai", "gpt-4o-mini").model_dump(exclude={"compat"})
    return Model.model_validate({**baseModel, "api": "openai-completions", "input": ["text", "image"]})


def buildToolResult(toolCallId: str, timestamp: int) -> ToolResultMessage:
    return ToolResultMessage(
        toolCallId=toolCallId,
        toolName="read",
        content=[TextContent(text="Read image file [image/png]"), ImageContent(data="ZmFrZQ==", mimeType="image/png")],
        isError=False,
        timestamp=timestamp,
    )


def buildEmptyToolResult(toolCallId: str, timestamp: int) -> ToolResultMessage:
    return ToolResultMessage(
        toolCallId=toolCallId, toolName="bash", content=[TextContent(text="")], isError=False, timestamp=timestamp
    )


# Regression test for https://github.com/earendil-works/pi/issues/9797
def test_omits_empty_text_parts_from_user_messages_with_images():
    model = _imageModel()
    context = normalizeContext(
        Context(
            messages=[
                UserMessage(
                    content=[TextContent(text=""), ImageContent(data="ZmFrZQ==", mimeType="image/png")], timestamp=_now()
                )
            ]
        )
    )

    assert convertMessages(model, context, compat) == [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,ZmFrZQ=="}}]}
    ]


def test_batches_tool_result_images_after_consecutive_tool_results():
    model = _imageModel()
    now = _now()
    assistantMessage = AssistantMessage(
        content=[
            ToolCall(id="tool-1", name="read", arguments={"path": "img-1.png"}),
            ToolCall(id="tool-2", name="read", arguments={"path": "img-2.png"}),
        ],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=emptyUsage,
        stopReason="toolUse",
        timestamp=now,
    )
    context = normalizeContext(
        Context(
            messages=[
                UserMessage(content="Read the images", timestamp=now - 2),
                assistantMessage,
                buildToolResult("tool-1", now + 1),
                buildToolResult("tool-2", now + 2),
            ]
        )
    )

    messages = convertMessages(model, context, compat)
    roles = [message["role"] for message in messages]
    assert roles == ["user", "assistant", "tool", "tool", "user"]

    imageMessage = messages[-1]
    assert imageMessage["role"] == "user"
    assert isinstance(imageMessage["content"], list)
    imageParts = [part for part in imageMessage["content"] if part.get("type") == "image_url"]
    assert len(imageParts) == 2


def test_uses_the_no_tool_output_placeholder_for_empty_tool_results_without_images():
    model = _imageModel()
    now = _now()
    assistantMessage = AssistantMessage(
        content=[ToolCall(id="tool-1", name="bash", arguments={"command": "true"})],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=emptyUsage,
        stopReason="toolUse",
        timestamp=now,
    )
    context = normalizeContext(
        Context(
            messages=[
                UserMessage(content="Run the command", timestamp=now - 1),
                assistantMessage,
                buildEmptyToolResult("tool-1", now + 1),
            ]
        )
    )

    messages = convertMessages(model, context, compat)
    toolMessage = next((message for message in messages if message["role"] == "tool"), None)
    assert toolMessage
    assert toolMessage["content"] == "(no tool output)"
    assert "see attached image" not in toolMessage["content"]
