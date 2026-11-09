"""pi chord's ``test/awaiting.test.ts``.

Entry 8: awaited gets. ``host.get`` and ``ctx.get`` resolve once the reference is ready; abort, the ctx closing and the
host's ``readyTimeout`` reject them and release only that reference.

MISAKA adaptations: a promise is an ``asyncio.Future``; ``promise.catch(() => {})`` marks the future's failure as
observed; ``AbortController`` is ``misaka.utils.js``'s.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from misaka.chord import (
    ChordError,
    createHost,
    hostNamed,
    isChordError,
    memoryLinkPair,
    remote,
)
from misaka.chord._typebox import Type
from misaka.utils.js import AbortController
from tests.fixtures.chord_counting import CountingLink


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


async def until(check: Callable[[], bool], attempts: int = 200) -> None:
    for _ in range(attempts):
        if check():
            break
        await sleep(2)
    assert check(), "condition not reached"


Slow = remote(
    "t.e8-slow",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("server"),
        "variants": {"main": {"state": {"n": Type.Number()}}},
    },
)


def setup(readyTimeout: float | None = None) -> tuple[Any, Any, CountingLink, Callable[[], None]]:
    """A server whose ``Slow`` object goes live when ``open()`` is called; a browser whose link counts frames."""
    opened: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def open() -> None:
        if not opened.done():
            opened.set_result(None)

    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        self.resource("gate", {"version": 1, "open": lambda _options: opened})
        return {"variant": "main", "n": self.state("n", Type.Number(), 1)}

    server.serve(Slow, factory)
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"user": "u"}})
    link = CountingLink(up)
    options: dict[str, Any] = {
        "self": {"kind": "host"},
        "name": "browser",
        "kind": "client",
        "upstream": {"link": link, "principal": {"kind": "server"}},
        "onError": lambda _error: None,
    }
    if readyTimeout is not None:
        options["readyTimeout"] = readyTimeout
    browser = createHost(options)
    return server, browser, link, open


async def test_an_aborted_get_rejects_and_releases_only_its_reference_the_binding_stays_for_the_others():
    server, browser, link, open = setup()
    abort = AbortController()
    first = browser.get(Slow, {}, {"signal": abort.signal})
    second = browser.get(Slow, {})
    await sleep(5)
    abort.abort()
    with pytest.raises(ChordError) as info:
        await first
    assert isChordError(info.value, "aborted")
    await sleep(5)
    assert link.count("sent", "release") == 0, "the other awaiting get keeps the hold"
    open()
    ref = await second
    assert ref.n.value == 1
    assert link.count("sent", "hold") == 1
    ref.release()
    await until(lambda: link.count("sent", "release") == 1)
    await server.close()


async def test_a_ctx_get_still_waiting_when_its_call_settles_rejects_aborted_and_releases_its_reference():
    server, browser, link, open = setup()
    pending: list[Any] = []

    async def run(ctx: Any) -> None:
        future = ctx.get(Slow, {})
        future.add_done_callback(lambda done: done.cancelled() or done.exception())
        pending.append(future)

    await browser.root(run)
    with pytest.raises(ChordError) as info:
        await pending[0]
    assert isChordError(info.value, "aborted")
    await until(lambda: link.count("sent", "release") == 1)
    open()
    await server.close()


async def test_a_get_times_out_after_readytimeout_with_ready_timeout_and_releases_its_hold():
    server, browser, link, _open = setup(30)
    with pytest.raises(ChordError) as info:
        await browser.get(Slow, {})
    assert isChordError(info.value, "unavailable") and info.value.reason == "ready_timeout"
    await until(lambda: link.count("sent", "release") == 1)
    await server.close()


async def test_ctx_get_resolves_hydrated_the_calls_reference_is_released_when_the_call_ends():
    server, browser, link, open = setup()
    open()

    async def run(ctx: Any) -> Any:
        slow = await ctx.get(Slow, {})
        return slow.n.value

    value = await browser.root(run)
    assert value == 1
    await until(lambda: link.count("sent", "release") == 1)
    await server.close()
