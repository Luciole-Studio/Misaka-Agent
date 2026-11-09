"""pi chord's ``test/provider-cost.test.ts``.

Entry 8: what a change costs the provider. The diff is computed once per change; each feed (one per connection and
member) encodes it with its own stateful encoder and its link serializes it.

MISAKA adaptations: ``vi.mock`` of the delta module's ``encoder`` is ``monkeypatch`` of the name the connection module
calls (``misaka.chord.connection.encoder``); a mutating ``change`` callback returns ``None``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.chord import connection as connectionModule
from misaka.chord import createHost, hostNamed, memoryLinkPair, method, remote
from misaka.chord._typebox import Type
from misaka.chord.delta import encoder as originalEncoder
from misaka.utils.js import jsonStringify
from tests.fixtures.chord_counting import CountingLink, stateFrames

List = remote(
    "t.e8-cost",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("server"),
        "variants": {"main": {"state": {"items": Type.Array(Type.String())}, "methods": {"add": method(Type.String())}}},
    },
)


async def test_one_change_to_k_clients_k_encodes_one_per_feed_one_frame_per_link_the_same_ops_on_every_link(monkeypatch):
    counts = {"encoders": 0, "encodes": 0}

    class CountingEncoder:
        def __init__(self) -> None:
            self.inner = originalEncoder()

        def encode(self, ops: Any) -> Any:
            counts["encodes"] += 1
            return self.inner.encode(ops)

    def encoder() -> CountingEncoder:
        counts["encoders"] += 1
        return CountingEncoder()

    monkeypatch.setattr(connectionModule, "encoder", encoder)
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})

    def factory(_input: Any, _principal: Any, self: Any) -> Any:
        items = self.state("items", Type.Array(Type.String()), [])

        def add(text: str, ctx: Any) -> None:
            def mutate(draft: Any) -> None:
                draft.push(text)

            items.change(ctx, mutate)

        return {"variant": "main", "items": items, "add": add}

    server.serve(List, factory)
    K = 4
    clients = []
    for i in range(K):
        down, up = memoryLinkPair()
        # One principal: every client reaches the same object.
        server.accept(down, {"principal": {"user": "u"}})
        link = CountingLink(up)
        host = createHost(
            {
                "self": {"kind": "host"},
                "name": f"browser{i}",
                "kind": "client",
                "upstream": {"link": link, "principal": {"kind": "server"}},
                "onError": lambda _error: None,
            }
        )
        clients.append({"host": host, "link": link, "ref": host.hold(List, {})})
    await asyncio.gather(*[client["ref"].ready for client in clients])
    encoders = counts["encoders"]
    encodes = counts["encodes"]
    before = [len(stateFrames(client["link"].received)) for client in clients]
    await clients[0]["ref"].add("x")
    await asyncio.sleep(0.01)
    assert counts["encodes"] - encodes == K, "one encode per feed"
    assert counts["encoders"] == encoders, "no new streams for a change"
    frames = [stateFrames(client["link"].received)[before[i] :] for i, client in enumerate(clients)]
    for entries in frames:
        assert len(entries) == 1
    ops = [jsonStringify(entries[0].get("ops")) for entries in frames]
    assert len(set(ops)) == 1, "every link carries the same ops"
    for client in clients:
        assert client["ref"].items.value == ["x"]
    await server.close()
