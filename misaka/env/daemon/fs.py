"""File operations with Node's semantics where the Rust standard library differs (docs/semantics.md)."""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import sys
from typing import Any

from . import system
from .errors import Failure
from .system import File

_SEPARATORS = re.compile(r"[\\/]" if system.WINDOWS else "/")
_DRIVE = re.compile(r"^[A-Za-z]:")


def _components(path: str) -> list[str]:
    """The normal components of ``path`` after its root, as Rust's ``Path::components`` yields them (``.`` dropped)."""
    if system.WINDOWS:
        # A UNC prefix `\\server\share` or a drive `C:` is no component.
        unc = re.match(r"^[\\/]{2}[^\\/]+[\\/]+[^\\/]+", path)
        if unc is not None:
            path = path[unc.end() :]
        elif _DRIVE.match(path):
            path = path[2:]
    return [part for part in _SEPARATORS.split(path) if part not in ("", ".")]


def file_name(path: str) -> str:
    """``Path::file_name``, or ``""`` where it is ``None``: the final component, unless that is ``..`` or a root."""
    parts = _components(path)
    if not parts or parts[-1] == "..":
        return ""
    return parts[-1]


def parent(path: str) -> str | None:
    """``Path::parent``, ``None`` for a root or a path of one relative component (whose parent is empty)."""
    if not _components(path):
        return None
    stripped = path
    while len(stripped) > 1 and stripped[-1] in ("\\/" if system.WINDOWS else "/"):
        stripped = stripped[:-1]
    result = os.path.dirname(stripped)
    return result if result else None


def _not_found(error: BaseException) -> bool:
    """``io::ErrorKind::NotFound``."""
    if not isinstance(error, OSError):
        return False
    if system.WINDOWS and getattr(error, "winerror", None) is not None:
        return getattr(error, "winerror", None) in (2, 3)
    return error.errno == errno.ENOENT


def _already_exists(error: BaseException) -> bool:
    """``io::ErrorKind::AlreadyExists``."""
    if not isinstance(error, OSError) or isinstance(error, system.Synthetic):
        return False
    if system.WINDOWS and getattr(error, "winerror", None) is not None:
        return getattr(error, "winerror", None) in (80, 183)
    return error.errno == errno.EEXIST


def info(path: str, metadata: os.stat_result) -> dict[str, Any]:
    """``{ name, kind, size, mtimeSec, mtimeNsec, dev, ino }`` of lstat or fstat metadata."""
    mtime_sec, mtime_nsec = system.mtime(metadata)
    dev, ino = system.identity(metadata)
    return {
        "name": file_name(path),
        "kind": system.file_type(metadata),
        "size": metadata.st_size,
        "mtimeSec": mtime_sec,
        "mtimeNsec": mtime_nsec,
        "dev": dev,
        "ino": ino,
    }


def lstat(path: str) -> dict[str, Any]:
    try:
        return info(path, os.lstat(system.os_path(path)))
    except (OSError, ValueError) as error:
        raise Failure.io(error, "lstat", path) from None


def realpath(path: str) -> dict[str, Any]:
    try:
        return {"path": system.realpath(path)}
    except (OSError, ValueError) as error:
        raise Failure.io(error, "realpath", path) from None


def _is_dir(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.stat(system.os_path(path)).st_mode)
    except (OSError, ValueError):
        return False


def mkdir_recursive(path: str) -> None:
    """Node's recursive ``mkdir``: an existing directory is fine, an existing file is ``EEXIST``, a file in the way of a
    parent is ``ENOTDIR``."""
    try:
        os.mkdir(system.os_path(path))
        return
    except OSError as error:
        if _already_exists(error):
            if _is_dir(path):
                return
            raise
        if not _not_found(error):
            raise
        missing = error
    above = parent(path)
    if above is None:
        raise missing
    try:
        mkdir_recursive(above)
    except OSError as parent_error:
        if _already_exists(parent_error):
            raise system.synthetic("ENOTDIR") from None
        raise
    try:
        os.mkdir(system.os_path(path))
    except OSError as error:
        if _already_exists(error) and _is_dir(path):
            return
        raise


def mkdir(path: str, recursive: bool) -> dict[str, Any]:
    try:
        if recursive:
            mkdir_recursive(path)
        else:
            os.mkdir(system.os_path(path))
    except (OSError, ValueError) as error:
        raise Failure.io(error, "mkdir", path) from None
    return {}


def write(path: str, append: bool, parents: bool, content: bytes) -> File:
    """``writeFile``/``appendFile``: create missing parents like Node's recursive ``mkdir``, then write. Returns the open
    file for further chunks."""
    above = parent(path) if parents else None
    if above is not None:
        try:
            mkdir_recursive(above)
        except (OSError, ValueError) as error:
            raise Failure.io(error, "mkdir", above) from None
    try:
        file = system.open(path, system.APPEND if append else system.WRITE)
    except (OSError, ValueError) as error:
        raise Failure.io(error, "open", path) from None
    try:
        file.write_all(content)
    except OSError as error:
        raise Failure.io(error, "write", path) from None
    return file


def open_existing_for_write(path: str) -> File:
    try:
        return system.open(path, system.READ_WRITE)
    except (OSError, ValueError) as error:
        raise Failure.io(error, "open", path) from None


def truncate(path: str, size: int) -> dict[str, Any]:
    file = open_existing_for_write(path)
    try:
        file.set_len(size)
    except (OSError, OverflowError) as error:
        raise Failure.io(error, "ftruncate", path) from None
    finally:
        file.close()
    return {}


def fsync(path: str) -> dict[str, Any]:
    # POSIX refuses to open a directory for writing; check first so Windows reports the same.
    if _is_dir(path):
        raise Failure("EISDIR", f"EISDIR: illegal operation on a directory, open '{path}'").path(path)
    file = open_existing_for_write(path)
    try:
        file.sync_all()
    except OSError as error:
        raise Failure.io(error, "fsync", path) from None
    finally:
        file.close()
    return {}


def rename(source: str, destination: str) -> dict[str, Any]:
    try:
        system.rename(source, destination)
    except (OSError, ValueError) as error:
        raise Failure.io_between(error, "rename", source, destination) from None
    return {}


def _remove_dir_all(path: str) -> None:
    """``fs::remove_dir_all``: the tree below ``path`` without following links; on Windows read-only entries go too."""
    if not system.WINDOWS:
        shutil.rmtree(system.os_path(path))
        return

    def retry(function: Any, failed: str, _info: Any) -> None:
        # Windows refuses to remove a read-only entry; make it writable and try again.
        os.chmod(failed, stat.S_IWRITE | stat.S_IREAD)
        function(failed)

    if sys.version_info >= (3, 12):  # noqa: UP036 - the daemon also runs on Python 3.9
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def rm(path: str, recursive: bool, force: bool) -> dict[str, Any]:
    """Node's ``fs.rm``: missing paths fail unless ``force``; a directory without ``recursive`` fails with
    ``ERR_FS_EISDIR``."""
    try:
        metadata = os.lstat(system.os_path(path))
    except (OSError, ValueError) as error:
        if force and _not_found(error):
            return {}
        raise Failure.io(error, "lstat", path) from None
    try:
        if system.file_type(metadata) == "directory":
            if not recursive:
                raise Failure(
                    "ERR_FS_EISDIR", f"Path is a directory: rm returned EISDIR (is a directory) {path}"
                ).path(path)
            _remove_dir_all(path)
        else:
            system.remove_file(path)
    except (OSError, ValueError) as error:
        raise Failure.io(error, "rm", path) from None
    return {}


def mkdtemp(prefix: str) -> dict[str, Any]:
    """A new directory named ``prefix`` plus six random characters, as libuv's ``mkdtemp`` creates it."""
    try:
        return {"path": system.mkdtemp(prefix)}
    except (OSError, ValueError) as error:
        raise Failure.io(error, "mkdtemp", f"{prefix}XXXXXX") from None


def open_reader(path: str, no_follow: bool) -> tuple[File, dict[str, Any]]:
    """Open a regular file for positional reads; directories fail with ``EISDIR``, other non-regular files with
    ``NOT_REGULAR``, and with ``no_follow`` a final symbolic link with ``SYMLINK``."""
    file = system.open_reader(path, no_follow)
    try:
        metadata = file.metadata()
    except OSError as error:
        raise Failure.io(error, "fstat", path) from None
    kind = system.file_type(metadata)
    if kind == "directory":
        raise Failure("EISDIR", "EISDIR: illegal operation on a directory, read").path(path)
    if kind != "file":
        raise Failure("NOT_REGULAR", "Not a regular file").path(path)
    return file, info(path, metadata)


def pread(file: File, path: str, offset: int, length: int) -> bytes:
    """Up to ``length`` bytes at ``offset``; fewer only at the end of the file."""
    chunks = []
    filled = 0
    while filled < length:
        try:
            data = system.read_at(file, length - filled, offset + filled)
        except InterruptedError:
            continue
        except (OSError, OverflowError) as error:
            raise Failure.io(error, "read", path) from None
        if not data:
            break
        chunks.append(data)
        filled += len(data)
    return b"".join(chunks)


def read(file: File, path: str, length: int) -> bytes:
    """Up to ``length`` bytes from the current position: one ``read`` call, as Node's ``readFile`` makes for files of
    unknown size (FIFOs, devices)."""
    while True:
        try:
            return file.read(length)
        except InterruptedError:
            continue
        except OSError as error:
            raise Failure.io(error, "read", path) from None


def dir_entry(directory: str, name_os: bytes | str) -> dict[str, Any]:
    """One directory entry as Node lists it: the name decoded as UTF-8 (invalid bytes replaced), and lstat of the
    directory joined with that decoded name, so a name that is not valid UTF-8 reports ``ENOENT`` as in Node. ``raw``
    carries the original bytes of such a name, which libuv sorts by."""
    name = system.to_string(name_os)
    entry_path = os.path.join(directory, name)
    try:
        entry: dict[str, Any] = {"name": name, "info": info(entry_path, os.lstat(system.os_path(entry_path)))}
    except (OSError, ValueError) as error:
        entry = {"name": name, "error": Failure.io(error, "lstat", entry_path).to_json()}
    raw = system.raw_name(name_os)
    if raw is not None and bytes(raw) != name.encode("utf-8"):
        entry["raw"] = raw
    return entry
