"""CCB AgentMemorySnapshot and auto-memory gate, with MISAKA storage roots.

The host keeps bounded disk reads, role-owned directories and project trust.
Snapshots never follow symlinks or replace existing memory automatically.
"""
from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from filelock import ReadWriteLock, Timeout

from misaka.config import home
from misaka.core.subagent.background import _truthy
from misaka.utils import atomic


def memory_enabled(settings: dict[str, Any] | None = None) -> bool:
    from misaka.config.product import setting

    if not setting("subagents", "auto_memory", True, bool) or setting("subagents", "simple", False, bool):
        return False
    if _truthy(os.environ.get("MISAKA_REMOTE")) and not os.environ.get("MISAKA_REMOTE_MEMORY_DIR"):
        return False                      # a remote runner's hand-off: no memory dir, no memory
    return (settings or {}).get("autoMemoryEnabled", True) is not False


def snapshot_dir(project: Path, agent_type: str) -> Path | None:
    """Where a project keeps the shared memory snapshot of one agent type; none outside a project."""
    project_dir = home.project_dir(project)
    return project_dir / "agent-memory-snapshots" / agent_type if project_dir is not None else None


def _meta(path: Path, key: str) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = data.get(key) if isinstance(data, dict) else None
        return value if isinstance(value, str) and value else None
    except (OSError, ValueError, UnicodeError):
        return None


def _regular_files(path: Path) -> list[Path]:
    try:
        return [item for item in path.iterdir() if not item.is_symlink() and item.is_file()]
    except OSError:
        return []


def check_snapshot(snapshot: Path, local: Path) -> dict[str, str]:
    timestamp = _meta(snapshot / "snapshot.json", "updatedAt")
    if timestamp is None:
        return {"action": "none"}
    if not any(item.name.endswith(".md") for item in _regular_files(local)):
        return {"action": "initialize", "snapshotTimestamp": timestamp}
    synced = _meta(local / ".snapshot-synced.json", "syncedFrom")
    newer = False
    if synced:
        try:
            newer = datetime.fromisoformat(timestamp) > datetime.fromisoformat(synced)
        except (ValueError, TypeError):
            pass  # CCB Invalid Date comparisons are false.
    if synced is None or newer:
        return {"action": "prompt-update", "snapshotTimestamp": timestamp}
    return {"action": "none"}


def mark_synced(local: Path, timestamp: str) -> None:
    local.mkdir(parents=True, exist_ok=True)
    atomic.write_text(str(local / ".snapshot-synced.json"), json.dumps({"syncedFrom": timestamp}))


def activity_lock(local: Path) -> ReadWriteLock:
    """Shared worker leases / exclusive snapshot writes, across sessions/processes.

    Separate instances avoid same-process lock upgrades. SQLite releases leases
    on process exit; memory files and existing role/project roots stay unchanged.
    """
    path = local.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return ReadWriteLock(str(path) + ".activity.sqlite", is_singleton=False)


def acquire_activity(local: Path) -> ReadWriteLock:
    lock = activity_lock(local)
    try:
        lock.acquire_read(timeout=30)
        return lock
    except BaseException:
        lock.close()
        raise


def copy_snapshot(snapshot: Path, local: Path, timestamp: str, *, replace: bool = False) -> None:
    """Never replace a live worker's memory, including owners in other sessions."""
    if snapshot.is_symlink() or local.is_symlink():
        raise ValueError("Memory snapshot roots must not be symlinks")
    with closing(activity_lock(local)) as lock:
        try:
            lock.acquire_write(timeout=0)
        except Timeout as error:
            if replace:
                raise ValueError("An active agent or snapshot operation is using this memory directory; wait for it to finish") from error
            return  # Another owner initialized or is already using this memory.
        if not replace and any(item.name.endswith(".md") for item in _regular_files(local)):
            return  # Recheck under the lock, not just before it was acquired.
        _copy_snapshot(snapshot, local, timestamp, replace=replace)


def _copy_snapshot(snapshot: Path, local: Path, timestamp: str, *, replace: bool = False) -> None:
    """CCB initialize/replaceFromSnapshot; replacement is explicitly invoked only."""
    if snapshot.is_symlink() or local.is_symlink():
        raise ValueError("Memory snapshot roots must not be symlinks")
    # Read/validate the entire source first. A corrupt snapshot must never
    # delete existing memories before its content has even been decoded.
    contents = {source.name: source.read_text(encoding="utf-8")
                for source in snapshot.iterdir()
                if not source.is_symlink() and source.is_file() and source.name != "snapshot.json"}
    local.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        atomic.write_text(str(local / name), content)
    if replace:
        for path in _regular_files(local):
            if path.name.endswith(".md") and path.name not in contents:
                path.unlink()
    mark_synced(local, timestamp)
