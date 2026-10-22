"""An extension that keeps its own per-conversation document.

Run from the repository root:
    python -m tests.durable_examples.example_11_extension_state
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider, fauxToolCall
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    Harness,
    MemoryStorage,
    createRegistry,
    defineDoc,
    defineExtension,
    defineTool,
    section,
)

context = BACKGROUND_CONTEXT

# A todo list per conversation. Rewindable with `asOf`, so a fork starts with
# the todos its parent had at the fork entry.
Todos = defineDoc(
    {
        "kind": "example.todos",
        "version": 1,
        "scope": "conversation",
        "history": "rewindable",
        "fork": "asOf",
        "initial": lambda: {"items": []},
    }
)


async def addTodo(args: dict[str, Any], api: Any, callContext: Any) -> dict[str, Any]:
    async def change(tx: Any) -> None:
        (await tx.doc(Todos, api.conversationId))["items"].push(args["item"])

    await api.commit(change, callContext)
    return {"output": [{"type": "text", "text": f"added {args['item']}"}]}


async def renderTodos(input: dict[str, Any], renderContext: Any) -> str | None:
    todos = await input["read"].snapshot(Todos, input["conversationId"], renderContext)
    return None if todos is None or len(todos["items"]) == 0 else "\n".join(todos["items"])


# The tool writes the document; the section shows it to the model before every
# request. Nothing needs to create it up front: tx.doc() creates it on first
# write, and the section treats a missing document as an empty list.
Todo = defineExtension(
    {
        "name": "todo",
        "tools": [
            defineTool(
                {
                    "name": "todo",
                    "description": "Add an item to your todo list",
                    "parameters": {"type": "object", "properties": {"item": {"type": "string"}}, "required": ["item"]},
                    "execute": addTodo,
                }
            )
        ],
        "sections": [section("todos", renderTodos)],
    }
)


async def main() -> None:
    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)
    registry = createRegistry()
    registry.install(Todo)
    harness = await Harness.open(MemoryStorage(), {"models": models, "registry": registry}, context)
    root = await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "faux-1"}}})

    faux.setResponses(
        [
            fauxAssistantMessage(fauxToolCall("todo", {"item": "fix the build"}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("Noted."),
            fauxAssistantMessage("Working on it."),
        ]
    )
    await (await root.submit({"type": "input", "content": "Remember to fix the build."}, context)).wait(context)
    print("todos:", await harness.snapshot(Todos, root.id, context))

    # The next request's system prompt carries the list. The first system message
    # only announced the todo tool; no section had text yet.
    await (await root.submit({"type": "input", "content": "What is next?"}, context)).wait(context)
    messages = (await root.context(context))["messages"]
    print(
        "system messages:",
        [
            {
                "sections": message.get("sections"),
                "toolsAdded": None
                if message.get("toolsAdded") is None
                else [tool["name"] for tool in message["toolsAdded"]],
            }
            for message in messages
            if message["role"] == "system"
        ],
    )

    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
