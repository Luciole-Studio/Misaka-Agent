"""pi chord's ``test/readiness.test.ts``.

Startup must not report ready while an eager object has failed.

MISAKA adaptations: the ``for (const operation of ...)`` loops are pytest parameters; facets are ``Facet`` objects;
upstream's ``unexpectedError`` (``assert.fail`` inside ``onError``) collects the errors and the test asserts none came,
since an assertion raised inside a done-callback would only be logged by the event loop; ``setImmediate`` is a few
event-loop turns.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.chord import (
    createHost,
    defineRegistry,
    forbidden,
    hostNamed,
    isChordError,
    local,
    memoryLinkPair,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.errors import ChordError
from misaka.chord.host import Facet


async def tick() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


Eager = local("ready.eager", {"version": 1, "input": Type.Object({})})
Lazy = local("ready.lazy", {"version": 1, "input": Type.Object({})})


def start(host: Any, operation: str, facet: Facet, options: Any = None) -> Any:
    return host.install(facet, options) if operation == "install" else host.load([facet], options)


@pytest.mark.parametrize("operation", ["install", "load"])
async def test_awaits_resource_activation_and_registration_leaving_lazy_objects_lazy(operation):
    unexpected: list[Any] = []
    host = createHost({"self": {"kind": "host"}, "onError": unexpected.append})
    entries = defineRegistry("ready.entries")
    opened: list[asyncio.Future[None]] = []
    lazyRuns = [0]
    settled = [False]

    def setup(api: Any) -> None:
        def eager(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            def open(_options: Any) -> asyncio.Future[None]:
                future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                opened.append(future)
                return future

            self.resource("r", {"version": 1, "open": open})
            self.register(entries, {"eager": "live"})
            return {}

        def lazy(*_: Any) -> dict[str, Any]:
            lazyRuns[0] += 1
            return {}

        api.serve(Eager, eager, {"lifetime": "host"})
        api.serve(Lazy, lazy)

    facet = Facet("ready", setup)
    try:
        loading = start(host, operation, facet)
        loading.add_done_callback(lambda _done: settled.__setitem__(0, True))
        await tick()
        assert settled[0] is False
        assert host.registry(entries).get("eager") is None
        opened[0].set_result(None)
        await loading
        assert host.registry(entries).get("eager") == "live"
        assert lazyRuns[0] == 0
    finally:
        await host.close()
    assert unexpected == []


@pytest.mark.parametrize("failure", ["factory", "dependency", "resource"])
@pytest.mark.parametrize("operation", ["install", "load"])
async def test_rejects_eager_failure_with_the_activation_error(operation, failure):
    errors: list[Any] = []
    host = createHost({"self": {"kind": "host"}, "onError": errors.append})
    cause = forbidden(f"{failure} failed")

    def setup(api: Any) -> None:
        def lazy(*_: Any) -> dict[str, Any]:
            raise cause

        def eager(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            if failure == "factory":
                raise cause
            if failure == "dependency":
                self.get(Lazy, {})
            if failure == "resource":

                def open(_options: Any) -> None:
                    raise cause

                self.resource("r", {"version": 1, "open": open})
            return {}

        api.serve(Lazy, lazy)
        api.serve(Eager, eager, {"lifetime": "host"})

    facet = Facet("bad", setup)
    try:
        with pytest.raises(ChordError) as info:
            await start(host, operation, facet)
        assert info.value is cause
        with pytest.raises(ChordError) as info:
            await host.ready()
        assert info.value is cause
        assert errors == [cause]
    finally:
        await host.close()


async def test_load_installs_every_facet_before_awaiting_eager_consumers():
    unexpected: list[Any] = []
    host = createHost({"self": {"kind": "host"}, "onError": unexpected.append})

    def consumer(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        self.get(Lazy, {})
        return {}

    try:
        await host.load(
            [
                Facet("consumer", lambda api: api.serve(Eager, consumer, {"lifetime": "host"})),
                Facet("provider", lambda api: api.serve(Lazy, lambda *_: {})),
            ]
        )
        assert host.hold(Eager, {}).status == "live"
    finally:
        await host.close()
    assert unexpected == []


async def test_missing_eager_dependencies_reject_load_instead_of_only_reaching_on_error():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})

    def missing(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        self.get(Lazy, {})
        return {}

    try:
        with pytest.raises(ChordError) as info:
            await host.load([Facet("missing", lambda api: api.serve(Eager, missing, {"lifetime": "host"}))])
        assert isChordError(info.value, "unavailable") and info.value.reason == "unknown_type"
    finally:
        await host.close()


async def test_ready_includes_direct_host_serves_but_not_later_serves():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    First = local("ready.first", {"version": 1, "input": Type.Object({})})
    created = [0]

    def first(*_: Any) -> dict[str, Any]:
        created[0] += 1
        return {}

    def later(*_: Any) -> dict[str, Any]:
        raise forbidden("later")

    try:
        host.serve(First, first, {"lifetime": "host"})
        firstReady = host.ready()
        host.serve(Eager, later, {"lifetime": "host"})
        await firstReady
        assert created[0] == 1
        with pytest.raises(ChordError, match="later"):
            await host.ready()
    finally:
        await host.close()


@pytest.mark.parametrize("operation", ["install", "load"])
async def test_can_defer_eager_readiness_until_the_parent_admits_the_worker(operation):
    unexpected: list[Any] = []
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": unexpected.append})
    parent, upstream = memoryLinkPair()
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "worker",
            "upstream": {"link": upstream, "principal": {"kind": "server"}},
            "onError": unexpected.append,
        }
    )
    Status = remote(
        "ready.status",
        {"version": 1, "input": Type.Object({}), "host": hostNamed("server"), "variants": {"default": {}}},
    )
    server.serve(Status, lambda *_: {"variant": "default"})
    bootstrap = [False]
    reported = [False]

    def setup(api: Any) -> None:
        def lazy(*_: Any) -> dict[str, Any]:
            bootstrap[0] = True
            return {}

        def eager(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            status = self.get(Status, {})
            self.resource(
                "r", {"version": 1, "deps": [status], "open": lambda _options: reported.__setitem__(0, True)}
            )
            return {}

        api.serve(Lazy, lazy, {"lifetime": "host"})
        api.serve(Eager, eager, {"lifetime": "host"})

    facet = Facet("worker", setup)
    try:
        await asyncio.wait_for(start(worker, operation, facet, {"awaitHostObjects": False}), 2)
        await asyncio.wait_for(worker.hold(Lazy, {}).ready, 2)
        assert bootstrap[0] is True
        settled = [False]
        ready = worker.ready()
        ready.add_done_callback(lambda _done: settled.__setitem__(0, True))
        await tick()
        assert settled[0] is False
        assert reported[0] is False
        # Control-ready announcement allows the parent to accept the queued upstream holds.
        server.accept(parent, {"kind": "host", "name": "worker", "principal": {"host": "worker"}})
        await asyncio.wait_for(ready, 2)
        assert reported[0] is True
    finally:
        await worker.close()
        await server.close()
    assert unexpected == []
