"""A reviewer agent next to the main one: a cheaper model, a review role and loop, read-only tools, and its own
checkout of the project.

Run from the repository root:
    python -m tests.durable_examples.example_28_reviewer
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider, fauxToolCall
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    AssistantEntry,
    GenerationTask,
    Harness,
    MemoryStorage,
    createRegistry,
    defineExtension,
    hook,
    section,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import CodingTools, createReadTool

context = BACKGROUND_CONTEXT


def textOf(message: dict[str, Any]) -> str:
    return "".join(part["text"] for part in message["content"] if part["type"] == "text")


# ─── Product code: the reviewer extension ───────────────────────────────────

DONE = "No further findings."


def reviewAgain(answer: dict[str, Any], *_: Any) -> dict[str, str] | None:
    if DONE in textOf(answer):
        return None
    return {"continue": f'Look again for anything you missed. Say "{DONE}" when there is nothing left.'}


# A role and a review loop: every answer that still has findings gets a second pass.
Reviewer = defineExtension(
    {
        "name": "reviewer",
        "sections": [section("role", lambda *_: "You review diffs. Report problems as a list. Never edit files.")],
        "hooks": [hook(GenerationTask, {"onYield": reviewAgain})],
    }
)


async def main() -> None:
    # ─── Host setup ─────────────────────────────────────────────────────────

    # The reviewer works in its own checkout, in practice a `git worktree add`.
    worktree = tempfile.mkdtemp(prefix="pi-durable-review-")
    Path(worktree, "user.ts").write_bytes(b"export const name = (user) => user.name;\n")

    faux = fauxProvider({"models": [{"id": "big"}, {"id": "small"}]})
    models = createModels()
    models.setProvider(faux.provider)
    faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("read", {"path": "user.ts"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("1. `name` does not handle a missing user."),
            fauxAssistantMessage(f"2. `user` has no type. {DONE}"),
        ]
    )

    registry = createRegistry()
    registry.install(CodingTools)
    registry.install(Reviewer)
    harness = await Harness.open(
        MemoryStorage(),
        # The main agent selects only CodingTools; the reviewer opts in.
        {
            "models": models,
            "registry": registry,
            "settings": {"extensions": [CodingTools]},
            "env": lambda target, *_: NodeExecutionEnv({"cwd": target.get("cwd", os.getcwd())}),
        },
        context,
    )
    await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "big"}}})

    # Everything the reviewer is, stored on its conversation: the model, exactly these extensions in this order, only
    # the read tool, and its directory. A restart keeps all of it.
    reviewer = await harness.createConversation(
        {
            "ownership": {"kind": "ownerless"},
            "agent": {
                "model": {"provider": "faux", "modelId": "small"},
                "extensions": [CodingTools, Reviewer],
                "tools": [createReadTool()],
                "cwd": worktree,
            },
        },
        context,
    )
    agent = await reviewer.agent(context)
    print(
        "reviewer:",
        (agent.get("model") or {}).get("modelId"),
        [extension["name"] for extension in agent["extensions"]],
        [tool["name"] for tool in agent["tools"]],
        agent.get("cwd") == worktree,
    )

    await (await reviewer.submit({"type": "input", "content": "Review user.ts."}, context)).wait(context)
    page = await reviewer.entries({}, 20, None, context)
    for entry in reversed(page["items"]):
        model = entry.get("model")
        message: Any = model[0] if model else None
        if message is not None and message["role"] == "user":
            print(">", message["content"])
        if AssistantEntry.is_(entry) and textOf(message) != "":
            print("reviewer:", textOf(message))

    await harness.close(context)
    shutil.rmtree(worktree, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
