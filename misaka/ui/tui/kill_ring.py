"""Translated from pi-tui's ``packages/tui/src/kill-ring.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class KillRingPushOptions(TypedDict):
    """The inline ``opts`` type of ``KillRing.push``."""

    prepend: bool
    accumulate: NotRequired[bool]


class KillRing:
    """Ring buffer for Emacs-style kill/yank operations.

    Tracks killed (deleted) text entries. Consecutive kills can accumulate
    into a single entry. Supports yank (paste most recent) and yank-pop
    (cycle through older entries).
    """

    def __init__(self) -> None:
        self.ring: list[str] = []

    def push(self, text: str, opts: KillRingPushOptions) -> None:
        """Add text to the kill ring.

        @param text - The killed text to add
        @param opts - Push options
        @param opts.prepend - If accumulating, prepend (backward deletion) or append (forward deletion)
        @param opts.accumulate - Merge with the most recent entry instead of creating a new one
        """
        if not text:
            return

        if opts.get("accumulate") and len(self.ring) > 0:
            last = self.ring.pop()
            self.ring.append(text + last if opts["prepend"] else last + text)
        else:
            self.ring.append(text)

    def peek(self) -> str | None:
        """Get most recent entry without modifying the ring."""
        return self.ring[len(self.ring) - 1] if len(self.ring) > 0 else None

    def rotate(self) -> None:
        """Move last entry to front (for yank-pop cycling)."""
        if len(self.ring) > 1:
            last = self.ring.pop()
            self.ring.insert(0, last)

    @property
    def length(self) -> int:
        return len(self.ring)


__all__ = ["KillRing", "KillRingPushOptions"]
