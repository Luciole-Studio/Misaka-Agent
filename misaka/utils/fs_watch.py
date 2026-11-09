"""Translated from pi coding-agent's ``src/utils/fs-watch.ts`` (v1.1.0).

MISAKA fork: Python's standard library has no ``fs.watch`` or ``fs.watchFile``, so ``watch``, ``watchFile`` and
``unwatchFile`` below stand in for Node's. A daemon thread polls ``os.stat``; listeners run on the event loop that was
running when the watch was set up (on the polling thread when none was), as Node runs them on its loop. ``watch`` on a
directory reports the names of the entries that changed, as ``fs.watch`` does, and a failed poll is the watcher's
``"error"`` event. ``FSWatcher`` carries only what callers here use of Node's: ``close()``, ``on()``, ``emit()`` and
``listenerCount()``; there is no ``"close"`` event, ``off()`` or ``ref()``/``unref()``. With no loop, a listener that
raises is reported through ``sys.excepthook`` and the watch goes on. A poll compares stat results, so a rewrite that
keeps a file's size and lands within the file system's timestamp tick is not seen (Node's inotify-backed watch sees
it); git replaces the files watched here through a rename, which a poll does see.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

FS_WATCH_RETRY_DELAY_MS = 5000

# How often the `fs.watch` stand-in polls.
_WATCH_POLL_INTERVAL_MS = 100


class WatchListener(Protocol):
    def __call__(self, eventType: str, filename: str | None) -> Any: ...


def _runningLoop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _deliver(loop: asyncio.AbstractEventLoop | None, callback: Callable[[], None]) -> None:
    if loop is None:
        try:
            callback()
        except Exception:  # noqa: BLE001 - reported as uncaught; the poller keeps watching
            sys.excepthook(*sys.exc_info())
        return
    try:
        loop.call_soon_threadsafe(callback)
    except RuntimeError:
        # The loop has closed; nothing is listening any more.
        return


def _entryState(stat: os.stat_result, ino: int | None = None) -> tuple[int, int, int, int, int]:
    return (stat.st_mode, stat.st_ino if ino is None else ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _watchSnapshot(path: str) -> tuple[tuple[int, int, int, int, int], dict[str, tuple[int, int, int, int, int]] | None]:
    stat = os.stat(path)
    if not os.path.isdir(path):
        return _entryState(stat), None
    entries: dict[str, tuple[int, int, int, int, int]] = {}
    with os.scandir(path) as iterator:
        for entry in iterator:
            try:
                # `DirEntry.stat()` reports st_ino 0 on Windows; `inode()` gives the file index there.
                entries[entry.name] = _entryState(entry.stat(follow_symlinks=False), entry.inode())
            except FileNotFoundError:
                continue
    return _entryState(stat), entries


class FSWatcher:
    """Node's ``fs.FSWatcher``: ``close()`` and the ``"change"``/``"error"`` events of its EventEmitter."""

    def __init__(self, path: str, listener: WatchListener) -> None:
        self._path = path
        self._listeners: dict[str, list[Callable[..., Any]]] = {"change": [listener], "error": []}
        self._closed = False
        self._loop = _runningLoop()
        # `fs.watch` throws at once when the path cannot be watched.
        self._snapshot = _watchSnapshot(path)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, name="fs-watch", daemon=True)
        self._thread.start()

    def on(self, event: str, listener: Callable[..., Any]) -> FSWatcher:
        self._listeners.setdefault(event, []).append(listener)
        return self

    def listenerCount(self, event: str) -> int:
        return len(self._listeners.get(event, []))

    def emit(self, event: str, *args: Any) -> bool:
        listeners = list(self._listeners.get(event, []))
        for listener in listeners:
            listener(*args)
        return bool(listeners)

    def close(self) -> None:
        # Like Node's, this does not wait: the poller stops at its next wake, and `_closed` drops late events.
        self._closed = True
        self._stop.set()

    def _emitUnlessClosed(self, event: str, *args: Any) -> None:
        if not self._closed:
            self.emit(event, *args)

    def _poll(self) -> None:
        while not self._stop.wait(_WATCH_POLL_INTERVAL_MS / 1000):
            try:
                current = _watchSnapshot(self._path)
            except FileNotFoundError:
                current = None
            except OSError as error:
                _deliver(self._loop, lambda error=error: self._emitUnlessClosed("error", error))
                return
            previous = self._snapshot
            if current == previous:
                continue
            self._snapshot = current
            for eventType, filename in self._changes(previous, current):
                _deliver(
                    self._loop,
                    lambda eventType=eventType, filename=filename: self._emitUnlessClosed(
                        "change", eventType, filename
                    ),
                )

    def _changes(self, previous: Any, current: Any) -> list[tuple[str, str | None]]:
        name = os.path.basename(self._path) or None
        previousEntries = previous[1] if previous is not None else None
        currentEntries = current[1] if current is not None else None
        if previousEntries is not None and current is None:
            # The watched directory went away: each entry was removed, then the directory itself.
            return [*(("rename", entryName) for entryName in sorted(previousEntries)), ("rename", name)]
        if previousEntries is None or currentEntries is None:
            # A watched file changed, or the watched path appeared or went away.
            if previous is None or current is None or previous[0][1] != current[0][1]:
                return [("rename", name)]
            return [("change", name)]
        changes: list[tuple[str, str | None]] = []
        for entryName in sorted(previousEntries.keys() | currentEntries.keys()):
            before = previousEntries.get(entryName)
            after = currentEntries.get(entryName)
            if before == after:
                continue
            if before is None or after is None or before[1] != after[1]:
                changes.append(("rename", entryName))
            else:
                changes.append(("change", entryName))
        return changes


def watch(path: str, listener: WatchListener) -> FSWatcher:
    """``fs.watch(path, listener)``."""
    return FSWatcher(path, listener)


def closeWatcher(watcher: FSWatcher | None) -> None:
    if not watcher:
        return

    try:
        watcher.close()
    except Exception:  # noqa: BLE001 - ignore watcher close errors
        return


def watchWithErrorHandler(
    path: str,
    listener: WatchListener,
    onError: Callable[[], None],
) -> FSWatcher | None:
    try:
        watcher = watch(path, listener)
        watcher.on("error", lambda *_: onError())
        return watcher
    except Exception:  # noqa: BLE001 - any watcher failure is reported through onError
        onError()
        return None


@dataclass(frozen=True, slots=True)
class Stats:
    """The fields of Node's ``fs.Stats`` that a ``watchFile`` listener reads; zero for a missing file."""

    dev: int = 0
    ino: int = 0
    mode: int = 0
    size: int = 0
    mtimeMs: float = 0
    ctimeMs: float = 0


def _stats(path: str) -> Stats:
    try:
        stat = os.stat(path)
    except OSError:
        return Stats()
    return Stats(
        dev=stat.st_dev,
        ino=stat.st_ino,
        mode=stat.st_mode,
        size=stat.st_size,
        mtimeMs=stat.st_mtime_ns / 1_000_000,
        ctimeMs=stat.st_ctime_ns / 1_000_000,
    )


StatsListener = Callable[[Stats, Stats], Any]


class _StatWatcher:
    def __init__(self, path: str, intervalMs: float) -> None:
        self.path = path
        self.listeners: list[tuple[StatsListener, asyncio.AbstractEventLoop | None]] = []
        self._previous = _stats(path)
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._poll, args=(max(intervalMs, 1) / 1000,), name="fs-watch-file", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _isListening(self, listener: StatsListener) -> bool:
        # A listener removed by `unwatchFile` after its event was queued does not run.
        return not self._stop.is_set() and any(entry[0] is listener for entry in self.listeners)

    def _poll(self, interval: float) -> None:
        while not self._stop.wait(interval):
            current = _stats(self.path)
            previous = self._previous
            if current == previous:
                continue
            self._previous = current
            for listener, loop in list(self.listeners):
                _deliver(
                    loop,
                    lambda listener=listener, current=current, previous=previous: (
                        listener(current, previous) if self._isListening(listener) else None
                    ),
                )


_statWatchers: dict[str, _StatWatcher] = {}
_statWatchersLock = threading.Lock()


def watchFile(
    path: str, options: dict[str, Any] | StatsListener, listener: StatsListener | None = None
) -> None:
    """``fs.watchFile(path[, { interval }], listener)``: the first watch of a path sets its interval."""
    if callable(options):
        listener, options = options, {}
    if listener is None:
        raise TypeError('The "listener" argument must be of type function')
    key = os.path.abspath(path)
    with _statWatchersLock:
        watcher = _statWatchers.get(key)
        if watcher is None:
            watcher = _StatWatcher(key, float(options.get("interval", 5007)))
            _statWatchers[key] = watcher
        watcher.listeners.append((listener, _runningLoop()))


def unwatchFile(path: str, listener: StatsListener | None = None) -> None:
    """``fs.unwatchFile(path, listener?)``: without a listener every listener of the path is removed."""
    key = os.path.abspath(path)
    with _statWatchersLock:
        watcher = _statWatchers.get(key)
        if watcher is None:
            return
        if listener is None:
            watcher.listeners.clear()
        else:
            watcher.listeners = [entry for entry in watcher.listeners if entry[0] is not listener]
        if not watcher.listeners:
            watcher.stop()
            del _statWatchers[key]


__all__ = [
    "FS_WATCH_RETRY_DELAY_MS",
    "FSWatcher",
    "Stats",
    "WatchListener",
    "closeWatcher",
    "unwatchFile",
    "watch",
    "watchFile",
    "watchWithErrorHandler",
]
