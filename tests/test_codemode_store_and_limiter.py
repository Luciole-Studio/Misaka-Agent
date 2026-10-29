"""Two translation slips in the Python port of pi's codemode ``execute.ts`` (v1.3.0), found 2026-10-29.

``readCodemodeStore``: upstream reads ``Object.entries(entry.data.set)``, which gives an array its indices as
string keys; the port called ``.items()`` on it, so one array-valued ``codemode-store`` entry on the branch failed
every later codemode call there, outside the sandbox.

``createLimiter``: upstream's limiter is a counter and a queue of resolvers. A JavaScript promise cannot be
cancelled; a Python waiter can, and the port's hand-off then raised ``InvalidStateError`` and never gave the slot
back, so every later ``models.*`` call of the script waited for ever."""
import asyncio

import pytest

from misaka.extensions.codemode.execute import (
    CODEMODE_STORE_ENTRY_TYPE,
    createLimiter,
    readCodemodeStore,
)


def store_entry(set_, delete=()):
    return {"type": "custom", "customType": CODEMODE_STORE_ENTRY_TYPE, "data": {"set": set_, "delete": list(delete)}}


def test_an_array_store_entry_reads_as_its_indices():
    branch = [store_entry({"kept": 1, "dropped": 2}), store_entry(["a", "b"], delete=["dropped"])]
    assert readCodemodeStore(branch) == {"kept": 1, "0": "a", "1": "b"}


async def test_a_cancelled_waiter_leaves_the_queue():
    limited = createLimiter(1)
    release = asyncio.Event()
    order = []

    async def hold():
        await release.wait()
        order.append("first")

    async def quick(name):
        order.append(name)

    first = asyncio.ensure_future(limited(hold))
    await asyncio.sleep(0)
    waiter = asyncio.ensure_future(limited(lambda: quick("cancelled")))
    third = asyncio.ensure_future(limited(lambda: quick("third")))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, third), 5)
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert order == ["first", "third"]
    await asyncio.wait_for(limited(lambda: quick("later")), 5)       # the slot came back
    assert order[-1] == "later"


async def test_a_waiter_cancelled_after_its_wake_up_passes_it_on():
    limited = createLimiter(1)
    release = asyncio.Event()
    order = []
    tasks = {}

    async def hold():
        await release.wait()
        # Cancel `woken` in the same loop turn as its wake-up, before it can resume.
        asyncio.get_running_loop().call_soon(tasks["woken"].cancel)

    async def quick(name):
        order.append(name)

    first = asyncio.ensure_future(limited(hold))
    await asyncio.sleep(0)
    tasks["woken"] = asyncio.ensure_future(limited(lambda: quick("woken")))
    next_ = asyncio.ensure_future(limited(lambda: quick("next")))
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, next_), 5)
    with pytest.raises(asyncio.CancelledError):
        await tasks["woken"]
    assert order == ["next"]
