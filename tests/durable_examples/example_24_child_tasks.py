"""A task that owns child tasks: a checkout charges four payments at once and waits for them.

The task graph view prints the checkout's tree while its payments run.

Run from the repository root:
    python -m tests.durable_examples.example_24_child_tasks
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from typing import Any

from misaka.ai.models import createModels
from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable import Harness, createRegistry, defineExtension, defineTask
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage

context = BACKGROUND_CONTEXT

# A fake bank: it declines an expired card at once, and confirms other charges after a moment.
charged: set[str] = set()


async def charge(task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    card = task["input"]["card"]
    if card.startswith("expired"):
        error = {"message": f"{card} declined"}
        await runtime.commit(
            lambda tx, current: {"status": "terminal", "outcome": {"status": "failed", "error": error}}, taskContext
        )
        return
    charged.add(card)
    await runtime.sleep(task["state"]["checkpoint"]["at"], taskContext)
    await runtime.commit(
        lambda tx, current: {"status": "terminal", "outcome": {"status": "completed", "result": {"card": card}}},
        taskContext,
    )


# Each payment undoes its own effect when aborted.
async def abortPayment(task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    card = task["input"]["card"]
    refunded = card in charged
    charged.discard(card)
    print(f"  payment {card} aborted{', refunded' if refunded else ''}")
    await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, taskContext)


Payment = defineTask(
    {
        "name": "example.payment",
        "version": 1,
        "initial": lambda input: {"phase": "charge", "at": int(time.time() * 1000) + 100},
        "phases": {"charge": charge},
        "abort": abortPayment,
    }
)


# The checkout creates its payments as child tasks and waits for all of them.
# With `failFast`, the first payment that fails aborts the others; the checkout
# itself is not aborted and decides its outcome once every payment is done.
async def pay(task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    async def createPayments(tx: Any, current: Any) -> dict[str, Any]:
        payments: list[int] = []
        for card in task["input"]["cards"]:
            payments.append(
                await tx.createTask(Payment, {"card": card}, {"ownership": {"kind": "task", "taskId": task["id"]}})
            )
        return {"status": "waiting", "checkpoint": {"phase": "decide", "payments": payments}, "on": payments, "policy": "failFast"}

    await runtime.commit(createPayments, taskContext)


async def decide(task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    outcomes = await runtime.outcomes(task["state"]["checkpoint"]["payments"], taskContext)
    print(f"  payments: {', '.join(outcome['status'] for outcome in outcomes)}")
    paid = all(outcome["status"] == "completed" for outcome in outcomes)
    await runtime.commit(
        lambda tx, current: (
            {"status": "terminal", "outcome": {"status": "completed", "result": "order placed"}}
            if paid
            else {"status": "terminal", "outcome": {"status": "failed", "error": {"message": "payment failed"}}}
        ),
        taskContext,
    )


# Runs only after every payment is done, so the refunds have already happened.
async def abortCheckout(_task: dict[str, Any], runtime: Any, taskContext: Any) -> None:
    print("  checkout aborted")
    await runtime.commit(lambda tx, current: {"status": "terminal", "outcome": {"status": "aborted"}}, taskContext)


Checkout = defineTask(
    {
        "name": "example.checkout",
        "version": 1,
        "initial": lambda input: {"phase": "pay"},
        "phases": {"pay": pay, "decide": decide},
        "abort": abortCheckout,
    }
)


# Print the live tasks as a tree along their owner edges.
def printGraph(graph: dict[str, Any]) -> None:
    nodes = list(graph["tasks"].values())

    def printNode(node: dict[str, Any], depth: int) -> None:
        state = node["state"]
        status = f"waiting on {', '.join(str(id) for id in state['on'])}" if state["status"] == "waiting" else state["status"]
        print(f"  {'  ' * depth}{node['kind']} {node['id']}: {status}")
        for child in nodes:
            if child.get("owner") == node["id"]:
                printNode(child, depth + 1)

    for node in nodes:
        if node.get("owner") is None:
            printNode(node, 0)


async def main() -> None:
    registry = createRegistry()
    registry.install(defineExtension({"name": "checkout", "tasks": [Payment, Checkout]}))
    directory = tempfile.mkdtemp(prefix="pi-durable-example-")
    databasePath = os.path.join(directory, "session.sqlite")

    async def open() -> Any:
        return await Harness.open(
            await openNodeSqliteStorage(databasePath), {"models": createModels(), "registry": registry}, context
        )

    def checkout(root: Any, cards: list[str]) -> Any:
        return root.commit(
            lambda tx: tx.createTask(Checkout, {"cards": cards}, {"ownership": {"kind": "conversation"}}), context
        )

    harness = await open()
    root = await harness.root(context)

    print("One card is declined:")
    id = await checkout(root, ["visa-1", "expired-2", "visa-3", "visa-4"])
    print(f"  checkout: {(await harness.waitForTask(id, context))['state']['outcome']['status']}")

    print("The customer cancels:")
    id = await checkout(root, ["visa-5", "visa-6", "visa-7", "visa-8"])
    harness.resume()
    await asyncio.sleep(0.02)
    graph = await harness.taskGraph(context)
    printGraph(graph.value)
    graph.dispose()
    await harness.abortTask(id, context)
    print(f"  checkout: {(await harness.waitForTask(id, context))['state']['outcome']['status']}")

    print("The process stops while the payments run, and a new one continues:")
    id = await checkout(root, ["visa-9", "visa-10", "visa-11", "visa-12"])
    await asyncio.sleep(0.02)
    await harness.close(context)
    harness = await open()
    root = await harness.root(context)
    print(f"  checkout: {(await harness.waitForTask(id, context))['state']['outcome']['status']}")

    await harness.close(context)
    shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
