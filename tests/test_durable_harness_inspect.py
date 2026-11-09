"""pi durable's ``test/harness-inspect.test.ts``."""

from __future__ import annotations

from typing import Any

from misaka.durable import MemoryStorage, defineTask
from tests.chat_support import chatSetup, openChat, unanswered, waitFor
from tests.durable_support import context
from tests.task_support import completed, deferred, eventually, openTasks


def task(name: str, version: int, gate: Any = None, migrate: Any = None) -> dict[str, Any]:
    """A one-phase task that completes once ``gate`` resolves; ``migrate`` runs when an older stored version migrates."""

    async def run(_task: Any, runtime: Any, ctx: Any) -> None:
        if gate is not None:
            await gate
        await runtime.commit(lambda tx, current: completed(None), ctx)

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    definition: dict[str, Any] = {
        "name": name,
        "version": version,
        "initial": lambda input: {"phase": "run"},
        "phases": {"run": run},
        "abort": abort,
    }
    if migrate is not None:

        def migrateDefinition(input: Any, checkpoint: Any, fromVersion: int) -> dict[str, Any]:
            migrate()
            return {"input": None, "checkpoint": {"phase": "run"}}

        definition["migrate"] = migrateDefinition
    return defineTask(definition)


def stateOf(tasks: list[dict[str, Any]], id: int) -> dict[str, Any] | None:
    return next((entry["state"] for entry in tasks if entry["record"]["id"] == id), None)


async def test_derives_every_live_tasks_state_without_running_task_code():
    gate = deferred()
    Gate = task("test.gate", 1, gate=gate.promise)
    gateId: int | None = None

    async def waitPhase(_task: Any, runtime: Any, ctx: Any) -> None:
        checkpoint = {"phase": "done"}
        await runtime.commit(
            lambda tx, current: {"status": "waiting", "checkpoint": checkpoint, "on": [gateId], "policy": "allSettled"}, ctx
        )

    async def donePhase(_task: Any, runtime: Any, ctx: Any) -> None:
        await runtime.commit(lambda tx, current: completed(None), ctx)

    async def noAbort(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    Dependent = defineTask(
        {
            "name": "test.dependent",
            "version": 1,
            "initial": lambda input: {"phase": "wait"},
            "phases": {"wait": waitPhase, "done": donePhase},
            "abort": noAbort,
        }
    )
    migrations = 0

    def countMigration() -> None:
        nonlocal migrations
        migrations += 1

    def failMigration() -> None:
        raise RuntimeError("cannot migrate")

    registered = [
        Gate,
        Dependent,
        task("test.migrating", 2, migrate=countMigration),
        task("test.no-migration", 2),
        task("test.failing", 2, migrate=failMigration),
        task("test.too-old", 1),
    ]
    harness, _registry, _reports = await openTasks(MemoryStorage(), registered)
    root = await harness.root(context)

    async def create(tx: Any) -> dict[str, int]:
        nonlocal gateId
        owned = {"ownership": {"kind": "conversation"}}
        gateId = await tx.createTask(Gate, None, owned)
        return {
            "gate": gateId,
            "dependent": await tx.createTask(Dependent, None, owned),
            # Stored by definitions other than the registered ones.
            "migrating": await tx.createTask(task("test.migrating", 1), None, owned),
            "noMigration": await tx.createTask(task("test.no-migration", 1), None, owned),
            "failing": await tx.createTask(task("test.failing", 1), None, owned),
            "tooOld": await tx.createTask(task("test.too-old", 2), None, owned),
            "missing": await tx.createTask(task("test.missing", 1), None, owned),
        }

    ids = await root.commit(create, context)

    paused = await harness.inspect(context)
    assert paused["scheduling"] == "paused"
    assert [entry["record"]["id"] for entry in paused["tasks"]] == list(ids.values())
    assert stateOf(paused["tasks"], ids["gate"]) == {"kind": "ready", "migrates": False}
    assert stateOf(paused["tasks"], ids["dependent"]) == {"kind": "ready", "migrates": False}
    assert stateOf(paused["tasks"], ids["migrating"]) == {"kind": "ready", "migrates": True}
    # A migration that was never tried is not run to find out.
    assert stateOf(paused["tasks"], ids["failing"]) == {"kind": "ready", "migrates": True}
    noMigration = stateOf(paused["tasks"], ids["noMigration"])
    assert noMigration["kind"] == "blocked"
    assert noMigration["reason"] == "migration_failed"
    assert str(noMigration["error"]) == "Task test.no-migration version 2 has no migration from 1"
    assert stateOf(paused["tasks"], ids["tooOld"]) == {"kind": "blocked", "reason": "task_too_old"}
    assert stateOf(paused["tasks"], ids["missing"]) == {"kind": "blocked", "reason": "missing_task"}
    assert migrations == 0
    assert (await harness.inspect(context))["scheduling"] == "paused"

    harness.resume()
    await eventually(lambda: migrations == 1)
    await harness.waitForTask(ids["migrating"], context)
    running = await harness.inspect(context)

    async def reached() -> bool:
        nonlocal running
        running = await harness.inspect(context)
        return (
            stateOf(running["tasks"], ids["gate"]) is not None
            and stateOf(running["tasks"], ids["gate"])["kind"] == "running"
            and stateOf(running["tasks"], ids["dependent"]) is not None
            and stateOf(running["tasks"], ids["dependent"])["kind"] == "waiting"
        )

    await waitFor(reached)
    assert stateOf(running["tasks"], ids["dependent"]) == {"kind": "waiting", "on": [ids["gate"]]}
    assert running["scheduling"] == "running"
    assert stateOf(running["tasks"], ids["gate"]) == {"kind": "running"}
    assert stateOf(running["tasks"], ids["migrating"]) is None
    failing = stateOf(running["tasks"], ids["failing"])
    assert failing["kind"] == "blocked"
    assert failing["reason"] == "migration_failed"
    assert str(failing["error"]) == "cannot migrate"

    gate.resolve()
    await harness.waitForTask(ids["dependent"], context)
    settled = await harness.inspect(context)
    assert [entry["record"]["id"] for entry in settled["tasks"]] == [
        ids["noMigration"],
        ids["failing"],
        ids["tooOld"],
        ids["missing"],
    ]
    await harness.close(context)


async def test_lists_unsettled_submissions():
    setup = chatSetup()
    busy = unanswered()
    setup.faux.setResponses([busy.step])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    other = await harness.createConversation({"ownership": {"kind": "ownerless"}}, context)
    await other.submit({"type": "write", "entry": {"kind": "note"}}, context)
    submitted = await root.submit({"type": "input", "content": "hi"}, context)
    await busy.reached

    inspection = await harness.inspect(context)
    assert inspection["submissions"] == [await submitted.status(context)]
    assert [[entry["record"]["kind"], entry["state"]["kind"]] for entry in inspection["tasks"]] == [
        ["pi.generation", "running"]
    ]
    await harness.close(context)
