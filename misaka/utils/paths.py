"""Path normalization helpers shared across coding-agent modules."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TypedDict
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

UNICODE_SPACES = re.compile(r"[\u00A0\u2000-\u200A\u202F\u205F\u3000]")


class PathInputOptions(TypedDict, total=False):
    trim: bool
    expandTilde: bool
    homeDir: str
    stripAtPrefix: bool
    normalizeUnicodeSpaces: bool


def canonicalize_path(path: str) -> str:
    try:
        return os.path.realpath(path, strict=True)
    except TypeError:
        if os.path.exists(path):
            return os.path.realpath(path)
    except OSError:
        return path
    return path


def is_local_path(value: str) -> bool:
    trimmed = value.strip()
    return not trimmed.startswith(("npm:", "git:", "github:", "http:", "https:", "ssh:"))


def normalize_path(
    value: str,
    *,
    trim: bool = False,
    expand_tilde: bool = True,
    home_dir: str | None = None,
    strip_at_prefix: bool = False,
    normalize_unicode_spaces: bool = False,
) -> str:
    normalized = value.strip() if trim else value
    if normalize_unicode_spaces:
        normalized = UNICODE_SPACES.sub(" ", normalized)
    if strip_at_prefix and normalized.startswith("@"):
        normalized = normalized[1:]

    if expand_tilde:
        home = home_dir or str(Path.home())
        if normalized == "~":
            return home
        if normalized.startswith("~/") or (os.name == "nt" and normalized.startswith("~\\")):
            return os.path.join(home, normalized[2:])

    if normalized.startswith("file://"):
        parsed = urlparse(normalized)
        return url2pathname(unquote(f"{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path))

    return normalized


def resolve_path(value: str, base_dir: str | None = None, **options: object) -> str:
    normalized = normalize_path(value, **options)
    normalized_base_dir = normalize_path(base_dir or os.getcwd())
    if os.path.isabs(normalized):
        return os.path.abspath(normalized)
    return os.path.abspath(os.path.join(normalized_base_dir, normalized))


def get_cwd_relative_path(file_path: str, cwd: str) -> str | None:
    resolved_cwd = resolve_path(cwd)
    resolved_path = resolve_path(file_path, resolved_cwd)
    relative_path = os.path.relpath(resolved_path, resolved_cwd)
    is_inside_cwd = relative_path == "." or (
        relative_path != ".." and not relative_path.startswith(f"..{os.sep}") and not os.path.isabs(relative_path)
    )
    return relative_path if is_inside_cwd else None


def format_path_relative_to_cwd_or_absolute(file_path: str, cwd: str) -> str:
    absolute_path = resolve_path(file_path, cwd)
    display = get_cwd_relative_path(absolute_path, cwd) or absolute_path
    return display.replace(os.sep, "/")


def mark_path_ignored_by_cloud_sync(path: str) -> None:
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


canonicalizePath = canonicalize_path
isLocalPath = is_local_path
normalizePath = normalize_path
resolvePath = resolve_path
getCwdRelativePath = get_cwd_relative_path
formatPathRelativeToCwdOrAbsolute = format_path_relative_to_cwd_or_absolute
markPathIgnoredByCloudSync = mark_path_ignored_by_cloud_sync

__all__ = [
    "PathInputOptions",
    "canonicalizePath",
    "formatPathRelativeToCwdOrAbsolute",
    "getCwdRelativePath",
    "isLocalPath",
    "markPathIgnoredByCloudSync",
    "normalizePath",
    "resolvePath",
]
