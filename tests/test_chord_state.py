"""pi chord's ``test/state.test.ts``: state cells.

MISAKA adaptations: ``setImmediate`` (a turn after the pending promise jobs) is ``tick()``, a few event-loop turns; a
listener's event source is a small emitter class; a task waits for its abort with ``ctx.signal.wait()``; a mutating
``change`` callback returns ``None`` (MISAKA fork of ``Cell.change``), so the "classic slip" of returning ``push``'s
length is a lambda returning it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from misaka.chord import ChordError, createHost, isChordError, local, method
from misaka.chord._typebox import Type
from misaka.chord.delta import applyImmutable


async def tick(turns: int = 20) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


class Emitter:
    def __init__(self) -> None:
        self.listeners: dict[Any, None] = {}

    def emit(self, event: Any) -> None:
        for fn in list(self.listeners):
            fn(event)

    def subscribe(self, fn: Any) -> Any:
        self.listeners[fn] = None
        return lambda: self.listeners.pop(fn, None)

    @property
    def size(self) -> int:
        return len(self.listeners)


Item = Type.Object({"id": Type.Number(), "label": Type.String()})
Items = Type.Object({"items": Type.Array(Item), "count": Type.Number()})

List = local(
    "t.list",
    {
        "version": 1,
        "input": Type.Object({}),
        "members": {
            "list": Items,
            "labels": Type.Array(Type.String()),
            "push": method(Item),
            "set": method(Items),
        },
    },
)


def serveList(host: Any) -> None:
    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        listState = self.state("list", Items, {"items": [], "count": 0})
        labels = self.derive([listState], lambda list_: [item["label"] for item in list_["items"]])

        def push(item: Any, ctx: Any) -> None:
            def mutate(draft: Any) -> None:
                draft["items"].push(item)
                draft["count"] += 1

            listState.change(ctx, mutate)

        def set_(value: Any, ctx: Any) -> None:
            listState.replace(ctx, value)

        return {"list": listState, "labels": labels, "push": push, "set": set_}

    host.serve(List, factory)


async def test_publishes_snapshots_then_exact_ops_derivations_follow():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _: None})
    serveList(host)
    ref = host.hold(List, {})
    events: list[Any] = []
    labels: list[Any] = []
    ref.list.events(events.append)
    ref.labels.events(labels.append)
    await ref.push({"id": 1, "label": "a"})
    # Subscribed while loading: the snapshot once live, then exact ops.
    assert events[0] == {"type": "snap", "value": {"items": [], "count": 0}}
    ops = sorted(json.dumps(op, separators=(",", ":")) for op in events[1]["ops"]) if events[1]["type"] == "ops" else []
    assert ops == ['["p",["items"],0,0,[{"id":1,"label":"a"}]]', '["s",["count"],1]']
    assert events[1]["value"] == {"items": [{"id": 1, "label": "a"}], "count": 1}
    assert ref.labels.value == ["a"]
    assert [event["type"] for event in labels] == ["snap", "ops"]
    await ref.set({"items": [], "count": 0})
    assert ref.labels.value == []


async def test_supports_primitive_cells_and_value_returning_changes():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _: None})
    Counter = local(
        "t.prim",
        {"version": 1, "input": Type.Object({}), "members": {"n": Type.Number(), "inc": method(None, Type.Number())}},
    )

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        nState = self.state("n", Type.Number(), 0)

        def inc(ctx: Any) -> Any:
            nState.change(ctx, lambda n: n + 1)
            return nState.value

        return {"n": nState, "inc": inc}

    host.serve(Counter, factory)
    ref = host.hold(Counter, {})
    values: list[Any] = []
    await ref.ready
    events: list[Any] = []
    ref.n.events(events.append)
    ref.n.subscribe(values.append)
    assert await ref.inc() == 1
    assert await ref.inc() == 2
    assert values == [0, 1, 2]
    assert events == [
        {"type": "snap", "value": 0},
        {"type": "ops", "ops": [["r", 1]], "value": 1},
        {"type": "ops", "ops": [["r", 2]], "value": 2},
    ]


async def test_rejects_a_returned_value_that_does_not_fit_the_schema_even_outside_dev_mode():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _: None})
    Box = local("t.box", {"version": 1, "input": Type.Object({}), "members": {"put": method()}})

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        list_ = self.state("list", Type.Array(Type.Number()), [])

        def put(ctx: Any) -> None:
            # The classic slip: an expression-bodied lambda returns push()'s length.
            list_.change(ctx, lambda draft: draft.push(1))

        return {"put": put}

    host.serve(Box, factory)
    with pytest.raises(ChordError) as error:
        await host.hold(Box, {}).put()
    assert isChordError(error.value, "invalid")


async def test_validates_initial_values_always_and_writes_only_in_dev_mode():
    Bad = local("t.bad-initial", {"version": 1, "input": Type.Object({})})
    host = createHost({"self": {"kind": "host"}, "onError": lambda _: None})

    def bad(_input: Any, _principal: Any, self: Any) -> Any:
        self.state("x", Type.Object({"n": Type.Number()}), {"n": "no"})
        return {}

    host.serve(Bad, bad)
    with pytest.raises(ChordError, match="initial value of state x"):
        await host.hold(Bad, {}).ready

    lax = createHost({"self": {"kind": "host"}, "onError": lambda _: None})
    serveList(lax)
    await lax.hold(List, {}).set({"items": [], "count": "x"})

    dev = createHost({"self": {"kind": "host"}, "dev": True, "onError": lambda _: None})
    serveList(dev)
    ref = dev.hold(List, {})
    with pytest.raises(ChordError) as error:
        await ref.set({"items": [], "count": "x"})
    assert isChordError(error.value, "invalid") and error.value.reason == "state"
    assert ref.list.value == {"items": [], "count": 0}


async def test_rejects_writes_from_inside_a_factory_and_placeholder_use_before_activation():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _: None})
    Writer = local("t.writer", {"version": 1, "input": Type.Object({})})
    captured: list[Any] = []
    await host.root(captured.append)

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        x = self.state("x", Type.Object({}), {})
        r = self.resource("r", {"version": 1, "open": lambda _options: 1})
        with pytest.raises(ChordError) as error:
            _ = r.value
        assert isChordError(error.value, "not_ready")
        x.replace(captured[0], {})
        return {}

    host.serve(Writer, factory)
    with pytest.raises(ChordError, match="read_only"):
        await host.hold(Writer, {}).ready


async def test_listeners_run_each_event_in_a_fresh_root_tasks_get_an_abortable_ctx():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _: None})
    events = Emitter()
    Watcher = local(
        "t.watcher",
        {"version": 1, "input": Type.Object({}), "members": {"seen": Type.Object({"sum": Type.Number()})}},
    )
    roots: set[str] = set()
    taskAborted = [False]

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        seen = self.state("seen", Type.Object({"sum": Type.Number()}), {"sum": 0})
        sourceResource = self.resource("source", {"version": 1, "open": lambda _options: events})

        def onEvent(value: Any, ctx: Any) -> None:
            roots.add(ctx.rootId)

            def mutate(draft: Any) -> None:
                draft["sum"] += value

            seen.change(ctx, mutate)

        self.on("onEvent", sourceResource, lambda source: source, onEvent)

        async def task(ctx: Any) -> None:
            await ctx.signal.wait()
            taskAborted[0] = True

        self.task(task)
        return {"seen": seen}

    host.serve(Watcher, factory)
    ref = host.hold(Watcher, {})
    await ref.ready
    events.emit(2)
    events.emit(3)
    await tick()
    assert ref.seen.value == {"sum": 5}
    assert len(roots) == 2
    ref.release()
    await tick()
    await tick()
    assert events.size == 0
    assert taskAborted[0]


async def test_re_entrant_writes_publish_in_adoption_order_derivations_never_go_stale_sol_3():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _: None})
    Echo = local(
        "t.reentrant",
        {
            "version": 1,
            "input": Type.Object({}),
            "members": {"n": Type.Number(), "d": Type.Number(), "set": method(Type.Number())},
        },
    )

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        nState = self.state("n", Type.Number(), 0)
        d = self.derive([nState], lambda n: n * 2)

        # Subscribed first: re-enters the write while revision 1 is being published.
        def bump(n: Any, ctx: Any) -> None:
            if n == 1:
                nState.replace(ctx, 2)

        self.on("bump", nState, bump)

        def set_(value: Any, ctx: Any) -> None:
            nState.replace(ctx, value)

        return {"n": nState, "d": d, "set": set_}

    host.serve(Echo, factory)
    ref = host.hold(Echo, {})
    await ref.ready
    values: list[Any] = []
    replica: list[Any] = [None]

    def onEvent(event: Any) -> None:
        replica[0] = applyImmutable(replica[0], event["ops"]) if event["type"] == "ops" else event["value"]
        values.append(replica[0])

    ref.n.events(onEvent)
    doubled: list[Any] = []
    ref.d.subscribe(doubled.append)
    await ref.set(1)
    await tick()
    assert values == [0, 1, 2]
    assert ref.d.value == 4
    assert doubled == [0, 2, 4]
