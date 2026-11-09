"""pi chord's ``test/json.test.ts`` and ``test/delta-clone.test.ts``.

Cases about property descriptors, accessors, symbols, sparse arrays and null prototypes have no counterpart in
Python and are left out; a list or dict subclass, a tuple, a set and a non-string key stand in for them.
"""

from __future__ import annotations

import pytest

from misaka.chord.delta import apply, track
from misaka.chord.json import copyJson, isJsonValue
from misaka.utils.js import UNDEFINED


def test_checks_strict_json_without_normalizing_it():
    assert isJsonValue({"nested": [1, True, None]}) is True
    assert isJsonValue({"omitted": UNDEFINED}) is False
    assert isJsonValue(bytes([1])) is False
    assert isJsonValue(float("inf")) is False
    cyclic: dict = {}
    cyclic["self"] = cyclic
    assert isJsonValue(cyclic) is False


def test_copies_strict_json_without_retaining_aliases():
    shared = {"value": 1}
    input_ = {"left": shared, "right": shared}
    copied = copyJson(input_)
    assert copied == input_
    assert copied is not input_
    assert copied["left"] is not shared
    assert copied["right"] is not shared
    assert copied["left"] is not copied["right"]


def test_optionally_omits_undefined_object_properties_without_normalizing_arrays():
    input_ = {"kept": 1, "omitted": UNDEFINED, "nested": {"omitted": UNDEFINED, "kept": True}}
    with pytest.raises(TypeError, match="strict JSON"):
        copyJson(input_)
    assert copyJson(input_, {"omitUndefinedProperties": True}) == {"kept": 1, "nested": {"kept": True}}
    with pytest.raises(TypeError, match="strict JSON"):
        copyJson([UNDEFINED], {"omitUndefinedProperties": True})


def test_preserves_own_proto_data_properties():
    copied = copyJson({"__proto__": {"safe": True}})
    assert "__proto__" in copied
    assert copied["__proto__"] == {"safe": True}


def test_rejects_cycles_and_non_strict_containers():
    cyclic: dict = {}
    cyclic["self"] = cyclic

    class ListSubclass(list):
        pass

    class DictSubclass(dict):
        pass

    for invalid in (cyclic, ListSubclass([1]), DictSubclass(a=1), (1, 2), {1, 2}, {1: "non-string key"}):
        assert isJsonValue(invalid) is False
        with pytest.raises(TypeError, match="strict JSON|cycles"):
            copyJson(invalid)


# -- tracker ownership ---------------------------------------------------------------------------


def test_takes_immutable_ownership_of_the_imported_revision():
    input_ = {"point": {"x": 3, "y": 7, "pressure": 0.1}, "rows": [{"values": [0, False, None, "text", {"n": 1}]}]}
    tracker = track(input_)
    assert tracker.value is input_
    assert tracker.value["point"] is input_["point"]
    assert tracker.value["rows"][0]["values"][4] is input_["rows"][0]["values"][4]


def test_keeps_alias_free_roots_apart():
    tracker = track({"left": {"nested": [{"n": 1}]}, "right": {"nested": [{"n": 1}]}})
    assert tracker.value["left"] is not tracker.value["right"]
    assert tracker.value["left"]["nested"][0] is not tracker.value["right"]["nested"][0]


def test_copies_assigned_and_inserted_values_immediately():
    tracker = track({"rows": []})
    assigned = {"nested": {"value": 1}}
    change = tracker.beginChange()
    change.state["rows"].push(assigned)
    assigned["nested"]["value"] = 9
    assert change.state["rows"][0]["nested"]["value"] == 1
    prepared = change.prepare()
    assert prepared.value["rows"][0]["nested"]["value"] == 1
    replica = apply(copyJson(prepared.base), prepared.ops)
    assert replica == prepared.value


# -- immutable revision ownership (state-value.test.ts) -------------------------------------------


def test_takes_ownership_of_an_alias_free_mutable_json_root_without_freezing():
    input_ = {"left": {"value": 1}, "right": {"value": 1}}
    tracker = track(input_)
    assert tracker.value is input_
    assert tracker.value["left"] is not tracker.value["right"]


def test_commits_transaction_copies_while_sharing_unchanged_branches():
    tracker = track({"changed": {"value": 1}, "retained": {"value": 2}})
    base = tracker.value
    baseSnapshot = copyJson(base)
    change = tracker.beginChange()
    change.state["changed"]["value"] = 3
    prepared = change.prepare()
    assert prepared.value["changed"] is not base["changed"]
    assert prepared.value["retained"] is base["retained"]
    tracker.adopt(prepared)
    assert base == baseSnapshot
    assert tracker.value is prepared.value


def test_makes_repeated_placements_independent():
    tracker = track({"values": []})
    change = tracker.beginChange()
    shared = {"value": 1}
    change.state["values"].push(shared, shared)
    prepared = change.prepare()
    assert prepared.value["values"][0] is not prepared.value["values"][1]


def test_copies_a_draft_as_the_plain_value_it_views():
    tracker = track({"nested": {"rows": [{"n": 1}, {"n": 2}]}, "text": "a"})
    change = tracker.beginChange()
    change.state["nested"]["rows"].push({"n": 3})
    copied = copyJson(change.state)
    assert copied == {"nested": {"rows": [{"n": 1}, {"n": 2}, {"n": 3}]}, "text": "a"}
    assert type(copied) is dict
    assert type(copied["nested"]["rows"]) is list
    assert copyJson(change.state["nested"]["rows"]) == [{"n": 1}, {"n": 2}, {"n": 3}]
    change.state["nested"]["rows"][0]["n"] = 9
    assert copied["nested"]["rows"][0] == {"n": 1}
