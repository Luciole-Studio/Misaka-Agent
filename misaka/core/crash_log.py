"""Translated from pi's ``coding-agent/src/core/crash-log.ts`` (v1.1.0).

MISAKA fork: only the extension stack matching is ported. pi records crashes in ``crashes.json`` so its ``/bug`` command can
attach them to a report for the Pi developers; MISAKA has no ``/bug``, so ``CrashRecord``, ``readCrashLog``,
``recordCrash``, ``takeUnnotifiedCrash`` and ``clearCrashLog`` are not ported. Stacks are Python tracebacks, whose frame
lines read ``File "<path>", line N, in <name>``, where pi reads V8 ``at`` lines.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from misaka.core.source_info import isSyntheticPath
from misaka.utils.js import jsIndex


def normalizeStackPath(value: str) -> str:
    return re.sub(r"/+$", "", value.replace("\\", "/"))


def stackContainsPath(stack: str, targetPath: str, includeDescendants: bool) -> bool:
    target = normalizeStackPath(targetPath)
    if not target or isSyntheticPath(target):
        return False
    caseInsensitive = re.match(r"^[a-z]:/", target, re.IGNORECASE) is not None
    haystack = stack.lower() if caseInsensitive else stack
    needle = target.lower() if caseInsensitive else target
    if includeDescendants:
        return f"{needle}/" in haystack

    index = haystack.find(needle)
    while index != -1:
        next_ = jsIndex(haystack, index + len(needle))
        # MISAKA fork: a traceback quotes the path, so a closing quote ends it too.
        if next_ is None or next_ in (":", ")", '"') or next_.isspace():
            return True
        index = haystack.find(needle, index + len(needle))
    return False


def findExtensionStackMatches(stack: str | None, extensions: Sequence[Any]) -> list[str]:
    """Find loaded extensions with source files in a stack trace."""
    if not stack:
        return []
    # MISAKA fork: Python traceback frame lines; their paths are not URI-encoded, so nothing is decoded.
    normalizedStack = "\n".join(line for line in stack.split("\n")[1:] if re.match(r'^\s+File "', line)).replace(
        "\\", "/"
    )
    matches: list[str] = []
    seen: set[str] = set()

    for extension in extensions:
        sourceInfo = extension.sourceInfo
        resolvedPath = normalizeStackPath(extension.resolvedPath)
        # MISAKA fork: a single-file package is a Python file.
        singleFilePackage = (
            sourceInfo.origin == "package"
            and re.match(r"^(?:npm:|git:|https?://|ssh://)", sourceInfo.source) is None
            and re.search(r"\.py$", sourceInfo.source) is not None
        )
        packageRoot = (
            sourceInfo.baseDir if sourceInfo.origin == "package" and not singleFilePackage and sourceInfo.baseDir else None
        )
        slashIndex = resolvedPath.rfind("/")
        # MISAKA fork: a directory extension's entry is `index.py`.
        directoryEntry = re.search(r"/index\.py$", resolvedPath) is not None
        if packageRoot:
            matched = stackContainsPath(normalizedStack, packageRoot, True)
        elif directoryEntry and slashIndex != -1:
            matched = stackContainsPath(normalizedStack, resolvedPath[:slashIndex], True)
        else:
            matched = stackContainsPath(normalizedStack, resolvedPath, False)
        if not matched:
            continue

        label = sourceInfo.source if sourceInfo.origin == "package" and sourceInfo.source else extension.path
        if label not in seen:
            seen.add(label)
            matches.append(label)
    return matches


__all__ = ["findExtensionStackMatches", "normalizeStackPath", "stackContainsPath"]
