"""A Session stores conversations, transcript entries, tasks, and documents.

Run from the repository root:
    python -m tests.durable_examples.example_00_conversation
"""

from __future__ import annotations

import asyncio

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import MemoryStorage, createSession


async def main() -> None:
    # MemoryStorage keeps everything in memory; other storage backends keep it on disk.
    session = createSession(MemoryStorage())

    # Every Session call takes a context, which is used for cancellation.
    # BACKGROUND_CONTEXT means "never cancel".
    context = BACKGROUND_CONTEXT

    # All writes happen inside session.commit(). The callback receives a
    # transaction `tx`; everything it writes is saved together when the callback
    # returns, or discarded if it throws.
    # "ownerless" means no task created this conversation.
    standalone = await session.commit(lambda tx: tx.createConversation({"ownership": {"kind": "ownerless"}}), context)
    print("standalone conversation:", standalone)

    await session.close(context)


if __name__ == "__main__":
    asyncio.run(main())
