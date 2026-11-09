"""pi durable's ``test/types.test.ts``.

Almost every assertion of the upstream file is compile-time (``expectTypeOf``, ``@ts-expect-error``): TypeScript checks the
brands of IDs, the discriminated unions of records and the typed hooks of tasks. Python records are plain ``dict``
objects and IDs are plain ``int`` values, so the type-level cases have no runtime counterpart here. What remains is what
the file executes: the numeric IDs, and defining a task with custom hooks, erasing it into an extension, and waiting for
it through the Harness.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from misaka.durable import MemoryStorage, defineExtension, defineTask, hook
from misaka.durable.ids import idFromNumber, seqFromNumber
from tests.durable_support import context
from tests.task_support import openTasks

conversationId = idFromNumber(1)
entryId = idFromNumber(2)
taskId = idFromNumber(4)
seq = seqFromNumber(1)


def test_brands_numeric_ids_by_record_kind_and_carries_task_result_types():
    assert isinstance(conversationId, int)
    assert json.dumps(taskId) == "4"
    assert isinstance(entryId, int)
    assert isinstance(seq, int)


@pytest.mark.skip(reason="compile-time only: `satisfies` and `expectTypeOf` checks of discriminated record unions")
def test_encodes_discriminator_dependent_fields():
    pass


async def test_types_submissions_task_waits_compaction_and_tasks_erased_into_extensions():
    input = {"type": "input", "content": "hi", "whenBusy": "steer"}
    write = {"type": "write", "entry": {"kind": "note"}}
    assert input["type"] == "input"
    assert write["type"] == "write"

    # A task with narrowed input, several phases, and custom hooks.
    async def plan(task: Any, runtime: Any, ctx: Any) -> None:
        assert task["state"]["checkpoint"]["steps"] == task["input"]["steps"]
        await runtime.commit(lambda tx, current: {"status": "running", "checkpoint": {"phase": "run", "step": 0}}, ctx)

    async def run(task: Any, runtime: Any, ctx: Any) -> None:
        assert task["state"]["checkpoint"]["step"] == 0
        await runtime.hooks.each("beforeStep", lambda handler: None)
        await runtime.commit(
            lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": {"ran": 1}}}, ctx
        )

    async def abort(_task: Any, runtime: Any, ctx: Any) -> None:
        return None

    Stepper = defineTask(
        {
            "name": "test.stepper",
            "version": 1,
            "initial": lambda input: {"phase": "plan", "steps": input["steps"]},
            "phases": {"plan": plan, "run": run},
            "abort": abort,
        }
    )
    registration = hook(Stepper, {"beforeStep": lambda step, *_: {"skip": True} if step > 1 else None})
    # Erased into an extension, whatever its input, phases, and hooks.
    extension = defineExtension({"name": "stepper", "tasks": [Stepper], "hooks": [registration]})
    assert registration["task"] == "test.stepper"
    assert extension["name"] == "stepper"

    harness, registry, _reports = await openTasks(MemoryStorage(), [])
    registry.install(extension)
    root = await harness.root(context)
    id = await root.commit(
        lambda tx: tx.createTask(Stepper, {"steps": 2}, {"ownership": {"kind": "conversation"}}), context
    )
    harness.resume()
    settled = await harness.waitForTask(id, context)
    assert settled["state"]["outcome"] == {"status": "completed", "result": {"ran": 1}}
    compaction = await root.compact(None, context)
    assert isinstance(compaction, int)
    # No model is configured here, so the compaction ends as a failure the same way for every task ID.
    outcome = (await harness.waitForTask(compaction, context))["state"]["outcome"]
    assert outcome["status"] == "failed"
    assert outcome["error"]["detail"] == {"reason": "no_model"}
    await harness.close(context)
