"""Translated from pi's ``packages/ai/test/retry.test.ts`` (v1.3.0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.utils.retry import (
    RetryCallbacks,
    RetryPolicy,
    isRetryableAssistantError,
    retryAssistantCall,
    retryDelayMs,
)
from misaka.utils.js import AbortController

openAIExplicitRetryMessage = (
    "An error occurred while processing your request. You can retry your request, or contact us through our help "
    "center at help.openai.com if the error persists. Please include the request ID req_******** in your message."
)
bedrockExplicitRetryMessage = (
    '{"message":"The system encountered an unexpected error during processing. Try your request again."}'
)
nvidiaNIMResourceExhaustedMessage = "ResourceExhausted: Worker local total request limit reached (288/48)"
bunFetchSocketClosedMessage = (
    "The socket connection was closed unexpectedly. For more information, pass `verbose: true` in the second "
    "argument to fetch()"
)
openAIResponsesEarlyEofMessage = "OpenAI Responses stream ended before a terminal response event"
wrappedDnsLookupError = (
    "The pending stream has been canceled (caused by: getaddrinfo ENOTFOUND bedrock-runtime.us-east-1.amazonaws.com)"
)
azurePeakLoadError = (
    "The system is currently experiencing high demand and cannot process your request. Your request exceeds the "
    "maximum usage size allowed during peak load. For improved capacity reliability, consider switching to "
    "Provisioned Throughput."
)


def _failed(errorMessage: str) -> Any:
    return fauxAssistantMessage("", {"stopReason": "error", "errorMessage": errorMessage})


class _Fn:
    """``vi.fn``: records each call's arguments and returns what ``implementation`` returns."""

    def __init__(self, implementation: Any = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self._implementation = implementation

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self._implementation(*args) if self._implementation is not None else None


def test_matches_explicit_provider_retry_guidance():
    assert isRetryableAssistantError(_failed(openAIExplicitRetryMessage)) is True
    assert isRetryableAssistantError(_failed(bedrockExplicitRetryMessage)) is True
    assert isRetryableAssistantError(_failed(nvidiaNIMResourceExhaustedMessage)) is True


def test_matches_bun_fetch_socket_drop_wording():
    assert isRetryableAssistantError(_failed(bunFetchSocketClosedMessage)) is True


def test_matches_upstream_request_buffer_exhaustion_wording():
    assert isRetryableAssistantError(_failed("Error: exceeded request buffer limit while retrying upstream")) is True


@pytest.mark.parametrize(
    "errorMessage",
    [
        wrappedDnsLookupError,
        "connect ENOTFOUND api.example.com",
        "EAI_AGAIN api.example.com",
        "getaddrinfo failed for api.example.com",
    ],
)
def test_matches_dns_transport_failure_wording(errorMessage):
    assert isRetryableAssistantError(_failed(errorMessage)) is True


@pytest.mark.parametrize(
    "errorMessage",
    ["The pending stream has been canceled", "The pending stream has been canceled (caused by: socket closed)"],
)
def test_matches_http2_pending_stream_cancellation(errorMessage):
    # Regression for #10379.
    assert isRetryableAssistantError(_failed(errorMessage)) is True


@pytest.mark.parametrize(
    "errorMessage",
    [
        # Regression for #10965 (provider unknown, no status code in the message).
        "The service is temporarily unavailable. Please retry later.",
        # Bedrock ServiceUnavailableException as formatted by formatBedrockError.
        "Service unavailable: The service is temporarily unavailable.",
    ],
)
def test_matches_service_unavailable_wording(errorMessage):
    assert isRetryableAssistantError(_failed(errorMessage)) is True


@pytest.mark.parametrize(
    "errorMessage",
    [
        # pi 1.2.0 (#10842): gateway wording of a transient upstream failure.
        "The upstream provider returned an error",
        # pi 1.3.0 (#10920): a `response.failed` event without an error code from a relaying gateway.
        "Unable to forward upstream stream",
    ],
)
def test_matches_gateway_upstream_failure_wording(errorMessage):
    assert isRetryableAssistantError(_failed(errorMessage)) is True


def test_matches_openai_responses_streams_that_end_before_terminal_events():
    assert isRetryableAssistantError(_failed(openAIResponsesEarlyEofMessage)) is True


def test_matches_azure_peak_load_capacity_errors():
    # Regression for #9669.
    assert isRetryableAssistantError(_failed(azurePeakLoadError)) is True


# MISAKA addition: pi 1.1.0 adds these capacity words to the retry pattern without a test of its own.
@pytest.mark.parametrize(
    "errorMessage",
    ["The model is at capacity, please retry", "server_busy", "Our servers are currently busy, try again later"],
)
def test_matches_capacity_wording_added_in_1_1_0(errorMessage):
    assert isRetryableAssistantError(_failed(errorMessage)) is True


def test_keeps_provider_limit_errors_non_retryable():
    assert isRetryableAssistantError(_failed("429 quota exceeded")) is False


def test_keeps_the_chatgpt_subscription_usage_limit_non_retryable():
    errorMessage = (
        'OpenAI API error (429): {"code":"subscription_sharing_usage_limit_exceeded","message":"Usage limit reached."}'
    )
    assert isRetryableAssistantError(_failed(errorMessage)) is False


@pytest.mark.parametrize(
    "errorMessage",
    [
        "subscription_sharing_usage_unavailable: Usage cannot be checked.",
        "subscription_sharing_user_unavailable: User cannot be loaded.",
    ],
)
def test_retries_temporary_chatgpt_subscription_errors(errorMessage):
    assert isRetryableAssistantError(_failed(errorMessage)) is True


def test_classifies_assistant_error_messages():
    assert isRetryableAssistantError(_failed("overloaded_error")) is True
    # Regression for #9627.
    assert isRetryableAssistantError(_failed("520 status code (no body)")) is True
    assert isRetryableAssistantError(_failed("524 status code (no body)")) is True
    assert isRetryableAssistantError(fauxAssistantMessage("not an error")) is False


def test_caps_agent_retry_delay():
    # Regression for #8826.
    assert retryDelayMs(SimpleNamespace(baseDelayMs=2000, maxAgentDelayMs=None), 6) == 60000
    assert retryDelayMs(SimpleNamespace(baseDelayMs=2000, maxAgentDelayMs=5000), 5) == 5000
    assert retryDelayMs(SimpleNamespace(baseDelayMs=2000, maxAgentDelayMs=0), 5) == 0


disabled = RetryPolicy(enabled=False, maxRetries=3, baseDelayMs=0)
enabled = RetryPolicy(enabled=True, maxRetries=3, baseDelayMs=0)


def _produce(implementation: Any) -> _Fn:
    async def call() -> Any:
        return implementation()

    return _Fn(call)


async def test_returns_a_successful_response_immediately_without_retrying():
    produce = _produce(lambda: fauxAssistantMessage("ok"))
    res = await retryAssistantCall(produce, enabled, None)
    assert [block.model_dump(exclude_none=True) for block in res.content] == [{"type": "text", "text": "ok"}]
    assert len(produce.calls) == 1


async def test_does_not_retry_an_aborted_message():
    produce = _produce(lambda: fauxAssistantMessage("", {"stopReason": "aborted"}))
    onRetryScheduled = _Fn()
    res = await retryAssistantCall(produce, enabled, None, RetryCallbacks(onRetryScheduled=onRetryScheduled))
    assert res.stopReason == "aborted"
    assert len(produce.calls) == 1
    assert onRetryScheduled.calls == []


async def test_does_not_retry_a_non_retryable_error_quota_billing():
    produce = _produce(lambda: _failed("insufficient_quota"))
    onRetryScheduled = _Fn()
    onRetryFinished = _Fn()
    res = await retryAssistantCall(
        produce, enabled, None, RetryCallbacks(onRetryScheduled=onRetryScheduled, onRetryFinished=onRetryFinished)
    )
    assert res.stopReason == "error"
    assert len(produce.calls) == 1
    assert onRetryScheduled.calls == []
    assert onRetryFinished.calls == []


async def test_retries_a_transient_error_up_to_max_retries_then_returns_the_final_error():
    produce = _produce(lambda: _failed("terminated"))
    onRetryScheduled = _Fn()
    onRetryFinished = _Fn()
    res = await retryAssistantCall(
        produce, enabled, None, RetryCallbacks(onRetryScheduled=onRetryScheduled, onRetryFinished=onRetryFinished)
    )
    assert res.stopReason == "error"
    assert len(produce.calls) == 4  # 1 initial + 3 retries
    assert len(onRetryScheduled.calls) == 3
    assert (False, 3, "terminated") in onRetryFinished.calls


async def test_reports_capped_retry_delays():
    # Regression for #8826.
    n = 0
    policy = RetryPolicy(enabled=True, maxRetries=4, baseDelayMs=10, maxAgentDelayMs=15)

    def respond() -> Any:
        nonlocal n
        n += 1
        return _failed("terminated") if n < 5 else fauxAssistantMessage("recovered")

    onRetryScheduled = _Fn()

    await retryAssistantCall(_produce(respond), policy, None, RetryCallbacks(onRetryScheduled=onRetryScheduled))

    assert [call[2] for call in onRetryScheduled.calls] == [10, 15, 15, 15]


async def test_stops_retrying_once_a_call_succeeds():
    n = 0

    def respond() -> Any:
        nonlocal n
        n += 1
        return _failed("terminated") if n < 3 else fauxAssistantMessage("recovered")

    produce = _produce(respond)
    onRetryFinished = _Fn()
    res = await retryAssistantCall(produce, enabled, None, RetryCallbacks(onRetryFinished=onRetryFinished))
    assert [block.model_dump(exclude_none=True) for block in res.content] == [{"type": "text", "text": "recovered"}]
    assert len(produce.calls) == 3
    assert (True, 2) in onRetryFinished.calls


async def test_reports_an_aborted_retried_call_as_unsuccessful():
    n = 0

    def respond() -> Any:
        nonlocal n
        n += 1
        return _failed("terminated") if n == 1 else fauxAssistantMessage("", {"stopReason": "aborted"})

    produce = _produce(respond)
    onRetryFinished = _Fn()
    res = await retryAssistantCall(produce, enabled, None, RetryCallbacks(onRetryFinished=onRetryFinished))
    assert res.stopReason == "aborted"
    assert len(produce.calls) == 2
    assert (False, 1) in onRetryFinished.calls


async def test_does_not_retry_when_policy_is_disabled():
    produce = _produce(lambda: _failed("terminated"))
    onRetryScheduled = _Fn()
    onRetryFinished = _Fn()
    res = await retryAssistantCall(
        produce, disabled, None, RetryCallbacks(onRetryScheduled=onRetryScheduled, onRetryFinished=onRetryFinished)
    )
    assert res.stopReason == "error"
    assert len(produce.calls) == 1
    assert onRetryScheduled.calls == []
    assert onRetryFinished.calls == []


async def test_emits_on_retry_attempt_start_after_backoff_before_each_retried_call():
    events: list[str] = []
    n = 0

    def respond() -> Any:
        nonlocal n
        events.append(f"produce:{n}")
        n += 1
        return _failed("terminated") if n < 3 else fauxAssistantMessage("recovered")

    produce = _produce(respond)
    onRetryScheduled = _Fn(lambda attempt, *rest: events.append(f"retry:{attempt}"))
    onRetryAttemptStart = _Fn(lambda: events.append("attempt-start"))
    res = await retryAssistantCall(
        produce,
        enabled,
        None,
        RetryCallbacks(onRetryScheduled=onRetryScheduled, onRetryAttemptStart=onRetryAttemptStart),
    )
    assert [block.model_dump(exclude_none=True) for block in res.content] == [{"type": "text", "text": "recovered"}]
    assert len(onRetryScheduled.calls) == 2
    assert len(onRetryAttemptStart.calls) == 2
    assert events == [
        "produce:0",
        "retry:1",
        "attempt-start",
        "produce:1",
        "retry:2",
        "attempt-start",
        "produce:2",
    ]


async def test_aborts_backoff_sleep_via_signal_returns_an_aborted_message_and_emits_on_retry_finished_false():
    controller = AbortController()
    produce = _produce(lambda: _failed("terminated"))
    policy = RetryPolicy(enabled=True, maxRetries=5, baseDelayMs=10_000)
    onRetryFinished = _Fn()
    p = asyncio.ensure_future(
        retryAssistantCall(produce, policy, controller.signal, RetryCallbacks(onRetryFinished=onRetryFinished))
    )
    # Let one error call resolve and the first backoff sleep start, then abort.
    while not produce.calls:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    controller.abort()
    res = await p
    assert res.stopReason == "aborted"
    assert res.errorMessage is None
    assert len(produce.calls) == 1
    assert (False, 1, "terminated") in onRetryFinished.calls
