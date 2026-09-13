"""Text selection in a pane, in screen-buffer coordinates.

Port of herdr ``src/selection.rs``: rows are absolute (0 = the oldest history line, the
live screen follows history), so a selection stays on its text while the pane scrolls or
the program prints more. Lifecycle: a press anchors (nothing shown), a drag activates the
highlight, a release finishes it; a plain click never becomes a selection.
"""
from dataclasses import dataclass


def viewport_top_row(metrics):
    """Absolute row of the first visible line for the pane's scroll metrics."""
    if not metrics:
        return 0
    return max(0, metrics["max_offset_from_bottom"] - metrics["offset_from_bottom"])


def absolute_row(viewport_row, metrics):
    return viewport_top_row(metrics) + viewport_row


def viewport_row(absolute, metrics):
    return absolute - viewport_top_row(metrics)


@dataclass(slots=True)
class Selection:
    pane: str
    anchor: tuple                     # (absolute row, col)
    cursor: tuple                     # (absolute row, col)
    phase: str = "anchored"           # anchored | dragging | done

    @classmethod
    def at(cls, pane, viewport_row_, col, metrics):
        """A potential selection: the press position; nothing is highlighted yet."""
        point = (absolute_row(viewport_row_, metrics), col)
        return cls(pane, point, point)

    @classmethod
    def range(cls, pane, viewport_row_, start_col, end_col, metrics):
        """An active selection over one viewport row (double-click word selection)."""
        row = absolute_row(viewport_row_, metrics)
        return cls(pane, (row, start_col), (row, end_col), "dragging")

    @classmethod
    def lines(cls, pane, anchor_row, cursor_row, end_col):
        """Whole absolute rows from the anchor row to the cursor row (copy mode V)."""
        if anchor_row <= cursor_row:
            return cls(pane, (anchor_row, 0), (cursor_row, end_col), "dragging")
        return cls(pane, (anchor_row, end_col), (cursor_row, 0), "dragging")

    def drag(self, screen_col, screen_row, inner, metrics):
        """Extend to a screen position, clamped to the pane's inner rect; the highlight
        appears once the cursor leaves the anchor cell."""
        col = min(max(screen_col, inner.x), inner.x + max(0, inner.width - 1)) - inner.x
        row = min(max(screen_row, inner.y), inner.y + max(0, inner.height - 1)) - inner.y
        self.cursor = (absolute_row(row, metrics), col)
        if self.cursor != self.anchor:
            self.phase = "dragging"

    def force_dragging(self):
        """The pointer left the anchor cell but clamping put it back: still a drag."""
        if self.phase == "anchored":
            self.phase = "dragging"

    def finish(self):
        """Release: True when this was a real selection (the user dragged)."""
        if self.phase == "dragging":
            self.phase = "done"
            return True
        return False

    @property
    def visible(self):
        return self.phase in ("dragging", "done")

    @property
    def finalized(self):
        return self.phase == "done"

    @property
    def in_progress(self):
        return self.phase in ("anchored", "dragging")

    @property
    def just_click(self):
        return self.phase == "anchored"

    def ordered(self):
        """(start, end) in reading order."""
        return (self.anchor, self.cursor) if self.anchor <= self.cursor else (self.cursor, self.anchor)

    def anchor_screen_pos(self, inner, metrics):
        """The anchor as a clamped screen (row, col), to compare with the pointer."""
        row = viewport_row(self.anchor[0], metrics) + inner.y
        col = self.anchor[1] + inner.x
        return (min(max(row, inner.y), inner.y + max(0, inner.height - 1)),
                min(max(col, inner.x), inner.x + max(0, inner.width - 1)))

    def span(self, viewport_row_, width, metrics):
        """Column range [c0, c1) the selection covers on a viewport row, or None."""
        if not self.visible:
            return None
        row = absolute_row(viewport_row_, metrics)
        (start_row, start_col), (end_row, end_col) = self.ordered()
        if row < start_row or row > end_row:
            return None
        first = start_col if row == start_row else 0
        last = end_col + 1 if row == end_row else width
        return first, min(last, width)
