"""Shared fresh-tail boundary calculation.

The protected tail is primarily message-count bounded.  An optional token cap
can move the boundary toward the newest message, while a tool-call integrity
check may move it back to the assistant that opened a tool-result group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Sequence

from .message_analysis import _tool_call_id
from .tokens import count_message_tokens, count_messages_tokens


@dataclass(frozen=True)
class FreshTailBoundary:
    """Resolved protected-tail metadata for one ordered message sequence."""

    start: int
    count: int
    tokens: int
    count_limit: int
    token_limit: int
    token_limited: bool
    tool_group_extended: bool


def _assistant_group_start(messages: Sequence[Dict[str, Any]], start: int) -> int:
    """Retreat a tool-result boundary to its nearest opening assistant."""
    if start >= len(messages) or messages[start].get("role") != "tool":
        return start
    result_id = str(messages[start].get("tool_call_id") or "").strip()
    if not result_id:
        return start

    for index in range(start - 1, -1, -1):
        message = messages[index]
        role = str(message.get("role") or "")
        if role in {"user", "system"}:
            break
        if role != "assistant":
            continue
        call_ids = {
            _tool_call_id(tool_call)
            for tool_call in (message.get("tool_calls") or [])
        }
        return index if result_id in call_ids else start
    return start


def resolve_fresh_tail_boundary(
    messages: Sequence[Dict[str, Any]],
    *,
    fresh_tail_count: int,
    fresh_tail_max_tokens: int = 0,
) -> FreshTailBoundary:
    """Resolve the protected suffix without splitting assistant/tool groups.

    ``fresh_tail_max_tokens`` is disabled at zero.  When enabled, the newest
    message is always retained even when it alone exceeds the cap.  Tool-call
    integrity takes precedence over both limits, so the returned tail can
    exceed a configured bound when necessary to retain the opening assistant.
    """
    message_count = len(messages)
    count_limit = max(0, int(fresh_tail_count or 0))
    token_limit = max(0, int(fresh_tail_max_tokens or 0))
    if message_count == 0:
        return FreshTailBoundary(0, 0, 0, count_limit, token_limit, False, False)

    effective_count_limit = max(1, count_limit) if token_limit > 0 else count_limit
    count_start = max(0, message_count - effective_count_limit)
    start = count_start
    token_limited = False

    if token_limit > 0:
        used = 0
        token_start = message_count - 1
        for index in range(message_count - 1, count_start - 1, -1):
            message_tokens = count_message_tokens(messages[index])
            if index != message_count - 1 and used + message_tokens > token_limit:
                token_limited = True
                break
            token_start = index
            used += message_tokens
        start = token_start

    group_start = _assistant_group_start(messages, start)
    tool_group_extended = group_start < start
    start = group_start
    tail = list(messages[start:])
    return FreshTailBoundary(
        start=start,
        count=len(tail),
        tokens=count_messages_tokens(tail),
        count_limit=count_limit,
        token_limit=token_limit,
        token_limited=token_limited,
        tool_group_extended=tool_group_extended,
    )
