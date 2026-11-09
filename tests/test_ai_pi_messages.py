"""Translated from pi's ``packages/ai/test/pi-messages.test.ts`` (v1.1.0)."""

from __future__ import annotations

import json
import time
from typing import Any

from misaka.ai.api.pi_messages import stream, streamSimple
from misaka.ai.compat import getApiProvider
from misaka.ai.types import Context, Model, ModelCost, SimpleStreamOptions, UserMessage
from misaka.ai.utils.transcript import normalizeContext
from tests.http_capture import Reply, captureServer


def startServer(
    status: int = 200, headers: dict[str, str] | None = None, events: list[Any] | None = None, rawBody: str | None = None
):
    def respond(_request: Any) -> Reply:
        if status != 200:
            return Reply(status=status, headers={"content-type": "application/json"}, body=(rawBody or "{}").encode())
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events or [])
        return Reply(headers={"content-type": "text/event-stream", **(headers or {})}, body=body.encode())

    return captureServer(respond)


def createModel(baseUrl: str) -> Model:
    return Model(
        id="auto",
        name="Radius Auto",
        api="pi-messages",
        provider="radius",
        baseUrl=baseUrl,
        reasoning=False,
        input=["text"],
        cost=ModelCost(input=1, output=2, cacheRead=0.1, cacheWrite=0.2),
        contextWindow=128000,
        maxTokens=16384,
    )


TIMESTAMP = time.time_ns() // 1_000_000
context = Context(messages=[UserMessage(content="Hello", timestamp=TIMESTAMP)])

usage = {
    "input": 10,
    "output": 5,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 15,
    "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0, "cacheWrite": 0, "total": 0.3},
}


def _usage(message: Any) -> dict[str, Any]:
    return message.usage.model_dump(exclude_none=True)


async def test_streams_text_and_tool_calls_and_resolves_the_terminal_message():
    with startServer(
        events=[
            {"type": "start"},
            {"type": "text_start", "contentIndex": 0},
            {"type": "text_delta", "contentIndex": 0, "delta": "Hel"},
            {"type": "text_delta", "contentIndex": 0, "delta": "lo"},
            {"type": "text_end", "contentIndex": 0, "content": "Hello"},
            {"type": "toolcall_start", "contentIndex": 1, "id": "call_1", "toolName": "read"},
            {"type": "toolcall_delta", "contentIndex": 1, "delta": '{"path":'},
            {"type": "toolcall_delta", "contentIndex": 1, "delta": '"a.txt"}'},
            {
                "type": "toolcall_end",
                "contentIndex": 1,
                "toolCall": {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "a.txt"}},
            },
            {"type": "done", "reason": "toolUse", "usage": usage, "responseId": "resp_1", "providerThinkingLevel": "high"},
        ]
    ) as server:
        model = createModel(f"{server.baseUrl}/v1")
        events: list[Any] = []
        partialStopReasons: list[str] = []
        eventStream = stream(
            model,
            normalizeContext(context),
            {"apiKey": "test-key", "sessionId": "session-1", "toolChoice": "auto", "maxTokens": 100, "headers": {"x-custom": "1"}},
        )
        async for event in eventStream:
            if hasattr(event, "partial"):
                partialStopReasons.append(event.partial.stopReason)
            events.append(event)
        message = await eventStream.result()

    assert partialStopReasons[0] == "pending"
    assert message.stopReason == "toolUse"
    assert _usage(message) == usage
    assert message.responseId == "resp_1"
    assert message.providerThinkingLevel == "high"
    assert message.model == "auto"
    assert message.provider == "radius"
    assert [block.model_dump(exclude_none=True) for block in message.content] == [
        {"type": "text", "text": "Hello"},
        {"type": "toolCall", "id": "call_1", "name": "read", "arguments": {"path": "a.txt"}},
    ]
    assert any(event.type == "text_delta" for event in events)
    assert len([event for event in events if event.type == "toolcall_end"]) == 1

    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.path == "/v1/messages"
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.headers["x-custom"] == "1"
    assert request.body == {
        "model": "auto",
        "context": {"messages": [{"role": "user", "content": "Hello", "timestamp": TIMESTAMP}]},
        "options": {"maxTokens": 100, "sessionId": "session-1", "toolChoice": "auto"},
    }


async def test_forwards_parsed_wire_events_in_order_before_converting_them():
    wireEvents = [
        {"type": "start"},
        {"type": "text_start", "contentIndex": 0},
        {"type": "text_delta", "contentIndex": 0, "delta": "Hello", "gatewayField": "upstream-value"},
        {"type": "text_end", "contentIndex": 0, "content": "Hello"},
        {"type": "done", "reason": "stop", "usage": usage, "responseId": "resp_1"},
    ]
    received: list[Any] = []
    eventModels: list[Any] = []

    async def onProviderStreamEvent(event: Any, eventModel: Any) -> None:
        received.append(event)
        eventModels.append(eventModel)

    with startServer(events=wireEvents) as server:
        model = createModel(f"{server.baseUrl}/v1")
        message = await streamSimple(
            model,
            normalizeContext(context),
            SimpleStreamOptions(apiKey="test-key", onProviderStreamEvent=onProviderStreamEvent),
        ).result()

    assert received == wireEvents
    assert eventModels == [model] * len(wireEvents)
    assert message.stopReason == "stop"
    assert message.responseId == "resp_1"
    assert [block.model_dump(exclude_none=True) for block in message.content] == [{"type": "text", "text": "Hello"}]


async def test_appends_debug_1_and_reports_response_headers_via_on_response():
    observed: list[Any] = []
    with startServer(
        headers={"x-pi-gateway-upstream-provider": "anthropic"}, events=[{"type": "done", "reason": "stop", "usage": usage}]
    ) as server:
        model = createModel(f"{server.baseUrl}/v1")
        message = await streamSimple(
            model,
            normalizeContext(context),
            {"apiKey": "test-key", "debug": True, "onResponse": lambda response, _model: observed.append(response["headers"])},
        ).result()

    assert message.stopReason == "stop"
    assert server.requests[0].path == "/v1/messages?debug=1"
    assert observed[0]["x-pi-gateway-upstream-provider"] == "anthropic"


async def test_surfaces_backend_error_responses_with_diagnostics():
    with startServer(status=401, rawBody=json.dumps({"error": {"message": "Token expired", "code": "unauthorized"}})) as server:
        model = createModel(f"{server.baseUrl}/v1")
        message = await stream(model, normalizeContext(context), {"apiKey": "stale"}).result()

    assert message.stopReason == "error"
    assert "401" in message.errorMessage
    assert "Token expired" in message.errorMessage
    assert "unauthorized" in message.errorMessage
    assert message.diagnostics[0].type == "pi_messages_response_failure"
    assert message.diagnostics[0].details["status"] == 401


async def test_propagates_server_sent_error_events():
    with startServer(
        events=[{"type": "start"}, {"type": "error", "reason": "error", "usage": usage, "errorMessage": "Upstream failed"}]
    ) as server:
        model = createModel(f"{server.baseUrl}/v1")
        message = await stream(model, normalizeContext(context), {"apiKey": "test-key"}).result()

    assert message.stopReason == "error"
    assert message.errorMessage == "Upstream failed"
    assert _usage(message) == usage


async def test_errors_when_no_api_key_is_provided():
    model = createModel("http://127.0.0.1:1/v1")

    message = await stream(model, normalizeContext(context)).result()

    assert message.stopReason == "error"
    assert "No API key provided" in message.errorMessage


async def test_errors_when_the_stream_ends_without_a_terminal_event():
    with startServer(
        events=[
            {"type": "start"},
            {"type": "text_start", "contentIndex": 0},
            {"type": "text_delta", "contentIndex": 0, "delta": "partial"},
        ]
    ) as server:
        model = createModel(f"{server.baseUrl}/v1")
        message = await stream(model, normalizeContext(context), {"apiKey": "test-key"}).result()

    assert message.stopReason == "error"
    assert "stream ended without a terminal event" in message.errorMessage


def test_is_registered_as_a_builtin_api_provider():
    assert getApiProvider("pi-messages") is not None
