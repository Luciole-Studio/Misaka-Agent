"""Run a durable task.

Run from the repository root:
    python -m tests.durable_examples.example_12_tasks
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.ai.models import createModels
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import (
    Harness,
    MemoryStorage,
    createRegistry,
    defineExtension,
    defineTask,
)

context = BACKGROUND_CONTEXT

# A task is a small state machine. Its state, the checkpoint, is saved after
# every step, so after a crash the next open continues from the last saved
# step. The usual pattern: save what you are about to do, do it, then save
# the result. A crash between doing and saving reruns that step, so the step
# must be safe to repeat; here the fake payment service ignores a repeated key.
payments: dict[str, int] = {}


# One handler per phase. Each must save progress through runtime.commit():
# its callback returns the next checkpoint or the final outcome, and that
# state is saved in the same commit as everything else the callback wrote.
async def prepare(task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    await runtime.commit(
        lambda tx, current: {"status": "running", "checkpoint": {"phase": "charge", "key": f"payment-{task['id']}"}},
        taskContext,
    )


async def charge(task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    key = task["state"]["checkpoint"]["key"]
    if key not in payments:
        payments[key] = task["input"]["amount"] * 100
    receipt = payments[key]
    await runtime.commit(
        lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": {"receipt": receipt}}},
        taskContext,
    )


# Runs instead of the phases after harness.abortTask(); it decides the outcome.
async def abort(_task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, taskContext)


Payment = defineTask(
    {
        "name": "example.payment",
        "version": 1,
        "initial": lambda input: {"phase": "prepare"},
        "phases": {"prepare": prepare, "charge": charge},
        "abort": abort,
    }
)


async def main() -> None:
    # Extensions bring task code; the Harness finds it by name in the registry.
    # Nothing runs until resume() or a call that waits for progress, such as
    # waitForTask().
    registry = createRegistry()
    registry.install(defineExtension({"name": "payments", "tasks": [Payment]}))
    harness = await Harness.open(MemoryStorage(), {"models": createModels(), "registry": registry}, context)
    root = await harness.root(context)
    # Every task names its owner. This one belongs to the conversation; a task can
    # also own child tasks and wait for them (example_24_child_tasks.py).
    paymentId = await root.commit(
        lambda tx: tx.createTask(Payment, {"amount": 5}, {"ownership": {"kind": "conversation"}}), context
    )
    # The finished task record is the durable receipt; waitForTask() knows its result type.
    paid = await harness.waitForTask(paymentId, context)
    print("payment outcome:", paid["state"]["outcome"])

    await harness.close(context)


if __name__ == "__main__":
    asyncio.run(main())
