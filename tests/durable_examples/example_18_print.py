"""Print mode: submit one prompt and print its answer, like ``pi -p``.

The host awaits its own Submission, not global idle. Uses OpenAI when OPENAI_API_KEY is set, and a scripted faux model
otherwise.

Run from the repository root:
    python -m tests.durable_examples.example_18_print "What is in this directory?"
"""

from __future__ import annotations

import asyncio
import os
import sys
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
    AssistantEntry,
    Harness,
    MemoryStorage,
    createRegistry,
    defineExtension,
    section,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import createBashTool, createReadTool

context = BACKGROUND_CONTEXT


async def main(prompt: str = "What is in this directory?") -> int:
    models = createModels()
    model = {"provider": "openai", "modelId": "gpt-6-sol"}
    if os.environ.get("OPENAI_API_KEY") is not None:
        models.setProvider(openaiProvider())
    else:
        faux = fauxProvider()
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
    harness = await Harness.open(
        MemoryStorage(), {"models": models, "registry": registry, "env": lambda *_: env}, context
    )
    root = await harness.root(context, {"agent": {"model": model}})

    submission = await root.submit({"type": "input", "content": prompt}, context)
    settled = await submission.wait(context)
    exitCode = 0
    if settled["status"] == "done" and settled["type"] == "input":
        entry = await root.commit(lambda tx: tx.entry(AssistantEntry, settled["answer"]), context)
        answer: dict[str, Any] = entry["model"][0]
        print("".join(content["text"] for content in answer["content"] if content["type"] == "text"))
    else:
        print(f"unanswered: {settled['reason'] if settled['status'] == 'unanswered' else settled['status']}", file=sys.stderr)
        exitCode = 1
    await harness.close(context)
    return exitCode


if __name__ == "__main__":
    sys.exit(asyncio.run(main(*sys.argv[1:2])))
