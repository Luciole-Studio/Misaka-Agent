"""Translated from pi's ``packages/ai/src/api/github-copilot-headers.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from misaka.ai.types import MessageValue


# Copilot expects X-Initiator to indicate whether the request is user-initiated
# or agent-initiated (e.g. follow-up after assistant/tool messages).
def inferCopilotInitiator(messages: Sequence[MessageValue]) -> Literal["user", "agent"]:
    last = messages[-1] if messages else None
    return "agent" if last and last.role != "user" else "user"


# Copilot requires Copilot-Vision-Request header when sending images
def hasCopilotVisionInput(messages: Sequence[MessageValue]) -> bool:
    def hasImage(msg: Any) -> bool:
        if msg.role == "user" and isinstance(msg.content, list):
            return any(c.type == "image" for c in msg.content)
        if msg.role == "toolResult" and isinstance(msg.content, list):
            return any(c.type == "image" for c in msg.content)
        return False

    return any(hasImage(msg) for msg in messages)


def buildCopilotDynamicHeaders(params: Mapping[str, Any]) -> dict[str, str]:
    """``params`` is ``{messages, hasImages}``."""
    headers: dict[str, str] = {
        "X-Initiator": inferCopilotInitiator(params["messages"]),
        "Openai-Intent": "conversation-edits",
    }

    if params["hasImages"]:
        headers["Copilot-Vision-Request"] = "true"

    return headers


__all__ = ["buildCopilotDynamicHeaders", "hasCopilotVisionInput", "inferCopilotInitiator"]
