"""Translated from pi's ``packages/ai/test/bedrock-error-metadata.test.ts``, ``bedrock-raw-stop-reason.test.ts``,
``bedrock-cache-write-1h-cost.test.ts``, ``bedrock-redacted-reasoning.test.ts`` and ``bedrock-response-headers.test.ts``
(v1.1.0).

MISAKA fork: pi mocks the AWS SDK; here boto3's client is replaced (``tests.bedrock_fake``), so what fails is what
boto3 raises -- a ``ClientError`` for a non-2xx response, an ``EventStreamError`` for an exception frame -- and the
response-headers case talks to a local HTTP server through real botocore.
"""

from __future__ import annotations

import base64
import time
from typing import Any

import pytest
from botocore.exceptions import ClientError, EventStreamError

from misaka.ai.api.bedrock_converse_stream import stream as streamBedrock
from misaka.ai.compat import getModel
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    ModelCost,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import AbortController
from tests.bedrock_fake import FakeBedrockClient, useFakeBedrock
from tests.http_capture import Reply, captureServer


def _now() -> int:
    return time.time_ns() // 1_000_000


@pytest.fixture(autouse=True)
def _cleanAwsEnv(monkeypatch):
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK", "AWS_BEDROCK_SKIP_AUTH"):
        monkeypatch.delenv(name, raising=False)


context = normalizeContext({"messages": [UserMessage(content="hello", timestamp=_now())]})

# --- bedrock-error-metadata.test.ts -------------------------------------------------------------

DIAGNOSTIC_TYPE = "bedrock_response_failure"
VALIDATION_MESSAGE = "The provided model identifier is invalid."
REQUEST_ID = "11111111-2222-3333-4444-555555555555"


def getModelFixture() -> Model:
    return getModel("amazon-bedrock", "us.anthropic.claude-opus-4-8")


def makeServiceException(name: str, status: int, requestId: str) -> ClientError:
    """What boto3 raises for a non-2xx response: the modeled code and message, and the response metadata."""
    return ClientError(
        {
            "Error": {"Code": name, "Message": VALIDATION_MESSAGE},
            "ResponseMetadata": {"HTTPStatusCode": status, "RequestId": requestId},
        },
        "ConverseStream",
    )


class _RejectingClient:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def converse_stream(self, **_request: Any) -> Any:
        raise self.error


def respondWithFailingStream(thrown: BaseException) -> FakeBedrockClient:
    """Fails after `messageStart`, raising from inside the event stream."""
    return FakeBedrockClient(
        {"messageStart": {"role": "assistant"}}, fail=thrown, metadata={"HTTPStatusCode": 200, "RequestId": REQUEST_ID}
    )


async def runBedrock(signal: Any = None) -> AssistantMessage:
    return await streamBedrock(getModelFixture(), context, {"cacheRetention": "none", "signal": signal}).result()


def findDiagnostic(message: AssistantMessage) -> Any:
    return next((d for d in message.diagnostics or [] if d.type == DIAGNOSTIC_TYPE), None)


async def test_records_status_error_code_and_request_id_for_a_non_2xx_from_client_send(monkeypatch):
    useFakeBedrock(monkeypatch, _RejectingClient(makeServiceException("ValidationException", 400, REQUEST_ID)))

    message = await runBedrock()
    diagnostic = findDiagnostic(message)

    assert message.stopReason == "error"
    assert diagnostic.details == {"status": 400, "errorCode": "ValidationException", "requestId": REQUEST_ID}
    assert diagnostic.error is None
    assert sorted(diagnostic.model_dump(exclude_none=True)) == ["details", "timestamp", "type"]


async def test_leaves_error_message_untouched_so_retry_classification_is_unaffected(monkeypatch):
    useFakeBedrock(monkeypatch, _RejectingClient(makeServiceException("ValidationException", 400, REQUEST_ID)))

    assert (await runBedrock()).errorMessage == f"Validation error: {VALIDATION_MESSAGE}"


async def test_reports_only_the_request_id_for_a_modeled_mid_stream_exception(monkeypatch):
    # The SDK throws a bare object literal here, so the code is genuinely unavailable.
    useFakeBedrock(
        monkeypatch,
        respondWithFailingStream(
            EventStreamError(
                {"Error": {"Code": "throttlingException", "Message": "Too many requests, please wait."}}, "ConverseStream"
            )
        ),
    )

    message = await runBedrock()

    assert message.stopReason == "error"
    assert findDiagnostic(message).details == {"requestId": REQUEST_ID}


async def test_captures_the_error_code_for_an_unmodeled_mid_stream_error(monkeypatch):
    # This branch throws a real `Error` named after the frame's `:error-code`.
    useFakeBedrock(
        monkeypatch,
        respondWithFailingStream(
            EventStreamError(
                {"Error": {"Code": "ModelStreamErrorException", "Message": "Model stream terminated unexpectedly."}},
                "ConverseStream",
            )
        ),
    )

    assert findDiagnostic(await runBedrock()).details == {"errorCode": "ModelStreamErrorException", "requestId": REQUEST_ID}


async def test_does_not_report_a_transport_failure_name_as_a_provider_error_code(monkeypatch):
    # Real `Error`s with informative names, but not AWS codes; modeled ones end in "Exception".
    timeout = RuntimeError("Connection timed out after 1000 ms")
    timeout.name = "TimeoutError"  # type: ignore[attr-defined]
    useFakeBedrock(monkeypatch, respondWithFailingStream(timeout))

    assert findDiagnostic(await runBedrock()).details == {"requestId": REQUEST_ID}


async def test_emits_no_diagnostic_when_the_failure_carries_no_provider_metadata(monkeypatch):
    useFakeBedrock(monkeypatch, _RejectingClient(RuntimeError("socket hang up")))

    message = await runBedrock()

    assert message.stopReason == "error"
    assert message.errorMessage == "socket hang up"
    assert findDiagnostic(message) is None


async def test_emits_no_diagnostic_for_an_aborted_turn(monkeypatch):
    controller = AbortController()
    controller.abort()
    useFakeBedrock(monkeypatch, _RejectingClient(makeServiceException("ValidationException", 400, REQUEST_ID)))

    message = await runBedrock(controller.signal)

    assert message.stopReason == "aborted"
    assert findDiagnostic(message) is None


async def test_drops_header_derived_values_that_exceed_the_length_bound(monkeypatch):
    useFakeBedrock(monkeypatch, _RejectingClient(makeServiceException(f"{'E' * 5000}Exception", 400, "R" * 5000)))

    assert findDiagnostic(await runBedrock()).details == {"status": 400}


async def test_omits_the_sdks_unknown_placeholder_instead_of_reporting_it_as_a_code(monkeypatch):
    # The SDK's fallback when the response carried no `x-amzn-errortype`.
    useFakeBedrock(monkeypatch, _RejectingClient(makeServiceException("Unknown", 403, REQUEST_ID)))

    assert findDiagnostic(await runBedrock()).details == {"status": 403, "requestId": REQUEST_ID}


# --- bedrock-raw-stop-reason.test.ts ------------------------------------------------------------


async def test_forwards_sdk_stream_items_in_order_before_normalizing_them(monkeypatch):
    streamEvents = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hello"}}},
        {"messageStop": {"stopReason": "end_turn", "additionalModelResponseFields": {"source": "test"}}},
        {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}}},
    ]
    useFakeBedrock(monkeypatch, FakeBedrockClient(*streamEvents))
    model = getModelFixture()
    received: list[Any] = []
    eventModels: list[Any] = []

    async def onProviderStreamEvent(item: Any, eventModel: Any) -> None:
        received.append(item)
        eventModels.append(eventModel)

    result = await streamBedrock(
        model, context, {"cacheRetention": "none", "onProviderStreamEvent": onProviderStreamEvent}
    ).result()

    assert received == streamEvents
    for index, item in enumerate(received):
        assert item is streamEvents[index]
    assert eventModels == [model] * 4
    assert result.stopReason == "stop"
    assert [block.model_dump(exclude_none=True) for block in result.content] == [{"type": "text", "text": "hello"}]


async def test_forwards_sdk_error_items_before_reporting_them(monkeypatch):
    exception = RuntimeError("bedrock stream failed")
    streamEvents = [{"messageStart": {"role": "assistant"}}, {"internalServerException": exception}]
    useFakeBedrock(monkeypatch, FakeBedrockClient(*streamEvents))
    received: list[Any] = []

    result = await streamBedrock(
        getModelFixture(), context, {"cacheRetention": "none", "onProviderStreamEvent": lambda item, _m: received.append(item)}
    ).result()

    assert received == streamEvents
    assert result.stopReason == "error"
    assert result.errorMessage == "bedrock stream failed"


@pytest.mark.parametrize(
    ("stopReason", "expected", "errorMessage"),
    [
        ("end_turn", "stop", None),
        ("guardrail_intervened", "error", "Provider stopped with: guardrail_intervened"),
    ],
)
async def test_preserves_raw_bedrock_stop_reasons(monkeypatch, stopReason, expected, errorMessage):
    useFakeBedrock(
        monkeypatch, FakeBedrockClient({"messageStart": {"role": "assistant"}}, {"messageStop": {"stopReason": stopReason}})
    )

    message = await streamBedrock(getModelFixture(), context, {"cacheRetention": "none"}).result()

    assert message.stopReason == expected
    assert message.rawStopReason == stopReason
    assert message.errorMessage == errorMessage


# --- bedrock-cache-write-1h-cost.test.ts --------------------------------------------------------


async def test_prices_the_1h_cache_details_at_2x_while_preserving_the_total_cache_write(monkeypatch):
    # Regression test for https://github.com/earendil-works/pi/issues/9457
    useFakeBedrock(
        monkeypatch,
        FakeBedrockClient(
            {"messageStart": {"role": "assistant"}},
            {
                "metadata": {
                    "usage": {
                        "inputTokens": 100,
                        "outputTokens": 5,
                        "totalTokens": 1_000_105,
                        "cacheWriteInputTokens": 1_000_000,
                        "cacheDetails": [
                            {"ttl": "1h", "inputTokens": 150_000},
                            {"ttl": "5m", "inputTokens": 600_000},
                            {"ttl": "1h", "inputTokens": 250_000},
                        ],
                    }
                }
            },
            {"messageStop": {"stopReason": "end_turn"}},
        ),
    )
    model = getModelFixture()

    result = await streamBedrock(model, context, {"cacheRetention": "none"}).result()

    assert result.usage.cacheWrite == 1_000_000
    assert result.usage.cacheWrite1h == 400_000
    expectedCacheWriteCost = (600_000 * model.cost.cacheWrite + 400_000 * model.cost.input * 2) / 1_000_000
    assert result.usage.cost.cacheWrite == pytest.approx(expectedCacheWriteCost, abs=1e-10)


# --- bedrock-redacted-reasoning.test.ts ---------------------------------------------------------

redactedBase64 = "cnNuXzVaVnJpZjRKMGJYSXFtV2RsZWRqN1FJRmVOaWtSUWJF"
redactedBytes = base64.b64decode(redactedBase64)

gptModel = Model(
    id="global.openai.gpt-5.6-terra",
    name="GPT-5.6 Terra (Global)",
    api="bedrock-converse-stream",
    provider="amazon-bedrock",
    baseUrl="https://bedrock-runtime.ap-northeast-1.amazonaws.com",
    reasoning=True,
    input=["text"],
    cost=ModelCost(input=1.25, output=10, cacheRead=0.125, cacheWrite=0),
    contextWindow=400000,
    maxTokens=128000,
)


def emptyUsage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def redactedReasoningEvents() -> list[Any]:
    """Mirrors the ConverseStream frames GPT-5.6 emits: encrypted reasoning, then text."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"redactedContent": redactedBytes}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"contentBlockDelta": {"contentBlockIndex": 1, "delta": {"text": "done"}}},
        {"contentBlockStop": {"contentBlockIndex": 1}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]


helloContext = normalizeContext({"messages": [UserMessage(content="hello", timestamp=_now())]})


async def captureGptPayload(messages: list[Any]) -> Any:
    captured: list[Any] = []
    controller = AbortController()
    controller.abort()

    def onPayload(payload: Any, _model: Any) -> Any:
        captured.append(payload)
        return payload

    s = streamBedrock(
        gptModel,
        normalizeContext(Context(messages=messages)),
        {"cacheRetention": "none", "signal": controller.signal, "onPayload": onPayload},
    )
    async for event in s:
        if event.type == "error":
            break
    if not captured:
        raise RuntimeError("Expected Bedrock payload to be captured before request abort")
    return captured[0]


async def test_does_not_fail_the_stream_when_reasoning_arrives_as_redacted_content(monkeypatch):
    useFakeBedrock(monkeypatch, FakeBedrockClient(*redactedReasoningEvents()))

    response = await streamBedrock(gptModel, helloContext).result()

    assert response.stopReason != "error", response.errorMessage
    # Reasoning precedes the answer, matching the order Bedrock streamed it.
    assert [c.type for c in response.content] == ["thinking", "text"]
    assert response.content[1].model_dump(exclude_none=True) == {"type": "text", "text": "done"}


async def test_preserves_the_encrypted_reasoning_payload_on_the_assistant_message(monkeypatch):
    useFakeBedrock(monkeypatch, FakeBedrockClient(*redactedReasoningEvents()))

    response = await streamBedrock(gptModel, helloContext).result()

    thinking = next(c for c in response.content if c.type == "thinking")
    # Same representation Anthropic redacted thinking already uses: the opaque
    # payload rides in `thinkingSignature` with `redacted: true`.
    assert thinking.redacted is True
    assert thinking.thinkingSignature == redactedBase64
    # The byte buffer is streaming scratch state and never reaches the message.
    assert "redactedChunks" not in thinking.model_dump()


async def test_encodes_the_payload_when_the_stream_never_sends_content_block_stop(monkeypatch):
    useFakeBedrock(
        monkeypatch,
        FakeBedrockClient(
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"redactedContent": redactedBytes}}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ),
    )

    response = await streamBedrock(gptModel, helloContext).result()

    thinking = next(c for c in response.content if c.type == "thinking")
    assert thinking.thinkingSignature == redactedBase64
    # Streaming scratch state must not survive into the persisted message.
    assert "redactedChunks" not in thinking.model_dump()
    assert "index" not in thinking.model_dump()


async def test_joins_encrypted_reasoning_split_across_deltas(monkeypatch):
    head, tail = redactedBytes[:7], redactedBytes[7:]
    useFakeBedrock(
        monkeypatch,
        FakeBedrockClient(
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"redactedContent": head}}}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"reasoningContent": {"redactedContent": tail}}}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"messageStop": {"stopReason": "end_turn"}},
        ),
    )

    response = await streamBedrock(gptModel, helloContext).result()

    thinking = next(c for c in response.content if c.type == "thinking")
    assert thinking.thinkingSignature == redactedBase64
    # The placeholder marks the block once, not once per delta.
    assert thinking.thinking == "[Reasoning redacted]"


def _gptAssistant(content: list[Any], stopReason: str) -> AssistantMessage:
    return AssistantMessage(
        role="assistant",
        content=content,
        api="bedrock-converse-stream",
        provider="amazon-bedrock",
        model=gptModel.id,
        usage=emptyUsage(),
        stopReason=stopReason,
        timestamp=_now(),
    )


async def test_replays_redacted_reasoning_as_reasoning_content_redacted_content():
    payload = await captureGptPayload(
        [
            UserMessage(content="hello", timestamp=_now()),
            _gptAssistant(
                [
                    ThinkingContent(thinking="", thinkingSignature=redactedBase64, redacted=True),
                    TextContent(text="done"),
                ],
                "stop",
            ),
            UserMessage(content="continue", timestamp=_now()),
        ]
    )

    assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert assistant["content"] == [{"reasoningContent": {"redactedContent": redactedBytes}}, {"text": "done"}]


async def test_replays_redacted_reasoning_before_the_tool_use_block_it_belongs_to():
    # Bedrock rejects a tool continuation whose reasoning block is missing or
    # reordered, so the opaque payload must land ahead of the matching toolUse.
    payload = await captureGptPayload(
        [
            UserMessage(content="read the file", timestamp=_now()),
            _gptAssistant(
                [
                    ThinkingContent(thinking="", thinkingSignature=redactedBase64, redacted=True),
                    ToolCall(id="tool-1", name="read", arguments={"path": "/tmp/a.txt"}),
                ],
                "toolUse",
            ),
            ToolResultMessage(
                toolCallId="tool-1", toolName="read", content=[TextContent(text="file body")], isError=False, timestamp=_now()
            ),
        ]
    )

    assistant = next(m for m in payload["messages"] if m["role"] == "assistant")
    assert assistant["content"] == [
        {"reasoningContent": {"redactedContent": redactedBytes}},
        {"toolUse": {"toolUseId": "tool-1", "name": "read", "input": {"path": "/tmp/a.txt"}}},
    ]


# --- bedrock-response-headers.test.ts -----------------------------------------------------------

MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


async def test_forwards_raw_response_headers_to_on_response():
    reply = Reply(
        headers={
            "content-type": "application/vnd.amazon.eventstream",
            "x-bifrost-provider": "bedrock",
            "x-bifrost-resolved-model": MODEL_ID,
            "x-amzn-requestid": "req-123",
        }
    )
    responses: list[Any] = []
    with captureServer(lambda _request: reply) as server:
        model = getModel("amazon-bedrock", MODEL_ID).model_copy(update={"baseUrl": server.baseUrl})
        result = await streamBedrock(
            model,
            normalizeContext({"messages": [UserMessage(content="hello", timestamp=_now())]}),
            {
                "cacheRetention": "none",
                "env": {"AWS_BEDROCK_FORCE_HTTP1": "1", "AWS_BEDROCK_SKIP_AUTH": "1"},
                "onResponse": lambda response, _model: responses.append(response),
            },
        ).result()

    # The fake server intentionally returns an empty event stream; this assertion
    # documents that the header callback still fires before stream consumption.
    assert result.stopReason == "error"
    assert len(responses) == 1
    assert responses[0]["status"] == 200
    assert responses[0]["headers"]["x-amzn-requestid"] == "req-123"
    assert responses[0]["headers"]["x-bifrost-provider"] == "bedrock"
    assert responses[0]["headers"]["x-bifrost-resolved-model"] == MODEL_ID
