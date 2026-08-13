"""Path resolution helpers for file-oriented tools."""

from __future__ import annotations

import os
import re
import unicodedata

from misaka.utils.paths import normalize_path, resolve_path

NARROW_NO_BREAK_SPACE = "\u202F"


def try_macos_screenshot_path(file_path: str) -> str:
    return re.sub(r" (AM|PM)\.", rf"{NARROW_NO_BREAK_SPACE}\1.", file_path, flags=re.IGNORECASE)


def try_nfd_variant(file_path: str) -> str:
    return unicodedata.normalize("NFD", file_path)


def try_curly_quote_variant(file_path: str) -> str:
    return file_path.replace("'", "\u2019")


def file_exists(file_path: str) -> bool:
    return os.path.exists(file_path)


def expand_path(file_path: str) -> str:
    return normalize_path(file_path, normalize_unicode_spaces=True, strip_at_prefix=True)


def resolve_to_cwd(file_path: str, cwd: str) -> str:
    return resolve_path(file_path, cwd, normalize_unicode_spaces=True, strip_at_prefix=True)


def resolve_read_path(file_path: str, cwd: str) -> str:
    resolved = resolve_to_cwd(file_path, cwd)
    if file_exists(resolved):
        return resolved

    am_pm_variant = try_macos_screenshot_path(resolved)
    if am_pm_variant != resolved and file_exists(am_pm_variant):
        return am_pm_variant

    nfd_variant = try_nfd_variant(resolved)
    if nfd_variant != resolved and file_exists(nfd_variant):
        return nfd_variant

    curly_variant = try_curly_quote_variant(resolved)
    if curly_variant != resolved and file_exists(curly_variant):
        return curly_variant

    nfd_curly_variant = try_curly_quote_variant(nfd_variant)
    if nfd_curly_variant != resolved and file_exists(nfd_curly_variant):
        return nfd_curly_variant

    return resolved

expandPath = expand_path
resolveToCwd = resolve_to_cwd
resolveReadPath = resolve_read_path

__all__ = ["expandPath", "resolveReadPath", "resolveToCwd"]
