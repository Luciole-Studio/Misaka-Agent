"""Close, reopen, and continue where a task stopped.

Run from the repository root:
    python -m tests.durable_examples.example_13_recovery
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from typing import Any

from misaka.ai.models import createModels
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import Harness, createRegistry, defineExtension, defineTask
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.utils.js import UNDEFINED

context = BACKGROUND_CONTEXT

reached: dict[str, Callable[[int], None]] = {"tick": lambda n: None}


async def tick(task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    n = task["state"]["checkpoint"]["n"]
    # Save the intent before the effect. A memo keeps the first value
    # written under its name, so if the process dies after printing but
    # before the next checkpoint is saved, the rerun sees the memo and
    # does not print the same tick twice.
    if (await runtime.memo(f"printed-{n}", taskContext)) is UNDEFINED:
        await runtime.memo(f"printed-{n}", True, taskContext)
        print(f"tick {n}")
    reached["tick"](n)

    # Save the outcome: the next tick, or the final result.
    def next_(tx: Any, current: Any) -> dict[str, Any]:
        if n == task["input"]["to"]:
            return {"status": "terminal", "outcome": {"status": "completed", "result": f"counted to {n}"}}
        return {"status": "running", "checkpoint": {"phase": "tick", "n": n + 1}}

    await runtime.commit(next_, taskContext)
    # Wait a little between ticks. Closing the Harness cancels this wait;
    # the checkpoint saved above is where the next Harness continues.
    await runtime.sleep(int(time.time() * 1000) + 50, taskContext)


async def abort(_task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, taskContext)


Ticker = defineTask(
    {
        "name": "example.ticker",
        "version": 1,
        "initial": lambda input: {"phase": "tick", "n": 1},
        "phases": {"tick": tick},
        "abort": abort,
    }
)


async def main() -> None:
    # Everything a task needs to continue is in storage, so a new Harness over the
    # same storage picks up where the last one stopped. This example keeps its
    # storage in a SQLite file so it survives closing.
    directory = tempfile.mkdtemp(prefix="pi-durable-example-")
    databasePath = os.path.join(directory, "session.sqlite")

    registry = createRegistry()
    registry.install(defineExtension({"name": "ticker", "tasks": [Ticker]}))

    async def open() -> Any:
        return await Harness.open(
            await openNodeSqliteStorage(databasePath), {"models": createModels(), "registry": registry}, context
        )

    # First run: start counting to 5, and close the Harness right after tick 2 is
    # printed, before its next checkpoint is saved. That is the same situation as
    # a crash between the effect and saving its outcome.
    firstRun = await open()
    tickerId = await (await firstRun.root(context)).commit(
        lambda tx: tx.createTask(Ticker, {"to": 5}, {"ownership": {"kind": "conversation"}}), context
    )
    tickTwo: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def onTick(n: int) -> None:
        if n == 2 and not tickTwo.done():
            tickTwo.set_result(None)

    reached["tick"] = onTick
    firstRun.resume()
    await tickTwo
    await firstRun.close(context)
    reached["tick"] = lambda n: None

    # Read the saved record through a Harness that never resumes, so nothing runs.
    reader = await open()
    saved = await reader.getTask(tickerId, context)
    await reader.close(context)
    print("closed; saved checkpoint:", saved["state"], "memos:", saved["memos"])

    # Second run: nothing to restart by hand. Waiting for the unfinished task
    # enables scheduling and continues it. Tick 2 runs again because its outcome
    # was never saved, but its memo says it was already printed.
    secondRun = await open()
    counted = await secondRun.waitForTask(tickerId, context)
    print("after reopen:", counted["state"]["outcome"])
    await secondRun.close(context)
    shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
