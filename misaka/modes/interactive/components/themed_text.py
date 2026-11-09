"""Translated from pi's ``modes/interactive/components/themed-text.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable

from misaka.ui.tui import Text


class ThemedText(Text):
    """Text whose content applies theme colors. Plain `Text` keeps the colors its string was built with, so
    a theme change, or the system theme receiving the terminal's colors, would leave it stale. This
    rebuilds the string from `build` after every invalidation, which the UI performs on theme changes.

    `build` must return the same content each time, apart from colors. Snapshot changing data before
    creating the component, or call `invalidate()` after changing state that `build` reads.
    """

    def __init__(self, build: Callable[[], str], paddingX: int = 1, paddingY: int = 1) -> None:
        super().__init__("", paddingX, paddingY)
        self.build = build
        self.stale = True

    def invalidate(self) -> None:
        super().invalidate()
        self.stale = True

    def render(self, width: int) -> list[str]:
        if self.stale:
            self.stale = False
            self.setText(self.build())
        return super().render(width)
