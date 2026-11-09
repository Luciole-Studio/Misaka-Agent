"""pi chord's ``test/delta.test.ts``.

Cases about ``Object.prototype`` setters, property accessors and frozen objects have no counterpart in Python
and are left out.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from misaka.chord.delta import (
    UnsafePathError,
    apply,
    applyImmutable,
    assertValidOp,
    assertValidWireOp,
    decoder,
    encoder,
    isBase,
    overlap,
    track,
)
from misaka.utils.js import UNDEFINED

# -- immutable tracker lifecycle -----------------------------------------------------------------


async def test_keeps_a_draft_alive_across_await_and_adopts_only_a_prepared_change():
    input_ = {"count": 1, "nested": {"text": "a"}, "values": [1]}
    tracker = track(input_)
    assert tracker.value is input_

    change = tracker.beginChange()
    change.state["count"] = 2
    await asyncio.sleep(0)
    change.state["nested"]["text"] += "b"
    change.state["values"].push(2)
    assert tracker.value == {"count": 1, "nested": {"text": "a"}, "values": [1]}

    prepared = change.prepare()
    assert prepared.base is tracker.value
    assert prepared.value == {"count": 2, "nested": {"text": "ab"}, "values": [1, 2]}
    assert prepared.ops == [
        ["s", ["count"], 2],
        ["a", ["nested", "text"], "b"],
        ["p", ["values"], 1, 0, [2]],
    ]
    with pytest.raises(TypeError):
        change.state["count"]
    assert tracker.value == {"count": 1, "nested": {"text": "a"}, "values": [1]}

    tracker.adopt(prepared)
    assert tracker.value is prepared.value


def test_aborts_and_revokes_without_changing_the_committed_value():
    tracker = track({"child": {"value": 1}})
    change = tracker.beginChange()
    child = change.state["child"]
    child["value"] = 2
    change.abort()
    assert tracker.value["child"]["value"] == 1
    with pytest.raises(TypeError):
        child["value"]
    change.abort()
    with pytest.raises(Exception, match="settled"):
        change.prepare()
    tracker.beginChange().abort()


def test_grows_arrays_with_explicit_nulls_and_revokes_drafts_after_preparation():
    tracker = track({"values": [1, 2]})
    change = tracker.beginChange()
    change.state["values"].length = 4
    prepared = change.prepare()
    assert prepared.value["values"] == [1, 2, None, None]
    with pytest.raises(TypeError):
        change.state["values"]
    tracker.adopt(prepared)


def test_normalizes_a_deep_no_op_to_exact_previous_identity():
    tracker = track({"value": {"nested": [1, 2]}})
    change = tracker.beginChange()
    change.state["value"] = {"nested": [1, 2]}
    prepared = change.prepare()
    assert prepared.value is prepared.base
    assert prepared.ops == []
    tracker.adopt(prepared)
    assert tracker.value is prepared.base


def test_takes_immutable_ownership_of_replacement_input_and_applies_no_op_normalization():
    tracker = track({"nested": {"value": 1}})
    replacement = {"nested": {"value": 2}}
    prepared = tracker.prepareReplace(replacement)
    assert prepared.value is replacement
    assert prepared.ops == [["r", replacement]]
    tracker.adopt(prepared)

    noOp = tracker.prepareReplace({"nested": {"value": 2}})
    assert noOp.value is tracker.value
    assert noOp.ops == []


def test_shares_immutable_operation_placements_with_the_prepared_candidate():
    tracker = track({"rows": []})
    change = tracker.beginChange()
    change.state["rows"].push({"id": 1})
    prepared = change.prepare()
    splice = prepared.ops[0]
    assert splice[0] == "p"
    assert splice[4][0] is prepared.value["rows"][0]


def test_rejects_foreign_stale_and_repeated_preparations_while_allowing_competing_changes():
    first = track({"value": 0})
    second = track({"value": 0})
    change = first.beginChange()
    competing = first.beginChange()
    change.state["value"] = 1
    competing.state["value"] = 2
    prepared = change.prepare()
    competingPrepared = competing.prepare()
    with pytest.raises(Exception, match="different tracker"):
        second.adopt(prepared)
    first.adopt(prepared)
    with pytest.raises(Exception, match="already been used"):
        first.adopt(prepared)
    with pytest.raises(Exception, match="stale"):
        first.adopt(competingPrepared)

    stale = first.prepareReplace({"value": 2})
    winner = first.prepareReplace({"value": 3})
    first.adopt(winner)
    with pytest.raises(Exception, match="stale"):
        first.adopt(stale)
    with pytest.raises(Exception, match="stale"):
        first.adopt(stale)


def test_invalidates_a_prepared_result_when_its_change_is_aborted():
    tracker = track({"value": 0})
    change = tracker.beginChange()
    change.state["value"] = 1
    prepared = change.prepare()
    change.abort()
    with pytest.raises(Exception, match="aborted"):
        tracker.adopt(prepared)
    change.abort()


def test_makes_competing_same_base_preparations_stale_after_adopting_a_no_op():
    tracker = track({"value": {"count": 1}})
    first = tracker.prepareReplace({"value": {"count": 1}})
    competing = tracker.prepareReplace({"value": {"count": 1}})
    assert first.value is first.base
    assert competing.value is competing.base
    tracker.adopt(first)
    with pytest.raises(Exception, match="already been used"):
        tracker.adopt(first)
    with pytest.raises(Exception, match="stale"):
        tracker.adopt(competing)


def test_emits_an_owned_root_replacement_without_traversing_large_replacement_input():
    rows = [{"value": value, "stable": {"value": value}} for value in range(10_000)]
    tracker = track({"rows": rows})
    replacement = {"rows": list(tracker.value["rows"])}
    replacement["rows"][5_000] = {"value": -1, "stable": replacement["rows"][5_000]["stable"]}
    prepared = tracker.prepareReplace(replacement)
    assert prepared.value is replacement
    assert prepared.ops == [["r", replacement]]
    assert applyImmutable(prepared.base, prepared.ops) == prepared.value


# -- canonical strings ---------------------------------------------------------------------------


def test_emits_append_and_rolling_window_operations():
    tracker = track({"text": "abcdefgh"})
    change = tracker.beginChange()
    change.state["text"] += "ij"
    prepared = change.prepare()
    assert prepared.ops == [["a", ["text"], "ij"]]
    tracker.adopt(prepared)

    change = tracker.beginChange()
    change.state["text"] = f"{change.state['text'][3:]}xyz"
    prepared = change.prepare()
    assert prepared.ops == [["t", ["text"], 3], ["a", ["text"], "xyz"]]


def test_finds_bounded_overlaps():
    assert overlap("abcdefgh", "defghxyz", 65_536) == 5
    assert overlap("abcdef", "defghi", 0) == 0


def test_counts_overlaps_and_truncations_in_utf16_code_units():
    # An astral character is two code units, so a batch means the same here as on a TypeScript peer.
    emoji = chr(0x1F600)
    tracker = track({"text": f"a{emoji}bcd"})
    change = tracker.beginChange()
    change.state["text"] = "bcdxy"
    prepared = change.prepare()
    assert prepared.ops == [["t", ["text"], 3], ["a", ["text"], "xy"]]
    assert replay(tracker.value, prepared.ops) == prepared.value
    assert overlap(f"a{emoji}", f"{emoji}z", 65_536) == 2


def replay(base, ops):
    return apply(json.loads(json.dumps(base)), json.loads(json.dumps(ops)))


# -- operation application and validation --------------------------------------------------------


def test_applies_mutable_and_immutable_operations():
    operations = [
        ["a", ["text"], "b"],
        ["p", ["values"], 1, 1, [3, 4]],
        ["s", ["nested", "value"], 2],
    ]
    base = {"text": "a", "values": [1, 2], "nested": {"value": 1}, "stable": {"value": 9}}
    immutable = applyImmutable(base, operations)
    assert immutable == {"text": "ab", "values": [1, 3, 4], "nested": {"value": 2}, "stable": {"value": 9}}
    assert base == {"text": "a", "values": [1, 2], "nested": {"value": 1}, "stable": {"value": 9}}
    assert immutable["stable"] is base["stable"]
    assert apply(json.loads(json.dumps(base)), operations) == immutable


def test_supports_root_replacement_root_splice_and_permutations():
    base = [["r", [1, 2, 3]]]
    assert isBase(base) is True
    value = apply(UNDEFINED, base)
    value = apply(value, [["p", [], 1, 1, [4]]])
    value = apply(value, [["m", [], [2, 0, 1]]])
    assert value == [3, 1, 4]


def test_rejects_unsafe_and_malformed_paths():
    with pytest.raises(UnsafePathError):
        apply({}, [["s", ["constructor", "prototype", "x"], True]])
    with pytest.raises(UnsafePathError):
        apply({"values": [1]}, [["s", ["values", 3], 2]])
    with pytest.raises(Exception):  # noqa: B017 - upstream `toThrow()` takes any error
        apply({"value": 1}, [["a", ["value"], "x"]])
    with pytest.raises(Exception):  # noqa: B017
        assertValidOp(["s", "value", 1])
    with pytest.raises(Exception, match="bijection"):
        assertValidOp(["m", [], [0, 0]])


def test_validates_immutable_operations_before_traversing_the_target():
    reads = []

    class Trap(dict):
        def __getitem__(self, key):
            reads.append(key)
            return {}

        def get(self, key, default=None):
            reads.append(key)
            return {}

    with pytest.raises(Exception, match="path"):
        applyImmutable(Trap(trap=1), [["s", "bad-path", 1]])
    assert reads == []


def test_validates_decoded_and_wire_vocabularies_separately():
    assertValidOp(["s", ["value"], 1])
    with pytest.raises(Exception):  # noqa: B017
        assertValidOp(["s", 1])
    assertValidWireOp(["s", 1])
    assertValidWireOp(["#", 0, ["value"]])


# -- codec ---------------------------------------------------------------------------------------


def test_interns_paths_omits_adjacent_paths_and_round_trips():
    path = ["nested", "text"]
    enc = encoder()
    dec = decoder()
    first = [["t", path, 1], ["a", path, "x"]]
    assert dec.decode(enc.encode(first)) == first
    second = [["a", path, "y"]]
    wire = enc.encode(second)
    assert wire == [["#", 0, path], ["a", 0, "y"]]
    assert dec.decode(wire) == second


def test_resets_path_dictionaries_on_a_base():
    enc = encoder()
    path = ["value"]
    enc.encode([["s", path, 1]])
    enc.encode([["s", path, 2]])
    assert enc.encode([["r", {"value": 3}]]) == [["r", {"value": 3}]]
    assert enc.encode([["s", path, 4]]) == [["s", path, 4]]


def test_rejects_unresolved_short_forms_and_unsafe_interned_paths():
    with pytest.raises(Exception):  # noqa: B017
        decoder().decode([["a", "x"]])
    wire = [["#", 0, ["__proto__"]], ["s", 0, True]]
    with pytest.raises(UnsafePathError):
        decoder().decode(wire)


def test_omits_an_adjacent_repeated_path():
    enc = encoder()
    path = ["value"]
    assert enc.encode([["s", path, 1], ["s", path, 2]]) == [["s", path, 1], ["s", 2]]


def test_interns_on_second_use_rather_than_first():
    enc = encoder()
    path = ["a", "deep"]
    assert enc.encode([["a", path, "1"]]) == [["a", path, "1"]]
    assert enc.encode([["a", path, "2"]]) == [["#", 0, path], ["a", 0, "2"]]


def test_does_not_collide_paths_containing_null_characters():
    operations = [["s", ["a" + chr(0) + "b"], 1], ["s", ["a", "b"], 2]]
    assert decoder().decode(encoder().encode(operations)) == operations


def test_clears_decoder_ids_on_a_base_batch():
    dec = decoder()
    dec.decode([["#", 0, ["a"]], ["a", 0, "1"]])
    dec.decode([["r", {"a": ""}]])
    with pytest.raises(Exception):  # noqa: B017
        dec.decode([["a", 0, "2"]])


def test_makes_batches_after_a_base_self_contained():
    enc = encoder()
    path = ["a", "deep"]
    enc.encode([["a", path, "1"]])
    enc.encode([["a", path, "2"]])
    base = enc.encode([["r", {"a": {"deep": "x"}}]])
    after = enc.encode([["a", path, "3"]])
    assert after == [["a", path, "3"]]
    dec = decoder()
    assert dec.decode(base) == [["r", {"a": {"deep": "x"}}]]
    assert dec.decode(after) == [["a", path, "3"]]


def test_round_trips_deterministic_mixed_operation_streams():
    batches = [
        [["s", ["rows", index, "value"], index], ["a", ["output"], str(index)], ["p", ["tail"], index, 0, [index]]]
        for index in range(100)
    ]
    enc = encoder()
    dec = decoder()
    assert [dec.decode(enc.encode(batch)) for batch in batches] == batches


# -- immutable application ownership -------------------------------------------------------------


def test_does_not_mutate_a_replacement_payload_targeted_by_a_later_operation():
    replacement = {"nested": {"value": 1}}
    next_ = applyImmutable(UNDEFINED, [["r", replacement], ["s", ["nested", "value"], 2]])
    assert replacement["nested"]["value"] == 1
    assert next_["nested"]["value"] == 2


def test_adopts_a_mutable_replacement_payload_rather_than_copying_it():
    batch = [["r", {"value": 0}]]
    first = apply(UNDEFINED, batch)
    second = apply(UNDEFINED, batch)
    first["value"] = 1
    assert second["value"] == 1


# -- path and prototype safety -------------------------------------------------------------------


def test_rejects_constructor_walks_and_forbidden_interned_paths():
    with pytest.raises(Exception):  # noqa: B017
        apply({}, json.loads('[["s",["constructor","prototype","gadget"],true]]'))
    assert {}.get("gadget") is None
    wire = [["#", 0, ["__proto__", "w"]], ["s", 0, True]]
    with pytest.raises(Exception):  # noqa: B017
        decoder().decode(wire)


def test_applies_plain_keys_without_side_effects():
    assert apply({}, [["s", ["trap"], 1]]) == {"trap": 1}


def test_allows_reserved_names_inside_values():
    value = json.loads('{"__proto__":{"z":1}}')
    out = apply({}, [["s", ["value"], value]])
    assert "__proto__" in out["value"]
    assert {}.get("z") is None


# -- array index safety --------------------------------------------------------------------------


def test_writes_an_existing_index_and_appends_exactly_one_past_the_end():
    assert apply({"values": [1, 2, 3]}, [["s", ["values", 1], 9]]) == {"values": [1, 9, 3]}
    assert apply({"values": [1, 2, 3]}, [["s", ["values", 3], 9]]) == {"values": [1, 2, 3, 9]}


def test_rejects_gaps_huge_indices_and_string_spelled_indices():
    for target, operation in (
        ({"values": [1, 2, 3]}, ["s", ["values", 5], 9]),
        ({"values": []}, ["s", ["values", 4_294_967_290], 1]),
        ({"values": [1]}, ["s", ["values", "0"], 9]),
        ({"values": ["a"]}, ["a", ["values", "0"], "b"]),
    ):
        with pytest.raises(Exception):  # noqa: B017
            apply(target, [operation])


def test_allows_explicit_growth_values_and_rejects_deletion_past_the_end():
    assert apply({"values": [1]}, [["p", ["values"], 1, 0, [None, None, 9]]]) == {"values": [1, None, None, 9]}
    with pytest.raises(Exception):  # noqa: B017
        apply({"values": [1]}, [["d", ["values", 1]]])


def test_applies_large_splice_payloads():
    items = [None] * 300_000
    result = apply({"values": []}, [["p", ["values"], 0, 0, items]])
    assert len(result["values"]) == len(items)


# -- operation structure safety ------------------------------------------------------------------


def test_rejects_unknown_verbs_malformed_tuples_paths_and_splice_payloads():
    for target, operation in (
        ({"value": 1}, ["ZZZ", ["value"], 9]),
        ({"values": [1]}, ["p", ["values"], 0, 0, "not-an-array"]),
        ({"value": 1}, ["s", "value", 9]),
        ({"value": 1}, {"op": "s"}),
        ({"value": 1}, None),
    ):
        with pytest.raises(Exception):  # noqa: B017
            apply(target, [operation])


def test_rejects_invalid_append_and_truncation_operations():
    with pytest.raises(Exception):  # noqa: B017
        apply({"value": 1}, [["a", ["missing"], "x"]])
    with pytest.raises(Exception):  # noqa: B017
        apply({"value": 1}, [["a", ["value"], "x"]])
    with pytest.raises(Exception):  # noqa: B017
        apply({"value": "abc"}, [["t", ["value"], -1]])
    with pytest.raises(Exception):  # noqa: B017
        decoder().decode([["t", ["value"], -1]])


def test_clamps_splice_removal_past_the_end():
    assert apply({"values": [1, 2]}, [["p", ["values"], 0, 1e9, []]]) == {"values": []}


# -- operation assertions ------------------------------------------------------------------------


def test_accepts_decoded_operations_and_rejects_wire_only_forms():
    for operation in (
        ["r", {"value": 1}],
        ["s", ["value"], 1],
        ["d", ["value"]],
        ["a", ["value"], "x"],
        ["t", ["value"], 2],
        ["p", ["value"], 0, 0, []],
        ["m", ["value"], [0]],
    ):
        assertValidOp(operation)
    for wireOnly in (
        ["s", 1],
        ["d"],
        ["a", "x"],
        ["t", 2],
        ["p", 0, 0, []],
        ["#", 0, ["value"]],
        ["s", 0, 1],
    ):
        with pytest.raises(Exception):  # noqa: B017
            assertValidOp(wireOnly)
        assertValidWireOp(wireOnly)


def test_does_not_recursively_inspect_operation_payloads():
    assertValidOp(["s", ["value"], object()])
    assertValidWireOp(["r", object()])
