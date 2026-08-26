"""Width-aware truncation by rendered visual lines."""

from __future__ import annotations

from dataclasses import dataclass

from misaka.ui.tui import Text


@dataclass(slots=True)
class VisualTruncateResult:
    visualLines: list[str]
    skippedCount: int


def truncate_to_visual_lines(
    text: str,
    maxVisualLines: int,
    width: int,
    paddingX: int = 0,
) -> VisualTruncateResult:
    if not text:
        return VisualTruncateResult(visualLines=[], skippedCount=0)

    all_visual_lines = Text(text, paddingX, 0).render(width)
    if len(all_visual_lines) <= maxVisualLines:
        return VisualTruncateResult(visualLines=all_visual_lines, skippedCount=0)

    return VisualTruncateResult(
        visualLines=all_visual_lines[-maxVisualLines:],
        skippedCount=len(all_visual_lines) - maxVisualLines,
    )


truncateToVisualLines = truncate_to_visual_lines

__all__ = ["VisualTruncateResult", "truncateToVisualLines"]
