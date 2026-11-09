"""Translated from pi's ``packages/ai/test/message-types.test.ts`` (v1.1.0).

MISAKA fork: pi's assertions here are mostly compile-time (``expectTypeOf``). The runtime half is kept: JSON-shaped
details of every kind survive on tool results and in a context. "rejects non-JSON detail types" is type-level only and
has no runtime counterpart (``ToolResultMessage.details`` is ``Any``).
"""

from __future__ import annotations

from typing import Any

from misaka.ai.types import Context, ToolResultMessage

base: dict[str, Any] = {
    "role": "toolResult",
    "toolCallId": "call-1",
    "toolName": "read",
    "content": [],
    "isError": False,
    "timestamp": 1,
}


def test_preserves_json_compatible_detail_types():
    objectMessage = ToolResultMessage(**base, details={"path": "a.ts", "items": ["first"], "summary": {"lines": 3}})
    arrayMessage = ToolResultMessage(**base, details=["a", "b"])
    readonlyArrayMessage = ToolResultMessage(**base, details=("a", {"count": 1}))
    primitiveMessage = ToolResultMessage(**base, details="diagnostic")
    nullMessage = ToolResultMessage(**base, details=None)
    context = Context(messages=[objectMessage, readonlyArrayMessage])

    assert objectMessage.details["summary"]["lines"] == 3
    assert arrayMessage.details == ["a", "b"]
    assert list(readonlyArrayMessage.details) == ["a", {"count": 1}]
    assert primitiveMessage.details == "diagnostic"
    assert nullMessage.details is None
    assert context.messages == [objectMessage, readonlyArrayMessage]
