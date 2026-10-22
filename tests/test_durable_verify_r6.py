"""Regression tests for the round-4 and final checks of the durable fixes (VERIFY2-R6, VERIFY2-R7): the macOS watcher's
stream teardown has one owner (R6-N2), the loop's stream is recreated exactly as libuv recreates it (R6-N3, R7-N1..N3),
and ``exec`` holds the pipes open for a backpressured spill from the moment the child exists (the spill flake)."""

from __future__ import annotations

import asyncio
import collections
import os
import sys
import tempfile
import threading
import time
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.env import getOrThrow
from misaka.durable.env import node as envNode
from misaka.durable.env.node import NodeExecutionEnv

macOS = pytest.mark.skipif(sys.platform != "darwin", reason="FSEvents is macOS only")


def writeFile(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as file:
        file.write(content)


# --- R6-N2: closing every stream at exit while a rescheduling is queued ------------------------------------------

@macOS
async def test_r6_n2_closing_every_stream_while_a_rescheduling_is_queued_never_destroys_one_twice() -> None:
    from misaka.durable.env import node_watch

    directory = os.path.realpath(tempfile.mkdtemp(prefix="misaka-r6-race-"))
    fs = node_watch._FSEvents()
    import atexit

    atexit.unregister(fs._closeAll)
    destroyed: list[int] = []
    destroy = fs._destroyStream

    def counted(stream: Any) -> int:
        # Slow, so a second owner would come in while the first is still at it; never twice for real (that crashes).
        first = stream not in destroyed
        destroyed.append(stream)
        time.sleep(0.05)
        return destroy(stream) if first else 0

    fs._destroyStream = counted  # type: ignore[method-assign]
    perCycle = []
    for _ in range(10):
        destroyed.clear()
        close = fs.open(asyncio.get_running_loop(), directory, True, lambda name: None, lambda: None)
        deadline = time.monotonic() + 2
        while fs.openStreams() == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.002)
        # The close queues a rescheduling that destroys the stream; what atexit runs destroys every stream.
        close()
        await asyncio.sleep(0.01)
        fs._closeAll()
        fs.inert = False
        perCycle.append(len(destroyed))
    # One stream per cycle (a released stream's address may come back in a later cycle), destroyed once.
    assert perCycle == [1] * 10


# --- R6-N3 / R7-N1 / R7-N3: the stream is recreated exactly as libuv recreates it --------------------------------


@macOS
async def test_r7_n1_every_added_or_removed_watch_recreates_the_stream_from_now_old_one_first() -> None:
    """libuv's ``uv__fsevents_reschedule``: on every add or remove (even when the paths stay the same) the old stream is
    destroyed, then one is created over one path per watch from ``kFSEventStreamEventIdSinceNow``, with no deferral, file
    events and a 50 ms latency."""
    import atexit

    from misaka.durable.env import node_watch

    directory = os.path.realpath(tempfile.mkdtemp(prefix="misaka-r7-reschedule-"))
    fs = node_watch._FSEvents()
    atexit.unregister(fs._closeAll)
    steps: list[Any] = []
    create, destroy = fs.create, fs._destroyStream

    def created(*args: Any) -> Any:
        stream = create(*args)
        steps.append(("create", args[4], args[5], args[6], stream))
        return stream

    def destroyed(stream: Any) -> None:
        steps.append(("destroy", stream))
        destroy(stream)

    fs.create = created  # type: ignore[method-assign]
    fs._destroyStream = destroyed  # type: ignore[method-assign]
    loop = asyncio.get_running_loop()

    async def settled() -> None:
        # The rescheduling runs on the dispatch queue.
        await asyncio.to_thread(fs.dispatchSync, fs.queue, None, fs.workType(lambda _info: None))

    try:
        closeA = fs.open(loop, directory, True, lambda name: None, lambda: None)
        await settled()
        closeB = fs.open(loop, directory, False, lambda name: None, lambda: None)
        await settled()
        closeB()
        await settled()
        closeA()
        await settled()
    finally:
        fs._closeAll()
    kinds = [step[0] for step in steps]
    assert kinds == ["create", "destroy", "create", "destroy", "create", "destroy"]
    for step in steps:
        if step[0] == "create":
            assert step[1:4] == (0xFFFFFFFFFFFFFFFF, 0.05, 0x02 | 0x10)
    # Each destroy is of the stream created just before it.
    assert [step[1] for step in steps if step[0] == "destroy"] == [step[4] for step in steps if step[0] == "create"]


# --- R7-N2: a watch below a directory the stream covers gets each event once ----------------------------------------


_NESTED_WRITER = """
import os, sys, time
index = 0
while True:
    with open(os.path.join(sys.argv[1], f"f{index:05d}"), "w", encoding="utf-8") as file:
        file.write("x")
    index += 1
    time.sleep(0.005)
"""


@macOS
async def test_r7_n2_a_watch_below_a_covered_directory_gets_each_event_once() -> None:
    from misaka.durable.env import node_watch
    from misaka.durable.env.node_watch import _fsWatch

    base = os.path.realpath(tempfile.mkdtemp(prefix="misaka-r7-nested-"))
    sub = os.path.join(base, "sub")
    os.makedirs(sub)
    outer = _fsWatch(base, False, lambda name: None, lambda: None)
    fs = node_watch._FSEvents.instance
    assert fs is not None
    deadline = time.monotonic() + 2
    while fs.openStreams() == 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.3)
    # Another process writes, so busy threads here (the GIL) do not split a file's creation and its write into two
    # events.
    writer = await asyncio.create_subprocess_exec(sys.executable, "-c", _NESTED_WRITER, sub)
    names: collections.Counter[str | None] = collections.Counter()
    schedule = fs._schedule
    try:
        await asyncio.sleep(0.2)
        # The inner watch's rescheduling waits 0.3 s on a busy queue; meanwhile the stream gives it its events through
        # the outer watch's path, and the new stream starts from now.
        fs._schedule = lambda id_: threading.Timer(0.3, schedule, (id_,)).start()  # type: ignore[method-assign]
        inner = _fsWatch(sub, False, lambda name: names.update([name]), lambda: None)
        await asyncio.sleep(0.9)
        fs._schedule = schedule  # type: ignore[method-assign]
        writer.terminate()
        await writer.wait()
        await asyncio.sleep(0.3)
        inner.close()
    finally:
        fs._schedule = schedule  # type: ignore[method-assign]
        if writer.returncode is None:
            writer.kill()
            await writer.wait()
        outer.close()
    assert len(names) > 20
    # FSEvents may report one file's creation as two events (`Created`, then `Created|Modified`), and libuv, so Node,
    # reports both: a name or two twice is native. A replay through the covered path gave dozens (36–42).
    assert len([name for name, count in names.items() if count > 1]) < 5


@macOS
async def test_r6_n3_a_watch_is_not_given_what_happened_before_it_was_opened() -> None:
    from misaka.durable.env.node_watch import _fsWatch

    base = os.path.realpath(tempfile.mkdtemp(prefix="misaka-r6-history-"))
    first, second = os.path.join(base, "a"), os.path.join(base, "b")
    os.mkdir(first)
    os.mkdir(second)
    writeFile(os.path.join(first, "before-a"), "x")
    await asyncio.sleep(0.3)
    namesA: list[str | None] = []
    namesB: list[str | None] = []
    watchA = _fsWatch(first, False, namesA.append, lambda: None)
    try:
        await asyncio.sleep(0.4)
        # Written before `b` is watched; the stream recreated for `b` resumes from `a`'s last event.
        writeFile(os.path.join(second, "before-b"), "x")
        await asyncio.sleep(0.3)
        watchB = _fsWatch(second, False, namesB.append, lambda: None)
        try:
            await asyncio.sleep(0.4)
            writeFile(os.path.join(second, "after-b"), "x")
            writeFile(os.path.join(first, "after-a"), "x")
            deadline = time.monotonic() + 3
            while ("after-b" not in namesB or "after-a" not in namesA) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
        finally:
            watchB.close()
    finally:
        watchA.close()
    assert "before-a" not in namesA and "after-a" in namesA
    assert "before-b" not in namesB and "after-b" in namesB


# --- The spill-backpressure flake: the draining check exists before the child can exit -----------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="the command needs a POSIX shell with tr and a background job")
async def test_a_backpressured_spill_holds_the_pipes_open_even_when_the_loop_resumes_exec_late(monkeypatch: Any) -> None:
    """The child exits within a millisecond; the loop then takes 150 ms to resume ``exec`` after the subprocess was
    created (as under load or a collection pause). The 100 ms post-exit grace ends in between: the spill's draining
    check must already be there, or the pipes close while the spill is backpressured and the descendant's output,
    written later, is lost (``20 == 1020``)."""
    root = tempfile.mkdtemp(prefix="misaka-r6-spill-")
    writeChunk = envNode._writeSpillChunk

    def slowWrite(file: Any, chunk: bytes) -> None:
        time.sleep(0.6)
        writeChunk(file, chunk)

    monkeypatch.setattr(envNode, "SPILL_HIGH_WATER_MARK", 1)
    monkeypatch.setattr(envNode, "_writeSpillChunk", slowWrite)
    loop = asyncio.get_running_loop()
    create = loop.subprocess_exec

    async def late(*args: Any, **kwargs: Any) -> Any:
        created = await create(*args, **kwargs)
        await asyncio.sleep(0.15)
        return created

    monkeypatch.setattr(loop, "subprocess_exec", late)
    env = NodeExecutionEnv({"cwd": root})
    command = "printf '%020d' 0 | tr 0 a; (sleep 0.2; printf '%01000d' 0 | tr 0 b) &"
    output: list[str] = []
    result = getOrThrow(
        await env.exec(
            command,
            {"spill": {"afterBytes": 10, "afterLines": 10}, "onOutput": lambda text, *_: output.append(text)},
            BACKGROUND_CONTEXT,
        )
    )
    assert len("".join(output)) == 1020
    assert getOrThrow(await env.readTextFile(result["spillPath"], BACKGROUND_CONTEXT)) == "a" * 20 + "b" * 1000
