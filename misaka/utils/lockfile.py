"""A port of the npm package ``proper-lockfile`` (4.1.2), which pi's coding agent locks files with (here the MCP OAuth
extension, ``misaka/extensions/mcp/oauth.py``), together with the part of ``retry`` it relies on.

The lock is the directory ``<file>.lock``: ``mkdir`` is the atomic acquire, a held lock keeps the directory's modification
time fresh, and a lock whose directory has not been touched for ``stale`` milliseconds is taken over. ``lock(file,
options)`` resolves to an async function that releases the lock. ``options`` is a dict with ``stale`` (milliseconds, at
least 2000, default 10000), ``update`` (milliseconds between refreshes, between 1000 and ``stale / 2``), ``realpath``
(default ``True``; the file must then exist), ``retries`` (a count, or ``{"retries", "factor", "minTimeout",
"maxTimeout", "maxRetryTime"}``) and ``onCompromised`` (called with the error when the lock can no longer be kept).

Errors carry ``code``: ``ELOCKED`` while another holder owns the lock (the error ``retries`` gives up with),
``ECOMPROMISED`` for a lock that could not be kept fresh, ``ENOTACQUIRED`` for releasing a lock this process does not own.

proper-lockfile is Copyright (c) 2018 Made With MOXY Lda <hello@moxy.studio> and retry is Copyright (c) 2011 Tim
Koschützki and Felix Geisendörfer, both under the MIT License; the texts are ``LICENSES/proper-lockfile-and-retry.txt``
(indexed in THIRD_PARTY_NOTICES.md).
"""

from __future__ import annotations

import asyncio
import atexit
import math
import os
import random
import signal
import time
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from misaka.utils.js import jsRound


class LockError(Exception):
    def __init__(self, message: str, code: str, file: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.file = file


class _Lock:
    def __init__(self, lockfilePath: str, mtime: float, mtimePrecision: str, options: dict[str, Any]) -> None:
        self.lockfilePath = lockfilePath
        self.mtime = mtime
        self.mtimePrecision = mtimePrecision
        self.options = options
        self.lastUpdate = _now()
        self.updateTimeout: asyncio.TimerHandle | None = None
        # MISAKA fork: asyncio keeps only weak references to tasks, so the running refresh is held here.
        self.updateTask: asyncio.Task[None] | None = None
        self.updateDelay: float | None = None
        self.released = False


_locks: dict[str, _Lock] = {}
# The precision of this process's file system modification times, probed once: ``"s"`` or ``"ms"``.
_precision: list[str] = []


def _now() -> float:
    """``Date.now()``: milliseconds."""
    return time.time() * 1000


def _getLockFile(file: str, options: dict[str, Any]) -> str:
    return options.get("lockfilePath") or f"{file}.lock"


def _statMtime(path: str) -> float:
    return os.stat(path).st_mtime_ns // 1_000_000


def _probe(path: str) -> tuple[float, str]:
    """Find the precision of modification times (``mtime-precision.js``)."""
    if len(_precision) > 0:
        return _statMtime(path), _precision[0]
    # Set the time by ceiling now to seconds plus 5 ms, so that it is not on the second.
    mtime = math.ceil(_now() / 1000) * 1000 + 5
    os.utime(path, ns=(mtime * 1_000_000, mtime * 1_000_000))
    stat = _statMtime(path)
    precision = "s" if stat % 1000 == 0 else "ms"
    _precision.append(precision)
    return stat, precision


def _getMtime(precision: str) -> float:
    now = math.floor(_now())
    if precision == "s":
        now = math.ceil(now / 1000) * 1000
    return now


def _isLockStale(mtime: float, options: dict[str, Any]) -> bool:
    return mtime < _now() - options["stale"]


def _acquireLockSync(file: str, options: dict[str, Any], checkStale: bool) -> tuple[float, str]:
    lockfilePath = _getLockFile(file, options)
    try:
        # mkdir is atomic: it is the lock.
        os.mkdir(lockfilePath)
    except FileExistsError:
        pass
    else:
        try:
            return _probe(lockfilePath)
        except OSError:
            try:
                os.rmdir(lockfilePath)
            except OSError:
                pass
            raise
    # The lock directory exists: held, or stale.
    if not checkStale or options["stale"] <= 0:
        raise LockError("Lock file is already being held", "ELOCKED", file)
    try:
        mtime = _statMtime(lockfilePath)
    except FileNotFoundError:
        # Retry if the lockfile has been removed meanwhile; skip the stale check to avoid recursion.
        return _acquireLockSync(file, options, False)
    if not _isLockStale(mtime, options):
        raise LockError("Lock file is already being held", "ELOCKED", file)
    # A stale lock is removed, and acquiring is tried again.
    try:
        os.rmdir(lockfilePath)
    except FileNotFoundError:
        pass
    return _acquireLockSync(file, options, False)


def _updateLock(file: str, options: dict[str, Any]) -> None:
    lock = _locks[file]
    # Just for safety, should never happen.
    if lock.updateTimeout is not None:
        return
    lock.updateDelay = lock.updateDelay if lock.updateDelay else options["update"]
    loop = asyncio.get_running_loop()

    async def refresh() -> None:
        lock.updateTimeout = None
        try:
            # Stat the directory to check that its time is still ours.
            mtime = await asyncio.to_thread(_statMtime, lock.lockfilePath)
        except OSError as error:
            # MISAKA fork: a release that removed the directory while this stat ran is not a compromise.
            if lock.released:
                return
            isOverThreshold = lock.lastUpdate + options["stale"] < _now()
            # Keep trying unless the lock was deleted or the threshold is over.
            if isinstance(error, FileNotFoundError) or isOverThreshold:
                _setLockAsCompromised(file, lock, _compromised(error))
                return
            lock.updateDelay = 1000
            _updateLock(file, options)
            return
        if lock.mtime != mtime:
            _setLockAsCompromised(file, lock, LockError("Unable to update lock within the stale threshold", "ECOMPROMISED"))
            return
        newMtime = _getMtime(lock.mtimePrecision)
        try:
            await asyncio.to_thread(os.utime, lock.lockfilePath, ns=(int(newMtime) * 1_000_000, int(newMtime) * 1_000_000))
        except OSError as error:
            isOverThreshold = lock.lastUpdate + options["stale"] < _now()
            # Ignore if the lock was released.
            if lock.released:
                return
            if isinstance(error, FileNotFoundError) or isOverThreshold:
                _setLockAsCompromised(file, lock, _compromised(error))
                return
            lock.updateDelay = 1000
            _updateLock(file, options)
            return
        # Ignore if the lock was released.
        if lock.released:
            return
        # All ok, keep updating.
        lock.mtime = newMtime
        lock.lastUpdate = _now()
        lock.updateDelay = None
        _updateLock(file, options)

    def fire() -> None:
        lock.updateTask = loop.create_task(refresh())
        lock.updateTask.add_done_callback(_observe)

    lock.updateTimeout = loop.call_later(lock.updateDelay / 1000, fire)


def _observe(task: asyncio.Task[Any]) -> None:
    """An error out of ``onCompromised`` (by default the compromise itself) ends the process: Node reports an exception
    thrown in a callback and exits with code 1 after the exit handlers ran, and a process that lost its lock must not
    carry on as a second owner. asyncio would only log the error from a done callback, so the exit is raised instead
    (``SystemExit`` is the one error its callbacks let through)."""
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    traceback.print_exception(error)
    raise SystemExit(1)


def _compromised(error: OSError) -> LockError:
    compromised = LockError(str(error), "ECOMPROMISED")
    compromised.__cause__ = error
    return compromised


def _setLockAsCompromised(file: str, lock: _Lock, error: Exception) -> None:
    # Signal the lock has been released.
    lock.released = True
    if lock.updateTimeout is not None:
        lock.updateTimeout.cancel()
        lock.updateTimeout = None
    if _locks.get(file) is lock:
        del _locks[file]
    lock.options["onCompromised"](error)


def _timeouts(options: dict[str, Any] | list[Any]) -> list[float]:
    """``retry.timeouts``: the wait before each retry, in milliseconds."""
    if isinstance(options, list):
        return list(options)
    opts: dict[str, Any] = {"retries": 10, "factor": 2, "minTimeout": 1000, "maxTimeout": math.inf, "randomize": False}
    opts.update(options)
    if opts["minTimeout"] > opts["maxTimeout"]:
        raise ValueError("minTimeout is greater than maxTimeout")

    def createTimeout(attempt: int) -> float:
        rand = random.random() + 1 if opts["randomize"] else 1
        timeout = rand * opts["minTimeout"] * opts["factor"] ** attempt
        timeout = jsRound(timeout) if math.isfinite(timeout) else timeout
        return min(timeout, opts["maxTimeout"])

    timeouts: list[float] = []
    i = 0
    while i < opts["retries"]:
        timeouts.append(createTimeout(i))
        i += 1
    if options.get("forever") and not timeouts:
        timeouts.append(createTimeout(i))
    timeouts.sort()
    return timeouts


async def lock(file: str, options: dict[str, Any] | None = None) -> Callable[[], Awaitable[None]]:
    options = dict(options or {})
    retries = options.get("retries", 0)
    # An empty object or array is truthy in JavaScript, so it keeps the retry defaults.
    if not retries and not isinstance(retries, dict | list):
        retries = 0
    retryOptions = {"retries": retries} if isinstance(retries, int | float) else retries
    options.setdefault("onCompromised", _throw)
    options["stale"] = max(options.get("stale", 10000) or 0, 2000)
    options["update"] = options["stale"] / 2 if options.get("update") is None else options["update"] or 0
    options["update"] = max(min(options["update"], options["stale"] / 2), 1000)
    if options.get("realpath", True):
        file = await asyncio.to_thread(os.path.realpath, file, strict=True)
    else:
        file = os.path.abspath(file)

    # Attempt to acquire the lock, retrying while another holder owns it.
    timeouts = _timeouts(retryOptions)
    forever = not isinstance(retryOptions, list) and bool(retryOptions.get("forever"))
    cachedTimeouts = list(timeouts) if forever else None
    maxRetryTime = (retryOptions.get("maxRetryTime") if isinstance(retryOptions, dict) else None) or math.inf
    start = _now()
    errors: list[Exception] = []
    while True:
        try:
            mtime, precision = await asyncio.to_thread(_acquireLockSync, file, options, True)
            break
        except OSError as error:
            current: Exception = error
        except LockError as error:
            current = error
        if _now() - start >= maxRetryTime:
            errors.insert(0, Exception("RetryOperation timeout occurred"))
            raise _mainError(errors)
        errors.append(current)
        if len(timeouts) == 0:
            if cachedTimeouts is None:
                raise _mainError(errors)
            # Retry forever, only keep the last error.
            del errors[len(errors) - 1 :]
            timeouts = list(cachedTimeouts)
        await asyncio.sleep(timeouts.pop(0) / 1000)

    # We now own the lock.
    held = _Lock(_getLockFile(file, options), mtime, precision, options)
    _locks[file] = held
    hookSignals()
    # We must keep the lock fresh to avoid staleness.
    _updateLock(file, options)

    async def release() -> None:
        if held.released:
            raise LockError("Lock is already released", "ERELEASED")
        owned = _locks.get(file)
        # Skip if the lock is not acquired.
        if owned is None:
            raise LockError("Lock is not acquired/owned by you", "ENOTACQUIRED")
        if owned.updateTimeout is not None:
            owned.updateTimeout.cancel()
        owned.released = True
        del _locks[file]
        try:
            await asyncio.to_thread(os.rmdir, _getLockFile(file, owned.options))
        except FileNotFoundError:
            pass

    return release


def _mainError(errors: list[Exception]) -> Exception:
    """``RetryOperation.mainError()``: the error seen most often, the latest of equals."""
    counts: dict[str, int] = {}
    mainError: Exception | None = None
    mainCount = 0
    for error in errors:
        message = str(error)
        counts[message] = counts.get(message, 0) + 1
        if counts[message] >= mainCount:
            mainError, mainCount = error, counts[message]
    assert mainError is not None
    return mainError


def _throw(error: Exception) -> None:
    raise error


def _removeHeldLocks() -> None:
    """Held locks are released when the process exits."""
    for file, held in list(_locks.items()):
        try:
            os.rmdir(_getLockFile(file, held.options))
        except OSError:
            pass


atexit.register(_removeHeldLocks)

def _onSignal(signum: int, frame: Any) -> None:
    # ``signal-exit``: with no other handler for the signal an exit is coming, so the locks go first and the signal is
    # sent again with its default action restored.
    _removeHeldLocks()
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def hookSignals() -> None:
    """Remove the held locks when SIGTERM or SIGHUP ends the process, which skips ``atexit``. A signal that already has a
    handler is left to it (the server and the session worker close their locks themselves, and call this again once
    their handler has served its one signal)."""
    for name in ("SIGHUP", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            if signal.getsignal(number) is signal.SIG_DFL:
                signal.signal(number, _onSignal)
        except (ValueError, OSError):
            # Signal handlers can only be set from the main thread.
            return


__all__ = ["LockError", "hookSignals", "lock"]
