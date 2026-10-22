"""pi chord's ``test/delta-tracker/tracker.test.ts``.

A draft is read and written with item access (``draft["count"]``); ``UNDEFINED`` assigned to a key deletes it.
Cases about JavaScript's ``Proxy`` internals (``isProxy``, property descriptors, Promise resolution reading
``then``) have no counterpart and are left out.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from misaka.chord.delta import apply, decoder, encoder, track
from misaka.chord.delta.tracker import DraftArray, DraftObject
from misaka.chord.json import copyJson
from misaka.utils.js import UNDEFINED


def clone(value):
    # One frame per level, so the 512-deep document below stays within the recursion limit.
    if isinstance(value, list):
        return [clone(item) for item in value]
    if isinstance(value, dict):
        return {key: clone(item) for key, item in value.items()}
    return value


def replay(base, operations):
    return apply(clone(base), clone(operations))


def expectAliasFree(value) -> None:
    seen: dict[int, str] = {}

    def visit(current, path: str) -> None:
        if not isinstance(current, list | dict):
            return
        previous = seen.get(id(current))
        if previous is not None:
            raise AssertionError(f"container at {path} aliases {previous}")
        seen[id(current)] = path
        if isinstance(current, list):
            for index, item in enumerate(current):
                visit(item, f"{path}[{index}]")
            return
        for key, item in current.items():
            visit(item, f"{path}.{key}")

    visit(value, "$root")


def settle(tracker, mutate):
    baseRoot = tracker.value
    base = clone(baseRoot)
    change = tracker.beginChange()
    mutate(change.state)
    prepared = change.prepare()
    candidate = clone(prepared.value)
    assert not isinstance(prepared.value, DraftObject | DraftArray)
    assert replay(base, prepared.ops) == candidate
    assert baseRoot == base
    tracker.adopt(prepared)
    assert baseRoot == base
    assert tracker.value is prepared.value
    assert tracker.value == candidate
    return candidate


# -- transactional overlay lifecycle -------------------------------------------------------------


def test_materializes_an_immutable_next_revision_and_adopts_it_by_pointer_swap():
    initial = {"count": 1, "nested": {"text": "a"}, "values": [1]}
    tracker = track(initial)
    assert tracker.value is initial
    change = tracker.beginChange()
    change.state["count"] = 2
    change.state["nested"]["text"] += "b"
    change.state["values"].push(2)
    assert initial == {"count": 1, "nested": {"text": "a"}, "values": [1]}
    prepared = change.prepare()
    next_ = prepared.value
    assert prepared.baseRevision == 0
    assert prepared.base is initial
    assert next_ is not initial
    assert not isinstance(next_, DraftObject | DraftArray)
    assert next_ == {"count": 2, "nested": {"text": "ab"}, "values": [1, 2]}
    assert prepared.ops == [
        ["s", ["count"], 2],
        ["a", ["nested", "text"], "b"],
        ["p", ["values"], 1, 0, [2]],
    ]
    assert tracker.value is initial
    tracker.adopt(prepared)
    assert initial == {"count": 1, "nested": {"text": "a"}, "values": [1]}
    assert tracker.value is next_
    assert prepared.value is next_
    assert prepared.base is initial
    with pytest.raises(TypeError):
        change.state["count"]


async def test_keeps_a_transaction_live_across_await_and_seals_it_only_at_prepare():
    tracker = track({"left": 0, "nested": {"right": 0}})
    change = tracker.beginChange()
    change.state["left"] = 1
    await asyncio.sleep(0)
    change.state["nested"]["right"] = 2
    prepared = change.prepare()
    assert prepared.value == {"left": 1, "nested": {"right": 2}}
    with pytest.raises(TypeError, match="settled"):
        change.state["left"] = 3
    tracker.adopt(prepared)


def test_reads_a_key_named_then_and_seals_large_drafts_at_prepare():
    tracker = track({"then": "document-value", "value": 1})
    change = tracker.beginChange()
    draft = change.state
    assert draft["then"] == "document-value"
    draft["value"] = 2
    prepared = change.prepare()
    with pytest.raises(TypeError, match="settled"):
        draft["value"]
    tracker.adopt(prepared)
    assert tracker.value["then"] == "document-value"
    assert tracker.value["value"] == 2

    largeTracker = track({"rows": [{"value": value} for value in range(4_100)]})
    largeChange = largeTracker.beginChange()
    largeDraft = largeChange.state
    for row in largeDraft["rows"]:
        assert row["value"] >= 0
    largeDraft["rows"][0]["value"] = -1
    largePrepared = largeChange.prepare()
    with pytest.raises(TypeError, match="settled"):
        largeDraft["rows"]
    largeTracker.adopt(largePrepared)


def test_aborts_idempotently_and_revokes_held_descendants():
    tracker = track({"child": {"value": 1}})
    change = tracker.beginChange()
    child = change.state["child"]
    child["value"] = 2
    change.abort()
    change.abort()
    assert tracker.value["child"]["value"] == 1
    with pytest.raises(TypeError):
        child["value"]
    with pytest.raises(Exception, match="settled"):
        change.prepare()


def test_allows_competing_contexts_and_invalidates_loser_views():
    tracker = track({"value": 0})
    first = tracker.beginChange()
    second = tracker.beginChange()
    first.state["value"] = 1
    second.state["value"] = 2
    firstPrepared = first.prepare()
    secondPrepared = second.prepare()
    held = secondPrepared.value
    tracker.adopt(firstPrepared)
    assert tracker.value["value"] == 1
    assert held["value"] == 2
    with pytest.raises(Exception, match="stale"):
        tracker.adopt(secondPrepared)
    with pytest.raises(Exception, match="already been used"):
        tracker.adopt(firstPrepared)


def test_invalidates_competing_open_overlays_without_changing_their_base_revision():
    initial = {"rows": [{"value": 0}, {"value": 1}]}
    tracker = track(initial)
    staleChange = tracker.beginChange()
    held = staleChange.state["rows"][1]
    held["value"] = 2

    winner = tracker.beginChange()
    winner.state["rows"][0]["value"] = 3
    prepared = winner.prepare()
    tracker.adopt(prepared)

    assert initial == {"rows": [{"value": 0}, {"value": 1}]}
    assert tracker.value == {"rows": [{"value": 3}, {"value": 1}]}
    with pytest.raises(Exception, match="settled"):
        held["value"]
    with pytest.raises(Exception, match="settled"):
        staleChange.prepare()
    staleChange.abort()


def test_adopts_object_edits_as_ordinary_immutable_revisions():
    initial = {"first": 1, "second": 2}
    tracker = track(initial)
    change = tracker.beginChange()
    change.state["first"] = 3
    del change.state["second"]
    change.state["third"] = 4
    prepared = change.prepare()
    tracker.adopt(prepared)
    assert tracker.value is not initial
    assert initial == {"first": 1, "second": 2}
    assert tracker.value == {"first": 3, "third": 4}


def test_supports_adopt_then_publish_ordering_and_retained_publication_reads():
    tracker = track({"value": 0, "nested": {"count": 0}})
    change = tracker.beginChange()
    change.state["value"] = 1
    change.state["nested"]["count"] = 1
    prepared = change.prepare()
    operations = prepared.ops
    tracker.adopt(prepared)
    # Matches services/state.ts: adopt first, then inspect ops and publish prepared.value.
    assert prepared.ops is operations
    published = None if len(prepared.ops) == 0 else prepared.value
    assert len(operations) == 2
    assert published is not None
    assert published is tracker.value
    assert clone(published) == {"value": 1, "nested": {"count": 1}}

    next_ = tracker.beginChange()
    next_.state["value"] = 2
    nextPrepared = next_.prepare()
    tracker.adopt(nextPrepared)
    # Retained immutable publications remain at their original revision.
    assert published is not tracker.value
    assert prepared.value == {"value": 1, "nested": {"count": 1}}
    assert tracker.value == {"value": 2, "nested": {"count": 1}}


def test_keeps_replacement_base_and_value_structurally_compatible_and_readable_after_adoption():
    original = {"value": 0}
    replacement = {"value": 1}
    tracker = track(original)
    prepared = tracker.prepareReplace(replacement)
    assert prepared.base is original
    assert prepared.value is replacement
    tracker.adopt(prepared)
    assert prepared.base is original
    assert prepared.value is replacement
    assert prepared.value is tracker.value


def test_keeps_aborted_and_stale_materialized_candidates_readable():
    tracker = track({"value": 0})
    abortedChange = tracker.beginChange()
    abortedChange.state["value"] = 1
    aborted = abortedChange.prepare()
    abortedView = aborted.value
    aborted.abort()
    assert abortedView["value"] == 1
    assert aborted.value["value"] == 1

    loserChange = tracker.beginChange()
    loserChange.state["value"] = 2
    loser = loserChange.prepare()
    loserView = loser.value
    winner = tracker.prepareReplace({"value": 3})
    tracker.adopt(winner)
    assert loserView["value"] == 2
    assert loser.value["value"] == 2


def test_lets_a_settled_change_abort_its_prepared_result_without_retaining_its_context():
    tracker = track({"value": 0, "nested": {"value": 1}})
    change = tracker.beginChange()
    change.state["value"] = 1
    prepared = change.prepare()
    operations = prepared.ops
    change.abort()
    change.abort()
    assert prepared.ops is operations
    assert prepared.value == {"value": 1, "nested": {"value": 1}}
    with pytest.raises(Exception, match="aborted"):
        tracker.adopt(prepared)


def test_rejects_foreign_and_aborted_prepared_values():
    first = track({"value": 0})
    second = track({"value": 0})
    prepared = first.prepareReplace({"value": 1})
    with pytest.raises(Exception, match="different tracker"):
        second.adopt(prepared)
    prepared.abort()
    prepared.abort()
    with pytest.raises(Exception, match="aborted"):
        first.adopt(prepared)


def test_reads_deleted_own_properties_as_absent():
    tracker = track({"value": 1})
    change = tracker.beginChange()
    del change.state["value"]
    change.state["other"] = 1
    del change.state["other"]
    assert change.state["value"] is UNDEFINED
    assert change.state["other"] is UNDEFINED
    assert "value" not in change.state
    prepared = change.prepare()
    assert "value" not in prepared.value
    tracker.adopt(prepared)
    assert list(tracker.value) == []


def plain(value):
    if isinstance(value, DraftArray):
        return [plain(item) for item in value]
    if isinstance(value, DraftObject):
        return {key: plain(item) for key, item in value.items()}
    return value


def test_detaches_old_handles_for_deeply_equal_object_and_array_assignments_before_normalizing():
    initial = {
        "object": {"nested": {"value": 1}},
        "array": [{"value": 1}],
        "rows": [{"value": 1}],
    }
    tracker = track(initial)
    change = tracker.beginChange()
    oldObject = change.state["object"]
    oldArray = change.state["array"]
    oldRow = change.state["rows"][0]
    change.state["object"] = {"nested": {"value": 1}}
    change.state["array"] = [{"value": 1}]
    change.state["rows"][0] = {"value": 1}
    assert change.state["object"] is not oldObject
    assert change.state["array"] is not oldArray
    assert change.state["rows"][0] is not oldRow
    oldObject["nested"]["value"] = 9
    oldArray[0]["value"] = 9
    oldRow["value"] = 9
    assert plain(change.state) == {
        "object": {"nested": {"value": 1}},
        "array": [{"value": 1}],
        "rows": [{"value": 1}],
    }
    prepared = change.prepare()
    assert prepared.ops == []
    tracker.adopt(prepared)
    assert tracker.value is initial
    assert tracker.value["object"] is initial["object"]
    assert tracker.value["array"] is initial["array"]
    assert tracker.value["rows"][0] is initial["rows"][0]


def test_normalizes_a_deeply_equal_replacement_without_changing_committed_identity():
    initial = {"nested": {"value": 1}, "rows": [1, 2, 3]}
    tracker = track(initial)
    prepared = tracker.prepareReplace({"nested": {"value": 1}, "rows": [1, 2, 3]})
    assert prepared.ops == []
    tracker.adopt(prepared)
    assert tracker.value is initial


def test_takes_o1_immutable_ownership_for_replacement_and_its_operation_payload():
    tracker = track({"value": 0, "rows": []})
    replacement = {"value": 1, "rows": [{"value": 2}]}
    prepared = tracker.prepareReplace(replacement)
    assert prepared.base is tracker.value
    assert prepared.value is replacement
    operations = prepared.ops
    assert operations == [["r", replacement]]
    assert operations[0][1] is replacement
    tracker.adopt(prepared)
    assert tracker.value is replacement


# -- policy view ---------------------------------------------------------------------------------


def test_supports_native_reads_keys_iteration_map_and_json():
    tracker = track({"values": [1, 2, 3], "object": {"a": 1}})
    change = tracker.beginChange()
    change.state["values"].splice(1, 1, 4, 5)
    change.state["object"]["b"] = 2
    assert isinstance(change.state["values"], DraftArray)
    assert len(change.state["values"]) == 4
    assert change.state["values"].length == 4
    assert list(change.state["values"]) == [1, 4, 5, 3]
    assert [value * 2 for value in change.state["values"]] == [2, 8, 10, 6]
    assert list(change.state["object"]) == ["a", "b"]
    assert change.state["object"].keys() == ["a", "b"]
    assert plain(change.state) == {"values": [1, 4, 5, 3], "object": {"a": 1, "b": 2}}
    # MISAKA fork: a draft is not a ``dict`` or a ``list``, so the JSON round trip goes through ``copyJson``.
    assert json.loads(json.dumps(copyJson(change.state))) == {"values": [1, 4, 5, 3], "object": {"a": 1, "b": 2}}
    change.abort()


def test_keeps_document_keys_distinct_from_object_proxy_target_fields():
    tracker = track({"object": {"context": 1, "base": 2, "parent": 3, "dirty": 4, "target": 5, "proxy": 6}})
    change = tracker.beginChange()
    change.state["object"]["context"] = 7
    change.state["object"]["proxy"] = 8
    assert list(change.state["object"]) == ["context", "base", "parent", "dirty", "target", "proxy"]
    prepared = change.prepare()
    assert prepared.value["object"] == {"context": 7, "base": 2, "parent": 3, "dirty": 4, "target": 5, "proxy": 8}
    tracker.adopt(prepared)


def test_keeps_string_operation_forms():
    tracker = track({"text": "abcdefgh", "values": [1, 2, 3], "marker": 0})
    change = tracker.beginChange()
    change.state["text"] = f"{change.state['text'][3:]}xyz"
    change.state["marker"] = 1
    change.state["values"].splice(1, 1, 4)
    prepared = change.prepare()
    assert prepared.value == {"text": "defghxyz", "values": [1, 4, 3], "marker": 1}
    assert ["t", ["text"], 3] in prepared.ops
    assert ["a", ["text"], "xyz"] in prepared.ops
    assert replay(tracker.value, decoder().decode(encoder().encode(prepared.ops))) == prepared.value
    tracker.adopt(prepared)


def test_emits_deletion_and_supports_root_arrays():
    objectTracker = track({"keep": 1, "remove": 2})

    def deleteRemove(draft):
        del draft["remove"]

    settle(objectTracker, deleteRemove)
    assert objectTracker.value == {"keep": 1}

    arrayTracker = track([1, 2, 3])

    def reverseAndPush(draft):
        draft.reverse()
        draft.push(4)

    settle(arrayTracker, reverseAndPush)
    assert arrayTracker.value == [3, 2, 1, 4]


def test_preserves_key_order_after_delete_and_re_add():
    tracker = track({"first": 1, "second": 2})
    change = tracker.beginChange()
    del change.state["first"]
    change.state["first"] = 1
    assert list(change.state) == ["second", "first"]
    prepared = change.prepare()
    tracker.adopt(prepared)
    assert list(tracker.value) == ["second", "first"]


def test_normalizes_deeply_equal_container_assignments_before_direct_candidate_materialization():
    initial = {"child": {"a": 1, "b": 2}, "count": 0}
    tracker = track(initial)
    change = tracker.beginChange()
    change.state["child"] = {"b": 2, "a": 1}
    change.state["count"] = 1
    prepared = change.prepare()
    replayed = replay(initial, prepared.ops)
    assert prepared.ops == [["s", ["count"], 1]]
    assert list(prepared.value["child"]) == ["a", "b"]
    assert list(replayed["child"]) == ["a", "b"]
    assert prepared.value == replayed
    tracker.adopt(prepared)


def test_does_not_report_methods_as_array_entries():
    tracker = track({"values": [1]})
    change = tracker.beginChange()
    assert not change.state["values"].has("map")
    assert change.state["values"].has(0)
    assert not change.state["values"].has(1)
    change.abort()


def test_keeps_repeated_identical_writes_instead_of_reverting_their_pending_override():
    tracker = track({"value": 0, "values": [0]})

    def mutate(draft):
        draft["value"] = 1
        draft["value"] = 1
        draft["values"][0] = None
        draft["values"][0] = None
        draft["values"].push(2)
        draft["values"][1] = None
        draft["values"][1] = None

    settle(tracker, mutate)
    assert tracker.value == {"value": 1, "values": [None, None]}


def test_rejects_array_index_gaps_without_mutation_while_allowing_replacement_and_append():
    tracker = track({"values": [1, 2]})
    change = tracker.beginChange()
    with pytest.raises(TypeError, match="holes"):
        change.state["values"][3] = 4
    assert plain(change.state["values"]) == [1, 2]
    assert tracker.value["values"] == [1, 2]

    change.state["values"][1] = 9
    change.state["values"][2] = 3
    prepared = change.prepare()
    assert prepared.value["values"] == [1, 9, 3]
    assert replay(tracker.value, prepared.ops) == prepared.value
    tracker.adopt(prepared)
    assert tracker.value["values"] == [1, 9, 3]


def test_supports_optional_deletion_explicit_null_length_growth_shrinking_and_default_sort():
    tracker = track({"optional": "remove", "values": [3, 1, 2]})

    def mutate(draft):
        draft["optional"] = UNDEFINED
        draft["values"].length = 5
        assert plain(draft["values"]) == [3, 1, 2, None, None]
        draft["values"].length = 4
        draft["values"].sort()
        with pytest.raises(TypeError, match="holes"):
            del draft["values"][0]

    settle(tracker, mutate)
    assert tracker.value == {"values": [1, 2, 3, None]}


def test_enumerates_wide_objects_without_duplicate_keys():
    tracker = track({"values": {}})
    change = tracker.beginChange()
    for index in range(20_000):
        change.state["values"][f"field{index}"] = index
    keys = list(change.state["values"])
    assert len(keys) == 20_000
    assert keys[0] == "field0"
    assert keys[-1] == "field19999"
    change.abort()


def test_does_not_dirty_read_only_traversals():
    tracker = track({"nested": {"rows": [{"value": 1}]}})
    change = tracker.beginChange()
    assert change.state["nested"]["rows"][0]["value"] == 1
    prepared = change.prepare()
    assert prepared.ops == []
    tracker.adopt(prepared)


def test_uses_one_proxy_identity_per_accessed_container():
    tracker = track({"nested": {"value": 1}, "rows": [{"value": 2}]})
    change = tracker.beginChange()
    assert change.state["nested"] is change.state["nested"]
    assert change.state["rows"] is change.state["rows"]
    assert change.state["rows"][0] is change.state["rows"][0]
    change.abort()


# -- by-value placements -------------------------------------------------------------------------


def test_clones_property_index_push_unshift_splice_fill_and_copy_within_placements():
    tracker = track({"property": None, "values": [{"value": 0}, {"value": 1}, {"value": 2}]})
    external = {"value": 5}
    change = tracker.beginChange()
    change.state["property"] = external
    change.state["values"][0] = external
    change.state["values"].push(external)
    change.state["values"].unshift(external)
    change.state["values"].splice(2, 0, external)
    external["value"] = 99
    change.state["values"].fill(change.state["values"][0], 1, 3)
    change.state["values"].copyWithin(3, 0, 2)
    prepared = change.prepare()
    candidate = clone(prepared.value)
    assert candidate["property"] == {"value": 5}
    assert candidate["values"][:5] == [{"value": 5}] * 5
    tracker.adopt(prepared)


def test_rejects_non_strict_json_placements_before_changing_the_draft():
    initial = {"optional": "remove", "payload": None, "number": 0, "values": [1, 2]}
    tracker = track(initial)
    change = tracker.beginChange()

    with pytest.raises(TypeError, match="strict JSON"):
        change.state["values"][0] = UNDEFINED
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["values"].push({"staged": True}, UNDEFINED)
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["values"].unshift(UNDEFINED)
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["values"].splice(1, 0, UNDEFINED)
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["values"].fill(UNDEFINED)
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["payload"] = {"nested": UNDEFINED}
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["payload"] = {"nested": lambda: 1}
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["payload"] = {"nested": 1j}
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["number"] = float("nan")
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["number"] = float("inf")
    with pytest.raises(TypeError, match="plain objects"):
        change.state["payload"] = {1, 2}
    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises(TypeError, match="cycles"):
        change.state["payload"] = cyclic

    assert plain(change.state) == initial
    change.state["optional"] = UNDEFINED
    change.state["payload"] = {"valid": True}
    change.state["values"].push({"valid": True})
    prepared = change.prepare()
    assert prepared.value == {"payload": {"valid": True}, "number": 0, "values": [1, 2, {"valid": True}]}
    assert replay(initial, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_expands_repeated_source_aliases_into_independent_placements():
    tracker = track({"left": None, "right": None, "rows": []})
    shared = {"nested": {"value": 1}}
    change = tracker.beginChange()
    change.state["left"] = shared
    change.state["right"] = shared
    change.state["rows"].push(shared, shared)
    change.state["left"]["nested"]["value"] = 9
    change.state["rows"][0]["nested"]["value"] = 8
    prepared = change.prepare()
    assert prepared.value == {
        "left": {"nested": {"value": 9}},
        "right": {"nested": {"value": 1}},
        "rows": [{"nested": {"value": 8}}, {"nested": {"value": 1}}],
    }
    assert prepared.value["left"] is not prepared.value["right"]
    assert prepared.value["rows"][0] is not prepared.value["rows"][1]
    assert replay(tracker.value, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_deep_clones_draft_sourced_placements_within_and_across_revisions():
    tracker = track({
        "a": {"child": {"value": 1}},
        "b": None,
        "rows": [{"child": {"value": 1}}, {"child": {"value": 2}}],
    })
    first = tracker.beginChange()
    first.state["b"] = first.state["a"]
    first.state["b"]["child"]["value"] = 3
    first.state["rows"][1] = first.state["rows"][0]
    firstPrepared = first.prepare()
    assert firstPrepared.value["a"]["child"]["value"] == 1
    assert firstPrepared.value["b"]["child"]["value"] == 3
    assert firstPrepared.value["a"] is not firstPrepared.value["b"]
    assert firstPrepared.value["a"]["child"] is not firstPrepared.value["b"]["child"]
    assert firstPrepared.value["rows"][0] is not firstPrepared.value["rows"][1]
    assert firstPrepared.value["rows"][0]["child"] is not firstPrepared.value["rows"][1]["child"]
    expectAliasFree(firstPrepared.value)
    tracker.adopt(firstPrepared)

    base = clone(tracker.value)
    second = tracker.beginChange()
    second.state["rows"][1]["child"]["value"] = 4
    secondPrepared = second.prepare()
    assert [row["child"]["value"] for row in secondPrepared.value["rows"]] == [1, 4]
    assert replay(base, secondPrepared.ops) == secondPrepared.value
    expectAliasFree(secondPrepared.value)
    tracker.adopt(secondPrepared)


def test_distinguishes_raw_committed_references_from_draft_references():
    tracker = track({"source": {"value": 1}, "rawCopy": None, "draftCopy": None})
    raw = tracker.value["source"]
    change = tracker.beginChange()
    change.state["source"]["value"] = 2
    change.state["rawCopy"] = raw
    change.state["draftCopy"] = change.state["source"]
    prepared = change.prepare()
    assert prepared.value == {"source": {"value": 2}, "rawCopy": {"value": 1}, "draftCopy": {"value": 2}}
    tracker.adopt(prepared)


def test_folds_edits_to_introduced_object_and_array_subtrees_into_placement_payloads():
    tracker = track({"nested": None, "rows": []})
    change = tracker.beginChange()
    change.state["nested"] = {"rows": [{"value": 1}]}
    change.state["nested"]["rows"][0]["value"] = 2
    change.state["nested"]["rows"].push({"value": 3})
    change.state["rows"].push({"values": [1]})
    change.state["rows"][0]["values"].push(2)
    prepared = change.prepare()
    assert prepared.ops == [
        ["s", ["nested"], {"rows": [{"value": 2}, {"value": 3}]}],
        ["p", ["rows"], 0, 0, [{"values": [1, 2]}]],
    ]
    assert replay(tracker.value, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_folds_pathological_operation_counts_without_payload_cost_estimation():
    wide = {f"field{index}": 0 for index in range(5_000)}
    wideTracker = track(wide)
    wideBase = clone(wideTracker.value)
    wideChange = wideTracker.beginChange()
    for index in range(5_000):
        wideChange.state[f"field{index}"] = 1
    widePrepared = wideChange.prepare()
    assert len(widePrepared.ops) == 1
    assert widePrepared.ops[0][0] == "r"
    assert replay(wideBase, widePrepared.ops) == widePrepared.value
    wideTracker.adopt(widePrepared)

    sparseTracker = track({"rows": [{"value": value} for value in range(15_000)]})
    sparseBase = clone(sparseTracker.value)
    sparseChange = sparseTracker.beginChange()
    for index in range(0, 15_000, 3):
        sparseChange.state["rows"][index]["value"] = -index - 1
    sparsePrepared = sparseChange.prepare()
    assert len(sparsePrepared.ops) == 1
    assert sparsePrepared.ops[0][0] == "r"
    assert replay(sparseBase, sparsePrepared.ops) == sparsePrepared.value
    sparseTracker.adopt(sparsePrepared)


def test_shares_immutable_operation_placements_with_the_materialized_candidate():
    tracker = track({"rows": []})
    change = tracker.beginChange()
    change.state["rows"].push({"value": 1})
    prepared = change.prepare()
    operations = prepared.ops
    pending = list(operations)
    while pending:
        value = pending.pop()
        assert not isinstance(value, DraftObject | DraftArray)
        if isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, dict):
            pending.extend(value.values())
    splice = operations[0]
    assert splice[0] == "p"
    assert splice[4][0] is prepared.value["rows"][0]
    tracker.adopt(prepared)
    assert tracker.value is prepared.value


# -- piece arrays --------------------------------------------------------------------------------


def test_emits_one_insert_for_an_append_to_a_long_array():
    tracker = track({"values": list(range(10_000))})
    change = tracker.beginChange()
    change.state["values"].push(-1)
    prepared = change.prepare()
    assert prepared.ops == [["p", ["values"], 10_000, 0, [-1]]]
    tracker.adopt(prepared)
    assert tracker.value["values"][-1] == -1
    assert len(tracker.value["values"]) == 10_001


def test_emits_removals_from_the_end_backwards_then_inserts_when_kept_elements_keep_their_order():
    def mutate(values):
        values.shift()  # gap before the first kept element
        values.splice(1, 2, 20)  # gap between kept elements, with an insert
        values.splice(5, 1)  # another gap
        values.pop()  # gap after the last kept element
        values.push(30)

    tracker = track({"values": list(range(10))})
    values = settle(tracker, lambda draft: mutate(draft["values"]))
    assert values["values"] == [1, 20, 4, 5, 6, 8, 30]
    change = track({"values": list(range(10))}).beginChange()
    mutate(change.state["values"])
    assert change.prepare().ops == [
        ["p", ["values"], 9, 1, []],
        ["p", ["values"], 7, 1, []],
        ["p", ["values"], 2, 2, []],
        ["p", ["values"], 0, 1, []],
        ["p", ["values"], 1, 0, [20]],
        ["p", ["values"], 6, 0, [30]],
    ]


def test_emits_a_permutation_when_kept_elements_change_order():
    def oddLast(left, right):
        return (left % 2) - (right % 2) or left - right

    for mutate, expected in (
        (lambda values: values.reverse(), [4, 3, 2, 1, 0]),
        (lambda values: values.sort(oddLast), [0, 2, 4, 1, 3]),
    ):
        tracker = track({"values": [0, 1, 2, 3, 4]})
        change = tracker.beginChange()
        mutate(change.state["values"])
        prepared = change.prepare()
        assert any(operation[0] == "m" for operation in prepared.ops)
        assert replay(tracker.value, prepared.ops) == {"values": expected}


def test_keeps_adoption_independent_from_mutable_detached_permutation_metadata():
    tracker = track({"values": [3, 1, 2]})
    change = tracker.beginChange()
    change.state["values"].sort(lambda left, right: left - right)
    prepared = change.prepare()
    permutation = next(operation for operation in prepared.ops if operation[0] == "m")
    permutation[2].reverse()
    tracker.adopt(prepared)
    assert tracker.value["values"] == [1, 2, 3]


def test_matches_native_splice_with_zero_or_one_argument():
    noArguments = track({"values": [1, 2, 3]})
    noArgumentsChange = noArguments.beginChange()
    assert noArgumentsChange.state["values"].splice() == []
    noArgumentsPrepared = noArgumentsChange.prepare()
    assert noArgumentsPrepared.value["values"] == [1, 2, 3]
    assert noArgumentsPrepared.ops == []
    noArguments.adopt(noArgumentsPrepared)

    oneArgument = track({"values": [1, 2, 3, 4]})
    oneArgumentChange = oneArgument.beginChange()
    assert oneArgumentChange.state["values"].splice(1) == [2, 3, 4]
    oneArgumentPrepared = oneArgumentChange.prepare()
    assert oneArgumentPrepared.value["values"] == [1]
    assert replay(oneArgument.value, oneArgumentPrepared.ops) == oneArgumentPrepared.value
    oneArgument.adopt(oneArgumentPrepared)

    pastEnd = track({"values": [1, 2, 3]})
    pastEndChange = pastEnd.beginChange()
    assert pastEndChange.state["values"].splice(3) == []
    pastEndPrepared = pastEndChange.prepare()
    assert pastEndPrepared.value["values"] == [1, 2, 3]
    pastEnd.adopt(pastEndPrepared)


def test_supports_all_structural_mutators_and_native_return_values():
    tracker = track({"values": [3, 1, 2]})
    base = clone(tracker.value)
    change = tracker.beginChange()
    assert change.state["values"].push(4) == 4
    assert change.state["values"].pop() == 4
    assert change.state["values"].unshift(0) == 4
    assert change.state["values"].shift() == 0
    assert change.state["values"].splice(1, 1, 5, 4) == [1]
    assert change.state["values"].sort(lambda left, right: left - right) is change.state["values"]
    assert change.state["values"].reverse() is change.state["values"]
    change.state["values"].fill(9, 1, 3)
    change.state["values"].copyWithin(1, 0, 2)
    prepared = change.prepare()
    assert prepared.value == {"values": [5, 5, 9, 2]}
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_tracks_held_handles_after_reindex_and_suppresses_detached_writes():
    tracker = track({"values": [{"value": "a"}, {"value": "b"}, {"value": "c"}]})
    change = tracker.beginChange()
    held = change.state["values"][1]
    change.state["values"].unshift({"value": "front"})
    held["value"] = "moved"
    assert change.state["values"][2]["value"] == "moved"
    change.state["values"].splice(2, 1)
    held["value"] = "detached"
    prepared = change.prepare()
    assert prepared.value == {"values": [{"value": "front"}, {"value": "a"}, {"value": "c"}]}
    assert replay(tracker.value, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_replays_introduced_moved_then_edited_array_descendants():
    tracker = track({"values": []})
    base = clone(tracker.value)
    change = tracker.beginChange()
    change.state["values"].push({"id": 1, "nested": [1]})
    held = change.state["values"][0]
    change.state["values"].unshift({"id": 0, "nested": []})
    held["nested"].push(2)
    change.state["values"].reverse()
    held["nested"].push(3)
    prepared = change.prepare()
    assert prepared.value == {"values": [{"id": 1, "nested": [1, 2, 3]}, {"id": 0, "nested": []}]}
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_combines_movement_and_edits_with_permutation_plus_final_paths():
    tracker = track({"values": [{"rank": 3, "edited": 0}, {"rank": 1, "edited": 0}, {"rank": 2, "edited": 0}]})
    base = clone(tracker.value)
    change = tracker.beginChange()
    held = change.state["values"][0]
    change.state["values"].sort(lambda left, right: left["rank"] - right["rank"])
    held["edited"] = 1
    prepared = change.prepare()
    assert prepared.ops[0] == ["m", ["values"], [1, 2, 0]]
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_keeps_comparator_writes_and_structurally_reentrant_appends():
    tracker = track({"values": [{"rank": 2, "comparisons": 0}, {"rank": 1, "comparisons": 0}]})
    base = clone(tracker.value)
    change = tracker.beginChange()
    appended = [False]

    def compare(left, right):
        left["comparisons"] += 1
        right["comparisons"] += 1
        if not appended[0]:
            appended[0] = True
            change.state["values"][0] = {"rank": 9, "comparisons": 0}
            change.state["values"].push({"rank": 3, "comparisons": 0})
        return left["rank"] - right["rank"]

    change.state["values"].sort(compare)
    prepared = change.prepare()
    assert [value["rank"] for value in prepared.value["values"]] == [1, 2, 3]
    assert [value["comparisons"] for value in prepared.value["values"]] == [1, 1, 0]
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_normalizes_duplicate_entries_created_by_structurally_reentrant_sort_callbacks():
    tracker = track({
        "values": [
            {"rank": 2, "edited": 0, "nested": {"value": 0}},
            {"rank": 1, "edited": 0, "nested": {"value": 0}},
        ],
    })
    base = clone(tracker.value)
    change = tracker.beginChange()
    inserted = [False]

    def compare(left, right):
        left["edited"] = left["rank"] * 10
        left["nested"]["value"] = left["rank"] * 100
        right["edited"] = right["rank"] * 10
        right["nested"]["value"] = right["rank"] * 100
        if not inserted[0]:
            inserted[0] = True
            change.state["values"].unshift({"rank": 4, "edited": 0, "nested": {"value": 0}})
        return left["rank"] - right["rank"]

    change.state["values"].sort(compare)
    prepared = change.prepare()
    values = prepared.value["values"]
    assert [value["rank"] for value in values] == [1, 2, 1]
    assert [value["edited"] for value in values] == [10, 20, 10]
    assert [value["nested"]["value"] for value in values] == [100, 200, 100]
    assert values[0] is not values[2]
    assert values[0]["nested"] is not values[2]["nested"]
    assert replay(base, prepared.ops) == prepared.value
    expectAliasFree(prepared.value)
    tracker.adopt(prepared)


def test_folds_dense_child_and_direct_index_edits_into_one_full_array_operation():
    rowsTracker = track({"rows": [{"value": value} for value in range(1_000)]})
    rowsBase = clone(rowsTracker.value)
    rowsChange = rowsTracker.beginChange()
    for row in rowsChange.state["rows"]:
        row["value"] += 1
    rowsPrepared = rowsChange.prepare()
    assert len(rowsPrepared.ops) == 1
    assert rowsPrepared.ops[0][0] == "p"
    assert replay(rowsBase, rowsPrepared.ops) == rowsPrepared.value
    rowsTracker.adopt(rowsPrepared)

    valuesTracker = track({"values": list(range(1_000))})
    valuesBase = clone(valuesTracker.value)
    valuesChange = valuesTracker.beginChange()
    for index in range(600):
        valuesChange.state["values"][index] = -index - 1
    valuesPrepared = valuesChange.prepare()
    assert len(valuesPrepared.ops) == 1
    assert valuesPrepared.ops[0][0] == "p"
    assert replay(valuesBase, valuesPrepared.ops) == valuesPrepared.value
    valuesTracker.adopt(valuesPrepared)


def test_folds_only_a_deeply_nested_dense_array_region():
    tracker = track({"rows": [{"nested": {"value": value}} for value in range(2_000)]})
    base = clone(tracker.value)
    change = tracker.beginChange()
    change.state["rows"][0]["nested"]["value"] = -1
    for index in range(500, 1_000):
        change.state["rows"][index]["nested"]["value"] = -index
    prepared = change.prepare()
    splice = next(operation for operation in prepared.ops if operation[0] == "p")
    assert splice[:4] == ["p", ["rows"], 500, 500]
    assert len(splice[4]) == 500
    assert len(prepared.ops) == 2
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_does_not_cache_emission_paths_for_nested_reserved_key_folds_covered_by_an_outer_region():
    rows = [{"flag": 0} for _ in range(400)]
    rows[100]["special"] = {"__proto__": {"values": list(range(400))}}
    tracker = track({"rows": rows})
    base = clone(tracker.value)
    change = tracker.beginChange()
    for index in range(256):
        change.state["rows"][index]["flag"] = 1
    for index in range(256):
        change.state["rows"][100]["special"]["__proto__"]["values"][index] = -index - 1
    prepared = change.prepare()
    assert len(prepared.ops) == 1
    assert prepared.ops[0][:4] == ["p", ["rows"], 0, 256]
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_suppresses_nested_structural_and_leaf_operations_covered_by_an_outer_dense_region():
    tracker = track({
        "rows": [
            {"flag": 0, "values": [{"value": value} for value in range(400)] if index == 100 else []}
            for index in range(400)
        ],
    })
    base = clone(tracker.value)
    change = tracker.beginChange()
    for index in range(256):
        change.state["rows"][index]["flag"] = 1
    for index in range(256):
        change.state["rows"][100]["values"][index]["value"] = -index - 1
    change.state["rows"][100]["values"].push({"value": 999})
    prepared = change.prepare()
    assert len(prepared.ops) == 1
    assert prepared.ops[0][:4] == ["p", ["rows"], 0, 256]
    assert len(prepared.value["rows"][100]["values"]) == 401
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_emits_multiple_disjoint_dense_regions_and_preserves_operations_outside_their_boundaries():
    tracker = track({"values": [{"value": value} for value in range(1_400)]})
    base = clone(tracker.value)
    change = tracker.beginChange()
    for index in (97, 358, 897, 1_158):
        change.state["values"][index]["value"] = -index - 1
    for index in range(100, 356):
        change.state["values"][index]["value"] = -index - 1
    for index in range(900, 1_156):
        change.state["values"][index]["value"] = -index - 1
    prepared = change.prepare()
    splices = [operation for operation in prepared.ops if operation[0] == "p"]
    assert [operation[:4] for operation in splices] == [["p", ["values"], 100, 256], ["p", ["values"], 900, 256]]
    for index in (97, 358, 897, 1_158):
        assert ["s", ["values", index, "value"], -index - 1] in prepared.ops
    assert len(prepared.ops) == 6
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_normalizes_a_20000_operation_queue_transaction_to_two_operations():
    tracker = track({"values": [{"value": value} for value in range(20_000)]})
    base = clone(tracker.value)
    change = tracker.beginChange()
    for index in range(10_000):
        change.state["values"].shift()
        change.state["values"].push({"value": 20_000 + index})
    prepared = change.prepare()
    assert len(prepared.ops) == 2
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_handles_adversarial_fragmentation_and_resolves_held_handles_without_piece_scans():
    size = 20_000
    tracker = track({"values": [{"value": value} for value in range(size)]})
    expected = clone(tracker.value)
    base = clone(tracker.value)
    change = tracker.beginChange()
    heldIndices = [1, 1_001, 5_001, 10_001, 15_001, 19_999]
    held = [change.state["values"][index] for index in heldIndices]
    for index in range(0, size, 2):
        change.state["values"].splice(index, 1, {"value": -index - 1})
        expected["values"][index : index + 1] = [{"value": -index - 1}]
    for value in held:
        value["value"] += 100_000
    for index in heldIndices:
        expected["values"][index]["value"] += 100_000
    prepared = change.prepare()
    assert prepared.value == expected
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


def test_normalizes_restored_overrides_and_cancelled_structural_edits_to_no_operations():
    tracker = track({"values": [1, 2, 3]})
    change = tracker.beginChange()
    change.state["values"][1] = 9
    change.state["values"][1] = 2
    change.state["values"].reverse()
    change.state["values"].reverse()
    change.state["values"].push(4)
    assert change.state["values"].pop() == 4
    prepared = change.prepare()
    assert prepared.value == {"values": [1, 2, 3]}
    assert prepared.ops == []
    tracker.adopt(prepared)


def test_handles_null_sparse_overrides_without_confusing_absence():
    tracker = track({"values": list(range(10_000))})

    def mutate(draft):
        draft["values"][17] = None
        draft["values"][9_000] = None

    settle(tracker, mutate)
    assert tracker.value["values"][17] is None
    assert tracker.value["values"][9_000] is None


def test_supports_self_overlapping_fill_and_copy_within_by_value():
    tracker = track({"values": [{"n": 0}, {"n": 1}, {"n": 2}, {"n": 3}]})

    def mutate(draft):
        draft["values"].copyWithin(1, 0, 3)
        draft["values"].fill(draft["values"][1], 0, 2)
        draft["values"][0]["n"] = 9

    settle(tracker, mutate)
    assert [value["n"] for value in tracker.value["values"]] == [9, 0, 1, 2]
    assert tracker.value["values"][0] is not tracker.value["values"][1]


@pytest.mark.parametrize("method", ["unshift", "splice"])
def test_inserts_100000_items(method):
    tracker = track({"values": [-1]})
    base = clone(tracker.value)
    change = tracker.beginChange()
    items = list(range(100_000))
    if method == "unshift":
        change.state["values"].unshift(*items)
    else:
        change.state["values"].splice(1, 0, *items)
    prepared = change.prepare()
    assert len(prepared.value["values"]) == 100_001
    assert prepared.value["values"][99_999 if method == "unshift" else 100_000] == 99_999
    assert replay(base, prepared.ops) == prepared.value
    tracker.adopt(prepared)


# -- randomized transactions ---------------------------------------------------------------------


def mulberry(seed: int):
    state = [seed]

    def next_() -> float:
        state[0] = (state[0] + 0x6D2B79F5) & 0xFFFFFFFF
        seedValue = state[0]
        value = imul(seedValue ^ (seedValue >> 15), 1 | seedValue)
        value = ((value + imul(value ^ (value >> 7), 61 | value)) & 0xFFFFFFFF) ^ value
        return ((value ^ (value >> 14)) & 0xFFFFFFFF) / 4_294_967_296

    return next_


def imul(left: int, right: int) -> int:
    return (left * right) & 0xFFFFFFFF


def mutateDocument(document, choice: int, value: int) -> None:
    values = document["values"]
    isDraft = isinstance(values, DraftArray)

    def push(item):
        values.push(item) if isDraft else values.append(item)

    def item():
        return {"id": value, "score": value % 7}

    if choice == 0:
        document["text"] += f"-{value}"
    elif choice == 1:
        push(item())
    elif choice == 2:
        values.unshift(item()) if isDraft else values.insert(0, item())
    elif choice == 3:
        if len(values) > 0:
            values.shift() if isDraft else values.pop(0)
    elif choice == 4:
        if len(values) > 0:
            values.pop()
    elif choice == 5:
        index = 0 if len(values) == 0 else value % (len(values) + 1)
        remove = 0 if len(values) == 0 else value % 2
        if isDraft:
            values.splice(index, remove, item())
        else:
            values[index : index + remove] = [item()]
    elif choice == 6:
        values.reverse()
    elif choice == 7:
        if isDraft:
            values.sort(lambda left, right: left["id"] - right["id"])
        else:
            values.sort(key=lambda entry: entry["id"])
    elif choice == 8:
        if len(values) > 0:
            values[value % len(values)]["score"] = value
    elif choice == 9:
        document["meta"]["revision"] += 1
        document["meta"]["label"] = f"r-{value}"
    elif "label" in document["meta"]:
        del document["meta"]["label"]


def test_matches_policy_detached_replay_and_adopted_state_after_multi_operation_transactions():
    for seed in range(1, 41):
        rng = mulberry(seed)
        initial = {"values": [{"id": id_, "score": 0} for id_ in range(4)], "text": "start", "meta": {"revision": 0}}
        tracker = track(initial)
        expected = clone(initial)
        for transaction in range(25):
            baseRoot = tracker.value
            base = clone(baseRoot)
            change = tracker.beginChange()
            for operation in range(5):
                choice = int(rng() * 11)
                value = seed * 10_000 + transaction * 10 + operation
                mutateDocument(expected, choice, value)
                mutateDocument(change.state, choice, value)
            prepared = change.prepare()
            policy = clone(prepared.value)
            assert baseRoot == base, f"prepare changed base seed {seed} transaction {transaction}"
            assert policy == expected, f"policy seed {seed} transaction {transaction}"
            assert replay(base, prepared.ops) == policy, f"replay seed {seed} transaction {transaction}"
            tracker.adopt(prepared)
            assert baseRoot == base, f"adopt changed base seed {seed} transaction {transaction}"
            assert tracker.value is prepared.value
            assert tracker.value == policy
            expectAliasFree(tracker.value)


# -- object emission scaling ---------------------------------------------------------------------


def test_orders_many_reverse_depth_object_edits_through_the_bounded_fallback():
    initial: dict = {"value": 0}
    for _depth in range(512):
        initial = {"value": 0, "next": initial}
    tracker = track(initial)
    change = tracker.beginChange()
    nodes = []
    node = change.state
    while node is not UNDEFINED:
        nodes.append(node)
        node = node["next"]
    for index in range(len(nodes) - 1, -1, -1):
        nodes[index]["value"] = index + 1
    prepared = change.prepare()
    assert len(prepared.ops) == len(nodes)
    assert replay(initial, prepared.ops) == prepared.value
    tracker.adopt(prepared)
    expectAliasFree(tracker.value)


# -- security and storage-style cloning ----------------------------------------------------------


def test_handles_reserved_own_keys():
    initial = {"safe": {"__proto__": {"value": 1}}}
    tracker = track(initial)
    change = tracker.beginChange()
    change.state["safe"]["__proto__"]["value"] = 2
    prepared = change.prepare()
    assert prepared.value["safe"]["__proto__"]["value"] == 2
    assert replay(tracker.value, prepared.ops) == prepared.value
    tracker.adopt(prepared)
    assert {}.get("value") is None


def test_supports_a_memory_storage_style_recursive_clone_of_prepared_value():
    def storageClone(value):
        if not isinstance(value, list | dict):
            return value
        if isinstance(value, list):
            return [storageClone(item) for item in value]
        return {key: storageClone(item) for key, item in value.items()}

    tracker = track({"rows": [{"value": 1}]})
    change = tracker.beginChange()
    change.state["rows"][0]["value"] = 2
    change.state["rows"].push({"value": 3})
    prepared = change.prepare()
    assert storageClone(prepared.value) == {"rows": [{"value": 2}, {"value": 3}]}
    tracker.adopt(prepared)
