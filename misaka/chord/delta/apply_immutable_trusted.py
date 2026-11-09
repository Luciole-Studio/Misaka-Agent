"""Translated from pi's ``packages/chord/src/delta/apply-immutable-trusted.ts``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from misaka.utils.js import UNDEFINED, jsSlice

from . import Op, Path, Seg


def applyImmutableTrusted(target: Any, operations: Sequence[Op]) -> Any:
    """Apply self-produced trusted operations while copying each touched container once."""
    root = target
    # MISAKA fork: `WeakSet<object>` becomes a map from id to the copy, which keeps every copy alive while
    # the call runs, so no id is reused (a plain `dict` or `list` cannot be weakly referenced).
    owned: dict[int, Any] = {}
    for operation in operations:
        verb = operation[0]
        if verb == "r":
            root = operation[1]
            continue
        if verb in ("p", "m"):
            copiedRoot, copiedTarget = copyPath(root, operation[1], len(operation[1]), owned)
            root = copiedRoot
            if not isinstance(copiedTarget, list):
                raise TypeError("Trusted array operation target is not an array")
            if verb == "p":
                spliceTrusted(copiedTarget, operation[2], operation[3], operation[4])
            else:
                permuteTrusted(copiedTarget, operation[2])
            continue

        path = operation[1]
        copiedRoot, copiedTarget = copyPath(root, path, len(path) - 1, owned)
        root = copiedRoot
        key = path[-1]
        if verb == "s":
            defineData(copiedTarget, key, operation[2])
        elif verb == "d":
            if isinstance(copiedTarget, list):
                if not isinstance(key, int) or isinstance(key, bool):
                    raise TypeError("Trusted array deletion key is not numeric")
                spliceTrusted(copiedTarget, key, 1, [])
            else:
                copiedTarget.pop(key, None)
        elif verb == "a":
            current = read(copiedTarget, key)
            if not isinstance(current, str):
                raise TypeError("Trusted append target is not a string")
            defineData(copiedTarget, key, f"{current}{operation[2]}")
        elif verb == "t":
            current = read(copiedTarget, key)
            if not isinstance(current, str):
                raise TypeError("Trusted truncate target is not a string")
            defineData(copiedTarget, key, jsSlice(current, operation[2]))
    return root


def copyPath(root: Any, path: Path, length: int, owned: dict[int, Any]) -> tuple[Any, Any]:
    if not isContainer(root):
        raise TypeError("Trusted operation root is not a container")
    copiedRoot = root
    if id(root) not in owned:
        copiedRoot = shallowCopy(root)
        owned[id(copiedRoot)] = copiedRoot
    destination = copiedRoot
    for index in range(length):
        segment = path[index]
        child = read(destination, segment)
        if not isContainer(child):
            raise TypeError("Trusted operation path is not a container")
        if id(child) in owned:
            destination = child
            continue
        copy = shallowCopy(child)
        defineData(destination, segment, copy)
        owned[id(copy)] = copy
        destination = copy
    return copiedRoot, destination


def shallowCopy(value: Any) -> Any:
    return list(value) if isinstance(value, list) else dict(value)


def read(target: Any, key: Seg) -> Any:
    try:
        return target[key]
    except (IndexError, KeyError, TypeError):
        return UNDEFINED


def defineData(target: Any, key: Seg, value: Any) -> None:
    if isinstance(target, list) and key == len(target):
        target.append(value)
    else:
        target[key] = value


def spliceTrusted(target: list[Any], start: int, remove: int, items: Sequence[Any]) -> None:
    target[start : start + remove] = items


def permuteTrusted(target: list[Any], permutation: Sequence[int]) -> None:
    if len(target) != len(permutation):
        raise TypeError("Trusted permutation length mismatch")
    previous = list(target)
    for index in range(len(permutation)):
        target[index] = previous[permutation[index]]


def isContainer(value: Any) -> bool:
    return isinstance(value, list | dict)


__all__ = ["applyImmutableTrusted"]
