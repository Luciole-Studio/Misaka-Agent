"""Translated from pi's ``packages/ai/src/utils/retry.ts`` (v1.3.0).

MISAKA fork: a few more patterns (marked where they stand), and callbacks are called the way JavaScript calls them:
the optional trailing ``finalError`` only reaches a callback that accepts it.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import AssistantMessage
from misaka.utils.js import addAbortListener, jsIsSafeInteger
from misaka.utils.values import maybe_await, signal_aborted


def _buildProviderErrorPattern(patterns: list[str]) -> re.Pattern[str]:
    # `new RegExp(patterns.join("|"), "i")`; `.test` on an unanchored pattern is `search`.
    return re.compile("|".join(patterns), re.IGNORECASE)


_NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN = _buildProviderErrorPattern(
    [
        # OpenCode Go/free-tier limits returned as 429 JSON error types by OpenCode's
        # Zen API. These are subscription/account limits, not transient throttles.
        "GoUsageLimitError",
        "FreeUsageLimitError",
        # OpenCode Go subscription-limit text asks users to enable available-balance
        # usage after rolling/weekly/monthly limits are reached.
        "Monthly usage limit reached",
        "available balance",
        # Generic quota/budget/billing exhaustion. `insufficient_quota` is OpenAI's
        # quota/billing error code; the other strings cover common gateway wording.
        "insufficient_quota",
        "out of budget",
        "quota exceeded",
        "billing",
        # Sign in with ChatGPT: the subscription's shared usage limit, which resets
        # after hours rather than seconds.
        "subscription_sharing_usage_limit_exceeded",
        # MISAKA fork: a workflow run's own token cap. Its message carries numbers ("has 429,000
        # of its 5,000,000 left") that the status codes below would otherwise read as a 429.
        "token_cap reached",
    ]
)

_RETRYABLE_PROVIDER_ERROR_PATTERN = _buildProviderErrorPattern(
    [
        # Generic provider load, HTTP status, and server-side transient failures.
        "overloaded",
        "server_busy",
        "servers are currently busy",
        "currently experiencing high demand",
        "model is at capacity",
        "rate.?limit",
        "too many requests",
        "429",
        "500",
        "502",
        "503",
        "504",
        "520",
        "524",
        # Also matches "The service is temporarily unavailable" (#10965).
        "service.?(?:is.?)?(?:temporarily.?)?unavailable",
        "server.?error",
        "internal.?error",
        # Wrapper/provider text for transient upstream failures, including OpenRouter
        # "Provider returned error" responses (#2264) and gateway wording such as
        # "The upstream provider returned an error" (#10842).
        "provider.?returned.?(?:an.?)?error",
        "exceeded request buffer limit while retrying upstream",
        # Gateway failure relaying the upstream response stream, sent as an OpenAI
        # Responses `response.failed` event without an error code (#10920).
        "unable to forward upstream",
        # Network, proxy, and fetch transport failures. This includes OpenAI Codex
        # raw-fetch failures such as "upstream connect", "connection refused", and
        # "reset before headers" (#733), plus OpenRouter connection drops (#3317).
        "network.?error",
        "connection.?error",
        "connection.?refused",
        "connection.?lost",
        "other side closed",
        "fetch failed",
        "getaddrinfo",
        "ENOTFOUND",
        "EAI_AGAIN",
        "upstream.?connect",
        "reset before headers",
        "socket hang up",
        "socket connection was closed",
        "timed? out",
        "timeout",
        "terminated",
        # WebSocket transports can report close/error text instead of HTTP/fetch text.
        "websocket.?closed",
        "websocket.?error",
        # Premature stream endings from SDKs and transports. Anthropic can throw
        # "stream ended without ..." and "Anthropic stream ended before message_stop"
        # (#4433); Bedrock/Smithy can throw an HTTP/2 no-response error (#3594).
        "ended without",
        "stream ended before message_stop",
        "stream ended before a terminal response event",
        "http2 request did not get a response",
        # Node ERR_HTTP2_STREAM_CANCEL: the HTTP/2 session died before the request was
        # sent, e.g. after the Bedrock SDK's 5-minute session timeout (#10379).
        "pending stream has been canceled",
        # MISAKA fork: a relaying gateway (sub2api) reports its own upstream drop as
        # `{"type":"stream_read_error","message":"upstream stream disconnected: unexpected
        # EOF"}`, wording none of pi's entries cover; a Sister on it then sat idle on the
        # error instead of retrying (2026-09-23, card t_c8b161).
        "stream.?disconnected",
        "unexpected EOF",
        "stream_read_error",
        # MISAKA fork: a reply dropped mid-stream, in httpx's and the OS's words (GitHub issue #6:
        # the provider gave up on a slow reader, and the turn ended with no retry). LCM's error
        # classifier already counts these as disconnects.
        "peer closed connection",
        "incomplete chunked read",
        "connection.?reset",
        "connection.?aborted",
        "WinError 1005[34]",
        # Provider-requested retry delay cap failures should flow through the outer
        # retry policy so callers can surface/abort the backoff (#1123).
        "retry delay",
        # Explicit retry guidance emitted mid-stream by OpenAI Responses and Bedrock
        # stream exceptions (#6019).
        "you can retry your request",
        "try your request again",
        "please retry your request",
        # gRPC based providers (e.g. NVIDIA NIM)
        "ResourceExhausted",
        # Sign in with ChatGPT: usage or user data temporarily unavailable. Usage
        # failures can arrive mid-stream without an HTTP 503 in the message.
        "subscription_sharing_usage_unavailable",
        "subscription_sharing_user_unavailable",
    ]
)


@dataclass(slots=True)
class RetryPolicy:
    """Retry policy: bounded attempts with exponential backoff (`baseDelayMs * 2^(attempt-1)`).
    `maxAgentDelayMs` caps each computed delay and defaults to 60 seconds.
    Matches `settings.retry` (`enabled`, `maxRetries`, `baseDelayMs`, `maxAgentDelayMs`) in coding-agent; kept
    here so the classifier and the policy-driven retry loop live together and stay reusable
    by the SDK and other callers."""

    enabled: bool
    # Max retry attempts (0 = no retries). The initial call never counts as a retry.
    maxRetries: int
    # Base delay in ms. Per-attempt delay is `baseDelayMs * 2^(attempt-1)` before jitter.
    baseDelayMs: int
    # Optional cap for agent-level retry delays in ms. Defaults to 60 seconds.
    maxAgentDelayMs: int | None = None


DEFAULT_MAX_AGENT_RETRY_DELAY_MS = 60_000
_MAX_SAFE_INTEGER = 2**53 - 1


def retryDelayMs(policy: Any, attempt: int) -> int:
    delay = policy.baseDelayMs * 2 ** max(0, attempt - 1)
    safeDelay = delay if jsIsSafeInteger(delay) else _MAX_SAFE_INTEGER
    return min(
        safeDelay,
        policy.maxAgentDelayMs if policy.maxAgentDelayMs is not None else DEFAULT_MAX_AGENT_RETRY_DELAY_MS,
    )


@dataclass(slots=True)
class RetryCallbacks:
    """Optional callbacks emitted by `retryAssistantCall` around each retry."""

    # Emitted before the backoff sleep of each retry attempt (1-indexed).
    onRetryScheduled: Callable[[int, int, int, str], Awaitable[None] | None] | None = None
    # Emitted after the backoff sleep, immediately before the retried call starts.
    onRetryAttemptStart: Callable[[], Awaitable[None] | None] | None = None
    # Emitted once when the loop ends: success if a later call completed normally.
    onRetryFinished: (
        Callable[[bool, int], Awaitable[None] | None] | Callable[[bool, int, str | None], Awaitable[None] | None] | None
    ) = None


class _RetrySleepAbortError(Exception):
    def __init__(self) -> None:
        super().__init__("Aborted")


async def _sleep(ms: float, signal: Any = None) -> None:
    if signal_aborted(signal):
        raise _RetrySleepAbortError()
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()
    timeout = loop.call_later(max(0.0, ms) / 1000, lambda: None if done.done() else done.set_result(None))

    def onAbort() -> None:
        timeout.cancel()
        if not done.done():
            done.set_exception(_RetrySleepAbortError())

    remove = addAbortListener(signal, onAbort) if signal is not None else (lambda: None)
    try:
        await done
    finally:
        timeout.cancel()
        remove()


async def _invoke_callback(callback: Callable[..., Any] | None, *args: Any) -> None:
    """MISAKA fork: ``await callbacks?.x?.(...)``."""
    if callback is None:
        return
    await maybe_await(callback(*args))


async def _invoke_finished_callback(
    callback: Callable[..., Any] | None,
    success: bool,
    attempt: int,
    *finalError: str | None,
) -> None:
    """MISAKA fork: ``callbacks?.onRetryFinished?.(success, attempt[, finalError])``. JavaScript passes the call's
    arguments, drops those a callback does not declare, and reads a declared one left out as ``undefined``."""
    if callback is None:
        return
    arguments = (success, attempt, *finalError)
    try:
        parameters = list(inspect.signature(callback).parameters.values())
    except (TypeError, ValueError):
        result = callback(*arguments)
    else:
        acceptsVarargs = any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters)
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if acceptsVarargs or (len(positional) >= 3 and finalError):
            result = callback(*arguments)
        elif len(positional) >= 3 and positional[2].default is inspect.Parameter.empty:
            result = callback(success, attempt, None)
        else:
            result = callback(success, attempt)
    await maybe_await(result)


async def retryAssistantCall(
    produce: Callable[[], Awaitable[AssistantMessage]],
    policy: RetryPolicy | None,
    signal: Any | None,
    callbacks: RetryCallbacks | None = None,
) -> AssistantMessage:
    """Run a single assistant-producing call with bounded retry on transient errors.

    Behavior:
    - A successful response is returned immediately. Aborts are terminal and never
      retried, but reported as unsuccessful if they happen after a retry was scheduled.
      Aborts during the backoff sleep are normalized to an aborted `AssistantMessage`
      too, so callers do not need to care when cancellation happened.
    - A non-retryable error (per `isRetryableAssistantError`, including quota/
      billing exhaustion) is returned immediately so deterministic errors fail fast.
    - Otherwise retries up to `maxRetries` times with exponential backoff, emitting
      `onRetryScheduled` before each sleep, `onRetryAttemptStart` after each sleep before
      the retried call starts, and `onRetryFinished` once at the end (whether the loop
      ends in success, exhausted retries, or an aborted backoff).

    When `policy` is undefined or disabled, the first response is returned unchanged
    (equivalent to calling `produce()` directly).
    """
    maxAttempts = policy.maxRetries if policy is not None and policy.enabled else 0

    attempt = 0
    lastRetry: dict[str, Any] | None = None
    while True:
        response = await produce()

        # Abort: terminal but not successful. Never retry an aborted message.
        if response.stopReason == "aborted":
            if lastRetry is not None:
                await _invoke_finished_callback(callbacks.onRetryFinished if callbacks else None, False, lastRetry["attempt"])
            return response

        # Success: non-error, non-abort responses return as-is.
        if response.stopReason != "error":
            if lastRetry is not None:
                await _invoke_finished_callback(callbacks.onRetryFinished if callbacks else None, True, lastRetry["attempt"])
            return response

        # Non-retryable, or budget exhausted: return the final error message.
        if attempt >= maxAttempts or not isRetryableAssistantError(response):
            if lastRetry is not None:
                await _invoke_finished_callback(
                    callbacks.onRetryFinished if callbacks else None, False, lastRetry["attempt"], response.errorMessage
                )
            return response

        attempt += 1
        lastRetry = {"attempt": attempt, "errorMessage": response.errorMessage or "Unknown error"}
        delayMs = retryDelayMs(policy, attempt)
        await _invoke_callback(
            callbacks.onRetryScheduled if callbacks else None, attempt, maxAttempts, delayMs, lastRetry["errorMessage"]
        )

        # Normalize aborts during retry backoff to the same AssistantMessage shape as
        # provider stream aborts, so callers do not need to care when cancellation happened.
        try:
            await _sleep(delayMs, signal)
        except Exception as error:
            await _invoke_finished_callback(
                callbacks.onRetryFinished if callbacks else None, False, attempt, lastRetry["errorMessage"]
            )
            if isinstance(error, _RetrySleepAbortError):
                # MISAKA fork: a pydantic copy cannot drop a field; `errorMessage` is cleared instead.
                return response.model_copy(update={"stopReason": "aborted", "errorMessage": None})
            raise
        await _invoke_callback(callbacks.onRetryAttemptStart if callbacks else None)


def isRetryableAssistantError(message: AssistantMessage) -> bool:
    """Classifies whether a failed assistant message looks like a transient provider
    or transport error, so callers can decide if the last assistant turn should be
    restarted.

    This does not implement retry policy. Callers should first handle context
    overflow separately, then apply their own retry budget, backoff, and reporting
    before restarting the assistant turn.
    """
    if message.stopReason != "error" or not message.errorMessage:
        return False
    errorMessage = message.errorMessage
    if _NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN.search(errorMessage):
        return False
    return bool(_RETRYABLE_PROVIDER_ERROR_PATTERN.search(errorMessage))


__all__ = [
    "DEFAULT_MAX_AGENT_RETRY_DELAY_MS",
    "RetryCallbacks",
    "RetryPolicy",
    "isRetryableAssistantError",
    "retryAssistantCall",
    "retryDelayMs",
]
