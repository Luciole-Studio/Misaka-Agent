"""A real model: stream an answer from OpenAI.

Run from the repository root (needs OPENAI_API_KEY):
    python -m tests.durable_examples.example_16_real_model
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.openai import openaiProvider
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    AssistantEntry,
    Harness,
    LiveDoc,
    MemoryStorage,
    createRegistry,
    defineExtension,
    section,
)

context = BACKGROUND_CONTEXT


# Production code passes a Models collection with real providers; the Harness
# never talks to a provider any other way. openaiProvider() reads
# OPENAI_API_KEY from the environment. While the answer streams, generation
# commits throttled partials to the conversation's pi.live document. Watching
# that document streams the answer; the watch sees only committed values.
async def main() -> None:
    if os.environ.get("OPENAI_API_KEY") is None:
        print("skipped: OPENAI_API_KEY is not set")
        return
    models = createModels()
    models.setProvider(openaiProvider())
    registry = createRegistry()
    registry.install(
        defineExtension(
            {
                "name": "concise",
                "sections": [section("preamble", lambda *_: "You are a concise assistant.", {"tag": False})],
            }
        )
    )
    harness = await Harness.open(MemoryStorage(), {"models": models, "registry": registry}, context)
    root = await harness.root(
        context, {"agent": {"model": {"provider": "openai", "modelId": "gpt-6-sol"}, "thinkingLevel": "high"}}
    )
    liveWatch = await harness.watchDoc(LiveDoc, root.id, context)
    # Print only what each committed partial adds to the text printed so far.
    printed = {"text": ""}

    def printText(text: str) -> None:
        if len(text) <= len(printed["text"]) or not text.startswith(printed["text"]):
            return
        sys.stdout.write(text[len(printed["text"]) :])
        printed["text"] = text

    async def onFrame(value: Any, ops: Any, callContext: Any) -> None:
        message = ((value or {}).get("generation") or {}).get("message")
        block = next((content for content in (message or {}).get("content", []) if content["type"] == "text"), None)
        if block is not None:
            printText(block["text"])

    liveWatch.start(onFrame)
    harness.resume()
    sys.stdout.write("answer: ")
    poem = await root.submit({"type": "input", "content": "Write a long poem"}, context)
    settledPoem = await poem.wait(context)
    await liveWatch.stop()
    if settledPoem["status"] == "done" and settledPoem["type"] == "input":
        # The last throttle window may not have been committed as a partial; the answer entry has the rest.
        entry = await root.commit(lambda tx: tx.entry(AssistantEntry, settledPoem["answer"]), context)
        message = ((entry or {}).get("model") or [None])[0]
        block = None
        if message is not None and message["role"] == "assistant":
            block = next((content for content in message["content"] if content["type"] == "text"), None)
        if block is not None:
            printText(block["text"])
        sys.stdout.write("\n")
    else:
        print("unanswered:", settledPoem.get("reason"), settledPoem.get("detail"))
    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
