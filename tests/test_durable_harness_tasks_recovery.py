"""pi durable's ``test/harness-tasks-recovery.test.ts``."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.models import createModels
from misaka.durable import (
    Harness,
    MemoryStorage,
    createRegistry,
    defineDoc,
    defineTask,
    defineTool,
)
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from tests.durable_support import ControlledStorage, context, flush, toMatchObject
from tests.harness_support import addTask, addTool
from tests.task_support import (
    aborted,
    abortedWith,
    completed,
    countingReader,
    deferred,
    eventually,
    openTasks,
)

EMPTY_OBJECT = {"type": "object", "properties": {}}


@pytest.fixture
def sqlitePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-tasks-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


def never() -> Any:
    """``new Promise(() => {})``: an awaitable that never settles."""
    return asyncio.get_running_loop().create_future()


def abandon(*harnesses: Any) -> None:
    """End of a crash simulation: a crashed Harness is dead for good and what it left suspended never continues.
    Sealing its scheduler keeps the cancellation at loop close from reviving it."""
    for harness in harnesses:
        harness._tasks._seal()


def consume(future: Any) -> None:
    """``void promise``: start it and ignore how it ends."""
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())


async def createIn(harness: Any, task: dict[str, Any]) -> int:
    root = await harness.root(context)
    return await root.commit(  # type: ignore[no-any-return]
        lambda tx: tx.createTask(task, None, {"ownership": {"kind": "conversation"}}), context
    )


class TransferService:
    """Fake external service whose operations are idempotent by request key."""

    def __init__(self) -> None:
        self.applied: dict[str, int] = {}
        self.calls = 0

    async def apply(self, key: str, amount: int) -> int:
        self.calls += 1
        if key not in self.applied:
            self.applied[key] = amount * 10
        return self.applied[key]


def transferTask(service: TransferService, interrupt: dict[str, bool]) -> dict[str, Any]:
    """Intent/effect/outcome task: ``prepare`` commits the intent, ``apply`` performs the effect and commits the
    outcome. While ``interrupt["first"]`` is set, the first ``apply`` blocks after the effect until the invocation is
    signalled."""

    async def prepare(task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.memo("requested", task["input"]["amount"], ctx)
        await runtime.commit(
            lambda tx, current: {"status": "running", "checkpoint": {"phase": "apply", "key": f"transfer-{task['id']}"}}, ctx
        )

    async def apply(task: Any, runtime: Any, ctx: Any) -> None:
        receipt = await service.apply(task["state"]["checkpoint"]["key"], task["input"]["amount"])
        if interrupt["first"]:
            interrupt["first"] = False
            await aborted(runtime.signal)
        await runtime.commit(lambda tx, current: completed({"receipt": receipt}), ctx)

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, ctx)

    return defineTask(
        {
            "name": "test.transfer",
            "version": 1,
            "initial": lambda input: {"phase": "prepare"},
            "phases": {"prepare": prepare, "apply": apply},
            "abort": abort,
        }
    )


def versioned(version: int, result: str, migrate: Callable[..., Any] | None = None) -> dict[str, Any]:
    """A versioned one-phase task that completes with ``result``, optionally migrating older records."""

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: completed(result), ctx)

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: abortedWith(result), ctx)

    definition: dict[str, Any] = {
        "name": "test.versioned",
        "version": version,
        "initial": lambda input: {"phase": "run"},
        "phases": {"run": run},
        "abort": abort,
    }
    if migrate is not None:
        definition["migrate"] = migrate
    return defineTask(definition)


async def noAbort(_task: Any, runtime: Any, ctx: Any) -> None:
    return None


async def test_resumes_an_intent_effect_outcome_task_interrupted_after_its_intent_across_close_and_reopen(sqlitePath):
    service = TransferService()
    Transfer = transferTask(service, {"first": True})

    first, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Transfer])
    root = await first.root(context)
    id = await root.commit(
        lambda tx: tx.createTask(Transfer, {"amount": 7}, {"ownership": {"kind": "conversation"}}), context
    )
    first.resume()
    await eventually(lambda: service.calls == 1)
    await first.close(context)

    second, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Transfer])
    # Open reconciled ``running`` to ``pending`` and kept the checkpoint and memos; nothing ran yet.
    toMatchObject(
        await second.getTask(id, context),
        {
            "abortRequested": False,
            "memos": {"requested": 7},
            "state": {"status": "pending", "checkpoint": {"phase": "apply", "key": f"transfer-{id}"}},
        },
    )
    assert service.calls == 1
    second.resume()
    receipt = await second.waitForTask(id, context)
    assert receipt["state"]["outcome"] == {"status": "completed", "result": {"receipt": 70}}
    assert service.calls == 2
    assert len(service.applied) == 1
    await second.close(context)

    third, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Transfer])
    assert await third.getTask(id, context) == receipt
    await third.close(context)


# #10549
async def test_keeps_a_tasks_startedat_across_close_and_reopen_and_stamps_endedat_when_it_settles(sqlitePath):
    service = TransferService()
    Transfer = transferTask(service, {"first": True})
    clock = {"now": 2_000}

    def now() -> int:
        return clock["now"]

    first, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Transfer], {"now": now})
    root = await first.root(context)
    id = await root.commit(
        lambda tx: tx.createTask(Transfer, {"amount": 7}, {"ownership": {"kind": "conversation"}}), context
    )
    first.resume()
    await eventually(lambda: service.calls == 1)
    clock["now"] = 3_000
    await first.close(context)

    clock["now"] = 9_000
    second, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Transfer], {"now": now})
    reopened = await second.getTask(id, context)
    assert [reopened["state"]["status"], reopened.get("startedAt"), reopened.get("endedAt")] == ["pending", 2_000, None]
    second.resume()
    receipt = await second.waitForTask(id, context)
    assert [receipt.get("startedAt"), receipt.get("endedAt")] == [2_000, 9_000]
    await second.close(context)


async def test_resumes_abort_work_after_close_at_every_direct_task_abort_stage(sqlitePath):
    log: list[str] = []
    abortGate = {"block": True}
    abortReached = deferred()
    runRelease = deferred()

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        log.append("run")
        # Ignores the abort signal until released, so the mark is durable while the run is active.
        await runRelease.promise

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        log.append("abort")
        if abortGate["block"]:
            abortReached.resolve()
            await aborted(runtime.signal)
        await runtime.commit(lambda tx, current: abortedWith("stop"), ctx)

    Abortable = defineTask(
        {
            "name": "test.abortable",
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": run},
            "abort": abort,
        }
    )

    # Stage 1: the mark is committed while the run invocation is still active.
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Abortable])
    id = await createIn(harness, Abortable)
    harness.resume()
    await eventually(lambda: len(log) == 1)
    aborting = harness.abortTask(id, context)
    while not (await harness.getTask(id, context))["abortRequested"]:
        await flush()
    closing = harness.close(context)
    runRelease.resolve()
    await closing
    assert await aborting == "marked"
    assert log == ["run"]

    # Stage 2: reopen dispatches the abort invocation, never the run; close while it is active.
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Abortable])
    toMatchObject(await harness.getTask(id, context), {"abortRequested": True, "state": {"status": "pending"}})
    harness.resume()
    await abortReached.promise
    await harness.close(context)
    assert log == ["run", "abort"]

    # Stage 3: a fresh abort invocation settles the task.
    abortGate["block"] = False
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Abortable])
    harness.resume()
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "aborted", "reason": "stop"}
    await harness.close(context)
    assert log == ["run", "abort", "abort"]

    # Stage 4: the terminal receipt survives reopen and nothing runs again.
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [Abortable])
    harness.resume()
    await flush()
    assert await harness.abortTask(id, context) == "terminal"
    await harness.close(context)
    assert log == ["run", "abort", "abort"]


# Crash simulation: the crashed Harness is abandoned without close, its held storage commit never lands, and its
# blocked handlers never return. A new Harness then opens the same storage.


def crashTask(log: list[str], options: dict[str, bool]) -> dict[str, Any]:
    """A task whose run ignores its signal and whose abort handler blocks while ``blockAbort`` is set."""

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        log.append("run")
        await never()

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        log.append("abort")
        if options["blockAbort"]:
            await never()
        await runtime.commit(lambda tx, current: abortedWith("recovered"), ctx)

    return defineTask(
        {
            "name": "test.crash",
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": run},
            "abort": abort,
        }
    )


async def recover(storage: ControlledStorage, log: list[str]) -> SimpleNamespace:
    storage.crash()
    harness, _registry, _reports = await openTasks(storage, [crashTask(log, {"blockAbort": False})])
    page = await harness.commit(lambda tx: tx.scanTasks({"kind": "test.crash"}, 1), context)
    return SimpleNamespace(harness=harness, id=page["items"][0]["id"])


async def crashedRun(log: list[str]) -> SimpleNamespace:
    storage = ControlledStorage()
    harness, _registry, _reports = await openTasks(storage, [crashTask(log, {"blockAbort": True})])
    id = await createIn(harness, crashTask(log, {"blockAbort": True}))
    harness.resume()
    await eventually(lambda: len(log) == 1)
    return SimpleNamespace(storage=storage, harness=harness, id=id)


async def test_crash_while_the_mark_commit_is_in_storage_the_run_resumes():
    log: list[str] = []
    crashed = await crashedRun(log)
    storage, harness, id = crashed.storage, crashed.harness, crashed.id
    held = storage.holdCommits()
    consume(harness.abortTask(id, context))
    await held.entered
    recovered = await recover(storage, log)
    toMatchObject(await recovered.harness.getTask(id, context), {"abortRequested": False, "state": {"status": "pending"}})
    recovered.harness.resume()
    await eventually(lambda: len(log) == 2)
    assert log == ["run", "run"]
    abandon(harness, recovered.harness)


async def test_crash_after_the_mark_before_the_run_joins_only_the_abort_handler_runs():
    log: list[str] = []
    crashed = await crashedRun(log)
    storage, harness, id = crashed.storage, crashed.harness, crashed.id
    # The run ignores its signal, so abortTask never finishes joining it.
    joined = False

    async def joining() -> None:
        nonlocal joined
        await harness.abortTask(id, context)
        joined = True

    task = asyncio.ensure_future(joining())
    while not (await harness.getTask(id, context))["abortRequested"]:
        await flush()
    await flush()
    assert joined is False
    recovered = await recover(storage, log)
    toMatchObject(await recovered.harness.getTask(id, context), {"abortRequested": True, "state": {"status": "pending"}})
    recovered.harness.resume()
    assert (await recovered.harness.waitForTask(id, context))["state"]["outcome"] == {
        "status": "aborted",
        "reason": "recovered",
    }
    assert log == ["run", "abort"]
    await recovered.harness.close(context)
    task.cancel()
    abandon(harness)


async def test_crash_while_the_abort_handler_runs_a_fresh_abort_invocation_settles_the_task():
    log: list[str] = []
    storage = ControlledStorage()
    Crash = crashTask(log, {"blockAbort": True})
    harness, _registry, _reports = await openTasks(storage, [Crash])
    id = await createIn(harness, Crash)
    await harness.abortTask(id, context)
    harness.resume()
    await eventually(lambda: len(log) == 1)
    assert log == ["abort"]
    toMatchObject(await harness.getTask(id, context), {"state": {"status": "running"}})
    recovered = await recover(storage, log)
    recovered.harness.resume()
    assert (await recovered.harness.waitForTask(id, context))["state"]["outcome"] == {
        "status": "aborted",
        "reason": "recovered",
    }
    assert log == ["abort", "abort"]
    await recovered.harness.close(context)
    abandon(harness)


async def test_crash_while_the_abort_outcome_is_in_storage_the_abort_handler_runs_again():
    log: list[str] = []
    storage = ControlledStorage()
    reached = deferred()
    proceed = deferred()

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        log.append("abort")
        reached.resolve()
        await proceed.promise
        await runtime.commit(lambda tx, current: abortedWith("lost"), ctx)

    Crash = defineTask(
        {
            "name": "test.crash",
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": run},
            "abort": abort,
        }
    )
    harness, _registry, _reports = await openTasks(storage, [Crash])
    id = await createIn(harness, Crash)
    await harness.abortTask(id, context)
    harness.resume()
    await reached.promise
    held = storage.holdCommits()
    proceed.resolve()
    await held.entered
    recovered = await recover(storage, log)
    recovered.harness.resume()
    assert (await recovered.harness.waitForTask(id, context))["state"]["outcome"] == {
        "status": "aborted",
        "reason": "recovered",
    }
    assert log == ["abort", "abort"]
    await recovered.harness.close(context)
    abandon(harness)


async def test_crash_after_the_terminal_outcome_nothing_runs_again():
    log: list[str] = []
    storage = ControlledStorage()
    Crash = crashTask(log, {"blockAbort": False})
    harness, _registry, _reports = await openTasks(storage, [Crash])
    id = await createIn(harness, Crash)
    await harness.abortTask(id, context)
    harness.resume()
    await harness.waitForTask(id, context)
    recovered = await recover(storage, log)
    recovered.harness.resume()
    await flush()
    assert log == ["abort"]
    assert (await recovered.harness.getTask(id, context))["state"] == {
        "status": "terminal",
        "outcome": {"status": "aborted", "reason": "recovered"},
    }
    await recovered.harness.close(context)
    abandon(harness)


async def test_crash_while_the_reservation_commit_is_in_storage_the_task_is_still_pending():
    log: list[str] = []
    storage = ControlledStorage()
    Crash = crashTask(log, {"blockAbort": False})
    harness, _registry, _reports = await openTasks(storage, [Crash])
    id = await createIn(harness, Crash)
    held = storage.holdCommits()
    harness.resume()
    await held.entered
    recovered = await recover(storage, log)
    assert (await recovered.harness.getTask(id, context))["state"]["status"] == "pending"
    assert log == []
    await recovered.harness.abortTask(id, context)
    recovered.harness.resume()
    await recovered.harness.waitForTask(id, context)
    assert log == ["abort"]
    await recovered.harness.close(context)
    abandon(harness)


async def test_blocked_keeps_a_task_with_a_missing_definition_pending_and_live_for_idle_waits_until_registration():
    V1 = versioned(1, "v1")
    harness, registry, _reports = await openTasks(MemoryStorage(), [])
    id = await createIn(harness, V1)
    harness.resume()
    await flush()
    assert (await harness.getTask(id, context))["state"]["status"] == "pending"
    idle = harness.waitForIdle(context)
    await flush()
    addTask(registry, V1)
    await idle
    assert (await harness.getTask(id, context))["state"] == {
        "status": "terminal",
        "outcome": {"status": "completed", "result": "v1"},
    }
    await harness.close(context)


async def test_blocked_keeps_a_task_stored_by_a_newer_version_pending_until_a_fitting_definition_is_registered():
    registry = createRegistry()
    addTask(registry, versioned(1, "old"))
    harness, _registry, _reports = await openTasks(MemoryStorage(), [], {"registry": registry})
    id = await createIn(harness, versioned(2, "new"))
    harness.resume()
    await flush()
    assert (await harness.getTask(id, context))["state"]["status"] == "pending"
    # The same extension name replaces the old one in place.
    addTask(registry, versioned(2, "new"))
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "completed", "result": "new"}
    await harness.close(context)


async def test_blocked_migrates_at_reservation_leaves_the_record_unchanged_when_migration_fails_and_retries_only_for_a_new_definition(
    sqlitePath,
):
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [])
    id = await createIn(harness, versioned(1, "v1"))
    await harness.close(context)

    registry = createRegistry()
    failures = 0

    def failing(_input: Any, _checkpoint: Any, _fromVersion: int) -> Any:
        nonlocal failures
        failures += 1
        raise RuntimeError("cannot migrate")

    addTask(registry, versioned(2, "v2", failing))
    harness, _registry, reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [], {"registry": registry})
    harness.resume()
    await eventually(lambda: len(reports) == 1)

    # An unrelated registry change wakes the scheduler without retrying the same failed definition.
    async def unrelated(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {}

    addTool(
        registry,
        defineTool({"name": "unrelated", "description": "unrelated", "parameters": EMPTY_OBJECT, "execute": unrelated}),
    )
    await flush()
    assert failures == 1
    assert len(reports) == 1
    assert "cannot migrate" in str(reports[0])
    toMatchObject(await harness.getTask(id, context), {"version": 1, "state": {"status": "pending", "checkpoint": {"phase": "run"}}})

    migrations: list[int] = []

    def migrate(_input: Any, checkpoint: Any, fromVersion: int) -> dict[str, Any]:
        migrations.append(fromVersion)
        return {"input": None, "checkpoint": checkpoint}

    # The same extension name replaces the old one in place.
    addTask(registry, versioned(2, "v2", migrate))
    receipt = await harness.waitForTask(id, context)
    toMatchObject(receipt, {"version": 2, "state": {"outcome": {"status": "completed", "result": "v2"}}})
    assert migrations == [1]
    await harness.close(context)


async def test_blocked_blocks_an_older_record_whose_newer_definition_has_no_migration(sqlitePath):
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [])
    id = await createIn(harness, versioned(1, "v1"))
    await harness.close(context)
    harness, _registry, reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [versioned(2, "v2")])
    harness.resume()
    await eventually(lambda: len(reports) == 1)
    assert "has no migration from 1" in str(reports[0])
    toMatchObject(await harness.getTask(id, context), {"version": 1, "state": {"status": "pending"}})
    await harness.close(context)


async def test_blocked_settles_an_aborted_blocked_task_as_orphaned_and_retires_its_documents():
    Scratch = defineDoc({"kind": "test.orphan-scratch", "version": 1, "scope": "task", "initial": lambda: {"n": 0}})
    harness, _registry, _reports = await openTasks(MemoryStorage(), [])
    root = await harness.root(context)

    async def create(tx: Any) -> int:
        created = await tx.createTask(versioned(1, "x"), None, {"ownership": {"kind": "conversation"}})
        (await tx.doc(Scratch, created))["n"] = 1
        return created  # type: ignore[no-any-return]

    id = await root.commit(create, context)
    # Before resume: the marking commit settles the blocked task directly.
    assert await harness.abortTask(id, context) == "marked"
    assert (await harness.getTask(id, context))["state"] == {
        "status": "terminal",
        "outcome": {"status": "orphaned", "reason": "missing_task"},
    }
    assert await harness.snapshot(Scratch, id, context) is None
    await harness.close(context)


async def test_blocked_orphans_a_marked_task_whose_definition_disappeared_while_its_run_was_active():
    reached = deferred()

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        reached.resolve()
        await aborted(runtime.signal)

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        raise RuntimeError("must not run")

    Running = defineTask(
        {
            "name": "test.vanishing",
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": run},
            "abort": abort,
        }
    )
    registry = createRegistry()
    registration = addTask(registry, Running)
    harness, _registry, _reports = await openTasks(MemoryStorage(), [], {"registry": registry})
    id = await createIn(harness, Running)
    harness.resume()
    await reached.promise
    registration.dispose()
    # An active run means the mark does not orphan directly; the scheduler orphans once the run has ended.
    assert await harness.abortTask(id, context) == "marked"
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {
        "status": "orphaned",
        "reason": "missing_task",
    }
    await harness.close(context)


async def test_blocked_orphans_a_reopened_abort_marked_task_without_a_definition_once_scheduling_resumes(sqlitePath):
    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [versioned(1, "x")])
    id = await createIn(harness, versioned(1, "x"))
    assert await harness.abortTask(id, context) == "marked"
    await harness.close(context)

    harness, _registry, _reports = await openTasks(await openNodeSqliteStorage(sqlitePath), [])
    toMatchObject(await harness.getTask(id, context), {"abortRequested": True, "state": {"status": "pending"}})
    idle = harness.waitForIdle(context)
    harness.resume()
    await idle
    assert (await harness.getTask(id, context))["state"] == {
        "status": "terminal",
        "outcome": {"status": "orphaned", "reason": "missing_task"},
    }
    await harness.close(context)


def handoverTask(label: str, version: int, log: list[str], options: dict[str, Any] | None = None) -> dict[str, Any]:
    options = options or {}
    gates = options.get("gates") or {}

    def advance(phase: str, following: dict[str, str]) -> Callable[..., Any]:
        async def run(_task: Any, runtime: Any, ctx: Any) -> None:
            log.append(f"{label}:{phase} start")
            gate = gates.get(phase)
            if gate is not None:
                await gate
            await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": following}, ctx)
            # Leave room for a wrongly dispatched successor before this invocation ends.
            await flush()
            onEnd = gates.get("onEnd")
            if onEnd is not None:
                onEnd()
            log.append(f"{label}:{phase} end")

        return run

    async def last(_task: Any, runtime: Any, ctx: Any) -> None:
        log.append(f"{label}:c")
        await runtime.commit(lambda tx, current: completed(None), ctx)

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        log.append(f"{label}:abort")
        await runtime.commit(lambda tx, current: abortedWith(label), ctx)

    definition: dict[str, Any] = {
        "name": "test.handover",
        "version": version,
        "initial": lambda input: {"phase": "a"},
        "phases": {"a": advance("a", {"phase": "b"}), "b": advance("b", {"phase": "c"}), "c": last},
        "abort": abort,
    }
    if options.get("migrate") is not None:
        definition["migrate"] = options["migrate"]
    return defineTask(definition)


async def startHandover(log: list[str], gates: dict[str, Any], storage: Any = None) -> SimpleNamespace:
    registry = createRegistry()
    old = addTask(registry, handoverTask("old", 1, log, {"gates": gates}))
    harness, _registry, reports = await openTasks(storage if storage is not None else MemoryStorage(), [], {"registry": registry})
    id = await createIn(harness, handoverTask("old", 1, log))
    harness.resume()
    await eventually(lambda: len(log) == 1)
    return SimpleNamespace(harness=harness, registry=registry, reports=reports, old=old, id=id)


async def test_handover_hands_over_at_the_next_phase_boundary_to_a_same_version_replacement_without_overlap():
    log: list[str] = []
    gate = deferred()
    started = await startHandover(log, {"a": gate.promise})
    harness, registry, id = started.harness, started.registry, started.id
    # The same extension name replaces the old one in place.
    addTask(registry, handoverTask("new", 1, log))
    gate.resolve()
    await harness.waitForTask(id, context)
    assert log == ["old:a start", "old:a end", "new:b start", "new:b end", "new:c"]
    await harness.close(context)


async def test_handover_keeps_the_memos_across_a_handover():
    log: list[str] = []
    gate = deferred()
    reached = deferred()

    def memoTask(label: str) -> dict[str, Any]:
        async def a(_task: Any, runtime: Any, ctx: Any) -> None:
            await runtime.memo("picked", label, ctx)
            reached.resolve()
            await gate.promise
            await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": {"phase": "b"}}, ctx)

        async def b(_task: Any, runtime: Any, ctx: Any) -> None:
            # The candidate loses to the memo the old definition stored.
            picked = await runtime.memo("picked", label, ctx)
            log.append(f"{label}:b {picked}")
            await runtime.commit(lambda tx, current: completed(picked), ctx)

        return defineTask(
            {
                "name": "test.handover-memo",
                "version": 1,
                "initial": lambda input: {"phase": "a"},
                "phases": {"a": a, "b": b},
                "abort": noAbort,
            }
        )

    registry = createRegistry()
    addTask(registry, memoTask("old"))
    harness, _registry, _reports = await openTasks(MemoryStorage(), [], {"registry": registry})
    id = await createIn(harness, memoTask("old"))
    harness.resume()
    await reached.promise
    addTask(registry, memoTask("new"))
    gate.resolve()
    receipt = await harness.waitForTask(id, context)
    assert receipt["state"]["outcome"] == {"status": "completed", "result": "old"}
    assert log == ["new:b old"]
    await harness.close(context)


async def test_handover_hands_over_to_a_newer_version_with_a_migration():
    log: list[str] = []
    gate = deferred()
    started = await startHandover(log, {"a": gate.promise})
    harness, registry, id = started.harness, started.registry, started.id
    # The same extension name replaces the old one in place.
    addTask(
        registry,
        handoverTask("v2", 2, log, {"migrate": lambda input, checkpoint, fromVersion: {"input": None, "checkpoint": {"phase": "c"}}}),
    )
    gate.resolve()
    receipt = await harness.waitForTask(id, context)
    assert receipt["version"] == 2
    assert log == ["old:a start", "old:a end", "v2:c"]
    await harness.close(context)


async def test_handover_hands_over_to_a_newer_version_whose_migration_fails_and_leaves_the_task_blocked():
    log: list[str] = []
    gate = deferred()
    started = await startHandover(log, {"a": gate.promise})
    harness, registry, id, reports = started.harness, started.registry, started.id, started.reports

    def broken(_input: Any, _checkpoint: Any, _fromVersion: int) -> Any:
        raise RuntimeError("broken migration")

    # The same extension name replaces the old one in place.
    addTask(registry, handoverTask("broken", 2, log, {"migrate": broken}))
    gate.resolve()
    await eventually(lambda: len(reports) == 1)
    await flush()
    toMatchObject(await harness.getTask(id, context), {"version": 1, "state": {"status": "pending", "checkpoint": {"phase": "b"}}})
    assert log == ["old:a start", "old:a end"]
    await harness.close(context)


async def test_handover_keeps_running_under_the_old_definition_when_the_replacement_is_missing_or_cannot_take_the_task():
    log: list[str] = []
    gateA = deferred()
    gateB = deferred()
    started = await startHandover(log, {"a": gateA.promise, "b": gateB.promise})
    harness, registry, old, id, reports = started.harness, started.registry, started.old, started.id, started.reports
    old.dispose()
    gateA.resolve()
    await eventually(lambda: "old:b start" in log)
    # A newer definition without a migration cannot take the task either.
    addTask(registry, handoverTask("incompatible", 2, log))
    gateB.resolve()
    await harness.waitForTask(id, context)
    assert log == ["old:a start", "old:a end", "old:b start", "old:b end", "old:c"]
    assert [report.cause for report in reports] == ["missing_task", "incompatible_task"]
    await harness.close(context)


async def test_handover_rejects_a_runtime_commit_of_the_old_invocation_queued_behind_its_handover_commit():
    log: list[str] = []
    gate = deferred()
    storage = ControlledStorage()
    held: Any = None
    oldRuntime: Any = None
    harnessRef: Any = None
    registry = createRegistry()

    async def a(task: Any, runtime: Any, ctx: Any) -> None:
        nonlocal oldRuntime, held
        oldRuntime = runtime
        await gate.promise
        await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": {"phase": "b"}}, ctx)
        # Hold the line with an unrelated commit so the handover commit queues behind it.
        held = storage.holdCommits()

        async def blocker(tx: Any) -> None:
            await tx.appendEntry(task["conversationId"], {"kind": "blocker"})

        consume(harnessRef.commit(blocker, ctx))

    async def nothing(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    Old = defineTask(
        {
            "name": "test.handover",
            "version": 1,
            "initial": lambda input: {"phase": "a"},
            "phases": {"a": a, "b": nothing, "c": nothing},
            "abort": noAbort,
        }
    )
    addTask(registry, Old)
    harness, _registry, _reports = await openTasks(storage, [], {"registry": registry})
    harnessRef = harness
    id = await createIn(harness, Old)
    harness.resume()
    await eventually(lambda: oldRuntime is not None)
    # The same extension name replaces the old one in place.
    addTask(registry, handoverTask("new", 1, log))
    gate.resolve()
    await eventually(lambda: held is not None)
    await held.entered
    await flush()
    # Queued behind the handover commit while the invocation has not ended yet.
    late = oldRuntime.commit(lambda tx, current: completed(None), context)
    held.release()
    with pytest.raises(Exception, match="invocation has ended"):
        await late
    await harness.waitForTask(id, context)
    assert log == ["new:b start", "new:b end", "new:c"]
    await harness.close(context)


async def test_handover_preserves_an_abort_mark_that_races_the_handover_commit_the_new_definition_aborts():
    log: list[str] = []
    gate = deferred()
    storage = ControlledStorage()
    held: Any = None

    # The progress commit landed; hold the next commit, the handover, and queue the abort mark behind it.
    def onEnd() -> None:
        nonlocal held
        if held is None:
            held = storage.holdCommits()

    started = await startHandover(log, {"a": gate.promise, "onEnd": onEnd}, storage)
    harness, registry, id = started.harness, started.registry, started.id
    # The same extension name replaces the old one in place.
    addTask(registry, handoverTask("new", 1, log))
    gate.resolve()
    await eventually(lambda: held is not None)
    await held.entered
    aborting = harness.abortTask(id, context)
    held.release()
    assert await aborting == "marked"
    assert (await harness.waitForTask(id, context))["state"]["outcome"] == {"status": "aborted", "reason": "new"}
    states = [
        f"{write['value']['state']['status']}{'+mark' if write['value']['abortRequested'] else ''}"
        for writes in storage.commits
        for write in writes
        if write["type"] == "task" and write["value"]["id"] == id
    ]
    # created, reserved, progress, handover, mark, abort reservation, aborted
    assert states == [
        "pending",
        "running",
        "running",
        "pending",
        "pending+mark",
        "running+mark",
        "terminal+mark",
    ]
    assert log == ["old:a start", "old:a end", "new:abort"]
    await harness.close(context)


async def test_harness_open_releases_its_registry_subscription_and_closes_the_session_when_open_fails():
    storage = ControlledStorage()
    log: list[str] = []

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        log.append("run")
        await never()

    Stuck = defineTask(
        {
            "name": "test.stuck",
            "version": 1,
            "initial": lambda input: {"phase": "run"},
            "phases": {"run": run},
            "abort": noAbort,
        }
    )
    # Leave a running task behind so open has a reconciliation commit to fail.
    first, _registry, _reports = await openTasks(storage, [Stuck])
    await createIn(first, Stuck)
    first.resume()
    await eventually(lambda: len(log) == 1)

    registry = createRegistry()
    reader = countingReader(registry)
    storage.failNextCommit(RuntimeError("disk full"))
    with pytest.raises(Exception, match="disk full"):
        await Harness.open(storage, {"models": createModels(), "registry": reader}, context)
    assert reader.subscriptions() == 0
    with pytest.raises(Exception):  # noqa: B017
        await storage.task(1, context)
    abandon(first)
