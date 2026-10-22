"""Reload an extension while a call runs, then restart the process. Running work finishes on the code it started
with; stored choices are names, so they survive a restart and bind to whatever code the new process installs.

Run from the repository root:
    python -m tests.durable_examples.example_31_reload_and_restart
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Callable
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider, fauxToolCall
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    Harness,
    ToolResultEntry,
    createRegistry,
    defineExtension,
    defineTool,
)
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage

context = BACKGROUND_CONTEXT


def resolve(future: asyncio.Future[None]) -> None:
    """Resolve a promise stand-in; resolving it again does nothing, as in JavaScript."""
    if not future.done():
        future.set_result(None)


async def main() -> None:
    # Stand-in for code loaded from disk: each call builds the extension as the file currently reads.
    started: Callable[[], None] = lambda: None
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    resolve(gate)

    def loadVersioned(version: str) -> Any:
        async def execute(*_: Any) -> dict[str, Any]:
            started()
            await gate
            return {"output": [{"type": "text", "text": version}]}

        return defineExtension(
            {
                "name": "versioned",
                "tools": [
                    defineTool(
                        {
                            "name": "version",
                            "description": "Report the tool's code version",
                            "parameters": {"type": "object", "properties": {}},
                            "execute": execute,
                        }
                    )
                ],
            }
        )

    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)

    def callVersion() -> Any:
        return fauxAssistantMessage([fauxToolCall("version", {})], {"stopReason": "toolUse"})

    faux.setResponses([callVersion(), fauxAssistantMessage("Done."), callVersion(), fauxAssistantMessage("Done.")])

    directory = tempfile.mkdtemp(prefix="pi-durable-reload-")

    async def open(registry: Any) -> Any:
        return await Harness.open(
            await openNodeSqliteStorage(os.path.join(directory, "session.sqlite")),
            {"models": models, "registry": registry},
            context,
        )

    # First process.
    registry = createRegistry()
    registry.install(loadVersioned("v1"))
    harness = await open(registry)
    root = await harness.root(
        context,
        {
            # Selected by name. The name is what is stored, never the code.
            "agent": {"model": {"provider": "faux", "modelId": "faux-1"}, "extensions": [loadVersioned("v1")]}
        },
    )

    async def lastResult() -> str:
        page = await root.entries({}, 10, None, context)
        entry = next(entry for entry in page["items"] if ToolResultEntry.is_(entry))
        result = entry["model"][0]
        return "".join(part["text"] if part["type"] == "text" else "" for part in result["content"])

    # The file changes while a call runs: the running call finishes on v1, the next call uses v2.
    gate = asyncio.get_running_loop().create_future()
    running: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    started = lambda: resolve(running)
    submission = await root.submit({"type": "input", "content": "Which version?"}, context)
    await running
    registry.install(loadVersioned("v2"))
    resolve(gate)
    await submission.wait(context)
    print("call running during the reload:", await lastResult())
    await (await root.submit({"type": "input", "content": "And now?"}, context)).wait(context)
    print("next call:", await lastResult())
    await harness.close(context)

    # Second process: the conversation still selects "versioned", but this process has not installed it yet.
    registry = createRegistry()
    harness = await open(registry)
    root = await harness.root(context)

    async def tools() -> list[str]:
        return [tool["name"] for tool in (await root.agent(context))["tools"]]

    print("after restart, before install:", await tools())
    registry.install(loadVersioned("v3"))
    print("after install:", await tools())

    await harness.close(context)
    shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
