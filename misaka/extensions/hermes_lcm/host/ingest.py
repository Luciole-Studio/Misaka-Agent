"""misaka's session shapes -> the OpenAI-shaped message dicts upstream ingests.

Upstream's contract with its host is one list per turn: *the messages the model is
actually being sent*, growing as the turn appends and shrinking when compaction
rewrites it. misaka's equivalent is ``build_session_context(branch).messages`` -- and
that includes the compaction-summary message, which is why it is converted here rather
than dropped. Upstream recognises its own summary scaffold, keeps it out of the durable
store, and its ingest cursor counts it, so replaying it is what keeps the cursor and the
store in step across a compaction.

Content is flattened to text on the way in. Upstream persists structured content as
canonical JSON, which would put block markup in front of the summariser and in every
recall result; misaka already stripped blobs the same way before the port.
"""

from __future__ import annotations

import json

from misaka.agent.harness.messages import convert_to_llm
from misaka.utils.values import read_field

# Placeholder for a content block that has no useful text: the durable store keeps the
# turn's shape and its position without the payload.
_OMITTED = "[{kind} omitted]"


def _text_of(content) -> str:
    """Flatten one message's content blocks into the text LCM stores."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        kind = read_field(block, "type")
        if kind == "text":
            parts.append(str(read_field(block, "text") or ""))
        elif kind == "image":
            parts.append(_OMITTED.format(kind=str(read_field(block, "mimeType") or "image")))
        elif kind in {"thinking", "toolCall"}:
            continue
        else:
            parts.append(_OMITTED.format(kind=str(kind or "content")))
    return "\n".join(part for part in parts if part)


def _tool_calls_of(content) -> list[dict] | None:
    """The assistant's tool calls, in the OpenAI shape upstream pairs against."""
    if not isinstance(content, list):
        return None
    calls = [
        {
            "id": str(read_field(block, "id") or ""),
            "type": "function",
            "function": {
                "name": str(read_field(block, "name") or ""),
                "arguments": json.dumps(read_field(block, "arguments") or {}, ensure_ascii=False),
            },
        }
        for block in content
        if read_field(block, "type") == "toolCall"
    ]
    return calls or None


def to_upstream(message) -> dict:
    """One converted LLM-shaped message.

    Optional keys are omitted rather than set to ``None``: upstream reads ``tool_calls``
    with ``msg.get("tool_calls", [])`` and iterates the result, so an explicit ``None``
    is not the same thing as an absent key.
    """
    role = str(read_field(message, "role"))
    content = read_field(message, "content")
    timestamp = read_field(message, "timestamp")
    converted = {"role": "tool" if role == "toolResult" else role, "content": _text_of(content)}
    if timestamp:
        # Unix seconds: upstream rejects anything it cannot read as an observation time,
        # and misaka counts milliseconds.
        converted["timestamp"] = float(timestamp) / 1000
    if role == "assistant":
        converted["tool_calls"] = _tool_calls_of(content) or []
    if role == "toolResult":
        converted["tool_call_id"] = str(read_field(message, "toolCallId") or "")
        converted["tool_name"] = str(read_field(message, "toolName") or "")
    return converted


def upstream_messages(messages) -> list[dict]:
    """A misaka active context (``build_session_context(...).messages``), converted."""
    return [to_upstream(message) for message in convert_to_llm(messages)]


def session_id(ctx) -> str:
    """The misaka session id, which is LCM's session identity."""
    try:
        return str(ctx.sessionManager.getSessionId())
    except Exception:  # noqa: BLE001 - a session without an id is addressed by nothing
        return ""
