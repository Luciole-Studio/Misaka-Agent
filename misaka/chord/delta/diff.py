"""Translated from pi's ``packages/chord/src/delta/diff.ts``.

MISAKA fork: ``Map<JsonValue, ...>`` compares primitives by value and containers by reference, with no
number-boolean confusion; ``_identityKey`` gives Python's ``dict`` the same keys (``1``, ``1.0`` and ``True``
would otherwise be one). ``===`` on two values is :func:`strictEquals`.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from misaka.utils.js import jsLength, jsSlice, jsString

from . import RESERVED_SEGMENTS, Op, Path, overlap

DEFAULT_OVERLAP_SCAN = 65_536
MAX_DELTA_OPERATIONS = 4_096


class _Batch(list[Any]):
    """The operations of one diff. Upstream remembers the batches that overflowed in a ``WeakSet``; a list
    subclass can carry the flag itself."""

    overflowed = False


def emitOperation(operations: _Batch, operation: Op) -> None:
    if operations.overflowed:
        return
    if len(operations) >= MAX_DELTA_OPERATIONS:
        operations.overflowed = True
        return
    operations.append(operation)


def isContainer(value: Any) -> bool:
    return isinstance(value, list | dict)


def strictEquals(left: Any, right: Any) -> bool:
    """JavaScript's ``===`` over JSON values: containers by identity, a boolean never equals a number."""
    if left is right:
        return True
    if isContainer(left) or isContainer(right):
        return False
    if isinstance(left, bool) or isinstance(right, bool) or left is None or right is None:
        return False
    return left == right


def _identityKey(value: Any) -> Any:
    """The key a JavaScript ``Map`` would use for the value."""
    if isContainer(value):
        return ("o", id(value))
    if value is None:
        return ("n",)
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, int | float):
        return ("d", value)
    return ("s", value)


def emitSet(path: Path, value: Any, operations: _Batch) -> None:
    if len(path) == 0:
        emitOperation(operations, ["r", value])
    else:
        emitOperation(operations, ["s", path, value])


def equalJson(left: Any, right: Any) -> bool:
    if strictEquals(left, right):
        return True
    if not isContainer(left) or not isContainer(right) or isinstance(left, list) != isinstance(right, list):
        return False
    if isinstance(left, list):
        if len(left) != len(right):
            return False
        for index in range(len(left)):
            if not equalJson(left[index], right[index]):
                return False
        return True
    if len(left) != len(right):
        return False
    for key in left:
        if key not in right or not equalJson(left[key], right[key]):
            return False
    return True


def permutation(before: Sequence[Any], after: Sequence[Any]) -> list[int] | None:
    if len(before) != len(after):
        return None
    positions: dict[Any, dict[str, Any]] = {}
    for index in range(len(before)):
        key = _identityKey(before[index])
        entry = positions.get(key)
        if entry is None:
            positions[key] = {"indices": [index], "used": 0}
        else:
            entry["indices"].append(index)
    result = [0] * len(after)
    for index in range(len(after)):
        entry = positions.get(_identityKey(after[index]))
        if entry is None or entry["used"] == len(entry["indices"]):
            return None
        result[index] = entry["indices"][entry["used"]]
        entry["used"] += 1
    return result


def emitString(before: str, after: str, path: Path, operations: _Batch) -> None:
    if before == after:
        return
    beforeLength = jsLength(before)
    if jsLength(after) > beforeLength and jsSlice(after, 0, beforeLength) == before:
        emitOperation(operations, ["a", path, jsSlice(after, beforeLength)])
        return
    shared = overlap(before, after, DEFAULT_OVERLAP_SCAN)
    if shared == 0:
        emitOperation(operations, ["s", path, after])
        return
    emitOperation(operations, ["t", path, beforeLength - shared])
    if jsLength(after) > shared:
        emitOperation(operations, ["a", path, jsSlice(after, shared)])


type ArrayMatch = tuple[int, int]

MAX_IDENTITY_CANDIDATES = 200_000
MAX_SEMANTIC_CELLS = 65_536


def sameValue(left: Any, right: Any) -> bool:
    return strictEquals(left, right) or equalJson(left, right)


def lcsMatches(
    before: Sequence[Any],
    after: Sequence[Any],
    equal: Callable[[Any, Any], bool],
    maxCells: int,
) -> list[ArrayMatch] | None:
    if len(before) == 0 or len(after) == 0:
        return []
    if len(before) * len(after) > maxCells:
        return None
    width = len(after) + 1
    lengths = [0] * ((len(before) + 1) * width)
    for left in range(len(before) - 1, -1, -1):
        for right in range(len(after) - 1, -1, -1):
            at = left * width + right
            lengths[at] = (
                lengths[(left + 1) * width + right + 1] + 1
                if equal(before[left], after[right])
                else max(lengths[(left + 1) * width + right], lengths[left * width + right + 1])
            )
    matches: list[ArrayMatch] = []
    left = 0
    right = 0
    while left < len(before) and right < len(after):
        if equal(before[left], after[right]) and lengths[left * width + right] == lengths[(left + 1) * width + right + 1] + 1:
            matches.append((left, right))
            left += 1
            right += 1
        elif lengths[(left + 1) * width + right] >= lengths[left * width + right + 1]:
            left += 1
        else:
            right += 1
    return matches


def semanticallyAligned(left: Any, right: Any) -> bool:
    if sameValue(left, right):
        return True
    if not isContainer(left) or not isContainer(right) or isinstance(left, list) != isinstance(right, list):
        return False
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return any(isContainer(value) and value is right[index] for index, value in enumerate(left))
    for key, value in left.items():
        if isContainer(value) and key in right and value is right[key]:
            return True
    return False


def lowerBound(values: Sequence[int], value: int) -> int:
    low = 0
    high = len(values)
    while low < high:
        middle = (low + high) >> 1
        if values[middle] < value:
            low = middle + 1
        else:
            high = middle
    return low


def identitySubsequence(
    before: Sequence[Any],
    beforeStart: int,
    beforeEnd: int,
    after: Sequence[Any],
    afterStart: int,
    afterEnd: int,
) -> list[ArrayMatch] | None:
    beforeCount = beforeEnd - beforeStart
    afterCount = afterEnd - afterStart
    matches: list[ArrayMatch] = []
    if afterCount < beforeCount:
        beforeIndex = beforeStart
        for afterIndex in range(afterStart, afterEnd):
            while beforeIndex < beforeEnd and not strictEquals(before[beforeIndex], after[afterIndex]):
                beforeIndex += 1
            if beforeIndex == beforeEnd:
                return None
            matches.append((beforeIndex, afterIndex))
            beforeIndex += 1
        return matches
    if beforeCount < afterCount:
        afterIndex = afterStart
        for beforeIndex in range(beforeStart, beforeEnd):
            while afterIndex < afterEnd and not strictEquals(before[beforeIndex], after[afterIndex]):
                afterIndex += 1
            if afterIndex == afterEnd:
                return None
            matches.append((beforeIndex, afterIndex))
            afterIndex += 1
        return matches
    return None


def greedyIdentityAnchors(
    positions: dict[Any, list[int]],
    after: Sequence[Any],
    afterStart: int,
    afterEnd: int,
) -> list[ArrayMatch]:
    matches: list[ArrayMatch] = []
    previous = -1
    for afterIndex in range(afterStart, afterEnd):
        candidates = positions.get(_identityKey(after[afterIndex]))
        if candidates is None:
            continue
        at = lowerBound(candidates, previous + 1)
        if at >= len(candidates):
            continue
        beforeIndex = candidates[at]
        matches.append((beforeIndex, afterIndex))
        previous = beforeIndex
    return matches


def identityAnchors(
    before: Sequence[Any],
    beforeStart: int,
    beforeEnd: int,
    after: Sequence[Any],
    afterStart: int,
    afterEnd: int,
) -> list[ArrayMatch]:
    positions: dict[Any, list[int]] = {}
    for index in range(beforeStart, beforeEnd):
        positions.setdefault(_identityKey(before[index]), []).append(index)
    candidateCount = 0
    for index in range(afterStart, afterEnd):
        candidateCount += len(positions.get(_identityKey(after[index]), ()))
        if candidateCount > MAX_IDENTITY_CANDIDATES:
            return greedyIdentityAnchors(positions, after, afterStart, afterEnd)
    if candidateCount == 0:
        return []

    # Each candidate is [before, after, previous].
    candidates: list[tuple[int, int, int]] = []
    tails: list[int] = []
    tailValues: list[int] = []
    for afterIndex in range(afterStart, afterEnd):
        beforePositions = positions.get(_identityKey(after[afterIndex]))
        if beforePositions is None:
            continue
        for index in range(len(beforePositions) - 1, -1, -1):
            beforeIndex = beforePositions[index]
            at = lowerBound(tailValues, beforeIndex)
            candidateIndex = len(candidates)
            candidates.append((beforeIndex, afterIndex, -1 if at == 0 else tails[at - 1]))
            if at < len(tails):
                tails[at] = candidateIndex
                tailValues[at] = beforeIndex
            else:
                tails.append(candidateIndex)
                tailValues.append(beforeIndex)
    matches: list[ArrayMatch] = []
    candidateIndex = tails[-1] if tails else -1
    while candidateIndex >= 0:
        beforeIndex, afterIndex, previous = candidates[candidateIndex]
        matches.append((beforeIndex, afterIndex))
        candidateIndex = previous
    matches.reverse()
    return matches


def processArrayMatches(
    before: list[Any],
    after: list[Any],
    path: Path,
    operations: _Batch,
    beforeStart: int,
    beforeEnd: int,
    afterStart: int,
    afterEnd: int,
    outputStart: int,
    matches: Sequence[ArrayMatch],
) -> None:
    beforeAt = beforeStart
    afterAt = afterStart
    outputAt = outputStart
    for beforeMatch, afterMatch in matches:
        if operations.overflowed:
            return
        diffArrayRegion(before, after, path, operations, beforeAt, beforeMatch, afterAt, afterMatch, outputAt)
        outputAt += afterMatch - afterAt
        if not sameValue(before[beforeMatch], after[afterMatch]):
            diffValue(before[beforeMatch], after[afterMatch], [*path, outputAt], operations)
        outputAt += 1
        beforeAt = beforeMatch + 1
        afterAt = afterMatch + 1
    if not operations.overflowed:
        diffArrayRegion(before, after, path, operations, beforeAt, beforeEnd, afterAt, afterEnd, outputAt)


def diffArrayRegion(
    before: list[Any],
    after: list[Any],
    path: Path,
    operations: _Batch,
    beforeStart: int,
    beforeEnd: int,
    afterStart: int,
    afterEnd: int,
    outputStart: int,
) -> None:
    if operations.overflowed:
        return
    while beforeStart < beforeEnd and afterStart < afterEnd and sameValue(before[beforeStart], after[afterStart]):
        beforeStart += 1
        afterStart += 1
        outputStart += 1
    while beforeStart < beforeEnd and afterStart < afterEnd and sameValue(before[beforeEnd - 1], after[afterEnd - 1]):
        beforeEnd -= 1
        afterEnd -= 1
    beforeCount = beforeEnd - beforeStart
    afterCount = afterEnd - afterStart
    if beforeCount == 0 and afterCount == 0:
        return
    if beforeCount == 0 or afterCount == 0:
        emitOperation(operations, ["p", path, outputStart, beforeCount, after[afterStart:afterEnd]])
        return

    if beforeCount == afterCount:
        positional: list[ArrayMatch] = []
        for offset in range(beforeCount):
            if sameValue(before[beforeStart + offset], after[afterStart + offset]):
                positional.append((beforeStart + offset, afterStart + offset))
        if len(positional) > 0:
            processArrayMatches(
                before, after, path, operations, beforeStart, beforeEnd, afterStart, afterEnd, outputStart, positional
            )
            return

    subsequence = identitySubsequence(before, beforeStart, beforeEnd, after, afterStart, afterEnd)
    if subsequence is not None and len(subsequence) > 0:
        processArrayMatches(
            before, after, path, operations, beforeStart, beforeEnd, afterStart, afterEnd, outputStart, subsequence
        )
        return

    identity = identityAnchors(before, beforeStart, beforeEnd, after, afterStart, afterEnd)
    if len(identity) > 0:
        processArrayMatches(
            before, after, path, operations, beforeStart, beforeEnd, afterStart, afterEnd, outputStart, identity
        )
        return

    semantic = lcsMatches(
        before[beforeStart:beforeEnd], after[afterStart:afterEnd], semanticallyAligned, MAX_SEMANTIC_CELLS
    )
    if semantic is not None and len(semantic) > 0:
        absolute = [(beforeStart + beforeIndex, afterStart + afterIndex) for beforeIndex, afterIndex in semantic]
        processArrayMatches(
            before, after, path, operations, beforeStart, beforeEnd, afterStart, afterEnd, outputStart, absolute
        )
        return

    if beforeCount == 1 and afterCount == 1:
        diffValue(before[beforeStart], after[afterStart], [*path, outputStart], operations)
        return
    emitOperation(operations, ["p", path, outputStart, beforeCount, after[afterStart:afterEnd]])


def diffArray(before: list[Any], after: list[Any], path: Path, operations: _Batch) -> None:
    if before is after or equalJson(before, after):
        return
    if (
        len(before) == len(after)
        and len(before) > 1
        and not sameValue(before[0], after[0])
        and not sameValue(before[-1], after[-1])
    ):
        order = permutation(before, after)
        if order is not None:
            emitOperation(operations, ["m", path, order])
            return
    diffArrayRegion(before, after, path, operations, 0, len(before), 0, len(after), 0)


def diffObject(before: dict[str, Any], after: dict[str, Any], path: Path, operations: _Batch) -> None:
    beforeKeys = list(before)
    afterKeys = list(after)
    if any(key in RESERVED_SEGMENTS for key in (*beforeKeys, *afterKeys)):
        if not equalJson(before, after):
            emitSet(path, after, operations)
        return
    for key in afterKeys:
        if operations.overflowed:
            return
        if key in before:
            diffValue(before[key], after[key], [*path, key], operations)
        else:
            emitSet([*path, key], after[key], operations)
    for key in beforeKeys:
        if operations.overflowed:
            return
        if key not in after:
            emitOperation(operations, ["d", [*path, key]])


def diffValue(before: Any, after: Any, path: Path, operations: _Batch) -> None:
    if strictEquals(before, after) or operations.overflowed:
        return
    if isinstance(before, str) and isinstance(after, str) and len(path) > 0:
        emitString(before, after, path, operations)
        return
    if isinstance(before, list) and isinstance(after, list):
        diffArray(before, after, path, operations)
        return
    if isinstance(before, dict) and isinstance(after, dict):
        diffObject(before, after, path, operations)
        return
    emitSet(path, after, operations)


def jsonCost(value: Any) -> int:
    if value is None:
        return 4
    if isinstance(value, str):
        return jsLength(value) + 2
    if isinstance(value, bool):
        return 4 if value else 5
    if isinstance(value, int | float):
        return len(jsString(value))
    if isinstance(value, list):
        cost = 2
        for index in range(len(value)):
            cost += jsonCost(value[index]) + (0 if index == 0 else 1)
        return cost
    cost = 2
    for index, key in enumerate(value):
        cost += jsLength(key) + 3 + jsonCost(value[key]) + (0 if index == 0 else 1)
    return cost


def pathCost(path: Path) -> int:
    cost = 2
    for index in range(len(path)):
        segment = path[index]
        cost += (jsLength(segment) + 2 if isinstance(segment, str) else len(str(segment))) + (0 if index == 0 else 1)
    return cost


def operationCost(operation: Op) -> int:
    verb = operation[0]
    if verb == "r":
        return 6 + jsonCost(operation[1])
    if verb == "s":
        return 7 + pathCost(operation[1]) + jsonCost(operation[2])
    if verb == "d":
        return 6 + pathCost(operation[1])
    if verb == "a":
        return 7 + pathCost(operation[1]) + jsLength(operation[2]) + 2
    if verb == "t":
        return 7 + pathCost(operation[1]) + len(str(operation[2]))
    if verb == "p":
        return 10 + pathCost(operation[1]) + len(str(operation[2])) + len(str(operation[3])) + jsonCost(operation[4])
    return 7 + pathCost(operation[1]) + jsonCost(operation[2])


def diffRevisions(before: Any, after: Any) -> list[Op]:
    """Compute a compact operation batch from two immutable JSON revisions."""
    operations = _Batch()
    diffValue(before, after, [], operations)
    if operations.overflowed:
        return [["r", after]]
    if len(operations) == 0 or operations[0][0] == "r":
        return list(operations)
    deltaCost = 2
    for operation in operations:
        deltaCost += operationCost(operation) + 1
    if deltaCost < 65_536:
        return list(operations)
    snapshotCost = jsonCost(after) + 6
    return [["r", after]] if deltaCost >= snapshotCost else list(operations)


__all__ = ["diffRevisions"]
