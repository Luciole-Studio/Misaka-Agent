"""Conversation handles, typed entries, created conversations, and forks.

Run from the repository root:
    python -m tests.durable_examples.example_08_harness_conversations
"""

from __future__ import annotations

import asyncio

from misaka.ai.models import createModels
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import Harness, MemoryStorage, createRegistry, defineEntry

context = BACKGROUND_CONTEXT


async def main() -> None:
    harness = await Harness.open(MemoryStorage(), {"models": createModels(), "registry": createRegistry()}, context)
    root = await harness.root(context)
    await root.configure({"thinkingLevel": "high"}, context)

    # Conversation handles are stateless; compare them by id. They bind commits
    # to their conversation. An entry token types an entry kind's `data`.
    Message = defineEntry("message")
    hello = await root.commit(
        lambda tx: tx.appendEntry(
            Message,
            root.id,
            {"data": {"from": "example"}, "model": [{"role": "user", "content": "hello", "timestamp": 1}]},
        ),
        context,
    )
    print("typed entry:", Message.is_(hello), hello["data"]["from"])

    # createConversation() and fork() apply `agent` and run `init` in the
    # creating commit. A fork starts with the agent the parent had at the fork
    # entry.
    helper = await harness.createConversation(
        {"ownership": {"kind": "ownerless"}, "agent": {"thinkingLevel": "minimal"}}, context
    )
    retry = await root.fork(hello["id"], {"ownership": {"kind": "ownerless"}}, context)
    print("helper thinking:", (await helper.agent(context))["thinkingLevel"])
    print("fork thinking:", (await retry.agent(context))["thinkingLevel"])
    found = await harness.conversation(retry.id, context)
    print("lookup:", found is not None and found.id == retry.id)

    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
