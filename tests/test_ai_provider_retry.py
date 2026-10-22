"""Translated from pi's ``packages/ai/test/provider-retry.test.ts`` (v1.1.0).

MISAKA fork: vitest's fake timers are stood in for by replacing the running loop's ``call_later`` with
``tests.fake_timers.FakeTimers``; ``httpx.Headers`` is the ``Headers`` object.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from misaka.ai.utils.provider_retry import retryProviderRequest
from misaka.utils.js import AbortController
from tests.fake_timers import FakeTimers


def providerError(status: int | None, headers: dict[str, str] | None = None) -> Exception:
    error = Exception(f"Provider error: {status}")
    error.status = status  # type: ignore[attr-defined]
    error.headers = httpx.Headers(headers)  # type: ignore[attr-defined]
    return error


def useFakeTimers(monkeypatch: pytest.MonkeyPatch) -> FakeTimers:
    timers = FakeTimers()
    loop = asyncio.get_running_loop()

    def call_later(delay: float, callback: Any, *args: Any, context: Any = None) -> SimpleNamespace:
        timer = timers.setTimeout(lambda: callback(*args), delay * 1000)
        return SimpleNamespace(cancel=lambda: timers.clearTimeout(timer))

    monkeypatch.setattr(loop, "call_later", call_later)
    return timers


class _Request:
    """``vi.fn().mockRejectedValueOnce(...).mockResolvedValue(...)``: each call takes the next outcome, the last one
    repeats."""

    def __init__(self, *outcomes: Any) -> None:
        self.calls = 0
        self._outcomes = list(outcomes)

    async def __call__(self) -> Any:
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


async def test_retries_retryable_provider_errors(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    request = _Request(providerError(429, {"retry-after-ms": "1000"}), "ok")

    result = asyncio.ensure_future(retryProviderRequest(request, {"maxRetries": 1}))
    await timers.advanceAsync(999)
    assert request.calls == 1
    await timers.advanceAsync(1)

    assert await result == "ok"
    assert request.calls == 2


async def test_does_not_retry_errors_the_provider_marks_as_non_retryable():
    error = providerError(429, {"x-should-retry": "false"})
    request = _Request(error)

    with pytest.raises(Exception) as raised:
        await retryProviderRequest(request, {"maxRetries": 2})
    assert raised.value is error
    assert request.calls == 1


async def test_does_not_retry_statuses_listed_in_no_retry_statuses():
    error = providerError(504, {"retry-after-ms": "0"})
    request = _Request(error)

    with pytest.raises(Exception) as raised:
        await retryProviderRequest(request, {"maxRetries": 2, "noRetryStatuses": [504]})
    assert raised.value is error
    assert request.calls == 1


async def test_rejects_a_provider_requested_retry_delay_above_the_limit():
    request = _Request(providerError(429, {"retry-after": "277403"}))

    with pytest.raises(Exception, match=r"Server requested 277403s retry delay \(max: 1s\)"):
        await retryProviderRequest(request, {"maxRetries": 1, "maxRetryDelayMs": 1000})
    assert request.calls == 1


async def test_allows_disabling_the_provider_requested_retry_delay_cap(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    request = _Request(providerError(429, {"retry-after": "2"}), "ok")

    result = asyncio.ensure_future(retryProviderRequest(request, {"maxRetries": 1, "maxRetryDelayMs": 0}))
    await timers.advanceAsync(1999)
    assert request.calls == 1
    await timers.advanceAsync(1)

    assert await result == "ok"
    assert request.calls == 2


async def test_aborts_a_provider_requested_retry_delay(monkeypatch):
    timers = useFakeTimers(monkeypatch)
    controller = AbortController()
    request = _Request(providerError(429, {"retry-after": "277403"}))

    result = asyncio.ensure_future(
        retryProviderRequest(request, {"maxRetries": 2, "maxRetryDelayMs": 0, "signal": controller.signal})
    )
    await timers.advanceAsync(0)
    assert request.calls == 1
    assert timers.pending == 1

    controller.abort()

    with pytest.raises(Exception) as raised:
        await result
    assert getattr(raised.value, "name", None) == "AbortError"
    assert request.calls == 1
    assert timers.pending == 0
