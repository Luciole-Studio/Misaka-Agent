"""Coalesce concurrent identical in-flight calls into one."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

# How long a follower waits on the leader before making its own call. Bounds the
# "leader stalled -> every caller blocks" failure mode; the wait is pure latency,
# so it is generous compared with any single request timeout.
FOLLOWER_WAIT_TIMEOUT = 90.0

# ponytail: a plain dict, no lock. The event loop never switches tasks between the
# lookup and the insert below, so the leader election is already atomic. The running
# loop is part of the slot because a future can only be woken by the loop that made
# it: sharing one across loops does not raise, it silently never wakes the follower,
# which would then sit out the whole ``wait_timeout`` before falling back.
_inflight: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Future[Any]] = {}


async def single_flight[T](
    key: str,
    fn: Callable[[], Awaitable[T]],
    *,
    wait_timeout: float = FOLLOWER_WAIT_TIMEOUT,
) -> T:
    """Run ``fn`` once per ``key`` while that call is in flight.

    The first caller (*leader*) runs ``fn``; callers arriving for the same ``key``
    while it runs (*followers*) take the leader's result instead of duplicating the
    call. Nothing is kept once the call finishes -- this deduplicates simultaneous
    work, it is not a cache.

    A follower never shares the leader's bad outcome: if the leader raises, is
    cancelled, or has not answered within ``wait_timeout``, the follower makes its
    own independent call. Coalescing therefore cannot turn one transient failure
    into a correlated failure for every caller. ``fn`` must be idempotent enough to
    tolerate that extra call -- it is the same call the follower would have made
    without any coalescing at all.
    """
    loop = asyncio.get_running_loop()
    slot = (loop, key)

    leader_future = _inflight.get(slot)
    if leader_future is not None:
        # asyncio.wait never cancels what it waits on, so a follower giving up cannot
        # disturb the leader or the other followers.
        await asyncio.wait([leader_future], timeout=wait_timeout)
        if leader_future.done() and not leader_future.cancelled() and leader_future.exception() is None:
            return leader_future.result()
        return await fn()

    future: asyncio.Future[T] = loop.create_future()
    _inflight[slot] = future
    try:
        result = await fn()
    except BaseException:
        # Cancel rather than hand the exception on: followers are meant to retry it
        # themselves, and nobody would ever retrieve the stored exception.
        future.cancel()
        raise
    else:
        future.set_result(result)
        return result
    finally:
        _inflight.pop(slot, None)
