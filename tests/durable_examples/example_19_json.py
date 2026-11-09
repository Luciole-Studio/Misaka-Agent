"""JSON mode: stream one prompt's run as JSON lines, like ``pi --mode json``.

Two modes:

  --events (default): the experimental agent events of watchEvents(), starting with a ``snapshot`` event.
  --ops: the raw ConversationView frames, starting with the view itself; each later line holds one commit's ops.

--storage sqlite (default) | jsonl | memory: sqlite writes a temporary database file, jsonl a temporary directory;
neither is deleted, and the path is printed to stderr at the end.
Uses OpenAI when OPENAI_API_KEY is set, and a scripted faux model otherwise.

Run from the repository root:
    python -m tests.durable_examples.example_19_json --events "What is in this directory?"
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import (
    fauxAssistantMessage,
    fauxProvider,
    fauxText,
    fauxToolCall,
)
from misaka.ai.providers.openai import openaiProvider
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    Harness,
    MemoryStorage,
    createRegistry,
    defineExtension,
    section,
    watchEvents,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.storage.jsonl.node import openNodeJsonlStorage
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.durable.tools import createBashTool, createReadTool
from misaka.utils.js import jsonStringify

context = BACKGROUND_CONTEXT


def printLine(value: Any) -> None:
    sys.stdout.write(f"{jsonStringify(value)}\n")


async def main(args: list[str]) -> None:
    mode = "ops" if "--ops" in args else "events"
    storageIndex = args.index("--storage") if "--storage" in args else -1
    storageKind = "sqlite" if storageIndex < 0 else args[storageIndex + 1]
    prompt = next(
        (
            arg
            for index, arg in enumerate(args)
            if not arg.startswith("--") and (storageIndex < 0 or index != storageIndex + 1)
        ),
        "What is in this directory?",
    )

    location: str | None = None
    if storageKind == "sqlite":
        location = os.path.join(tempfile.gettempdir(), f"pi-durable-json-{int(time.time() * 1000)}.sqlite")
        storage: Any = await openNodeSqliteStorage(location)
    elif storageKind == "jsonl":
        location = tempfile.mkdtemp(prefix="pi-durable-json-")
        storage = await openNodeJsonlStorage(location, context)
    elif storageKind == "memory":
        storage = MemoryStorage()
    else:
        raise RuntimeError(f"Unknown --storage {storageKind}; use sqlite, jsonl, or memory")

    models = createModels()
    model = {"provider": "openai", "modelId": "gpt-6-sol"}
    if os.environ.get("OPENAI_API_KEY") is not None:
        models.setProvider(openaiProvider())
    else:
        faux = fauxProvider({"tokensPerSecond": 200})
        models.setProvider(faux.provider)
        model = {"provider": "faux", "modelId": "faux-1"}
        faux.setResponses(
            [
                fauxAssistantMessage([fauxToolCall("bash", {"command": "ls"}, {"id": "call-1"})], {"stopReason": "toolUse"}),
                fauxAssistantMessage([fauxText("This directory holds the durable package sources, tests, and docs.")]),
            ]
        )

    registry = createRegistry()
    registry.install(
        defineExtension(
            {
                "name": "coding",
                "tools": [createReadTool(), createBashTool()],
                "sections": [section("preamble", lambda *_: "You are a concise coding assistant.", {"tag": False})],
            }
        )
    )
    env = NodeExecutionEnv({"cwd": os.getcwd()})
    harness = await Harness.open(storage, {"models": models, "registry": registry, "env": lambda *_: env}, context)
    root = await harness.root(context, {"agent": {"model": model}})

    # Attach before submitting, so the stream covers the whole run.
    if mode == "events":
        stream = await watchEvents(harness, root.id, context)
        printLine(stream.snapshot)

        async def onEvents(events: Any, *_: Any) -> None:
            for event in events:
                printLine(event)

        stream.start(onEvents)
        stop = stream.stop
    else:
        watch = await root.watch(context)
        printLine({"view": watch.value})

        async def onOps(_value: Any, ops: Any, _context: Any) -> None:
            printLine({"ops": ops})

        watch.start(onOps)
        stop = watch.stop

    submission = await root.submit({"type": "input", "content": prompt}, context)
    await submission.wait(context)
    await harness.waitForIdle(context)
    # Let the last batch reach the listener before stopping.
    await asyncio.sleep(0)
    await stop()
    await harness.close(context)
    if location is not None:
        print(f"{storageKind} storage: {location}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
