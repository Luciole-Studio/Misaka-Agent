"""Translated from pi's ``packages/codemode/src/identifier.ts``."""

from __future__ import annotations

import re

_FIRST = re.compile(r"^[A-Za-z_$]$")
_REST = re.compile(r"^[A-Za-z0-9_$]$")


def toCodemodeIdentifier(name: str) -> str:
    """The identifier a script uses for a tool: characters that are not valid in a JavaScript
    identifier become ``_``. ``mcp__docs__search`` stays as is, ``my-tool`` becomes ``my_tool``."""
    identifier = ""
    for char in name:
        pattern = _FIRST if identifier == "" else _REST
        identifier += char if pattern.match(char) else "_"
    return "_" if identifier == "" else identifier


__all__ = ["toCodemodeIdentifier"]
