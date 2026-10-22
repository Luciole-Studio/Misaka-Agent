"""``spawn_child_process``'s ``is_draining`` and ``on_spawn``: both are in place before the child's first chunk or its exit
is delivered, so a caller that resumes late (a loaded loop) still holds the pipes open and has its child at once."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

import pytest

from misaka.utils.child_process import ChildProcess, spawn_child_process


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell and a background job")
async def test_the_draining_check_and_the_child_are_in_place_before_the_child_can_exit(monkeypatch: Any) -> None:
    loop = asyncio.get_running_loop()
    create = loop.subprocess_exec

    async def late(*args: Any, **kwargs: Any) -> Any:
        created = await create(*args, **kwargs)
        # The child prints and exits meanwhile; the 100 ms post-exit grace starts before this call returns.
        await asyncio.sleep(0.25)
        return created

    monkeypatch.setattr(loop, "subprocess_exec", late)
    spawned: list[ChildProcess] = []
    chunks: list[tuple[int, bytes]] = []
    holdUntil = time.monotonic() + 0.6
    # The shell exits at once; a background `sleep` keeps its stdout open, so only the grace (or the check) ends the wait.
    child = await spawn_child_process(
        "/bin/sh",
        "-c",
        "echo x; sleep 1 &",
        cwd=os.getcwd(),
        on_data=lambda fd, data: chunks.append((len(spawned), data)),
        is_draining=lambda: time.monotonic() < holdUntil,
        on_spawn=spawned.append,
    )
    await child.wait()
    assert spawned == [child]
    # `on_spawn` ran before the first chunk was delivered.
    assert chunks[0][0] == 1
    # The check, in place from the start, held the pipes open past the grace that began before the call returned
    # (without it the wait ends 100 ms after the exit, while the call is still returning).
    assert time.monotonic() >= holdUntil


async def test_a_spawn_without_the_hooks_behaves_as_before() -> None:
    chunks: list[bytes] = []
    child = await spawn_child_process(
        sys.executable, "-c", "print('y', flush=True)", cwd=os.getcwd(), on_data=lambda fd, data: chunks.append(data)
    )
    assert await child.wait() == 0
    assert b"".join(chunks).strip() == b"y"


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX shell and a background job")
async def test_output_waiting_in_a_pipe_resumed_in_the_turn_the_grace_ends_is_still_read() -> None:
    """A consumer pauses the output (a backpressured spill), a descendant writes meanwhile, and the consumer resumes in
    the same loop turn as the post-exit grace ends. Node's paused stream had read those bytes already and emits them on
    the next tick, re-arming the grace; here they wait in the pipe, and must not be dropped by the closing pipes."""
    chunks: list[bytes] = []
    state = {"draining": True}
    child = await spawn_child_process(
        "/bin/sh",
        "-c",
        "echo a; (sleep 0.2; echo b) &",
        cwd=os.getcwd(),
        on_data=lambda fd, data: chunks.append(data),
        is_draining=lambda: state["draining"],
        on_spawn=lambda spawned: spawned.pause_output(),
    )
    # `b` waits in the paused pipe (`a` may have been read before `on_spawn` paused it: asyncio reads the pipes from
    # before `connection_made`); the grace keeps going while the consumer drains.
    await asyncio.sleep(0.5)
    state["draining"] = False
    child.resume_output()
    child._protocol._idle_expired()  # the grace ends in this same turn, before the pipe is polled again
    await asyncio.wait_for(child.wait(), 5)
    assert b"".join(chunks) == b"a\nb\n"
