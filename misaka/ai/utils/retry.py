"""Assistant-level retry classification and policy, translated from pi's ``utils/retry.ts``.

This is a *text* classifier over a failed ``AssistantMessage``: providers report transient
trouble as prose ("terminated", "socket hang up", "Provider returned error"), and by the
time a stream has failed the HTTP status is long gone. ``ai/utils/provider_retry.py`` is
the other half -- an SDK-level policy reading an exception's status and headers -- and the
two do not replace each other.

The exclusion table is the part worth being loud about: a provider that is out of quota
answers with a 429 whose body says ``insufficient_quota``. Matching only the retry table
made that look like throttling, so an exhausted account paid three more doomed requests
and ~14s of backoff before failing. Excluded patterns are checked first and win.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import AssistantMessage
from misaka.ai.utils.abort import sleep
from misaka.utils.values import signal_aborted


def _build_provider_error_pattern(patterns: tuple[str, ...]) -> re.Pattern[str]:
    # pi builds `new RegExp(patterns.join("|"), "i")` and calls `.test`; the Python
    # equivalent of `test` on an unanchored pattern is `search`, not `match`.
    return re.compile("|".join(patterns), re.IGNORECASE)


NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN = _build_provider_error_pattern(
    (
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
    )
)

RETRYABLE_PROVIDER_ERROR_PATTERN = _build_provider_error_pattern(
    (
        # Generic provider load, HTTP status, and server-side transient failures.
        "overloaded",
        "rate.?limit",
        "too many requests",
        "429",
        "500",
        "502",
        "503",
        "504",
        "524",
        "service.?unavailable",
        "server.?error",
        "internal.?error",
        # Wrapper/provider text for transient upstream failures, including OpenRouter
        # "Provider returned error" responses (#2264).
        "provider.?returned.?error",
        "exceeded request buffer limit while retrying upstream",
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
    )
)


@dataclass(slots=True)
class RetryPolicy:
    """Bounded attempts with exponential backoff (``baseDelayMs * 2**(attempt-1)``).

    Mirrors ``settings.retry`` (``enabled`` / ``maxRetries`` / ``baseDelayMs``), so the
    classifier and the policy-driven loop stay together and stay reusable.
    """

    enabled: bool
    #: Max retry attempts (0 = no retries). The initial call never counts as a retry.
    maxRetries: int
    #: Base delay in ms. Per-attempt delay is ``baseDelayMs * 2**(attempt-1)``.
    baseDelayMs: int


async def retry_assistant_call(
    produce: Callable[[], Awaitable[AssistantMessage]],
    policy: RetryPolicy | None,
    signal: Any | None = None,
) -> AssistantMessage:
    """Run one assistant-producing call with bounded retry on transient errors.

    - A successful response returns immediately. Aborts are terminal and never retried.
      An abort *during* the backoff sleep is normalized to an aborted ``AssistantMessage``
      too, so callers do not need to care when cancellation happened.
    - A non-retryable error (per :func:`is_retryable_assistant_error`, quota/billing
      exhaustion included) returns immediately so deterministic errors fail fast.
    - Otherwise retries up to ``maxRetries`` times with exponential backoff.

    With ``policy`` ``None`` or disabled this is equivalent to awaiting ``produce()``.

    pi additionally takes an optional ``RetryCallbacks`` bundle here and invokes it around
    each attempt; coding-agent's ``core/agent-session.ts._summarizationRetryCallbacks``
    is what turns those calls into ``summarization_retry_*`` events. misaka has no such
    event and nothing listening for one, so the parameter is left out rather than shipped
    as a hook nothing can reach.
    """
    max_attempts = policy.maxRetries if policy is not None and policy.enabled else 0

    attempt = 0
    while True:
        response = await produce()

        # Anything that is not an error is terminal: a normal stop, or an abort, which pi
        # never retries either. pi splits those two branches only to tell its callbacks
        # whether the loop ended in success; without callbacks one check covers both.
        if response.stopReason != "error":
            return response

        if attempt >= max_attempts or not is_retryable_assistant_error(response):
            return response

        attempt += 1
        delay_ms = (policy.baseDelayMs if policy is not None else 0) * (2 ** (attempt - 1))

        try:
            await sleep(delay_ms, signal)
        except RuntimeError:
            # misaka's `abort.sleep` reports cancellation by raising
            # RuntimeError("Request was aborted") where pi throws a private
            # RetrySleepAbortError. Only a real abort is normalized to an aborted
            # message; anything else propagates, matching pi's `throw error`.
            if not signal_aborted(signal):
                raise
            return response.model_copy(update={"stopReason": "aborted", "errorMessage": None})


def is_retryable_assistant_error(message: AssistantMessage) -> bool:
    """Whether a failed assistant message looks like a transient provider/transport error.

    This is not a retry policy. Callers handle context overflow separately first, then
    apply their own budget, backoff, and reporting before restarting the assistant turn.
    """
    if message.stopReason != "error" or not message.errorMessage:
        return False
    if NON_RETRYABLE_PROVIDER_LIMIT_ERROR_PATTERN.search(message.errorMessage):
        return False
    return bool(RETRYABLE_PROVIDER_ERROR_PATTERN.search(message.errorMessage))
