"""Translated from pi's ``packages/coding-agent/src/utils/paths.ts`` (v1.1.0).

MISAKA forks:
- ``getFileRevision`` is a tuple, compared as pi compares its string, and on Windows it uses the creation time where
  POSIX uses the change time (``file_revision_from_stat``), so a ``stat`` and an ``fstat`` agree.
- ``posix_relpath`` and ``sys_platform`` are MISAKA's helpers.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TypedDict
from urllib.parse import unquote, urlsplit

from misaka.utils import node_path
from misaka.utils.js import jsTrim

UNICODE_SPACES = re.compile(r"[\u00A0\u2000-\u200A\u202F\u205F\u3000]")
_WINDOWS_SHELL_DRIVE = re.compile(r"^/(?:mnt/|cygdrive/)?([a-zA-Z])(?:/(.*))?$")


class PathInputOptions(TypedDict, total=False):
    trim: bool
    """Trim leading/trailing whitespace before normalization."""
    expandTilde: bool
    """Expand leading `~` to a home directory. Defaults to true."""
    homeDir: str
    """Home directory used for `~` expansion. Defaults to `os.homedir()`."""
    stripAtPrefix: bool
    """Strip a leading `@`, used for CLI @file paths."""
    normalizeUnicodeSpaces: bool
    """Normalize unicode space variants to regular spaces."""


def canonicalizePath(path: str) -> str:
    # `strict=` has existed since 3.10 and this package requires >=3.12, so there is no
    # interpreter that answers this call with TypeError; the only failure is the OSError
    # a missing or unresolvable path raises, and that answer is the path unchanged.
    try:
        return os.path.realpath(path, strict=True)
    except OSError:
        return path


def getFileRevision(path: str) -> tuple[int, int, int, int, int] | None:
    """Identity of the bytes currently at ``path`` (pi paths.ts:36-42).

    ``os.stat`` follows symlinks and imposes no shape filters, on purpose. An earlier
    revision of this helper used ``os.lstat`` plus ``S_ISREG``/``st_nlink == 1``/
    ``st_size > 0`` guards; each of those made a perfectly ordinary credential file
    (``~/.misaka/auth.json`` symlinked into a dotfiles repo, hard-linked by a backup
    tool, or momentarily zero bytes mid-write) return ``None`` forever, which silently
    disabled the concurrent-edit detection in ``core/auth_storage.py``. ``None`` here
    means only "cannot stat it".
    """
    try:
        stats = os.stat(path)
    except OSError:
        return None
    return file_revision_from_stat(stats)


def file_revision_from_stat(stats: os.stat_result) -> tuple[int, int, int, int, int]:
    """The revision ``getFileRevision`` reads, from a ``stat`` or an ``fstat`` alike.

    On Windows CPython (3.12+) fills ``st_ctime`` with the creation time from ``os.stat`` but
    with the last metadata change from ``os.fstat``, so once a file has been renamed into place
    the two never agree; the creation time both report stands in there.
    """
    changed = stats.st_birthtime_ns if os.name == "nt" else stats.st_ctime_ns
    return stats.st_dev, stats.st_ino, stats.st_size, stats.st_mtime_ns, changed


def isLocalPath(value: str) -> bool:
    """Returns true if the value is NOT a package source (npm:, git:, etc.),
    a built-in extension (builtin:), or a remote URL protocol. Bare names,
    relative paths, and file: URLs are considered local.
    """
    trimmed = jsTrim(value)
    # Known non-local prefixes. file: URLs are local paths and are intentionally resolved by resolvePath().
    return not trimmed.startswith(("npm:", "git:", "github:", "http:", "https:", "ssh:", "builtin:"))


def normalizeWindowsShellPath(filePath: str) -> str:
    """Git Bash, MSYS, Cygwin and WSL drive paths in the form native Windows APIs accept (pi paths.ts:66-73)."""
    if not filePath.startswith("/") or filePath.startswith("//") or "\\" in filePath:
        return filePath
    match = _WINDOWS_SHELL_DRIVE.match(filePath)
    if match is None:
        return filePath
    suffix = (match[2] or "").replace("/", "\\")
    return f"{match[1].upper()}:\\{suffix}"


def _fileURLToPath(url: str) -> str:
    """``url.fileURLToPath``: a file URL's host must be empty or ``localhost`` (on Windows another host is a UNC share),
    an encoded separator is refused, and on Windows the path must start with a drive."""
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    if hostname == "localhost":
        hostname = ""
    pathname = parts.path
    if os.name == "nt":
        if "%2f" in pathname.lower() or "%5c" in pathname.lower():
            raise TypeError("File URL path must not include encoded \\ or / characters")
        pathname = unquote(pathname).replace("/", "\\")
        if hostname:
            return f"\\\\{hostname}{pathname}"
        if len(pathname) < 3 or not pathname[1].isascii() or not pathname[1].isalpha() or pathname[2] != ":":
            raise TypeError("File URL path must be absolute")
        return pathname[1:]
    if hostname:
        raise TypeError(f'File URL host must be "localhost" or empty on {sys_platform()}')
    if "%2f" in pathname.lower():
        raise TypeError("File URL path must not include encoded / characters")
    return unquote(pathname)


def homedir() -> str:
    """Node's ``os.homedir()`` (libuv ``uv_os_homedir``): ``HOME`` when it is set, on every platform, else the user's
    profile. ``Path.home()`` differs on Windows, where it ignores ``HOME``."""
    return os.environ.get("HOME") or str(Path.home())


def normalizePath(input: str, options: PathInputOptions | None = None) -> str:
    options = options if options is not None else {}
    normalized = jsTrim(input) if options.get("trim") else input
    if options.get("normalizeUnicodeSpaces"):
        normalized = UNICODE_SPACES.sub(" ", normalized)
    if options.get("stripAtPrefix") and normalized.startswith("@"):
        normalized = normalized[1:]
    if os.name == "nt":
        normalized = normalizeWindowsShellPath(normalized)

    if options.get("expandTilde", True) is not False:
        home = options.get("homeDir") if options.get("homeDir") is not None else homedir()
        if normalized == "~":
            return home
        if normalized.startswith("~/") or (os.name == "nt" and normalized.startswith("~\\")):
            return node_path.join(home, normalized[2:])

    if normalized.startswith("file://"):
        return _fileURLToPath(normalized)

    return normalized


def resolvePath(input: str, baseDir: str | None = None, options: PathInputOptions | None = None) -> str:
    normalized = normalizePath(input, options)
    normalizedBaseDir = normalizePath(baseDir if baseDir is not None else os.getcwd())
    return node_path.resolve(normalized) if node_path.isAbsolute(normalized) else node_path.resolve(normalizedBaseDir, normalized)


def getCwdRelativePath(filePath: str, cwd: str) -> str | None:
    resolvedCwd = resolvePath(cwd)
    resolvedPath = resolvePath(filePath, resolvedCwd)
    relativePath = node_path.relative(resolvedCwd, resolvedPath)
    isInsideCwd = relativePath == "" or (
        relativePath != ".." and not relativePath.startswith(f"..{node_path.sep}") and not node_path.isAbsolute(relativePath)
    )

    return (relativePath or ".") if isInsideCwd else None


def formatPathRelativeToCwdOrAbsolute(filePath: str, cwd: str) -> str:
    absolutePath = resolvePath(filePath, cwd)
    found = getCwdRelativePath(absolutePath, cwd)
    return "/".join((found if found is not None else absolutePath).split(node_path.sep))


def markPathIgnoredByCloudSync(path: str) -> None:
    if sys_platform() == "darwin":
        attrs = ["com.dropbox.ignored", "com.apple.fileprovider.ignore#P"]
        for attr in attrs:
            _best_effort_run(["xattr", "-w", attr, "1", path])
    elif sys_platform() == "linux":
        _best_effort_run(["setfattr", "-n", "user.com.dropbox.ignored", "-v", "1", path])


def _best_effort_run(command: list[str]) -> None:
    try:
        subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return


def sys_platform() -> str:
    if os.name == "nt":
        return "win32"
    return os.uname().sysname.lower()


def posix_relpath(path: str | os.PathLike[str], start: str | os.PathLike[str]) -> str:
    """``os.path.relpath`` with ``/`` between the parts on every platform: the form a relative
    path takes in a board row, a project file or a prompt, so what Windows writes reads the
    same everywhere (the grep and find tools already report paths this way)."""
    return os.path.relpath(path, start).replace(os.sep, "/")



__all__ = [
    "PathInputOptions",
    "canonicalizePath",
    "file_revision_from_stat",
    "formatPathRelativeToCwdOrAbsolute",
    "getCwdRelativePath",
    "getFileRevision",
    "isLocalPath",
    "markPathIgnoredByCloudSync",
    "normalizePath",
    "posix_relpath",
    "resolvePath",
]
