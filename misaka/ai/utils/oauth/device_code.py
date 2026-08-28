"""OAuth device-code polling helpers."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, NotRequired, TypedDict

from misaka.utils.values import signal_aborted

CANCEL_MESSAGE = "Login cancelled"
TIMEOUT_MESSAGE = "Device flow timed out"
SLOW_DOWN_TIMEOUT_MESSAGE = (
    "Device flow timed out after one or more slow_down responses. "
    "This is often caused by clock drift in WSL or VM environments. "
    "Please sync or restart the VM clock and try again."
)
MINIMUM_INTERVAL_MS = 1000
DEFAULT_POLL_INTERVAL_SECONDS = 5
SLOW_DOWN_INTERVAL_INCREMENT_MS = 5000


class OAuthDeviceCodePendingResult(TypedDict):
    status: Literal["pending"]


class OAuthDeviceCodeSlowDownResult(TypedDict):
    status: Literal["slow_down"]
    # The new minimum the server is asking for. Upstream's own note at
    # `auth/oauth/device-code.ts:78-80` is that GitHub reports it here, and that honouring
    # it is what stops a client whose own clock drifts -- WSL and VMs are the usual case --
    # from polling early forever and burning the whole device-code window. Absent, RFC 8628
    # section 3.5 applies instead: add five seconds to what we were using.
    intervalSeconds: NotRequired[float | None]


class OAuthDeviceCodeCompleteResult(TypedDict):
    status: Literal["complete"]
    accessToken: str


class OAuthDeviceCodeFailedResult(TypedDict):
    status: Literal["failed"]
    message: str


OAuthDeviceCodePollResult = (
    OAuthDeviceCodePendingResult
    | OAuthDeviceCodeSlowDownResult
    | OAuthDeviceCodeCompleteResult
    | OAuthDeviceCodeFailedResult
)


class OAuthDeviceCodePollOptions(TypedDict, total=False):
    intervalSeconds: int | float
    expiresInSeconds: int | float
    poll: Callable[[], Awaitable[OAuthDeviceCodePollResult]]
    signal: Any | None


async def _abortable_sleep(ms: int, signal: Any, cancel_message: str) -> None:
    if signal_aborted(signal):
        raise RuntimeError(cancel_message)

    # Wait on the abort itself instead of waking twenty times a second for the whole
    # login window, which can be a quarter of an hour.
    sleep_task = asyncio.create_task(asyncio.sleep(ms / 1000))
    waiters: list[asyncio.Task[Any]] = [sleep_task]
    wait = getattr(signal, "wait", None)
    if callable(wait):
        result = wait()
        if isinstance(result, Awaitable):
            waiters.append(asyncio.create_task(result))
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        if signal_aborted(signal):
            raise RuntimeError(cancel_message)
    finally:
        for task in waiters:
            if not task.done():
                task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _sleep_until_next_poll(deadline: float, interval_ms: int, signal: Any) -> bool:
    """Wait out one interval, or report that the deadline arrived first."""
    remaining_ms = int(max(0.0, deadline - time.time()) * 1000) if deadline != float("inf") else interval_ms
    if remaining_ms <= 0:
        return False
    await _abortable_sleep(min(interval_ms, remaining_ms), signal, CANCEL_MESSAGE)
    return True


async def poll_oauth_device_code_flow(
    options: OAuthDeviceCodePollOptions | None = None,
    *,
    intervalSeconds: float | None = None,
    expiresInSeconds: float | None = None,
    poll: Callable[[], Awaitable[OAuthDeviceCodePollResult]] | None = None,
    signal: Any | None = None,
    waitBeforeFirstPoll: bool = False,
) -> str:
    if options is not None:
        intervalSeconds = options.get("intervalSeconds", intervalSeconds)
        expiresInSeconds = options.get("expiresInSeconds", expiresInSeconds)
        poll = options.get("poll", poll)
        signal = options.get("signal", signal)
        waitBeforeFirstPoll = options.get("waitBeforeFirstPoll", waitBeforeFirstPoll)

    if poll is None:
        raise TypeError("poll is required")

    deadline = time.time() + expiresInSeconds if isinstance(expiresInSeconds, (int, float)) else float("inf")
    interval_seconds = intervalSeconds if intervalSeconds is not None else DEFAULT_POLL_INTERVAL_SECONDS
    interval_ms = max(MINIMUM_INTERVAL_MS, int(interval_seconds * 1000))

    slow_down_responses = 0
    # Upstream polls first and sleeps after; the leading sleep is opt-in, because a user
    # who has already approved should not wait an interval to be told so. The three flows
    # that ask for it (xAI, GitHub Copilot, Kimi) do so because their endpoints reject a
    # poll that arrives before the code is registered.
    if waitBeforeFirstPoll:
        remaining_ms = int(max(0, deadline - time.time()) * 1000) if deadline != float("inf") else interval_ms
        if remaining_ms > 0:
            await _abortable_sleep(min(interval_ms, remaining_ms), signal, CANCEL_MESSAGE)

    while time.time() < deadline:
        if signal_aborted(signal):
            raise RuntimeError(CANCEL_MESSAGE)

        result = await poll()
        if result["status"] == "complete":
            return result["accessToken"]
        if result["status"] == "pending":
            if not await _sleep_until_next_poll(deadline, interval_ms, signal):
                break
            continue
        if result["status"] == "slow_down":
            slow_down_responses += 1
            requested = result.get("intervalSeconds")
            # `bool` is an `int` subclass, so a backend answering `"interval": true` would
            # pass the isinstance test and be read as one second -- collapsing the poll
            # interval to the floor at exactly the moment the server asked for less
            # traffic. The producers exclude booleans too, but this is the shared seam.
            if (
                isinstance(requested, (int, float))
                and not isinstance(requested, bool)
                and math.isfinite(requested)
                and requested > 0
            ):
                interval_ms = max(MINIMUM_INTERVAL_MS, int(requested * 1000))
            else:
                interval_ms = max(MINIMUM_INTERVAL_MS, interval_ms + SLOW_DOWN_INTERVAL_INCREMENT_MS)
            if not await _sleep_until_next_poll(deadline, interval_ms, signal):
                break
            continue
        raise RuntimeError(result["message"])

    raise RuntimeError(SLOW_DOWN_TIMEOUT_MESSAGE if slow_down_responses > 0 else TIMEOUT_MESSAGE)


pollOAuthDeviceCodeFlow = poll_oauth_device_code_flow

__all__ = [
    "OAuthDeviceCodeCompleteResult",
    "OAuthDeviceCodeFailedResult",
    "OAuthDeviceCodePendingResult",
    "OAuthDeviceCodePollOptions",
    "OAuthDeviceCodePollResult",
    "OAuthDeviceCodeSlowDownResult",
    "pollOAuthDeviceCodeFlow",
    "poll_oauth_device_code_flow",
]
