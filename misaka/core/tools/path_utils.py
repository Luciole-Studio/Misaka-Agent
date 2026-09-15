"""Path resolution helpers for file-oriented tools."""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata

from misaka.utils.paths import normalize_path, resolve_path

NARROW_NO_BREAK_SPACE = "\u202f"

# The workspace directory everything pulled off the internet lands in: files from
# download_file, page text from web_fetch and web_extract under ``pages/``. Named here
# rather than in either of them because both need it and neither owns the other -- an
# import in either direction is the circle this constant used to create.
DOWNLOAD_DIR_NAME = "downloads"


def try_macos_screenshot_path(file_path: str) -> str:
    return re.sub(
        r" (AM|PM)\.", rf"{NARROW_NO_BREAK_SPACE}\1.", file_path, flags=re.IGNORECASE
    )


def try_nfd_variant(file_path: str) -> str:
    return unicodedata.normalize("NFD", file_path)


def try_curly_quote_variant(file_path: str) -> str:
    return file_path.replace("'", "\u2019")


def file_exists(file_path: str) -> bool:
    return os.path.exists(file_path)


async def path_exists(file_path: str) -> bool:
    if not isinstance(file_path, str):
        return False
    try:
        return await asyncio.to_thread(file_exists, file_path)
    except Exception:  # noqa: BLE001 - existence checks map lookup failures to false
        return False


def expand_path(file_path: str) -> str:
    return normalize_path(
        file_path,
        normalize_unicode_spaces=True,
        strip_at_prefix=True,
    )


def resolve_to_cwd(file_path: str, cwd: str) -> str:
    return resolve_path(
        file_path, cwd, normalize_unicode_spaces=True, strip_at_prefix=True
    )


def resolve_read_path(file_path: str, cwd: str) -> str:
    resolved = resolve_to_cwd(file_path, cwd)
    from misaka.core.tools._web.evidence import check_material_read
    check_material_read(resolved)
    if file_exists(resolved):
        return resolved

    am_pm_variant = try_macos_screenshot_path(resolved)
    if am_pm_variant != resolved and file_exists(am_pm_variant):
        check_material_read(am_pm_variant)
        return am_pm_variant

    nfd_variant = try_nfd_variant(resolved)
    if nfd_variant != resolved and file_exists(nfd_variant):
        check_material_read(nfd_variant)
        return nfd_variant

    curly_variant = try_curly_quote_variant(resolved)
    if curly_variant != resolved and file_exists(curly_variant):
        check_material_read(curly_variant)
        return curly_variant

    nfd_curly_variant = try_curly_quote_variant(nfd_variant)
    if nfd_curly_variant != resolved and file_exists(nfd_curly_variant):
        check_material_read(nfd_curly_variant)
        return nfd_curly_variant

    return resolved


async def resolve_read_path_async(file_path: str, cwd: str) -> str:
    """Resolve read fallbacks without probing the filesystem on the event loop."""
    return await asyncio.to_thread(resolve_read_path, file_path, cwd)


pathExists = path_exists
expandPath = expand_path
resolveToCwd = resolve_to_cwd
resolveReadPath = resolve_read_path
resolveReadPathAsync = resolve_read_path_async


__all__ = [
    "DOWNLOAD_DIR_NAME",
    "expandPath",
    "expand_path",
    "file_exists",
    "pathExists",
    "path_exists",
    "resolveReadPath",
    "resolveReadPathAsync",
    "resolveToCwd",
    "resolve_read_path",
    "resolve_read_path_async",
    "resolve_to_cwd",
    "try_curly_quote_variant",
    "try_macos_screenshot_path",
    "try_nfd_variant",
]
