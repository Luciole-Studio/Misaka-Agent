"""pi durable's ``test/task-support.ts``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from misaka.ai.models import createModels
from misaka.chord.context import abortError
from misaka.durable import Harness, createRegistry, defineExtension
from tests.durable_support import context, flush


class Deferred:
    def __init__(self) -> None:
        self.promise: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def resolve(self, value: Any = None) -> None:
        if not self.promise.done():
            self.promise.set_result(value)

    def reject(self, error: BaseException) -> None:
        if not self.promise.done():
            self.promise.set_exception(error)


def deferred() -> Deferred:
    return Deferred()


async def aborted(signal: Any) -> Any:
    """Reject with the signal's reason once it aborts; for handlers that block until cancelled."""
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    if signal.aborted:
        raise abortError(signal)
    signal.addEventListener("abort", lambda: future.done() or future.set_exception(abortError(signal)), once=True)
    return await future


async def race(*awaitables: Any) -> Any:
    """JS ``Promise.race``: settles like the first awaitable to settle. Awaitables wrapped here that lost are cancelled."""
    futures = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    own = [future for future, awaitable in zip(futures, awaitables, strict=True) if future is not awaitable]
    try:
        done, _pending = await asyncio.wait(futures, return_when=asyncio.FIRST_COMPLETED)
        return next(future for future in futures if future in done).result()
    finally:
        for future in own:
            future.cancel()


async def eventually(check: Callable[[], Any]) -> None:
    """Flush macrotask turns until ``check`` holds."""
    for _ in range(200):
        result = check()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        if result:
            return
        await flush()
    raise RuntimeError("Condition was not reached")


async def settled(promise: Any) -> bool:
    """Whether ``promise`` has settled after pending work flushes."""
    state = {"done": False}

    def mark(_: Any) -> None:
        state["done"] = True

    asyncio.ensure_future(promise).add_done_callback(mark)
    await flush()
    return state["done"]


async def openTasks(storage: Any, tasks: list[dict[str, Any]], options: dict[str, Any] | None = None) -> Any:
    """Open a Harness whose registry holds ``tasks``; failures passed to ``onReport`` are collected. Returns
    ``(harness, registry, reports)``."""
    options = options or {}
    registry = options.get("registry") or createRegistry()
    if len(tasks) > 0:
        registry.install(defineExtension({"name": "tasks", "tasks": tasks}))
    reports: list[Any] = []
    harness = await Harness.open(
        storage,
        {
            "models": createModels(),
            "registry": registry,
            "onReport": reports.append,
            **({} if options.get("now") is None else {"now": options["now"]}),
            **({} if options.get("settings") is None else {"settings": options["settings"]}),
        },
        context,
    )
    return harness, registry, reports


def completed(result: Any) -> dict[str, Any]:
    """Next state that completes a task with ``result``."""
    return {"status": "terminal", "outcome": {"status": "completed", "result": result}}


def abortedWith(reason: str) -> dict[str, Any]:
    """Next state that aborts a task."""
    return {"status": "terminal", "outcome": {"status": "aborted", "reason": reason}}


class CountingReader:
    """Registry reader that counts live subscriptions."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry
        self._count = 0

    def snapshot(self) -> Any:
        return self._registry.snapshot()

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._count += 1
        unsubscribe = self._registry.subscribe(listener)
        active = {"on": True}

        def release() -> None:
            if not active["on"]:
                return
            active["on"] = False
            self._count -= 1
            unsubscribe()

        return release

    def subscriptions(self) -> int:
        return self._count


def countingReader(registry: Any) -> CountingReader:
    return CountingReader(registry)
