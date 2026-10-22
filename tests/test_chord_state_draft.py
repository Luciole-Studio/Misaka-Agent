"""pi chord's ``test/state-draft.test.ts`` and ``test/state-fuzz.test.ts``."""

from __future__ import annotations

import datetime
import json

import pytest

from misaka.chord.delta import applyImmutable, track
from misaka.chord.delta.tracker import DraftArray, DraftObject
from misaka.utils.js import UNDEFINED


def clone(value):
    return json.loads(json.dumps(value))


def plain(value):
    if isinstance(value, DraftArray):
        return [plain(item) for item in value]
    if isinstance(value, DraftObject):
        return {key: plain(item) for key, item in value.items()}
    return value


def expectAliasFree(value) -> None:
    seen = {}

    def visit(current, path):
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


# -- transactional overlay drafts ----------------------------------------------------------------


def test_copies_only_changed_branches():
    tracker = track({"changed": {"count": 1, "sibling": {"value": "kept"}}, "untouched": {"value": 2}})
    base = tracker.value
    change = tracker.beginChange()
    first = change.state["changed"]
    assert change.state["changed"] is first
    change.state["changed"]["count"] = 3
    prepared = change.prepare()
    assert prepared.value is not base
    assert prepared.value["changed"] is not base["changed"]
    assert prepared.value["changed"]["sibling"] is base["changed"]["sibling"]
    assert prepared.value["untouched"] is base["untouched"]


def test_supports_deletion_and_native_array_mutators():
    tracker = track({"optional": "remove", "values": [3, 1, 2]})
    change = tracker.beginChange()
    del change.state["optional"]
    change.state["values"].push(4)
    assert change.state["values"].pop() == 4
    change.state["values"].unshift(0)
    assert change.state["values"].shift() == 0
    assert change.state["values"].splice(1, 1, 5, 4) == [1]
    change.state["values"].sort(lambda left, right: left - right)
    change.state["values"].reverse()
    change.state["values"].fill(9, 1, 3)
    change.state["values"].copyWithin(1, 0, 2)
    prepared = change.prepare()
    assert prepared.value == {"values": [5, 5, 9, 2]}


def test_copies_inserted_draft_values_without_aliasing_their_original_reference():
    tracker = track({"values": [{"value": 1}, {"value": 2}]})
    change = tracker.beginChange()
    held = change.state["values"][0]
    change.state["values"].unshift(held)
    held["value"] = 9
    prepared = change.prepare()
    assert prepared.value["values"] == [{"value": 1}, {"value": 9}, {"value": 2}]
    assert prepared.value["values"][0] is not prepared.value["values"][1]


def test_keeps_comparator_edits_when_sorting_object_drafts():
    tracker = track({"rows": [{"rank": 2, "comparisons": 0}, {"rank": 1, "comparisons": 0}]})
    change = tracker.beginChange()

    def compare(left, right):
        left["comparisons"] += 1
        right["comparisons"] += 1
        return left["rank"] - right["rank"]

    change.state["rows"].sort(compare)
    prepared = change.prepare()
    assert [row["rank"] for row in prepared.value["rows"]] == [1, 2]
    assert [row["comparisons"] for row in prepared.value["rows"]] == [1, 1]


def test_drops_writes_through_detached_child_references():
    tracker = track({"child": {"value": "removed"}, "items": [{"value": "removed"}, {"value": "kept"}]})
    change = tracker.beginChange()
    child = change.state["child"]
    shifted = change.state["items"].shift()
    del change.state["child"]
    child["value"] = "detached child"
    if shifted is not UNDEFINED:
        shifted["value"] = "detached item"
    assert change.prepare().value == {"items": [{"value": "kept"}]}


@pytest.mark.parametrize("method", ["unshift", "splice"])
def test_inserts_100000_items_without_argument_overflow(method):
    tracker = track({"values": [-1]})
    change = tracker.beginChange()
    items = list(range(100_000))
    if method == "unshift":
        change.state["values"].unshift(*items)
    else:
        change.state["values"].splice(1, 0, *items)
    prepared = change.prepare()
    assert len(prepared.value["values"]) == 100_001
    assert prepared.value["values"][0 if method == "unshift" else 1] == 0
    assert prepared.value["values"][99_999 if method == "unshift" else 100_000] == 99_999
    assert applyImmutable(prepared.base, prepared.ops) == prepared.value


def test_rejects_non_strict_json_placements_without_mutating_the_draft_or_base():
    tracker = track({"number": 0, "payload": None, "values": [1, 2]})
    change = tracker.beginChange()
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["values"][0] = UNDEFINED
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["values"].push({"nested": UNDEFINED})
    with pytest.raises(TypeError, match="strict JSON"):
        change.state["number"] = float("nan")
    with pytest.raises(TypeError, match="plain objects"):
        change.state["payload"] = datetime.datetime.now()
    assert plain(change.state) == tracker.value
    change.abort()
    assert tracker.value == {"number": 0, "payload": None, "values": [1, 2]}


def test_rejects_array_holes_without_mutating_the_committed_base():
    tracker = track({"values": [1, 2]})
    change = tracker.beginChange()
    with pytest.raises(TypeError, match="dense|holes"):
        del change.state["values"][0]
    change.abort()
    assert tracker.value["values"] == [1, 2]


# -- randomized prepared revisions ---------------------------------------------------------------


def mulberry(seed: int):
    state = [seed]

    def imul(left, right):
        return (left * right) & 0xFFFFFFFF

    def next_() -> float:
        state[0] = (state[0] + 0x6D2B79F5) & 0xFFFFFFFF
        seedValue = state[0]
        value = imul(seedValue ^ (seedValue >> 15), 1 | seedValue)
        value = ((value + imul(value ^ (value >> 7), 61 | value)) & 0xFFFFFFFF) ^ value
        return ((value ^ (value >> 14)) & 0xFFFFFFFF) / 4_294_967_296

    return next_


def mutate(document, choice: int, value: int) -> None:
    items = document["items"]
    isDraft = isinstance(items, DraftArray)

    def item():
        return {"id": value, "text": f"item-{value}", "score": value % 7}

    if choice == 0:
        document["text"] += f"-{value}"
    elif choice == 1:
        text = document["text"]
        document["text"] = f"{text[min(2, len(text)) :]}{value}"
    elif choice == 2:
        items.push(item()) if isDraft else items.append(item())
    elif choice == 3:
        items.unshift(item()) if isDraft else items.insert(0, item())
    elif choice == 4:
        if len(items) > 0:
            items.shift() if isDraft else items.pop(0)
    elif choice == 5:
        if len(items) > 0:
            items.pop()
    elif choice == 6:
        index = 0 if len(items) == 0 else value % (len(items) + 1)
        remove = 0 if len(items) == 0 else value % 2
        if isDraft:
            items.splice(index, remove, item())
        else:
            items[index : index + remove] = [item()]
    elif choice == 7:
        items.reverse()
    elif choice == 8:
        if isDraft:
            items.sort(lambda left, right: left["id"] - right["id"])
        else:
            items.sort(key=lambda entry: entry["id"])
    elif choice == 9:
        if len(items) > 0:
            items[value % len(items)]["score"] = value
    elif choice == 10:
        document["meta"]["revision"] += 1
        document["meta"]["label"] = f"revision-{value}"
    elif choice == 11:
        if "label" in document["meta"]:
            del document["meta"]["label"]
    elif choice == 12:
        if len(items) > 1:
            items[1] = clone(plain(items[0]))
    else:
        for index in range(min(2, len(items))):
            items[index] = item()


def test_converges_across_randomized_prepared_revisions():
    for seed in range(1, 101):
        rng = mulberry(seed)
        initial = {
            "items": [{"id": id_, "text": f"item-{id_}", "score": 0} for id_ in range(4)],
            "text": "start",
            "meta": {"revision": 0},
        }
        tracker = track(initial)
        expected = clone(initial)
        replica = clone(tracker.value)
        for step in range(100):
            choice = int(rng() * 14)
            value = seed * 1_000 + step
            baseRoot = tracker.value
            base = clone(baseRoot)
            mutate(expected, choice, value)
            change = tracker.beginChange()
            mutate(change.state, choice, value)
            prepared = change.prepare()
            operations = clone(prepared.ops)
            where = f"seed {seed} step {step} choice {choice}"
            assert baseRoot == base, f"prepare base {where}"
            assert prepared.base is baseRoot
            replica = applyImmutable(replica, operations)
            assert replica == prepared.value, f"replay {where}"
            tracker.adopt(prepared)
            assert baseRoot == base, f"adopt base {where}"
            assert tracker.value is prepared.value
            assert tracker.value == expected, f"state {where}"
            assert replica == expected, f"replica {where}"
            expectAliasFree(tracker.value)
