"""Translated from pi's ``packages/ai/test/anthropic-error-tool-result-images.test.ts`` (v1.3.0)."""

from __future__ import annotations

import time
from typing import Any

from misaka.ai.compat import streamSimple
from misaka.ai.types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)


class PayloadCaptured(Exception):
    def __init__(self) -> None:
        super().__init__("payload captured")
        self.name = "PayloadCaptured"


model = Model(
    id="claude-test",
    name="Claude Test",
    api="anthropic-messages",
    provider="anthropic",
    baseUrl="http://127.0.0.1:9",
    reasoning=False,
    input=["text", "image"],
    cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
    contextWindow=200000,
    maxTokens=1024,
)


def _now() -> int:
    return time.time_ns() // 1_000_000


assistant = AssistantMessage(
    role="assistant",
    content=[
        ToolCall(id="call-1", name="codemode", arguments={}),
        ToolCall(id="call-2", name="read", arguments={}),
    ],
    api="anthropic-messages",
    provider="anthropic",
    model="claude-test",
    usage=Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    ),
    stopReason="toolUse",
    timestamp=_now(),
)


async def capturePayload(context: Context) -> dict[str, Any]:
    captured: list[Any] = []

    def onPayload(payload: Any, _model: Any) -> Any:
        captured.append(payload)
        raise PayloadCaptured()

    stream = streamSimple(model, context, {"apiKey": "fake-key", "cacheRetention": "none", "onPayload": onPayload})
    await stream.result()
    if not captured:
        raise RuntimeError("Expected payload capture before request")
    return captured[0]


def image(data: str) -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


# Regression for #10794: Anthropic rejects error tool results that contain images.
async def test_moves_the_images_after_the_tool_results_of_the_turn():
    payload = await capturePayload(
        Context(
            messages=[
                UserMessage(content="Show the screenshots", timestamp=_now()),
                assistant,
                ToolResultMessage(
                    toolCallId="call-1",
                    toolName="codemode",
                    content=[
                        TextContent(text="Script failed"),
                        ImageContent(data="AAAA", mimeType="image/png"),
                        ImageContent(data="CCCC", mimeType="image/png"),
                        TextContent(text="Script error"),
                    ],
                    isError=True,
                    timestamp=_now(),
                ),
                ToolResultMessage(
                    toolCallId="call-2",
                    toolName="read",
                    content=[ImageContent(data="BBBB", mimeType="image/png")],
                    isError=False,
                    timestamp=_now(),
                ),
            ]
        )
    )

    assert payload["messages"][2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": "Script failed\n(image attached after the tool results)\nScript error",
                "is_error": True,
            },
            {
                "type": "tool_result",
                "tool_use_id": "call-2",
                "content": [{"type": "text", "text": "(see attached image)"}, image("BBBB")],
                "is_error": False,
            },
            {"type": "text", "text": "Images from the error result of tool call call-1 (codemode):"},
            image("AAAA"),
            image("CCCC"),
        ],
    }


async def test_keeps_error_tool_results_without_images_unchanged():
    payload = await capturePayload(
        Context(
            messages=[
                UserMessage(content="Run it", timestamp=_now()),
                assistant.model_copy(update={"content": [assistant.content[0]]}),
                ToolResultMessage(
                    toolCallId="call-1",
                    toolName="codemode",
                    content=[TextContent(text="Script failed")],
                    isError=True,
                    timestamp=_now(),
                ),
            ]
        )
    )

    assert payload["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "Script failed", "is_error": True}],
    }
