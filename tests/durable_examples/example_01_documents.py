"""Store document state next to transcript entries.

Run from the repository root:
    python -m tests.durable_examples.example_01_documents
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import MemoryStorage, createSession, defineDoc

# A document is a JSON object attached to something; here, one per conversation.
# "rewindable" keeps old values readable, so you can ask what the document
# looked like when a particular entry was written.
# `fork` says what a forked copy of the conversation starts with (see example_02_forks.py).
Notes = defineDoc(
    {
        "kind": "example.notes",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",  # a fork starts with the value these notes had at the fork entry
        "initial": lambda: {"text": ""},
    }
)


async def main() -> None:
    context = BACKGROUND_CONTEXT
    session = createSession(MemoryStorage())

    chat = await session.commit(lambda tx: tx.createConversation({"ownership": {"kind": "ownerless"}}), context)

    # tx.doc() returns an editable copy of the document (created on first use).
    # Plain assignments to it are saved when the commit finishes.
    async def hello(tx: Any) -> dict[str, Any]:
        entry = await tx.appendEntry(chat["id"], {"kind": "note", "data": "hello"})
        (await tx.doc(Notes, chat["id"]))["text"] = "after hello"
        return entry  # type: ignore[no-any-return]

    firstEntry = await session.commit(hello, context)

    async def goodbye(tx: Any) -> dict[str, Any]:
        entry = await tx.appendEntry(chat["id"], {"kind": "note", "data": "goodbye"})
        (await tx.doc(Notes, chat["id"]))["text"] = "after goodbye"
        return entry  # type: ignore[no-any-return]

    secondEntry = await session.commit(goodbye, context)

    # snapshot() reads the latest value. snapshotAsOf() reads the value that was
    # saved in the same commit as the given entry.
    print("latest notes:", await session.snapshot(Notes, chat["id"], context))
    print("notes at first entry:", await session.snapshotAsOf(Notes, chat["id"], firstEntry["id"], context))
    print("notes at second entry:", await session.snapshotAsOf(Notes, chat["id"], secondEntry["id"], context))

    await session.close(context)


if __name__ == "__main__":
    asyncio.run(main())
