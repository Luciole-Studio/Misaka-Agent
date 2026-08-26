"""Component that renders a fixed number of blank lines."""

from __future__ import annotations

from misaka.ui.tui.tui import Component


class Spacer(Component):
    def __init__(self, lines: int = 1) -> None:
        self.lines = lines

    def setLines(self, lines: int) -> None:
        self.lines = lines

    def invalidate(self) -> None:
        return None

    def render(self, _width: int) -> list[str]:
        return ["" for _ in range(self.lines)]


__all__ = ["Spacer"]
