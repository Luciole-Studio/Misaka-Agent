"""PID-reuse-safe recursive process-tree termination."""

from __future__ import annotations

import errno
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True, slots=True)
class ProcessToken:
    pid: int
    created: float


def identity(pid: int | None) -> str | None:
    """Return a host/PID/start-time identity that survives PID reuse checks."""

    if not pid:
        return None
    host = socket.gethostname()
    try:
        process = psutil.Process(int(pid))
        return f"{host}:{int(pid)}:{process.create_time():.6f}"
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        pass
    try:
        started = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return f"{host}:{int(pid)}:{started}" if started else None


def identity_is_alive(pid: int | None, expected: str | None) -> bool:
    if not pid or not expected:
        return False
    current = identity(pid)
    if current is None:
        # 读不出身份 ≠ 死亡：ps 瞬时失败时若进程还在就保守判活，宁可这轮不回收
        try:
            return psutil.pid_exists(int(pid))
        except ValueError:
            return False
    if current != expected:
        return False
    try:
        return psutil.Process(int(pid)).status() not in {
            psutil.STATUS_DEAD,
            psutil.STATUS_ZOMBIE,
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(int(pgid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists, but proving it empty is impossible for this owner.
        return True
    except OSError as error:
        return error.errno != errno.ESRCH


def terminate_orphaned_group(pgid: int, leader_identity: str) -> bool:
    """Fence a dead process-group leader before its board lease is reclaimed.

    A live process group reserves its numeric PGID, even after the leader has
    exited.  Thus a missing leader plus an existing group identifies the old
    descendants without a new database column.  Conversely, if the leader PID
    has already been reused with a different identity, the old group is gone
    and the new process must never be signalled.

    Returns ``True`` only when the old group is proven absent.
    """

    if os.name != "posix" or int(pgid) <= 1:
        return not _group_exists(int(pgid)) if os.name == "posix" else False
    pgid = int(pgid)
    if pgid == os.getpgrp():
        return False
    current = identity(pgid)
    if current is not None and current != leader_identity:
        # PID reuse implies the prior PID/PGID namespace entry was released;
        # this is an unrelated new process and the old group is already empty.
        return True
    if current is None and psutil.pid_exists(pgid):
        # 领导进程还在但身份读不出（ps 瞬时失败）：无法证明是旧组，绝不盲杀
        return False
    if not _group_exists(pgid):
        return True

    for sig, deadline in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 3.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if not _group_exists(pgid):
                return True
            time.sleep(0.05)
    return not _group_exists(pgid)


def _token(process: psutil.Process) -> ProcessToken | None:
    try:
        return ProcessToken(process.pid, process.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def snapshot(pid: int) -> list[ProcessToken]:
    """Capture a root and every current descendant before reparenting."""

    try:
        root = psutil.Process(int(pid))
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return []
    result: list[ProcessToken] = []
    seen: set[int] = set()
    for process in processes:
        item = _token(process)
        if item and item.pid not in seen:
            seen.add(item.pid)
            result.append(item)
    return result


def _resolve(tokens: list[ProcessToken]) -> list[psutil.Process]:
    result: list[psutil.Process] = []
    for item in tokens:
        try:
            process = psutil.Process(item.pid)
            if abs(process.create_time() - item.created) < 0.001:
                result.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def terminate(pid: int, captured: list[ProcessToken] | None = None) -> None:
    """Suspend, then terminate a whole recursive tree.

    ``captured`` keeps independently-sessioned descendants addressable even if
    the root exits and the OS reparents them before cleanup begins.
    """

    tokens = list(captured or [])
    tokens.extend(snapshot(pid))
    unique = {item.pid: item for item in tokens}
    processes = _resolve(list(unique.values()))

    # Freeze known parents before signalling so they cannot fork an escaping
    # child in the terminate/kill gap.  Pull in one final descendant snapshot
    # after suspension to cover a child created during enumeration.
    for process in processes:
        try:
            process.suspend()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    for process in tuple(processes):
        try:
            descendants = process.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            descendants = []
        for descendant in descendants:
            item = _token(descendant)
            if item:
                unique[item.pid] = item
    processes = _resolve(list(unique.values()))

    for process in reversed(processes):
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(processes, timeout=2)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    psutil.wait_procs(alive, timeout=2)


__all__ = [
    "ProcessToken",
    "identity",
    "identity_is_alive",
    "snapshot",
    "terminate",
    "terminate_orphaned_group",
]
