"""Context size estimation, translated from pi's ``utils/estimate.ts``.

The estimate anchors on the last usage the provider reported and estimates only what was
added after it. The four returned numbers keep the reported part and the estimated part
separate rather than collapsing them into one total.

The anchor has one subtlety worth keeping. A message inserted *after* an assistant
response -- a compaction summary is the usual case -- makes that response's usage describe
a prefix that no longer exists, so timestamps decide whether a usage block still applies.
That is upstream's reasoning too (``estimate.ts:71-73``).

``clamp_max_tokens_to_context`` comes from ``api/simple-options.ts:15-19``; it lives here
because it is the one caller that needs the estimate. Every ``streamSimple`` request goes
through it (``ai/providers/simple_options.py:41``), so an estimate that reads low here
hands the provider a larger output budget than the window can actually hold.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import Model, TranscriptContext, Usage
from misaka.ai.utils.text import get_system_message_text

CHARS_PER_TOKEN = 4
ESTIMATED_IMAGE_CHARS = 4800
CONTEXT_SAFETY_TOKENS = 4096
MIN_MAX_TOKENS = 1


@dataclass(slots=True)
class ContextUsageEstimate:
    # Estimated total context tokens.
    tokens: int
    # Tokens reported by the most recent applicable assistant usage block.
    usageTokens: int
    # Estimated tokens after that block.
    trailingTokens: int
    # Index of the message that provided usage, or None when none applies.
    lastUsageIndex: int | None


def calculate_context_tokens(usage: Usage) -> int:
    """The provider's own total, falling back to the sum of its parts."""
    return usage.totalTokens or (usage.input + usage.output + usage.cacheRead + usage.cacheWrite)


def _utf16_length(text: str) -> int:
    """The length JavaScript's ``String.length`` reports.

    Every measurement here feeds a token estimate that upstream computes from
    ``text.length`` -- a count of UTF-16 code units, where a character outside the Basic
    Multilingual Plane counts as two. Python's ``len`` counts code points and gives one,
    so an emoji-heavy context read as half its real size and compaction fired late.
    """
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


def _safe_json(value: Any) -> str:
    # Compact and unescaped, like `JSON.stringify`: the result is measured by length, and
    # both Python defaults inflate it -- separator spacing pads every comma, and ASCII
    # escaping turns each CJK character into six, overstating non-ASCII tool calls and
    # schemas by 2-3x against what the provider actually tokenises.
    try:
        return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "[unserializable]"


def _content_chars(content: Any) -> int:
    if isinstance(content, str):
        return _utf16_length(content)
    chars = 0
    for block in content:
        kind = getattr(block, "type", None)
        chars += _utf16_length(block.text) if kind == "text" else ESTIMATED_IMAGE_CHARS
    return chars


def estimate_text_tokens(text: str) -> int:
    return math.ceil(_utf16_length(text) / CHARS_PER_TOKEN)


def estimate_text_and_image_content_tokens(content: Any) -> int:
    return math.ceil(_content_chars(content) / CHARS_PER_TOKEN)


def estimate_message_tokens(message: Any) -> int:
    """Text by length, images by a flat allowance, tool calls by name plus arguments."""
    role = getattr(message, "role", None)
    if role == "system":
        return (
            estimate_text_tokens(get_system_message_text(message))
            + _estimate_tools_tokens(message.toolsAdded)
            + _estimate_tools_tokens(message.toolsRemoved)
        )
    if role in ("user", "toolResult"):
        return estimate_text_and_image_content_tokens(message.content)

    chars = 0
    for block in message.content:
        kind = getattr(block, "type", None)
        if kind == "text":
            chars += _utf16_length(block.text)
        elif kind == "thinking":
            chars += _utf16_length(block.thinking)
        else:
            chars += _utf16_length(block.name) + _utf16_length(_safe_json(block.arguments))
    return math.ceil(chars / CHARS_PER_TOKEN)


def _last_assistant_usage(messages: list[Any]) -> tuple[Usage, int] | None:
    latest_prefix_timestamp = float("-inf")
    found: tuple[Usage, int] | None = None

    for index, message in enumerate(messages):
        if getattr(message, "role", None) == "assistant":
            # A newer prefix message was inserted after this response (a compaction
            # summary, say), so its usage cannot describe the current prefix.
            applies = message.timestamp >= latest_prefix_timestamp
            if (
                applies
                and message.stopReason not in ("aborted", "error")
                and calculate_context_tokens(message.usage) > 0
            ):
                found = (message.usage, index)
        latest_prefix_timestamp = max(latest_prefix_timestamp, message.timestamp)

    return found


def _estimate_messages(messages: list[Any]) -> ContextUsageEstimate:
    anchor = _last_assistant_usage(messages)
    if anchor is not None:
        usage, index = anchor
        usage_tokens = calculate_context_tokens(usage)
        trailing = sum(estimate_message_tokens(m) for m in messages[index + 1 :])
        return ContextUsageEstimate(
            tokens=usage_tokens + trailing,
            usageTokens=usage_tokens,
            trailingTokens=trailing,
            lastUsageIndex=index,
        )

    tokens = sum(estimate_message_tokens(m) for m in messages)
    return ContextUsageEstimate(
        tokens=tokens, usageTokens=0, trailingTokens=tokens, lastUsageIndex=None
    )


def _tool_payload(tool: Any) -> Any:
    """What a tool costs on the wire, as far as a character count can tell.

    Upstream stringifies ``Tool[]`` directly (``estimate.ts:105-108``) and its
    ``parameters`` *is* the JSON schema. Misaka's ``Tool.parameters`` may instead be a
    pydantic model class -- every built-in tool passes one -- and ``model_dump`` leaves
    that class untouched, so the fallback stringifier collapsed a schema worth hundreds
    of tokens into ``"<class '...BashToolInput'>"``. Expanding it here is what makes the
    estimate comparable with what the provider is actually sent.
    """
    if not hasattr(tool, "model_dump") or not hasattr(tool, "parameters_json_schema"):
        # Not a ``Tool``: leave it to ``_safe_json``, which is what handled it before.
        return tool.model_dump() if hasattr(tool, "model_dump") else tool
    payload = tool.model_dump(exclude={"parameters"})
    payload["parameters"] = tool.parameters_json_schema()
    return payload


def _estimate_tools_tokens(tools: list[Any] | None) -> int:
    if not tools:
        return 0
    return estimate_text_tokens(_safe_json([_tool_payload(t) for t in tools]))


def estimate_context_tokens(context: TranscriptContext | list[Any]) -> ContextUsageEstimate:
    """The whole transcript, or just a message list. The prompt and tools are system
    messages, so they are counted where they sit: only after the usage anchor when one
    exists, otherwise in full."""
    messages = context.messages if hasattr(context, "messages") else context
    return _estimate_messages(list(messages))


def clamp_max_tokens_to_context(model: Model, context: TranscriptContext, maxTokens: int) -> int:
    """Never ask for more output than the window can still hold.

    ``CONTEXT_SAFETY_TOKENS`` is subtracted on top of the estimate, the same margin
    upstream keeps (``simple-options.ts:12,17``).
    """
    if model.contextWindow <= 0:
        return max(MIN_MAX_TOKENS, maxTokens)
    available = model.contextWindow - estimate_context_tokens(context).tokens - CONTEXT_SAFETY_TOKENS
    return min(maxTokens, max(MIN_MAX_TOKENS, available))


__all__ = [
    "CHARS_PER_TOKEN",
    "CONTEXT_SAFETY_TOKENS",
    "ContextUsageEstimate",
    "calculate_context_tokens",
    "clamp_max_tokens_to_context",
    "estimate_context_tokens",
    "estimate_message_tokens",
    "estimate_text_and_image_content_tokens",
    "estimate_text_tokens",
]
