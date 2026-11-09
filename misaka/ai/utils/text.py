"""Translated from pi's ``packages/ai/src/utils/text.ts`` (v1.1.0).

MISAKA fork: blocks and messages may be plain mappings as well as models (hand-built histories, extensions, durable
records); upstream reads ``block.type`` either way.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def contentText(content: str | list[Any], separator: str = "\n") -> str:
    """Extract and join text from message content."""
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


def getSystemMessageText(message: Any) -> str:
    """Render a system message as a complete prompt: its content followed by its sections."""
    parts = [contentText(_field(message, "content"))]
    for text in (_field(message, "sections") or {}).values():
        if text is not None:
            parts.append(text)
    return "\n\n".join(part for part in parts if len(part) > 0)


def renderSystemMessageUpdate(message: Any) -> str:
    """Render a later system message for APIs that accept system messages mid-conversation.

    Section changes are framed by name so the model can relate them to the leading prompt.
    This framing is request-time only and may change between versions.
    """
    parts: list[str] = []
    text = contentText(_field(message, "content"))
    if len(text) > 0:
        parts.append(text)
    for name, value in (_field(message, "sections") or {}).items():
        parts.append(
            f'Removed system prompt section "{name}".'
            if value is None
            else f'Updated system prompt section "{name}":\n\n{value}'
        )
    return "\n\n".join(parts)


def _field(message: Any, name: str) -> Any:
    # Messages arrive as models from the pipeline and as plain mappings from hand-built
    # histories and extensions, the same two shapes ``content_text`` accepts.
    if isinstance(message, Mapping):
        return message.get(name)
    return getattr(message, name, None)


__all__ = ["contentText", "getSystemMessageText", "renderSystemMessageUpdate"]
