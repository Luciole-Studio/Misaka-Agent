"""pi chord's ``test/variants.test.ts``.

Entry 8: a binding applies a shape only once its new state members have snapshots; members outside the variant
throw ``not_in_variant``; a re-hold after a link loss keeps the last values readable and sends one fresh unit.

MISAKA adaptations: facets are ``Facet`` objects; ``void joining.ready.then(...)`` is a done callback on the future.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from misaka.chord import createHost, hostNamed, memoryLinkPair, method, remote
from misaka.chord._typebox import Type
from misaka.chord.errors import ChordError
from misaka.chord.host import Facet
from tests.fixtures.chord_counting import CountingLink, stateFrames


async def sleep(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


async def until(check: Callable[[], bool], attempts: int = 200) -> None:
    for _ in range(attempts):
        if check():
            break
        await sleep(2)
    assert check(), "condition not reached"


Doc = remote(
    "t.e8-doc",
    {
        "version": 1,
        "input": Type.Object({}),
        "host": hostNamed("server"),
        "variants": {
            "a": {"state": {"x": Type.Number()}, "methods": {"bump": method()}},
            "b": {"state": {"x": Type.Number(), "y": Type.String()}, "methods": {"bump": method()}},
            "c": {"state": {"y": Type.String()}, "methods": {"bump": method()}},
        },
    },
)


def docFacet(variant: str) -> Facet:
    """The server's facet: ``Doc`` in variant ``variant`` (the cells survive reloads by name)."""

    def factory(_input, _principal, self):
        x = self.state("x", Type.Number(), 1)
        y = self.state("y", Type.String(), "why")

        def bump(ctx):
            x.replace(ctx, x.value + 1)

        if variant == "a":
            return {"variant": variant, "x": x, "bump": bump}
        if variant == "b":
            return {"variant": variant, "x": x, "y": y, "bump": bump}
        return {"variant": variant, "y": y, "bump": bump}

    return Facet("doc", lambda api: api.serve(Doc, factory))


def setup():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    down, up = memoryLinkPair()
    server.accept(down, {"principal": {"user": "u"}})
    link = CountingLink(up)
    browser = createHost(
        {
            "self": {"kind": "host"},
            "name": "browser",
            "kind": "client",
            "upstream": {"link": link, "principal": {"kind": "server"}},
            "onError": lambda _error: None,
        }
    )
    return server, browser, link


# ─── variants ───


async def test_a_new_variants_members_arrive_with_its_shape_a_member_outside_the_variant_throws_not_in_variant():
    server, browser, link = setup()
    await server.install(docFacet("a"))
    doc = browser.hold(Doc, {})
    await doc.ready
    assert doc.variant == "a"
    assert doc.x.value == 1
    with pytest.raises(ChordError) as raised:
        _ = doc.y.value
    assert raised.value.code == "not_in_variant"
    # Every shape the reference shows is readable in full, at the moment it is shown.
    failures: list[BaseException] = []

    def onShape(shape):
        for name in ("x", "y"):
            member = (Doc.description["variants"].get(shape["variant"] or "") or {}).get(name)
            if member is None or member["kind"] != "state":
                continue
            try:
                _ = getattr(doc, name).value
            except Exception as error:  # noqa: BLE001 - collected for the assertion below
                failures.append(error)

    doc.onShape(onShape)
    before = len(stateFrames(link.received))
    await server.reload([docFacet("b")])
    await until(lambda: doc.variant == "b")
    assert doc.y.value == "why"
    assert doc.x.value == 1
    unit = stateFrames(link.received)[before:]
    assert [frame["m"] for frame in unit if frame["as"] == "snap"] == ["y"], "only the new member's snapshot"
    await server.reload([docFacet("c")])
    await until(lambda: doc.variant == "c")
    with pytest.raises(ChordError) as raised:
        _ = doc.x.value
    assert raised.value.code == "not_in_variant"
    assert doc.y.value == "why"
    assert failures == []
    await browser.close()
    await server.close()


# ─── re-hold after a link loss ───


async def test_keeps_the_last_values_readable_sends_one_fresh_unit_and_a_reference_joining_meanwhile_waits_for_it():
    server = createHost({"self": {"kind": "host"}, "name": "server", "onError": lambda _error: None})
    await server.install(docFacet("b"))
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
    doc = browser.hold(Doc, {})
    await doc.ready
    await doc.bump()
    await until(lambda: doc.x.value == 2)
    up.close()
    await until(lambda: doc.status == "loading")
    # The last values stay readable while the binding re-holds.
    assert doc.x.value == 2
    joining = browser.hold(Doc, {})
    assert joining.status == "loading"
    joined = [False]
    joining.ready.add_done_callback(lambda done: joined.__setitem__(0, not done.cancelled() and done.exception() is None))
    await sleep(5)
    assert joined[0] is False, "a reference joining during the re-hold waits for the fresh unit"

    down2, up2 = memoryLinkPair()
    server.accept(down2, {"principal": {"user": "u"}})
    link = CountingLink(up2)
    browser.connect(link, {"principal": {"kind": "server"}})
    await joining.ready
    assert doc.status == "live"
    hold = next((frame for frame in link.sent if frame["t"] == "hold"), None)
    assert hold is not None and hold.get("again") is True, "a re-hold says again"
    assert link.count("sent", "hold") == 1, "one hold for both references"
    snaps = [frame for frame in stateFrames(link.received) if frame["as"] == "snap"]
    assert sorted(frame["m"] for frame in snaps) == ["x", "y"], "one snapshot per member"
    # The old hold was released with the link: the server made a fresh object (its cells start over).
    assert doc.x.value == 1
    await browser.close()
    await server.close()
