"""Translated from pi's ``packages/coding-agent/src/core/pi-manifest.ts`` (v1.1.0)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict, TypeGuard, cast

from misaka.utils.text import stripBom


class PiManifest(TypedDict, total=False):
    extensions: list[str]
    skills: list[str]
    prompts: list[str]
    themes: list[str]


RESOURCE_FIELDS = ("extensions", "skills", "prompts", "themes")


def isObject(value: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


_JSON_NUMBER = object()


def _rejectNonJsonConstant(value: str) -> None:
    raise ValueError(value)


def _parseJsonNumber(_value: str) -> object:
    # `JSON.parse`: a manifest only tells strings from the rest. A sentinel also avoids Python's integer digit limit,
    # which JavaScript lacks, and NaN/Infinity are refused as JSON.parse refuses them.
    return _JSON_NUMBER


def readPiManifest(packageJsonPath: str) -> PiManifest | None:
    try:
        pkg: Any = json.loads(
            stripBom(Path(packageJsonPath).read_bytes().decode("utf-8", errors="replace")),
            parse_constant=_rejectNonJsonConstant,
            parse_float=_parseJsonNumber,
            parse_int=_parseJsonNumber,
        )
        if not isObject(pkg) or not isObject(pkg.get("pi")):
            return None

        manifest: dict[str, list[str]] = {}
        for field in RESOURCE_FIELDS:
            entries = pkg["pi"].get(field)
            if isinstance(entries, list) and all(isinstance(entry, str) for entry in entries):
                manifest[field] = entries
        return cast(PiManifest, manifest)
    except Exception:  # noqa: BLE001 - pi treats every manifest error as absent
        return None


__all__ = ["PiManifest", "readPiManifest"]
