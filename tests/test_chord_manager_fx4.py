"""Regression tests for the host manager's verification findings (VERIFY-V4-chord, fixed in FX4), each checked against
pi chord v1.3.0's ``src/manager.ts`` run under Node: a caller that stops waiting does not wedge the manager (V4-B12),
a step starts a turn after the call (V4-B33), a child's exit needs no executor thread (V4-B14), a signal Python
cannot name still settles the exit (V4-B30), a released lock file is unlinked before its socket closes (V4-B31),
output pipes close at their end and mirror into a text-only stream (V4-B32), the channel descriptor's variable is
not passed on (V4-O4), a failing shutdown handler surfaces (V4-O5), and ``None`` options take their defaults (V4-B16).

The manager needs POSIX descriptor passing, so the file is skipped on Windows (as ``test_chord_manager.py`` is).
"""

from __future__ import annotations

import asyncio
import errno
import gc
import io
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.chord import manager as managerModule
from misaka.chord.manager import (
    ControlMessage,
    childControl,
    createHostManager,
    forkDriver,
    processLock,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fork driver passes a descriptor (POSIX)")

ROOT = Path(__file__).resolve().parents[1]
probe = str(Path(__file__).resolve().parent / "fixtures" / "chord_probe_child.py")


class FakeInstance:
    """A child that never starts by itself: the test emits its control messages; a shutdown exits it after
    ``exitAfter`` seconds (None: never)."""

    def __init__(self, exitAfter: float | None = 0) -> None:
        self.pid = 0
        self.link = None
        self.exited: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.listeners: list[Callable[[ControlMessage], None]] = []
        self.kills = 0
        self._exitAfter = exitAfter

    def _exit(self, code: int | None, signalName: str | None) -> None:
        if not self.exited.done():
            self.exited.set_result({"code": code, "signal": signalName})

    def send(self, message: ControlMessage) -> None:
        if message["type"] == "shutdown" and self._exitAfter is not None:
            asyncio.get_running_loop().call_later(self._exitAfter, self._exit, 0, None)

    def killTree(self) -> None:
        self.kills += 1
        self._exit(None, "SIGKILL")

    def onControl(self, listener: Callable[[ControlMessage], None]) -> None:
        self.listeners.append(listener)

    def emit(self, message: ControlMessage) -> None:
        for listener in self.listeners:
            listener(message)


class FailingDriver:
    def spawn(self) -> Any:
        raise OSError("spawn")


class FakeDriver:
    def __init__(self, exitAfter: float | None = 0) -> None:
        self.spawned: list[FakeInstance] = []
        self._exitAfter = exitAfter

    def spawn(self) -> FakeInstance:
        instance = FakeInstance(self._exitAfter)
        self.spawned.append(instance)
        return instance


async def turns(count: int = 5) -> None:
    for _ in range(count):
        await asyncio.sleep(0)


async def startReady(manager: Any, driver: FakeDriver) -> Any:
    started = manager.start()
    count = len(driver.spawned)
    for _ in range(100):
        if len(driver.spawned) > count and driver.spawned[-1].listeners:
            break
        await asyncio.sleep(0.001)
    driver.spawned[-1].emit({"type": "ready"})
    return await started


async def until(predicate: Callable[[], bool], seconds: float = 5) -> bool:
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# ─── HostManager: serialized steps (V4-B12, V4-B33) ───


async def test_a_caller_that_stops_waiting_leaves_the_step_running_and_the_next_steps_queued_behind_it():
    # Upstream: a promise cannot be cancelled, and `queue = next.catch(() => {})` never fails, so after
    # `Promise.race([manager.start(), timeout])` the start still kills its unready child at readyTimeout, then the stop
    # runs, then a new start works. A cancelled step used to fail every later step with CancelledError.
    driver = FakeDriver()
    manager = createHostManager({"driver": driver, "readyTimeout": 300})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(manager.start(), 0.05)
    assert manager.status == "starting"
    assert driver.spawned[0].kills == 0
    await asyncio.wait_for(manager.stop(), 5)
    assert manager.status == "stopped"
    assert driver.spawned[0].kills == 1  # the start's own ready timeout, not the caller's
    assert await startReady(manager, driver) is driver.spawned[1]
    assert manager.status == "running"
    await manager.stop()


async def test_a_step_runs_a_turn_after_the_call():
    # `Promise.resolve().then(step)`: `start()` returns before the step sets `starting`.
    driver = FakeDriver()
    manager = createHostManager({"driver": driver})
    started = manager.start()
    assert manager.status == "stopped" and driver.spawned == []
    await turns(2)
    assert manager.status == "starting"
    driver.spawned[0].emit({"type": "ready"})
    await started
    stopped = manager.stop()
    assert manager.status == "running"
    await stopped
    assert manager.status == "stopped"


async def test_an_ignored_step_failure_is_not_reported():
    # `void serialized(doStop)` and an unawaited `start()` are handled by the queue's `.catch`.
    loop = asyncio.get_running_loop()
    reported: list[Any] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        manager = createHostManager({"driver": FailingDriver()})
        manager.start()
        with pytest.raises(OSError, match="spawn"):
            await manager.start()
        await turns()
        gc.collect()
        await turns()
        assert reported == []
    finally:
        loop.set_exception_handler(previous)


# ─── HostManager: `??` defaults (V4-B16) ───


async def test_none_options_take_their_defaults():
    # `options.readyTimeout ?? 30_000` etc.: an explicit null is the default, not a 1 ms timer (or a crash).
    driver = FakeDriver(exitAfter=0.02)
    manager = createHostManager(
        {
            "driver": driver,
            "readyTimeout": None,
            "grace": None,
            "checkInterval": None,
            "keepAlive": lambda _liveness: True,
        }
    )
    started = manager.start()
    await asyncio.sleep(0.05)
    driver.spawned[0].emit({"type": "ready"})
    await started
    assert manager.status == "running"
    driver.spawned[0].emit({"type": "status", "activeCalls": None, "viewers": 2, "durableTasks": None})
    assert manager.liveness["activeCalls"] == 0 and manager.liveness["viewers"] == 2
    await manager.stop()
    assert driver.spawned[0].kills == 0  # it exited within the default grace


# ─── fork driver (real child processes) ───


def test_a_childs_exit_is_observed_without_holding_an_executor_thread():
    # Each child's exit used to be awaited with `asyncio.to_thread(process.wait)`: one default-executor thread per live
    # child, so past the executor's size later exits went unobserved and every other `to_thread` call stalled.
    async def scenario() -> None:
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=1))
        sleeper = forkDriver({"entry": probe, "env": {"CHILD_MODE": "sleep"}})
        instances = [sleeper.spawn() for _ in range(2)]
        try:
            instances[1].killTree()
            assert await asyncio.wait_for(asyncio.shield(instances[1].exited), 10) == {
                "code": None,
                "signal": "SIGKILL",
            }
            assert await asyncio.wait_for(asyncio.to_thread(lambda: 42), 10) == 42
        finally:
            for instance in instances:
                instance.killTree()
            await asyncio.wait_for(asyncio.gather(*(instance.exited for instance in instances)), 10)

    asyncio.run(scenario())


def _unnamedSignal() -> int:
    named = {int(member) for member in signal.Signals}
    return next(number for number in range(1, 128) if number not in named)


@pytest.mark.parametrize(
    ("returncode", "exit"),
    [
        (-_unnamedSignal(), {"code": 0, "signal": None}),
        pytest.param(-getattr(signal, "SIGKILL", 9), {"code": None, "signal": "SIGKILL"},
                     marks=pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="no SIGKILL on Windows")),
        (3, {"code": 3, "signal": None}),
    ],
)
async def test_the_exit_names_the_signal_as_node_does(monkeypatch, returncode, exit):
    # Node reports a signal its table has no name for (a Linux real-time one) as no signal, with libuv's exit code 0;
    # `signal.Signals(n)` used to raise there, so `exited` never settled and `stop()` hung.
    class Popen:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.pid = 0
            self.stdout = self.stderr = None

        def wait(self) -> int:
            return returncode

        def kill(self) -> None:
            pass

    monkeypatch.setattr(
        managerModule, "subprocess", SimpleNamespace(Popen=Popen, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL)
    )
    monkeypatch.setattr(managerModule, "_killGroup", lambda _pid: None)
    instance = forkDriver({"entry": probe}).spawn()
    assert await asyncio.wait_for(asyncio.shield(instance.exited), 5) == exit


def openDescriptors() -> int:
    return len(os.listdir("/dev/fd"))


async def test_output_pipes_close_at_their_end():
    before = openDescriptors()
    chunks: list[bytes] = []
    instance = forkDriver(
        {"entry": probe, "env": {"CHILD_MODE": "print"}, "onOutput": lambda _name, chunk: chunks.append(chunk)}
    ).spawn()
    assert await asyncio.wait_for(asyncio.shield(instance.exited), 10) == {"code": 0, "signal": None}
    assert await until(lambda: openDescriptors() == before), f"{openDescriptors() - before} descriptors left open"
    assert b"".join(chunks) == "héllo\n".encode()


async def test_output_mirrors_into_a_stream_without_a_byte_buffer(monkeypatch):
    # `process.stdout.write(chunk)` takes bytes; a text-only `sys.stdout` used to fail the mirror (AttributeError).
    mirror = io.StringIO()
    monkeypatch.setattr(sys, "stdout", mirror)
    instance = forkDriver({"entry": probe, "env": {"CHILD_MODE": "print"}, "onOutput": lambda *_: None}).spawn()
    await asyncio.wait_for(asyncio.shield(instance.exited), 10)
    assert await until(lambda: mirror.getvalue() == "héllo\n"), repr(mirror.getvalue())


async def test_the_child_does_not_pass_its_channel_descriptor_on():
    # Node deletes NODE_CHANNEL_FD from the child's environment; CHORD2_IPC_FD used to stay, so a grandchild calling
    # `childControl()` adopted a descriptor it does not have (EBADF) or an unrelated one.
    out: list[bytes] = []
    instance = forkDriver(
        {
            "entry": probe,
            "env": {"CHILD_MODE": "env", "PYTHONPATH": str(ROOT)},
            "onOutput": lambda name, chunk: out.append(chunk) if name == "stdout" else None,
        }
    ).spawn()
    assert await asyncio.wait_for(asyncio.shield(instance.exited), 20) == {"code": 0, "signal": None}
    assert await until(lambda: b"\n" in b"".join(out))
    assert json.loads(b"".join(out)) == {"inEnv": False, "grandchild": "None"}


# ─── process lock (V4-B31) ───


@pytest.mark.skipif(
    sys.platform.startswith("linux") or hasattr(sys, "getandroidapilevel"),
    reason="the lock is an abstract socket here, with no file",
)
async def test_a_released_lock_unlinks_its_socket_file_while_it_still_accepts(monkeypatch):
    # libuv unlinks a pipe server's file before closing its descriptor; closing first left a window in which another
    # process found the file unanswered, bound its own socket there, and lost it to our unlink.
    seen: list[str | None] = []
    realUnlink = os.unlink

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if os.path.basename(str(path)).startswith("chord2-"):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(str(path))
                seen.append("accepting")
            except OSError as error:
                seen.append(errno.errorcode.get(error.errno or 0))
            finally:
                client.close()
        realUnlink(path, *args, **kwargs)

    release = await processLock(f"chord2-test-{os.getpid()}-release-order")
    monkeypatch.setattr(os, "unlink", unlink)
    await release()
    assert seen == ["accepting"]


# ─── childControl (V4-O5) ───


def fakeChannel() -> tuple[Any, Callable[[Any], None]]:
    listeners: list[Callable[[Any], None]] = []
    channel = SimpleNamespace(
        connected=True,
        send=lambda *_args: True,
        on=lambda _event, listener: listeners.append(listener),
    )

    def emit(message: Any) -> None:
        for listener in listeners:
            listener(message)

    return channel, emit


async def test_a_shutdown_handler_that_rejects_is_reported_as_unhandled():
    # `void handler()`: Node reports the unhandled rejection; it used to vanish. Handlers are a set.
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        channel, emit = fakeChannel()
        control = childControl(channel)
        runs: list[str] = []

        async def rejects() -> None:
            runs.append("rejects")
            await asyncio.sleep(0)
            raise RuntimeError("cleanup failed")

        control.onShutdown(rejects)
        control.onShutdown(rejects)
        emit({"chord2control": {"type": "shutdown"}})
        await turns()
        assert runs == ["rejects"]
        assert [str(context.get("exception")) for context in reported] == ["cleanup failed"]
    finally:
        loop.set_exception_handler(previous)


async def test_a_shutdown_handler_that_throws_throws_out_of_the_message_listener():
    channel, emit = fakeChannel()
    control = childControl(channel)

    def throws() -> None:
        raise RuntimeError("sync cleanup failed")

    control.onShutdown(throws)
    with pytest.raises(RuntimeError, match="sync cleanup failed"):
        emit({"chord2control": {"type": "shutdown"}})
