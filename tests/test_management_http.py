"""pi's ``management-http.test.ts`` cases (``misaka/utils/management_http.py``)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from misaka.utils import management_http
from misaka.utils.js import AbortController, AbortSignal, jsonStringify
from misaka.utils.management_http import FetchRetryOptions, fetchWithRetry
from misaka.utils.whatwg_fetch import Response


def _json(value) -> Response:
    """``Response.json(value)``."""
    return Response(jsonStringify(value), headers={"content-type": "application/json"})


def _mock_fetch(monkeypatch, implementation) -> list:
    calls: list = []

    async def fetch(input, init=None):
        calls.append(init)
        return await implementation(len(calls), init)

    monkeypatch.setattr(management_http, "fetch", fetch)
    return calls


async def test_retries_a_transient_transport_failure_once(monkeypatch):
    async def implementation(call, _init):
        if call < 3:
            raise RuntimeError("fetch failed")
        return _json({"ok": True})

    calls = _mock_fetch(monkeypatch, implementation)

    response = await fetchWithRetry("https://example.test")
    assert response.ok is True
    assert len(calls) == 3


async def test_shares_the_timeout_budget_across_attempts(monkeypatch):
    signals: list = []

    async def implementation(_call, init):
        if init and init.get("signal"):
            signals.append(init["signal"])
        if len(signals) == 1:
            raise RuntimeError("fetch failed")
        return _json({"ok": True})

    calls = _mock_fetch(monkeypatch, implementation)

    response = await fetchWithRetry("https://example.test", None, FetchRetryOptions(timeoutMs=1000))
    assert response.ok is True
    assert len(calls) == 2
    assert signals[0] is signals[1]


async def test_retries_an_attempt_timeout(monkeypatch):
    controllers: list[AbortController] = []

    def timeout(_ms):
        controller = AbortController()
        controllers.append(controller)
        return controller.signal

    monkeypatch.setattr(AbortSignal, "timeout", staticmethod(timeout))

    async def implementation(call, init):
        if call == 1:
            controllers[0].abort()
            init["signal"].throwIfAborted()
        return _json({"ok": True})

    calls = _mock_fetch(monkeypatch, implementation)

    await fetchWithRetry("https://example.test", None, FetchRetryOptions(attemptTimeoutMs=4000))

    assert len(calls) == 2
    assert len(controllers) == 2


async def test_retries_transient_http_responses_and_returns_the_successful_response(monkeypatch):
    async def implementation(call, _init):
        return Response("busy", status=503) if call == 1 else _json({"ok": True})

    calls = _mock_fetch(monkeypatch, implementation)

    response = await fetchWithRetry("https://example.test")
    assert response.ok is True
    assert len(calls) == 2


async def test_does_not_retry_caller_cancellation(monkeypatch):
    controller = AbortController()
    controller.abort()

    async def implementation(_call, _init):
        return _json({"ok": True})

    calls = _mock_fetch(monkeypatch, implementation)

    with pytest.raises(Exception):  # noqa: B017 - upstream's `rejects.toThrow()`
        await fetchWithRetry("https://example.test", {"signal": controller.signal})
    assert calls == []


async def test_an_aborted_fetch_cancels_its_request(monkeypatch):
    # MISAKA: `fetch` aborts the request itself; a raced request must not keep its socket open.
    cancelled = asyncio.Event()

    async def hang(self, request, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(httpx.AsyncClient, "send", hang)
    controller = AbortController()
    pending = asyncio.ensure_future(management_http.fetch("https://example.test", {"signal": controller.signal}))
    await asyncio.sleep(0.01)
    controller.abort()

    with pytest.raises(Exception) as raised:
        await pending
    assert getattr(raised.value, "name", None) == "AbortError"
    await asyncio.wait_for(cancelled.wait(), 1)
