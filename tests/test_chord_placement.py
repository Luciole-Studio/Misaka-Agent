"""pi chord's ``test/placement.test.ts``.

MISAKA adaptations: ``tick`` (Node's ``setImmediate``) lets the event loop run a few turns, since memory links deliver
through ``loop.call_soon``. Frames are dicts. The last case's ``@ts-expect-error`` lines check TypeScript types at
compile time; Python has no compile-time check of a ``hostFamily`` field, so only the runtime assertions are ported.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from misaka.chord import (
    PROTOCOL_VERSION,
    ChordError,
    bind,
    createHost,
    hostFamily,
    hostNamed,
    isChordError,
    memoryLinkPair,
    method,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.host import Facet


async def tick() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


# Lives on `session:` + its input's `session`.
Note = remote(
    "t.note",
    {
        "version": 1,
        "input": Type.Object({"session": Type.String(), "id": Type.String()}),
        "host": hostFamily("session:", "session"),
        "variants": {"main": {"methods": {"where": method(None, Type.String())}}},
    },
)

# Lives on `server`.
Catalog = remote(
    "t.catalog",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("server"),
        "variants": {"main": {"methods": {"where": method(None, Type.String())}}},
    },
)


def cluster() -> dict[str, Any]:
    """A server with two session hosts as children; every host serves what it may and reports its name."""
    server = createHost({"name": "server", "self": "host", "onError": lambda _error: None})
    server.serve(Catalog, lambda _input, _principal, _self: {"variant": "main", "where": lambda ctx: "server"})
    children: dict[str, Any] = {}
    links: dict[str, Any] = {}

    def child(name: str) -> Any:
        down, up = memoryLinkPair()
        links[name] = down
        server.accept(down, {"kind": "host", "name": name, "principal": "server-side"})
        host = createHost(
            {"name": name, "self": "host", "upstream": {"link": up, "principal": "server"}, "onError": lambda _error: None}
        )
        host.serve(
            Note,
            lambda input, _principal, _self: {"variant": "main", "where": lambda ctx: f"{host.name} {input['id']}"},
        )
        children[name] = host
        return host

    s1 = child("session:S1")
    s2 = child("session:S2")

    async def close() -> None:
        for host in children.values():
            await host.close()
        await server.close()

    # Relays need a route (where authorization lives); it finds the connected child.
    async def resolve(name: str) -> Any:
        return links[name]

    server.route(
        "session:",
        {"authorize": lambda _principal, _host: True, "resolve": resolve, "principal": lambda name: name},
    )
    return {"server": server, "s1": s1, "s2": s2, "close": close}


async def test_a_get_goes_where_the_contract_places_the_input_here_a_child_or_the_parent_never_named_by_the_caller():
    c = cluster()
    server, s1 = c["server"], c["s1"]
    await tick()
    assert await server.hold(Note, {"session": "S1", "id": "a"}).where() == "session:S1 a"
    assert await server.hold(Note, {"session": "S2", "id": "b"}).where() == "session:S2 b"
    # From a session host: its own notes locally, the other session's through the server, the catalog upstream.
    assert await s1.hold(Note, {"session": "S1", "id": "c"}).where() == "session:S1 c"
    assert await s1.hold(Catalog, {}).where() == "server"
    await c["close"]()


async def test_serve_refuses_a_type_the_host_is_not_the_place_of_also_at_a_reload():
    host = createHost({"name": "session:S1", "self": "host", "onError": lambda _error: None})
    with pytest.raises(TypeError, match=r"t\.catalog is declared for server, but session:S1 serves it"):
        host.serve(Catalog, lambda _input, _principal, _self: {"variant": "main", "where": lambda ctx: ""})
    server = createHost({"name": "server", "self": "host", "onError": lambda _error: None})
    with pytest.raises(TypeError, match=r"t\.note is declared for session:<session>, but server serves it"):
        server.serve(Note, lambda _input, _principal, _self: {"variant": "main", "where": lambda ctx: ""})
    misplaced = Facet(
        "misplaced",
        lambda api: api.serve(Note, lambda _input, _principal, _self: {"variant": "main", "where": lambda ctx: ""}),
    )
    with pytest.raises(Exception, match=r"declared for session:<session>, but server serves it"):
        await server.reload([misplaced])
    await host.close()
    await server.close()


async def test_a_host_refuses_a_hold_its_own_contract_places_elsewhere_and_a_relay_one_whose_target_disagrees():
    c = cluster()
    server = c["server"]
    await tick()

    async def holds(target: str, input: Any) -> dict[str, Any] | None:
        down, raw = memoryLinkPair()
        server.accept(down, {"principal": "guest"})
        frames: list[dict[str, Any]] = []
        raw.receive(frames.append)
        raw.send({"t": "hello", "protocol": PROTOCOL_VERSION, "name": "raw", "kind": "client", "codeVersion": 0})
        raw.send({"t": "hold", "id": 1, "at": target, "token": Note.description, "input": input})
        for _ in range(50):
            if any(frame["t"] in ("shape", "gone") for frame in frames):
                break
            await tick()
        return next((frame for frame in frames if frame["t"] in ("shape", "gone")), None)

    # A client picks session:S2 for a note of S1: the relay refuses, nothing reaches S2.
    relayed = await holds("session:S2", {"session": "S1", "id": "x"})
    assert relayed is not None and relayed["t"] == "gone"
    assert re.search(r"placement|does not live on session:S2", relayed["error"]["message"])
    # The right target is served.
    served = await holds("session:S1", {"session": "S1", "id": "x"})
    assert served is not None and served["t"] == "shape"
    # Addressed to the server itself, a note is refused by the server's own contract check.
    direct = await holds("server", {"session": "S1", "id": "x"})
    assert direct is not None and direct["t"] == "gone"
    await c["close"]()


async def test_the_host_comes_from_the_normalized_input_unknown_fields_and_defaults_do_not_change_it():
    Doc = remote(
        "t.doc-default",
        {
            "version": 1,
            "input": Type.Object({"session": Type.String({"default": "S1"})}),
            "host": hostFamily("session:", "session"),
            "variants": {"main": {"methods": {"where": method(None, Type.String())}}},
        },
    )
    c = cluster()
    server, s1 = c["server"], c["s1"]
    s1.serve(Doc, lambda _input, _principal, _self: {"variant": "main", "where": lambda ctx: "session:S1"})
    await tick()
    # An untyped caller leaves the field out: the default places it.
    assert await server.hold(Doc, {}).where() == "session:S1"
    with pytest.raises(ChordError, match=r"input field session names no host"):
        server.hold(Note, {"session": "", "id": "x"})
    await c["close"]()


async def test_placement_is_part_of_the_contract_a_moved_type_is_a_breaking_change_never_bridged_by_an_adapter():
    def at(host: str) -> Any:
        return remote(
            "t.moved",
            {
                "version": 1,
                "input": Type.Object({}),
                "host": hostNamed(host),
                "variants": {"main": {"methods": {"ping": method()}}},
            },
        )

    assert at("server").fingerprint != at("session:S1").fingerprint
    host = createHost({"name": "server", "self": "host", "onError": lambda _error: None})

    def facet(version: int, place: str) -> Facet:
        return Facet(
            "moved",
            lambda api: api.serve(
                remote(
                    "t.moved",
                    {
                        "version": version,
                        "input": Type.Object({}),
                        "host": hostNamed(place),
                        "variants": {"main": {"methods": {"ping": method()}}},
                    },
                ),
                lambda _input, _principal, _self: {"variant": "main", "ping": lambda ctx: None},
                {"accepts": {1: lambda current: current}},
            ),
        )

    await host.install(facet(1, "server"))
    await host.get(at("server"), {})
    # Same version, other placement: a forgotten bump.
    with pytest.raises(Exception, match=r"declared for session:S1|without a version bump"):
        await host.reload([facet(1, "session:S1")])
    # A new version that `accepts` version 1 but lives elsewhere: no adapter makes version 1 consumers bind.
    moved = remote(
        "t.moved",
        {
            "version": 2,
            "input": Type.Object({}),
            "host": hostNamed("session:S1"),
            "variants": {"main": {"methods": {"ping": method()}}},
        },
    )
    with pytest.raises(ChordError) as info:
        bind(moved, {1: lambda current: current}, at("server"))
    assert isChordError(info.value, "contract_mismatch") and re.search(r"lives on session:S1", info.value.message)
    await host.close()


def test_types_the_familys_field_is_a_required_string_of_the_input_and_remote_contracts_need_a_host():
    input = Type.Object(
        {"session": Type.String(), "count": Type.Number(), "note": Type.Optional(Type.String())}
    )
    variants: dict[str, Any] = {"main": {}}
    remote("t.ok", {"version": 1, "input": input, "host": hostFamily("session:", "session"), "variants": variants})
    # not ported: the three `@ts-expect-error` contracts (a field that does not exist, is not a string, or is
    # optional) — TypeScript rejects them at compile time; Python has no counterpart.
    with pytest.raises(TypeError, match=r"needs `host`"):
        remote("t.nowhere", {"version": 1, "input": input, "variants": variants})
    with pytest.raises(TypeError, match=r"prefix"):
        hostFamily("", "session")
