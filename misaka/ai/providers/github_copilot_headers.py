"""Dynamic request headers for GitHub Copilot-backed providers."""

from __future__ import annotations

from misaka.ai.types import MessageValue


def infer_copilot_initiator(messages: list[MessageValue]) -> str:
    last = messages[-1] if messages else None
    return "agent" if last is not None and last.role != "user" else "user"


def has_copilot_vision_input(messages: list[MessageValue]) -> bool:
    for message in messages:
        if message.role in {"user", "toolResult"} and isinstance(message.content, list):
            if any(block.type == "image" for block in message.content):
                return True
    return False


def build_copilot_dynamic_headers(*, messages: list[MessageValue], hasImages: bool) -> dict[str, str]:
    headers = {
        "X-Initiator": infer_copilot_initiator(messages),
        "Openai-Intent": "conversation-edits",
    }
    if hasImages:
        headers["Copilot-Vision-Request"] = "true"
    return headers


inferCopilotInitiator = infer_copilot_initiator
hasCopilotVisionInput = has_copilot_vision_input
buildCopilotDynamicHeaders = build_copilot_dynamic_headers

__all__ = [
    "inferCopilotInitiator",
    "hasCopilotVisionInput",
    "buildCopilotDynamicHeaders",
]
