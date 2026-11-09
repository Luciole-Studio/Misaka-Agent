"""Serialize asynchronous document work with a watch.

Run from the repository root:
    python -m tests.durable_examples.example_05_watches
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import MemoryStorage, createSession, defineDoc

Notes = defineDoc(
    {
        "kind": "example.notes",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda: {"text": ""},
    }
)


async def main() -> None:
    context = BACKGROUND_CONTEXT
    session = createSession(MemoryStorage())

    async def create(tx: Any) -> dict[str, Any]:
        conversation = await tx.createConversation({"ownership": {"kind": "ownerless"}})
        (await tx.doc(Notes, conversation["id"]))["text"] = "first"
        return conversation  # type: ignore[no-any-return]

    chat = await session.commit(create, context)

    # A watch starts from one stable acquisition revision. Slow callbacks never
    # overlap; exact committed frames buffer, with a full-value reset after 100.
    notesWatch = await session.watchDoc(Notes, chat["id"], context)
    if notesWatch is None:
        raise RuntimeError("notes are absent")
    print("watch baseline:", notesWatch.value)
    delivered: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def onUpdate(value: Any, _ops: Any, _deliveryContext: Any) -> None:
        print("watch update:", value)
        if not delivered.done():
            delivered.set_result(None)

    notesWatch.start(onUpdate)

    async def observe(tx: Any) -> None:
        (await tx.doc(Notes, chat["id"]))["text"] = "observed asynchronously"

    await session.commit(observe, context)
    await delivered
    await notesWatch.stop()

    await session.close(context)


if __name__ == "__main__":
    asyncio.run(main())
