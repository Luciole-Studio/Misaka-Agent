"""A background task that owns a child conversation.

Run from the repository root:
    python -m tests.durable_examples.example_03_owned_conversations
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import MemoryStorage, createSession, defineDoc, defineTask


async def nothing(*_: Any) -> None:
    return None


# A typical agent setup: a background task supervises a helper conversation,
# and the main conversation keeps a registry that maps agent names to their
# conversations. All three are created in one commit, so after a crash either
# all of them exist or none do.

# A task definition needs a name, a version, the task's starting state, a
# handler for every phase, and an abort handler (example_12_tasks.py runs a task).
# This example only creates the task record; a plain Session never runs it.
Supervisor = defineTask(
    {
        "name": "example.supervisor",
        "version": 1,
        "initial": lambda input: {"phase": "ready"},
        "phases": {"ready": nothing},
        "abort": nothing,
    }
)

# "latest" keeps only the current value. "initial" means forks of this
# conversation start without a registry, so a child doesn't inherit its
# parent's list of agents.
AgentRegistry = defineDoc(
    {
        "kind": "example.agent-registry",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": lambda: {"agents": {}},
    }
)


async def main() -> None:
    context = BACKGROUND_CONTEXT
    session = createSession(MemoryStorage())

    main_ = await session.commit(lambda tx: tx.createConversation({"ownership": {"kind": "ownerless"}}), context)

    async def setUp(tx: Any) -> dict[str, Any]:
        # `background: True` means the task is side work: waiting for the main
        # conversation to finish does not wait for it.
        supervisorId = await tx.createTask(
            Supervisor, None, {"ownership": {"kind": "conversation"}, "conversationId": main_["id"], "background": True}
        )

        # The child records that it belongs to the supervisor task. The task was
        # created a few lines above in this same commit, which is allowed.
        child = await tx.createConversation({"ownership": {"kind": "task", "taskId": supervisorId}})

        # requestId is a fixed name for the child's first message. Later code sends
        # that message using this requestId, so a retry after a crash cannot
        # deliver it twice.
        (await tx.doc(AgentRegistry, main_["id"]))["agents"]["researcher"] = {
            "conversationId": child["id"],
            "requestId": f"researcher:first-message:{supervisorId}",
        }
        return {"supervisorId": supervisorId, "child": child}

    setup = await session.commit(setUp, context)

    print("supervisor task:", setup["supervisorId"])
    print("child conversation:", setup["child"])
    print("registry:", await session.snapshot(AgentRegistry, main_["id"], context))

    await session.close(context)


if __name__ == "__main__":
    asyncio.run(main())
