"""pi durable's ``test/harness-cancellation-barrier.test.ts``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from misaka.chord.context import Context
from misaka.durable import MemoryStorage, createSession, defineTask
from tests.durable_support import context, flush, toMatchObject
from tests.task_support import aborted, deferred, eventually, openTasks


async def abortedOutcome(_task: Any, runtime: Any, abortContext: Any) -> None:
    await runtime.commit(lambda *_: {"status": "terminal", "outcome": {"status": "aborted"}}, abortContext)


async def dormantWork(_task: Any, runtime: Any, *_: Any) -> None:
    await aborted(runtime.signal)


# Runs until aborted; its abort handler ends it ``aborted``.
Dormant = defineTask(
    {
        "name": "test.dormant",
        "version": 1,
        "initial": lambda input: {"phase": "work"},
        "phases": {"work": dormantWork},
        "abort": abortedOutcome,
    }
)


class HoldMarks(MemoryStorage):
    """Holds every commit that abort-marks ``target``, while set, until released."""

    def __init__(self) -> None:
        super().__init__()
        self.target: int | None = None
        self.held = deferred()
        self.release = deferred()

    async def commit(self, writes: Sequence[dict[str, Any]], commitContext: Context) -> int:  # type: ignore[override]
        if any(
            write["type"] == "task" and write["value"]["id"] == self.target and write["value"].get("abortRequested")
            for write in writes
        ):
            self.held.resolve()
            await self.release.promise
        return await super().commit(writes, commitContext)


async def test_does_not_start_its_next_phase_while_the_cascade_that_marks_it_has_not_committed():
    storage = HoldMarks()
    entered = deferred()
    advance = deferred()
    ran = {"next": False}

    async def first(_task: Any, runtime: Any, phaseContext: Any) -> None:
        entered.resolve()
        await advance.promise
        await runtime.commit(lambda *_: {"status": "running", "checkpoint": {"phase": "next"}}, phaseContext)

    async def next_(_task: Any, runtime: Any, *_: Any) -> None:
        ran["next"] = True
        await aborted(runtime.signal)

    Child = defineTask(
        {
            "name": "test.child",
            "version": 1,
            "initial": lambda input: {"phase": "first"},
            "phases": {"first": first, "next": next_},
            "abort": abortedOutcome,
        }
    )
    harness, _registry, _reports = await openTasks(storage, [Dormant, Child])
    root = await harness.root(context)
    parent = await root.commit(lambda tx: tx.createTask(Dormant, None, {"ownership": {"kind": "conversation"}}), context)
    child = await root.commit(
        lambda tx: tx.createTask(Child, None, {"ownership": {"kind": "task", "taskId": parent}}), context
    )
    storage.target = child
    try:
        harness.resume()
        await entered.promise
        aborting = harness.abortTask(parent, context)
        await storage.held.promise
        # The parent's request is durable, the cascade's mark of the child is not: its phase must not advance.
        advance.resolve()
        for _ in range(5):
            await flush()
        assert ran["next"] is False
        storage.release.resolve()
        assert (await harness.waitForTask(child, context))["state"]["outcome"]["status"] == "aborted"
        await aborting
        assert ran["next"] is False
    finally:
        advance.resolve()
        storage.release.resolve()
        await harness.close(context)


async def test_lets_a_failfast_request_in_the_same_pass_override_a_restart_mark():
    storage = MemoryStorage()
    session = createSession(storage)
    conversation = await session.commit(lambda tx: tx.createConversation({"ownership": {"kind": "ownerless"}}), context)
    parent = await session.commit(
        lambda tx: tx.createTask(Dormant, None, {"conversationId": conversation["id"], "ownership": {"kind": "conversation"}}),
        context,
    )
    failed = await session.commit(
        lambda tx: tx.createTask(Dormant, None, {"ownership": {"kind": "task", "taskId": parent}}), context
    )
    child = await session.commit(
        lambda tx: tx.createTask(Dormant, None, {"ownership": {"kind": "task", "taskId": parent}}), context
    )
    # A restart-abandoned owner that waits failFast on a failed child and a live one.
    owner = await storage.task(parent, context)
    failedRecord = await storage.task(failed, context)
    waiting = {"status": "waiting", "checkpoint": {"phase": "work"}, "on": [failed, child], "policy": "failFast"}
    failure = {"status": "failed", "error": {"message": "failed"}}
    await storage.commit(
        [
            {"type": "task", "value": {**owner, "abortRequested": True, "abortReason": "restart", "state": waiting}},
            {"type": "task", "value": {**failedRecord, "state": {"status": "terminal", "outcome": failure}}},
        ],
        context,
    )
    # The child's definition is missing: a restart mark would keep it waiting, a request orphans it.
    harness, _registry, _reports = await openTasks(storage, [])
    try:
        harness.resume()

        async def ended() -> bool:
            return (await harness.getTask(child, context))["state"]["status"] == "terminal"

        await eventually(ended)
        toMatchObject(
            (await harness.getTask(child, context))["state"], {"outcome": {"status": "orphaned", "reason": "missing_task"}}
        )
    finally:
        await harness.close(context)


async def test_upgrades_restart_marks_down_the_tree_when_an_abort_is_requested_above_them():
    storage = MemoryStorage()
    session = createSession(storage)
    conversation = await session.commit(lambda tx: tx.createConversation({"ownership": {"kind": "ownerless"}}), context)
    top = await session.commit(
        lambda tx: tx.createTask(Dormant, None, {"conversationId": conversation["id"], "ownership": {"kind": "conversation"}}),
        context,
    )
    middle = await session.commit(
        lambda tx: tx.createTask(Dormant, None, {"ownership": {"kind": "task", "taskId": top}, "abandonOnRestart": True}),
        context,
    )
    Missing = defineTask({**Dormant["definition"], "name": "test.missing"})
    leaf = await session.commit(
        lambda tx: tx.createTask(Missing, None, {"ownership": {"kind": "task", "taskId": middle}}), context
    )
    # Only Dormant is registered: the leaf waits for its definition under the restart cascade.
    harness, _registry, _reports = await openTasks(storage, [Dormant])
    try:
        harness.resume()

        async def marked() -> bool:
            return (await harness.getTask(leaf, context)).get("abortReason") == "restart"

        await eventually(marked)

        async def blocked() -> bool:
            inspected = next(
                (task for task in (await harness.inspect(context))["tasks"] if task["record"]["id"] == leaf), None
            )
            return inspected is not None and inspected["state"]["kind"] == "blocked"

        await eventually(blocked)
        assert (await harness.getTask(leaf, context))["state"]["status"] != "terminal"
        # A request on the top task reaches the leaf two levels down, which is then orphaned.
        await harness.abortTask(top, context)
        for id, status in [(leaf, "orphaned"), (middle, "aborted"), (top, "aborted")]:
            assert (await harness.waitForTask(id, context))["state"]["outcome"]["status"] == status
    finally:
        await harness.close(context)


async def test_marks_a_flagged_task_that_holds_its_outcome_aborting_its_owned_work_and_keeps_the_outcome():
    storage = MemoryStorage()
    session = createSession(storage)
    conversation = await session.commit(lambda tx: tx.createConversation({"ownership": {"kind": "ownerless"}}), context)
    holder = await session.commit(
        lambda tx: tx.createTask(
            Dormant,
            None,
            {"conversationId": conversation["id"], "ownership": {"kind": "conversation"}, "abandonOnRestart": True},
        ),
        context,
    )
    child = await session.commit(
        lambda tx: tx.createTask(Dormant, None, {"ownership": {"kind": "task", "taskId": holder}}), context
    )
    record = await storage.task(holder, context)
    completing = {"status": "completing", "outcome": {"status": "completed", "result": None}}
    await storage.commit([{"type": "task", "value": {**record, "state": completing}}], context)
    harness, _registry, _reports = await openTasks(storage, [Dormant])
    try:
        harness.resume()
        assert (await harness.waitForTask(child, context))["state"]["outcome"]["status"] == "aborted"
        settled = await harness.waitForTask(holder, context)
        toMatchObject(settled, {"abortReason": "restart", "state": {"outcome": {"status": "completed"}}})
    finally:
        await harness.close(context)
