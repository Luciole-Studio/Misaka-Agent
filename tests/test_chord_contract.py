"""pi chord's ``test/contract.test.ts``.

MISAKA adaptations: a contract's ``def`` is ``def_`` (``def`` is a Python keyword); object spreads are dict unpacking;
``assert.throws`` is ``pytest.raises``.
"""

from __future__ import annotations

from typing import Any

import pytest

from misaka.chord import (
    ChordError,
    bind,
    coverage,
    hostFamily,
    hostNamed,
    isChordError,
    local,
    method,
    opaque,
    remote,
)
from misaka.chord._typebox import Type
from misaka.chord.schema import canonicalIdentity, fits, normalize

View = Type.Object({"text": Type.String()})

chatV1 = remote(
    "t.chat",
    {
        "version": 1,
        "input": Type.Object({"session": Type.String(), "conversation": Type.Number({"default": 0})}),
        "host": hostFamily("session:", "session"),
        "variants": {
            "viewer": {"state": {"view": View}},
            "participant": {
                "state": {"view": View},
                "methods": {
                    "send": method(Type.Object({"text": Type.String()}), Type.Object({"id": Type.Number()}))
                },
            },
        },
    },
)


def test_normalizes_input_and_omits_defaults_from_the_identity():
    a = normalize(chatV1.input, {"session": "s", "extra": 1}, "input")
    assert a == {"session": "s", "conversation": 0}
    b = normalize(chatV1.input, {"conversation": 0, "session": "s"}, "input")
    assert canonicalIdentity(chatV1.input, a) == canonicalIdentity(chatV1.input, b)
    assert canonicalIdentity(chatV1.input, a) == '{"session":"s"}'
    with pytest.raises(ChordError) as info:
        normalize(chatV1.input, {"session": 1}, "input")
    assert isChordError(info.value, "invalid") and info.value.reason == "input"


def test_fingerprints_are_stable_and_description_based():
    copy = remote("t.chat", chatV1.def_)
    assert copy.fingerprint == chatV1.fingerprint
    assert remote("t.chat", {**chatV1.def_, "version": 2}).fingerprint != chatV1.fingerprint


def test_rejects_reserved_member_names_and_opaque_remote_members():
    with pytest.raises(TypeError):
        local("t.x", {"version": 1, "input": Type.Object({}), "members": {"status": method()}})
    with pytest.raises(TypeError):
        remote(
            "t.x",
            {
                "host": hostNamed("server"),
                "version": 1,
                "input": Type.Object({}),
                "variants": {"a": {"state": {"h": opaque("x@1")}}},
            },
        )


def test_covers_additive_changes_and_refuses_breaking_ones():
    additive = remote(
        "t.chat",
        {
            **chatV1.def_,
            "variants": {
                **chatV1.def_["variants"],
                "participant": {
                    "state": {"view": Type.Object({"text": Type.String(), "extra": Type.Number()})},
                    "methods": {
                        "send": method(
                            Type.Object({"text": Type.String(), "urgent": Type.Optional(Type.Boolean())}),
                            Type.Object({"id": Type.Number(), "at": Type.Number()}),
                        )
                    },
                },
            },
        },
    )
    assert coverage(additive.description, chatV1.description) is None
    assert bind(additive, None, chatV1) is None

    required = remote(
        "t.chat",
        {
            **chatV1.def_,
            "variants": {
                **chatV1.def_["variants"],
                "participant": {
                    "state": {"view": View},
                    "methods": {
                        "send": method(
                            Type.Object({"text": Type.String(), "urgent": Type.Boolean()}),
                            Type.Object({"id": Type.Number()}),
                        )
                    },
                },
            },
        },
    )
    assert "arguments" in (coverage(required.description, chatV1.description) or "")
    with pytest.raises(ChordError) as info:
        bind(required, None, chatV1)
    assert isChordError(info.value, "contract_mismatch")

    retyped = remote(
        "t.chat",
        {
            **chatV1.def_,
            "variants": {**chatV1.def_["variants"], "viewer": {"state": {"view": Type.Object({"text": Type.Number()})}}},
        },
    )
    assert "state" in (coverage(retyped.description, chatV1.description) or "")


def test_methods_without_arguments_or_result_described_without_them_their_presence_must_match_a_void_consumer_accepts_a_result():
    def of(members: dict[str, Any]) -> dict[str, Any]:
        return local("t.methods", {"version": 1, "input": Type.Object({}), "members": members}).description

    none = of({"m": method()})
    assert none["variants"][""]["m"] == {"kind": "method"}
    withArgs = of({"m": method(Type.String())})
    withResult = of({"m": method(None, Type.Number())})
    assert coverage(none, none) is None
    # The consumer sends none, the provider expects some.
    assert "arguments" in (coverage(withArgs, none) or "")
    # The consumer sends some, the provider takes none.
    assert "arguments" in (coverage(none, withArgs) or "")
    # A void consumer ignores a result.
    assert coverage(withResult, none) is None
    # A consumer expecting a result needs one.
    assert "result" in (coverage(none, withResult) or "")


def test_binds_other_versions_only_through_accepts_adapters():
    v2 = remote("t.chat", {**chatV1.def_, "version": 2, "variants": {"viewer": {"state": {"body": View}}}})
    with pytest.raises(ChordError) as info:
        bind(v2, None, chatV1)
    assert isChordError(info.value, "contract_mismatch")

    def adapter(current: dict[str, Any]) -> dict[str, Any]:
        return {"variant": "viewer", "view": current.get("body")}

    assert bind(v2, {1: adapter}, chatV1) is adapter


def test_compares_opaque_members_by_name_version_and_function_members_structurally():
    a = local(
        "t.idx",
        {
            "version": 1,
            "input": Type.Object({}),
            "members": {
                "harness": opaque("h@1"),
                "onChange": method(Type.Function([Type.String()], Type.Void())),
            },
        },
    )
    b = local("t.idx", {**a.def_, "members": {**a.def_["members"], "harness": opaque("h@2")}})
    assert "opaque" in (coverage(b.description, a.description) or "")
    assert fits(Type.Function([Type.Unknown()], Type.Void()), Type.Function([Type.String()], Type.Void()))
    assert not fits(Type.Function([Type.String()], Type.Void()), Type.Function([Type.Unknown()], Type.Void()))
