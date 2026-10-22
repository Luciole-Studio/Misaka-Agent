"""``misaka.chord._typebox`` against ``typebox`` 1.3.27 (the version chord 1.3.0 depends on), and the contract/schema
helpers that read JavaScript regular expressions and numbers.

Every expectation below was recorded from typebox 1.3.27 (or chord 1.3.0's sources) under Node.
"""

from __future__ import annotations

import json
import math

import pytest

from misaka.chord._typebox import Type, Value, _unionPrioritySort
from misaka.chord.contract import (
    contractFromDescription,
    hostNamed,
    local,
    opaque,
    remote,
)
from misaka.chord.schema import fits, normalize
from misaka.utils.js import UNDEFINED


def errors(schema, value):
    return [[error["instancePath"], error["message"]] for error in Value.Errors(schema, value)]


def text(value) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


# ─── Clean: TypeBox's union priority sort (V4-B07) ──────────────────────────


def test_clean_tries_the_narrower_object_first_by_typeboxs_structural_compare():
    # {b: string} is inside {a?: string} (assignable to it), so it is tried first and keeps `b`.
    union = Type.Union([Type.Object({"a": Type.Optional(Type.String())}), Type.Object({"b": Type.String()})])
    assert Value.Clean(union, {"b": "y"}) == {"b": "y"}


def test_clean_keeps_the_declaration_order_of_disjoint_branches():
    # Neither branch is inside the other: the first one cleans, not the wider one.
    union = Type.Union([Type.Object({"a": Type.Literal("x")}), Type.Object({"a": Type.String(), "b": Type.String()})])
    assert Value.Clean(union, {"a": "x", "b": "y", "c": 1}) == {"a": "x"}


def test_union_priority_sort_orders_narrowest_first():
    union = Type.Union(
        [
            Type.String(),
            Type.Literal("x"),
            Type.Number(),
            Type.Integer(),
            Type.Object({"a": Type.Optional(Type.String())}),
            Type.Object({"a": Type.String(), "b": Type.Number()}),
        ]
    )
    assert text(_unionPrioritySort(union)) == (
        '{"anyOf":[{"type":"string","const":"x"},{"type":"string"},{"type":"integer"},{"type":"number"},'
        '{"type":"object","required":["a","b"],"properties":{"a":{"type":"string"},"b":{"type":"number"}}},'
        '{"type":"object","properties":{"a":{"type":"string"}}}]}'
    )


def test_clean_sorts_unions_nested_in_objects_and_reads_a_records_custom_pattern_as_typebox_does():
    nested = Type.Object(
        {"p": Type.Optional(Type.Union([Type.Object({"a": Type.Optional(Type.String())}), Type.Object({"b": Type.String()})]))}
    )
    assert Value.Clean(nested, {"p": {"b": "y"}}) == {"p": {"b": "y"}}
    # TypeBox re-derives a record's key type while sorting: `^x` reads as `string`, a `^...$` pattern stays.
    assert Value.Clean(Type.Record(Type.String({"pattern": "^x"}), Type.Number()), {"x1": 1, "y": 2}) == {"x1": 1, "y": 2}
    assert Value.Clean(Type.Record(Type.String({"pattern": "^x.*$"}), Type.Number()), {"x1": 1, "y": 2}) == {"x1": 1}


def test_clean_walks_an_array_under_a_record_as_typebox_does():
    assert Value.Clean(Type.Record(Type.String(), Type.Object({"a": Type.Number()})), [{"a": 1, "b": 2}]) == [{"a": 1}]
    with pytest.raises(TypeError, match=r"Cannot delete property 'length' of \[object Array\]"):
        Value.Clean(Type.Record(Type.Number(), Type.Number()), [1])


# ─── Default: declaration order (V4-B08) ─────────────────────────────────────


def test_default_tries_union_branches_in_declaration_order():
    union = Type.Union(
        [
            Type.Object({"a": Type.Number({"default": 1})}),
            Type.Object({"a": Type.Number({"default": 1}), "b": Type.Number({"default": 2})}),
        ]
    )
    assert Value.Default(union, {}) == {"a": 1}
    assert Value.Default(union, {"a": 3}) == {"a": 3}


def test_default_gives_an_array_the_index_properties_of_an_object_schema():
    # `Guard.IsObject` accepts an array; index `1` grows it past a hole (JSON `[null,7]`).
    assert Value.Default(Type.Object({"1": Type.Number({"default": 7})}), []) == [UNDEFINED, 7]


# ─── patterns are JavaScript RegExps with the u flag (V4-B09) ────────────────


@pytest.mark.parametrize(
    ("pattern", "value", "expected"),
    [
        ("^a+$", "a\n", False),  # `$` is the end of the input
        ("^\\d+$", "١٢٣", False),  # `\d` is ASCII
        ("^\\w+$", "é", False),  # `\w` is ASCII
        ("^\\p{L}+$", "abc", True),  # property escapes under `u`
        ("^\\p{L}+$", "12", False),
        ("^(?<x>a)$", "a", True),  # named groups
    ],
)
def test_a_pattern_is_a_javascript_unicode_regexp(pattern, value, expected):
    assert Value.Check(Type.String({"pattern": pattern}), value) is expected


def test_fits_reads_pattern_properties_as_javascript_regexps():
    sub = {"type": "object", "properties": {"ab": {"type": "number"}}, "additionalProperties": False}
    sup = {"type": "object", "patternProperties": {"^\\p{L}+$": {"type": "number"}}}
    assert fits(sub, sup) is True
    bad = {"type": "object", "properties": {"ab": {"type": "string"}}, "additionalProperties": False}
    assert fits(bad, sup) is False


def test_additional_properties_use_one_expression_for_names_and_patterns():
    schema = {"type": "object", "properties": {"a.b": {}}, "patternProperties": {"^x": {}}, "additionalProperties": False}
    # `a.b` is a name, not a pattern: `axb` is additional.
    assert errors(schema, {"a.b": 1, "axb": 1, "x1": 1}) == [["/axb", "schema is false"], ["", "must not have additional properties"]]


# ─── lengths count grapheme clusters (V4-B10) ────────────────────────────────


@pytest.mark.parametrize(
    ("options", "value", "expected"),
    [
        ({"minLength": 2}, "❤️", False),  # heart + variation selector
        ({"minLength": 2}, "👨‍👩‍👧", False),  # ZWJ sequence
        ({"minLength": 2}, "é̂", False),  # combining marks
        ({"minLength": 2}, "🇯🇵", False),  # regional indicator pair
        ({"minLength": 2}, "ab", True),
        ({"maxLength": 1}, "é", True),
        ({"maxLength": 1}, "é", True),
        ({"maxLength": 1}, "🇯🇵", True),
        ({"maxLength": 2}, "éé", True),
        ({"minLength": 2, "maxLength": 3}, "🇯🇵🇯🇵", True),
        ({"minLength": 2}, "a‍b", False),
        ({"maxLength": 1}, "😀", True),
        ({"maxLength": 1}, "😀😀", False),
    ],
)
def test_string_lengths_count_typeboxs_grapheme_clusters(options, value, expected):
    assert Value.Check(Type.String(options), value) is expected


def test_length_messages_print_javascript_numbers():
    assert errors(Type.String({"minLength": 2.0}), "a") == [["", "must not have fewer than 2 characters"]]
    assert errors(Type.Number({"minimum": 1.0, "maximum": 2.5}), 0) == [["", "must be >= 1"]]


# ─── Intersect in Clean / Default (V4-B42) ───────────────────────────────────


def test_clean_evaluates_an_intersection_of_objects():
    intersection = Type.Intersect([Type.Object({"a": Type.Number()}), Type.Object({"b": Type.String()})])
    assert Value.Clean(intersection, {"a": 1, "b": "x", "c": 1}) == {"a": 1, "b": "x"}
    loose = Type.Intersect([Type.Object({"a": Type.Number()}), Type.Object({"b": Type.String()})], {"unevaluatedProperties": True})
    assert Value.Clean(loose, {"a": 1, "b": "x", "c": 1}) == {"a": 1, "b": "x", "c": 1}
    union = Type.Union([Type.Intersect([Type.Object({"a": Type.Number()}), Type.Object({"b": Type.Number()})]), Type.Object({"a": Type.Number()})])
    assert Value.Clean(union, {"a": 1, "b": 2, "c": 3}) == {"a": 1, "b": 2}


def test_default_evaluates_an_intersection_as_typeboxs_type_engine_does():
    # The shared `a` narrows to the second schema (equal types: the right one), which has no default.
    intersection = Type.Intersect(
        [
            Type.Object({"a": Type.Number({"default": 2})}),
            Type.Object({"b": Type.String({"default": "q"}), "a": Type.Optional(Type.Number({"minimum": 1}))}),
        ]
    )
    assert Value.Default(intersection, {}) == {"b": "q"}


# ─── multipleOf (V4-B43), paths (V4-B44), messages (V4-B45) ─────────────────


@pytest.mark.parametrize(("value", "expected"), [(0.3, True), (0.7, True), (0.25, False), (3, True)])
def test_multiple_of_allows_typeboxs_tolerance(value, expected):
    assert Value.Check(Type.Number({"multipleOf": 0.1}), value) is expected


def test_multiple_of_zero_is_never_satisfied_and_does_not_raise():
    assert Value.Check({"type": "number", "multipleOf": 0}, 1.5) is False
    assert Value.Check({"type": "number", "multipleOf": 0}, 2) is False


def test_instance_paths_hold_raw_property_names():
    assert errors(Type.Record(Type.String(), Type.Number()), {"a/b": "x"}) == [["/a/b", "must be number"]]
    raw = {"type": "object", "properties": {"a/b": {"type": "number"}, "c~d": {"type": "number"}}}
    assert errors(raw, {"a/b": "x", "c~d": "y"}) == [["/a/b", "must be number"], ["/c~d", "must be number"]]


def test_a_type_list_message_says_either():
    assert errors({"type": ["string", "number"]}, True) == [["", "must be either string or number"]]
    assert errors({"type": ["string"]}, True) == [["", "must be either string"]]
    # An unknown type name passes (TypeBox's `CheckTypeName`).
    assert Value.Check({"type": "weird"}, 1) is True


def test_errors_reports_at_most_typeboxs_max_errors():
    schema = Type.Object({key: Type.String() for key in "abcdefghi"})
    assert errors(schema, dict.fromkeys("abcdefghi", 1)) == [[f"/{key}", "must be string"] for key in "abcdefgh"]


def test_numeric_keywords_apply_only_to_finite_numbers():
    assert errors({"minimum": 0}, math.nan) == []
    assert errors({"type": "number", "minimum": 0}, -math.inf) == [["", "must be number"]]


# ─── Enum and Record keys (V4-B46) ───────────────────────────────────────────


def test_an_enum_of_an_object_takes_its_values():
    schema = Type.Enum({"A": "a", "B": 2, "0": "A", "1.5": "q", "Infinity": "i", " ": "s"})
    assert schema == {"enum": ["a", 2]}


def test_record_keys_build_what_typebox_builds():
    assert text(Type.Record(Type.Number(), Type.String())) == (
        '{"type":"object","patternProperties":{"^-?(?:0|[1-9][0-9]*)(?:\\\\.[0-9]+)?$":{"type":"string"}}}'
    )
    assert text(Type.Record(Type.Integer(), Type.String())) == '{"type":"object","patternProperties":{"^-?(?:0|[1-9][0-9]*)$":{"type":"string"}}}'
    assert text(Type.Record(Type.Boolean(), Type.Number())) == (
        '{"type":"object","required":["true","false"],"properties":{"true":{"type":"number"},"false":{"type":"number"}}}'
    )
    assert text(Type.Record(Type.Union([Type.Literal("b"), Type.Literal(1), Type.Literal("a")]), Type.Number())) == (
        '{"type":"object","required":["1","b","a"],"properties":{"1":{"type":"number"},"b":{"type":"number"},"a":{"type":"number"}}}'
    )
    assert text(Type.Record(Type.Literal(5), Type.Number(), {"description": "d"})) == (
        '{"type":"object","required":["5"],"properties":{"5":{"type":"number"}},"description":"d"}'
    )
    assert text(Type.Record(Type.Enum(["e", "f"]), Type.Number())) == (
        '{"type":"object","required":["e","f"],"properties":{"e":{"type":"number"},"f":{"type":"number"}}}'
    )
    assert text(Type.Record(Type.Unknown(), Type.Number())) == '{"type":"object","properties":{}}'
    assert text(Type.Record(Type.String(), Type.Unknown())) == '{"type":"object","patternProperties":{"^.*$":{}}}'


# ─── MISAKA fork: hand-written schemas are cleaned like built ones (V4-B47) ──


def test_a_hand_written_schema_is_cleaned_and_defaulted_like_a_built_one():
    # TypeBox leaves a schema without its hidden `~kind` alone; here the kind is read from the JSON (module docstring).
    raw = {"type": "object", "properties": {"a": {"type": "number", "default": 3}}, "required": ["a"]}
    assert Value.Default(raw, {}) == {"a": 3}
    assert Value.Clean(raw, {"a": 1, "z": 2}) == {"a": 1}
    assert normalize(raw, {"z": 1}, "t") == {"a": 3}


# ─── contract helpers (V4-B48, V4-B35, V4-O6) ────────────────────────────────


@pytest.mark.parametrize("id", ["x@1\n", "x@١", "x@1 ", "@1", "x@"])
def test_an_opaque_id_is_checked_as_javascript_does(id):
    with pytest.raises(TypeError, match="opaque id must be name@version"):
        opaque(id)


def test_an_opaque_id_accepts_name_at_version():
    assert opaque("pkg.native@12").id == "pkg.native@12"


def test_a_contract_version_is_a_javascript_integer():
    whole = remote("A", {"version": 2.0, "input": Type.Object({}), "host": hostNamed("s"), "variants": {"v": {}}})
    exact = remote("A", {"version": 2, "input": Type.Object({}), "host": hostNamed("s"), "variants": {"v": {}}})
    assert whole.version == 2 and isinstance(whole.version, int)
    assert whole.fingerprint == exact.fingerprint
    received = contractFromDescription({**exact.description, "version": 2.0})
    assert received.version == 2 and received.fingerprint == exact.fingerprint
    assert local("L", {"version": 1.0, "input": Type.Object({})}).version == 1
    # `checkHeader` has no default: a missing version is not 1.
    for version in (None, 1.5, 0, True):
        with pytest.raises(TypeError, match="contract version must be a positive integer"):
            local("L", {"version": version, "input": Type.Object({})})


def test_a_remote_contract_without_a_host_names_the_builders_with_an_ellipsis():
    with pytest.raises(TypeError) as raised:
        remote("A", {"version": 1, "input": Type.Object({}), "variants": {"v": {}}})
    assert str(raised.value) == "remote contract A needs `host`: hostNamed(…) or hostFamily(…)"


# ─── record key patterns are decoded as TypeBox decodes them (VERIFY2 R2-T1) ──
# Expected values recorded from typebox 1.3.27 under Node (`UnionPrioritySort`, `Value.Clean`).


def _patternRecord(pattern: str):
    return Type.Record(Type.String({"pattern": pattern}), Type.Number())


@pytest.mark.parametrize(
    ("pattern", "sorted_"),
    [
        ("^x.*$", {"type": "object", "patternProperties": {"^x.*$": {"type": "number"}}}),
        ("^a+$", {"type": "object", "required": ["a+"], "properties": {"a+": {"type": "number"}}, "patternProperties": {"^a+$": {"type": "number"}}}),
        ("^\\d+$", {"type": "object", "required": ["\\d+"], "properties": {"\\d+": {"type": "number"}}, "patternProperties": {"^\\d+$": {"type": "number"}}}),
        ("^.$", {"type": "object", "required": ["."], "properties": {".": {"type": "number"}}, "patternProperties": {"^.$": {"type": "number"}}}),
        ("^$", {"type": "object", "patternProperties": {"^.*$": {"type": "number"}}}),
        ("^(a|b)$", {"type": "object", "required": ["a", "b"], "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "patternProperties": {"^(a|b)$": {"type": "number"}}}),
        ("x", {"type": "object", "patternProperties": {"^.*$": {"type": "number"}}}),
        ("^a.*b$", {"type": "object", "patternProperties": {"^a.*b$": {"type": "number"}}}),
        ("^(?<x>a)b$", {"type": "object", "required": ["?<x>ab"], "properties": {"?<x>ab": {"type": "number"}}, "patternProperties": {"^(?<x>a)b$": {"type": "number"}}}),
        ("^x(.*)$", {"type": "object", "patternProperties": {"^x(.*)$": {"type": "number"}}}),
        ("^-?(?:0|[1-9][0-9]*)$", {"type": "object", "patternProperties": {"^-?(?:0|[1-9][0-9]*)$": {"type": "number"}}}),
    ],
)
def test_a_records_key_pattern_is_decoded_as_typebox_decodes_it(pattern, sorted_):
    assert _unionPrioritySort(_patternRecord(pattern)) == sorted_


def test_clean_keeps_only_the_decoded_keys_of_a_finite_record_pattern():
    assert Value.Clean(_patternRecord("^a+$"), {"a+": 1, "aa": 2, "a": 3}) == {"a+": 1}
    assert Value.Clean(_patternRecord("^a|b$"), {"a": 1, "b": 2, "ab": 3}) == {"ab": 3}  # TypeBox's grammar: a sequence
    assert Value.Clean(_patternRecord("^ /*c*/a$"), {"a": 1, " a": 2, " /*c*/a": 3}) == {" /*c*/a": 3}
    assert Value.Clean(_patternRecord("^x.*$"), {"xa": 1, "y": 2, "x": 3}) == {"xa": 1, "x": 3}
    assert Value.Clean(_patternRecord("x"), {"q": 1, "x": 2}) == {"q": 1, "x": 2}


def test_clean_reads_a_finite_record_pattern_as_an_object_also_in_a_union():
    # An object leaves an array alone (where a record walk would fail on `length`).
    assert Value.Clean(_patternRecord("^(a|b)$"), [1, 2]) == [1, 2]
    union = Type.Union([_patternRecord("^(a|b)$"), Type.Object({"z": Type.String()})])
    assert Value.Clean(union, {"a": 1, "z": "q"}) == {"z": "q"}


# ─── unions are sorted at every Clean (VERIFY2 R2-T2) ────────────────────────


def test_clean_sorts_a_schema_changed_since_an_earlier_clean():
    schema = Type.Object({"u": Type.Union([Type.Object({"a": Type.String()}), Type.Object({"b": Type.String()})])})
    assert Value.Clean(schema, {"u": {"a": "x", "b": "y"}}) == {"u": {"a": "x"}}
    # TypeBox's types are not frozen (`Settings.immutableTypes` is false): a wider branch added later is used.
    schema["properties"]["u"]["anyOf"].insert(0, Type.Object({"a": Type.String(), "b": Type.String()}))
    assert Value.Clean(schema, {"u": {"a": "x", "b": "y"}}) == {"u": {"a": "x", "b": "y"}}


# ─── `new RegExp` refuses what JavaScript refuses (VERIFY2 R2-T3) ────────────


@pytest.mark.parametrize(
    ("pattern", "reason"),
    [
        ("\\a", "Invalid escape"),
        ("]", "Lone quantifier brackets"),
        ("{", "Lone quantifier brackets"),
        ("a{", "Incomplete quantifier"),
        ("\\-", "Invalid escape"),
        ("(?i)a", "Invalid group"),
        ("a{,2}", "Incomplete quantifier"),
        ("\\c", "Invalid Unicode escape"),
        ("[\\d-z]", "Invalid character class"),
        ("(?<a>x)(?<a>y)", "Duplicate capture group name"),
        ("(?=a)*a", "Invalid quantifier"),
        ("\\01", "Invalid decimal escape"),
        ("\\p{Foo}", "Invalid property name"),
        ("^\\p{Han}$", "Invalid property name"),
        ("\\k<n>", "Invalid named capture referenced"),
        ("[z-a]", "Range out of order in character class"),
        ("a{2,1}", "numbers out of order in {} quantifier"),
        ("\\u{110000}", "Invalid Unicode escape"),
        ("(?-:a)", "Invalid flag group"),
    ],
)
def test_a_pattern_javascript_refuses_under_u_throws_its_syntax_error(pattern, reason):
    with pytest.raises(SyntaxError) as raised:
        Value.Check(Type.String({"pattern": pattern}), "a")
    assert str(raised.value) == f"Invalid regular expression: /{pattern}/u: {reason}"


@pytest.mark.parametrize(
    ("pattern", "value", "expected"),
    [
        ("(?i:a)", "A", True),  # modifiers (ECMAScript 2025)
        ("^\\p{sc=Greek}+$", "αβ", True),
        ("^(?:(?<a>x)|(?<a>y))$", "y", True),  # a duplicate name in another alternative
    ],
)
def test_a_pattern_javascript_accepts_under_u_is_not_refused(pattern, value, expected):
    assert Value.Check(Type.String({"pattern": pattern}), value) is expected


def test_pattern_properties_and_record_walks_refuse_what_javascript_refuses():
    with pytest.raises(SyntaxError, match=r"/a\{,2\}/u: Incomplete quantifier"):
        Value.Check(Type.Object({}, {"patternProperties": {"a{,2}": Type.Number()}}), {"a": 1})
    # `Clean` and `Default` walk a record with `new RegExp(pattern)` (no `u`): Annex B's grammar, which still refuses
    # `(?i)` but takes `\a`.
    with pytest.raises(SyntaxError, match=r"/\^\(\?i\)a\.\*\$/: Invalid group"):
        Value.Clean(_patternRecord("^(?i)a.*$"), {"a": 1})
    with pytest.raises(SyntaxError, match=r"/\(\?i\)a/: Invalid group"):
        Value.Default(_patternRecord("(?i)a"), {"a": 1})
    assert Value.Clean(_patternRecord("^\\a.*$"), {"a": 1, "b": 2}) == {"a": 1}
