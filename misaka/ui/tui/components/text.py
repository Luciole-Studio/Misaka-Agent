"""Translated from pi-tui's ``components/text.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable

from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import (
    applyBackgroundToLine,
    flattenLines,
    visibleWidth,
    wrapTextWithAnsi,
)
from misaka.utils.js import jsTrim


class Text(Component):
    """Text component - displays multi-line text with word wrapping"""

    def __init__(
        self,
        text: str = "",
        paddingX: int = 1,
        paddingY: int = 1,
        customBgFn: Callable[[str], str] | None = None,
    ) -> None:
        self.text = text
        self.paddingX = paddingX  # Left/right padding
        self.paddingY = paddingY  # Top/bottom padding
        self.customBgFn = customBgFn

        # Cache for rendered output
        self.cachedText: str | None = None
        self.cachedWidth: int | None = None
        self.cachedLines: list[str] | None = None

    def setText(self, text: str) -> None:
        self.text = text
        self.cachedText = None
        self.cachedWidth = None
        self.cachedLines = None

    def setCustomBgFn(self, customBgFn: Callable[[str], str] | None = None) -> None:
        self.customBgFn = customBgFn
        self.cachedText = None
        self.cachedWidth = None
        self.cachedLines = None

    def setPaddingX(self, paddingX: int) -> None:
        self.paddingX = paddingX
        self.invalidate()

    def invalidate(self) -> None:
        self.cachedText = None
        self.cachedWidth = None
        self.cachedLines = None

    def render(self, width: int) -> list[str]:
        # Check cache
        if self.cachedLines is not None and self.cachedText == self.text and self.cachedWidth == width:
            return self.cachedLines

        # Don't render anything if there's no actual text
        if not self.text or jsTrim(self.text) == "":
            result: list[str] = []
            self.cachedText = self.text
            self.cachedWidth = width
            self.cachedLines = result
            return result

        # Replace tabs with 3 spaces
        normalizedText = self.text.replace("\t", "   ")

        # Reduce margins when necessary so content and padding fit within the available width.
        paddingX = min(self.paddingX, max(0, (width - 1) // 2))
        contentWidth = max(1, width - paddingX * 2)

        # Wrap text (this preserves ANSI codes but does NOT pad)
        wrappedLines = wrapTextWithAnsi(normalizedText, contentWidth)

        # Add margins and background to each line
        leftMargin = " " * paddingX
        rightMargin = " " * paddingX
        contentLines: list[str] = []

        for line in wrappedLines:
            # Add margins
            lineWithMargins = leftMargin + line + rightMargin

            # Apply background if specified (this also pads to full width)
            if self.customBgFn:
                contentLines.append(applyBackgroundToLine(lineWithMargins, width, self.customBgFn))
            else:
                # No background - just pad to width with spaces
                visibleLen = visibleWidth(lineWithMargins)
                paddingNeeded = max(0, width - visibleLen)
                contentLines.append(lineWithMargins + " " * paddingNeeded)

        # Add top/bottom padding (empty lines)
        emptyLine = " " * width
        emptyLines: list[str] = []
        for _i in range(self.paddingY):
            line = applyBackgroundToLine(emptyLine, width, self.customBgFn) if self.customBgFn else emptyLine
            emptyLines.append(line)

        result = [*emptyLines, *contentLines, *emptyLines]
        flattenLines(result)

        # Update cache
        self.cachedText = self.text
        self.cachedWidth = width
        self.cachedLines = result

        return result if len(result) > 0 else [""]


__all__ = ["Text"]
