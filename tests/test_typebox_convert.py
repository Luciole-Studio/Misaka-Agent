"""``typebox`` 1.3.27 ``Value.Convert``, ported for pi's ``validateToolArguments`` (2026-10-29): upstream converts
every TypeBox tool's arguments before checking them, and MISAKA's pydantic tools (its TypeBox tools) skipped the step.
Each expectation follows the package source (``build/value/convert``)."""
import pytest
from pydantic import BaseModel, Field

from misaka.ai.types import Tool, ToolCall
from misaka.ai.utils.typebox_convert import Convert
from misaka.ai.utils.validation import validateToolArguments


def converted(schema, value):
    return Convert(schema, value)


@pytest.mark.parametrize("value,expected", [
    ("42", 42), ("3.5", 3.5), (" 12 ", 12), ("0x10", 16), ("", 0), (True, 1), (False, 0), (None, 0),
    ("true", 1), ("FALSE", 0), ("12n", 12), ("abc", "abc"), ("Infinity", "Infinity"), ([1], [1]), (7, 7),
])
def test_number(value, expected):
    assert converted({"type": "number"}, value) == expected


@pytest.mark.parametrize("value,expected", [("7.9", 7), (-2.5, -2), ("x", "x"), (True, 1)])
def test_integer_truncates(value, expected):
    result = converted({"type": "integer"}, value)
    assert result == expected and type(result) is type(expected)


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("False", False), ("1", True), ("0", False), (1, True), (0, False), (None, False),
    ("yes", "yes"), (2, 2),
])
def test_boolean(value, expected):
    assert converted({"type": "boolean"}, value) == expected


@pytest.mark.parametrize("value,expected", [
    (5, "5"), (3.0, "3"), (2.5, "2.5"), (True, "true"), (None, "null"), ("x", "x"), ([1], [1]), ({"a": 1}, {"a": 1}),
])
def test_string(value, expected):
    assert converted({"type": "string"}, value) == expected


@pytest.mark.parametrize("value,expected", [
    ("null", None), ("UNDEFINED", None), ("", None), ("0", None), (0, None), (False, None), ("none", "none"), (1, 1),
])
def test_null(value, expected):
    assert converted({"type": "null"}, value) == expected


def test_array_wraps_a_non_array_and_converts_each_item():
    schema = {"type": "array", "items": {"type": "number"}}
    assert converted(schema, "1") == [1]
    assert converted(schema, ["1", 2, "x"]) == [1, 2, "x"]
    # A JSON-encoded array is not parsed: it becomes a one-element array, as upstream does.
    assert converted({"type": "array", "items": {"type": "object", "properties": {}}}, '[{"a": 1}]') == ['[{"a": 1}]']


def test_tuple_converts_positions_in_place():
    value = ["1", "true", "extra"]
    assert converted({"type": "array", "items": [{"type": "number"}, {"type": "boolean"}]}, value) is value
    assert value == [1, True, "extra"]
    assert converted({"type": "array", "items": [{"type": "number"}]}, "1") == "1"


def test_literal_converts_only_to_its_own_value():
    assert converted({"const": 1, "type": "number"}, "1") == 1
    assert converted({"const": 1, "type": "number"}, "2") == "2"
    assert converted({"const": True, "type": "boolean"}, "true") is True
    assert converted({"const": "5", "type": "string"}, 5) == "5"


def test_enum_evaluates_to_a_union_of_literals():
    schema = {"enum": [1, 2], "type": "number"}
    assert converted(schema, "2") == 2
    assert converted(schema, "3") == "3"
    assert converted({"enum": ["a", "b"], "type": "string"}, "b") == "b"


def test_union_keeps_a_matching_value_and_takes_the_first_candidate_that_checks():
    schema = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    assert converted(schema, None) is None
    assert converted(schema, "42") == 42
    assert converted(schema, "null") is None
    assert converted(schema, "x") == "x"


def test_object_converts_known_properties_in_place_and_additional_ones():
    value = {"n": "1", "rest": "2", "keep": "x"}
    schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "number"}, "keep": {"type": "string"}},
              "additionalProperties": {"type": "number"}}
    assert converted(schema, value) is value
    assert value == {"n": 1, "rest": 2, "keep": "x"}
    assert converted(schema, ["not", "an", "object"]) == ["not", "an", "object"]


def test_record_converts_matching_keys():
    # TypeBox emits whole-key patterns (`^.*$`); each key is matched as `new RegExp(`^${key}$`)`.
    schema = {"type": "object", "patternProperties": {"^n_.*$": {"type": "number"}}}
    assert converted(schema, {"n_a": "1", "other": "2"}) == {"n_a": 1, "other": "2"}
    assert converted({"type": "object", "patternProperties": {"^n_": {"type": "number"}}}, {"n_a": "1"}) == {"n_a": "1"}


def test_an_unknown_kind_is_left_as_it_is():
    assert converted({"allOf": [{"type": "number"}]}, "1") == "1"
    assert converted({}, "1") == "1"


# ── through validateToolArguments, as the agent loop calls it ─────────────────────────────────


class Item(BaseModel):
    id: str


class Params(BaseModel):
    count: int
    ratio: float
    flag: bool
    names: list[str]
    items: list[Item]
    note: str | None = Field(default=None)


def call(arguments):
    tool = Tool(name="probe", description="probe", parameters=Params)
    return validateToolArguments(tool, ToolCall(type="toolCall", id="c1", name="probe", arguments=arguments))


def test_a_pydantic_tool_gets_upstreams_conversion():
    result = call({"count": "7.9", "ratio": "0.5", "flag": "1", "names": "solo", "items": {"id": 3}, "note": 5})
    assert result == {"count": 7, "ratio": 0.5, "flag": True, "names": ["solo"], "items": [{"id": "3"}], "note": "5"}


def test_a_json_encoded_list_still_fails_as_upstream_does():
    with pytest.raises(ValueError, match=r"items\.0"):
        call({"count": 1, "ratio": 1, "flag": True, "names": [], "items": '[{"id": "a"}]'})


def test_a_value_pydantic_would_take_but_typebox_check_refuses_is_refused():
    """Upstream accepts only what `Check` accepts after `Convert`: "yes" is no boolean to TypeBox (Convert leaves it),
    though pydantic's lax mode reads it as True."""
    with pytest.raises(ValueError, match=r"Validation failed for tool \"probe\":\n  - flag: 'yes' is not of type 'boolean'"):
        call({"count": 1, "ratio": 1, "flag": "yes", "names": [], "items": []})
    assert call({"count": 1, "ratio": 1, "flag": "true", "names": [], "items": []})["flag"] is True
