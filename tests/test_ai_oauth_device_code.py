"""Translated from pi's ``packages/ai/test/oauth-device-code.test.ts`` (v1.1.0).

MISAKA fork: vitest's fake timers are stood in for by replacing the running loop's ``call_later`` with
``tests.fake_timers.FakeTimers`` and the module's clock with the fake one.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.auth.oauth import device_code
from misaka.ai.auth.oauth.device_code import pollOAuthDeviceCodeFlow
from misaka.utils.js import AbortController
from tests.fake_timers import FakeTimers

neverAbortedSignal = None


def _ms(text: str) -> float:
    return datetime.fromisoformat(text).astimezone(UTC).timestamp() * 1000


def useFakeTimers(monkeypatch: pytest.MonkeyPatch, systemTime: float = 0) -> FakeTimers:
    timers = FakeTimers()
    timers.now = systemTime
    loop = asyncio.get_running_loop()

    def call_later(delay: float, callback: Any, *args: Any, context: Any = None) -> SimpleNamespace:
        timer = timers.setTimeout(lambda: callback(*args), delay * 1000)
        return SimpleNamespace(cancel=lambda: timers.clearTimeout(timer))

    monkeypatch.setattr(loop, "call_later", call_later)
    monkeypatch.setattr(device_code, "_now", lambda: timers.now)
    return timers


@pytest.fixture(autouse=True)
def _signal():
    global neverAbortedSignal
    neverAbortedSignal = AbortController().signal


async def test_polls_immediately_and_returns_the_completed_value(monkeypatch):
    timers = useFakeTimers(monkeypatch, _ms("2026-03-09T00:00:00Z"))

    pollTimes: list[float] = []

    async def poll() -> Any:
        pollTimes.append(timers.now)
        return {"status": "pending"} if len(pollTimes) == 1 else {"status": "complete", "value": "token"}

    resultPromise = asyncio.ensure_future(
        pollOAuthDeviceCodeFlow(
            {
                "intervalSeconds": 2,
                "expiresInSeconds": 30,
                "poll": poll,
                "signal": neverAbortedSignal,
            }
        )
    )

    await timers.advanceAsync(0)
    assert pollTimes == [_ms("2026-03-09T00:00:00Z")]

    await timers.advanceAsync(1999)
    assert pollTimes == [_ms("2026-03-09T00:00:00Z")]

    await timers.advanceAsync(1)
    assert await resultPromise == "token"
    assert pollTimes == [_ms("2026-03-09T00:00:00Z"), _ms("2026-03-09T00:00:02Z")]


async def test_can_wait_before_the_first_poll(monkeypatch):
    timers = useFakeTimers(monkeypatch, _ms("2026-03-09T00:00:00Z"))

    pollTimes: list[float] = []

    async def poll() -> Any:
        pollTimes.append(timers.now)
        return {"status": "complete", "value": "token"}

    resultPromise = asyncio.ensure_future(
        pollOAuthDeviceCodeFlow(
            {
                "intervalSeconds": 2,
                "expiresInSeconds": 30,
                "waitBeforeFirstPoll": True,
                "poll": poll,
                "signal": neverAbortedSignal,
            }
        )
    )

    await timers.advanceAsync(1999)
    assert pollTimes == []

    await timers.advanceAsync(1)
    assert await resultPromise == "token"
    assert pollTimes == [_ms("2026-03-09T00:00:02Z")]


async def test_increases_the_interval_by_5_seconds_after_slow_down_without_a_server_interval(monkeypatch):
    timers = useFakeTimers(monkeypatch, _ms("2026-03-09T00:00:00Z"))
    startTime = timers.now

    pollTimes: list[float] = []
    results: list[Any] = [{"status": "slow_down"}, {"status": "complete", "value": "token"}]

    async def poll() -> Any:
        pollTimes.append(timers.now)
        if not results:
            raise RuntimeError("Unexpected extra poll")
        return results.pop(0)

    resultPromise = asyncio.ensure_future(
        pollOAuthDeviceCodeFlow(
            {
                "intervalSeconds": 2,
                "expiresInSeconds": 900,
                "poll": poll,
                "signal": neverAbortedSignal,
            }
        )
    )

    await timers.advanceAsync(0)
    assert pollTimes == [startTime]

    await timers.advanceAsync(6999)
    assert pollTimes == [startTime]

    await timers.advanceAsync(1)
    assert await resultPromise == "token"
    assert pollTimes == [startTime, startTime + 7000]


async def test_honors_a_server_provided_slow_down_interval(monkeypatch):
    timers = useFakeTimers(monkeypatch, _ms("2026-03-09T00:00:00Z"))
    startTime = timers.now

    pollTimes: list[float] = []
    results: list[Any] = [
        {"status": "slow_down", "intervalSeconds": 30},
        {"status": "complete", "value": "token"},
    ]

    async def poll() -> Any:
        pollTimes.append(timers.now)
        if not results:
            raise RuntimeError("Unexpected extra poll")
        return results.pop(0)

    resultPromise = asyncio.ensure_future(
        pollOAuthDeviceCodeFlow(
            {
                "intervalSeconds": 2,
                "expiresInSeconds": 900,
                "poll": poll,
                "signal": neverAbortedSignal,
            }
        )
    )

    await timers.advanceAsync(0)
    assert pollTimes == [startTime]

    await timers.advanceAsync(29999)
    assert pollTimes == [startTime]

    await timers.advanceAsync(1)
    assert await resultPromise == "token"
    assert pollTimes == [startTime, startTime + 30000]


async def test_cancels_an_in_flight_wait(monkeypatch):
    useFakeTimers(monkeypatch)
    controller = AbortController()

    async def poll() -> Any:
        return {"status": "pending"}

    resultPromise = asyncio.ensure_future(
        pollOAuthDeviceCodeFlow(
            {
                "intervalSeconds": 5,
                "expiresInSeconds": 30,
                "poll": poll,
                "signal": controller.signal,
            }
        )
    )
    await asyncio.sleep(0)

    controller.abort()
    with pytest.raises(RuntimeError, match="Login cancelled"):
        await resultPromise
