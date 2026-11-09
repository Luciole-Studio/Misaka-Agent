"""pi chord's ``test/link.test.ts``.

MISAKA adaptations: ``tick`` (Node's ``setImmediate``) lets the event loop run a few turns, since a memory link delivers
one frame per ``loop.call_soon`` step (upstream: one promise chain step per frame); ``sleep(ms)`` is ``asyncio.sleep``. Frames are dicts; ``undefined`` is ``UNDEFINED``. The
IPC case's ``process.nextTick`` is ``loop.call_soon``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.chord import (
    PROTOCOL_VERSION,
    ChordError,
    createHost,
    hostFamily,
    hostNamed,
    ipcLink,
    isChordError,
    memoryLinkPair,
    method,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.link import Packed, unpack
from misaka.utils.js import UNDEFINED


async def tick() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


counterVariants = {
    "main": {
        "state": {"count": Type.Object({"n": Type.Number()})},
        "methods": {
            "add": method(Type.Number(), Type.Number()),
            "who": method(None, Type.Unknown()),
        },
    },
}
# A counter on the server.
Counter = remote(
    "t.counter",
    {
        "version": 1,
        "input": Type.Object({"name": Type.String()}),
        "host": hostNamed("server"),
        "variants": counterVariants,
    },
)
# A counter on a session host (`session:` + its `session` field).
SessionCounter = remote(
    "t.session-counter",
    {
        "version": 1,
        "input": Type.Object({"session": Type.String(), "name": Type.String()}),
        "host": hostFamily("session:", "session"),
        "variants": counterVariants,
    },
)


def serveCounter(host: Any, principals: list[Any] | None = None) -> None:
    """Serves the counter of the host's kind (``Counter`` on the server, ``SessionCounter`` elsewhere) and records the
    principal of every object it creates."""
    principals = [] if principals is None else principals

    def counter(principal: Any, self: Any) -> dict[str, Any]:
        principals.append(principal)
        count = self.state("count", Type.Object({"n": Type.Number()}), {"n": 0})

        def add(delta: float, ctx: Any) -> Any:
            def bump(draft: Any) -> None:
                draft["n"] += delta

            count.change(ctx, bump)
            return count.value["n"]

        return {"variant": "main", "count": count, "add": add, "who": lambda ctx: principal}

    if host.name == "server":
        host.serve(Counter, lambda _input, principal, self: counter(principal, self))
    else:
        host.serve(SessionCounter, lambda _input, principal, self: counter(principal, self))


def serverWithWorker(workerName: str = "session:S1") -> dict[str, Any]:
    """A server and a worker connected by a memory link; the server accepts the worker as ``{"worker": name}``."""
    errors: list[Any] = []
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": errors.append})
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": workerName}, "kind": "host", "name": workerName})
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": workerName,
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": errors.append,
        }
    )
    return {"server": server, "worker": worker, "down": down, "up": up, "errors": errors}


# ─── links: calls and state across hosts ───


async def test_a_child_calls_the_server_the_server_stamps_the_childs_principal():
    pair = serverWithWorker()
    server, worker = pair["server"], pair["worker"]
    principals: list[Any] = []
    serveCounter(server, principals)

    async def run(ctx: Any) -> list[Any]:
        counter = await ctx.get(Counter, {"name": "c"})
        assert counter.variant == "main"
        return [await counter.add(2, ctx), await counter.who(ctx)]

    result = await worker.root(run)
    assert result == [2, {"worker": "session:S1"}]
    assert principals == [{"worker": "session:S1"}]


async def test_state_members_snapshot_then_delta_encoded_ops_values_decode_to_the_providers():
    pair = serverWithWorker()
    server, worker = pair["server"], pair["worker"]
    serveCounter(server)
    counter = worker.hold(Counter, {"name": "s"})
    events: list[Any] = []
    values: list[Any] = []
    counter.count.events(events.append)
    counter.count.subscribe(values.append)
    await counter.ready
    await counter.add(1)
    await counter.add(1)
    await tick()
    assert counter.count.value == {"n": 2}
    # Listeners added before the hydration get the snapshot then (not `undefined` first).
    assert [event["type"] for event in events] == ["snap", "ops", "ops"]
    assert values == [{"n": 0}, {"n": 1}, {"n": 2}]


async def test_the_server_reaches_a_connected_child_by_name():
    pair = serverWithWorker("session:w1")
    server, worker = pair["server"], pair["worker"]
    principals: list[Any] = []
    serveCounter(worker, principals)
    await tick()
    counter = server.hold(SessionCounter, {"session": "w1", "name": "x"})
    assert await counter.add(5) == 5
    # The parent's own requests act as the principal the child gave `connect`.
    assert principals == [{"kind": "server"}]


async def test_rejects_local_types_and_unknown_hosts():
    worker = serverWithWorker()["worker"]
    missing = worker.hold(Counter, {"name": "m"})
    with pytest.raises(ChordError) as info:
        await missing.ready
    assert isChordError(info.value, "unavailable")


# ─── links: relay, stamping, lifetime ───


class Cluster:
    """Server routing ``session:*`` to workers it spawns on demand (memory links); browsers connect as clients."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self.options = options or {}
        self.log: list[str] = []
        self.server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
        self.workers: dict[str, Any] = {}
        self.allow = lambda principal, host: True
        log = self.log

        async def resolve(name: str) -> Any:
            down, up = memoryLinkPair()
            worker = createHost(
                {
                    "self": {"kind": "host"},
                    "name": name,
                    "upstream": {"link": up, "principal": {"kind": "server"}, **self.options},
                    "onError": lambda _error: None,
                }
            )

            def factory(input: Any, principal: Any, self_: Any) -> dict[str, Any]:
                self_.resource(
                    "probe",
                    {
                        "version": 1,
                        "open": lambda _options: log.append(f"open {input['name']}") and None,
                        "close": lambda _value: log.append(f"close {input['name']}") and None,
                    },
                )
                count = self_.state("count", Type.Object({"n": Type.Number()}), {"n": 0})

                def add(delta: float, ctx: Any) -> Any:
                    def bump(draft: Any) -> None:
                        draft["n"] += delta

                    count.change(ctx, bump)
                    return count.value["n"]

                return {"variant": "main", "count": count, "add": add, "who": lambda ctx: principal}

            worker.serve(SessionCounter, factory)
            self.workers[name] = worker
            return down

        self.server.route(
            "session:",
            {
                "principal": lambda name: {"host": name},
                "authorize": lambda principal, host: self.allow(principal, host),
                "resolve": resolve,
            },
        )

    def browser(self, principal: Any, restamp: Any = None) -> Any:
        down, up = memoryLinkPair()
        self.server.accept(down, {"principal": principal, "restamp": restamp, **self.options})
        host = createHost(
            {
                "self": {"kind": "host"},
                "name": "browser",
                "kind": "client",
                "upstream": {"link": up, "principal": {"kind": "server"}, **self.options},
                "onError": lambda _error: None,
            }
        )
        server = self.server

        class Browser:
            def __init__(self) -> None:
                self.host = host
                self.up = up

            def reconnect(self) -> None:
                nextDown, nextUp = memoryLinkPair()
                server.accept(nextDown, {"principal": principal})
                self.up = nextUp
                host.connect(nextUp, {"principal": {"kind": "server"}})

        return Browser()


async def test_relays_browser_server_worker_the_worker_sees_the_browsers_stamped_principal():
    c = Cluster()
    guest = {"guest": "g1"}
    browser = c.browser(guest)
    counter = browser.host.hold(SessionCounter, {"session": "S1", "name": "a"})
    assert await counter.add(3) == 3
    assert await counter.who() == guest
    values: list[Any] = []
    counter.count.subscribe(values.append)
    await counter.add(1)
    await tick()
    assert values[-1] == {"n": 4}


async def test_ignores_payload_principals_from_clients_and_from_child_hosts():
    c = Cluster()
    guest = {"guest": "g1"}
    down, raw = memoryLinkPair()
    c.server.accept(down, {"principal": guest})
    frames: list[Any] = []
    raw.receive(frames.append)
    raw.send({"t": "hello", "protocol": PROTOCOL_VERSION, "name": "raw", "kind": "client", "codeVersion": 0})
    raw.send(
        {
            "t": "hold",
            "id": 1,
            "at": "session:S1",
            "token": SessionCounter.description,
            "input": {"session": "S1", "name": "f"},
            "principal": {"owner": True},
        }
    )
    raw.send({"t": "call", "id": 2, "hold": 1, "member": "who", "args": None})
    for _ in range(50):
        if any(frame["t"] == "result" for frame in frames):
            break
        await sleep(5)
    result = next((frame for frame in frames if frame["t"] == "result"), None)
    assert (result or {}).get("value", UNDEFINED) == guest

    # A child host cannot forward principals upward either: the server stamps it.
    pair = serverWithWorker("w9")
    server, up = pair["server"], pair["up"]
    serveCounter(server)
    childFrames: list[Any] = []
    up.receive(childFrames.append)
    up.send(
        {
            "t": "hold",
            "id": 7,
            "at": "server",
            "token": Counter.description,
            "input": {"name": "f"},
            "principal": {"owner": True},
        }
    )
    up.send({"t": "call", "id": 8, "hold": 7, "member": "who", "args": None})
    for _ in range(50):
        if any(frame["t"] == "result" for frame in childFrames):
            break
        await sleep(5)
    childResult = next((frame for frame in childFrames if frame["t"] == "result"), None)
    assert (childResult or {}).get("value", UNDEFINED) == {"worker": "w9"}


async def test_a_route_that_returns_an_already_closed_link_does_not_poison_the_host_name():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    attempt = [0]

    async def resolve(name: str) -> Any:
        attempt[0] += 1
        down, up = memoryLinkPair()
        worker = createHost(
            {
                "self": {"kind": "host"},
                "name": name,
                "upstream": {"link": up, "principal": {"kind": "server"}},
                "onError": lambda _error: None,
            }
        )
        serveCounter(worker)
        # The first worker dies before the server wraps its link.
        if attempt[0] == 1:
            down.close("worker exited")
        return down

    server.route(
        "session:", {"principal": lambda name: {"host": name}, "authorize": lambda principal, host: True, "resolve": resolve}
    )
    first = server.hold(SessionCounter, {"session": "S1", "name": "a"})
    with pytest.raises(ChordError, match="closed"):
        await first.add(1)
    # The next call resolves the route again and reaches the new worker.
    second = server.hold(SessionCounter, {"session": "S1", "name": "a"})
    assert await second.add(2) == 2
    assert attempt[0] == 2


async def test_authorizes_before_resolving_and_again_after():
    c = Cluster()
    c.allow = lambda principal, host: principal["guest"] != "blocked"
    blocked = c.browser({"guest": "blocked"})
    with pytest.raises(ChordError) as info:
        await blocked.host.hold(SessionCounter, {"session": "S1", "name": "a"}).ready
    assert isChordError(info.value, "forbidden")
    assert len(c.workers) == 0
    calls = [0]

    def allowOnce(principal: Any, host: str) -> bool:
        calls[0] += 1
        return calls[0] == 1

    c.allow = allowOnce
    flaky = c.browser({"guest": "flaky"})
    with pytest.raises(ChordError) as info:
        await flaky.host.hold(SessionCounter, {"session": "S2", "name": "a"}).ready
    assert isChordError(info.value, "forbidden")
    assert calls[0] == 2


async def test_a_browser_disconnect_releases_exactly_its_holds_the_worker_link_stays_up():
    c = Cluster()
    one = c.browser({"guest": "g1"})
    two = c.browser({"guest": "g2"})
    await one.host.hold(SessionCounter, {"session": "S1", "name": "a"}).add(1)
    other = two.host.hold(SessionCounter, {"session": "S1", "name": "b"})
    await other.add(1)
    one.up.close()
    for _ in range(20):
        if "close a" in c.log:
            break
        await tick()
    assert "close a" in c.log
    assert "close b" not in c.log
    assert await other.add(1) == 2


async def test_a_dead_link_no_pongs_is_closed_and_everything_held_through_it_is_released():
    c = Cluster({"pingInterval": 10, "pingTimeout": 40})
    browser = c.browser({"guest": "g1"})
    await browser.host.hold(SessionCounter, {"session": "S1", "name": "a"}).add(1)
    browser.up.sever()
    for _ in range(40):
        if "close a" in c.log:
            break
        await sleep(10)
    assert "close a" in c.log


async def test_checks_contract_tokens_across_links_contract_mismatch_for_hosts_upgrade_required_for_browsers():
    CounterV2 = remote(
        "t.counter",
        {
            "version": 2,
            "input": Type.Object({"name": Type.String()}),
            "host": hostNamed("server"),
            "variants": {"main": {"methods": {"inc": method(None, Type.Number())}}},
        },
    )
    pair = serverWithWorker()
    server, worker = pair["server"], pair["worker"]
    server.serve(CounterV2, lambda _input, _principal, _self: {"variant": "main", "inc": lambda ctx: 1})
    with pytest.raises(ChordError) as info:
        await worker.hold(Counter, {"name": "x"}).ready
    assert isChordError(info.value, "contract_mismatch")
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
    with pytest.raises(ChordError) as info:
        await browser.hold(Counter, {"name": "x"}).ready
    assert isChordError(info.value, "upgrade_required")


async def test_invalidateprincipal_re_stamps_the_link_and_propagates_to_workers():
    c = Cluster()
    browser = c.browser({"guest": "g1", "revision": 1}, restamp=lambda p: {**p, "revision": 2})
    counter = browser.host.hold(SessionCounter, {"session": "S1", "name": "a"})
    await counter.add(1)
    await c.server.invalidatePrincipal({"guest": "g1"})
    # The relay under the old principal is closed at once; the worker disposes its object after.
    for _ in range(20):
        if counter.status == "gone":
            break
        await tick()
    assert counter.status == "gone"
    for _ in range(20):
        if "close a" in c.log:
            break
        await tick()
    assert "close a" in c.log
    again = browser.host.hold(SessionCounter, {"session": "S1", "name": "a"})
    assert await again.who() == {"guest": "g1", "revision": 2}


async def test_after_a_reconnect_state_subscribers_get_a_reset_and_calls_work_again():
    c = Cluster()
    browser = c.browser({"guest": "g1"})
    counter = browser.host.hold(SessionCounter, {"session": "S1", "name": "r"})
    events: list[str] = []
    counter.count.events(lambda event: events.append(event["type"]))
    await counter.add(2)
    browser.up.close()
    await tick()
    await tick()
    assert counter.status == "loading"
    browser.reconnect()
    assert await counter.add(1) == 1
    await tick()
    # The old hold was released with the link, so the worker made a fresh object; the reset carries its value.
    assert counter.count.value == {"n": 1}
    # The first hydration is the listener's snapshot; the re-hold's snapshot is a reset.
    assert events[0] == "snap"
    assert "reset" in events


Big = remote(
    "t.big",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("session:S1"),
        "variants": {
            "main": {
                "state": {"notes": Type.Array(Type.String())},
                "methods": {"echo": method(Type.Array(Type.String()), Type.Array(Type.String()))},
            },
        },
    },
)


def large(tag: str) -> list[str]:
    """About 160,000 characters of JSON: above PACK_THRESHOLD."""
    return [f"{tag}{i}:{'x' * 4000}" for i in range(40)]


# ─── links: methods without arguments or result ───


async def test_travel_without_args_and_without_a_value_and_resolve_to_the_result_or_undefined():
    Tool = remote(
        "t.tool",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {
                "main": {"methods": {"ping": method(), "count": method(None, Type.Number()), "note": method(Type.String())}}
            },
        },
    )
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    notes: list[str] = []

    def note(text: str, ctx: Any) -> Any:
        notes.append(text)
        return "dropped"

    server.serve(
        Tool,
        lambda _input, _principal, _self: {
            "variant": "main",
            "ping": lambda ctx: None,
            "count": lambda ctx: 2,
            "note": note,
        },
    )
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"guest": "g"}})
    sent: list[Any] = []
    send = up.send

    def recording(frame: Any) -> Any:
        sent.append(frame)
        return send(frame)

    up.send = recording  # type: ignore[method-assign]
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    tool = browser.hold(Tool, {})
    await tool.ready
    assert await tool.ping() is UNDEFINED
    assert await tool.count() == 2
    assert await tool.note("hi") is UNDEFINED
    assert notes == ["hi"]
    calls = [frame for frame in sent if frame["t"] == "call"]
    assert [frame.get("args", UNDEFINED) is not UNDEFINED for frame in calls] == [False, False, True]


# ─── links: protocol version ───


def mismatches() -> tuple[list[Any], Any]:
    seen: list[Any] = []
    return seen, seen.append


async def test_a_peer_without_a_protocol_version_an_old_client_is_told_and_dropped_nothing_it_sends_is_served():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveCounter(server)
    seen, onProtocolMismatch = mismatches()
    down, raw = memoryLinkPair()
    server.accept(down, {"principal": {"guest": "g"}, "onProtocolMismatch": onProtocolMismatch})
    frames: list[Any] = []
    closed: list[str] = []
    raw.receive(frames.append)
    raw.onClose(closed.append)
    raw.send({"t": "hello", "name": "old", "kind": "client", "codeVersion": 0})
    raw.send({"t": "hold", "id": 1, "at": "server", "token": Counter.description, "input": {"name": "c"}})
    await tick()
    assert seen == [{"local": PROTOCOL_VERSION, "peer": 1}]
    assert closed == [f"protocol-version {PROTOCOL_VERSION}"]
    assert down.closed is True
    assert not any(frame["t"] == "shape" for frame in frames), "the old client's hold was not served"


async def test_a_client_learns_that_its_server_speaks_another_version_and_stops():
    seen, onProtocolMismatch = mismatches()
    down, up = memoryLinkPair()
    browser = createHost({"self": {"kind": "host"}, "name": "browser", "kind": "client", "onError": lambda _error: None})
    browser.connect(up, {"principal": {"kind": "server"}, "onProtocolMismatch": onProtocolMismatch})
    # A newer server: its hello announces the next version, and it closes the link the same way Chord does.
    down.send({"t": "hello", "protocol": PROTOCOL_VERSION + 1, "name": "server", "kind": "host", "codeVersion": 1})
    await tick()
    assert seen == [{"local": PROTOCOL_VERSION, "peer": PROTOCOL_VERSION + 1}]
    assert up.closed is True

    # Told by the peer (it noticed first): the close reason carries its version.
    told, onTold = mismatches()
    down2, up2 = memoryLinkPair()
    other = createHost({"self": {"kind": "host"}, "name": "browser", "kind": "client", "onError": lambda _error: None})
    other.connect(up2, {"principal": {"kind": "server"}, "onProtocolMismatch": onTold})
    down2.close(f"protocol-version {PROTOCOL_VERSION + 1}")
    await tick()
    assert told == [{"local": PROTOCOL_VERSION, "peer": PROTOCOL_VERSION + 1}]


async def test_peers_of_the_same_version_talk_an_ordinary_close_is_no_mismatch():
    seen, onProtocolMismatch = mismatches()
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"guest": "g"}, "onProtocolMismatch": onProtocolMismatch})
    browser = createHost({"self": {"kind": "host"}, "name": "browser", "kind": "client", "onError": lambda _error: None})
    browser.connect(up, {"principal": {"kind": "server"}, "onProtocolMismatch": onProtocolMismatch})
    await tick()
    up.close("bye")
    await tick()
    assert seen == []


# ─── links: packed payloads ───


async def test_large_args_results_and_snapshots_cross_a_relay_deflated_and_arrive_intact():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})

    async def resolve(name: str) -> Any:
        down, up = memoryLinkPair()
        worker = createHost(
            {
                "self": {"kind": "host"},
                "name": name,
                "upstream": {"link": up, "principal": {"kind": "server"}},
                "onError": lambda _error: None,
            }
        )

        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            notesState = self.state("notes", Type.Array(Type.String()), large("a"))
            return {"variant": "main", "notes": notesState, "echo": lambda notes, ctx: notes}

        worker.serve(Big, factory)
        return down

    server.route(
        "session:", {"principal": lambda name: {"host": name}, "authorize": lambda principal, host: True, "resolve": resolve}
    )
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"owner": True}})
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    big = browser.hold(Big, {})
    assert await big.echo(large("c")) == large("c")
    big.notes.subscribe(lambda _value: None)
    for _ in range(100):
        try:
            if big.notes.value is not UNDEFINED:
                break
        except ChordError:
            pass
        await sleep(5)
    assert big.notes.value == large("a")


async def test_only_payloads_above_the_threshold_are_packed_the_receiver_unpacks_them():
    a, b = memoryLinkPair()
    frames: list[Any] = []
    b.receive(frames.append)
    a.send({"t": "result", "id": 1, "value": large("v")})
    a.send({"t": "result", "id": 2, "value": ["small"]})
    a.send({"t": "state", "hold": 3, "m": "notes", "as": "snap", "value": large("s")})
    for _ in range(50):
        if len(frames) >= 3:
            break
        await tick()
    packed, small, snap = frames
    assert packed["t"] == "result" and isinstance(packed["value"], Packed)
    assert unpack(packed["value"]) == large("v")
    assert small["t"] == "result"
    assert small["value"] == ["small"]
    assert snap["t"] == "state" and isinstance(snap["value"], Packed)
    assert unpack(snap["value"]) == large("s")

    # A relay forwards the packed bytes as they are.
    c, d = memoryLinkPair()
    forwarded: list[Any] = []
    d.receive(forwarded.append)
    c.send({**packed, "id": 9})
    for _ in range(50):
        if len(forwarded) >= 1:
            break
        await tick()
    relayed = forwarded[0]
    assert relayed["t"] == "result" and isinstance(relayed["value"], Packed)
    assert relayed["value"].bytes == packed["value"].bytes


# ─── links: IPC ───


async def test_a_write_that_fails_after_the_peer_process_exited_epipe_ends_the_link_instead_of_raising():
    # The channel reports such a failure asynchronously: to the send callback, or else as an error nobody handles.
    sent: list[Any] = []
    loop = asyncio.get_running_loop()

    class Channel:
        connected = True

        def send(self, message: Any, callback: Any = None) -> bool:
            sent.append(message)
            error = OSError("write EPIPE")
            error.code = "EPIPE"  # type: ignore[attr-defined]
            if callback is None:
                raise RuntimeError("an IPC send without a callback cannot report EPIPE")
            loop.call_soon(callback, error)
            return False

        def on(self, event: str, listener: Any) -> None:
            return None

        def off(self, event: str, listener: Any) -> None:
            return None

    link = ipcLink(Channel())
    reasons: list[str] = []
    link.onClose(reasons.append)
    link.send({"t": "ping", "n": 1})
    await tick()
    assert link.closed is True
    assert reasons == ["ipc send failed: EPIPE"]
    assert len(sent) == 1
    link.send({"t": "ping", "n": 2})
    assert len(sent) == 1, "a closed link sends nothing more"
