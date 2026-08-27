"""Choose a protected recent-message tail without splitting tool-call groups."""

from dataclasses import dataclass

from misaka.extensions.hermes_lcm.tokens import count_message_tokens


@dataclass
class FreshTailBoundary:
    start: int
    count: int
    tokens: int
    token_limited: bool = False
    tool_group_extended: bool = False


def _assistant_group_start(messages, start):
    """Move a tool-result boundary back to its originating assistant call."""
    if start <= 0 or start >= len(messages):
        return start
    boundary = messages[start]
    if boundary.get("role") != "tool":
        return start
    result_id = boundary.get("tool_call_id")
    if not result_id:
        return start
    index = start - 1
    while index >= 0:
        msg = messages[index]
        role = msg.get("role")
        if role in ("user", "system"):
            return start
        if role == "assistant":
            ids = {tc.get("id") for tc in msg.get("tool_calls") or []
                   if isinstance(tc, dict)}
            return index if result_id in ids else start
        index -= 1
    return start


def resolve_fresh_tail_boundary(messages, *, fresh_tail_count,
                                fresh_tail_max_tokens=0):
    n = len(messages)
    count_limit = max(1, int(fresh_tail_count)) if fresh_tail_max_tokens > 0 \
        else max(0, int(fresh_tail_count))
    start = max(0, n - count_limit) if count_limit else n
    token_limited = False
    if fresh_tail_max_tokens > 0:
        used = 0
        boundary = n
        for index in range(n - 1, start - 1, -1):
            tokens = count_message_tokens(messages[index])
            if index != n - 1 and used + tokens > fresh_tail_max_tokens:
                token_limited = True
                break
            used += tokens
            boundary = index
        start = boundary
    group_start = _assistant_group_start(messages, start)
    extended = group_start < start
    start = group_start
    tail = messages[start:]
    return FreshTailBoundary(
        start=start, count=len(tail),
        tokens=sum(count_message_tokens(m) for m in tail),
        token_limited=token_limited, tool_group_extended=extended)
