"""Per-conversation agent choices and Harness-wide settings.

Run from the repository root:
    python -m tests.durable_examples.example_07_configuration
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
    defineExtension,
    defineTool,
    section,
)

context = BACKGROUND_CONTEXT


def exampleTool(name: str, description: str) -> dict[str, Any]:
    """Apps may attach their own metadata to tools, such as a prompt snippet."""

    async def execute(args: dict[str, Any], api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": f"{name} {args['path']}"}]}

    tool = defineTool(
        {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            "execute": execute,
        }
    )
    return {**tool, "snippet": f"Use {name} for files."}


read = exampleTool("read", "Read a file")
write = exampleTool("write", "Write a file")
grep = exampleTool("grep", "Search files")
Files = defineExtension({"name": "files", "tools": [read, write]})
Search = defineExtension({"name": "search", "tools": [grep]})
# Sections see the offered tools as app tools, so one can render their snippets.
Snippets = defineExtension(
    {
        "name": "snippets",
        "sections": [
            section("tool_snippets", lambda input, *_: "\n".join(tool["snippet"] for tool in input["agent"]["tools"]))
        ],
    }
)


async def main() -> None:
    registry = createRegistry()
    registry.install(Files)
    registry.install(Search)
    registry.install(Snippets)

    # Settings are Harness-wide run policy, read at every use and never stored.
    # A getter makes a value live, such as one backed by the app's settings file.
    live = {"timeoutMs": 60_000}

    class Settings(dict[str, Any]):
        def get(self, key: str, default: Any = None) -> Any:
            if key == "stream":
                return {"timeoutMs": live["timeoutMs"]}
            return super().get(key, default)

    settings = Settings(retry={"maxRetries": 5}, toolExecution="sequential")
    harness = await Harness.open(
        MemoryStorage(), {"models": createModels(), "registry": registry, "settings": settings}, context
    )
    root = await harness.root(context)

    # A new conversation stores no choices and follows the host: every installed
    # extension, all of their tools.
    async def tools() -> list[str]:
        return [tool["name"] for tool in (await root.agent(context))["tools"]]

    print("default tools:", await tools())

    # configure() stores choices in the conversation's pi.agent document, one
    # commit per call. Extensions and tools are passed as objects and stored by
    # name, so a typo cannot slip in.
    await root.configure(
        {
            "model": {"provider": "anthropic", "modelId": "claude-sonnet-4-5"},
            "thinkingLevel": "high",
            "tools": [write, read],
        },
        context,
    )
    print("stored:", await harness.snapshot(AgentDoc, root.id, context))
    agent = await root.agent(context)
    print("model:", agent["model"], "thinking:", agent["thinkingLevel"], "tools:", await tools())

    # Deselect an extension; `None` clears a stored field back to the host default.
    await root.configure({"extensions": {"remove": [Search]}, "tools": None}, context)
    print("without search:", await tools())

    # Stored names outlive the code. The conversation selects exactly these two
    # extensions; uninstalling "files" leaves it selected, and requests just stop
    # offering its tools until it is back.
    await root.configure({"extensions": [Files, Search]}, context)
    registry.uninstall(Files)
    print("files uninstalled:", await tools())
    registry.install(Files)
    print("files reinstalled:", await tools())

    # Settings changes need no commit; the next request uses the new timeout.
    live["timeoutMs"] = 120_000

    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
