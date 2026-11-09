"""Replace a tool for some conversations, and decorate whichever tool wins: a bash that runs inside a Python
virtualenv, and a wrapper that times every bash call.

Run from the repository root:
    python -m tests.durable_examples.example_30_tool_override
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider, fauxToolCall
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    Harness,
    MemoryStorage,
    ToolResultEntry,
    createRegistry,
    defineExtension,
    wrapTool,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import CodingTools, createBashTool

context = BACKGROUND_CONTEXT

# ─── Product code ───────────────────────────────────────────────────────────

# A bash with the same name: where selected after CodingTools, it replaces CodingTools' bash in place.
Venv = defineExtension(
    {"name": "venv", "tools": [createBashTool({"commandPrefix": "source .venv/bin/activate"})]}
)

# Wraps the bash that won, whichever it is. Wrappers never capture a base tool, so reloading either bash keeps it.
timings: list[int] = []


def timed(bash: dict[str, Any]) -> dict[str, Any]:
    async def execute(args: dict[str, Any], api: Any, callContext: Any) -> Any:
        start = int(time.time() * 1000)
        try:
            return await bash["execute"](args, api, callContext)
        finally:
            timings.append(int(time.time() * 1000) - start)

    return {**bash, "execute": execute}


Timing = defineExtension({"name": "timing", "wraps": [wrapTool(createBashTool(), timed)]})


async def main() -> None:
    # ─── Host setup ─────────────────────────────────────────────────────────

    project = tempfile.mkdtemp(prefix="pi-durable-venv-")
    os.makedirs(os.path.join(project, ".venv/bin"), exist_ok=True)
    Path(project, ".venv/bin/activate").write_bytes(f'export VIRTUAL_ENV="{project}/.venv"\n'.encode())

    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)

    def probe() -> Any:
        return fauxAssistantMessage([fauxToolCall("bash", {"command": "echo venv: $VIRTUAL_ENV"})], {"stopReason": "toolUse"})

    faux.setResponses([probe(), fauxAssistantMessage("Done."), probe(), fauxAssistantMessage("Done.")])

    registry = createRegistry()
    registry.install(CodingTools)
    registry.install(Timing)
    registry.install(Venv)
    harness = await Harness.open(
        MemoryStorage(),
        {
            "models": models,
            "registry": registry,
            # Venv is installed but not selected by default.
            "settings": {"extensions": [CodingTools, Timing]},
            "env": lambda *_: NodeExecutionEnv({"cwd": project}),
        },
        context,
    )
    model = {"provider": "faux", "modelId": "faux-1"}
    plain = await harness.root(context, {"agent": {"model": model}})
    python = await harness.createConversation(
        {"ownership": {"kind": "ownerless"}, "agent": {"model": model, "extensions": {"add": [Venv]}}}, context
    )

    async def probeBash(conversation: Any) -> str:
        await (await conversation.submit({"type": "input", "content": "Which venv?"}, context)).wait(context)
        page = await conversation.entries({}, 10, None, context)
        entry = next(entry for entry in page["items"] if ToolResultEntry.is_(entry))
        result = entry["model"][0]
        return "".join(part["text"].strip() if part["type"] == "text" else "" for part in result["content"])

    print("plain conversation:", await probeBash(plain))
    print("python conversation:", (await probeBash(python)).replace(project, "<project>"))
    print("timed bash calls:", len(timings))

    await harness.close(context)
    shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
