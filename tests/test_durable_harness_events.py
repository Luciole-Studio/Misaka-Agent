"""pi durable's ``test/harness-events.test.ts``."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import (
    fauxAssistantMessage,
    fauxText,
    fauxThinking,
    fauxToolCall,
)
from misaka.chord.context import withAbortSignal
from misaka.durable import (
    AgentDoc,
    LiveDoc,
    MemoryStorage,
    UsageDoc,
    defineTool,
    watchEvents,
)
from misaka.durable.harness.ai_bridge import toMessageDict
from misaka.utils.js import AbortController
from tests.chat_support import chatSetup, openChat, textOf, waitFor
from tests.durable_support import AnyOf, context, documentChanges, flush, toMatchObject
from tests.harness_support import addTool
from tests.task_support import aborted, deferred, race


async def listen(harness: Any, conversation: Any) -> SimpleNamespace:
    """Attach and start an event stream that records every delivered batch."""
    stream = await watchEvents(harness, conversation.id, context)
    batches: list[list[dict[str, Any]]] = []

    async def onBatch(events: Any, *_: Any) -> None:
        batches.append(list(events))

    stream.start(onBatch)
    return SimpleNamespace(
        stream=stream, batches=batches, events=lambda: [event for batch in batches for event in batch]
    )


async def drained() -> None:
    await flush()


def slow() -> SimpleNamespace:
    return chatSetup({"tokensPerSecond": 400, "tokenSize": {"min": 1, "max": 1}})


def streamedText(events: list[dict[str, Any]]) -> str:
    """Rebuild the streamed text of block 0 from ``message_start`` and the text deltas that follow it."""
    text = ""
    for event in events:
        if event["type"] == "message_start" and event["message"]["role"] == "assistant":
            content = event["message"]["content"]
            block = content[0] if content else None
            text = block["text"] if block is not None and block["type"] == "text" else ""
        if event["type"] != "message_update":
            continue
        for change in event["changes"]:
            if change["type"] == "text_start" and change["contentIndex"] == 0 and change["block"]["type"] == "text":
                text = change["block"]["text"]
            if change["type"] == "text_delta" and change["contentIndex"] == 0:
                text += change["delta"]
    return text


def applyChanges(message: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply message changes to a copy of ``message``, as an events-only consumer would."""
    following = copy.deepcopy(message)
    for change in changes:
        kind = change["type"]
        if kind == "message":
            following = copy.deepcopy(change["message"])
        elif kind == "block":
            following["content"][change["contentIndex"]] = copy.deepcopy(change["block"])
        elif kind.endswith("_start") and "block" in change:
            following["content"].insert(change["contentIndex"], copy.deepcopy(change["block"]))
        elif kind in ("text_delta", "thinking_delta"):
            block = following["content"][change["contentIndex"]]
            field = "text" if kind == "text_delta" else "thinking"
            block[field] = f"{block[field]}{change['delta']}"
        elif kind == "toolcall_delta":
            target = following["content"][change["contentIndex"]]["arguments"]
            for segment in change["path"][:-1]:
                target = target[segment]
            last = change["path"][-1]
            target[last] = f"{target[last]}{change['delta']}"
    return following


def generationMessage(change: dict[str, Any]) -> dict[str, Any] | None:
    value = change["value"]
    return None if value is None else (value.get("generation") or {}).get("message")


def partialsOf(harness: Any) -> list[dict[str, Any]]:
    """Committed partials of a conversation's generation, one per commit that has one."""
    partials: list[dict[str, Any]] = []

    def onCommit(publication: Any, _context: Any) -> None:
        for change in documentChanges(publication):
            message = generationMessage(change)
            if change["record"]["kind"] == "pi.live" and message is not None:
                partials.append(message)

    harness.subscribeCommits(onCommit)
    return partials


def outputOf(events: list[dict[str, Any]]) -> str:
    """The output events rebuild the slot's retained window."""
    text = ""
    for event in events:
        if event["type"] != "tool_execution_update" or event.get("output") is None:
            continue
        output = event["output"]
        if "set" in output:
            text = output["set"]
        else:
            text = text[output.get("trimStart", 0) :] + output.get("append", "")
    return text


async def test_streams_a_run_as_lifecycle_events_and_text_deltas_that_rebuild_the_answer():
    setup = slow()
    text = "streamed answer text " * 20
    setup.faux.setResponses([fauxAssistantMessage([fauxText(text)])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    listened = await listen(harness, root)
    stream, events = listened.stream, listened.events
    toMatchObject(stream.snapshot, {"type": "snapshot", "entries": [], "tools": [], "inbox": []})
    partials: list[str] = []

    def onCommit(publication: Any, _context: Any) -> None:
        for change in documentChanges(publication):
            message = generationMessage(change)
            if change["record"]["kind"] == "pi.live" and message is not None:
                partials.append(textOf(message) or "")

    harness.subscribeCommits(onCommit)
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    await submission.wait(context)
    await drained()
    everything = events()
    allTypes = [event["type"] for event in everything]
    types = [kind for index, kind in enumerate(allTypes) if index == 0 or kind != allTypes[index - 1]]
    assert types == [
        "message_start",
        "message_end",
        "submission",
        "run_start",
        "turn_start",
        "message_start",
        "message_update",
        "message_end",
        "turn_end",
        "run_end",
        "submission",
        "usage_changed",
    ]
    assert next(event for event in everything if event["type"] == "run_start") == {
        "type": "run_start",
        "inputs": [submission.id],
    }
    # After each event, the rebuilt text equals the committed partial of that commit.
    rebuilt = [
        streamedText(everything[: index + 1])
        for index, event in enumerate(everything)
        if (event["type"] == "message_start" and event["message"]["role"] == "assistant")
        or event["type"] == "message_update"
    ]
    assert rebuilt == partials
    assert len(partials) > 1
    end = [event for event in everything if event["type"] == "message_end"][-1]
    assert end["type"] == "message_end"
    assert textOf(end["entry"]["model"][0]) == text
    await stream.stop()
    await harness.close(context)


async def test_reports_tool_start_output_appends_and_the_result_entry():
    setup = chatSetup()
    gate = deferred()

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        api.output("one\n")
        await asyncio.sleep(0.15)
        api.output("two\n")
        await gate.promise
        return {}

    addTool(
        setup.registry,
        defineTool(
            {
                "name": "print",
                "description": "Prints",
                "parameters": {"type": "object", "properties": {"n": {"type": "number"}}, "required": ["n"]},
                "execute": execute,
            }
        ),
    )
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("print", {"n": 1}, {"id": "c1"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    listened = await listen(harness, root)
    stream, events = listened.stream, listened.events
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await waitFor(lambda: outputOf(events()) == "one\ntwo\n")
    gate.resolve()
    await submission.wait(context)
    await drained()
    tool = [event for event in events() if event["type"].startswith("tool_execution")]
    assert tool[0] == {
        "type": "tool_execution_start",
        "toolCallId": "c1",
        "toolName": "print",
        "taskId": AnyOf(int),
        "args": {"n": 1},
    }
    end = tool[-1]
    toMatchObject(end, {"type": "tool_execution_end", "toolCallId": "c1", "entry": {"kind": "pi.tool-result"}})
    # As in the coding agent, the tool ends directly before its result message.
    everything = events()
    endIndex = next(index for index, event in enumerate(everything) if event is end)
    assert [event["type"] for event in everything[endIndex : endIndex + 3]] == [
        "tool_execution_end",
        "message_start",
        "message_end",
    ]
    toMatchObject(everything[endIndex + 1], {"message": {"role": "toolResult", "toolCallId": "c1"}})
    # Two turns: the tool round, and the answer.
    assert len([event for event in events() if event["type"] == "turn_start"]) == 2
    assert len([event for event in events() if event["type"] == "turn_end"]) == 2
    await stream.stop()
    await harness.close(context)


async def test_reports_queued_submissions_inbox_changes_and_retries():
    setup = chatSetup()
    release = deferred()

    async def held(_request: Any, options: Any, *_: Any) -> Any:
        await race(release.promise, aborted(options.signal))
        return fauxAssistantMessage([fauxText("first")])

    error = fauxAssistantMessage([], {"stopReason": "error", "errorMessage": "503 Service Unavailable"})
    setup.faux.setResponses([held, error, fauxAssistantMessage([fauxText("second")])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    setup.settings["retry"] = {"enabled": True, "maxRetries": 1, "baseDelayMs": 1}
    listened = await listen(harness, root)
    stream, events = listened.stream, listened.events
    await root.submit({"type": "input", "content": "a"}, context)
    followUp = await root.submit({"type": "input", "content": "f"}, context)
    await drained()
    assert [event for event in events() if event["type"] == "inbox_update"] == [
        {"type": "inbox_update", "items": [{"id": followUp.id, "mode": "followUp"}]}
    ]
    release.resolve()
    await followUp.wait(context)
    await drained()
    types = [event["type"] for event in events()]
    assert "auto_retry_start" in types
    assert "auto_retry_end" in types
    assert len([kind for kind in types if kind == "run_start"]) == 2
    assert [event["record"]["status"] for event in events() if event["type"] == "submission"] == [
        "placed",
        "queued",
        "done",
        "placed",
        "done",
    ]
    await stream.stop()
    await harness.close(context)


async def test_replaces_undelivered_batches_with_one_snapshot_after_100_pending_batches():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    stream = await watchEvents(harness, root.id, context)
    for _ in range(101):
        await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "note"}), context)
    batches: list[list[dict[str, Any]]] = []

    async def onBatch(events: Any, *_: Any) -> None:
        batches.append(list(events))

    stream.start(onBatch)
    await drained()
    assert len(batches) == 1
    assert len(batches[0]) == 1
    toMatchObject(batches[0][0], {"type": "snapshot"})
    assert len(batches[0][0]["entries"]) == 101
    await stream.stop()
    await harness.close(context)


async def test_rebuilds_every_committed_partial_of_thinking_text_and_tool_call_arguments_from_message_changes():
    setup = chatSetup({"tokensPerSecond": 150, "tokenSize": {"min": 1, "max": 1}})
    message = fauxAssistantMessage(
        [
            fauxThinking("thinking about it " * 10),
            fauxText("some text " * 10),
            fauxToolCall("missing", {"path": "a/long/path/" * 10, "note": "x" * 60}, {"id": "c1"}),
        ], {"stopReason": "toolUse"})
    setup.faux.setResponses([message, fauxAssistantMessage([fauxText("done")])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    partials = partialsOf(harness)
    listened = await listen(harness, root)
    stream, events = listened.stream, listened.events
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    await drained()
    rebuilt: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    # The streamed tool-calling message, up to its end; the short final answer commits no partial.
    for event in events():
        if event["type"] == "message_end" and (event.get("entry") or {}).get("kind") == "pi.assistant":
            break
        if event["type"] == "message_start" and event["message"]["role"] == "assistant":
            current = event["message"]
        elif event["type"] == "message_update":
            assert current is not None
            current = applyChanges(current, event["changes"])
        else:
            continue
        rebuilt.append(current)
    assert len(partials) > 2
    assert [partial["content"] for partial in rebuilt] == [partial["content"] for partial in partials]
    types = {
        change["type"] for event in events() if event["type"] == "message_update" for change in event["changes"]
    }
    assert "thinking_delta" in types or "text_delta" in types
    await stream.stop()
    await harness.close(context)


async def test_rebuilds_a_sliding_tail_window_from_output_trims_and_appends():
    setup = chatSetup()
    gate = deferred()

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        for line in range(6):
            api.output(f"line {line}\n")
            await asyncio.sleep(0.12)
        await gate.promise
        return {}

    addTool(
        setup.registry,
        defineTool(
            {
                "name": "tail",
                "description": "Prints lines",
                "parameters": {"type": "object", "properties": {}},
                "outputLimits": {"maxLines": 3, "retain": "tail"},
                "execute": execute,
            }
        ),
    )
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("tail", {}, {"id": "c1"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    outputs: list[str] = []

    def onCommit(publication: Any, _context: Any) -> None:
        for change in documentChanges(publication):
            value = change["value"]
            tools = None if value is None else value.get("tools")
            output = tools[0].get("output") if tools else None
            if change["record"]["kind"] == "pi.live" and output is not None and output != (outputs[-1] if outputs else None):
                outputs.append(output)

    harness.subscribeCommits(onCommit)
    listened = await listen(harness, root)
    stream, events = listened.stream, listened.events
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await waitFor(lambda: len(outputs) > 0 and outputs[-1].endswith("line 5\n"))
    gate.resolve()
    await submission.wait(context)
    await drained()
    rebuilt: list[str] = []
    text = ""
    for event in events():
        if event["type"] != "tool_execution_update" or event.get("output") is None:
            continue
        output = event["output"]
        if "set" in output:
            text = output["set"]
        else:
            text = text[output.get("trimStart", 0) :] + output.get("append", "")
        rebuilt.append(text)
    assert rebuilt == outputs
    assert any(
        event["type"] == "tool_execution_update" and "trimStart" in (event.get("output") or {}) for event in events()
    )
    await stream.stop()
    await harness.close(context)


async def test_emits_one_exact_batch_when_a_run_ends_and_a_queued_follow_up_starts_the_next():
    setup = chatSetup()
    release = deferred()

    async def held(_request: Any, options: Any, *_: Any) -> Any:
        await race(release.promise, aborted(options.signal))
        return fauxAssistantMessage([fauxText("first")])

    setup.faux.setResponses([held, fauxAssistantMessage([fauxText("second")])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    followUp = await root.submit({"type": "input", "content": "f"}, context)
    listened = await listen(harness, root)
    stream, batches = listened.stream, listened.batches
    release.resolve()
    await followUp.wait(context)
    await drained()
    boundary = next(batch for batch in batches if any(event["type"] == "run_end" for event in batch))
    assert [event["type"] for event in boundary] == [
        "message_start",
        "message_end",
        "message_start",
        "message_end",
        "turn_end",
        "run_end",
        "submission",
        "submission",
        "inbox_update",
        "usage_changed",
        "run_start",
        "turn_start",
    ]
    assert [event["record"]["id"] for event in boundary if event["type"] == "submission"] == [input.id, followUp.id]
    await stream.stop()
    await harness.close(context)


async def test_ends_a_call_that_never_runs_and_a_tool_aborted_with_its_generation():
    setup = chatSetup()

    async def execute(_args: Any, _api: Any, callContext: Any) -> Any:
        return await aborted(callContext.abortSignal)

    addTool(
        setup.registry,
        defineTool(
            {
                "name": "wait",
                "description": "Waits until aborted",
                "parameters": {"type": "object", "properties": {}},
                "execute": execute,
            }
        ),
    )
    setup.faux.setResponses(
        [fauxAssistantMessage([fauxToolCall("ghost", {}, {"id": "c1"}), fauxToolCall("wait", {}, {"id": "c2"})], {"stopReason": "toolUse"})]
    )
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    listened = await listen(harness, root)
    stream, events = listened.stream, listened.events
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await waitFor(lambda: any(event["type"] == "tool_execution_start" for event in events()))
    # Aborting the generation aborts its round: the tool ends with its aborted result first (spec 8.5).
    await harness.abortTask((await harness.snapshot(LiveDoc, root.id, context))["run"]["taskId"], context)
    await submission.wait(context)
    await drained()
    # The call not offered ends after its calling message and directly before its result message.
    rounds = [
        f"end:{event['entry']['kind']}"
        if event["type"] == "message_end"
        else f"start:{event['message']['role']}"
        if event["type"] == "message_start"
        else event["type"]
        for event in events()
    ]
    ghostEnd = rounds.index("tool_execution_end")
    assert rounds[ghostEnd - 1 : ghostEnd + 3] == [
        "end:pi.assistant",
        "tool_execution_end",
        "start:toolResult",
        "end:pi.tool-result",
    ]
    tool = [event for event in events() if event["type"].startswith("tool_execution")]
    assert [[event["type"], event.get("toolCallId", False), "entry" in event] for event in tool] == [
        ["tool_execution_end", "c1", True],
        ["tool_execution_start", "c2", False],
        ["tool_execution_end", "c2", True],
    ]
    await stream.stop()
    await harness.close(context)


async def test_reports_a_steer_without_run_events_a_reset_as_an_appended_entry_and_nothing_for_other_conversations():
    setup = chatSetup()
    gate = deferred()

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await gate.promise
        return {}

    addTool(
        setup.registry,
        defineTool(
            {
                "name": "hold",
                "description": "Waits",
                "parameters": {"type": "object", "properties": {}},
                "execute": execute,
            }
        ),
    )
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("hold", {}, {"id": "c1"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    other = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    listened = await listen(harness, root)
    stream, events, batches = listened.stream, listened.events, listened.batches
    input = await root.submit({"type": "input", "content": "a"}, context)
    await waitFor(lambda: any(event["type"] == "tool_execution_start" for event in events()))
    await root.submit({"type": "input", "content": "s", "whenBusy": "steer"}, context)
    before = len(batches)
    await other.commit(lambda tx: tx.appendEntry(other.id, {"kind": "note"}), context)
    await drained()
    assert len(batches) == before
    gate.resolve()
    await input.wait(context)
    await root.reset(None, context)
    await drained()
    assert len([event for event in events() if event["type"] == "run_start"]) == 1
    assert len([event for event in events() if event["type"] == "run_end"]) == 1
    assert [event["type"] for event in batches[-1]] == ["entry_appended", "submission"]
    await stream.stop()
    await harness.close(context)


async def test_rejects_an_attachment_cancelled_while_it_waits_for_the_session_line():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    release = deferred()

    async def block(tx: Any) -> None:
        await release.promise

    blocking = root.commit(block, context)
    controller = AbortController()
    attaching = watchEvents(harness, root.id, withAbortSignal(controller.signal, context))
    controller.abort(RuntimeError("cancelled"))
    release.resolve()
    await blocking
    with pytest.raises(Exception, match="cancelled"):
        await attaching
    await harness.close(context)


async def test_applies_deltas_after_an_overflow_snapshot_that_holds_an_in_flight_partial():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    partial = fauxAssistantMessage([fauxText("hel")])
    stream = await watchEvents(harness, root.id, context)

    async def setGeneration(tx: Any) -> None:
        (await tx.doc(LiveDoc, root.id))["generation"] = {"attempt": 1, "message": toMessageDict(partial)}

    await root.commit(setGeneration, context)
    for _ in range(101):
        await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "note"}), context)
    batches: list[list[dict[str, Any]]] = []

    async def onBatch(events: Any, *_: Any) -> None:
        batches.append(list(events))

    stream.start(onBatch)
    await drained()
    snapshot = batches[0][0]
    assert snapshot["type"] == "snapshot"

    async def extend(tx: Any) -> None:
        block = (await tx.doc(LiveDoc, root.id))["generation"]["message"]["content"][0]
        block["text"] = block["text"] + "lo"

    await root.commit(extend, context)
    await drained()
    update = batches[-1][0]
    if update["type"] != "message_update":
        raise AssertionError(f"Unexpected {update['type']}")
    assert update["changes"] == [{"type": "text_delta", "contentIndex": 0, "delta": "lo"}]
    rebuilt = applyChanges(snapshot["generation"]["message"], update["changes"])
    assert rebuilt["content"] == [{"type": "text", "text": "hello"}]
    await stream.stop()
    await harness.close(context)


async def test_reports_usage_only_updates_cleared_tool_progress_tools_ending_without_entries_and_deferred_polls():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    listened = await listen(harness, root)
    stream, batches = listened.stream, listened.batches

    def change(edit: Callable[[Any], None]) -> Any:
        async def run(tx: Any) -> None:
            edit(await tx.doc(LiveDoc, root.id))

        return root.commit(run, context)

    partial = toMessageDict(fauxAssistantMessage([fauxText("partial")]))

    def setPartial(live: Any) -> None:
        live["generation"] = {"attempt": 1, "message": partial}

    def setUsage(live: Any) -> None:
        live["generation"]["message"]["usage"]["input"] = 42

    def setDeferred(live: Any) -> None:
        live["generation"] = {"attempt": 1, "deferred": {"pollAt": 1}}

    def setPollAt(live: Any) -> None:
        live["generation"]["deferred"]["pollAt"] = 2

    def setTools(live: Any) -> None:
        live["tools"] = [{"callId": "c1", "name": "t", "status": "running", "details": {"n": 1}, "diagnostics": []}]

    def clearProgress(live: Any) -> None:
        del live["tools"][0]["details"]
        del live["tools"][0]["diagnostics"]

    def markDone(live: Any) -> None:
        live["tools"][0]["status"] = "done"

    await change(setPartial)
    # A usage-only change sends the usage and no changes.
    await change(setUsage)
    await change(setDeferred)
    await change(setPollAt)
    await change(setTools)
    # A safe replay clears the running slot's progress.
    await change(clearProgress)
    # A fault marks the slot done without an entry.
    await change(markDone)
    await root.commit(lambda tx: tx.retireDoc(UsageDoc, root.id), context)
    await root.commit(lambda tx: tx.retireDoc(AgentDoc, root.id), context)
    await drained()
    assert [[event for event in batch if event["type"] != "task_failed"] for batch in batches] == [
        [{"type": "message_start", "message": partial}],
        [{"type": "message_update", "usage": {**partial["usage"], "input": 42}, "changes": []}],
        [{"type": "deferred_poll", "pollAt": 1}],
        [{"type": "deferred_poll", "pollAt": 2}],
        [{"type": "tool_execution_start", "toolCallId": "c1", "toolName": "t", "args": {}}],
        [{"type": "tool_execution_update", "toolCallId": "c1", "toolName": "t", "details": None, "diagnostics": []}],
        [{"type": "tool_execution_end", "toolCallId": "c1", "toolName": "t"}],
        # Retired documents read as their initial values.
        [{"type": "usage_changed", "usage": {"models": {}, "tools": {}}}],
        [{"type": "agent_changed", "agent": {}}],
    ]
    await stream.stop()
    await harness.close(context)


async def test_ends_the_stream_with_the_harness():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    stream = await watchEvents(harness, root.id, context)
    await harness.close(context)
    assert await stream.closed == {"reason": "session_closed"}


async def test_starts_a_message_at_the_first_committed_partial_and_ends_it_with_the_converted_entry_on_abort():
    setup = chatSetup({"tokensPerSecond": 100, "tokenSize": {"min": 1, "max": 1}})
    setup.faux.setResponses([fauxAssistantMessage([fauxText("x" * 400)])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    listened = await listen(harness, root)
    stream, events = listened.stream, listened.events
    submission = await root.submit({"type": "input", "content": "hi"}, context)
    await waitFor(
        lambda: any(event["type"] == "message_start" and event["message"]["role"] == "assistant" for event in events())
    )
    live = await harness.snapshot(LiveDoc, root.id, context)
    await harness.abortTask(live["run"]["taskId"], context)
    await submission.wait(context)
    await drained()
    assistantEnds = [
        event
        for event in events()
        if event["type"] == "message_end" and (event.get("entry") or {}).get("kind") == "pi.assistant"
    ]
    assert len(assistantEnds) == 1
    assert len([event for event in events() if event["type"] == "message_start" and event["message"]["role"] == "assistant"]) == 1
    await stream.stop()
    await harness.close(context)
