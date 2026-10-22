"""pi chord's ``test/host.test.ts``.

MISAKA adaptations: ``setImmediate`` (a turn after the pending promise jobs) is ``tick()``, a few event-loop turns; a
mutating ``change`` callback returns ``None`` (MISAKA fork of ``Cell.change``); ``AggregateError`` is an
``ExceptionGroup``; ``typeof x`` checks are ``isinstance``; the ``@ts-expect-error`` lines are runtime checks only
(TypeScript refuses them at compile time upstream). "serve refuses a lifetime \"host\" ..." is an async test: an object
(created by a ``lifetime: "host"`` serve) needs the running event loop for its promises. A ``principalKey`` that reads a
field the host's own principal lacks uses ``.get`` (JavaScript reads ``undefined``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from misaka.chord import (
    ChordError,
    createHost,
    defineRegistry,
    forbidden,
    hostNamed,
    isChordError,
    local,
    method,
    opaque,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.host import Facet
from misaka.utils.js import UNDEFINED, jsonStringify


async def tick(turns: int = 20) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


def quiet(errors: list[Any] | None = None) -> Any:
    sink = [] if errors is None else errors
    return createHost({"self": {"kind": "host"}, "onError": sink.append})


async def until(check: Callable[[], bool]) -> None:
    for _ in range(200):
        if check():
            break
        await tick()
    assert check(), "condition not reached"


Counter = local(
    "t.counter",
    {
        "version": 1,
        "input": Type.Object({"name": Type.String()}),
        "members": {
            "count": Type.Object({"n": Type.Number()}),
            "add": method(Type.Number(), Type.Number()),
        },
    },
)


def serveCounter(host: Any, log: list[str] | None = None, options: dict[str, Any] | None = None) -> None:
    log = [] if log is None else log

    def factory(input: Any, _principal: Any, self: Any) -> Any:
        name = input["name"]
        log.append(f"factory {name}")
        count = self.state("count", Type.Object({"n": Type.Number()}), {"n": 0})

        async def open(_options: Any) -> Any:
            log.append(f"open {name}")
            return {"name": name}

        def close(_value: Any) -> None:
            log.append(f"close {name}")

        conn = self.resource("conn", {"version": 1, "open": open, "close": close})

        def add(delta: float, ctx: Any) -> float:
            def mutate(draft: Any) -> None:
                draft["n"] += delta

            count.change(ctx, mutate)
            return count.value["n"]

        return {"count": count, "conn": conn, "add": add}

    host.serve(Counter, factory, options or {})


# ─── host: activation and lifetime ───


async def test_activates_goes_live_binds_members_late_disposes_after_release():
    host = quiet()
    log: list[str] = []
    serveCounter(host, log)
    ref = host.hold(Counter, {"name": "a"})
    assert ref.status == "loading"
    with pytest.raises(ChordError) as info:
        ref.count.value  # noqa: B018 - the read is the check
    assert isChordError(info.value, "not_ready")
    pending = ref.add(2)
    await ref.ready
    assert ref.status == "live"
    assert await pending == 2
    assert ref.count.value == {"n": 2}
    ref.release()
    assert ref.status == "released"
    with pytest.raises(ChordError) as info:
        await ref.add(1)
    assert isChordError(info.value, "released")
    await tick()
    assert log == ["factory a", "open a", "close a"]


async def test_coalesces_concurrent_gets_and_keeps_the_object_while_held():
    host = quiet()
    log: list[str] = []
    serveCounter(host, log)
    a = host.hold(Counter, {"name": "x"})
    b = host.hold(Counter, {"name": "x"})
    await asyncio.gather(a.ready, b.ready)
    assert log == ["factory x", "open x"]
    a.release()
    await tick()
    assert await b.add(1) == 1
    b.release()
    await tick()
    assert log == ["factory x", "open x", "close x"]


async def test_lingers_after_the_last_release_and_revives_on_a_new_get():
    host = quiet()
    log: list[str] = []
    serveCounter(host, log, {"linger": 30})
    a = host.hold(Counter, {"name": "l"})
    await a.add(5)
    a.release()
    b = host.hold(Counter, {"name": "l"})
    await b.ready
    assert b.count.value == {"n": 5}
    b.release()
    await asyncio.sleep(0.06)
    assert log == ["factory l", "open l", "close l"]


async def test_disposes_an_abandoned_activation_once_it_settles():
    host = quiet()
    log: list[str] = []
    release: list[Callable[[], None]] = [lambda: None]
    Slow = local("t.slow", {"version": 1, "input": Type.Object({})})

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        def open(options: Any) -> Any:
            opened: asyncio.Future[str] = asyncio.get_running_loop().create_future()
            release[0] = lambda: opened.done() or opened.set_result("r")
            options["signal"].addEventListener("abort", lambda: log.append("abort seen"))
            return opened

        self.resource("r", {"version": 1, "open": open, "close": lambda _value: log.append("closed")})
        return {}

    host.serve(Slow, factory)
    ref = host.hold(Slow, {})
    await tick()
    ref.release()
    await tick()
    release[0]()
    await tick()
    await tick()
    assert log == ["abort seen", "closed"]
    again = host.hold(Slow, {})
    await tick()
    release[0]()
    await again.ready
    assert again.status == "live"


async def test_propagates_activation_failures_to_dependents_and_retries_on_the_next_get():
    host = quiet()
    fail = [True]
    Leaf = local("t.leaf", {"version": 1, "input": Type.Object({})})
    Parent = local("t.parent", {"version": 1, "input": Type.Object({})})

    def leaf(_input: Any, _principal: Any, self: Any) -> Any:
        def open(_options: Any) -> int:
            if fail[0]:
                raise RuntimeError("boom")
            return 1

        self.resource("r", {"version": 1, "open": open})
        return {}

    def parent_(_input: Any, _principal: Any, self: Any) -> Any:
        self.get(Leaf, {})
        return {}

    host.serve(Leaf, leaf)
    host.serve(Parent, parent_)
    parent = host.hold(Parent, {})
    # A plain error from a dependency surfaces as `unavailable`, with the original as cause.
    with pytest.raises(ChordError) as info:
        await parent.ready
    assert isChordError(info.value, "unavailable") and "boom" in info.value.message
    assert parent.status == "gone"
    fail[0] = False
    retry = host.hold(Parent, {})
    await retry.ready
    assert retry.status == "live"


async def test_unwinds_declarations_when_a_factory_throws_and_rejects_async_factories():
    host = quiet()
    Leaf = local("t.leaf2", {"version": 1, "input": Type.Object({})})
    Bad = local("t.bad", {"version": 1, "input": Type.Object({"async": Type.Boolean()})})
    leafLive = [0]

    def leaf(_input: Any, _principal: Any, self: Any) -> Any:
        def open(_options: Any) -> None:
            leafLive[0] += 1

        def close(_value: Any) -> None:
            leafLive[0] -= 1

        self.resource("r", {"version": 1, "open": open, "close": close})
        return {}

    def bad(input: Any, _principal: Any, self: Any) -> Any:
        self.get(Leaf, {})
        if input["async"]:

            async def later() -> Any:
                return {}

            return later()
        raise RuntimeError("factory failed")

    host.serve(Leaf, leaf)
    host.serve(Bad, bad)
    with pytest.raises(ChordError, match="factory failed"):
        await host.hold(Bad, {"async": False}).ready
    with pytest.raises(ChordError, match="synchronous"):
        await host.hold(Bad, {"async": True}).ready
    await tick()
    await tick()
    assert leafLive[0] == 0


async def test_rejects_duplicate_declaration_names():
    host = quiet()
    Dup = local("t.dup", {"version": 1, "input": Type.Object({})})

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        self.state("a", Type.Object({}), {})
        self.state("a", Type.Object({}), {})
        return {}

    host.serve(Dup, factory)
    with pytest.raises(ChordError, match="duplicate state name a"):
        await host.hold(Dup, {}).ready


async def test_detects_hold_cycles():
    host = quiet()
    A = local("t.cycle-a", {"version": 1, "input": Type.Object({})})
    B = local("t.cycle-b", {"version": 1, "input": Type.Object({})})

    def a(_input: Any, _principal: Any, self: Any) -> Any:
        self.get(B, {})
        return {}

    def b(_input: Any, _principal: Any, self: Any) -> Any:
        self.get(A, {})
        return {}

    host.serve(A, a)
    host.serve(B, b)
    with pytest.raises(ChordError, match="cycle"):
        await host.hold(A, {}).ready


async def test_closes_resources_in_reverse_order_runs_all_steps_and_aggregates_errors():
    errors: list[Any] = []
    host = quiet(errors)
    log: list[str] = []
    Multi = local("t.multi", {"version": 1, "input": Type.Object({})})

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        a = self.resource("a", {"version": 1, "open": lambda _o: "a", "close": lambda _v: log.append("close a")})

        def closeB(_value: Any) -> None:
            log.append("close b")
            raise RuntimeError("b failed")

        def closeC(_value: Any) -> None:
            log.append("close c")
            raise RuntimeError("c failed")

        self.resource("b", {"version": 1, "deps": [a], "open": lambda _o: "b", "close": closeB})
        self.resource("c", {"version": 1, "open": lambda _o: "c", "close": closeC})
        return {}

    host.serve(Multi, factory)
    ref = host.hold(Multi, {})
    await ref.ready
    ref.release()
    await tick()
    await tick()
    assert log[-1] == "close a"
    assert len(log) == 3
    assert isinstance(errors[0], ExceptionGroup)
    assert len(errors[0].exceptions) == 2


async def test_releases_ctx_get_holds_when_the_call_settles():
    host = quiet()
    log: list[str] = []
    serveCounter(host, log)

    async def run(ctx: Any) -> Any:
        counter = await ctx.get(Counter, {"name": "c"})
        return counter.add(3, ctx)

    value = await host.root(run)
    assert value == 3
    await tick()
    assert log == ["factory c", "open c", "close c"]


# ─── host: declarations and methods ───


async def test_refuses_a_second_declaration_of_one_name_per_kind_kinds_have_their_own_names():
    host = quiet()
    Twice = local("t.twice", {"version": 1, "input": Type.Object({"kind": Type.String()})})

    def factory(input: Any, _principal: Any, self: Any) -> Any:
        kind = input["kind"]
        open = {"version": 1, "open": lambda _o: None}
        self.state("x", Type.Number(), 0)
        self.resource("x", open)
        self.on("x", self.state("y", Type.Number(), 0), lambda *_: None)
        if kind == "state":
            self.state("x", Type.Number(), 1)
        if kind == "resource":
            self.resource("x", open)
        if kind == "listener":
            self.on("x", self.state("z", Type.Number(), 0), lambda *_: None)
        return {}

    host.serve(Twice, factory)
    await host.hold(Twice, {"kind": "none"}).ready
    for kind in ["state", "resource", "listener"]:
        with pytest.raises(ChordError) as info:
            await host.hold(Twice, {"kind": kind}).ready
        error = info.value
        assert isChordError(error, "invalid") and error.reason == "duplicate" and f"{kind} name x" in error.message


async def test_methods_without_arguments_are_called_with_ctx_alone_void_methods_resolve_to_undefined():
    host = quiet()
    Tool = local(
        "t.tool",
        {
            "version": 1,
            "input": Type.Object({}),
            "members": {
                "ping": method(),
                "count": method(None, Type.Number()),
                "note": method(Type.String()),
            },
        },
    )
    seen: list[Any] = []

    def ping(ctx: Any) -> Any:
        seen.append("string" if isinstance(ctx.rootId, str) else type(ctx.rootId).__name__)
        return "ignored"

    def note(text: str, ctx: Any) -> None:
        seen.extend([text, "string" if isinstance(ctx.rootId, str) else type(ctx.rootId).__name__])

    host.serve(Tool, lambda *_: {"ping": ping, "count": lambda _ctx: 3, "note": note})
    tool = host.hold(Tool, {})
    await tool.ready
    assert await tool.ping() is UNDEFINED
    assert await host.root(lambda ctx: tool.count(ctx)) == 3
    assert await tool.note("hi") is UNDEFINED
    assert seen == ["string", "hi", "string"]


async def test_a_listener_on_state_gets_the_current_value_at_start_and_again_after_a_reload_replaced_its_derivation():
    host = quiet()
    Doubled = local(
        "t.doubled",
        {"version": 1, "input": Type.Object({}), "members": {"set": method(Type.Number())}},
    )
    seen: list[float] = []

    def facet(label: str) -> Facet:
        def factory(_input: Any, _principal: Any, self: Any) -> Any:
            n = self.state("n", Type.Number(), 1)
            doubled = self.derive([n], lambda value: value * 2)

            def onDoubled(value: float, _ctx: Any) -> None:
                seen.append(value)
                _ = label

            self.on("doubled", doubled, onDoubled)

            def set_(value: float, ctx: Any) -> None:
                n.replace(ctx, value)

            return {"set": set_}

        return Facet("doubled", lambda api: api.serve(Doubled, factory, {"lifetime": "host"}))

    await host.install(facet("v1"))
    await until(lambda: len(seen) == 1)
    await host.root(lambda ctx: host.hold(Doubled, {}).set(5, ctx))
    await until(lambda: len(seen) == 2)
    assert seen == [2, 10]
    # The reload makes a new derivation: the listener subscribes again and gets its current value once.
    await host.reload([facet("v2")])
    await until(lambda: len(seen) == 3)
    await tick()
    assert seen == [2, 10, 10]


# ─── host: lifetime ───

Store = local(
    "t.store",
    {
        "version": 1,
        "input": Type.Object({"scope": Type.Optional(Type.String({"default": "all"}))}),
        "members": {"seen": opaque("t.seen@1")},
    },
)


async def test_serve_refuses_a_lifetime_host_the_type_cannot_have_at_compile_time_and_at_runtime():
    host = quiet()
    Remote = remote(
        "t.remote-store",
        {"version": 1, "input": Type.Object({}), "host": hostNamed("host"), "variants": {"main": {}}},
    )
    Keyed = local("t.keyed", {"version": 1, "input": Type.Object({"id": Type.String()})})
    # Each refused call is also a type error upstream; the runtime check is the backstop for untyped callers.
    refused: list[tuple[Callable[[], None], str]] = [
        (lambda: host.serve(Remote, lambda *_: {"variant": "main"}, {"lifetime": "host"}), "needs a local type"),
        (lambda: host.serve(Keyed, lambda *_: {}, {"lifetime": "host"}), r"input that accepts \{\}"),
        (lambda: host.serve(Store, lambda *_: {"seen": None}, {"lifetime": "host", "linger": 10}), "does not linger"),
        (lambda: host.serve(Store, lambda *_: {"seen": None}, {"lifetime": "forever"}), "unknown lifetime"),
    ]
    for serve, error in refused:
        with pytest.raises(TypeError, match=error):
            serve()
    # Allowed: a local type whose input has only optional fields, and any type with the default lifetime.
    Optional = local("t.optional", {"version": 1, "input": Type.Object({"id": Type.Optional(Type.String())})})
    host.serve(Optional, lambda *_: {}, {"lifetime": "host"})
    host.serve(Keyed, lambda *_: {}, {"lifetime": "referenced", "linger": 10})
    host.serve(Remote, lambda *_: {"variant": "main"}, {"linger": 10})
    await host.ready()


async def test_every_get_reaches_the_one_object_created_at_serve_for_the_host_principal_another_input_is_invalid():
    host = createHost({"self": {"kind": "host"}, "principalKey": jsonStringify, "onError": lambda _error: None})
    created: list[dict[str, Any]] = []

    def factory(input: Any, principal: Any, _self: Any) -> Any:
        created.append({"input": input, "principal": principal})
        return {"seen": principal}

    host.serve(Store, factory, {"lifetime": "host"})
    await host.ready()
    assert len(created) == 1, "created when served, before anyone asked"
    guest = host.hold(Store, {}, {"principal": {"guest": "g"}})
    defaults = host.hold(Store, {"scope": "all"})
    await asyncio.gather(guest.ready, defaults.ready)
    assert len(created) == 1, "the same object for every principal and the default input"
    assert guest.seen == created[0]["principal"]
    with pytest.raises(ChordError) as info:
        host.hold(Store, {"scope": "other"})
    assert isChordError(info.value, "invalid")
    # References do not change its lifetime: released by everyone else, it stays until the host closes.
    guest.release()
    defaults.release()
    await tick()
    again = host.hold(Store, {})
    await again.ready
    assert len(created) == 1
    await host.close()
    assert again.status == "gone"


# ─── host: principals, variants and disposal ───

Doc = remote(
    "t.doc",
    {
        "version": 1,
        "input": Type.Object({"id": Type.String()}),
        "host": hostNamed("host"),
        "variants": {
            "viewer": {"state": {"view": Type.Object({"text": Type.String()})}},
            "editor": {
                "state": {"view": Type.Object({"text": Type.String()})},
                "methods": {
                    "edit": method(Type.Object({"text": Type.String()}), Type.Object({"ok": Type.Boolean()}))
                },
            },
        },
    },
)


def serveDoc(host: Any, principals: list[Any] | None = None) -> None:
    principals = [] if principals is None else principals

    def factory(_input: Any, principal: Any, self: Any) -> Any:
        principals.append(principal)
        view = self.state("view", Type.Object({"text": Type.String()}), {"text": ""})
        if principal.get("role") != "editor":
            return {"variant": "viewer", "view": view}

        def edit(args: Any, ctx: Any) -> Any:
            view.replace(ctx, {"text": args["text"]})
            return {"ok": "no"} if args["text"] == "bad" else {"ok": True}

        return {"variant": "editor", "view": view, "edit": edit}

    host.serve(Doc, factory)


async def test_keeps_objects_per_principal_key_and_enforces_variants():
    host = createHost(
        {"self": {"kind": "host"}, "principalKey": lambda principal: principal.get("user"), "onError": lambda _e: None}
    )
    principals: list[Any] = []
    serveDoc(host, principals)
    viewer = host.hold(Doc, {"id": "1"}, {"principal": {"user": "v", "role": "viewer"}})
    editor = host.hold(Doc, {"id": "1"}, {"principal": {"user": "e", "role": "editor"}})
    await asyncio.gather(viewer.ready, editor.ready)
    assert len(principals) == 2
    assert viewer.variant == "viewer"
    assert viewer.shape["members"] == ["view"]
    # A viewer has no edit: TypeScript refuses it upstream, and so does the provider at runtime.
    with pytest.raises(ChordError) as info:
        await viewer.edit({"text": "x"})
    assert isChordError(info.value, "not_in_variant")
    assert editor.variant == "editor"
    assert await editor.edit({"text": "x"}) == {"ok": True}
    assert editor.view.value == {"text": "x"}
    assert viewer.view.value == {"text": ""}


async def test_validates_remote_args_and_results_at_the_provider_boundary():
    host = quiet()
    serveDoc(host)
    editor = await host.get(Doc, {"id": "2"}, {"principal": {"role": "editor"}})
    assert editor.variant == "editor"
    with pytest.raises(ChordError) as info:
        await editor.edit({"text": 1})
    assert isChordError(info.value, "invalid") and info.value.reason == "args"
    with pytest.raises(ChordError) as info:
        await editor.edit({"text": "bad"})
    assert isChordError(info.value, "invalid") and info.value.reason == "result"


async def test_forced_disposal_makes_references_gone_and_aborts_in_flight_calls():
    host = quiet()
    seenAbort = [False]
    Slow = local("t.inflight", {"version": 1, "input": Type.Object({}), "members": {"wait": method()}})

    def wait(ctx: Any) -> Any:
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def onAbort() -> None:
            seenAbort[0] = True
            if not done.done():
                done.set_result(None)

        ctx.signal.addEventListener("abort", onAbort)
        return done

    host.serve(Slow, lambda *_: {"wait": wait})
    ref = host.hold(Slow, {}, {"principal": {"user": "u"}})
    await ref.ready
    call = ref.wait()
    await tick()
    await host.invalidatePrincipal(lambda principal: principal.get("user") == "u")
    assert ref.status == "gone"
    await call
    assert seenAbort[0]
    with pytest.raises(ChordError) as info:
        await ref.wait()
    assert isChordError(info.value, "gone")
    fresh = host.hold(Slow, {}, {"principal": {"user": "u"}})
    await fresh.ready
    assert fresh.status == "live"


async def test_state_members_are_late_bound_readables_values_then_snapshot_reset_ops_events():
    host = quiet()
    serveDoc(host)
    editor = host.hold(Doc, {"id": "3"}, {"principal": {"role": "editor"}})
    shapes: list[Any] = []
    editor.onShape(lambda shape: shapes.append(shape["variant"]))
    events: list[Any] = []
    values: list[Any] = []
    editor.view.events(
        lambda event: events.append(["ops", event["ops"]] if event["type"] == "ops" else [event["type"], event["value"]])
    )
    editor.view.subscribe(values.append)
    await editor.ready
    assert editor.variant == "editor"
    await editor.edit({"text": "hi"})
    assert shapes == ["editor"]
    # Listeners added while loading get their snapshot once the reference is live.
    assert events == [
        ["snap", {"text": ""}],
        ["ops", [["r", {"text": "hi"}]]],
    ]
    assert values == [{"text": ""}, {"text": "hi"}]


# ─── host: registries ───

Routes = defineRegistry("t.routes")


async def test_only_objects_of_lifetime_host_register_tables_follow_registration_order():
    host = quiet()
    RoutesA = local("t.routes-a", {"version": 1, "input": Type.Object({})})
    RoutesB = local("t.routes-b", {"version": 1, "input": Type.Object({})})

    def routesA(_input: Any, _principal: Any, self: Any) -> Any:
        self.register(Routes, {"GET /a": "a", "GET /c": "c"})
        return {}

    def routesB(_input: Any, _principal: Any, self: Any) -> Any:
        self.register(Routes, {"GET /b": "b"})
        return {}

    host.serve(RoutesA, routesA, {"lifetime": "host"})
    host.serve(RoutesB, routesB, {"lifetime": "host"})
    tables: list[list[str]] = []
    host.registry(Routes).subscribe(lambda table, _codeVersion: tables.append(list(table.keys())))
    await tick()
    await tick()
    assert list(host.registry(Routes).table().keys()) == ["GET /a", "GET /c", "GET /b"]
    assert host.registry(Routes).get("GET /b") == "b"
    assert tables[-1] == ["GET /a", "GET /c", "GET /b"]

    Lazy = local("t.lazy-register", {"version": 1, "input": Type.Object({})})

    def lazy(_input: Any, _principal: Any, self: Any) -> Any:
        self.register(Routes, {"x": "x"})
        return {}

    host.serve(Lazy, lazy)
    with pytest.raises(ChordError, match='only objects of lifetime "host" may register'):
        await host.hold(Lazy, {}).ready


async def test_rejects_conflicting_registrations():
    errors: list[Any] = []
    host = quiet(errors)
    for name in ["t.conflict-a", "t.conflict-b"]:

        def factory(_input: Any, _principal: Any, self: Any, name: str = name) -> Any:
            self.register(Routes, {"same": name})
            return {}

        host.serve(local(name, {"version": 1, "input": Type.Object({})}), factory, {"lifetime": "host"})
    await tick()
    await tick()
    assert any("registry_conflict" in str(error) for error in errors)


# ─── host: composition ───

Source = local(
    "t.source",
    {"version": 1, "input": Type.Object({}), "members": {"n": Type.Number(), "set": method(Type.Number())}},
)
Mirror = local(
    "t.mirror",
    {"version": 1, "input": Type.Object({}), "members": {"copy": Type.Number(), "doubled": Type.Number()}},
)


def orZero(value: Any) -> Any:
    """``value ?? 0``."""
    return 0 if value is None or value is UNDEFINED else value


async def test_reference_state_members_are_sources_for_listeners_and_derivations():
    host = quiet()

    def source(_input: Any, _principal: Any, self: Any) -> Any:
        n = self.state("n", Type.Number(), 1)

        def set_(value: float, ctx: Any) -> None:
            n.replace(ctx, value)

        return {"n": n, "set": set_}

    def mirror_(_input: Any, _principal: Any, self: Any) -> Any:
        sourceRef = self.get(Source, {})
        copy = self.state("copy", Type.Number(), 0)
        self.on("follow", sourceRef.n, lambda value, ctx: copy.replace(ctx, orZero(value)))
        doubled = self.derive([sourceRef.n], lambda n: orZero(n) * 2)
        return {"copy": copy, "doubled": doubled}

    host.serve(Source, source)
    host.serve(Mirror, mirror_)
    mirror = host.hold(Mirror, {})
    await mirror.ready
    await tick()
    assert mirror.copy.value == 1
    assert mirror.doubled.value == 2
    await host.hold(Source, {}).set(5)
    await tick()
    assert mirror.copy.value == 5
    assert mirror.doubled.value == 10


async def test_ctx_get_acts_as_the_root_principal_at_ingress_and_as_the_host_inside_objects():
    host = createHost({"self": {"kind": "host"}, "principalKey": jsonStringify, "onError": lambda _e: None})
    seen: list[Any] = []
    Who = local("t.who", {"version": 1, "input": Type.Object({}), "members": {"ping": method()}})
    Front = local("t.front", {"version": 1, "input": Type.Object({}), "members": {"relay": method()}})

    def who(_input: Any, principal: Any, _self: Any) -> Any:
        seen.append(principal)
        return {"ping": lambda _ctx: None}

    def front(_input: Any, principal: Any, _self: Any) -> Any:
        seen.append(principal)

        async def relay(ctx: Any) -> Any:
            return (await ctx.get(Who, {})).ping(ctx)

        return {"relay": relay}

    host.serve(Who, who)
    host.serve(Front, front)

    async def run(ctx: Any) -> Any:
        return (await ctx.get(Front, {})).relay(ctx)

    await host.root(run, {"principal": {"user": "guest"}})
    assert seen == [{"user": "guest"}, {"kind": "host"}]


async def test_calling_a_reference_inside_a_factory_throws_not_ready():
    host = quiet()
    host.serve(Source, lambda _i, _p, self: {"n": self.state("n", Type.Number(), 0), "set": lambda *_: None})
    Eager = local("t.eager-call", {"version": 1, "input": Type.Object({})})

    def eager(_input: Any, _principal: Any, self: Any) -> Any:
        source = self.get(Source, {})
        source.set(1)
        return {}

    host.serve(Eager, eager)
    with pytest.raises(ChordError) as info:
        await host.hold(Eager, {}).ready
    assert isChordError(info.value, "not_ready")


async def test_a_chord_error_from_a_factory_keeps_its_code_through_dependents():
    host = quiet()
    Secret = local("t.secret", {"version": 1, "input": Type.Object({})})
    Page = local("t.page", {"version": 1, "input": Type.Object({})})

    def secret(*_: Any) -> Any:
        raise forbidden("not for you")

    def page(_input: Any, _principal: Any, self: Any) -> Any:
        self.get(Secret, {})
        return {}

    host.serve(Secret, secret)
    host.serve(Page, page)
    with pytest.raises(ChordError) as info:
        await host.hold(Page, {}).ready
    assert isChordError(info.value, "forbidden")
