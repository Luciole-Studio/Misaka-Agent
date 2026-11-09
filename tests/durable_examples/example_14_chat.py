"""A chat turn.

Run from the repository root:
    python -m tests.durable_examples.example_14_chat
"""

from __future__ import annotations

import asyncio

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    AssistantEntry,
    Harness,
    MemoryStorage,
    createRegistry,
    defineExtension,
    section,
)

context = BACKGROUND_CONTEXT


async def main() -> None:
    # The faux provider stands in for a real model (see example_16_real_model.py).
    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)
    faux.setResponses([fauxAssistantMessage("Paris.")])

    registry = createRegistry()
    registry.install(
        defineExtension(
            {"name": "terse", "sections": [section("preamble", lambda *_: "You answer in one word.", {"tag": False})]}
        )
    )
    harness = await Harness.open(MemoryStorage(), {"models": models, "registry": registry}, context)
    root = await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "faux-1"}}})

    # submit() durably admits user input and returns a Submission. The built-in
    # pi.generation task prepares the system prompt from the agent's sections,
    # calls the model, and appends the answer; wait() resolves once the input is
    # answered or has failed.
    capital = await root.submit({"type": "input", "content": "Capital of France?"}, context)
    answered = await capital.wait(context)
    if answered["status"] == "done" and answered["type"] == "input":
        entry = await root.commit(lambda tx: tx.entry(AssistantEntry, answered["answer"]), context)
        reply = (entry.get("model") or [None])[0] if entry is not None else None
        print("answer:", reply["content"] if reply is not None and reply["role"] == "assistant" else reply)

    # The transcript holds the user input, the positional system prompt, and the answer.
    transcript = await root.entries({}, 10, None, context)
    print("transcript:", [entry["kind"] for entry in reversed(transcript["items"])])
    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
