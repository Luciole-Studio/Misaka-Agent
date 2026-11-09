"""pi chord's ``test/principals.test.ts``.

MISAKA adaptations: an app's principal type (``createHost<Principal>``, ``defineFacet<Principal>``) is a TypeScript
type parameter; Python has none, so the cases check what happens at run time and the ``@ts-expect-error`` lines (a
principal not of the app's type, a pattern with unknown fields, a host without ``self`` given to the compiler) keep
only their runtime assertion where upstream has one. ``host.install({ name, ...facet })`` is ``Facet(name,
facet.setup, facet.uses)``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.chord import (
    createHost,
    defineFacet,
    hostNamed,
    local,
    memoryLinkPair,
    method,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.host import Facet


def principalKey(principal: Any) -> str:
    return f"guest:{principal['id']}" if principal["kind"] == "guest" else principal["kind"]


Who = local(
    "t.who",
    {"version": 1, "input": Type.Object({}), "members": {"who": method(None, Type.String())}},
)


async def test_factories_methods_and_contexts_see_the_apps_principal_type():
    host = createHost({"self": {"kind": "host"}, "principalKey": principalKey})

    def setup(api: Any) -> None:
        def factory(_input: Any, principal: Any, self: Any) -> dict[str, Any]:
            created = f"guest {principal['id']}" if principal["kind"] == "guest" else principal["kind"]

            async def task(ctx: Any) -> None:
                kind = ctx.principal["kind"]
                del kind

            self.task(task)
            return {"who": lambda ctx: f"{created}, called by {ctx.principal['kind']}"}

        api.serve(Who, factory)

    facet = defineFacet(setup)
    await host.install(Facet("who", facet.setup, facet.uses))
    guest = host.hold(Who, {}, {"principal": {"kind": "guest", "id": "g1"}})
    assert (
        await host.root(lambda ctx: guest.who(ctx), {"principal": {"kind": "guest", "id": "g1"}})
        == "guest g1, called by host"
    )

    async def plain(ctx: Any) -> None:
        return None

    await host.root(plain)
    # not ported: the two `@ts-expect-error` lines (a principal not of the app's type, a pattern with an unknown
    # field) — TypeScript rejects them at compile time; Python has no counterpart.
    await host.close()


async def test_the_hosts_own_principal_is_recognized_by_key_an_equal_object_reaches_the_same_objects():
    host = createHost({"self": {"kind": "host"}, "principalKey": principalKey})
    created = 0

    def factory(_input: Any, _principal: Any, _self: Any) -> dict[str, Any]:
        nonlocal created
        created += 1
        return {"who": lambda ctx: "one"}

    host.serve(Who, factory)
    own = host.hold(Who, {})
    equal = host.hold(Who, {}, {"principal": {"kind": "host"}})
    await asyncio.gather(own.ready, equal.ready)
    assert created == 1
    # Never disposed as a matching principal: it is the host's own.
    await host.invalidatePrincipal({"kind": "host"})
    assert own.status == "live"
    await host.close()


async def test_a_childs_parent_acts_as_the_principal_the_child_gave_connect_relayed_callers_keep_theirs():
    Echo = remote(
        "t.echo",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("worker"),
            "variants": {"main": {"methods": {"who": method(None, Type.String())}}},
        },
    )
    server = createHost({"name": "server", "self": {"kind": "host"}, "principalKey": principalKey})
    down, up = memoryLinkPair()
    server.accept(down, {"kind": "host", "name": "worker", "principal": {"kind": "host"}})
    worker = createHost(
        {
            "name": "worker",
            "self": {"kind": "host"},
            "principalKey": principalKey,
            "upstream": {"link": up, "principal": {"kind": "server"}},
        }
    )
    worker.serve(
        Echo, lambda _input, principal, _self: {"variant": "main", "who": lambda ctx: principalKey(principal)}
    )
    browserDown, browserUp = memoryLinkPair()
    server.accept(browserDown, {"principal": {"kind": "guest", "id": "g1"}})
    browser = createHost(
        {
            "name": "browser",
            "kind": "client",
            "self": {"kind": "host"},
            "upstream": {"link": browserUp, "principal": {"kind": "server"}},
        }
    )
    assert await server.hold(Echo, {}).who() == "server"

    async def resolve(_name: str) -> Any:
        return down

    server.route(
        "worker",
        {"authorize": lambda _principal, _host: True, "resolve": resolve, "principal": lambda _name: {"kind": "host"}},
    )
    assert await browser.hold(Echo, {}).who() == "guest:g1"
    await browser.close()
    await worker.close()
    await server.close()


def test_refuses_a_host_a_parent_link_or_a_route_without_its_principal():
    with pytest.raises(TypeError, match=r"needs `self`"):
        createHost({"name": "x"})
    host = createHost({"self": "host"})
    _, up = memoryLinkPair()
    with pytest.raises(TypeError, match=r"connect needs `principal`"):
        host.connect(up, {})

    async def resolve(_name: str) -> Any:
        return up

    with pytest.raises(TypeError, match=r"needs `principal`"):
        host.route("w:", {"authorize": lambda _principal, _host: True, "resolve": resolve})
