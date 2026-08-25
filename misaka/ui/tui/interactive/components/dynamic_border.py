"""Viewport-width border component for interactive selectors and panels."""

from __future__ import annotations

from collections.abc import Callable

from misaka.modes.interactive.theme.theme import theme


class DynamicBorder:
    def __init__(self, color: Callable[[str], str] | None = None) -> None:
        self.color = color or (lambda text: theme.fg("border", text))

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        return [self.color("─" * max(1, width))]


__all__ = ["DynamicBorder"]
