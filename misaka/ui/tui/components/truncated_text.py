"""Translated from pi-tui's ``components/truncated-text.ts`` (v1.1.0)."""

from __future__ import annotations

from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import truncateToWidth, visibleWidth


class TruncatedText(Component):
    """Text component that truncates to fit viewport width"""

    def __init__(self, text: str, paddingX: int = 0, paddingY: int = 0) -> None:
        self.text = text
        self.paddingX = paddingX
        self.paddingY = paddingY

    def invalidate(self) -> None:
        # No cached state to invalidate currently
        pass

    def render(self, width: int) -> list[str]:
        result: list[str] = []

        # Empty line padded to width
        emptyLine = " " * width

        # Add vertical padding above
        for _i in range(self.paddingY):
            result.append(emptyLine)

        # Calculate available width after horizontal padding
        availableWidth = max(1, width - self.paddingX * 2)

        # Take only the first line (stop at newline)
        singleLineText = self.text
        newlineIndex = self.text.find("\n")
        if newlineIndex != -1:
            singleLineText = self.text[:newlineIndex]

        # Truncate text if needed (accounting for ANSI codes)
        displayText = truncateToWidth(singleLineText, availableWidth)

        # Add horizontal padding
        leftPadding = " " * self.paddingX
        rightPadding = " " * self.paddingX
        lineWithPadding = leftPadding + displayText + rightPadding

        # Pad line to exactly width characters
        lineVisibleWidth = visibleWidth(lineWithPadding)
        paddingNeeded = max(0, width - lineVisibleWidth)
        finalLine = lineWithPadding + " " * paddingNeeded

        result.append(finalLine)

        # Add vertical padding below
        for _i in range(self.paddingY):
            result.append(emptyLine)

        return result


__all__ = ["TruncatedText"]
