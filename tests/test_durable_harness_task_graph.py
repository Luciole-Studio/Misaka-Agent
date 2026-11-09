"""pi durable's ``test/harness-task-graph.test.ts``."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest

from misaka.chord.context import withAbortSignal
from misaka.chord.delta import applyImmutable
from misaka.durable import MemoryStorage, defineTask
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.utils.js import AbortController
from tests.durable_support import ControlledStorage, context, flush
from tests.task_support import aborted, completed, deferred, eventually, openTasks, race


@pytest.fixture
def sqlitePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-graph-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


async def noAbort(_task: Any, runtime: Any, ctx: Any) -> None:
    return None


def family(gates: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A parent that creates a child task and a conversation it owns, waits for the child, creates a second child, and
    completes while that child is still live, so its outcome is held as ``completing``. Returns ``(Parent, Child)``."""

    async def work(task: Any, runtime: Any, ctx: Any) -> None:
        await (gates["late"] if task["input"]["late"] else gates["child"])
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Child = defineTask(
        {
            "name": "test.graph-child",
            "version": 1,
            "initial": lambda input: {"phase": "work"},
            "phases": {"work": work},
            "abort": noAbort,
        }
    )

    async def spawn(task: Any, runtime: Any, ctx: Any) -> None:
        ownership = {"kind": "task", "taskId": task["id"]}

        # Two conversations in a commit that leaves the parent's record unchanged.
        async def conversations(tx: Any, current: Any) -> None:
            await tx.createConversation({"ownership": ownership})
            await tx.createConversation({"ownership": ownership})

        await runtime.commit(conversations, ctx)

        async def child(tx: Any, current: Any) -> dict[str, Any]:
            created = await tx.createTask(Child, {"late": False}, {"ownership": ownership})
            return {"status": "waiting", "checkpoint": {"phase": "join", "child": created}, "on": [created], "policy": "allSettled"}

        await runtime.commit(child, ctx)

    async def join(task: Any, runtime: Any, ctx: Any) -> None:
        async def late(tx: Any, current: Any) -> dict[str, Any]:
            await tx.createTask(Child, {"late": True}, {"ownership": {"kind": "task", "taskId": task["id"]}})
            return {"status": "running", "checkpoint": {"phase": "finish"}}

        await runtime.commit(late, ctx)

    async def finish(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Parent = defineTask(
        {
            "name": "test.graph-parent",
            "version": 1,
            "initial": lambda input: {"phase": "spawn"},
            "phases": {"spawn": spawn, "join": join, "finish": finish},
            "abort": noAbort,
        }
    )
    return Parent, Child


def gatesNever() -> dict[str, Any]:
    never: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    return {"child": never, "late": never}


def statuses(graph: dict[str, Any]) -> dict[str, str]:
    """Node states by task kind, for compact assertions."""
    return {f"{node['kind']}#{node['id']}": node["state"]["status"] for node in graph["tasks"].values()}


def statusOf(graph: dict[str, Any], id: int) -> str | None:
    node = graph["tasks"].get(str(id))
    return None if node is None else node["state"]["status"]  # type: ignore[no-any-return]


async def test_follows_every_live_task_through_its_statuses_owner_edges_and_owned_conversations():
    child = deferred()
    late = deferred()
    Parent, Child = family({"child": child.promise, "late": late.promise})
    harness, _registry, _reports = await openTasks(MemoryStorage(), [Parent, Child])
    root = await harness.root(context)
    seen: list[Any] = []

    async def observe() -> Any:
        opened = await harness.taskGraph(context)

        def listener(value: Any, _context: Any, delivery: Any) -> None:
            if delivery["kind"] == "update":
                seen.append(value)

        opened.subscribe(listener)
        return opened

    graph = await observe()
    assert graph.value == {"tasks": {}}

    async def rebuild() -> None:
        """The advanced value equals a fresh build from Storage once the last observer left."""
        nonlocal graph
        advanced = graph.value
        graph.dispose()
        graph = await observe()
        assert graph.value is not advanced
        assert graph.value == advanced

    parent = await root.commit(lambda tx: tx.createTask(Parent, None, {"ownership": {"kind": "conversation"}}), context)
    await flush()
    assert graph.value["tasks"][str(parent)] == {
        "id": parent,
        "kind": "test.graph-parent",
        "conversationId": root.id,
        "background": False,
        "abortRequested": False,
        "state": {"status": "pending", "phase": "spawn"},
        "conversations": [],
    }

    harness.resume()

    def childId() -> str:
        node = next((node for node in graph.value["tasks"].values() if node["kind"] == "test.graph-child"), None)
        return "" if node is None else str(node["id"])

    await eventually(
        lambda: len(graph.value["tasks"]) == 2 and statuses(graph.value).get(f"test.graph-child#{childId()}") == "running"
    )
    first = int(childId())
    parentNode = graph.value["tasks"][str(parent)]
    assert parentNode["state"] == {"status": "waiting", "phase": "join", "on": [first], "policy": "allSettled"}
    assert len(parentNode["conversations"]) == 2
    assert sorted(parentNode["conversations"]) == parentNode["conversations"]
    owned = parentNode["conversations"][0]
    firstNode = graph.value["tasks"][str(first)]
    assert firstNode["owner"] == parent
    assert firstNode["conversationId"] == root.id
    assert await harness.conversation(owned, context) is not None
    await rebuild()

    child.resolve()
    # The parent completes while the late child lives: its outcome is held.
    await eventually(lambda: statusOf(graph.value, parent) == "completing")
    assert graph.value["tasks"][str(parent)]["state"] == {"status": "completing", "outcome": "completed"}
    assert str(first) not in graph.value["tasks"]
    # Owned conversations stay listed while the owner lives.
    assert graph.value["tasks"][str(parent)]["conversations"] == parentNode["conversations"]
    await rebuild()

    late.resolve()
    await harness.waitForTask(parent, context)
    await flush()
    assert graph.value == {"tasks": {}}
    # Every revision is one commit that changed a node; none repeats its predecessor.
    for index in range(1, len(seen)):
        assert seen[index] != seen[index - 1]
    # The commit that created the two conversations published one revision setting them.
    assert any(
        str(parent) in value["tasks"] and len(value["tasks"][str(parent)]["conversations"]) == 2 for value in seen
    )
    graph.dispose()
    await harness.close(context)


async def test_builds_from_committed_tasks_shows_surviving_tasks_as_pending_after_reopen_and_marks_aborts(sqlitePath):
    gate = deferred()

    async def work(_task: Any, runtime: Any, ctx: Any) -> None:
        await race(gate.promise, aborted(runtime.signal))
        await runtime.commit(lambda tx, current: completed(None), ctx)

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted", "reason": "test"}}, ctx)

    Work = defineTask(
        {
            "name": "test.graph-work",
            "version": 1,
            "initial": lambda input: {"phase": "work"},
            "phases": {"work": work},
            "abort": abort,
        }
    )
    first, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Work])
    firstRoot = await first.root(context)

    async def create(tx: Any) -> list[int]:
        foreground = await tx.createTask(Work, None, {"ownership": {"kind": "conversation"}})
        background = await tx.createTask(Work, None, {"ownership": {"kind": "conversation"}, "background": True})
        owned = await tx.createConversation({"ownership": {"kind": "task", "taskId": foreground}})
        return [foreground, background, owned["id"]]

    foreground, background, owned = await firstRoot.commit(create, context)
    first.resume()

    async def allRunning() -> bool:
        return all(task["state"]["kind"] == "running" for task in (await first.inspect(context))["tasks"])

    await eventually(allRunning)
    running = await first.taskGraph(context)
    assert statuses(running.value) == {
        f"test.graph-work#{foreground}": "running",
        f"test.graph-work#{background}": "running",
    }
    await first.close(context)

    # Acquired after reopen: built from the committed records and owner edges; open reconciled running to pending.
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Work])
    watch = await harness.watchTaskGraph(context)
    assert statuses(watch.value) == {
        f"test.graph-work#{foreground}": "pending",
        f"test.graph-work#{background}": "pending",
    }
    assert watch.value["tasks"][str(foreground)]["conversations"] == [owned]
    assert watch.value["tasks"][str(background)]["background"] is True

    # Exact frames: replaying their operations from the acquisition revision gives each delivered value.
    replica = watch.value
    frames: list[Any] = []

    async def onFrame(value: Any, ops: Any, callContext: Any) -> None:
        nonlocal replica
        replica = applyImmutable(replica, ops)
        assert replica == value
        frames.append(ops)

    watch.start(onFrame)
    harness.resume()
    await eventually(lambda: statusOf(watch.value, background) == "running")
    assert await harness.abortTask(background, context) == "marked"
    await harness.waitForTask(background, context)
    await flush()
    assert any(
        len(ops) == 1 and ops[0][0] == "s" and ops[0][1] == ["tasks", str(background)] and ops[0][2]["abortRequested"] is True
        for ops in frames
    )
    assert frames[-1] == [["d", ["tasks", str(background)]]]
    assert list(replica["tasks"].keys()) == [str(foreground)]
    await watch.stop()
    gate.resolve()
    await harness.waitForTask(foreground, context)
    await harness.close(context)


async def test_lists_owned_conversations_in_id_order_whatever_order_one_commit_creates_them_in():
    gate = deferred()
    created: list[int] = []

    async def spawn(task: Any, runtime: Any, ctx: Any) -> None:
        async def make(tx: Any, current: Any) -> None:
            ownership = {"kind": "task", "taskId": task["id"]}
            forked, fresh = await asyncio.gather(
                tx.forkConversation(runtime.conversationId, task["input"]["at"], {"ownership": ownership}),
                tx.createConversation({"ownership": ownership}),
            )
            created.extend([forked["id"], fresh["id"]])

        await runtime.commit(make, ctx)
        await gate.promise
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Spawner = defineTask(
        {
            "name": "test.graph-spawner",
            "version": 1,
            "initial": lambda input: {"phase": "spawn"},
            "phases": {"spawn": spawn},
            "abort": noAbort,
        }
    )
    harness, _registry, _reports = await openTasks(MemoryStorage(), [Spawner])
    root = await harness.root(context)
    graph = await harness.taskGraph(context)

    async def create(tx: Any) -> int:
        entry = await tx.appendEntry(root.id, {"kind": "note"})
        return await tx.createTask(Spawner, {"at": entry["id"]}, {"ownership": {"kind": "conversation"}})  # type: ignore[no-any-return]

    id = await root.commit(create, context)
    harness.resume()
    await eventually(lambda: len(graph.value["tasks"].get(str(id), {}).get("conversations", [])) == 2)
    advanced = graph.value
    assert advanced["tasks"][str(id)]["conversations"] == sorted(created)
    graph.dispose()
    rebuilt = await harness.taskGraph(context)
    assert rebuilt.value == advanced
    rebuilt.dispose()
    gate.resolve()
    await harness.waitForTask(id, context)
    await harness.close(context)


async def test_registers_nothing_for_an_acquisition_cancelled_while_it_waits_for_the_line():
    storage = ControlledStorage()
    harness, _registry, _reports = await openTasks(storage, [])
    root = await harness.root(context)
    Child = family(gatesNever())[1]
    await root.commit(lambda tx: tx.createTask(Child, {"late": False}, {"ownership": {"kind": "conversation"}}), context)
    held = storage.holdCommits()

    async def blocker(tx: Any) -> None:
        await tx.appendEntry(root.id, {"kind": "blocker"})

    blocking = harness.commit(blocker, context)
    await held.entered
    controller = AbortController()
    cancelled = harness.watchTaskGraph(withAbortSignal(controller.signal, context))
    controller.abort(RuntimeError("cancelled"))
    held.release()
    await blocking
    with pytest.raises(Exception, match="cancelled"):
        await cancelled
    # No observer kept the mount: each new observer builds a new revision.
    first = await harness.taskGraph(context)
    value = first.value
    first.dispose()
    second = await harness.taskGraph(context)
    assert second.value is not value
    assert second.value == value
    second.dispose()
    await harness.close(context)


async def test_publishes_no_revision_for_a_commit_that_changes_no_node_and_shares_one_mount_between_observers():
    gate = deferred()
    reached = deferred()

    async def work(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.memo("seen", True, ctx)
        reached.resolve()
        await gate.promise
        await runtime.commit(lambda tx, current: completed(None), ctx)

    Memo = defineTask(
        {
            "name": "test.graph-memo",
            "version": 1,
            "initial": lambda input: {"phase": "work"},
            "phases": {"work": work},
            "abort": noAbort,
        }
    )
    harness, _registry, _reports = await openTasks(MemoryStorage(), [Memo])
    root = await harness.root(context)
    id = await root.commit(lambda tx: tx.createTask(Memo, None, {"ownership": {"kind": "conversation"}}), context)
    first = await harness.taskGraph(context)
    second = await harness.taskGraph(context)
    assert second.value is first.value
    updates: list[str] = []

    def listener(value: Any, _context: Any, delivery: Any) -> None:
        if delivery["kind"] == "update":
            node = value["tasks"].get(str(id))
            updates.append("gone" if node is None else node["state"]["status"])

    first.subscribe(listener)
    harness.resume()
    await reached.promise
    await flush()
    # Reservation changed the node; the memo commit did not.
    assert updates == ["running"]
    gate.resolve()
    await harness.waitForTask(id, context)
    await flush()
    assert updates == ["running", "gone"]
    last = first.value
    first.dispose()
    second.dispose()
    # No observer is left, so the mount was dropped: a new observer builds a new revision.
    rebuilt = await harness.taskGraph(context)
    assert rebuilt.value == last
    assert rebuilt.value is not last
    rebuilt.dispose()
    await harness.close(context)
