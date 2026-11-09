"""Path resolution helpers for file-oriented tools."""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata

from misaka.utils.paths import normalizePath, resolvePath

NARROW_NO_BREAK_SPACE = "\u202f"

# The workspace directory everything pulled off the internet lands in: files from
# download_file, page text from web_fetch and web_extract under ``pages/``. Named here
# rather than in either of them because both need it and neither owns the other -- an
# import in either direction is the circle this constant used to create.
DOWNLOAD_DIR_NAME = "downloads"


def tryMacOSScreenshotPath(file_path: str) -> str:
    return re.sub(
        r" (AM|PM)\.", rf"{NARROW_NO_BREAK_SPACE}\1.", file_path, flags=re.IGNORECASE
    )


def tryNFDVariant(file_path: str) -> str:
    return unicodedata.normalize("NFD", file_path)


def tryCurlyQuoteVariant(file_path: str) -> str:
    return file_path.replace("'", "\u2019")


def fileExists(file_path: str) -> bool:
    return os.path.exists(file_path)


async def pathExists(file_path: str) -> bool:
    if not isinstance(file_path, str):
        return False
    try:
        return await asyncio.to_thread(fileExists, file_path)
    except Exception:  # noqa: BLE001 - existence checks map lookup failures to false
        return False


def expandPath(file_path: str) -> str:
    return normalizePath(file_path, {"normalizeUnicodeSpaces": True, "stripAtPrefix": True})


def resolveToCwd(file_path: str, cwd: str) -> str:
    return resolvePath(file_path, cwd, {"normalizeUnicodeSpaces": True, "stripAtPrefix": True})


def resolveReadPath(file_path: str, cwd: str) -> str:
    resolved = resolveToCwd(file_path, cwd)
    from misaka.core.web.evidence import check_material_read
    check_material_read(resolved)
    if fileExists(resolved):
        return resolved

    am_pm_variant = tryMacOSScreenshotPath(resolved)
    if am_pm_variant != resolved and fileExists(am_pm_variant):
        check_material_read(am_pm_variant)
        return am_pm_variant

    nfd_variant = tryNFDVariant(resolved)
    if nfd_variant != resolved and fileExists(nfd_variant):
        check_material_read(nfd_variant)
        return nfd_variant

    curly_variant = tryCurlyQuoteVariant(resolved)
    if curly_variant != resolved and fileExists(curly_variant):
        check_material_read(curly_variant)
        return curly_variant

    nfd_curly_variant = tryCurlyQuoteVariant(nfd_variant)
    if nfd_curly_variant != resolved and fileExists(nfd_curly_variant):
        check_material_read(nfd_curly_variant)
        return nfd_curly_variant

    return resolved


async def resolveReadPathAsync(file_path: str, cwd: str) -> str:
    """Resolve read fallbacks without probing the filesystem on the event loop."""
    return await asyncio.to_thread(resolveReadPath, file_path, cwd)




__all__ = [
    "DOWNLOAD_DIR_NAME",
    "expandPath",
    "fileExists",
    "pathExists",
    "resolveReadPath",
    "resolveReadPathAsync",
    "resolveToCwd",
    "tryCurlyQuoteVariant",
    "tryMacOSScreenshotPath",
    "tryNFDVariant",
]
