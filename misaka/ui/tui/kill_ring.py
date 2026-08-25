"""Ring buffer for Emacs-style kill and yank behavior."""

from __future__ import annotations


class KillRing:
    def __init__(self) -> None:
        self._ring: list[str] = []

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
            last = self._ring.pop()
            self._ring.insert(0, last)

    @property
    def length(self) -> int:
        return len(self._ring)


__all__ = ["KillRing"]
