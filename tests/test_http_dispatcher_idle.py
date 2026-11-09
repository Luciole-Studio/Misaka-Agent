"""``configureHttpDispatcher`` sets the idle timeout every ``fetch`` opens its connection with, as pi's
undici dispatcher does; ``0`` disables it."""

from __future__ import annotations

import asyncio

import pytest

from misaka.core.http_dispatcher import configureHttpDispatcher
from misaka.utils.whatwg_fetch import createClient


def _readTimeout() -> float | None:
    async def read() -> float | None:
        client = createClient()
        try:
            return client.timeout.read
        finally:
            await client.aclose()

    return asyncio.run(read())


def test_fetch_uses_the_configured_idle_timeout():
    assert _readTimeout() == 300.0
    configureHttpDispatcher(60_000)
    assert _readTimeout() == 60.0
    configureHttpDispatcher(0)
    assert _readTimeout() is None


def test_an_invalid_idle_timeout_is_refused():
    with pytest.raises(ValueError, match="Invalid HTTP idle timeout"):
        configureHttpDispatcher(-1)
