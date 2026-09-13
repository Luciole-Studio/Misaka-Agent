"""Container component that applies padding and optional background."""

from __future__ import annotations

from dataclasses import dataclass

from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import visibleWidth


@dataclass(slots=True)
class RenderCache:
    childLines: list[str]
    width: int
    bgSample: str | None
    lines: list[str]


class Box(Component):
    def __init__(self, paddingX: int = 1, paddingY: int = 1, bgFn: callable | None = None) -> None:
        self.children: list[Component] = []
        self.paddingX = paddingX
        self.paddingY = paddingY
        self.bgFn = bgFn
        self.cache: RenderCache | None = None

    def addChild(self, component: Component) -> None:
        self.children.append(component)
        self.invalidateCache()

    def removeChild(self, component: Component) -> None:
        if component in self.children:
            self.children.remove(component)
            self.invalidateCache()

    def clear(self) -> None:
        self.children.clear()
        self.invalidateCache()

    def setBgFn(self, bgFn: callable | None = None) -> None:
        self.bgFn = bgFn

    def invalidateCache(self) -> None:
        self.cache = None

    def matchCache(self, width: int, childLines: list[str], bgSample: str | None) -> bool:
        cache = self.cache
        return bool(
            cache
            and cache.width == width
            and cache.bgSample == bgSample
            and len(cache.childLines) == len(childLines)
            and all(line == childLines[index] for index, line in enumerate(cache.childLines))
        )

    def invalidate(self) -> None:
        self.invalidateCache()
        for child in self.children:
            invalidate = getattr(child, "invalidate", None)
            if callable(invalidate):
                invalidate()

    def render(self, width: int) -> list[str]:
        if not self.children:
            return []

        content_width = max(1, width - self.paddingX * 2)
        left_pad = " " * self.paddingX
        child_lines: list[str] = []
        for child in self.children:
            for line in child.render(content_width):
                child_lines.append(left_pad + line)

        if not child_lines:
            return []

        bg_sample = self.bgFn("test") if callable(self.bgFn) else None
        if self.matchCache(width, child_lines, bg_sample):
            return self.cache.lines if self.cache is not None else []

        result: list[str] = []
        for _index in range(self.paddingY):
            result.append(self.applyBg("", width))
        for line in child_lines:
            result.append(self.applyBg(line, width))
        for _index in range(self.paddingY):
            result.append(self.applyBg("", width))

        self.cache = RenderCache(childLines=child_lines, width=width, bgSample=bg_sample, lines=result)
        return result

    def applyBg(self, line: str, width: int) -> str:
        padded = line + (" " * max(0, width - visibleWidth(line)))
        if callable(self.bgFn):
            # pi: applyBackgroundToLine(padded, width, bgFn), which measures the padded line
            # once more only to find nothing left to pad; the measurement is the cost.
            return self.bgFn(padded)
        return padded


__all__ = ["Box"]
