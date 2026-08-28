"""Text extraction from message content, translated from pi's ``utils/text.ts``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def content_text(content: str | list[Any], separator: str = "\n") -> str:
    """The text blocks of a message, joined.

    Non-text blocks -- images, thinking, tool calls -- are dropped rather than rendered:
    callers use this to read what was said, and a stringified tool call is not that.
    """
    if isinstance(content, str):
        return content
    texts: list[str] = []
    for block in content:
        # Blocks arrive as models from the message pipeline and as plain mappings from
        # hand-built histories and extensions. Upstream reads `block.type` either way;
        # `getattr` alone silently dropped every mapping-shaped text block.
        if isinstance(block, Mapping):
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
        elif getattr(block, "type", None) == "text":
            texts.append(block.text)
    return separator.join(texts)


__all__ = ["content_text"]
