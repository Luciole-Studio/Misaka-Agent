"""Translated from pi's ``packages/chord/src/delta/revision-validator.ts``."""

from __future__ import annotations

import math
from typing import Any

from misaka.chord.json import JsonValue
from misaka.utils.identity import IdentityWeakSet
from misaka.utils.js import UNDEFINED


def isContainer(value: Any) -> bool:
    return isinstance(value, list | dict)


class JsonRevisionValidator:
    """Validates immutable replica revisions while skipping containers validated in earlier revisions."""

    def __init__(self) -> None:
        self._validated = IdentityWeakSet()

    def validate[T: JsonValue](self, value: T) -> T:
        return self._validate(value, set(), set())

    def _validate(self, value: Any, ancestors: set[int], finished: set[int]) -> Any:
        if not isContainer(value) or value in self._validated:
            return value if isContainer(value) else assertPrimitive(value)
        if id(value) in finished:
            return value
        if id(value) in ancestors:
            raise TypeError("Replicated state cannot contain cycles")
        ancestors.add(id(value))
        try:
            if isinstance(value, list):
                assertDenseArray(value)
                for item in value:
                    self._validate(item, ancestors, finished)
            else:
                assertPlainObject(value)
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise TypeError("Replicated state cannot contain symbol properties")
                    self._validate(item, ancestors, finished)
            finished.add(id(value))
            self._validated.add(value)
            return value
        finally:
            ancestors.discard(id(value))


def assertPrimitive(value: Any) -> None | bool | int | float | str:
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int | float) and (isinstance(value, int) or math.isfinite(value)):
        return value
    raise TypeError("Replicated state values must be strict JSON")


def assertPlainObject(value: Any) -> None:
    if type(value) is not dict:
        raise TypeError("Replicated state containers must be plain objects or arrays")


def assertDenseArray(value: list[Any]) -> None:
    for item in value:
        if item is UNDEFINED:
            raise TypeError(
                "Replicated state arrays must contain enumerable indexed data properties with defined values"
            )


__all__ = ["JsonRevisionValidator"]
