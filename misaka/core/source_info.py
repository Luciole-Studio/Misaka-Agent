"""Source metadata helpers for coding-agent resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

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


def create_source_info(path: str, metadata: PathMetadata) -> SourceInfo:
    return SourceInfo(
        path=path,
        source=metadata["source"],  # type: ignore[arg-type]
        scope=metadata["scope"],  # type: ignore[arg-type]
        origin=metadata["origin"],  # type: ignore[arg-type]
        baseDir=metadata.get("baseDir"),  # type: ignore[arg-type]
    )


def create_synthetic_source_info(
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


createSyntheticSourceInfo = create_synthetic_source_info

__all__ = [
    "SourceInfo",
    "SourceOrigin",
    "SourceScope",
    "createSyntheticSourceInfo",
]
