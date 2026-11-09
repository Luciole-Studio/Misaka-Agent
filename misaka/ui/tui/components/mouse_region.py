"""Translated from pi-tui's ``components/mouse-region.ts`` (v1.1.0).

MISAKA fork: no alt screen (D1), and only the alt screen produces mouse events, so ``handleMouse`` is not ported and
the handler is kept but never called; the region renders its child unchanged, as upstream's does.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.ui.tui.tui import Component

type MouseRegionHandler = Callable[[Any], Any]


class MouseRegion(Component):
    """Adds mouse handling to an existing component without changing its rendering."""

    def __init__(self, child: Component, onMouse: MouseRegionHandler) -> None:
        self.child = child
        self.onMouse = onMouse

    def render(self, width: int) -> list[str]:
        return self.child.render(width)

    def invalidate(self) -> None:
        self.child.invalidate()


__all__ = ["MouseRegion", "MouseRegionHandler"]
