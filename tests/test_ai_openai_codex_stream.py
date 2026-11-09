"""Translated from pi's ``packages/ai/test/openai-codex-stream.test.ts`` (v1.1.0).

MISAKA fork, in what the Python adapter forces on the fakes:

- ``fetch`` is httpx: requests go to an ``httpx.MockTransport`` (``fetchMock``); a body that stays open or is
  cancelled is a ``ControlledBody`` stream.
- ``WebSocket`` is the ``websockets`` client: ``MockWebSocket`` is built by the connector the adapter caches
  (``_cachedWebsocket``), answers ``recv()`` from a queue, and reports ``state`` the way ``readyState`` is numbered. A
  socket that never opens is a connector that waits out ``open_timeout`` and raises ``TimeoutError``, as websockets does.
- vitest's fake timers: the WebSocket timeouts run on real 50 ms clocks; the cached socket's age is moved back on its
  creation time instead of moving the system time; retry sleeps are recorded by replacing ``_sleep``.
- ``originator`` defaults to MISAKA's client name; zstd is checked only where a codec is installed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from misaka.ai.api import openai_codex_responses as codex
from misaka.ai.api.openai_codex_responses import (
    closeOpenAICodexWebSocketSessions,
    getOpenAICodexWebSocketDebugStats,
    resetOpenAICodexWebSocketDebugStats,
)
from misaka.ai.api.openai_codex_responses import stream as streamOpenAICodexResponses
from misaka.ai.api.openai_codex_responses import (
    streamSimple as streamSimpleOpenAICodexResponses,
)
from misaka.ai.types import (
    Context,
    Model,
    TextContent,
    Tool,
    ToolResultMessage,
    UserMessage,
)
from misaka.ai.utils.pi_user_agent import getPiUserAgent
from misaka.ai.utils.transcript import normalizeContext
from misaka.core.provider_attribution import CLIENT_NAME
from misaka.utils.js import AbortController


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    closeOpenAICodexWebSocketSessions()
    resetOpenAICodexWebSocketDebugStats()


def mockToken(accountId: str = "acc_test") -> str:
    payload = base64.b64encode(
        json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": accountId}}).encode("utf-8")
    ).decode("ascii")
    return f"aaa.{payload}.bbb"


def _zstdDecompress(body: bytes) -> bytes | None:
    try:
        from compression import zstd  # type: ignore[import-not-found]

        return zstd.decompress(body)
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore[import-not-found]

        return zstandard.ZstdDecompressor().decompress(body)
    except ImportError:
        return None


def _hasZstd() -> bool:
    try:
        from compression import zstd  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        try:
            import zstandard  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            return False
    return True


_HAS_ZSTD = _hasZstd()


def decodeCodexRequestBody(request: httpx.Request) -> dict[str, Any] | None:
    if request.headers.get("content-encoding") == "zstd":
        decompressed = _zstdDecompress(request.content)
        return None if decompressed is None else json.loads(decompressed)
    return json.loads(request.content) if request.content else None


def buildSSEPayload(status: str, includeDone: bool = False, endTurn: bool | None = None) -> str:
    terminalType = "response.incomplete" if status == "incomplete" else "response.completed"
    response: dict[str, Any] = {
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8, "input_tokens_details": {"cached_tokens": 0}},
    }
    if endTurn is not None:
        response["end_turn"] = endTurn
    events = [
        "data: "
        + json.dumps({
            "type": "response.output_item.added",
            "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "in_progress", "content": []},
        }),
        "data: " + json.dumps({"type": "response.content_part.added", "part": {"type": "output_text", "text": ""}}),
        "data: " + json.dumps({"type": "response.output_text.delta", "delta": "Hello"}),
        "data: "
        + json.dumps({
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Hello"}],
            },
        }),
        "data: " + json.dumps({"type": terminalType, "response": response}),
    ]

    if includeDone:
        events.append("data: [DONE]")

    return "\n\n".join(events) + "\n\n"


class ControlledBody(httpx.AsyncByteStream):
    """``new ReadableStream({start(controller) {...}})``: chunks are enqueued, the stream closes when told, and
    ``cancelled`` records the reader's ``cancel()``."""

    def __init__(self, *chunks: str, close: bool = True) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.cancelled = False
        for chunk in chunks:
            self.enqueue(chunk)
        if close:
            self.close()

    def enqueue(self, chunk: str) -> None:
        if not self.cancelled:
            self.queue.put_nowait(chunk.encode("utf-8"))

    def close(self) -> None:
        self.queue.put_nowait(None)

    async def __aiter__(self):
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                return
            yield chunk

    async def aclose(self) -> None:
        self.cancelled = True


def sseResponse(body: str | ControlledBody, status: int = 200) -> httpx.Response:
    stream = body if isinstance(body, ControlledBody) else ControlledBody(body)
    return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=stream)


@pytest.fixture
def fetchMock(monkeypatch):
    """``vi.stubGlobal("fetch", fetchMock)``: ``fetchMock.impl(request)`` answers (sync or async)."""
    state = SimpleNamespace(calls=[], impl=lambda _request: httpx.Response(500, text="unexpected fetch"))
    realClient = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        state.calls.append(request)
        result = state.impl(request)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    monkeypatch.setattr(codex.httpx, "AsyncClient", lambda **kwargs: realClient(transport=httpx.MockTransport(handler), **kwargs))
    return state


class MockWebSocket:
    """A websockets connection: ``send`` hands the parsed body to ``onSend``, which queues the answers (an exception
    in the queue is what ``recv`` raises)."""

    OPEN = 1
    CLOSED = 3

    def __init__(self, headers: dict[str, str], onSend: Callable[[MockWebSocket, Any], None], state: int | None = 1) -> None:
        self.headers = headers
        self.onSend = onSend
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = 0
        if state is not None:
            self.state = state

    def dispatchEvents(self, events: list[Any]) -> None:
        for event in events:
            self.queue.put_nowait(event if isinstance(event, BaseException) else json.dumps(event))

    def send(self, data: str) -> None:
        self.onSend(self, json.loads(data))

    async def recv(self) -> Any:
        item = await self.queue.get()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed += 1
        if hasattr(self, "state"):
            self.state = MockWebSocket.CLOSED


def stubWebSocket(monkeypatch, onSend: Callable[[MockWebSocket, Any], None], state: int | None = 1) -> list[MockWebSocket]:
    """``vi.stubGlobal("WebSocket", MockWebSocket)``: returns the sockets in the order they were opened."""
    sockets: list[MockWebSocket] = []

    async def connect(url: str, *, additional_headers: dict[str, str], **_kwargs: Any) -> MockWebSocket:
        socket = MockWebSocket(dict(additional_headers), onSend, state)
        sockets.append(socket)
        return socket

    monkeypatch.setattr(codex, "_cachedWebsocket", connect)
    return sockets


def codexModel(**fields: Any) -> Model:
    return Model.model_validate(
        {
            "id": "gpt-5.1-codex",
            "name": "GPT-5.1 Codex",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": "https://chatgpt.com/backend-api",
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 400000,
            "maxTokens": 128000,
            **fields,
        }
    )


def helloContext(timestamp: int | None = None) -> Any:
    return normalizeContext(
        Context(
            systemPrompt="You are a helpful assistant.",
            messages=[UserMessage(content="Say hello", timestamp=timestamp if timestamp is not None else time.time_ns() // 1_000_000)],
        )
    )


def _text(message: Any) -> str | None:
    return next((block.text for block in message.content if block.type == "text"), None)


def _completedEvent(responseId: str, **response: Any) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {"id": responseId, "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}, **response},
    }


# --- SSE ----------------------------------------------------------------------------------------


async def test_streams_sse_responses_and_forwards_raw_provider_events(fetchMock):
    token = mockToken()

    def impl(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://chatgpt.com/backend-api/codex/responses":
            assert request.headers["Authorization"] == f"Bearer {token}"
            assert request.headers["chatgpt-account-id"] == "acc_test"
            assert request.headers["OpenAI-Beta"] == "responses=experimental"
            assert request.headers["originator"] == CLIENT_NAME
            assert request.headers["User-Agent"] == getPiUserAgent()
            assert request.headers["accept"] == "text/event-stream"
            assert "x-api-key" not in request.headers
            return sseResponse(buildSSEPayload("completed"))
        return httpx.Response(404, text="not found")

    fetchMock.impl = impl
    model = codexModel()
    providerEvents: list[Any] = []
    providerEventModels: list[Model] = []
    streamResult = streamOpenAICodexResponses(
        model,
        helloContext(),
        {
            "apiKey": token,
            "transport": "sse",
            "onProviderStreamEvent": lambda event, eventModel: (providerEvents.append(event), providerEventModels.append(eventModel)),
        },
    )
    sawTextDelta = False
    sawDone = False

    async for event in streamResult:
        if event.type == "text_delta":
            sawTextDelta = True
        if event.type == "done":
            sawDone = True
            assert _text(event.message) == "Hello"

    assert sawTextDelta
    assert sawDone
    assert [event["type"] for event in providerEvents] == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_item.done",
        "response.completed",
    ]
    assert providerEventModels == [model] * 5


# Regression test for https://github.com/earendil-works/pi/issues/9047
async def test_processes_a_terminal_sse_event_without_a_trailing_blank_line(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed").rstrip())

    result = await streamOpenAICodexResponses(codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse"}).result()

    assert result.stopReason == "stop"
    assert _text(result) == "Hello"


async def test_completes_after_response_completed_even_when_the_sse_body_stays_open(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(
        ControlledBody(buildSSEPayload("completed", includeDone=True, endTurn=False), close=False)
    )

    result = await asyncio.wait_for(
        streamOpenAICodexResponses(codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse"}).result(), 1
    )

    assert _text(result) == "Hello"
    assert result.stopReason == "stop"
    assert result.endTurn is False


async def test_maps_response_incomplete_to_stop_reason_length_even_when_the_sse_body_stays_open(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(ControlledBody(buildSSEPayload("incomplete"), close=False))

    result = await asyncio.wait_for(
        streamOpenAICodexResponses(codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse"}).result(), 1
    )

    assert _text(result) == "Hello"
    assert result.stopReason == "length"


async def test_aborts_sse_fetch_after_the_configured_http_timeout_when_response_headers_do_not_arrive(fetchMock):
    async def neverAnswers(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    fetchMock.impl = neverAnswers

    result = await streamOpenAICodexResponses(
        codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse", "timeoutMs": 10}
    ).result()

    assert len(fetchMock.calls) == 1
    assert result.stopReason == "error"
    assert result.errorMessage == "Codex SSE response headers timed out after 10ms"


async def test_aborts_sse_body_reads_after_response_headers_arrive(fetchMock):
    body = ControlledBody(
        "\n\n".join([
            "data: "
            + json.dumps({
                "type": "response.output_item.added",
                "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "in_progress", "content": []},
            }),
            "data: " + json.dumps({"type": "response.content_part.added", "part": {"type": "output_text", "text": ""}}),
            "data: " + json.dumps({"type": "response.output_text.delta", "delta": "one"}),
        ])
        + "\n\n",
        close=False,
    )

    async def later() -> None:
        await asyncio.sleep(0.01)
        body.enqueue("data: " + json.dumps({"type": "response.output_text.delta", "delta": "two"}) + "\n\n")
        await asyncio.sleep(0.01)
        if body.cancelled:
            return
        body.enqueue(
            "\n\n".join([
                "data: "
                + json.dumps({
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "id": "msg_1",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "onetwo"}],
                    },
                }),
                "data: "
                + json.dumps({
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8, "input_tokens_details": {"cached_tokens": 0}},
                    },
                }),
            ])
            + "\n\n"
        )
        body.close()

    timer = asyncio.ensure_future(later())
    fetchMock.impl = lambda _request: sseResponse(body)
    controller = AbortController()
    events: list[str] = []

    resultStream = streamOpenAICodexResponses(
        codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse", "signal": controller.signal}
    )
    async for event in resultStream:
        events.append(f"text_delta:{event.delta}" if event.type == "text_delta" else event.type)
        if event.type == "text_delta" and event.delta == "one":
            controller.abort()

    result = await resultStream.result()
    timer.cancel()
    assert result.stopReason == "aborted"
    assert result.errorMessage == "Request was aborted"
    assert "text_delta:one" in events
    assert "text_delta:two" not in events
    assert body.cancelled is True


async def test_sets_session_id_x_client_request_id_headers_and_prompt_cache_key_when_session_id_is_provided(fetchMock):
    sessionId = "test-session-123"

    def impl(request: httpx.Request) -> httpx.Response:
        # Verify sessionId is set in headers
        assert request.headers["session-id"] == sessionId
        assert "session_id" not in request.headers
        assert request.headers["x-client-request-id"] == sessionId
        # Verify sessionId is set in request body as prompt_cache_key
        body = decodeCodexRequestBody(request)
        if body is not None:
            assert body["prompt_cache_key"] == sessionId
        return sseResponse(buildSSEPayload("completed"))

    fetchMock.impl = impl

    result = await streamOpenAICodexResponses(
        codexModel(), helloContext(), {"apiKey": mockToken(), "sessionId": sessionId, "transport": "sse"}
    ).result()

    assert result.stopReason == "stop", result.errorMessage


async def test_omits_sse_cache_affinity_when_cache_retention_is_none(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))

    await streamOpenAICodexResponses(
        codexModel(),
        helloContext(),
        {"apiKey": mockToken(), "cacheRetention": "none", "sessionId": "one-off-summary", "transport": "sse"},
    ).result()

    [request] = fetchMock.calls
    assert "session-id" not in request.headers
    assert "x-client-request-id" not in request.headers
    body = decodeCodexRequestBody(request)
    if body is not None:
        assert "prompt_cache_key" not in body


async def test_clamps_prompt_cache_key_to_openais_64_character_limit(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))
    captured: list[Any] = []

    await streamOpenAICodexResponses(
        codexModel(),
        helloContext(),
        {
            "apiKey": mockToken(),
            "transport": "sse",
            "sessionId": "x" * 67,
            "onPayload": lambda payload, _model: captured.append(payload),
        },
    ).result()

    assert captured[0]["prompt_cache_key"] == "x" * 64


async def test_clamps_codex_session_id_header_to_64_characters(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))

    await streamOpenAICodexResponses(
        codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse", "sessionId": "x" * 67}
    ).result()

    [request] = fetchMock.calls
    assert request.headers["session-id"] == "x" * 64
    assert request.headers["x-client-request-id"] == "x" * 64


async def test_lets_model_and_caller_headers_override_originator_and_user_agent(fetchMock):
    token = mockToken()
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))

    await streamOpenAICodexResponses(
        codexModel(headers={"originator": "my-app"}),
        helloContext(),
        {"apiKey": token, "transport": "sse", "headers": {"user-agent": "my-app/1.0", "Authorization": "Bearer ignored"}},
    ).result()

    [request] = fetchMock.calls
    assert request.headers["originator"] == "my-app"
    assert request.headers["User-Agent"] == "my-app/1.0"
    assert request.headers["Authorization"] == f"Bearer {token}"


async def test_preserves_gpt_5_5_xhigh_reasoning_effort_from_simple_options(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))
    captured: list[Any] = []

    await streamSimpleOpenAICodexResponses(
        codexModel(id="gpt-5.5", name="GPT-5.5", thinkingLevelMap={"xhigh": "xhigh"}),
        helloContext(),
        {"apiKey": mockToken(), "reasoning": "xhigh", "transport": "sse", "onPayload": lambda payload, _m: captured.append(payload)},
    ).result()

    assert captured[0]["reasoning"] == {"effort": "xhigh", "summary": "auto"}


async def test_forwards_required_tool_choice(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))
    captured: list[Any] = []

    await streamOpenAICodexResponses(
        codexModel(id="gpt-5.5", name="GPT-5.5"),
        normalizeContext(
            Context(
                messages=[UserMessage(content="Do not call ping. Respond with text instead.", timestamp=time.time_ns() // 1_000_000)],
                tools=[
                    Tool(
                        name="ping",
                        description="Ping",
                        parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
                    )
                ],
            )
        ),
        {"apiKey": mockToken(), "transport": "sse", "toolChoice": "required", "onPayload": lambda payload, _m: captured.append(payload)},
    ).result()

    assert captured[0]["tool_choice"] == "required"


async def test_sets_codex_strict_mode_explicitly_and_honors_constrained_sampling(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))
    captured: list[Any] = []

    await streamOpenAICodexResponses(
        codexModel(id="gpt-5.5", name="GPT-5.5"),
        normalizeContext(
            Context(
                messages=[UserMessage(content="Use a tool", timestamp=time.time_ns() // 1_000_000)],
                tools=[
                    Tool(
                        name="optional",
                        description="Optional constrained sampling",
                        parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
                        constrainedSampling=False,
                    ),
                    Tool(
                        name="strict",
                        description="Strict constrained sampling",
                        parameters={
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                            "additionalProperties": False,
                        },
                        constrainedSampling={"type": "json_schema", "strict": "prefer"},
                    ),
                ],
            )
        ),
        {"apiKey": mockToken(), "transport": "sse", "onPayload": lambda payload, _m: captured.append(payload)},
    ).result()

    assert [(tool["type"], tool["name"], tool["strict"]) for tool in captured[0]["tools"]] == [
        ("function", "optional", None),
        ("function", "strict", True),
    ]


@pytest.mark.parametrize("modelId", ["gpt-5.3-codex", "gpt-5.4", "gpt-5.5"])
async def test_clamps_minimal_reasoning_effort_to_low(fetchMock, modelId):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))
    captured: list[Any] = []

    await streamOpenAICodexResponses(
        codexModel(id=modelId, name=modelId, thinkingLevelMap={"minimal": "low"}),
        helloContext(),
        {"apiKey": mockToken(), "reasoningEffort": "minimal", "transport": "sse", "onPayload": lambda payload, _m: captured.append(payload)},
    ).result()

    assert captured[0]["reasoning"] == {"effort": "low", "summary": "auto"}


@pytest.mark.parametrize(
    ("modelId", "serviceTier", "multiplier"),
    [("gpt-5.1-codex", "flex", 0.5), ("gpt-5.1-codex", "priority", 2), ("gpt-5.5", "flex", 0.5), ("gpt-5.5", "priority", 2.5)],
)
async def test_uses_the_client_sent_service_tier_when_codex_echoes_default(fetchMock, modelId, serviceTier, multiplier):
    sse = buildSSEPayload("completed").replace(
        '"usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8, "input_tokens_details": {"cached_tokens": 0}}',
        '"service_tier": "default", "usage": {"input_tokens": 1000000, "output_tokens": 1000000, "total_tokens": 2000000, "input_tokens_details": {"cached_tokens": 0}}',
    )
    fetchMock.impl = lambda _request: sseResponse(sse)

    result = await streamOpenAICodexResponses(
        codexModel(
            id=modelId,
            name="GPT-5.5" if modelId == "gpt-5.5" else "GPT-5.1 Codex",
            cost={"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0},
        ),
        helloContext(),
        {"apiKey": mockToken(), "serviceTier": serviceTier, "transport": "sse"},
    ).result()

    assert result.usage.cost.input == 1 * multiplier
    assert result.usage.cost.output == 2 * multiplier
    assert result.usage.cost.total == 3 * multiplier


async def test_does_not_set_session_headers_when_session_id_is_not_provided(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))

    # No sessionId provided
    await streamOpenAICodexResponses(codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse"}).result()

    [request] = fetchMock.calls
    assert "session-id" not in request.headers
    assert "session_id" not in request.headers
    assert "x-client-request-id" not in request.headers


# --- WebSocket ----------------------------------------------------------------------------------


async def test_forwards_auto_transport_and_raw_provider_events_from_stream_simple(fetchMock, monkeypatch):
    sentBodies: list[Any] = []

    def onSend(socket: MockWebSocket, body: Any) -> None:
        sentBodies.append(body)
        socket.dispatchEvents([
            {
                "type": "response.output_item.added",
                "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "in_progress", "content": []},
            },
            {"type": "response.content_part.added", "part": {"type": "output_text", "text": ""}},
            {"type": "response.output_text.delta", "delta": "Hello"},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Hello"}],
                },
            },
            {
                "type": "response.done",
                "response": {
                    "status": "completed",
                    "end_turn": False,
                    "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8, "input_tokens_details": {"cached_tokens": 0}},
                },
            },
        ])

    sockets = stubWebSocket(monkeypatch, onSend, state=None)
    model = codexModel()
    providerEvents: list[Any] = []
    providerEventModels: list[Model] = []

    result = await streamSimpleOpenAICodexResponses(
        model,
        helloContext(timestamp=1),
        {
            "apiKey": mockToken(),
            "sessionId": "session-auto",
            "transport": "auto",
            "onProviderStreamEvent": lambda event, eventModel: (providerEvents.append(event), providerEventModels.append(eventModel)),
        },
    ).result()

    assert result.endTurn is False
    assert len(sentBodies) == 1
    assert [event["type"] for event in providerEvents] == [
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_item.done",
        "response.done",
    ]
    assert providerEventModels == [model] * 5
    capturedWebSocketHeaders = sockets[0].headers
    assert capturedWebSocketHeaders["session-id"] == "session-auto"
    assert "session_id" not in capturedWebSocketHeaders
    assert capturedWebSocketHeaders["x-client-request-id"] == "session-auto"
    assert fetchMock.calls == []
    stats = getOpenAICodexWebSocketDebugStats("session-auto")
    assert (stats.cachedContextRequests, stats.fullContextRequests) == (1, 1)


async def test_scopes_cached_websockets_to_the_authenticated_account(fetchMock, monkeypatch):
    # Regression for #7284: rotating accounts must not reuse a socket authenticated by another account.
    responseId = 0

    def onSend(socket: MockWebSocket, _body: Any) -> None:
        nonlocal responseId
        responseId += 1
        socket.dispatchEvents([_completedEvent(f"resp_{responseId}")])

    sockets = stubWebSocket(monkeypatch, onSend)
    model = codexModel()
    context = normalizeContext(Context(systemPrompt="", messages=[]))
    options = {"sessionId": "shared-session", "transport": "websocket-cached"}

    await streamOpenAICodexResponses(model, context, {**options, "apiKey": mockToken("account-a")}).result()
    await streamOpenAICodexResponses(model, context, {**options, "apiKey": mockToken("account-b")}).result()
    await streamOpenAICodexResponses(model, context, {**options, "apiKey": mockToken("account-a")}).result()

    assert [socket.headers["chatgpt-account-id"] for socket in sockets] == ["account-a", "account-b"]
    assert [socket.headers["authorization"] for socket in sockets] == [
        f"Bearer {mockToken('account-a')}",
        f"Bearer {mockToken('account-b')}",
    ]
    assert fetchMock.calls == []
    stats = getOpenAICodexWebSocketDebugStats("shared-session")
    assert (stats.connectionsCreated, stats.connectionsReused) == (2, 1)


async def test_closes_one_shot_websockets_when_cache_retention_is_none(fetchMock, monkeypatch):
    sentBodies: list[Any] = []

    def onSend(socket: MockWebSocket, body: Any) -> None:
        sentBodies.append(body)
        socket.dispatchEvents([_completedEvent(f"resp_{len(sockets)}", usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8})])

    sockets = stubWebSocket(monkeypatch, onSend, state=None)
    options = {"apiKey": mockToken(), "cacheRetention": "none", "sessionId": "one-off-summary", "transport": "auto"}

    await streamOpenAICodexResponses(codexModel(), helloContext(timestamp=1), options).result()
    await streamOpenAICodexResponses(codexModel(), helloContext(timestamp=1), options).result()

    assert len(sockets) == 2
    assert sum(socket.closed for socket in sockets) == 2
    assert len(sentBodies) == 2
    assert all("prompt_cache_key" not in body for body in sentBodies)
    assert getOpenAICodexWebSocketDebugStats("one-off-summary") is None
    assert fetchMock.calls == []


async def test_falls_back_to_sse_when_websocket_connect_does_not_open_before_the_connect_timeout(fetchMock, monkeypatch):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))

    async def neverOpens(_url: str, *, open_timeout: float | None = None, **_kwargs: Any) -> Any:
        # What websockets does when the handshake outlasts `open_timeout`.
        await asyncio.sleep(open_timeout)
        raise TimeoutError

    monkeypatch.setattr(codex, "_cachedWebsocket", neverOpens)

    result = await streamOpenAICodexResponses(
        codexModel(),
        helloContext(timestamp=1),
        {
            "apiKey": mockToken(),
            "sessionId": "ws-connect-timeout",
            "transport": "auto",
            "timeoutMs": 300_000,
            "websocketConnectTimeoutMs": 50,
        },
    ).result()

    assert _text(result) == "Hello"
    assert len(fetchMock.calls) == 1
    stats = getOpenAICodexWebSocketDebugStats("ws-connect-timeout")
    assert (stats.websocketFailures, stats.sseFallbacks, stats.websocketFallbackActive, stats.lastWebSocketError) == (
        1,
        1,
        True,
        "WebSocket connect timeout after 50ms",
    )


async def test_reconnects_once_when_the_websocket_connection_limit_is_reached_before_output_starts(fetchMock, monkeypatch):
    def onSend(socket: MockWebSocket, _body: Any) -> None:
        limitReached = socket is sockets[0]
        socket.dispatchEvents([
            {"type": "error", "error": {"code": "websocket_connection_limit_reached"}}
            if limitReached
            else _completedEvent("resp_1", usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8})
        ])

    sockets = stubWebSocket(monkeypatch, onSend, state=None)

    result = await streamOpenAICodexResponses(
        codexModel(), normalizeContext(Context(systemPrompt="", messages=[])), {"apiKey": mockToken()}
    ).result()

    assert result.stopReason == "stop"
    assert len(sockets) == 2
    assert fetchMock.calls == []


async def test_falls_back_to_sse_when_a_websocket_is_idle_before_the_first_event(fetchMock, monkeypatch):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))
    sentBodies: list[Any] = []
    stubWebSocket(monkeypatch, lambda _socket, body: sentBodies.append(body))

    result = await streamOpenAICodexResponses(
        codexModel(),
        helloContext(timestamp=1),
        {"apiKey": mockToken(), "sessionId": "ws-idle-before-start", "transport": "auto", "timeoutMs": 50},
    ).result()

    assert len(sentBodies) == 1
    assert _text(result) == "Hello"
    assert len(fetchMock.calls) == 1
    stats = getOpenAICodexWebSocketDebugStats("ws-idle-before-start")
    assert (stats.websocketFailures, stats.sseFallbacks, stats.websocketFallbackActive) == (1, 1, True)


async def test_errors_when_a_websocket_is_idle_after_the_stream_started(fetchMock, monkeypatch):
    stubWebSocket(
        monkeypatch,
        lambda socket, _body: socket.dispatchEvents([
            {
                "type": "response.output_item.added",
                "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "in_progress", "content": []},
            }
        ]),
    )

    result = await streamOpenAICodexResponses(
        codexModel(), helloContext(timestamp=1), {"apiKey": mockToken(), "transport": "auto", "timeoutMs": 50}
    ).result()

    assert result.stopReason == "error"
    assert result.errorMessage == "WebSocket idle timeout after 50ms"
    assert fetchMock.calls == []


async def test_opens_a_fresh_cached_websocket_before_the_backend_connection_age_limit(fetchMock, monkeypatch):
    sentConnectionIds: list[int] = []

    def onSend(socket: MockWebSocket, _body: Any) -> None:
        connectionId = sockets.index(socket) + 1
        sentConnectionIds.append(connectionId)
        socket.dispatchEvents([_completedEvent(f"resp_{connectionId}", usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8})])

    sockets = stubWebSocket(monkeypatch, onSend)
    model = codexModel()
    sessionId = "aged-ws-session"
    firstContext = helloContext(timestamp=1)

    first = await streamOpenAICodexResponses(
        model, firstContext, {"apiKey": mockToken(), "sessionId": sessionId, "transport": "websocket-cached"}
    ).result()
    # `vi.setSystemTime(startedAt + 56 min)`: the cached socket's creation time moves back instead.
    for entry in codex._websocketSessionCache[sessionId].values():
        entry.createdAt -= 56 * 60 * 1000
    secondContext = normalizeContext(
        Context(messages=[*firstContext.messages, first, UserMessage(content="Now finish", timestamp=2)])
    )

    await streamOpenAICodexResponses(
        model, secondContext, {"apiKey": mockToken(), "sessionId": sessionId, "transport": "websocket-cached"}
    ).result()

    assert len(sockets) == 2
    assert sentConnectionIds == [1, 2]
    stats = getOpenAICodexWebSocketDebugStats(sessionId)
    assert (stats.connectionsCreated, stats.connectionsReused) == (2, 0)


async def test_sends_only_response_input_deltas_in_websocket_cached_mode(fetchMock, monkeypatch):
    sentBodies: list[Any] = []

    def onSend(socket: MockWebSocket, body: Any) -> None:
        sentBodies.append(body)
        responseId = f"resp_{len(sentBodies)}"
        outputEvents = (
            [
                {
                    "type": "response.output_item.added",
                    "item": {"type": "custom_tool_call", "id": "ctc_1", "call_id": "call_1", "name": "sample_tool", "input": ""},
                },
                {"type": "response.custom_tool_call_input.delta", "item_id": "ctc_1", "delta": "abc"},
                {"type": "response.custom_tool_call_input.done", "item_id": "ctc_1", "input": "abc"},
                {
                    "type": "response.output_item.done",
                    "item": {"type": "custom_tool_call", "id": "ctc_1", "call_id": "call_1", "name": "sample_tool", "input": "abc"},
                },
            ]
            if len(sentBodies) == 1
            else []
        )
        socket.dispatchEvents([
            {"type": "response.created", "response": {"id": responseId}},
            *outputEvents,
            {
                "type": "response.completed",
                "response": {
                    "id": responseId,
                    "status": "completed",
                    "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8, "input_tokens_details": {"cached_tokens": 0}},
                },
            },
        ])

    stubWebSocket(monkeypatch, onSend)
    model = codexModel(compat={"supportsOpenAIGrammarTools": True})
    sampleTool = Tool(
        name="sample_tool",
        description="Sample tool",
        parameters={"type": "object", "properties": {"payload": {"type": "string"}}, "required": ["payload"]},
        constrainedSampling={"type": "grammar", "variants": {"openai_lark": "start: /[a-z]+/"}},
    )
    firstContext = Context(
        systemPrompt="You are a helpful assistant.",
        messages=[UserMessage(content="Use the tool", timestamp=1)],
        tools=[sampleTool],
    )

    first = await streamOpenAICodexResponses(
        model, normalizeContext(firstContext), {"apiKey": mockToken(), "sessionId": "session-1", "transport": "websocket-cached"}
    ).result()

    secondContext = normalizeContext(
        Context(
            systemPrompt=firstContext.systemPrompt,
            tools=firstContext.tools,
            messages=[
                *firstContext.messages,
                first,
                ToolResultMessage(
                    toolCallId="call_1|ctc_1",
                    toolName="sample_tool",
                    content=[TextContent(text="real result")],
                    isError=False,
                    timestamp=2,
                ),
                UserMessage(content="Now finish", timestamp=3),
            ],
        )
    )
    await streamOpenAICodexResponses(
        model, secondContext, {"apiKey": mockToken(), "sessionId": "session-1", "transport": "websocket-cached"}
    ).result()

    assert len(sentBodies) == 2
    firstBody, secondBody = sentBodies
    assert firstBody["store"] is False
    assert "previous_response_id" not in firstBody
    assert firstBody["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "Use the tool"}]}]
    assert secondBody["store"] is False
    assert secondBody["previous_response_id"] == "resp_1"
    assert secondBody["input"] == [
        {"type": "custom_tool_call_output", "call_id": "call_1", "output": "real result"},
        {"role": "user", "content": [{"type": "input_text", "text": "Now finish"}]},
    ]
    stats = getOpenAICodexWebSocketDebugStats("session-1")
    assert (
        stats.requests,
        stats.connectionsCreated,
        stats.connectionsReused,
        stats.cachedContextRequests,
        stats.storeTrueRequests,
        stats.fullContextRequests,
        stats.deltaRequests,
        stats.lastDeltaInputItems,
        stats.lastPreviousResponseId,
    ) == (2, 1, 1, 2, 0, 1, 1, 2, "resp_1")


@pytest.mark.parametrize("recoveryTransport", ["websocket", "sse"])
async def test_recovers_a_missing_cached_websocket_continuation(fetchMock, monkeypatch, recoveryTransport):
    sessionId = f"missing-continuation-{recoveryTransport}"
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))
    sentBodies: list[dict[str, Any]] = []

    def onSend(socket: MockWebSocket, body: Any) -> None:
        sentBodies.append({**body, "connectionId": sockets.index(socket) + 1})
        if len(sentBodies) == 2:
            socket.dispatchEvents([
                {
                    "type": "codex.rate_limits",
                    "plan_type": "plus",
                    "rate_limits": {
                        "allowed": True,
                        "limit_reached": False,
                        "primary": {
                            "used_percent": 7,
                            "window_minutes": 10080,
                            "reset_after_seconds": 556112,
                            "reset_at": 1785269351,
                        },
                        "secondary": None,
                    },
                    "code_review_rate_limits": None,
                    "additional_rate_limits": None,
                    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
                    "promo": None,
                },
                {
                    "type": "error",
                    "status": 400,
                    "error": {
                        "code": "previous_response_not_found",
                        "message": "Previous response with id 'resp_1' not found.",
                        "param": "previous_response_id",
                    },
                },
            ])
            return
        if len(sentBodies) == 3 and recoveryTransport == "sse":
            socket.dispatchEvents([RuntimeError("retry websocket failed")])
            return

        response = (
            {"responseId": "resp_1", "messageId": "msg_1", "text": "Hello"}
            if len(sentBodies) == 1
            else {"responseId": "resp_2", "messageId": "msg_2", "text": "Recovered"}
        )
        socket.dispatchEvents([
            {"type": "response.created", "response": {"id": response["responseId"]}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "message", "id": response["messageId"], "role": "assistant", "status": "in_progress", "content": []},
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "message",
                    "id": response["messageId"],
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": response["text"]}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": response["responseId"],
                    "status": "completed",
                    "usage": {"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                },
            },
        ])

    sockets = stubWebSocket(monkeypatch, onSend)
    model = codexModel()
    firstContext = helloContext(timestamp=1)

    first = await streamOpenAICodexResponses(
        model, firstContext, {"apiKey": mockToken(), "sessionId": sessionId, "transport": "websocket-cached"}
    ).result()
    secondContext = normalizeContext(
        Context(messages=[*firstContext.messages, first, UserMessage(content="Now finish", timestamp=2)])
    )
    eventTypes: list[str] = []
    secondStream = streamOpenAICodexResponses(
        model, secondContext, {"apiKey": mockToken(), "sessionId": sessionId, "transport": "websocket-cached"}
    )
    async for event in secondStream:
        eventTypes.append(event.type)
    second = await secondStream.result()

    assert second.stopReason == "stop", second.errorMessage
    assert _text(second) == ("Hello" if recoveryTransport == "sse" else "Recovered")
    assert eventTypes.count("start") == 1
    assert "error" not in eventTypes
    assert len(sockets) == 2
    assert len(sentBodies) == 3
    assert [body["connectionId"] for body in sentBodies] == [1, 1, 2]
    assert sentBodies[1]["previous_response_id"] == "resp_1"
    assert sentBodies[1]["input"] == [{"role": "user", "content": [{"type": "input_text", "text": "Now finish"}]}]
    assert "previous_response_id" not in sentBodies[2]
    assert len(sentBodies[2]["input"]) == 3
    assert sentBodies[2]["input"][-1] == {"role": "user", "content": [{"type": "input_text", "text": "Now finish"}]}
    assert len(fetchMock.calls) == (1 if recoveryTransport == "sse" else 0)
    stats = getOpenAICodexWebSocketDebugStats(sessionId)
    assert (
        stats.requests,
        stats.connectionsCreated,
        stats.connectionsReused,
        stats.fullContextRequests,
        stats.deltaRequests,
        stats.websocketFailures,
        stats.sseFallbacks,
    ) == (3, 2, 1, 2, 1, 1 if recoveryTransport == "sse" else 0, 1 if recoveryTransport == "sse" else 0)


# --- SSE retries --------------------------------------------------------------------------------


@pytest.fixture
def recordedSleeps(monkeypatch):
    """``vi.spyOn(globalThis, "setTimeout")`` over the retry sleeps; they return at once."""
    delays: list[float] = []

    async def sleep(ms: float, _signal: Any = None) -> None:
        delays.append(ms)

    monkeypatch.setattr(codex, "_sleep", sleep)
    return delays


_FIXED_NOW = datetime(2026, 5, 13, tzinfo=UTC).timestamp()


@pytest.mark.parametrize(
    ("name", "makeHeaders", "expectedDelay"),
    [
        ("retry-after-ms", lambda: {"content-type": "application/json", "retry-after-ms": "1500"}, 1500),
        ("retry-after seconds", lambda: {"content-type": "application/json", "retry-after": "60"}, 60_000),
        (
            "retry-after HTTP date",
            lambda: {
                "content-type": "application/json",
                "retry-after": format_datetime(datetime.fromtimestamp(_FIXED_NOW + 45, UTC), usegmt=True),
            },
            45_000,
        ),
    ],
)
async def test_uses_the_retry_header_for_sse_retries(fetchMock, recordedSleeps, monkeypatch, name, makeHeaders, expectedDelay):
    # `vi.setSystemTime(...)`, for the adapter's own clock only.
    monkeypatch.setattr(codex, "time", SimpleNamespace(time=lambda: _FIXED_NOW, time_ns=time.time_ns, monotonic=time.monotonic))
    codexRequests = 0

    def impl(_request: httpx.Request) -> httpx.Response:
        nonlocal codexRequests
        codexRequests += 1
        if codexRequests == 1:
            return httpx.Response(
                429, headers=makeHeaders(), content=json.dumps({"error": {"code": "rate_limit_exceeded", "message": "rate limited"}})
            )
        return sseResponse(buildSSEPayload("completed"))

    fetchMock.impl = impl

    result = await streamOpenAICodexResponses(
        codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse", "maxRetries": 1}
    ).result()

    assert recordedSleeps == [expectedDelay]
    assert _text(result) == "Hello"
    assert codexRequests == 2


@pytest.mark.parametrize("status", [429, 503])
async def test_fails_immediately_when_a_retry_delay_exceeds_the_limit(fetchMock, status):
    fetchMock.impl = lambda _request: httpx.Response(
        status,
        headers={"content-type": "application/json", "retry-after": "2"},
        content=json.dumps({"error": {"code": "temporarily_unavailable", "message": "retry later"}}),
    )

    result = await streamOpenAICodexResponses(
        codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse", "maxRetries": 3, "maxRetryDelayMs": 1000}
    ).result()

    assert result.stopReason == "error"
    assert result.errorMessage == "Server requested 2s retry delay (max: 1s)"
    assert len(fetchMock.calls) == 1


@pytest.mark.skipif(not _HAS_ZSTD, reason="no zstd codec installed (compression.zstd or zstandard)")
async def test_zstd_compresses_sse_request_bodies(fetchMock):
    fetchMock.impl = lambda _request: sseResponse(buildSSEPayload("completed"))
    largeText = "compress me " * 400

    await streamOpenAICodexResponses(
        codexModel(),
        normalizeContext(
            Context(systemPrompt="You are a helpful assistant.", messages=[UserMessage(content=largeText, timestamp=1)])
        ),
        {"apiKey": mockToken(), "transport": "sse"},
    ).result()

    request = fetchMock.calls[-1]
    assert request.headers["content-encoding"] == "zstd"
    decoded = decodeCodexRequestBody(request)
    assert decoded["input"][0]["content"][0]["text"] == largeText

    await streamOpenAICodexResponses(
        codexModel(),
        normalizeContext(Context(systemPrompt="You are a helpful assistant.", messages=[UserMessage(content="hi", timestamp=1)])),
        {"apiKey": mockToken(), "transport": "sse"},
    ).result()

    assert fetchMock.calls[-1].headers["content-encoding"] == "zstd"


async def test_uses_exponential_backoff_across_repeated_sse_retries_without_retry_headers(fetchMock, recordedSleeps):
    codexRequests = 0

    def impl(_request: httpx.Request) -> httpx.Response:
        nonlocal codexRequests
        codexRequests += 1
        if codexRequests <= 3:
            return httpx.Response(
                429,
                headers={"content-type": "application/json"},
                content=json.dumps({"error": {"code": "rate_limit_exceeded", "message": "rate limited"}}),
            )
        return sseResponse(buildSSEPayload("completed"))

    fetchMock.impl = impl

    result = await streamOpenAICodexResponses(
        codexModel(), helloContext(), {"apiKey": mockToken(), "transport": "sse", "maxRetries": 3}
    ).result()

    assert recordedSleeps == [1000, 2000, 4000]
    assert _text(result) == "Hello"
    assert codexRequests == 4
