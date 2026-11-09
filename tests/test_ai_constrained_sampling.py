"""Translated from pi's ``packages/ai/test/constrained-sampling.test.ts`` (v1.1.0)."""

from __future__ import annotations

import copy
import json
import time
from typing import Any

import pytest

from misaka.ai.api.constrained_sampling import (
    GrammarToolInputJsonBuffer,
    appendGrammarToolInputJsonDelta,
    makeStrictJsonSchema,
    resolveJsonSchemaStrictSampling,
)
from misaka.ai.api.openai_responses_shared import (
    convertResponsesMessages,
    convertResponsesTools,
    processResponsesStream,
)
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import normalizeContext


def makeModel() -> Model:
    return Model.model_validate(
        {
            "id": "gpt-test",
            "name": "GPT Test",
            "api": "openai-responses",
            "provider": "openai",
            "baseUrl": "https://api.openai.com/v1",
            "reasoning": False,
            "input": ["text", "image"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000,
            "maxTokens": 4096,
        }
    )


def makeUsage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def makeOutput() -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api="openai-responses",
        provider="openai",
        model="gpt-test",
        usage=makeUsage(),
        stopReason="pending",
        timestamp=time.time_ns() // 1_000_000,
    )


async def iterateEvents(events: list[dict[str, Any]]):
    for event in events:
        yield event


def makeTool(**overrides: Any) -> Tool:
    return Tool(
        **{
            "name": "sample_tool",
            "description": "Sample tool",
            # Type.Object({ payload: Type.String() }, { additionalProperties: false })
            "parameters": {
                "type": "object",
                "properties": {"payload": {"type": "string"}},
                "required": ["payload"],
                "additionalProperties": False,
            },
            **overrides,
        }
    )


def captureToolCallEvents(stream: AssistantMessageEventStream) -> tuple[list[Any], list[str]]:
    starts: list[Any] = []
    deltas: list[str] = []
    originalPush = stream.push

    def push(event: Any, *args: Any, **kwargs: Any) -> None:
        if event.type == "toolcall_start":
            block = event.partial.content[event.contentIndex]
            if block.type == "toolCall":
                starts.append(copy.deepcopy(block.arguments))
        elif event.type == "toolcall_delta":
            deltas.append(event.delta)
        originalPush(event, *args, **kwargs)

    stream.push = push
    return starts, deltas


def test_converts_supported_constraints_and_falls_back_when_unsupported():
    converted = convertResponsesTools([makeTool(constrainedSampling={"type": "json_schema", "strict": "prefer"})])[0]
    assert (converted["type"], converted["name"], converted["strict"]) == ("function", "sample_tool", True)

    with pytest.raises(Exception, match='Tool "sample_tool" requires JSON-schema constrained sampling'):
        convertResponsesTools(
            [makeTool(constrainedSampling={"type": "json_schema", "strict": "require"})], {"supportsStrictMode": False}
        )

    grammarTool = makeTool(constrainedSampling={"type": "grammar", "variants": {"openai_lark": "start: /[a-z]+/"}})
    grammar = convertResponsesTools([grammarTool], {"supportsOpenAIGrammarTools": True})[0]
    assert (grammar["type"], grammar["name"], grammar["format"]) == (
        "custom",
        "sample_tool",
        {"type": "grammar", "syntax": "lark", "definition": "start: /[a-z]+/"},
    )
    with pytest.raises(
        Exception,
        match='Tool "sample_tool" cannot use grammar constrained sampling: no supported grammar variant was provided',
    ):
        convertResponsesTools(
            [makeTool(constrainedSampling={"type": "grammar", "variants": {}})], {"supportsOpenAIGrammarTools": True}
        )

    fallback = convertResponsesTools([grammarTool], {"supportsOpenAIGrammarTools": False, "supportsStrictMode": False})[0]
    assert (fallback["type"], fallback["name"]) == ("function", "sample_tool")
    assert "strict" not in fallback

    assert convertResponsesTools([makeTool(constrainedSampling=False)]) == convertResponsesTools([makeTool()])


def test_derives_strict_provider_schemas_without_changing_tool_definitions():
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "number"},
            "metadata": {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
            "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["path", "metadata"],
    }

    strict = makeStrictJsonSchema(parameters)

    assert "additionalProperties" not in parameters
    assert parameters["required"] == ["path", "metadata"]
    assert strict["additionalProperties"] is False
    assert strict["required"] == ["path", "offset", "metadata", "nullable"]
    assert strict["properties"]["offset"] == {"anyOf": [{"type": "number"}, {"type": "null"}]}
    metadata = strict["properties"]["metadata"]
    assert metadata["additionalProperties"] is False
    assert metadata["required"] == ["enabled"]
    assert metadata["properties"] == {"enabled": {"anyOf": [{"type": "boolean"}, {"type": "null"}]}}
    assert strict["properties"]["nullable"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}


@pytest.mark.parametrize(
    ("parameters", "error"),
    [
        (
            {
                "type": "object",
                "properties": {"metadata": {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}}},
                "required": ["metadata"],
            },
            "additionalProperties is unsupported",
        ),
        (
            {
                "allOf": [
                    {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                    {"type": "object", "properties": {"b": {"type": "number"}}, "required": ["b"]},
                ]
            },
            "allOf schemas are unsupported",
        ),
        (
            {
                "type": "object",
                "properties": {
                    "value": {
                        "anyOf": [
                            {"type": "object", "properties": {"nested": {"type": "string"}}, "required": ["nested"]},
                            {"type": "null"},
                        ]
                    }
                },
                "required": ["value"],
            },
            "object and array unions are unsupported",
        ),
        (
            {
                "type": "object",
                "properties": {"child": {"$ref": "https://example.com/child.json"}},
                "required": ["child"],
            },
            r"\$ref schemas are unsupported",
        ),
    ],
)
def test_falls_back_or_rejects_schemas_that_cannot_be_safely_converted(parameters, error):
    tool = makeTool(parameters=parameters, constrainedSampling={"type": "json_schema", "strict": "prefer"})

    with pytest.raises(Exception, match=error):
        makeStrictJsonSchema(parameters)
    assert resolveJsonSchemaStrictSampling(tool, True) is None
    converted = convertResponsesTools([tool], {"supportsStrictMode": True})[0]
    assert converted["strict"] is False
    assert converted["parameters"] == parameters

    # `tool.constrainedSampling = {...}`: rebuilt, so the strict model validates the new constraint.
    tool = makeTool(parameters=parameters, constrainedSampling={"type": "json_schema", "strict": "require"})
    with pytest.raises(Exception, match=error):
        resolveJsonSchemaStrictSampling(tool, True)


def _grammarContext(**assistant: Any) -> Any:
    return normalizeContext(
        Context(
            messages=[
                AssistantMessage(
                    **{
                        "api": "openai-responses",
                        "provider": "openai",
                        "model": "gpt-test",
                        "usage": makeUsage(),
                        "stopReason": "toolUse",
                        "timestamp": time.time_ns() // 1_000_000,
                        **assistant,
                    }
                ),
                ToolResultMessage(
                    toolCallId="call_1|ctc_1",
                    toolName="sample_tool",
                    content=[TextContent(text="done")],
                    isError=False,
                    timestamp=time.time_ns() // 1_000_000,
                ),
            ]
        )
    )


def test_replays_grammar_calls_as_custom_responses_items():
    replayedToolCall = ToolCall(id="call_1|ctc_1", name="sample_tool", arguments={"payload": "abc"})
    context = _grammarContext(content=[replayedToolCall])
    replayed = context.messages[0].content[0]

    for invalidArguments in ({}, {"payload": 42}):
        replayed.arguments = invalidArguments
        with pytest.raises(Exception, match='Grammar tool call "sample_tool" requires argument "payload" to be a string'):
            convertResponsesMessages(makeModel(), context, {"openai"}, {"grammarToolInputProperties": {"sample_tool": "payload"}})

    replayed.arguments = {"payload": "abc"}
    messages = convertResponsesMessages(makeModel(), context, {"openai"}, {"grammarToolInputProperties": {"sample_tool": "payload"}})

    assert {"type": "custom_tool_call", "id": "ctc_1", "call_id": "call_1", "name": "sample_tool", "input": "abc"} in messages
    assert {"type": "custom_tool_call_output", "call_id": "call_1", "output": "done"} in messages


# earendil-works/radius#115: a gateway forwards another model's history as a foreign provider.
def test_drops_foreign_item_ids_when_replaying_grammar_calls_as_custom_responses_items():
    context = _grammarContext(
        api="pi-messages",
        provider="radius",
        model="gpt-other",
        content=[ToolCall(id="call_1|ctc_1", name="sample_tool", arguments={"payload": "abc"})],
    )

    messages = convertResponsesMessages(makeModel(), context, {"openai"}, {"grammarToolInputProperties": {"sample_tool": "payload"}})

    call = next(item for item in messages if item.get("type") == "custom_tool_call")
    assert (call["type"], call["call_id"], call["input"]) == ("custom_tool_call", "call_1", "abc")
    assert "id" not in call


def test_keeps_grammar_input_json_deltas_append_only():
    buffer = GrammarToolInputJsonBuffer(input="", started=False, closed=False)
    first = appendGrammarToolInputJsonDelta(buffer, "payload", 'a"', False)
    second = appendGrammarToolInputJsonDelta(buffer, "payload", 'a"\nb', True)

    assert json.loads(f"{first}{second}") == {"payload": 'a"\nb'}
    assert appendGrammarToolInputJsonDelta(buffer, "payload", 'a"\nb', True) is None
    with pytest.raises(Exception, match='grammar tool input for property "payload" changed after it was closed'):
        appendGrammarToolInputJsonDelta(buffer, "payload", "changed", True)


async def test_starts_custom_responses_tool_calls_with_their_initial_input():
    output = makeOutput()
    stream = AssistantMessageEventStream()
    starts, deltas = captureToolCallEvents(stream)
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "custom_tool_call", "call_id": "call_1", "id": "ctc_1", "name": "sample_tool", "input": "a"},
        },
        {"type": "response.custom_tool_call_input.delta", "output_index": 0, "item_id": "ctc_1", "delta": "b"},
        {"type": "response.custom_tool_call_input.done", "output_index": 0, "item_id": "ctc_1", "input": "abc"},
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {"type": "custom_tool_call", "call_id": "call_1", "id": "ctc_1", "name": "sample_tool", "input": "abc"},
        },
        {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}},
        },
    ]

    await processResponsesStream(
        iterateEvents(events), output, stream, makeModel(), {"grammarToolInputProperties": {"sample_tool": "payload"}}
    )

    assert output.stopReason == "toolUse"
    assert starts == [{"payload": "a"}]
    assert [block.model_dump(exclude_none=True) for block in output.content] == [
        {"type": "toolCall", "id": "call_1|ctc_1", "name": "sample_tool", "arguments": {"payload": "abc"}}
    ]
    assert json.loads("".join(deltas)) == {"payload": "abc"}
