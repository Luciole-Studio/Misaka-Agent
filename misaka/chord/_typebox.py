"""MISAKA fork: the parts of the ``typebox`` npm package (1.3.27) that Chord uses -- ``Type`` builders and the
``Value`` operations ``Check``, ``Errors``, ``Clean``, ``Default`` and ``Clone``.

A schema is the JSON Schema TypeBox builds, as a ``dict``, with TypeBox's key order (``type``, ``required``,
``properties``, then the options). TypeBox marks optional properties and immutable arrays with hidden (non-enumerable)
``~optional`` / ``~immutable`` keys that ``JSON.stringify`` drops; a dict has no hidden keys, so ``Type.Optional``
returns a marked copy that ``Type.Object`` reads and leaves out of ``required``, and ``Type.Immutable`` is the schema
itself. TypeBox recognizes the kind of a schema by its hidden ``~kind`` (``Object``, ``Record``, ``Union``, ...); here
the kind is read from the JSON Schema itself (``_kind``: ``type: "object"`` with ``properties`` is an object,
``patternProperties`` without ``properties`` a record, ``anyOf`` a union, ``allOf`` an intersection, ``{}`` unknown).
Two consequences differ from TypeBox: ``Type.Any`` and ``Type.Unknown`` emit the same ``{}`` and are both read as
``Unknown``, and ``Type.Unsafe`` is read by its JSON (TypeBox's type engine reads an unsafe schema as ``Unknown``).
And a hand-written JSON Schema (no ``~kind``: TypeBox's ``Clean``/``Default`` leave the value as it is) is cleaned and
defaulted like a built one. The one Chord path that reaches this is ``ObjectImpl.call`` cleaning a call's arguments
with the schema a remote consumer sent in its contract token (``wanted.args``, JSON from the wire); a consumer strips
with its own schema before sending (``RemoteRef.call``), so for a well-behaved peer that clean removes nothing.

``Clean`` first applies TypeBox's ``UnionPrioritySort`` (``_unionPrioritySort``): every union's branches are put in
TypeBox's priority order, narrowest first, by its insertion sort over its structural ``Compare`` (``_priority``,
``_compare``, ``_extends`` port ``type/engine/priority``, ``type/engine/evaluate/compare`` and ``type/extends``), so a
disjoint branch keeps its declaration order. Intersections are evaluated as TypeBox's type engine does
(``_evaluateIntersect``: distribute, narrow, composite objects, broaden). ``Default`` tries union branches in
declaration order. Not ported, because Chord's schemas use none of them: references, cyclic and recursive types,
tuples, template literals (beyond decoding a record's key pattern), ``infer``, bigint, symbols and constructors. When
unions are sorted, a record is rebuilt from its key pattern as TypeBox rebuilds it (``RecordPatternToType``, its
template-literal ``Pattern`` grammar ported in ``_parsePatternIntoTypes``): a finite pattern becomes an object of the
keys it spells (``^(a|b)$`` the keys ``a`` and ``b``; ``^a+$`` the one key ``a+``, as TypeBox reads text literally), a
pattern that does not parse reads as ``string`` (``^.*$``), and any other one (``^x.*$``) stays. TypeBox sorts at every
``Clean`` (its types are not frozen), and so does this module.

``Check`` and ``Errors`` implement the JSON Schema keywords TypeBox emits plus the common constraints, with JavaScript's
value semantics (a ``bool`` is not a number, ``1.0`` is an integer, NaN and the infinities are not numbers, a
``pattern`` is a JavaScript ``RegExp`` with the ``u`` flag, a record's walk one without it) and TypeBox's error messages
and paths; ``minLength``/``maxLength`` count grapheme clusters by TypeBox's own segmentation (``GraphemeCount`` with its
fast paths, over UTF-16 code units); ``multipleOf`` allows TypeBox's 1e-10 tolerance; ``Errors`` stops at TypeBox's
``maxErrors`` (8). ``UNDEFINED`` is JavaScript's ``undefined`` (accepted for an optional property, as TypeBox does;
it also stands for an array hole). An array given to a record schema (``Clean``, ``Default``) or to an object schema
(``Default``) is walked as JavaScript sees it, as an object of its indices and ``length``; named properties that
JavaScript would set on such an array are left out (JSON does not carry them). Property names inherited from
JavaScript's ``Object.prototype`` (``"toString" in {}``) are not emulated. A pattern is first checked against
ECMAScript's grammar (``_PatternSyntax``: the ``u`` grammar, or Annex B's without ``u``), so what ``new RegExp`` refuses
raises its ``SyntaxError`` (``(?i)a``, ``a{,2}``, ``\\a`` or ``\\p{Han}`` under ``u``), then matched by
``misaka.utils.js_regexp`` (JavaScript's backreferences and Annex B escapes included), whose limits remain: captures
inside a quantified atom are not cleared at each iteration, an empty iteration of a ``*``/``?`` quantifier is not
rejected, and ``[^\\D]``-style complemented escapes in a class and the ``s``/``m`` modifiers inside ``(?s:...)`` are
not supported.

Portions of this file are derived from TypeBox 1.3.27 (https://github.com/sinclairzx81/typebox, npm ``typebox``):

The MIT License (MIT)

Copyright (c) 2017-2026 Haydn Paterson

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from functools import lru_cache
from itertools import islice
from typing import Any, Final

import regex

from misaka.utils.js import (
    UNDEFINED,
    formatNumber,
    fromUtf16Units,
    jsNumber,
    jsString,
    jsTrimStart,
    objectKeys,
    toUtf16Units,
)
from misaka.utils.js_regexp import RegExp

type TSchema = dict[str, Any]
type Static = Any


class _Optional(dict):  # type: ignore[type-arg]
    """``Type.Optional(schema)``: the schema, marked optional for the enclosing ``Type.Object``."""


def _options(schema: dict[str, Any], options: Mapping[str, Any] | None) -> TSchema:
    if options:
        schema.update(options)
    return schema


# `Settings.maxErrors`: `Value.Errors` reports at most this many errors.
_MAX_ERRORS: Final = 8

# Record key patterns (`type/types/{string,integer,number}.mjs`).
_STRING_KEY: Final = "^.*$"
_INTEGER_KEY: Final = "^-?(?:0|[1-9][0-9]*)$"
_NUMBER_KEY: Final = "^-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?$"


class _Type:
    """``Type``: builders of the JSON Schemas TypeBox emits."""

    @staticmethod
    def Object(properties: Mapping[str, TSchema], options: Mapping[str, Any] | None = None) -> TSchema:
        required = [key for key, value in properties.items() if not isinstance(value, _Optional)]
        schema: dict[str, Any] = {"type": "object"}
        if required:
            schema["required"] = required
        schema["properties"] = {key: dict(value) for key, value in properties.items()}
        return _options(schema, options)

    @staticmethod
    def Optional(schema: TSchema) -> TSchema:
        return _Optional(schema)

    @staticmethod
    def Immutable(schema: TSchema) -> TSchema:
        return schema

    @staticmethod
    def String(options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"type": "string"}, options)

    @staticmethod
    def Number(options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"type": "number"}, options)

    @staticmethod
    def Integer(options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"type": "integer"}, options)

    @staticmethod
    def Boolean(options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"type": "boolean"}, options)

    @staticmethod
    def Null(options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"type": "null"}, options)

    @staticmethod
    def Void(options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"type": "void"}, options)

    @staticmethod
    def Undefined(options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"type": "undefined"}, options)

    @staticmethod
    def Unknown(options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({}, options)

    @staticmethod
    def Any(options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({}, options)

    @staticmethod
    def Literal(value: str | float | bool, options: Mapping[str, Any] | None = None) -> TSchema:
        kind = "boolean" if isinstance(value, bool) else "string" if isinstance(value, str) else "number"
        return _options({"type": kind, "const": value}, options)

    @staticmethod
    def Enum(values: Sequence[Any] | Mapping[str, Any], options: Mapping[str, Any] | None = None) -> TSchema:
        # An object is a TypeScript enum (`IsTypeScriptEnumLike`): its values, without the numeric keys of a numeric
        # enum's reverse mapping (`TypeScriptEnumToEnumValues`: the keys that are `isNaN`).
        if isinstance(values, Mapping):
            values = [values[key] for key in values if math.isnan(jsNumber(key))]
        return _options({"enum": list(values)}, options)

    @staticmethod
    def Union(schemas: Sequence[TSchema], options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"anyOf": [dict(schema) for schema in schemas]}, options)

    @staticmethod
    def Intersect(schemas: Sequence[TSchema], options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"allOf": [dict(schema) for schema in schemas]}, options)

    @staticmethod
    def Array(items: TSchema, options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"type": "array", "items": dict(items)}, options)

    @staticmethod
    def Record(key: TSchema, value: TSchema, options: Mapping[str, Any] | None = None) -> TSchema:
        """A record (``patternProperties``) for a ``string``, ``number`` or ``integer`` key, an object of the keys for a
        literal, boolean or finite union key (``type/engine/record/from_key*``)."""
        return _options(_recordFromKey(key, dict(value)), options)

    @staticmethod
    def Function(parameters: Sequence[TSchema], returns: TSchema, options: Mapping[str, Any] | None = None) -> TSchema:
        return _options({"type": "function", "parameters": [dict(p) for p in parameters], "returnType": dict(returns)}, options)

    @staticmethod
    def Unsafe(schema: Mapping[str, Any]) -> TSchema:
        return dict(schema)


Type = _Type()


def _createRecord(pattern: str, value: TSchema) -> TSchema:
    return {"type": "object", "patternProperties": {pattern: value}}


def _propertyKey(value: Any) -> str:
    """A literal as a JavaScript property key (``String(value)``)."""
    return value if isinstance(value, str) else formatNumber(value)


def _keyed(keys: Sequence[str], value: TSchema) -> TSchema:
    """``Object({ [key]: value, ... })``: keys in JavaScript's property order (array indices first)."""
    properties = dict.fromkeys(keys)
    return Type.Object({key: value for key in objectKeys(properties)})


def _recordFromKey(key: Any, value: TSchema) -> TSchema:
    """``FromKey``: what ``Type.Record(key, value)`` builds for each kind of key."""
    # `{}` is `Type.Unknown()` here, which falls through to `Object({})` (`Type.Any()` would make a string record).
    kind = _kind(key)
    if kind == "Boolean":
        return _keyed(["true", "false"], value)
    if kind == "Enum":
        return _recordFromKey(_evaluateEnum(key["enum"]), value)
    if kind == "Integer":
        return _createRecord(_INTEGER_KEY, value)
    if kind == "Intersect":
        return _recordFromKey(_evaluateIntersect(key["allOf"]), value)
    if kind == "Literal":
        const = key["const"]
        if isinstance(const, bool):
            return _keyed(["true" if const else "false"], value)
        return _keyed([_propertyKey(const)], value)
    if kind == "Number":
        return _createRecord(_NUMBER_KEY, value)
    if kind == "Union":
        types = _flatten(key["anyOf"])
        if any(_kind(type_) in ("String", "Number", "Integer") for type_ in types):
            return _createRecord(_STRING_KEY, value)
        literals = [type_["const"] for type_ in types if _kind(type_) == "Literal" and not isinstance(type_["const"], bool)]
        return _keyed([_propertyKey(literal) for literal in literals], value)
    if kind == "String":
        pattern = key.get("pattern")
        return _createRecord(pattern if isinstance(pattern, str) else _STRING_KEY, value)
    return Type.Object({})


# ─── kinds ───────────────────────────────────────────────────────────────────

_SIMPLE_KINDS: Final = {
    "string": "String",
    "number": "Number",
    "integer": "Integer",
    "boolean": "Boolean",
    "null": "Null",
    "void": "Void",
    "undefined": "Undefined",
}
# Keywords that make a schema something other than `Unknown` (`{}` with annotations).
_STRUCTURAL: Final = frozenset(
    {"type", "anyOf", "allOf", "oneOf", "enum", "const", "not", "$ref", "properties", "patternProperties", "items", "prefixItems", "if"}
)


def _isLiteralValue(value: Any) -> bool:
    return isinstance(value, str | bool) or (isinstance(value, int | float) and math.isfinite(value))


def _kind(schema: Any) -> str | None:
    """The TypeBox kind of a schema (its hidden ``~kind``), read from its JSON; ``None`` for none TypeBox builds."""
    if not isinstance(schema, dict):
        return None
    if isinstance(schema.get("anyOf"), list):
        return "Union"
    if isinstance(schema.get("allOf"), list):
        return "Intersect"
    if isinstance(schema.get("enum"), list):
        return "Enum"
    kind = schema.get("type")
    if "const" in schema and _isLiteralValue(schema["const"]) and kind in (None, "string", "number", "boolean"):
        return "Literal"
    if kind is None:
        if schema.get("not") == {} and isinstance(schema.get("not"), dict):
            return "Never"
        return None if any(key in schema for key in _STRUCTURAL) else "Unknown"
    if kind == "object":
        if isinstance(schema.get("properties"), dict):
            return "Object"
        return "Record" if isinstance(schema.get("patternProperties"), dict) and schema["patternProperties"] else None
    if kind == "array":
        return "Array" if isinstance(schema.get("items"), dict) else None
    if kind == "function":
        return "Function" if isinstance(schema.get("parameters"), list) else None
    return _SIMPLE_KINDS.get(kind) if isinstance(kind, str) else None


def _never() -> TSchema:
    return {"not": {}}


def _properties(schema: TSchema) -> dict[str, tuple[Any, bool]]:
    """An object's properties with their optionality (TypeBox's hidden ``~optional``: not in ``required``)."""
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    return {key: (value, key not in required) for key, value in schema["properties"].items()}


def _recordPattern(schema: TSchema) -> str:
    return next(iter(schema["patternProperties"]))


def _recordValue(schema: TSchema) -> Any:
    return schema["patternProperties"][_recordPattern(schema)]


# ─── type engine: extends, compare, priority, evaluate ──────────────────────


def _strictEqual(a: Any, b: Any) -> bool:
    """JavaScript's ``===`` between literal values."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, str) or isinstance(b, str):
        return type(a) is type(b) and a == b
    return a == b


def _extends(left: Any, right: Any) -> bool:
    """``Extends({}, left, right)`` (``type/extends``): whether ``left`` is assignable to ``right`` at the type level
    (constraints such as ``minLength`` are not part of a type). True for TypeBox's ``ExtendsTrue`` and
    ``ExtendsUnion``."""
    kind = _kind(left)
    rightKind = _kind(right)
    if kind == "Array":
        # Immutable arrays are the arrays themselves here (no `~immutable`): mutable extends mutable.
        return _extends(left["items"], right["items"]) if rightKind == "Array" else _extendsRight(left, right)
    if kind == "Boolean":
        return rightKind == "Boolean" or _extendsRight(left, right)
    if kind == "Enum":
        return _extends(_evaluateEnum(left["enum"]), right)
    if kind == "Function":
        if rightKind == "Unknown":
            return True
        if rightKind == "Function":
            return _extendsParameters(left["parameters"], right["parameters"]) and _extendsReturnType(
                left.get("returnType"), right.get("returnType")
            )
        return False
    if kind == "Integer":
        return rightKind in ("Integer", "Number") or _extendsRight(left, right)
    if kind == "Intersect":
        return _extends(_evaluateIntersect(left["allOf"]), right)
    if kind == "Literal":
        const = left["const"]
        if rightKind == "Literal":
            return _strictEqual(const, right["const"])
        primitive = "Boolean" if isinstance(const, bool) else "String" if isinstance(const, str) else "Number"
        return rightKind == primitive or _extendsRight(left, right)
    if kind == "Never":
        return True
    if kind == "Null":
        return rightKind == "Null" or _extendsRight(left, right)
    if kind == "Number":
        return rightKind == "Number" or _extendsRight(left, right)
    if kind == "Object":
        if rightKind == "Record":
            value = _recordValue(right)
            return all(_extends(property, value) for property, _ in _properties(left).values())
        if rightKind == "Object":
            return _extendsProperties(_properties(left), _properties(right))
        return _extendsRight(left, right)
    if kind == "Record":
        if rightKind == "Record":
            return _extends(_recordValue(left), _recordValue(right))
        if rightKind == "Object":
            return len(right["properties"]) == 0
        return rightKind == "Unknown"
    if kind == "String":
        return rightKind == "String" or _extendsRight(left, right)
    if kind == "Undefined":
        return rightKind in ("Void", "Undefined") or _extendsRight(left, right)
    if kind == "Union":
        targets = right["anyOf"] if rightKind == "Union" else [right]
        return all(any(_extends(member, target) for target in targets) for member in left["anyOf"])
    if kind == "Unknown":
        return rightKind == "Unknown"
    if kind == "Void":
        return rightKind == "Void" or _extendsRight(left, right)
    return False


def _extendsRight(left: Any, right: Any) -> bool:
    """``ExtendsRight``: ``right`` is ``any``/``unknown``, an enum, an intersection or a union."""
    kind = _kind(right)
    if kind == "Enum":
        return _extends(left, _evaluateEnum(right["enum"]))
    if kind == "Intersect":
        return all(_extends(left, member) for member in right["allOf"])
    if kind == "Union":
        return any(_extends(left, member) for member in right["anyOf"])
    return kind == "Unknown"


def _extendsProperties(left: dict[str, tuple[Any, bool]], right: dict[str, tuple[Any, bool]]) -> bool:
    for key, (property, optional) in right.items():
        if key in left:
            leftProperty, leftOptional = left[key]
            # An optional property only extends an optional one.
            if not _extends(leftProperty, property) or (leftOptional and not optional):
                return False
        elif not optional:
            return False
    return True


def _extendsParameters(left: list[Any], right: list[Any]) -> bool:
    """Parameters are contravariant; a function taking fewer parameters extends one taking more. (Optional
    parameters are not marked here: every parameter is required.)"""
    for index, parameter in enumerate(left):
        if index >= len(right) or not _extends(right[index], parameter):
            return False
    return True


def _extendsReturnType(left: Any, right: Any) -> bool:
    return _kind(right) == "Void" or _extends(left, right)


_EQUAL: Final = 0
_DISJOINT: Final = 1
_LEFT_INSIDE: Final = 2
_RIGHT_INSIDE: Final = 3


def _compare(left: Any, right: Any) -> int:
    """``Compare``: the set relation of two types."""
    leftExtends = _extends(left, right)
    rightExtends = _extends(right, left)
    if leftExtends and rightExtends:
        return _EQUAL
    if leftExtends:
        return _LEFT_INSIDE
    if rightExtends:
        return _RIGHT_INSIDE
    return _DISJOINT


def _priority(types: Sequence[Any]) -> list[Any]:
    """``Priority``: an insertion sort, narrowest first. A type is inserted before the first sorted type that is not
    inside it or disjoint from it, so disjoint types keep their order."""
    result: list[Any] = []
    for type_ in types:
        for index, sorted_ in enumerate(result):
            if _compare(type_, sorted_) not in (_RIGHT_INSIDE, _DISJOINT):
                result = [*result[:index], type_, *result[index:]]
                break
        else:
            result = [*result, type_]
    return result


# The bodies of TypeBox's key patterns: `PatternBase` reads each as a type; `PatternText` reads up to the first of them
# (or a parenthesis, `$` or `|`).
_PATTERN_BIGINT: Final = "-?(?:0|[1-9][0-9]*)n"
_PATTERN_STRING: Final = ".*"
_PATTERN_NUMBER: Final = "-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?"
_PATTERN_INTEGER: Final = "-?(?:0|[1-9][0-9]*)"
_PATTERN_NEVER: Final = "(?!)"
_PATTERN_BASES: Final = (
    (_PATTERN_BIGINT, "BigInt"),
    (_PATTERN_STRING, "String"),
    (_PATTERN_NUMBER, "Number"),
    (_PATTERN_INTEGER, "Integer"),
    (_PATTERN_NEVER, "Never"),
)
_PATTERN_TEXT_ENDS: Final = (*(base for base, _ in _PATTERN_BASES), "(", ")", "$", "|")

# A parsed pattern part: ("Literal", text), ("Union", parts), or the kind of a non-finite base ("String", ...).
type _PatternType = tuple[Any, ...]


def _patternTrim(text: str) -> str:
    """ParseBox's ``Trim`` (what ``Token.Const`` does before it compares): leading white space and comments go."""
    while True:
        text = jsTrimStart(text)
        if text.startswith("/*"):
            end = text.find("*/", 2)
            text = "" if end == -1 else text[end + 2 :]
        elif text.startswith("//"):
            end = text.find("\n", 2)
            text = "" if end == -1 else text[end:]
        else:
            return text


def _patternConst(const: str, text: str) -> str | None:
    """``Token.Const``: the rest after ``const``, or None."""
    text = _patternTrim(text)
    return text[len(const) :] if text.startswith(const) else None


def _patternBody(text: str) -> tuple[list[_PatternType], str]:
    """``PatternBody`` (= ``PatternUnion``, which always matches): ``Term '|' Union | Term | ε``, its terms' types
    concatenated (TypeBox's grammar flattens the alternatives and the sequences of a union alike)."""
    term = _patternTerm(text)
    if term is None:
        return [], text
    types, rest = term
    after = _patternConst("|", rest)
    if after is None:
        return types, rest
    more, rest = _patternBody(after)
    return [*types, *more], rest


def _patternTerm(text: str) -> tuple[list[_PatternType], str] | None:
    """``PatternTerm``: ``Base Body``."""
    base = _patternBase(text)
    if base is None:
        return None
    type_, rest = base
    body, rest = _patternBody(rest)
    return [type_, *body], rest


def _patternBase(text: str) -> tuple[_PatternType, str] | None:
    """``PatternBase``: a key pattern (bigint, string, number, integer, never), a group, or text."""
    for pattern, kind in _PATTERN_BASES:
        rest = _patternConst(pattern, text)
        if rest is not None:
            return (kind,), rest
    rest = _patternConst("(", text)
    if rest is not None:
        body, rest = _patternBody(rest)
        rest = _patternConst(")", rest)
        if rest is not None:
            return ("Union", body), rest
    # `Token.Until_1`: the text up to (not including) the first end; no match without one, or when it is empty.
    for index in range(len(text)):
        if text.startswith(_PATTERN_TEXT_ENDS, index):
            return (("Literal", text[:index]), text[index:]) if index > 0 else None
    return None


def _parsePatternIntoTypes(pattern: str) -> list[_PatternType]:
    """``ParsePatternIntoTypes``: ``'^' Body '$'`` (what follows the ``$`` is not read); ``[]`` when it does not
    parse."""
    rest = _patternConst("^", pattern)
    if rest is None:
        return []
    body, rest = _patternBody(rest)
    return body if _patternConst("$", rest) is not None else []


def _isPatternFinite(types: Sequence[_PatternType]) -> bool:
    """``IsTemplateLiteralFinite``: only literals, in (non-empty) unions."""
    return len(types) > 0 and all(
        type_[0] == "Literal" or (type_[0] == "Union" and _isPatternFinite(type_[1])) for type_ in types
    )


def _patternVariants(variants: list[str], type_: _PatternType) -> list[str]:
    """``DecodeFromSpan``'s step: every variant so far, followed by each text ``type_`` stands for."""
    if type_[0] == "Union":
        return [variant for member in type_[1] for variant in _patternVariants(variants, member)]
    return [type_[1]] if len(variants) == 0 else [f"{variant}{type_[1]}" for variant in variants]


def _recordKey(pattern: str) -> TSchema:
    """``RecordPatternToType``: the key type a record's pattern stands for. A finite pattern (``^abc$``, ``^(a|b)$``,
    ``^a+$`` read as the text ``a+``) is decoded to its literals (``TemplateLiteralDecodeUnsafe``); a pattern that does
    not parse is ``string``; any other one stays a pattern (a template literal, built back into the same record)."""
    if pattern == _STRING_KEY:
        return Type.String()
    if pattern == _INTEGER_KEY:
        return Type.Integer()
    if pattern == _NUMBER_KEY:
        return Type.Number()
    types = _parsePatternIntoTypes(pattern)
    if len(types) == 0:
        return Type.String()
    if not _isPatternFinite(types):
        return Type.String({"pattern": pattern})
    if len(types) == 1 and types[0][0] == "Literal":
        return Type.Literal(types[0][1])
    variants: list[str] = []
    for type_ in types:
        variants = _patternVariants(variants, type_)
    return Type.Union([Type.Literal(variant) for variant in variants])


def _modifiers(schema: TSchema, sorted_: TSchema) -> TSchema:
    """``Modifiers``: the sorted form keeps every key of the schema it does not set itself."""
    for key, value in schema.items():
        if key not in sorted_:
            sorted_[key] = value
    return sorted_


def _unionPrioritySort(schema: Any) -> Any:
    """``UnionPrioritySort``: every union, however deep, in ``Priority`` order (a copy; ``Clean`` reads it)."""
    kind = _kind(schema)
    if kind == "Array":
        return {**schema, "items": _unionPrioritySort(schema["items"])}
    if kind == "Intersect":
        return {**schema, "allOf": [_unionPrioritySort(member) for member in schema["allOf"]]}
    if kind == "Union":
        return {**schema, "anyOf": [_unionPrioritySort(member) for member in _priority(schema["anyOf"])]}
    if kind == "Object":
        return {**schema, "properties": {key: _unionPrioritySort(value) for key, value in schema["properties"].items()}}
    if kind == "Record":
        # `Record(RecordKey(type), FromType(RecordValue(type)))`: a finite key pattern becomes an object of its keys.
        key = _recordKey(_recordPattern(schema))
        return _modifiers(schema, _recordFromKey(key, _unionPrioritySort(_recordValue(schema))))
    return schema


def _evaluateType(type_: Any) -> Any:
    kind = _kind(type_)
    if kind == "Enum":
        return _evaluateEnum(type_["enum"])
    if kind == "Intersect":
        return _evaluateIntersect(type_["allOf"])
    if kind == "Union":
        return _evaluateUnion(type_["anyOf"])
    return type_


def _evaluateEnum(values: Sequence[Any]) -> Any:
    return _evaluateUnion([Type.Literal(value) for value in values])


def _evaluateUnion(types: Sequence[Any]) -> Any:
    broadened = _broaden(types)
    if len(broadened) == 1:
        return broadened[0]
    if len(broadened) == 0:
        return _never()
    return Type.Union(broadened)


def _evaluateIntersect(types: Sequence[Any]) -> Any:
    return _evaluateUnion(_broaden(_distribute(types)))


def _flatten(types: Sequence[Any]) -> list[Any]:
    result: list[Any] = []
    for type_ in types:
        result += _flatten(type_["anyOf"]) if _kind(type_) == "Union" else [type_]
    return result


def _broaden(types: Sequence[Any]) -> list[Any]:
    """``Broaden``: the broadest type, or the union of disjoint types; ``never`` is dropped, objects are kept."""
    result: list[Any] = []
    for type_ in types:
        evaluated = _evaluateType(type_)
        kind = _kind(evaluated)
        if kind == "Unknown":
            return _flatten([evaluated])
        if kind == "Never":
            continue
        result = [*result, evaluated] if kind == "Object" else _broadenFilter(evaluated, result)
    return _flatten(result)


def _broadenFilter(type_: Any, types: list[Any]) -> list[Any]:
    kept: list[Any] = []
    for other in types:
        relation = _compare(type_, other)
        if relation in (_LEFT_INSIDE, _EQUAL):
            return types
        if relation == _DISJOINT:
            kept.append(other)
    return [*kept, type_]


def _distribute(types: Sequence[Any], result: list[Any] | None = None) -> list[Any]:
    distribution = [] if result is None else result
    for type_ in types:
        if _kind(type_) == "Union":
            unioned: list[Any] = []
            for member in type_["anyOf"]:
                unioned += _distribute([member], distribution)
            distribution = unioned
        else:
            distribution = [_distributeOperation(other, type_) for other in distribution] or [type_]
    return distribution


def _distributeOperation(left: Any, right: Any) -> Any:
    evaluatedLeft = _evaluateType(left)
    evaluatedRight = _evaluateType(right)
    if _kind(evaluatedLeft) == "Union" or _kind(evaluatedRight) == "Union":
        return _evaluateIntersect([evaluatedLeft, evaluatedRight])
    return _narrow(evaluatedLeft, evaluatedRight)


def _narrow(left: Any, right: Any) -> Any:
    leftKind = _kind(left)
    rightKind = _kind(right)
    if leftKind == "Never":
        return left
    if leftKind == "Unknown":
        return right
    if rightKind == "Never":
        return right
    if rightKind == "Unknown":
        return left
    if leftKind == "Object" and rightKind == "Object":
        return _composite(left, right)
    if leftKind == "Object":
        return left
    if rightKind == "Object":
        return right
    relation = _compare(left, right)
    if relation == _LEFT_INSIDE:
        return left
    if relation in (_RIGHT_INSIDE, _EQUAL):
        return right
    return _never()


def _composite(left: TSchema, right: TSchema) -> TSchema:
    """``Composite``: one object of both objects' properties; a shared property is the intersection of both, and
    optional only when optional in both."""
    leftProperties = _properties(left)
    rightProperties = _properties(right)
    properties: dict[str, Any] = {}
    for key in dict.fromkeys([*leftProperties, *rightProperties]):
        if key in leftProperties and key in rightProperties:
            optional = leftProperties[key][1] and rightProperties[key][1]
            evaluated = _evaluateIntersect([leftProperties[key][0], rightProperties[key][0]])
        else:
            evaluated, optional = leftProperties[key] if key in leftProperties else rightProperties[key]
        properties[key] = _Optional(evaluated) if optional else evaluated
    return Type.Object(properties)


# ─── values ──────────────────────────────────────────────────────────────────


def _isNumber(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _isFiniteNumber(value: Any) -> bool:
    """``Guard.IsNumber``: ``Number.isFinite(value)``."""
    return _isNumber(value) and (isinstance(value, int) or math.isfinite(value))


def _isInteger(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value) and value.is_integer()


def _typeOk(kind: Any, value: Any) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return _isInteger(value)
    if kind == "number":
        return _isFiniteNumber(value)
    if kind == "null":
        return value is None
    if kind == "string":
        return isinstance(value, str)
    if kind in ("bigint", "symbol"):
        return False
    if kind == "constructor":
        return isinstance(value, type)
    if kind == "function":
        return callable(value)
    if kind in ("void", "undefined"):
        return value is UNDEFINED
    # An unknown type name passes (`CheckTypeName`).
    return True


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if _isNumber(a) and _isNumber(b):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[key], b[key]) for key in a)
    return type(a) is type(b) and a == b


# ─── JavaScript regular expression syntax ────────────────────────────────────

# `\p{...}` (with `u`): ECMAScript's property names and values, exactly as `new RegExp(source, "u")` takes them (case and
# underscores matter). General categories and binary properties may stand alone; `Script`/`Script_Extensions` values
# follow `sc=`, `scx=` (or the long names). The tables are those Node 26 accepts.


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


_GENERAL_CATEGORIES: Final = _words(
    "Cased_Letter LC Close_Punctuation Pe Connector_Punctuation Pc Control Cc cntrl Currency_Symbol Sc "
    "Dash_Punctuation Pd Decimal_Number Nd digit Enclosing_Mark Me Final_Punctuation Pf Format Cf "
    "Initial_Punctuation Pi Letter L Letter_Number Nl Line_Separator Zl Lowercase_Letter Ll Mark M Combining_Mark "
    "Math_Symbol Sm Modifier_Letter Lm Modifier_Symbol Sk Nonspacing_Mark Mn Number N Open_Punctuation Ps Other C "
    "Other_Letter Lo Other_Number No Other_Punctuation Po Other_Symbol So Paragraph_Separator Zp Private_Use Co "
    "Punctuation P punct Separator Z Space_Separator Zs Spacing_Mark Mc Surrogate Cs Symbol S Titlecase_Letter Lt "
    "Unassigned Cn Uppercase_Letter Lu "
)
_BINARY_PROPERTIES: Final = _words(
    "ASCII ASCII_Hex_Digit AHex Alphabetic Alpha Any Assigned Bidi_Control Bidi_C Bidi_Mirrored Bidi_M "
    "Case_Ignorable CI Cased Changes_When_Casefolded CWCF Changes_When_Casemapped CWCM Changes_When_Lowercased CWL "
    "Changes_When_NFKC_Casefolded CWKCF Changes_When_Titlecased CWT Changes_When_Uppercased CWU Dash "
    "Default_Ignorable_Code_Point DI Deprecated Dep Diacritic Dia Emoji Emoji_Component EComp Emoji_Modifier EMod "
    "Emoji_Modifier_Base EBase Emoji_Presentation EPres Extended_Pictographic ExtPict Extender Ext Grapheme_Base "
    "Gr_Base Grapheme_Extend Gr_Ext Hex_Digit Hex IDS_Binary_Operator IDSB IDS_Trinary_Operator IDST ID_Continue "
    "IDC ID_Start IDS Ideographic Ideo Join_Control Join_C Logical_Order_Exception LOE Lowercase Lower Math "
    "Noncharacter_Code_Point NChar Pattern_Syntax Pat_Syn Pattern_White_Space Pat_WS Quotation_Mark QMark Radical "
    "Regional_Indicator RI Sentence_Terminal STerm Soft_Dotted SD Terminal_Punctuation Term Unified_Ideograph UIdeo "
    "Uppercase Upper Variation_Selector VS White_Space space XID_Continue XIDC XID_Start XIDS "
)
_SCRIPTS: Final = _words(
    "Adlam Adlm Aghb Ahom Anatolian_Hieroglyphs Arab Arabic Armenian Armi Armn Avestan Avst Bali Balinese Bamu "
    "Bamum Bass Bassa_Vah Batak Batk Beng Bengali Berf Beria_Erfe Bhaiksuki Bhks Bopo Bopomofo Brah Brahmi Brai "
    "Braille Bugi Buginese Buhd Buhid Cakm Canadian_Aboriginal Cans Cari Carian Caucasian_Albanian Chakma Cham Cher "
    "Cherokee Chorasmian Chrs Common Copt Coptic Cpmn Cprt Cuneiform Cypriot Cypro_Minoan Cyrillic Cyrl Deseret "
    "Deva Devanagari Diak Dives_Akuru Dogr Dogra Dsrt Dupl Duployan Egyp Egyptian_Hieroglyphs Elba Elbasan Elym "
    "Elymaic Ethi Ethiopic Gara Garay Geor Georgian Glag Glagolitic Gong Gonm Goth Gothic Gran Grantha Greek Grek "
    "Gujarati Gujr Gukh Gunjala_Gondi Gurmukhi Guru Gurung_Khema Han Hang Hangul Hani Hanifi_Rohingya Hano Hanunoo "
    "Hatr Hatran Hebr Hebrew Hira Hiragana Hluw Hmng Hmnp Hung Imperial_Aramaic Inherited Inscriptional_Pahlavi "
    "Inscriptional_Parthian Ital Java Javanese Kaithi Kali Kana Kannada Katakana Kawi Kayah_Li Khar Kharoshthi "
    "Khitan_Small_Script Khmer Khmr Khoj Khojki Khudawadi Kirat_Rai Kits Knda Krai Kthi Lana Lao Laoo Latin Latn "
    "Lepc Lepcha Limb Limbu Lina Linb Linear_A Linear_B Lisu Lyci Lycian Lydi Lydian Mahajani Mahj Maka Makasar "
    "Malayalam Mand Mandaic Mani Manichaean Marc Marchen Masaram_Gondi Medefaidrin Medf Meetei_Mayek Mend "
    "Mende_Kikakui Merc Mero Meroitic_Cursive Meroitic_Hieroglyphs Miao Mlym Modi Mong Mongolian Mro Mroo Mtei Mult "
    "Multani Myanmar Mymr Nabataean Nag_Mundari Nagm Nand Nandinagari Narb Nbat New_Tai_Lue Newa Nko Nkoo Nshu "
    "Nushu Nyiakeng_Puachue_Hmong Ogam Ogham Ol_Chiki Ol_Onal Olck Old_Hungarian Old_Italic Old_North_Arabian "
    "Old_Permic Old_Persian Old_Sogdian Old_South_Arabian Old_Turkic Old_Uyghur Onao Oriya Orkh Orya Osage Osge "
    "Osma Osmanya Ougr Pahawh_Hmong Palm Palmyrene Pau_Cin_Hau Pauc Perm Phag Phags_Pa Phli Phlp Phnx Phoenician "
    "Plrd Prti Psalter_Pahlavi Qaac Qaai Rejang Rjng Rohg Runic Runr Samaritan Samr Sarb Saur Saurashtra Sgnw "
    "Sharada Shavian Shaw Shrd Sidd Siddham Sidetic Sidt SignWriting Sind Sinh Sinhala Sogd Sogdian Sogo Sora "
    "Sora_Sompeng Soyo Soyombo Sund Sundanese Sunu Sunuwar Sylo Syloti_Nagri Syrc Syriac Tagalog Tagb Tagbanwa "
    "Tai_Le Tai_Tham Tai_Viet Tai_Yo Takr Takri Tale Talu Tamil Taml Tang Tangsa Tangut Tavt Tayo Telu Telugu Tfng "
    "Tglg Thaa Thaana Thai Tibetan Tibt Tifinagh Tirh Tirhuta Tnsa Todhri Todr Tolong_Siki Tols Toto Tulu_Tigalari "
    "Tutg Ugar Ugaritic Unknown Vai Vaii Vith Vithkuqi Wancho Wara Warang_Citi Wcho Xpeo Xsux Yezi Yezidi Yi Yiii "
    "Zanabazar_Square Zanb Zinh Zyyy Zzzz "
)
_SYNTAX_CHARACTERS: Final = frozenset("^$\\.*+?()[]{}|")
_CONTROL_ESCAPES: Final = {"f": 0x0C, "n": 0x0A, "r": 0x0D, "t": 0x09, "v": 0x0B}
_ID_START: Final = regex.compile(r"[\p{ID_Start}$_]")
_ID_CONTINUE: Final = regex.compile(r"[\p{ID_Continue}$‌‍]")
_HEX: Final = frozenset("0123456789abcdefABCDEF")
_DIGITS: Final = frozenset("0123456789")


class _PatternSyntax:
    """The early errors of ``new RegExp(source, flags)``: ECMAScript 2025's ``Pattern`` grammar (named groups,
    duplicate names in different alternatives, modifiers ``(?ims-ims:...)``), with ``u`` its Unicode mode and
    without it Annex B's web grammar. ``misaka.utils.js_regexp`` translates a source to the ``regex`` module without
    checking it, and both accept syntax JavaScript refuses (``(?i)a``, ``a{,2}`` or ``\\a`` under ``u``); TypeBox's
    ``new RegExp`` throws a ``SyntaxError`` for it, so the source is checked first. Raises ``SyntaxError`` with V8's
    message."""

    def __init__(self, source: str, unicode: bool) -> None:
        self.source = source
        self.flags = "u" if unicode else ""
        self.unicode = unicode
        # Characters as JavaScript reads them: code points with `u`, UTF-16 code units without.
        self.text = fromUtf16Units(toUtf16Units(source)) if unicode else toUtf16Units(source)
        self.index = 0
        self.groups = 0
        self.names: set[str] = set()
        self.references: list[int] = []
        self.namedReferences: list[str] = []
        # Without `u`, `\k` is a named reference only in a pattern that has a named group (the grammar's `N`).
        self.named = unicode or self._hasNamedGroup()

    def fail(self, reason: str) -> None:
        raise SyntaxError(f"Invalid regular expression: /{self.source}/{self.flags}: {reason}")

    def peek(self, offset: int = 0) -> str:
        index = self.index + offset
        return self.text[index] if index < len(self.text) else ""

    def startsWith(self, prefix: str) -> bool:
        return self.text.startswith(prefix, self.index)

    def check(self) -> None:
        self.disjunction(set())
        if self.index < len(self.text):
            self.fail("Unmatched ')'")
        if any(number > self.groups for number in self.references):
            self.fail("Invalid escape")
        if any(name not in self.names for name in self.namedReferences):
            self.fail("Invalid named capture referenced")

    def _hasNamedGroup(self) -> bool:
        text, index, inClass = self.text, 0, False
        while index < len(text):
            char = text[index]
            if char == "\\":
                index += 2
                continue
            if inClass:
                inClass = char != "]"
            elif char == "[":
                inClass = True
            elif text.startswith("(?<", index) and text[index + 3 : index + 4] not in ("=", "!"):
                return True
            index += 1
        return False

    # ─── disjunctions, terms, quantifiers ───

    def disjunction(self, visible: set[str]) -> set[str]:
        """Parses ``Alternative ('|' Alternative)*``; returns the group names it defines. ``visible`` holds the names
        a new name might participate with (a duplicate is allowed only in another alternative)."""
        defined: set[str] = set()
        while True:
            defined |= self.alternative(set(visible))
            if self.peek() != "|":
                return defined
            self.index += 1

    def alternative(self, visible: set[str]) -> set[str]:
        defined: set[str] = set()
        while self.index < len(self.text) and self.peek() not in "|)":
            self.term(visible, defined)
        return defined

    def term(self, visible: set[str], defined: set[str]) -> None:
        char = self.peek()
        if char in ("^", "$") or (char == "\\" and self.peek(1) in ("b", "B") and self.peek(1) != ""):
            self.index += 1 if char != "\\" else 2
            self.noQuantifier("Nothing to repeat")
            return
        lookbehind = self.startsWith("(?<=") or self.startsWith("(?<!")
        if lookbehind or self.startsWith("(?=") or self.startsWith("(?!"):
            self.index += 4 if lookbehind else 3
            names = self.disjunction(visible)
            visible |= names
            defined |= names
            self.close()
            if lookbehind or self.unicode:
                self.noQuantifier("Invalid quantifier")
            else:
                self.quantifier()  # Annex B: a lookahead is quantifiable
            return
        if char in ("*", "+", "?"):
            self.fail("Nothing to repeat")
        if char == "{":
            if self.braced(self.index) is not None:
                self.fail("Nothing to repeat")
            if self.unicode:
                self.fail("Lone quantifier brackets")
            self.index += 1
        elif char in ("}", "]"):
            if self.unicode:
                self.fail("Lone quantifier brackets")
            self.index += 1
        elif char == ".":
            self.index += 1
        elif char == "\\":
            self.atomEscape()
        elif char == "[":
            self.characterClass()
        elif char == "(":
            self.group(visible, defined)
        else:
            self.index += 1
        self.quantifier()

    def braced(self, index: int) -> tuple[int, int, int | None] | None:
        """A ``{n}``, ``{n,}`` or ``{n,m}`` at ``index``: (its end, n, m), or None."""
        text = self.text
        end = index + 1
        while end < len(text) and text[end] in _DIGITS:
            end += 1
        if end == index + 1:
            return None
        low = int(text[index + 1 : end])
        high: int | None = low
        if end < len(text) and text[end] == ",":
            start = end = end + 1
            while end < len(text) and text[end] in _DIGITS:
                end += 1
            high = int(text[start:end]) if end > start else None
        if end >= len(text) or text[end] != "}":
            return None
        return end + 1, low, high

    def quantifier(self) -> None:
        char = self.peek()
        if char in ("*", "+", "?") and char != "":
            self.index += 1
        elif char == "{":
            braced = self.braced(self.index)
            if braced is None:
                if self.unicode:
                    self.fail("Incomplete quantifier")
                return  # Annex B: a `{` that starts no quantifier is a character
            end, low, high = braced
            if high is not None and low > high:
                self.fail("numbers out of order in {} quantifier")
            self.index = end
        else:
            return
        if self.peek() == "?":
            self.index += 1
        self.noQuantifier("Nothing to repeat")

    def noQuantifier(self, reason: str) -> None:
        char = self.peek()
        if (char in ("*", "+", "?") and char != "") or (char == "{" and self.braced(self.index) is not None):
            self.fail(reason)

    def close(self) -> None:
        if self.peek() != ")":
            self.fail("Unterminated group")
        self.index += 1

    # ─── groups ───

    def group(self, visible: set[str], defined: set[str]) -> None:
        if not self.startsWith("(?"):
            self.index += 1
            self.groups += 1
        elif self.startsWith("(?:"):
            self.index += 3
        elif self.startsWith("(?<"):
            self.index += 2
            name = self.groupName("Invalid capture group name")
            if name in visible:
                self.fail("Duplicate capture group name")
            self.groups += 1
            self.names.add(name)
            visible.add(name)
            defined.add(name)
        else:
            self.index += 2
            self.modifiers()
        names = self.disjunction(visible)
        visible |= names
        defined |= names
        self.close()

    def modifiers(self) -> None:
        """``(?ims-ims:``: at most one of each flag, not both lists empty."""
        adding = self.flagList()
        removing = None
        if self.peek() == "-":
            self.index += 1
            removing = self.flagList()
        if self.peek() != ":":
            self.fail("Invalid group")
        flags = adding + (removing or "")
        if len(set(flags)) != len(flags):
            self.fail("Repeated flag in flag group")
        if removing is not None and flags == "":
            self.fail("Invalid flag group")
        self.index += 1

    def flagList(self) -> str:
        start = self.index
        while self.peek() in ("i", "m", "s") and self.peek() != "":
            self.index += 1
        return self.text[start : self.index]

    def groupName(self, reason: str) -> str:
        """``<RegExpIdentifierName>`` (``\\u`` escapes as with ``u``, in either mode)."""
        if self.peek() != "<":
            self.fail(reason)
        self.index += 1
        name: list[str] = []
        while True:
            char = self.peek()
            if char == "" or (char == ">" and len(name) == 0):
                self.fail(reason)
            if char == ">":
                self.index += 1
                return "".join(name)
            if char == "\\":
                if self.peek(1) != "u":
                    self.fail(reason)
                self.index += 1
                point = self.unicodeEscape(True)
                if point is None:
                    self.fail(reason)
                char = chr(point)  # type: ignore[arg-type]
            else:
                self.index += 1
                # A surrogate pair written as two code units is one identifier character.
                if 0xD800 <= ord(char) <= 0xDBFF and 0xDC00 <= ord(self.peek() or "\0") <= 0xDFFF:
                    char = fromUtf16Units(char + self.peek())
                    self.index += 1
            if not (_ID_CONTINUE if name else _ID_START).fullmatch(char):
                self.fail(reason)
            name.append(char)

    # ─── escapes ───

    def atomEscape(self) -> None:
        self.index += 1
        char = self.peek()
        if char == "":
            self.fail("\\ at end of pattern")
        if char in "123456789":
            start = self.index
            while self.peek() in _DIGITS and self.peek() != "":
                self.index += 1
            # With `u` a backreference must name a group; without it, a number past the groups reads as an octal or
            # identity escape, which is always valid.
            if self.unicode:
                self.references.append(int(self.text[start : self.index]))
            return
        if char == "0" and self.peek(1) in _DIGITS and self.peek(1) != "":
            if self.unicode:
                self.fail("Invalid decimal escape")
            self.legacyOctal()
            return
        if char in ("d", "D", "s", "S", "w", "W"):
            self.index += 1
            return
        if char in ("p", "P") and self.unicode:
            self.property("Invalid property name")
            return
        if char == "k" and self.named:
            self.index += 1
            self.namedReferences.append(self.groupName("Invalid named reference"))
            return
        self.characterEscape(False)

    def characterEscape(self, inClass: bool) -> int:
        """A ``CharacterEscape`` after ``\\`` (at the character after it); its code point."""
        char = self.peek()
        if char in _CONTROL_ESCAPES:
            self.index += 1
            return _CONTROL_ESCAPES[char]
        if char == "c":
            letter = self.peek(1)
            if letter != "" and letter.isascii() and letter.isalpha():
                self.index += 2
                return ord(letter) % 32
            if not self.unicode:
                if inClass and letter != "" and (letter in _DIGITS or letter == "_"):
                    self.index += 2
                    return ord(letter) % 32
                return 0x5C  # Annex B: the backslash stands for itself; `c` is read next
            self.fail("Invalid Unicode escape")
        if char == "0":
            self.index += 1
            return 0
        if char in _DIGITS:
            if self.unicode:
                self.fail("Invalid decimal escape" if inClass else "Invalid escape")
            return self.legacyOctal()
        if char == "x":
            digits = self.text[self.index + 1 : self.index + 3]
            if len(digits) == 2 and all(digit in _HEX for digit in digits):
                self.index += 3
                return int(digits, 16)
            if self.unicode:
                self.fail("Invalid escape")
            self.index += 1
            return ord("x")
        if char == "u":
            point = self.unicodeEscape(self.unicode)
            if point is None:
                if self.unicode:
                    self.fail("Invalid Unicode escape")
                self.index += 1
                return ord("u")
            return point
        if self.unicode:
            if char in _SYNTAX_CHARACTERS or char == "/" or (inClass and char == "-"):
                self.index += 1
                return ord(char)
            self.fail("Invalid escape")
        if char == "k" and self.named:
            self.fail("Invalid escape")
        self.index += 1
        return ord(char)

    def unicodeEscape(self, unicode: bool) -> int | None:
        """``u`` ``HHHH`` (a surrogate pair of two with ``unicode``) or ``u{H...}`` (only with it), at the ``u``; the
        code point, or None (nothing consumed) when there is none."""
        text, start = self.text, self.index + 1
        if unicode and text.startswith("{", start):
            end = start + 1
            while end < len(text) and text[end] in _HEX:
                end += 1
            if end == start + 1 or end >= len(text) or text[end] != "}" or int(text[start + 1 : end], 16) > 0x10FFFF:
                return None
            self.index = end + 1
            return int(text[start + 1 : end], 16)
        digits = text[start : start + 4]
        if len(digits) != 4 or not all(digit in _HEX for digit in digits):
            return None
        point = int(digits, 16)
        self.index = start + 4
        trail = text[self.index + 2 : self.index + 6]
        if (
            unicode
            and 0xD800 <= point <= 0xDBFF
            and text.startswith("\\u", self.index)
            and len(trail) == 4
            and all(digit in _HEX for digit in trail)
            and 0xDC00 <= int(trail, 16) <= 0xDFFF
        ):
            self.index += 6
            return 0x10000 + ((point - 0xD800) << 10) + (int(trail, 16) - 0xDC00)
        return point

    def legacyOctal(self) -> int:
        """Annex B's ``LegacyOctalEscapeSequence`` (or ``\\8``/``\\9`` as themselves), at its first digit."""
        text, start = self.text, self.index
        if text[start] in "89":
            self.index += 1
            return ord(text[start])
        end = start + 1
        limit = start + (3 if text[start] in "0123" else 2)
        while end < min(limit, len(text)) and text[end] in "01234567":
            end += 1
        self.index = end
        return int(text[start:end], 8)

    def property(self, reason: str) -> None:
        """``\\p{...}``/``\\P{...}`` (with ``u``), at the ``p``."""
        self.index += 1
        if self.peek() != "{":
            self.fail(reason)
        end = self.text.find("}", self.index)
        if end < 0:
            self.fail(reason)
        body = self.text[self.index + 1 : end]
        self.index = end + 1
        name, equals, value = body.partition("=")
        if equals:
            known = (name in ("General_Category", "gc") and value in _GENERAL_CATEGORIES) or (
                name in ("Script", "sc", "Script_Extensions", "scx") and value in _SCRIPTS
            )
        else:
            known = body in _GENERAL_CATEGORIES or body in _BINARY_PROPERTIES
        if not known:
            self.fail(reason)

    # ─── character classes ───

    def characterClass(self) -> None:
        self.index += 1
        if self.peek() == "^":
            self.index += 1
        while True:
            if self.index >= len(self.text):
                self.fail("Unterminated character class")
            if self.peek() == "]":
                self.index += 1
                return
            low = self.classAtom()
            if self.peek() == "-" and self.peek(1) not in ("", "]"):
                self.index += 1
                high = self.classAtom()
                if low is None or high is None:
                    if self.unicode:
                        self.fail("Invalid character class")
                    continue  # Annex B: a class escape beside `-` is a union, not a range
                if low > high:
                    self.fail("Range out of order in character class")

    def classAtom(self) -> int | None:
        """A ``ClassAtom``: its code point, or None for a class escape (a set)."""
        char = self.peek()
        if char != "\\":
            self.index += 1
            return ord(char)
        self.index += 1
        char = self.peek()
        if char == "":
            self.fail("Unterminated character class")
        if char == "b":
            self.index += 1
            return 0x08
        if char == "-" and self.unicode:
            self.index += 1
            return 0x2D
        if char in ("d", "D", "s", "S", "w", "W"):
            self.index += 1
            return None
        if char in ("p", "P") and self.unicode:
            self.property("Invalid property name in character class")
            return None
        if char == "0" and self.peek(1) in _DIGITS and self.peek(1) != "":
            if self.unicode:
                self.fail("Invalid class escape")
            return self.legacyOctal()
        return self.characterEscape(True)


def _checkPattern(source: str, unicode: bool) -> None:
    """Throws the ``SyntaxError`` ``new RegExp(source, unicode ? "u" : "")`` throws, if any."""
    _PatternSyntax(source, unicode).check()


@lru_cache(maxsize=1024)
def _regexp(pattern: str, flags: str) -> RegExp:
    """``new RegExp(pattern, flags)``: a ``SyntaxError`` for a source JavaScript refuses."""
    _checkPattern(pattern, "u" in flags)
    return RegExp(pattern, flags)


def UnicodeRegExp(pattern: str) -> RegExp:
    """``UnicodeRegExp`` (``new RegExp(pattern, "u")``): how TypeBox reads ``pattern`` and ``patternProperties``."""
    return _regexp(pattern, "u")


@lru_cache(maxsize=512)
def _patternsRegExp(patterns: tuple[str, ...]) -> RegExp:
    return UnicodeRegExp(f"({'|'.join(patterns)})")


def _isKnownName(schema: TSchema, key: str) -> bool:
    """Whether ``GetPropertiesPattern``'s expression matches ``key``: TypeBox joins the ``patternProperties``
    patterns and every property name (escaped, anchored: ``^name$``) into one ``(...|...)``. An anchored name matches
    only itself, so it is tested as a name; the patterns stay one expression (a pattern's group numbers count from the
    enclosing group, as in TypeBox)."""
    properties = schema.get("properties")
    if isinstance(properties, dict) and key in properties:
        return True
    patterns = schema.get("patternProperties")
    return isinstance(patterns, dict) and len(patterns) > 0 and _patternsRegExp(tuple(patterns)).test(key)


def _jsRemainder(dividend: float, divisor: float) -> float:
    """JavaScript's ``%`` on numbers."""
    if math.isnan(dividend) or math.isnan(divisor) or math.isinf(dividend) or divisor == 0:
        return math.nan
    if math.isinf(divisor):
        return dividend
    return math.fmod(dividend, divisor)


def _isMultipleOf(dividend: Any, divisor: Any) -> bool:
    """``Guard.IsMultipleOf``: within 1e-10 of a multiple (so ``0.3`` is a multiple of ``0.1``)."""
    tolerance = 1e-10
    if not _isFiniteNumber(dividend):
        return True
    dividend = float(jsNumber(dividend))
    divisor = float(jsNumber(divisor))
    inverse = math.copysign(math.inf, divisor) if divisor == 0 else 1 / divisor
    if dividend.is_integer() and _jsRemainder(inverse, 1) == 0:
        return True
    mod = _jsRemainder(dividend, divisor)
    distances = [abs(mod), abs(mod - divisor), abs(mod + divisor)]
    return not any(math.isnan(distance) for distance in distances) and min(distances) < tolerance


# ─── grapheme clusters (`guard/string.mjs`) ─────────────────────────────────


def _isCombiningMark(value: int) -> bool:
    return 0x0300 <= value <= 0x036F or 0x1AB0 <= value <= 0x1AFF or 0x1DC0 <= value <= 0x1DFF or 0xFE20 <= value <= 0xFE2F


def _isVariationSelector(value: int) -> bool:
    return 0xFE00 <= value <= 0xFE0F


def _isRegionalIndicator(value: int) -> bool:
    return 0x1F1E6 <= value <= 0x1F1FF


def _codePointLength(value: int) -> int:
    return 2 if value > 0xFFFF else 1


def _charCodeAt(units: str, index: int) -> int:
    """``charCodeAt``; -1 stands for NaN (past the end)."""
    return ord(units[index]) if 0 <= index < len(units) else -1


def _codePointAt(units: str, index: int) -> int:
    first = _charCodeAt(units, index)
    if 0xD800 <= first <= 0xDBFF:
        second = _charCodeAt(units, index + 1)
        if 0xDC00 <= second <= 0xDFFF:
            return ((first - 0xD800) << 10) + (second - 0xDC00) + 0x10000
    return first


def _consumeModifiers(units: str, index: int) -> int:
    while index < len(units):
        point = _codePointAt(units, index)
        if not (_isCombiningMark(point) or _isVariationSelector(point)):
            break
        index += _codePointLength(point)
    return index


def _nextGraphemeClusterIndex(units: str, clusterStart: int) -> int:
    startCP = _codePointAt(units, clusterStart)
    clusterEnd = _consumeModifiers(units, clusterStart + _codePointLength(startCP))
    # Multi-ZWJ sequences.
    while clusterEnd < len(units) - 1 and _codePointAt(units, clusterEnd) == 0x200D:
        nextCP = _codePointAt(units, clusterEnd + 1)
        clusterEnd = _consumeModifiers(units, clusterEnd + 1 + _codePointLength(nextCP))
    # Regional indicator pairs (flags).
    if _isRegionalIndicator(startCP) and clusterEnd < len(units) and _isRegionalIndicator(_codePointAt(units, clusterEnd)):
        clusterEnd += _codePointLength(_codePointAt(units, clusterEnd))
    return clusterEnd


def _isGraphemeCodePoint(value: int) -> bool:
    return value >= 0x0300 and (0xD800 <= value <= 0xDBFF or _isCombiningMark(value) or _isVariationSelector(value) or value == 0x200D)


def _isMinLength(value: str, minLength: Any) -> bool:
    """``IsMinLength``: at least ``minLength`` grapheme clusters (fast path while no unit can start or extend one)."""
    if minLength == 0:
        return True
    units = toUtf16Units(value)
    if len(units) < minLength:
        return False
    index = 0
    while True:
        if _isGraphemeCodePoint(_charCodeAt(units, index)):
            count = 0
            index = 0
            while index < len(units):
                index = _nextGraphemeClusterIndex(units, index)
                count += 1
                if count >= minLength:
                    return True
            return False
        index += 1
        if index >= minLength:
            return True


def _isMaxLength(value: str, maxLength: Any) -> bool:
    """``IsMaxLength``: at most ``maxLength`` grapheme clusters."""
    units = toUtf16Units(value)
    if len(units) <= maxLength:
        return True
    index = 0
    while True:
        if _isGraphemeCodePoint(_charCodeAt(units, index)):
            count = 0
            index = 0
            while index < len(units):
                index = _nextGraphemeClusterIndex(units, index)
                count += 1
                if count > maxLength:
                    return False
            return True
        index += 1
        if index > maxLength:
            return False


# ─── Check / Errors ──────────────────────────────────────────────────────────


def _error(keyword: str, path: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"keyword": keyword, "instancePath": path, "message": message, **extra}


def _errors(schema: Any, value: Any, path: str) -> Iterator[dict[str, Any]]:
    """TypeBox's errors for ``value`` under ``schema``, in its keyword order (``ErrorSchema``: type; the object, array,
    string and number keywords; const, enum, not, allOf, anyOf, oneOf), wording (``en_US``) and instance paths (raw
    property names, ``/a/b`` for the property ``a/b``)."""
    if schema is True or schema is None:
        return
    if schema is False:
        yield _error("boolean", path, "schema is false")
        return
    if not isinstance(schema, dict):
        return
    kind = schema.get("type")
    if "type" in schema:
        kinds = kind if isinstance(kind, list) else [kind]
        if not any(_typeOk(item, value) for item in kinds):
            names = f"either {' or '.join(jsString(item) for item in kind)}" if isinstance(kind, list) else jsString(kind)
            yield _error("type", path, f"must be {names}")
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        missing = [key for key in required if key not in value]
        if missing:
            yield _error("required", path, f"must have required properties {', '.join(missing)}", params={"requiredProperties": missing})
        patterns = schema.get("patternProperties") if isinstance(schema.get("patternProperties"), dict) else {}
        additional = schema.get("additionalProperties", True)
        if additional is not True and (isinstance(additional, dict) or additional is False):
            extra = False
            for key, item in value.items():
                if _isKnownName(schema, key):
                    continue
                failures = list(_errors(additional, item, f"{path}/{key}"))
                if failures:
                    extra = True
                    yield from failures
            if extra:
                yield _error("additionalProperties", path, "must not have additional properties")
        for pattern, property in patterns.items():
            regexp = UnicodeRegExp(pattern)
            for key, item in value.items():
                if regexp.test(key):
                    yield from _errors(property, item, f"{path}/{key}")
        for key, property in properties.items():
            if key not in value:
                continue
            item = value[key]
            # An optional property may hold `undefined` (TypeBox's default, without exactOptionalPropertyTypes).
            if item is UNDEFINED and key not in required:
                continue
            yield from _errors(property, item, f"{path}/{key}")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            yield _error("minProperties", path, f"must not have fewer than {jsString(schema['minProperties'])} properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            yield _error("maxProperties", path, f"must not have more than {jsString(schema['maxProperties'])} properties")
    if isinstance(value, list):
        prefix = schema.get("prefixItems")
        items = schema.get("items")
        if isinstance(items, list):
            prefix = items
            items = schema.get("additionalItems")
        if "contains" in schema:
            matched = sum(1 for item in value if _check(schema["contains"], item))
            most = schema.get("maxContains")
            if matched < schema.get("minContains", 1) or (most is not None and matched > most):
                yield _error("contains", path, "must contain at least 1 valid item")
        start = len(prefix) if isinstance(prefix, list) else 0
        if items is not None and not isinstance(items, list):
            for index in range(start, len(value)):
                yield from _errors(items, value[index], f"{path}/{index}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            yield _error("maxItems", path, f"must not have more than {jsString(schema['maxItems'])} items")
        if "minItems" in schema and len(value) < schema["minItems"]:
            yield _error("minItems", path, f"must not have fewer than {jsString(schema['minItems'])} items")
        if isinstance(prefix, list):
            for index, item in enumerate(value[: len(prefix)]):
                yield from _errors(prefix[index], item, f"{path}/{index}")
        if schema.get("uniqueItems") is True and any(_equal(value[i], value[j]) for i in range(len(value)) for j in range(i)):
            yield _error("uniqueItems", path, "must not have duplicate items")
    if isinstance(value, str):
        if "maxLength" in schema and not _isMaxLength(value, schema["maxLength"]):
            yield _error("maxLength", path, f"must not have more than {jsString(schema['maxLength'])} characters")
        if "minLength" in schema and not _isMinLength(value, schema["minLength"]):
            yield _error("minLength", path, f"must not have fewer than {jsString(schema['minLength'])} characters")
        if "pattern" in schema and not UnicodeRegExp(schema["pattern"]).test(value):
            yield _error("pattern", path, f'must match pattern "{schema["pattern"]}"')
    if _isFiniteNumber(value):
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            yield _error("exclusiveMaximum", path, f"must be < {jsString(schema['exclusiveMaximum'])}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            yield _error("exclusiveMinimum", path, f"must be > {jsString(schema['exclusiveMinimum'])}")
        if "maximum" in schema and value > schema["maximum"]:
            yield _error("maximum", path, f"must be <= {jsString(schema['maximum'])}")
        if "minimum" in schema and value < schema["minimum"]:
            yield _error("minimum", path, f"must be >= {jsString(schema['minimum'])}")
        if "multipleOf" in schema and not _isMultipleOf(value, schema["multipleOf"]):
            yield _error("multipleOf", path, f"must be multiple of {jsString(schema['multipleOf'])}")
    if "const" in schema and not _equal(schema["const"], value):
        yield _error("const", path, "must be equal to constant")
    if isinstance(schema.get("enum"), list) and not any(_equal(item, value) for item in schema["enum"]):
        yield _error("enum", path, "must be equal to one of the allowed values")
    if "not" in schema and next(_errors(schema["not"], value, path), None) is None:
        yield _error("not", path, "must not be valid")
    if isinstance(schema.get("allOf"), list):
        for branch in schema["allOf"]:
            yield from _errors(branch, value, path)
    if isinstance(schema.get("anyOf"), list):
        branches = [list(_errors(branch, value, path)) for branch in schema["anyOf"]]
        if all(branches):
            for branch in branches:
                yield from branch
            yield _error("anyOf", path, "must match a schema in anyOf")
    if isinstance(schema.get("oneOf"), list):
        matches = sum(1 for branch in schema["oneOf"] if next(_errors(branch, value, path), None) is None)
        if matches != 1:
            yield _error("oneOf", path, "must match exactly one schema in oneOf")


def _check(schema: Any, value: Any) -> bool:
    return next(_errors(schema, value, ""), None) is None


def _clone(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    return value


# ─── Default (`value/default`) ───────────────────────────────────────────────


def _default(schema: Any, value: Any) -> Any:
    if not isinstance(schema, dict):
        return value
    if value is UNDEFINED and "default" in schema:
        default = schema["default"]
        value = default() if callable(default) else _clone(default)
    kind = _kind(schema)
    if kind == "Array":
        if isinstance(value, list):
            for index, item in enumerate(value):
                value[index] = _default(schema["items"], item)
        return value
    if kind == "Intersect":
        return _default(_evaluateIntersect(schema["allOf"]), value)
    if kind == "Object":
        if isinstance(value, list):
            return _defaultArrayAsObject(schema, value)
        if not isinstance(value, dict):
            return value
        properties = _properties(schema)
        for key, (property, optional) in properties.items():
            result = _default(property, value.get(key, UNDEFINED))
            # An `undefined` result is not assigned when the property is optional or has no default.
            if result is UNDEFINED and (optional or not (isinstance(property, dict) and "default" in property)):
                continue
            value[key] = result
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            for key in list(value):
                if key not in properties:
                    value[key] = _default(additional, value[key])
        return value
    if kind == "Record":
        if not isinstance(value, dict | list):
            return value
        recordKey = _regexp(_recordPattern(schema), "")
        recordValue = _recordValue(schema)
        names = _ownNames(value)
        for key, slot in names:
            if recordKey.test(key) and isinstance(recordValue, dict) and "default" in recordValue:
                value[slot] = _default(recordValue, value[slot])
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            for key, slot in names:
                if not recordKey.test(key):
                    value[slot] = _default(additional, value[slot])
        return value
    if kind == "Union":
        # Declaration order (Default does not priority-sort).
        for branch in schema["anyOf"]:
            result = _default(branch, _clone(value))
            if _check(branch, result):
                return result
        return value
    return value


def _isArrayIndex(key: str) -> bool:
    """A canonical array index (``"0"``, ``"12"``, not ``"01"``)."""
    return key.isascii() and key.isdigit() and (key == "0" or not key.startswith("0")) and int(key) < 2**32 - 1


def _defaultArrayAsObject(schema: TSchema, value: list[Any]) -> list[Any]:
    """``FromObject`` given an array (``Guard.IsObject`` accepts one): a property named by an array index defaults
    that element, growing the array with holes (``UNDEFINED``). Other names would become properties of the array,
    which JSON does not carry, and are left out."""
    for key, (property, optional) in _properties(schema).items():
        if not _isArrayIndex(key):
            continue
        index = int(key)
        result = _default(property, value[index] if index < len(value) else UNDEFINED)
        if result is UNDEFINED and (optional or not (isinstance(property, dict) and "default" in property)):
            continue
        value.extend([UNDEFINED] * (index + 1 - len(value)))
        value[index] = result
    return value


# ─── Clean (`value/clean`) ───────────────────────────────────────────────────


def _ownNames(value: dict[str, Any] | list[Any]) -> list[tuple[str, Any]]:
    """The names ``Guard.Keys`` (``Object.getOwnPropertyNames``) gives a record's value, each with where it lives: an
    array, which TypeBox's record walk accepts as an object, has its indices (then ``length``, handled by the
    caller)."""
    if isinstance(value, list):
        return [(str(index), index) for index in range(len(value))]
    return [(key, key) for key in list(value)]


def _keepsUnknown(additional: Any, item: Any) -> bool:
    """An unknown name stays when ``additionalProperties`` is ``true`` or a schema the value satisfies."""
    return additional is True or (isinstance(additional, dict) and _check(additional, item))


def _clean(schema: Any, value: Any) -> Any:
    kind = _kind(schema)
    if kind == "Array":
        return [_clean(schema["items"], item) for item in value] if isinstance(value, list) else value
    if kind == "Intersect":
        # Evaluated, normally to one object; `unevaluatedProperties` reads as its `additionalProperties`.
        evaluated = _evaluateIntersect(schema["allOf"])
        if "unevaluatedProperties" in schema and _kind(evaluated) == "Object":
            evaluated = {**evaluated, "additionalProperties": schema["unevaluatedProperties"]}
        return _clean(evaluated, value)
    if kind == "Object":
        if not isinstance(value, dict):
            return value
        properties = schema["properties"]
        additional = schema.get("additionalProperties")
        for key in list(value):
            if key in properties:
                value[key] = _clean(properties[key], value[key])
            elif _keepsUnknown(additional, value[key]):
                value[key] = _clean(additional, value[key])
            else:
                del value[key]
        return value
    if kind == "Record":
        if not isinstance(value, dict | list):
            return value
        recordKey = _regexp(_recordPattern(schema), "")
        recordValue = _recordValue(schema)
        additional = schema.get("additionalProperties")
        for key, slot in _ownNames(value):
            if recordKey.test(key):
                value[slot] = _clean(recordValue, value[slot])
            elif _keepsUnknown(additional, value[slot]):
                value[slot] = _clean(additional, value[slot])
            elif isinstance(value, list):
                value[slot] = UNDEFINED  # `delete array[index]` leaves a hole
            else:
                del value[slot]
        # An array's last own name is `length` (a number, which cleaning leaves as it is); JavaScript cannot delete it.
        if isinstance(value, list) and not recordKey.test("length") and not _keepsUnknown(additional, len(value)):
            raise TypeError("Cannot delete property 'length' of [object Array]")
        return value
    if kind == "Union":
        for branch in schema["anyOf"]:
            cleaned = _clean(branch, _clone(value))
            if _check(branch, cleaned):
                return cleaned
        return value
    return value


class _Value:
    """``Value`` from ``typebox/value``."""

    @staticmethod
    def Check(schema: Any, value: Any) -> bool:
        return _check(schema, value)

    @staticmethod
    def Errors(schema: Any, value: Any) -> Iterator[dict[str, Any]]:
        """The first ``maxErrors`` (TypeBox's setting, 8) errors."""
        return islice(_errors(schema, value, ""), _MAX_ERRORS)

    @staticmethod
    def Clone(value: Any) -> Any:
        return _clone(value)

    @staticmethod
    def Default(schema: Any, value: Any) -> Any:
        """Fills absent values that have a schema default. Mutates ``value`` (clone it first)."""
        return _default(schema, value)

    @staticmethod
    def Clean(schema: Any, value: Any) -> Any:
        """Removes what ``schema`` does not evaluate, after ``UnionPrioritySort``. Mutates ``value`` (clone it
        first)."""
        # Sorted at every call, as TypeBox sorts (its types are not frozen: a schema may change between calls).
        return _clean(_unionPrioritySort(schema), value)


Value = _Value()

__all__ = ["Static", "TSchema", "Type", "UnicodeRegExp", "Value"]
