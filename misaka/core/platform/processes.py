"""PID-reuse-safe recursive process-tree termination."""

from __future__ import annotations

import errno
import logging
import os
import signal
import socket
import subprocess
import time
from dataclasses import dataclass

import psutil

_LOG = logging.getLogger(__name__)
_REPORTED: set[tuple] = set()      # liveness anomalies already logged by this process (one line each)
_SELF_IDENTITY: str | None = None  # what this process computed for itself the first time it checked


@dataclass(frozen=True, slots=True)
class ProcessToken:
    pid: int
    created: float


def identity(pid: int | None) -> str | None:
    """Return a host:PID:start-time identity so a reused PID is not mistaken for the original process."""

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


def explain_liveness(pid: int | None, expected: str | None) -> tuple[bool, str]:
    """``identity_is_alive`` with its reason: what the check saw, in words a log can carry.

    2026-09-18 (B20/B33): a three-hour-old daemon judged every session dead and a same-age
    driver's node processes found their own claims "superseded", while fresh processes
    computing the same identities agreed with each other. The verdicts left no trace of
    *why*, so the mechanism could not be pinned before the restart cleared it. Every
    verdict that is not "the PID is gone" now says what it compared.
    """
    if not pid or not expected:
        return False, "no pid or no recorded identity"
    current = identity(pid)
    if current is None:
        # An unreadable identity does not prove death; stay conservative while the PID exists.
        try:
            exists = psutil.pid_exists(int(pid))
        except ValueError:
            return False, f"pid {pid!r} is not a number"
        return exists, ("identity unreadable, pid exists" if exists else "pid gone")
    if current != expected:
        return False, f"identity mismatch: recorded {expected!r}, now {current!r}"
    try:
        status = psutil.Process(int(pid)).status()
    except psutil.NoSuchProcess:
        return False, "pid gone"
    except (psutil.AccessDenied, ValueError) as error:
        return False, f"status unreadable ({type(error).__name__}: {error})"
    if status in {psutil.STATUS_DEAD, psutil.STATUS_ZOMBIE}:
        return False, f"status {status}"
    return True, "alive"


def _self_check() -> None:
    """This process's own identity must not drift: if it does, every comparison it makes is suspect."""
    global _SELF_IDENTITY
    current = identity(os.getpid())
    if _SELF_IDENTITY is None:
        _SELF_IDENTITY = current
        return
    if current != _SELF_IDENTITY and ("self-drift",) not in _REPORTED:
        _REPORTED.add(("self-drift",))
        _LOG.warning("process identity drifted inside this process: first %r, now %r "
                     "(every liveness verdict it makes is suspect; see runtime-bugs-2026-09-18 B20/B33)",
                     _SELF_IDENTITY, current)


def identity_is_alive(pid: int | None, expected: str | None) -> bool:
    alive, reason = explain_liveness(pid, expected)
    if not alive and reason not in ("pid gone", "no pid or no recorded identity"):
        # A gone PID is the normal way a session ends; anything else is the anomaly to keep.
        key = (int(pid) if pid else None, reason.split(":")[0])
        if key not in _REPORTED:
            _REPORTED.add(key)
            _self_check()
            _LOG.warning("process %s judged not alive: %s", pid, reason)
    return alive


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

    A live process group keeps its numeric PGID reserved even after the leader
    has exited, so a missing leader plus an existing group identifies the old
    descendants without needing another database column.  Conversely, if the
    leader PID has already been reused by a process with a different identity,
    the old group is gone and the new process must never be signalled.

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
        # A live but unreadable leader cannot be safely identified as the orphan.
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


def terminate(
    pid: int, captured: list[ProcessToken] | None = None, *, reap_root: bool = True,
) -> None:
    """Suspend, then terminate a whole recursive tree.

    ``captured`` keeps independently-sessioned descendants addressable even if
    the root exits and the OS reparents them before cleanup begins.
    Set ``reap_root=False`` when asyncio owns the direct child: its watcher
    must be the only waiter, and the caller handles its wait/kill fallback.
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
    for process in processes:
        # Resume stopped processes so they can receive SIGTERM before the SIGKILL fallback.
        try:
            process.resume()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    waitable = processes if reap_root else [process for process in processes if process.pid != pid]
    _, alive = psutil.wait_procs(waitable, timeout=2)
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
