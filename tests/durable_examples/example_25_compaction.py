"""Compaction: a long trip-planning chat whose older messages are summarized so the model context stays small.

Run from the repository root:
    python -m tests.durable_examples.example_25_compaction
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from typing import Any

from misaka.ai.models import createModels
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    CompactionEntry,
    Harness,
    LiveDoc,
    MemoryStorage,
    createRegistry,
)

context = BACKGROUND_CONTEXT


def field(value: Any, name: str) -> Any:
    """A field of a stored message dict or of a request message model."""
    return value[name] if isinstance(value, dict) else getattr(value, name)


def text(message: Any) -> str:
    if message is None or field(message, "role") == "system":
        return ""
    content = field(message, "content")
    if isinstance(content, str):
        return content
    block = next((item for item in content if field(item, "type") == "text"), None)
    return "" if block is None else field(block, "text").replace("\n", " ")


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
    # A fake model with a tiny 3000-token window. It answers chat messages, writes summaries when asked to summarize,
    # and once rejects a request as too long, the way real providers report a context overflow.
    overflowOnce = False
    summaries = 0
    # While set, the next chat answer waits for it, which keeps the conversation busy.
    hold: asyncio.Future[None] | None = None

    async def respond(transcript: Any, *_: Any) -> Any:
        nonlocal overflowOnce, summaries, hold
        first = transcript.messages[0] if transcript.messages else None
        if first is not None and first.role == "system" and isinstance(first.content, str) and "summarization" in first.content:
            summaries += 1
            return fauxAssistantMessage(f"## Goal\nPlan a week in Lisbon (summary #{summaries}).")
        if overflowOnce:
            overflowOnce = False
            return fauxAssistantMessage("", {"stopReason": "error", "errorMessage": "prompt is too long"})
        held = hold
        hold = None
        if held is not None:
            await held
        question = next((message for message in reversed(transcript.messages) if message.role == "user"), None)
        return fauxAssistantMessage(f'A detailed answer to "{text(question)}": {"details " * 200}')

    faux = fauxProvider({"models": [{"id": "tiny", "contextWindow": 3000, "maxTokens": 1000}]})
    faux.setResponses([respond for _ in range(100)])
    models = createModels()
    models.setProvider(faux.provider)

    # Generation blocks to compact above 3000 - 1000 = 2000 tokens and starts a background compaction above
    # 2000 - 800. Settings are read at every use, so the getter makes `backgroundTokens` live.
    backgroundTokens = 800
    settings = Settings(
        {"compaction": lambda: {"reserveTokens": 1000, "keepRecentTokens": 400, "backgroundTokens": backgroundTokens}}
    )
    harness = await Harness.open(
        MemoryStorage(), {"models": models, "registry": createRegistry(), "settings": settings}, context
    )
    root = await harness.root(context, {"agent": {"model": {"provider": "faux", "modelId": "tiny"}}})

    async def show(conversation: Any, label: str) -> None:
        """Print the model context: one line per message, and the number of stored entries behind it."""
        view = await conversation.context(context)
        stored = len((await conversation.entries({}, 1000, None, context))["items"])
        print(f"\n{label}: {len(view['messages'])} messages in context, {stored} entries stored")
        head = view.get("head")
        reason = head["data"]["reason"] if CompactionEntry.is_(head) else None
        if reason is not None:
            print(f"  ({reason} compaction summary first)")
        for message in view["messages"]:
            line = "(system prompt)" if message["role"] == "system" else text(message)[:70]
            print(f"  {message['role'].ljust(9)} {line}")

    async def ask(question: str) -> None:
        submission = await root.submit({"type": "input", "content": question}, context)
        record = await submission.wait(context)
        # Let a background compaction started by this turn finish, so its summary shows below.
        running = ((await harness.snapshot(LiveDoc, root.id, context)) or {}).get("compactions", [])
        for item in running:
            await harness.waitForTask(item["taskId"], context)
        await show(root, f'after "{question}" ({"answered" if record["status"] == "done" else record["reason"]})')

    # 1. A long chat: once the context crosses the background threshold, a compaction runs while the chat goes on, and
    # its summary lands at once when the conversation is idle, otherwise at the next turn boundary.
    for question in [
        "Where should we stay?",
        "What should we eat?",
        "Which day trips?",
        "Any museums?",
        "Nightlife?",
    ]:
        await ask(question)

    # 2. A manual compaction while an answer is still being written. The summary is ready first, waits in the inbox,
    # and is placed right after the answer.
    finishAnswer: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    hold = finishAnswer
    busy = await root.submit({"type": "input", "content": "How do we get around?"}, context)
    manual = await root.compact("Keep the hotel shortlist", context)
    state = (await harness.waitForTask(manual, context))["state"]
    placement: Any = state["outcome"]["result"]["submissionId"] if state["outcome"]["status"] == "completed" else None
    summary = await harness.submission(placement, context)
    print("\nmanual compaction finished; its summary is", (await summary.status(context))["status"])
    finishAnswer.set_result(None)
    await busy.wait(context)
    print("after the answer, the summary is", (await summary.wait(context))["status"])
    await show(root, "after compact()")

    # 3. The provider rejects a request as too long: generation compacts and retries it once. Background compaction is
    # turned off so the summary below is the overflow one.
    backgroundTokens = 0
    await ask("What should we pack?")
    overflowOnce = True
    await ask("Summarize the plan for my partner")

    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
