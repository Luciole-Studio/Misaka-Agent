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
_REPORTED: set[tuple] = set()

IDENTITY_TOLERANCE_SECONDS = 5.0
_SELF_IDENTITY: str | None = None


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


def _identity_parts(value: str | None) -> tuple[str, str, float | None] | None:
    """``(host, pid, start)`` of an identity string; ``start`` is None when it is not a number
    (the ``ps lstart`` fallback), in which case only exact equality counts."""
    if not value:
        return None
    host, sep, rest = value.partition(":")
    pid, sep2, started = rest.partition(":")
    if not sep or not sep2:
        return None
    try:
        return host, pid, float(started)
    except ValueError:
        return host, pid, None


def same_identity(a: str | None, b: str | None, tolerance: float = IDENTITY_TOLERANCE_SECONDS) -> bool:
    """Whether two identity strings name the same process: equal, or the same host and PID with
    start times within ``tolerance`` (see IDENTITY_TOLERANCE_SECONDS)."""
    if a == b:
        return a is not None
    left, right = _identity_parts(a), _identity_parts(b)
    if left is None or right is None or left[:2] != right[:2]:
        return False
    if left[2] is None or right[2] is None:
        return False
    return abs(left[2] - right[2]) <= tolerance


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
        try:
            exists = psutil.pid_exists(int(pid))
        except ValueError:
            return False, f"pid {pid!r} is not a number"
        return exists, ("identity unreadable, pid exists" if exists else "pid gone")
    if not same_identity(current, expected):
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
    if not same_identity(current, _SELF_IDENTITY) and ("self-drift",) not in _REPORTED:
        _REPORTED.add(("self-drift",))
        _LOG.warning("process identity drifted inside this process: first %r, now %r "
                     "(every liveness verdict it makes is suspect; see runtime-bugs-2026-09-18 B20/B33)",
                     _SELF_IDENTITY, current)


def identity_is_alive(pid: int | None, expected: str | None) -> bool:
    alive, reason = explain_liveness(pid, expected)
    if not alive and reason not in ("pid gone", "no pid or no recorded identity"):
        key = (int(pid) if pid else None, reason.split(":")[0])
        if key not in _REPORTED:
            _REPORTED.add(key)
            _self_check()
            _LOG.warning("process %s judged not alive: %s", pid, reason)
    return alive


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(int(pgid), 0)  # windows-footgun: ok - POSIX only: callers return early elsewhere
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
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

    Windows has no process groups: the recorded leader is the process, and its tree is what the
    group stands for. Refusing there outright (as this did) left every card a Sister root had
    claimed on Windows unreclaimable once its process died (issue #10 plan, O1).
    """

    if os.name != "posix":
        return _terminate_recorded_tree(int(pgid), leader_identity)
    if int(pgid) <= 1:
        return not _group_exists(int(pgid))
    pgid = int(pgid)
    if pgid == os.getpgrp():
        return False
    current = identity(pgid)
    if current is not None and not same_identity(current, leader_identity):
        return True
    if current is None and psutil.pid_exists(pgid):
        return False
    if not _group_exists(pgid):
        return True

    for sig, deadline in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 3.0)):  # windows-footgun: ok - POSIX only: returned early above
        try:
            os.killpg(pgid, sig)  # windows-footgun: ok - POSIX only: returned early above
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


def _terminate_recorded_tree(pid: int, leader_identity: str) -> bool:
    """The non-POSIX half of :func:`terminate_orphaned_group`: now another process's PID is absent;
    the recorded process still running is terminated with its tree; gone, its children that outlived
    it are -- Windows has no process group to signal, and they keep the dead leader's PID as their
    parent's (a card's bash or tool processes would otherwise go on writing into the workspace while
    the card runs again)."""
    if pid <= 0:
        return False
    current = identity(pid)
    if current is not None and not same_identity(current, leader_identity):
        return True
    if pid == os.getpid():
        return False
    if current is None:
        if psutil.pid_exists(pid):
            return False
        for child in _outlived_children(pid, leader_identity):
            terminate(child.pid)
        return True
    terminate(pid)
    return not identity_is_alive(pid, leader_identity)


def _outlived_children(pid: int, leader_identity: str) -> list[psutil.Process]:
    """Processes whose parent is the dead ``pid`` and that started after it did: its own children.
    Without the leader's start time nothing can be told apart from an unrelated later process."""
    parts = _identity_parts(leader_identity)
    started = parts[2] if parts else None
    if started is None:
        return []
    found = []
    for process in psutil.process_iter(["ppid", "create_time"]):
        info = process.info
        if info.get("ppid") == pid and (info.get("create_time") or 0) >= started - IDENTITY_TOLERANCE_SECONDS:
            found.append(process)
    return found


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
            if abs(process.create_time() - item.created) <= IDENTITY_TOLERANCE_SECONDS:
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
    "IDENTITY_TOLERANCE_SECONDS",
    "ProcessToken",
    "identity",
    "identity_is_alive",
    "same_identity",
    "snapshot",
    "terminate",
    "terminate_orphaned_group",
]
