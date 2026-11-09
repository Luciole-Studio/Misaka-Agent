"""pi chord's ``test/bindings.test.ts``.

Entry 8: one shared binding per host and key; a hold implies the state of its variant (units: shape, then snapshots);
method-only references; origins of client holds; protocol version 4.

MISAKA adaptations: ``tick`` (Node's ``setImmediate``) lets the event loop run a few turns, since memory links deliver
through ``loop.call_soon``; ``sleep(ms)`` is ``asyncio.sleep``. Frames are dicts; ``undefined`` is ``UNDEFINED``. The
``@ts-expect-error`` on reading another variant's member only silences TypeScript; the runtime assertion is ported.
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
    isChordError,
    local,
    memoryLinkPair,
    method,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.host import HostImpl
from misaka.utils.js import UNDEFINED
from tests.fixtures.chord_counting import CountingLink, stateFrames


async def tick() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


async def until(check: Any, attempts: int = 200) -> None:
    for _ in range(attempts):
        if check():
            break
        await sleep(2)
    assert check(), "condition not reached"


NoteSchema = Type.Array(Type.String())
Notes = remote(
    "t.e8-notes",
    {
        "version": 1,
        "input": Type.Object({"list": Type.Optional(Type.String({"default": "main"}))}),
        "host": hostNamed("server"),
        "variants": {
            "main": {
                "state": {"notes": NoteSchema, "title": Type.String()},
                "methods": {"add": method(Type.String()), "rename": method(Type.String())},
            },
        },
    },
)


def serveNotes(host: Any) -> None:
    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        notes = self.state("notes", NoteSchema, [])
        title = self.state("title", Type.String(), "untitled")

        def add(text: str, ctx: Any) -> None:
            def push(draft: Any) -> None:
                draft.push(text)

            notes.change(ctx, push)

        def rename(text: str, ctx: Any) -> None:
            title.replace(ctx, text)

        return {"variant": "main", "notes": notes, "title": title, "add": add, "rename": rename}

    host.serve(Notes, factory)


def setup(window: float | None = None, linger: dict[str, Any] | None = None) -> dict[str, Any]:
    """A server serving ``Notes``, and a browser whose end of the link counts what crosses it."""
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveNotes(server)
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"user": "u"}, **({} if window is None else {"window": window})})
    link = CountingLink(up)
    errors: list[Any] = []
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": link, "principal": {"kind": "server"}},
            "onError": errors.append,
            **({} if linger is None else {"linger": linger}),
        }
    )
    return {"server": server, "browser": browser, "link": link, "errors": errors}


# ─── shared bindings: traffic ───


async def test_n_references_to_one_object_one_hold_one_snapshot_per_member_one_frame_per_change_one_release():
    s = setup()
    server, browser, link = s["server"], s["browser"], s["link"]
    Panel = local("t.e8-panel", {"version": 1, "input": Type.Object({}), "members": {"count": Type.Number()}})

    def panel_(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        notes = self.get(Notes, {})
        return {"count": self.derive([notes.notes], lambda notes: len(notes))}

    browser.serve(Panel, panel_)
    a = browser.hold(Notes, {})
    b = browser.hold(Notes, {"list": "main"})
    panel = browser.hold(Panel, {})
    await asyncio.gather(a.ready, b.ready, panel.ready)

    async def viaCtx_(ctx: Any) -> Any:
        ref = await ctx.get(Notes, {})
        return ref.title.value

    viaCtx = await browser.root(viaCtx_)
    assert viaCtx == "untitled"
    assert link.count("sent", "hold") == 1, "one hold for every reference"
    snaps = [frame for frame in stateFrames(link.received) if frame["as"] == "snap"]
    assert sorted(frame["m"] for frame in snaps) == ["notes", "title"]

    before = len(stateFrames(link.received))
    await a.add("milk")
    await until(lambda: len(a.notes.value) == 1 and len(b.notes.value) == 1 and panel.count.value == 1)
    await tick()
    assert len(stateFrames(link.received)) - before == 1, "one delta frame for one change, whatever N"

    sent = len(link.traffic("sent"))
    a.release()
    panel.release()
    await sleep(5)
    assert len(link.traffic("sent")) == sent, "releasing all but one reference sends nothing"
    b.release()
    await until(lambda: link.count("sent", "release") == 1)
    await browser.close()
    await server.close()


async def test_a_held_reference_stays_subscribed_without_listeners_values_are_readable_at_once_after_ready():
    s = setup()
    server, browser = s["server"], s["browser"]
    notes = browser.hold(Notes, {})
    await notes.ready
    assert notes.notes.value == []
    await notes.add("bread")
    await until(lambda: len(notes.notes.value) == 1)
    assert notes.title.value == "untitled"
    await browser.close()
    await server.close()


# ─── shared bindings: linger ───


async def test_a_lingering_binding_keeps_its_hold_and_state_a_reference_taken_meanwhile_costs_nothing_on_the_link():
    s = setup(linger={"types": {Notes.name: 40}})
    server, browser, link = s["server"], s["browser"], s["link"]
    first = await browser.get(Notes, {})
    frames = len(link.traffic("received"))
    first.release()
    await sleep(10)
    assert link.count("sent", "release") == 0, "not released before the linger"
    # A hook mounting and unmounting: no frames either way.
    for _ in range(3):
        again = await browser.get(Notes, {})
        assert again.notes.value == []
        again.release()
    assert link.count("sent", "hold") == 1
    assert len(link.traffic("received")) == frames, "nothing new crossed the link"
    await until(lambda: link.count("sent", "release") == 1)
    await browser.close()
    await server.close()


async def test_at_most_cap_bindings_linger_the_least_recently_released_goes_first():
    s = setup(linger={"types": {Notes.name: 10_000}, "cap": 1})
    server, browser, link = s["server"], s["browser"], s["link"]
    a = await browser.get(Notes, {"list": "a"})
    b = await browser.get(Notes, {"list": "b"})
    a.release()
    await sleep(5)
    assert link.count("sent", "release") == 0
    b.release()
    await until(lambda: link.count("sent", "release") == 1)
    release = next((frame for frame in link.sent if frame["t"] == "release"), None)
    holdOfA = next((frame for frame in link.sent if frame["t"] == "hold" and frame["input"]["list"] == "a"), None)
    assert (release["id"] if release is not None else 0) == (holdOfA["id"] if holdOfA is not None else -1)
    await browser.close()
    await server.close()


# ─── shared bindings: identity ───


async def test_normalized_equal_inputs_and_equal_principal_keys_share_other_principals_and_fingerprints_do_not():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    NotesV2 = remote(
        "t.e8-notes",
        {
            "version": 2,
            "input": Type.Object({"list": Type.Optional(Type.String({"default": "main"}))}),
            "host": hostNamed("server"),
            "variants": {
                "main": {
                    "state": {"notes": NoteSchema, "title": Type.String()},
                    "methods": {"add": method(Type.String()), "rename": method(Type.String()), "clear": method()},
                },
            },
        },
    )

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        notes = self.state("notes", NoteSchema, [])
        title = self.state("title", Type.String(), "untitled")
        return {
            "variant": "main",
            "notes": notes,
            "title": title,
            "add": lambda args, ctx: None,
            "rename": lambda args, ctx: None,
            "clear": lambda ctx: None,
        }

    server.serve(NotesV2, factory, {"accepts": {1: lambda current: current}})
    # A worker holds the server's objects; the principals it forwards key its bindings.
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "w"}, "kind": "host", "name": "session:w"})
    link = CountingLink(up)
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:w",
            "upstream": {"link": link, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    refs = [
        worker.hold(Notes, {}),
        worker.hold(Notes, {"list": "main"}),
        worker.hold(Notes, {}, {"principal": {"user": "a", "grant": 1}}),
        worker.hold(Notes, {}, {"principal": {"grant": 1, "user": "a"}}),
        worker.hold(Notes, {}, {"principal": {"user": "a", "grant": 2}}),
        worker.hold(NotesV2, {}),
    ]
    await asyncio.gather(*[ref.ready for ref in refs])
    # host (shared by two inputs), user a grant 1 (shared by two key orders), user a grant 2, the v2 contract.
    assert link.count("sent", "hold") == 4
    await worker.close()
    await server.close()


# ─── hydration over a link ───

Matrix = remote(
    "t.e8-matrix",
    {
        "version": 1,
        "input": Type.Object({"variant": Type.String()}),
        "host": hostNamed("server"),
        "variants": {
            "values": {"state": {"none": Type.Null(), "empty": Type.Array(Type.String()), "maybe": Type.Unknown()}},
            "bare": {"methods": {"ping": method(None, Type.String())}},
            "broken": {"state": {"n": Type.Number()}},
        },
    },
)


def serveMatrix(host: Any) -> None:
    def factory(input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        variant = input["variant"]
        if variant == "bare":
            return {"variant": "bare", "ping": lambda ctx: "pong"}
        if variant == "broken":
            source = self.state("source", Type.String(), "not a number")
            # A derivation exposed under a number schema: its snapshot fails validation.
            return {"variant": "broken", "n": self.derive([source], lambda text: text)}
        none = self.state("none", Type.Null(), None)
        empty = self.state("empty", Type.Array(Type.String()), [])
        return {"variant": "values", "none": none, "empty": empty, "maybe": self.derive([none], lambda _none: UNDEFINED)}

    host.serve(Matrix, factory)


async def test_null_empty_and_undefined_values_hydrate_a_state_free_variant_is_ready_a_failed_snapshot_rejects():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    serveMatrix(server)
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"user": "u"}})
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    values = await browser.get(Matrix, {"variant": "values"})
    assert values.variant == "values"
    assert values.none.value is None
    assert values.empty.value == []
    assert values.maybe.value is UNDEFINED
    bare = await browser.get(Matrix, {"variant": "bare"})
    assert bare.variant == "bare"
    assert await bare.ping() == "pong"
    # `none` is not a member of the bare variant.
    with pytest.raises(ChordError) as info:
        bare.none.value  # noqa: B018 - reading is the test
    assert isChordError(info.value, "not_in_variant")
    broken = browser.hold(Matrix, {"variant": "broken"})
    with pytest.raises(ChordError) as rejected:
        await broken.ready
    assert isChordError(rejected.value, "invalid")
    assert broken.status == "gone"
    await browser.close()
    await server.close()


async def test_reading_before_the_first_hydration_throws_not_ready():
    s = setup()
    server, browser = s["server"], s["browser"]
    notes = browser.hold(Notes, {})
    with pytest.raises(ChordError) as info:
        notes.notes.value  # noqa: B018 - reading is the test
    assert isChordError(info.value, "not_ready")
    await notes.ready
    assert notes.notes.value == []
    await browser.close()
    await server.close()


# ─── method-only references (state: false) ───


async def test_transfer_no_state_alone_a_full_reference_joining_starts_it_once_its_release_stops_it():
    s = setup()
    server, browser, link = s["server"], s["browser"], s["link"]
    caller = browser.hold(Notes, {}, {"state": False})
    await caller.ready
    await caller.add("x")
    hold = next((frame for frame in link.sent if frame["t"] == "hold"), None)
    assert hold is not None and hold.get("state") is False
    assert stateFrames(link.received) == []
    with pytest.raises(ChordError) as info:
        caller.notes.value  # noqa: B018 - reading is the test
    assert isChordError(info.value, "not_ready")

    viewer = browser.hold(Notes, {})
    await viewer.ready
    assert viewer.notes.value == ["x"]
    assert link.count("sent", "sub") == 1
    assert link.count("sent", "hold") == 1, "the same hold"
    viewer.release()
    await until(lambda: link.count("sent", "unsub") == 1)
    after = len(stateFrames(link.received))
    await caller.add("y")
    await sleep(5)
    assert len(stateFrames(link.received)) == after, "no state once only method-only references remain"
    await browser.close()
    await server.close()


# ─── origins of client holds ───

Counter = remote(
    "t.e8-counter",
    {
        "version": 1,
        "input": Type.Object({"session": Type.String()}),
        "host": hostFamily("session:", "session"),
        "variants": {"main": {"state": {"n": Type.Number()}, "methods": {"add": method(Type.Number())}}},
    },
)


def cluster() -> dict[str, Any]:
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    state: dict[str, Any] = {"worker": None, "toWorker": None}

    async def resolve(name: str) -> Any:
        if state["toWorker"] is not None:
            return state["toWorker"]
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
        state["worker"] = created
        state["toWorker"] = CountingLink(down)
        return state["toWorker"]

    server.route(
        "session:",
        {"principal": lambda name: {"host": name}, "authorize": lambda _principal, _host: True, "resolve": resolve},
    )

    def browser() -> dict[str, Any]:
        down, up = memoryLinkPair()
        server.accept(down, {"principal": {"guest": "g"}})
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

    def origins() -> list[Any]:
        worker = state["worker"]
        upstream = None if worker is None else worker.upstream
        served = [] if upstream is None else list(upstream.served.values())
        return [entry.origin if entry.kind == "local" else None for entry in served]

    def sentToWorker() -> list[Any]:
        return [] if state["toWorker"] is None else state["toWorker"].sent

    return {"server": server, "browser": browser, "origins": origins, "sentToWorker": sentToWorker}


async def test_a_relayed_hold_carries_its_client_connections_id_the_servers_own_holds_carry_none():
    c = cluster()
    one = c["browser"]()
    two = c["browser"]()
    await one["host"].hold(Counter, {"session": "S1"}).ready
    await two["host"].hold(Counter, {"session": "S1"}).ready
    own = c["server"].hold(Counter, {"session": "S1"})
    await own.ready
    holds = [frame for frame in c["sentToWorker"]() if frame["t"] == "hold"]
    assert len(holds) == 3
    relayed = [frame for frame in holds if frame.get("origin") is not None]
    assert len(relayed) == 2
    assert relayed[0]["origin"] != relayed[1]["origin"], "two tabs, two origins"
    assert len([frame for frame in holds if frame.get("origin") is None]) == 1

    # Disconnect is departure: the worker loses that origin at once; a reconnect holds under a new id.
    before = set(c["origins"]())
    one["up"].close()
    await until(lambda: len([origin for origin in c["origins"]() if origin is not None]) == 1)
    three = c["browser"]()
    await three["host"].hold(Counter, {"session": "S1"}).ready
    after = [origin for origin in c["origins"]() if origin is not None]
    assert len(after) == 2
    assert any(origin not in before for origin in after), "a new connection gets a new id"
    await c["server"].close()


async def test_an_origin_a_client_sends_is_ignored_the_server_stamps_its_own():
    c = cluster()
    down, raw = memoryLinkPair()
    c["server"].accept(down, {"principal": {"guest": "g"}})
    raw.receive(lambda _frame: None)
    raw.send({"t": "hello", "protocol": PROTOCOL_VERSION, "name": "raw", "kind": "client", "codeVersion": 0})
    raw.send(
        {
            "t": "hold",
            "id": 1,
            "at": "session:S1",
            "token": Counter.description,
            "input": {"session": "S1"},
            "origin": "spoofed",
        }
    )
    await until(lambda: any(frame["t"] == "hold" for frame in c["sentToWorker"]()))
    hold = next((frame for frame in c["sentToWorker"]() if frame["t"] == "hold"), None)
    assert hold is not None and hold.get("origin") is not None and hold["origin"] != "spoofed"
    await c["server"].close()


# ─── protocol version 4 ───


async def test_a_version_3_peer_is_refused_shapes_carry_no_code_version():
    s = setup()
    server, browser, link = s["server"], s["browser"], s["link"]
    await browser.hold(Notes, {}).ready
    shape = next((frame for frame in link.received if frame["t"] == "shape"), None)
    assert shape is not None and "codeVersion" not in shape
    assert sorted(shape["state"]) == ["notes", "title"]

    down, raw = memoryLinkPair()
    server.accept(down, {"principal": {"user": "old"}})
    closed: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    raw.onClose(lambda reason: closed.done() or closed.set_result(reason))
    raw.receive(lambda _frame: None)
    raw.send({"t": "hello", "protocol": 3, "name": "old", "kind": "client", "codeVersion": 0})
    assert await asyncio.wait_for(closed, 5) == f"protocol-version {PROTOCOL_VERSION}"
    assert PROTOCOL_VERSION == 4
    await browser.close()
    await server.close()
