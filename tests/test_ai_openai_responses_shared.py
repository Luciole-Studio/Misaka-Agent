"""Translated from pi's ``packages/ai/test/openai-responses-{namespace,partial-json-cleanup,foreign-toolcall-id,
message-id,empty-tool-result}.test.ts`` (v1.3.0)."""

from __future__ import annotations

import re
import time
from typing import Any

from misaka.ai.api.openai_responses_shared import (
    convertResponsesMessages,
    processResponsesStream,
)
from misaka.ai.compat import getModel
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    ModelCost,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.hash import shortHash
from misaka.ai.utils.transcript import normalizeContext


def _now() -> int:
    return time.time_ns() // 1_000_000


def _usage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def _responsesModel(**fields: Any) -> Model:
    return Model.model_validate(
        {
            "id": "gpt-5.4",
            "name": "GPT-5.4",
            "api": "openai-responses",
            "provider": "openai",
            "baseUrl": "https://api.openai.com/v1",
            "reasoning": True,
            "input": ["text"],
            "cost": ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0).model_dump(),
            "contextWindow": 400000,
            "maxTokens": 128000,
            **fields,
        }
    )


def createOutput(model: Model) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_usage(),
        stopReason="pending",
        timestamp=_now(),
    )


# --- openai-responses-namespace.test.ts ---------------------------------------------------------

model = _responsesModel()


async def createFunctionCallEvents():
    yield {
        "type": "response.output_item.added",
        "sequence_number": 0,
        "output_index": 0,
        "item": {"type": "function_call", "id": "fc_test", "call_id": "call_test", "name": "lookup", "arguments": ""},
    }
    yield {
        "type": "response.output_item.done",
        "sequence_number": 1,
        "output_index": 0,
        "item": {
            "type": "function_call",
            "id": "fc_test",
            "call_id": "call_test",
            "name": "lookup",
            "arguments": '{"value":"hello"}',
            "namespace": "dynamic_tools",
        },
    }
    yield {"type": "response.completed", "sequence_number": 2, "response": {"id": "resp_test", "status": "completed"}}


async def createCustomToolCallEvents():
    yield {
        "type": "response.output_item.added",
        "sequence_number": 0,
        "output_index": 0,
        "item": {"type": "custom_tool_call", "id": "ctc_test", "call_id": "call_test", "name": "query", "input": ""},
    }
    yield {
        "type": "response.output_item.done",
        "sequence_number": 1,
        "output_index": 0,
        "item": {
            "type": "custom_tool_call",
            "id": "ctc_test",
            "call_id": "call_test",
            "name": "query",
            "input": "hello",
            "namespace": "dynamic_tools",
        },
    }
    yield {"type": "response.completed", "sequence_number": 2, "response": {"id": "resp_test", "status": "completed"}}


def getToolCall(output: AssistantMessage) -> ToolCall:
    block = output.content[0] if output.content else None
    if block is None or block.type != "toolCall":
        raise AssertionError("Expected toolCall block")
    return block


def _find(items: list[dict[str, Any]], type: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("type") == type), None)


async def test_omits_an_absent_error_message():
    output = createOutput(model)
    await processResponsesStream(createFunctionCallEvents(), output, AssistantMessageEventStream(), model)

    assert "errorMessage" not in output.model_dump(exclude_none=True)


async def test_round_trips_a_function_namespace_received_only_on_output_item_done():
    output = createOutput(model)
    await processResponsesStream(createFunctionCallEvents(), output, AssistantMessageEventStream(), model)

    toolCall = getToolCall(output)
    assert (toolCall.id, toolCall.name, toolCall.arguments, toolCall.namespace) == (
        "call_test|fc_test",
        "lookup",
        {"value": "hello"},
        "dynamic_tools",
    )

    replayed = _find(convertResponsesMessages(model, normalizeContext(Context(messages=[output])), {"openai"}), "function_call")
    assert replayed == {
        "type": "function_call",
        "id": "fc_test",
        "call_id": "call_test",
        "name": "lookup",
        "arguments": '{"value":"hello"}',
        "namespace": "dynamic_tools",
    }


async def test_round_trips_a_custom_tool_namespace_received_only_on_output_item_done():
    output = createOutput(model)
    grammarToolInputProperties = {"query": "input"}
    await processResponsesStream(
        createCustomToolCallEvents(),
        output,
        AssistantMessageEventStream(),
        model,
        {"grammarToolInputProperties": grammarToolInputProperties},
    )

    toolCall = getToolCall(output)
    assert (toolCall.id, toolCall.name, toolCall.arguments, toolCall.namespace) == (
        "call_test|ctc_test",
        "query",
        {"input": "hello"},
        "dynamic_tools",
    )

    replayed = _find(
        convertResponsesMessages(
            model,
            normalizeContext(Context(messages=[output])),
            {"openai"},
            {"grammarToolInputProperties": grammarToolInputProperties},
        ),
        "custom_tool_call",
    )
    assert replayed == {
        "type": "custom_tool_call",
        "id": "ctc_test",
        "call_id": "call_test",
        "name": "query",
        "input": "hello",
        "namespace": "dynamic_tools",
    }


def test_drops_namespaces_when_the_target_cannot_replay_their_load_items():
    output = createOutput(model)
    output.content.extend([
        ToolCall(id="call_function|fc_test", name="lookup", arguments={"value": "hello"}, namespace="dynamic_tools"),
        ToolCall(id="call_custom|ctc_test", name="query", arguments={"input": "hello"}, namespace="dynamic_tools"),
    ])
    targetModels = [
        _responsesModel(id="gpt-5.2", name="GPT-5.2"),
        _responsesModel(provider="azure"),
        _responsesModel(
            api="openai-codex-responses", provider="openai-codex", id="gpt-5.3-codex-spark", name="GPT-5.3 Codex Spark"
        ),
    ]

    for targetModel in targetModels:
        replayed = convertResponsesMessages(
            targetModel,
            normalizeContext(Context(messages=[output])),
            {"openai"},
            {"grammarToolInputProperties": {"query": "input"}},
        )
        functionCall = _find(replayed, "function_call")
        customToolCall = _find(replayed, "custom_tool_call")
        assert functionCall is not None
        assert "namespace" not in functionCall
        assert customToolCall is not None
        assert "namespace" not in customToolCall


def test_does_not_add_a_namespace_to_ordinary_function_calls():
    output = createOutput(model)
    output.content.append(ToolCall(id="call_test|fc_test", name="lookup", arguments={"value": "hello"}))

    replayed = _find(convertResponsesMessages(model, normalizeContext(Context(messages=[output])), {"openai"}), "function_call")
    assert replayed is not None
    assert "namespace" not in replayed


# --- openai-responses-partial-json-cleanup.test.ts ----------------------------------------------


async def _partialJsonEvents(argumentsJson: str):
    yield {
        "type": "response.output_item.added",
        "item": {"type": "function_call", "id": "fc_test", "call_id": "call_test", "name": "edit", "arguments": ""},
    }
    yield {"type": "response.function_call_arguments.delta", "delta": '{"path":"README.md"'}
    yield {"type": "response.function_call_arguments.delta", "delta": ',"content":"updated"}'}
    yield {"type": "response.function_call_arguments.done", "arguments": argumentsJson}
    yield {
        "type": "response.output_item.done",
        "item": {"type": "function_call", "id": "fc_test", "call_id": "call_test", "name": "edit", "arguments": argumentsJson},
    }
    yield {"type": "response.completed", "sequence_number": 5, "response": {"id": "resp_test", "status": "completed"}}


async def test_removes_partial_json_from_persisted_tool_call_blocks_at_output_item_done():
    model = _responsesModel(id="gpt-5-mini", name="GPT-5 Mini")
    output = createOutput(model)
    stream = AssistantMessageEventStream()
    pushed: list[Any] = []
    push = stream.push

    def pushSpy(event: Any, *args: Any, **kwargs: Any) -> None:
        pushed.append(event)
        push(event, *args, **kwargs)

    stream.push = pushSpy
    argumentsJson = '{"path":"README.md","content":"updated"}'

    await processResponsesStream(_partialJsonEvents(argumentsJson), output, stream, model)

    assert len(output.content) == 1
    persistedToolCall = output.content[0]
    assert persistedToolCall.type == "toolCall"
    assert persistedToolCall.arguments == {"path": "README.md", "content": "updated"}
    assert "partialJson" not in persistedToolCall.model_dump()

    toolCallEnd = next((event for event in pushed if event.type == "toolcall_end"), None)
    assert toolCallEnd is not None
    assert toolCallEnd.toolCall is persistedToolCall
    assert "partialJson" not in toolCallEnd.toolCall.model_dump()


# #10897
async def test_marks_malformed_final_arguments_instead_of_keeping_the_parsed_prefix():
    model = _responsesModel(id="gpt-5-mini", name="GPT-5 Mini")
    output = createOutput(model)
    stream = AssistantMessageEventStream()
    argumentsJson = '{"path":"README.md","content":"updated","mode":.5}'

    await processResponsesStream(_partialJsonEvents(argumentsJson), output, stream, model)

    toolCall = output.content[0]
    assert toolCall.type == "toolCall"
    assert toolCall.arguments == {}
    assert toolCall.argumentsError.endswith(f"Received arguments:\n{argumentsJson}")


# --- openai-responses-foreign-toolcall-id.test.ts -----------------------------------------------

COPILOT_RAW_TOOL_CALL_ID = (
    "call_4VnzVawQXPB9MgYib7CiQFEY|I9b95oN1wD/cHXKTw3PpRkL6KkCtzTJhUxMouMWYwHeTo2j3htzfSk7YPx2vifiIM4g3A8XXyOj8q4Bt6SLUG7gqY1E3ELkrkVQNHglRfUmWj84lqxJY+Puieb3VKyX0FB+83TUzn91cDMF/4gzt990IzqVrc+nIb9RRscRD070Du16q1glydVjWR0SBJsE6TbY/esOjFpqplogQqrajm1eI++f3eLi73R6q7hVusY0QbeFySVxABCjhN0lXB04caBe1rzHjYzul6MAXj7uq+0r17VLq+yrtyYhN12wkmFqHeqTyEei6EFPbMy24Nc+IbJlkP0OCg02W+gOnyBFcbi2ctvJFSOhSjt1CqBdqCnnhwUqXjbWiT0wh3DmLScRgTHmGkaI+oAcQQjfic65nxj+TnEkReA=="
)


def test_hashes_foreign_copilot_tool_item_ids_into_a_bounded_codex_safe_shape():
    model = getModel("openai-codex", "gpt-5.5")
    assistant = AssistantMessage(
        content=[ToolCall(id=COPILOT_RAW_TOOL_CALL_ID, name="edit", arguments={"path": "src/styles/app.css"})],
        api="openai-responses",
        provider="github-copilot",
        model="gpt-5.5",
        usage=_usage(),
        stopReason="toolUse",
        timestamp=_now() - 2000,
    )
    toolResult = ToolResultMessage(
        toolCallId=COPILOT_RAW_TOOL_CALL_ID,
        toolName="edit",
        content=[TextContent(text="ok")],
        isError=False,
        timestamp=_now() - 1000,
    )
    context = normalizeContext(
        Context(
            systemPrompt="You are concise.",
            messages=[UserMessage(content="Use the tool.", timestamp=_now() - 3000), assistant, toolResult],
        )
    )

    input = convertResponsesMessages(model, context, {"openai", "openai-codex", "opencode"})
    functionCall = _find(input, "function_call")

    assert functionCall is not None
    expectedItemId = f"fc_{shortHash(COPILOT_RAW_TOOL_CALL_ID.split('|')[1])}"
    assert functionCall["id"] == expectedItemId
    assert len(functionCall["id"]) <= 64
    assert re.fullmatch(r"fc_[A-Za-z0-9]+", functionCall["id"])


# --- openai-responses-message-id.test.ts --------------------------------------------------------


def test_generates_unique_fallback_message_ids_for_multiple_text_blocks_in_one_assistant_turn():
    model = getModel("openai-codex", "gpt-5.5")
    assistant = AssistantMessage(
        content=[TextContent(text="first answer"), TextContent(text="visible answer")],
        api="anthropic-messages",
        provider="anthropic",
        model="claude-opus-4-8",
        usage=_usage(),
        stopReason="stop",
        timestamp=_now() - 1000,
    )
    context = normalizeContext(
        Context(systemPrompt="You are concise.", messages=[UserMessage(content="hello", timestamp=_now() - 2000), assistant])
    )

    input = convertResponsesMessages(model, context, {"openai", "openai-codex", "opencode"})
    messageIds = [item["id"] for item in input if item.get("type") == "message" and isinstance(item.get("id"), str)]

    assert messageIds == ["msg_pi_1", "msg_pi_1_1"]
    assert len(set(messageIds)) == len(messageIds)


# --- openai-responses-empty-tool-result.test.ts -------------------------------------------------


def test_uses_the_no_tool_output_placeholder_for_empty_tool_results_without_images():
    model = getModel("openai", "gpt-4o-mini")
    now = _now()
    assistant = AssistantMessage(
        content=[ToolCall(id="tool-1", name="bash", arguments={"command": "true"})],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=_usage(),
        stopReason="toolUse",
        timestamp=now,
    )
    context = normalizeContext(
        Context(
            messages=[
                UserMessage(content="Run the command", timestamp=now - 1),
                assistant,
                ToolResultMessage(
                    toolCallId="tool-1", toolName="bash", content=[TextContent(text="")], isError=False, timestamp=now + 1
                ),
            ]
        )
    )

    input = convertResponsesMessages(model, context, {"openai", "openai-codex", "opencode"})
    functionCallOutput = _find(input, "function_call_output")

    assert functionCallOutput
    assert functionCallOutput["output"] == "(no tool output)"
    assert "see attached image" not in functionCallOutput["output"]
