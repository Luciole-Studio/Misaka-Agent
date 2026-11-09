"""Translated from pi-tui's ``components/box.ts`` (v1.1.0).

MISAKA fork: no alt screen (D1), so the mouse half (``mouseLayout``, ``handleMouse`` and the child heights ``render``
records for it) is not ported; only the alt screen produces mouse events.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import flattenLines, visibleWidth


@dataclass(slots=True)
class RenderCache:
    childLines: list[str]
    width: int
    bgSample: str | None
    lines: list[str]


class Box(Component):
    """Box component - a container that applies padding and background to all children"""

    def __init__(self, paddingX: int = 1, paddingY: int = 1, bgFn: Callable[[str], str] | None = None) -> None:
        self.children: list[Component] = []
        self.paddingX = paddingX
        self.paddingY = paddingY
        self.bgFn = bgFn

        # Cache for rendered output
        self.cache: RenderCache | None = None

    def addChild(self, component: Component) -> None:
        self.children.append(component)
        self.invalidateCache()

    def removeChild(self, component: Component) -> None:
        index = next((i for i, child in enumerate(self.children) if child is component), -1)
        if index != -1:
            del self.children[index]
            self.invalidateCache()

    def clear(self) -> None:
        self.children = []
        self.invalidateCache()

    def setBgFn(self, bgFn: Callable[[str], str] | None = None) -> None:
        self.bgFn = bgFn
        # Don't invalidate here - we'll detect bgFn changes by sampling output

    def setPaddingX(self, paddingX: int) -> None:
        self.paddingX = paddingX
        self.invalidateCache()

    def invalidateCache(self) -> None:
        self.cache = None

    def matchCache(self, width: int, childLines: list[str], bgSample: str | None) -> bool:
        cache = self.cache
        return (
            cache is not None
            and cache.width == width
            and cache.bgSample == bgSample
            and len(cache.childLines) == len(childLines)
            and all(line == childLines[i] for i, line in enumerate(cache.childLines))
        )

    def invalidate(self) -> None:
        self.invalidateCache()
        for child in self.children:
            invalidate = getattr(child, "invalidate", None)
            if callable(invalidate):
                invalidate()

    def render(self, width: int) -> list[str]:
        if len(self.children) == 0:
            return []

        contentWidth = max(1, width - self.paddingX * 2)
        leftPad = " " * self.paddingX

        # Render all children. Keep the child lines unpadded: children usually return the same string
        # objects every frame, so the cache check below is a cheap identity comparison per line.
        # Padding here would create new strings that must be compared character by character.
        childLines: list[str] = []
        for child in self.children:
            lines = child.render(contentWidth)
            for line in lines:
                childLines.append(line)  # noqa: PERF402 - upstream loop

        if len(childLines) == 0:
            return []

        # Check if bgFn output changed by sampling
        bgSample = self.bgFn("test") if self.bgFn else None

        # Check cache validity
        if self.matchCache(width, childLines, bgSample):
            assert self.cache is not None
            return self.cache.lines

        # Apply background and padding
        result: list[str] = []

        # Top padding
        for _i in range(self.paddingY):
            result.append(self.applyBg("", width))

        # Content
        for line in childLines:
            result.append(self.applyBg(leftPad + line, width))

        # Bottom padding
        for _i in range(self.paddingY):
            result.append(self.applyBg("", width))

        # Update cache
        flattenLines(result)
        self.cache = RenderCache(childLines=childLines, width=width, bgSample=bgSample, lines=result)

        return result

    def applyBg(self, line: str, width: int) -> str:
        visLen = visibleWidth(line)
        padNeeded = max(0, width - visLen)
        padded = line + " " * padNeeded
        # Already padded to width, so apply the background directly instead of measuring the line again.
        return self.bgFn(padded) if self.bgFn else padded


__all__ = ["Box"]
