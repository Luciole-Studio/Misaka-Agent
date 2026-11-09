"""One sandbox per conversation: the app records each conversation's sandbox in its own document, and the Harness
environment function looks it up for every tool call. Here a sandbox is a directory; a hosted product would return an
ExecutionEnv that runs inside the conversation's container.

Run from the repository root:
    python -m tests.durable_examples.example_29_sandbox_per_conversation
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider, fauxToolCall
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import Harness, MemoryStorage, createRegistry, defineDoc
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import CodingTools

context = BACKGROUND_CONTEXT

# Which sandbox a conversation runs in, ``{"path"?: str}``. `fork: "initial"`: a fork gets no sandbox until the app
# assigns one.
Sandbox = defineDoc(
    {
        "kind": "app.sandbox",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        "fork": "initial",
        "initial": dict,
    }
)


async def main() -> None:
    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)

    def note(text: str) -> Any:
        return fauxAssistantMessage([fauxToolCall("write", {"path": "note.txt", "content": text})], {"stopReason": "toolUse"})

    faux.setResponses(
        [note("from alice"), fauxAssistantMessage("Saved."), note("from bob"), fauxAssistantMessage("Saved.")]
    )

    registry = createRegistry()
    registry.install(CodingTools)

    # Committed reads only; a conversation without a sandbox gets no environment, so its tools fail cleanly.
    async def env(target: dict[str, Any], envContext: Any) -> Any:
        sandbox = await target["read"].snapshot(Sandbox, target["conversationId"], envContext)
        return None if sandbox is None or "path" not in sandbox else NodeExecutionEnv({"cwd": sandbox["path"]})

    harness = await Harness.open(MemoryStorage(), {"models": models, "registry": registry, "env": env}, context)

    # Each user's conversation gets a fresh sandbox in the creating commit.
    model = {"provider": "faux", "modelId": "faux-1"}

    async def conversationFor(user: str) -> tuple[Any, str]:
        path = tempfile.mkdtemp(prefix=f"pi-durable-sandbox-{user}-")

        async def init(tx: Any, id: int) -> None:
            (await tx.doc(Sandbox, id))["path"] = path

        conversation = await harness.createConversation(
            {"ownership": {"kind": "ownerless"}, "agent": {"model": model}, "init": init}, context
        )
        return conversation, path

    alice, alicePath = await conversationFor("alice")
    bob, bobPath = await conversationFor("bob")
    await (await alice.submit({"type": "input", "content": "Leave a note."}, context)).wait(context)
    await (await bob.submit({"type": "input", "content": "Leave a note."}, context)).wait(context)
    print("alice's sandbox:", Path(alicePath, "note.txt").read_text(encoding="utf-8"))
    print("bob's sandbox:", Path(bobPath, "note.txt").read_text(encoding="utf-8"))

    await harness.close(context)
    shutil.rmtree(alicePath, ignore_errors=True)
    shutil.rmtree(bobPath, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
