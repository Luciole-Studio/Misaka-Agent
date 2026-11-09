"""Translated from pi's ``modes/interactive/components/dynamic-border.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable

from misaka.modes.interactive.theme.theme import theme


class DynamicBorder:
    """Dynamic border component that adjusts to viewport width.

    Note: When used from extensions loaded via jiti, the global `theme` may be undefined
    because jiti creates a separate module cache. Always pass an explicit color
    function when using DynamicBorder in components exported for extension use.
    """

    def __init__(self, color: Callable[[str], str] = lambda str: theme.fg("border", str)) -> None:
        self.color = color

    def invalidate(self) -> None:
        # No cached state to invalidate currently
        pass

    def render(self, width: int) -> list[str]:
        return [self.color("─" * max(1, width))]
