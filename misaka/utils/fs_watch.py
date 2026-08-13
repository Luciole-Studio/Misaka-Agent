"""Best-effort filesystem watch helpers."""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Protocol

FS_WATCH_RETRY_DELAY_MS = 5000
_POLL_INTERVAL_SECONDS = 0.1


class WatchListener(Protocol):
    def __call__(self, event_type: str, filename: str | None = None) -> Any: ...


class FSWatcher(Protocol):
    def close(self) -> None: ...


class _PollingWatcher:
    def __init__(self, path: str, listener: WatchListener, on_error: Any) -> None:
        self._path = path
        self._listener = listener
        self._on_error = on_error
        self._stop = threading.Event()
        self._snapshot = _snapshot(path)
        self._thread = threading.Thread(target=self._watch_loop, name="harn-fs-watch", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=0.5)

    def _watch_loop(self) -> None:
        while not self._stop.wait(_POLL_INTERVAL_SECONDS):
            try:
                current = _snapshot(self._path)
            except OSError:
                self._safe_on_error()
                return
            if current != self._snapshot:
                self._snapshot = current
                filename = Path(self._path).name or None
                try:
                    self._listener("change", filename)
                except Exception:
                    self._safe_on_error()
                    return

    def _safe_on_error(self) -> None:
        try:
            self._on_error()
        finally:
            self._stop.set()


def close_watcher(watcher: FSWatcher | None) -> None:
    if watcher is None:
        return
    try:
        watcher.close()
    except Exception:
        return


def watch_with_error_handler(path: str, listener: WatchListener, on_error: Any) -> FSWatcher | None:
    try:
        return _PollingWatcher(path, listener, on_error)
    except Exception:
        on_error()
        return None


def _snapshot(path: str) -> tuple[Any, ...]:
    stat = os.stat(path)
    base = (stat.st_mode, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if os.path.isdir(path):
        children = tuple(sorted(entry.name for entry in os.scandir(path)))
        return (*base, children)
    digest = hashlib.blake2b(Path(path).read_bytes(), digest_size=16).digest()
    return (*base, digest)


closeWatcher = close_watcher
watchWithErrorHandler = watch_with_error_handler

__all__ = [
    "FS_WATCH_RETRY_DELAY_MS",
    "closeWatcher",
    "watchWithErrorHandler",
]
