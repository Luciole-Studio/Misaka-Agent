"""Translated from grok-mermaid 0.2.3's ``src/ansi.ts``."""

from __future__ import annotations

from typing import Final

from .types import Cls, MermaidArt

ESC: Final = chr(27)

# SGR parameter per semantic class, e.g. `'2'` for dim, `'36'` for cyan,
# `'38;5;244'` for a 256-colour index. A class left out is printed unstyled.
type AnsiTheme = dict[Cls, str]

# Dim frame, plain labels, cyan connectors. Readable on light and dark.
DEFAULT_THEME: Final[AnsiTheme] = {
    "border": "2",
    "edge": "36",
    "edgeLabel": "2;36",
    "title": "1",
}


def toAnsi(art: MermaidArt, theme: AnsiTheme = DEFAULT_THEME) -> list[str]:
    """Render art to ANSI-coloured lines.

    A convenience over mapping ``art.styled`` yourself — reach for that directly
    when your TUI has its own styling model.
    """
    lines: list[str] = []
    for row in art.styled:
        parts: list[str] = []
        for span in row:
            sgr = theme.get(span.cls)
            parts.append(span.text if sgr is None else f"{ESC}[{sgr}m{span.text}{ESC}[0m")
        lines.append("".join(parts))
    return lines


__all__ = ["DEFAULT_THEME", "AnsiTheme", "toAnsi"]
