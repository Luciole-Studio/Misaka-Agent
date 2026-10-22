"""Test support for the ports of pi's ``packages/ai/test/*-oauth.test.ts``.

MISAKA fork: ``vi.stubGlobal("fetch", ...)`` becomes a ``FetchMock`` patched over a flow module's ``fetch``, ``new
Response(...)`` is ``misaka.utils.whatwg_fetch.Response``, and vitest's fake timers replace the running loop's
``call_later`` and the wall clock (``time.time``) with ``tests.fake_timers.FakeTimers``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.utils.js import UNDEFINED, jsonStringify
from misaka.utils.whatwg_fetch import Response
from misaka.utils.whatwg_url import URL, URLSearchParams
from tests.fake_timers import FakeTimers


def jsonResponse(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(
        jsonStringify(body),
        status=status,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def textResponse(body: str, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(body, status=status, headers=headers)


def requestUrl(input: Any) -> str:
    if isinstance(input, str):
        return input
    if isinstance(input, URL):
        return input.toString()
    raise RuntimeError(f"Unsupported request input: {input!r}")


def requestForm(init: Any) -> URLSearchParams:
    return URLSearchParams(str((init or {}).get("body")))


def withOverrides(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """`{ ...base, ...overrides }` as JSON sees it: an `undefined` override drops the key."""
    merged = {**base, **overrides}
    return {key: value for key, value in merged.items() if value is not UNDEFINED}


class FetchMock:
    """``vi.fn(handler)`` standing in for ``fetch``: every call is recorded and answered by ``handler``."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[Any, Any]] = []

    async def __call__(self, input: Any, init: Any = None) -> Response:
        self.calls.append((input, init))
        result = self.handler(input, init)
        if asyncio.iscoroutine(result):
            result = await result
        return result


def stubFetch(monkeypatch: pytest.MonkeyPatch, module: Any, handler: Any) -> FetchMock:
    mock = FetchMock(handler)
    monkeypatch.setattr(module, "fetch", mock)
    return mock


def epochMs(text: str) -> float:
    return datetime.fromisoformat(text).astimezone(UTC).timestamp() * 1000


def useFakeTimers(monkeypatch: pytest.MonkeyPatch, systemTime: float | None = None) -> FakeTimers:
    timers = FakeTimers()
    timers.now = systemTime if systemTime is not None else time.time() * 1000
    loop = asyncio.get_running_loop()

    def call_later(delay: float, callback: Any, *args: Any, context: Any = None) -> SimpleNamespace:
        timer = timers.setTimeout(lambda: callback(*args), delay * 1000)
        return SimpleNamespace(cancel=lambda: timers.clearTimeout(timer))

    monkeypatch.setattr(loop, "call_later", call_later)
    monkeypatch.setattr(time, "time", lambda: timers.now / 1000)
    return timers


def interaction(*, signal: Any = None, prompt: Any = None, notify: Any = None) -> SimpleNamespace:
    """A `ProviderAuthInteraction`."""
    from misaka.utils.js import AbortController

    async def unexpectedPrompt(_prompt: Any) -> str:
        raise RuntimeError("Unexpected prompt")

    return SimpleNamespace(
        signal=signal if signal is not None else AbortController().signal,
        prompt=prompt if prompt is not None else unexpectedPrompt,
        notify=notify if notify is not None else (lambda _event: None),
    )
