"""pi durable's ``test/harness-scheduler-retention.test.ts``.

Scheduler memory is bounded by live work: an ended task or a conversation edge is kept only while live work is below it,
and a chain dropped that way is loaded again from storage when work appears below it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.durable import LiveDoc, defineTask
from misaka.durable.harness.harness import SCHEDULER_INDEX_SIZES
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from tests.durable_support import context, toMatchObject
from tests.task_support import aborted, deferred, eventually, openTasks, race, settled

gates: dict[str, Any] = {}


def gate(name: str) -> Any:
    found = gates.get(name)
    if found is None:
        found = deferred()
        gates[name] = found
    return found


async def gateOpened(name: str) -> bool:
    await gate(name).promise
    return True


# How often each named task started its phase.
runs: dict[str, int] = {}

COMPLETED = {"status": "terminal", "outcome": {"status": "completed", "result": None}}
ABORTED = {"status": "terminal", "outcome": {"status": "aborted"}}


async def endAborted(_task: Any, runtime: Any, ctx: Any) -> None:
    await runtime.commit(lambda tx, current: ABORTED, ctx)


async def holdPhase(task: Any, runtime: Any, ctx: Any) -> None:
    name = task["input"]["name"]
    runs[name] = runs.get(name, 0) + 1
    opened = await race(gateOpened(name), aborted(runtime.signal))
    if opened is not True:
        return
    await runtime.commit(lambda tx, current: COMPLETED, ctx)


# Holds until its gate opens, then completes; its abort handler ends it `aborted`.
Hold = defineTask(
    {
        "name": "test.retention.hold",
        "version": 1,
        "initial": lambda input: {"phase": "hold"},
        "phases": {"hold": holdPhase},
        "abort": endAborted,
    }
)


async def spawnerPhase(task: Any, runtime: Any, ctx: Any) -> None:
    opened = await race(gateOpened(task["input"]["name"]), aborted(runtime.signal))
    if opened is not True:
        return

    async def spawn(tx: Any, _current: Any) -> dict[str, Any]:
        await tx.createTask(
            Hold,
            {"name": f"inside.{task['input']['name']}"},
            {"ownership": {"kind": "conversation"}, "conversationId": task["input"]["into"]},
        )
        return COMPLETED

    await runtime.commit(spawn, ctx)


# Holds until its gate opens, then creates a held task `inside.<name>` in conversation `input.into` and ends.
Spawner = defineTask(
    {
        "name": "test.retention.spawner",
        "version": 1,
        "initial": lambda input: {"phase": "hold"},
        "phases": {"hold": spawnerPhase},
        "abort": endAborted,
    }
)

# Rejected commits it caught.
rejections: list[Any] = []


async def rejectedPhase(task: Any, runtime: Any, ctx: Any) -> None:
    opened = await race(gateOpened("rejected"), aborted(runtime.signal))
    if opened is not True:
        return

    async def terminal(tx: Any, _current: Any) -> dict[str, Any]:
        await tx.createTask(
            Hold, {"name": "staged"}, {"ownership": {"kind": "conversation"}, "conversationId": task["input"]["into"]}
        )
        await tx.createTask(Hold, {"name": "child"}, {"ownership": {"kind": "task", "taskId": task["id"]}})
        return COMPLETED

    try:
        await runtime.commit(terminal, ctx)
    except Exception as error:  # noqa: BLE001 - the test inspects what was rejected
        rejections.append(error)
    await aborted(runtime.signal)


# Holds until `rejected` opens, then tries one terminal commit that creates a task in conversation `into` and a child of
# its own, which the Transaction rejects as the task is ending. Catches the rejection and holds.
Rejected = defineTask(
    {
        "name": "test.retention.rejected",
        "version": 1,
        "initial": lambda input: {"phase": "hold"},
        "phases": {"hold": rejectedPhase},
        "abort": endAborted,
    }
)


async def waiterWait(task: Any, runtime: Any, ctx: Any) -> None:
    await runtime.commit(
        lambda tx, current: {
            "status": "waiting",
            "checkpoint": {"phase": "done"},
            "on": task["input"]["on"],
            "policy": "allSettled",
        },
        ctx,
    )


async def waiterDone(_task: Any, runtime: Any, ctx: Any) -> None:
    await runtime.commit(lambda tx, current: COMPLETED, ctx)


# Waits on the tasks in its input, then completes.
Waiter = defineTask(
    {
        "name": "test.retention.waiter",
        "version": 1,
        "initial": lambda input: {"phase": "wait"},
        "phases": {"wait": waiterWait, "done": waiterDone},
        "abort": endAborted,
    }
)


def sizes(harness: Any) -> dict[str, int]:
    return getattr(harness, SCHEDULER_INDEX_SIZES)  # type: ignore[no-any-return]


directories: list[str] = []


@pytest.fixture(autouse=True)
def cleanUp() -> Iterator[None]:
    yield
    gates.clear()
    runs.clear()
    rejections.clear()
    for directory in directories:
        shutil.rmtree(directory, ignore_errors=True)
    directories.clear()


def sqlitePath() -> str:
    directory = tempfile.mkdtemp(prefix="pi-durable-retention-")
    directories.append(directory)
    return os.path.join(directory, "session.sqlite")


def countReads(storage: Any) -> SimpleNamespace:
    """Count the task and conversation reads a storage serves."""
    reads = SimpleNamespace(tasks=0, conversations=0)
    task = storage.task
    conversation = storage.conversation

    def countedTask(id: Any, readContext: Any) -> Any:
        reads.tasks += 1
        return task(id, readContext)

    def countedConversation(id: Any, readContext: Any) -> Any:
        reads.conversations += 1
        return conversation(id, readContext)

    storage.task = countedTask
    storage.conversation = countedConversation
    return reads


async def open_(storage: Any = None) -> SimpleNamespace:
    harness, registry, reports = await openTasks(
        storage if storage is not None else await openNodeSqliteStorage(sqlitePath()), [Hold, Spawner, Rejected]
    )
    root = await harness.root(context)
    harness.resume()
    return SimpleNamespace(harness=harness, registry=registry, reports=reports, root=root)


async def status(harness: Any, id: int) -> dict[str, Any]:
    return (await harness.getTask(id, context))["state"]  # type: ignore[no-any-return]


async def subagentCycle(harness: Any, root: Any, parent: int, n: int) -> int:
    """Under live ``parent``: tool task T owning conversation C, a task in C; all of them end. Returns C."""

    async def create(tx: Any) -> dict[str, Any]:
        tool = await tx.createTask(Hold, {"name": f"tool{n}"}, {"ownership": {"kind": "task", "taskId": parent}})
        child = await tx.createConversation({"ownership": {"kind": "task", "taskId": tool}})
        inner = await tx.createTask(
            Hold, {"name": f"inner{n}"}, {"ownership": {"kind": "conversation"}, "conversationId": child["id"]}
        )
        return {"tool": tool, "inner": inner, "child": child["id"]}

    created = await root.commit(create, context)
    gate(f"inner{n}").resolve()
    gate(f"tool{n}").resolve()
    await harness.waitForTask(created["inner"], context)
    await harness.waitForTask(created["tool"], context)
    return created["child"]  # type: ignore[no-any-return]


def createHeld(name: str, ownership: dict[str, Any], **options: Any) -> Any:
    return lambda tx: tx.createTask(Hold, {"name": name}, {"ownership": ownership, **options})


CONVERSATION = {"kind": "conversation"}


def ownedBy(taskId: int) -> dict[str, Any]:
    return {"kind": "task", "taskId": taskId}


class TestSchedulerRetention:
    async def test_forgets_ended_subagent_chains_once_their_work_ends(self):
        opened = await open_()
        harness, root = opened.harness, opened.root
        parent = await root.commit(createHeld("parent", CONVERSATION), context)
        await subagentCycle(harness, root, parent, 0)
        baseline = sizes(harness)
        for n in range(1, 201):
            await subagentCycle(harness, root, parent, n)
        assert sizes(harness) == baseline
        gate("parent").resolve()
        await harness.waitForTask(parent, context)
        await harness.close(context)

    async def test_holds_a_live_owner_for_work_later_created_in_a_forgotten_conversation_loading_its_chain_again(self):
        storage = await openNodeSqliteStorage(sqlitePath())
        reads = countReads(storage)
        opened = await open_(storage)
        harness, root = opened.harness, opened.root

        # Live P -> ended B -> ended A -> conversation C, empty: forgotten.
        async def create(tx: Any) -> dict[str, Any]:
            p = await tx.createTask(Hold, {"name": "p"}, {"ownership": CONVERSATION})
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": ownedBy(p)})
            a = await tx.createTask(Hold, {"name": "a"}, {"ownership": ownedBy(b)})
            c = await tx.createConversation({"ownership": ownedBy(a)})
            return {"p": p, "b": b, "a": a, "c": c["id"]}

        tree = await root.commit(create, context)
        gate("a").resolve()
        gate("b").resolve()
        await harness.waitForTask(tree["b"], context)
        before = sizes(harness)
        loaded = reads.tasks + reads.conversations
        c = await harness.conversation(tree["c"], context)
        d = await c.commit(createHeld("d", CONVERSATION), context)
        gate("p").resolve()

        async def completing() -> bool:
            return (await status(harness, tree["p"]))["status"] == "completing"

        await eventually(completing)
        assert reads.tasks + reads.conversations > loaded
        gate("d").resolve()
        assert (await harness.waitForTask(d, context))["state"]["outcome"]["status"] == "completed"
        assert (await harness.waitForTask(tree["p"], context))["state"]["outcome"]["status"] == "completed"
        assert sizes(harness)["settled"] <= before["settled"]
        await harness.close(context)

    async def test_holds_an_owner_for_work_its_own_terminal_commit_creates_below_a_forgotten_chain(self):
        opened = await open_()
        harness, root = opened.harness, opened.root

        # Live P (a spawner) -> ended B -> ended A -> conversation C, empty: forgotten. P's terminal commit creates D in C.
        async def create(tx: Any) -> dict[str, Any]:
            p = await tx.createTask(Spawner, {"name": "p", "into": 0}, {"ownership": CONVERSATION})
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": ownedBy(p)})
            a = await tx.createTask(Hold, {"name": "a"}, {"ownership": ownedBy(b)})
            c = await tx.createConversation({"ownership": ownedBy(a)})
            return {"p": p, "b": b, "c": c["id"]}

        tree = await root.commit(create, context)

        # Point the spawner at C, now that its ID is known.
        async def point(tx: Any) -> None:
            record = await tx.task(tree["p"])
            tx.setTask({**record, "input": {"name": "p", "into": tree["c"]}})

        await root.commit(point, context)
        gate("a").resolve()
        gate("b").resolve()
        await harness.waitForTask(tree["b"], context)
        gate("p").resolve()

        async def notRunning() -> bool:
            return (await status(harness, tree["p"]))["status"] != "running"

        await eventually(notRunning)
        assert (await status(harness, tree["p"]))["status"] == "completing"
        gate("inside.p").resolve()
        assert (await harness.waitForTask(tree["p"], context))["state"]["outcome"]["status"] == "completed"
        await harness.close(context)

    async def test_withdraws_an_input_queued_in_a_forgotten_conversation_below_an_abort_marked_owner(self):
        opened = await open_()
        harness, root = opened.harness, opened.root

        # Live P -> ended B -> conversation C with no task; C gets a queued input while P is cancelling.
        async def create(tx: Any) -> dict[str, Any]:
            p = await tx.createTask(Hold, {"name": "p"}, {"ownership": CONVERSATION})
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": ownedBy(p)})
            c = await tx.createConversation({"ownership": ownedBy(b)})
            # A live child keeps P holding its abort until the input is withdrawn and the child ends.
            keep = await tx.createTask(Hold, {"name": "keep"}, {"ownership": ownedBy(p)})
            return {"p": p, "b": b, "c": c["id"], "keep": keep}

        tree = await root.commit(create, context)
        gate("b").resolve()
        await harness.waitForTask(tree["b"], context)
        c = await harness.conversation(tree["c"], context)

        # Busy, so the input queues instead of starting a run.
        async def busy(tx: Any) -> None:
            (await tx.doc(LiveDoc, tree["c"]))["run"] = {"taskId": tree["keep"], "inputs": []}

        await c.commit(busy, context)
        queued = (await c.submit({"type": "input", "content": "later"}, context)).id
        await harness.abortTask(tree["p"], context)

        async def unanswered() -> bool:
            settledState = await (await harness.submission(queued, context)).status(context)
            return settledState["status"] == "unanswered"

        await eventually(unanswered)
        toMatchObject(await (await harness.submission(queued, context)).status(context), {"reason": "aborted"})
        await harness.waitForTask(tree["p"], context)
        await harness.close(context)

    async def test_aborts_work_created_in_a_forgotten_conversation_below_an_abort_marked_owner_before_it_runs(self):
        opened = await open_()
        harness, root = opened.harness, opened.root

        async def create(tx: Any) -> dict[str, Any]:
            p = await tx.createTask(Hold, {"name": "p"}, {"ownership": CONVERSATION})
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": ownedBy(p)})
            c = await tx.createConversation({"ownership": ownedBy(b)})
            keep = await tx.createTask(Hold, {"name": "keep"}, {"ownership": ownedBy(p)})
            return {"p": p, "b": b, "c": c["id"], "keep": keep}

        tree = await root.commit(create, context)
        gate("b").resolve()
        await harness.waitForTask(tree["b"], context)

        # P cancels; its live child keeps it holding.
        async def cancel(tx: Any) -> None:
            record = await tx.task(tree["p"])
            tx.setTask({**record, "abortRequested": True})

        await harness.commit(cancel, context)

        async def keepAborting() -> bool:
            return (await harness.getTask(tree["keep"], context))["abortRequested"]  # type: ignore[no-any-return]

        await eventually(keepAborting)
        c = await harness.conversation(tree["c"], context)
        late = await c.commit(createHeld("late", CONVERSATION), context)
        assert (await harness.waitForTask(late, context))["state"]["outcome"]["status"] == "aborted"
        assert runs.get("late", 0) == 0
        await harness.close(context)

    async def test_keeps_a_scope_busy_while_the_chain_of_new_work_below_a_forgotten_conversation_loads(self):
        path = sqlitePath()
        storage = await openNodeSqliteStorage(path)
        opened = await open_(storage)
        harness, root = opened.harness, opened.root

        async def create(tx: Any) -> dict[str, Any]:
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": CONVERSATION})
            c = await tx.createConversation({"ownership": ownedBy(b)})
            return {"b": b, "c": c["id"]}

        tree = await root.commit(create, context)
        gate("b").resolve()
        await harness.waitForTask(tree["b"], context)
        # Hold the chain load's read of C: the second read once holding, after the creating commit's own.
        read = storage.conversation
        held = deferred()
        reached = deferred()
        holding = SimpleNamespace(on=False, readsOfC=0)

        async def heldRead(id: Any, readContext: Any) -> Any:
            if holding.on and id == tree["c"]:
                holding.readsOfC += 1
                if holding.readsOfC == 2:
                    reached.resolve()
                    await held.promise
            return await read(id, readContext)

        storage.conversation = heldRead
        c = await harness.conversation(tree["c"], context)
        holding.on = True
        await c.commit(createHeld("d", CONVERSATION), context)
        await reached.promise
        assert await settled(harness.waitForIdle(context)) is False
        held.resolve()
        gate("d").resolve()
        await harness.waitForIdle(context)
        await harness.close(context)

    async def test_keeps_a_chain_when_one_commit_ends_its_last_task_and_creates_new_work_below_it(self):
        opened = await open_()
        harness, root = opened.harness, opened.root

        # Live P -> ended B -> conversation C with background task `old`, which keeps the chain known without holding B.
        async def create(tx: Any) -> dict[str, Any]:
            p = await tx.createTask(Hold, {"name": "p"}, {"ownership": CONVERSATION})
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": ownedBy(p)})
            c = await tx.createConversation({"ownership": ownedBy(b)})
            old = await tx.createTask(
                Hold, {"name": "old"}, {"ownership": CONVERSATION, "conversationId": c["id"], "background": True}
            )
            return {"p": p, "b": b, "c": c["id"], "old": old}

        tree = await root.commit(create, context)
        gate("b").resolve()
        await harness.waitForTask(tree["b"], context)

        # One commit: the old task ends, which leaves nothing live below B, and a replacement starts in C.
        async def replace(tx: Any) -> int:
            old = await tx.task(tree["old"])
            id = await tx.createTask(Hold, {"name": "new"}, {"ownership": CONVERSATION, "conversationId": tree["c"]})
            tx.setTask({**old, "state": COMPLETED})
            return id  # type: ignore[no-any-return]

        replacement = await harness.commit(replace, context)
        # The chain stayed known through the publication, so P holds for the replacement.
        gate("p").resolve()

        async def completing() -> bool:
            return (await status(harness, tree["p"]))["status"] == "completing"

        await eventually(completing)
        gate("new").resolve()
        await harness.waitForTask(replacement, context)
        assert (await harness.waitForTask(tree["p"], context))["state"]["outcome"]["status"] == "completed"
        await harness.close(context)

    async def test_drops_what_a_rejected_commit_s_chain_load_read_without_a_later_publication(self):
        opened = await open_()
        harness, root = opened.harness, opened.root

        # Live P owns ended B, which owns conversation C: forgotten. P's terminal commit stages work in C, which loads C's
        # chain, and a child of P, which the Transaction rejects, as P is ending.
        async def create(tx: Any) -> dict[str, Any]:
            p = await tx.createTask(Rejected, {"into": 0}, {"ownership": CONVERSATION})
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": ownedBy(p)})
            c = await tx.createConversation({"ownership": ownedBy(b)})
            return {"p": p, "b": b, "c": c["id"]}

        tree = await root.commit(create, context)

        async def point(tx: Any) -> None:
            record = await tx.task(tree["p"])
            tx.setTask({**record, "input": {"into": tree["c"]}})

        await root.commit(point, context)
        gate("b").resolve()
        await harness.waitForTask(tree["b"], context)
        baseline = sizes(harness)
        gate("rejected").resolve()
        await eventually(lambda: len(rejections) > 0)
        # The rejected commit published nothing; the sweep job still cleans up what its chain load read.
        await eventually(lambda: sizes(harness)["dropQueue"] == 0)
        assert sizes(harness) == baseline
        await harness.close(context)

    async def test_drops_what_a_read_loaded_after_reopen_without_a_later_publication(self):
        path = sqlitePath()
        opened = await open_(await openNodeSqliteStorage(path))

        # A live task D in conversation C, owned by ended B: after reopen D's chain is not loaded.
        async def create(tx: Any) -> dict[str, Any]:
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": CONVERSATION})
            c = await tx.createConversation({"ownership": ownedBy(b)})
            d = await tx.createTask(Hold, {"name": "d"}, {"ownership": CONVERSATION, "conversationId": c["id"]})
            return {"b": b, "d": d}

        tree = await opened.root.commit(create, context)
        gate("b").resolve()

        async def completing() -> bool:
            return (await status(opened.harness, tree["b"]))["status"] == "completing"

        await eventually(completing)
        await opened.harness.close(context)
        gates.clear()
        opened = await open_(await openNodeSqliteStorage(path))
        await opened.harness.inspect(context)
        # D is still live, so its chain stays; once D ends, everything below the root goes, published or not.
        assert sizes(opened.harness)["unloaded"] == 0
        gate("d").resolve()
        await opened.harness.waitForTask(tree["d"], context)
        await opened.harness.waitForTask(tree["b"], context)
        await eventually(lambda: sizes(opened.harness)["dropQueue"] == 0)
        toMatchObject(sizes(opened.harness), {"settled": 0, "below": 0, "unloaded": 0})
        await opened.harness.close(context)

    async def test_withdraws_an_input_queued_two_ended_levels_below_an_owner_whose_intent_appears_later(self):
        opened = await open_()
        harness, root = opened.harness, opened.root

        # Live P -> ended B -> ended A -> conversation C with no task, and a live child keeping P holding.
        async def create(tx: Any) -> dict[str, Any]:
            p = await tx.createTask(Hold, {"name": "p"}, {"ownership": CONVERSATION})
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": ownedBy(p)})
            a = await tx.createTask(Hold, {"name": "a"}, {"ownership": ownedBy(b)})
            c = await tx.createConversation({"ownership": ownedBy(a)})
            keep = await tx.createTask(Hold, {"name": "keep"}, {"ownership": ownedBy(p)})
            return {"p": p, "b": b, "c": c["id"], "keep": keep}

        tree = await root.commit(create, context)
        gate("a").resolve()
        gate("b").resolve()
        await harness.waitForTask(tree["b"], context)
        c = await harness.conversation(tree["c"], context)

        async def busy(tx: Any) -> None:
            (await tx.doc(LiveDoc, tree["c"]))["run"] = {"taskId": tree["keep"], "inputs": []}

        await c.commit(busy, context)
        # Queued while nothing cancels: no withdrawal scan.
        queued = (await c.submit({"type": "input", "content": "later"}, context)).id
        await eventually(lambda: sizes(harness)["dropQueue"] == 0)

        # P fails while its child runs: it holds the failed outcome, which is cancellation intent.
        async def fail(tx: Any) -> None:
            record = await tx.task(tree["p"])
            tx.setTask(
                {**record, "state": {"status": "completing", "outcome": {"status": "failed", "error": {"message": "x"}}}}
            )

        await harness.commit(fail, context)

        async def unanswered() -> bool:
            return (await (await harness.submission(queued, context)).status(context))["status"] == "unanswered"

        await eventually(unanswered)
        toMatchObject(await (await harness.submission(queued, context)).status(context), {"reason": "aborted"})
        await harness.close(context)

    async def test_forgets_a_20_000_deep_chain_of_ended_tasks_without_recursion(self):
        opened = await open_()
        harness, root = opened.harness, opened.root
        depth = 20_000
        baseline = sizes(harness)

        async def create(tx: Any) -> dict[str, Any]:
            links: list[int] = []
            owner = await tx.createTask(Hold, {"name": "link0"}, {"ownership": CONVERSATION})
            links.append(owner)
            for n in range(1, depth):
                owner = await tx.createTask(Hold, {"name": f"link{n}"}, {"ownership": ownedBy(owner)})
                links.append(owner)
            leaf = await tx.createTask(Hold, {"name": "leaf"}, {"ownership": ownedBy(owner)})
            return {"links": links, "leaf": leaf}

        created = await root.commit(create, context)
        for n in range(depth):
            gate(f"link{n}").resolve()
        gate("leaf").resolve()
        await harness.waitForTask(created["leaf"], context)
        await harness.waitForTask(created["links"][0], context)
        await harness.waitForIdle(context)
        assert sizes(harness) == baseline
        await harness.close(context)

    async def test_keeps_no_more_after_all_work_ended_than_a_fresh_reopen(self):
        path = sqlitePath()
        opened = await open_(await openNodeSqliteStorage(path))
        parent = await opened.root.commit(createHeld("parent", CONVERSATION), context)
        for n in range(20):
            await subagentCycle(opened.harness, opened.root, parent, n)
        gate("parent").resolve()
        await opened.harness.waitForTask(parent, context)
        await opened.harness.waitForIdle(context)
        ended = sizes(opened.harness)
        await opened.harness.close(context)
        opened = await open_(await openNodeSqliteStorage(path))
        await opened.harness.waitForIdle(context)
        assert ended == sizes(opened.harness)
        await opened.harness.close(context)

    async def test_does_not_scan_queued_submissions_for_an_input_when_nothing_cancels(self):
        storage = await openNodeSqliteStorage(sqlitePath())
        scans = [0]
        scan = storage.scanSubmissions

        def countedScan(query: Any, limit: Any, cursor: Any, scanContext: Any) -> Any:
            if query.get("status") == "queued":
                scans[0] += 1
            return scan(query, limit, cursor, scanContext)

        storage.scanSubmissions = countedScan
        opened = await open_(storage)
        harness, root = opened.harness, opened.root
        busy = await root.commit(createHeld("busy", CONVERSATION), context)

        async def markBusy(tx: Any) -> None:
            (await tx.doc(LiveDoc, root.id))["run"] = {"taskId": busy, "inputs": []}

        await root.commit(markBusy, context)
        before = scans[0]
        await root.submit({"type": "input", "content": "queued"}, context)
        # Let any reconcile it would cause run.
        await asyncio.sleep(0.02)
        assert scans[0] == before
        gate("busy").resolve()
        await harness.close(context)

    # A wait on an ended task closes no cycle (#10411), also when the ended task owns the waiter's conversation and the
    # scheduler forgot it: the wait resumes at once.
    async def test_lets_a_task_wait_on_a_forgotten_ended_owner(self):
        harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath()), [Hold, Waiter])
        root = await harness.root(context)
        harness.resume()

        async def create(tx: Any) -> dict[str, Any]:
            b = await tx.createTask(Hold, {"name": "b"}, {"ownership": CONVERSATION})
            c = await tx.createConversation({"ownership": ownedBy(b)})
            return {"b": b, "c": c["id"]}

        tree = await root.commit(create, context)
        gate("b").resolve()
        await harness.waitForTask(tree["b"], context)
        c = await harness.conversation(tree["c"], context)
        waiter = await c.commit(
            lambda tx: tx.createTask(Waiter, {"on": [tree["b"]]}, {"ownership": CONVERSATION}), context
        )
        outcome = (await harness.waitForTask(waiter, context))["state"]["outcome"]
        toMatchObject(outcome, {"status": "completed"})
        await harness.close(context)
