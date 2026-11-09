"""Tool path resolution.

Translated from pi's ``packages/durable/src/tools/path-utils.ts``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from misaka.chord.context import Context

from ..env import getOrThrow

UNICODE_SPACES = re.compile("[\u00a0\u2000-\u200a\u202f\u205f\u3000]")
NARROW_NO_BREAK_SPACE = "\u202f"
_MERIDIEM = re.compile(r" (AM|PM)\.", re.IGNORECASE)


def normalizeToolPath(path: str) -> str:
    normalized = UNICODE_SPACES.sub(" ", path)
    return normalized.removeprefix("@")


async def resolveToolPath(env: Any, path: str, context: Context) -> str:
    return getOrThrow(await env.absolutePath(normalizeToolPath(path), context))  # type: ignore[no-any-return]


async def resolveReadToolPath(env: Any, path: str, context: Context) -> str:
    resolved = await resolveToolPath(env, path, context)
    variants = [
        resolved,
        _MERIDIEM.sub(lambda match: f"{NARROW_NO_BREAK_SPACE}{match.group(1)}.", resolved),
        unicodedata.normalize("NFD", resolved),
        resolved.replace("'", "\u2019"),
        unicodedata.normalize("NFD", resolved).replace("'", "\u2019"),
    ]
    for variant in dict.fromkeys(variants):
        if getOrThrow(await env.exists(variant, context)):
            return variant
    return resolved


__all__ = ["resolveReadToolPath", "resolveToolPath"]
