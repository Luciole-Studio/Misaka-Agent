"""pi chord's ``test/manager.test.ts``: the process lock and the host manager over real child processes.

MISAKA adaptations: the child is ``tests/fixtures/chord_worker.py``, a Python script the fork driver runs with
``sys.executable``; the driver's ``env`` carries ``PYTHONPATH`` (the repository root) so it imports ``misaka`` and
``tests``. Node's ``process.env.TMPDIR`` is ``os.environ`` (the lock reads it at every call, as ``os.tmpdir()`` does).
The fake driver's ``exited`` is a future that never settles. Process ids are probed with ``os.kill(pid, 0)``. The
manager needs POSIX descriptor passing, so the file is skipped on Windows.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.chord import createHost, ipcLink, isChordError
from misaka.chord.errors import ChordError
from misaka.chord.manager import (
    ControlMessage,
    createHostManager,
    forkDriver,
    processLock,
)
from tests.fixtures.chord_echo import Echo

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the fork driver passes a descriptor (POSIX)")

ROOT = Path(__file__).resolve().parents[1]
entry = str(Path(__file__).resolve().parent / "fixtures" / "chord_worker.py")


def scratch(name: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"chord2-{os.getpid()}-{name}")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def driver(env: dict[str, str] | None = None, lock: str | None = None) -> Any:
    options: dict[str, Any] = {
        "entry": entry,
        "env": {"HOST_NAME": "w1", "PYTHONPATH": str(ROOT), **(env or {})},
        "init": {"greeting": "hi"},
    }
    if lock is not None:
        options["lock"] = lock
    return forkDriver(options)


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


# ─── processLock ───


async def test_admits_one_holder_when_the_temporary_directory_is_too_deep_for_a_socket_path():
    # A socket path over 104 bytes failed with EINVAL on macOS when TMPDIR was deep.
    deep = os.path.join(scratch("deep"), "a" * 60, "b" * 60)
    os.makedirs(deep, exist_ok=True)
    previous = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = deep
    try:
        name = f"deep-{os.getpid()}"
        release = await processLock(name)
        with pytest.raises(ChordError) as rejected:
            await processLock(name)
        assert isChordError(rejected.value) and rejected.value.reason == "locked"
        await release()
        await (await processLock(name))()
    finally:
        if previous is None:
            del os.environ["TMPDIR"]
        else:
            os.environ["TMPDIR"] = previous
        shutil.rmtree(scratch("deep"), ignore_errors=True)


# ─── host manager (real child processes) ───


async def test_a_child_without_a_lock_does_not_inherit_the_parents_lock():
    # A child host that forks its own children used to pass its CHORD2_LOCK on, so they failed with "locked".
    name = f"chord2-test-{os.getpid()}-inherited"
    release = await processLock(name)
    previous = os.environ.get("CHORD2_LOCK")
    os.environ["CHORD2_LOCK"] = name
    manager = createHostManager({"driver": driver()})
    try:
        await manager.start()
        assert manager.status == "running"
    finally:
        if previous is None:
            del os.environ["CHORD2_LOCK"]
        else:
            os.environ["CHORD2_LOCK"] = previous
        await manager.stop()
        await release()


async def test_starts_a_child_host_links_it_stops_it_gracefully():
    manager = createHostManager({"driver": driver()})
    instance = await manager.start()
    assert manager.status == "running"
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    server.accept(instance.link, {"principal": {"worker": "w1"}, "kind": "host", "name": "w1"})
    echo = server.hold(Echo, {})
    assert await echo.echo("x") == f"hi x from {instance.pid}"
    assert await manager.start() is instance
    await manager.stop()
    assert manager.status == "stopped"
    assert await instance.exited == {"code": 0, "signal": None}
    await server.close()


async def test_a_child_owned_kernel_lock_admits_one_live_process_replace_waits_for_the_exit_before_the_next_start():
    lock = f"chord2-test-{os.getpid()}-lock"
    pids: list[int] = []
    base = driver({}, lock)

    def spawn() -> Any:
        # Exit before replace: the previous child is gone when the next one spawns.
        for pid in pids:
            assert not alive(pid), f"pid {pid} still alive"
        instance = base.spawn()
        pids.append(instance.pid)
        return instance

    first = createHostManager({"driver": SimpleNamespace(spawn=spawn)})
    second = createHostManager({"driver": driver({}, lock)})
    a = await first.start()
    # Both managers race for the same lock; only the kernel decides, so the second child cannot start.
    with pytest.raises(ChordError) as rejected:
        await second.start()
    assert isChordError(rejected.value, "unavailable") and rejected.value.reason == "locked"
    b = await first.replace()
    assert a.pid != b.pid
    await first.stop()
    await second.start()
    await second.stop()


async def test_a_graceful_exit_still_takes_the_childs_descendants_with_it():
    file = scratch("grandchild-graceful")
    Path(file).unlink(missing_ok=True)
    manager = createHostManager({"driver": driver({"GRANDCHILD_FILE": file})})
    await manager.start()
    grandchild = int(Path(file).read_text(encoding="utf-8"))
    assert alive(grandchild)
    await manager.stop()
    for _ in range(50):
        if not alive(grandchild):
            break
        await sleep(20)
    assert not alive(grandchild)
    Path(file).unlink(missing_ok=True)


async def test_idlefor_counts_from_the_busy_idle_transition():
    listeners: list[Callable[[ControlMessage], None]] = []
    never: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    channel = SimpleNamespace(connected=False, send=lambda *_: False, on=lambda *_: None, off=lambda *_: None)
    fake = SimpleNamespace(
        spawn=lambda: SimpleNamespace(
            pid=0,
            link=ipcLink(channel),
            exited=never,
            send=lambda _message: None,
            killTree=lambda: None,
            onControl=listeners.append,
        )
    )

    def emit(message: ControlMessage) -> None:
        for listener in listeners:
            listener(message)

    manager = createHostManager({"driver": fake})
    started = manager.start()
    for _ in range(20):
        if len(listeners) > 0:
            break
        await asyncio.sleep(0.001)
    emit({"type": "ready"})
    await started
    emit({"type": "status", "activeCalls": 1})
    await sleep(120)
    assert manager.liveness["idleFor"] == 0
    emit({"type": "status", "activeCalls": 0})
    assert manager.liveness["idleFor"] < 60, f"idleFor {manager.liveness['idleFor']}"


async def test_kills_the_process_tree_when_a_child_ignores_shutdown_past_the_grace_period():
    file = scratch("grandchild")
    Path(file).unlink(missing_ok=True)
    manager = createHostManager(
        {"driver": driver({"FIXTURE_MODE": "ignore-shutdown", "GRANDCHILD_FILE": file}), "grace": 300}
    )
    instance = await manager.start()
    grandchild = int(Path(file).read_text(encoding="utf-8"))
    assert alive(grandchild)
    await manager.stop()
    assert (await instance.exited)["signal"] == "SIGKILL"
    for _ in range(50):
        if not alive(grandchild):
            break
        await sleep(20)
    assert not alive(grandchild)
    Path(file).unlink(missing_ok=True)


async def test_reports_a_fatal_child_as_a_failed_start():
    manager = createHostManager({"driver": driver({"FIXTURE_MODE": "fatal"})})
    with pytest.raises(ChordError, match="fixture refuses to start"):
        await manager.start()
    assert manager.status == "stopped"


async def test_stops_an_idle_child_when_keepalive_says_so():
    exits: list[Any] = []
    manager = createHostManager(
        {
            "driver": driver(),
            "keepAlive": lambda liveness: not liveness["booting"] and liveness["idleFor"] < 150,
            "checkInterval": 50,
            "onExit": exits.append,
        }
    )
    await manager.start()
    for _ in range(100):
        if manager.status == "stopped":
            break
        await sleep(20)
    assert manager.status == "stopped"
    assert len(exits) == 1
