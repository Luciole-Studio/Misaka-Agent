"""A foreground subagent tool: the parent's tool call creates a child conversation it owns, runs one task there, and
returns the child's answer. Aborting the tool call aborts the child. A UI finds the child through the tool's running
details and shows its events under the call.

Uses OpenAI when OPENAI_API_KEY is set, and a scripted faux model otherwise.

Run from the repository root:
    python -m tests.durable_examples.example_22_subagent_foreground
"""

from __future__ import annotations

import asyncio
import os
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
    configure,
    createRegistry,
    defineExtension,
    defineTool,
    watchEvents,
)
from misaka.utils.js import jsonStringify

context = BACKGROUND_CONTEXT

# ─── Product code: the subagent extension ───────────────────────────────────


async def answerText(api: Any, answer: int, callContext: Any) -> str:
    entry = await api.commit(lambda tx: tx.entry(AssistantEntry, answer), callContext)
    message = entry["model"][0]
    return "".join(c["text"] for c in message["content"] if c["type"] == "text")


async def delegate(args: dict[str, Any], api: Any, callContext: Any) -> dict[str, Any]:
    task = args["task"]

    # The child is owned by this tool call's task, so aborting the call aborts the child, and the call
    # finishes only once the child's work is done.
    async def createChild(tx: Any) -> int:
        # Ownership records the child: a rerun of this call finds it instead of creating another.
        existing = (await tx.scanConversations({"ownerTaskId": api.taskId}, 1))["items"]
        if existing:
            return existing[0]["id"]  # type: ignore[no-any-return]
        # Starts as a copy of this conversation's agent: model, thinking level, cwd, extensions, tools.
        created = await tx.createConversation({"ownership": {"kind": "task", "taskId": api.taskId}})
        # Without this extension, the child is not offered this tool.
        await configure(tx, created["id"], {"extensions": {"remove": [Subagent]}})
        return created["id"]  # type: ignore[no-any-return]

    child = await api.commit(createChild, callContext)
    # A UI watching the parent sees this and can attach to the child.
    await api.details({"conversationId": child}, callContext)

    handle = await api.conversation(child, callContext)
    # The request ID makes a rerun get back the submission it made before the crash.
    request = {"type": "input", "content": task, "requestId": f"subagent:{api.taskId}"}
    settled = await (await handle.submit(request, callContext)).wait(callContext)
    if settled["status"] != "done" or settled["type"] != "input":
        raise RuntimeError(f"Subagent failed: {settled['status']}")
    text = await answerText(api, settled["answer"], callContext)
    return {"output": [{"type": "text", "text": text}], "details": {"conversationId": child}}


Subagent = defineExtension(
    {
        "name": "subagent",
        "tools": [
            defineTool(
                {
                    "name": "subagent",
                    "description": "Delegate a self-contained task to a subagent and get its answer back.",
                    "parameters": {
                        "type": "object",
                        "properties": {"task": {"type": "string", "description": "What the subagent should do"}},
                        "required": ["task"],
                    },
                    # Safe to rerun after a crash: a rerun finds the child it already created and the submission it
                    # already made.
                    "replay": "safe",
                    "execute": delegate,
                }
            )
        ],
    }
)


# ─── UI: the parent's events, with each subagent's events indented under its call ───


def printEvent(indent: str, event: dict[str, Any]) -> None:
    if event["type"] == "message_end" and event["entry"]["kind"] == "pi.assistant":
        message = event["entry"]["model"][0]
        text = "".join(c["text"] for c in message["content"] if c["type"] == "text")
        if text != "":
            print(f"{indent}assistant: {text}")
    elif event["type"] == "tool_execution_start":
        print(f"{indent}tool {event['toolName']}({jsonStringify(event['args'])})")


async def main() -> None:
    # ─── Host setup ─────────────────────────────────────────────────────────

    models = createModels()
    model = {"provider": "openai", "modelId": "gpt-6-sol"}
    if os.environ.get("OPENAI_API_KEY") is not None:
        models.setProvider(openaiProvider())
    else:
        # The parent delegates, the child answers, and the parent reports.
        faux = fauxProvider()
        models.setProvider(faux.provider)
        model = {"provider": "faux", "modelId": "faux-1"}
        delegation = fauxToolCall("subagent", {"task": "Name three prime numbers."}, {"id": "call-1"})
        faux.setResponses(
            [
                fauxAssistantMessage([delegation], {"stopReason": "toolUse"}),
                fauxAssistantMessage([fauxText("2, 3, and 5.")]),
                fauxAssistantMessage([fauxText("The subagent says: 2, 3, and 5.")]),
            ]
        )
    registry = createRegistry()
    registry.install(Subagent)
    harness = await Harness.open(MemoryStorage(), {"models": models, "registry": registry}, context)
    root = await harness.root(context, {"agent": {"model": model}})

    attached: set[int] = set()
    streams: list[Any] = []

    async def attach(id: int, indent: str) -> None:
        attached.add(id)
        stream = await watchEvents(harness, id, context)
        streams.append(stream)

        async def onEvents(events: Any, *_: Any) -> None:
            for event in events:
                printEvent(indent, event)
                if event["type"] != "tool_execution_update":
                    continue
                child = (event.get("details") or {}).get("conversationId")
                if child is not None and child not in attached:
                    await attach(child, f"{indent}  ")

        stream.start(onEvents)

    await attach(root.id, "")

    submission = await root.submit(
        {"type": "input", "content": "Use the subagent tool to find three prime numbers, then tell me what it said."},
        context,
    )
    await submission.wait(context)
    await harness.waitForIdle(context)
    # Event callbacks run after their commit; let the last ones print.
    await asyncio.sleep(0)
    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
