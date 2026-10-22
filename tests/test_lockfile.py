"""The ``proper-lockfile`` / ``retry`` port (``misaka/utils/lockfile.py``)."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from misaka.utils import lockfile
from misaka.utils.lockfile import LockError, lock


def _target(tmp_path: Path) -> str:
    target = tmp_path / "resource"
    target.write_text("x", encoding="utf-8")
    return str(target)


def _age(lockDirectory: str, seconds: float) -> None:
    old = time.time() - seconds
    os.utime(lockDirectory, (old, old))


async def test_lock_is_a_directory_and_release_removes_it(tmp_path: Path) -> None:
    target = _target(tmp_path)
    release = await lock(target)
    assert os.path.isdir(os.path.realpath(target) + ".lock")
    await release()
    assert not os.path.exists(os.path.realpath(target) + ".lock")


async def test_second_lock_fails_with_elocked(tmp_path: Path) -> None:
    target = _target(tmp_path)
    release = await lock(target)
    try:
        with pytest.raises(LockError) as caught:
            await lock(target)
        assert caught.value.code == "ELOCKED"
    finally:
        await release()


async def test_default_stale_is_ten_seconds(tmp_path: Path) -> None:
    target = _target(tmp_path)
    held = await lock(target, {"update": 1000000})
    try:
        directory = os.path.realpath(target) + ".lock"
        # Five seconds old: stale at the 2000 ms minimum, held at the 10000 ms default.
        _age(directory, 5)
        with pytest.raises(LockError) as caught:
            await lock(target)
        assert caught.value.code == "ELOCKED"
        _age(directory, 12)
        takeover = await lock(target, {"onCompromised": lambda error: None})
        await takeover()
    finally:
        with pytest.raises(LockError):
            await held()


async def test_release_twice_is_erleased(tmp_path: Path) -> None:
    target = _target(tmp_path)
    release = await lock(target)
    await release()
    with pytest.raises(LockError) as caught:
        await release()
    assert caught.value.code == "ERELEASED"
    assert str(caught.value) == "Lock is already released"


async def test_old_release_does_not_free_a_newer_lock(tmp_path: Path) -> None:
    target = _target(tmp_path)
    first = await lock(target)
    await first()
    second = await lock(target)
    try:
        with pytest.raises(LockError) as caught:
            await first()
        assert caught.value.code == "ERELEASED"
        assert os.path.isdir(os.path.realpath(target) + ".lock")
    finally:
        await second()


async def test_missing_file_with_realpath_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await lock(str(tmp_path / "missing"))


async def test_realpath_false_locks_a_missing_file(tmp_path: Path) -> None:
    target = str(tmp_path / "directory")
    release = await lock(target, {"realpath": False})
    assert os.path.isdir(target + ".lock")
    await release()


async def test_retries_acquire_once_the_holder_releases(tmp_path: Path) -> None:
    target = _target(tmp_path)
    holder = await lock(target)
    asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(holder()))
    # maxRetryTime 0 means no limit.
    release = await lock(target, {"retries": {"retries": 3, "minTimeout": 100, "maxTimeout": 100, "maxRetryTime": 0}})
    await release()


async def test_empty_retries_object_keeps_the_defaults(tmp_path: Path) -> None:
    target = _target(tmp_path)
    holder = await lock(target)
    asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(holder()))
    # One second to the first retry (the default minTimeout).
    release = await lock(target, {"retries": {}})
    await release()


async def test_retries_give_up_with_the_lock_error(tmp_path: Path) -> None:
    target = _target(tmp_path)
    holder = await lock(target)
    try:
        with pytest.raises(LockError) as caught:
            await lock(target, {"retries": {"retries": 2, "minTimeout": 10, "maxTimeout": 10}})
        assert caught.value.code == "ELOCKED"
    finally:
        await holder()


async def test_max_retry_time_gives_up_early(tmp_path: Path) -> None:
    target = _target(tmp_path)
    holder = await lock(target)
    try:
        started = time.monotonic()
        with pytest.raises(LockError):
            await lock(target, {"retries": {"retries": 50, "minTimeout": 30, "maxTimeout": 30, "maxRetryTime": 100}})
        assert time.monotonic() - started < 2
    finally:
        await holder()


async def test_min_timeout_above_max_timeout_is_rejected(tmp_path: Path) -> None:
    target = _target(tmp_path)
    with pytest.raises(ValueError, match="minTimeout is greater than maxTimeout"):
        await lock(target, {"retries": {"retries": 3, "minTimeout": 200, "maxTimeout": 100}})


def test_timeouts_follow_retry() -> None:
    assert lockfile._timeouts({"retries": 4, "minTimeout": 100, "factor": 2}) == [100, 200, 400, 800]
    assert lockfile._timeouts({"retries": 4, "minTimeout": 100, "factor": 2, "maxTimeout": 300}) == [100, 200, 300, 300]
    # Math.round: halves go up.
    assert lockfile._timeouts({"retries": 1, "minTimeout": 0.5}) == [1]
    assert lockfile._timeouts({"retries": 0, "forever": True, "minTimeout": 100}) == [100]
    assert lockfile._timeouts([5, 10]) == [5, 10]
    randomized = lockfile._timeouts({"retries": 3, "minTimeout": 100, "factor": 1, "randomize": True})
    assert randomized == sorted(randomized)
    assert all(100 <= timeout <= 200 for timeout in randomized)
    assert len(lockfile._timeouts({})) == 10


async def test_a_compromised_lock_is_reported_to_on_compromised(tmp_path: Path) -> None:
    target = _target(tmp_path)
    reported: list[Exception] = []
    release = await lock(target, {"stale": 2000, "onCompromised": reported.append})
    os.rmdir(os.path.realpath(target) + ".lock")
    for _ in range(40):
        if reported:
            break
        await asyncio.sleep(0.1)
    assert len(reported) == 1
    assert isinstance(reported[0], LockError)
    assert reported[0].code == "ECOMPROMISED"
    with pytest.raises(LockError) as caught:
        await release()
    assert caught.value.code == "ERELEASED"


def _run(script: str, *args: str) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(script), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def test_the_default_on_compromised_ends_the_process(tmp_path: Path) -> None:
    target = _target(tmp_path)
    script = """
        import asyncio, os, sys
        from misaka.utils.lockfile import lock

        async def main():
            await lock(sys.argv[1], {"stale": 2000})
            os.rmdir(os.path.realpath(sys.argv[1]) + ".lock")
            await asyncio.sleep(30)

        asyncio.run(main())
    """
    child = _run(script, target)
    try:
        _, stderr = child.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        raise
    assert child.returncode == 1
    assert "LockError" in stderr


@pytest.mark.skipif(sys.platform == "win32", reason="signals")
@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
def test_a_terminating_signal_removes_the_held_locks(tmp_path: Path, name: str) -> None:
    target = _target(tmp_path)
    script = """
        import asyncio, sys
        from misaka.utils.lockfile import lock

        async def main():
            await lock(sys.argv[1])
            print("ready", flush=True)
            await asyncio.sleep(30)

        asyncio.run(main())
    """
    child = _run(script, target)
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        assert os.path.isdir(os.path.realpath(target) + ".lock")
        child.send_signal(getattr(signal, name))
        child.communicate(timeout=20)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert child.returncode == -getattr(signal, name)
    assert not os.path.exists(os.path.realpath(target) + ".lock")
