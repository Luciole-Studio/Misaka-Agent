"""Coalesce concurrent identical in-flight calls into one."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from misaka.core.web import debug

# How long a follower waits on the leader before making its own call. Bounds the
# "leader stalled -> every caller blocks" failure mode; the wait is pure latency,
# so it is generous compared with any single request timeout.
FOLLOWER_WAIT_TIMEOUT = 90.0  # Standalone callers without a logical Web operation.

# ponytail: a plain dict, no lock. The event loop never switches tasks between the
# lookup and the insert below, so the leader election is already atomic. The running
# loop is part of the slot because a future can only be woken by the loop that made
# it: sharing one across loops does not raise, it silently never wakes the follower,
# which would then sit out the whole ``wait_timeout`` before falling back.
_inflight: dict[tuple[asyncio.AbstractEventLoop, str], tuple[asyncio.Future[Any], str | None]] = {}


async def single_flight[T](
    key: str,
    fn: Callable[[], Awaitable[T]],
    *,
    wait_timeout: float | None = None,
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

    leader = _inflight.get(slot)
    if leader is not None:
        leader_future, leader_trace = leader
        from misaka.core.web.timeouts import remaining_operation

        if wait_timeout is None:
            remaining = remaining_operation()
            wait_timeout = FOLLOWER_WAIT_TIMEOUT if remaining is None else remaining
        # asyncio.wait never cancels what it waits on, so a follower giving up cannot
        # disturb the leader or the other followers.
        debug.event("flight_wait", leader_trace_id=leader_trace)
        await asyncio.wait([leader_future], timeout=wait_timeout)
        if leader_future.done() and not leader_future.cancelled() and leader_future.exception() is None:
            debug.event("flight_reused", leader_trace_id=leader_trace)
            return leader_future.result()
        remaining_operation()  # An expired follower must not start another request.
        debug.event("flight_fallback", leader_trace_id=leader_trace)
        return await fn()

    future: asyncio.Future[T] = loop.create_future()
    _inflight[slot] = (future, debug.trace_id())
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
