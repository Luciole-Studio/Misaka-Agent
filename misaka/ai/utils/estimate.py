"""Translated from pi's ``packages/ai/src/utils/estimate.ts`` (v1.1.0).

MISAKA fork: lengths are JavaScript's (UTF-16 code units, ``jsLength``), serializations are ``JSON.stringify``'s
(``jsonStringify``), and a tool whose ``parameters`` is a pydantic class is measured by its JSON schema
(``_tool_payload``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import TranscriptContext, Usage
from misaka.ai.utils.text import getSystemMessageText
from misaka.utils.js import jsLength, jsonStringify


@dataclass(slots=True)
class ContextUsageEstimate:
    # Estimated total context tokens.
    tokens: int
    # Tokens reported by the most recent applicable assistant usage block.
    usageTokens: int
    # Estimated tokens after the most recent applicable assistant usage block.
    trailingTokens: int
    # Index of the applicable message that provided usage, or null when none exists.
    lastUsageIndex: int | None


_CHARS_PER_TOKEN = 3.5
_ESTIMATED_IMAGE_CHARS = 4800


def calculateContextTokens(usage: Usage) -> int:
    return usage.totalTokens or usage.input + usage.output + usage.cacheRead + usage.cacheWrite


def _safeJsonStringify(value: Any) -> str:
    try:
        return jsonStringify(value)
    except Exception:  # noqa: BLE001 - JSON.stringify throws on cycles; the estimate still needs a length
        return "[unserializable]"


def _estimateTextAndImageContentChars(content: Any) -> int:
    if isinstance(content, str):
        return jsLength(content)

    chars = 0
    for block in content:
        chars += jsLength(block.text) if block.type == "text" else _ESTIMATED_IMAGE_CHARS
    return chars


def estimateTextTokens(text: str) -> int:
    return math.ceil(jsLength(text) / _CHARS_PER_TOKEN)


def estimateTextAndImageContentTokens(content: Any) -> int:
    return math.ceil(_estimateTextAndImageContentChars(content) / _CHARS_PER_TOKEN)


def estimateMessageTokens(message: Any) -> int:
    chars = 0

    if message.role == "system":
        return (
            estimateTextTokens(getSystemMessageText(message))
            + _estimateToolsTokens(message.toolsAdded)
            + _estimateToolsTokens(message.toolsRemoved)
        )
    if message.role == "user":
        return estimateTextAndImageContentTokens(message.content)
    if message.role == "toolResult":
        return estimateTextAndImageContentTokens(message.content)

    for block in message.content:
        if block.type == "text":
            chars += jsLength(block.text)
        elif block.type == "thinking":
            chars += jsLength(block.thinking)
        else:
            chars += jsLength(block.name) + jsLength(_safeJsonStringify(block.arguments))
    return math.ceil(chars / _CHARS_PER_TOKEN)


def _getLastAssistantUsageInfo(messages: list[Any]) -> dict[str, Any] | None:
    latestPrefixTimestamp = float("-inf")
    usageInfo: dict[str, Any] | None = None

    for i, message in enumerate(messages):
        if message.role == "assistant":
            # A newer prefix message was inserted after this response (for example, a
            # compaction summary), so its usage cannot describe the current prefix.
            usageAppliesToPrefix = message.timestamp >= latestPrefixTimestamp
            if (
                usageAppliesToPrefix
                and message.stopReason != "aborted"
                and message.stopReason != "error"
                and calculateContextTokens(message.usage) > 0
            ):
                usageInfo = {"usage": message.usage, "index": i}
        latestPrefixTimestamp = max(latestPrefixTimestamp, message.timestamp)

    return usageInfo


def estimateContextTokens(context: TranscriptContext | list[Any]) -> ContextUsageEstimate:
    messages = list(context.messages if hasattr(context, "messages") else context)
    usageInfo = _getLastAssistantUsageInfo(messages)
    if usageInfo is not None:
        usageTokens = calculateContextTokens(usageInfo["usage"])
        trailingTokens = 0
        for i in range(usageInfo["index"] + 1, len(messages)):
            trailingTokens += estimateMessageTokens(messages[i])
        return ContextUsageEstimate(
            tokens=usageTokens + trailingTokens,
            usageTokens=usageTokens,
            trailingTokens=trailingTokens,
            lastUsageIndex=usageInfo["index"],
        )

    tokens = 0
    for message in messages:
        tokens += estimateMessageTokens(message)
    return ContextUsageEstimate(tokens=tokens, usageTokens=0, trailingTokens=tokens, lastUsageIndex=None)


def _tool_payload(tool: Any) -> Any:
    """MISAKA fork: what a tool costs on the wire, as far as a character count can tell. Upstream stringifies `Tool[]`
    and its `parameters` *is* the JSON schema; a MISAKA `Tool.parameters` may be a pydantic model class, which
    serializes to its class name. Expanding it makes the estimate comparable with what the provider is sent."""
    if not hasattr(tool, "model_dump"):
        return tool
    if not hasattr(tool, "parameters_json_schema"):
        return tool.model_dump(mode="json", exclude_none=True)
    payload = tool.model_dump(mode="json", exclude_none=True, exclude={"parameters"})
    payload["parameters"] = tool.parameters_json_schema()
    return payload


def _estimateToolsTokens(tools: list[Any] | None) -> int:
    if not tools or len(tools) == 0:
        return 0
    return estimateTextTokens(_safeJsonStringify([_tool_payload(tool) for tool in tools]))


__all__ = [
    "ContextUsageEstimate",
    "calculateContextTokens",
    "estimateContextTokens",
    "estimateMessageTokens",
    "estimateTextAndImageContentTokens",
    "estimateTextTokens",
]
