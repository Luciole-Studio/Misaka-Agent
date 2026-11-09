"""pi chord's ``test/flow-units.test.ts``: entry 8, flow control with units. First units pass the window, so a get
never waits behind other streams; waiting ops are capped per link; relays ask downstream for a reset when they drop
ops.

MISAKA adaptations: the link without sizes (upstream's object literal with a getter) is a small class; mutating
``change`` callbacks return ``None`` (``draft.push`` returns the new length, which would otherwise be taken as a new
value); ``CountingLink`` is ``tests/fixtures/chord_counting.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from misaka.chord import (
    createHost,
    hostFamily,
    hostNamed,
    memoryLinkPair,
    method,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.host import HostImpl
from tests.fixtures.chord_counting import CountingLink, stateFrames


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


async def until(check: Callable[[], bool], attempts: int = 400) -> None:
    for _ in range(attempts):
        if check():
            break
        await sleep(2)
    assert check(), "condition not reached"


Blob = remote(
    "t.e8-blob",
    {
        "version": 1,
        "input": Type.Object({"name": Type.String()}),
        "host": hostNamed("server"),
        "variants": {
            "main": {
                "state": {"lines": Type.Array(Type.String())},
                "methods": {"push": method(Type.String()), "ping": method(None, Type.String())},
            }
        },
    },
)


def lines(kib: int, tag: str) -> list[str]:
    """About ``kib`` KiB of lines."""
    return [f"{tag}{i}:{'x' * 1000}" for i in range(kib)]


def serveBlob(host: Any, size: Callable[[str], int]) -> None:
    def factory(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        name = input["name"]
        state = self.state("lines", Type.Array(Type.String()), lines(size(name), name))

        def push(line: str, ctx: Any) -> None:
            def mutate(draft: Any) -> None:
                draft.push(line)

            state.change(ctx, mutate)

        return {"variant": "main", "lines": state, "push": push, "ping": lambda _ctx: "pong"}

    host.serve(Blob, factory)


def client(server: Any, window: float, pendingCap: float | None = None) -> tuple[Any, CountingLink, Callable[[], Any]]:
    down, up = memoryLinkPair()
    options: dict[str, Any] = {"principal": {"user": "u"}, "window": window}
    if pendingCap is not None:
        options["pendingCap"] = pendingCap
    server.accept(down, options)
    link = CountingLink(up)
    host = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": link, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )

    def connection() -> Any:
        return next(candidate for candidate in server.connections if candidate.role == "downstream")

    return host, link, connection


# ─── flow control with units ───


async def test_with_large_streams_waiting_behind_a_full_window_a_new_get_resolves_at_once_and_calls_are_answered():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveBlob(server, lambda name: 1 if name == "small" else 500)
    host, link, _ = client(server, 64 * 1024)
    bigs = [host.hold(Blob, {"name": name}) for name in ("a", "b", "c")]
    await asyncio.gather(*[big.ready for big in bigs])
    link.paused = True
    # Changes to every large stream now wait for acknowledgements that do not come.
    for big in bigs:
        for i in range(50):
            await big.push(f"{i}:{'y' * 2000}")
    small = host.hold(Blob, {"name": "small"})
    await small.ready
    assert len(small.lines.value) == 1
    assert await bigs[0].ping() == "pong"
    assert len(bigs[0].lines.value) < 550, "the large streams still wait"
    link.release()
    await until(lambda: all(len(big.lines.value) == 550 for big in bigs))
    await server.close()


async def test_a_snapshot_larger_than_the_window_arrives_whole_later_changes_wait_for_acknowledgements():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveBlob(server, lambda _name: 300)
    host, link, _ = client(server, 16 * 1024)
    link.paused = True
    big = host.hold(Blob, {"name": "chat"})
    await big.ready
    assert len(big.lines.value) == 300
    for i in range(40):
        await big.push(f"{i}:{'z' * 1000}")
    assert len(big.lines.value) < 340
    link.release()
    await until(lambda: len(big.lines.value) == 340)
    await server.close()


class _Unsized:
    """A link without sizes: what an IPC channel is."""

    def __init__(self, down: Any) -> None:
        self._down = down

    @property
    def closed(self) -> bool:
        return self._down.closed

    def send(self, frame: Any) -> None:
        self._down.send(frame)

    def receive(self, handler: Callable[[Any], None]) -> None:
        self._down.receive(handler)

    def onClose(self, handler: Callable[[str], None]) -> None:
        self._down.onClose(handler)

    def close(self, reason: str = "closed") -> None:
        self._down.close(reason)


async def test_a_link_that_reports_no_sizes_ipc_never_holds_state_back():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveBlob(server, lambda _name: 1)
    down, up = memoryLinkPair()
    server.accept(_Unsized(down), {"principal": {"user": "u"}, "window": 1})
    link = CountingLink(up)
    host = createHost(
        {
            "self": {"kind": "host"},
            "name": "worker",
            "kind": "client",
            "upstream": {"link": link, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    link.paused = True
    blob = host.hold(Blob, {"name": "x"})
    await blob.ready
    for i in range(20):
        await blob.push(f"{i}")
    await until(lambda: len(blob.lines.value) == 21)
    await server.close()


async def test_waiting_ops_stay_under_the_per_link_cap_however_many_streams_stall_each_then_gets_one_reset():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveBlob(server, lambda _name: 1)
    cap = 8 * 1024
    host, link, connection = client(server, 1024, cap)
    K = 6
    blobs = [host.hold(Blob, {"name": f"s{i}"}) for i in range(K)]
    await asyncio.gather(*[blob.ready for blob in blobs])
    link.paused = True
    peak = 0
    for round in range(30):
        for blob in blobs:
            await blob.push(f"{round}:{'w' * 200}")
        peak = max(peak, connection().waiting["bytes"])
    assert peak <= cap, f"waiting ops {peak} exceed the cap {cap}"
    before = len(link.received)
    link.release()
    await until(lambda: all(len(blob.lines.value) == 31 for blob in blobs))
    resets = [frame for frame in stateFrames(link.received[before:]) if frame["as"] == "reset"]
    per: dict[int, int] = {}
    for frame in resets:
        per[frame["hold"]] = per.get(frame["hold"], 0) + 1
    assert len(resets) >= 1, "dropped streams came back as resets"
    for count in per.values():
        assert count <= 2, "about one reset per stream"
    await server.close()


# ─── relay topology ───

Counter = remote(
    "t.e8-relayed",
    {
        "version": 1,
        "input": Type.Object({"session": Type.String()}),
        "host": hostFamily("session:", "session"),
        "variants": {"main": {"state": {"n": Type.Number()}, "methods": {"add": method(Type.Number())}}},
    },
)


class _Cluster:
    def __init__(self) -> None:
        self.server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
        self.worker: HostImpl | None = None
        self._toWorker: CountingLink | None = None

        async def resolve(name: str) -> Any:
            if self._toWorker is not None:
                return self._toWorker
            down, up = memoryLinkPair()
            created = HostImpl(
                {
                    "self": {"kind": "host"},
                    "name": name,
                    "upstream": {"link": up, "principal": {"kind": "server"}},
                    "onError": lambda _error: None,
                }
            )

            def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
                n = self.state("n", Type.Number(), 0)

                def add(delta: float, ctx: Any) -> None:
                    n.replace(ctx, n.value + delta)

                return {"variant": "main", "n": n, "add": add}

            created.serve(Counter, factory)
            self.worker = created
            self._toWorker = CountingLink(down)
            return self._toWorker

        self.server.route(
            "session:", {"principal": lambda name: {"host": name}, "authorize": lambda _p, _h: True, "resolve": resolve}
        )

    def browser(self, principal: Any, window: float | None = None) -> tuple[Any, CountingLink, Any]:
        down, up = memoryLinkPair()
        options: dict[str, Any] = {"principal": principal}
        if window is not None:
            options["window"] = window
            options["pendingCap"] = window * 2
        self.server.accept(down, options)
        link = CountingLink(up)
        host = createHost(
            {
                "self": {"kind": "host"},
                "name": "browser",
                "kind": "client",
                "upstream": {"link": link, "principal": {"kind": "server"}},
                "onError": lambda _error: None,
            }
        )
        return host, link, up

    def served(self) -> list[Any]:
        upstream = None if self.worker is None else self.worker.upstream
        return [] if upstream is None else list(upstream.served.values())

    def relayed(self) -> int:
        return sum(len(connection.relayedRefs) for connection in self.server.connections if connection.peer.kind == "host")

    def toWorker(self) -> CountingLink:
        assert self._toWorker is not None
        return self._toWorker


async def test_each_origin_hold_is_one_downstream_hold_nothing_is_left_after_release_close_or_revoke():
    c = _Cluster()
    # Two tabs of one guest (two connections, one principal) and another guest.
    one = c.browser({"guest": "g"})
    two = c.browser({"guest": "g"})
    three = c.browser({"guest": "h"})
    a = one[0].hold(Counter, {"session": "S1"})
    a2 = one[0].hold(Counter, {"session": "S1"})
    b = two[0].hold(Counter, {"session": "S1"})
    d = three[0].hold(Counter, {"session": "S1"})
    await asyncio.gather(a.ready, a2.ready, b.ready, d.ready)
    assert c.toWorker().count("sent", "hold") == 3, "two tabs and another principal: three holds"
    assert len(c.served()) == 3
    a.release()
    a2.release()
    await until(lambda: len(c.served()) == 2 and c.relayed() == 2)
    two[2].close()
    await until(lambda: len(c.served()) == 1 and c.relayed() == 1)
    await c.server.invalidatePrincipal({"guest": "h"})
    await until(lambda: len(c.served()) == 0 and c.relayed() == 0)
    await c.server.close()


async def test_a_stalled_tab_overflows_and_gets_a_reset_while_another_tab_keeps_getting_every_change():
    c = _Cluster()
    slow = c.browser({"guest": "g"}, 256)
    fast = c.browser({"guest": "g"})
    stalled = slow[0].hold(Counter, {"session": "S1"})
    live = fast[0].hold(Counter, {"session": "S1"})
    await asyncio.gather(stalled.ready, live.ready)
    slow[1].paused = True
    seen: list[float] = []
    live.n.subscribe(seen.append)
    for _ in range(200):
        await live.add(1)
    await until(lambda: live.n.value == 200)
    assert len(seen) > 100, "the fast tab saw the changes as they came"
    assert stalled.n.value < 200, "the stalled tab waits"
    assert c.toWorker().count("sent", "resync") >= 1, "the relay asked the worker for a reset"
    slow[1].release()
    await until(lambda: stalled.n.value == 200)
    await c.server.close()
