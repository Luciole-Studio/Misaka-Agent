"""pi durable's ``test/harness-context.test.ts``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.models import createModels
from misaka.durable import Harness, MemoryStorage, createRegistry, defineTask
from misaka.utils.js import eager
from tests.durable_support import context
from tests.harness_support import (
    addTask,
    assistant,
    describeMessage,
    openHarness,
    system,
    toolResult,
    user,
)


async def setup() -> SimpleNamespace:
    opened = await openHarness(MemoryStorage())
    harness = opened.harness
    root = await harness.root(context)

    def append(draft: dict[str, Any]) -> Any:
        return root.commit(lambda tx: tx.appendEntry(root.id, draft), context)

    def message(model: dict[str, Any], kind: str = "message") -> Any:
        return append({"kind": kind, "model": [model]})

    return SimpleNamespace(harness=harness, root=root, append=append, message=message)


class CountingStorage(MemoryStorage):
    """MemoryStorage that counts scanned entry rows. When ``hold`` is set, the next range scan waits for it; bounds
    probes (one row, on the Session line) do not."""

    def __init__(self) -> None:
        super().__init__()
        self.rows = 0
        self.hold: asyncio.Future[None] | None = None

    @eager
    async def scanEntries(self, query: dict[str, Any], limit: int, cursor: Any, context: Any) -> Any:
        held = self.hold if limit > 1 else None
        if held is not None:
            self.hold = None
            await held
        page = await super().scanEntries(query, limit, cursor, context)
        self.rows += len(page["items"])
        return page


async def countingSetup(options: dict[str, Any] | None = None) -> SimpleNamespace:
    """A Harness over a row-counting MemoryStorage with a 21-entry root transcript."""
    storage = CountingStorage()
    registry = createRegistry()
    harness = await Harness.open(storage, {"models": createModels(), "registry": registry, **(options or {})}, context)
    root = await harness.root(context)
    first = await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "message", "model": [user("first")]}), context)
    for index in range(20):
        await root.commit(
            lambda tx, index=index: tx.appendEntry(root.id, {"kind": "message", "model": [assistant(f"old {index}")]}),
            context,
        )
    return SimpleNamespace(harness=harness, registry=registry, root=root, first=first, storage=storage)


def ids(entries: list[dict[str, Any]]) -> list[int]:
    return [entry["id"] for entry in entries]


async def test_returns_the_whole_transcript_without_a_head_and_excludes_model_less_entries_from_messages():
    s = await setup()
    first = await s.message(user("hi"))
    note = await s.append({"kind": "note", "data": {"text": "display only"}})
    answer = await s.message(assistant("hello"))
    view = await s.root.context(context)
    assert view["head"] is None
    assert ids(view["entries"]) == [first["id"], note["id"], answer["id"]]
    assert [describeMessage(message) for message in view["messages"]] == ["user:hi", "assistant:hello"]


async def test_excludes_aborted_error_and_deferred_assistant_messages_but_keeps_their_raw_entries():
    s = await setup()
    await s.message(user("q"))
    aborted = await s.message(assistant("partial", stopReason="aborted"))
    await s.message(assistant("failed", stopReason="error"))
    await s.message(assistant("later", stopReason="deferred"))
    await s.message(assistant("done", stopReason="length"))
    view = await s.root.context(context)
    assert len(view["entries"]) == 5
    assert view["entries"][1]["id"] == aborted["id"]
    assert [describeMessage(message) for message in view["messages"]] == ["user:q", "assistant:done"]


async def test_resolves_self_heads_and_uses_the_newest_head_marker():
    s = await setup()
    await s.message(user("old"))
    reset = await s.append({"kind": "reset", "head": "self", "model": [user("fresh start")]})
    assert reset["head"] == reset["id"]
    after = await s.message(assistant("after reset"))
    view = await s.root.context(context)
    assert view["head"]["id"] == reset["id"]
    assert ids(view["entries"]) == [reset["id"], after["id"]]
    assert [describeMessage(message) for message in view["messages"]] == ["user:fresh start", "assistant:after reset"]

    # A compaction summary heads an earlier kept entry; older head markers in range drop out.
    summary = await s.append({"kind": "summary", "head": after["id"], "model": [user("summary")]})
    tail = await s.message(user("next"))
    view = await s.root.context(context)
    assert view["head"]["id"] == summary["id"]
    assert ids(view["entries"]) == [summary["id"], after["id"], tail["id"]]
    assert [describeMessage(message) for message in view["messages"]] == [
        "user:summary",
        "assistant:after reset",
        "user:next",
    ]


async def test_applies_the_newest_edit_per_target_within_the_active_range():
    s = await setup()
    first = await s.message(user("first"))
    second = await s.message(user("second"))
    await s.append({"kind": "edit", "edits": [{"target": first["id"], "action": "replace", "messages": [user("first v2")]}]})
    await s.append({"kind": "edit", "edits": [{"target": first["id"], "action": "replace", "messages": [user("first v3")]}]})
    await s.append({"kind": "edit", "edits": [{"target": second["id"], "action": "omit"}]})
    view = await s.root.context(context)
    assert len(view["entries"]) == 5
    assert [describeMessage(message) for message in view["messages"]] == ["user:first v3"]

    # Edits before the active range no longer apply.
    reset = await s.append({"kind": "reset", "head": second["id"]})
    view = await s.root.context(context)
    assert view["head"]["id"] == reset["id"]
    assert [describeMessage(message) for message in view["messages"]] == []
    await s.append(
        {"kind": "edit", "edits": [{"target": second["id"], "action": "replace", "messages": [user("second v2")]}]}
    )
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == ["user:second v2"]


async def test_keeps_positional_system_messages_and_orders_tool_results_by_call_order():
    s = await setup()
    await s.message(system({"preamble": "You help."}), "pi.system")
    await s.message(user("run tools"))
    await s.message(assistant("calling", calls=["b", "a"]))
    await s.message(toolResult("a"))
    await s.append({"kind": "pi.system", "model": [system({"cwd": "/repo"})]})
    await s.message(toolResult("b"))
    await s.message(toolResult("zz"))
    await s.message(assistant("done"))
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "system:preamble",
        "user:run tools",
        "assistant:calling",
        "result:b:result b",
        "result:a:result a",
        "system:cwd",
        "assistant:done",
    ]


# #10542
async def test_leads_with_a_system_message_that_only_user_messages_precede_and_keeps_later_ones_in_place():
    s = await setup()
    await s.message(user("first"))
    await s.message(user("steered"))
    await s.message(system({"preamble": "You help."}), "pi.system")
    await s.message(assistant("answer"))
    await s.message(user("next"))
    await s.message(system({"cwd": "/repo"}), "pi.system")
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "system:preamble",
        "user:first",
        "user:steered",
        "assistant:answer",
        "user:next",
        "system:cwd",
    ]
    # Stored order and contributions stay as committed.
    assert [describeMessage(message) for contributed in view["contributions"] for message in contributed][:3] == [
        "user:first",
        "user:steered",
        "system:preamble",
    ]

    # After a reset, the baseline written after the handoff leads too.
    await s.append({"kind": "reset", "head": "self", "model": [user("handoff")]})
    await s.message(system({"preamble": "Baseline."}), "pi.system")
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == ["system:preamble", "user:handoff"]


# #10904
async def test_leads_with_the_baseline_after_a_compaction_that_keeps_assistant_messages():
    s = await setup()
    await s.message(user("old"))
    kept = await s.message(user("kept"))
    await s.message(assistant("calling", calls=["a"]))
    await s.message(toolResult("a"))
    await s.message(assistant("answer"))
    await s.append({"kind": "summary", "head": kept["id"], "model": [user("summary")]})
    await s.message(user("next"))
    await s.message(system({"preamble": "Baseline."}), "pi.system")
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "system:preamble",
        "user:summary",
        "user:kept",
        "assistant:calling",
        "result:a:result a",
        "assistant:answer",
        "user:next",
    ]

    # Later system messages stay in place.
    await s.message(assistant("done"))
    await s.message(system({"cwd": "/repo"}), "pi.system")
    view = await s.root.context(context)
    described = [describeMessage(message) for message in view["messages"]]
    assert described[:2] == ["system:preamble", "user:summary"]
    assert described[-2:] == ["assistant:done", "system:cwd"]


# #10904
async def test_leads_with_a_baseline_written_after_the_first_answers_when_the_agent_had_no_section_or_tool_before():
    s = await setup()
    await s.message(user("first"), "pi.user")
    await s.message(assistant("answer"), "pi.assistant")
    await s.message(user("next"), "pi.user")
    await s.message(system({"plan": "Plan."}), "pi.system")
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "system:plan",
        "user:first",
        "assistant:answer",
        "user:next",
    ]


# #10904
async def test_keeps_system_messages_other_than_the_baseline_in_place_once_an_assistant_message_precedes_them():
    s = await setup()
    await s.message(user("first"))
    await s.message(assistant("answer"))
    # Another kind's system message is positional; the baseline after it is not the first system message.
    await s.message(system({"note": "From here on."}), "host.note")
    await s.message(system({"preamble": "Baseline."}), "pi.system")
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "user:first",
        "assistant:answer",
        "system:note",
        "system:preamble",
    ]

    # After a compaction that keeps the other kind's system message, the new baseline does not move ahead of it.
    kept = await s.message(user("kept"))
    await s.message(assistant("kept answer"))
    await s.message(system({"mode": "plan"}), "host.note")
    await s.append({"kind": "summary", "head": kept["id"], "model": [user("summary")]})
    await s.message(user("next"))
    await s.message(system({"preamble": "Baseline."}), "pi.system")
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "user:summary",
        "user:kept",
        "assistant:kept answer",
        "system:mode",
        "user:next",
        "system:preamble",
    ]


# #10904
async def test_does_not_lead_with_a_first_system_entry_written_after_another_system_message_even_once_that_is_omitted():
    s = await setup()
    host = await s.message(system({"legacy": "Use the legacy format."}), "host.system")
    await s.message(user("first"))
    await s.message(assistant("answer"))
    # Generation wrote this entry against the host's message: it only removes a section, so it is no baseline.
    await s.message(system({"legacy": None}), "pi.system")
    await s.append({"kind": "edit", "edits": [{"target": host["id"], "action": "omit"}]})
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "user:first",
        "assistant:answer",
        "system:legacy",
    ]

    # The same holds for a system message an edit put before it.
    other = await setup()
    first = await other.message(user("first"))
    replaced = {"target": first["id"], "action": "replace", "messages": [user("first"), system({"note": "Note."})]}
    await other.append({"kind": "edit", "edits": [replaced]})
    await other.message(assistant("answer"))
    await other.message(system({"note": None}), "pi.system")
    await other.append({"kind": "edit", "edits": [{"target": first["id"], "action": "replace", "messages": [user("first")]}]})
    view = await other.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "user:first",
        "assistant:answer",
        "system:note",
    ]


# #10904
async def test_does_not_lead_with_a_retained_edited_or_later_system_entry_in_place_of_the_baseline():
    s = await setup()
    kept = await s.message(user("kept"))
    await s.message(assistant("answer"))
    retained = await s.message(system({"cwd": "/repo"}), "pi.system")
    # Before generation writes the baseline after the head marker, a retained system entry stays in place.
    await s.append({"kind": "summary", "head": kept["id"], "model": [user("summary")]})
    await s.message(user("next"))
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "user:summary",
        "user:kept",
        "assistant:answer",
        "system:cwd",
        "user:next",
    ]

    # The baseline omits the retained entry, as generation writes it, and leads.
    baseline = await s.append(
        {
            "kind": "pi.system",
            "model": [system({"preamble": "Baseline."})],
            "edits": [{"target": retained["id"], "action": "omit"}],
        }
    )
    await s.message(assistant("done"))
    await s.message(system({"preamble": None}), "pi.system")
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "system:preamble",
        "user:summary",
        "user:kept",
        "assistant:answer",
        "user:next",
        "assistant:done",
        "system:preamble",
    ]

    # An omitted baseline leaves no message to lead; the delta after it is not a baseline and stays in place.
    await s.append({"kind": "edit", "edits": [{"target": baseline["id"], "action": "omit"}]})
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "user:summary",
        "user:kept",
        "assistant:answer",
        "user:next",
        "assistant:done",
        "system:preamble",
    ]

    # A replacement of the baseline is not one either.
    await s.append(
        {"kind": "edit", "edits": [{"target": baseline["id"], "action": "replace", "messages": [system({"other": "Other."})]}]}
    )
    view = await s.root.context(context)
    assert [describeMessage(message) for message in view["messages"]] == [
        "user:summary",
        "user:kept",
        "assistant:answer",
        "user:next",
        "system:other",
        "assistant:done",
        "system:preamble",
    ]


async def test_synthesizes_missing_tool_results_after_a_fork_and_drops_results_cut_from_their_call():
    s = await setup()
    await s.message(user("go"))
    call = await s.message(assistant("calling", calls=["x", "y"]))
    await s.message(toolResult("x"))
    second = await s.message(toolResult("y"))
    child = await s.root.fork(call["id"], {"ownership": {"kind": "ownerless"}}, context)
    childView = await child.context(context)
    assert [describeMessage(message) for message in childView["messages"]] == [
        "user:go",
        "assistant:calling",
        "result:x:error",
        "result:y:error",
    ]
    missing = childView["messages"][2]
    assert missing["role"] == "toolResult"
    assert missing["toolName"] == "tool-x"
    assert missing["details"] == {"reason": "missing_result"}

    # A head between a call and its results leaves stray results that are not sent.
    await s.root.commit(lambda tx: tx.appendEntry(s.root.id, {"kind": "reset", "head": second["id"]}), context)
    parentView = await s.root.context(context)
    assert [describeMessage(message) for message in parentView["messages"]] == []
    assert [entry["kind"] for entry in parentView["entries"]] == ["reset", "message"]


# #10512
async def test_reads_the_context_as_of_an_earlier_entry_as_a_fork_at_that_entry_starts():
    s = await setup()
    first = await s.message(user("first"))
    call = await s.message(assistant("calling", calls=["x", "y"]))
    result = await s.message(toolResult("x"))
    await s.message(toolResult("y"))
    edit = await s.append(
        {"kind": "edit", "edits": [{"target": first["id"], "action": "replace", "messages": [user("first v2")]}]}
    )
    reset = await s.append({"kind": "reset", "head": "self", "model": [user("fresh start")]})
    tail = await s.message(assistant("after reset"))

    for at in [first, call, result, edit, reset, tail]:
        fork = await s.root.fork(at["id"], {"ownership": {"kind": "ownerless"}}, context)
        assert await s.root.context(context, {"at": at["id"]}) == await fork.context(context)
    # Stepped back before the edit and the reset: neither applies, and a cut call gets synthesized results.
    assert [describeMessage(message) for message in (await s.root.context(context, {"at": call["id"]}))["messages"]] == [
        "user:first",
        "assistant:calling",
        "result:x:error",
        "result:y:error",
    ]
    assert await s.root.context(context, {"at": tail["id"]}) == await s.root.context(context)
    assert await s.root.context(context, {}) == await s.root.context(context)

    other = await s.root.fork(first["id"], {"ownership": {"kind": "ownerless"}}, context)
    with pytest.raises(Exception, match="is not visible"):
        await other.context(context, {"at": tail["id"]})


DONE = {"status": "terminal", "outcome": {"status": "completed", "result": None}}

ABORTED = {"status": "terminal", "outcome": {"status": "aborted"}}


async def abortTask(_task: Any, runtime: Any, taskContext: Any) -> None:
    await runtime.commit(lambda tx, current: ABORTED, taskContext)


def oneRun(name: str, run: Callable[..., Any]) -> dict[str, Any]:
    return defineTask(
        {"name": name, "version": 1, "initial": lambda input: {"phase": "run"}, "phases": {"run": run}, "abort": abortTask}
    )


def probeTask(read: Callable[..., Any]) -> dict[str, Any]:
    """A task that reads its conversation's context once, named by its input."""

    async def run(task: Any, runtime: Any, taskContext: Any) -> None:
        await read(task["input"]["name"], lambda: runtime.context(runtime.conversationId, taskContext), taskContext)
        await runtime.commit(lambda tx, current: DONE, taskContext)

    return oneRun("test.context-probe", run)


def conversationTask(root: Any) -> dict[str, Any]:
    return {"ownership": {"kind": "conversation"}, "conversationId": root.id}


async def test_extends_a_tasks_context_read_with_only_newer_entries():
    s = await countingSetup()
    root, storage = s.root, s.storage

    async def run(_task: Any, runtime: Any, taskContext: Any) -> None:
        async def write(draft: dict[str, Any]) -> None:
            async def change(tx: Any, _current: Any) -> None:
                await tx.appendEntry(root.id, draft)

            await runtime.commit(change, taskContext)

        async def read(at: int | None = None) -> SimpleNamespace:
            before = storage.rows
            view = await runtime.context(root.id, taskContext, {"at": at})
            return SimpleNamespace(view=view, rows=storage.rows - before)

        initial = await read()
        assert initial.view == await root.context(taskContext)

        # Three new entries: the bounds probe reads one row, the extension the three new ones.
        await write({"kind": "message", "model": [user("new")]})
        await write({"kind": "note", "data": {"text": "display only"}})
        await write({"kind": "message", "model": [assistant("answer")]})
        extended = await read()
        assert extended.rows == 4
        assert extended.view == await root.context(taskContext)

        # A newer edit of an older entry applies to the extended range.
        await write({"kind": "edit", "edits": [{"target": s.first["id"], "action": "omit"}]})
        edited = await read()
        assert edited.rows == 2
        assert edited.view == await root.context(taskContext)
        assert "user:first" not in [describeMessage(message) for message in edited.view["messages"]]

        # An earlier cutoff reuses the range.
        cutoff = await read(extended.view["entries"][-1]["id"])
        assert cutoff.rows == 0
        assert cutoff.view == extended.view

        # A new head marker changes the range: read it whole.
        await write({"kind": "reset", "head": "self", "model": [user("fresh")]})
        reset = await read()
        assert reset.view == await root.context(taskContext)
        assert [describeMessage(message) for message in reset.view["messages"]] == ["user:fresh"]

        await runtime.commit(lambda tx, current: DONE, taskContext)

    Reads = oneRun("test.context-reads", run)
    addTask(s.registry, Reads)
    id = await s.harness.commit(lambda tx: tx.createTask(Reads, {}, conversationTask(root)), context)
    s.harness.resume()
    settled = await s.harness.waitForTask(id, context)
    assert settled["state"]["outcome"] == {"status": "completed", "result": None}
    await s.harness.close(context)


async def test_keeps_a_conversations_context_read_across_its_tasks_and_for_the_retention_period_once_idle():
    clock = {"now": 1_000}
    s = await countingSetup({"now": lambda: clock["now"]})
    harness, root, storage = s.harness, s.root, s.storage
    rows: dict[str, int] = {}

    async def read(name: str, load: Callable[[], Any], taskContext: Any) -> None:
        before = storage.rows
        view = await load()
        rows[name] = storage.rows - before
        assert view == await root.context(taskContext)

    async def childRun(_task: Any, runtime: Any, taskContext: Any) -> None:
        async def change(tx: Any, _current: Any) -> None:
            await tx.appendEntry(root.id, {"kind": "message", "model": [user("from child")]})

        await runtime.commit(change, taskContext)
        await read("child", lambda: runtime.context(root.id, taskContext), taskContext)
        await runtime.commit(lambda tx, current: DONE, taskContext)

    Child = oneRun("test.context-child", childRun)

    async def start(_task: Any, runtime: Any, taskContext: Any) -> None:
        await read("parent", lambda: runtime.context(root.id, taskContext), taskContext)

        async def change(tx: Any, _current: Any) -> dict[str, Any]:
            child = await tx.createTask(
                Child, {}, {"ownership": {"kind": "task", "taskId": runtime.taskId}, "conversationId": root.id}
            )
            return {"status": "waiting", "checkpoint": {"phase": "after"}, "on": [child], "policy": "allSettled"}

        await runtime.commit(change, taskContext)

    async def after(_task: Any, runtime: Any, taskContext: Any) -> None:
        await read("after", lambda: runtime.context(root.id, taskContext), taskContext)
        await runtime.commit(lambda tx, current: DONE, taskContext)

    Parent = defineTask(
        {
            "name": "test.context-parent",
            "version": 1,
            "initial": lambda input: {"phase": "start"},
            "phases": {"start": start, "after": after},
            "abort": abortTask,
        }
    )
    Probe = probeTask(read)
    for task in [Child, Parent, Probe]:
        addTask(s.registry, task)
    harness.resume()
    parent = await harness.commit(lambda tx: tx.createTask(Parent, {}, conversationTask(root)), context)
    assert (await harness.waitForTask(parent, context))["state"]["outcome"] == DONE["outcome"]
    await root.waitForIdle(context)

    async def probe(name: str) -> None:
        id = await harness.commit(lambda tx: tx.createTask(Probe, {"name": name}, conversationTask(root)), context)
        assert (await harness.waitForTask(id, context))["state"]["outcome"] == DONE["outcome"]
        await root.waitForIdle(context)

    await probe("idle")
    clock["now"] += 600_000
    await probe("expired")
    # Bounds probes read one row each. The child scans its own new entry; the waiting parent's later invocation and
    # the probe within the retention period scan nothing new. After it, the probe reads the whole transcript again.
    assert rows == {"parent": 22, "child": 2, "after": 1, "idle": 1, "expired": 23}
    await harness.close(context)


async def test_derives_the_same_view_from_extended_reads_as_from_whole_reads():
    s = await countingSetup()
    root = s.root
    # Deterministic pseudo-random transcript (32-bit LCG, high bits): calls and results in any order, missing and
    # stray results, excluded stop reasons, system entries, notes, edits of earlier entries, and head markers.
    state = {"seed": 7}

    def random(n: int) -> int:
        state["seed"] = (state["seed"] * 1_664_525 + 1_013_904_223) & 0xFFFFFFFF
        return int((state["seed"] / 2**32) * n)

    seen: dict[str, int] = {}

    def count(category: str) -> None:
        seen[category] = seen.get(category, 0) + 1

    async def run(_task: Any, runtime: Any, taskContext: Any) -> None:
        written: list[dict[str, Any]] = []
        calls: list[str] = []
        counter = {"next": 0}

        def nextId() -> int:
            value = counter["next"]
            counter["next"] += 1
            return value

        def draft() -> dict[str, Any]:
            pick = random(14)
            if pick < 3:
                count("user")
                return {"kind": "message", "model": [user(f"u{nextId()}")]}
            if pick < 5:
                ids = [f"c{nextId()}" for _ in range(random(3))]
                calls.extend(ids)
                index = random(9)
                stopReason = ["aborted", "error", "deferred"][index] if index < 3 else None
                count(stopReason or "assistant")
                return {"kind": "message", "model": [assistant(f"a{nextId()}", calls=ids, stopReason=stopReason)]}
            if pick < 8:
                matched = len(calls) > 0 and random(5) > 0
                count("result" if matched else "stray result")
                id = calls.pop(random(len(calls))) if matched else f"stray{nextId()}"
                return {"kind": "message", "model": [toolResult(id)]}
            if pick < 9:
                # `pi.system` entries can be the baseline that leads the context (#10904).
                kind = "pi.system" if random(2) == 0 else "message"
                count("baseline system" if kind == "pi.system" else "system")
                return {"kind": kind, "model": [system({"s": f"v{nextId()}"})]}
            if pick < 10:
                count("note")
                return {"kind": "note", "data": {"n": nextId()}}
            index = random(len(written))
            target = written[index] if index < len(written) else None
            if pick < 12 and target is not None:
                omit = random(2) == 0
                count("omit" if omit else "replace")
                action = {"action": "omit"} if omit else {"action": "replace", "messages": [user(f"r{nextId()}")]}
                return {"kind": "edit", "edits": [{"target": target["id"], **action}]}
            if pick < 13 and target is not None:
                count("head")
                return {"kind": "summary", "head": target["id"], "model": [user(f"h{nextId()}")]}
            count("user")
            return {"kind": "message", "model": [user(f"u{nextId()}")]}

        async def append(conversationId: int) -> None:
            async def change(tx: Any, _current: Any) -> None:
                added = 1 + random(3)
                for _ in range(added):
                    written.append(await tx.appendEntry(conversationId, draft()))

            await runtime.commit(change, taskContext)

        for step in range(150):
            await append(root.id)
            whole = await root.context(taskContext)
            assert await runtime.context(root.id, taskContext) == whole
            contributed = [message for messages in whole["contributions"] for message in messages]
            first = next((index for index, message in enumerate(contributed) if message["role"] == "system"), -1)
            if (
                first > 0
                and whole["messages"][0] is contributed[first]
                and any(message["role"] == "assistant" for message in contributed[:first])
            ):
                count("baseline past assistant")
            if step % 10 == 9:
                # Overlapping reads, one with an earlier cutoff, share the kept range.
                at = written[random(len(written))]["id"]
                whole, cut = await asyncio.gather(
                    runtime.context(root.id, taskContext), runtime.context(root.id, taskContext, {"at": at})
                )
                assert whole == await root.context(taskContext)
                assert cut == await root.context(taskContext, {"at": at})
        # A fork sees its parent's entries through the fork point and extends with its own.
        fork = await root.fork(written[random(len(written))]["id"], {"ownership": {"kind": "ownerless"}}, taskContext)
        for _ in range(20):
            assert await runtime.context(fork.id, taskContext) == await fork.context(taskContext)
            await append(fork.id)
        await runtime.commit(lambda tx, current: DONE, taskContext)

    Steps = oneRun("test.context-steps", run)
    addTask(s.registry, Steps)
    s.harness.resume()
    id = await s.harness.commit(lambda tx: tx.createTask(Steps, {}, conversationTask(root)), context)
    assert (await s.harness.waitForTask(id, context))["state"]["outcome"] == DONE["outcome"]
    categories = [
        "user",
        "assistant",
        "aborted",
        "error",
        "deferred",
        "result",
        "stray result",
        "system",
        "baseline system",
        "baseline past assistant",
        "note",
    ]
    for category in [*categories, "omit", "replace", "head"]:
        assert seen.get(category, 0) > 0, category
    await s.harness.close(context)


# #10904
async def test_finds_the_baseline_among_the_entries_an_extended_read_adds():
    s = await countingSetup()
    root, first, storage = s.root, s.first, s.storage

    async def run(_task: Any, runtime: Any, taskContext: Any) -> None:
        async def append(conversationId: int, draft: dict[str, Any]) -> None:
            async def change(tx: Any, _current: Any) -> None:
                await tx.appendEntry(conversationId, draft)

            await runtime.commit(change, taskContext)

        async def read(conversation: Any) -> SimpleNamespace:
            before = storage.rows
            view = await runtime.context(conversation.id, taskContext)
            rows = storage.rows - before
            assert view == await conversation.context(taskContext)
            return SimpleNamespace(messages=[describeMessage(message) for message in view["messages"]], rows=rows)

        def ownerless(conversation: Any) -> Any:
            return conversation.fork(first["id"], {"ownership": {"kind": "ownerless"}}, taskContext)

        # The cached range has answers and no system message; the baseline arrives in a later read.
        assert (await read(root)).messages[0] == "user:first"
        await append(root.id, {"kind": "pi.system", "model": [system({"plan": "Plan."})]})
        read2 = await read(root)
        # One bounds probe and the added entry: the range was extended, not derived again.
        assert read2.rows == 2
        assert read2.messages[:3] == ["system:plan", "user:first", "assistant:old 0"]

        # A system message the cached range already has keeps a later first `pi.system` entry in place.
        fork = await ownerless(root)
        await append(fork.id, {"kind": "message", "model": [assistant("answer")]})
        await append(fork.id, {"kind": "host.system", "model": [system({"note": "Note."})]})
        assert (await read(fork)).messages == ["user:first", "assistant:answer", "system:note"]
        await append(fork.id, {"kind": "pi.system", "model": [system({"note": None})]})
        read2 = await read(fork)
        assert read2.rows == 2
        assert read2.messages == ["user:first", "assistant:answer", "system:note", "system:note"]

        # An omitted system message still rules out a later baseline; the extended read keeps that.
        omitted = await ownerless(root)
        await append(omitted.id, {"kind": "message", "model": [assistant("answer")]})
        host: dict[str, Any] = {}

        async def hostChange(tx: Any, _current: Any) -> None:
            host["entry"] = await tx.appendEntry(
                omitted.id, {"kind": "host.system", "model": [system({"legacy": "Legacy."})]}
            )

        await runtime.commit(hostChange, taskContext)
        await append(omitted.id, {"kind": "edit", "edits": [{"target": host["entry"]["id"], "action": "omit"}]})
        assert (await read(omitted)).messages == ["user:first", "assistant:answer"]
        await append(omitted.id, {"kind": "pi.system", "model": [system({"legacy": None})]})
        read2 = await read(omitted)
        assert read2.rows == 2
        assert read2.messages == ["user:first", "assistant:answer", "system:legacy"]

        # A first `pi.system` entry without a system message leaves no baseline for a later one to take.
        empty = await ownerless(root)
        await append(empty.id, {"kind": "pi.system"})
        await append(empty.id, {"kind": "message", "model": [assistant("answer")]})
        assert (await read(empty)).messages == ["user:first", "assistant:answer"]
        await append(empty.id, {"kind": "pi.system", "model": [system({"s": "Delta."})]})
        read2 = await read(empty)
        assert read2.rows == 2
        assert read2.messages == ["user:first", "assistant:answer", "system:s"]
        await runtime.commit(lambda tx, current: DONE, taskContext)

    Steps = oneRun("test.context-baseline", run)
    addTask(s.registry, Steps)
    s.harness.resume()
    id = await s.harness.commit(lambda tx: tx.createTask(Steps, {}, conversationTask(root)), context)
    assert (await s.harness.waitForTask(id, context))["state"]["outcome"] == DONE["outcome"]
    await s.harness.close(context)


async def test_keeps_returned_views_and_kept_ranges_apart():
    s = await countingSetup()
    root = s.root

    async def run(_task: Any, runtime: Any, taskContext: Any) -> None:
        view = await runtime.context(root.id, taskContext)
        view["messages"].clear()
        view["entries"].pop()
        # MISAKA fork: upstream's kept entries are frozen, so this assignment throws there; here the view is a
        # detached copy, so it changes only the view.
        view["contributions"][0][0]["content"] = "changed"
        assert await runtime.context(root.id, taskContext) == await root.context(taskContext)
        await runtime.commit(lambda tx, current: DONE, taskContext)

    Mutate = oneRun("test.context-mutate", run)
    addTask(s.registry, Mutate)
    s.harness.resume()
    id = await s.harness.commit(lambda tx: tx.createTask(Mutate, {}, conversationTask(root)), context)
    assert (await s.harness.waitForTask(id, context))["state"]["outcome"] == DONE["outcome"]
    await s.harness.close(context)


class SharingStorage(MemoryStorage):
    """MemoryStorage whose scanned records are also kept here, so a change to them would show."""

    def __init__(self) -> None:
        super().__init__()
        self.handedOut: list[dict[str, Any]] = []

    @eager
    async def scanEntries(self, query: dict[str, Any], limit: int, cursor: Any, context: Any) -> Any:
        page = await super().scanEntries(query, limit, cursor, context)
        self.handedOut.extend(page["items"])
        return page


async def test_keeps_kept_entries_apart_from_returned_views_even_when_storage_shares_its_records():
    # MISAKA fork of "freezes kept entries deeply even when storage freezes them shallowly": a change to a returned
    # view reaches neither the kept range nor the records storage handed out.
    storage = SharingStorage()
    registry = createRegistry()
    harness = await Harness.open(storage, {"models": createModels(), "registry": registry}, context)
    root = await harness.root(context)
    await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "message", "model": [user("hello")]}), context)

    async def run(_task: Any, runtime: Any, taskContext: Any) -> None:
        view = await runtime.context(root.id, taskContext)
        view["messages"][0]["content"] = "changed"
        assert all(entry["model"][0]["content"] == "hello" for entry in storage.handedOut if entry.get("model"))
        assert [describeMessage(message) for message in (await runtime.context(root.id, taskContext))["messages"]] == [
            "user:hello"
        ]
        await runtime.commit(lambda tx, current: DONE, taskContext)

    Mutate = oneRun("test.context-shallow", run)
    addTask(registry, Mutate)
    harness.resume()
    id = await harness.commit(lambda tx: tx.createTask(Mutate, {}, conversationTask(root)), context)
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == DONE["outcome"]
    await harness.close(context)


async def test_does_not_keep_a_read_that_finishes_after_its_task_ended():
    s = await countingSetup({"settings": {"contextRetentionMs": 0}})
    harness, root, storage = s.harness, s.root, s.storage
    loop = asyncio.get_running_loop()
    scans: asyncio.Future[None] = loop.create_future()
    probeStart: asyncio.Future[None] = loop.create_future()
    late: dict[str, Any] = {}

    async def lateRun(_task: Any, runtime: Any, taskContext: Any) -> None:
        storage.hold = scans
        late["read"] = runtime.context(root.id, taskContext)
        await runtime.commit(lambda tx, current: DONE, taskContext)

    Late = oneRun("test.context-late", lateRun)
    rows: dict[str, int] = {}

    async def read(name: str, load: Callable[[], Any], _taskContext: Any) -> None:
        await probeStart
        before = storage.rows
        await load()
        rows[name] = storage.rows - before

    Probe = probeTask(read)
    for task in [Late, Probe]:
        addTask(s.registry, task)
    harness.resume()
    lateId = await harness.commit(lambda tx: tx.createTask(Late, {}, conversationTask(root)), context)
    assert (await harness.waitForTask(lateId, context))["state"]["outcome"] == DONE["outcome"]
    # The conversation is busy again when the late read finishes; its range must not be kept.
    probeId = await harness.commit(lambda tx: tx.createTask(Probe, {"name": "probe"}, conversationTask(root)), context)
    scans.set_result(None)
    try:
        await late["read"]
    except Exception:  # noqa: BLE001, S110 - the late read may fail; it only must not be kept
        pass
    probeStart.set_result(None)
    assert (await harness.waitForTask(probeId, context))["state"]["outcome"] == DONE["outcome"]
    assert rows == {"probe": 22}
    await harness.close(context)


async def test_drops_an_idle_conversations_context_read_on_a_timer_when_nothing_else_runs():
    # MISAKA fork: vitest's fake timers count Node timers; here a short real retention runs the asyncio timer, and
    # the scheduler's pending expiry stands in for the timer count.
    s = await countingSetup({"settings": {"contextRetentionMs": 50}})
    harness, root, storage = s.harness, s.root, s.storage
    rows: dict[str, int] = {}

    async def read(name: str, load: Callable[[], Any], _taskContext: Any) -> None:
        before = storage.rows
        await load()
        rows[name] = storage.rows - before

    Probe = probeTask(read)
    addTask(s.registry, Probe)
    harness.resume()

    async def probe(name: str) -> None:
        id = await harness.commit(lambda tx: tx.createTask(Probe, {"name": name}, conversationTask(root)), context)
        assert (await harness.waitForTask(id, context))["state"]["outcome"] == DONE["outcome"]
        await root.waitForIdle(context)

    scheduler = harness._tasks
    assert scheduler._expiry is None
    await probe("first")
    assert scheduler._expiry is not None
    await asyncio.sleep(0.2)
    assert scheduler._expiry is None
    assert scheduler._contexts == {}
    await probe("second")
    assert rows == {"first": 22, "second": 22}
    await harness.close(context)


async def test_drops_a_conversations_context_read_once_idle_with_zero_retention():
    s = await countingSetup({"settings": {"contextRetentionMs": 0}})
    harness, root, storage = s.harness, s.root, s.storage
    rows: dict[str, int] = {}

    async def read(name: str, load: Callable[[], Any], _taskContext: Any) -> None:
        before = storage.rows
        await load()
        rows[name] = storage.rows - before

    Probe = probeTask(read)
    addTask(s.registry, Probe)
    harness.resume()
    for name in ["first", "second"]:
        id = await harness.commit(
            lambda tx, name=name: tx.createTask(Probe, {"name": name}, conversationTask(root)), context
        )
        assert (await harness.waitForTask(id, context))["state"]["outcome"] == DONE["outcome"]
        await root.waitForIdle(context)
    assert rows == {"first": 22, "second": 22}
    await harness.close(context)
