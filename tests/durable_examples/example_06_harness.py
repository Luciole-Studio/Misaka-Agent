"""Open a Harness with a registry of extensions.

Run from the repository root:
    python -m tests.durable_examples.example_06_harness
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.ai.models import createModels
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    AgentDoc,
    Harness,
    MemoryStorage,
    createRegistry,
    defineDoc,
    defineExtension,
    defineTool,
)

context = BACKGROUND_CONTEXT


# A Harness is a Session plus conversation handles and durable tasks. Extension
# code (tools, prompt sections, hooks, tasks) comes in named extensions
# installed in a registry the application owns. Nothing in the registry is
# saved; it is this process's code.
async def readFile(args: dict[str, Any], api: Any, callContext: Any) -> dict[str, Any]:
    return {"output": [{"type": "text", "text": f"contents of {args['path']}"}]}


read = defineTool(
    {
        "name": "read",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "execute": readFile,
    }
)
Files = defineExtension({"name": "files", "tools": [read]})

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
    registry = createRegistry()
    registry.install(Files)

    # `models` is pi-ai's model access; generation calls models through it.
    harness = await Harness.open(MemoryStorage(), {"models": createModels(), "registry": registry}, context)

    # The root conversation always has ID 1. The first root() call creates it,
    # its built-in documents, the `agent` choices, and whatever `init` writes, all
    # in one commit. Later calls, including after a restart, return it and ignore
    # both options.
    async def init(tx: Any, rootId: int) -> None:
        (await tx.doc(Notes, rootId))["text"] = "root notes"

    root = await harness.root(context, {"agent": {"thinkingLevel": "low"}, "init": init})
    print("root:", root.id, await harness.snapshot(Notes, root.id, context))
    # The stored choices are names; agent() resolves them against the registry.
    print("stored agent:", await harness.snapshot(AgentDoc, root.id, context))
    agent = await root.agent(context)
    print(
        "resolved:",
        agent["thinkingLevel"],
        [extension["name"] for extension in agent["extensions"]],
        [tool["name"] for tool in agent["tools"]],
    )

    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
