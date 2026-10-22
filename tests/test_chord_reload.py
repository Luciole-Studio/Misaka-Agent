"""pi chord's ``test/reload.test.ts``.

MISAKA adaptations: facets are ``Facet`` objects; handlers take the arguments the runtime passes (``(args, ctx)``, or
``(ctx)`` for methods without arguments), as Python functions cannot ignore extra arguments; ``tick`` (upstream's
``setImmediate``) yields to the event loop a few times, which drains what upstream's microtasks would; vitest's fake
timers become real time with the same small deadlines, except where a test asserts the deadline itself (``FakeTimers``
in place of the object's ``setTimeout``).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

import pytest

from misaka.chord import createHost, defineRegistry, hostNamed, local, method, remote
from misaka.chord import object as objectModule
from misaka.chord._typebox import Type
from misaka.chord.errors import ChordError, isChordError
from misaka.chord.host import Facet, HostImpl
from tests.fake_timers import FakeTimers


async def tick() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


def quiet() -> HostImpl:
    return createHost({"self": {"kind": "host"}, "onError": lambda _error: None})


class Latch:
    def __init__(self) -> None:
        self.promise: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def open(self) -> None:
        if not self.promise.done():
            self.promise.set_result(None)


def abortedOf(ctx: Any) -> asyncio.Future[None]:
    """``new Promise((resolve) => ctx.signal.addEventListener("abort", () => resolve()))``."""
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    ctx.signal.addEventListener("abort", lambda: future.done() or future.set_result(None))
    return future


Greeter = local(
    "t.greeter",
    {
        "version": 1,
        "input": Type.Object({"id": Type.Optional(Type.String({"default": "a"}))}),
        "members": {"greet": method(Type.String(), Type.String()), "count": Type.Unknown()},
    },
)

CountV1 = Type.Object({"n": Type.Number()})


def defaultBump(draft: Any) -> None:
    draft["n"] = draft.get("n", 0) + 1


def greeterFacet(
    greeting: str,
    schema: Any = None,
    initial: Any = None,
    bump: Callable[[Any], None] | None = None,
    options: Any = None,
    state: Any = None,
    extra: Callable[[Any, Any], None] | None = None,
) -> Facet:
    def setup(api: Any) -> None:
        def factory(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            if extra is not None:
                extra(self, input)
            count = self.state("count", schema or CountV1, initial if initial is not None else {"n": 0}, state)

            def greet(name: str, ctx: Any) -> str:
                count.change(ctx, lambda draft: (bump or defaultBump)(draft))
                return f"{greeting} {name}"

            return {"count": count, "greet": greet}

        api.serve(Greeter, factory, options)

    return Facet("greeter", setup)


async def until(check: Callable[[], Any], attempts: int = 200) -> None:
    for _ in range(attempts):
        if await check():
            return
        await tick()
    raise RuntimeError("condition not reached")


async def test_compatible_reload_no_gate_pinned_roots_keep_old_code_state_preserved_previous_retires():
    host = quiet()
    host.install(greeterFacet("hello"))
    ref = host.hold(Greeter, {})
    assert await ref.greet("a") == "hello a"
    go = Latch()

    async def pinnedRoot(ctx: Any) -> list[str]:
        first = await ref.greet("p", ctx)
        await go.promise
        return [first, await ref.greet("p", ctx)]

    pinned = host.root(pinnedRoot)
    await tick()
    shapes: list[str] = []
    ref.onShape(lambda shape: shapes.append(shape["contract"]))
    report = await host.reload([greeterFacet("hi")])
    assert report["gated"] == []
    assert len(report["prepared"]) == 1
    assert await ref.greet("b") == "hi b"
    assert len(shapes) == 1, "one shape push at the commit"
    assert host.previousCodeVersion == report["previous"]
    go.open()
    assert await pinned == ["hello p", "hello p"]
    assert host.previousCodeVersion is None
    assert ref.count.value == {"n": 4}


async def test_prepare_runs_in_recording_mode_writes_throw_and_a_failing_factory_leaves_everything_unchanged():
    host = quiet()
    host.install(greeterFacet("hello"))
    a = host.hold(Greeter, {"id": "a"})
    b = host.hold(Greeter, {"id": "b"})
    await asyncio.gather(a.ready, b.ready)
    captured: list[Any] = []
    await host.root(lambda ctx: captured.append(ctx))

    def write(self: Any, _input: Any) -> None:
        probe = self.state("probe", Type.Object({}), {})
        probe.replace(captured[0], {})

    with pytest.raises(ChordError, match="read_only"):
        await host.reload([greeterFacet("hi", extra=write)])

    log: list[str] = []

    def fail(self: Any, input: Any) -> None:
        self.resource(
            "extra",
            {
                "version": 1,
                "open": lambda _options: log.append(f"open {input['id']}"),
                "close": lambda _value: log.append(f"close {input['id']}"),
            },
        )
        if input["id"] == "b":
            raise RuntimeError("b fails")

    codeVersion = host.codeVersion
    with pytest.raises(ChordError) as info:
        await host.reload([greeterFacet("hi", extra=fail)])
    # A plain error from the new factory surfaces as `unavailable`; the message says the reload aborted.
    assert isChordError(info.value, "unavailable")
    assert "reload aborted" in str(info.value) and "b fails" in str(info.value)
    assert host.codeVersion == codeVersion
    assert log == ["open a", "close a"]
    assert await a.greet("x") == "hello x"


async def test_refuses_a_changed_cell_schema_without_a_version_bump():
    host = quiet()
    host.install(greeterFacet("hello"))
    await host.hold(Greeter, {}).ready
    changed = greeterFacet("hi", schema=Type.Object({"total": Type.Number()}), initial={"total": 0})
    with pytest.raises(ChordError) as info:
        await host.reload([changed])
    assert isChordError(info.value, "invalid") and info.value.reason == "state"
    assert "without a version bump" in str(info.value)


async def test_state_bump_gate_migrate_old_from_version_into_the_new_schema_reset_to_watchers_old_roots_upgrade_conflict():
    host = quiet()
    host.install(greeterFacet("hello"))
    ref = host.hold(Greeter, {})
    await ref.greet("a")
    await ref.greet("a")
    events: list[Any] = []
    ref.count.events(events.append)
    go = Latch()

    # Started before the reload, never touched the object: rejected after the commit.
    async def lateRoot(ctx: Any) -> Any:
        await go.promise
        return await ref.greet("late", ctx)

    late = host.root(lateRoot)
    froms: list[int] = []

    def bump(draft: Any) -> None:
        draft["total"] = draft.get("total", 0) + 1

    def migrate(old: Any, from_: int) -> Any:
        froms.append(from_)
        return {"total": old["n"]}

    v2 = greeterFacet(
        "hi",
        schema=Type.Object({"total": Type.Number()}),
        initial={"total": 0},
        bump=bump,
        state={"version": 2, "migrate": migrate},
    )
    report = await host.reload([v2])
    assert len(report["gated"]) == 1
    assert froms == [1]
    assert events[-1] == {"type": "reset", "value": {"total": 2}}
    assert await ref.greet("b") == "hi b"
    assert ref.count.value == {"total": 3}
    go.open()
    with pytest.raises(ChordError) as info:
        await late
    assert isChordError(info.value, "upgrade_conflict") and info.value.retryable


async def test_versions_are_per_cell_a_raised_one_migrates_only_that_cell_a_lowered_one_is_refused():
    host = quiet()
    Pair = local(
        "t.pair",
        {
            "version": 1,
            "input": Type.Object({}),
            "members": {"a": Type.Unknown(), "b": Type.Unknown(), "bump": method()},
        },
    )
    migrated: list[str] = []

    def facet(version: int) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                a = self.state("a", Type.Number(), 1)

                def migrate(old: Any, from_: int) -> Any:
                    migrated.append(f"b {from_}->{version}")
                    return old * 10

                b = self.state("b", Type.Number(), 1, {"version": version, "migrate": migrate})

                def bump(ctx: Any) -> None:
                    a.replace(ctx, a.value + 1)

                return {"a": a, "b": b, "bump": bump}

            api.serve(Pair, factory)

        return Facet("pair", setup)

    host.install(facet(1))
    pair = host.hold(Pair, {})
    await pair.ready
    await pair.bump()
    report = await host.reload([facet(2)])
    assert len(report["gated"]) == 1
    assert migrated == ["b 1->2"]
    assert [pair.a.value, pair.b.value] == [2, 10], "a kept as it was, b migrated"
    with pytest.raises(ChordError, match="version of state b lowered from 2 to 1"):
        await host.reload([facet(1)])


async def _probeConflict(ref: Any) -> bool:
    try:
        await ref.greet("probe")
        return False
    except ChordError as error:
        return isChordError(error, "upgrade_conflict")


async def test_gate_late_old_roots_get_upgrade_conflict_grandfathered_roots_re_enter_then_commit():
    host = quiet()
    host.install(greeterFacet("hello"))
    ref = host.hold(Greeter, {})
    await ref.ready
    go = Latch()

    async def grandfatheredRoot(ctx: Any) -> Any:
        await ref.greet("g", ctx)
        await go.promise
        return await ref.greet("g", ctx)

    grandfathered = host.root(grandfatheredRoot)
    await tick()
    reload = host.reload([greeterFacet("hi", state={"version": 2})])
    await until(lambda: _probeConflict(ref))
    go.open()
    assert await grandfathered == "hello g"
    report = await reload
    assert len(report["gated"]) == 1
    assert await ref.greet("n") == "hi n"


async def test_gate_deadline_reload_aborts_reports_blocking_roots_nothing_changes():
    host = quiet()
    log: list[str] = []

    def facet(greeting: str, version: int) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                async def task(ctx: Any) -> None:
                    log.append(f"start {greeting}")
                    await abortedOf(ctx)
                    log.append(f"stop {greeting}")

                self.task(task)
                count = self.state("count", Type.Number(), 0, {"version": version})
                return {"count": count, "greet": lambda name, _ctx: f"{greeting} {name}"}

            api.serve(Greeter, factory)

        return Facet("greeter", setup)

    host.install(facet("hello", 1))
    ref = host.hold(Greeter, {})
    await ref.ready
    go = Latch()

    async def blockerRoot(ctx: Any) -> None:
        await ref.greet("b", ctx)
        await go.promise

    blocker = host.root(blockerRoot)
    await tick()
    with pytest.raises(ChordError) as info:
        await host.reload([facet("hi", 2)], {"gateDeadline": 30})
    assert isChordError(info.value, "aborted") and info.value.reason == "gate_deadline"
    assert re.search(r"blocking roots: host#\w+:\d+", str(info.value))
    assert await ref.greet("x") == "hello x"
    await tick()
    assert log == ["start hello", "stop hello", "start hello"]
    go.open()
    await blocker


async def test_exclusive_resources_are_replaced_at_the_gate_non_exclusive_ones_at_prepare_and_closed_at_retirement():
    host = quiet()
    log: list[str] = []

    def facet(rev: int) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                def openLock(_options: Any) -> int:
                    log.append(f"open lock {rev}")
                    return rev

                lock = self.resource(
                    "lock",
                    {
                        "version": rev,
                        "exclusive": True,
                        "open": openLock,
                        "close": lambda _value: log.append(f"close lock {rev}"),
                    },
                )
                self.resource(
                    "cache",
                    {
                        "version": rev,
                        "open": lambda _options: log.append(f"open cache {rev}"),
                        "close": lambda _value: log.append(f"close cache {rev}"),
                    },
                )
                count = self.state("count", Type.Number(), 0)
                return {"count": count, "greet": lambda name, _ctx: f"{lock.value} {name}"}

            api.serve(Greeter, factory)

        return Facet("greeter", setup)

    host.install(facet(1))
    ref = host.hold(Greeter, {})
    assert await ref.greet("x") == "1 x"
    log.clear()
    report = await host.reload([facet(2)])
    assert len(report["gated"]) == 1
    assert await ref.greet("x") == "2 x"
    await tick()
    assert log == ["open cache 2", "close lock 1", "open lock 2", "close cache 1"]


async def test_two_version_rule_the_next_reload_retires_the_previous_code_version_its_roots_get_superseded():
    host = quiet()
    host.install(greeterFacet("v1"))
    ref = host.hold(Greeter, {})
    await ref.ready
    first = host.codeVersion
    go = Latch()

    async def pinnedRoot(ctx: Any) -> Any:
        await ref.greet("p", ctx)
        await go.promise
        return await ref.greet("p", ctx)

    pinned = host.root(pinnedRoot)
    await tick()
    await host.reload([greeterFacet("v2")])
    assert host.previousCodeVersion == first
    await host.reload([greeterFacet("v3")], {"retireDeadline": 20})
    go.open()
    with pytest.raises(ChordError) as info:
        await pinned
    assert isChordError(info.value, "superseded") and info.value.retryable
    with pytest.raises(ChordError) as info:
        await host.root(lambda _ctx: 1, {"codeVersion": first})
    assert isChordError(info.value, "superseded")
    assert await ref.greet("n") == "v3 n"


async def test_forced_retirement_aborts_the_leftover_roots_before_it_closes_the_resources_only_their_code_used():
    host = quiet()
    log: list[str] = []
    hold = Latch()

    def facet(version: int) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                conn = self.resource(
                    "conn",
                    {
                        "version": version,
                        "open": lambda _options: {"version": version},
                        "close": lambda _value: log.append(f"close {version}"),
                    },
                )
                count = self.state("count", CountV1, {"n": 0})

                async def greet(name: str, ctx: Any) -> str:
                    if name == "hold":
                        await hold.promise
                        return f"held on {conn.value['version']}, aborted {'true' if ctx.signal.aborted else 'false'}"
                    if name != "wait":
                        return name
                    await abortedOf(ctx)
                    # Still open: the abort comes first, then the grace, then the close.
                    log.append(f"aborted, conn {conn.value['version']}")
                    return name

                return {"count": count, "greet": greet}

            api.serve(Greeter, factory)

        return Facet("greeter", setup)

    host.install(facet(1))
    ref = host.hold(Greeter, {})
    await ref.ready
    old = host.root(lambda ctx: ref.greet("wait", ctx))
    await tick()
    await host.reload([facet(2)])
    # A root of the code version that becomes the previous one: forced retirement of the older one leaves it alone.
    current = host.root(lambda ctx: ref.greet("hold", ctx))
    await tick()
    await host.reload([facet(3)], {"retireDeadline": 20})
    assert await old == "wait"
    for _ in range(50):
        if "close 1" in log:
            break
        await tick()
    assert log == ["aborted, conn 1", "close 1"]
    hold.open()
    assert await current == "held on 2, aborted false"
    assert await ref.greet("x") == "x"


async def test_a_kept_resource_closes_with_the_newest_declarations_close():
    host = quiet()
    log: list[str] = []

    def facet(label: str) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                self.resource(
                    "conn",
                    {"version": 1, "open": lambda _options: {}, "close": lambda _value: log.append(f"close by {label}")},
                )
                count = self.state("count", CountV1, {"n": 0})
                return {"count": count, "greet": lambda name, _ctx: f"{label} {name}"}

            api.serve(Greeter, factory)

        return Facet("greeter", setup)

    host.install(facet("a"))
    ref = host.hold(Greeter, {})
    await ref.ready
    await host.reload([facet("b")])
    await host.reload([facet("c")])
    assert await ref.greet("x") == "c x"
    assert log == [], "kept, not reopened"
    ref.release()
    for _ in range(10):
        if log:
            break
        await tick()
    assert log == ["close by c"]


async def test_a_task_that_ignores_its_abort_delays_its_replacement_at_most_task_stop_deadline_reported(monkeypatch):
    # vitest's fake setTimeout/clearTimeout: the timers the object starts for its delayed task start.
    timers = FakeTimers()
    monkeypatch.setattr(objectModule, "setTimeout", timers.setTimeout)
    monkeypatch.setattr(objectModule, "clearTimeout", timers.clearTimeout)
    errors: list[Any] = []
    host = createHost({"self": {"kind": "host"}, "onError": errors.append, "taskStopDeadline": 30})
    log: list[str] = []
    stuck = Latch()

    def facet(label: str) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                async def task(_ctx: Any) -> None:
                    log.append(f"start {label}")
                    if label == "a":
                        await stuck.promise  # ignores ctx.signal

                self.task(task)
                count = self.state("count", CountV1, {"n": 0})
                return {"count": count, "greet": lambda name, _ctx: name}

            api.serve(Greeter, factory)

        return Facet("greeter", setup)

    host.install(facet("a"))
    ref = host.hold(Greeter, {})
    await ref.ready
    await host.reload([facet("b")])
    await timers.advanceAsync(29)
    await tick()
    assert log == ["start a"], "waits for the old run"
    assert len(errors) == 0
    await timers.advanceAsync(1)
    await tick()
    assert log == ["start a", "start b"]
    assert any(isChordError(error, "aborted") and "still running after 30 ms" in str(error) for error in errors)
    stuck.open()


async def test_a_facets_signal_aborts_when_its_run_is_replaced_or_removed_when_its_reload_aborts_and_when_the_host_closes():
    host = quiet()
    signals: dict[str, Any] = {}

    def facet(label: str, fail: bool = False) -> Facet:
        def setup(api: Any) -> None:
            signals[label] = api.signal

            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                if fail:
                    raise RuntimeError("broken")
                count = self.state("count", CountV1, {"n": 0})
                return {"count": count, "greet": lambda name, _ctx: name}

            api.serve(Greeter, factory)

        return Facet("greeter", setup)

    other = Facet("other", lambda api: signals.__setitem__("other", api.signal))
    host.install(facet("a"))
    host.install(other)
    await host.hold(Greeter, {}).ready
    with pytest.raises(ChordError):
        await host.reload([facet("b", True)])
    assert signals["b"].aborted is True, "the aborted reload's run"
    assert signals["a"].aborted is False, "still installed"
    await host.reload([facet("c")])
    assert signals["a"].aborted is True, "replaced"
    assert signals["c"].aborted is False
    await host.reload([], {"remove": ["other"]})
    assert signals["other"].aborted is True, "removed"
    await host.close()
    assert signals["c"].aborted is True, "host closed"


async def test_checks_contracts_at_reload_forgotten_bumps_live_consumers_accepts_adapters():
    DocV1 = remote(
        "t.rdoc",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("host"),
            "variants": {"main": {"methods": {"read": method(None, Type.Object({"text": Type.String()}))}}},
        },
    )
    DocBroken = remote(
        "t.rdoc",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("host"),
            "variants": {"main": {"methods": {"read": method(None, Type.Object({"text": Type.Number()}))}}},
        },
    )
    DocV2 = remote(
        "t.rdoc",
        {
            "version": 2,
            "input": Type.Object({}),
            "host": hostNamed("host"),
            "variants": {"main": {"methods": {"fetch": method(None, Type.Object({"body": Type.String()}))}}},
        },
    )
    host = quiet()
    host.install(
        Facet(
            "doc",
            lambda api: api.serve(DocV1, lambda *_: {"variant": "main", "read": lambda _ctx: {"text": "v1"}}),
        )
    )
    consumer = host.hold(DocV1, {})
    assert await consumer.read() == {"text": "v1"}

    broken = Facet(
        "doc", lambda api: api.serve(DocBroken, lambda *_: {"variant": "main", "read": lambda _ctx: {"text": 1}})
    )
    with pytest.raises(ChordError, match="without a version bump"):
        await host.reload([broken])

    def fetch(_ctx: Any = None) -> dict[str, str]:
        return {"body": "v2"}

    v2 = Facet("doc", lambda api: api.serve(DocV2, lambda *_: {"variant": "main", "fetch": fetch}))
    with pytest.raises(ChordError) as info:
        await host.reload([v2])
    assert isChordError(info.value, "contract_mismatch")

    def adapter(current: Any) -> dict[str, Any]:
        return {"variant": "main", "read": lambda _ctx=None: {"text": f"{current['fetch']()['body']} via adapter"}}

    adapted = Facet(
        "doc",
        lambda api: api.serve(DocV2, lambda *_: {"variant": "main", "fetch": fetch}, {"accepts": {1: adapter}}),
    )
    await host.reload([adapted])
    assert await consumer.read() == {"text": "v2 via adapter"}
    modern = host.hold(DocV2, {})
    assert await modern.fetch() == {"body": "v2"}


async def test_rebinds_listeners_to_the_new_code_and_restarts_tasks():
    host = quiet()
    listeners: dict[Callable[[int], None], None] = {}

    class Events:
        def subscribe(self, fn: Callable[[int], None]) -> Callable[[], None]:
            listeners[fn] = None
            return lambda: listeners.pop(fn, None)

    events = Events()

    def emit(value: int) -> None:
        for fn in list(listeners):
            fn(value)

    log: list[str] = []
    Sum = local(
        "t.sum",
        {"version": 1, "input": Type.Object({}), "members": {"sum": Type.Object({"v": Type.Number()})}},
    )

    def facet(factor: int) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                sum = self.state("sum", Type.Object({"v": Type.Number()}), {"v": 0})

                def add(value: int, ctx: Any) -> None:
                    def mutate(draft: Any) -> None:
                        draft["v"] += value * factor

                    sum.change(ctx, mutate)

                self.on("add", events, lambda source: source, add)

                async def task(ctx: Any) -> None:
                    log.append(f"start {factor}")
                    await abortedOf(ctx)
                    log.append(f"stop {factor}")

                self.task(task)
                return {"sum": sum}

            api.serve(Sum, factory)

        return Facet("sum", setup)

    host.install(facet(1))
    ref = host.hold(Sum, {})
    await ref.ready
    emit(1)
    await tick()
    await host.reload([facet(10)])
    assert len(listeners) == 1
    emit(1)
    await tick()
    assert ref.sum.value == {"v": 11}
    # The replacement starts once the aborted run finished.
    assert log == ["start 1", "stop 1", "start 10"]


async def test_keeps_registry_tables_per_code_version():
    host = quiet()
    Routes = defineRegistry("t.reload-routes")
    Router = local("t.router", {"version": 1, "input": Type.Object({})})

    def facet(value: str) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                self.register(Routes, {"GET /": value})
                return {}

            api.serve(Router, factory, {"lifetime": "host"})

        return Facet("router", setup)

    host.install(facet("one"))
    await tick()
    await tick()
    seen: list[list[Any]] = []
    host.registry(Routes).subscribe(lambda table, codeVersion: seen.append([table.get("GET /"), codeVersion]))
    go = Latch()

    async def pinnedRoot(ctx: Any) -> Any:
        await go.promise
        return host.registry(Routes).get("GET /", ctx)

    pinned = host.root(pinnedRoot)
    report = await host.reload([facet("two")])
    assert host.registry(Routes).get("GET /") == "two"
    go.open()
    assert await pinned == "one"
    assert seen == [["one", report["previous"]], ["two", report["codeVersion"]]]


async def test_a_reload_can_add_a_type_together_with_a_consumer_that_holds_it():
    host = quiet()
    host.install(greeterFacet("hello"))
    ref = host.hold(Greeter, {})
    await ref.ready
    Clock = local(
        "t.clock",
        {"version": 1, "input": Type.Object({}), "members": {"now": method(None, Type.Number())}},
    )
    clockFacet = Facet("clock", lambda api: api.serve(Clock, lambda *_: {"now": lambda _ctx: 42}))

    def consumer(fail: bool) -> Facet:
        def setup(api: Any) -> None:
            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                clock = self.get(Clock, {})
                count = self.state("count", CountV1, {"n": 0})
                if fail:
                    raise RuntimeError("consumer fails")

                async def greet(name: str, ctx: Any) -> str:
                    return f"{await clock.now(ctx)} {name}"

                return {"count": count, "greet": greet}

            api.serve(Greeter, factory)

        return Facet("greeter", setup)

    codeVersion = host.codeVersion
    with pytest.raises(ChordError, match="consumer fails"):
        await host.reload([clockFacet, consumer(True)])
    assert host.codeVersion == codeVersion
    assert await ref.greet("x") == "hello x"
    await host.reload([clockFacet, consumer(False)])
    assert await ref.greet("x") == "42 x"


async def test_the_gate_rejects_new_objects_of_a_changed_type_after_commit_they_get_the_new_code():
    host = quiet()
    host.install(greeterFacet("hello"))
    ref = host.hold(Greeter, {})
    await ref.ready
    go = Latch()

    async def grandfatheredRoot(ctx: Any) -> None:
        await ref.greet("g", ctx)
        await go.promise

    grandfathered = host.root(grandfatheredRoot)
    later = Latch()

    async def pinnedRoot(ctx: Any) -> Any:
        await later.promise
        return await ref.greet("old", ctx)

    pinned = host.root(pinnedRoot)
    await tick()
    reload = host.reload([greeterFacet("hi", state={"version": 2})])
    await until(lambda: _probeConflict(ref))
    with pytest.raises(ChordError) as info:
        host.hold(Greeter, {"id": "late"})
    assert isChordError(info.value, "upgrade_conflict") and info.value.retryable
    go.open()
    await grandfathered
    await reload
    assert await host.hold(Greeter, {"id": "late"}).greet("l") == "hi l"
    # The previous code version is still retained (pinned root), but the gated object dropped its old implementation.
    assert host.previousCodeVersion is not None
    later.open()
    with pytest.raises(ChordError) as info:
        await pinned
    assert isChordError(info.value, "upgrade_conflict")
