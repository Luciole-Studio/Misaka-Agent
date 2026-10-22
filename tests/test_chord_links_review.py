"""pi chord's ``test/links-review.test.ts``.

Regression tests for Astra's phase 1b review (findings #1-#11) and the shared-worker test gap.

MISAKA adaptations: ``tick`` (Node's ``setImmediate``) lets the event loop run a few turns, since a memory link delivers
one frame per ``loop.call_soon`` step; ``sleep(ms)`` is ``asyncio.sleep``. Frames are dicts. A host's internals (``connections``,
``reloading``, a connection's ``relayedRefs``/``relayedCalls``) are read directly, as upstream reads them through a
cast. The ``accepts`` adapter of #11 returns a small readable class (a property getter needs a class in Python).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.chord import (
    PROTOCOL_VERSION,
    ChordError,
    createHost,
    hostNamed,
    isChordError,
    local,
    memoryLinkPair,
    method,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.host import Facet


async def tick() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


async def until(check: Any, attempts: int = 100) -> None:
    for _ in range(attempts):
        if check():
            break
        await sleep(2)
    assert check(), "condition not reached"


class Latch:
    def __init__(self) -> None:
        self.promise: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def open(self) -> None:
        if not self.promise.done():
            self.promise.set_result(None)


def latch() -> Latch:
    return Latch()


def quiet(future: Any) -> None:
    """``void promise.catch(() => {})``."""
    future.add_done_callback(lambda done: done.cancelled() or done.exception())


class WatchedLink:
    """A link that logs what it sends and can drop a frame by closing instead."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.sent: list[dict[str, Any]] = []
        self.closeOn: Any = None

    @property
    def closed(self) -> bool:
        return self.inner.closed

    def send(self, frame: dict[str, Any]) -> int | None:
        self.sent.append(frame)
        if self.closeOn is not None and self.closeOn(frame):
            self.inner.close("dropped")
            return None
        return self.inner.send(frame)

    def receive(self, handler: Any) -> None:
        self.inner.receive(handler)

    def onClose(self, handler: Any) -> None:
        self.inner.onClose(handler)

    def close(self, reason: str = "closed") -> None:
        self.inner.close(reason)


Counter = remote(
    "t.counter",
    {
        "version": 1,
        "input": Type.Object({"name": Type.String()}),
        "host": hostNamed("session:S1"),
        "variants": {
            "main": {
                "state": {"count": Type.Object({"n": Type.Number()})},
                "methods": {
                    "add": method(Type.Number(), Type.Number()),
                    "who": method(None, Type.Unknown()),
                    "slow": method(),
                },
            },
        },
    },
)


class Cluster:
    """A server routing ``session:*`` to workers it spawns once per name; the server→worker link is watched."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.principals: list[Any] = []
        self.server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
        self.workers: dict[str, dict[str, Any]] = {}
        self.resolveGate: asyncio.Future[None] | None = None
        self.slowGate = latch()
        self.server.route(
            "session:",
            {
                "principal": lambda name: {"host": name},
                "authorize": lambda _principal, _host: True,
                "resolve": self._resolve,
            },
        )

    async def _resolve(self, name: str) -> Any:
        if self.resolveGate is not None:
            await self.resolveGate
        existing = self.workers.get(name)
        if existing is not None:
            return existing["link"]
        down, up = memoryLinkPair()
        link = WatchedLink(down)
        worker = createHost(
            {
                "self": {"kind": "host"},
                "name": name,
                "upstream": {"link": up, "principal": {"kind": "server"}},
                "onError": lambda _error: None,
            }
        )

        cluster = self

        def serve(input: Any, principal: Any, objectSelf: Any) -> dict[str, Any]:
            cluster.principals.append(principal)
            objectSelf.resource(
                "probe",
                {
                    "version": 1,
                    "open": lambda _options: cluster.log.append(f"open {input['name']}"),
                    "close": lambda _value: cluster.log.append(f"close {input['name']}"),
                },
            )
            count = objectSelf.state("count", Type.Object({"n": Type.Number()}), {"n": 0})

            def add(delta: float, ctx: Any) -> Any:
                def bump(draft: Any) -> None:
                    draft["n"] += delta

                count.change(ctx, bump)
                return count.value["n"]

            async def slow(ctx: Any) -> None:
                ctx.signal.addEventListener("abort", lambda: cluster.log.append("slow aborted"))
                await cluster.slowGate.promise

            return {"variant": "main", "count": count, "add": add, "who": lambda ctx: principal, "slow": slow}

        worker.serve(Counter, serve)
        self.workers[name] = {"host": worker, "link": link}
        return link

    def browser(self, principal: Any, restamp: Any = None) -> dict[str, Any]:
        down, up = memoryLinkPair()
        self.server.accept(down, {"principal": principal, "restamp": restamp})
        host = createHost(
            {
                "self": {"kind": "host"},
                "name": "browser",
                "kind": "client",
                "upstream": {"link": up, "principal": {"kind": "server"}},
                "onError": lambda _error: None,
            }
        )
        return {"host": host, "up": up}

    def raw(self, principal: Any) -> dict[str, Any]:
        down, raw = memoryLinkPair()
        self.server.accept(down, {"principal": principal})
        frames: list[dict[str, Any]] = []
        raw.receive(frames.append)
        raw.send({"t": "hello", "protocol": PROTOCOL_VERSION, "name": "raw", "kind": "client", "codeVersion": 0})
        return {"raw": raw, "frames": frames}


def connectionTo(host: Any, name: str) -> Any:
    return next(connection for connection in host.connections if connection.peer.name == name)


# ─── links review: identity and stamping ───


async def test_1_the_acceptor_decides_what_a_peer_is_a_client_announcing_itself_as_a_host_is_cut_off():
    c = Cluster()
    down, raw = memoryLinkPair()
    c.server.accept(down, {"principal": {"guest": "g"}})
    raw.receive(lambda _frame: None)
    raw.send({"t": "hello", "name": "session:S9", "kind": "host", "codeVersion": 1})
    await until(lambda: raw.closed)
    # A host link must have a name and its hello must match it.
    with pytest.raises(TypeError, match=r"needs its name"):
        c.server.accept(memoryLinkPair()[0], {"principal": {}, "kind": "host"})


async def test_1_root_end_is_honored_only_from_the_link_the_root_came_over():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    Echo = remote(
        "t.echo",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"methods": {"root": method(None, Type.String())}}},
        },
    )

    def facet(version: int) -> Facet:
        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            self.state("n", Type.Number(), 0, {"version": version})
            return {"variant": "main", "root": lambda ctx: ctx.rootId}

        return Facet("echo", lambda api: api.serve(Echo, factory))

    server.install(facet(1))
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "w"}, "kind": "host", "name": "w"})
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "w",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    go = latch()
    rootId = [""]

    async def pin(ctx: Any) -> None:
        rootId[0] = await (await ctx.get(Echo, {})).root(ctx)
        await go.promise

    pinned = worker.root(pin)
    await until(lambda: rootId[0] != "")
    # A client forging the worker root's end must not release its lease.
    clientDown, raw = memoryLinkPair()
    server.accept(clientDown, {"principal": {"guest": "g"}})
    raw.receive(lambda _frame: None)
    raw.send({"t": "hello", "protocol": PROTOCOL_VERSION, "name": "raw", "kind": "client", "codeVersion": 0})
    raw.send({"t": "root_end", "rootId": rootId[0]})
    await sleep(10)
    with pytest.raises(ChordError) as info:
        await server.reload([facet(2)], {"gateDeadline": 40})
    assert isChordError(info.value, "aborted") and info.value.reason == "gate_deadline"
    assert rootId[0] in info.value.message
    go.open()
    await pinned


async def test_2_a_revocation_while_the_route_is_still_resolving_closes_the_relay_no_worker_object_is_made():
    c = Cluster()
    gate = latch()
    c.resolveGate = gate.promise
    browser = c.browser({"guest": "g", "revision": 1}, lambda p: {**p, "revision": 2})
    counter = browser["host"].hold(Counter, {"name": "a"})
    await sleep(5)
    await c.server.invalidatePrincipal({"guest": "g"})
    await until(lambda: counter.status == "gone")
    gate.open()
    await sleep(10)
    assert c.principals == []
    again = browser["host"].hold(Counter, {"name": "a"})
    assert await again.who() == {"guest": "g", "revision": 2}


async def test_10_malformed_frames_and_duplicate_ids_close_the_link():
    c = Cluster()
    first = c.raw({"guest": "g"})
    first["raw"].send({"t": "call", "hold": 1})
    await until(lambda: first["raw"].closed)
    second = c.raw({"guest": "g"})
    token = Counter.description
    second["raw"].send({"t": "hold", "id": 1, "at": "session:S1", "token": token, "input": {"name": "a"}})
    second["raw"].send({"t": "hold", "id": 1, "at": "session:S1", "token": token, "input": {"name": "b"}})
    await until(lambda: second["raw"].closed)


# ─── links review: relay ownership ───


async def test_7_a_browser_disconnect_cancels_its_call_downstream():
    c = Cluster()
    browser = c.browser({"guest": "g"})
    counter = browser["host"].hold(Counter, {"name": "a"})
    await counter.ready
    quiet(counter.slow())
    await sleep(10)
    browser["up"].close()
    await until(lambda: "slow aborted" in c.log)
    down = connectionTo(c.server, "session:S1")
    assert len(down.relayedCalls) == 0


async def test_9_state_stopped_while_the_route_resolves_is_forwarded_as_a_hold_without_state():
    c = Cluster()
    gate = latch()
    c.resolveGate = gate.promise
    raw = c.raw({"guest": "g"})["raw"]
    raw.send({"t": "hold", "id": 1, "at": "session:S1", "token": Counter.description, "input": {"name": "a"}})
    raw.send({"t": "unsub", "hold": 1})
    await sleep(5)
    gate.open()
    await until(
        lambda: "session:S1" in c.workers and any(frame["t"] == "hold" for frame in c.workers["session:S1"]["link"].sent)
    )
    await sleep(10)
    sent = c.workers["session:S1"]["link"].sent
    hold = next((frame for frame in sent if frame["t"] == "hold"), None)
    assert hold is not None and hold.get("state") is False
    assert not any(frame["t"] in ("sub", "unsub") for frame in sent)


async def test_9_a_release_while_the_route_resolves_settles_waiting_calls_and_forwards_nothing():
    c = Cluster()
    gate = latch()
    c.resolveGate = gate.promise
    opened = c.raw({"guest": "g"})
    raw, frames = opened["raw"], opened["frames"]
    raw.send({"t": "hold", "id": 1, "at": "session:S1", "token": Counter.description, "input": {"name": "a"}})
    raw.send({"t": "call", "id": 2, "hold": 1, "member": "add", "args": 1})
    await sleep(5)
    raw.send({"t": "release", "id": 1})
    await until(lambda: any(frame["t"] == "result" and frame["id"] == 2 for frame in frames))
    result = next(frame for frame in frames if frame["t"] == "result" and frame["id"] == 2)
    assert result.get("error") is not None
    gate.open()
    await sleep(10)
    sent = c.workers["session:S1"]["link"].sent if "session:S1" in c.workers else []
    assert not any(frame["t"] == "hold" for frame in sent)


async def test_9_an_origin_disconnect_removes_its_relayed_holds_and_subscriptions_downstream():
    c = Cluster()
    browser = c.browser({"guest": "g"})
    counter = browser["host"].hold(Counter, {"name": "a"})
    counter.count.subscribe(lambda _value: None)
    await counter.add(1)
    down = connectionTo(c.server, "session:S1")
    assert len(down.relayedRefs) == 1
    browser["up"].close()
    await until(lambda: len(down.relayedRefs) == 0)
    await until(lambda: "close a" in c.log)


async def test_two_browsers_share_one_worker_connection_one_leaving_releases_only_its_own_holds():
    c = Cluster()
    one = c.browser({"guest": "g1"})
    two = c.browser({"guest": "g2"})
    a = one["host"].hold(Counter, {"name": "same"})
    b = two["host"].hold(Counter, {"name": "same"})
    assert await a.add(1) == 1
    assert await b.add(5) == 5
    assert len(c.workers) == 1
    assert c.principals == [{"guest": "g1"}, {"guest": "g2"}]
    one["up"].close()
    await until(lambda: len([entry for entry in c.log if entry == "close same"]) == 1)
    assert await b.add(1) == 6


async def test_11_snapshots_through_an_accepts_adapter_are_validated_against_the_consumers_schema():
    DocV1 = remote(
        "t.doc",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"state": {"view": Type.Object({"text": Type.String()})}}},
        },
    )
    DocV2 = remote(
        "t.doc",
        {
            "version": 2,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"state": {"view": Type.Object({"body": Type.String()})}}},
        },
    )
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})

    class Mapped:
        def __init__(self, view: Any) -> None:
            self._view = view

        @staticmethod
        def map(value: Any) -> dict[str, Any]:
            return {"text": value["body"]}

        @property
        def value(self) -> Any:
            return self.map(self._view.value)

        def subscribe(self, fn: Any) -> Any:
            return self._view.events(lambda _event: fn(self.map(self._view.value)))

        def events(self, fn: Any) -> Any:
            return self._view.events(
                lambda event: fn(
                    {"type": "reset" if event["type"] == "ops" else event["type"], "value": self.map(self._view.value)}
                )
            )

    server.serve(
        DocV2,
        lambda _input, _principal, self: {
            "variant": "main",
            "view": self.state("view", Type.Object({"body": Type.String()}), {"body": "hi"}),
        },
        {"accepts": {1: lambda current: {"variant": "main", "view": Mapped(current["view"])}}},
    )
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"guest": "g"}})
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    doc = browser.hold(DocV1, {})
    await doc.ready
    assert doc.view.value == {"text": "hi"}


# ─── links review: cross-host reload ───

Greeter = remote(
    "t.greeter",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("session:S1"),
        "variants": {"main": {"methods": {"greet": method(Type.String(), Type.String())}}},
    },
)


def greeterFacet(greeting: str) -> Facet:
    return Facet(
        "greeter",
        lambda api: api.serve(
            Greeter, lambda _input, _principal, _self: {"variant": "main", "greet": lambda name, ctx: f"{greeting} {name}"}
        ),
    )


def setup(slowLoad: asyncio.Future[None] | None = None) -> dict[str, Any]:
    modules: dict[str, Facet] = {"greeter": greeterFacet("v1")}

    async def loader(names: Any) -> list[Facet]:
        if slowLoad is not None:
            await slowLoad
        return [modules[str(name)] for name in names]

    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})

    def connect() -> dict[str, Any]:
        down, up = memoryLinkPair()
        watched = WatchedLink(down)
        server.accept(watched, {"principal": {"worker": "w"}, "kind": "host", "name": "session:S1"})
        return {"watched": watched, "up": up}

    first = connect()
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:S1",
            "upstream": {"link": first["up"], "principal": {"kind": "server"}},
            "loader": loader,
            "onError": lambda _error: None,
        }
    )
    worker.install(modules["greeter"])
    return {"server": server, "worker": worker, "modules": modules, "link": first["watched"], "connect": connect}


async def test_3_an_abort_while_the_child_is_still_loading_modules_frees_its_reload_slot_a_later_reload_works():
    gate = latch()
    s = setup(gate.promise)
    server, worker, modules = s["server"], s["worker"], s["modules"]
    await tick()
    modules["greeter"] = greeterFacet("v2")
    with pytest.raises(ChordError) as info:
        await server.reload([], {"remote": {"session:": ["greeter"]}, "voteDeadline": 30})
    assert isChordError(info.value, "aborted")
    gate.open()
    await until(lambda: not worker.reloading)
    await server.reload([], {"remote": {"session:": ["greeter"]}})
    assert await server.hold(Greeter, {}).greet("x") == "v2 x"


async def test_3_the_parent_link_dying_while_the_child_prepares_unwinds_the_child():
    gate = latch()
    s = setup(gate.promise)
    server, worker, modules, link = s["server"], s["worker"], s["modules"], s["link"]
    await tick()
    modules["greeter"] = greeterFacet("v2")
    reload = server.reload([], {"remote": {"session:": ["greeter"]}})
    await sleep(5)
    link.close("gone")
    with pytest.raises(Exception):  # noqa: B017 - any rejection, as upstream's bare `assert.rejects`
        await reload
    gate.open()
    await until(lambda: not worker.reloading)
    assert worker.codeVersion == 1


async def test_4_tasks_the_commit_starts_cannot_reach_a_child_before_the_childs_commit():
    s = setup()
    server, modules = s["server"], s["modules"]
    await tick()
    results: list[Any] = []
    Caller = local("t.caller", {"version": 1, "input": Type.Object({})})

    def callerFacet(active: bool) -> Facet:
        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            greeter = self.get(Greeter, {})
            if active:

                async def task(ctx: Any) -> None:
                    try:
                        results.append(await greeter.greet("t", ctx))
                    except ChordError as error:
                        results.append(error.code)

                self.task(task)
            return {}

        return Facet("caller", lambda api: api.serve(Caller, factory, {"lifetime": "host"}))

    server.install(callerFacet(False))
    await sleep(10)
    modules["greeter"] = greeterFacet("v2")
    await server.reload([callerFacet(True)], {"remote": {"session:": ["greeter"]}})
    await until(lambda: len(results) == 1)
    assert results == ["v2 t"]


async def test_5_a_child_that_missed_a_commit_is_reconciled_on_reconnect_never_relabeled():
    s = setup()
    server, worker, modules, link, connect = s["server"], s["worker"], s["modules"], s["link"], s["connect"]
    await tick()
    modules["greeter"] = greeterFacet("v2")
    # The commit frame is lost: the link dies right after the child voted.
    link.closeOn = lambda frame: frame["t"] == "commit"
    await server.reload([], {"remote": {"session:": ["greeter"]}})
    await until(lambda: not worker.reloading)
    assert worker.codeVersion != server.codeVersion
    next_ = connect()
    worker.connect(next_["up"], {"principal": {"kind": "server"}})
    greeter = server.hold(Greeter, {})
    assert await greeter.greet("x") == "v2 x"
    assert worker.codeVersion == server.codeVersion


async def test_6_a_server_root_that_bounces_through_a_worker_and_back_ends_normally_no_leaked_lease():
    s = setup()
    server, worker = s["server"], s["worker"]
    await tick()
    Echo = remote(
        "t.echo2",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"methods": {"ping": method()}}},
        },
    )
    Bouncer = remote(
        "t.bouncer",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("session:S1"),
            "variants": {"main": {"methods": {"bounce": method()}}},
        },
    )

    def echoFacet(version: int) -> Facet:
        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            self.state("n", Type.Number(), 0, {"version": version})
            return {"variant": "main", "ping": lambda ctx: None}

        return Facet("echo", lambda api: api.serve(Echo, factory))

    server.install(echoFacet(1))
    # Keeps the Echo object the bounced call reaches (it acts as the worker's principal) alive, so the reload gate
    # must decide about the bounced root's lease.
    echo = server.hold(Echo, {}, {"principal": {"worker": "w"}})
    await echo.ready

    async def bounce(ctx: Any) -> None:
        await (await ctx.get(Echo, {})).ping(ctx)

    worker.serve(Bouncer, lambda _input, _principal, _self: {"variant": "main", "bounce": bounce})
    await server.root(lambda ctx: server.hold(Bouncer, {}).bounce(ctx))
    await sleep(10)
    await server.reload([echoFacet(2)], {"gateDeadline": 200})

