"""pi durable's ``test/harness-structured.test.ts``."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import (
    GenerationTask,
    LiveDoc,
    MemoryStorage,
    ToolResultEntry,
    createSession,
    defineDoc,
    defineTask,
    defineTool,
    watchEvents,
)
from misaka.durable.errors import SessionFailed
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from tests.chat_support import (
    WithOverrides,
    allEntries,
    chatSetup,
    invalidFinalStream,
    openChat,
    waitFor,
)
from tests.durable_support import ControlledStorage, context, toMatchObject
from tests.harness_support import addHooks, addTask, addTool
from tests.task_support import aborted, deferred, openTasks, race, settled

EMPTY_OBJECT = {"type": "object", "properties": {}}

# ─── A scriptable task ──────────────────────────────────────────────────────

behaviors: dict[str, dict[str, Callable[..., Any]]] = {}
gates: dict[str, Any] = {}
log: list[str] = []
"""Handler starts and ends in order, such as ``run:a``, ``abort:a``."""


def gate(name: str) -> Any:
    found = gates.get(name)
    if found is None:
        found = deferred()
        gates[name] = found
    return found


def openGate(name: str, ending: str = "completed") -> None:
    gate(name).resolve(ending)


def script(name: str, behavior: dict[str, Callable[..., Any]]) -> None:
    behaviors[name] = behavior


def end(ending: str, name: str) -> dict[str, Any]:
    if ending == "completed":
        return {"status": "terminal", "outcome": {"status": "completed", "result": name}}
    return {"status": "terminal", "outcome": {"status": "failed", "error": {"message": f"{name} failed"}}}


async def defaultRun(runtime: Any, ctx: Any, name: str) -> None:
    """Wait for the gate or the abort signal, then end as the gate says."""
    ending = await race(gate(name).promise, aborted(runtime.signal))
    if ending == "throw":
        raise RuntimeError(f"{name} threw")
    await runtime.commit(lambda tx, current: end(ending, name), ctx)


async def nodeRun(task: Any, runtime: Any, ctx: Any) -> None:
    name = task["input"]["name"]
    log.append(f"run:{name}")
    behavior = behaviors.get(name, {}).get("run")
    if behavior is None:
        await defaultRun(runtime, ctx, name)
    else:
        await behavior(runtime, ctx)


async def nodeResume(task: Any, runtime: Any, ctx: Any) -> None:
    name = task["input"]["name"]
    log.append(f"resume:{name}")
    behavior = behaviors.get(name, {}).get("resume")
    if behavior is not None:
        await behavior(runtime, ctx, task["state"]["checkpoint"]["round"])
        return
    await runtime.commit(lambda tx, current: end("completed", name), ctx)


async def nodeAbort(task: Any, runtime: Any, ctx: Any) -> None:
    name = task["input"]["name"]
    log.append(f"abort:{name}")
    behavior = behaviors.get(name, {}).get("abort")
    if behavior is not None:
        await behavior(runtime, ctx)
        return
    await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted", "result": name}}, ctx)


# A task that runs its name's behavior.
Node = defineTask(
    {
        "name": "test.node",
        "version": 1,
        "initial": lambda input: {"phase": "run"},
        "phases": {"run": nodeRun, "resume": nodeResume},
        "abort": nodeAbort,
    }
)


async def nothing(*_: Any) -> None:
    return None


# Never registered: aborting it can only orphan it.
Unregistered = defineTask(
    {
        "name": "test.unregistered",
        "version": 1,
        "initial": lambda input: {"phase": "run"},
        "phases": {"run": nothing},
        "abort": nothing,
    }
)

TaskNotes = defineDoc({"kind": "test.task-notes", "version": 1, "scope": "task", "initial": lambda: {"text": ""}})

OWN_CONVERSATION = {"ownership": {"kind": "conversation"}}


def owned(owner: int) -> dict[str, Any]:
    return {"ownership": {"kind": "task", "taskId": owner}}


def spawn(tx: Any, owner: int, name: str) -> Any:
    """A child task named ``name``, owned by ``owner``."""
    return tx.createTask(Node, {"name": name}, owned(owner))


def waitOn(on: list[int], policy: str, round: int = 1) -> dict[str, Any]:
    """Wait on ``on``, resuming in round ``round``."""
    return {"status": "waiting", "checkpoint": {"phase": "resume", "round": round}, "on": on, "policy": policy}


def start(conversation: Any, name: str, options: dict[str, Any] | None = None) -> Any:
    return conversation.commit(
        lambda tx: tx.createTask(Node, {"name": name}, options if options is not None else OWN_CONVERSATION), context
    )


async def openNodes(storage: Any = None) -> SimpleNamespace:
    harness, registry, reports = await openTasks(storage if storage is not None else MemoryStorage(), [Node])
    root = await harness.root(context)
    harness.resume()
    return SimpleNamespace(harness=harness, root=root, registry=registry, reports=reports)


async def state(harness: Any, id: int) -> dict[str, Any]:
    return (await harness.getTask(id, context))["state"]  # type: ignore[no-any-return]


async def outcomeOf(harness: Any, id: int) -> str:
    return (await harness.waitForTask(id, context))["state"]["outcome"]["status"]  # type: ignore[no-any-return]


async def until(check: Callable[[], Any]) -> None:
    await waitFor(check)


def hasStatus(harness: Any, id: int, status: str) -> Callable[[], Any]:
    async def check() -> bool:
        return (await state(harness, id))["status"] == status

    return check


def consume(future: Any) -> Any:
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return future


@pytest.fixture
def sqlitePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-structured-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def reset() -> Iterator[None]:
    yield
    behaviors.clear()
    gates.clear()
    log.clear()


async def slowAbort(runtime: Any, ctx: Any, name: str) -> None:
    await gate(f"abort.{name}").promise
    await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, ctx)


def slowAbortOf(name: str) -> Callable[..., Any]:
    async def abort(runtime: Any, ctx: Any) -> None:
        await slowAbort(runtime, ctx, name)

    return abort


# ─── Ownership ──────────────────────────────────────────────────────────────


async def test_ownership_creates_a_child_in_its_owners_conversation_with_its_owner_recorded():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    holder: dict[str, Any] = {}

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["child"] = await spawn(tx, runtime.taskId, "child")
            return waitOn([holder["child"]], "allSettled")

        await runtime.commit(change, ctx)

    script("parent", {"run": parentRun})
    parent = await start(root, "parent")
    await until(lambda: "child" in holder)
    record = await harness.getTask(holder["child"], context)
    toMatchObject(record, {"owner": parent, "conversationId": root.id, "background": False})
    assert (await harness.getTask(parent, context)).get("owner") is None
    openGate("child")
    assert await outcomeOf(harness, parent) == "completed"
    await harness.close(context)


async def test_ownership_rejects_no_ownership_a_missing_owner_a_child_in_another_conversation_and_a_background_child():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    parent = await start(root, "parent")
    other = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)

    def create(options: dict[str, Any]) -> Any:
        return root.commit(lambda tx: tx.createTask(Node, {"name": "x"}, options), context)

    with pytest.raises(Exception):  # noqa: B017
        await create({})
    with pytest.raises(Exception, match="does not exist"):
        await create(owned(999_999))
    with pytest.raises(Exception, match="owner's conversation"):
        await create({**owned(parent), "conversationId": other.id})
    with pytest.raises(Exception, match="cannot be background"):
        await create({**owned(parent), "background": True})
    # The owner's conversation is the default, even from a commit bound to another conversation.
    child = await other.commit(lambda tx: spawn(tx, parent, "child"), context)
    assert (await harness.getTask(child, context))["conversationId"] == root.id
    openGate("parent")
    openGate("child")
    await harness.waitForTask(parent, context)
    await harness.close(context)


async def test_ownership_rejects_new_owned_work_below_an_owner_that_is_completing_terminal_or_abort_marked():
    opened = await openNodes()
    harness, root = opened.harness, opened.root

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            await spawn(tx, runtime.taskId, "child")
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    script("parent", {"run": parentRun})
    parent = await start(root, "parent")
    await until(hasStatus(harness, parent, "completing"))

    def createChild() -> Any:
        return root.commit(lambda tx: spawn(tx, parent, "late"), context)

    def createConversation() -> Any:
        return root.commit(lambda tx: tx.createConversation({"ownership": {"kind": "task", "taskId": parent}}), context)

    with pytest.raises(Exception, match="is completing"):
        await createChild()
    with pytest.raises(Exception, match="is completing"):
        await createConversation()
    openGate("child")
    await harness.waitForTask(parent, context)
    with pytest.raises(Exception, match="is terminal"):
        await createChild()
    with pytest.raises(Exception, match="is terminal"):
        await createConversation()

    script("slow", {"abort": slowAbortOf("slow")})
    slow = await start(root, "slow")
    await until(lambda: "run:slow" in log)
    await harness.abortTask(slow, context)
    with pytest.raises(Exception, match="is abort-marked"):
        await root.commit(lambda tx: spawn(tx, slow, "late"), context)
    openGate("abort.slow")
    assert await outcomeOf(harness, slow) == "aborted"
    await harness.close(context)


async def test_ownership_cannot_create_a_child_in_its_finishing_commit_but_work_it_starts_in_an_owned_conversation_holds_it():
    opened = await openNodes()
    harness, root = opened.harness, opened.root

    async def eagerRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            await spawn(tx, runtime.taskId, "never")
            return end("completed", "eager")

        await runtime.commit(change, ctx)

    script("eager", {"run": eagerRun})
    eager = await start(root, "eager")
    outcome = (await harness.waitForTask(eager, context))["state"]["outcome"]
    assert outcome["status"] == "faulted"
    assert "is completing" in outcome["error"]["message"]
    # The rejected commit wrote nothing.
    nodes = await harness.commit(lambda tx: tx.scanTasks({"kind": "test.node"}, 20), context)
    assert [task["input"]["name"] for task in nodes["items"]] == ["eager"]

    holder: dict[str, Any] = {}

    async def hostRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["child"] = (await tx.createConversation({"ownership": {"kind": "task", "taskId": runtime.taskId}}))["id"]
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    async def hostResume(runtime: Any, ctx: Any, round: int) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            await tx.createTask(Node, {"name": "inner"}, {**OWN_CONVERSATION, "conversationId": holder["child"]})
            return end("completed", "host")

        await runtime.commit(change, ctx)

    script("host", {"run": hostRun, "resume": hostResume})
    host = await start(root, "host")
    await until(hasStatus(harness, host, "completing"))
    assert await settled(harness.waitForTask(host, context)) is False
    openGate("inner")
    assert await outcomeOf(harness, host) == "completed"
    await harness.close(context)


# ─── Waiting ────────────────────────────────────────────────────────────────


def parentOf(parent: str, children: list[str], policy: str) -> dict[str, Any]:
    """Script ``parent`` to spawn ``children`` in one commit and wait on them with ``policy``; it records their
    outcomes."""
    found: dict[str, Any] = {"ids": [], "outcomes": []}

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            for name in children:
                found["ids"].append(await spawn(tx, runtime.taskId, name))
            return waitOn(found["ids"], policy)

        await runtime.commit(change, ctx)

    async def parentResume(runtime: Any, ctx: Any, round: int) -> None:
        found["outcomes"] = [outcome["status"] for outcome in await runtime.outcomes(found["ids"], ctx)]
        await runtime.commit(lambda tx, current: end("completed", parent), ctx)

    script(parent, {"run": parentRun, "resume": parentResume})
    return found


async def test_waiting_resumes_once_every_awaited_task_is_terminal_and_reads_their_outcomes_in_order_all_settled():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    found: dict[str, Any] = {"ids": [], "outcomes": []}

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            for name in ("ok", "fails", "throws", "aborted"):
                found["ids"].append(await spawn(tx, runtime.taskId, name))
            found["ids"].append(await tx.createTask(Unregistered, {"name": "orphan"}, owned(runtime.taskId)))
            return waitOn(found["ids"], "allSettled")

        await runtime.commit(change, ctx)

    async def parentResume(runtime: Any, ctx: Any, round: int) -> None:
        found["outcomes"] = [outcome["status"] for outcome in await runtime.outcomes(found["ids"], ctx)]
        await runtime.commit(lambda tx, current: end("completed", "parent"), ctx)

    script("parent", {"run": parentRun, "resume": parentResume})
    parent = await start(root, "parent")
    await until(lambda: len(found["ids"]) == 5)
    await until(hasStatus(harness, parent, "waiting"))
    openGate("ok")
    openGate("fails", "failed")
    openGate("throws", "throw")
    await harness.abortTask(found["ids"][3], context)
    assert await harness.abortTask(found["ids"][4], context) == "marked"
    assert await outcomeOf(harness, parent) == "completed"
    assert found["outcomes"] == ["completed", "failed", "faulted", "aborted", "orphaned"]
    # allSettled never marks siblings.
    assert [line for line in log if line.startswith("abort:")] == ["abort:aborted"]
    await harness.close(context)


@pytest.mark.parametrize("ending", ["failed", "throw"])
async def test_waiting_fails_fast_when_a_child_ends_failed_or_faulted_its_live_siblings_are_aborted_the_parent_is_not(
    ending,
):
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    found = parentOf("checkout", ["p1", "p2", "p3", "p4"], "failFast")
    parent = await start(root, "checkout")
    await until(hasStatus(harness, parent, "waiting"))
    openGate("p2", ending)
    assert await outcomeOf(harness, parent) == "completed"
    assert found["outcomes"] == ["aborted", "faulted" if ending == "throw" else "failed", "aborted", "aborted"]
    assert (await harness.getTask(parent, context))["abortRequested"] is False
    assert sorted(line for line in log if line.startswith("abort:")) == ["abort:p1", "abort:p3", "abort:p4"]
    await harness.close(context)


async def test_waiting_fails_fast_on_a_held_failure_before_the_failing_child_drains():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    found: dict[str, Any] = {"ids": []}
    holder: dict[str, Any] = {}

    async def p1Run(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            await spawn(tx, runtime.taskId, "grandchild")
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    async def p1Resume(runtime: Any, ctx: Any, round: int) -> None:
        await runtime.commit(lambda tx, current: end("failed", "p1"), ctx)

    script("p1", {"run": p1Run, "resume": p1Resume})
    script("grandchild", {"abort": slowAbortOf("grandchild")})

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            for name in ("p1", "p2"):
                found["ids"].append(await spawn(tx, runtime.taskId, name))
            holder["outside"] = await spawn(tx, runtime.taskId, "outside")
            return waitOn(found["ids"], "failFast")

        await runtime.commit(change, ctx)

    script("parent", {"run": parentRun})
    parent = await start(root, "parent")
    await until(lambda: len(found["ids"]) == 2)
    # p1 holds ``failed`` while its grandchild's abort handler runs; p2 is already aborted.
    assert await outcomeOf(harness, found["ids"][1]) == "aborted"
    toMatchObject(await state(harness, found["ids"][0]), {"status": "completing", "outcome": {"status": "failed"}})
    assert (await state(harness, parent))["status"] == "waiting"
    openGate("abort.grandchild")
    await until(lambda: "resume:parent" in log)

    # Only the other tasks in ``on`` are marked: not the failed one, not the parent, not a child outside ``on``.
    async def marked(id: int) -> bool:
        return (await harness.getTask(id, context))["abortRequested"]  # type: ignore[no-any-return]

    assert await outcomeOf(harness, found["ids"][0]) == "failed"
    assert [await marked(found["ids"][0]), await marked(found["ids"][1]), await marked(holder["outside"])] == [
        False,
        True,
        False,
    ]
    assert await marked(parent) is False
    assert (await state(harness, holder["outside"]))["status"] == "running"
    # Finished, the parent holds for the child outside ``on``.
    await until(hasStatus(harness, parent, "completing"))
    openGate("outside")
    assert await outcomeOf(harness, parent) == "completed"
    await harness.close(context)


async def test_waiting_waits_with_all_settled_on_tasks_it_does_not_own_including_already_terminal_ones():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    done = await start(root, "done")
    openGate("done")
    await harness.waitForTask(done, context)
    live = await start(root, "live")
    holder: dict[str, Any] = {"outcomes": []}

    async def parentRun(runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: waitOn([done, live], "allSettled"), ctx)

    async def parentResume(runtime: Any, ctx: Any, round: int) -> None:
        holder["outcomes"] = [outcome["status"] for outcome in await runtime.outcomes([done, live], ctx)]
        await runtime.commit(lambda tx, current: end("completed", "parent"), ctx)

    script("parent", {"run": parentRun, "resume": parentResume})
    parent = await start(root, "parent")
    await until(hasStatus(harness, parent, "waiting"))
    # A task it does not own is not its work: the parent may finish while it lives, but here it waits for it.
    openGate("live", "failed")
    assert await outcomeOf(harness, parent) == "completed"
    assert holder["outcomes"] == ["completed", "failed"]
    await harness.close(context)


async def test_waiting_rejects_waits_on_itself_its_owner_a_missing_task_and_fail_fast_on_a_task_it_does_not_own():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    other = await start(root, "other")
    cases: list[tuple[str, Callable[[Any], list[int]], str, str]] = [
        ("self", lambda runtime: [runtime.taskId], "allSettled", "is a wait cycle"),
        ("missing", lambda runtime: [999_999], "allSettled", "does not exist"),
        ("foreign", lambda runtime: [other], "failFast", "only on tasks it owns"),
    ]

    def waiter(on: Callable[[Any], list[int]], policy: str) -> Callable[..., Any]:
        async def run(runtime: Any, ctx: Any) -> None:
            await runtime.commit(lambda tx, current: waitOn(on(runtime), policy), ctx)

        return run

    for name, on, policy, message in cases:
        script(name, {"run": waiter(on, policy)})
        id = await start(root, name)
        outcome = (await harness.waitForTask(id, context))["state"]["outcome"]
        assert outcome["status"] == "faulted"
        assert message in outcome["error"]["message"]
    # A child waiting on its owner could never resume.
    holder: dict[str, Any] = {}

    async def childRun(runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: waitOn([holder["parent"]], "allSettled"), ctx)

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["child"] = await spawn(tx, runtime.taskId, "child")
            return waitOn([holder["child"]], "allSettled")

        await runtime.commit(change, ctx)

    script("child", {"run": childRun})
    script("parent", {"run": parentRun})
    holder["parent"] = await start(root, "parent")
    assert (await harness.waitForTask(holder["parent"], context))["state"]["outcome"]["status"] == "completed"
    outcome = (await harness.waitForTask(holder["child"], context))["state"]["outcome"]
    assert outcome["status"] == "faulted"
    assert "is a wait cycle" in outcome["error"]["message"]
    openGate("other")
    await harness.close(context)


def waitsOn(on: Callable[[], list[int]]) -> Callable[..., Any]:
    """A run behavior that waits with ``allSettled`` on the tasks ``on`` returns when it runs."""

    async def run(runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: waitOn(on(), "allSettled"), ctx)

    return run


# #10411; this test and the next two are from #10533 by @odysseus0.
async def test_waiting_rejects_the_wait_that_closes_a_cycle():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    ids: dict[str, int] = {}
    script("a", {"run": waitsOn(lambda: [ids["b"]])})
    script("b", {"run": waitsOn(lambda: [ids["a"]])})

    async def create(tx: Any) -> None:
        ids["a"] = await tx.createTask(Node, {"name": "a"}, OWN_CONVERSATION)
        ids["b"] = await tx.createTask(Node, {"name": "b"}, OWN_CONVERSATION)

    await root.commit(create, context)
    # a -> b and b -> a are waits, and whichever comes second closes the cycle.
    outcomes = await asyncio.gather(outcomeOf(harness, ids["a"]), outcomeOf(harness, ids["b"]))
    assert sorted(outcomes) == ["completed", "faulted"]
    await harness.close(context)


# #10411
async def test_waiting_rejects_the_wait_that_closes_a_cycle_through_a_held_owner():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    ids: dict[str, int] = {}
    child = spawnAndFinish("held", "child")
    script("child", {"run": waitsOn(lambda: [ids["other"]])})
    script("other", {"run": waitsOn(lambda: [ids["held"]])})

    async def create(tx: Any) -> None:
        ids["held"] = await tx.createTask(Node, {"name": "held"}, OWN_CONVERSATION)
        ids["other"] = await tx.createTask(Node, {"name": "other"}, OWN_CONVERSATION)

    await root.commit(create, context)
    # held -> child is a hold; child -> other -> held are waits, and whichever wait comes second closes the cycle.
    assert await outcomeOf(harness, ids["held"]) == "completed"
    outcomes = await asyncio.gather(outcomeOf(harness, child["id"]), outcomeOf(harness, ids["other"]))
    assert sorted(outcomes) == ["completed", "faulted"]
    await harness.close(context)


# #10411
async def test_waiting_rejects_a_cycle_through_holds_two_levels_deep():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    ids: dict[str, int] = {}
    child = spawnAndFinish("held", "child")
    grandchild = spawnAndFinish("child", "grandchild")
    script("grandchild", {"run": waitsOn(lambda: [ids["other"]])})
    script("other", {"run": waitsOn(lambda: [ids["held"]])})

    async def create(tx: Any) -> None:
        ids["held"] = await tx.createTask(Node, {"name": "held"}, OWN_CONVERSATION)
        ids["other"] = await tx.createTask(Node, {"name": "other"}, OWN_CONVERSATION)

    await root.commit(create, context)
    # held -> child -> grandchild are holds; grandchild -> other -> held are waits.
    await until(lambda: "id" in grandchild)
    outcomes = await asyncio.gather(outcomeOf(harness, grandchild["id"]), outcomeOf(harness, ids["other"]))
    assert sorted(outcomes) == ["completed", "faulted"]
    assert await outcomeOf(harness, child["id"]) == "completed"
    await harness.close(context)


# #10411
async def test_waiting_lets_a_background_task_wait_on_the_owner_of_its_conversation():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    ids: dict[str, int] = {}
    script("background", {"run": waitsOn(lambda: [ids["owner"]])})

    async def create(tx: Any) -> int:
        ids["owner"] = await tx.createTask(Node, {"name": "owner"}, OWN_CONVERSATION)
        conversation = await tx.createConversation({"ownership": {"kind": "task", "taskId": ids["owner"]}})
        options = {**OWN_CONVERSATION, "conversationId": conversation["id"], "background": True}
        return await tx.createTask(Node, {"name": "background"}, options)  # type: ignore[no-any-return]

    background = await root.commit(create, context)
    # background -> owner is a wait but owner -> background is no hold, so there is no cycle.
    await until(hasStatus(harness, background, "waiting"))
    openGate("owner")
    assert await outcomeOf(harness, background) == "completed"
    await harness.close(context)


# #10411: the issue's orchestrator, worker, and reviewer.
async def test_waiting_rejects_a_cycle_of_a_hold_and_two_waits_that_each_look_reasonable():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    ids: dict[str, int] = {}
    reviewerWaits = deferred()

    # The orchestrator spawns a worker and finishes; the running worker holds it as completing.
    async def orchestrator(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            ids["worker"] = await spawn(tx, runtime.taskId, "worker")
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    # The reviewer waits for the orchestrator's result.
    async def reviewer(runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: waitOn([ids["orchestrator"]], "allSettled"), ctx)
        reviewerWaits.resolve()

    # The worker waits for the reviewer, closing the cycle.
    async def worker(runtime: Any, ctx: Any) -> None:
        await reviewerWaits.promise
        await runtime.commit(lambda tx, current: waitOn([ids["reviewer"]], "allSettled"), ctx)

    script("orchestrator", {"run": orchestrator})
    script("reviewer", {"run": reviewer})
    script("worker", {"run": worker})
    ids["orchestrator"] = await start(root, "orchestrator")
    await until(hasStatus(harness, ids["orchestrator"], "completing"))
    ids["reviewer"] = await start(root, "reviewer")
    outcomes = await asyncio.gather(
        *[outcomeOf(harness, ids[name]) for name in ["orchestrator", "worker", "reviewer"]]
    )
    assert outcomes == ["completed", "faulted", "completed"]
    workerOutcome = (await harness.waitForTask(ids["worker"], context))["state"]["outcome"]
    assert (
        f"{ids['worker']} -> {ids['reviewer']} -> {ids['orchestrator']} -> {ids['worker']} is a wait cycle"
        in json.dumps(workerOutcome)
    )
    await harness.close(context)


# #10411
async def test_waiting_allows_a_diamond_of_waits():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    ids: dict[str, int] = {}
    # A diamond: top waits on left and right, both wait on bottom. No cycle.
    script("top", {"run": waitsOn(lambda: [ids["left"], ids["right"]])})
    script("left", {"run": waitsOn(lambda: [ids["bottom"]])})
    script("right", {"run": waitsOn(lambda: [ids["bottom"]])})

    async def create(tx: Any) -> None:
        for name in ["bottom", "left", "right", "top"]:
            ids[name] = await tx.createTask(Node, {"name": name}, OWN_CONVERSATION)

    await root.commit(create, context)
    openGate("bottom")
    assert await outcomeOf(harness, ids["top"]) == "completed"
    await harness.close(context)


# #10411
async def test_waiting_follows_no_wait_of_an_abort_marked_task_which_waits_only_for_its_owned_work():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    ids: dict[str, int] = {}
    cleanup = deferred()

    # b owns c and waits on a; aborting b leaves c's cleanup pending, so b waits only for c.
    async def b(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            ids["c"] = await spawn(tx, runtime.taskId, "c")
            return waitOn([ids["a"]], "allSettled")

        await runtime.commit(change, ctx)

    async def cAbort(runtime: Any, ctx: Any) -> None:
        await cleanup.promise
        await runtime.commit(
            lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted", "result": "c"}}, ctx
        )

    script("b", {"run": b})
    script("c", {"abort": cAbort})
    aWaits = deferred()

    async def a(runtime: Any, ctx: Any) -> None:
        await aWaits.promise
        await runtime.commit(lambda tx, current: waitOn([ids["b"]], "allSettled"), ctx)

    async def aResume(runtime: Any, ctx: Any, _round: Any) -> None:
        await runtime.commit(lambda tx, current: end("completed", "a"), ctx)

    script("a", {"run": a, "resume": aResume})

    async def create(tx: Any) -> None:
        ids["a"] = await tx.createTask(Node, {"name": "a"}, OWN_CONVERSATION)
        ids["b"] = await tx.createTask(Node, {"name": "b"}, OWN_CONVERSATION)

    await root.commit(create, context)
    await until(hasStatus(harness, ids["b"], "waiting"))
    assert await harness.abortTask(ids["b"], context) == "marked"
    await until(lambda: "abort:c" in log)
    # a -> b would close a -> b -> a through b's stale wait; b now waits only for c.
    aWaits.resolve()
    await until(hasStatus(harness, ids["a"], "waiting"))
    cleanup.resolve()
    assert await outcomeOf(harness, ids["b"]) == "aborted"
    assert await outcomeOf(harness, ids["a"]) == "completed"
    await harness.close(context)


# #10411
async def test_waiting_rejects_a_longer_cycle_of_waits():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    ids: dict[str, int] = {}
    names = ["x", "y", "z"]
    for index, name in enumerate(names):
        following = names[(index + 1) % len(names)]
        script(name, {"run": waitsOn(lambda following=following: [ids[following]])})

    async def create(tx: Any) -> None:
        for name in names:
            ids[name] = await tx.createTask(Node, {"name": name}, OWN_CONVERSATION)

    await root.commit(create, context)
    outcomes = await asyncio.gather(*[outcomeOf(harness, ids[name]) for name in names])
    assert len([outcome for outcome in outcomes if outcome == "faulted"]) == 1
    assert len([outcome for outcome in outcomes if outcome == "completed"]) == 2
    await harness.close(context)


async def test_waiting_resumes_at_the_next_pass_when_it_waits_on_nothing():
    opened = await openNodes()
    harness, root = opened.harness, opened.root

    async def parentRun(runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: waitOn([], "failFast"), ctx)

    script("parent", {"run": parentRun})
    assert await outcomeOf(harness, await start(root, "parent")) == "completed"
    assert log == ["run:parent", "resume:parent"]
    await harness.close(context)


async def test_waiting_keeps_running_phases_after_spawning_and_waits_on_subsets_in_sequence():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    ids: list[int] = []

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            for name in ("a", "b", "c"):
                ids.append(await spawn(tx, runtime.taskId, name))
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 0}}

        await runtime.commit(change, ctx)

    async def parentResume(runtime: Any, ctx: Any, round: int) -> None:
        log.append(f"round:{round}")
        if round == 0:
            await runtime.commit(lambda tx, current: waitOn([ids[0]], "allSettled", 1), ctx)
            return
        if round == 1:
            await runtime.commit(lambda tx, current: waitOn([ids[1], ids[2]], "failFast", 2), ctx)
            return
        await runtime.commit(lambda tx, current: end("completed", "parent"), ctx)

    script("parent", {"run": parentRun, "resume": parentResume})
    parent = await start(root, "parent")
    await until(lambda: "round:0" in log)
    await until(hasStatus(harness, parent, "waiting"))
    openGate("b")
    assert await settled(harness.waitForTask(ids[1], context)) is True
    assert (await state(harness, parent))["status"] == "waiting"
    openGate("a")
    await until(lambda: "round:1" in log)
    openGate("c")
    assert await outcomeOf(harness, parent) == "completed"
    assert [line for line in log if line.startswith("round:")] == ["round:0", "round:1", "round:2"]
    await harness.close(context)


# ─── Completing ─────────────────────────────────────────────────────────────


async def test_completing_holds_a_finished_task_until_its_owned_work_drains_waiters_and_task_documents_wait_for_the_final_commit():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    holder: dict[str, Any] = {}

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            (await tx.doc(TaskNotes, runtime.taskId))["text"] = "notes"
            holder["child"] = await spawn(tx, runtime.taskId, "child")
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    script("parent", {"run": parentRun})
    parent = await start(root, "parent")
    await until(hasStatus(harness, parent, "completing"))
    assert await state(harness, parent) == {"status": "completing", "outcome": {"status": "completed", "result": "parent"}}
    waiter = harness.waitForTask(parent, context)
    assert await settled(waiter) is False
    assert await harness.snapshot(TaskNotes, parent, context) == {"text": "notes"}
    inspected = next(task for task in (await harness.inspect(context))["tasks"] if task["record"]["id"] == parent)
    assert inspected["state"] == {"kind": "completing"}
    assert await settled(root.waitForIdle(context)) is False
    openGate("child")
    assert (await waiter)["state"]["outcome"] == {"status": "completed", "result": "parent"}
    assert await harness.snapshot(TaskNotes, parent, context) is None
    await root.waitForIdle(context)
    assert "child" in holder
    await harness.close(context)


async def test_completing_keeps_holding_for_ordinary_work_created_during_the_hold():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    holder: dict[str, Any] = {}

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["conversation"] = (
                await tx.createConversation({"ownership": {"kind": "task", "taskId": runtime.taskId}})
            )["id"]
            await tx.createTask(Node, {"name": "first"}, {**OWN_CONVERSATION, "conversationId": holder["conversation"]})
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    script("parent", {"run": parentRun})
    parent = await start(root, "parent")
    await until(hasStatus(harness, parent, "completing"))
    child = await harness.conversation(holder["conversation"], context)
    await start(child, "second")
    openGate("first")
    await until(lambda: "run:second" in log)
    assert (await state(harness, parent))["status"] == "completing"
    openGate("second")
    assert await outcomeOf(harness, parent) == "completed"
    await harness.close(context)


@pytest.mark.parametrize(("label", "ending"), [("a held failure", "failed"), ("a held scheduler fault", "throw")])
async def test_completing_aborts_the_work_below_a_held_outcome_then_finishes_with_it(label, ending):
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    holder: dict[str, Any] = {}

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["child"] = await spawn(tx, runtime.taskId, "child")
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    async def parentResume(runtime: Any, ctx: Any, round: int) -> None:
        if ending == "throw":
            raise RuntimeError("parent threw")
        await runtime.commit(lambda tx, current: end("failed", "parent"), ctx)

    script("parent", {"run": parentRun, "resume": parentResume})
    parent = await start(root, "parent")
    await until(lambda: "child" in holder)
    assert await outcomeOf(harness, holder["child"]) == "aborted"
    assert await outcomeOf(harness, parent) == ("faulted" if ending == "throw" else "failed")
    await harness.close(context)


async def test_completing_only_marks_a_completing_task_when_aborted_the_work_below_is_aborted_the_held_outcome_stays():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    holder: dict[str, Any] = {}

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["child"] = await spawn(tx, runtime.taskId, "child")
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    script("parent", {"run": parentRun})
    parent = await start(root, "parent")
    await until(hasStatus(harness, parent, "completing"))
    assert await harness.abortTask(parent, context) == "marked"
    assert await outcomeOf(harness, holder["child"]) == "aborted"
    settledParent = await harness.waitForTask(parent, context)
    assert settledParent["state"]["outcome"] == {"status": "completed", "result": "parent"}
    assert settledParent["abortRequested"] is True
    assert "abort:parent" not in log
    assert await harness.abortTask(parent, context) == "terminal"
    await harness.close(context)


# ─── Abort order ────────────────────────────────────────────────────────────


def chain(harness: Callable[[], Any], name: str, child: str) -> dict[str, Any]:
    """Script ``name`` to spawn ``child`` and wait on it; its abort handler records the child's status first."""
    found: dict[str, Any] = {}

    async def run(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            found["id"] = await spawn(tx, runtime.taskId, child)
            return waitOn([found["id"]], "allSettled")

        await runtime.commit(change, ctx)

    async def abort(runtime: Any, ctx: Any) -> None:
        log.append(f"{name} saw {(await harness().getTask(found['id'], context))['state']['status']}")
        await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, ctx)

    script(name, {"run": run, "abort": abort})
    return found


async def test_abort_order_runs_abort_handlers_bottom_up_across_three_levels_each_after_the_level_below_is_terminal():
    holder: dict[str, Any] = {}
    b = chain(lambda: holder["opened"].harness, "a", "b")
    c = chain(lambda: holder["opened"].harness, "b", "c")
    holder["opened"] = await openNodes()
    harness, root = holder["opened"].harness, holder["opened"].root
    a = await start(root, "a")
    await until(lambda: "run:c" in log)
    await until(hasStatus(harness, c["id"], "running")) if "id" in c else await until(lambda: "id" in c)
    assert await harness.abortTask(a, context) == "marked"
    assert await outcomeOf(harness, a) == "aborted"
    assert [line for line in log if line.startswith("abort:") or " saw " in line] == [
        "abort:c",
        "abort:b",
        "b saw terminal",
        "abort:a",
        "a saw terminal",
    ]
    assert "id" in b
    await harness.close(context)


async def test_abort_order_does_not_wait_for_a_task_it_waits_on_but_does_not_own():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    other = await start(root, "other")

    async def parentRun(runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: waitOn([other], "allSettled"), ctx)

    script("parent", {"run": parentRun})
    parent = await start(root, "parent")
    await until(hasStatus(harness, parent, "waiting"))
    await harness.abortTask(parent, context)
    assert await outcomeOf(harness, parent) == "aborted"
    assert (await state(harness, other))["status"] == "running"
    openGate("other")
    await harness.close(context)


async def test_abort_order_reports_an_abort_marked_task_as_waiting_for_its_live_owned_work():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    holder: dict[str, Any] = {}
    script("child", {"abort": slowAbortOf("child")})

    async def parentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["child"] = await spawn(tx, runtime.taskId, "child")
            return waitOn([holder["child"]], "allSettled")

        await runtime.commit(change, ctx)

    script("parent", {"run": parentRun})
    parent = await start(root, "parent")
    await until(hasStatus(harness, parent, "waiting"))
    await harness.abortTask(parent, context)
    await until(lambda: "abort:child" in log)
    inspected = next(task for task in (await harness.inspect(context))["tasks"] if task["record"]["id"] == parent)
    assert inspected["state"] == {"kind": "waiting", "on": [holder["child"]]}
    openGate("abort.child")
    assert await outcomeOf(harness, parent) == "aborted"
    await harness.close(context)


async def test_abort_order_faults_an_abort_handler_that_tries_to_wait():
    opened = await openNodes()
    harness, root = opened.harness, opened.root

    async def parentAbort(runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: waitOn([], "allSettled"), ctx)

    script("parent", {"abort": parentAbort})
    parent = await start(root, "parent")
    await until(lambda: "run:parent" in log)
    await harness.abortTask(parent, context)
    outcome = (await harness.waitForTask(parent, context))["state"]["outcome"]
    assert outcome["status"] == "faulted"
    assert "cannot wait" in outcome["error"]["message"]
    await harness.close(context)


async def test_abort_order_orphans_a_blocked_task_only_after_its_owned_work_drained():
    opened = await openNodes()
    harness, root = opened.harness, opened.root

    async def create(tx: Any) -> SimpleNamespace:
        owner = await tx.createTask(Unregistered, {"name": "owner"}, OWN_CONVERSATION)
        return SimpleNamespace(owner=owner, child=await spawn(tx, owner, "child"))

    tree = await root.commit(create, context)
    await until(lambda: "run:child" in log)
    assert await harness.abortTask(tree.owner, context) == "marked"
    assert await outcomeOf(harness, tree.child) == "aborted"
    assert (await harness.waitForTask(tree.owner, context))["state"]["outcome"] == {
        "status": "orphaned",
        "reason": "missing_task",
    }
    await harness.close(context)


async def test_abort_order_cascades_through_task_and_conversation_edges_bottom_up():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    holder: dict[str, Any] = {}

    async def aRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["b"] = await spawn(tx, runtime.taskId, "b")
            return waitOn([holder["b"]], "allSettled")

        await runtime.commit(change, ctx)

    async def bRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            conversation = await tx.createConversation({"ownership": {"kind": "task", "taskId": runtime.taskId}})
            holder["x"] = await tx.createTask(Node, {"name": "x"}, {**OWN_CONVERSATION, "conversationId": conversation["id"]})
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    async def bResume(runtime: Any, ctx: Any, round: int) -> None:
        await aborted(runtime.signal)

    script("a", {"run": aRun})
    script("b", {"run": bRun, "resume": bResume})
    a = await start(root, "a")
    await until(lambda: "run:x" in log)
    await harness.abortTask(a, context)
    assert await outcomeOf(harness, a) == "aborted"
    assert await outcomeOf(harness, holder["x"]) == "aborted"
    assert [line for line in log if line.startswith("abort:")] == ["abort:x", "abort:b", "abort:a"]
    await harness.close(context)


# ─── Background and terminal owners ─────────────────────────────────────────


async def test_boundaries_keeps_background_work_through_conversation_abort_with_background_true_aborting_it_and_waiting():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    foreground = await start(root, "foreground")
    holder: dict[str, Any] = {}

    async def backgroundRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["child"] = await spawn(tx, runtime.taskId, "child")
            conversation = await tx.createConversation({"ownership": {"kind": "task", "taskId": runtime.taskId}})
            holder["below"] = await tx.createTask(
                Node, {"name": "below"}, {**OWN_CONVERSATION, "conversationId": conversation["id"]}
            )
            return waitOn([holder["child"]], "allSettled")

        await runtime.commit(change, ctx)

    script("background", {"run": backgroundRun})
    background = await start(root, "background", {**OWN_CONVERSATION, "background": True})
    await until(lambda: "run:below" in log and "run:child" in log)
    await root.abort(context)
    assert await outcomeOf(harness, foreground) == "aborted"
    for id in (background, holder["child"], holder["below"]):
        assert (await state(harness, id))["status"] != "terminal"
    await root.abort(context, {"background": True})
    for id in (background, holder["child"], holder["below"]):
        assert (await state(harness, id))["status"] == "terminal"
    await harness.close(context)


async def test_boundaries_never_cascades_from_a_terminal_owner_an_aborted_subagents_conversation_runs_new_work_normally():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    holder: dict[str, Any] = {}

    async def agentRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["conversation"] = (
                await tx.createConversation({"ownership": {"kind": "task", "taskId": runtime.taskId}})
            )["id"]
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    async def agentResume(runtime: Any, ctx: Any, round: int) -> None:
        await aborted(runtime.signal)

    script("agent", {"run": agentRun, "resume": agentResume})
    agent = await start(root, "agent")
    await until(lambda: "resume:agent" in log)
    await harness.abortTask(agent, context)
    assert await outcomeOf(harness, agent) == "aborted"
    child = await harness.conversation(holder["conversation"], context)
    question = await start(child, "question")
    openGate("question")
    assert await outcomeOf(harness, question) == "completed"
    await harness.close(context)


# ─── Recovery ───────────────────────────────────────────────────────────────


async def seed(path: str, children: list[str], edit: Callable[..., Any]) -> SimpleNamespace:
    """Write records a crash could leave, without a Harness: ``parent`` with children named ``children``, then ``edit``
    replaces states through the internal task write."""
    session = createSession(await openNodeSqliteStorage(path))

    async def build(tx: Any) -> SimpleNamespace:
        root = await tx.createConversation({"ownership": {"kind": "ownerless"}})
        parent = await tx.createTask(Node, {"name": "parent"}, {**OWN_CONVERSATION, "conversationId": root["id"]})
        ids = []
        for name in children:
            ids.append(await spawn(tx, parent, name))
        return SimpleNamespace(parent=parent, children=ids)

    seeded = await session.commit(build, context)

    async def setTask(id: int, patch: dict[str, Any]) -> None:
        async def change(tx: Any) -> None:
            record = await tx.task(id)
            tx.setTask({**record, **patch})

        await session.commit(change, context)

    await edit(setTask, seeded)
    await session.close(context)
    return seeded  # type: ignore[no-any-return]


COMPLETED = {"status": "terminal", "outcome": {"status": "completed", "result": "done"}}
FAILED = {"status": "terminal", "outcome": {"status": "failed", "error": {"message": "declined"}}}


async def test_recovery_resumes_a_parent_whose_awaited_children_finished_before_the_crash(sqlitePath):
    async def edit(set: Any, seeded: Any) -> None:
        await set(seeded.children[0], {"state": COMPLETED})
        await set(
            seeded.parent,
            {"state": {"status": "waiting", "checkpoint": {"phase": "resume", "round": 1}, "on": seeded.children, "policy": "allSettled"}},
        )

    seeded = await seed(sqlitePath, ["c1"], edit)
    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    assert await outcomeOf(opened.harness, seeded.parent) == "completed"
    assert log == ["resume:parent"]
    assert len(seeded.children) == 1
    await opened.harness.close(context)


async def test_recovery_marks_fail_fast_siblings_a_crash_left_unmarked(sqlitePath):
    async def edit(set: Any, seeded: Any) -> None:
        await set(seeded.children[0], {"state": FAILED})
        await set(
            seeded.parent,
            {"state": {"status": "waiting", "checkpoint": {"phase": "resume", "round": 1}, "on": seeded.children, "policy": "failFast"}},
        )

    seeded = await seed(sqlitePath, ["c1", "c2"], edit)
    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    assert await outcomeOf(opened.harness, seeded.children[1]) == "aborted"
    assert await outcomeOf(opened.harness, seeded.parent) == "completed"
    assert "run:c2" not in log
    await opened.harness.close(context)


async def test_recovery_finalizes_a_held_outcome_whose_work_drained_before_the_crash_and_keeps_one_whose_work_lives(
    sqlitePath,
):
    held = {"status": "completing", "outcome": {"status": "completed", "result": "parent"}}

    async def edit(set: Any, seeded: Any) -> None:
        await set(seeded.parent, {"state": held})

    seeded = await seed(sqlitePath, ["c1"], edit)
    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    await until(lambda: "run:c1" in log)
    assert await state(opened.harness, seeded.parent) == held
    await opened.harness.close(context)

    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    openGate("c1")
    assert await outcomeOf(opened.harness, seeded.children[0]) == "completed"
    assert await outcomeOf(opened.harness, seeded.parent) == "completed"
    await opened.harness.close(context)

    drained = os.path.join(os.path.dirname(sqlitePath), "drained.sqlite")

    async def editDrained(set: Any, seededDrained: Any) -> None:
        await set(seededDrained.children[0], {"state": COMPLETED})
        await set(seededDrained.parent, {"state": held})

    second = await seed(drained, ["c1"], editDrained)
    opened = await openNodes(await openNodeSqliteStorage(drained))
    assert await outcomeOf(opened.harness, second.parent) == "completed"
    await opened.harness.close(context)


async def test_recovery_resumes_a_bottom_up_abort_a_crash_interrupted_before_the_cascade(sqlitePath):
    async def edit(set: Any, seeded: Any) -> None:
        await set(
            seeded.parent,
            {
                "abortRequested": True,
                "state": {"status": "waiting", "checkpoint": {"phase": "resume", "round": 1}, "on": seeded.children, "policy": "allSettled"},
            },
        )

    seeded = await seed(sqlitePath, ["c1"], edit)
    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    assert await outcomeOf(opened.harness, seeded.parent) == "aborted"
    assert await outcomeOf(opened.harness, seeded.children[0]) == "aborted"
    assert [line for line in log if line.startswith("abort:")] == ["abort:c1", "abort:parent"]
    await opened.harness.close(context)


async def test_recovery_reopens_a_checkout_waiting_on_live_payments_and_finishes_it(sqlitePath):
    found = parentOf("checkout", ["p1", "p2"], "failFast")
    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    parent = await start(opened.root, "checkout")
    await until(hasStatus(opened.harness, parent, "waiting"))
    await until(lambda: "run:p1" in log and "run:p2" in log)
    await opened.harness.close(context)

    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    found["ids"][:] = (await state(opened.harness, parent))["on"]
    openGate("p1")
    openGate("p2")
    assert await outcomeOf(opened.harness, parent) == "completed"
    assert found["outcomes"] == ["completed", "completed"]
    await opened.harness.close(context)


# ─── Built-in tool rounds ───────────────────────────────────────────────────


def blockingTool(name: str) -> SimpleNamespace:
    started = deferred()

    async def execute(_args: Any, api: Any, callContext: Any) -> Any:
        api.output("partial")
        started.resolve()
        return await aborted(callContext.abortSignal)

    registration = defineTool(
        {
            "name": name,
            "description": f"The {name} tool",
            "parameters": EMPTY_OBJECT,
            "executionMode": "sequential",
            "execute": execute,
        }
    )
    return SimpleNamespace(started=started.promise, registration=registration)


def toolCalls(*ids: tuple[str, str]) -> Any:
    return fauxAssistantMessage([fauxToolCall(name, {}, {"id": id}) for name, id in ids], {"stopReason": "toolUse"})


async def noopExecute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
    return {"output": []}


def noopTool() -> dict[str, Any]:
    return defineTool(
        {"name": "noop", "description": "Does nothing", "parameters": EMPTY_OBJECT, "execute": noopExecute}
    )


async def test_tool_rounds_owns_its_tool_tasks_waits_for_them_and_hands_the_run_to_a_conversation_owned_generation():
    setup = chatSetup()
    addTool(setup.registry, noopTool())
    setup.faux.setResponses([toolCalls(("noop", "c1"), ("noop", "c2")), fauxAssistantMessage([fauxText("done")])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    assert (await (await root.submit({"type": "input", "content": "go"}, context)).wait(context))["status"] == "done"
    tasks = (await harness.commit(lambda tx: tx.scanTasks({"conversationId": root.id}, 20), context))["items"]
    generations = [task for task in tasks if task["kind"] == "pi.generation"]
    first, second = generations[0], generations[1]
    assert [task.get("owner") for task in tasks if task["kind"] == "pi.tool"] == [first["id"], first["id"]]
    assert first.get("owner") is None
    assert second.get("owner") is None
    assistant = next(entry for entry in await allEntries(root) if entry["kind"] == "pi.assistant")
    assert first["state"] == {
        "status": "terminal",
        "outcome": {"status": "completed", "result": {"entryId": assistant["id"]}},
    }
    await harness.close(context)


async def test_tool_rounds_aborts_a_parallel_round_with_its_generation_tools_first_then_the_generation_with_every_result_written():
    setup = chatSetup()
    one = blockingTool("one")
    two = blockingTool("two")
    addTool(setup.registry, defineTool({**one.registration, "executionMode": "parallel"}))
    addTool(setup.registry, defineTool({**two.registration, "executionMode": "parallel"}))
    setup.faux.setResponses([toolCalls(("one", "c1"), ("two", "c2"))])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await asyncio.gather(one.started, two.started)
    generation = (await harness.snapshot(LiveDoc, root.id, context))["run"]["taskId"]
    await harness.abortTask(generation, context)
    toMatchObject(await submission.wait(context), {"status": "unanswered", "reason": "aborted"})
    results = [entry for entry in await allEntries(root) if ToolResultEntry.is_(entry)]
    assert [entry["model"][0]["toolCallId"] for entry in results] == ["c1", "c2"]
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    assert (await harness.waitForTask(generation, context))["state"]["outcome"]["status"] == "aborted"
    await harness.close(context)


async def test_tool_rounds_answers_the_unstarted_calls_of_an_aborted_sequential_round_with_aborted_results_in_call_order():
    setup = chatSetup()
    one = blockingTool("one")
    addTool(setup.registry, one.registration)
    addTool(setup.registry, defineTool({**blockingTool("two").registration, "name": "two"}))
    setup.faux.setResponses([toolCalls(("one", "c1"), ("two", "c2"), ("two", "c3"))])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await one.started
    live = await harness.snapshot(LiveDoc, root.id, context)
    assert [slot.get("taskId") is not None for slot in live["tools"]] == [True, False, False]
    await harness.abortTask(live["run"]["taskId"], context)
    toMatchObject(await submission.wait(context), {"status": "unanswered", "reason": "aborted"})
    results = [entry["model"][0] for entry in await allEntries(root) if ToolResultEntry.is_(entry)]
    assert [result["toolCallId"] for result in results] == ["c1", "c2", "c3"]
    assert [result["isError"] for result in results] == [True, True, True]
    assert results[1]["content"] == [{"type": "text", "text": "<harness>\n[error] Tool two was aborted\n</harness>"}]
    tools = (
        await harness.commit(lambda tx: tx.scanTasks({"conversationId": root.id, "kind": "pi.tool"}, 20), context)
    )["items"]
    assert len(tools) == 1
    await harness.close(context)


async def test_tool_rounds_ends_a_turn_at_the_generations_hold_before_its_successors_turn_starts():
    setup = chatSetup()
    addTask(setup.registry, Node)
    addTool(setup.registry, noopTool())
    holder: dict[str, Any] = {}

    # An extension's hook starts work owned by the generation, which holds it while the next turn runs.
    async def afterTools(_assistant: Any, _results: Any, api: Any, callContext: Any) -> None:
        await holder["opened"].commit(
            lambda tx: tx.createTask(Node, {"name": "hooked"}, owned(api.taskId)), callContext
        )

    addHooks(setup.registry, GenerationTask, {"afterTools": afterTools})
    setup.faux.setResponses([toolCalls(("noop", "c1")), fauxAssistantMessage([fauxText("done")])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    holder["opened"] = harness
    stream = await watchEvents(harness, root.id, context)
    events: list[dict[str, Any]] = []

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(batch)

    stream.start(onBatch)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    first = (await harness.commit(lambda tx: tx.scanTasks({"kind": "pi.generation"}, 1), context))["items"][0]
    assert (await state(harness, first["id"]))["status"] == "completing"

    def turns() -> list[str]:
        return [event["type"] for event in events if event["type"].startswith("turn_")]

    await waitFor(lambda: len(turns()) == 4)
    assert turns() == ["turn_start", "turn_end", "turn_start", "turn_end"]
    openGate("hooked")
    assert await outcomeOf(harness, first["id"]) == "completed"
    await waitFor(lambda: len(turns()) == 4)
    await stream.stop()
    await harness.close(context)


async def test_tool_rounds_keeps_run_control_with_a_faulted_generation_until_its_owned_work_drains_and_fails_the_harness_on_a_failed_final_commit():
    base = chatSetup()
    setup = SimpleNamespace(**{**vars(base), "models": WithOverrides(base.models, streamSimple=invalidFinalStream)})
    addTask(setup.registry, Node)
    holder: dict[str, Any] = {}

    async def beforeRequest(_request: Any, api: Any, callContext: Any) -> None:
        await holder["opened"].commit(
            lambda tx: tx.createTask(Node, {"name": "hooked"}, owned(api.taskId)), callContext
        )

    addHooks(setup.registry, GenerationTask, {"beforeRequest": beforeRequest})
    script("hooked", {"abort": slowAbortOf("hooked")})
    reject: int | None = None
    failure = RuntimeError("disk gone")

    class Failing(ControlledStorage):
        async def commit(self, writes: Any, ctx: Any) -> int:  # type: ignore[override]
            nonlocal reject
            finalizes = any(
                write["type"] == "task" and write["value"]["id"] == reject and write["value"]["state"]["status"] == "terminal"
                for write in writes
            )
            if finalizes:
                reject = None
                raise failure
            return await super().commit(writes, ctx)

    storage = Failing()
    opened = await openChat(storage, setup)
    harness, root = opened.harness, opened.root
    holder["opened"] = harness
    submission = await root.submit({"type": "input", "content": "hi"}, context)

    async def generationHeld() -> bool:
        live = await harness.snapshot(LiveDoc, root.id, context)
        generation = None if live is None else (live.get("run") or {}).get("taskId")
        return generation is not None and (await state(harness, generation))["status"] == "completing"

    await waitFor(generationHeld)
    generation = (await harness.snapshot(LiveDoc, root.id, context))["run"]["taskId"]
    toMatchObject(await state(harness, generation), {"status": "completing", "outcome": {"status": "faulted"}})
    assert await settled(submission.wait(context)) is False
    assert ((await harness.snapshot(LiveDoc, root.id, context)).get("run") or {}).get("taskId") == generation
    # The faulted outcome is cancellation intent: the hooked work is aborted, then the run settles.
    await waitFor(lambda: "abort:hooked" in log)
    reject = generation
    openGate("abort.hooked")
    # The final commit, with the run's cleanup, fails: nothing of the cleanup lands, and the Harness fails.
    assert await harness.closed == {"reason": "failed", "error": failure}
    with pytest.raises(SessionFailed) as failed:
        await submission.wait(context)
    assert failed.value.cause is failure
    await harness.close(context)
    # Reopening finalizes the run; the partial becomes one aborted entry.
    reopened = await openChat(storage.reopen(), setup)
    holder["opened"] = reopened.harness
    again = await reopened.harness.submission(submission.id, context)
    toMatchObject(await again.wait(context), {"status": "unanswered", "reason": "faulted"})
    assert await reopened.harness.snapshot(LiveDoc, reopened.root.id, context) == {}
    assert [entry["kind"] for entry in await allEntries(reopened.root)] == ["pi.user", "pi.assistant"]
    await reopened.harness.close(context)


# ─── Review follow-ups ──────────────────────────────────────────────────────


def versioned(version: int) -> dict[str, Any]:
    """A waiter of its own kind, so its definition can be removed or replaced."""

    async def wait(task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(
            lambda tx, current: {"status": "waiting", "checkpoint": {"phase": "resume"}, "on": task["input"]["on"], "policy": "allSettled"},
            ctx,
        )

    async def resume(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": "v1"}}, ctx)

    async def migrated(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": "v2"}}, ctx)

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, ctx)

    definition: dict[str, Any] = {
        "name": "test.versioned",
        "version": version,
        "initial": lambda input: {"phase": "wait"},
        "phases": {"wait": wait, "resume": resume, "migrated": migrated},
        "abort": abort,
    }
    if version == 2:
        definition["migrate"] = lambda input, checkpoint, fromVersion: {"input": input, "checkpoint": {"phase": "migrated"}}
    return defineTask(definition)


def spawnAndFinish(name: str, child: str) -> dict[str, Any]:
    """Script ``name`` to spawn ``child`` and then complete, holding while the child lives."""
    found: dict[str, Any] = {}

    async def run(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            found["id"] = await spawn(tx, runtime.taskId, child)
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    script(name, {"run": run})
    return found


async def test_definitions_keeps_a_waiting_task_blocked_without_its_definition_and_resumes_it_migrated_under_a_newer_one():
    opened = await openNodes()
    harness, root, registry = opened.harness, opened.root, opened.registry
    registration = addTask(registry, versioned(1))
    other = await start(root, "other")
    waiter = await root.commit(lambda tx: tx.createTask(versioned(1), {"on": [other]}, OWN_CONVERSATION), context)
    await until(hasStatus(harness, waiter, "waiting"))
    registration.dispose()
    openGate("other")
    await harness.waitForTask(other, context)

    async def inspected() -> Any:
        return next(task for task in (await harness.inspect(context))["tasks"] if task["record"]["id"] == waiter)["state"]

    assert await inspected() == {"kind": "blocked", "reason": "missing_task"}
    assert (await state(harness, waiter))["status"] == "waiting"
    addTask(registry, versioned(2))
    assert (await harness.waitForTask(waiter, context))["state"]["outcome"] == {"status": "completed", "result": "v2"}
    await harness.close(context)


async def test_definitions_orphans_an_aborted_waiting_task_without_its_definition_at_once_leaving_the_task_it_waits_on_running():
    opened = await openNodes()
    harness, root, registry = opened.harness, opened.root, opened.registry
    registration = addTask(registry, versioned(1))
    other = await start(root, "other")
    waiter = await root.commit(lambda tx: tx.createTask(versioned(1), {"on": [other]}, OWN_CONVERSATION), context)
    await until(hasStatus(harness, waiter, "waiting"))
    registration.dispose()
    assert await harness.abortTask(waiter, context) == "marked"
    assert (await state(harness, waiter))["outcome"] == {"status": "orphaned", "reason": "missing_task"}
    assert (await state(harness, other))["status"] == "running"
    openGate("other")
    await harness.close(context)


async def test_definitions_never_migrates_a_held_outcome_a_newer_definition_leaves_it_and_its_version_alone():
    opened = await openNodes()
    harness, root, registry, reports = opened.harness, opened.root, opened.registry, opened.reports

    def holder(version: int) -> dict[str, Any]:
        async def run(task: Any, runtime: Any, ctx: Any) -> None:
            async def change(tx: Any, current: Any) -> None:
                await spawn(tx, task["id"], "child")

            await runtime.commit(change, ctx)
            await runtime.commit(
                lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": "held"}}, ctx
            )

        async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
            await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, ctx)

        def migrate(*_: Any) -> Any:
            raise RuntimeError("never migrates")

        return defineTask(
            {
                "name": "test.holder",
                "version": version,
                "initial": lambda input: {"phase": "run"},
                "phases": {"run": run},
                "abort": abort,
                "migrate": migrate,
            }
        )

    addTask(registry, holder(1))
    parent = await root.commit(lambda tx: tx.createTask(holder(1), None, OWN_CONVERSATION), context)
    await until(hasStatus(harness, parent, "completing"))
    # The same extension name replaces the old one in place.
    addTask(registry, holder(2))
    openGate("child")
    settledParent = await harness.waitForTask(parent, context)
    assert settledParent["state"]["outcome"] == {"status": "completed", "result": "held"}
    assert settledParent["version"] == 1
    assert reports == []
    await harness.close(context)


async def test_definitions_treats_a_held_task_as_live_outcomes_rejects_and_a_task_waiting_on_it_resumes_at_its_final_commit():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    spawnAndFinish("held", "child")
    held = await start(root, "held")
    await until(hasStatus(harness, held, "completing"))
    holder = {"rejection": ""}

    async def readerRun(runtime: Any, ctx: Any) -> None:
        try:
            await runtime.outcomes([held], ctx)
            holder["rejection"] = "resolved"
        except Exception as error:  # noqa: BLE001 - the message is the observation
            holder["rejection"] = str(error)
        await runtime.commit(lambda tx, current: waitOn([held], "allSettled"), ctx)

    script("reader", {"run": readerRun})
    reader = await start(root, "reader")
    await until(hasStatus(harness, reader, "waiting"))
    assert holder["rejection"] == f"Task {held} is not terminal"
    openGate("child")
    assert await outcomeOf(harness, reader) == "completed"
    assert "resume:reader" in log
    await harness.close(context)


async def test_conversation_abort_marks_a_held_completed_task_with_conversation_abort_the_work_below_is_aborted_the_outcome_stays():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    child = spawnAndFinish("held", "child")
    held = await start(root, "held")
    await until(hasStatus(harness, held, "completing"))
    await root.abort(context)
    assert await outcomeOf(harness, child["id"]) == "aborted"
    record = await harness.waitForTask(held, context)
    assert record["state"]["outcome"]["status"] == "completed"
    assert record["abortRequested"] is True
    await harness.close(context)


async def test_conversation_abort_marks_and_awaits_only_the_background_work_reached_when_background_true_is_admitted():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    script("first", {"abort": slowAbortOf("first")})
    background = {**OWN_CONVERSATION, "background": True}
    first = await start(root, "first", background)
    await until(lambda: "run:first" in log)
    aborting = root.abort(context, {"background": True})

    async def marked() -> bool:
        return (await harness.getTask(first, context))["abortRequested"]  # type: ignore[no-any-return]

    await until(marked)
    later = await start(root, "later", background)
    openGate("abort.first")
    await aborting
    assert (await state(harness, later))["status"] != "terminal"
    assert (await harness.getTask(later, context))["abortRequested"] is False
    await harness.abortTask(later, context)
    await harness.close(context)


async def test_conversation_abort_stops_a_cascade_at_an_unmarked_background_task_but_not_at_a_marked_one():
    opened = await openNodes()
    harness, root = opened.harness, opened.root
    script("owner", {"abort": slowAbortOf("owner")})

    async def create(tx: Any) -> SimpleNamespace:
        owner = await tx.createTask(Node, {"name": "owner"}, OWN_CONVERSATION)
        outer = await tx.createConversation({"ownership": {"kind": "task", "taskId": owner}})
        background = await tx.createTask(
            Node, {"name": "background"}, {**OWN_CONVERSATION, "conversationId": outer["id"], "background": True}
        )
        inner = await tx.createConversation({"ownership": {"kind": "task", "taskId": background}})
        return SimpleNamespace(owner=owner, outer=outer["id"], background=background, inner=inner["id"])

    tree = await root.commit(create, context)
    await until(lambda: "run:owner" in log and "run:background" in log)
    # The owner's abort handler starts at once: background work is not its ordinary owned work.
    await harness.abortTask(tree.owner, context)
    await until(lambda: "abort:owner" in log)
    inner = await harness.conversation(tree.inner, context)
    outer = await harness.conversation(tree.outer, context)
    shielded = await start(inner, "shielded")
    exposed = await start(outer, "exposed")
    assert await outcomeOf(harness, exposed) == "aborted"
    assert (await harness.getTask(shielded, context))["abortRequested"] is False
    # Marked directly, the background task cascades into its own subtree.
    await harness.abortTask(tree.background, context)
    assert await outcomeOf(harness, shielded) == "aborted"
    assert await outcomeOf(harness, tree.background) == "aborted"
    openGate("abort.owner")
    assert await outcomeOf(harness, tree.owner) == "aborted"
    await harness.close(context)


async def test_conversation_abort_fails_the_harness_on_a_failed_finalization_which_reopening_applies():
    reject: int | None = None
    failure = RuntimeError("disk gone")

    class Failing(ControlledStorage):
        async def commit(self, writes: Any, ctx: Any) -> int:  # type: ignore[override]
            nonlocal reject
            finalizes = any(
                write["type"] == "task" and write["value"]["id"] == reject and write["value"]["state"]["status"] == "terminal"
                for write in writes
            )
            if finalizes:
                reject = None
                raise failure
            return await super().commit(writes, ctx)

    storage = Failing()
    opened = await openNodes(storage)
    harness, root, reports = opened.harness, opened.root, opened.reports
    spawnAndFinish("parent", "child")
    parent = await start(root, "parent")
    await until(hasStatus(harness, parent, "completing"))
    reject = parent
    openGate("child")
    assert await harness.closed == {"reason": "failed", "error": failure}
    assert reports == [failure]
    await harness.close(context)
    reopened = await openNodes(storage.reopen())
    assert await outcomeOf(reopened.harness, parent) == "completed"
    await reopened.harness.close(context)


async def test_recovery_through_ownership_edges_shows_an_abort_marked_owner_waiting_for_work_in_its_owned_conversation_before_resume_after_reopen(
    sqlitePath,
):
    session = createSession(await openNodeSqliteStorage(sqlitePath))

    async def build(tx: Any) -> SimpleNamespace:
        conversation = await tx.createConversation({"ownership": {"kind": "ownerless"}})
        owner = await tx.createTask(Node, {"name": "owner"}, {**OWN_CONVERSATION, "conversationId": conversation["id"]})
        child = await tx.createConversation({"ownership": {"kind": "task", "taskId": owner}})
        inner = await tx.createTask(Node, {"name": "inner"}, {**OWN_CONVERSATION, "conversationId": child["id"]})
        return SimpleNamespace(owner=owner, inner=inner)

    tree = await session.commit(build, context)

    async def mark(tx: Any) -> None:
        record = await tx.task(tree.owner)
        tx.setTask({**record, "abortRequested": True})

    await session.commit(mark, context)
    await session.close(context)

    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Node])
    inspected = next(task for task in (await harness.inspect(context))["tasks"] if task["record"]["id"] == tree.owner)
    assert inspected["state"] == {"kind": "waiting", "on": [tree.inner]}
    harness.resume()
    assert await outcomeOf(harness, tree.owner) == "aborted"
    assert [line for line in log if line.startswith("abort:")] == ["abort:inner", "abort:owner"]
    await harness.close(context)


async def test_recovery_through_ownership_edges_keeps_the_root_busy_after_reopen_for_work_below_a_child_tasks_owned_conversation(
    sqlitePath,
):
    holder: dict[str, Any] = {}

    async def childRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            conversation = await tx.createConversation({"ownership": {"kind": "task", "taskId": runtime.taskId}})
            holder["inner"] = await tx.createTask(
                Node, {"name": "inner"}, {**OWN_CONVERSATION, "conversationId": conversation["id"]}
            )
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    script("child", {"run": childRun})
    parentOf("parent", ["child"], "allSettled")
    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    parent = await start(opened.root, "parent")
    await until(lambda: "run:inner" in log)
    await opened.harness.close(context)

    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    assert await settled(opened.root.waitForIdle(context)) is False
    openGate("inner")
    await opened.root.waitForIdle(context)
    assert await outcomeOf(opened.harness, parent) == "completed"
    assert "inner" in holder
    await opened.harness.close(context)


async def test_recovery_through_ownership_edges_keeps_a_finished_background_ancestor_a_boundary_after_reopen_which_background_true_crosses(
    sqlitePath,
):
    holder: dict[str, Any] = {}

    async def childRun(runtime: Any, ctx: Any) -> None:
        async def change(tx: Any, current: Any) -> dict[str, Any]:
            holder["conversation"] = (
                await tx.createConversation({"ownership": {"kind": "task", "taskId": runtime.taskId}})
            )["id"]
            return {"status": "running", "checkpoint": {"phase": "resume", "round": 1}}

        await runtime.commit(change, ctx)

    script("child", {"run": childRun})
    parentOf("background", ["child"], "allSettled")
    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    background = await start(opened.root, "background", {**OWN_CONVERSATION, "background": True})
    await opened.harness.waitForTask(background, context)
    await opened.harness.close(context)

    opened = await openNodes(await openNodeSqliteStorage(sqlitePath))
    below = await start(await opened.harness.conversation(holder["conversation"], context), "below")
    await until(lambda: "run:below" in log)
    await opened.root.waitForIdle(context)
    await opened.root.abort(context)
    assert (await state(opened.harness, below))["status"] == "running"
    await opened.root.abort(context, {"background": True})
    assert await outcomeOf(opened.harness, below) == "aborted"
    await opened.harness.close(context)


async def listen(harness: Any, conversation: Any) -> SimpleNamespace:
    stream = await watchEvents(harness, conversation.id, context)
    events: list[dict[str, Any]] = []

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(batch)

    stream.start(onBatch)
    return SimpleNamespace(stream=stream, events=events)


def labels(events: list[dict[str, Any]]) -> list[str]:
    """Event types, with tool ends and message ends labelled by call ID and whether an entry came along."""
    result: list[str] = []
    for event in events:
        if event["type"] == "tool_execution_end":
            result.append(f"end:{event['toolCallId']}:{event.get('entry') is not None}")
        elif event["type"] == "message_end":
            model = event["entry"].get("model")
            message = model[0] if model else None
            if message is not None and message["role"] == "toolResult":
                result.append(f"result:{message['toolCallId']}")
            else:
                result.append(f"message:{None if message is None else message['role']}")
    return result


async def test_tool_rounds_and_events_ends_an_aborted_sequential_rounds_unstarted_calls_with_their_result_entries_right_before_them():
    setup = chatSetup()
    one = blockingTool("one")
    addTool(setup.registry, one.registration)
    addTool(setup.registry, defineTool({**blockingTool("two").registration, "name": "two"}))
    setup.faux.setResponses([toolCalls(("one", "c1"), ("two", "c2"), ("two", "c3"))])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    listened = await listen(harness, root)
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await one.started
    await harness.abortTask((await harness.snapshot(LiveDoc, root.id, context))["run"]["taskId"], context)
    await submission.wait(context)
    await waitFor(lambda: "result:c3" in labels(listened.events))
    assert [label for label in labels(listened.events) if not label.startswith("message:")] == [
        "end:c1:True",
        "result:c1",
        "end:c2:True",
        "result:c2",
        "end:c3:True",
        "result:c3",
    ]
    await listened.stream.stop()
    await harness.close(context)


async def test_tool_rounds_and_events_emits_one_turn_end_per_generation_also_for_a_stream_attached_while_it_holds():
    setup = chatSetup()
    addTask(setup.registry, Node)
    addTool(setup.registry, noopTool())
    holder: dict[str, Any] = {}

    async def afterTools(_assistant: Any, _results: Any, api: Any, callContext: Any) -> None:
        await holder["opened"].commit(
            lambda tx: tx.createTask(Node, {"name": "hooked"}, owned(api.taskId)), callContext
        )

    addHooks(setup.registry, GenerationTask, {"afterTools": afterTools})
    setup.faux.setResponses([toolCalls(("noop", "c1")), fauxAssistantMessage([fauxText("done")])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    holder["opened"] = harness
    early = await listen(harness, root)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    late = await listen(harness, root)
    openGate("hooked")
    first = (await harness.commit(lambda tx: tx.scanTasks({"kind": "pi.generation"}, 1), context))["items"][0]
    await harness.waitForTask(first["id"], context)
    await root.commit(lambda tx: tx.appendEntry(root.id, {"kind": "note"}), context)
    await waitFor(lambda: any(event["type"] == "entry_appended" for event in late.events))

    def turns(events: list[dict[str, Any]]) -> int:
        return len([event for event in events if event["type"] == "turn_end"])

    assert turns(early.events) == 2
    assert turns(late.events) == 0
    await early.stream.stop()
    await late.stream.stop()
    await harness.close(context)


async def test_tool_rounds_and_events_lets_a_tool_that_owns_live_work_finish_its_call_at_the_hold_while_the_generation_waits_for_its_final_commit():
    setup = chatSetup()
    addTask(setup.registry, Node)

    async def delegate(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        async def change(tx: Any) -> None:
            child = await tx.createConversation({"ownership": {"kind": "task", "taskId": api.taskId}})
            await tx.createTask(Node, {"name": "sub"}, {**OWN_CONVERSATION, "conversationId": child["id"]})

        await api.commit(change, callContext)
        return {"output": [{"type": "text", "text": "started"}]}

    addTool(
        setup.registry,
        defineTool(
            {
                "name": "delegate",
                "description": "Starts work in a conversation it owns",
                "parameters": EMPTY_OBJECT,
                "execute": delegate,
            }
        ),
    )
    setup.faux.setResponses([toolCalls(("delegate", "c1")), fauxAssistantMessage([fauxText("done")])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    listened = await listen(harness, root)
    submission = await root.submit({"type": "input", "content": "go"}, context)

    async def toolDone() -> bool:
        live = await harness.snapshot(LiveDoc, root.id, context)
        tools = None if live is None else live.get("tools")
        return tools is not None and len(tools) > 0 and tools[0]["status"] == "done"  # type: ignore[no-any-return]

    await waitFor(toolDone)
    live = await harness.snapshot(LiveDoc, root.id, context)
    tool = live["tools"][0]["taskId"]
    assert live["tools"][0].get("entry") is not None
    assert (await state(harness, tool))["status"] == "completing"
    assert (await state(harness, live["run"]["taskId"]))["status"] == "waiting"
    await waitFor(lambda: "end:c1:True" in labels(listened.events))
    assert await settled(submission.wait(context)) is False
    openGate("sub")
    assert (await submission.wait(context))["status"] == "done"
    assert await outcomeOf(harness, tool) == "completed"
    await listened.stream.stop()
    await harness.close(context)


async def test_tool_rounds_and_events_holds_a_faulted_tools_slot_and_task_failed_until_the_work_it_owns_drained():
    setup = chatSetup()
    addTask(setup.registry, Node)
    script("held", {"abort": slowAbortOf("held")})

    async def broken(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.commit(lambda tx: tx.createTask(Node, {"name": "held"}, owned(api.taskId)), callContext)
        return {"output": [], "details": {"fn": lambda: 1}}

    addTool(
        setup.registry,
        defineTool(
            {
                "name": "broken",
                "description": "Starts owned work, then returns a result that is not strict JSON",
                "parameters": EMPTY_OBJECT,
                "execute": broken,
            }
        ),
    )
    setup.faux.setResponses([toolCalls(("broken", "c1")), fauxAssistantMessage([fauxText("done")])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    listened = await listen(harness, root)
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await waitFor(lambda: "abort:held" in log)
    live = await harness.snapshot(LiveDoc, root.id, context)
    tool = live["tools"][0]["taskId"]
    toMatchObject(await state(harness, tool), {"status": "completing", "outcome": {"status": "faulted"}})
    assert live["tools"][0]["status"] != "done"
    assert not any(event["type"] == "task_failed" for event in listened.events)
    openGate("abort.held")
    assert (await submission.wait(context))["status"] == "done"
    assert await outcomeOf(harness, tool) == "faulted"
    await waitFor(lambda: any(event["type"] == "task_failed" for event in listened.events))
    assert "end:c1:False" in labels(listened.events)
    await listened.stream.stop()
    await harness.close(context)
