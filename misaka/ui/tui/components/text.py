"""Multi-line text display with wrapping and optional background."""

from __future__ import annotations

from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import applyBackgroundToLine, visibleWidth, wrapTextWithAnsi


class Text(Component):
    def __init__(
        self,
        text: str = "",
        paddingX: int = 1,
        paddingY: int = 1,
        customBgFn: callable | None = None,
    ) -> None:
        self.text = text
        self.paddingX = paddingX
        self.paddingY = paddingY
        self.customBgFn = customBgFn
        self.cachedText: str | None = None
        self.cachedWidth: int | None = None
        self.cachedLines: list[str] | None = None

    def setText(self, text: str) -> None:
        self.text = text
        self.cachedText = None
        self.cachedWidth = None
        self.cachedLines = None

    def setCustomBgFn(self, customBgFn: callable | None = None) -> None:
        self.customBgFn = customBgFn
        self.cachedText = None
        self.cachedWidth = None
        self.cachedLines = None

    def invalidate(self) -> None:
        self.cachedText = None
        self.cachedWidth = None
        self.cachedLines = None

    def render(self, width: int) -> list[str]:
        if self.cachedLines is not None and self.cachedText == self.text and self.cachedWidth == width:
            return self.cachedLines

        if not self.text or self.text.strip() == "":
            result: list[str] = []
            self.cachedText = self.text
            self.cachedWidth = width
            self.cachedLines = result
            return result

        normalized_text = self.text.replace("\t", "   ")
        content_width = max(1, width - self.paddingX * 2)
        wrapped_lines = wrapTextWithAnsi(normalized_text, content_width)

        left_margin = " " * self.paddingX
        right_margin = " " * self.paddingX
        content_lines: list[str] = []
        for line in wrapped_lines:
            line_with_margins = left_margin + line + right_margin
            if callable(self.customBgFn):
                content_lines.append(applyBackgroundToLine(line_with_margins, width, self.customBgFn))
            else:
                visible_length = visibleWidth(line_with_margins)
                content_lines.append(line_with_margins + (" " * max(0, width - visible_length)))

        empty_line = " " * width
        empty_lines: list[str] = []
        for _index in range(self.paddingY):
            line = (
                applyBackgroundToLine(empty_line, width, self.customBgFn) if callable(self.customBgFn) else empty_line
            )
            empty_lines.append(line)

        result = [*empty_lines, *content_lines, *empty_lines]
        self.cachedText = self.text
        self.cachedWidth = width
        self.cachedLines = result
        return result if result else [""]


__all__ = ["Text"]
