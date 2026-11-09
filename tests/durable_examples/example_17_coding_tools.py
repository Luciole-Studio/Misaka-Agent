"""A coding-agent tool turn on JSONL storage: the model reads, edits, and runs commands, then answers.

A hook times a ``cat`` of /tmp/1gb.txt (create it first to measure; without it that call ends in an error result). The
storage directory is left behind for inspection.

Run from the repository root:
    python -m tests.durable_examples.example_17_coding_tools
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import (
    fauxAssistantMessage,
    fauxProvider,
    fauxText,
    fauxToolCall,
)
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    AssistantEntry,
    Harness,
    ToolResultEntry,
    ToolTask,
    createRegistry,
    defineExtension,
    hook,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.storage.jsonl.node import openNodeJsonlStorage
from misaka.durable.tools import CodingTools
from misaka.utils.js import jsonStringify

context = BACKGROUND_CONTEXT


def toolCall(name: str, args: dict[str, Any], id: str) -> Any:
    return fauxAssistantMessage([fauxToolCall(name, args, {"id": id})], {"stopReason": "toolUse"})


# Hooks see every call before and after execution; these time the big `cat`.
def isBigCat(call: dict[str, Any]) -> bool:
    return call["name"] == "bash" and "1gb.txt" in call["arguments"]["command"]


async def main() -> None:
    directory = tempfile.mkdtemp(prefix="pi-durable-example-")
    Path(directory, "notes.txt").write_text("hello world\n", encoding="utf-8", newline="")

    # The faux provider plays the model: four tool-calling answers, then a final answer.
    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)
    faux.setResponses(
        [
            toolCall("read", {"path": "notes.txt"}, "r"),
            toolCall("edit", {"path": "notes.txt", "edits": [{"oldText": "world", "newText": "durable"}]}, "e"),
            toolCall("bash", {"command": "cat notes.txt"}, "b"),
            toolCall("bash", {"command": "cat /tmp/1gb.txt"}, "c"),
            fauxAssistantMessage([fauxText("The file now greets durable.")]),
        ]
    )

    timer = {"start": 0.0}

    def beforeTool(call: dict[str, Any], *_: Any) -> None:
        if isBigCat(call):
            timer["start"] = time.time()

    def afterTool(call: dict[str, Any], *_: Any) -> None:
        if isBigCat(call):
            print(f"cat /tmp/1gb.txt took {int((time.time() - timer['start']) * 1000)} ms")

    Timing = defineExtension({"name": "timing", "hooks": [hook(ToolTask, {"beforeTool": beforeTool, "afterTool": afterTool})]})
    registry = createRegistry()
    # CodingTools brings read, write, edit, and bash. Tools reach files and processes only through the environment the
    # Harness builds for each call.
    registry.install(CodingTools)
    registry.install(Timing)

    # The environment is built per use and follows the conversation's agent `cwd`.
    storage = await openNodeJsonlStorage(directory, context)
    harness = await Harness.open(
        storage,
        {
            "models": models,
            "registry": registry,
            "env": lambda target, callContext: NodeExecutionEnv({"cwd": target.get("cwd") or os.getcwd()}),
        },
        context,
    )
    root = await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "faux-1"}, "cwd": directory}})

    # Each tool call runs as a durable pi.tool task owned by the generation, which waits for them and continues the run
    # with the next generation.
    settled = await (await root.submit({"type": "input", "content": "Greet durable instead."}, context)).wait(context)
    print("status:", settled["status"])
    transcript = await root.entries({}, 20, None, context)
    for entry in reversed(transcript["items"]):
        if not ToolResultEntry.is_(entry):
            print(entry["kind"])
            continue
        result = entry["model"][0]
        text = "".join(item["text"] if item["type"] == "text" else "" for item in result["content"])
        print(f"{entry['kind']} {result['toolName']}:", jsonStringify(f"{text[:200]}…" if len(text) > 200 else text))
    if settled["status"] == "done" and settled["type"] == "input":
        answer = await root.commit(lambda tx: tx.entry(AssistantEntry, settled["answer"]), context)
        message = ((answer or {}).get("model") or [None])[0]
        print("answer:", message["content"] if message is not None and message["role"] == "assistant" else message)
    print("file:", jsonStringify(Path(directory, "notes.txt").read_text(encoding="utf-8")))
    await harness.close(context)
    print("storage:", directory)


if __name__ == "__main__":
    asyncio.run(main())
