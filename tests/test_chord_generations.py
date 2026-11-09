"""pi chord's ``test/generations.test.ts``.

Old code generations and disposed objects must be collectable. A "generation" is the code of one facet module
evaluation: here a facet built by a function call, whose factory closes over a per-generation sentinel watched with
``weakref.finalize`` (upstream's ``FinalizationRegistry``). A generation is alive while anything Chord keeps references
its code.

MISAKA adaptations: ``gc()`` is ``gc.collect()``; a sentinel is a small class instance (a ``dict`` cannot be weakly
referenced); the normalized input of the last test is a ``dict`` too, so the object's sentinel is attached through
the factory's closure instead (what the input stood for: the object).
"""

from __future__ import annotations

import asyncio
import gc
import time
import weakref
from typing import Any

from misaka.chord import createHost, defineRegistry, local, method
from misaka.chord._typebox import Type
from misaka.chord.host import Facet
from misaka.chord.object import refOf


class Sentinel:
    def __init__(self, generation: int) -> None:
        self.generation = generation


class Tracker:
    """Tracks which sentinels are still reachable."""

    def __init__(self) -> None:
        self.alive: set[int] = set()

    def track[T](self, id: int, value: T) -> T:
        self.alive.add(id)
        weakref.finalize(value, self.alive.discard, id)
        return value

    async def collect(self, expected: list[int], timeout: float = 5000) -> list[int]:
        """Collects garbage until only ``expected`` is alive, at most ``timeout`` ms, and returns what is alive
        then."""
        deadline = time.monotonic() + timeout / 1000
        while True:
            gc.collect()
            await asyncio.sleep(0.01)
            if sorted(self.alive) == expected or time.monotonic() >= deadline:
                break
        return sorted(self.alive)


Store = local(
    "t.gen-store",
    {
        "version": 1,
        "input": Type.Object({}),
        "members": {"get": method(None, Type.Number()), "count": Type.Number()},
    },
)
Routes = defineRegistry("t.gen-routes")
RELOADS = 6


def abortedOf(ctx: Any) -> asyncio.Future[None]:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    ctx.signal.addEventListener("abort", lambda: future.done() or future.set_result(None))
    return future


async def test_a_reference_held_across_reloads_keeps_only_the_current_generation_alive():
    generations = Tracker()
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})

    def facet(generation: int) -> Facet:
        sentinel = generations.track(generation, Sentinel(generation))

        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            count = self.state("count", Type.Number(), 0)
            return {"count": count, "get": lambda _ctx: sentinel.generation}

        return Facet("store", lambda api: api.serve(Store, factory))

    host.install(facet(0))
    ref = host.hold(Store, {})
    await ref.ready
    for generation in range(1, RELOADS + 1):
        await host.reload([facet(generation)])
        assert await ref.get() == generation
    assert await generations.collect([RELOADS]) == [RELOADS]
    await host.close()


async def test_a_lifetime_host_object_with_state_a_kept_resource_a_listener_a_task_and_registrations_keeps_only_the_current():
    generations = Tracker()
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})

    def facet(generation: int) -> Facet:
        sentinel = generations.track(generation, Sentinel(generation))

        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            count = self.state("count", Type.Number(), 0)
            # Kept across reloads (same version): adopts each new declaration's close.
            self.resource(
                "conn", {"version": 1, "open": lambda _options: {"open": True}, "close": lambda _value: sentinel.generation}
            )
            self.on("count", count, lambda _event, _ctx: sentinel.generation and None)

            async def task(ctx: Any) -> None:
                await abortedOf(ctx)
                _ = sentinel.generation

            self.task(task)
            self.register(Routes, {"generation": lambda: sentinel.generation})
            return {"count": count, "get": lambda _ctx: sentinel.generation}

        return Facet("store", lambda api: api.serve(Store, factory, {"lifetime": "host"}))

    await host.install(facet(0))
    for generation in range(1, RELOADS + 1):
        await host.reload([facet(generation)])
        assert host.registry(Routes).get("generation")() == generation
    assert await generations.collect([RELOADS]) == [RELOADS]
    await host.close()


async def test_abort_listeners_that_calls_and_tasks_add_to_their_ctx_signal_do_not_outlive_them():
    generations = Tracker()
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})

    def facet(generation: int) -> Facet:
        sentinel = generations.track(generation, Sentinel(generation))

        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            count = self.state("count", Type.Number(), 0)

            async def task(ctx: Any) -> None:
                await abortedOf(ctx)
                _ = sentinel.generation

            self.task(task)

            def get(ctx: Any) -> int:
                ctx.signal.addEventListener("abort", lambda: sentinel.generation)
                return sentinel.generation

            return {"count": count, "get": get}

        return Facet("store", lambda api: api.serve(Store, factory, {"lifetime": "host"}))

    await host.install(facet(0))
    for generation in range(1, RELOADS + 1):
        await host.reload([facet(generation)])

        async def call(ctx: Any) -> Any:
            return await (await ctx.get(Store, {})).get(ctx)

        assert await host.root(call) == generation
    assert await generations.collect([RELOADS]) == [RELOADS]
    await host.close()


async def test_disposed_objects_are_not_kept_once_their_disposal_finished():
    objects = Tracker()
    Item = local(
        "t.gen-item",
        {"version": 1, "input": Type.Object({"id": Type.Number()}), "members": {"id": method(None, Type.Number())}},
    )
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})

    implementations = Tracker()

    def factory(input: Any, _principal: Any, _self: Any) -> dict[str, Any]:
        # MISAKA fork: the normalized input (a dict) cannot be weakly referenced. What holds it stands for it: the
        # object (`object.input` is that input), tracked by `use`, and the implementation, through a sentinel in the
        # factory's closure.
        sentinel = implementations.track(input["id"], Sentinel(input["id"]))
        return {"id": lambda _ctx: sentinel.generation}

    host.serve(Item, factory)

    # In a function of its own, so no reference outlives the iteration in this frame.
    async def use(id: int) -> None:
        ref = host.hold(Item, {"id": id})
        object_ = refOf(ref).object
        assert object_.input == {"id": id}
        objects.track(id, object_)
        del object_
        assert await ref.id() == id
        ref.release()

    for id in range(50):
        await use(id)
    await asyncio.sleep(0.01)
    assert await objects.collect([]) == []
    assert await implementations.collect([]) == []
    await host.close()
