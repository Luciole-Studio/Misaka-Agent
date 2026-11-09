"""Translated from pi-tui's ``packages/tui/src/undo-stack.ts`` (v1.1.0)."""

from __future__ import annotations

import copy


class UndoStack[S]:
    """Generic undo stack with clone-on-push semantics.

    Stores deep clones of state snapshots. Popped snapshots are returned
    directly (no re-cloning) since they are already detached.
    """

    def __init__(self) -> None:
        self.stack: list[S] = []

    def push(self, state: S) -> None:
        """Push a deep clone of the given state onto the stack."""
        # `structuredClone`; strings are immutable and shared, as a clone of them would read.
        self.stack.append(copy.deepcopy(state))

    def pop(self) -> S | None:
        """Pop and return the most recent snapshot, or undefined if empty."""
        return self.stack.pop() if self.stack else None

    def clear(self) -> None:
        """Remove all snapshots."""
        self.stack.clear()

    @property
    def length(self) -> int:
        return len(self.stack)


__all__ = ["UndoStack"]
