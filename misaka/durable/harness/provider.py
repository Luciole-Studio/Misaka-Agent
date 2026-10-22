"""Stable provider-facing identity of one conversation.

Ported from: pi packages/durable/src/harness/provider.ts @ v1.3.0
"""

from __future__ import annotations

from typing import Any, TypedDict

from misaka.ai.utils.uuid import uuidv7
from misaka.chord.context import Context

from ..documents import defineDoc


class ProviderState(TypedDict):
    """Stable provider-facing identity of one conversation."""

    sessionId: str


# Built-in provider state; every fork starts with a fresh identity instead of copying its parent.
ProviderDoc = defineDoc(
    {
        "kind": "pi.provider",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda: {"sessionId": uuidv7()},
        "checkpointWhen": lambda *_: True,
    }
)


async def ensureProviderSessionId(runtime: Any, context: Context) -> str:
    """Return the persisted identity without writing in the normal path. A legacy conversation without ``pi.provider``
    gets one migration commit whose ``tx.doc()`` runs ``initial()`` before the provider request starts."""
    existing = await runtime.snapshot(ProviderDoc, runtime.conversationId, context)
    if existing is not None:
        return existing["sessionId"]
    created: str | None = None

    async def change(tx: Any, _current: Any) -> None:
        nonlocal created
        created = (await tx.doc(ProviderDoc, runtime.conversationId))["sessionId"]

    await runtime.commit(change, context)
    if created is None:
        raise RuntimeError(f"Conversation {runtime.conversationId} has no provider session ID")
    return created


__all__ = ["ProviderDoc", "ProviderState", "ensureProviderSessionId"]
