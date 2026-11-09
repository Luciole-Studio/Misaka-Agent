"""Transcript history and model context.

Run from the repository root:
    python -m tests.durable_examples.example_09_context
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.ai.models import createModels
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import Harness, MemoryStorage, createRegistry
from misaka.utils.js import jsonStringify

context = BACKGROUND_CONTEXT


# Entries are immutable. `model` holds the messages an entry contributes to
# the next model request; `data` is for the app only. context() turns the
# stored transcript into those request messages:
#   - an entry with `head` starts a new context; older entries stay stored,
#   - `edits` replace or omit what an earlier entry contributes,
#   - aborted, error, and deferred assistant messages are not sent,
#   - tool results are sent right after their call, in call order,
#   - a call without a result gets a synthesized error result.
def assistantMessage(text: str, calls: list[str] | None = None, stopReason: str | None = None) -> dict[str, Any]:
    calls = calls or []
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": text},
            *[{"type": "toolCall", "id": id, "name": "read", "arguments": {}} for id in calls],
        ],
        "api": "example",
        "provider": "example",
        "model": "example",
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": stopReason if stopReason is not None else ("toolUse" if calls else "stop"),
        "timestamp": 2,
    }


def toolResultMessage(id: str) -> dict[str, Any]:
    return {
        "role": "toolResult",
        "toolCallId": id,
        "toolName": "read",
        "content": [{"type": "text", "text": f"file {id}"}],
        "isError": False,
        "timestamp": 3,
    }


def show(message: dict[str, Any]) -> str:
    role = message["role"]
    if role == "user":
        return f"user: {message['content']}"
    if role == "system":
        return f"system: {jsonStringify(message['sections'])}"
    if role == "assistant":
        parts = [
            part["text"] if part["type"] == "text" else f"call({part['id']})" if part["type"] == "toolCall" else ""
            for part in message["content"]
        ]
        return f"assistant: {' '.join(parts)}"
    return f"result({message['toolCallId']}){' error' if message['isError'] else ''}"


async def main() -> None:
    harness = await Harness.open(MemoryStorage(), {"models": createModels(), "registry": createRegistry()}, context)

    transcript = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)

    def say(kind: str, *model: dict[str, Any]) -> Any:
        return transcript.commit(lambda tx: tx.appendEntry(transcript.id, {"kind": kind, "model": list(model)}), context)

    question = await say("message", {"role": "user", "content": "read a and b", "timestamp": 1})
    await say("message", assistantMessage("I crashed", [], "aborted"))  # stored, never sent
    calls = await say("message", assistantMessage("reading", ["a", "b"]))
    await say("message", toolResultMessage("b"))  # results finish out of order
    await say("pi.system", {"role": "system", "content": "", "sections": {"cwd": "<cwd>/repo</cwd>"}, "timestamp": 4})
    await say("message", toolResultMessage("a"))
    await say("message", assistantMessage("a and b look fine"))
    await transcript.commit(
        lambda tx: tx.appendEntry(
            transcript.id,
            {
                "kind": "edit",
                "data": "user fixed a typo",
                "edits": [
                    {
                        "target": question["id"],
                        "action": "replace",
                        "messages": [{"role": "user", "content": "read files a and b", "timestamp": 1}],
                    }
                ],
            },
        ),
        context,
    )
    await transcript.commit(lambda tx: tx.appendEntry(transcript.id, {"kind": "note", "data": "display only"}), context)

    view = await transcript.context(context)
    print("raw active entries:", [entry["kind"] for entry in view["entries"]])
    print("request messages:", [show(message) for message in view["messages"]])

    # A fork at the tool call has no results yet; context() fills them in.
    cut = await transcript.fork(calls["id"], {"ownership": {"kind": "ownerless"}}, context)
    print("fork messages:", [show(message) for message in (await cut.context(context))["messages"]])

    # A headed summary replaces everything before the entry it points at.
    # "self" points the head at the summary entry itself.
    await transcript.commit(
        lambda tx: tx.appendEntry(
            transcript.id,
            {
                "kind": "summary",
                "head": "self",
                "model": [{"role": "user", "content": "Summary: a and b are fine.", "timestamp": 5}],
            },
        ),
        context,
    )
    view = await transcript.context(context)
    print("after summary:", view["head"]["kind"], [show(message) for message in view["messages"]])

    # entries() pages the stored transcript, newest first, including inherited
    # parent entries. Nothing is ever deleted by heads or edits.
    history = await transcript.entries({}, 3, None, context)
    print("newest stored entries:", [entry["kind"] for entry in history["items"]], "more:", history.get("next") is not None)

    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
