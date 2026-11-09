"""Fork a conversation.

Run from the repository root:
    python -m tests.durable_examples.example_02_forks
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

    chat = await session.commit(lambda tx: tx.createConversation({"ownership": {"kind": "ownerless"}}), context)

    async def hello(tx: Any) -> dict[str, Any]:
        entry = await tx.appendEntry(chat["id"], {"kind": "note", "data": "hello"})
        (await tx.doc(Notes, chat["id"]))["text"] = "after hello"
        return entry  # type: ignore[no-any-return]

    firstEntry = await session.commit(hello, context)

    async def goodbye(tx: Any) -> None:
        await tx.appendEntry(chat["id"], {"kind": "note", "data": "goodbye"})
        (await tx.doc(Notes, chat["id"]))["text"] = "after goodbye"

    await session.commit(goodbye, context)

    # A fork is a new conversation that continues from one entry of another. It
    # sees the parent's transcript up to that entry, and each document follows its
    # own `fork` setting. Notes uses "asOf", so the fork starts with the notes
    # value from the fork entry.
    branch = await session.commit(
        lambda tx: tx.forkConversation(chat["id"], firstEntry["id"], {"ownership": {"kind": "ownerless"}}), context
    )

    # scanEntries() pages through visible entries, newest first. The fork sees
    # "hello" (inherited from the parent) but not "goodbye", which came later.
    branchEntries = await session.commit(lambda tx: tx.scanEntries({"conversationId": branch["id"]}, 10), context)
    print("fork transcript:", [entry["data"] for entry in branchEntries["items"]])
    print("fork notes:", await session.snapshot(Notes, branch["id"], context))

    # The fork's copy is independent: editing it leaves the parent unchanged.
    async def edit(tx: Any) -> None:
        (await tx.doc(Notes, branch["id"]))["text"] = "changed only in the fork"

    await session.commit(edit, context)
    print("fork notes after edit:", await session.snapshot(Notes, branch["id"], context))
    print("parent notes after edit:", await session.snapshot(Notes, chat["id"], context))

    await session.close(context)


if __name__ == "__main__":
    asyncio.run(main())
