"""Leaf-by-leaf assignment into a draft.

Translated from pi's ``packages/durable/src/harness/json.ts``. A target is a ``DraftObject`` or ``DraftArray`` (``misaka.chord``), a
value is strict JSON.
"""

from __future__ import annotations

from typing import Any

from misaka.chord.delta.diff import strictEquals
from misaka.chord.delta.tracker import DraftArray, DraftObject


def assignJson(target: Any, key: str | int, value: Any) -> None:
    """Assign ``value`` at ``target[key]`` leaf by leaf. Chord records a container assignment as one full set and only
    emits an append when a string leaf is reassigned with a longer string, so writing the partial whole would store and
    publish the complete message on every flush."""
    current = target[key]
    if isRecord(current) and isRecord(value):
        for name in list(current.keys()):
            if name not in value:
                del current[name]
        for name, child in list(value.items()):
            assignJson(current, name, child)
        return
    if isinstance(current, DraftArray | list) and isinstance(value, list) and len(current) <= len(value):
        for index in range(len(value)):
            if index < len(current):
                assignJson(current, index, value[index])
            elif isinstance(current, list):
                current.append(value[index])
            else:
                current.push(value[index])
        return
    if not strictEquals(current, value):
        target[key] = value


def isRecord(value: Any) -> bool:
    return isinstance(value, dict | DraftObject)


__all__ = ["assignJson"]
