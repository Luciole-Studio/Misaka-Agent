"""Project-owned, disposable LCM storage. Sessions remain the durable archive.

One process holds a shared lease until its project runtimes finish. A short gate
serializes lease admission and last-owner cleanup, including across processes.
Lock files live outside the disposable directory and contain no conversation data.
"""
from __future__ import annotations

import logging
import secrets
import shutil
import threading
from pathlib import Path

from filelock import FileLock, ReadWriteLock, Timeout

from misaka.utils.values import read_field

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_LEASES: dict[Path, ReadWriteLock] = {}
_MARKER = ".misaka-lcm-cache"


def project(ctx=None) -> Path:
    root = read_field(ctx, "lcm_project") or read_field(ctx, "cwd")
    return Path(root or Path.cwd()).expanduser().resolve()


class ProjectContext:
    """Keep project identity separate from a worker's execution directory."""

    def __init__(self, ctx, workspace):
        self._ctx = ctx
        self.lcm_project = str(Path(workspace).expanduser().resolve())

    def __getattr__(self, name):
        return getattr(self._ctx, name)


def context(ctx, workspace):
    # A resumed worktree/child retains its original project, even if its old
    # execution directory was temporary. The marker is native session metadata.
    manager = read_field(ctx, "sessionManager")
    entries = read_field(manager, "getEntries")
    if callable(entries):
        for entry in entries():
            if entry.get("type") == "custom" and entry.get("customType") == "lcm-project":
                workspace = entry["data"]["workspace"]
                break
    return ProjectContext(ctx, workspace)


def directory(workspace: Path) -> Path:
    return workspace / ".misaka" / "lcm"


def _paths(workspace):
    root = directory(workspace)
    paths = (root.parent, root, root.parent / "lcm.gate", root.parent / "lcm.activity.sqlite")
    if any(path.is_symlink() for path in paths):
        raise ValueError("MISAKA LCM storage and lock paths must not be symlinks")
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root, FileLock(paths[2], timeout=30, preserve_lock_file=True), paths[3]


def _discard(root):
    if root.is_symlink():
        raise ValueError("MISAKA LCM cache must not be a symlink")
    if root.exists():
        contents = list(root.iterdir())
        if not contents:
            root.rmdir()  # Interrupted initialization before the marker was written.
            return
        marker = root / _MARKER
        if marker.is_symlink() or not marker.is_file():
            raise ValueError(f"Not a MISAKA LCM cache; leave existing files untouched: {root}")
        content = marker.read_text()
        if content != "misaka-lcm\n" and not (content == "" and contents == [marker]):
            raise ValueError(f"Not a MISAKA LCM cache; leave existing files untouched: {root}")
        shutil.rmtree(root)


def acquire(workspace: Path) -> None:
    workspace = workspace.resolve()
    with _LOCK:
        if workspace in _LEASES:
            return
        root, gate, lock_path = _paths(workspace)
        lease = ReadWriteLock(lock_path, is_singleton=False)
        try:
            with gate:
                try:
                    lease.acquire_write(timeout=0)
                except Timeout:
                    pass  # Another live process owns this project's cache.
                else:
                    try:
                        _discard(root)  # Orphan from an interrupted previous run.
                        root.mkdir(mode=0o700)
                        (root / _MARKER).write_text("misaka-lcm\n")
                    finally:
                        lease.release()
                lease.acquire_read(timeout=30)
            _LEASES[workspace] = lease
        except BaseException:
            lease.close()
            raise


def release(workspace: Path) -> None:
    workspace = workspace.resolve()
    with _LOCK:
        lease = _LEASES.get(workspace)
        if lease is None:
            return
        root, gate, lock_path = _paths(workspace)
        with gate:
            lease.close()
            del _LEASES[workspace]
            cleanup = ReadWriteLock(lock_path, is_singleton=False)
            try:
                try:
                    cleanup.acquire_write(timeout=0)
                except Timeout:
                    return  # A sibling is still using it. Its exit owns cleanup.
                _discard(root)
            finally:
                cleanup.close()


def release_all() -> None:
    for workspace in list(_LEASES):
        try:
            release(workspace)
        except Exception:
            logger.exception("MISAKA LCM cleanup failed for %s; retry on next start", workspace)


def namespace_ids(connection) -> None:
    """Old tool-result handles must not silently select new cache rows/nodes.

    SQLite still allocates ordinary increasing integer IDs. The random starting
    range belongs to the cache generation and stays below JSON's exact-int limit.
    Use the engine's existing connection: opening/closing another connection to
    this file can release another thread's POSIX SQLite locks on macOS.
    """
    base = (1 << 32) + secrets.randbits(48)
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for table in ("messages", "summary_nodes"):
            connection.execute("INSERT INTO sqlite_sequence(name, seq) SELECT ?, ? "
                               "WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = ?)",
                               (table, base, table))
