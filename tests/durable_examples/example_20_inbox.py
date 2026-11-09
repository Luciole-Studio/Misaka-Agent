"""The inbox: what happens to submissions while a conversation is busy.

Run from the repository root:
    python -m tests.durable_examples.example_20_inbox
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import Harness, InboxDoc, MemoryStorage, createRegistry

context = BACKGROUND_CONTEXT


async def main() -> None:
    # The first answer waits until we let it go, so the conversation stays busy while we submit more.
    held: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def slow(*_: Any) -> Any:
        await held
        return fauxAssistantMessage("Answer to the first question.")

    faux = fauxProvider()
    faux.setResponses([slow, fauxAssistantMessage("Answer to the follow-up and the steer.")])
    models = createModels()
    models.setProvider(faux.provider)

    # Settings apply to every conversation: place every queued follow-up at once instead of one per run.
    harness = await Harness.open(
        MemoryStorage(),
        {"models": models, "registry": createRegistry(), "settings": {"followUpMode": "all"}},
        context,
    )
    root = await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "faux-1"}}})

    first = await root.submit({"type": "input", "content": "First question"}, context)

    # While busy, input queues as a follow-up (the default) or a steer, and writes queue too.
    followUp = await root.submit({"type": "input", "content": "A follow-up"}, context)
    steer = await root.submit({"type": "input", "content": "A steer", "whenBusy": "steer"}, context)
    note = await root.submit({"type": "write", "entry": {"kind": "app.note", "data": "noted while busy"}}, context)
    withdrawn = await root.submit({"type": "input", "content": "Never mind"}, context)
    # whenBusy: "reject" refuses instead of queueing.
    try:
        await root.submit({"type": "input", "content": "Now or never", "whenBusy": "reject"}, context)
    except Exception as error:  # noqa: BLE001 - the refusal is the point of the example
        print("rejected:", error)
    # A queued submission can be withdrawn until a boundary places it.
    print("withdraw:", await withdrawn.abort(context))

    inbox = await harness.snapshot(InboxDoc, root.id, context)
    print("inbox:", [f"{item['id']} {item['mode']}" for item in inbox["items"]] if inbox is not None else None)

    # The first answer ends the run at a final boundary: the write is placed first, then the steer and the
    # follow-up, which start the next run together.
    held.set_result(None)

    async def status(name: str, submission: Any) -> None:
        record = await submission.wait(context)
        print(f"{name}:", record["status"], record["reason"] if record["status"] == "unanswered" else "")

    await status("first", first)
    await status("follow-up", followUp)
    await status("steer", steer)
    await status("note", note)
    await status("withdrawn", withdrawn)

    page = await root.entries({}, 20, None, context)
    print("transcript:", [entry["kind"] for entry in reversed(page["items"])])
    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
