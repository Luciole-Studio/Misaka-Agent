"""A small coding agent: the built-in coding tools, a prompt that knows the working directory, settings backed by the
app's settings file, and an environment that follows the conversation's directory.

Run from the repository root:
    python -m tests.durable_examples.example_26_coding_agent
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping
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
    section,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import CodingTools

context = BACKGROUND_CONTEXT


class Settings(Mapping[str, Any]):
    """Settings whose fields are read at every use, like getters."""

    def __init__(self, getters: dict[str, Any]) -> None:
        self.getters = getters

    def __getitem__(self, key: str) -> Any:
        return self.getters[key]()

    def __iter__(self) -> Iterator[str]:
        return iter(self.getters)

    def __len__(self) -> int:
        return len(self.getters)


async def main() -> None:
    workspace = tempfile.mkdtemp(prefix="pi-durable-agent-")
    os.mkdir(os.path.join(workspace, "app"))

    # The app's own prompt, next to the coding tools.
    Coding = defineExtension(
        {
            "name": "coding",
            "sections": [
                section("preamble", lambda *_: "You are a coding agent. Use the tools to inspect the project.", {"tag": False}),
                section("cwd", lambda input, *_: None if input["env"] is None else input["env"].cwd),
            ],
        }
    )
    registry = createRegistry()
    registry.install(CodingTools)
    registry.install(Coding)

    # Settings the user edits while the agent runs, like a settings.json the app watches. The getters make every
    # Harness read see the current file; nothing is copied or stored.
    userSettings = {"parallelTools": True, "maxRetries": 3}
    settings = Settings(
        {
            "toolExecution": lambda: "parallel" if userSettings["parallelTools"] else "sequential",
            "retry": lambda: {"maxRetries": userSettings["maxRetries"]},
        }
    )

    # The Harness calls this for every tool call and request with the conversation's `cwd`, so changing a
    # conversation's directory needs no restart.
    def env(target: dict[str, Any], callContext: Any) -> Any:
        return NodeExecutionEnv({"cwd": target.get("cwd", workspace)})

    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)

    def pwd() -> Any:
        return fauxAssistantMessage([fauxToolCall("bash", {"command": "pwd"})], {"stopReason": "toolUse"})

    faux.setResponses([pwd(), fauxAssistantMessage("Done."), pwd(), fauxAssistantMessage("Done.")])

    harness = await Harness.open(
        MemoryStorage(), {"models": models, "registry": registry, "settings": settings, "env": env}, context
    )
    root = await harness.root(
        context, {"agent": {"model": {"provider": "faux", "modelId": "faux-1"}, "cwd": workspace}}
    )

    async def lastBashOutput() -> str:
        page = await root.entries({}, 10, None, context)
        entry = next(entry for entry in page["items"] if ToolResultEntry.is_(entry))
        result = entry["model"][0]
        return "".join(part["text"].strip() if part["type"] == "text" else "" for part in result["content"])

    await (await root.submit({"type": "input", "content": "Where are we?"}, context)).wait(context)
    print("bash ran in:", await lastBashOutput())

    # The user switches the project directory and turns off parallel tools. Both apply from the next use.
    await root.configure({"cwd": os.path.join(workspace, "app")}, context)
    userSettings["parallelTools"] = False
    await (await root.submit({"type": "input", "content": "And now?"}, context)).wait(context)
    print("bash ran in:", await lastBashOutput())

    await harness.close(context)
    shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
