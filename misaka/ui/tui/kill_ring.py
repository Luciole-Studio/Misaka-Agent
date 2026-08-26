"""Ring buffer for Emacs-style kill and yank behavior."""

from __future__ import annotations

from collections import deque

MAX_KILLS = 60          # as in Emacs: old kills fall off the far end


class KillRing:
    def __init__(self, limit: int = MAX_KILLS) -> None:
        self._ring: deque[str] = deque(maxlen=max(1, limit))

    def push(self, text: str, opts: dict[str, bool]) -> None:
        if not text:
            return

        if opts.get("accumulate") and self._ring:
            last = self._ring.pop()
            self._ring.append(text + last if opts["prepend"] else last + text)
            return

        self._ring.append(text)

    def peek(self) -> str | None:
        return self._ring[-1] if self._ring else None

    def rotate(self) -> None:
        if len(self._ring) > 1:
            self._ring.appendleft(self._ring.pop())

    @property
    def length(self) -> int:
        return len(self._ring)


__all__ = ["MAX_KILLS", "KillRing"]
