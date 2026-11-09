"""Translated from pi's ``packages/ai/src/auth/oauth/device-code.ts`` (v1.1.0).

MISAKA fork (each is marked where it happens): a JSON ``true`` is not a number, and a missing signal (internal callers
pass none) never aborts.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, NotRequired, TypedDict

from misaka.utils.js import addAbortListener, isNumber
from misaka.utils.values import signal_aborted

_CANCEL_MESSAGE = "Login cancelled"
_TIMEOUT_MESSAGE = "Device flow timed out"
_SLOW_DOWN_TIMEOUT_MESSAGE = (
    "Device flow timed out after one or more slow_down responses. This is often caused by clock drift in WSL or VM"
    " environments. Please sync or restart the VM clock and try again."
)
_MINIMUM_INTERVAL_MS = 1000
# RFC 8628 section 3.2: if the authorization server omits `interval`, the client must use 5 seconds.
_DEFAULT_POLL_INTERVAL_SECONDS = 5
# RFC 8628 section 3.5: `slow_down` means the polling interval must increase by 5 seconds.
_SLOW_DOWN_INTERVAL_INCREMENT_MS = 5000


class _OAuthDeviceCodePendingResult(TypedDict):
    status: Literal["pending"]


class _OAuthDeviceCodeSlowDownResult(TypedDict):
    status: Literal["slow_down"]
    intervalSeconds: NotRequired[float | None]


class _OAuthDeviceCodeFailedResult(TypedDict):
    status: Literal["failed"]
    message: str


class _OAuthDeviceCodeCompleteResult(TypedDict):
    status: Literal["complete"]
    value: Any


_OAuthDeviceCodeIncompletePollResult = (
    _OAuthDeviceCodePendingResult | _OAuthDeviceCodeSlowDownResult | _OAuthDeviceCodeFailedResult
)

OAuthDeviceCodePollResult = _OAuthDeviceCodeIncompletePollResult | _OAuthDeviceCodeCompleteResult


class OAuthDeviceCodePollOptions(TypedDict):
    intervalSeconds: NotRequired[float | None]
    expiresInSeconds: NotRequired[float | None]
    waitBeforeFirstPoll: NotRequired[bool]
    poll: Callable[[], Awaitable[OAuthDeviceCodePollResult]]
    signal: Any


def _now() -> float:
    return time.time() * 1000


def abortableSleep(ms: float, signal: Any, cancelMessage: str) -> asyncio.Future[None]:
    loop = asyncio.get_running_loop()
    result: asyncio.Future[None] = loop.create_future()
    if signal_aborted(signal):
        result.set_exception(RuntimeError(cancelMessage))
        return result

    def onAbort() -> None:
        timeout.cancel()
        if not result.done():
            result.set_exception(RuntimeError(cancelMessage))

    def finish() -> None:
        removeAbortListener()
        if not result.done():
            result.set_result(None)

    timeout = loop.call_later(max(0.0, ms) / 1000, finish)
    # MISAKA fork: without a signal nothing can abort the sleep.
    removeAbortListener = addAbortListener(signal, onAbort) if signal is not None else (lambda: None)
    return result


async def pollOAuthDeviceCodeFlow(options: OAuthDeviceCodePollOptions) -> Any:
    expiresInSeconds = options.get("expiresInSeconds")
    deadline = _now() + expiresInSeconds * 1000 if isNumber(expiresInSeconds) else math.inf
    intervalSeconds = options.get("intervalSeconds")
    intervalMs = max(
        _MINIMUM_INTERVAL_MS,
        math.floor((intervalSeconds if intervalSeconds is not None else _DEFAULT_POLL_INTERVAL_SECONDS) * 1000),
    )

    slowDownResponses = 0
    if options.get("waitBeforeFirstPoll"):
        remainingMs = deadline - _now()
        if remainingMs > 0:
            await abortableSleep(min(intervalMs, remainingMs), options.get("signal"), _CANCEL_MESSAGE)

    while _now() < deadline:
        if signal_aborted(options.get("signal")):
            raise RuntimeError(_CANCEL_MESSAGE)

        result = await options["poll"]()
        if result["status"] == "complete":
            return result["value"]
        if result["status"] == "failed":
            raise RuntimeError(result["message"])
        if result["status"] == "slow_down":
            slowDownResponses += 1
            # Use the server-provided interval when given (GitHub reports the new required minimum
            # in `interval`); trusting only a client-tracked value risks polling early forever under
            # WSL/VM clock drift. Otherwise apply RFC 8628 section 3.5: increase by 5 seconds.
            requested = result.get("intervalSeconds")
            intervalMs = (
                max(_MINIMUM_INTERVAL_MS, math.floor(requested * 1000))
                if isNumber(requested) and math.isfinite(requested) and requested > 0
                else max(_MINIMUM_INTERVAL_MS, intervalMs + _SLOW_DOWN_INTERVAL_INCREMENT_MS)
            )

        remainingMs = deadline - _now()
        if remainingMs <= 0:
            break

        await abortableSleep(min(intervalMs, remainingMs), options.get("signal"), _CANCEL_MESSAGE)

    raise RuntimeError(_SLOW_DOWN_TIMEOUT_MESSAGE if slowDownResponses > 0 else _TIMEOUT_MESSAGE)


__all__ = ["OAuthDeviceCodePollOptions", "OAuthDeviceCodePollResult", "abortableSleep", "pollOAuthDeviceCodeFlow"]
