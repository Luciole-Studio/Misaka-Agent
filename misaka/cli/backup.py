"""Copies of the home taken before something could lose it: ``misaka update`` and ``misaka uninstall``.

Hermes takes the same two (the pre-update snapshot, and ``hermes backup`` before data is deleted):
before an update, a quick copy of what a person could not recreate, kept inside the home and
bounded in number; before the home is removed, one archive of all of it, kept outside. A SQLite
file is copied through SQLite's own backup, so a board another process is writing is copied
whole rather than torn.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from contextlib import closing
from pathlib import Path

from misaka.config import home

KEEP = 5                   # snapshots kept per reason; the oldest go first
LARGEST = 1 << 30          # a quick snapshot skips a file this large, as Hermes's does
# What an update snapshot holds: what a person wrote, or the program could not rebuild. Sessions
# stay out: they are append-only, and no update rewrites them.
SNAPSHOT = ("settings", "models", "keybindings", "env", "mcp_config", "credentials",
            "db", "messages_db", "spaces", "trust", "models_store")
# What the archive leaves out: rebuilt on demand, meaningless once processes exit, or copies already.
ARCHIVE_SKIPS = ("cache", "logs", "run", home.LAYOUT["backups"].rel)
_SIDECARS = ("-wal", "-shm", "-journal")


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if _is_sqlite(source):
        with closing(sqlite3.connect(source, timeout=30)) as live, closing(sqlite3.connect(target)) as copy:
            live.backup(copy)
    else:
        shutil.copy2(source, target)


def _roles() -> list[Path]:
    """Last Order's folder and every Sister's: each may hold its own settings.json, .env, mcp.json and MCP
    sign-ins."""
    return sorted(path for root in (home.path("roles_root"), home.path("profiles_root"))
                  if root.is_dir() for path in root.iterdir() if path.is_dir())


def snapshot(reason: str) -> Path:
    """Copy the home's settings, credentials and state databases to ``state/backups/<reason>-<stamp>/``.

    The newest ``KEEP`` snapshots for ``reason`` survive. Raises ``OSError`` or ``sqlite3.Error``;
    the caller decides whether a failed snapshot stops it."""
    root = home.home()
    shelf = home.path("backups")
    home.private_dir(shelf)          # the copies hold credentials
    target = shelf / f"{reason}-{time.strftime('%Y%m%d-%H%M%S')}"
    sources = [home.path(name) for name in SNAPSHOT]
    sources += [home.path(name, role) for role in _roles() for name in ("settings", "env", "mcp_config", "mcp_auth")]
    for source in sources:
        if source.is_dir():
            for here, _dirs, files in os.walk(source):
                for name in files:
                    if not name.endswith(_SIDECARS):
                        path = Path(here) / name
                        _copy(path, target / path.relative_to(root))
        elif source.is_file() and source.stat().st_size <= LARGEST:
            _copy(source, target / source.relative_to(root))
    for old in sorted(path for path in shelf.glob(f"{reason}-*") if path.is_dir())[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def archive(destination: Path) -> list[str]:
    """The home as one owner-only ``.tar.gz`` at ``destination``, less ``ARCHIVE_SKIPS``.

    Returns the files it could not read; they are listed rather than stopping the archive."""
    root = home.home()
    skipped: list[str] = []
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar, \
            tempfile.TemporaryDirectory() as scratch:
        for here, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs
                       if (Path(here) / name).relative_to(root).as_posix() not in ARCHIVE_SKIPS]
            for name in (*dirs, *files):
                path = Path(here) / name
                arcname = str(Path(home.DIR_NAME) / path.relative_to(root))
                if name.endswith(_SIDECARS):
                    continue
                try:
                    if name in files and not path.is_symlink() and _is_sqlite(path):
                        copy = Path(scratch) / "copy.db"
                        _copy(path, copy)
                        tar.add(copy, arcname)
                        copy.unlink()
                    else:
                        tar.add(path, arcname, recursive=False)
                except (OSError, sqlite3.Error):
                    skipped.append(str(path))
    return skipped


__all__ = ["archive", "snapshot"]
