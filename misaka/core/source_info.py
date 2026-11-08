"""Source metadata helpers for coding-agent resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from misaka.utils.paths import canonicalizePath, getPathComparisonKey

if TYPE_CHECKING:
    from misaka.core.package_manager import PathMetadata

SourceScope = Literal["user", "project", "temporary"]
SourceOrigin = Literal["package", "top-level"]


class _SyntheticSourceInfoOptions(TypedDict):
    source: str
    scope: NotRequired[SourceScope]
    origin: NotRequired[SourceOrigin]
    baseDir: NotRequired[str | None]


@dataclass(slots=True)
class SourceInfo:
    path: str
    source: str
    scope: SourceScope
    origin: SourceOrigin
    baseDir: str | None = None


# Prefix of built-in tool and extension paths, such as `builtin:read` or `builtin:mcp`.
BUILTIN_PATH_PREFIX = "builtin:"


def getSyntheticPathSource(path: str) -> str | None:
    """Source of a path that names no file: ``builtin`` for ``builtin:<name>``, or the prefix of an
    angle-bracket path such as ``inline`` for ``<inline:name>``. None for file paths."""
    if path.startswith(BUILTIN_PATH_PREFIX):
        return "builtin"
    if path.startswith("<") and path.endswith(">"):
        return path[1:-1].split(":")[0] or "temporary"
    return None


def isSyntheticPath(path: str) -> bool:
    return path.startswith((BUILTIN_PATH_PREFIX, "<"))


def getResourcePathKey(path: str) -> str:
    """Key for deduplicating resource paths: synthetic paths as-is, file paths via realpath and platform casing.
    (pi v1.5.0 ``source-info.ts``.)"""
    return path if isSyntheticPath(path) else getPathComparisonKey(canonicalizePath(path))


def createSourceInfo(path: str, metadata: PathMetadata) -> SourceInfo:
    return SourceInfo(
        path=path,
        source=metadata["source"],  # type: ignore[arg-type]
        scope=metadata["scope"],  # type: ignore[arg-type]
        origin=metadata["origin"],  # type: ignore[arg-type]
        baseDir=metadata.get("baseDir"),  # type: ignore[arg-type]
    )


def createSyntheticSourceInfo(
    path: str,
    options: _SyntheticSourceInfoOptions,
) -> SourceInfo:
    return SourceInfo(
        path=path,
        source=options["source"],
        scope=options.get("scope", "temporary"),  # type: ignore[arg-type]
        origin=options.get("origin", "top-level"),  # type: ignore[arg-type]
        baseDir=options.get("baseDir"),
    )



__all__ = [
    "BUILTIN_PATH_PREFIX",
    "SourceInfo",
    "SourceOrigin",
    "SourceScope",
    "createSyntheticSourceInfo",
    "getResourcePathKey",
    "getSyntheticPathSource",
    "isSyntheticPath",
]
