"""Translated from pi-tui's ``components/spacer.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ui.tui.tui import Component


class Spacer(Component):
    """Spacer component that renders empty lines"""

    def __init__(self, lines: int = 1) -> None:
        self.lines = lines

    def setLines(self, lines: int) -> None:
        self.lines = lines

    def invalidate(self) -> None:
        # No cached state to invalidate currently
        pass

    def render(self, _width: int) -> list[str]:
        result: list[str] = []
        for _i in range(self.lines):
            result.append("")
        return result


__all__ = ["Spacer"]
