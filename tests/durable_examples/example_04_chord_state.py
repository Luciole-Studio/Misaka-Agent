"""Expose a document through Chord.

Run from the repository root:
    python -m tests.durable_examples.example_04_chord_state
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

    # documentState() never creates a document. It returns a hydrated read-only
    # Chord state bound to the current concrete incarnation.
    notesState = await session.documentState(Notes, chat["id"], context)
    if notesState is None:
        raise RuntimeError("notes are absent")
    stopNotes = notesState.subscribe(
        lambda value, _deliveryContext, delivery: print("Chord notes:", delivery["kind"], delivery["sequence"], value)
    )

    async def publish(tx: Any) -> None:
        (await tx.doc(Notes, chat["id"]))["text"] = "published through Chord"

    await session.commit(publish, context)
    stopNotes()
    notesState.dispose()

    await session.close(context)


if __name__ == "__main__":
    asyncio.run(main())
