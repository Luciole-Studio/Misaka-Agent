"""pi chord's ``test/delta-apply-immutable.test.ts``.

Python has no frozen objects, so each case compares the inputs with a snapshot taken before the call instead.
The complexity case, which counts ``Object.keys`` calls, is checked for its result only.
"""

from __future__ import annotations

import json

import pytest

from misaka.chord.delta import (
    PathError,
    UnsafePathError,
    apply,
    applyImmutable,
    applyImmutableBatches,
    track,
)
from misaka.utils.js import UNDEFINED


def clone(value):
    return json.loads(json.dumps(value))


def test_copies_each_touched_container_once_while_preserving_input_and_payload_ownership():
    shared = {"nested": {"value": 1}}
    untouched = {"value": 9}
    rowPayload = {"id": 4, "label": "placed"}
    base = {
        "text": "abcdef",
        "stable": {"value": 7},
        "branch": {"value": 1},
        "copy": None,
        "placed": None,
        "untouched": None,
        "left": None,
        "right": None,
        "meta": {"count": 0, "obsolete": True},
        "rows": [{"id": 1, "label": "one"}, {"id": 2, "label": "two"}, {"id": 3, "label": "three"}],
    }
    operations = [
        ["t", ["text"], 2],
        ["a", ["text"], "!"],
        ["s", ["meta", "count"], 1],
        ["s", ["meta", "count"], 2],
        ["d", ["meta", "obsolete"]],
        ["s", ["copy"], base["branch"]],
        ["s", ["copy", "value"], 2],
        ["s", ["placed"], shared],
        ["s", ["placed", "nested", "value"], 2],
        ["s", ["untouched"], untouched],
        ["s", ["left"], shared],
        ["s", ["right"], shared],
        ["s", ["left", "nested", "value"], 3],
        ["p", ["rows"], 1, 1, [rowPayload]],
        ["s", ["rows", 1, "label"], "edited"],
        ["m", ["rows"], [1, 0, 2]],
        ["s", ["rows", 0, "label"], "moved"],
    ]
    snapshot = clone(base)

    result = applyImmutable(base, operations)
    mutableResult = apply(clone(base), clone(operations))

    assert result == mutableResult
    assert result["text"] == "cdef!"
    assert result["stable"] is base["stable"]
    assert result["untouched"] is untouched
    assert result["copy"] is not base["branch"]
    assert result["copy"] == {"value": 2}
    assert result["placed"] is not shared
    assert result["placed"] == {"nested": {"value": 2}}
    assert result["left"] is not shared
    assert result["right"] is shared
    assert result["rows"][0] is not rowPayload
    assert result["rows"][0] == {"id": 4, "label": "moved"}
    assert base == snapshot
    assert base["branch"]["value"] == 1
    assert shared["nested"]["value"] == 1
    assert rowPayload["label"] == "placed"


def test_protects_root_replacement_payloads_before_later_object_and_array_edits():
    replacement = {"nested": {"value": 1}, "values": [1, 2, 3]}
    result = applyImmutableBatches(
        UNDEFINED,
        [
            [["r", replacement]],
            [["s", ["nested", "value"], 2]],
            [["p", ["values"], 1, 1, [4, 5]], ["m", ["values"], [3, 0, 1, 2]]],
        ],
    )
    assert result == {"nested": {"value": 2}, "values": [3, 1, 4, 5]}
    assert replacement == {"nested": {"value": 1}, "values": [1, 2, 3]}

    array = [1, 2, 3]
    arrayResult = applyImmutable(array, [["p", [], 1, 1, [4, 5]], ["m", [], [3, 0, 1, 2]], ["d", [1]]])
    assert arrayResult == [3, 4, 5]
    assert array == [1, 2, 3]


def test_shares_one_private_copy_on_write_scope_across_batch_partitions():
    base = {
        "text": "abcdef",
        "meta": {"count": 0},
        "values": [{"id": 1, "value": 1}, {"id": 2, "value": 2}, {"id": 3, "value": 3}],
    }
    batches = [
        [["s", ["meta", "count"], 1], ["p", ["values"], 1, 1, [{"id": 4, "value": 4}]]],
        [],
        [["m", ["values"], [2, 0, 1]], ["s", ["values", 2, "value"], 40]],
        [["t", ["text"], 2], ["a", ["text"], "!"]],
    ]

    intermediate = applyImmutable(base, batches[0])
    intermediateSnapshot = clone(intermediate)
    sequential = intermediate
    for index in range(1, len(batches)):
        sequential = applyImmutable(sequential, batches[index])
    streamed = applyImmutableBatches(base, batches)
    flattened = applyImmutable(base, [op for batch in batches for op in batch])

    assert streamed == sequential
    assert streamed == flattened
    assert intermediate == intermediateSnapshot
    assert base["meta"]["count"] == 0
    assert base["values"][1]["id"] == 2


def test_handles_tracker_produced_batches_across_arbitrary_revision_boundaries():
    initial = {"text": "start", "values": [{"id": id_, "score": 0} for id_ in range(8)], "revision": 0}
    tracker = track(initial)
    batches = []
    for revision in range(1, 41):
        change = tracker.beginChange()
        state = change.state
        state["revision"] = revision
        state["text"] = f"{state['text'][1:]}{revision}"
        match revision % 5:
            case 0:
                state["values"].reverse()
            case 1:
                state["values"].push({"id": 100 + revision, "score": revision})
            case 2:
                state["values"].shift()
            case 3:
                state["values"][revision % len(state["values"])]["score"] = revision
            case _:
                state["values"].splice(1, 1, {"id": 200 + revision, "score": revision})
        prepared = change.prepare()
        batches.append(prepared.ops)
        tracker.adopt(prepared)

    assert applyImmutableBatches(initial, batches) == tracker.value


def test_does_not_expose_partial_application_when_validation_or_iteration_fails():
    base = {"nested": {"value": 1}}
    invalidBatches = [[["s", ["nested", "value"], 2]], [["s", ["constructor", "prototype", "polluted"], True]]]
    with pytest.raises(Exception):  # noqa: B017
        applyImmutableBatches(base, invalidBatches)
    assert base["nested"]["value"] == 1
    assert {}.get("polluted") is None

    def throwingBatches():
        yield [["s", ["nested", "value"], 3]]
        raise RuntimeError("revision stream failed")

    with pytest.raises(RuntimeError, match="revision stream failed"):
        applyImmutableBatches(base, throwingBatches())
    assert base["nested"]["value"] == 1

    state = {"advancedPastInvalid": False, "iteratorClosed": False}

    def invalidOperationBatches():
        try:
            yield [["s", ["nested", "value"], 4]]
            yield [["s", ["__proto__", "polluted"], True]]
            state["advancedPastInvalid"] = True
        finally:
            state["iteratorClosed"] = True

    with pytest.raises(UnsafePathError):
        applyImmutableBatches(base, invalidOperationBatches())
    assert state["advancedPastInvalid"] is False
    assert state["iteratorClosed"] is True
    assert base["nested"]["value"] == 1

    reads = []

    class Trap(dict):
        def __getitem__(self, key):
            reads.append(key)
            return {}

        def get(self, key, default=None):
            reads.append(key)
            return {}

    with pytest.raises(Exception, match="path"):
        applyImmutableBatches(Trap(trap=1), [[["s", "bad-path", 1]]])
    assert reads == []

    with pytest.raises(PathError):
        applyImmutable({"values": []}, [["s", ["values", "missing", "value"], 1]])
    with pytest.raises(UnsafePathError):
        applyImmutable({"values": [{}]}, [["s", ["values", "0", "value"], 1]])


def test_allows_one_immutable_batch_to_fan_out_without_mutating_shared_payloads():
    payload = {"nested": {"value": 1}}
    operations = [["s", ["placed"], payload], ["s", ["placed", "nested", "value"], 2]]
    base = {"placed": None}
    first = applyImmutable(base, operations)
    second = applyImmutable(base, operations)
    assert first == second
    assert first is not second
    assert first["placed"] is not second["placed"]
    assert payload["nested"]["value"] == 1


def test_appends_to_an_array_not_yet_copied_in_one_copy_sharing_everything_else():
    entries = [{"index": index} for index in range(1_000)]
    base = {"log": {"entries": entries, "meta": {"count": 1_000}}, "other": {"value": 1}}
    before = clone(base)
    result = applyImmutable(
        base,
        [
            ["p", ["log", "entries"], 1_000, 0, [{"index": 1_000}]],
            ["p", ["log", "entries"], 1_001, 0, [{"index": 1_001}]],
        ],
    )
    assert len(result["log"]["entries"]) == 1_002
    assert result["log"]["entries"][-2:] == [{"index": 1_000}, {"index": 1_001}]
    assert len(base["log"]["entries"]) == 1_000
    assert base == before
    assert result["log"]["entries"][0] is entries[0]
    assert result["log"]["meta"] is base["log"]["meta"]
    assert result["other"] is base["other"]
    assert result["log"] is not base["log"]

    rootArray = [1, 2]
    assert applyImmutable(rootArray, [["p", [], 2, 0, [3]]]) == [1, 2, 3]
    assert rootArray == [1, 2]
    # An appended item that is itself an array stays one element.
    assert applyImmutable(rootArray, [["p", [], 2, 0, [[3, 4]]]]) == [1, 2, [3, 4]]
    # Invalid paths fail with the general path's errors.
    invalid = [
        (["p", ["values", "missing"], 0, 0, []], PathError),
        (["p", ["missing"], 0, 0, []], PathError),
        (["p", ["values", 0], 0, 0, []], PathError),
        (["p", ["__proto__"], 0, 0, []], UnsafePathError),
        (["p", ["list", "x"], 0, 0, []], PathError),
        (["p", ["text"], 0, 0, []], PathError),
        (["p", ["list", -1], 0, 0, []], UnsafePathError),
        (["p", ["list", 1.5], 0, 0, []], UnsafePathError),
        (["p", ["list", 0, "constructor"], 0, 0, []], UnsafePathError),
    ]
    document = {"values": [], "list": [[1]], "text": "a"}
    for op, error in invalid:
        with pytest.raises(error):
            applyImmutable(document, [op])
    # An insert that is not at the end takes the general path.
    assert applyImmutable(rootArray, [["p", [], 1, 0, [9]]]) == [1, 9, 2]


def test_copies_a_wide_object_for_repeated_writes():
    base = {f"field{index}": index for index in range(20_000)}
    operations = [["s", [f"field{index}"], -index] for index in range(1_000)]
    result = applyImmutable(base, operations)
    assert result["field999"] == -999
    assert base["field999"] == 999
    assert result is not base
