"""Translated from pi's ``packages/ai/test/openai-responses-terminal-event.test.ts`` (v1.1.0).

pi mocks the ``openai`` module with a fake client whose ``responses.create().withResponse()`` yields a short stream;
here the SDK glue's ``_create_with_response`` answers with it.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from misaka.ai.api import _openai_sdk
from misaka.ai.api.openai_responses import stream as streamOpenAIResponses
from misaka.ai.api.openai_responses_shared import processResponsesStream
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    ModelCost,
    TextContent,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import normalizeContext


@pytest.fixture(autouse=True)
def _fakeOpenAI(monkeypatch):
    async def createMockResponsesStream():
        yield {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_wrapper_early_eof"}}
        yield {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": {"type": "reasoning", "id": "rs_wrapper_early_eof", "summary": []},
        }
        yield {
            "type": "response.reasoning_text.delta",
            "sequence_number": 2,
            "output_index": 0,
            "content_index": 0,
            "item_id": "rs_wrapper_early_eof",
            "delta": "partial reasoning before the wrapper stream ends",
        }

    async def create(*_args: Any) -> _openai_sdk._WithResponse:
        return _openai_sdk._WithResponse(createMockResponsesStream(), httpx.Response(200))

    monkeypatch.setattr(_openai_sdk, "_create_with_response", create)


def createModel() -> Model:
    return Model(
        id="gpt-5-mini",
        name="GPT-5 Mini",
        api="openai-responses",
        provider="openai",
        baseUrl="https://api.openai.com/v1",
        reasoning=True,
        input=["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=400000,
        maxTokens=128000,
    )


def createOutput(model: Model) -> AssistantMessage:
    return AssistantMessage(
        content=[],
        api=model.api,
        provider=model.provider,
        model=model.id,
        usage=Usage(
            input=0,
            output=0,
            cacheRead=0,
            cacheWrite=0,
            totalTokens=0,
            cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
        ),
        stopReason="pending",
        timestamp=time.time_ns() // 1_000_000,
    )


async def createEarlyEofEvents():
    yield {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_early_eof"}}
    yield {
        "type": "response.output_item.added",
        "sequence_number": 1,
        "output_index": 0,
        "item": {"type": "reasoning", "id": "rs_early_eof", "summary": []},
    }
    yield {
        "type": "response.reasoning_text.delta",
        "sequence_number": 2,
        "output_index": 0,
        "content_index": 0,
        "item_id": "rs_early_eof",
        "delta": "partial reasoning before the stream ends",
    }


async def createCompletedEvents():
    yield {
        "type": "response.completed",
        "sequence_number": 0,
        "response": {
            "id": "resp_completed",
            "status": "completed",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 7,
                "total_tokens": 27,
                "input_tokens_details": {"cached_tokens": 2, "cache_write_tokens": 3},
            },
        },
    }


async def createIncompleteEvents(reason: str = "max_output_tokens"):
    yield {
        "type": "response.incomplete",
        "sequence_number": 0,
        "response": {
            "id": "resp_incomplete",
            "status": "incomplete",
            "incomplete_details": {"reason": reason},
            "usage": {
                "input_tokens": 30,
                "output_tokens": 12,
                "total_tokens": 42,
                "input_tokens_details": {"cached_tokens": 5},
            },
        },
    }


async def createFailedEvents():
    yield {
        "type": "response.failed",
        "sequence_number": 0,
        "response": {"id": "resp_failed", "status": "failed", "error": {"code": "server_error", "message": "boom"}},
    }


async def createPhasedMessageEvents(phases: tuple[str, str], terminalStatus: str = "completed"):
    yield {
        "type": "response.output_item.added",
        "sequence_number": 0,
        "output_index": 0,
        "item": {
            "type": "message",
            "id": "msg_phase",
            "role": "assistant",
            "status": "in_progress",
            "content": [],
            "phase": phases[0],
        },
    }
    yield {
        "type": "response.output_item.done",
        "sequence_number": 1,
        "output_index": 0,
        "item": {
            "type": "message",
            "id": "msg_phase",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "answer", "annotations": []}],
            "phase": phases[1],
        },
    }
    if terminalStatus == "incomplete":
        yield {
            "type": "response.incomplete",
            "sequence_number": 2,
            "response": {"id": "resp_phase", "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        }
        return
    yield {"type": "response.completed", "sequence_number": 2, "response": {"id": "resp_phase", "status": "completed"}}


async def createUnfinishedToolCallEvents():
    yield {
        "type": "response.output_item.added",
        "sequence_number": 0,
        "output_index": 0,
        "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "bash", "arguments": ""},
    }
    yield {
        "type": "response.function_call_arguments.delta",
        "sequence_number": 1,
        "output_index": 0,
        "item_id": "fc_1",
        "delta": '{"command":"rm -rf /tmp/build',
    }
    yield {"type": "response.completed", "sequence_number": 2, "response": {"id": "resp_unfinished", "status": "completed"}}


# llama.cpp omits output_index from every event and sends both done events after all deltas.
async def createToolCallsWithoutOutputIndexEvents():
    def call(n: str) -> dict[str, Any]:
        return {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": "bash"}

    events = [
        {"type": "response.output_item.added", "item": {**call("a"), "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_a", "delta": '{"command":"echo a"}'},
        {"type": "response.output_item.added", "item": {**call("b"), "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_b", "delta": '{"command":"echo b"}'},
        {"type": "response.output_item.done", "item": {**call("a"), "arguments": '{"command":"echo a"}'}},
        {"type": "response.output_item.done", "item": {**call("b"), "arguments": '{"command":"echo b"}'}},
        {"type": "response.completed", "response": {"id": "resp_no_output_index", "status": "completed"}},
    ]
    for event in events:
        yield event


def _hiContext():
    return normalizeContext(
        Context(systemPrompt="", messages=[UserMessage(content=[TextContent(text="hi")], timestamp=0)], tools=[])
    )


def _observingStopReasons(stream: AssistantMessageEventStream) -> list[str]:
    observedStopReasons: list[str] = []
    push = stream.push

    def observe(event: Any, *args: Any, **kwargs: Any) -> None:
        if hasattr(event, "partial"):
            observedStopReasons.append(event.partial.stopReason)
        push(event, *args, **kwargs)

    stream.push = observe
    return observedStopReasons


async def test_rejects_streams_that_end_before_a_terminal_response_event():
    model = createModel()
    output = createOutput(model)
    stream = AssistantMessageEventStream()

    with pytest.raises(RuntimeError, match="OpenAI Responses stream ended before a terminal response event"):
        await processResponsesStream(createEarlyEofEvents(), output, stream, model)


async def test_rejects_completed_streams_whose_tool_call_never_received_output_item_done():
    model = createModel()

    with pytest.raises(
        RuntimeError, match=r"OpenAI Responses stream completed with an unfinished tool call: bash \(call_1\|fc_1\)"
    ):
        await processResponsesStream(createUnfinishedToolCallEvents(), createOutput(model), AssistantMessageEventStream(), model)


# https://github.com/earendil-works/pi/issues/9974
async def test_rejects_parallel_tool_calls_without_output_index_instead_of_running_mixed_up_calls():
    model = createModel()

    with pytest.raises(
        RuntimeError, match=r"OpenAI Responses stream completed with an unfinished tool call: bash \(call_a\|fc_a\)"
    ):
        await processResponsesStream(
            createToolCallsWithoutOutputIndexEvents(), createOutput(model), AssistantMessageEventStream(), model
        )


async def test_forwards_parsed_provider_stream_events_in_order():
    model = createModel()
    providerEvents: list[Any] = []
    eventModels: list[Model] = []

    async def onProviderStreamEvent(event: Any, eventModel: Model) -> None:
        providerEvents.append(event)
        eventModels.append(eventModel)

    stream = streamOpenAIResponses(model, _hiContext(), {"apiKey": "test", "onProviderStreamEvent": onProviderStreamEvent})

    await stream.result()

    assert len(providerEvents) == 3
    assert [event["type"] for event in providerEvents] == [
        "response.created",
        "response.output_item.added",
        "response.reasoning_text.delta",
    ]
    assert eventModels == [model, model, model]


async def test_emits_an_error_final_result_when_the_wrapper_stream_ends_before_a_terminal_response_event():
    model = createModel()
    stream = streamOpenAIResponses(model, _hiContext(), {"apiKey": "test"})
    events: list[Any] = []
    initialStopReason = None

    async for event in stream:
        if event.type == "start":
            initialStopReason = event.partial.stopReason
        events.append(event)

    result = await stream.result()
    assert initialStopReason == "pending"
    assert events[-1].type == "error"
    assert result.stopReason == "error"
    assert result.errorMessage == "OpenAI Responses stream ended before a terminal response event"


@pytest.mark.parametrize(
    ("phases", "expected"),
    [
        (("commentary", "commentary"), ["pending", "pending"]),
        (("final_answer", "final_answer"), ["stop", "stop"]),
        (("commentary", "final_answer"), ["pending", "stop"]),
    ],
)
async def test_tracks_message_phases(phases, expected):
    model = createModel()
    output = createOutput(model)
    stream = AssistantMessageEventStream()
    observedStopReasons = _observingStopReasons(stream)

    await processResponsesStream(createPhasedMessageEvents(phases), output, stream, model)

    assert observedStopReasons == expected
    assert output.stopReason == "stop"


async def test_replaces_a_provisional_final_answer_stop_with_an_incomplete_terminal_reason():
    model = createModel()
    output = createOutput(model)
    stream = AssistantMessageEventStream()
    observedStopReasons = _observingStopReasons(stream)

    await processResponsesStream(
        createPhasedMessageEvents(("final_answer", "final_answer"), "incomplete"), output, stream, model
    )

    assert observedStopReasons == ["stop", "stop"]
    assert output.stopReason == "length"


async def test_finalizes_completed_terminal_events_as_stop():
    model = createModel()
    output = createOutput(model)

    await processResponsesStream(createCompletedEvents(), output, AssistantMessageEventStream(), model)

    assert output.responseId == "resp_completed"
    assert output.stopReason == "stop"
    assert output.rawStopReason == "completed"
    assert (output.usage.input, output.usage.output, output.usage.cacheRead, output.usage.cacheWrite) == (15, 7, 2, 3)
    assert output.usage.totalTokens == 27


async def test_finalizes_incomplete_terminal_events_as_length_stops():
    model = createModel()
    output = createOutput(model)

    await processResponsesStream(createIncompleteEvents(), output, AssistantMessageEventStream(), model)

    assert output.responseId == "resp_incomplete"
    assert output.stopReason == "length"
    assert output.rawStopReason == "incomplete.max_output_tokens"
    assert (output.usage.input, output.usage.output, output.usage.cacheRead, output.usage.cacheWrite) == (25, 12, 5, 0)
    assert output.usage.totalTokens == 42


async def test_finalizes_content_filtered_incomplete_responses_as_non_retryable_errors():
    model = createModel()
    output = createOutput(model)

    await processResponsesStream(createIncompleteEvents("content_filter"), output, AssistantMessageEventStream(), model)

    assert output.stopReason == "error"
    assert output.rawStopReason == "incomplete.content_filter"
    assert output.errorMessage == "Response incomplete: content_filter"


async def test_preserves_unknown_provider_incomplete_reasons_as_non_retryable_errors():
    model = createModel()
    output = createOutput(model)

    await processResponsesStream(createIncompleteEvents("max_time_limit"), output, AssistantMessageEventStream(), model)

    assert output.stopReason == "error"
    assert output.rawStopReason == "incomplete.max_time_limit"
    assert output.errorMessage == "Response incomplete: max_time_limit"


async def test_rejects_failed_terminal_events_with_the_provider_error():
    model = createModel()
    output = createOutput(model)

    with pytest.raises(RuntimeError, match="server_error: boom"):
        await processResponsesStream(createFailedEvents(), output, AssistantMessageEventStream(), model)
    assert output.rawStopReason == "failed"
