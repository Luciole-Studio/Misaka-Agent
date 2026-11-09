"""pi chord's ``test/requests.test.ts``: regression tests for resolved application requests (#1-#10), numbered as in
their test names.

MISAKA adaptations: ``setImmediate`` ticks are a 1 ms sleep (a turn that lets every queued callback run, as a Node
macrotask runs after the microtasks); objects with methods that upstream writes as literals (a watch adapter, an event
source) are ``SimpleNamespace``\\ s, since Chord reads ``subscribe`` as an attribute; a void method resolves to
``UNDEFINED``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.chord import (
    createHost,
    defineFacet,
    defineRegistry,
    hostNamed,
    isChordError,
    local,
    memoryLinkPair,
    method,
    opaque,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.delta import applyImmutable
from misaka.chord.host import Facet
from misaka.utils.js import UNDEFINED


async def tick() -> None:
    await asyncio.sleep(0.001)


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


async def until(check, attempts: int = 100) -> None:
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


def noop(*_: Any) -> None:
    return None


View = Type.Object({"text": Type.String()})


async def test_1_a_held_objects_state_member_can_be_returned_as_this_objects_state_member():
    Conv = remote(
        "t.conv",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"state": {"view": View}, "methods": {"set": method(Type.String())}}},
        },
    )
    Chat = remote(
        "t.chat",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"viewer": {"state": {"view": View}}},
        },
    )
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": noop})

    def conv(_input, _principal, self):
        view = self.state("view", View, {"text": ""})

        def set(text, ctx):
            view.replace(ctx, {"text": text})

        return {"variant": "main", "view": view, "set": set}

    server.serve(Conv, conv)
    # No derivation, no cast: the reference member is the implementation member.

    def chat(_input, _principal, self):
        held = self.get(Conv, {})
        return {"variant": "viewer", "view": held.view}

    server.serve(Chat, chat)
    chatRef = server.hold(Chat, {})
    values: list[Any] = []
    chatRef.view.subscribe(values.append)
    await chatRef.ready
    await server.hold(Conv, {}).set("hi")
    await tick()
    assert chatRef.view.value == {"text": "hi"}
    assert values[-1] == {"text": "hi"}

    # Across a link: the snapshot and its ops reach a browser.
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"guest": "g"}})
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": noop,
        }
    )
    remoteChat = browser.hold(Chat, {})
    await remoteChat.ready
    await server.hold(Conv, {}).set("there")

    def arrived() -> bool:
        value = remoteChat.view.value
        return isinstance(value, dict) and value.get("text") == "there"

    await until(arrived)


async def test_2_facets_reach_their_own_host_through_facetapi_root_and_facetapi_registry():
    Config = defineRegistry("t.config")
    Settings = local("t.settings", {"version": 1, "input": Type.Object({})})
    Reader = local(
        "t.reader",
        {"version": 1, "input": Type.Object({}), "members": {"read": method(None, Type.String())}},
    )
    captured: list[Any] = []

    def facet(value: str) -> Facet:
        def setup(api):
            captured.append(api)

            def settings(_input, _principal, self):
                self.register(Config, {"greeting": value})
                return {}

            api.serve(Settings, settings, {"lifetime": "host"})
            api.serve(Reader, lambda *_: {"read": lambda ctx: api.registry(Config).get("greeting", ctx) or ""})

        definition = defineFacet(setup)
        return Facet("config", definition.setup, definition.uses)

    host = createHost({"self": {"kind": "host"}, "name": "worker", "onError": noop})
    host.install(facet("v1"))
    await sleep(5)
    api = captured[0]
    assert api.name == "worker"

    # A callback outside Chord (a Durable tool, say) starts its own root.
    async def read(ctx):
        return await (await ctx.get(Reader, {})).read(ctx)

    assert await api.root(read) == "v1"
    await host.reload([facet("v2")])
    await sleep(5)
    assert captured[1].registry(Config).get("greeting") == "v2"
    assert await api.root(read) == "v2"


async def test_3_a_listener_keeps_its_subscription_across_reloads_when_resource_and_select_are_unchanged():
    Watcher = local(
        "t.watcher",
        {"version": 1, "input": Type.Object({}), "members": {"seen": Type.Array(Type.String())}},
    )
    # A source that accepts one subscriber at a time (like a Durable watch).
    state = {"subscribers": 0, "emit": noop}

    def startWatch(fn):
        if state["subscribers"] > 0:
            raise RuntimeError("Watch is already started")
        state["subscribers"] += 1
        state["emit"] = fn

        def stop():
            state["subscribers"] -= 1

        return stop

    single = SimpleNamespace(start=startWatch)

    def facet(tag: str) -> Facet:
        def setup(api):
            def watcher(_input, _principal, self):
                seen = self.state("seen", Type.Array(Type.String()), [])
                watchResource = self.resource("watch", {"version": 1, "open": lambda _options: single})
                # A new adapter object per call, and a new function per factory run.
                self.on(
                    "events",
                    watchResource,
                    lambda watch: SimpleNamespace(subscribe=lambda fn: watch.start(fn)),
                    lambda event, ctx: seen.change(ctx, lambda draft: draft.push(f"{tag}:{event}") and None),
                )
                return {"seen": seen}

            api.serve(Watcher, watcher)

        return Facet("watcher", setup)

    host = createHost({"self": {"kind": "host"}, "onError": noop})
    host.install(facet("v1"))
    ref = host.hold(Watcher, {})
    await ref.ready
    state["emit"]("a")
    await host.reload([facet("v2")])
    await host.reload([facet("v3")])
    state["emit"]("b")
    await sleep(5)
    assert state["subscribers"] == 1
    assert ref.seen.value == ["v1:a", "v3:b"]


async def test_3_a_changed_select_resubscribes():
    Watcher = local("t.watcher2", {"version": 1, "input": Type.Object({})})
    counts = {"subscribes": 0}

    def subscribe(_listener):
        counts["subscribes"] += 1
        return noop

    source = SimpleNamespace(subscribe=subscribe)

    def facet(selectText: bool) -> Facet:
        def setup(api):
            def watcher(_input, _principal, self):
                sResource = self.resource("s", {"version": 1, "open": lambda _options: source})
                self.on("s", sResource, (lambda s: s) if selectText else (lambda value: value), noop)
                return {}

            api.serve(Watcher, watcher)

        return Facet("watcher2", setup)

    host = createHost({"self": {"kind": "host"}, "onError": noop})
    host.install(facet(True))
    await host.hold(Watcher, {}).ready
    await host.reload([facet(True)])
    assert counts["subscribes"] == 1
    await host.reload([facet(False)])
    assert counts["subscribes"] == 2


async def test_4_replace_diffs_a_new_revision_that_shares_structure_unrelated_values_replace():
    Mirror = local(
        "t.mirror",
        {
            "version": 1,
            "input": Type.Object({}),
            "members": {"doc": Type.Unknown(), "set": method(Type.Unknown())},
        },
    )
    Doc = Type.Object({"entries": Type.Array(Type.String()), "status": Type.String()})
    last: dict[str, Any] = {"options": None}
    host = createHost({"self": {"kind": "host"}, "onError": noop})

    def mirror(_input, _principal, self):
        doc = self.state("doc", Doc, {"entries": ["a", "b"], "status": "idle"})

        def set(value, ctx):
            doc.replace(ctx, value, last["options"])

        return {"doc": doc, "set": set}

    host.serve(Mirror, mirror)
    ref = host.hold(Mirror, {})
    await ref.ready
    events: list[Any] = []
    replica: dict[str, Any] = {"value": None}

    def onEvent(event):
        replica["value"] = applyImmutable(replica["value"], event["ops"]) if event["type"] == "ops" else event["value"]
        events.append(event)

    ref.doc.events(onEvent)

    def opsOf(event):
        return event["ops"] if event is not None and event["type"] == "ops" else []

    current = ref.doc.value
    # A new revision reusing `entries`: a diff.
    await ref.set({"entries": current["entries"], "status": "running"})
    assert opsOf(events[-1]) == [["s", ["status"], "running"]]
    # Unrelated containers: one replacement op.
    await ref.set({"entries": ["a", "b", "c"], "status": "running"})
    assert opsOf(events[-1])[0][0] == "r"
    # Forced either way.
    last["options"] = {"diff": True}
    await ref.set({"entries": ["a", "b", "c", "d"], "status": "running"})
    assert opsOf(events[-1])[0][0] != "r"
    last["options"] = {"diff": False}
    shared = ref.doc.value
    await ref.set({"entries": shared["entries"], "status": "done"})
    assert opsOf(events[-1])[0][0] == "r"
    await tick()
    assert replica["value"] == ref.doc.value


async def test_5_roots_of_a_child_that_connected_behind_the_server_are_admitted_in_the_servers_code_version():
    Echo = remote(
        "t.echo",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"methods": {"codeVersion": method(None, Type.Number())}}},
        },
    )
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": noop})
    echoFacet = Facet(
        "echo", lambda api: api.serve(Echo, lambda *_: {"variant": "main", "codeVersion": lambda ctx: ctx.codeVersion})
    )
    server.install(echoFacet)
    await server.reload([echoFacet])
    assert server.codeVersion == 2
    assert server.previousCodeVersion is None

    async def loader(_names):
        return []

    worker = createHost({"self": {"kind": "host"}, "name": "session:S1", "loader": loader, "onError": noop})
    go = Latch()

    # A worker root of code version 1 (an eager object's listener, say) calls the server after connecting.
    async def call(ctx):
        await go.promise
        return await (await ctx.get(Echo, {})).codeVersion(ctx)

    called = worker.root(call)
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "S1"}, "kind": "host", "name": "session:S1"})
    worker.connect(up, {"principal": {"kind": "server"}})
    go.open()
    assert await called == 2
    await until(lambda: worker.codeVersion == 2)
    # Unknown code versions from in-step peers stay superseded.
    with pytest.raises(Exception) as info:
        await server.root(lambda _ctx: 0, {"codeVersion": 1})
    assert isChordError(info.value, "superseded")


async def test_6_self_expose_an_opaque_member_backed_by_a_resource_readable_once_live():
    Store = local(
        "t.store",
        {"version": 1, "input": Type.Object({}), "members": {"db": opaque("t.db@1")}},
    )
    gate = Latch()
    native = SimpleNamespace(queries=[])
    host = createHost({"self": {"kind": "host"}, "onError": noop})

    def store(_input, _principal, self):
        async def open(_options):
            await gate.promise
            return native

        db = self.resource("db", {"version": 1, "open": open})
        return {"db": self.expose(db)}

    host.serve(Store, store)
    ref = host.hold(Store, {})
    assert ref.db is None
    gate.open()
    await ref.ready
    assert ref.db is native

    Other = local("t.other", {"version": 1, "input": Type.Object({})})

    def other(_input, _principal, self):
        self.expose({"key": "x", "value": 1})
        return {}

    host.serve(Other, other)
    with pytest.raises(Exception, match="expose takes a resource declared by this object"):
        await host.hold(Other, {}).ready


async def test_7_a_cluster_reload_reports_what_each_child_prepared():
    Greeter = remote(
        "t.greeter",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("session:S1"),
            "variants": {"main": {"methods": {"greet": method(Type.String(), Type.String())}}},
        },
    )

    def greeter(text: str) -> Facet:
        return Facet(
            "greeter",
            lambda api: api.serve(Greeter, lambda *_: {"variant": "main", "greet": lambda name, _ctx: f"{text} {name}"}),
        )

    modules = {"greeter": greeter("v1")}

    async def loader(names):
        return [modules[str(name)] for name in names]

    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": noop})
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "S1"}, "kind": "host", "name": "session:S1"})
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "session:S1",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "loader": loader,
            "onError": noop,
        }
    )
    worker.install(greeter("v1"))
    await tick()
    ref = server.hold(Greeter, {})
    assert await ref.greet("a") == "v1 a"
    modules["greeter"] = greeter("v2")
    report = await server.reload([], {"remote": {"session:": ["greeter"]}})
    assert report["changed"] == []
    child = report.get("children", {}).get("session:S1")
    assert child["changed"] == ["t.greeter"]
    assert len(child["prepared"]) == 1
    assert child["codeVersion"] == report["codeVersion"]


async def test_8_a_held_remote_reference_keeps_its_state_without_listeners_a_later_subscriber_sees_the_current_value():
    Counter = remote(
        "t.count8",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"state": {"n": Type.Number()}, "methods": {"set": method(Type.Number())}}},
        },
    )
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": noop})

    def counter(_input, _principal, self):
        n = self.state("n", Type.Number(), 0)

        def set(value, ctx):
            n.replace(ctx, value)

        return {"variant": "main", "n": n, "set": set}

    server.serve(Counter, counter)
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"guest": "g"}})
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": noop,
        }
    )
    ref = browser.hold(Counter, {})
    await ref.ready
    assert ref.n.value == 0
    stop = ref.n.subscribe(noop)
    await ref.set(1)
    await until(lambda: ref.n.value == 1)
    stop()
    await ref.set(2)
    await until(lambda: ref.n.value == 2)
    seen: list[Any] = []
    again = ref.n.subscribe(seen.append)
    assert seen == [2]
    again()


async def test_9_references_report_status_changes_live_loading_while_a_link_reconnects_gone_released():
    Thing = remote(
        "t.thing9",
        {"version": 1, "input": Type.Object({}), "host": hostNamed("server"), "variants": {"main": {}}},
    )
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": noop})
    server.serve(Thing, lambda *_: {"variant": "main"})
    localThing = server.hold(Thing, {}, {"principal": {"user": "u"}})
    localStatus: list[Any] = []
    localThing.onStatus(lambda status, error: localStatus.append([status, error.code] if status == "gone" else status))
    await localThing.ready
    await tick()
    await server.invalidatePrincipal({"user": "u"})
    await tick()
    assert localStatus == ["live", ["gone", "gone"]]

    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"guest": "g"}})
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": noop,
        }
    )
    remoteThing = browser.hold(Thing, {})
    remoteStatus: list[str] = []
    remoteThing.onStatus(lambda status, _error: remoteStatus.append(status))
    await remoteThing.ready
    up.close()
    await until(lambda: len(remoteStatus) == 2)
    down2, up2 = memoryLinkPair()
    server.accept(down2, {"principal": {"guest": "g"}})
    browser.connect(up2, {"principal": {"kind": "server"}})
    await until(lambda: len(remoteStatus) == 3)
    remoteThing.release()
    await tick()
    assert remoteStatus == ["live", "loading", "live", "released"]


async def test_10_host_close_disposes_holders_before_what_they_hold_and_before_closing_links():
    log: list[str] = []
    Remote = remote(
        "t.remote10",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"methods": {"note": method(Type.String())}}},
        },
    )
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": noop})
    server.serve(Remote, lambda *_: {"variant": "main", "note": lambda text, _ctx: log.append(f"server: {text}")})
    Proc = local(
        "t.proc",
        {"version": 1, "input": Type.Object({}), "members": {"tracker": opaque("t.tracker@1")}},
    )
    Chat = local("t.chat10", {"version": 1, "input": Type.Object({})})
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"worker": "w"}, "kind": "host", "name": "w"})
    worker = createHost(
        {
            "self": {"kind": "host"},
            "name": "w",
            "upstream": {"link": up, "principal": {"kind": "server"}},
            "onError": lambda error: log.append(f"error: {error}"),
        }
    )

    # Eager: activated (and registered with the host) before the object that holds it.
    def proc(_input, _principal, self):
        tracker = self.resource(
            "tracker", {"version": 1, "open": lambda _options: set(), "close": lambda _value: log.append("proc closed")}
        )
        return {"tracker": self.expose(tracker)}

    worker.serve(Proc, proc, {"lifetime": "host"})
    await sleep(5)

    def chat(_input, _principal, self):
        held = self.get(Proc, {})
        upstream = self.get(Remote, {})

        async def close(_value):
            if held.tracker is not None:
                held.tracker.discard("chat")
            log.append(f"chat closed, proc {held.status}, tracker {'gone' if held.tracker is None else 'ok'}")
            await upstream.note("bye")

        self.resource(
            "session",
            {
                "version": 1,
                "open": lambda _options: held.tracker.add("chat") if held.tracker is not None else None,
                "close": close,
            },
        )
        return {}

    worker.serve(Chat, chat)
    await worker.hold(Chat, {}).ready
    await worker.close()
    assert log == ["chat closed, proc live, tracker ok", "server: bye", "proc closed"]


_ = UNDEFINED
