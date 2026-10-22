"""Translated from pi's ``packages/ai/test/anthropic-strict-tool-fallback.test.ts`` (v1.3.0).

MISAKA fork: Python has no ``fetch`` override. The Anthropic requests go to a local HTTP server (``captureServer``), so
the real SDK parses the 400 into its ``APIStatusError``; the pi-messages gateway is an ``httpx.MockTransport``
(``httpTransport``) whose handler runs the Anthropic request. A ``TypeBox`` schema is its JSON schema. The abort the
upstream fetch fires inline is scheduled onto the event loop from the server's thread.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any, Self

import httpx
import pytest
from anthropic import BadRequestError

from misaka.ai.api.anthropic_messages import stream as streamAnthropic
from misaka.ai.api.pi_messages import stream as streamPiMessages
from misaka.ai.types import (
    Model,
    ModelCost,
    SystemMessage,
    Tool,
    TranscriptContext,
    UserMessage,
)
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import AbortController
from tests.http_capture import CapturedRequest, Reply, captureServer

grammarError = (
    "The compiled grammar is too large, which would cause performance issues. Simplify your tool schemas or reduce "
    "the number of strict tools."
)


def _model(baseUrl: str = "https://anthropic.example", **overrides: Any) -> Model:
    return Model(
        **{
            "id": "claude-haiku-4-5",
            "name": "Test Claude",
            "api": "anthropic-messages",
            "provider": "anthropic",
            "baseUrl": baseUrl,
            "reasoning": False,
            "input": ["text"],
            "cost": ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
            "contextWindow": 200000,
            "maxTokens": 100,
            "compat": {"supportsStrictTools": True},
            **overrides,
        }
    )


# `Type.Object({ value: Type.String(), optional: Type.Optional(Type.Number()) })`.
_PARAMETERS = {
    "type": "object",
    "required": ["value"],
    "properties": {"value": {"type": "string"}, "optional": {"type": "number"}},
}


def tool(name: str, strict: str | None = None, parameters: dict[str, Any] | None = None) -> Tool:
    return Tool(
        name=name,
        description="Look up a value",
        parameters=deepcopy(parameters or _PARAMETERS),
        **({"constrainedSampling": {"type": "json_schema", "strict": strict}} if strict else {}),
    )


def context(tools: list[Tool]) -> TranscriptContext:
    return normalizeContext({"tools": tools, "messages": [UserMessage(content="Hello", timestamp=1)]})


def errorResponse(message: str = grammarError, status: int = 400, type: str = "invalid_request_error") -> Reply:
    return Reply(
        status=status,
        headers={"content-type": "application/json"},
        body=json.dumps({"type": "error", "error": {"type": type, "message": message}}).encode(),
    )


def successResponse() -> Reply:
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_test", "model": "claude-haiku-4-5", "usage": {"input_tokens": 10, "output_tokens": 0}},
        },
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return Reply(body="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode())


class Harness:
    """``harness(respond)``: a server whose replies come from ``respond(payload, attempt)``, the payloads it saw, and
    the options that point the request at it."""

    def __init__(self, respond: Any) -> None:
        self.payloads: list[dict[str, Any]] = []
        self._respond = respond
        self._server = captureServer(self._reply)

    def _reply(self, request: CapturedRequest) -> Reply:
        self.payloads.append(request.body)
        return self._respond(request.body, len(self.payloads))

    def __enter__(self) -> Self:
        self.baseUrl = self._server.__enter__().baseUrl
        self.model = _model(self.baseUrl)
        self.options: dict[str, Any] = {"apiKey": "test-key", "maxRetries": 0}
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.__exit__(*exc)


async def _events(stream: Any) -> list[str]:
    return [event.type async for event in stream]


def _tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return payload["tools"]


# Regression tests for https://github.com/earendil-works/pi/issues/10409.
@pytest.mark.parametrize(
    "message",
    [
        grammarError,
        # Observed from the live API: 32 strict tools, then 20 tools with 24 optional fields each.
        (
            "Too many strict tools (32). The maximum number of strict tools supported is 20. Try reducing the number "
            "of tools marked as strict."
        ),
        (
            "Schemas contains too many parameters with union types (480 parameters with type arrays or anyOf). This "
            "causes exponential compilation cost. Reduce the number of nullable or union-typed parameters (limit: 16 "
            "parameters with unions)."
        ),
    ],
)
async def test_retries_with_original_non_strict_schemas_and_preserves_required_tools(message):
    preferred = tool("preferred", "prefer")
    required = tool("required", "require")
    ordinary = tool("ordinary")
    transcript = context([preferred, required, ordinary])
    original = transcript.model_dump()
    onPayloadCalls: list[Any] = []
    onResponseCalls: list[Any] = []

    def onPayload(payload: Any, _model: Any) -> Any:
        onPayloadCalls.append(payload)
        return {**payload, "inference_geo": "us", "stream": False}

    # Use the real SDK's HTTP error parsing, not a synthetic thrown Error.
    with Harness(lambda _payload, attempt: errorResponse(message) if attempt == 1 else successResponse()) as h:
        stream = streamAnthropic(
            h.model,
            transcript,
            {
                **h.options,
                "onPayload": onPayload,
                "onResponse": lambda response, _model: onResponseCalls.append(response),
                "cacheRetention": "long",
            },
        )
        events = await _events(stream)
        result = await stream.result()

    assert result.stopReason == "stop", result.errorMessage
    assert events == ["start", "done"]
    assert len(h.payloads) == 2
    assert len(onPayloadCalls) == 2
    assert len(onResponseCalls) == 1
    first, second = _tools(h.payloads[0]), _tools(h.payloads[1])
    assert first[0]["strict"] is True
    assert first[0]["input_schema"]["required"] == ["value", "optional"]
    assert first[0]["input_schema"]["properties"]["optional"] == {"anyOf": [{"type": "number"}, {"type": "null"}]}
    assert "strict" not in second[0]
    assert second[0]["input_schema"] == {
        "type": "object",
        "properties": {"value": {"type": "string"}, "optional": {"type": "number"}},
        "required": ["value"],
    }
    assert second[1] == first[1]
    assert second[1]["strict"] is True
    assert second[2] == first[2]
    assert second[2]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert [payload["stream"] for payload in h.payloads] == [True, True]
    assert h.payloads[1]["inference_geo"] == "us"
    assert transcript.model_dump() == original
    assert result.diagnostics[0].type == "anthropic_strict_tools_fallback"
    assert result.diagnostics[0].details == {"reason": "strict_tool_limits_exceeded"}


async def test_downgrades_inline_additions_and_same_name_redefinitions_without_downgrading_require():
    transcript = context([tool("lookup", "require")])
    transcript.messages.extend(
        [
            SystemMessage(content="", toolsAdded=[tool("lookup", "prefer")], timestamp=2),
            SystemMessage(content="", toolsAdded=[tool("lookup", "require")], timestamp=3),
        ]
    )
    with Harness(lambda _payload, attempt: errorResponse() if attempt == 1 else successResponse()) as h:
        result = await streamAnthropic(
            h.model.model_copy(
                update={
                    "compat": h.model.compat.model_copy(
                        update={"supportsMidConvoSystemMessages": True, "supportsMidConvoToolChanges": True}
                    )
                }
            ),
            transcript,
            {**h.options, "apiKey": "sk-ant-oat-test"},
        ).result()

    assert result.stopReason == "stop", result.errorMessage
    assert len(h.payloads) == 2
    assert h.payloads[1]["tools"] == h.payloads[0]["tools"]
    additions = [
        [
            block["tool"]["definition"]
            for message in payload["messages"]
            if isinstance(message["content"], list)
            for block in message["content"]
            if block["type"] == "tool_addition" and block["tool"]["type"] == "tool_definition"
        ]
        for payload in h.payloads
    ]
    assert [definition.get("strict") for definition in additions[0]] == [True, True]
    assert [definition.get("strict") for definition in additions[1]] == [None, True]
    assert additions[1][0]["input_schema"]["required"] == ["value"]
    assert additions[1][1] == additions[0][1]


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param([tool("lookup", "require")], id="required"),
        pytest.param([tool("lookup")], id="ordinary"),
        pytest.param(
            [
                tool(
                    "lookup",
                    "prefer",
                    {"type": "object", "required": ["value"], "properties": {"value": {"type": "number", "minimum": 1}}},
                )
            ],
            id="locally unsupported prefer",
        ),
    ],
)
async def test_does_not_retry_a_tool_set(tools):
    with Harness(lambda _payload, _attempt: errorResponse()) as h:
        result = await streamAnthropic(h.model, context(tools), h.options).result()

    assert result.stopReason == "error"
    assert grammarError in result.errorMessage
    assert len(h.payloads) == 1


@pytest.mark.parametrize(
    "message,status,type",
    [
        pytest.param("Invalid tool schema", 400, "invalid_request_error", id="unrelated 400"),
        pytest.param(grammarError, 400, "authentication_error", id="wrong error type"),
        pytest.param(grammarError, 500, "invalid_request_error", id="wrong HTTP status"),
    ],
)
async def test_does_not_downgrade_on(message, status, type):
    with Harness(lambda _payload, _attempt: errorResponse(message, status, type)) as h:
        result = await streamAnthropic(h.model, context([tool("lookup", "prefer")]), h.options).result()

    assert result.stopReason == "error"
    assert len(h.payloads) == 1


async def test_surfaces_a_second_rejection_rather_than_retrying_again():
    with Harness(
        lambda _payload, attempt: errorResponse(grammarError if attempt == 1 else "Required grammar is still too large")
    ) as h:
        result = await streamAnthropic(
            h.model, context([tool("lookup", "prefer"), tool("required", "require")]), h.options
        ).result()

    assert result.stopReason == "error"
    assert "Required grammar is still too large" in result.errorMessage
    assert len(h.payloads) == 2
    assert _tools(h.payloads[1])[1]["strict"] is True


async def test_does_not_downgrade_a_hook_exception_even_if_it_looks_like_the_provider_rejection():
    def onPayload(_payload: Any, _model: Any) -> Any:
        raise BadRequestError(
            grammarError,
            response=httpx.Response(400, request=httpx.Request("POST", "https://anthropic.example/v1/messages")),
            body={"error": {"type": "invalid_request_error", "message": grammarError}},
        )

    with Harness(lambda _payload, _attempt: successResponse()) as h:
        result = await streamAnthropic(
            h.model, context([tool("lookup", "prefer")]), {**h.options, "onPayload": onPayload}
        ).result()

    assert result.stopReason == "error"
    assert len(h.payloads) == 0
    assert result.diagnostics is None


async def test_honors_cancellation_before_the_fallback_attempt():
    controller = AbortController()
    loop = asyncio.get_running_loop()

    def respond(_payload: Any, _attempt: int) -> Reply:
        # `controller.abort()` from inside the fetch; the server answers on its own thread.
        loop.call_soon_threadsafe(controller.abort)
        return errorResponse()

    with Harness(respond) as h:
        result = await streamAnthropic(
            h.model, context([tool("lookup", "prefer")]), {**h.options, "signal": controller.signal}
        ).result()

    assert result.stopReason == "aborted"
    assert len(h.payloads) == 1


async def test_does_not_retry_an_sse_error_after_response_headers():
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": grammarError}}
    with Harness(lambda _payload, _attempt: Reply(body=f"event: error\ndata: {json.dumps(error)}\n\n".encode())) as h:
        result = await streamAnthropic(h.model, context([tool("lookup", "prefer")]), h.options).result()

    assert result.stopReason == "error"
    assert len(h.payloads) == 1


async def test_does_not_disable_strict_sampling_on_later_requests():
    with Harness(lambda _payload, attempt: errorResponse() if attempt == 1 else successResponse()) as h:
        transcript = context([tool("lookup", "prefer")])
        await streamAnthropic(h.model, transcript, h.options).result()
        await streamAnthropic(h.model, transcript, h.options).result()

    assert [_tools(payload)[0].get("strict") for payload in h.payloads] == [True, None, True]


async def test_preserves_prefer_through_pi_messages_so_the_gateways_anthropic_adapter_can_fall_back():
    with Harness(lambda _payload, attempt: errorResponse() if attempt == 1 else successResponse()) as h:
        transcript = context([tool("lookup", "prefer")])
        gatewayModel = h.model.model_copy(
            update={"api": "pi-messages", "provider": "radius", "baseUrl": "https://radius.example/v1", "compat": None}
        )

        async def gateway(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            # The gateway receives the transcript as written, `strict: "prefer"` included.
            assert body["context"]["messages"][0]["toolsAdded"][0]["constrainedSampling"] == {
                "type": "json_schema",
                "strict": "prefer",
            }
            upstream = await streamAnthropic(
                h.model, TranscriptContext.model_validate(body["context"]), h.options
            ).result()
            event = {"type": "done", "reason": upstream.stopReason, "usage": upstream.usage.model_dump()}
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=f"data: {json.dumps(event)}\n\n".encode()
            )

        result = await streamPiMessages(
            gatewayModel, transcript, {"apiKey": "radius-test", "httpTransport": httpx.MockTransport(gateway)}
        ).result()

    assert result.stopReason == "stop", result.errorMessage
    assert [_tools(payload)[0].get("strict") for payload in h.payloads] == [True, None]
