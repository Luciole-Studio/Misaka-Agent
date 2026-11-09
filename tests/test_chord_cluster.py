"""pi chord's ``test/cluster.test.ts``: cross-host reload, coordinated by the server.

MISAKA adaptations: after closing a child host the removal test waits one tick before the next reload (JavaScript's
``await`` yields a turn in which the memory link delivers the close; Python's does not when the close finished
synchronously); ``setImmediate`` ticks are a 1 ms sleep (a turn that lets every queued callback run, as a Node
macrotask runs after the microtasks); ``onTestFinished`` is the ``finished`` fixture (cleanups awaited after the
test, in order); facets are ``Facet(name, setup)``; the links upstream writes as object literals with a getter are a
small class (``dropping``); mutating ``change`` callbacks return ``None``; ``Date.now()`` is ``time.monotonic()``.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from misaka.chord import (
    createHost,
    hostFamily,
    hostNamed,
    isChordError,
    local,
    memoryLinkPair,
    method,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.errors import ChordError
from misaka.chord.host import Facet


async def tick() -> None:
    await asyncio.sleep(0.001)


class Latch:
    def __init__(self) -> None:
        self.promise: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def open(self) -> None:
        if not self.promise.done():
            self.promise.set_result(None)


@pytest.fixture
async def finished():
    """``onTestFinished``: cleanups run after the test, in registration order."""
    cleanups: list[Callable[[], Any]] = []
    yield cleanups.append
    for cleanup in cleanups:
        result = cleanup()
        if isinstance(result, Awaitable):
            await result


Greeter = remote(
    "t.greeter",
    {
        "version": 1,
        "input": Type.Object({"session": Type.String()}),
        "host": hostFamily("session:", "session"),
        "variants": {
            "main": {
                "state": {"count": Type.Object({"n": Type.Number()})},
                "methods": {"greet": method(Type.String(), Type.String())},
            }
        },
    },
)

GreeterV2 = remote(
    "t.greeter",
    {
        "version": 2,
        "input": Type.Object({"session": Type.String()}),
        "host": hostFamily("session:", "session"),
        "variants": {"main": {"methods": {"hello": method(Type.String(), Type.String())}}},
    },
)

# The server's own greeter: the greeter facet serves this one on the server, `Greeter` on session hosts.
ServerGreeter = remote(
    "t.server-greeter",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("server"),
        "variants": {
            "main": {
                "state": {"count": Type.Object({"n": Type.Number()})},
                "methods": {"greet": method(Type.String(), Type.String())},
            }
        },
    },
)


def greeterFacet(greeting: str, fail: bool = False, stateVersion: int = 1) -> Facet:
    def setup(api: Any) -> None:
        def greeter(self: Any) -> dict[str, Any]:
            if fail:
                raise RuntimeError("worker factory fails")
            count = self.state("count", Type.Object({"n": Type.Number()}), {"n": 0}, {"version": stateVersion})

            def greet(name: str, ctx: Any) -> str:
                def mutate(draft: Any) -> None:
                    draft["n"] = draft["n"] + 1

                count.change(ctx, mutate)
                return f"{greeting} {name}"

            return {"variant": "main", "count": count, "greet": greet}

        if api.name == "server":
            api.serve(ServerGreeter, lambda _input, _principal, self: greeter(self))
        else:
            api.serve(Greeter, lambda _input, _principal, self: greeter(self))

    return Facet("greeter", setup)


class dropping:
    """``link``, except that frames ``drop`` selects are never sent (a frame lost when its link dies)."""

    def __init__(self, link: Any, drop: Callable[[Any], bool]) -> None:
        self._link = link
        self._drop = drop

    @property
    def closed(self) -> bool:
        return self._link.closed

    def send(self, frame: Any) -> int | None:
        return None if self._drop(frame) else self._link.send(frame)

    def receive(self, handler: Callable[[Any], None]) -> None:
        self._link.receive(handler)

    def onClose(self, handler: Callable[[str], None]) -> None:
        self._link.onClose(handler)

    def close(self, reason: str = "closed") -> None:
        self._link.close(reason)


Ping = remote(
    "t.cluster-ping",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("server"),
        "variants": {"main": {"methods": {"ping": method(None, Type.String())}}},
    },
)


def modulesLoader(modules: dict[str, Facet]) -> Callable[[Any], Awaitable[list[Facet]]]:
    async def loader(names: Any) -> list[Facet]:
        return [modules[str(name)] for name in names]

    return loader


def setup() -> tuple[Any, Any, dict[str, Facet], Any]:
    """Server + worker over a memory link; the worker loads facets by name from ``modules``."""
    modules = {"greeter": greeterFacet("v1")}
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "w"}, "kind": "host", "name": "session:S1"})
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:S1",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "loader": modulesLoader(modules),
            "onError": lambda _error: None,
        }
    )
    worker.install(modules["greeter"])
    return server, worker, modules, down


# ─── cross-host reload ───


async def test_prepares_every_host_commits_the_server_first_then_the_workers_code_versions_stay_in_step():
    server, worker, modules, _ = setup()
    await tick()
    greeter = server.hold(Greeter, {"session": "S1"})
    assert await greeter.greet("a") == "v1 a"
    go = Latch()

    async def pin(ctx: Any) -> str:
        await greeter.greet("p", ctx)
        await go.promise
        return await greeter.greet("p", ctx)

    pinned = server.root(pin)
    await tick()
    shapes: list[str] = []
    greeter.onShape(lambda shape: shapes.append(shape["contract"]))
    modules["greeter"] = greeterFacet("v2")
    report = await server.reload([], {"remote": {"session:": ["greeter"]}})
    # No waiting: the commit frame precedes this call on the FIFO link, so the worker has committed when it arrives.
    assert await greeter.greet("b") == "v2 b"
    assert server.codeVersion == report["codeVersion"]
    assert worker.codeVersion == report["codeVersion"]
    go.open()
    assert await pinned == "v1 p"
    assert len(shapes) == 1, "one shape push at the commit"
    # The server retires the old code version once its last old root ended, then tells the worker.
    for _ in range(20):
        if worker.previousCodeVersion is None:
            break
        await tick()
    assert server.previousCodeVersion is None
    assert worker.previousCodeVersion is None


async def test_a_failing_vote_aborts_every_host_the_error_keeps_its_code_and_names_the_host():
    server, worker, modules, _ = setup()
    await tick()
    greeter = server.hold(Greeter, {"session": "S1"})
    await greeter.greet("a")
    before = [server.codeVersion, worker.codeVersion]
    modules["greeter"] = greeterFacet("v2", fail=True)
    with pytest.raises(ChordError) as rejected:
        await server.reload([], {"remote": {"session:": ["greeter"]}})
    assert isChordError(rejected.value, "unavailable")
    assert "session:S1: " in rejected.value.message and "worker factory fails" in rejected.value.message
    assert [server.codeVersion, worker.codeVersion] == before
    assert await greeter.greet("x") == "v1 x"


async def test_a_worker_gate_blocked_past_its_deadline_aborts_the_whole_reload():
    server, worker, modules, _ = setup()
    await tick()
    greeter = server.hold(Greeter, {"session": "S1"})
    go = Latch()

    async def block(ctx: Any) -> None:
        await greeter.greet("g", ctx)
        await go.promise

    blocker = server.root(block)
    await tick()
    before = [server.codeVersion, worker.codeVersion]
    modules["greeter"] = greeterFacet("v2", stateVersion=2)
    with pytest.raises(ChordError) as rejected:
        await server.reload([], {"remote": {"session:": ["greeter"]}, "gateDeadline": 30})
    assert isChordError(rejected.value, "aborted") and rejected.value.reason == "gate_deadline"
    assert re.search(r"session:S1: .*blocking roots: server#\w+:\d+", rejected.value.message)
    assert [server.codeVersion, worker.codeVersion] == before
    go.open()
    await blocker
    assert await greeter.greet("x") == "v1 x"


async def test_removes_facets_from_server_and_workers_reconciles_removal_tombstones_and_clears_them_on_re_add(finished):
    server, worker, modules, _ = setup()

    async def closeBoth() -> None:
        await worker.close()
        await server.close()

    finished(closeBoth)
    await server.install(greeterFacet("v1"))
    await tick()
    local_ = server.hold(ServerGreeter, {})
    child = server.hold(Greeter, {"session": "S1"})
    assert await local_.greet("a") == "v1 a"
    assert await child.greet("a") == "v1 a"
    await server.reload([], {"remove": ["greeter"], "remoteRemove": {"session:": ["greeter"]}})
    for _ in range(20):
        if child.status == "gone":
            break
        await tick()
    assert local_.status == "gone"
    assert child.status == "gone"

    # A child starting with the old set must remove it without trying to import its removed module.
    async def refuse(_names: Any) -> list[Facet]:
        raise AssertionError("Removed modules must not be imported")

    cold = createHost(
        {"self": {"kind": "host"}, "name": "session:cold", "loader": refuse, "onError": lambda _error: None}
    )
    finished(cold.close)
    await cold.install(greeterFacet("v1"))
    old = cold.hold(Greeter, {"session": "cold"})
    await old.ready
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "cold"}, "kind": "host", "name": "session:cold"})
    cold.connect(up, {"principal": {"kind": "server"}})
    for _ in range(20):
        if old.status == "gone":
            break
        await tick()
    assert old.status == "gone"
    assert cold.codeVersion == server.codeVersion
    # Close this loader-less child before the addition; it tested only the removal reconciliation.
    await cold.close()
    # MISAKA fork: in JavaScript the server has seen the child's `bye` here: the loop above resumes from `setImmediate`
    # only after every microtask (so the child's link has no frame left in its chain), and `await cold.close()` resumes
    # one microtask after the `bye` step close queued. asyncio has no microtask checkpoint (a frame sent in the turn the
    # tick's timer fired is still queued) and a Python `await` of a close that finished without suspending does not
    # yield, so the `bye` is two link steps away: the test gives the link those turns (one yield in `Host.close` would
    # not be enough).
    await tick()
    modules["greeter"] = greeterFacet("v2")
    await server.reload([greeterFacet("v2")], {"remote": {"session:": ["greeter"]}})
    assert await server.hold(Greeter, {"session": "S1"}).greet("b") == "v2 b"
    fresh = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:fresh",
            "loader": modulesLoader(modules),
            "onError": lambda _error: None,
        }
    )
    finished(fresh.close)
    await fresh.install(greeterFacet("v1"))
    freshDown, freshUp = memoryLinkPair()
    server.accept(freshDown, {"principal": {"worker": "fresh"}, "kind": "host", "name": "session:fresh"})
    fresh.connect(freshUp, {"principal": {"kind": "server"}})
    assert await server.hold(Greeter, {"session": "fresh"}).greet("c") == "v2 c"


async def test_only_the_server_coordinates_reloads():
    _, worker, _, _ = setup()
    await tick()
    with pytest.raises(ChordError) as rejected:
        await worker.reload([])
    assert isChordError(rejected.value, "unavailable")


async def test_browsers_are_not_participants_they_see_shape_and_reset_pushes_or_upgrade_required():
    server, _, modules, workerLink = setup()

    # The route resolves to the already connected worker.
    async def resolve(_name: str) -> Any:
        return workerLink

    server.route("session:", {"authorize": lambda _p, _h: True, "resolve": resolve, "principal": lambda name: {"host": name}})
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
    viewer = browser.hold(Greeter, {"session": "S1"})
    assert await viewer.greet("a") == "v1 a"
    shapes: list[str] = []
    viewer.onShape(lambda shape: shapes.append(shape["contract"]))
    modules["greeter"] = greeterFacet("v2")
    await server.reload([], {"remote": {"session:": ["greeter"]}})
    for _ in range(20):
        if len(shapes) > 0:
            break
        await tick()
    assert len(shapes) == 1, "one shape push at the commit"
    assert await viewer.greet("b") == "v2 b"

    modules["greeter"] = Facet(
        "greeter",
        lambda api: api.serve(GreeterV2, lambda *_: {"variant": "main", "hello": lambda name, _ctx: f"hi {name}"}),
    )
    await server.reload([], {"remote": {"session:": ["greeter"]}})
    for _ in range(20):
        if viewer.status == "gone":
            break
        await tick()
    assert viewer.status == "gone"
    assert isChordError(viewer.error, "upgrade_required")


async def test_an_abort_while_a_worker_is_still_preparing_a_late_open_closes_itself_and_the_worker_can_reload_again():
    server, worker, modules, _ = setup()
    await tick()
    greeter = server.hold(Greeter, {"session": "S1"})
    await greeter.greet("a")
    log: list[str] = []
    go = Latch()

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        async def open(_options: Any) -> None:
            await go.promise
            log.append("opened")

        self.resource("late", {"version": 1, "open": open, "close": lambda _value: log.append("closed")})
        count = self.state("count", Type.Object({"n": Type.Number()}), {"n": 0})
        return {"variant": "main", "count": count, "greet": lambda name, _ctx: f"slow {name}"}

    slow = Facet("greeter", lambda api: api.serve(Greeter, factory))
    modules["greeter"] = slow
    codeVersion = worker.codeVersion
    with pytest.raises(ChordError) as rejected:
        await server.reload([], {"remote": {"session:": ["greeter"]}, "voteDeadline": 50})
    assert isChordError(rejected.value, "aborted")
    go.open()
    for _ in range(50):
        if "closed" in log:
            break
        await tick()
    assert log == ["opened", "closed"]
    assert worker.codeVersion == codeVersion
    modules["greeter"] = greeterFacet("v3")
    await server.reload([], {"remote": {"session:": ["greeter"]}})
    assert await greeter.greet("b") == "v3 b"


def _emptyFacets(names: Any) -> list[Facet]:
    return [Facet(str(name), lambda _api: None) for name in names]


async def test_a_child_that_says_hello_while_a_reload_runs_is_reconciled_after_it_never_left_in_step_with_the_old_code(
    finished,
):
    modules = {"greeter": greeterFacet("v1")}
    loading = Latch()

    async def loader(names: Any) -> list[Facet]:
        await loading.promise
        return _emptyFacets(names)

    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None, "loader": loader})
    finished(server.close)
    reloading = server.reload(["server-plugin"], {"remote": {"session:": ["greeter"]}})
    # The worker connects while the reload loads its modules: it announces the server's current code version.
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "w"}, "kind": "host", "name": "session:S1"})
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:S1",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "loader": modulesLoader(modules),
            "onError": lambda _error: None,
        }
    )
    finished(worker.close)
    worker.install(modules["greeter"])
    await tick()
    modules["greeter"] = greeterFacet("v2")
    loading.open()
    report = await reloading
    assert list((report.get("children") or {}).keys()) == [], "it was not a participant"
    greeter = server.hold(Greeter, {"session": "S1"})
    assert await greeter.greet("a") == "v2 a"
    assert worker.codeVersion == server.codeVersion


async def test_a_child_keeps_the_previous_code_version_until_its_parent_retired_it_spec_12():
    server, worker, modules, _ = setup()
    await tick()
    greeter = server.hold(Greeter, {"session": "S1"})
    assert await greeter.greet("a") == "v1 a"
    first = server.codeVersion
    go = Latch()

    # A server root of the first code version that calls the worker only after the next two reloads started.
    async def pin(ctx: Any) -> str:
        await go.promise
        return await greeter.greet("p", ctx)

    pinned = server.root(pin)
    modules["greeter"] = greeterFacet("v2")
    await server.reload([], {"remote": {"session:": ["greeter"]}})
    assert worker.previousCodeVersion == first
    second = server.reload([], {"remote": {"session:": []}, "retireDeadline": 2000})
    await tick()
    await tick()
    # The server still waits for its root, so the worker must not have retired that code version.
    assert worker.previousCodeVersion == first
    go.open()
    assert await pinned == "v1 p"
    await second
    assert server.previousCodeVersion is None


async def test_a_child_reconciled_after_its_parent_retired_old_code_versions_may_retire_its_own_previous_one_at_once(
    finished,
):
    modules = {"greeter": greeterFacet("v1")}
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    finished(server.close)

    def plugin() -> Facet:
        return Facet("server-plugin", lambda _api: None)

    server.install(plugin())
    await server.reload([plugin()])
    await server.reload([plugin()])
    assert server.previousCodeVersion is None
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "w"}, "kind": "host", "name": "session:S1"})
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:S1",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "loader": modulesLoader(modules),
            "onError": lambda _error: None,
        }
    )
    finished(worker.close)
    worker.install(modules["greeter"])
    for _ in range(50):
        if worker.codeVersion == server.codeVersion:
            break
        await tick()
    assert worker.codeVersion == server.codeVersion
    # Without the parent's retire watermark it would keep code version 1 until the parent's next retirement.
    for _ in range(20):
        if worker.previousCodeVersion is None:
            break
        await tick()
    assert worker.previousCodeVersion is None
    # And the next cluster reload does not wait for a retirement the parent will never announce.
    started = time.monotonic()
    await server.reload([], {"remote": {"session:": ["greeter"]}, "retireDeadline": 10_000})
    assert time.monotonic() - started < 2


async def test_a_reconciliation_running_when_a_reload_starts_syncs_the_child_only_after_it_to_the_committed_code_version(
    finished,
):
    modules = {"greeter": greeterFacet("v1")}
    serverLoading = Latch()
    blocks = {"server": False, "worker": True}

    async def serverLoader(names: Any) -> list[Facet]:
        if blocks["server"]:
            await serverLoading.promise
        return _emptyFacets(names)

    server = createHost(
        {"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None, "loader": serverLoader}
    )
    finished(server.close)
    server.install(Facet("server-plugin", lambda _api: None))
    await server.reload(["server-plugin"], {"remote": {"session:": ["greeter"]}})
    # The worker connects behind: its reconciliation to code version 2 waits in the worker's module loading.
    workerLoading = Latch()
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "w"}, "kind": "host", "name": "session:S1"})

    async def workerLoader(names: Any) -> list[Facet]:
        if blocks["worker"]:
            await workerLoading.promise
        return [modules[str(name)] for name in names]

    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:S1",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "loader": workerLoader,
            "onError": lambda _error: None,
        }
    )
    finished(worker.close)
    worker.install(modules["greeter"])
    await tick()
    # A reload starts before the reconciliation committed; the worker is not one of its participants.
    blocks["server"] = True
    modules["greeter"] = greeterFacet("v2")
    reloading = server.reload(["server-plugin"], {"remote": {"session:": ["greeter"]}})
    blocks["worker"] = False
    workerLoading.open()
    for _ in range(50):
        if worker.codeVersion == 2:
            break
        await tick()
    assert worker.codeVersion == 2
    serverLoading.open()
    report = await reloading
    assert list((report.get("children") or {}).keys()) == []
    assert report["codeVersion"] == 3
    greeter = server.hold(Greeter, {"session": "S1"})
    assert await greeter.greet("a") == "v2 a"
    assert worker.codeVersion == server.codeVersion


async def test_a_child_behind_whose_hello_waits_for_a_running_reload_is_fenced_at_once_its_calls_up_are_admitted_spec_9(
    finished,
):
    pingFacet = Facet("ping", lambda api: api.serve(Ping, lambda *_: {"variant": "main", "ping": lambda _ctx: "pong"}))
    loading = Latch()

    async def loader(names: Any) -> list[Facet]:
        await loading.promise
        return _emptyFacets(names)

    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None, "loader": loader})
    finished(server.close)
    server.install(pingFacet)
    await server.reload([pingFacet])
    assert server.previousCodeVersion is None, "code version 1 is retired"
    reloading = server.reload(["server-plugin"], {"remote": {"session:": []}})
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "w"}, "kind": "host", "name": "session:S1"})
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:S1",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    finished(worker.close)

    # While the server's reload still loads its modules, a worker root (code version 1) calls the server.
    async def call(ctx: Any) -> str:
        return await (await ctx.get(Ping, {})).ping(ctx)

    answer = await worker.root(call)
    assert answer == "pong"
    loading.open()
    await reloading
    for _ in range(50):
        if worker.codeVersion == server.codeVersion:
            break
        await tick()
    assert worker.codeVersion == server.codeVersion


async def test_a_reconnecting_child_behind_its_parent_still_retaining_its_previous_code_version_reconciles_at_once(
    finished,
):
    # Upstream title: "... reconciles without waiting out the retire deadline".
    modules = {"greeter": greeterFacet("v1")}
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    finished(server.close)
    down, up = memoryLinkPair()
    # The `retire` frame is lost: the worker keeps code version 1, not permitted to retire it.
    server.accept(
        dropping(down, lambda frame: frame["t"] == "retire"),
        {"principal": {"worker": "w"}, "kind": "host", "name": "session:S1"},
    )
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:S1",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "loader": modulesLoader(modules),
            "onError": lambda _error: None,
        }
    )
    finished(worker.close)
    worker.install(modules["greeter"])
    await tick()
    modules["greeter"] = greeterFacet("v2")
    await server.reload([], {"remote": {"session:": ["greeter"]}})
    await tick()
    assert worker.previousCodeVersion == 1
    up.close("gone")
    await tick()
    # The server moves on while the worker is away, then the worker reconnects behind it.
    modules["greeter"] = greeterFacet("v3")
    await server.reload([Facet("server-plugin", lambda _api: None)])
    down2, up2 = memoryLinkPair()
    server.accept(down2, {"principal": {"worker": "w"}, "kind": "host", "name": "session:S1"})
    started = time.monotonic()
    worker.connect(up2, {"principal": {"kind": "server"}})
    for _ in range(400):
        if worker.codeVersion == server.codeVersion:
            break
        await asyncio.sleep(0.005)
    assert worker.codeVersion == server.codeVersion
    elapsed = time.monotonic() - started
    assert elapsed < 2, f"reconciled after {elapsed * 1000:.0f} ms"


async def test_forced_retirement_cancels_a_browser_call_relayed_to_a_worker_before_the_server_closes_the_old_codes_resources(
    finished,
):
    log: list[str] = []
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    finished(server.close)
    down, up = memoryLinkPair()
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:S1",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    finished(worker.close)
    never = Latch()
    Slow = remote(
        "t.cluster-slow",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("session:S1"),
            "variants": {"main": {"methods": {"wait": method()}}},
        },
    )

    async def wait(ctx: Any) -> None:
        ctx.signal.addEventListener("abort", lambda: log.append("worker aborted"))
        await never.promise

    worker.serve(Slow, lambda *_: {"variant": "main", "wait": wait})

    # Records the cancel the server sends down: the guarantee is that it leaves before the old resources close.
    def watch(frame: Any) -> bool:
        if frame["t"] == "cancel":
            log.append("cancel sent")
        return False

    watched = dropping(down, watch)

    async def resolve(_name: str) -> Any:
        return watched

    server.route("session:", {"authorize": lambda _p, _h: True, "resolve": resolve, "principal": lambda name: {"host": name}})
    Store = local("t.cluster-store", {"version": 1, "input": Type.Object({})})

    def storeFacet(version: int) -> Facet:
        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            self.resource(
                "conn",
                {"version": version, "open": lambda _options: {}, "close": lambda _value: log.append(f"server closes {version}")},
            )
            return {}

        return Facet("store", lambda api: api.serve(Store, factory, {"lifetime": "host"}))

    await server.install(storeFacet(1))
    browserDown, browserUp = memoryLinkPair()
    server.accept(browserDown, {"principal": {"guest": "g"}})
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": browserUp, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    finished(browser.close)
    # The relayed call's root runs on the server's code version 1.
    call = browser.hold(Slow, {}).wait()
    call.add_done_callback(lambda done: done.cancelled() or done.exception())
    for _ in range(50):
        if len(log) != 0 or worker.codeVersion:
            break
        await tick()
    await asyncio.sleep(0.01)
    await server.reload([storeFacet(2)])
    await server.reload([storeFacet(3)], {"retireDeadline": 20})
    done, _ = await asyncio.wait({call}, timeout=2)
    settled = "pending" if not done else "rejected" if call.exception() is not None else "resolved"
    assert settled == "rejected", "the browser's call is cancelled with its root"
    for _ in range(50):
        if "worker aborted" in log:
            break
        await tick()
    assert [entry for entry in log if entry in ("cancel sent", "server closes 1")] == ["cancel sent", "server closes 1"]
    assert "worker aborted" in log
    never.open()
