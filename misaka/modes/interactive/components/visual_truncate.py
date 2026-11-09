"""Translated from pi's ``modes/interactive/components/visual-truncate.ts`` (v1.1.0).

Shared utility for truncating text to visual lines (accounting for line wrapping).
Used by tool renderers and bash-execution.ts for consistent behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from misaka.ui.tui import Text, truncateToWidth


@dataclass(slots=True)
class VisualTruncateResult:
    # The visual lines to display
    visualLines: list[str]
    # Number of visual lines that were skipped (hidden)
    skippedCount: int


def truncateToVisualLines(
    text: str,
    maxVisualLines: int,
    width: int,
    paddingX: int = 0,
    keep: Literal["start", "end"] = "end",
) -> VisualTruncateResult:
    """Truncate text to a maximum number of visual lines.
    This accounts for line wrapping based on terminal width.

    :param text: The text content (may contain newlines)
    :param maxVisualLines: Maximum number of visual lines to show
    :param width: Terminal/render width
    :param paddingX: Horizontal padding for Text component (default 0).
                     Use 0 when result will be placed in a Box (Box adds its own padding).
                     Use 1 when result will be placed in a plain Container.
    :param keep: Which visual lines to keep: the last ones (default) or the first ones.
    :returns: The truncated visual lines and count of skipped lines
    """
    if not text:
        return VisualTruncateResult(visualLines=[], skippedCount=0)

    # Create a temporary Text component to render and get visual lines
    tempText = Text(text, paddingX, 0)
    allVisualLines = tempText.render(width)

    if len(allVisualLines) <= maxVisualLines:
        return VisualTruncateResult(visualLines=allVisualLines, skippedCount=0)

    truncatedLines = allVisualLines[0:maxVisualLines] if keep == "start" else allVisualLines[-maxVisualLines:]
    skippedCount = len(allVisualLines) - maxVisualLines

    return VisualTruncateResult(visualLines=truncatedLines, skippedCount=skippedCount)


@dataclass(slots=True)
class VisualLinePreviewOptions:
    # Styled text; may contain newlines.
    text: str
    maxVisualLines: int
    # Which visual lines to keep. The hint goes before kept end lines and after kept start lines.
    keep: Literal["start", "end"]
    # Styled hint line for the given number of hidden visual lines.
    formatHint: Callable[[int], str]


class VisualLinePreview:
    """Collapsed tool output limited to a number of visual lines, like bash output. Limiting logical
    lines instead lets a single long line (such as minified JSON) wrap across the whole screen.
    Caches its lines per width, since it renders on every frame for every result in the transcript.
    """

    def __init__(self, options: VisualLinePreviewOptions) -> None:
        self.options = options
        self.cachedWidth: int | None = None
        self.cachedLines: list[str] | None = None

    def render(self, width: int) -> list[str]:
        if self.cachedLines is None or self.cachedWidth != width:
            text, maxVisualLines, keep, formatHint = (
                self.options.text,
                self.options.maxVisualLines,
                self.options.keep,
                self.options.formatHint,
            )
            preview = truncateToVisualLines(text, maxVisualLines, width, 0, keep)
            lines = preview.visualLines
            if preview.skippedCount > 0:
                hint = truncateToWidth(formatHint(preview.skippedCount), width, "...")
                self.cachedLines = [*lines, hint] if keep == "start" else [hint, *lines]
            else:
                self.cachedLines = lines
            self.cachedWidth = width
        return self.cachedLines

    def invalidate(self) -> None:
        self.cachedWidth = None
        self.cachedLines = None
