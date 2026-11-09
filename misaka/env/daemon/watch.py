"""``watch``: Durable's ``NodeFileWatcher`` (packages/durable/src/env/node-watch.ts) running next to the files, so a scan
costs no round trips. Native events only trigger a debounced rescan; changes are the difference between snapshots plus
the event paths. Events go out as ``change`` events of the request, which lasts until it is cancelled.

MISAKA fork: Rust's ``notify`` crate is the native backend there. Here it is inotify through ``ctypes`` on Linux and
Android, one watch per directory as ``notify`` installs them on Linux. macOS has no FSEvents binding in the standard
library, so it polls by default, as Windows does; a request for native watching on macOS or Windows switches to polling
with ``overflow`` at once, as a native watcher that cannot be created does.
"""

from __future__ import annotations

import hashlib
import os
import queue
import select
import struct
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from . import system
from .errors import Failure
from .exec import Control
from .frame import EVENT, Frame, as_bool, as_str, as_u64, get
from .fs import parent
from .output import Output

DEBOUNCE = 0.05
DEFAULT_POLL_MS = 2000
DEFAULT_MAX_DIRECTORIES = 10_000
# In polling mode, recently modified small files are also compared by content.
HASH_MAX_BYTES = 256 * 1024
HASH_RECENT = 5.0
# How often a waiting watcher looks for a cancel.
CANCEL_POLL = 0.1

# Linux `statfs` magic numbers of file systems that accept watches but do not report changes made elsewhere.
UNRELIABLE_FILE_SYSTEMS = (
    0x6969,  # NFS
    0x517B,  # SMB
    0xFF534D42,  # CIFS
    0xFE534D42,  # SMB2
    0x65735546,  # FUSE (sshfs, Android shared storage)
    0x01021997,  # 9P (WSL2 Windows drives)
    0x0BD00BD0,  # Lustre
    0x47504653,  # GPFS
    0x00C36400,  # Ceph
    0x5346414F,  # OpenAFS
    0x6B414653,  # kAFS
    0x5DCA2DF5,  # sdcardfs
)

_LINUX = sys.platform.startswith("linux") or sys.platform == "android" or hasattr(sys, "getandroidapilevel")


class Target:
    __slots__ = ("hidden", "names", "path", "recursive")

    def __init__(self, path: str, recursive: bool, hidden: bool, names: set[str]) -> None:
        self.path = path
        self.recursive = recursive
        self.hidden = hidden
        self.names = names


# What a snapshot remembers of one path: `(kind, dev, ino, size, mtime, hash)`. Directories and ancestors are compared
# by identity only.
Entry = tuple

NATIVE = "native"
POLLING = "polling"


class ScanError(Exception):
    def __init__(self, budget: int | None = None, error: BaseException | None = None, syscall: str = "", path: str = ""):
        super().__init__(path)
        self.budget = budget
        self.error = error
        self.syscall = syscall
        self.path = path

    def failure(self) -> Failure:
        """The error ``NodeExecutionEnv.watch`` returns when opening fails."""
        if self.budget is not None:
            return Failure("EINVAL", f"Watched paths exceed {self.budget} directories")
        assert self.error is not None
        return Failure.io(self.error, self.syscall, self.path)

    def code(self) -> str:
        """The ``FileError`` code ``NodeFileWatcher`` reports when a later rescan fails."""
        if self.error is not None and denied(self.error):
            return "permission_denied"
        return "invalid"


def denied(error: BaseException) -> bool:
    return isinstance(error, PermissionError)


def kind_of(metadata: os.stat_result) -> str:
    return system.file_type(metadata)


def entry_of(path: str, metadata: os.stat_result, follow: bool, hash: bytes | None) -> Entry:
    """What a snapshot remembers of ``path``, whose ``metadata`` came from ``stat`` (``follow``) or ``lstat``."""
    kind = kind_of(metadata)
    dev, ino = system.path_identity(path, metadata, follow)
    directory = kind == "directory"
    return (
        kind,
        dev,
        ino,
        0 if directory else metadata.st_size,
        (0, 0) if directory else system.mtime(metadata),
        hash,
    )


def ancestors(path: str) -> list[str]:
    """Ancestors of ``path``, nearest first, up to the root."""
    result = []
    current = parent(path)
    while current is not None:
        result.append(current)
        current = parent(current)
    return result


def with_separator(directory: str) -> str:
    return directory if directory.endswith(system.PATH_SEPARATOR) else f"{directory}{system.PATH_SEPARATOR}"


def is_within(path: str, ancestor: str) -> bool:
    """Whether ``path`` is ``ancestor`` or below it."""
    return path == ancestor or path.startswith(with_separator(ancestor))


def excluded(target: Target, name: str) -> bool:
    return (target.hidden and name.startswith(".")) or name in target.names


def sort_like_javascript(paths: list[str]) -> None:
    """JavaScript's default sort: by UTF-16 code units."""
    paths.sort(key=lambda path: path.encode("utf-16-be", "surrogatepass"))


def _statfs_type(path: str) -> int | None:
    import ctypes

    try:
        library = ctypes.CDLL(None, use_errno=True)
        # `struct statfs` begins with `f_type`, a long; 32 longs cover the whole structure.
        buffer = (ctypes.c_long * 32)()
        if library.statfs(system.os_path(path), ctypes.byref(buffer)) != 0:
            return None
        return int(buffer[0]) & 0xFFFFFFFF
    except (OSError, AttributeError, ValueError):
        return None


def any_unreliable(targets: list[Target]) -> bool:
    """Whether any path, or its nearest existing ancestor, is on a file system that does not report remote changes."""
    if not _LINUX:
        return False
    for target in targets:
        for candidate in [target.path, *ancestors(target.path)]:
            if "\0" in candidate:
                break
            kind = _statfs_type(candidate)
            if kind is None:
                continue
            if kind in UNRELIABLE_FILE_SYSTEMS:
                return True
            break
    return False


# inotify(7)
_IN_MODIFY, _IN_ATTRIB, _IN_MOVED_FROM, _IN_MOVED_TO = 0x2, 0x4, 0x40, 0x80
_IN_CREATE, _IN_DELETE, _IN_DELETE_SELF, _IN_MOVE_SELF = 0x100, 0x200, 0x400, 0x800
_IN_Q_OVERFLOW, _IN_IGNORED = 0x4000, 0x8000
_IN_NONBLOCK, _IN_CLOEXEC = 0o4000, 0o2000000
_INOTIFY_MASK = (
    _IN_MODIFY | _IN_ATTRIB | _IN_MOVED_FROM | _IN_MOVED_TO | _IN_CREATE | _IN_DELETE | _IN_DELETE_SELF | _IN_MOVE_SELF
)
_INOTIFY_EVENT = struct.Struct("iIII")

# A native event: `("event", paths)`, `("rescan",)` when events were lost, or `("error", paths)` when the watches on
# `paths` ended.
NativeEvent = tuple


class NativeWatcher:
    """``notify``'s ``RecommendedWatcher`` on Linux: one inotify instance and a reader thread that hands each event to
    ``callback``. A watch covers one directory (or file); ``recursive`` is not supported."""

    def __init__(self, callback: Callable[[NativeEvent], None]) -> None:
        if not _LINUX:
            raise OSError("native file watching is not available")
        import ctypes

        library = ctypes.CDLL(None, use_errno=True)
        fd = library.inotify_init1(_IN_NONBLOCK | _IN_CLOEXEC)
        if fd < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        self._ctypes = ctypes
        self._library = library
        self._fd = fd
        self._callback = callback
        self._lock = threading.Lock()
        self._paths: dict[int, str] = {}
        self._descriptors: dict[str, int] = {}
        self._closed = False
        self._thread = threading.Thread(target=self._read, name="pi-env-inotify", daemon=True)
        self._thread.start()

    def watch(self, path: str, recursive: bool) -> None:
        if recursive:
            raise OSError("recursive native watching is not available")
        descriptor = self._library.inotify_add_watch(self._fd, system.os_path(path), _INOTIFY_MASK)
        if descriptor < 0:
            number = self._ctypes.get_errno()
            raise OSError(number, os.strerror(number), path)
        with self._lock:
            self._paths[descriptor] = path
            self._descriptors[path] = descriptor

    def unwatch(self, path: str) -> None:
        with self._lock:
            descriptor = self._descriptors.pop(path, None)
            if descriptor is None:
                return
            self._paths.pop(descriptor, None)
        self._library.inotify_rm_watch(self._fd, descriptor)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True

    def _read(self) -> None:
        poller = select.poll()  # type: ignore[attr-defined]
        poller.register(self._fd, select.POLLIN)  # type: ignore[attr-defined]
        try:
            while not self._closed:
                if not poller.poll(100):
                    continue
                try:
                    data = os.read(self._fd, 64 * 1024)
                except BlockingIOError:
                    continue
                except OSError:
                    return
                self._dispatch(data)
        finally:
            os.close(self._fd)

    def _dispatch(self, data: bytes) -> None:
        offset = 0
        while offset + _INOTIFY_EVENT.size <= len(data):
            descriptor, mask, _cookie, length = _INOTIFY_EVENT.unpack_from(data, offset)
            raw = data[offset + _INOTIFY_EVENT.size : offset + _INOTIFY_EVENT.size + length]
            offset += _INOTIFY_EVENT.size + length
            if mask & _IN_Q_OVERFLOW:
                self._callback(("rescan",))
                continue
            with self._lock:
                path = self._paths.get(descriptor)
                if mask & _IN_IGNORED and path is not None:
                    self._paths.pop(descriptor, None)
                    self._descriptors.pop(path, None)
            if path is None:
                continue
            if mask & _IN_IGNORED:
                # The watch ended: its directory was removed or replaced.
                self._callback(("error", [path]))
                continue
            name = raw.split(b"\0", 1)[0]
            self._callback(("event", [os.path.join(path, system.to_string(name)) if name else path]))


class Scan:
    """One scan: the snapshot, and targets that are symbolic links to files, whose files need their own watchers."""

    def __init__(self) -> None:
        self.snapshot: dict[str, Entry] = {}
        self.linked_files: set[str] = set()
        # Directories counted against the limit, and directories traversed per target.
        self.counted: set[str] = set()
        self.traversed: set[tuple[int, str]] = set()
        # Kinds of listed entries without following links.
        self.listed: dict[str, str] = {}


class Installed:
    __slots__ = ("canonical", "dev", "ino")

    def __init__(self, dev: int, ino: int, canonical: str | None) -> None:
        self.dev = dev
        self.ino = ino
        # The watched path with links resolved, as a native backend may spell event paths.
        self.canonical = canonical


class FileWatcher:
    def __init__(
        self,
        id: int,
        output: Output,
        control: Control,
        targets: list[Target],
        mode: str,
        poll_interval: float,
        max_directories: int,
        events_sender: queue.SimpleQueue[NativeEvent],
    ) -> None:
        self.id = id
        self.output = output
        self.control = control
        self.targets = targets
        self.mode = mode
        self.poll_interval = poll_interval
        self.max_directories = max_directories
        self.snapshot: dict[str, Entry] = {}
        self.native: NativeWatcher | None = None
        self.events_sender = events_sender
        self.installed: dict[str, Installed] = {}
        self.events: set[str] = set()
        self.settle_at: float | None = None

    def send(self, json: dict[str, Any]) -> None:
        self.output.control(Frame(EVENT, self.id, json))

    def cancelled(self) -> bool:
        return self.control.aborted.is_set()

    def record(self, scan: Scan, path: str, metadata: os.stat_result, follow: bool) -> None:
        hash = None
        age = time.time() - metadata.st_mtime
        recent = age < 0 or age < HASH_RECENT
        if (
            self.mode == POLLING
            and kind_of(metadata) == "file"
            and metadata.st_size <= HASH_MAX_BYTES
            and recent
        ):
            try:
                with open(system.os_path(path), "rb") as file:  # windows-footgun: ok - binary mode
                    hash = hashlib.blake2b(file.read(), digest_size=8).digest()
            except (OSError, ValueError):
                hash = None
        scan.snapshot[path] = entry_of(path, metadata, follow, hash)

    def count(self, scan: Scan, path: str) -> None:
        scan.counted.add(path)
        if len(scan.counted) > self.max_directories:
            raise ScanError(budget=self.max_directories)

    def scan_directory(self, scan: Scan, index: int, directory: str) -> None:
        target = self.targets[index]
        key = (index, directory)
        if key in scan.traversed:
            return
        scan.traversed.add(key)

        def skip(error: OSError) -> None:
            # The watched directory itself must be readable; below it, unreadable directories are skipped.
            if directory == target.path and denied(error):
                raise ScanError(error=error, syscall="scandir", path=directory)
            if isinstance(error, (FileNotFoundError, PermissionError, NotADirectoryError)):
                return
            raise ScanError(error=error, syscall="scandir", path=directory)

        names = []
        try:
            with os.scandir(system.os_path(directory)) as entries:
                for entry in entries:
                    names.append(system.to_string(entry.name))
        except OSError as error:
            skip(error)
            return
        prefix = with_separator(directory)
        for name in names:
            if excluded(target, name):
                continue
            path = f"{prefix}{name}"
            kind = scan.listed.get(path)
            if kind is None:
                try:
                    metadata = os.lstat(system.os_path(path))
                except (OSError, ValueError):
                    continue
                kind = kind_of(metadata)
                scan.listed[path] = kind
                # A target's own entry (following links) wins over its listing by another target.
                if path not in scan.snapshot:
                    self.record(scan, path, metadata, False)
            if target.recursive and kind == "directory":
                self.count(scan, path)
                self.scan_directory(scan, index, path)

    def scan(self) -> Scan:
        scan = Scan()
        for index, target in enumerate(self.targets):
            for ancestor in ancestors(target.path):
                if ancestor in scan.snapshot:
                    continue
                # Identity only: an ancestor's own timestamps change with every unrelated sibling.
                try:
                    metadata = os.lstat(system.os_path(ancestor))
                except (OSError, ValueError):
                    continue
                kind, dev, ino, _size, _mtime, hash = entry_of(ancestor, metadata, False, None)
                scan.snapshot[ancestor] = (kind, dev, ino, 0, (0, 0), hash)
            # The target may be a symbolic link to what is watched; follow it. A missing target is watched for its
            # creation; one that cannot be reached for lack of permission fails.
            try:
                metadata = os.stat(system.os_path(target.path))
            except (OSError, ValueError) as error:
                if denied(error):
                    raise ScanError(error=error, syscall="stat", path=target.path) from None
                continue
            self.record(scan, target.path, metadata, True)
            kind = kind_of(metadata)
            if kind == "file":
                try:
                    linked = kind_of(os.lstat(system.os_path(target.path))) == "symlink"
                except (OSError, ValueError):
                    linked = False
                if linked:
                    scan.linked_files.add(target.path)
            if kind == "directory":
                self.count(scan, target.path)
                self.scan_directory(scan, index, target.path)
        return scan

    def reported(self, path: str) -> str:
        """An ancestor that changed identity moved every target below it; report those targets."""
        if any(is_within(path, target.path) for target in self.targets):
            return path
        for target in self.targets:
            if is_within(target.path, path):
                return target.path
        return path

    def diff(self, previous: dict[str, Entry], next: dict[str, Entry]) -> list[str]:
        changed = []
        for path, entry in next.items():
            if previous.get(path) != entry:
                changed.append(self.reported(path))
        for path in previous:
            if path not in next:
                changed.append(self.reported(path))
        return changed

    def sync(self, report: bool) -> set[str]:
        """Rescan, report differences, and install watchers for new directories, rescanning until none are new."""
        changed: set[str] = set()
        for _ in range(10):
            if self.cancelled():
                break
            scan = self.scan()
            if report:
                changed.update(self.diff(self.snapshot, scan.snapshot))
            self.snapshot = scan.snapshot
            if self.mode == POLLING or not self.reconcile(scan.linked_files):
                break
            # Something written into a new directory before its watcher existed shows up in the next round.
            report = True
        return changed

    def reconcile(self, linked_files: set[str]) -> bool:
        """Watch every existing ancestor of each target, each target directory, each target that links to a file, and
        each directory below a recursive target. Returns whether a watcher was added."""
        # The inotify backend watches each directory on its own, as `notify` does on Linux.
        per_directory = True
        wanted: dict[str, bool] = {path: False for path in linked_files}
        for target in self.targets:
            for ancestor in ancestors(target.path):
                entry = self.snapshot.get(ancestor)
                if entry is not None and entry[0] == "directory":
                    wanted.setdefault(ancestor, False)
            entry = self.snapshot.get(target.path)
            if entry is None or entry[0] != "directory":
                continue
            wanted[target.path] = target.recursive and not per_directory
            if target.recursive and per_directory:
                for path, entry in self.snapshot.items():
                    if entry[0] == "directory" and path != target.path and is_within(path, target.path):
                        wanted[path] = False
        # Gone, or replaced: a watcher follows the directory it was installed on, not the path.
        stale = [
            path
            for path, installed in self.installed.items()
            if path not in wanted
            or self.snapshot.get(path) is None
            or self.snapshot[path][1] != installed.dev
            or self.snapshot[path][2] != installed.ino
        ]
        for path in stale:
            del self.installed[path]
            if self.native is not None:
                self.native.unwatch(path)
        added = False
        for path, recursive in wanted.items():
            snapshot_entry = self.snapshot.get(path)
            if snapshot_entry is None or path in self.installed:
                continue
            dev, ino = snapshot_entry[1], snapshot_entry[2]
            if self.native is None:
                try:
                    self.native = NativeWatcher(self.events_sender.put)
                except OSError:
                    self.switch_to_polling()
                    return False
            try:
                self.native.watch(path, recursive)
            except OSError as error:
                gone = isinstance(error, (FileNotFoundError, PermissionError))
                # Out of watches or unsupported: compare snapshots from now on, and say coverage was uncertain.
                if not gone:
                    self.switch_to_polling()
                    return False
                continue
            try:
                canonical: str | None = system.realpath(path)
            except (OSError, ValueError):
                canonical = None
            self.installed[path] = Installed(dev, ino, canonical if canonical != path else None)
            added = True
        return added

    def switch_to_polling(self) -> None:
        if self.mode == POLLING:
            return
        self.mode = POLLING
        if self.native is not None:
            self.native.close()
        self.native = None
        self.installed.clear()
        self.send({"kind": "change", "overflow": True, "mode": "polling"})

    def in_scope(self, path: str) -> bool:
        for target in self.targets:
            if is_within(target.path, path):
                return True
            if not is_within(path, target.path) or path == target.path:
                continue
            relative = path[len(with_separator(target.path)) :]
            components = relative.split(system.PATH_SEPARATOR)
            if not target.recursive and len(components) > 1:
                continue
            if any(excluded(target, name) for name in components):
                continue
            return True
        return False

    def watched_spelling(self, path: str) -> str:
        """An event path in the spelling of the watched path it is under."""
        best: tuple[str, str] | None = None
        for watched, installed in self.installed.items():
            canonical = installed.canonical
            if canonical is not None and is_within(path, canonical) and (best is None or len(canonical) > len(best[1])):
                best = (watched, canonical)
        if best is None:
            return path
        return f"{best[0]}{path[len(best[1]) :]}"

    def on_event(self, event: NativeEvent) -> bool:
        """Note a native event; returns whether it calls for a rescan."""
        if event[0] == "error":
            # A watched directory that disappears or fails: rescan, which also replaces its watcher.
            for path in event[1]:
                self.installed.pop(path, None)
            return True
        if event[0] == "rescan" or not event[1]:
            return True
        flush = False
        for path in event[1]:
            spelled = self.watched_spelling(path)
            # Events about unrelated siblings of an ancestor, or about excluded entries, are ignored.
            if self.in_scope(spelled):
                self.events.add(self.reported(spelled))
                flush = True
        return flush

    def flush(self) -> bool:
        """Rescan and report what changed; an error ends the watcher."""
        events, self.events = self.events, set()
        try:
            changed = self.sync(True)
        except ScanError as error:
            self.send({"kind": "error", "code": error.code(), "message": error.failure().message})
            return False
        changed.update(events)
        if changed and not self.cancelled():
            paths = list(changed)
            sort_like_javascript(paths)
            self.send({"kind": "change", "paths": paths})
        return True

    def close(self) -> None:
        if self.native is not None:
            self.native.close()
            self.native = None


def parse_targets(json: Any) -> list[Target]:
    def invalid() -> Failure:
        return Failure("EINVAL", "watch needs targets")

    targets = get(json, "targets")
    if not isinstance(targets, list):
        raise invalid()
    result = []
    for target in targets:
        path = as_str(get(target, "path"))
        if path is None:
            raise invalid()
        exclude = get(target, "exclude")
        names = get(exclude, "names")
        result.append(
            Target(
                path,
                as_bool(get(target, "recursive")) or False,
                as_bool(get(exclude, "hidden")) or False,
                {name for name in names if isinstance(name, str)} if isinstance(names, list) else set(),
            )
        )
    return result


def run(id: int, json: Any, output: Output, control: Control) -> dict[str, Any]:
    """Watch until cancelled: a ``ready`` event once coverage is established, then ``change`` events, or an ``error``
    event after which nothing follows."""
    targets = parse_targets(json)
    # Windows refuses to rename a directory while a directory below it is open, and native watchers keep watched
    # directories open, so Windows polls, as do file systems that do not report remote changes (and, here, macOS).
    requested = as_str(get(json, "mode"))
    if requested == NATIVE:
        mode = NATIVE
    elif requested == POLLING or system.WINDOWS or not _LINUX or any_unreliable(targets):
        mode = POLLING
    else:
        mode = NATIVE
    poll_ms = as_u64(get(json, "pollIntervalMs"))
    max_directories = as_u64(get(json, "maxDirectories"))
    events: queue.SimpleQueue[NativeEvent] = queue.SimpleQueue()
    watcher = FileWatcher(
        id,
        output,
        control,
        targets,
        mode,
        (DEFAULT_POLL_MS if poll_ms is None else poll_ms) / 1000,
        DEFAULT_MAX_DIRECTORIES if max_directories is None else max_directories,
        events,
    )
    try:
        try:
            watcher.sync(False)
        except ScanError as error:
            raise error.failure() from None
        watcher.send({"kind": "ready", "mode": watcher.mode})
        flush_at: float | None = None
        poll_at = time.monotonic() + watcher.poll_interval
        while True:
            if watcher.cancelled():
                return {}
            now = time.monotonic()
            wake = now + CANCEL_POLL
            for at in (flush_at, watcher.settle_at):
                if at is not None:
                    wake = min(wake, at)
            if watcher.mode == POLLING:
                wake = min(wake, poll_at)
            try:
                event = events.get(timeout=max(wake - now, 0.0))
            except queue.Empty:
                pass
            else:
                if watcher.on_event(event) and flush_at is None and watcher.mode == NATIVE:
                    flush_at = time.monotonic() + DEBOUNCE
            now = time.monotonic()
            flush_due = flush_at is not None and flush_at <= now
            settle_due = watcher.settle_at is not None and watcher.settle_at <= now
            polling_due = watcher.mode == POLLING and poll_at <= now
            if flush_due or settle_due or polling_due:
                if flush_due:
                    flush_at = None
                if settle_due:
                    watcher.settle_at = None
                if not watcher.flush():
                    return {}
                if polling_due:
                    poll_at = time.monotonic() + watcher.poll_interval
    finally:
        watcher.close()
