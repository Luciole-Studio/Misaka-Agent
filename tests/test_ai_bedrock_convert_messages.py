"""Translated from pi's ``packages/ai/test/bedrock-convert-messages.test.ts`` (v1.3.0).

MISAKA fork: the three "unknown content block" cases are not ported -- the messages are pydantic models, and a
block of an unknown type is rejected where the message is built, so it never reaches the converter.
"""

from __future__ import annotations

import time
from typing import Any

from misaka.ai.api.bedrock_converse_stream import stream as streamBedrock
from misaka.ai.types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    ModelCost,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import AbortController
from tests.bedrock_fake import FakeBedrockClient, useFakeBedrock


def _now() -> int:
    return time.time_ns() // 1_000_000


baseModel = Model(
    id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    name="Claude Sonnet 4.5 (US)",
    api="bedrock-converse-stream",
    provider="amazon-bedrock",
    baseUrl="https://bedrock-runtime.us-east-1.amazonaws.com",
    reasoning=True,
    input=["text", "image"],
    cost=ModelCost(input=3, output=15, cacheRead=0.3, cacheWrite=3.75),
    contextWindow=200000,
    maxTokens=64000,
    compat={"supportsStrictMode": True},
)

novaModel = Model(
    **{
        **baseModel.model_dump(exclude_none=True, exclude={"compat"}),
        "id": "amazon.nova-lite-v1:0",
        "name": "Nova Lite",
        "reasoning": False,
    }
)


def _usage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def _assistant(content: list[Any], stopReason: str = "stop") -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=content,
        api="bedrock-converse-stream",
        provider="amazon-bedrock",
        model=baseModel.id,
        usage=_usage(),
        stopReason=stopReason,
        timestamp=_now(),
    )


async def capturePayload(context: Context, model: Model = baseModel) -> Any:
    captured: list[Any] = []
    controller = AbortController()
    controller.abort()

    def onPayload(payload: Any, _model: Any) -> Any:
        captured.append(payload)
        return payload

    s = streamBedrock(
        model, normalizeContext(context), {"cacheRetention": "none", "signal": controller.signal, "onPayload": onPayload}
    )
    async for event in s:
        if event.type == "error":
            break
    return captured[0] if captured else None


# --- Bedrock constrained sampling ------------------------------------------------------------


async def test_gates_native_strict_tool_use_by_model_capability():
    tool = Tool(
        name="lookup",
        description="Look up a value",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        constrainedSampling={"type": "json_schema", "strict": "require"},
    )
    context = Context(messages=[UserMessage(content="Use the tool", timestamp=_now())], tools=[tool])
    payload = await capturePayload(context)
    assert payload["toolConfig"]["tools"][0]["toolSpec"]["strict"] is True

    preferTool = Tool(
        name=tool.name,
        description=tool.description,
        parameters=tool.parameters,
        constrainedSampling={"type": "json_schema", "strict": "prefer"},
    )
    novaPayload = await capturePayload(context.model_copy(update={"tools": [preferTool]}), novaModel)
    assert "strict" not in novaPayload["toolConfig"]["tools"][0]["toolSpec"]


# --- Bedrock tool arguments -------------------------------------------------------------------


async def test_preserves_empty_property_names_in_streamed_tool_arguments(monkeypatch):
    useFakeBedrock(
        monkeypatch,
        FakeBedrockClient(
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "tool-1", "name": "edit"}}}},
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {
                        "toolUse": {
                            "input": '{"path":"/workspace/foobar/file.js","edits":[{"oldText":"first","newText":"updated first"},{"oldText":"second","newText":"updated second","":""}]}'
                        }
                    },
                }
            },
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "tool_use"}},
        ),
    )

    message = await streamBedrock(
        baseModel,
        normalizeContext({"messages": [UserMessage(content="Use the tool", timestamp=_now())]}),
        {"cacheRetention": "none"},
    ).result()

    assert message.content[0].model_dump(exclude_none=True) == {
        "type": "toolCall",
        "id": "tool-1",
        "name": "edit",
        "arguments": {
            "path": "/workspace/foobar/file.js",
            "edits": [
                {"oldText": "first", "newText": "updated first"},
                {"oldText": "second", "newText": "updated second", "": ""},
            ],
        },
    }


# --- bedrock convertMessages ------------------------------------------------------------------


async def test_replaces_blank_user_string_content_with_a_placeholder():
    payload = await capturePayload(Context(messages=[UserMessage(content="   ", timestamp=_now())]))
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"] == [{"text": "<empty>"}]


async def test_filters_blank_user_text_blocks_when_other_content_remains():
    payload = await capturePayload(
        Context(messages=[UserMessage(content=[TextContent(text=""), TextContent(text="hello")], timestamp=_now())])
    )
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"] == [{"text": "hello"}]


async def test_replaces_user_content_emptied_by_surrogate_sanitization_with_a_placeholder():
    payload = await capturePayload(Context(messages=[UserMessage(content=chr(0xD83D), timestamp=_now())]))
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"] == [{"text": "<empty>"}]


async def test_skips_assistant_text_blocks_emptied_by_surrogate_sanitization():
    payload = await capturePayload(Context(messages=[_assistant([TextContent(text=chr(0xD83D))])]))
    assert len(payload["messages"]) == 0


async def test_replaces_blank_tool_result_content_with_a_placeholder():
    payload = await capturePayload(
        Context(
            messages=[
                ToolResultMessage(
                    toolCallId="tool-1",
                    toolName="tool",
                    content=[TextContent(text="")],
                    isError=False,
                    timestamp=_now(),
                )
            ]
        )
    )
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"][0]["toolResult"]["content"] == [{"text": "<empty>"}]


# Regression for #10794: Claude rejects error tool results that contain images.
async def test_moves_images_of_error_tool_results_after_the_tool_results():
    messages = [
        _assistant(
            [ToolCall(id="call-1", name="codemode", arguments={}), ToolCall(id="call-2", name="read", arguments={})],
            "toolUse",
        ),
        ToolResultMessage(
            toolCallId="call-1",
            toolName="codemode",
            content=[
                TextContent(text="Script failed"),
                ImageContent(data="AAAA", mimeType="image/png"),
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
    payload = await capturePayload(Context(messages=messages))
    content = payload["messages"][1]["content"]
    assert len(content) == 4
    assert content[0]["toolResult"] == {
        "toolUseId": "call-1",
        "content": [
            {"text": "Script failed"},
            {"text": "(image attached after the tool results)"},
            {"text": "Script error"},
        ],
        "status": "error",
    }
    assert content[1]["toolResult"]["status"] == "success"
    assert [block["image"]["format"] for block in content[1]["toolResult"]["content"]] == ["png"]
    assert content[2] == {"text": "Images from the error result of tool call call-1 (codemode):"}
    assert content[3]["image"]["format"] == "png"


async def test_keeps_images_in_error_tool_results_for_non_claude_models():
    messages = [
        ToolResultMessage(
            toolCallId="call-1",
            toolName="codemode",
            content=[TextContent(text="Script failed"), ImageContent(data="AAAA", mimeType="image/png")],
            isError=True,
            timestamp=_now(),
        )
    ]
    payload = await capturePayload(Context(messages=messages), novaModel)
    ((block,),) = [message["content"] for message in payload["messages"]]
    assert block["toolResult"]["toolUseId"] == "call-1"
    assert block["toolResult"]["status"] == "error"
    assert block["toolResult"]["content"][0] == {"text": "Script failed"}
    assert block["toolResult"]["content"][1]["image"]["format"] == "png"
    assert len(block["toolResult"]["content"]) == 2


async def test_removes_empty_property_names_only_from_replayed_bedrock_input():
    toolArguments = {
        "path": "/workspace/foobar/file.js",
        "edits": [
            {"oldText": "first", "newText": "updated first"},
            {"oldText": "second", "newText": "updated second", "": ""},
        ],
    }
    payload = await capturePayload(
        Context(
            messages=[
                _assistant([ToolCall(id="tool-1", name="edit", arguments=toolArguments)], "toolUse"),
                ToolResultMessage(
                    toolCallId="tool-1", toolName="edit", content=[TextContent(text="done")], isError=False, timestamp=_now()
                ),
                UserMessage(content="Continue", timestamp=_now()),
            ]
        )
    )

    assert payload["messages"][0]["content"][0]["toolUse"]["input"] == {
        "path": "/workspace/foobar/file.js",
        "edits": [
            {"oldText": "first", "newText": "updated first"},
            {"oldText": "second", "newText": "updated second"},
        ],
    }
    assert toolArguments["edits"][1] == {"oldText": "second", "newText": "updated second", "": ""}
