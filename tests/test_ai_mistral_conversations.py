"""Translated from pi's ``packages/ai/test/mistral-{http-transport,raw-stop-reason,tool-schema}.test.ts`` (v1.3.0).

MISAKA fork: there is no ``fetch`` option; requests go to an ``httpx.MockTransport`` (``fetchMock``) and a body is a
``ChunkedBody`` stream (byte-wise, or one that never sends). The tool-schema case answers with an error instead of
dialling a closed local port.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from misaka.ai.api import mistral_conversations as mistral
from misaka.ai.api.mistral_conversations import stream as streamMistral
from misaka.ai.compat import complete, getModel
from misaka.ai.types import (
    AssistantMessage,
    Context,
    ImageContent,
    Model,
    TextContent,
    ThinkingContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.retry import isRetryableAssistantError
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import AbortController

PI_USER_AGENT = getPiUserAgent()


class ChunkedBody(httpx.AsyncByteStream):
    """``new ReadableStream({...})``: the chunks, then the end; ``endless`` never sends anything."""

    def __init__(self, chunks: list[bytes], endless: bool = False) -> None:
        self.chunks = chunks
        self.endless = endless

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.endless:
            await asyncio.Event().wait()


def createSseResponse(events: list[Any], headers: dict[str, str] | None = None) -> httpx.Response:
    body = "\r\n\r\n".join(f"data: {json.dumps(event)}" for event in events) + "\r\n\r\ndata: [DONE]\r\n\r\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream", **(headers or {})}, stream=ChunkedBody([body.encode()]))


def createBytewiseSseResponse(event: Any) -> httpx.Response:
    data = f"data: {json.dumps(event, ensure_ascii=False)}\r\n\r\ndata: [DONE]\r\n\r\n".encode()
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkedBody([bytes([byte]) for byte in data]))


def createTerminalEvent(finishReason: str = "stop") -> dict[str, Any]:
    return {
        "id": "mistral-response-id",
        "model": "mistral-large-latest",
        "choices": [{"index": 0, "finish_reason": finishReason, "delta": {}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


@pytest.fixture
def fetchMock(monkeypatch):
    """``options.fetch``: ``fetchMock.impl(request)`` answers; the requests are recorded."""
    state = SimpleNamespace(calls=[], impl=lambda _request: createSseResponse([createTerminalEvent()]))
    realClient = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        state.calls.append(request)
        return state.impl(request)

    monkeypatch.setattr(mistral.httpx, "AsyncClient", lambda **kwargs: realClient(transport=httpx.MockTransport(handler), **kwargs))
    return state


def _hello() -> Any:
    return normalizeContext(Context(messages=[UserMessage(content="hello", timestamp=1)]))


def _blocks(message: AssistantMessage) -> list[dict[str, Any]]:
    return [block.model_dump(exclude_none=True) for block in message.content]


# --- mistral-http-transport.test.ts -------------------------------------------------------------


async def test_serializes_sdk_style_payloads_to_the_mistral_wire_format(fetchMock):
    model = getModel("mistral", "mistral-large-latest")
    context = normalizeContext(
        Context(
            systemPrompt="Be precise",
            messages=[
                UserMessage(
                    content=[TextContent(text="describe"), ImageContent(data="aGVsbG8=", mimeType="image/png")], timestamp=1
                )
            ],
            tools=[
                Tool(
                    name="lookup",
                    description="Look something up",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                )
            ],
        )
    )
    fetchMock.impl = lambda _request: createSseResponse([createTerminalEvent()], {"x-request-id": "request-1"})
    callbackPayload: dict[str, Any] = {}
    callbackResponse: list[Any] = []

    def onPayload(payload: Any, _model: Any) -> Any:
        callbackPayload.update(payload)
        return {
            **payload,
            "topP": 0.9,
            "randomSeed": 42,
            "responseFormat": {
                "type": "json_schema",
                "jsonSchema": {
                    "name": "result",
                    "schemaDefinition": {"type": "object", "properties": {"maxTokens": {"type": "number"}}},
                },
            },
            "presencePenalty": 0.1,
            "frequencyPenalty": 0.2,
            "parallelToolCalls": True,
            "safePrompt": True,
        }

    message = await streamMistral(
        model,
        context,
        {
            "apiKey": "secret",
            "headers": {"x-custom": "value"},
            "maxTokens": 123,
            "promptMode": "reasoning",
            "reasoningEffort": "high",
            "toolChoice": {"type": "function", "function": {"name": "lookup"}},
            "sessionId": "session-1",
            "onPayload": onPayload,
            "onResponse": lambda response, _model: callbackResponse.append(response),
        },
    ).result()

    assert message.stopReason == "stop", message.errorMessage
    [request] = fetchMock.calls
    assert str(request.url) == "https://api.mistral.ai/v1/chat/completions"
    headers = request.headers
    assert headers["authorization"] == "Bearer secret"
    assert headers["accept"] == "text/event-stream"
    assert headers["x-affinity"] == "session-1"
    assert headers["x-custom"] == "value"
    assert headers["user-agent"] == PI_USER_AGENT
    assert callbackPayload["maxTokens"] == 123
    assert callbackPayload["promptMode"] == "reasoning"
    assert callbackPayload["promptCacheKey"] == "session-1"
    assert callbackResponse[0] == {
        "status": 200,
        "headers": {"content-type": "text/event-stream", "x-request-id": "request-1"},
    }

    wirePayload = json.loads(request.content)
    assert wirePayload["max_tokens"] == 123
    assert wirePayload["prompt_mode"] == "reasoning"
    assert wirePayload["reasoning_effort"] == "high"
    assert wirePayload["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}
    assert wirePayload["prompt_cache_key"] == "session-1"
    assert wirePayload["top_p"] == 0.9
    assert wirePayload["random_seed"] == 42
    assert wirePayload["presence_penalty"] == 0.1
    assert wirePayload["frequency_penalty"] == 0.2
    assert wirePayload["parallel_tool_calls"] is True
    assert wirePayload["safe_prompt"] is True
    assert wirePayload["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "result", "schema": {"type": "object", "properties": {"maxTokens": {"type": "number"}}}},
    }
    assert "maxTokens" not in wirePayload
    assert "promptMode" not in wirePayload
    assert "promptCacheKey" not in wirePayload
    assert wirePayload["messages"] == [
        {"role": "system", "content": "Be precise"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": "data:image/png;base64,aGVsbG8="},
            ],
        },
    ]


async def test_serializes_assistant_thinking_tool_calls_and_tool_results_for_replay(fetchMock):
    model = getModel("mistral", "mistral-large-latest")
    context = normalizeContext(
        Context(
            messages=[
                AssistantMessage(
                    api="mistral-conversations",
                    provider="mistral",
                    model=model.id,
                    content=[
                        ThinkingContent(thinking="reason"),
                        TextContent(text="answer"),
                        ToolCall(id="abc123456", name="lookup", arguments={"query": "pi"}),
                    ],
                    usage=Usage(
                        input=0,
                        output=0,
                        cacheRead=0,
                        cacheWrite=0,
                        totalTokens=0,
                        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
                    ),
                    stopReason="toolUse",
                    timestamp=1,
                ),
                ToolResultMessage(
                    toolCallId="abc123456",
                    toolName="lookup",
                    content=[TextContent(text="found"), ImageContent(data="aGVsbG8=", mimeType="image/png")],
                    isError=False,
                    timestamp=2,
                ),
            ]
        )
    )

    message = await streamMistral(model, context, {"apiKey": "test"}).result()

    assert message.stopReason == "stop", message.errorMessage
    wirePayload = json.loads(fetchMock.calls[0].content)
    assert wirePayload["messages"] == [
        {
            "role": "assistant",
            "prefix": False,
            "content": [
                {"type": "thinking", "thinking": [{"type": "text", "text": "reason"}]},
                {"type": "text", "text": "answer"},
            ],
            "tool_calls": [
                {
                    "id": "abc123456",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"query":"pi"}'},
                    "index": 0,
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "abc123456",
            "name": "lookup",
            "content": [
                {"type": "text", "text": "found"},
                {"type": "image_url", "image_url": "data:image/png;base64,aGVsbG8="},
            ],
        },
    ]


async def test_parses_native_thinking_text_fragmented_tool_calls_and_cached_token_usage(fetchMock):
    model = getModel("mistral", "mistral-large-latest")
    events = [
        {
            "id": "response-1",
            "model": model.id,
            "choices": [
                {"index": 0, "finish_reason": None, "delta": {"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "reason"}]}]}}
            ],
        },
        {
            "id": "response-1",
            "model": model.id,
            "choices": [{"index": 0, "finish_reason": None, "delta": {"content": [{"type": "text", "text": "answer"}]}}],
        },
        {
            "id": "response-1",
            "model": model.id,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": None,
                    "delta": {"tool_calls": [{"id": "abc123456", "index": 0, "function": {"name": "lookup", "arguments": '{"query":'}}]},
                }
            ],
        },
        {
            "id": "response-1",
            "model": model.id,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "delta": {"tool_calls": [{"index": 0, "function": {"name": "", "arguments": '"pi"}'}}]},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14, "prompt_tokens_details": {"cached_tokens": 3}},
        },
    ]
    fetchMock.impl = lambda _request: createSseResponse(events)

    message = await streamMistral(model, _hello(), {"apiKey": "test"}).result()

    assert message.stopReason == "toolUse"
    assert message.rawStopReason == "tool_calls"
    assert message.responseId == "response-1"
    assert _blocks(message) == [
        {"type": "thinking", "thinking": "reason"},
        {"type": "text", "text": "answer"},
        {"type": "toolCall", "id": "abc123456", "name": "lookup", "arguments": {"query": "pi"}},
    ]
    assert (message.usage.input, message.usage.output, message.usage.cacheRead, message.usage.cacheWrite, message.usage.totalTokens) == (
        7,
        4,
        3,
        0,
        14,
    )


# #9674: GLM models on Mistral send empty content deltas at the start, around tool calls,
# and sometimes mid-thinking. They must not open blocks or split thinking.
async def test_ignores_empty_content_deltas(fetchMock):
    model = getModel("mistral", "zai-glm-5-3")

    def thinking(text: str) -> dict[str, Any]:
        return {"type": "thinking", "thinking": [{"type": "text", "text": text}]}

    def toolCall(args: str, first: bool) -> dict[str, Any]:
        return {"index": 0, **({"id": "abc123456"} if first else {}), "function": {"name": "read" if first else "", "arguments": args}}

    deltas = [
        {"content": ""},
        {"content": [thinking("first part,")]},
        {"content": ""},
        {"content": [{"type": "text", "text": ""}]},
        {"content": [thinking(" second part."), {"type": "text", "text": "Reading."}]},
        {"content": "", "tool_calls": [toolCall("", True)]},
        {"content": "", "tool_calls": [toolCall('{"path":', False)]},
        {"content": "", "tool_calls": [toolCall('"a.txt"}', False)]},
        {"content": ""},
    ]
    events = [
        {"id": "response-1", "model": model.id, "choices": [{"index": 0, "finish_reason": "tool_calls" if i == len(deltas) - 1 else None, "delta": delta}]}
        for i, delta in enumerate(deltas)
    ]
    fetchMock.impl = lambda _request: createSseResponse(events)

    message = await streamMistral(model, _hello(), {"apiKey": "test"}).result()

    assert _blocks(message) == [
        {"type": "thinking", "thinking": "first part, second part."},
        {"type": "text", "text": "Reading."},
        {"type": "toolCall", "id": "abc123456", "name": "read", "arguments": {"path": "a.txt"}},
    ]


async def test_forwards_each_parsed_sse_payload_before_normalizing_it(fetchMock):
    model = getModel("mistral", "mistral-large-latest")
    events = [
        {"id": "response-1", "provider_metadata": {"request": "test"}, "choices": [{"index": 0, "finish_reason": None, "delta": {"content": "hello"}}]},
        {
            "id": "response-1",
            "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    fetchMock.impl = lambda _request: createSseResponse(events)
    received: list[Any] = []
    eventModels: list[Model] = []

    async def onProviderStreamEvent(event: Any, eventModel: Model) -> None:
        received.append(event)
        eventModels.append(eventModel)

    result = await streamMistral(model, _hello(), {"apiKey": "test", "onProviderStreamEvent": onProviderStreamEvent}).result()

    assert received == events
    assert eventModels == [model, model]
    assert result.stopReason == "stop"
    assert _blocks(result) == [{"type": "text", "text": "hello"}]


async def test_parses_sse_and_utf8_sequences_split_across_transport_chunks(fetchMock):
    model = getModel("mistral", "mistral-large-latest")
    fetchMock.impl = lambda _request: createBytewiseSseResponse({
        "id": "response-bytewise",
        "model": model.id,
        "choices": [{"index": 0, "finish_reason": "stop", "delta": {"content": "héllo 🌍"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    })

    message = await streamMistral(model, _hello(), {"apiKey": "test"}).result()

    assert message.stopReason == "stop"
    assert _blocks(message) == [{"type": "text", "text": "héllo 🌍"}]


async def test_honors_case_insensitive_header_overrides_and_explicit_affinity_suppression(fetchMock):
    model = Model.model_validate(
        {
            **getModel("mistral", "mistral-large-latest").model_dump(),
            "headers": {"Authorization": "Bearer model-key", "X-Affinity": "model-affinity"},
        }
    )

    await streamMistral(
        model,
        _hello(),
        {
            "apiKey": "request-key",
            "sessionId": "automatic-affinity",
            "headers": {"authorization": None, "x-affinity": None, "User-Agent": "custom-agent"},
        },
    ).result()

    requestHeaders = fetchMock.calls[0].headers
    assert "authorization" not in requestHeaders
    assert "x-affinity" not in requestHeaders
    assert requestHeaders["user-agent"] == "custom-agent"


def _endlessBody(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkedBody([], endless=True))


async def test_aborts_while_waiting_for_an_sse_chunk(fetchMock):
    fetchMock.impl = _endlessBody
    controller = AbortController()

    result = asyncio.ensure_future(
        streamMistral(getModel("mistral", "mistral-large-latest"), _hello(), {"apiKey": "test", "signal": controller.signal}).result()
    )
    controller.abort()
    message = await result

    assert message.stopReason == "aborted"


async def test_applies_the_request_timeout_while_waiting_for_response_headers(fetchMock):
    async def neverAnswers(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fetchMock.impl = neverAnswers

    message = await streamMistral(getModel("mistral", "mistral-large-latest"), _hello(), {"apiKey": "test", "timeoutMs": 5}).result()

    assert message.stopReason == "error"
    assert message.errorMessage == "Mistral response headers timed out after 5ms"


class _PacedThinkingBody(httpx.AsyncByteStream):
    """Five thinking deltas 10 ms apart, then the terminal event."""

    async def __aiter__(self):
        thinkingEvent = {
            "choices": [{"index": 0, "delta": {"content": [{"type": "thinking", "thinking": [{"type": "text", "text": "x"}]}]}}],
        }
        for _ in range(5):
            yield f"data: {json.dumps(thinkingEvent)}\n\n".encode()
            await asyncio.sleep(0.01)
        yield f"data: {json.dumps(createTerminalEvent())}\n\ndata: [DONE]\n\n".encode()


# Regression test for #10609: an active stream must not be cut off after timeoutMs.
async def test_does_not_abort_an_active_stream_that_lasts_longer_than_the_request_timeout(fetchMock):
    fetchMock.impl = lambda _request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_PacedThinkingBody()
    )

    message = await streamMistral(getModel("mistral", "mistral-large-latest"), _hello(), {"apiKey": "test", "timeoutMs": 20}).result()

    assert message.stopReason == "stop"
    assert _blocks(message) == [{"type": "thinking", "thinking": "xxxxx"}]


async def test_preserves_http_status_and_response_bodies_in_errors(fetchMock):
    fetchMock.impl = lambda _request: httpx.Response(
        403, content=b'{"message":"blocked by gateway"}', extensions={"reason_phrase": b"Forbidden"}
    )

    message = await streamMistral(getModel("mistral", "mistral-large-latest"), _hello(), {"apiKey": "test"}).result()

    assert message.stopReason == "error"
    assert message.errorMessage == 'Mistral API error (403): {"message":"blocked by gateway"}'


# --- mistral-raw-stop-reason.test.ts ------------------------------------------------------------


def createFetch(finishReason: str) -> Callable[[httpx.Request], httpx.Response]:
    def answer(_request: httpx.Request) -> httpx.Response:
        event = {
            "id": "mistral-response-id",
            "model": "devstral-medium-latest",
            "choices": [{"index": 0, "finish_reason": finishReason, "delta": {}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1},
        }
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=ChunkedBody([f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n".encode()])
        )

    return answer


async def test_preserves_raw_mistral_finish_reasons_for_successful_stops(fetchMock):
    fetchMock.impl = createFetch("stop")

    message = await streamMistral(getModel("mistral", "devstral-medium-latest"), _hello(), {"apiKey": "test"}).result()

    assert message.stopReason == "stop"
    assert message.rawStopReason == "stop"
    assert message.errorMessage is None


async def test_preserves_raw_mistral_finish_reasons_for_provider_error_stops(fetchMock):
    fetchMock.impl = createFetch("error")

    message = await streamMistral(getModel("mistral", "devstral-medium-latest"), _hello(), {"apiKey": "test"}).result()

    assert message.stopReason == "error"
    assert message.rawStopReason == "error"
    assert message.errorMessage == "Provider stopped with: error (server error)"
    # #10487
    assert isRetryableAssistantError(message) is True


async def test_treats_unknown_mistral_finish_reasons_as_provider_error_stops(fetchMock):
    fetchMock.impl = createFetch("unmapped_error")

    message = await streamMistral(getModel("mistral", "devstral-medium-latest"), _hello(), {"apiKey": "test"}).result()

    assert message.stopReason == "error"
    assert message.rawStopReason == "unmapped_error"
    assert message.errorMessage == "Provider stopped with: unmapped_error"
    assert isRetryableAssistantError(message) is False


# --- mistral-tool-schema.test.ts ----------------------------------------------------------------


async def test_strips_symbol_keys_before_the_tool_schema_goes_out(fetchMock):
    fetchMock.impl = lambda _request: httpx.Response(400, content=b'{"message":"rejected"}')
    model = Model.model_validate({**getModel("mistral", "devstral-medium-latest").model_dump(), "baseUrl": "http://127.0.0.1:9"})
    context = Context(
        messages=[UserMessage(content="Hi", timestamp=1)],
        tools=[
            Tool(
                name="inspect_schema",
                description="Inspect the schema",
                parameters={
                    "type": "object",
                    "properties": {
                        "nested": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
                    },
                    "required": ["nested"],
                },
                constrainedSampling={"type": "json_schema", "strict": "require"},
            )
        ],
    )
    captured: list[Any] = []

    def onPayload(payload: Any, _model: Any) -> Any:
        captured.append(payload)
        return payload

    response = await complete(model, context, {"apiKey": "fake-key", "onPayload": onPayload})

    tools = captured[0]["tools"]
    assert len(tools) == 1
    assert tools[0]["function"]["strict"] is True
    payloadParameters = tools[0]["function"]["parameters"]
    assert all(isinstance(key, str) for key in payloadParameters)
    properties = payloadParameters["properties"]
    assert all(isinstance(key, str) for key in properties)
    assert all(isinstance(key, str) for key in properties["nested"])
    assert response.stopReason == "error"
    assert "Input validation failed" not in response.errorMessage
