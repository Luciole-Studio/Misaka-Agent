"""Single-line text component that truncates to fit width."""

from __future__ import annotations

from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import truncateToWidth, visibleWidth


class TruncatedText(Component):
    def __init__(self, text: str, paddingX: int = 0, paddingY: int = 0) -> None:
        self.text = text
        self.paddingX = paddingX
        self.paddingY = paddingY

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        result: list[str] = []
        empty_line = " " * width

        for _index in range(self.paddingY):
            result.append(empty_line)

        available_width = max(1, width - self.paddingX * 2)
        single_line_text = self.text.split("\n", 1)[0]
        display_text = truncateToWidth(single_line_text, available_width)

        left_padding = " " * self.paddingX
        right_padding = " " * self.paddingX
        line_with_padding = left_padding + display_text + right_padding
        line_visible_width = visibleWidth(line_with_padding)
        final_line = line_with_padding + (" " * max(0, width - line_visible_width))
        result.append(final_line)

        for _index in range(self.paddingY):
            result.append(empty_line)

        return result


__all__ = ["TruncatedText"]
