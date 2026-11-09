"""Built-in entry kinds.

Translated from pi's ``packages/durable/src/entries.ts``. ``Entry.is`` is ``Entry.is_`` here, because ``is`` is a
Python keyword.
"""

from __future__ import annotations

from typing import Any


class Entry:
    """Typed entry kind with a narrowing guard: ``is_(entry)`` is true for an entry of this kind."""

    __slots__ = ("kind",)

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def is_(self, entry: dict[str, Any] | None) -> bool:
        return entry is not None and entry["kind"] == self.kind


def defineEntry(kind: str) -> Entry:
    """Define a typed entry kind whose ``is_()`` guard narrows by ``EntryRecord.kind``."""
    if not isinstance(kind, str) or len(kind) == 0:
        raise TypeError("Entry kind must be a non-empty string")
    return Entry(kind)


UserEntry = defineEntry("pi.user")
"""User input: ``model`` is ``[UserMessage]``. Written by submissions."""
AssistantEntry = defineEntry("pi.assistant")
"""Provider result with any stop reason: ``model`` is ``[AssistantMessage]``. Written by generation."""
SystemEntry = defineEntry("pi.system")
"""Positional prompt and tool change: ``model`` is ``[SystemMessage]`` with empty ``content``."""
ToolResultEntry = defineEntry("pi.tool-result")
"""Tool result: ``model`` is ``[ToolResultMessage]``, whose content ends with the rendered diagnostics block; ``data``
holds the structured diagnostics (``{"diagnostics": [...]}``), possibly none. Written by tool tasks, and by generation
for calls it did not offer."""
ResetEntry = defineEntry("pi.reset")
"""Start of a new context: always ``head: "self"``, with ``model`` absent for a plain reset or ``[UserMessage]``
carrying the handoff text. Written by ``Conversation.reset()`` and the ``handoff`` tool control."""
CompactionEntry = defineEntry("pi.compaction")
"""Compaction summary: ``model`` is ``[UserMessage]`` with the wrapped summary, ``head`` the first kept entry; ``data``
is ``{"reason": ...}``. Written by compaction tasks, directly or through a write submission."""

__all__ = [
    "AssistantEntry",
    "CompactionEntry",
    "Entry",
    "ResetEntry",
    "SystemEntry",
    "ToolResultEntry",
    "UserEntry",
    "defineEntry",
]
