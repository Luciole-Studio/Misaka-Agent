"""pi durable's ``test/harness-inbox.test.ts``."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import (
    GenerationTask,
    InboxDoc,
    LiveDoc,
    MemoryStorage,
    ResetEntry,
    ToolTask,
    UsageDoc,
    defineDoc,
    defineTool,
)
from misaka.durable.harness.usage import recordUsage
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from tests.chat_support import (
    allEntries,
    chatSetup,
    openChat,
    requestText,
    textOf,
    waitFor,
)
from tests.durable_support import (
    AnyOf,
    ControlledStorage,
    context,
    documentChanges,
    flush,
    toMatchObject,
)
from tests.harness_support import addHooks, addTool
from tests.task_support import aborted, deferred, race

EMPTY_OBJECT = {"type": "object", "properties": {}}


@pytest.fixture
def sqliteDirectory() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-inbox-")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def answer(text: str) -> Any:
    return fauxAssistantMessage([fauxText(text)])


def gated(message: Any) -> SimpleNamespace:
    """A faux response held until ``release`` or cancellation; ``reached`` resolves when the request is sent."""
    reached = deferred()
    gate = deferred()

    async def step(_request: Any, options: Any, *_: Any) -> Any:
        reached.resolve()
        await race(gate.promise, aborted(options.signal))
        return message

    return SimpleNamespace(step=step, reached=reached.promise, release=lambda: gate.resolve())


def holdTool(setup: Any, gate: Any, result: dict[str, Any] | None = None) -> None:
    """Register a ``hold`` tool whose calls wait for ``gate`` and then return ``result``."""

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await gate.promise
        return result if result is not None else {"output": []}

    addTool(
        setup.registry,
        defineTool(
            {"name": "hold", "description": "Waits for the test", "parameters": EMPTY_OBJECT, "execute": execute}
        ),
    )


def queuedNote(text: str) -> dict[str, Any]:
    """A write that affects model context, so it queues while busy; a data-only write would be appended at once."""
    return {"kind": "note", "model": [{"role": "user", "content": text, "timestamp": 0}]}


HOLD = fauxAssistantMessage([fauxToolCall("hold", {}, {"id": "c1"})], {"stopReason": "toolUse"})


def status(submission: Any) -> Any:
    return submission.status(context)


def transcript(entries: list[dict[str, Any]]) -> list[str]:
    """Kind and text of each entry, skipping system entries."""
    result = []
    for entry in entries:
        if entry["kind"] == "pi.system":
            continue
        model = entry.get("model")
        message = model[0] if model else None
        text = None if message is not None and message["role"] == "toolResult" else textOf(message)
        result.append(entry["kind"] if text is None else f"{entry['kind']}:{text}")
    return result


async def inbox(harness: Any, root: Any) -> list[list[Any]]:
    return [[item["id"], item["mode"]] for item in (await harness.snapshot(InboxDoc, root.id, context))["items"]]


async def toolRunning(harness: Any, root: Any) -> None:
    async def running() -> bool:
        live = await harness.snapshot(LiveDoc, root.id, context)
        tools = None if live is None else live.get("tools")
        return tools is not None and len(tools) > 0 and tools[0]["status"] == "running"  # type: ignore[no-any-return]

    await waitFor(running)


def assistantMessages(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry["model"][0] for entry in entries if entry["kind"] == "pi.assistant"]


async def test_inbox_queues_busy_submissions_and_places_writes_before_user_items_at_the_final_boundary_one_follow_up_per_run():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("second"), answer("third")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    f1 = await root.submit({"type": "input", "content": "f1"}, context)
    write = await root.submit({"type": "write", "entry": queuedNote("w")}, context)
    f2 = await root.submit({"type": "input", "content": "f2", "whenBusy": "followUp"}, context)
    for submission in (f1, write, f2):
        assert (await status(submission))["status"] == "queued"
    assert await inbox(harness, root) == [[f1.id, "followUp"], [write.id, "write"], [f2.id, "followUp"]]
    assert transcript(await allEntries(root)) == ["pi.user:a"]

    first.release()
    await f2.wait(context)
    assert transcript(await allEntries(root)) == [
        "pi.user:a",
        "pi.assistant:first",
        "note:w",
        "pi.user:f1",
        "pi.assistant:second",
        "pi.user:f2",
        "pi.assistant:third",
    ]
    answers = [entry for entry in await allEntries(root) if entry["kind"] == "pi.assistant"]
    toMatchObject(await status(input), {"status": "done", "answer": answers[0]["id"]})
    toMatchObject(await status(write), {"status": "done"})
    toMatchObject(await status(f1), {"status": "done", "answer": answers[1]["id"]})
    toMatchObject(await status(f2), {"status": "done", "answer": answers[2]["id"]})
    assert await inbox(harness, root) == []
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    await harness.close(context)


async def test_inbox_places_every_follow_up_in_one_successor_run_with_follow_up_mode_all():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("both")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    setup.settings["followUpMode"] = "all"
    await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    f1 = await root.submit({"type": "input", "content": "f1"}, context)
    f2 = await root.submit({"type": "input", "content": "f2"}, context)
    first.release()
    settled = await f2.wait(context)
    assert await status(f1) == {**settled, "id": f1.id, "entry": AnyOf(int)}
    assert transcript(await allEntries(root)) == [
        "pi.user:a",
        "pi.assistant:first",
        "pi.user:f1",
        "pi.user:f2",
        "pi.assistant:both",
    ]
    assert setup.faux.state.callCount == 2
    await harness.close(context)


async def test_inbox_reads_queue_modes_when_the_final_boundarys_commit_runs_on_the_session_line():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("both")])
    yielded = deferred()

    def onYield(*_: Any) -> None:
        yielded.resolve()

    addHooks(setup.registry, GenerationTask, {"onYield": onYield})
    storage = ControlledStorage()
    opened = await openChat(storage, setup)
    harness, root = opened.harness, opened.root
    await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    f1 = await root.submit({"type": "input", "content": "f1"}, context)
    f2 = await root.submit({"type": "input", "content": "f2"}, context)
    # Occupy the line, let the answer queue its boundary commit behind it, then change the mode.
    held = storage.holdCommits()
    Marker = defineDoc({"kind": "test.marker", "version": 1, "scope": "session", "initial": lambda: {"n": 0}})

    async def occupy(tx: Any) -> None:
        marker = await tx.doc(Marker)
        marker["n"] = marker["n"] + 1

    occupying = root.commit(occupy, context)
    await held.entered
    first.release()
    await yielded.promise
    await flush()
    setup.settings["followUpMode"] = "all"
    held.release()
    await occupying
    settled = await f2.wait(context)
    assert await status(f1) == {**settled, "id": f1.id, "entry": AnyOf(int)}
    assert setup.faux.state.callCount == 2
    await harness.close(context)


async def test_inbox_adds_steers_to_the_run_at_the_post_tools_boundary_and_holds_follow_ups_for_the_final_boundary():
    setup = chatSetup()
    gate = deferred()
    holdTool(setup, gate)
    setup.faux.setResponses([HOLD, answer("after tools"), answer("follow-up")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await toolRunning(harness, root)
    steer = await root.submit({"type": "input", "content": "s", "whenBusy": "steer"}, context)
    followUp = await root.submit({"type": "input", "content": "f"}, context)
    gate.resolve()
    await followUp.wait(context)
    assert transcript(await allEntries(root)) == [
        "pi.user:a",
        "pi.assistant",
        "pi.tool-result",
        "pi.user:s",
        "pi.assistant:after tools",
        "pi.user:f",
        "pi.assistant:follow-up",
    ]
    answers = [entry for entry in await allEntries(root) if entry["kind"] == "pi.assistant"]
    toMatchObject(await status(input), {"status": "done", "answer": answers[1]["id"]})
    toMatchObject(await status(steer), {"status": "done", "answer": answers[1]["id"]})
    toMatchObject(await status(followUp), {"status": "done", "answer": answers[2]["id"]})
    await harness.close(context)


async def test_inbox_ends_the_run_at_a_queued_reset_after_tools_and_runs_earlier_follow_ups_in_the_new_context():
    setup = chatSetup()
    gate = deferred()
    holdTool(setup, gate)
    requests: list[list[str]] = []

    def record(request: Any, *_: Any) -> Any:
        requests.append([f"{message.role}:{requestText(message) or ''}" for message in request.messages])
        return answer("fresh")

    setup.faux.setResponses([HOLD, record])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await toolRunning(harness, root)
    followUp = await root.submit({"type": "input", "content": "f"}, context)
    await root.reset(None, context)
    gate.resolve()
    await followUp.wait(context)
    toMatchObject(await status(input), {"status": "unanswered", "reason": "reset"})
    assert transcript(await allEntries(root)) == [
        "pi.user:a",
        "pi.assistant",
        "pi.tool-result",
        "pi.reset",
        "pi.user:f",
        "pi.assistant:fresh",
    ]
    reset = next(entry for entry in await allEntries(root) if ResetEntry.is_(entry))
    assert reset["head"] == reset["id"]
    # The follow-up's request starts at the reset: the complete system baseline after the cut leads the follow-up.
    assert requests == [["system:", "user:f"]]
    assert setup.faux.state.callCount == 2
    await harness.close(context)


async def test_inbox_places_a_queued_reset_after_the_answer_at_the_final_boundary():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    await root.reset("handoff", context)
    first.release()
    await input.wait(context)
    await harness.waitForIdle(context)
    toMatchObject(await status(input), {"status": "done"})
    assert transcript(await allEntries(root)) == ["pi.user:a", "pi.assistant:first", "pi.reset:handoff"]
    assert (await root.context(context))["messages"] == [{"role": "user", "content": "handoff", "timestamp": AnyOf(int)}]
    await harness.close(context)


async def test_inbox_resets_an_idle_conversation_at_once_with_or_without_handoff_text():
    setup = chatSetup()
    setup.now = lambda: 7
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "note"}), context)
    await root.reset(None, context)
    view = await root.context(context)
    assert view["head"]["kind"] == "pi.reset"
    assert view["head"].get("model") is None
    assert view["messages"] == []
    await root.reset("carry on", context)
    view = await root.context(context)
    assert view["head"]["head"] == view["head"]["id"]
    assert view["messages"] == [{"role": "user", "content": "carry on", "timestamp": 7}]
    await harness.close(context)


async def test_inbox_makes_a_queued_head_write_stale_when_it_targets_an_entry_before_the_active_range():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("second")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    old = await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "note"}), context)
    await root.reset(None, context)
    reset = (await root.context(context))["head"]
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    stale = await root.submit({"type": "write", "entry": {"kind": "summary", "head": old["id"]}}, context)
    fresh = await root.submit({"type": "write", "entry": {"kind": "summary", "head": reset["id"]}}, context)
    first.release()
    await input.wait(context)
    toMatchObject(await status(stale), {"status": "unanswered", "reason": "stale"})
    toMatchObject(await status(fresh), {"status": "done"})
    assert await inbox(harness, root) == []

    # The fresh summary's marker starts the range at the reset: a target inside the range is not stale, even when it is
    # older than the marker itself. A reset placed earlier in the same boundary makes it stale.
    inside = next(entry for entry in await allEntries(root) if entry["kind"] == "pi.user")
    second = await root.submit({"type": "input", "content": "b"}, context)
    kept = await root.submit({"type": "write", "entry": {"kind": "summary", "head": inside["id"]}}, context)
    await second.wait(context)
    toMatchObject(await status(kept), {"status": "done"})
    await harness.close(context)


async def test_inbox_makes_a_head_write_stale_behind_a_reset_placed_earlier_in_the_same_boundary():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    target = (await allEntries(root))[0]
    await root.reset(None, context)
    summary = await root.submit({"type": "write", "entry": {"kind": "summary", "head": target["id"]}}, context)
    first.release()
    await input.wait(context)
    toMatchObject(await status(summary), {"status": "unanswered", "reason": "stale"})
    await harness.close(context)


async def test_inbox_ends_the_run_with_a_pi_reset_entry_when_a_tool_requests_a_handoff():
    setup = chatSetup()
    gate = deferred()
    gate.resolve()
    holdTool(setup, gate, {"output": [], "control": {"handoff": "continue here"}})
    setup.faux.setResponses([HOLD])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    settled = await (await root.submit({"type": "input", "content": "a"}, context)).wait(context)
    entries = await allEntries(root)
    calling = next(entry for entry in entries if entry["kind"] == "pi.assistant")
    toMatchObject(settled, {"status": "done", "answer": calling["id"]})
    assert transcript(entries) == ["pi.user:a", "pi.assistant", "pi.tool-result", "pi.reset:continue here"]
    assert entries[-1]["head"] == entries[-1]["id"]
    assert setup.faux.state.callCount == 1
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    await harness.close(context)


async def test_inbox_drops_an_on_yield_continuation_when_the_final_boundary_selects_a_follow_up():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("second")])
    yields = 0

    def onYield(*_: Any) -> Any:
        nonlocal yields
        yields += 1
        return {"continue": "more"} if yields == 1 else None

    addHooks(setup.registry, GenerationTask, {"onYield": onYield})
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    followUp = await root.submit({"type": "input", "content": "f"}, context)
    first.release()
    await followUp.wait(context)
    toMatchObject(await status(input), {"status": "done"})
    assert transcript(await allEntries(root)) == [
        "pi.user:a",
        "pi.assistant:first",
        "pi.user:f",
        "pi.assistant:second",
    ]
    await harness.close(context)


async def test_inbox_withdraws_a_queued_submission_and_removes_its_item():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    kept = await root.submit({"type": "write", "entry": queuedNote("kept")}, context)
    withdrawn = await root.submit({"type": "input", "content": "f"}, context)
    assert await withdrawn.abort(context) == "aborted"
    toMatchObject(await withdrawn.wait(context), {"status": "unanswered", "reason": "aborted"})
    assert await inbox(harness, root) == [[kept.id, "write"]]
    first.release()
    await input.wait(context)
    toMatchObject(await status(kept), {"status": "done"})
    assert setup.faux.state.callCount == 1
    await harness.close(context)


async def test_inbox_leaves_queued_items_after_a_failed_run_until_the_next_submission_places_them_in_order():
    setup = chatSetup()
    failing = gated(fauxAssistantMessage([], {"stopReason": "error", "errorMessage": "invalid request"}))
    setup.faux.setResponses([failing.step, answer("for f"), answer("for g")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await failing.reached
    f = await root.submit({"type": "input", "content": "f"}, context)
    failing.release()
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "model_error"})
    await harness.waitForIdle(context)
    assert (await status(f))["status"] == "queued"
    assert await inbox(harness, root) == [[f.id, "followUp"]]

    # Idle with a queued item: the new input queues behind it, and a final boundary places the older one first.
    g = await root.submit({"type": "input", "content": "g", "whenBusy": "reject"}, context)
    await g.wait(context)
    toMatchObject(await status(f), {"status": "done"})
    assert transcript(await allEntries(root))[-4:] == [
        "pi.user:f",
        "pi.assistant:for f",
        "pi.user:g",
        "pi.assistant:for g",
    ]
    await harness.close(context)


async def test_inbox_keeps_queued_submissions_across_reopen_and_settles_them_afterwards(sqliteDirectory):
    path = os.path.join(sqliteDirectory, "session.sqlite")
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("first again"), answer("f")])
    opened = await openChat(await openNodeSqliteStorage(path), setup)
    await opened.root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    f = (await opened.root.submit({"type": "input", "content": "f"}, context)).id
    await opened.harness.close(context)

    opened = await openChat(await openNodeSqliteStorage(path), setup)
    settled = await (await opened.harness.submission(f, context)).wait(context)
    toMatchObject(settled, {"status": "done"})
    assert transcript(await allEntries(opened.root))[-2:] == ["pi.user:f", "pi.assistant:f"]
    await opened.harness.close(context)


async def test_inbox_commits_inbox_changes_as_positional_chord_operations_and_a_base_when_empty():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("second")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    ops: list[list[Any]] = []

    def onCommit(publication: Any, _context: Any) -> None:
        for change in documentChanges(publication):
            if change["record"]["kind"] == "pi.inbox" and len(change["ops"]) > 0:
                ops.append(list(change["ops"]))

    harness.subscribeCommits(onCommit)
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    w1 = await root.submit({"type": "write", "entry": queuedNote("w")}, context)
    f1 = await root.submit({"type": "input", "content": "f1"}, context)
    w2 = await root.submit({"type": "write", "entry": queuedNote("w")}, context)
    f2 = await root.submit({"type": "input", "content": "f2"}, context)
    w3 = await root.submit({"type": "write", "entry": queuedNote("w")}, context)
    assert ops == [
        [["p", ["items"], 0, 0, [{"id": w1.id, "mode": "write", "entry": queuedNote("w")}]]],
        [["p", ["items"], 1, 0, [{"id": f1.id, "mode": "followUp", "content": "f1"}]]],
        [["p", ["items"], 2, 0, [{"id": w2.id, "mode": "write", "entry": queuedNote("w")}]]],
        [["p", ["items"], 3, 0, [{"id": f2.id, "mode": "followUp", "content": "f2"}]]],
        [["p", ["items"], 4, 0, [{"id": w3.id, "mode": "write", "entry": queuedNote("w")}]]],
    ]
    first.release()
    await input.wait(context)
    # Every write and the first follow-up leave; only f2 at index 3 remains. No retained value is carried.
    assert all(op[0] == "p" and len(op[4]) == 0 for op in ops[5])
    assert ["p", ["items"], 4, 1, []] in ops[5]
    assert "f2" not in json.dumps(ops[5])
    await f2.wait(context)
    await harness.close(context)


async def test_inbox_settles_a_stale_write_at_once_while_idle_and_a_queued_one_behind_waiting_items():
    setup = chatSetup()
    failing = gated(fauxAssistantMessage([], {"stopReason": "error", "errorMessage": "invalid request"}))
    setup.faux.setResponses([failing.step, answer("for f")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    old = await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "note"}), context)
    await root.reset(None, context)
    before = len(await allEntries(root))
    idle = await root.submit({"type": "write", "entry": {"kind": "summary", "head": old["id"]}}, context)
    toMatchObject(await idle.wait(context), {"status": "unanswered", "reason": "stale"})
    assert len(await allEntries(root)) == before

    # After a failed run, a follow-up waits; a stale write queues behind it and the boundary rejects it.
    input = await root.submit({"type": "input", "content": "a"}, context)
    await failing.reached
    f = await root.submit({"type": "input", "content": "f"}, context)
    failing.release()
    await input.wait(context)
    await harness.waitForIdle(context)
    queued = await root.submit({"type": "write", "entry": {"kind": "summary", "head": old["id"]}}, context)
    toMatchObject(await queued.wait(context), {"status": "unanswered", "reason": "stale"})
    toMatchObject(await f.wait(context), {"status": "done"})
    await harness.close(context)


async def test_inbox_keeps_an_on_yield_continuation_across_a_queued_plain_write_with_the_runs_original_input():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("second")])
    yields = 0

    def onYield(*_: Any) -> Any:
        nonlocal yields
        yields += 1
        return {"continue": "more"} if yields == 1 else None

    addHooks(setup.registry, GenerationTask, {"onYield": onYield})
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    write = await root.submit({"type": "write", "entry": queuedNote("w")}, context)
    first.release()
    await input.wait(context)
    entries = await allEntries(root)
    assert transcript(entries) == [
        "pi.user:a",
        "pi.assistant:first",
        "note:w",
        "pi.user:more",
        "pi.assistant:second",
    ]
    toMatchObject(await status(input), {"status": "done", "answer": entries[-1]["id"]})
    toMatchObject(await status(write), {"status": "done"})
    await harness.close(context)


async def test_inbox_drops_an_on_yield_continuation_for_a_queued_reset():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step])
    addHooks(setup.registry, GenerationTask, {"onYield": lambda *_: {"continue": "more"}})
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    await root.reset(None, context)
    first.release()
    await input.wait(context)
    await harness.waitForIdle(context)
    toMatchObject(await status(input), {"status": "done"})
    assert transcript(await allEntries(root)) == ["pi.user:a", "pi.assistant:first", "pi.reset"]
    assert setup.faux.state.callCount == 1
    await harness.close(context)


async def test_inbox_adds_every_steer_to_the_run_at_the_post_tools_boundary_with_steering_mode_all():
    setup = chatSetup()
    gate = deferred()
    holdTool(setup, gate)
    setup.faux.setResponses([HOLD, answer("after tools"), answer("follow-up")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    setup.settings["steeringMode"] = "all"
    input = await root.submit({"type": "input", "content": "a"}, context)
    await toolRunning(harness, root)
    s1 = await root.submit({"type": "input", "content": "s1", "whenBusy": "steer"}, context)
    f = await root.submit({"type": "input", "content": "f"}, context)
    s2 = await root.submit({"type": "input", "content": "s2", "whenBusy": "steer"}, context)
    gate.resolve()
    await f.wait(context)
    entries = await allEntries(root)
    assert transcript(entries) == [
        "pi.user:a",
        "pi.assistant",
        "pi.tool-result",
        "pi.user:s1",
        "pi.user:s2",
        "pi.assistant:after tools",
        "pi.user:f",
        "pi.assistant:follow-up",
    ]
    answers = [entry for entry in entries if entry["kind"] == "pi.assistant"]
    for submission in (input, s1, s2):
        toMatchObject(await status(submission), {"status": "done", "answer": answers[1]["id"]})
    await harness.close(context)


async def test_inbox_queues_an_idle_steer_behind_waiting_items_and_places_it_with_the_first_follow_up_in_id_order():
    setup = chatSetup()
    failing = gated(fauxAssistantMessage([], {"stopReason": "error", "errorMessage": "invalid request"}))
    setup.faux.setResponses([failing.step, answer("both")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await failing.reached
    f = await root.submit({"type": "input", "content": "f"}, context)
    failing.release()
    await input.wait(context)
    await harness.waitForIdle(context)
    steer = await root.submit({"type": "input", "content": "s", "whenBusy": "steer"}, context)
    settled = await steer.wait(context)
    toMatchObject(await status(f), {"status": "done", "answer": settled["status"] == "done" and settled["answer"]})
    assert transcript(await allEntries(root))[-3:] == ["pi.user:f", "pi.user:s", "pi.assistant:both"]
    await harness.close(context)


async def test_inbox_starts_a_queued_follow_up_after_a_terminating_round():
    setup = chatSetup()
    gate = deferred()
    holdTool(setup, gate, {"output": [], "control": {"terminate": True}})
    setup.faux.setResponses([HOLD, answer("follow-up")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await toolRunning(harness, root)
    f = await root.submit({"type": "input", "content": "f"}, context)
    gate.resolve()
    await f.wait(context)
    calling = next(entry for entry in await allEntries(root) if entry["kind"] == "pi.assistant")
    toMatchObject(await status(input), {"status": "done", "answer": calling["id"]})
    assert transcript(await allEntries(root)) == [
        "pi.user:a",
        "pi.assistant",
        "pi.tool-result",
        "pi.user:f",
        "pi.assistant:follow-up",
    ]
    await harness.close(context)


async def test_inbox_writes_the_last_handoff_in_call_order_and_then_runs_queued_follow_ups_in_the_new_context():
    setup = chatSetup()
    firstGate = deferred()
    secondGate = deferred()
    holdTool(setup, firstGate, {"output": [], "control": {"handoff": "one"}})

    async def later(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await secondGate.promise
        return {"output": [], "control": {"handoff": "two"}}

    addTool(
        setup.registry,
        defineTool({"name": "later", "description": "Finishes first", "parameters": EMPTY_OBJECT, "execute": later}),
    )
    round = fauxAssistantMessage(
        [fauxToolCall("hold", {}, {"id": "c1"}), fauxToolCall("later", {}, {"id": "c2"})], {"stopReason": "toolUse"})
    setup.faux.setResponses([round, answer("follow-up")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await toolRunning(harness, root)
    f = await root.submit({"type": "input", "content": "f"}, context)
    secondGate.resolve()

    async def secondDone() -> bool:
        live = await harness.snapshot(LiveDoc, root.id, context)
        tools = None if live is None else live.get("tools")
        return tools is not None and len(tools) > 1 and tools[1]["status"] == "done"

    await waitFor(secondDone)
    firstGate.resolve()
    await f.wait(context)
    toMatchObject(await status(input), {"status": "done"})
    assert transcript(await allEntries(root))[-4:] == [
        "pi.tool-result",
        "pi.reset:two",
        "pi.user:f",
        "pi.assistant:follow-up",
    ]
    await harness.close(context)


async def test_inbox_leaves_the_inbox_alone_when_the_runs_task_is_aborted():
    setup = chatSetup()
    first = gated(answer("never"))
    setup.faux.setResponses([first.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    f = await root.submit({"type": "input", "content": "f"}, context)
    taskId = (await harness.snapshot(LiveDoc, root.id, context))["run"]["taskId"]
    await harness.abortTask(taskId, context)
    toMatchObject(await input.wait(context), {"status": "unanswered", "reason": "aborted"})
    await harness.waitForIdle(context)
    assert (await status(f))["status"] == "queued"
    assert await inbox(harness, root) == [[f.id, "followUp"]]
    await harness.close(context)


async def test_inbox_returns_a_queued_submission_for_its_repeated_request_id_without_a_second_item():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    queued = await root.submit({"type": "input", "content": "f", "requestId": "r"}, context)
    again = await root.submit({"type": "input", "content": "f", "requestId": "r"}, context)
    assert again.id == queued.id
    assert await inbox(harness, root) == [[queued.id, "followUp"]]
    await harness.close(context)


async def test_inbox_withdraws_a_middle_item_with_one_positional_removal():
    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    items = []
    for text in ("x", "y", "z"):
        items.append(await root.submit({"type": "input", "content": text}, context))
    ops: list[list[Any]] = []

    def onCommit(publication: Any, _context: Any) -> None:
        for change in documentChanges(publication):
            if change["record"]["kind"] == "pi.inbox" and len(change["ops"]) > 0:
                ops.append(list(change["ops"]))

    harness.subscribeCommits(onCommit)
    await items[1].abort(context)
    assert ops == [[["p", ["items"], 1, 1, []]]]
    assert await inbox(harness, root) == [[items[0].id, "followUp"], [items[2].id, "followUp"]]
    await harness.close(context)


async def test_inbox_stores_the_inbox_as_a_base_exactly_when_it_becomes_empty():
    written: list[dict[str, Any]] = []
    inboxId: int | None = None

    class RecordingStorage(MemoryStorage):
        async def commit(self, writes: Any, callContext: Any) -> int:  # type: ignore[override]
            for write in writes:
                if write["type"] != "document.change" or write["id"] != inboxId:
                    continue
                content = write["content"]
                empty = content["kind"] == "base" and len(content["value"]["items"]) == 0
                written.append({"kind": content["kind"], "empty": empty})
            return await super().commit(writes, callContext)

    setup = chatSetup()
    first = gated(answer("first"))
    setup.faux.setResponses([first.step, answer("second")])
    opened = await openChat(RecordingStorage(), setup)
    harness, root = opened.harness, opened.root

    def onCommit(publication: Any, _context: Any) -> None:
        nonlocal inboxId
        for change in documentChanges(publication):
            if change["record"]["kind"] == "pi.inbox":
                inboxId = change["record"]["id"]

    harness.subscribeCommits(onCommit)
    input = await root.submit({"type": "input", "content": "a"}, context)
    await first.reached
    await root.submit({"type": "input", "content": "f1"}, context)
    f2 = await root.submit({"type": "input", "content": "f2"}, context)
    first.release()
    await input.wait(context)
    await f2.wait(context)
    # The inbox ID is learned from the f1 push; later writes: the f2 push, removing f1, and emptying the inbox.
    assert written == [
        {"kind": "delta", "empty": False},
        {"kind": "delta", "empty": False},
        {"kind": "base", "empty": True},
    ]
    await harness.close(context)


def test_inbox_keeps_a_complete_inbox_base_exactly_while_it_is_empty_and_a_usage_base_on_every_change():
    info = {"deltasSinceBase": 1000}
    item = {"id": 1, "mode": "followUp", "content": "x"}
    assert InboxDoc["definition"]["checkpointWhen"]({"items": []}, [], info) is True
    assert InboxDoc["definition"]["checkpointWhen"]({"items": [item]}, [], info) is False
    assert UsageDoc["definition"]["checkpointWhen"]({"models": {}, "tools": {}}, [], info) is True


async def test_usage_totals_assistant_usage_per_model_and_tool_usage_per_tool_and_sums_the_session():
    setup = chatSetup()
    spent = {
        "input": 1,
        "output": 2,
        "cacheRead": 3,
        "cacheWrite": 4,
        "totalTokens": 10,
        "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0.3, "cacheWrite": 0.4, "total": 1},
    }
    gate = deferred()
    gate.resolve()
    holdTool(setup, gate, {"output": [], "usage": spent})
    setup.faux.setResponses([HOLD, answer("done"), answer("other")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await (await root.submit({"type": "input", "content": "a"}, context)).wait(context)
    entries = await allEntries(root)
    assistants = assistantMessages(entries)
    expected = {
        "input": sum(message["usage"]["input"] for message in assistants),
        "output": sum(message["usage"]["output"] for message in assistants),
        "totalTokens": sum(message["usage"]["totalTokens"] for message in assistants),
    }
    usage = await harness.snapshot(UsageDoc, root.id, context)
    toMatchObject(usage["models"]["faux/faux-1"], expected)
    assert usage["tools"] == {"hold": spent}
    result = next(entry for entry in entries if entry["kind"] == "pi.tool-result")["model"][0]
    assert result["usage"] == spent

    # A fork starts at zero; the Session total adds every conversation once.
    fork = await root.fork(entries[-1]["id"], {"ownership": {"kind": "ownerless"}}, context)
    assert await harness.snapshot(UsageDoc, fork.id, context) == {"models": {}, "tools": {}}
    await (await fork.submit({"type": "input", "content": "b"}, context)).wait(context)
    forkUsage = (await harness.snapshot(UsageDoc, fork.id, context))["models"]["faux/faux-1"]
    total = await harness.usage(context)
    assert total["models"]["faux/faux-1"]["output"] == expected["output"] + forkUsage["output"]
    assert total["tools"] == {"hold": spent}
    await harness.close(context)


async def test_usage_counts_failed_attempts_converted_partials_and_tool_usage_replaced_by_after_tool():
    setup = chatSetup({"tokensPerSecond": 200, "tokenSize": {"min": 1, "max": 1}})
    spent = {
        "input": 5,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 5,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }
    gate = deferred()
    gate.resolve()
    holdTool(setup, gate)
    addHooks(setup.registry, ToolTask, {"afterTool": lambda _call, result, *_: {**result, "usage": spent}})
    retryable = fauxAssistantMessage([], {"stopReason": "error", "errorMessage": "503 Service Unavailable"})
    setup.faux.setResponses([retryable, HOLD, answer("done"), answer("x" * 400)])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    setup.settings["retry"] = {"enabled": True, "maxRetries": 1, "baseDelayMs": 1}
    await (await root.submit({"type": "input", "content": "a"}, context)).wait(context)
    assert (await harness.snapshot(UsageDoc, root.id, context))["tools"] == {"hold": spent}

    # A partial committed while streaming becomes an aborted entry on abort; its usage counts too.
    input = await root.submit({"type": "input", "content": "b"}, context)

    async def streaming() -> bool:
        live = await harness.snapshot(LiveDoc, root.id, context)
        return live is not None and (live.get("generation") or {}).get("message") is not None

    await waitFor(streaming)
    await harness.abortTask((await harness.snapshot(LiveDoc, root.id, context))["run"]["taskId"], context)
    await input.wait(context)
    assistants = assistantMessages(await allEntries(root))
    assert [message["stopReason"] for message in assistants] == ["error", "toolUse", "stop", "aborted"]
    output = sum(message["usage"]["output"] for message in assistants)
    assert (await harness.snapshot(UsageDoc, root.id, context))["models"]["faux/faux-1"]["output"] == output
    await harness.close(context)


async def test_usage_keeps_tools_named_like_object_prototype_keys_in_the_ledger_and_the_session_total():
    opened = await openChat(MemoryStorage(), chatSetup())
    harness, root = opened.harness, opened.root
    usage = {
        "input": 1,
        "output": 1,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 2,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }
    names = ["constructor", "__proto__", "toString"]
    for name in names:
        await root.commit(lambda tx, name=name: recordUsage(tx, root.id, "tools", name, usage), context)  # type: ignore[misc]
        await root.commit(lambda tx, name=name: recordUsage(tx, root.id, "tools", name, usage), context)  # type: ignore[misc]
    total = await harness.usage(context)
    assert list(total["tools"].keys()) == names
    for name in names:
        assert total["tools"][name]["output"] == 2
    await harness.close(context)


async def test_usage_keeps_usage_totals_exact_across_reopen_counting_a_partial_converted_after_reopen_once(
    sqliteDirectory,
):
    path = os.path.join(sqliteDirectory, "session.sqlite")
    setup = chatSetup({"tokensPerSecond": 200, "tokenSize": {"min": 1, "max": 1}})
    setup.faux.setResponses([answer("first"), answer("x" * 400), answer("again")])
    opened = await openChat(await openNodeSqliteStorage(path), setup)
    await (await opened.root.submit({"type": "input", "content": "a"}, context)).wait(context)
    await opened.root.submit({"type": "input", "content": "b"}, context)
    live = opened.harness

    async def streaming() -> bool:
        snapshot = await live.snapshot(LiveDoc, opened.root.id, context)
        return snapshot is not None and (snapshot.get("generation") or {}).get("message") is not None

    await waitFor(streaming)
    # Closing mid-stream keeps the committed partial; the reopened request converts it into an aborted entry.
    await opened.harness.close(context)
    opened = await openChat(await openNodeSqliteStorage(path), setup)
    opened.harness.resume()
    await opened.harness.waitForIdle(context)
    assistants = assistantMessages(await allEntries(opened.root))
    assert [message["stopReason"] for message in assistants] == ["stop", "aborted", "stop"]
    total = (await opened.harness.usage(context))["models"]["faux/faux-1"]

    def sumOf(field: str) -> int:
        return sum(message["usage"][field] for message in assistants)

    assert [total["input"], total["output"], total["totalTokens"]] == [
        sumOf("input"),
        sumOf("output"),
        sumOf("totalTokens"),
    ]
    await opened.harness.close(context)


async def test_usage_records_usage_as_numeric_sets_on_the_ledger_in_the_entrys_commit():
    setup = chatSetup()
    setup.faux.setResponses([answer("one"), answer("two")])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    commits: list[dict[str, Any]] = []

    def onCommit(publication: Any, _context: Any) -> None:
        for change in documentChanges(publication):
            if change["record"]["kind"] != "pi.usage":
                continue
            entries = len([other for other in publication["changes"] if other["type"] == "entry"])
            commits.append({"entries": entries, "ops": change["ops"]})

    harness.subscribeCommits(onCommit)
    await (await root.submit({"type": "input", "content": "a"}, context)).wait(context)
    await (await root.submit({"type": "input", "content": "b"}, context)).wait(context)
    assert len(commits) == 2
    assert commits[0]["ops"] == [["s", ["models", "faux/faux-1"], AnyOf(dict)]]
    assert all(op[0] == "s" and op[1][1] == "faux/faux-1" for op in commits[1]["ops"])
    assert all(commit["entries"] >= 1 for commit in commits)
    await harness.close(context)
