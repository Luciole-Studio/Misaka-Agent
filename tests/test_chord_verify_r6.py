"""Regression tests for the round-6 verification of the chord port (VERIFY2-R6-stack, chord findings R6-N1, R6-N4).

R6-L1 (the boundary scanner's false flag) is pinned by ``tests/test_stack_boundary.py``'s probe table.
"""

from __future__ import annotations

import asyncio
import gc
import time
from typing import Any

import pytest

from misaka.chord._promise import promiseAll

# ─── R6-N1: a reload whose caller gave up fails silently, as upstream's ──────


def _reloadScenario() -> tuple[Any, Any, Any, Any]:
    from tests.test_chord_reload import Greeter, Latch, greeterFacet, quiet

    return Greeter, Latch, greeterFacet, quiet


async def _withHandler() -> list[str]:
    seen: list[str] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: seen.append(f"{context['message']} | {context.get('exception')!r} | {context.get('task') or context.get('future')!r}")
    )
    return seen


async def test_n1_a_reload_that_fails_after_its_caller_gave_up_is_not_reported_as_never_retrieved():
    """Node (`reload_fail_giveup.mts`): `unhandled rejections: []`."""
    Greeter, Latch, greeterFacet, quiet = _reloadScenario()
    seen = await _withHandler()
    host = quiet()
    host.install(greeterFacet("hello"))
    ref = host.hold(Greeter, {})
    await ref.ready
    go = Latch()

    async def blocker(ctx: Any) -> None:
        await ref.greet("g", ctx)
        await go.promise

    root = host.root(blocker)
    for _ in range(10):
        await asyncio.sleep(0)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(host.reload([greeterFacet("hi", state={"version": 2})], {"gateDeadline": 100}), 0.02)
    await asyncio.sleep(0.25)  # the gate deadline passes: the reload fails with nobody waiting any more
    assert not host.reloading
    go.open()
    await root
    await host.close()
    gc.collect()
    await asyncio.sleep(0)
    assert seen == []


async def test_n1_a_failing_reload_nobody_ever_awaits_is_still_reported_and_an_awaiting_caller_gets_its_error():
    from misaka.chord import ChordError
    from misaka.chord.host import Facet

    _Greeter, _Latch, _greeterFacet, quiet = _reloadScenario()
    seen = await _withHandler()
    host = quiet()

    def broken(_api: Any) -> None:
        raise RuntimeError("setup fails")

    with pytest.raises(ChordError, match="setup fails"):
        await host.reload([Facet("broken", broken)])
    unawaited = host.reload([Facet("broken", broken)])
    for _ in range(10):
        await asyncio.sleep(0)
    assert unawaited.done()
    del unawaited
    gc.collect()
    await asyncio.sleep(0)
    assert len(seen) == 1 and "never retrieved" in seen[0] and "setup fails" in seen[0]
    await host.close()


# ─── R6-N4: promiseAll is linear ─────────────────────────────────────────────


async def test_n4_promise_all_settles_ten_thousand_inputs_in_linear_time():
    """Before: 8.5 s for 10,000 inputs settled one per turn (quadratic); `asyncio.gather` takes about 0.15 s."""
    loop = asyncio.get_running_loop()
    futures: list[asyncio.Future[int]] = [loop.create_future() for _ in range(10_000)]

    async def settleOnePerTurn() -> None:
        for index, future in enumerate(futures):
            future.set_result(index)
            await asyncio.sleep(0)

    started = time.perf_counter()
    feeder = asyncio.ensure_future(settleOnePerTurn())
    results = await promiseAll(futures)
    await feeder
    elapsed = time.perf_counter() - started
    assert results == list(range(10_000))
    assert elapsed < 3.0, f"promiseAll took {elapsed:.2f} s for 10,000 inputs"


async def test_n4_promise_all_keeps_promise_all_semantics():
    loop = asyncio.get_running_loop()
    # Order kept, plain values pass through.
    first, second = loop.create_future(), loop.create_future()
    loop.call_soon(second.set_result, "b")
    loop.call_later(0.01, first.set_result, "a")
    assert await promiseAll([first, "x", second]) == ["a", "x", "b"]
    # The first failure to settle wins; the siblings are not cancelled and their later failures count as handled.
    slow, early, late = loop.create_future(), loop.create_future(), loop.create_future()
    loop.call_soon(early.set_exception, ValueError("early"))
    loop.call_later(0.01, late.set_exception, KeyError("late"))
    with pytest.raises(ValueError, match="early"):
        await promiseAll([slow, late, early])
    assert not slow.cancelled()
    await asyncio.sleep(0.02)
    assert late.done() and not slow.done()
    # Failures already settled report in input order; a cancelled input is a rejection.
    one, two = loop.create_future(), loop.create_future()
    one.set_exception(ValueError("one"))
    two.set_exception(ValueError("two"))
    with pytest.raises(ValueError, match="one"):
        await promiseAll([one, two])
    gone = loop.create_future()
    gone.cancel()
    with pytest.raises(asyncio.CancelledError):
        await promiseAll([gone])
    # A waiter that gives up cancels only its wait.
    shared = loop.create_future()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(promiseAll([shared]), 0.01)
    assert not shared.cancelled()
    slow.set_result(None)


async def test_x1_a_missed_gate_deadline_leaves_no_wait_pending_to_be_destroyed():
    """Found by the R6-N1 test: after a missed gate deadline the gates are opened without draining, and the private
    wait for them stayed pending until collected (asyncio's "Task was destroyed but it is pending!")."""
    from misaka.chord import ChordError

    Greeter, Latch, greeterFacet, quiet = _reloadScenario()
    seen = await _withHandler()
    host = quiet()
    host.install(greeterFacet("hello"))
    ref = host.hold(Greeter, {})
    await ref.ready
    go = Latch()

    async def blocker(ctx: Any) -> None:
        await ref.greet("g", ctx)
        await go.promise

    root = host.root(blocker)
    for _ in range(10):
        await asyncio.sleep(0)
    with pytest.raises(ChordError, match="gate deadline"):
        await host.reload([greeterFacet("hi", state={"version": 2})], {"gateDeadline": 20})
    go.open()
    await root
    await host.close()
    gc.collect()
    await asyncio.sleep(0)
    assert [entry for entry in seen if "destroyed" in entry] == []
