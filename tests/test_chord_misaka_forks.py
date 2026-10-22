"""The MISAKA-specific adaptations of the chord port (the ``# MISAKA fork:`` places), each pinned by a test.

Upstream's behaviour is the reference: the TypeBox expectations below were recorded from ``typebox`` 1.3.27 under
Node (the version chord 1.3.0 depends on).
"""

from __future__ import annotations

import asyncio
import gc
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import misaka.chord
from misaka.chord import createHost, loader, local, memoryLinkPair, method, remote
from misaka.chord._promise import background, deferred, race
from misaka.chord._typebox import Type, Value
from misaka.chord.link import PACK_THRESHOLD, inflateMessage, packMessage, splitMessage
from misaka.chord.loader import nodeLoader
from misaka.chord.object import RefProxy, refOf, sameSelect
from misaka.utils.js import UNDEFINED


async def settle(turns: int = 20) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


# ─── _typebox ────────────────────────────────────────────────────────────────

RECORDED_SCHEMA = json.loads(
    '{"type":"object","required":["a","c","d","e","f","g","h"],"properties":{"a":{"type":"string","minLength":1},'
    '"b":{"type":"integer","minimum":1},"c":{"type":"array","items":{"type":"string"}},"d":{"enum":["x","y"]},'
    '"e":{"type":"string","const":"hi"},"f":{},"g":{"type":"object","patternProperties":{"^.*$":{}}},'
    '"h":{"anyOf":[{"type":"string","const":"a"},{"type":"number","const":2},{"type":"boolean","const":true}]}},'
    '"additionalProperties":false}'
)


def sample() -> dict[str, Any]:
    return Type.Object(
        {
            "a": Type.String({"minLength": 1}),
            "b": Type.Optional(Type.Integer({"minimum": 1})),
            "c": Type.Immutable(Type.Array(Type.String())),
            "d": Type.Enum(["x", "y"]),
            "e": Type.Literal("hi"),
            "f": Type.Unknown(),
            "g": Type.Record(Type.String(), Type.Unknown()),
            "h": Type.Union([Type.Literal("a"), Type.Literal(2), Type.Literal(True)]),
        },
        {"additionalProperties": False},
    )


def test_type_builders_emit_typeboxs_json_schema_key_order_included():
    schema = sample()
    assert schema == RECORDED_SCHEMA
    assert json.dumps(schema) == json.dumps(RECORDED_SCHEMA)


def test_value_errors_follow_typeboxs_keyword_order_and_wording():
    errors = [[error["instancePath"], error["message"]] for error in Value.Errors(sample(), {"a": "", "b": 0, "z": 1})]
    assert errors == [
        ["", "must have required properties c, d, e, f, g, h"],
        ["/z", "schema is false"],
        ["", "must not have additional properties"],
        ["/a", "must not have fewer than 1 characters"],
        ["/b", "must be >= 1"],
    ]


def test_value_default_and_clean_match_typebox():
    defaulted = Type.Object(
        {
            "x": Type.Number({"default": 3}),
            "y": Type.Optional(Type.String({"default": "q"})),
            "z": Type.Object({"w": Type.Boolean({"default": False})}),
        }
    )
    assert Value.Default(defaulted, {}) == {"x": 3, "y": "q"}
    assert Value.Clean(defaulted, {"x": 1, "extra": 2, "z": {"w": True, "more": 1}}) == {"x": 1, "z": {"w": True}}
    # Union priority (TypeBox's structural compare): {a, b} is inside {a}, so it is tried first and keeps more.
    union = Type.Union([Type.Object({"a": Type.Number()}), Type.Object({"a": Type.Number(), "b": Type.Number()})])
    assert Value.Clean(union, {"a": 1, "b": 2, "c": 3}) == {"a": 1, "b": 2}


def test_value_check_has_javascripts_value_semantics():
    assert Value.Check(Type.Integer(), 1.0) is True
    assert Value.Check(Type.Number(), True) is False
    assert Value.Check(Type.Number(), float("nan")) is False
    assert Value.Check(Type.String({"maxLength": 1}), "😀") is True
    assert Value.Check(Type.Object({}), []) is False
    # An optional property may hold `undefined`.
    assert Value.Check(Type.Object({"a": Type.Optional(Type.Number())}), {"a": UNDEFINED}) is True


# ─── _promise ────────────────────────────────────────────────────────────────


async def test_race_settles_with_the_first_and_leaves_the_others_running_and_handled():
    slow = deferred()
    fast = deferred()
    winner = asyncio.ensure_future(race([slow.promise, fast.promise]))
    fast.resolve("fast")
    assert await winner == "fast"
    slow.reject(RuntimeError("late"))
    await settle()
    # The loser's failure counts as handled (no "exception was never retrieved").
    assert slow.promise.done()


async def test_a_deferred_settles_once():
    waiter = deferred()
    waiter.resolve(1)
    waiter.reject(RuntimeError("ignored"))
    waiter.resolve(2)
    assert await waiter.promise == 1


async def test_background_work_is_held_until_it_finishes_and_its_failure_reported():
    reported: list[BaseException] = []

    async def work() -> None:
        await asyncio.sleep(0)
        gc.collect()
        raise RuntimeError("boom")

    background(work(), reported.append)
    gc.collect()
    await settle()
    assert [str(error) for error in reported] == ["boom"]


# ─── references ──────────────────────────────────────────────────────────────

Doc = local("t.fork-doc", {"version": 1, "input": Type.Object({}), "members": {"text": Type.String(), "edit": method(Type.String())}})


def docFactory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
    text = self.state("text", Type.String(), "a")

    def edit(value: str, ctx: Any) -> None:
        text.replace(ctx, value)

    return {"text": text, "edit": edit}


async def test_a_reference_proxy_binds_members_late_and_has_the_has_trap():
    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(Doc, docFactory)
    ref = await host.get(Doc, {})
    assert isinstance(ref, RefProxy)
    assert refOf(ref) is not None and refOf(ref).proxy is ref
    assert "text" in ref and "edit" in ref and "status" in ref
    assert "missing" not in ref
    # Not a thenable: `await ref` and `ref.then` do not pretend it is a promise.
    with pytest.raises(AttributeError):
        _ = ref.then
    with pytest.raises(TypeError):
        ref.text = "x"
    await ref.edit("b")
    assert ref.text.value == "b"
    await host.close()


async def test_a_cells_change_treats_none_as_no_new_value():
    Box = local("t.fork-box", {"version": 1, "input": Type.Object({}), "members": {"items": Type.Array(Type.String()), "push": method(Type.String()), "reset": method()}})

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        items = self.state("items", Type.Array(Type.String()), [])

        def push(item: str, ctx: Any) -> None:
            def mutate(draft: Any) -> None:
                draft.push(item)

            items.change(ctx, mutate)

        def reset(ctx: Any) -> None:
            items.change(ctx, lambda _draft: ["fresh"])

        return {"items": items, "push": push, "reset": reset}

    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(Box, factory)
    box = await host.get(Box, {})
    await box.push("a")
    assert box.items.value == ["a"]
    await box.reset()
    assert box.items.value == ["fresh"]
    await host.close()


async def test_adapter_views_are_cached_per_implementation():
    calls: list[int] = []
    V2 = local("t.fork-adapted", {"version": 2, "input": Type.Object({}), "members": {"n": Type.Number()}})
    V1 = local("t.fork-adapted", {"version": 1, "input": Type.Object({}), "members": {"m": Type.Number()}})

    def adapt(current: dict[str, Any]) -> dict[str, Any]:
        calls.append(1)
        return {"m": current["n"]}

    def factory(_input: Any, _principal: Any, self: Any) -> dict[str, Any]:
        return {"n": self.state("n", Type.Number(), 7)}

    host = createHost({"self": {"kind": "host"}, "onError": lambda _error: None})
    host.serve(V2, factory, {"accepts": {1: adapt}})
    old = await host.get(V1, {})
    assert old.m.value == 7
    assert old.m.value == 7
    assert len(calls) == 1
    await host.close()


def test_select_functions_are_the_same_when_their_code_is():
    def make() -> Any:
        return lambda value: value.events

    assert sameSelect(make(), make())
    assert not sameSelect(make(), lambda value: value.other)
    assert sameSelect(None, None)
    assert not sameSelect(make(), None)


def test_the_remote_module_is_remote_ref_so_remote_stays_the_contract_builder():
    assert misaka.chord.remote is remote
    assert callable(misaka.chord.remote)
    import misaka.chord.remote_ref as module

    assert module.RemoteBinding.__name__ == "RemoteBinding"


# ─── links ───────────────────────────────────────────────────────────────────


async def test_a_memory_link_reports_json_text_length_in_utf16_units():
    a, b = memoryLinkPair()
    received: list[Any] = []
    b.receive(received.append)
    size = a.send({"t": "ping", "n": 1, "pad": "😀"})  # type: ignore[typeddict-unknown-key]
    # `{"t":"ping","n":1,"pad":"😀"}`: the emoji counts as two code units, as in JavaScript.
    assert size == len('{"t":"ping","n":1,"pad":"') + 2 + 2
    await settle()
    assert received[0]["pad"] == "😀"


def test_a_packed_websocket_message_round_trips_through_inflate_message():
    value = ["x" * (PACK_THRESHOLD + 10)]
    message = packMessage({"t": "result", "id": 1, "value": value}, True)
    assert isinstance(message, bytes)
    envelope, field, _payload = splitMessage(message)
    assert envelope == {"t": "result", "id": 1} and field == "value"
    assert json.loads(inflateMessage(message)) == {"t": "result", "id": 1, "value": value}


# ─── loader ──────────────────────────────────────────────────────────────────


async def test_each_load_has_fresh_facet_modules_and_one_copy_of_shared_code(tmp_path: Path):
    shared = tmp_path / "shared.py"
    shared.write_text("loads = []\n", encoding="utf-8")
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "state.py").write_text("token = object()\n", encoding="utf-8")
    (plugin / "facet.py").write_text(
        "from misaka.chord import defineFacet\n"
        "from . import state\n"
        "from .. import shared\n"
        "shared.loads.append(state.token)\n"
        "default = defineFacet(lambda api: None)\n",
        encoding="utf-8",
    )
    first = await nodeLoader([str(plugin / "facet.py")])
    second = await nodeLoader([str(plugin / "facet.py")])
    assert first[0].name == (plugin / "facet.py").resolve().as_uri() == second[0].name
    # One shared module object for both loads (outside every facet directory); two fresh copies of the facet's own
    # state module (inside it).
    only = sys.modules[loader._moduleName(loader._SHARED, shared.resolve().with_suffix(""))]
    assert len(only.loads) == 2 and only.loads[0] is not only.loads[1]
