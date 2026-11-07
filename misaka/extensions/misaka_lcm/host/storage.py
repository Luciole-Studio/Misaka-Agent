"""Project-owned LCM storage. Sessions remain the durable archive.

The cache outlives the processes that use it, so what one session stored can still be found after
every process has exited, and a numeric handle stays good. It stays disposable: nothing in it is
the only copy, ``clear`` removes it, and a session whose rows drifted from its file is rebuilt
(``context_engine._messages``). One process holds a shared lease until its project runtimes finish;
only ``clear`` and the recovery of an unreadable database take the exclusive one. A short gate
serializes admission. Lock files live outside the directory and contain no conversation data.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

from filelock import FileLock, ReadWriteLock, Timeout

from misaka.config import home
from misaka.utils.values import read_field

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_LEASES: dict[Path, ReadWriteLock] = {}
_IDENTITY: dict[Path, tuple | None] = {}
_PRIVATE: dict[Path, bool] = {}
_FIRST_OWNER: set[Path] = set()
_MARKER = ".misaka-lcm-cache"
_STAMP = "misaka-lcm 3\n"             # 2: images stay in the session file (`images`); 3: rows say who spoke (`ingest.archive_role`)
_OUTDATED = ("misaka-lcm\n", "misaka-lcm 2\n")
# A subagent's copy of this plugin joins the project store of the session that started it: the
# parent sets this for the child on ``subagent_start`` (see ``extension.register``).
PROJECT_ENV = "MISAKA_LCM_PROJECT"


def project(ctx=None) -> Path:
    root = read_field(ctx, "lcm_project") or read_field(ctx, "cwd")
    return Path(root or Path.cwd()).expanduser().resolve()


def source(ctx=None) -> str:
    """The session's address, which is what LCM stores as each row's ``source`` (upstream's
    "which platform"): ``last-order``, a Sister's name, ``momo@t_3fa9c1`` for her card session
    (``misaka_lcm.identity``). ``misaka`` where no session owns the engine: the operator CLI."""
    return read_field(ctx, "lcm_source") or "misaka"


class ProjectContext:
    """Keep project identity separate from a worker's execution directory, and carry the
    session's own address with it."""

    def __init__(self, ctx, workspace, source=None):
        self._ctx = ctx
        self.lcm_project = str(Path(workspace).expanduser().resolve())
        self.lcm_source = source if source is not None else read_field(ctx, "lcm_source")

    def __getattr__(self, name):
        return getattr(self._ctx, name)


def context(ctx, workspace, source=None):
    # A resumed worktree/child retains its original project, even if its old
    # execution directory was temporary. The marker is native session metadata.
    manager = read_field(ctx, "sessionManager")
    entries = read_field(manager, "getEntries")
    if callable(entries):
        for entry in entries():
            if entry.get("type") == "custom" and entry.get("customType") == "lcm-project":
                workspace = entry["data"]["workspace"]
                break
    return ProjectContext(ctx, workspace, source)


def plugin_home() -> Path:
    """This plugin's own corner of the home: the one place it keeps anything that is not a
    project's. The core knows only that plugins have such directories, not what is in them."""
    return home.path("plugins") / "misaka-lcm"


def _private(folder: Path) -> bool:
    """Whether a directory made in ``folder`` keeps owner-only permissions. The store refuses a
    database whose directory others can write, and a drive that maps another system's permissions
    -- a Windows drive under WSL, a network share -- shows every directory as writable by all,
    whatever chmod asks for (2026-09-30, a project under /mnt/c failed every turn)."""
    if os.name != "posix":
        return True
    with _LOCK:
        if folder not in _PRIVATE:
            try:
                probe = Path(tempfile.mkdtemp(prefix=".misaka-lcm-probe-", dir=folder))
            except OSError:
                return True  # the store's own checks say what is wrong with this folder
            try:
                _PRIVATE[folder] = not probe.stat().st_mode & 0o022
            finally:
                probe.rmdir()
        return _PRIVATE[folder]


def directory(workspace: Path) -> Path:
    """The project's own store; a workspace that is no project (its config directory is the
    home) keeps it in the plugin's own directory instead, and so does a project on a drive that
    cannot keep it private, in a directory of its own there."""
    project_dir = home.project_dir(workspace)
    if project_dir is None:
        return plugin_home() / "lcm"
    if _private(project_dir if project_dir.is_dir() else Path(workspace)):
        return project_dir / "lcm"
    resolved = Path(workspace).resolve()
    key = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return plugin_home() / "projects" / f"{resolved.name}-{key}" / "lcm"


def _paths(workspace):
    root = directory(workspace)
    paths = (root.parent, root, root.parent / "lcm.gate", root.parent / "lcm.activity.sqlite")
    if any(path.is_symlink() for path in paths):
        raise ValueError("MISAKA LCM storage and lock paths must not be symlinks")
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root, FileLock(paths[2], timeout=30, preserve_lock_file=True), paths[3]


def _marked(root):
    """True for this plugin's cache, None for an empty or half-initialized one (the marker not
    written yet), False for anything else."""
    contents = list(root.iterdir())
    marker = root / _MARKER
    content = marker.read_text(encoding="utf-8-sig") if marker.is_file() and not marker.is_symlink() else None
    if content == _STAMP or content in _OUTDATED:
        return True
    if not contents or (content == "" and contents == [marker]):
        return None
    return False


def _outdated(root):
    """Whether this cache's rows hold what this code no longer compares against. A row with a payload
    reference matches no message now, and a session end then searches for it (20-40 s a session)."""
    marker = root / _MARKER
    return (marker.is_file() and not marker.is_symlink()
            and marker.read_text(encoding="utf-8-sig") in _OUTDATED)


def _set_aside(root):
    """Files at the cache path that this plugin did not mark are kept, under a name that says so,
    rather than refused for as long as they sit there: a refusal failed every turn of every
    session in the project (2026-09-27, when a project's `.misaka` was deleted under a running
    session and a path-level open recreated the directory without its marker)."""
    aside = root.with_name(f"{root.name}.unrecognized-{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}")
    root.rename(aside)
    logger.warning("MISAKA LCM moved files it did not write from %s to %s; a fresh cache starts there.", root, aside)


def _discard(root):
    """Remove this plugin's cache; only the holder of the exclusive lease may."""
    if root.is_symlink():
        raise ValueError("MISAKA LCM cache must not be a symlink")
    if root.exists():
        if _marked(root) is False:
            _set_aside(root)
        else:
            shutil.rmtree(root)


def _prepare(root):
    """A marked cache directory at ``root``: a missing one is created and anything unmarked is set
    aside first. Neither destroys anything, so the gate is enough -- a process that joins a project
    whose directory went away restores it before any engine opens a database there."""
    if root.is_symlink():
        raise ValueError("MISAKA LCM cache must not be a symlink")
    state = _marked(root) if root.exists() else None
    if state is False:
        _set_aside(root)
    if state is not True:
        root.mkdir(mode=0o700, exist_ok=True)
        (root / _MARKER).write_text(_STAMP, encoding="utf-8")


def _identity(root, lock_path):
    """The cache directory and lease file a process joined, as files rather than paths: a project
    folder deleted or replaced while sessions run leaves other files at the same paths."""
    try:
        return tuple((info.st_dev, info.st_ino) for info in (os.stat(root), os.stat(lock_path)))
    except OSError:
        return None


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
                    if _outdated(root):
                        _discard(root)   # the session files rebuild it, and nobody else holds it
                    lease.release()
                    _FIRST_OWNER.add(workspace)
                _prepare(root)
                lease.acquire_read(timeout=30)
            _LEASES[workspace] = lease
            _IDENTITY[workspace] = _identity(root, lock_path)
        except BaseException:
            lease.close()
            raise


def current(workspace: Path):
    """The cache this process's lease guards, as ``_identity`` names it, or None without a lease.
    A lease whose directory or lease file is no longer the one on disk guards nothing -- its
    engines write into a folder that was moved away, and other processes see a lease file nobody
    holds -- so it is let go here, and the next ``acquire`` joins or restores the project's cache."""
    workspace = workspace.resolve()
    with _LOCK:
        lease = _LEASES.get(workspace)
        if lease is None:
            return None
        root = directory(workspace)
        identity = _IDENTITY.get(workspace)
        if identity is not None and identity == _identity(root, root.parent / "lcm.activity.sqlite"):
            return identity
        logger.warning("MISAKA LCM cache of %s was deleted or replaced while in use; rebuilding it "
                       "from the session transcripts.", workspace)
        lease.close()
        del _LEASES[workspace]
        _IDENTITY.pop(workspace, None)
        return None


def take_first_owner(workspace: Path) -> bool:
    """Whether this process opened the project's cache with nobody else in it, once: the moment
    for housekeeping no running session should see."""
    workspace = workspace.resolve()
    with _LOCK:
        first = workspace in _FIRST_OWNER
        _FIRST_OWNER.discard(workspace)
        return first


def release(workspace: Path) -> None:
    """Let the cache go. Nothing is removed and nothing waits on the gate: a run's processes all
    exit together, and the gate used to be where they queued to delete it."""
    workspace = workspace.resolve()
    with _LOCK:
        lease = _LEASES.pop(workspace, None)
        _IDENTITY.pop(workspace, None)
        _FIRST_OWNER.discard(workspace)
        if lease is not None:
            lease.close()


def clear(workspace: Path) -> bool:
    """Remove the project's cache. False, with nothing removed, while a process is using it."""
    workspace = workspace.resolve()
    if not directory(workspace).exists():
        return True  # and no project layout is created to say so
    root, gate, lock_path = _paths(workspace)
    lease = ReadWriteLock(lock_path, is_singleton=False)
    try:
        with gate:
            try:
                lease.acquire_write(timeout=0)
            except Timeout:
                return False
            _discard(root)
            return True
    finally:
        lease.close()


def reset_unreadable(workspace: Path) -> bool:
    """Replace a cache whose database cannot be opened, now that an exit no longer sweeps it away.
    False while another process is using it: this one then runs without LCM, as before."""
    release(workspace)
    cleared = clear(workspace)
    acquire(workspace)
    return cleared


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
