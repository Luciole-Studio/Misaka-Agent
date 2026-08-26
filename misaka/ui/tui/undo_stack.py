"""Bounded undo history."""

from __future__ import annotations

from collections import deque

MAX_UNDO_STEPS = 100


class UndoStack[S]:
    """Keeps the last ``MAX_UNDO_STEPS`` snapshots and stores exactly what it is given.

    Cloning here used to be this class's job, which meant a deep copy of the caller's whole
    state on every keystroke -- for the editor that included every paste payload in the
    buffer. The caller knows which parts of its state get rebound and copies just those.
    """

    def __init__(self, limit: int = MAX_UNDO_STEPS) -> None:
        self._stack: deque[S] = deque(maxlen=max(1, limit))

    def push(self, state: S) -> None:
        self._stack.append(state)

    def pop(self) -> S | None:
        return self._stack.pop() if self._stack else None

    def clear(self) -> None:
        self._stack.clear()

    @property
    def length(self) -> int:
        return len(self._stack)


__all__ = ["MAX_UNDO_STEPS", "UndoStack"]
