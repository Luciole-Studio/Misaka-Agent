"""Persistent background subagents.

One ``subagent`` tool lets the main agent start named subagents, message them (steer or follow up), stop them mid-answer,
and list them. Subagents keep working while the main agent answers the user, and each answer is delivered back to the
main agent as a new message once it arrives. Everything survives a restart: the example closes the Harness while a
subagent works and reopens it.

Uses OpenAI when OPENAI_API_KEY is set, and a scripted faux model otherwise.

Run from the repository root:
    python -m tests.durable_examples.example_23_subagent_background
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
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
    LiveDoc,
    configure,
    createRegistry,
    defineDoc,
    defineExtension,
    defineTask,
    defineTool,
    watchEvents,
)
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.utils.js import jsonStringify

context = BACKGROUND_CONTEXT

# ─── Product code ────────────────────────────────────────────────────────────

# Each subagent is its own conversation with its own transcript, so it remembers earlier messages. The main
# conversation keeps a small document that maps subagent names to their conversations. Documents are durable state
# next to a transcript, changed in commits like entries. An agent is
# ``{"conversationId": ..., "reported": [...]}``; ``reported`` holds the answers already reported to the main agent:
# several messages can end in one answer, reported once.
Subagents = defineDoc(
    {
        "kind": "app.subagents",
        "version": 1,
        "scope": "conversation",
        "history": "latest",
        # A fork of the main conversation starts without subagents.
        "fork": "initial",
        "initial": lambda: {"agents": {}, "reporters": {}},
    }
)


# Every task and conversation has an owner, and that decides what an abort or an idle wait reaches: aborting a task
# aborts what it owns, and waiting for a conversation to be idle waits for its work. A subagent must outlive the main
# agent's turns, so its conversation is owned by an anchor: a background task that finishes at once. A background task
# is a boundary: the main agent's Esc and idle waits stop there, but `abort({ background: true })` still reaches past
# it, so a host can stop everything.
async def anchorDone(_anchor: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    await runtime.commit(
        lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": None}}, taskContext
    )


async def abortTask(_task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, taskContext)


Anchor = defineTask(
    {
        "name": "app.subagent-anchor",
        "version": 1,
        "initial": lambda input: {"phase": "done"},
        "phases": {"done": anchorDone},
        "abort": abortTask,
    }
)


def textOf(message: dict[str, Any] | None) -> str:
    return "".join(c["text"] for c in (message or {}).get("content", []) if c["type"] == "text")


# A reporter delivers one message to a subagent and reports the answer to the main agent. It is a background task
# too, so the main agent's Esc and idle waits leave it alone. Tasks are durable: each phase ends with a saved
# checkpoint, and after a restart the task continues from the last one. Request IDs make a repeated submission return
# the first one, so the subagent gets the message once and the main agent gets the report once.
# The input is ``{"name", "conversationId", "message", "followUp"}``; the state is ``{"phase": "deliver"}`` or
# ``{"phase": "report", "report"?: str}``.
# Send the message, wait for its answer, and decide what to report.
async def deliver(reporter: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    name, conversationId, message, followUp = (
        reporter["input"]["name"],
        reporter["input"]["conversationId"],
        reporter["input"]["message"],
        reporter["input"]["followUp"],
    )
    # While the subagent is busy, a steer reaches it at its next step and a follow-up after its current answer.
    subagent = await runtime.conversation(conversationId, taskContext)
    request = {"type": "input", "content": message, "whenBusy": "followUp" if followUp else "steer"}
    submission = await subagent.submit({**request, "requestId": f"subagent:{reporter['id']}"}, taskContext)
    settled = await submission.wait(taskContext)

    # One commit decides the report and records the answer as delivered, so a restart does not decide again.
    async def decide(tx: Any, current: Any) -> dict[str, Any]:
        def next_(report: str | None = None) -> dict[str, Any]:
            checkpoint: dict[str, Any] = {"phase": "report"}
            if report is not None:
                checkpoint["report"] = report
            return {"status": "running", "checkpoint": checkpoint}

        # `aborted`: stopped, or withdrawn while queued. Nothing to report.
        if settled["status"] == "unanswered":
            return next_(None if settled["reason"] == "aborted" else f"[subagent {name} failed: {settled['reason']}]")
        # Always an answered input here; the check tells the type checker.
        if settled["type"] != "input":
            return next_()
        agent = (await tx.doc(Subagents, runtime.conversationId))["agents"][name]
        if agent["reported"].indexOf(settled["answer"]) >= 0:
            return next_()
        agent["reported"].push(settled["answer"])
        entry = await tx.entry(AssistantEntry, settled["answer"])
        answer = ((entry or {}).get("model") or [None])[0]
        return next_(f"[subagent {name} answered, no reply needed] {textOf(answer)}")

    await runtime.commit(decide, taskContext)


# Post the report as a follow-up input: it starts a turn when the main agent is idle, or waits for its current
# answer. If the user presses Esc while it waits in the main agent's queue, it is dropped with the other input; a
# report that arrives after Esc starts a new turn.
async def report(reporter: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    text = reporter["state"]["checkpoint"].get("report")
    if text is not None:
        main = await runtime.conversation(runtime.conversationId, taskContext)
        request = {"type": "input", "content": text, "whenBusy": "followUp"}
        await main.submit({**request, "requestId": f"subagent-report:{reporter['id']}"}, taskContext)
    await runtime.commit(
        lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": None}}, taskContext
    )


Reporter = defineTask(
    {
        "name": "app.subagent-reporter",
        "version": 1,
        "initial": lambda input: {"phase": "deliver"},
        "phases": {"deliver": deliver, "report": report},
        "abort": abortTask,
    }
)


async def executeSubagent(args: dict[str, Any], api: Any, callContext: Any) -> dict[str, Any]:
    action, name, message, followUp = args["action"], args.get("name"), args.get("message"), args.get("followUp")

    def reply(text: str, conversationId: int | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        # A UI can attach to the subagent's conversation through the call's details.
        if conversationId is not None and name is not None:
            result["details"] = {"name": name, "conversationId": conversationId}
        return result

    registry = await api.snapshot(Subagents, api.conversationId, callContext)
    if registry is None:
        registry = {"agents": {}, "reporters": {}}

    if action == "status":
        names = list(registry["agents"]) if name is None else [name]
        lines: list[str] = []
        for each in names:
            found = registry["agents"].get(each)
            if found is None:
                continue
            # A conversation is busy while it has a run: from an input until its final answer.
            live = await api.snapshot(LiveDoc, found["conversationId"], callContext)
            busy = live is not None and "run" in live
            lines.append(f"{each}: {'working' if busy else 'idle'}")
        return reply("No subagents." if len(lines) == 0 else "\n".join(lines))
    if name is None:
        return reply(f"{action} needs a name.")
    agent = registry["agents"].get(name)
    if action != "spawn" and agent is None:
        return reply(f"No subagent named {name}.")

    if action == "stop":
        # Aborts the subagent's current answer and tools and drops its queued messages. It stays usable.
        await (await api.conversation(agent["conversationId"], callContext)).abort(callContext)
        return reply(f"Stopped {name}.", agent["conversationId"])
    if message is None:
        return reply(f"{action} needs a message.")

    # spawn and send: one commit starts a reporter for the message.
    async def start(tx: Any) -> str:
        state = await tx.doc(Subagents, api.conversationId)
        # Both tasks belong to the main conversation and are background: its Esc and idle waits skip them.
        background = {"ownership": {"kind": "conversation"}, "background": True}
        if action == "spawn":
            if name in state["agents"]:
                return f"{name} already exists; use send."
            anchor = await tx.createTask(Anchor, None, background)
            # Owned by a task of the main conversation: starts as a copy of the main agent.
            child = await tx.createConversation({"ownership": {"kind": "task", "taskId": anchor}})
            # Subagents cannot start subagents, and know who they are.
            await configure(
                tx,
                child["id"],
                {
                    "extensions": {"remove": [SubagentTools]},
                    "instructions": f'You are the subagent "{name}". Answer the main agent\'s requests.',
                },
            )
            state["agents"][name] = {"conversationId": child["id"], "reported": []}
        conversationId = state["agents"][name]["conversationId"]
        reporterInput = {
            "name": name,
            "conversationId": conversationId,
            "message": message,
            "followUp": action == "send" and followUp is True,
        }
        state["reporters"][api.taskId] = await tx.createTask(Reporter, reporterInput, background)
        return f"Sent to {name}." if action == "send" else f"Started {name}."

    result = await api.commit(start, callContext)
    current = ((await api.snapshot(Subagents, api.conversationId, callContext)) or {}).get("agents", {}).get(name)
    return reply(result, None if current is None else current["conversationId"])


subagentTool = defineTool(
    {
        "name": "subagent",
        "description": "Manage persistent subagents that work in the background. Actions: spawn (name, message), "
        "send (name, message; followUp: true queues it after the current answer instead of steering), stop (name: "
        "aborts its current work), status (name, or all subagents without one). Answers are reported back to you "
        "when they arrive.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "anyOf": [
                        {"const": "spawn", "type": "string"},
                        {"const": "send", "type": "string"},
                        {"const": "stop", "type": "string"},
                        {"const": "status", "type": "string"},
                    ]
                },
                "name": {"type": "string"},
                "message": {"type": "string"},
                "followUp": {"type": "boolean"},
            },
            "required": ["action"],
        },
        # A call interrupted by a crash is not rerun: repeating `stop` could stop newer work. The model sees that the
        # call was interrupted and can check with `status`.
        "replay": "unsafe",
        "execute": executeSubagent,
    }
)

# The task definitions come with the extension, so pending reporters resume after a restart once the host installs
# it again.
SubagentTools = defineExtension({"name": "subagent-tools", "tasks": [Anchor, Reporter], "tools": [subagentTool]})

# ─── UI: the main conversation's transcript, as a user would see it ───


def color(code: int) -> Callable[[str], str]:
    return lambda text: f"\x1b[{code}m{text}\x1b[0m" if sys.stdout.isatty() else text


bold = color(1)
dim = color(2)
cyan = color(36)
yellow = color(33)
magenta = color(35)


def contentText(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "".join(part["text"] for part in content if part.get("text") is not None)


async def follow(harness: Any, id: int) -> None:
    """Print the main conversation's messages as they are committed. Subagents work in their own conversations."""
    stream = await watchEvents(harness, id, context)

    async def onEvents(events: Any, *_: Any) -> None:
        for event in events:
            if event["type"] != "message_end":
                continue
            model = event["entry"].get("model")
            message: Any = model[0] if model else None
            role = None if message is None else message["role"]
            if role == "user":
                text = contentText(message["content"])
                # Input is either the user or a subagent's report, which arrives whenever the subagent is done.
                match = re.match(r"^\[subagent (\S+) ([^\]]*)\] ?(.*)$", text, re.DOTALL)
                if match is None:
                    print(f"\n{cyan(bold('>'))} {bold(text)}")
                else:
                    print(f"\n{magenta(bold(f'> {match.group(1)}:'))} {match.group(3) or match.group(2)}")
            elif role == "assistant":
                for part in message["content"]:
                    if part["type"] != "toolCall":
                        continue
                    arguments = part["arguments"]
                    action, name, sent = arguments.get("action"), arguments.get("name"), arguments.get("message")
                    quoted = "" if sent is None else f" {jsonStringify(sent)}"
                    print(yellow(f"  {part['name']} {action}{'' if name is None else f' {name}'}{quoted}"))
                if textOf(message) != "":
                    print(textOf(message))
            elif role == "toolResult":
                print(dim(f"  → {contentText(message['content'])}"))

    stream.start(onEvents)


def messageText(message: Any) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(part.text for part in content if part.type == "text")


async def main() -> None:
    # ─── Host setup ─────────────────────────────────────────────────────────

    models = createModels()
    model = {"provider": "openai", "modelId": "gpt-6-sol"}
    if os.environ.get("OPENAI_API_KEY") is not None:
        models.setProvider(openaiProvider())
    else:
        # The main agent and the subagent share one scripted model, which answers each request by its last message.
        # It streams its answers at 50 tokens per second, like a slow real model.
        faux = fauxProvider({"tokensPerSecond": 50})
        models.setProvider(faux.provider)
        model = {"provider": "faux", "modelId": "faux-1"}

        def call(input: dict[str, str]) -> Any:
            return fauxAssistantMessage([fauxToolCall("subagent", input)], {"stopReason": "toolUse"})

        def answer(text: str) -> Any:
            return fauxAssistantMessage([fauxText(text)])

        def route(request: Any, *_: Any) -> Any:
            # System messages carry prompt changes; the request is about the message before them.
            last = next(message for message in reversed(request.messages) if message.role != "system")
            text = messageText(last)
            # The main agent repeats what the tool said.
            if last.role == "toolResult":
                return answer(f"OK. {text}")
            # The main agent.
            if "Start a subagent" in text:
                return call({"action": "spawn", "name": "reader", "message": "Summarize the plot of Moby Dick."})
            if "whale's name" in text:
                return call({"action": "send", "name": "reader", "message": "What is the whale called?"})
            if "every chapter" in text:
                return call({"action": "send", "name": "reader", "message": "Now go through all chapters in detail."})
            if "Stop reader" in text:
                return call({"action": "stop", "name": "reader"})
            if "my subagents" in text:
                return call({"action": "status"})
            if "[subagent" in text:
                return answer("Noted.")
            # The subagent: short answers, and a long chapter walk-through that is stopped halfway.
            if "Summarize the plot" in text:
                return answer("A whale, a captain, an obsession.")
            if "whale called" in text:
                return answer("Moby Dick.")
            return answer("\n".join(f"Chapter {index + 1}: more whaling." for index in range(135)))

        # More responses than the script needs; each request takes the next one.
        faux.setResponses([route for _ in range(40)])
    registry = createRegistry()
    registry.install(SubagentTools)
    directory = tempfile.mkdtemp(prefix="pi-durable-subagents-")

    async def open() -> tuple[Any, Any]:
        harness = await Harness.open(
            await openNodeSqliteStorage(os.path.join(directory, "session.sqlite")),
            {"models": models, "registry": registry},
            context,
        )
        root = await harness.root(context, {"agent": {"model": model}})
        return harness, root

    harness, root = await open()
    await follow(harness, root.id)

    async def say(text: str) -> None:
        """Say something to the main agent and wait for its answer."""
        await (await root.submit({"type": "input", "content": text}, context)).wait(context)

    async def settle() -> None:
        """Wait until every message to a subagent was answered and reported, and the main agent has reacted."""
        reporters = ((await harness.snapshot(Subagents, root.id, context)) or {}).get("reporters", {})
        for id in list(reporters.values()):
            await harness.waitForTask(id, context)
        await root.waitForIdle(context)
        # Event callbacks run after their commit; let the last ones print. A slow machine may need longer.
        await asyncio.sleep(0.05)

    async def until(check: Callable[[], Any]) -> None:
        """Poll `check` for up to 10 seconds."""
        tries = 0
        while tries < 1000 and not await check():
            await asyncio.sleep(0.01)
            tries += 1

    async def working(name: str) -> bool:
        agent = ((await harness.snapshot(Subagents, root.id, context)) or {}).get("agents", {}).get(name)
        if agent is None:
            return False
        live = await harness.snapshot(LiveDoc, agent["conversationId"], context)
        return live is not None and "run" in live

    # The main agent answers at once; the subagent's answer is reported back when it arrives.
    await say("Start a subagent named reader that summarizes Moby Dick.")
    await settle()

    # A long request, stopped while the subagent is still answering.
    await say("Ask reader to summarize every chapter.")
    await until(lambda: working("reader"))
    await say("Stop reader.")
    await settle()

    await say("What are my subagents doing?")
    await settle()

    # The process stops while the subagent works on a message; after the restart its answer still arrives.
    async def reporterCount() -> int:
        return len(((await harness.snapshot(Subagents, root.id, context)) or {}).get("reporters", {}))

    before = await reporterCount()
    await root.submit({"type": "input", "content": "Ask reader for the whale's name."}, context)

    async def reporterStarted() -> bool:
        return await reporterCount() > before

    await until(reporterStarted)
    await harness.close(context)
    print(dim("\n  (process restarts)"))
    harness, root = await open()
    await follow(harness, root.id)
    await settle()

    await harness.close(context)
    shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
