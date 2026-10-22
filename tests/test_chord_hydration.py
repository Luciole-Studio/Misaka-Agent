"""pi chord's ``test/hydration.test.ts``.

Entry 8: a local object goes live only with its state, also where it forwards or derives from a remote reference's
member; activation waits at most ``readyTimeout`` (a cycle across hosts fails with a hint instead of hanging).

MISAKA adaptations: facets are ``Facet`` objects; ``setTimeout(admit, delay)`` is ``loop.call_later``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from misaka.chord import createHost, hostNamed, local, memoryLinkPair, method, remote
from misaka.chord._typebox import Type
from misaka.chord.errors import ChordError, isChordError
from misaka.chord.host import Facet, HostImpl


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


def quiet(_error: Any) -> None:
    return None


# Lives on the worker `w`; its value is set by `set`.
Source = remote(
    "t.e8-source",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("w"),
        "variants": {"main": {"state": {"v": Type.String()}, "methods": {"set": method(Type.String())}}},
    },
)

# Lives on the server; forwards (and derives from) the worker's `Source`.
Forward = remote(
    "t.e8-forward",
    {
        "version": 1,
        "input": Type.Object({"how": Type.String()}),
        "host": hostNamed("server"),
        "variants": {"main": {"state": {"v": Type.String(), "size": Type.Number()}}},
    },
)


def serveSource(host: HostImpl) -> None:
    def factory(_input, _principal, self):
        v = self.state("v", Type.String(), "from the worker")

        def set(text, ctx):
            v.replace(ctx, text)

        return {"variant": "main", "v": v, "set": set}

    host.serve(Source, factory)


class Cluster:
    """A server with a worker child ``w`` (accepted after ``delay`` ms, as p2's server admits a worker after it is
    ready)."""

    def __init__(self, delay: float | None = None, readyTimeout: float | None = None) -> None:
        extra = {} if readyTimeout is None else {"readyTimeout": readyTimeout}
        self.server = createHost({"self": {"kind": "host"}, "name": "server", "onError": quiet, **extra})
        down, up = memoryLinkPair()
        self.worker = createHost(
            {
                "self": {"kind": "host"},
                "name": "w",
                "upstream": {"link": up, "principal": {"kind": "server"}},
                "onError": quiet,
                **extra,
            }
        )

        def admit() -> None:
            self.server.accept(down, {"principal": {"kind": "worker"}, "kind": "host", "name": "w"})

        if delay is None:
            admit()
        else:
            asyncio.get_running_loop().call_later(delay / 1000, admit)

    def browser(self) -> HostImpl:
        down2, up2 = memoryLinkPair()
        self.server.accept(down2, {"principal": {"user": "u"}})
        return createHost(
            {
                "self": {"kind": "host"},
                "name": "browser",
                "kind": "client",
                "upstream": {"link": up2, "principal": {"kind": "server"}},
                "onError": quiet,
            }
        )


# ─── local hydration ───


async def test_an_object_that_forwards_and_derives_from_a_remote_member_goes_live_with_its_value():
    c = Cluster()
    serveSource(c.worker)
    # Held outside the factory (no `self.get`): activation still waits for its snapshot.
    held = c.server.hold(Source, {})

    def factory(input, _principal, self):
        source = self.get(Source, {}) if input["how"] == "declared" else held
        return {"variant": "main", "v": source.v, "size": self.derive([source.v], lambda text: len(text))}

    c.server.serve(Forward, factory)
    browser = c.browser()
    for how in ("held", "declared"):
        forward = browser.hold(Forward, {"how": how})
        await forward.ready
        assert forward.v.value == "from the worker"
        assert forward.size.value == len("from the worker")
        local_ = c.server.hold(Forward, {"how": how})
        await local_.ready
        assert local_.v.value == "from the worker"
    await c.server.close()
    await c.worker.close()


async def test_a_member_read_inside_a_factory_throws_not_ready_it_is_a_plain_value_once_live():
    host = createHost({"self": {"kind": "host"}, "onError": quiet})
    Inner = local("t.e8-inner", {"version": 1, "input": Type.Object({}), "members": {"n": Type.Number()}})
    Outer = local("t.e8-outer", {"version": 1, "input": Type.Object({}), "members": {"m": Type.Number()}})
    host.serve(Inner, lambda _input, _principal, self: {"n": self.state("n", Type.Number(), 7)})
    seen: list[Any] = []

    def outer(_input, _principal, self):
        inner = self.get(Inner, {})
        try:
            seen.append(inner.n.value)
        except Exception as error:  # noqa: BLE001 - recorded for the assertion
            seen.append("not_ready" if isChordError(error, "not_ready") else error)
        return {"m": self.derive([inner.n], lambda n: n * 2)}

    host.serve(Outer, outer)
    ref = host.hold(Outer, {})
    await ref.ready
    assert seen == ["not_ready"]
    assert ref.m.value == 14


async def test_a_host_object_of_a_child_holding_the_parents_object_goes_live_once_the_parent_admits_the_child():
    Settings = remote(
        "t.e8-settings",
        {
            "version": 1,
            "input": Type.Object({}),
            "host": hostNamed("server"),
            "variants": {"main": {"state": {"theme": Type.String()}}},
        },
    )
    Local = local("t.e8-boot", {"version": 1, "input": Type.Object({}), "members": {"theme": Type.String()}})
    c = Cluster(delay=20)
    c.server.serve(
        Settings,
        lambda _input, _principal, self: {"variant": "main", "theme": self.state("theme", Type.String(), "dark")},
    )

    def boot(_input, _principal, self):
        settings = self.get(Settings, {})
        return {"theme": settings.theme}

    await c.worker.install(
        Facet("boot", lambda api: api.serve(Local, boot, {"lifetime": "host"})),
        {"awaitHostObjects": False},
    )
    await c.worker.ready()
    ref = c.worker.hold(Local, {})
    await ref.ready
    assert ref.theme.value == "dark"
    await c.server.close()
    await c.worker.close()


async def test_a_reference_cycle_across_hosts_fails_at_readytimeout_with_a_cycle_hint():
    A = remote(
        "t.e8-cycle-a",
        {"version": 1, "input": Type.Object({}), "host": hostNamed("server"), "variants": {"main": {}}},
    )
    B = remote(
        "t.e8-cycle-b",
        {"version": 1, "input": Type.Object({}), "host": hostNamed("w"), "variants": {"main": {}}},
    )
    c = Cluster(readyTimeout=50)

    def a(_input, _principal, self):
        self.get(B, {})
        return {"variant": "main"}

    def b(_input, _principal, self):
        self.get(A, {})
        return {"variant": "main"}

    c.server.serve(A, a)
    c.worker.serve(B, b)
    ref = c.server.hold(A, {})
    with pytest.raises(ChordError) as raised:
        await ref.ready
    error = raised.value
    assert isChordError(error, "unavailable") and error.reason == "ready_timeout" and re.search("cycle", error.message)
    await sleep(10)
    await c.server.close()
    await c.worker.close()
