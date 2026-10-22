"""Strict-JSON copying and checking.

Translated from pi's ``packages/chord/src/json.ts``.

A JSON value is ``None``, a ``bool``, a finite ``int`` or ``float``, a ``str``, a ``list`` of JSON values,
or a ``dict`` with ``str`` keys and JSON values. Only the exact built-in containers qualify, as only
plain objects and arrays do upstream: a subclass, a tuple, a set, or a mapping of another type does
not. ``UNDEFINED`` (``misaka.utils.js``) stands for JavaScript's ``undefined``, which is not JSON but
is the one value :func:`copyJson` can be told to leave out of an object.
"""

from __future__ import annotations

import math
from typing import Any, TypedDict

from misaka.utils.js import UNDEFINED

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

# Strict-JSON representation of an application data type. Unknown payloads become JsonValue.
# MISAKA fork: upstream computes it with a TypeScript conditional type (and its `IsAny` helper), which has no runtime
# counterpart: here it is the JSON value type itself.
type JsonRepresentation[T] = JsonValue


class CopyJsonOptions(TypedDict, total=False):
    # Omit undefined object properties while preserving strict array semantics.
    omitUndefinedProperties: bool


def _typeName(value: Any) -> str:
    """``typeof value``, for the values JSON does not hold."""
    if value is UNDEFINED:
        return "undefined"
    if callable(value):
        return "function"
    return "object"


def _isFinite(value: float) -> bool:
    return isinstance(value, int) or math.isfinite(value)


def _draftContents(value: Any) -> dict[str, Any] | list[Any] | None:
    # Imported here: the tracker imports this module.
    from misaka.chord.delta.tracker import DraftArray, DraftObject

    if isinstance(value, DraftObject):
        return {key: value[key] for key in value}
    if isinstance(value, DraftArray):
        return list(value)
    return None


def copyJson(value: Any, options: CopyJsonOptions | None = None) -> JsonValue:
    """Copy a value into an alias-free strict-JSON tree owned by the caller. A draft counts as the plain value it views."""
    return copy(value, None, bool((options or {}).get("omitUndefinedProperties")))


def copy(value: Any, ancestors: set[int] | None, omitUndefinedProperties: bool) -> JsonValue:
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int | float):
        if _isFinite(value):
            return value
        raise TypeError("Value contains a non-finite number and is not strict JSON")
    if not isinstance(value, list | dict):
        # A draft is read as the plain object or array JavaScript sees through its proxy.
        contents = _draftContents(value)
        if contents is None:
            # A tuple, a set, a class instance, `bytes`: an object that is not a plain object or array. A function and
            # `UNDEFINED` are the values `typeof` does not call objects.
            if _typeName(value) == "object":
                raise TypeError("Value must contain strict JSON plain objects or arrays")
            raise TypeError(f"Value contains a non-JSON {_typeName(value)}; expected strict JSON")
        value = contents
    active = ancestors if ancestors is not None else set()
    # Identity, as `Set<object>` compares: two equal lists are two values, one list twice is a cycle.
    if id(value) in active:
        raise TypeError("Value contains cycles and is not strict JSON")
    active.add(id(value))
    try:
        if type(value) is list:
            return [copy(item, active, omitUndefinedProperties) for item in value]
        if type(value) is not dict:
            raise TypeError("Value must contain strict JSON plain objects or arrays")
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                # Upstream rejects symbol keys; no other kind of key exists there.
                raise TypeError("Value contains a non-string key and is not strict JSON")
            if item is UNDEFINED and omitUndefinedProperties:
                continue
            result[key] = copy(item, active, omitUndefinedProperties)
        return result
    finally:
        active.discard(id(value))


def isJsonValue(value: Any) -> bool:
    """Return whether a value is finite strict JSON with plain objects and no cycles."""
    return check(value, set())


def check(value: Any, ancestors: set[int]) -> bool:
    if value is None or isinstance(value, str | bool):
        return True
    if isinstance(value, int | float):
        return _isFinite(value)
    if type(value) is not list and type(value) is not dict:
        # A draft is the plain object or array JavaScript sees through its proxy.
        contents = _draftContents(value)
        if contents is None:
            return False
        value = contents
    if type(value) is list:
        if id(value) in ancestors:
            return False
        ancestors.add(id(value))
        try:
            return all(check(item, ancestors) for item in value)
        finally:
            ancestors.discard(id(value))
    if type(value) is not dict:
        return False
    if any(not isinstance(key, str) for key in value):
        return False
    if id(value) in ancestors:
        return False
    ancestors.add(id(value))
    try:
        return all(check(item, ancestors) for item in value.values())
    finally:
        ancestors.discard(id(value))


__all__ = ["CopyJsonOptions", "JsonRepresentation", "JsonValue", "copyJson", "isJsonValue"]
