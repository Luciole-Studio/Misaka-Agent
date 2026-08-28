"""Context size estimation, translated from pi's ``utils/estimate.ts``.

The estimate anchors on the last usage the provider reported and estimates only what was
added after it. The four returned numbers keep the reported part and the estimated part
separate rather than collapsing them into one total.

The anchor has one subtlety worth keeping. A message inserted *after* an assistant
response -- a compaction summary is the usual case -- makes that response's usage describe
a prefix that no longer exists, so timestamps decide whether a usage block still applies.
That is upstream's reasoning too (``estimate.ts:71-73``).

``clamp_max_tokens_to_context`` comes from ``api/simple-options.ts:15-19``; it lives here
because it is the one caller that needs the estimate. Nothing under ``misaka/`` calls it
today; only the tests do.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from misaka.ai.types import Context, Model, Usage

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
        return len(content)
    chars = 0
    for block in content:
        kind = getattr(block, "type", None)
        chars += len(block.text) if kind == "text" else ESTIMATED_IMAGE_CHARS
    return chars


def estimate_text_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_text_and_image_content_tokens(content: Any) -> int:
    return math.ceil(_content_chars(content) / CHARS_PER_TOKEN)


def estimate_message_tokens(message: Any) -> int:
    """Text by length, images by a flat allowance, tool calls by name plus arguments."""
    role = getattr(message, "role", None)
    if role in ("user", "toolResult"):
        return estimate_text_and_image_content_tokens(message.content)

    chars = 0
    for block in message.content:
        kind = getattr(block, "type", None)
        if kind == "text":
            chars += len(block.text)
        elif kind == "thinking":
            chars += len(block.thinking)
        else:
            chars += len(block.name) + len(_safe_json(block.arguments))
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


def _estimate_tools_tokens(tools: list[Any] | None) -> int:
    if not tools:
        return 0
    return estimate_text_tokens(
        _safe_json([t.model_dump() if hasattr(t, "model_dump") else t for t in tools])
    )


def estimate_context_tokens(context: Context | list[Any]) -> ContextUsageEstimate:
    """The whole context, or just a message list.

    With a usage anchor, the only tools added on top of the reported number are the ones
    named by ``addedToolNames`` after the anchor. Without one, the system prompt and the
    whole tool list are estimated too.
    """
    if isinstance(context, list):
        return _estimate_messages(context)

    estimate = _estimate_messages(context.messages)
    if estimate.lastUsageIndex is not None:
        added_names: set[str] = set()
        for message in context.messages[estimate.lastUsageIndex + 1 :]:
            if getattr(message, "role", None) == "toolResult":
                added_names.update(message.addedToolNames or [])
        added = _estimate_tools_tokens(
            [tool for tool in (context.tools or []) if tool.name in added_names]
        )
        return ContextUsageEstimate(
            tokens=estimate.tokens + added,
            usageTokens=estimate.usageTokens,
            trailingTokens=estimate.trailingTokens + added,
            lastUsageIndex=estimate.lastUsageIndex,
        )

    prefix = (
        estimate_text_tokens(context.systemPrompt) if context.systemPrompt else 0
    ) + _estimate_tools_tokens(context.tools)
    return ContextUsageEstimate(
        tokens=estimate.tokens + prefix,
        usageTokens=estimate.usageTokens,
        trailingTokens=estimate.trailingTokens + prefix,
        lastUsageIndex=estimate.lastUsageIndex,
    )


def clamp_max_tokens_to_context(model: Model, context: Context, maxTokens: int) -> int:
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
