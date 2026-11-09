"""Regression tests for the round-2 verification of the chord port (VERIFY2-R2-chord, findings R2-*).

Each test is named after its finding and fails on the port as it was before the fix. The expected values are
upstream's (chord v1.3.0 under Node) where the finding was compared with it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.chord import createHost, local, memoryLinkPair
from misaka.chord._typebox import Type
from misaka.chord.host import Facet
from misaka.chord.object import refOf


async def settle(turns: int = 20) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


# ─── R2-N2: a waiter that gives up never cancels shared work ─────────────────


async def test_n2_one_holder_giving_up_on_ready_leaves_the_object_live_for_every_holder():
    """Node: `caller A gave up`, `A ready ok; status live`, `B … ready ok; status live`, `object status live`."""
    S = local("t.r2-ready", {"version": 1, "input": Type.Object({})})
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        async def open_(_options: Any) -> int:
            await gate
            return 1

        self.resource("a", {"version": 1, "open": open_})
        return {"hi": lambda *_: "hi"}

    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(S, factory)
    ref = host.hold(S, {})
    other = host.hold(S, {})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(ref.ready, 0.02)
    gate.set_result(None)
    await settle(10)
    await ref.ready
    await other.ready
    assert (ref.status, other.status, refOf(ref).object.status) == ("live", "live", "live")
    assert await ref.hi() == "hi"
    await host.close()


async def test_n2_giving_up_on_a_successor_does_not_cancel_its_predecessors_disposal():
    """Node: `close 1 done`, new object `live`."""
    S = local("t.r2-successor", {"version": 1, "input": Type.Object({})})
    log: list[str] = []
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    count = [0]

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        count[0] += 1
        k = count[0]

        async def close(_value: Any) -> None:
            log.append(f"close {k} begins")
            await gate
            log.append(f"close {k} done")

        self.resource("a", {"version": 1, "open": lambda _options: k, "close": close})
        return {}

    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(S, factory)
    first = host.hold(S, {})
    await first.ready
    old = refOf(first).object
    first.release()
    await settle(5)
    assert old.status == "disposing"
    second = host.hold(S, {})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(second.ready, 0.02)
    assert not gate.cancelled()
    gate.set_result(None)
    await settle()
    assert (old.status, refOf(second).object.status, log) == ("disposed", "live", ["close 1 begins", "close 1 done"])
    await host.close()


async def test_n2_giving_up_on_install_leaves_its_host_objects_activating_to_live():
    S = local("t.r2-host-object", {"version": 1, "input": Type.Object({})})
    gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        async def open_(_options: Any) -> int:
            await asyncio.shield(gate)
            return 1

        self.resource("a", {"version": 1, "open": open_})
        return {"hi": lambda *_: "hi"}

    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(host.install(Facet("f", lambda api: api.serve(S, factory, {"lifetime": "host"}))), 0.02)
    gate.set_result(None)
    await settle()
    assert [object_.status for object_ in host.objects.values()] == ["live"]
    await host.close()


async def test_n2_promise_all_does_not_cancel_its_inputs_when_its_waiter_gives_up():
    from misaka.chord._promise import promiseAll

    shared: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(promiseAll([shared]), 0.01)
    assert not shared.cancelled()
    shared.set_result(1)
    assert await promiseAll([shared, 2]) == [1, 2]
    failed: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    failed.set_exception(ValueError("first"))
    with pytest.raises(ValueError, match="first"):
        await promiseAll([shared, failed])


# ─── R2-N3: a reload runs on when its caller gives up (cluster path) ─────────


async def test_n3_a_cluster_reload_whose_caller_stops_waiting_runs_on_to_its_commit_everywhere():
    """Node (the same scenario on upstream's cluster test setup): `caller gave up`, server and worker not reloading,
    nothing pending, no gating, code version 2 on both, `greet after: v2 x`."""
    from tests.test_chord_cluster import Greeter, Latch, greeterFacet, setup, tick

    server, worker, modules, _down = setup()
    await tick()
    greeter = server.hold(Greeter, {"session": "S1"})
    assert await greeter.greet("a") == "v1 a"
    go = Latch()

    async def block(ctx: Any) -> None:
        await greeter.greet("g", ctx)
        await go.promise

    blocker = server.root(block)
    await tick()
    modules["greeter"] = greeterFacet("v2", stateVersion=2)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server.reload([], {"remote": {"session:": ["greeter"]}, "gateDeadline": 60_000}), 0.1)
    go.open()
    await blocker
    for _ in range(30):
        await tick()
    for host in (server, worker):
        assert (host.reloading, host.pending, host.gating, host.codeVersion) == (False, None, None, 2)
    assert await asyncio.wait_for(greeter.greet("x"), 1) == "v2 x"
    await server.close()
    await worker.close()


# ─── R2-N1: rejection-like cancellations in aggregates ───────────────────────


async def test_n1_run_all_aggregates_a_cancelled_step_and_reports_a_single_one():
    from misaka.chord._promise import CancelledRejection, background
    from misaka.chord.errors import runAll

    async def cancelled() -> None:
        future = asyncio.get_running_loop().create_future()
        future.cancel()
        await future

    def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ExceptionGroup) as group:
        await runAll([boom, cancelled], "cleanup failed")
    assert [type(error) for error in group.value.exceptions] == [ValueError, CancelledRejection]
    reported: list[BaseException] = []
    task = background(runAll([cancelled], "x"), reported.append)
    await settle(5)
    assert not task.cancelled()
    assert [type(error) for error in reported] == [CancelledRejection]


# ─── R2-N5: Progress when a stop() caller gives up ───────────────────────────


async def test_n5_a_stop_whose_caller_gives_up_leaves_the_commit_in_flight_intact():
    from misaka.chord import _promise
    from misaka.chord.output import Progress

    loop = asyncio.get_running_loop()
    gate: asyncio.Future[None] = loop.create_future()

    async def write() -> int:
        await gate
        raise RuntimeError("write failed")

    def onError(_error: BaseException) -> None:
        raise ValueError("onError threw")

    progress = Progress(write, onError, 10)
    progress.mark()
    inFlight = progress._inFlight
    assert inFlight is not None
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(progress.stop(), 0.02)
    assert not inFlight.cancelled()
    runs = list(_promise._background)
    gate.set_result(None)
    await settle(5)
    assert [(run.done(), run.cancelled(), run.exception() if run.done() and not run.cancelled() else None) for run in runs] == [
        (True, False, None)
    ]
    with pytest.raises(ValueError, match="onError threw"):
        await inFlight


# ─── R2-N6: a closed coroutine is not cleaned up inside its GeneratorExit ────


def test_n6_closing_a_loop_with_an_activation_pending_does_not_await_inside_generator_exit(monkeypatch):
    import gc
    import sys

    S = local("t.r2-genexit", {"version": 1, "input": Type.Object({})})
    loop = asyncio.new_event_loop()
    unraisable: list[str] = []
    monkeypatch.setattr(sys, "unraisablehook", lambda info: unraisable.append(f"{info.exc_type.__name__}: {info.exc_value}"))

    async def main() -> None:
        host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
        never = loop.create_future()

        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            async def open_(_options: Any) -> None:
                await never

            self.resource("a", {"version": 1, "open": open_})
            return {}

        host.serve(S, factory)
        host.hold(S, {})
        await asyncio.sleep(0.01)

    loop.run_until_complete(main())
    # "Task was destroyed but it is pending!" is this scenario's expected report, not the finding.
    loop.set_exception_handler(lambda _loop, _context: None)
    loop.close()  # the pending tasks are not cancelled: their coroutines are closed when collected
    from misaka.chord import _promise

    _promise._background.clear()
    gc.collect()
    # Before: `RuntimeError: coroutine ignored GeneratorExit` (wrapped as `ChordError: unavailable: activating …`).
    assert [entry for entry in unraisable if "GeneratorExit" in entry] == []


# ─── R2-L1: a memory link closed without a running loop ──────────────────────


def test_l1_a_memory_link_closed_outside_a_loop_reports_its_own_reason_on_both_sides():
    """Node: `a reasons [ 'bye now' ] b reasons [ 'bye now' ]`."""
    a, b = memoryLinkPair()
    closedA: list[str] = []
    closedB: list[str] = []
    a.onClose(closedA.append)
    b.onClose(closedB.append)
    a.close("bye now")
    assert (closedA, closedB) == (["bye now"], ["bye now"])
    # Frames sent before the close arrive first, in order.
    c, d = memoryLinkPair()
    got: list[Any] = []
    d.receive(lambda frame: got.append(frame["n"]))
    for n in range(3):
        c.send({"t": "ping", "n": n})
    c.close("done")
    assert got == [0, 1, 2] and d.closed
