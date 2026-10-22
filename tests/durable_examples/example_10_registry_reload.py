"""Reload extension code through the registry.

Run from the repository root:
    python -m tests.durable_examples.example_10_registry_reload
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.ai.models import createModels
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    Harness,
    MemoryStorage,
    createRegistry,
    defineExtension,
    defineTool,
    wrapTool,
)

context = BACKGROUND_CONTEXT


def exampleTool(name: str, description: str) -> dict[str, Any]:
    async def execute(args: dict[str, Any], api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": f"{name} {args['path']}"}]}

    return defineTool(
        {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            "execute": execute,
        }
    )


read = exampleTool("read", "Read a file")


async def main() -> None:
    registry = createRegistry()
    registry.install(defineExtension({"name": "files", "tools": [read, exampleTool("grep", "Search files")]}))
    harness = await Harness.open(MemoryStorage(), {"models": createModels(), "registry": registry}, context)
    root = await harness.root(context)

    async def tools() -> list[str]:
        return [f"{tool['name']}: {tool['description']}" for tool in (await root.agent(context))["tools"]]

    # Reloading is installing a new object with the same name: it replaces the old
    # one in place, in one publication, so no conversation ever sees it missing.
    # A task phase that already started keeps the snapshot it took; the next phase
    # uses the new code.
    registry.install(defineExtension({"name": "files", "tools": [read, exampleTool("grep", "Search files, faster")]}))
    print("after reload:", await tools())

    # Another extension may wrap a tool by name. The wrap applies where the
    # wrapping extension is selected, and survives reloads of the wrapped tool.
    Audit = defineExtension(
        {
            "name": "audit",
            "wraps": [wrapTool(read, lambda tool: {**tool, "description": f"{tool['description']} (audited)"})],
        }
    )
    registry.install(Audit)
    print("with audit:", await tools())

    # Uninstall removes an extension by name; conversations that select it stop
    # getting its code.
    registry.uninstall(Audit)
    print("after uninstall:", await tools())

    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
