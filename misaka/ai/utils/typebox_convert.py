"""Translated from the ``typebox`` npm package (1.3.27): ``value/convert`` (``Value.Convert``) and the
``value/convert/try`` helpers it rests on, for pi's ``validateToolArguments``, which converts every tool's arguments
with it before checking them.

MISAKA fork: a schema here is the JSON a pydantic tool class stands for (``typebox_helpers.typeboxSchema``), and its
kind -- TypeBox's hidden ``~kind`` -- is read from that JSON as ``misaka.chord._typebox`` reads it (``_kind``):
``anyOf`` is a union, ``enum`` an enum, a ``const`` with a literal value a literal, ``type: "object"`` with
``properties`` an object, with only ``patternProperties`` a record, ``type: "array"`` with an ``items`` schema an
array and with an ``items`` list a tuple, and the plain ``type`` names the rest. A schema none of those name is left
as it is, as ``FromType`` leaves a type it has no case for. TypeBox's ``Intersect``, ``TemplateLiteral``, ``Cyclic``
and ``Ref`` cases are not ported: they need its type engine, and no tool schema reaches them (``typeboxSchema``
inlines every ``$ref``; a 2026-10-29 survey of the tool parameter classes found no ``allOf``). ``BigInt``,
``Undefined`` and ``Void`` have no JSON schema; the ``Try`` helpers for them are kept because the others call them.

A value is JSON: ``None`` is ``null``, a ``bool`` is a boolean and never a number, and a missing property is
``UNDEFINED``. ``Check`` (union branch selection) is a Draft 2020-12 validation of the schema, as the rest of
``validation`` checks JSON schemas.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from misaka.utils.js import UNDEFINED, jsNumber, jsString, objectKeys

MAX_SAFE_INTEGER = 9007199254740991
MIN_SAFE_INTEGER = -9007199254740991

# ─── guard ──────────────────────────────────────────────────────────────────


def _isBigInt(value: Any) -> bool:
    return False                      # a JSON value is never a bigint


def _isBoolean(value: Any) -> bool:
    return isinstance(value, bool)


def _isNumber(value: Any) -> bool:
    """``Guard.IsNumber``: ``Number.isFinite(value)``."""
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _isString(value: Any) -> bool:
    return isinstance(value, str)


def _isNull(value: Any) -> bool:
    return value is None


def _isUndefined(value: Any) -> bool:
    return value is UNDEFINED


def _isArray(value: Any) -> bool:
    return isinstance(value, list)


def _isObjectNotArray(value: Any) -> bool:
    return isinstance(value, dict)


def _entriesRegExp(value: dict[str, Any]) -> list[tuple[re.Pattern[str], Any]]:
    """``Guard.EntriesRegExp``: each key as ``new RegExp(`^${key}$`)``."""
    return [(re.compile(f"^{key}$"), value[key]) for key in objectKeys(value)]


def _test(regexp: re.Pattern[str], key: str) -> bool:
    return regexp.search(key) is not None


# ─── try ────────────────────────────────────────────────────────────────────
# ``Ok(value)`` is ``(True, value)``, ``Fail()`` is ``(False, None)``.

_FAIL = (False, None)


def _ok(value: Any) -> tuple[bool, Any]:
    return (True, value)


def TryArray(value: Any) -> tuple[bool, Any]:
    return _ok(value) if _isArray(value) else _ok([value])


_BIGINT_PATTERN = re.compile(r"^-?(0|[1-9]\d*)n$")
_DECIMAL_PATTERN = re.compile(r"^-?(0|[1-9]\d*)\.\d+$")
_INTEGER_PATTERN = re.compile(r"^-?(0|[1-9]\d*)$")


def TryBigInt(value: Any) -> tuple[bool, Any]:
    if _isBigInt(value):
        return _ok(value)
    if _isBoolean(value):
        return _ok(1 if value is True else 0)
    if _isNumber(value):
        return _ok(math.trunc(value))
    if _isNull(value):
        return _ok(0)
    if _isString(value):
        lowercase = value.lower()
        if _BIGINT_PATTERN.fullmatch(value):
            return _ok(int(value[:-1]))
        if _DECIMAL_PATTERN.fullmatch(value):
            return _ok(int(value.split(".")[0]))
        if _INTEGER_PATTERN.fullmatch(value):
            return _ok(int(value))
        if lowercase == "false":
            return _ok(0)
        if lowercase == "true":
            return _ok(1)
        return _FAIL
    if _isUndefined(value):
        return _ok(0)
    return _FAIL


def TryBoolean(value: Any) -> tuple[bool, Any]:
    if _isBigInt(value):
        return _ok(False) if value == 0 else _ok(True) if value == 1 else _FAIL
    if _isBoolean(value):
        return _ok(value)
    if _isNumber(value):
        return _ok(False) if value == 0 else _ok(True) if value == 1 else _FAIL
    if _isNull(value):
        return _ok(False)
    if _isString(value):
        return (_ok(False) if value.lower() == "false" else
                _ok(True) if value.lower() == "true" else
                _ok(False) if value == "0" else
                _ok(True) if value == "1" else
                _FAIL)
    if _isUndefined(value):
        return _ok(False)
    return _FAIL


def TryNull(value: Any) -> tuple[bool, Any]:
    if _isBigInt(value):
        return _ok(None) if value == 0 else _FAIL
    if _isBoolean(value):
        return _ok(None) if value is False else _FAIL
    if _isNumber(value):
        return _ok(None) if value == 0 else _FAIL
    if _isNull(value):
        return _ok(None)
    if _isString(value):
        lowercase = value.lower()
        return _ok(None) if lowercase in ("undefined", "null") or value in ("", "0") else _FAIL
    if _isUndefined(value):
        return _ok(None)
    return _FAIL


def TryNumber(value: Any) -> tuple[bool, Any]:
    if _isBigInt(value):
        return _ok(value) if MIN_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER else _FAIL
    if _isBoolean(value):
        return _ok(1 if value else 0)
    if _isNumber(value):
        return _ok(value)
    if _isNull(value):
        return _ok(0)
    if _isString(value):
        coerced = jsNumber(value)                    # `+value`
        if _isNumber(coerced):
            return _ok(coerced)
        lowercase = value.lower()
        if lowercase == "false":
            return _ok(0)
        if lowercase == "true":
            return _ok(1)
        ok, bigint = TryBigInt(value)
        if ok:
            return _ok(bigint) if MIN_SAFE_INTEGER <= bigint <= MAX_SAFE_INTEGER else _FAIL
        return _FAIL
    if _isUndefined(value):
        return _ok(0)
    return _FAIL


def TryString(value: Any) -> tuple[bool, Any]:
    if _isBigInt(value):
        return _ok(str(value))
    if _isBoolean(value):
        return _ok("true" if value else "false")
    if _isNumber(value):
        return _ok(jsString(value))                  # `value.toString()`
    if _isNull(value):
        return _ok("null")
    if _isString(value):
        return _ok(value)
    if _isUndefined(value):
        return _ok("")
    return _FAIL


def TryUndefined(value: Any) -> tuple[bool, Any]:
    if _isBigInt(value):
        return _ok(UNDEFINED) if value == 0 else _FAIL
    if _isBoolean(value):
        return _ok(UNDEFINED) if value is False else _FAIL
    if _isNumber(value):
        return _ok(UNDEFINED) if value == 0 else _FAIL
    if _isNull(value):
        return _ok(UNDEFINED)
    if _isString(value):
        lowercase = value.lower()
        return _ok(UNDEFINED) if lowercase in ("undefined", "null") or value in ("", "0") else _FAIL
    if _isUndefined(value):
        return _ok(value)
    return _FAIL


# ─── kinds ──────────────────────────────────────────────────────────────────

_SIMPLE_KINDS = {"string": "String", "number": "Number", "integer": "Integer", "boolean": "Boolean", "null": "Null"}


def _isLiteralValue(value: Any) -> bool:
    return isinstance(value, str | bool) or (isinstance(value, int | float) and math.isfinite(value))


def _kind(schema: Any) -> str | None:
    """The TypeBox kind of a schema (its hidden ``~kind``), read from its JSON."""
    if not isinstance(schema, dict):
        return None
    if isinstance(schema.get("anyOf"), list):
        return "Union"
    if isinstance(schema.get("enum"), list):
        return "Enum"
    kind = schema.get("type")
    if "const" in schema and _isLiteralValue(schema["const"]) and kind in (None, "string", "number", "boolean"):
        return "Literal"
    if kind == "object":
        if isinstance(schema.get("properties"), dict):
            return "Object"
        return "Record" if isinstance(schema.get("patternProperties"), dict) and schema["patternProperties"] else None
    if kind == "array":
        items = schema.get("items")
        return "Array" if isinstance(items, dict) else "Tuple" if isinstance(items, list) else None
    return _SIMPLE_KINDS.get(kind) if isinstance(kind, str) else None


@lru_cache(maxsize=512)
def _validator(serialized: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads(serialized))


def Check(schema: dict[str, Any], value: Any) -> bool:
    """``Value.Check(context, type, value)``."""
    if value is UNDEFINED:
        return False
    return _validator(json.dumps(schema, sort_keys=True)).is_valid(value)


def _clone(value: Any) -> Any:
    """``Clone(value)`` for a JSON value."""
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    return value


# ─── convert ────────────────────────────────────────────────────────────────


def FromAdditionalProperties(context: dict[str, Any], entries: list[tuple[re.Pattern[str], Any]],
                             additionalProperties: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    """Used by Object and Record types. The entries are derived from the known properties obtained from
    ``properties`` and ``patternProperties`` respectively."""
    keys = objectKeys(value)
    for regexp, _ in entries:
        for key in keys:
            if not _test(regexp, key):
                value[key] = FromType(context, additionalProperties, value[key])
    return value


def FromArray(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    _, array = TryArray(value)
    return [FromType(context, type_["items"], item) for item in array]


def FromBoolean(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    ok, result = TryBoolean(value)
    return result if ok else value


def FromEnum(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    return FromType(context, _evaluateEnum(type_["enum"]), value)


def _literal(value: Any) -> dict[str, Any]:
    """``Literal(value)``: the JSON TypeBox emits for it."""
    kind = "boolean" if isinstance(value, bool) else "string" if isinstance(value, str) else "number"
    return {"const": value, "type": kind}


def _evaluateEnum(values: list[Any]) -> dict[str, Any]:
    """``EvaluateEnum``: a literal per value, broadened (an equal literal is dropped), then ``EvaluateUnionFast``:
    one literal is itself, none is ``Never``, more are a union."""
    literals: list[dict[str, Any]] = []
    for value in values:
        literal = _literal(value)
        if not any(_strictEqual(existing["const"], literal["const"]) for existing in literals):
            literals.append(literal)
    if len(literals) == 1:
        return literals[0]
    if len(literals) == 0:
        return {"not": {}}
    return {"anyOf": literals}


def _strictEqual(left: Any, right: Any) -> bool:
    """``Guard.IsEqual``: JavaScript's ``===`` between JSON scalars."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, str) or isinstance(right, str):
        return type(left) is type(right) and left == right
    if left is None or right is None:
        return left is right
    if left is UNDEFINED or right is UNDEFINED:
        return left is right
    return left == right


def FromInteger(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    ok, result = TryNumber(value)
    return math.trunc(result) if ok else value


def FromLiteral(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    const = type_["const"]
    if _strictEqual(const, value):
        return value
    if _isBoolean(const):
        ok, result = TryBoolean(value)
    elif _isNumber(const):
        ok, result = TryNumber(value)
    elif _isString(const):
        ok, result = TryString(value)
    else:
        raise AssertionError("Unreachable")
    return result if ok and _strictEqual(const, result) else value


def FromNull(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    ok, result = TryNull(value)
    return result if ok else value


def FromNumber(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    ok, result = TryNumber(value)
    return result if ok else value


def _isOptionalUndefined(type_: dict[str, Any], key: str, value: dict[str, Any]) -> bool:
    """``IsOptionalUndefined``: an optional property (TypeBox's ``~optional``, here: not ``required``) whose value is
    undefined."""
    required = type_.get("required") if isinstance(type_.get("required"), list) else []
    return key not in required and value.get(key, UNDEFINED) is UNDEFINED


def FromObject(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    if not _isObjectNotArray(value):
        return value
    entries = _entriesRegExp(type_["properties"])
    keys = objectKeys(value)
    for regexp, property_ in entries:
        for key in keys:
            # Ignore for non-present or optional-undefined
            if not _test(regexp, key) or _isOptionalUndefined(type_, key, value):
                continue
            value[key] = FromType(context, property_, value[key])
    additional = type_.get("additionalProperties")
    return FromAdditionalProperties(context, entries, additional, value) if isinstance(additional, dict) else value


def FromRecord(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    if not _isObjectNotArray(value):
        return value
    entries = _entriesRegExp(type_["patternProperties"])
    keys = objectKeys(value)
    for regexp, schema in entries:
        for key in keys:
            if _test(regexp, key):
                value[key] = FromType(context, schema, value[key])
    additional = type_.get("additionalProperties")
    return FromAdditionalProperties(context, entries, additional, value) if isinstance(additional, dict) else value


def FromString(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    ok, result = TryString(value)
    return result if ok else value


def FromTuple(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    if not _isArray(value):
        return value
    items = type_["items"]
    for index in range(min(len(items), len(value))):
        value[index] = FromType(context, items[index], value[index])
    return value


def FromUnion(context: dict[str, Any], type_: dict[str, Any], value: Any) -> Any:
    if any(Check(variant, value) for variant in type_["anyOf"]):
        return value
    candidates = [FromType(context, variant, _clone(value)) for variant in type_["anyOf"]]
    selected = next((candidate for candidate in candidates if Check(type_, candidate)), UNDEFINED)
    return value if selected is UNDEFINED else selected


_CASES = {
    "Array": FromArray,
    "Boolean": FromBoolean,
    "Enum": FromEnum,
    "Integer": FromInteger,
    "Literal": FromLiteral,
    "Null": FromNull,
    "Number": FromNumber,
    "Object": FromObject,
    "Record": FromRecord,
    "String": FromString,
    "Tuple": FromTuple,
    "Union": FromUnion,
}


def FromType(context: dict[str, Any], type_: Any, value: Any) -> Any:
    case = _CASES.get(_kind(type_))
    return case(context, type_, value) if case is not None else value


def Convert(type_: Any, value: Any) -> Any:
    """Converts a value to the given type, coercing interior values if a reasonable conversion is possible. This
    function returns unknown so callers should Check the return value before use. This function mutates the
    provided value. If mutation is not wanted, you should Clone the value before passing to this function."""
    return FromType({}, type_, value)


__all__ = ["Check", "Convert"]
