"""CHANGELOG parsing helpers shared across coding-agent modules."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from misaka.config import get_changelog_path


@dataclass(slots=True)
class ChangelogEntry:
    major: int
    minor: int
    patch: int
    content: str


def parse_changelog(changelog_path: str) -> list[ChangelogEntry]:
    path = Path(changelog_path)
    if not path.exists():
        return []

    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except Exception as error:  # noqa: BLE001
        print(f"Warning: Could not parse changelog: {error}", file=sys.stderr)
        return []

    entries: list[ChangelogEntry] = []
    current_lines: list[str] = []
    current_version: tuple[int, int, int] | None = None

    for line in lines:
        if line.startswith("## "):
            if current_version is not None and current_lines:
                entries.append(
                    ChangelogEntry(
                        major=current_version[0],
                        minor=current_version[1],
                        patch=current_version[2],
                        content="\n".join(current_lines).strip(),
                    )
                )

            current_version = _parse_version_header(line)
            current_lines = [line] if current_version is not None else []
        elif current_version is not None:
            current_lines.append(line)

    if current_version is not None and current_lines:
        entries.append(
            ChangelogEntry(
                major=current_version[0],
                minor=current_version[1],
                patch=current_version[2],
                content="\n".join(current_lines).strip(),
            )
        )

    return entries


def _parse_version_header(line: str) -> tuple[int, int, int] | None:
    import re

    match = re.match(r"##\s+\[?(\d+)\.(\d+)\.(\d+)\]?", line)
    if match is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
    )


def compare_versions(v1: ChangelogEntry, v2: ChangelogEntry) -> int:
    if v1.major != v2.major:
        return v1.major - v2.major
    if v1.minor != v2.minor:
        return v1.minor - v2.minor
    return v1.patch - v2.patch


def get_new_entries(entries: list[ChangelogEntry], last_version: str) -> list[ChangelogEntry]:
    parts = [int(part) if part.isdigit() else 0 for part in last_version.split(".")]
    last = ChangelogEntry(
        major=parts[0] if len(parts) > 0 else 0,
        minor=parts[1] if len(parts) > 1 else 0,
        patch=parts[2] if len(parts) > 2 else 0,
        content="",
    )
    return [entry for entry in entries if compare_versions(entry, last) > 0]


getChangelogPath = get_changelog_path

__all__ = [
    "ChangelogEntry",
    "getChangelogPath",
    ]
