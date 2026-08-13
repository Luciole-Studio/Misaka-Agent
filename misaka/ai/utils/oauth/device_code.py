"""OAuth device-code polling helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

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


def _signal_aborted(signal: Any) -> bool:
    if signal is None:
        return False
    if hasattr(signal, "aborted"):
        return bool(signal.aborted)
    if hasattr(signal, "is_set"):
        return bool(signal.is_set())
    return False


async def _abortable_sleep(ms: int, signal: Any, cancel_message: str) -> None:
    if _signal_aborted(signal):
        raise RuntimeError(cancel_message)

    sleep_task = asyncio.create_task(asyncio.sleep(ms / 1000))
    try:
        while not sleep_task.done():
            if _signal_aborted(signal):
                sleep_task.cancel()
                raise RuntimeError(cancel_message)
            await asyncio.sleep(0.05)
        await sleep_task
    finally:
        if not sleep_task.done():
            sleep_task.cancel()


async def poll_oauth_device_code_flow(
    options: OAuthDeviceCodePollOptions | None = None,
    *,
    intervalSeconds: int | float | None = None,
    expiresInSeconds: int | float | None = None,
    poll: Callable[[], Awaitable[OAuthDeviceCodePollResult]] | None = None,
    signal: Any | None = None,
) -> str:
    if options is not None:
        intervalSeconds = options.get("intervalSeconds", intervalSeconds)
        expiresInSeconds = options.get("expiresInSeconds", expiresInSeconds)
        poll = options.get("poll", poll)
        signal = options.get("signal", signal)

    if poll is None:
        raise TypeError("poll is required")

    deadline = time.time() + expiresInSeconds if isinstance(expiresInSeconds, (int, float)) else float("inf")
    interval_seconds = intervalSeconds if intervalSeconds is not None else DEFAULT_POLL_INTERVAL_SECONDS
    interval_ms = max(MINIMUM_INTERVAL_MS, int(interval_seconds * 1000))

    slow_down_responses = 0
    while time.time() < deadline:
        if _signal_aborted(signal):
            raise RuntimeError(CANCEL_MESSAGE)

        remaining_ms = int(max(0, deadline - time.time()) * 1000) if deadline != float("inf") else interval_ms
        await _abortable_sleep(min(interval_ms, remaining_ms), signal, CANCEL_MESSAGE)

        result = await poll()
        if result["status"] == "complete":
            return result["accessToken"]
        if result["status"] == "pending":
            continue
        if result["status"] == "slow_down":
            slow_down_responses += 1
            interval_ms = max(MINIMUM_INTERVAL_MS, interval_ms + SLOW_DOWN_INTERVAL_INCREMENT_MS)
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
