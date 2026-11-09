"""pi chord's ``test/delta-diff.test.ts``."""

from __future__ import annotations

import copy
import json

import pytest

from misaka.chord.delta import (
    applyImmutable,
    assertValidOp,
    assertValidWireOp,
    decoder,
    diffRevisions,
    encoder,
)


def expectDiff(before, after, expected) -> None:
    operations = diffRevisions(before, after)
    assert operations == expected
    assert applyImmutable(before, operations) == after


def test_emits_sets_and_deletes():
    expectDiff(
        {"keep": 1, "change": 1, "remove": True},
        {"keep": 1, "change": 2, "add": 3},
        [["s", ["change"], 2], ["s", ["add"], 3], ["d", ["remove"]]],
    )


def test_emits_string_append_and_front_truncation():
    expectDiff({"text": "hello"}, {"text": "hello world"}, [["a", ["text"], " world"]])
    expectDiff({"text": "hello world"}, {"text": "world"}, [["t", ["text"], 6]])
    expectDiff({"text": "abcdefgh"}, {"text": "defghxyz"}, [["t", ["text"], 3], ["a", ["text"], "xyz"]])


def test_represents_array_insertion_removal_and_shift_with_splices():
    a, b, c = {"id": "a"}, {"id": "b"}, {"id": "c"}
    expectDiff({"values": [a, b]}, {"values": [a, c, b]}, [["p", ["values"], 1, 0, [c]]])
    expectDiff({"values": [a, b, c]}, {"values": [b, c]}, [["p", ["values"], 0, 1, []]])


def test_collapses_a_same_length_queue_update_to_two_splices():
    a, b, c, d = {"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}
    expectDiff({"values": [a, b, c]}, {"values": [b, c, d]}, [["p", ["values"], 0, 1, []], ["p", ["values"], 2, 0, [d]]])


def test_emits_a_permutation_for_a_pure_reorder():
    a, b, c = {"id": "a"}, {"id": "b"}, {"id": "c"}
    expectDiff({"values": [a, b, c]}, {"values": [c, a, b]}, [["m", ["values"], [2, 0, 1]]])


def test_normalizes_reordered_distinct_deeply_equal_objects_to_a_no_op():
    first = {"nested": {"value": 1}}
    second = {"nested": {"value": 1}}
    assert first is not second
    assert diffRevisions({"values": [first, second]}, {"values": [second, first]}) == []


def test_validates_and_encodes_permutations():
    operations = [["m", ["values"], [2, 0, 1]], ["m", ["values"], [1, 2, 0]]]
    for operation in operations:
        assertValidOp(operation)
    wire = encoder().encode(operations)
    assert wire == [["m", ["values"], [2, 0, 1]], ["m", [1, 2, 0]]]
    for operation in wire:
        assertValidWireOp(operation)
    assert decoder().decode(wire) == operations
    with pytest.raises(Exception, match="bijection"):
        assertValidOp(["m", ["values"], [0, 0]])


def test_emits_nothing_for_deeply_equal_reconstructed_values():
    assert diffRevisions({"value": {"nested": [1, 2]}}, {"value": {"nested": [1, 2]}}) == []
    assert diffRevisions({"values": [{"id": 1}, {"id": 2}]}, {"values": [{"id": 1}, {"id": 2}]}) == []
    assert diffRevisions({"values": [True, True, True]}, {"values": [True, True, True]}) == []


def test_keeps_a_leaf_edit_inside_a_reconstructed_array_narrow():
    expectDiff(
        {"values": [{"id": 1, "label": "one"}, {"id": 2, "label": "two"}]},
        {"values": [{"id": 1, "label": "one"}, {"id": 2, "label": "changed"}]},
        [["s", ["values", 1, "label"], "changed"]],
    )


def test_emits_payload_free_splices_for_scattered_removals():
    a, b, c, d, e = ({"id": id_} for id_ in "abcde")
    expectDiff({"values": [a, b, c, d, e]}, {"values": [a, c, e]}, [["p", ["values"], 1, 1, []], ["p", ["values"], 2, 1, []]])


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ([1, 2, 3, 4], [2, 3, 4], [["p", ["values"], 0, 1, []]]),
        ([1, 2, 3, 4], [1, 2, 3], [["p", ["values"], 3, 1, []]]),
        ([1, 2, 3, 4], [1, 3, 4], [["p", ["values"], 1, 1, []]]),
        ([1, 2, 3, 4], [], [["p", ["values"], 0, 4, []]]),
        ([1, 2, 3, 4], [1, 2, 3, 4], []),
    ],
    ids=["front", "tail", "middle", "all", "none"],
)
def test_encodes_removal_canonically(before, after, expected):
    expectDiff({"values": list(before)}, {"values": list(after)}, expected)


def test_does_not_field_diff_unrelated_shifted_objects_with_common_fields():
    before = [{"type": "row", "value": value} for value in (1, 2, 3)]
    after = [{"type": "row", "value": value} for value in (2, 3, 4)]
    expectDiff(
        {"values": before},
        {"values": after},
        [["p", ["values"], 0, 1, []], ["p", ["values"], 2, 0, [{"type": "row", "value": 4}]]],
    )


def test_does_not_treat_coincidental_id_or_key_fields_as_structural_identity():
    left = {"value": "left"}
    right = {"value": "right"}
    before = [left, {"id": 1, "key": "a", "value": "first"}, {"id": 2, "key": "b", "value": "second"}, right]
    replacements = [{"id": 2, "key": "b", "value": "edited-second"}, {"id": 1, "key": "a", "value": "edited-first"}]
    after = [left, *replacements, right]
    expectDiff({"values": before}, {"values": after}, [["p", ["values"], 1, 2, replacements]])


def test_combines_removals_append_and_a_shared_subtree_survivor_edit():
    a = {"id": "a", "stable": {}, "detail": {"text": "a"}}
    b = {"id": "b", "stable": {}, "detail": {"text": "b"}}
    c = {"id": "c", "stable": {}, "detail": {"text": "c"}}
    d = {"id": "d", "stable": {}, "detail": {"text": "d"}}
    changedC = {"id": "c", "stable": c["stable"], "detail": {"text": "changed"}}
    appended = {"id": "e", "stable": {}, "detail": {"text": "e"}}
    expectDiff(
        {"values": [a, b, c, d]},
        {"values": [a, changedC, d, appended]},
        [["p", ["values"], 1, 1, []], ["a", ["values", 1, "detail", "text"], "hanged"], ["p", ["values"], 3, 0, [appended]]],
    )


@pytest.mark.parametrize("size", [256 * 1024, 1024 * 1024])
def test_does_not_retain_a_removed_neighbor_payload(size):
    payload = "x" * size
    retained = [{"id": id_, "payload": payload} for id_ in "abcde"]
    before = {"values": list(retained)}
    after = {"values": [retained[0], retained[2], retained[4]]}
    operations = diffRevisions(before, after)
    assert operations == [["p", ["values"], 1, 1, []], ["p", ["values"], 2, 1, []]]
    assert len(json.dumps(operations)) < 100
    assert applyImmutable(before, operations) == after
    assert before["values"] == retained


@pytest.mark.parametrize("size", [1_001, 10_000])
def test_keeps_push_pop_and_middle_removal_narrow(size):
    values = [{"value": value} for value in range(size)]
    appended = {"value": size}
    expectDiff({"values": values}, {"values": [*values, appended]}, [["p", ["values"], size, 0, [appended]]])
    expectDiff({"values": values}, {"values": values[:-1]}, [["p", ["values"], size - 1, 1, []]])
    middle = size // 2
    expectDiff({"values": values}, {"values": [*values[:middle], *values[middle + 1 :]]}, [["p", ["values"], middle, 1, []]])


def test_keeps_forty_thousand_row_sparse_edits_narrow():
    values = [{"value": value, "stable": {"value": value}} for value in range(40_000)]
    after = list(values)
    changed = []
    for index in range(100, len(after), 400):
        after[index] = {"value": -index, "stable": values[index]["stable"]}
        changed.append(index)
    operations = diffRevisions({"values": values}, {"values": after})
    assert operations == [["s", ["values", index, "value"], -index] for index in changed]
    assert len(json.dumps(operations, separators=(",", ":"))) < 7_500
    assert applyImmutable({"values": values}, operations) == {"values": after}


def test_keeps_a_reconstructed_large_array_leaf_edit_narrow():
    before = [{"value": value, "label": f"row-{value}"} for value in range(1_000)]
    after = copy.deepcopy(before)
    after[700]["label"] = "changed"
    operations = diffRevisions({"values": before}, {"values": after})
    assert operations == [["s", ["values", 700, "label"], "changed"]]
    assert len(json.dumps(operations, separators=(",", ":"))) < 100
    assert applyImmutable({"values": before}, operations) == {"values": after}


def test_splices_an_ambiguous_equal_count_moved_and_edited_gap():
    left = {"value": "left"}
    right = {"value": "right"}
    before = [left, {"id": 1, "value": "a"}, {"id": 2, "value": "b"}, right]
    replacements = [{"id": 2, "value": "edited"}, {"id": 1, "value": "also-edited"}]
    after = [left, *replacements, right]
    expectDiff({"values": before}, {"values": after}, [["p", ["values"], 1, 2, replacements]])


def test_keeps_a_large_rotation_payload_free():
    values = [{"value": value} for value in range(10_000)]
    rotated = [*values[1_000:], *values[:1_000]]
    operations = diffRevisions({"values": values}, {"values": rotated})
    assert len(operations) == 1
    assert operations[0][0] == "m"
    assert len(json.dumps(operations, separators=(",", ":"))) < 60_000
    assert applyImmutable({"values": values}, operations) == {"values": rotated}


def test_encodes_five_hundred_unshifts_without_snapshotting_retained_rows():
    retained = [{"value": value, "payload": "x" * 100} for value in range(10_000)]
    inserted = [{"value": -value - 1} for value in range(500)]
    operations = diffRevisions({"values": retained}, {"values": [*inserted, *retained]})
    assert operations == [["p", ["values"], 0, 0, inserted]]
    assert len(json.dumps(operations, separators=(",", ":"))) < 20_000
    assert applyImmutable({"values": retained}, operations) == {"values": [*inserted, *retained]}


def test_bounds_wide_object_operation_emission_with_a_root_replacement():
    before = {f"field{index}": 0 for index in range(20_000)}
    after = {f"field{index}": 1 for index in range(20_000)}
    assert diffRevisions(before, after) == [["r", after]]


def test_bounds_a_wide_normalized_array_fallback_with_a_root_replacement():
    before = {"values": [0] * 40_000}
    after = {"values": [1] * 40_000}
    operations = diffRevisions(before, after)
    assert operations == [["r", after]]
    assert applyImmutable(before, operations) == after
