import asyncio
import threading
import time

import pytest

from misaka.core.web.single_flight import _inflight, single_flight


@pytest.fixture(autouse=True)
def _clean_inflight():
    """A failing test must not hand its leftover leader to the next one."""
    _inflight.clear()
    yield
    _inflight.clear()


async def _settle():
    """Let every already-scheduled task reach its first real await."""
    for _ in range(3):
        await asyncio.sleep(0)


async def test_concurrent_callers_of_one_key_share_a_single_call():
    calls = 0
    gate = asyncio.Event()

    async def fn():
        nonlocal calls
        calls += 1
        await gate.wait()
        return "payload"

    tasks = [asyncio.create_task(single_flight("k", fn)) for _ in range(5)]
    await _settle()
    gate.set()
    assert await asyncio.gather(*tasks) == ["payload"] * 5
    assert calls == 1
    assert _inflight == {}


async def test_different_keys_do_not_wait_on_each_other():
    started: list[str] = []
    gate = asyncio.Event()

    async def fn(key):
        started.append(key)
        await gate.wait()
        return key

    tasks = [asyncio.create_task(single_flight(key, lambda key=key: fn(key))) for key in ("a", "b")]
    await _settle()
    assert sorted(started) == ["a", "b"]
    gate.set()
    assert sorted(await asyncio.gather(*tasks)) == ["a", "b"]
    assert _inflight == {}


async def test_a_failing_leader_sends_followers_to_their_own_call():
    calls = 0
    gate = asyncio.Event()

    async def fn():
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()
            raise RuntimeError("boom")
        return "own call"

    leader = asyncio.create_task(single_flight("k", fn))
    await _settle()
    followers = [asyncio.create_task(single_flight("k", fn)) for _ in range(2)]
    await _settle()
    gate.set()
    with pytest.raises(RuntimeError, match="boom"):
        await leader
    assert await asyncio.gather(*followers) == ["own call", "own call"]
    assert calls == 3
    assert _inflight == {}


async def test_a_follower_stops_waiting_on_a_stalled_leader():
    calls = 0
    gate = asyncio.Event()

    async def fn():
        nonlocal calls
        calls += 1
        mine = calls
        if mine == 1:
            await gate.wait()
        return f"r{mine}"

    leader = asyncio.create_task(single_flight("k", fn))
    await _settle()
    assert await single_flight("k", fn, wait_timeout=0.01) == "r2"
    # The leader is still registered, so later arrivals still coalesce onto it.
    assert [key for _loop, key in _inflight] == ["k"]
    gate.set()
    assert await leader == "r1"
    assert calls == 2
    assert _inflight == {}


async def test_a_cancelled_leader_leaves_nothing_behind():
    gate = asyncio.Event()

    async def fn():
        await gate.wait()
        return "never"

    leader = asyncio.create_task(single_flight("k", fn))
    await _settle()
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert _inflight == {}


async def test_a_cancelled_leader_sends_a_waiting_follower_to_its_own_call():
    """The follower must not inherit the leader's CancelledError."""
    calls = 0
    gate = asyncio.Event()

    async def fn():
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()
            return "never"
        return "own call"

    leader = asyncio.create_task(single_flight("k", fn))
    await _settle()
    follower = asyncio.create_task(single_flight("k", fn))
    await _settle()
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert await follower == "own call"
    assert calls == 2
    assert _inflight == {}


async def test_a_follower_giving_up_does_not_disturb_the_leader():
    """Waiting must never cancel the shared call -- that is why this uses asyncio.wait."""
    calls = 0
    gate = asyncio.Event()

    async def fn():
        nonlocal calls
        calls += 1
        await gate.wait()
        return "payload"

    leader = asyncio.create_task(single_flight("k", fn))
    await _settle()
    quitter = asyncio.create_task(single_flight("k", fn))
    stayer = asyncio.create_task(single_flight("k", fn))
    await _settle()
    quitter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await quitter
    gate.set()
    assert await leader == "payload"
    assert await stayer == "payload"
    assert calls == 1
    assert _inflight == {}


def test_a_leader_on_another_loop_does_not_stall_this_one():
    """A future cannot be woken by a foreign loop, so it must not be waited on at all."""
    registered = threading.Event()
    release = threading.Event()
    leader_result: list[str] = []

    def leader_thread():
        async def run():
            async def fn():
                registered.set()
                while not release.is_set():
                    await asyncio.sleep(0.005)
                return "leader"

            leader_result.append(await single_flight("cross", fn))

        asyncio.run(run())

    thread = threading.Thread(target=leader_thread)
    thread.start()
    try:
        assert registered.wait(5)

        async def own():
            return "mine"

        started = time.monotonic()
        # A generous cap: before the slot was keyed by loop this call sat out all of it.
        result = asyncio.run(single_flight("cross", own, wait_timeout=10.0))
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(timeout=5)

    assert result == "mine"
    assert elapsed < 2.0
    assert leader_result == ["leader"]
    assert _inflight == {}
