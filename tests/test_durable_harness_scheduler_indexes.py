"""pi durable's ``test/harness-scheduler-indexes.test.ts``.

The scheduler's in-memory indexes: cases where a wrong index changes behaviour, each against what the committed records
imply (spec sections 5.4, 5.5).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest

from misaka.durable import createSession, defineTask
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from tests.durable_support import context
from tests.task_support import aborted, deferred, eventually, openTasks, race, settled

gates: dict[str, Any] = {}


def gate(name: str) -> Any:
    found = gates.get(name)
    if found is None:
        found = deferred()
        gates[name] = found
    return found


async def opened(name: str) -> bool:
    await gate(name).promise
    return True


async def abortedOutcome(_task: Any, runtime: Any, ctx: Any) -> Any:
    return await runtime.commit(lambda *_: {"status": "terminal", "outcome": {"status": "aborted"}}, ctx)


async def holdRun(task: Any, runtime: Any, ctx: Any) -> None:
    if await race(opened(task["input"]["name"]), aborted(runtime.signal)) is not True:
        return
    await runtime.commit(lambda *_: {"status": "terminal", "outcome": {"status": "completed", "result": None}}, ctx)


# Holds until its gate opens, then completes; its abort handler ends it ``aborted``.
Hold = defineTask(
    {
        "name": "test.index.hold",
        "version": 1,
        "initial": lambda input: {"phase": "hold"},
        "phases": {"hold": holdRun},
        "abort": abortedOutcome,
    }
)

owned: dict[str, list[int]] = {}
"""What ``ownedTasks()`` returned to each run of a ``Probe``, by its name."""
runtimes: dict[str, Any] = {}
"""The runtime of each ``Probe``'s last run, by its name."""


async def probeLook(task: Any, runtime: Any, ctx: Any) -> None:
    name = task["input"]["name"]
    runtimes[name] = runtime
    await race(gate(f"look.{name}").promise, aborted(runtime.signal))
    owned[name] = [record["id"] for record in await runtime.ownedTasks(ctx)]
    await race(gate(name).promise, aborted(runtime.signal))
    await runtime.commit(lambda *_: {"status": "terminal", "outcome": {"status": "completed", "result": None}}, ctx)


# Records its owned tasks once its ``look`` gate opens, then holds until its own gate opens.
Probe = defineTask(
    {
        "name": "test.index.probe",
        "version": 1,
        "initial": lambda input: {"phase": "look"},
        "phases": {"look": probeLook},
        "abort": abortedOutcome,
    }
)


@pytest.fixture(autouse=True)
def clearState() -> Iterator[None]:
    yield
    gates.clear()
    owned.clear()
    runtimes.clear()


@pytest.fixture
def directory() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-indexes-")
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


async def state(harness: Any, id: int) -> dict[str, Any]:
    return await harness.getTask(id, context)  # type: ignore[no-any-return]


def statusIs(harness: Any, id: int, expected: str) -> Any:
    async def check() -> bool:
        return (await state(harness, id))["state"]["status"] == expected

    return check


async def test_scheduler_indexes_takes_a_cascades_reason_from_the_nearest_cancelling_owner(directory: str):
    # top (abort requested) -> middle (restart-marked) -> leaf: the leaf gets middle's `restart` first, as the nearest
    # cancelling owner, and the request only once middle carries it.
    path = os.path.join(directory, "session.sqlite")
    session = createSession(await openNodeSqliteStorage(path))

    async def create(tx: Any) -> dict[str, int]:
        conversation = await tx.createConversation({"ownership": {"kind": "ownerless"}})
        top = await tx.createTask(
            Hold, {"name": "top"}, {"conversationId": conversation["id"], "ownership": {"kind": "conversation"}}
        )
        middle = await tx.createTask(Hold, {"name": "middle"}, {"ownership": {"kind": "task", "taskId": top}})
        leaf = await tx.createTask(Hold, {"name": "leaf"}, {"ownership": {"kind": "task", "taskId": middle}})
        return {"top": top, "middle": middle, "leaf": leaf}

    ids = await session.commit(create, context)

    async def mark(tx: Any) -> None:
        top = await tx.task(ids["top"])
        middle = await tx.task(ids["middle"])
        tx.setTask({**top, "abortRequested": True})
        tx.setTask({**middle, "abortRequested": True, "abortReason": "restart"})

    await session.commit(mark, context)
    await session.close(context)
    # Record every mark of the leaf, in commit order.
    storage = await openNodeSqliteStorage(path)
    reasons: list[Any] = []
    commit = storage.commit

    def recording(writes: Any, commitContext: Any) -> Any:
        for write in writes:
            if write["type"] == "task" and write["value"]["id"] == ids["leaf"] and write["value"].get("abortRequested"):
                reasons.append(write["value"].get("abortReason"))
        return commit(writes, commitContext)

    storage.commit = recording  # type: ignore[method-assign]
    harness, _registry, _reports = await openTasks(storage, [Hold])
    harness.resume()
    for id in [ids["leaf"], ids["middle"], ids["top"]]:
        await harness.waitForTask(id, context)
    assert reasons[0] == "restart"
    await harness.close(context)


async def test_scheduler_indexes_finalizes_a_chain_of_held_owners_in_the_commit_that_frees_the_last_one(directory: str):
    storage = await openNodeSqliteStorage(os.path.join(directory, "session.sqlite"))
    # The commit, by count, that wrote each terminal record.
    endedIn: dict[int, int] = {}
    commits = {"count": 0}
    commit = storage.commit

    def recording(writes: Any, commitContext: Any) -> Any:
        commits["count"] += 1
        for write in writes:
            if write["type"] == "task" and write["value"]["state"]["status"] == "terminal":
                endedIn[write["value"]["id"]] = commits["count"]
        return commit(writes, commitContext)

    storage.commit = recording  # type: ignore[method-assign]
    harness, _registry, _reports = await openTasks(storage, [Hold])
    root = await harness.root(context)
    harness.resume()

    async def create(tx: Any) -> dict[str, int]:
        top = await tx.createTask(Hold, {"name": "top"}, {"ownership": {"kind": "conversation"}})
        middle = await tx.createTask(Hold, {"name": "middle"}, {"ownership": {"kind": "task", "taskId": top}})
        leaf = await tx.createTask(Hold, {"name": "leaf"}, {"ownership": {"kind": "task", "taskId": middle}})
        return {"top": top, "middle": middle, "leaf": leaf}

    ids = await root.commit(create, context)
    top, middle, leaf = ids["top"], ids["middle"], ids["leaf"]
    gate("top").resolve()
    gate("middle").resolve()
    await eventually(statusIs(harness, top, "completing"))
    await eventually(statusIs(harness, middle, "completing"))
    gate("leaf").resolve()
    assert (await harness.waitForTask(top, context))["state"]["outcome"]["status"] == "completed"
    # Both owners end in one finalize commit, after the leaf's.
    assert endedIn.get(middle) == endedIn.get(top)
    assert endedIn[middle] > endedIn[leaf]
    await harness.close(context)


async def test_scheduler_indexes_keeps_a_scope_busy_after_reopen_for_work_below_an_ended_task_whose_chain_is_not_loaded_yet(
    directory: str,
):
    path = os.path.join(directory, "session.sqlite")
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(path), [Hold])
    root = await harness.root(context)
    harness.resume()

    # root conversation -> owner (ends) -> conversation C -> inner (created after owner ended), as a subagent call's.
    async def create(tx: Any) -> dict[str, int]:
        owner = await tx.createTask(Hold, {"name": "owner"}, {"ownership": {"kind": "conversation"}})
        child = await tx.createConversation({"ownership": {"kind": "task", "taskId": owner}})
        return {"owner": owner, "child": child["id"]}

    ids = await root.commit(create, context)
    owner, child = ids["owner"], ids["child"]
    gate("owner").resolve()
    await harness.waitForTask(owner, context)
    conversation = await harness.conversation(child, context)
    inner = await conversation.commit(
        lambda tx: tx.createTask(Hold, {"name": "inner"}, {"ownership": {"kind": "conversation"}}), context
    )
    await harness.close(context)
    gates.clear()
    # Hold the read of C's record, so the chain above `inner` stays unloaded while idle is decided.
    storage = await openNodeSqliteStorage(path)
    read = storage.conversation
    held = deferred()

    async def holding(id: int, readContext: Any) -> Any:
        if id == child:
            await held.promise
        return await read(id, readContext)

    storage.conversation = holding  # type: ignore[method-assign]
    harness, _registry, _reports = await openTasks(storage, [Hold])
    # A task whose chain is not loaded counts as inside every scope. (The held read blocks the Session line, so the root
    # handle comes after.)
    assert await settled(harness.waitForIdle(context)) is False
    held.resolve()
    root = await harness.root(context)
    harness.resume()
    gate("inner").resolve()
    await harness.waitForTask(inner, context)
    await root.waitForIdle(context)
    await harness.close(context)


async def test_owned_tasks_lists_the_live_tasks_a_task_owns_directly_in_id_order_also_after_a_reopen(directory: str):
    path = os.path.join(directory, "session.sqlite")
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(path), [Hold, Probe])
    root = await harness.root(context)
    harness.resume()

    # The probe owns: two held tasks, one that ends, a grandchild below the first, and a conversation with a task.
    async def create(tx: Any) -> dict[str, int]:
        probe = await tx.createTask(Probe, {"name": "probe"}, {"ownership": {"kind": "conversation"}})
        first = await tx.createTask(Hold, {"name": "first"}, {"ownership": {"kind": "task", "taskId": probe}})
        ends = await tx.createTask(Hold, {"name": "ends"}, {"ownership": {"kind": "task", "taskId": probe}})
        second = await tx.createTask(Hold, {"name": "second"}, {"ownership": {"kind": "task", "taskId": probe}})
        await tx.createTask(Hold, {"name": "grandchild"}, {"ownership": {"kind": "task", "taskId": first}})
        child = await tx.createConversation({"ownership": {"kind": "task", "taskId": probe}})
        await tx.createTask(Hold, {"name": "inside"}, {"ownership": {"kind": "conversation"}, "conversationId": child["id"]})
        return {"probe": probe, "first": first, "ends": ends, "second": second}

    ids = await root.commit(create, context)
    gate("ends").resolve()
    await harness.waitForTask(ids["ends"], context)
    # The first ends too, but holds its outcome as `completing` while its grandchild runs: still live, so listed.
    gate("first").resolve()
    await eventually(statusIs(harness, ids["first"], "completing"))
    gate("look.probe").resolve()
    await eventually(lambda: "probe" in owned)
    assert owned["probe"] == [ids["first"], ids["second"]]
    await harness.close(context)

    # After a reopen the rerun sees the same tasks, which an earlier invocation created.
    gates.clear()
    owned.clear()
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(path), [Hold, Probe])
    harness.resume()
    gate("look.probe").resolve()
    await eventually(lambda: "probe" in owned)
    assert owned["probe"] == [ids["first"], ids["second"]]
    await harness.close(context)


async def test_owned_tasks_rejects_once_its_invocation_has_ended(directory: str):
    harness, _registry, _reports = await openTasks(
        await openNodeSqliteStorage(os.path.join(directory, "session.sqlite")), [Hold, Probe]
    )
    root = await harness.root(context)
    harness.resume()
    probe = await root.commit(
        lambda tx: tx.createTask(Probe, {"name": "probe"}, {"ownership": {"kind": "conversation"}}), context
    )
    await eventually(lambda: "probe" in runtimes)
    runtime = runtimes["probe"]
    # Aborting joins the run invocation, which has ended by then.
    await harness.abortTask(probe, context)
    with pytest.raises(Exception, match="invocation has ended"):
        await runtime.ownedTasks(context)
    await harness.close(context)
