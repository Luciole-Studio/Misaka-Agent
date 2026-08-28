"""Text extraction from message content, translated from pi's ``utils/text.ts``."""

from __future__ import annotations

from typing import Any


def content_text(content: str | list[Any], separator: str = "\n") -> str:
    """The text blocks of a message, joined.

    Non-text blocks -- images, thinking, tool calls -- are dropped rather than rendered:
    callers use this to read what was said, and a stringified tool call is not that.
    """
    if isinstance(content, str):
        return content
    return separator.join(
        block.text for block in content if getattr(block, "type", None) == "text"
    )


__all__ = ["content_text"]
