"""Read Pi resource manifests from ``package.json`` files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, cast


class PiManifest(TypedDict, total=False):
    extensions: list[str]
    skills: list[str]
    prompts: list[str]
    themes: list[str]


_RESOURCE_FIELDS = ("extensions", "skills", "prompts", "themes")
_JSON_NUMBER = object()


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(value)


def _parse_json_number(_value: str) -> object:
    # Manifest validation only distinguishes strings from non-strings. A sentinel
    # also avoids Python's integer digit limit, which JavaScript JSON.parse lacks.
    return _JSON_NUMBER


def read_pi_manifest(package_json_path: str) -> PiManifest | None:
    """Python equivalent of Pi's ``readPiManifest``."""
    try:
        content = Path(package_json_path).read_bytes().decode("utf-8", errors="replace")
        package: object = json.loads(
            content.removeprefix("\ufeff"),
            parse_constant=_reject_non_json_constant,
            parse_float=_parse_json_number,
            parse_int=_parse_json_number,
        )
    except Exception:  # noqa: BLE001 - Pi intentionally treats every manifest error as absent
        return None

    if not isinstance(package, dict):
        return None
    pi_section = package.get("pi")
    if not isinstance(pi_section, dict):
        return None

    manifest: dict[str, list[str]] = {}
    for field in _RESOURCE_FIELDS:
        entries = pi_section.get(field)
        if isinstance(entries, list) and all(
            isinstance(entry, str) for entry in entries
        ):
            manifest[field] = entries
    return cast(PiManifest, manifest)


readPiManifest = read_pi_manifest

__all__ = ["PiManifest", "readPiManifest", "read_pi_manifest"]
