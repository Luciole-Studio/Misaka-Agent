"""pi durable's ``test/harness-tasks.test.ts``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.models import createModels
from misaka.chord.context import withAbortSignal, withCancel
from misaka.durable import (
    Harness,
    MemoryStorage,
    createRegistry,
    defineDoc,
    defineEntry,
    defineTask,
    defineTool,
)
from misaka.durable.errors import SessionFailed
from misaka.utils.js import UNDEFINED, AbortController
from tests.durable_support import ControlledStorage, context, flush, toMatchObject
from tests.harness_support import addHooks, addTask, addTool, user
from tests.task_support import aborted as abortedBy
from tests.task_support import (
    abortedWith,
    completed,
    deferred,
    eventually,
    openTasks,
    settled,
)

EMPTY_OBJECT = {"type": "object", "properties": {}}


def oneStep(name: str, run: Callable[..., Any], abort: Callable[..., Any] | None = None) -> dict[str, Any]:
    """A one-phase task; the default abort handler settles ``aborted`` with reason "test"."""

    async def runPhase(task: Any, runtime: Any, ctx: Any) -> None:
        await run(task, runtime, ctx)

    async def abortPhase(_task: Any, runtime: Any, ctx: Any) -> None:
        if abort is None:
            await runtime.commit(lambda tx, current: abortedWith("test"), ctx)
        else:
            await abort(runtime, ctx)

    return defineTask(
        {
            "name": name,
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": runPhase},
            "abort": abortPhase,
        }
    )


def gated(name: str, gate: Any) -> dict[str, Any]:
    """A one-phase task that waits for ``gate`` and completes with null."""

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        await gate
        await runtime.commit(lambda tx, current: completed(None), ctx)

    return oneStep(name, run)


def start(conversation: Any, task: dict[str, Any], options: dict[str, Any] | None = None) -> Any:
    return conversation.commit(
        lambda tx: tx.createTask(task, None, {"ownership": {"kind": "conversation"}, **(options or {})}), context
    )


async def openRoot(tasks: list[dict[str, Any]], options: dict[str, Any] | None = None) -> SimpleNamespace:
    options = options or {}
    harness, registry, reports = await openTasks(options.get("storage") or MemoryStorage(), tasks, options)
    root = await harness.root(context)
    return SimpleNamespace(harness=harness, registry=registry, reports=reports, root=root)


async def markDurably(harness: Any, id: int) -> Any:
    """Start ``abortTask()`` and resolve once its mark is durable, before it has joined the run. Returns ``aborting``."""
    aborting = harness.abortTask(id, context)
    while not (await harness.getTask(id, context))["abortRequested"]:
        await flush()
    return aborting


def withSignal(signal: Any) -> Any:
    return withAbortSignal(signal, context)


def consume(future: Any) -> Any:
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return future


async def noAbort(_task: Any, runtime: Any, ctx: Any) -> None:
    return None


def never() -> Any:
    return asyncio.get_running_loop().create_future()


async def catching(awaitable: Any) -> Any:
    """``promise.catch((error) => error)``."""
    try:
        return await awaitable
    except Exception as error:  # noqa: BLE001 - the error is the value
        return error


async def test_phases_continue_one_invocation_through_checkpoint_progress_and_complete_with_a_typed_result():
    seen: list[int] = []
    runtimes: set[int] = set()

    async def count(task: Any, runtime: Any, ctx: Any) -> None:
        seen.append(task["state"]["checkpoint"]["n"])
        runtimes.add(id(runtime))

        def change(tx: Any, current: Any) -> dict[str, Any]:
            n = current["state"]["checkpoint"]["n"] + 1
            return completed(n) if n == task["input"]["to"] else {"status": "running", "checkpoint": {"phase": "count", "n": n}}

        await runtime.commit(change, ctx)

    Counter = defineTask(
        {
            "name": "test.counter",
            "version": 1,
            "initial": lambda input: {"phase": "count", "n": 0},
            "phases": {"count": count},
            "abort": noAbort,
        }
    )
    opened = await openRoot([Counter])
    harness, root = opened.harness, opened.root
    taskId = await root.commit(
        lambda tx: tx.createTask(Counter, {"to": 3}, {"ownership": {"kind": "conversation"}}), context
    )
    harness.resume()
    receipt = await harness.waitForTask(taskId, context)
    outcome = receipt["state"]["outcome"]
    result = outcome["result"] if outcome["status"] == "completed" else None
    assert result == 3
    assert seen == [0, 1, 2]
    assert len(runtimes) == 1
    await harness.close(context)


async def test_phases_fault_a_phase_without_durable_progress_and_a_throwing_phase():
    async def idle(*_: Any) -> None:
        return None

    async def documentOnly(_task: Any, runtime: Any, ctx: Any) -> None:
        # A commit that returns no state is not progress.
        await runtime.commit(lambda tx, current: None, ctx)

    async def throws(*_: Any) -> None:
        raise RuntimeError("boom")

    Idle = oneStep("test.idle", idle)
    DocumentOnly = oneStep("test.document-only", documentOnly)
    Throws = oneStep("test.throws", throws)
    opened = await openRoot([Idle, DocumentOnly, Throws])
    harness, root = opened.harness, opened.root
    ids = [await start(root, Idle), await start(root, DocumentOnly), await start(root, Throws)]
    harness.resume()
    outcomes = [(await harness.waitForTask(id, context))["state"]["outcome"] for id in ids]
    assert outcomes == [
        {"status": "faulted", "error": {"message": "Task test.idle phase run returned without durable progress"}},
        {
            "status": "faulted",
            "error": {"message": "Task test.document-only phase run returned without durable progress"},
        },
        {"status": "faulted", "error": {"message": "boom"}},
    ]
    await harness.close(context)


async def test_phases_keep_a_committed_terminal_outcome_when_the_handler_throws_afterwards():
    holder: dict[str, Any] = {}

    async def done(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: completed("ok"), ctx)
        holder["lateCommit"] = await catching(runtime.commit(lambda tx, current: None, ctx))
        raise RuntimeError("after terminal")

    Done = oneStep("test.done", done)
    opened = await openRoot([Done])
    harness, root = opened.harness, opened.root
    id = await start(root, Done)
    harness.resume()
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "completed", "result": "ok"}
    await eventually(lambda: holder.get("lateCommit") is not None)
    assert f"Task {id} is terminal" in str(holder["lateCommit"])
    await harness.close(context)


async def test_phases_compare_checkpoints_by_value_including_arrays():
    async def collect(task: Any, runtime: Any, ctx: Any) -> None:
        items = task["state"]["checkpoint"]["items"]
        # Two rounds of progress, then an equal copy of the checkpoint, which is no progress.
        following = [*items, f"item{len(items)}"] if len(items) < 2 else [*items]
        await runtime.commit(
            lambda tx, current: {"status": "running", "checkpoint": {"phase": "collect", "items": following}}, ctx
        )

    Collect = defineTask(
        {
            "name": "test.collect",
            "version": 1,
            "initial": lambda input: {"phase": "collect", "items": []},
            "phases": {"collect": collect},
            "abort": noAbort,
        }
    )
    opened = await openRoot([Collect])
    harness, root = opened.harness, opened.root
    id = await root.commit(lambda tx: tx.createTask(Collect, None, {"ownership": {"kind": "conversation"}}), context)
    harness.resume()
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {
        "status": "faulted",
        "error": {"message": "Task test.collect phase collect returned without durable progress"},
    }
    await harness.close(context)


async def test_phases_commit_results_with_entries_atomically_keep_memos_until_terminal_and_retire_task_documents():
    Progress = defineDoc({"kind": "test.task-progress", "version": 1, "scope": "task", "initial": lambda: {"lines": []}})
    Answer = defineEntry("answer")

    async def childRun(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Child = oneStep("test.child", childRun)
    holder: dict[str, Any] = {}

    async def write(task: Any, runtime: Any, ctx: Any) -> None:
        assert await runtime.memo("choice", ctx) is UNDEFINED
        winners = await asyncio.gather(runtime.memo("choice", "a", ctx), runtime.memo("choice", "b", ctx))
        assert winners == ["a", "a"]
        assert await runtime.memo("choice", ctx) == "a"
        # Memo names never resolve to inherited object properties.
        assert await runtime.memo("toString", ctx) is UNDEFINED
        assert await runtime.memo("toString", "own", ctx) == "own"

        async def change(tx: Any, current: Any) -> dict[str, Any]:
            (await tx.doc(Progress, task["id"]))["lines"].push("wrote")
            # Task creation defaults to the task's own conversation.
            holder["childId"] = await tx.createTask(Child, None, {"ownership": {"kind": "conversation"}})
            return {"status": "running", "checkpoint": {"phase": "answer"}}

        await runtime.commit(change, ctx)

    async def answer(task: Any, runtime: Any, ctx: Any) -> None:
        assert task["memos"] == {"choice": "a", "toString": "own"}

        async def readProgress(tx: Any, current: Any) -> None:
            holder["progressSeen"] = list((await tx.doc(Progress, task["id"]))["lines"])

        await runtime.commit(readProgress, ctx)

        async def finish(tx: Any, current: Any) -> dict[str, Any]:
            entry = await tx.appendEntry(current["conversationId"], {"kind": "answer", "model": [user("done")]})
            return completed({"entryId": entry["id"]})

        await runtime.commit(finish, ctx)

    Writer = defineTask(
        {
            "name": "test.writer",
            "version": 1,
            "initial": lambda input: {"phase": "write"},
            "phases": {"write": write, "answer": answer},
            "abort": noAbort,
        }
    )
    opened = await openRoot([Writer, Child])
    harness, root = opened.harness, opened.root
    conversation = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    id = await conversation.commit(
        lambda tx: tx.createTask(Writer, None, {"ownership": {"kind": "conversation"}}), context
    )
    harness.resume()
    receipt = await harness.waitForTask(id, context)
    assert "checkpoint" not in receipt["state"]
    assert "memos" not in receipt
    outcome = receipt["state"]["outcome"]
    if outcome["status"] != "completed":
        raise AssertionError("expected completion")
    entry = await harness.commit(lambda tx: tx.entry(outcome["result"]["entryId"]), context)
    assert Answer.is_(entry) is True
    assert entry["conversationId"] == conversation.id
    assert holder["progressSeen"] == ["wrote"]
    assert await harness.snapshot(Progress, id, context) is None
    assert await harness.getTask(id, context) == receipt
    assert (await harness.waitForTask(holder["childId"], context))["conversationId"] == conversation.id
    assert root.id != conversation.id
    await harness.close(context)


async def test_phases_resume_a_waiting_task_once_every_task_in_on_is_terminal_whatever_the_outcome():
    gate = deferred()
    order: list[str] = []
    state: dict[str, Any] = {"on": [], "outcomes": []}

    async def waitPhase(_task: Any, runtime: Any, ctx: Any) -> None:
        order.append("wait")
        checkpoint = {"phase": "resume"}
        await runtime.commit(
            lambda tx, current: {"status": "waiting", "checkpoint": checkpoint, "on": state["on"], "policy": "allSettled"}, ctx
        )

    async def resumePhase(_task: Any, runtime: Any, ctx: Any) -> None:
        order.append("resume")
        state["outcomes"] = [outcome["status"] for outcome in await runtime.outcomes(state["on"], ctx)]
        await runtime.commit(lambda tx, current: completed(None), ctx)

    async def waiterAbort(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: abortedWith("test"), ctx)

    Waiter = defineTask(
        {
            "name": "test.waiter",
            "version": 1,
            "initial": lambda input: {"phase": "wait"},
            "phases": {"wait": waitPhase, "resume": resumePhase},
            "abort": waiterAbort,
        }
    )

    async def firstRun(_task: Any, runtime: Any, ctx: Any) -> None:
        order.append("first")
        await gate.promise
        await runtime.commit(lambda tx, current: completed(None), ctx)

    async def faultingRun(*_: Any) -> None:
        order.append("faulting")
        raise RuntimeError("fails")

    First = oneStep("test.first", firstRun)
    Faulting = oneStep("test.faulting", faultingRun)
    opened = await openRoot([First, Faulting, Waiter])
    harness, root = opened.harness, opened.root
    first = await start(root, First)
    faulting = await start(root, Faulting)
    state["on"] = [first, faulting]
    waiter = await root.commit(lambda tx: tx.createTask(Waiter, None, {"ownership": {"kind": "conversation"}}), context)
    harness.resume()
    await eventually(lambda: len(order) == 3)
    await flush()
    assert sorted(order) == ["faulting", "first", "wait"]
    toMatchObject((await harness.getTask(waiter, context))["state"], {"status": "waiting", "on": state["on"]})
    gate.resolve()
    await harness.waitForTask(waiter, context)
    assert order[-1] == "resume"
    assert state["outcomes"] == ["completed", "faulted"]
    await harness.close(context)


# #10549
async def test_phases_stamp_startedat_at_the_first_run_and_endedat_when_terminal_keeping_startedat_through_a_wait():
    clock = {"now": 1_000}
    gate = deferred()
    state: dict[str, Any] = {"on": []}

    async def waitPhase(_task: Any, runtime: Any, ctx: Any) -> None:
        checkpoint = {"phase": "resume"}
        await runtime.commit(
            lambda tx, current: {"status": "waiting", "checkpoint": checkpoint, "on": state["on"], "policy": "allSettled"},
            ctx,
        )

    async def resumePhase(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: completed(None), ctx)

    async def waiterAbort(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: abortedWith("test"), ctx)

    Waiter = defineTask(
        {
            "name": "test.timed-waiter",
            "version": 1,
            "initial": lambda input: {"phase": "wait"},
            "phases": {"wait": waitPhase, "resume": resumePhase},
            "abort": waiterAbort,
        }
    )
    Held = gated("test.timed-held", gate.promise)
    opened = await openRoot([Waiter, Held], {"now": lambda: clock["now"]})
    harness, root = opened.harness, opened.root
    held = await start(root, Held)
    state["on"] = [held]
    waiter = await root.commit(lambda tx: tx.createTask(Waiter, None, {"ownership": {"kind": "conversation"}}), context)

    async def times() -> list[list[Any]]:
        return [
            [each["record"]["id"], each["record"].get("startedAt"), each["record"].get("endedAt")]
            for each in (await harness.inspect(context))["tasks"]
        ]

    assert await times() == [[held, None, None], [waiter, None, None]]

    clock["now"] = 2_000
    harness.resume()

    async def waiting() -> bool:
        return (await harness.getTask(waiter, context))["state"]["status"] == "waiting"

    await eventually(waiting)
    assert await times() == [[held, 2_000, None], [waiter, 2_000, None]]

    # The waiter runs again at 5_000; its start stays the first run.
    clock["now"] = 5_000
    gate.resolve()
    settledWaiter = await harness.waitForTask(waiter, context)
    settledHeld = await harness.getTask(held, context)
    assert [settledHeld.get("startedAt"), settledHeld.get("endedAt")] == [2_000, 5_000]
    assert [settledWaiter.get("startedAt"), settledWaiter.get("endedAt")] == [2_000, 5_000]
    await harness.close(context)


async def test_phases_keep_one_registry_snapshot_per_phase_and_refresh_it_at_the_phase_boundary():
    seen: list[dict[str, Any]] = []
    phaseGate = deferred()
    entered = deferred()

    def tools(snapshot: Any) -> list[str]:
        return [entry["tool"]["name"] for entry in snapshot.tools()]

    async def a(_task: Any, runtime: Any, ctx: Any) -> None:
        before = runtime.registry
        entered.resolve()
        await phaseGate.promise
        seen.append({"phase": "a", "tools": tools(runtime.registry), "same": runtime.registry is before})
        await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": {"phase": "b"}}, ctx)

    async def b(_task: Any, runtime: Any, ctx: Any) -> None:
        seen.append({"phase": "b", "tools": tools(runtime.registry), "same": True})
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Snapshots = defineTask(
        {
            "name": "test.snapshots",
            "version": 1,
            "initial": lambda input: {"phase": "a"},
            "phases": {"a": a, "b": b},
            "abort": noAbort,
        }
    )
    opened = await openRoot([Snapshots])
    harness, registry, root = opened.harness, opened.registry, opened.root
    id = await root.commit(lambda tx: tx.createTask(Snapshots, None, {"ownership": {"kind": "conversation"}}), context)
    harness.resume()
    await entered.promise

    async def late(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {}

    addTool(
        registry, defineTool({"name": "late", "description": "late", "parameters": EMPTY_OBJECT, "execute": late})
    )
    phaseGate.resolve()
    await harness.waitForTask(id, context)
    assert seen == [
        {"phase": "a", "tools": [], "same": True},
        {"phase": "b", "tools": ["late"], "same": True},
    ]
    await harness.close(context)


async def test_phases_resolve_the_agent_at_first_use_in_a_phase_from_its_snapshot_keep_it_for_the_phase_and_anew_at_the_next():
    pings: list[str] = []
    seen: list[dict[str, Any]] = []
    beforeUse = deferred()
    afterUse = deferred()
    waitingBeforeUse = deferred()
    waitingAfterUse = deferred()

    async def ping(runtime: Any) -> list[str]:
        pings.clear()
        await runtime.hooks.each("ping", lambda handler: handler())
        return list(pings)

    async def a(_task: Any, runtime: Any, ctx: Any) -> None:
        waitingBeforeUse.resolve()
        await beforeUse.promise
        first = await runtime.agent(ctx)
        seen.append({"phase": "a", "pings": await ping(runtime), "thinking": first["thinkingLevel"]})
        waitingAfterUse.resolve()
        await afterUse.promise
        second = await runtime.agent(ctx)
        seen.append({"phase": "a", "pings": await ping(runtime), "thinking": second["thinkingLevel"], "same": second is first})
        await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": {"phase": "b"}}, ctx)

    async def b(_task: Any, runtime: Any, ctx: Any) -> None:
        agent = await runtime.agent(ctx)
        seen.append({"phase": "b", "pings": await ping(runtime), "thinking": agent["thinkingLevel"]})
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Phases = defineTask(
        {
            "name": "test.agent-phases",
            "version": 1,
            "initial": lambda input: {"phase": "a"},
            "phases": {"a": a, "b": b},
            "abort": noAbort,
        }
    )
    opened = await openRoot([Phases])
    harness, registry, root = opened.harness, opened.registry, opened.root
    addHooks(registry, Phases, {"ping": lambda *_: pings.append("before")})
    id = await root.commit(lambda tx: tx.createTask(Phases, None, {"ownership": {"kind": "conversation"}}), context)
    harness.resume()

    # Configured during the phase but before its first use: the lazy resolution reads it.
    await waitingBeforeUse.promise
    await root.configure({"thinkingLevel": "low"}, context)
    beforeUse.resolve()
    # Configured and installed after the first use: not seen until the next phase.
    await waitingAfterUse.promise
    addHooks(registry, Phases, {"ping": lambda *_: pings.append("late")})
    await root.configure({"thinkingLevel": "high"}, context)
    afterUse.resolve()

    await harness.waitForTask(id, context)
    assert seen == [
        {"phase": "a", "pings": ["before"], "thinking": "low"},
        {"phase": "a", "pings": ["before"], "thinking": "low", "same": True},
        {"phase": "b", "pings": ["before", "late"], "thinking": "high"},
    ]
    await harness.close(context)


async def test_phases_reject_an_agent_wait_whose_caller_context_is_cancelled_without_affecting_the_phases_resolution():
    holder: dict[str, Any] = {}

    async def agentCancel(_task: Any, runtime: Any, ctx: Any) -> None:
        caller = withCancel(ctx)
        caller.cancel(RuntimeError("caller gone"))
        holder["cancelled"] = await catching(runtime.agent(caller.context))
        holder["resolved"] = (await runtime.agent(ctx))["thinkingLevel"]
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Agent = oneStep("test.agent-cancel", agentCancel)
    opened = await openRoot([Agent])
    harness, root = opened.harness, opened.root
    await harness.waitForTask(await start(root, Agent), context)
    assert str(holder["cancelled"]) == "caller gone"
    assert holder["resolved"] == "off"
    await harness.close(context)


async def test_phases_observe_a_failed_agent_resolution_whose_only_caller_stopped_waiting():
    state = {"fail": False}

    class Settings(dict[str, Any]):
        """Settings whose ``extensions`` is a getter."""

        def get(self, key: str, default: Any = None) -> Any:
            if key == "extensions":
                if state["fail"]:
                    raise RuntimeError("settings broke")
                return None
            return super().get(key, default)

    holder: dict[str, Any] = {}

    async def agentFailure(_task: Any, runtime: Any, ctx: Any) -> None:
        caller = withCancel(ctx)
        caller.cancel(RuntimeError("caller gone"))
        state["fail"] = True
        await catching(runtime.agent(caller.context))
        # Let an unobserved rejection surface before the task ends.
        await asyncio.sleep(0.01)
        # MISAKA fork: vitest fails the run on an unhandled rejection; asyncio reports an unretrieved task exception only when
        # the task is collected, so the observation that the shared resolution's failure was retrieved is checked directly.
        holder["observed"] = runtime._phase.agent is not None and runtime._phase.agent._log_traceback is False
        holder["resolved"] = await catching(runtime.agent(ctx))
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Agent = oneStep("test.agent-failure", agentFailure)
    opened = await openRoot([Agent], {"settings": Settings()})
    harness, root = opened.harness, opened.root
    await harness.waitForTask(await start(root, Agent), context)
    assert str(holder["resolved"]) == "settings broke"
    assert holder["observed"] is True
    await harness.close(context)


async def test_runtime_rejects_runtime_operations_after_the_invocation_ends_and_stops_its_watches():
    Notes = defineDoc({"kind": "test.task-notes", "version": 1, "scope": "session", "initial": lambda: {"text": ""}})
    Absent = defineDoc({"kind": "test.task-absent", "version": 1, "scope": "session", "initial": lambda: {"text": ""}})
    delivered: list[str] = []
    holder: dict[str, Any] = {"absent": "unset"}

    async def watcher(_task: Any, runtime: Any, ctx: Any) -> None:
        holder["captured"] = runtime
        holder["handlerContext"] = ctx
        holder["absent"] = await runtime.watchDoc(Absent, ctx)
        watch = await runtime.watchDoc(Notes, ctx)

        async def onValue(value: Any, ops: Any, callContext: Any) -> None:
            delivered.append("retired" if value is None else value["text"])

        watch.start(onValue)
        holder["watchClosed"] = watch.closed
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Watcher = oneStep("test.watcher", watcher)
    opened = await openRoot([Watcher])
    harness, root = opened.harness, opened.root

    async def hello(tx: Any) -> None:
        (await tx.doc(Notes))["text"] = "hello"

    await harness.commit(hello, context)
    id = await start(root, Watcher)
    harness.resume()
    await harness.waitForTask(id, context)
    assert holder["absent"] is None
    # The watch stops when the step after the phase ends the invocation; later commits deliver nothing.
    assert await holder["watchClosed"] == {"reason": "stopped"}

    async def after(tx: Any) -> None:
        (await tx.doc(Notes))["text"] = "after"

    await harness.commit(after, context)
    await flush()
    assert delivered == []
    runtime = holder["captured"]
    with pytest.raises(Exception, match="invocation has ended"):
        await runtime.commit(lambda tx, current: None, context)
    with pytest.raises(Exception, match="invocation has ended"):
        await runtime.memo("x", context)
    with pytest.raises(Exception, match="invocation has ended"):
        await runtime.memo("x", 1, context)
    with pytest.raises(Exception, match="invocation has ended"):
        await runtime.sleep(0, context)
    with pytest.raises(Exception, match="invocation has ended"):
        await runtime.watchDoc(Notes, context)
    # The handler's own context is cancelled by now; the ended invocation still wins.
    with pytest.raises(Exception, match="invocation has ended"):
        await runtime.agent(holder["handlerContext"])
    with pytest.raises(Exception, match="invocation has ended"):
        runtime.now()
    with pytest.raises(Exception, match="invocation has ended"):
        runtime.report(RuntimeError("late"))
    await harness.close(context)


async def test_runtime_reads_committed_documents_and_context_through_the_runtime_and_forwards_the_clock_and_reports():
    Notes = defineDoc(
        {
            "kind": "test.runtime-notes",
            "version": 1,
            "scope": "conversation",
            "history": "rewindable",
            "fork": "asOf",
            "initial": lambda: {"text": ""},
        }
    )
    seen: list[Any] = []
    holder: dict[str, Any] = {}

    async def reader(_task: Any, runtime: Any, ctx: Any) -> None:
        holder["captured"] = runtime
        ids = [entry["id"] for entry in (await runtime.context(runtime.conversationId, ctx))["entries"]]
        first, second = ids[0], ids[1]
        seen.append((await runtime.snapshot(Notes, runtime.conversationId, ctx))["text"])
        seen.append((await runtime.snapshotAsOf(Notes, runtime.conversationId, first, ctx))["text"])
        seen.append(len((await runtime.context(runtime.conversationId, ctx, {"at": first}))["entries"]))
        seen.append(len((await runtime.context(runtime.conversationId, ctx, {"at": second}))["messages"]))
        seen.append(runtime.now())
        runtime.report(RuntimeError("reported"))
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Reader = oneStep("test.reader", reader)
    opened = await openRoot([Reader], {"now": lambda: 1234})
    harness, root, reports = opened.harness, opened.root, opened.reports
    for text in ("one", "two"):

        async def write(tx: Any, text: str = text) -> None:
            (await tx.doc(Notes, root.id))["text"] = text
            await tx.appendEntry(root.id, {"kind": "message", "model": [user(text)]})

        await root.commit(write, context)
    id = await start(root, Reader)
    harness.resume()
    await harness.waitForTask(id, context)
    assert seen == ["two", "one", 1, 2, 1234]
    assert [str(error) for error in reports] == ["reported"]
    # The step after the phase ends the invocation.
    await flush()
    captured = holder["captured"]
    with pytest.raises(Exception, match="invocation has ended"):
        await captured.snapshot(Notes, root.id, context)
    with pytest.raises(Exception, match="invocation has ended"):
        await captured.context(root.id, context)
    await harness.close(context)


async def test_runtime_orders_runtime_commits_against_the_step_one_queued_before_it_lands_one_after_it_rejects():
    storage = ControlledStorage()
    holder: dict[str, Any] = {}

    async def detached(_task: Any, runtime: Any, ctx: Any) -> None:
        # Hold the line, queue a commit without awaiting it, and return; the step queues behind that commit.
        holder["held"] = storage.holdCommits()

        async def blocker(tx: Any) -> None:
            await tx.appendEntry(runtime.conversationId, {"kind": "blocker"})

        consume(holder["harness"].commit(blocker, context))
        holder["before"] = consume(runtime.commit(lambda tx, current: completed("before"), context))

        # Queued after the handler returned, so after the step.
        def queueAfter() -> None:
            holder["after"] = consume(runtime.commit(lambda tx, current: completed("after"), context))

        asyncio.get_running_loop().call_later(0, queueAfter)

    Detached = oneStep("test.detached", detached)
    opened = await openRoot([Detached], {"storage": storage})
    harness, root = opened.harness, opened.root
    holder["harness"] = harness
    id = await start(root, Detached)
    harness.resume()
    await eventually(lambda: "held" in holder)
    await holder["held"].entered
    await eventually(lambda: "after" in holder)
    holder["held"].release()
    await holder["before"]
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "completed", "result": "before"}
    with pytest.raises(Exception, match="invocation has ended|is terminal"):
        await holder["after"]
    await harness.close(context)


async def test_runtime_stops_a_watch_whose_acquisition_finishes_after_the_invocation_ended():
    Notes = defineDoc({"kind": "test.late-watch", "version": 1, "scope": "session", "initial": lambda: {"text": ""}})
    storage = ControlledStorage()
    holder: dict[str, Any] = {}

    async def late(_task: Any, runtime: Any, ctx: Any) -> None:
        holder["held"] = storage.holdCommits()

        async def blocker(tx: Any) -> None:
            await tx.appendEntry(runtime.conversationId, {"kind": "blocker"})

        consume(holder["harness"].commit(blocker, context))

        # Starts after the handler returned, so its line job queues behind the step that ends the invocation.
        def startWatch() -> None:
            holder["watching"] = asyncio.ensure_future(catching(runtime.watchDoc(Notes, context)))

        asyncio.get_running_loop().call_later(0, startWatch)

    Late = oneStep("test.late-watch", late)
    opened = await openRoot([Late], {"storage": storage})
    harness, root = opened.harness, opened.root
    holder["harness"] = harness

    async def setText(tx: Any) -> None:
        (await tx.doc(Notes))["text"] = "x"

    await harness.commit(setText, context)
    id = await start(root, Late)
    harness.resume()
    await eventually(lambda: "held" in holder)
    await holder["held"].entered
    await eventually(lambda: "watching" in holder)
    holder["held"].release()
    assert "invocation has ended" in str(await holder["watching"])
    toMatchObject((await harness.waitForTask(id, context))["state"]["outcome"], {"status": "faulted"})
    await harness.close(context)


async def test_runtime_sleeps_until_the_harness_clock_reaches_the_deadline_rechecking_after_each_timer():
    clock = 1_000
    woke = deferred()

    async def sleeper(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.sleep(900, ctx)
        await runtime.sleep(1_005, ctx)
        woke.resolve()
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Sleeper = oneStep("test.sleeper", sleeper)
    opened = await openRoot([Sleeper], {"now": lambda: clock})
    harness, root = opened.harness, opened.root
    id = await start(root, Sleeper)
    harness.resume()
    # The clock stands still, so real timers keep firing without waking the task.
    await asyncio.sleep(0.03)
    assert await settled(woke.promise) is False
    clock = 1_005
    await harness.waitForTask(id, context)
    await harness.close(context)


async def test_runtime_rejects_a_sleep_when_the_invocation_is_signalled_or_the_sleeps_own_context_is_cancelled():
    results: list[str] = []
    sleeping = deferred()

    def nowMs() -> int:
        return int(time.time() * 1000)

    async def signalled(_task: Any, runtime: Any, ctx: Any) -> None:
        sleeping.resolve()
        try:
            await runtime.sleep(nowMs() + 60_000, ctx)
        except Exception as error:
            results.append(f"signalled:{getattr(error, 'name', type(error).__name__)}")
            raise

    async def cancelledRun(_task: Any, runtime: Any, ctx: Any) -> None:
        controller = AbortController()
        sleepingCall = runtime.sleep(nowMs() + 60_000, withSignal(controller.signal))
        controller.abort(RuntimeError("stop sleeping"))
        try:
            await sleepingCall
        except Exception as error:  # noqa: BLE001 - the rejection is the observation
            results.append(f"cancelled:{error}")
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Signalled = oneStep("test.sleep-signalled", signalled)
    Cancelled = oneStep("test.sleep-cancelled", cancelledRun)
    opened = await openRoot([Signalled, Cancelled])
    harness, root = opened.harness, opened.root
    signalledId = await start(root, Signalled)
    cancelledId = await start(root, Cancelled)
    harness.resume()
    await harness.waitForTask(cancelledId, context)
    await sleeping.promise
    await harness.abortTask(signalledId, context)
    assert (await harness.waitForTask(signalledId, context))["state"]["outcome"] == {"status": "aborted", "reason": "test"}
    assert results == ["cancelled:stop sleeping", "signalled:AbortError"]
    await harness.close(context)


async def test_scheduling_fails_the_harness_on_a_failed_reservation_commit_reopening_runs_the_task():
    runs = 0

    async def once(_task: Any, runtime: Any, ctx: Any) -> None:
        nonlocal runs
        runs += 1
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Once = oneStep("test.once", once)
    storage = ControlledStorage()
    opened = await openRoot([Once], {"storage": storage})
    harness, root, reports = opened.harness, opened.root, opened.reports
    id = await start(root, Once)
    waiting = harness.waitForTask(id, context)
    failure = RuntimeError("disk gone")
    storage.failNextCommit(failure)
    harness.resume()
    end = await harness.closed
    assert end == {"reason": "failed", "error": failure}
    assert reports == [failure]
    # The pending wait and every later call get SessionFailed with the storage error.
    with pytest.raises(SessionFailed) as waitFailed:
        await waiting
    assert waitFailed.value.cause is failure
    with pytest.raises(SessionFailed) as getFailed:
        await harness.getTask(id, context)
    assert getFailed.value.cause is failure
    with pytest.raises(SessionFailed):
        await root.submit({"type": "input", "content": "x"}, context)
    assert runs == 0
    await harness.close(context)

    reopened = await openRoot([Once], {"storage": storage.reopen()})
    reopened.harness.resume()
    assert (await reopened.harness.waitForTask(id, context))["state"]["outcome"]["status"] == "completed"
    assert runs == 1
    await reopened.harness.close(context)


async def test_scheduling_fails_the_harness_instead_of_running_a_task_again_when_its_fault_write_fails():
    runs = 0
    storage = ControlledStorage()
    failure = RuntimeError("disk gone")

    async def throws(*_: Any) -> None:
        nonlocal runs
        runs += 1
        # The next commit is the step's fault write.
        if runs == 1:
            storage.failNextCommit(failure)
        raise RuntimeError("boom")

    Throws = oneStep("test.failed-fault", throws)
    opened = await openRoot([Throws], {"storage": storage})
    harness, root, reports = opened.harness, opened.root, opened.reports
    id = await start(root, Throws)
    harness.resume()
    assert await harness.closed == {"reason": "failed", "error": failure}
    assert runs == 1
    assert reports == [failure]
    await harness.close(context)

    # Reopening recovers the task as after a crash: still running, so its phase runs again.
    reopened = await openRoot([Throws], {"storage": storage.reopen()})
    reopened.harness.resume()
    assert (await reopened.harness.waitForTask(id, context))["state"]["outcome"] == {
        "status": "faulted",
        "error": {"message": "boom"},
    }
    assert runs == 2
    await reopened.harness.close(context)


async def test_scheduling_waits_for_harness_and_conversation_idleness_counting_blocked_work_and_ignoring_background_tasks():
    gates: dict[int, Callable[[], None]] = {}

    async def gatedRun(task: Any, runtime: Any, ctx: Any) -> None:
        future = asyncio.get_running_loop().create_future()
        gates[task["id"]] = lambda: future.set_result(None)
        await future
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Gated = oneStep("test.gated", gatedRun)
    opened = await openRoot([Gated])
    harness, root = opened.harness, opened.root
    await harness.waitForIdle(context)
    other = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    foreground = await start(root, Gated)
    background = await start(root, Gated, {"background": True})
    elsewhere = await start(other, Gated)
    # Work that has not started yet is live; cancelling a wait only rejects that wait.
    cancelled = AbortController()
    cancelledWait = harness.waitForIdle(withSignal(cancelled.signal))
    cancelled.abort(RuntimeError("stop waiting"))
    with pytest.raises(Exception, match="stop waiting"):
        await cancelledWait
    already = AbortController()
    already.abort(RuntimeError("already cancelled"))
    with pytest.raises(Exception, match="already cancelled"):
        await root.waitForIdle(withSignal(already.signal))
    harness.resume()
    await eventually(lambda: len(gates) == 3)
    rootIdle = root.waitForIdle(context)
    harnessIdle = harness.waitForIdle(context)
    gates[foreground]()
    await rootIdle
    assert await settled(harnessIdle) is False
    gates[elsewhere]()
    await harnessIdle
    assert (await harness.getTask(background, context))["state"]["status"] == "running"
    gates[background]()
    await harness.waitForTask(background, context)
    await harness.close(context)
    with pytest.raises(Exception, match="closed"):
        await harness.waitForIdle(context)
    with pytest.raises(Exception, match="closed"):
        await root.waitForIdle(context)


# #10546
async def test_scheduling_pages_the_newest_tasks_first_without_scanning_older_ones():
    async def idle(*_: Any) -> None:
        return None

    Idle = oneStep("test.idle", idle)
    opened = await openRoot([Idle])
    harness, root = opened.harness, opened.root
    ids = [await start(root, Idle) for _ in range(5)]
    first = await harness.commit(lambda tx: tx.scanTasks({"order": "descending"}, 2), context)
    assert [each["id"] for each in first["items"]] == [ids[4], ids[3]]
    second = await harness.commit(lambda tx: tx.scanTasks({}, 2, first["next"]), context)
    assert [each["id"] for each in second["items"]] == [ids[2], ids[1]]
    await harness.close(context)


async def test_scheduling_rejects_unknown_tasks_and_reports_terminal_tasks():
    async def quick(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Done = oneStep("test.quick", quick)
    opened = await openRoot([Done])
    harness, root = opened.harness, opened.root
    unknown = 999_999
    assert await harness.getTask(unknown, context) is None
    with pytest.raises(Exception, match="does not exist"):
        await harness.waitForTask(unknown, context)
    with pytest.raises(Exception, match="does not exist"):
        await harness.abortTask(unknown, context)
    id = await start(root, Done)
    harness.resume()
    await harness.waitForTask(id, context)
    toMatchObject(await harness.waitForTask(id, context), {"state": {"status": "terminal"}})
    assert await harness.abortTask(id, context) == "terminal"
    await harness.close(context)
    with pytest.raises(Exception, match="closed"):
        harness.resume()


async def test_scheduling_rejects_task_waits_cancelled_or_closed_while_queued_on_the_line_and_pending_waits_on_close():
    gate = deferred()
    Blocking = gated("test.wait-close", gate.promise)
    storage = ControlledStorage()
    opened = await openRoot([Blocking], {"storage": storage})
    harness, root = opened.harness, opened.root
    id = await start(root, Blocking)

    async def blocker(tx: Any) -> None:
        await tx.appendEntry(root.id, {"kind": "blocker"})

    # Hold the line with a commit, then queue waits behind it.
    held = storage.holdCommits()
    blocking = harness.commit(blocker, context)
    await held.entered
    controller = AbortController()
    cancelledWhileQueued = harness.waitForTask(id, withSignal(controller.signal))
    controller.abort(RuntimeError("wait cancelled"))
    held.release()
    await blocking
    with pytest.raises(Exception, match="wait cancelled"):
        await cancelledWhileQueued

    pending = harness.waitForTask(id, context)
    idle = harness.waitForIdle(context)
    conversationIdle = root.waitForIdle(context)
    await flush()
    heldAgain = storage.holdCommits()
    blockerAgain = harness.commit(blocker, context)
    await heldAgain.entered
    closedWhileQueued = harness.waitForTask(id, context)
    closing = harness.close(context)
    heldAgain.release()
    await blockerAgain
    with pytest.raises(Exception, match="closed"):
        await closedWhileQueued
    with pytest.raises(Exception, match="closed"):
        await pending
    with pytest.raises(Exception, match="closed"):
        await idle
    with pytest.raises(Exception, match="closed"):
        await conversationIdle
    gate.resolve()
    await closing


async def test_abort_rejects_the_runs_commits_and_memo_writes_after_the_mark_and_settles_through_a_fresh_abort_invocation():
    reached = deferred()
    errors: list[str] = []
    holder: dict[str, Any] = {}
    abortRuntimes: list[Any] = []

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        holder["runRuntime"] = runtime
        await runtime.memo("kept", 1, ctx)
        reached.resolve()
        # Keep working after the signal: every later write of this run must reject.
        try:
            await abortedBy(runtime.signal)
        except Exception:  # noqa: BLE001, S110 - the run goes on after its signal
            pass
        holder["memoRead"] = await runtime.memo("kept", context)
        try:
            await runtime.memo("late", 2, context)
        except Exception as error:  # noqa: BLE001
            errors.append(str(error))
        try:
            await runtime.commit(lambda tx, current: completed(None), context)
        except Exception as error:  # noqa: BLE001
            errors.append(str(error))

    async def abort(runtime: Any, ctx: Any) -> None:
        abortRuntimes.append(runtime)

        def change(tx: Any, current: Any) -> dict[str, Any]:
            assert current["abortRequested"] is True
            assert current["memos"] == {"kept": 1}
            return abortedWith("mark")

        await runtime.commit(change, ctx)

    Marked = oneStep("test.marked", run, abort)
    opened = await openRoot([Marked])
    harness, root = opened.harness, opened.root
    id = await start(root, Marked)
    harness.resume()
    await reached.promise
    assert await harness.abortTask(id, context) == "marked"
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "aborted", "reason": "mark"}
    assert holder["memoRead"] == 1
    assert errors == [f"Task {id} has a durable abort mark", f"Task {id} has a durable abort mark"]
    assert len(abortRuntimes) == 1
    assert abortRuntimes[0] is not holder["runRuntime"]
    await harness.close(context)


async def test_abort_starts_no_further_phase_after_a_mark_that_lands_during_a_phase_with_progress():
    reached = deferred()
    proceed = deferred()
    phases: list[str] = []

    async def one(_task: Any, runtime: Any, ctx: Any) -> None:
        phases.append("one")
        await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": {"phase": "two"}}, ctx)
        reached.resolve()
        # Ignores the signal and returns normally after the mark.
        await proceed.promise

    async def two(*_: Any) -> None:
        phases.append("two")

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        phases.append("abort")
        await runtime.commit(lambda tx, current: abortedWith("boundary"), ctx)

    Two = defineTask(
        {
            "name": "test.mark-boundary",
            "version": 1,
            "initial": lambda input: {"phase": "one"},
            "phases": {"one": one, "two": two},
            "abort": abort,
        }
    )
    opened = await openRoot([Two])
    harness, root = opened.harness, opened.root
    id = await root.commit(lambda tx: tx.createTask(Two, None, {"ownership": {"kind": "conversation"}}), context)
    harness.resume()
    await reached.promise
    aborting = await markDurably(harness, id)
    proceed.resolve()
    assert await aborting == "marked"
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "aborted", "reason": "boundary"}
    assert phases == ["one", "abort"]
    await harness.close(context)


async def test_abort_signals_and_joins_the_run_before_returning_then_runs_the_abort_handler():
    reached = deferred()
    ended = {"runEnded": False}

    async def signalled(_task: Any, runtime: Any, ctx: Any) -> None:
        reached.resolve()
        try:
            await abortedBy(runtime.signal)
        finally:
            ended["runEnded"] = True

    Signalled = oneStep("test.signalled", signalled)
    opened = await openRoot([Signalled])
    harness, root = opened.harness, opened.root
    id = await start(root, Signalled)
    harness.resume()
    await reached.promise
    assert await harness.abortTask(id, context) == "marked"
    assert ended["runEnded"] is True
    assert await harness.abortTask(id, context) == "marked"
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "aborted", "reason": "test"}
    await harness.close(context)


async def test_abort_aborts_waiting_work_before_its_wait_ends_and_faults_abort_handlers_that_throw_or_settle_nothing():
    gate = deferred()
    First = gated("test.dependency", gate.promise)
    holder: dict[str, Any] = {}

    async def wait(_task: Any, runtime: Any, ctx: Any) -> None:
        checkpoint = {"phase": "run"}
        await runtime.commit(
            lambda tx, current: {"status": "waiting", "checkpoint": checkpoint, "on": [holder["first"]], "policy": "allSettled"},
            ctx,
        )

    async def lazyAbort(runtime: Any, ctx: Any) -> None:
        return None

    async def throwingAbort(runtime: Any, ctx: Any) -> None:
        raise RuntimeError("abort failed")

    Lazy = oneStep("test.lazy-abort", wait, lazyAbort)
    Throwing = oneStep("test.throwing-abort", wait, throwingAbort)
    opened = await openRoot([First, Lazy, Throwing])
    harness, root = opened.harness, opened.root
    holder["first"] = await start(root, First)
    lazy = await start(root, Lazy)
    throwing = await start(root, Throwing)
    harness.resume()

    async def throwingWaiting() -> bool:
        return (await harness.getTask(throwing, context))["state"]["status"] == "waiting"

    async def lazyWaiting() -> bool:
        return (await harness.getTask(lazy, context))["state"]["status"] == "waiting"

    await eventually(throwingWaiting)
    await eventually(lazyWaiting)
    await harness.abortTask(lazy, context)
    await harness.abortTask(throwing, context)
    assert (await harness.waitForTask(lazy, context))["state"]["outcome"] == {
        "status": "faulted",
        "error": {"message": f"Abort handler of task {lazy} returned without a terminal outcome"},
    }
    assert (await harness.waitForTask(throwing, context))["state"]["outcome"] == {
        "status": "faulted",
        "error": {"message": "abort failed"},
    }
    assert (await harness.getTask(holder["first"], context))["state"]["status"] == "running"
    gate.resolve()
    await harness.waitForTask(holder["first"], context)
    await harness.close(context)


async def test_abort_does_not_signal_a_running_abort_handler_when_aborted_again_and_a_cancelled_caller_leaves_the_mark_durable():
    reached = deferred()
    proceed = deferred()
    runRelease = deferred()
    state: dict[str, Any] = {"abortSignalled": None, "aborts": 0}

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        # Ignores the signal, so the first caller is still joining when it gives up.
        await runRelease.promise

    async def abort(runtime: Any, ctx: Any) -> None:
        state["aborts"] += 1
        reached.resolve()
        await proceed.promise
        state["abortSignalled"] = runtime.signal.aborted
        await runtime.commit(lambda tx, current: abortedWith("once"), ctx)

    Run = oneStep("test.abort-again", run, abort)
    opened = await openRoot([Run])
    harness, root = opened.harness, opened.root
    id = await start(root, Run)
    harness.resume()
    await flush()
    # This caller gives up while joining; the mark and the abort invocation are unaffected.
    controller = AbortController()
    cancelled = harness.abortTask(id, withSignal(controller.signal))
    while not (await harness.getTask(id, context))["abortRequested"]:
        await flush()
    controller.abort(RuntimeError("caller gave up"))
    with pytest.raises(Exception, match="caller gave up"):
        await cancelled
    runRelease.resolve()
    await reached.promise
    assert await harness.abortTask(id, context) == "marked"
    proceed.resolve()
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "aborted", "reason": "once"}
    assert state["abortSignalled"] is False
    assert state["aborts"] == 1
    await harness.close(context)


async def test_abort_keeps_a_terminal_outcome_committed_by_an_abort_handler_that_throws_afterwards():
    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        await abortedBy(runtime.signal)

    async def abort(runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: abortedWith("done"), ctx)
        raise RuntimeError("after terminal")

    Run = oneStep("test.abort-then-throw", run, abort)
    opened = await openRoot([Run])
    harness, root = opened.harness, opened.root
    id = await start(root, Run)
    harness.resume()
    await flush()
    await harness.abortTask(id, context)
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "aborted", "reason": "done"}
    await harness.close(context)


async def test_abort_never_starts_phase_one_when_the_abort_lands_while_the_reservation_settles():
    ran = {"value": False}

    async def reservedRun(_task: Any, runtime: Any, ctx: Any) -> None:
        ran["value"] = True
        await abortedBy(runtime.signal)

    Reserved = oneStep("test.reserved", reservedRun)
    storage = ControlledStorage()
    opened = await openRoot([Reserved], {"storage": storage})
    harness, root = opened.harness, opened.root
    id = await start(root, Reserved)
    held = storage.holdCommits()
    harness.resume()
    await held.entered
    # The reservation commit is in storage; the abort mark commit queues behind it.
    aborting = harness.abortTask(id, context)
    held.release()
    assert await aborting == "marked"
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "aborted", "reason": "test"}
    assert ran["value"] is False
    await harness.close(context)


async def test_abort_lets_an_abort_mark_win_over_a_fault_that_races_it():
    storage = ControlledStorage()
    holder: dict[str, Any] = {}

    async def racing(task: Any, runtime: Any, ctx: Any) -> None:
        holder["held"] = storage.holdCommits()
        holder["marking"] = holder["harness"].abortTask(task["id"], context)
        await holder["held"].entered
        # The mark is in storage but not committed when the handler throws, so the fault commit queues behind it.
        raise RuntimeError("would fault")

    Racing = oneStep("test.racing-fault", racing)
    opened = await openRoot([Racing], {"storage": storage})
    harness, root = opened.harness, opened.root
    holder["harness"] = harness
    id = await start(root, Racing)
    harness.resume()
    await eventually(lambda: "held" in holder)
    await holder["held"].entered
    await flush()
    holder["held"].release()
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "aborted", "reason": "test"}
    assert await holder["marking"] == "marked"
    await harness.close(context)


async def test_close_stops_without_outcomes_and_starts_no_fresh_phase_or_abort_invocation_while_closing():
    reached = deferred()
    proceed = deferred()
    phases: list[str] = []
    abortRan = {"value": False}

    async def one(_task: Any, runtime: Any, ctx: Any) -> None:
        phases.append("one")
        await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": {"phase": "two"}}, ctx)
        reached.resolve()
        # Ignores signals and returns normally once released; the closing rule wins over the next phase.
        await proceed.promise

    async def two(*_: Any) -> None:
        phases.append("two")

    async def abort(*_: Any) -> None:
        abortRan["value"] = True

    Two = defineTask(
        {
            "name": "test.two",
            "version": 1,
            "initial": lambda input: {"phase": "one"},
            "phases": {"one": one, "two": two},
            "abort": abort,
        }
    )
    storage = ControlledStorage()
    opened = await openRoot([Two], {"storage": storage})
    harness, root = opened.harness, opened.root
    id = await root.commit(lambda tx: tx.createTask(Two, None, {"ownership": {"kind": "conversation"}}), context)
    harness.resume()
    await reached.promise
    aborting = await markDurably(harness, id)
    record = await harness.getTask(id, context)
    commits = len(storage.commits)
    closing = harness.close(context)
    proceed.resolve()
    await closing
    assert await aborting == "marked"
    assert len(storage.commits) == commits
    assert phases == ["one"]
    assert abortRan["value"] is False
    toMatchObject(record, {"abortRequested": True, "state": {"status": "running", "checkpoint": {"phase": "two"}}})
    with pytest.raises(Exception, match="closed"):
        await harness.commit(lambda tx: None, context)


async def test_close_seals_admission_before_signalling_handlers_and_stops_watches_before_joining_them():
    Notes = defineDoc({"kind": "test.close-notes", "version": 1, "scope": "session", "initial": lambda: {"text": ""}})
    reached = deferred()
    holder: dict[str, Any] = {}

    async def stubborn(_task: Any, runtime: Any, ctx: Any) -> None:
        # Uses a context close does not cancel, then waits for the watch to close.
        watch = await runtime.watchDoc(Notes, context)

        def onAbort() -> None:
            holder["fromListener"] = consume(holder["harness"].commit(lambda tx: None, context))

        runtime.signal.addEventListener("abort", onAbort)
        reached.resolve()
        holder["watchEnd"] = await watch.closed

    Stubborn = oneStep("test.stubborn", stubborn)
    opened = await openRoot([Stubborn])
    harness, root = opened.harness, opened.root
    holder["harness"] = harness

    async def setText(tx: Any) -> None:
        (await tx.doc(Notes))["text"] = "x"

    await harness.commit(setText, context)
    await start(root, Stubborn)
    harness.resume()
    await reached.promise
    await harness.close(context)
    with pytest.raises(Exception, match="closed"):
        await holder["fromListener"]
    assert holder["watchEnd"] == {"reason": "session_closed"}


async def test_close_writes_no_fault_when_close_seals_while_the_step_after_a_failed_phase_is_queued():
    storage = ControlledStorage()
    holder: dict[str, Any] = {}

    async def throws(_task: Any, runtime: Any, ctx: Any) -> None:
        holder["held"] = storage.holdCommits()

        async def blocker(tx: Any) -> None:
            await tx.appendEntry(runtime.conversationId, {"kind": "blocker"})

        consume(holder["harness"].commit(blocker, context))
        raise RuntimeError("would fault")

    Throws = oneStep("test.close-fault", throws)
    opened = await openRoot([Throws], {"storage": storage})
    harness, root = opened.harness, opened.root
    holder["harness"] = harness
    id = await start(root, Throws)
    harness.resume()
    await eventually(lambda: "held" in holder)
    await holder["held"].entered
    await flush()
    closing = harness.close(context)
    holder["held"].release()
    await closing
    taskWrites = [
        write for writes in storage.commits for write in writes if write["type"] == "task" and write["value"]["id"] == id
    ]
    assert [write["value"]["state"]["status"] for write in taskWrites] == ["pending", "running"]


async def test_close_starts_no_abort_handler_whose_reservation_settles_while_closing():
    ran = {"value": False}

    async def nothingRun(*_: Any) -> None:
        return None

    async def abort(runtime: Any, ctx: Any) -> None:
        ran["value"] = True

    Marked = oneStep("test.close-abort-reservation", nothingRun, abort)
    storage = ControlledStorage()
    opened = await openRoot([Marked], {"storage": storage})
    harness, root = opened.harness, opened.root
    id = await start(root, Marked)
    assert await harness.abortTask(id, context) == "marked"
    held = storage.holdCommits()
    harness.resume()
    await held.entered
    closing = harness.close(context)
    held.release()
    await closing
    assert ran["value"] is False
    writes = [write for write in storage.commits[-1] if write["type"] == "task"]
    toMatchObject(writes, [{"value": {"id": id, "abortRequested": True, "state": {"status": "running"}}}])


async def test_close_rejects_a_runtime_commit_that_was_queued_on_the_line_when_close_sealed_it():
    storage = ControlledStorage()
    holder: dict[str, Any] = {}
    proceed = deferred()

    async def queuedRun(_task: Any, runtime: Any, ctx: Any) -> None:
        holder["held"] = storage.holdCommits()

        async def blocker(tx: Any) -> None:
            await tx.appendEntry(runtime.conversationId, {"kind": "blocker"})

        consume(holder["harness"].commit(blocker, context))
        holder["queued"] = asyncio.ensure_future(catching(runtime.commit(lambda tx, current: completed(None), context)))
        # Ignores the close signal, so the invocation is still alive when the queued commit reaches the line.
        await proceed.promise

    Queued = oneStep("test.close-queued-commit", queuedRun)
    opened = await openRoot([Queued], {"storage": storage})
    harness, root = opened.harness, opened.root
    holder["harness"] = harness
    id = await start(root, Queued)
    harness.resume()
    await eventually(lambda: "held" in holder)
    await holder["held"].entered
    await flush()
    closing = harness.close(context)
    holder["held"].release()
    assert "Harness is closed" in str(await holder["queued"])
    proceed.resolve()
    await closing
    taskWrites = [
        write for writes in storage.commits for write in writes if write["type"] == "task" and write["value"]["id"] == id
    ]
    assert len(taskWrites) == 2


async def test_close_starts_no_next_phase_when_close_seals_during_a_step_that_decided_to_continue():
    phases: list[str] = []
    registry = createRegistry()
    holder: dict[str, Any] = {"closeOnSnapshot": False}

    # The step refreshes the snapshot after progress, inside its line callback and after its closing check.
    def snapshot() -> Any:
        if holder["closeOnSnapshot"]:
            holder["closeOnSnapshot"] = False
            holder["closing"] = holder["harness"].close(context)
        return registry.snapshot()

    reader = SimpleNamespace(snapshot=snapshot, subscribe=lambda listener: registry.subscribe(listener))

    async def one(_task: Any, runtime: Any, ctx: Any) -> None:
        phases.append("one")
        await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": {"phase": "two"}}, ctx)
        holder["closeOnSnapshot"] = True

    async def two(*_: Any) -> None:
        phases.append("two")

    Two = defineTask(
        {
            "name": "test.close-in-step",
            "version": 1,
            "initial": lambda input: {"phase": "one"},
            "phases": {"one": one, "two": two},
            "abort": noAbort,
        }
    )
    addTask(registry, Two)
    harness = await Harness.open(MemoryStorage(), {"models": createModels(), "registry": reader}, context)
    holder["harness"] = harness
    root = await harness.root(context)
    await root.commit(lambda tx: tx.createTask(Two, None, {"ownership": {"kind": "conversation"}}), context)
    harness.resume()
    await eventually(lambda: "closing" in holder)
    await holder["closing"]
    assert phases == ["one"]


async def test_close_joins_a_reservation_that_settles_while_closing_without_starting_its_handler():
    ran = {"value": False}

    async def neverRun(*_: Any) -> None:
        ran["value"] = True

    Never = oneStep("test.close-reservation", neverRun)
    storage = ControlledStorage()
    opened = await openRoot([Never], {"storage": storage})
    harness, root = opened.harness, opened.root
    id = await start(root, Never)
    held = storage.holdCommits()
    harness.resume()
    await held.entered
    closing = harness.close(context)
    held.release()
    await closing
    assert ran["value"] is False
    writes = [write for write in storage.commits[-1] if write["type"] == "task"]
    toMatchObject(writes, [{"type": "task", "value": {"id": id, "state": {"status": "running"}}}])
