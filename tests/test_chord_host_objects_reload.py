"""pi chord's ``test/host-objects-reload.test.ts``.

MISAKA adaptations: facets are ``Facet`` objects; JavaScript object keys are strings, so a numeric record key
(``{ 1: true }``, ``draft[conversation]``) is the string ``"1"`` here (a draft converts an integer key as JavaScript
does); ``onTestFinished(() => host.close())`` is a ``finally``; ``setImmediate`` is a few event-loop turns.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.chord import (
    createHost,
    defineRegistry,
    isChordError,
    local,
    memoryLinkPair,
    method,
)
from misaka.chord._typebox import Type
from misaka.chord.errors import ChordError
from misaka.chord.host import Facet, HostImpl


async def tick() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


input = Type.Object({})
flags = Type.Record(Type.String(), Type.Boolean())
SwitchV1 = local(
    "t.eager-switch",
    {"version": 1, "input": input, "members": {"enabled": Type.Boolean(), "set": method(Type.Boolean())}},
)
SwitchV2 = local(
    "t.eager-switch",
    {
        "version": 2,
        "input": input,
        "members": {
            "enabled": flags,
            "set": method(Type.Object({"conversation": Type.Number(), "enabled": Type.Boolean()})),
        },
    },
)
SwitchV3 = local(
    "t.eager-switch",
    {
        "version": 3,
        "input": input,
        "members": {
            "enabled": flags,
            "update": method(Type.Object({"conversation": Type.Number(), "enabled": Type.Boolean()})),
        },
    },
)
Read = defineRegistry("t.eager-switch-read")


class SwitchFacets:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def declareResource(self, self_: Any) -> Any:
        def open(_options: Any) -> None:
            self.log.append("open")

        return self_.resource("watch", {"version": 1, "open": open, "close": lambda _value: self.log.append("close")})

    @property
    def v1(self) -> Facet:
        def factory(_input: Any, _principal: Any, self_: Any) -> dict[str, Any]:
            self.declareResource(self_)
            enabledState = self_.state("enabled", Type.Boolean(), False)
            self_.register(Read, {"current": lambda: enabledState.value})

            def set(enabled: bool, ctx: Any) -> None:
                enabledState.replace(ctx, enabled)

            return {"enabled": enabledState, "set": set}

        return Facet("switch", lambda api: api.serve(SwitchV1, factory, {"lifetime": "host"}))

    def v2(self, valid: bool) -> Facet:
        def factory(_input: Any, _principal: Any, self_: Any) -> dict[str, Any]:
            self.declareResource(self_)

            def migrate(old: Any, from_: int) -> Any:
                assert from_ == 1
                return {"1": old} if valid else "invalid migration"

            enabledState = self_.state("enabled", flags, {}, {"version": 2, "migrate": migrate})
            self_.register(Read, {"current": lambda: enabledState.value})

            def set(args: Any, ctx: Any) -> None:
                def mutate(draft: Any) -> None:
                    draft[args["conversation"]] = args["enabled"]

                enabledState.change(ctx, mutate)

            return {"enabled": enabledState, "set": set}

        return Facet("switch", lambda api: api.serve(SwitchV2, factory, {"lifetime": "host"}))

    @property
    def v3(self) -> Facet:
        def factory(_input: Any, _principal: Any, self_: Any) -> dict[str, Any]:
            self.declareResource(self_)
            enabledState = self_.state("enabled", flags, {}, {"version": 2})
            self_.register(Read, {"current": lambda: enabledState.value})

            def update(args: Any, ctx: Any) -> None:
                def mutate(draft: Any) -> None:
                    draft[args["conversation"]] = args["enabled"]

                enabledState.change(ctx, mutate)

            return {"enabled": enabledState, "update": update}

        return Facet("switch", lambda api: api.serve(SwitchV3, factory, {"lifetime": "host"}))


async def test_the_hosts_reference_to_a_host_object_follows_contract_bumps_at_commit_without_reopening_failures_retain_the_old():
    errors: list[Any] = []
    host = HostImpl({"self": {"kind": "host"}, "dev": True, "onError": errors.append})
    try:
        log: list[str] = []
        facets = SwitchFacets(log)
        await host.load([facets.v1])
        initial = host.hostObjects[SwitchV1.name]
        object = initial.object
        original = host.hold(SwitchV1, {})
        await original.set(True)
        original.release()
        codeVersion = host.codeVersion
        with pytest.raises(ChordError) as info:
            await host.reload([facets.v2(False)])
        assert isChordError(info.value, "invalid")
        assert host.codeVersion == codeVersion
        assert host.hostObjects[SwitchV1.name] is initial
        assert initial.status == "live"
        assert host.registry(Read).get("current")() is True
        report = await host.reload([facets.v2(True)])
        assert len(report["gated"]) == 1
        updated = host.hostObjects[SwitchV2.name]
        assert updated.contract is SwitchV2
        assert updated.object is object
        assert initial.status == "released"
        assert object is not None and len(object.refs) == 1, "Attach the replacement before releasing the only old hold"
        assert host.registry(Read).get("current")() == {"1": True}
        modern = host.hold(SwitchV2, {})
        await modern.set({"conversation": 2, "enabled": True})
        modern.release()
        assert (await host.reload([facets.v3]))["gated"] == []
        assert host.hostObjects[SwitchV3.name].contract is SwitchV3
        assert host.hostObjects[SwitchV3.name].object is object
        assert updated.status == "released"
        assert host.registry(Read).get("current")() == {"1": True, "2": True}
        await host.ready()
        assert log == ["open"]
        assert errors == []
        await host.close()
        assert log == ["open", "close"]
    finally:
        await host.close()


ReadV1 = local(
    "t.eager-read", {"version": 1, "input": Type.Object({}), "members": {"read": method(None, Type.String())}}
)
ReadV2 = local(
    "t.eager-read", {"version": 2, "input": Type.Object({}), "members": {"fetch": method(None, Type.String())}}
)
readerV1 = Facet("read", lambda api: api.serve(ReadV1, lambda *_: {"read": lambda _ctx: "one"}, {"lifetime": "host"}))


def readerV2(adapt: bool = False) -> Facet:
    options: dict[str, Any] = {"lifetime": "host"}
    if adapt:
        options["accepts"] = {1: lambda current: {"read": current["fetch"]}}
    return Facet("read", lambda api: api.serve(ReadV2, lambda *_: {"fetch": lambda _ctx: "two"}, options))


async def test_external_old_version_consumers_still_refuse_a_host_objects_upgrade_without_accepts_and_keep_their_adapter():
    def raising(error: Any) -> None:
        raise error

    host = HostImpl({"self": {"kind": "host"}, "onError": raising})
    try:
        await host.load([readerV1])
        own = host.hostObjects[ReadV1.name]
        consumer = host.hold(ReadV1, {})
        assert await consumer.read() == "one"
        with pytest.raises(ChordError) as info:
            await host.reload([readerV2()])
        assert isChordError(info.value, "contract_mismatch")
        assert host.hostObjects[ReadV1.name] is own
        assert await consumer.read() == "one"
        await host.reload([readerV2(True)])
        assert host.hostObjects[ReadV2.name].contract is ReadV2
        assert await consumer.read() == "two"
        assert consumer.contract is ReadV1, "Only the bootstrap hold is retokened"
        assert await host.hold(ReadV2, {}).fetch() == "two"
    finally:
        await host.close()


async def test_object_owned_old_version_references_still_block_a_host_objects_upgrade_unless_their_consumer_is_replaced():
    host = HostImpl({"self": {"kind": "host"}, "onError": lambda _error: None})
    try:
        Consumer = local(
            "t.eager-consumer",
            {"version": 1, "input": Type.Object({}), "members": {"read": method(None, Type.String())}},
        )

        def oldFactory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            reader = self.get(ReadV1, {})
            return {"read": lambda ctx: reader.read(ctx)}

        def newFactory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            reader = self.get(ReadV2, {})
            return {"read": lambda ctx: reader.fetch(ctx)}

        oldConsumer = Facet("consumer", lambda api: api.serve(Consumer, oldFactory))
        newConsumer = Facet("consumer", lambda api: api.serve(Consumer, newFactory))
        await host.load([readerV1, oldConsumer])
        consumer = host.hold(Consumer, {})
        assert await consumer.read() == "one"
        with pytest.raises(ChordError) as info:
            await host.reload([readerV2()])
        assert isChordError(info.value, "contract_mismatch")
        assert await consumer.read() == "one"
        await host.reload([readerV2(), newConsumer])
        assert await consumer.read() == "two"
        assert host.hostObjects[ReadV2.name].contract is ReadV2
    finally:
        await host.close()


async def test_a_forgotten_contract_bump_remains_refused_even_when_the_only_consumer_is_the_hosts_own_reference():
    host = HostImpl({"self": {"kind": "host"}, "onError": lambda _error: None})
    try:
        await host.load([readerV1])
        incompatible = local(
            ReadV1.name, {"version": 1, "input": Type.Object({}), "members": {"fetch": method(None, Type.String())}}
        )
        facet = Facet(
            "read", lambda api: api.serve(incompatible, lambda *_: {"fetch": lambda _ctx: "bad"}, {"lifetime": "host"})
        )
        with pytest.raises(ChordError) as info:
            await host.reload([facet])
        assert isChordError(info.value, "contract_mismatch") and "without a version bump" in str(info.value)
        assert host.hostObjects[ReadV1.name].contract is ReadV1
    finally:
        await host.close()


async def test_a_cluster_contract_upgrade_retokens_the_servers_and_workers_references_only_after_unanimous_preparation():
    log: list[str] = []
    facets = SwitchFacets(log)
    valid = [False]
    server = HostImpl({"self": {"kind": "host"}, "onError": lambda _error: None})

    async def loader(_modules: Any) -> list[Facet]:
        return [facets.v2(valid[0])]

    worker = HostImpl({"self": {"kind": "host"}, "name": "worker", "onError": lambda _error: None, "loader": loader})
    try:
        await server.load([facets.v1])
        await worker.load([facets.v1])
        down, up = memoryLinkPair()
        server.accept(down, {"kind": "host", "name": "worker", "principal": {"worker": True}})
        worker.connect(up, {"principal": {"kind": "server"}})
        await tick()
        serverOwn = server.hostObjects.get(SwitchV1.name)
        workerOwn = worker.hostObjects.get(SwitchV1.name)
        with pytest.raises(ChordError) as info:
            await server.reload([facets.v2(True)], {"remote": {"worker": ["switch"]}})
        assert isChordError(info.value, "invalid")
        assert server.hostObjects.get(SwitchV1.name) is serverOwn
        assert worker.hostObjects.get(SwitchV1.name) is workerOwn
        valid[0] = True
        await server.reload([facets.v2(True)], {"remote": {"worker": ["switch"]}})
        await tick()
        assert server.hostObjects[SwitchV2.name].contract is SwitchV2
        assert worker.hostObjects[SwitchV2.name].contract is SwitchV2
        assert server.registry(Read).get("current")() == {"1": False}
        assert worker.registry(Read).get("current")() == {"1": False}
        assert server.codeVersion == worker.codeVersion
        assert log == ["open", "open"]
    finally:
        await worker.close()
        await server.close()


async def test_a_reload_that_changes_a_types_lifetime_creates_its_host_object_or_lets_it_go():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    Kept = local("t.lifetime-change", {"version": 1, "input": Type.Object({})})
    log: list[str] = []

    def facet(lifetime: str) -> Facet:
        def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
            self.resource(
                "life",
                {
                    "version": 1,
                    "open": lambda _options: log.append(f"open {lifetime}"),
                    "close": lambda _value: log.append("close"),
                },
            )
            return {}

        return Facet("kept", lambda api: api.serve(Kept, factory, {"lifetime": lifetime}))

    try:
        await host.install(facet("referenced"))
        assert log == [], "a referenced object waits for its first reference"
        await host.reload([facet("host")])
        await host.ready()
        assert log == ["open host"], "created by the reload that made it a host object"
        await host.reload([facet("referenced")])
        for _ in range(50):
            if len(log) >= 2:
                break
            await asyncio.sleep(0.005)
        assert log == ["open host", "close"], "nobody else references it: disposed"
    finally:
        await host.close()
