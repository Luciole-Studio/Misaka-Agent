"""pi chord's ``test/review.test.ts``: regression tests for Sol's phase 1a review (findings #1-#11) and the requested
race tests. (``tests/test_chord_review.py`` is misaka's own review suite.)

MISAKA adaptations: facets are ``Facet`` objects; handlers take the arguments the runtime passes; ``setImmediate`` is a
few event-loop turns; the white-box break of ``startTasks`` sets an instance attribute; the forged ctx is a
``SimpleNamespace``; ``Value``/``fits`` are ``misaka.chord._typebox.Value`` and ``misaka.chord.schema.fits``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.chord import (
    createHost,
    defineRegistry,
    hostNamed,
    isChordError,
    local,
    memoryLinkPair,
    method,
    remote,
)
from misaka.chord._typebox import Type, Value
from misaka.chord.errors import ChordError
from misaka.chord.host import Facet
from misaka.chord.schema import fits
from misaka.utils.js import AbortController
from tests.fixtures.chord_counting import CountingLink


async def tick() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def ticks(n: int = 10) -> None:
    for _ in range(n):
        await tick()


class Latch:
    def __init__(self) -> None:
        self.promise: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def open(self) -> None:
        if not self.promise.done():
            self.promise.set_result(None)


class Emitter:
    def __init__(self, failSubscribe: bool = False) -> None:
        self._failSubscribe = failSubscribe
        self._listeners: dict[Callable[[Any], None], None] = {}

    def emit(self, event: Any) -> None:
        for fn in list(self._listeners):
            fn(event)

    def subscribe(self, fn: Callable[[Any], None]) -> Callable[[], Any]:
        if self._failSubscribe:
            raise RuntimeError("subscribe fails")
        self._listeners[fn] = None
        return lambda: self._listeners.pop(fn, None)

    @property
    def size(self) -> int:
        return len(self._listeners)


def quiet(**options: Any) -> Any:
    return createHost({"self": {"kind": "host"}, "onError": lambda _error: None, **options})


Item = local(
    "t.item",
    {
        "version": 1,
        "input": Type.Object({"id": Type.String()}),
        "members": {"version": method(None, Type.String())},
    },
)


def itemFacet(version: str) -> Facet:
    return Facet("item", lambda api: api.serve(Item, lambda *_: {"version": lambda _ctx: version}))


# ─── review: reload commit ───


async def test_1_a_failing_listener_subscription_fails_prepare_the_code_version_and_all_objects_stay_unchanged():
    host = quiet()
    sources = {"a": Emitter(), "b": Emitter(failSubscribe=True), "c": Emitter()}

    def facet(version: str, listen: bool) -> Facet:
        def factory(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            if listen:
                self.on("events", sources[input["id"]], lambda _event, _ctx: None)
            return {"version": lambda _ctx: version}

        return Facet("item", lambda api: api.serve(Item, factory))

    host.install(facet("v1", False))
    refs = [host.hold(Item, {"id": id}) for id in ["a", "b", "c"]]
    await asyncio.gather(*[ref.ready for ref in refs])
    codeVersion = host.codeVersion
    with pytest.raises(Exception, match="subscribe fails"):
        await host.reload([facet("v2", True)])
    assert host.codeVersion == codeVersion
    assert await asyncio.gather(*[ref.version() for ref in refs]) == ["v1", "v1", "v1"]
    assert sources["a"].size == 0
    assert sources["c"].size == 0


async def test_2_migration_is_validated_against_the_new_remote_schemas_not_the_obsolete_ones_dev_mode():
    host = quiet(dev=True)
    V1 = remote(
        "t.counted",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("host"),
            "variants": {"main": {"state": {"count": Type.Object({"n": Type.Number()})}}},
        },
    )
    V2 = remote(
        "t.counted",
        {
            "version": 2,
            "input": Type.Object({}),
            "host": hostNamed("host"),
            "variants": {"main": {"state": {"count": Type.Object({"total": Type.Number()})}}},
        },
    )
    host.install(
        Facet(
            "counted",
            lambda api: api.serve(
                V1,
                lambda _input, _principal, self: {
                    "variant": "main",
                    "count": self.state("count", Type.Object({"n": Type.Number()}), {"n": 3}),
                },
                {"linger": 60_000},
            ),
        )
    )
    first = host.hold(V1, {})
    await first.ready
    first.release()
    await host.reload(
        [
            Facet(
                "counted",
                lambda api: api.serve(
                    V2,
                    lambda _input, _principal, self: {
                        "variant": "main",
                        "count": self.state(
                            "count",
                            Type.Object({"total": Type.Number()}),
                            {"total": 0},
                            {"version": 2, "migrate": lambda old, _from: {"total": old["n"]}},
                        ),
                    },
                    {"linger": 60_000},
                ),
            )
        ]
    )
    second = host.hold(V2, {})
    values: list[Any] = []
    second.count.subscribe(values.append)
    await second.ready
    await ticks()
    assert second.count.value == {"total": 3}
    await host.close()


async def test_1_a_commit_that_still_fails_midway_fails_the_host_terminally():
    host = quiet()
    host.install(itemFacet("v1"))
    ref = host.hold(Item, {"id": "a"})
    await ref.ready

    def fails() -> None:
        raise RuntimeError("install step fails")

    # Everything fallible runs in prepare now, so break an install step directly (white box).
    for object in host.objects.values():
        object.startTasks = fails
    with pytest.raises(ChordError, match="install step fails"):
        await host.reload([itemFacet("v2")])
    with pytest.raises(ChordError) as info:
        await ref.version()
    assert isChordError(info.value, "unavailable") and info.value.reason == "host_failed"


async def test_reentrant_observers_calling_another_object_during_a_commit_see_the_whole_new_code_version():
    host = quiet()
    Other = local(
        "t.other", {"version": 1, "input": Type.Object({}), "members": {"version": method(None, Type.String())}}
    )

    def facet(version: str) -> Facet:
        def setup(api: Any) -> None:
            api.serve(Item, lambda *_: {"version": lambda _ctx: version})
            api.serve(Other, lambda *_: {"version": lambda _ctx: version})

        return Facet("pair", setup)

    host.install(facet("v1"))
    item = host.hold(Item, {"id": "a"})
    other = host.hold(Other, {})
    await asyncio.gather(item.ready, other.ready)
    seen: list[Any] = []
    item.onShape(lambda _shape: seen.append(other.version()))
    await host.reload([facet("v2")])
    assert await asyncio.gather(*seen) == ["v2"]


# ─── review: leases, invocations, activation ───

Store = local("t.store", {"version": 1, "input": Type.Object({}), "members": {"count": Type.Unknown()}})


async def test_4_a_running_listener_holds_a_lease_the_gate_waits_for_it_before_migrating():
    host = quiet()
    events = Emitter()
    go = Latch()
    seen: list[Any] = []

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        count = self.state("count", Type.Object({"n": Type.Number()}), {"n": 1})

        async def slow(_event: Any, _ctx: Any) -> None:
            await go.promise
            seen.append(count.value)

        self.on("slow", events, slow)
        return {"count": count}

    host.install(Facet("store", lambda api: api.serve(Store, factory)))
    ref = host.hold(Store, {})
    await ref.ready
    events.emit(1)
    await tick()
    codeVersion = host.codeVersion
    migrated = Facet(
        "store",
        lambda api: api.serve(
            Store,
            lambda _input, _principal, self: {
                "count": self.state(
                    "count",
                    Type.Object({"total": Type.Number()}),
                    {"total": 0},
                    {"version": 2, "migrate": lambda old, _from: {"total": old["n"]}},
                )
            },
        ),
    )
    reload = host.reload([migrated], {"gateDeadline": 2000})
    await ticks(20)
    assert host.codeVersion == codeVersion, "the gate must wait for the listener"
    go.open()
    await reload
    assert seen == [{"n": 1}]
    assert ref.count.value == {"total": 1}


async def test_5_disposal_waits_for_running_methods_before_closing_resources_released_and_forced():
    Worker = local(
        "t.busy",
        {"version": 1, "input": Type.Object({"id": Type.String()}), "members": {"work": method(None, Type.Boolean())}},
    )
    log: list[str] = []
    go = [Latch()]
    host = quiet()

    def factory(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        id = input["id"]

        def close(conn: Any) -> None:
            conn["closed"] = True
            log.append(f"close {id}")

        connResource = self.resource("conn", {"version": 1, "open": lambda _options: {"closed": False}, "close": close})

        async def work(_ctx: Any) -> bool:
            await go[0].promise
            log.append(f"done {id}")
            return connResource.value["closed"]

        return {"work": work}

    host.serve(Worker, factory)
    released = host.hold(Worker, {"id": "r"})
    call = released.work()
    await ticks()
    released.release()
    await ticks()
    go[0].open()
    assert await call is False
    await ticks()
    assert log == ["done r", "close r"]

    go[0] = Latch()
    log.clear()
    forced = host.hold(Worker, {"id": "f"}, {"principal": {"user": "u"}})
    forcedCall = forced.work()
    await ticks()
    invalidated = host.invalidatePrincipal(lambda principal: principal.get("user") == "u")
    await ticks()
    assert forced.status == "gone"
    assert log == []
    go[0].open()
    await invalidated
    assert await forcedCall is False
    assert log == ["done f", "close f"]


async def test_6_a_registry_conflict_found_while_finalizing_activation_unwinds_the_loser_completely():
    errors: list[Any] = []
    host = createHost({"self": {"kind": "host"}, "onError": errors.append})
    Routes = defineRegistry("t.review-routes")
    log: list[str] = []
    types = [local(name, {"version": 1, "input": Type.Object({})}) for name in ["t.eager-a", "t.eager-b"]]
    for type in types:

        def factory(_input: Any, _principal: Any, self: Any, type: Any = type) -> dict[str, Any]:
            self.resource(
                "r",
                {
                    "version": 1,
                    "open": lambda _options: log.append(f"open {type.name}"),
                    "close": lambda _value: log.append(f"close {type.name}"),
                },
            )
            self.register(Routes, {"same": type.name})
            return {}

        host.serve(type, factory, {"lifetime": "host"})
    await ticks(20)
    assert len([entry for entry in log if entry.startswith("open")]) == 2
    assert len([entry for entry in log if entry.startswith("close")]) == 1
    assert len(host.registry(Routes).table()) == 1
    assert any(isChordError(error, "invalid") and error.reason == "registry_conflict" for error in errors)


async def test_6_releasing_the_last_hold_while_listeners_start_up_does_not_resurrect_the_object():
    host = quiet()
    log: list[str] = []
    ref: list[Any] = []
    runs = [0]

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        runs[0] += 1
        count = self.state("count", Type.Number(), 0)
        self.resource(
            "r", {"version": 1, "open": lambda _options: log.append("open"), "close": lambda _value: log.append("close")}
        )
        # A readable delivers its current value as the listener starts: release the only hold right then.
        self.on("drop", count, lambda _value, _ctx: ref[0].release() if ref else None)
        return {"count": count}

    host.serve(Store, factory)
    ref.append(host.hold(Store, {}))
    try:
        await ref[0].ready
    except ChordError:
        pass
    await ticks(20)
    assert log == ["open", "close"]
    again = host.hold(Store, {})
    ref.clear()
    await again.ready
    assert runs[0] == 2


# ─── review: prepare isolation and contracts ───

Db = local("t.db", {"version": 1, "input": Type.Object({}), "members": {"read": method(None, Type.Unknown())}})


async def test_7_a_prepare_rerun_cannot_touch_retained_resources_even_open_ones():
    host = quiet()
    native = {"mutated": False}

    def facet(touch: bool) -> Facet:
        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            db = self.resource("db", {"version": 1, "open": lambda _options: native})
            if touch:
                db.value["mutated"] = True
            return {"read": lambda _ctx: db.value}

        return Facet("db", lambda api: api.serve(Db, factory))

    host.install(facet(False))
    ref = host.hold(Db, {})
    await ref.ready
    with pytest.raises(ChordError) as info:
        await host.reload([facet(True)])
    assert isChordError(info.value, "not_ready")
    assert native["mutated"] is False
    assert await ref.read() == {"mutated": False}


async def test_7_objects_of_types_a_reload_adds_start_listeners_and_tasks_only_at_commit():
    host = quiet()
    log: list[str] = []
    Fresh = local("t.fresh", {"version": 1, "input": Type.Object({})})
    host.install(itemFacet("v1"))
    await asyncio.gather(*[host.hold(Item, {"id": id}).ready for id in ["a", "b"]])

    def plugins(fail: bool) -> list[Facet]:
        def fresh(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            async def task(_ctx: Any) -> None:
                log.append("task started")

            self.task(task)
            return {}

        def item(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            self.get(Fresh, {})
            if fail and input["id"] == "b":
                raise RuntimeError("b fails")
            return {"version": lambda _ctx: "v2"}

        return [
            Facet("fresh", lambda api: api.serve(Fresh, fresh)),
            Facet("item", lambda api: api.serve(Item, item)),
        ]

    with pytest.raises(ChordError, match="b fails"):
        await host.reload(plugins(True))
    await ticks()
    assert log == []
    await host.reload(plugins(False))
    await ticks()
    assert log == ["task started"]


async def test_8_a_breaking_provider_change_and_its_updated_consumer_reload_in_one_batch():
    host = quiet()
    P1 = remote(
        "t.provider",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("host"),
            "variants": {"main": {"methods": {"get": method(None, Type.String())}}},
        },
    )
    P2 = remote(
        "t.provider",
        {
            "version": 2,
            "input": Type.Object({}),
            "host": hostNamed("host"),
            "variants": {"main": {"methods": {"fetch": method(None, Type.String())}}},
        },
    )
    Consumer = local(
        "t.consumer", {"version": 1, "input": Type.Object({}), "members": {"ask": method(None, Type.String())}}
    )

    def both(old: bool) -> Facet:
        def setup(api: Any) -> None:
            if old:
                api.serve(P1, lambda *_: {"variant": "main", "get": lambda _ctx: "old"})

                def consumer(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                    provider = self.get(P1, {})
                    return {"ask": lambda ctx: provider.get(ctx)}

            else:
                api.serve(P2, lambda *_: {"variant": "main", "fetch": lambda _ctx: "new"})

                def consumer(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                    provider = self.get(P2, {})
                    return {"ask": lambda ctx: provider.fetch(ctx)}

            api.serve(Consumer, consumer)

        return Facet("both", setup)

    host.install(both(True))
    consumer = host.hold(Consumer, {})
    assert await consumer.ask() == "old"
    await host.reload([both(False)])
    assert await consumer.ask() == "new"


@pytest.mark.parametrize("reopen", ["works", "fails"])
async def test_9_an_exclusive_resource_whose_close_failed_is_restored_on_rollback_or_marked_unavailable(reopen):
    host = quiet()
    log: list[str] = []
    opens = [0]

    def facet(rev: int) -> Facet:
        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            def open(_options: Any) -> Any:
                if rev == 2:
                    raise RuntimeError("new port fails")
                opens[0] += 1
                if opens[0] > 1 and reopen == "fails":
                    raise RuntimeError("reopen fails")
                log.append(f"open {rev}")
                return {"rev": rev}

            def close(_value: Any) -> None:
                log.append(f"close {rev}")
                raise RuntimeError("close fails")

            port = self.resource("port", {"version": rev, "exclusive": True, "open": open, "close": close})
            return {"read": lambda _ctx: port.value}

        return Facet("db", lambda api: api.serve(Db, factory))

    host.install(facet(1))
    ref = host.hold(Db, {})
    await ref.ready
    # The failing close aborts the reload at the gate; the old resource must not stay closed.
    with pytest.raises(ChordError, match="close fails"):
        await host.reload([facet(2)])
    if reopen == "works":
        assert log == ["open 1", "close 1", "open 1"]
        assert await ref.read() == {"rev": 1}
    else:
        with pytest.raises(ChordError) as info:
            await ref.read()
        assert isChordError(info.value, "unavailable")


async def test_10_retirement_closes_dropped_resources_before_releasing_dropped_holds():
    host = quiet()
    log: list[str] = []
    Dep = local("t.dep", {"version": 1, "input": Type.Object({})})

    def facet(withDecls: bool) -> Facet:
        def setup(api: Any) -> None:
            def dep(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                self.resource(
                    "r", {"version": 1, "open": lambda _options: None, "close": lambda _value: log.append("dep closed")}
                )
                return {}

            def item(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                if withDecls:
                    self.get(Dep, {})

                    # Slow close: releasing the hold first would let the dependency close before it finishes.
                    async def close(_value: Any) -> None:
                        await ticks(3)
                        log.append("own closed")

                    self.resource("own", {"version": 1, "open": lambda _options: None, "close": close})
                return {"version": lambda _ctx: "v1" if withDecls else "v2"}

            api.serve(Dep, dep)
            api.serve(Item, item)

        return Facet("item", setup)

    host.install(facet(True))
    ref = host.hold(Item, {"id": "a"})
    await ref.ready
    go = Latch()

    async def pinnedRoot(ctx: Any) -> None:
        await ref.version(ctx)
        await go.promise

    pinned = host.root(pinnedRoot)
    await tick()
    await host.reload([facet(False)])
    assert log == []
    go.open()
    await pinned
    await ticks(20)
    assert log == ["own closed", "dep closed"]


def test_11_fits_has_no_false_positives_for_unmodelled_keywords_counterexamples_checked_with_value_check():
    cases = [
        (Type.String(), Type.String({"not": {"const": "x"}}), "x"),
        (Type.Array(Type.Number()), Type.Array(Type.Number(), {"contains": Type.Literal(1)}), [2]),
        (
            Type.Object({"a": Type.String()}),
            Type.Object({"a": Type.String()}, {"additionalProperties": Type.Number()}),
            {"a": "s", "b": "t"},
        ),
        (
            Type.Object({"x": Type.String()}),
            Type.Object({}, {"patternProperties": {"^x$": Type.Number()}}),
            {"x": "s"},
        ),
        (Type.Number(), Type.Intersect([Type.Number(), Type.Number({"minimum": 1})]), 0),
        (
            Type.Object({"a": Type.String()}, {"additionalProperties": Type.Number()}),
            Type.Object({"a": Type.String(), "b": Type.Optional(Type.String())}),
            {"a": "", "b": 1},
        ),
    ]
    for sub, sup, value in cases:
        assert Value.Check(sub, value) and not Value.Check(sup, value), f"bad counterexample {json.dumps(value)}"
        assert fits(sub, sup) is False, f"fits({json.dumps(sub)}, {json.dumps(sup)})"
    # Additive changes still fit.
    assert fits(
        Type.Object({"a": Type.String()}), Type.Object({"a": Type.String(), "b": Type.Optional(Type.Number())})
    )
    assert fits(Type.Object({"a": Type.String(), "extra": Type.Number()}), Type.Object({"a": Type.String()}))


async def test_11_arguments_are_stripped_to_what_the_consumer_knows_then_to_what_the_provider_knows():
    host = quiet()
    Strict = remote(
        "t.strict",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("host"),
            "variants": {
                "main": {
                    "methods": {
                        "echo": method(
                            Type.Object({"a": Type.String()}, {"additionalProperties": False}), Type.Unknown()
                        )
                    }
                }
            },
        },
    )
    host.serve(Strict, lambda *_: {"variant": "main", "echo": lambda args, _ctx: args})
    ref = host.hold(Strict, {})
    assert await ref.echo({"a": "x", "junk": 1}) == {"a": "x"}


# ─── review: ctx races ───

Target = local("t.target", {"version": 1, "input": Type.Object({}), "members": {"hit": method()}})


async def test_an_aborted_task_ctx_cannot_start_fresh_calls():
    host = quiet()
    Runner = local("t.runner", {"version": 1, "input": Type.Object({})})
    hits = [0]
    go = Latch()
    outcome: list[Any] = []

    def hit(_ctx: Any) -> None:
        hits[0] += 1

    host.serve(Target, lambda *_: {"hit": hit})

    def runner(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        target = self.get(Target, {})

        async def task(ctx: Any) -> None:
            await go.promise
            try:
                outcome.append(await target.hit(ctx))
            except Exception as error:  # noqa: BLE001 - `.catch((error) => error)`
                outcome.append(error)

        self.task(task)
        return {}

    host.serve(Runner, runner)
    runnerRef = host.hold(Runner, {})
    await runnerRef.ready
    target = host.hold(Target, {})
    await target.ready
    runnerRef.release()
    await ticks()
    go.open()
    await ticks(20)
    assert hits[0] == 0
    assert isChordError(outcome[0], "aborted")


async def test_an_expired_ctx_and_a_foreign_ctx_are_refused():
    host = quiet()
    other = quiet()
    host.serve(Target, lambda *_: {"hit": lambda _ctx: None})
    target = host.hold(Target, {})
    await target.ready
    expired = await host.root(lambda ctx: ctx)
    with pytest.raises(ChordError) as info:
        await target.hit(expired)
    assert isChordError(info.value, "aborted") and info.value.reason == "expired"

    async def foreignRoot(ctx: Any) -> bool:
        with pytest.raises(ChordError) as info:
            await target.hit(ctx)
        assert isChordError(info.value, "invalid") and info.value.reason == "foreign_ctx"
        return True

    assert await other.root(foreignRoot)


# #10959
async def test_a_remote_reference_refuses_a_foreign_ctx_and_a_ctx_not_made_by_chord_without_calling():
    RemoteTarget = remote(
        "t.remote-target",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"state": {}, "methods": {"hit": method()}}},
        },
    )
    hits = [0]
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})

    def hit(_ctx: Any) -> None:
        hits[0] += 1

    server.serve(RemoteTarget, lambda *_: {"variant": "main", "hit": hit})
    down, up = memoryLinkPair()
    server.accept(down, {"kind": "host", "name": "worker", "principal": {"worker": True}})
    link = CountingLink(up)
    worker = createHost({"self": {"kind": "host"}, "name": "worker", "onError": lambda _error: None})
    worker.connect(link, {"principal": {"kind": "server"}})
    other = createHost({"self": {"kind": "host"}, "name": "other", "onError": lambda _error: None})
    target = worker.hold(RemoteTarget, {})
    await target.ready

    async def foreignRoot(ctx: Any) -> Any:
        try:
            return await target.hit(ctx)
        except Exception as error:  # noqa: BLE001 - `.catch((error) => error)`
            return error

    foreign = await other.root(foreignRoot)
    assert isChordError(foreign, "invalid") and foreign.reason == "foreign_ctx", str(foreign)
    forged = SimpleNamespace(rootId="forged", codeVersion=1, signal=AbortController().signal)
    with pytest.raises(ChordError) as info:
        await target.hit(forged)
    assert isChordError(info.value, "invalid") and info.value.reason == "foreign_ctx"
    await ticks()
    assert hits[0] == 0
    assert link.count("sent", "call") == 0
    await worker.root(lambda ctx: target.hit(ctx))
    assert hits[0] == 1
