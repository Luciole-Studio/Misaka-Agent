"""pi's ``model-catalog-refresh.test.ts`` (``misaka/modes/interactive/model_catalog_refresh.py``)."""

from __future__ import annotations

import asyncio

import pytest

from misaka.ai.models import ModelsRefreshResult
from misaka.modes.interactive.model_catalog_refresh import refreshModelCatalogs
from misaka.utils.js import AbortController


def successfulRefresh() -> ModelsRefreshResult:
    return ModelsRefreshResult(aborted=False, errors={})


class _Runtime:
    def __init__(self, respond):
        self.calls: list = []
        self._respond = respond

    async def refresh(self, options=None):
        self.calls.append(options)
        return await self._respond()


async def test_shares_one_runtime_refresh_between_concurrent_callers():
    deferred: asyncio.Future[ModelsRefreshResult] = asyncio.get_running_loop().create_future()

    async def respond():
        return await deferred

    runtime = _Runtime(respond)
    first = asyncio.ensure_future(refreshModelCatalogs(runtime, AbortController().signal))
    second = asyncio.ensure_future(refreshModelCatalogs(runtime, AbortController().signal))
    await asyncio.sleep(0)

    assert len(runtime.calls) == 1
    deferred.set_result(successfulRefresh())
    assert await first == successfulRefresh()
    assert await second == successfulRefresh()


async def test_keeps_the_shared_refresh_alive_when_one_caller_stops_waiting():
    deferred: asyncio.Future[ModelsRefreshResult] = asyncio.get_running_loop().create_future()

    async def respond():
        return await deferred

    runtime = _Runtime(respond)
    firstController = AbortController()
    first = asyncio.ensure_future(refreshModelCatalogs(runtime, firstController.signal))
    second = asyncio.ensure_future(refreshModelCatalogs(runtime, AbortController().signal))
    await asyncio.sleep(0)

    firstController.abort()
    with pytest.raises(Exception) as raised:
        await first
    assert getattr(raised.value, "name", None) == "AbortError"
    assert runtime.calls[0].signal.aborted is False

    deferred.set_result(successfulRefresh())
    assert await second == successfulRefresh()


async def test_aborts_an_abandoned_refresh_and_allows_a_later_refresh_to_start():
    async def respond():
        await asyncio.Event().wait()

    runtime = _Runtime(respond)
    firstController = AbortController()
    first = asyncio.ensure_future(refreshModelCatalogs(runtime, firstController.signal))
    await asyncio.sleep(0)

    firstController.abort()
    with pytest.raises(Exception) as raised:
        await first
    assert getattr(raised.value, "name", None) == "AbortError"
    for _ in range(20):
        if runtime.calls[0].signal.aborted:
            break
        await asyncio.sleep(0.01)
    assert runtime.calls[0].signal.aborted is True

    secondController = AbortController()
    second = asyncio.ensure_future(refreshModelCatalogs(runtime, secondController.signal))
    await asyncio.sleep(0)
    assert len(runtime.calls) == 2
    secondController.abort()
    with pytest.raises(Exception) as raised:
        await second
    assert getattr(raised.value, "name", None) == "AbortError"
