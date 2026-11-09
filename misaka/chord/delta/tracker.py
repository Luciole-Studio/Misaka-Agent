"""Immutable revision tracking over plain JSON: overlay drafts, exact operation batches.

Translated from pi's ``packages/chord/src/delta/tracker.ts``.

MISAKA forks, all forced by the language and none of them visible in a batch:

- A ``Proxy`` becomes a draft class. :class:`DraftObject` is read and written with item access
  (``draft["key"]``); reading a missing key answers ``UNDEFINED``, as a property read answers ``undefined``,
  and assigning ``UNDEFINED`` to a key deletes it. :class:`DraftArray` keeps the array mutators as methods
  (``push``, ``pop``, ``shift``, ``unshift``, ``splice``, ``reverse``, ``sort``, ``fill``, ``copyWithin``) and
  has a ``length`` property. Using a draft after its change settled raises ``TypeError``, as every trap of the
  upstream ``Proxy`` does.
- ``WeakMap`` and ``WeakSet`` over plain containers become dictionaries keyed by ``id()`` that the owner
  keeps alive; the registries of contexts stay weak (``weakref``).
- ``null`` is ``None`` and ``undefined`` is ``UNDEFINED`` (``misaka.utils.js``) wherever a stored value or a
  map lookup can be either.
"""

from __future__ import annotations

import functools
import math
import weakref
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from misaka.chord.json import JsonValue, copyJson
from misaka.utils.js import (
    UNDEFINED,
    RangeError,
    jsLength,
    jsNumber,
    jsSlice,
    jsString,
    toUtf16Units,
)

from . import RESERVED_SEGMENTS, NonEmptyPath, Op, Path, applyImmutable, overlap
from .apply_immutable_trusted import applyImmutableTrusted
from .diff import strictEquals

# Status: "open" | "prepared" | "consumed" | "aborted" | "stale"
# ParentKind: 0 object, 1 base-array entry, 2 inserted-array entry.

RELEASED: dict[str, JsonValue] = {}
MAX_DELTA_OPERATIONS = 4_096
MAX_SIMPLE_OBJECT_NODES = 128


class Prepared(Protocol):
    base: Any
    value: Any
    ops: Sequence[Op]
    baseRevision: int

    def abort(self) -> None: ...


class Change(Protocol):
    state: Any

    def prepare(self) -> Prepared: ...

    def abort(self) -> None: ...


class Tracker(Protocol):
    value: Any
    revision: int

    def beginChange(self) -> Change: ...

    def prepareReplace(self, value: Any) -> Prepared: ...

    def adopt(self, prepared: Prepared) -> None: ...


class PreparedImpl:
    def __init__(self, context: _OverlayContext, value: Any, operations: Sequence[Op]) -> None:
        self._context = context
        self.value = value
        self.base = context.baseValue
        self.ops = operations
        PREPARED[self] = context

    @property
    def baseRevision(self) -> int:
        return self._context.baseRevision

    def abort(self) -> None:
        abortContext(self._context)


class ChangeImpl:
    def __init__(self, context: _OverlayContext) -> None:
        self._context: _OverlayContext | None = context
        self._preparedStatus: _StatusCell | None = None
        self._settled = False

    @property
    def state(self) -> Any:
        context = self._context
        state = context.root.proxy if context is not None and context.root is not None else None
        if state is None:
            raise TypeError("Cannot use a settled overlay")
        return state

    def prepare(self) -> PreparedImpl:
        if self._settled:
            raise RuntimeError("Change has already been settled")
        context = self._context
        assert context is not None
        assertWritable(context)
        context.status.value = "prepared"
        try:
            if not context.replacement:
                context.ops = [] if len(context.dirty) == 0 else emitOperations(context)
            prepared = materializePrepared(context)
            self._preparedStatus = context.status
            self._settled = True
            return prepared
        except BaseException:
            context.status.value = "aborted"
            clearContext(context)
            self._settled = True
            raise
        finally:
            self._context = None

    def abort(self) -> None:
        if self._settled:
            if self._preparedStatus is not None and self._preparedStatus.value == "prepared":
                self._preparedStatus.value = "aborted"
            self._preparedStatus = None
            return
        self._settled = True
        assert self._context is not None
        abortContext(self._context)
        self._context = None


class TrackerImpl:
    def __init__(self, initial: Any) -> None:
        self._owner = object()
        self._contexts: set[weakref.ref[_OverlayContext]] = set()
        self._value = initial
        self._revision = 0
        self._pruneBudget = 256
        self._selfRef = weakref.ref(self)

    @property
    def value(self) -> Any:
        return self._value

    @property
    def revision(self) -> int:
        return self._revision

    def beginChange(self) -> ChangeImpl:
        context = createContext(self._selfRef, self._owner, self._revision, self._value, False, self._value)
        self._register(context)
        return ChangeImpl(context)

    def prepareReplace(self, value: Any) -> PreparedImpl:
        context = createContext(self._selfRef, self._owner, self._revision, value, True, self._value)
        context.status.value = "prepared"
        self._register(context)
        try:
            return materializePrepared(context)
        except BaseException:
            context.status.value = "aborted"
            clearContext(context)
            raise

    def adopt(self, prepared: Any) -> None:
        try:
            context = PREPARED.get(prepared)
        except TypeError:
            context = None
        if context is None or context.owner is not self._owner:
            raise RuntimeError("Prepared change belongs to a different tracker")
        if context.status.value == "consumed":
            raise RuntimeError("Prepared change has already been used")
        if context.status.value == "aborted":
            raise RuntimeError("Prepared change has been aborted")
        if context.status.value == "stale":
            raise RuntimeError("Prepared change is stale")
        if context.status.value != "prepared":
            raise RuntimeError("Prepared change is not ready")
        if context.baseRevision != self._revision:
            context.status.value = "stale"
            clearContext(context)
            raise RuntimeError("Prepared change is stale")

        if self._value is not prepared.base:
            context.status.value = "stale"
            raise RuntimeError("Prepared change is stale")
        # Materialization happened during prepare. Adoption is an infallible pointer swap so storage
        # failure can discard the candidate without touching authority.
        self._value = prepared.value
        context.status.value = "consumed"
        self._revision += 1
        self._invalidate(context)

    def releaseContext(self, context: _OverlayContext) -> None:
        if context.registryRef is not None:
            self._contexts.discard(context.registryRef)
        context.registryRef = None

    def _register(self, context: _OverlayContext) -> None:
        reference = weakref.ref(context)
        context.registryRef = reference
        self._contexts.add(reference)
        self._pruneBudget -= 1
        if self._pruneBudget == 0:
            self._prune()
            self._pruneBudget = max(256, len(self._contexts))

    def _prune(self) -> None:
        for reference in list(self._contexts):
            if reference() is None:
                self._contexts.discard(reference)

    def _invalidate(self, winner: _OverlayContext) -> None:
        for reference in list(self._contexts):
            context = reference()
            if context is None or context is winner:
                continue
            if context.status.value in ("open", "prepared"):
                context.status.value = "stale"
            # Adoption must not walk every node touched by a competing draft. Drop the context's strong
            # overlay references in O(1); externally retained proxies still see the stale status and are
            # reclaimed with their holders.
            releaseOverlayReferences(context)
        self._contexts.clear()
        self._pruneBudget = 256

    def replacementBase(self, context: _OverlayContext) -> Any:
        return self._value if context.baseRevision == self._revision else UNDEFINED


def track(initial: Any) -> TrackerImpl:
    """Take immutable ownership of an alias-free strict-JSON root in O(1)."""
    return TrackerImpl(initial)


def materializePrepared(context: _OverlayContext) -> PreparedImpl:
    operations = ensureOperations(context)
    base = context.baseValue
    # Operation payloads are already detached from the mutable overlay. The resulting revision and public
    # batch intentionally share those payloads under the trusted immutable-transfer contract.
    if len(operations) == 0:
        value = base
    elif context.simpleObjectMaterialization:
        assert context.root is not None
        value = cloneNode(context.root)
    else:
        value = materializeOperations(base, operations)
    prepared = PreparedImpl(context, value, operations)
    if len(context.nodes) > 4_096:
        releaseOverlayReferences(context)
    else:
        clearContext(context)
    return prepared


def materializeOperations(base: Any, operations: Sequence[Op]) -> Any:
    for operation in operations:
        # Native splice/permutation remains faster for large structural batches.
        if operation[0] in ("p", "m"):
            return applyImmutable(base, operations)
    return applyImmutableTrusted(base, operations)


def createNode(
    context: _OverlayContext,
    base: Any,
    parent: _OverlayNode | None,
    parentKind: int = 0,
    parentKey: str | int = "",
    parentSource: _InsertSource | None = None,
    parentPlacement: bool = False,
) -> _OverlayNode:
    assert context.rawNodes is not None
    existing = context.rawNodes.get(id(base))
    if existing is not None:
        return existing
    assert context.bases is not None
    baseIndex = len(context.bases)
    parentIndex = parent.baseIndex if parent is not None else -1
    context.bases.append(base)
    node = _OverlayNode(context, baseIndex, parentIndex, parentKind, parentKey, parentPlacement)
    if parentSource is not None:
        node.parentSource = parentSource
    node.proxy = DraftArray(node) if isinstance(base, list) else DraftObject(node)
    context.rawNodes[id(base)] = node
    context.nodes.append(node)
    return node


def nodeBase(node: _OverlayNode) -> Any:
    assert node.context.bases is not None
    return node.context.bases[node.baseIndex]


def nodeParent(node: _OverlayNode) -> _OverlayNode | None:
    return None if node.parentIndex < 0 else node.context.nodes[node.parentIndex]


def storeValue(context: _OverlayContext, value: Any) -> Any:
    if not isContainer(value):
        return value
    assert context.stored is not None
    index = len(context.stored)
    context.stored.append(value)
    return _Index(index)


def storedValue(context: _OverlayContext, reference: Any) -> Any:
    assert context.stored is not None
    return context.stored[reference.index] if isinstance(reference, _Index) else reference


def replaceStoredValue(context: _OverlayContext, reference: Any, value: Any) -> Any:
    assert context.stored is not None
    if isContainer(value):
        if isinstance(reference, _Index):
            context.stored[reference.index] = value
            return reference
        return storeValue(context, value)
    if isinstance(reference, _Index):
        context.stored[reference.index] = RELEASED
    return value


def releaseStoredValue(context: _OverlayContext, reference: Any) -> None:
    assert context.stored is not None
    if isinstance(reference, _Index):
        context.stored[reference.index] = RELEASED


def isSettledContext(context: _OverlayContext) -> bool:
    return context.overlayReleased or context.status.value in ("consumed", "aborted", "stale")


def assertReadable(context: _OverlayContext) -> None:
    if isSettledContext(context):
        raise TypeError("Cannot use a settled overlay")


def assertWritable(context: _OverlayContext) -> None:
    assertReadable(context)
    if context.status.value != "open":
        raise TypeError("Prepared overlays are read-only")


def getProperty(node: _OverlayNode, property: str) -> Any:
    """Read a property of an object draft."""
    assertReadable(node.context)
    if not objectHas(node, property):
        return UNDEFINED
    value = objectValue(node, property)
    if not isContainer(value):
        return value
    return createNode(node.context, value, node, 0, property, None, hasObjectWrite(node, property)).proxy


def getArrayIndex(node: _OverlayNode, index: int) -> Any:
    overlay = arrayOverlay(node)
    if index >= arrayLength(overlay):
        return UNDEFINED
    piece = locatePiece(overlay, index)
    sourceIndex = piece.start + piece.step * overlay.locatedOffset
    value = entryValueAt(node, piece, sourceIndex)
    if not isContainer(value):
        return value
    return createNode(
        node.context,
        value,
        node,
        1 if piece.kind == "base" else 2,
        sourceIndex,
        piece.source if piece.kind == "insert" else None,
        piece.kind == "insert" or hasEntryOverrideAt(overlay, piece, sourceIndex),
    ).proxy


def setProperty(node: _OverlayNode, property: str, supplied: Any) -> None:
    """Write a property of an object draft. Writing ``UNDEFINED`` deletes it."""
    assertWritable(node.context)
    if supplied is UNDEFINED:
        deleteProperty(node, property)
        return
    stored = clonePlacement(supplied)
    current = objectValue(node, property)
    wasDeleted = isObjectDeleted(node, property)
    if not wasDeleted and not isContainer(stored) and strictEquals(current, stored):
        return
    setObjectWrite(node, property, stored)
    if wasDeleted and property in nodeBase(node):
        if node.readded is None:
            node.readded = {}
        node.readded[property] = None
    deleteObjectDeletion(node, property)
    markDirty(node)


def deleteProperty(node: _OverlayNode, property: str) -> None:
    assertWritable(node.context)
    if not objectHas(node, property):
        return
    deleteObjectWrite(node, property)
    if node.readded is not None:
        node.readded.pop(property, None)
    setObjectDeletion(node, property)
    markDirty(node)


def hasProperty(node: _OverlayNode, property: str) -> bool:
    assertReadable(node.context)
    return objectHas(node, property)


def ownKeys(node: _OverlayNode) -> list[str]:
    assertReadable(node.context)
    base = nodeBase(node)
    existingKeysOnly = (
        node.deleteKey is None
        and node.deletes is None
        and node.readded is None
        and (node.writeKey is None or node.writeKey in base)
    )
    if existingKeysOnly and node.writes is not None:
        for key in node.writes:
            if key not in base:
                existingKeysOnly = False
                break
    if existingKeysOnly:
        return list(base)
    keys = [
        key
        for key in base
        if not isObjectDeleted(node, key) and not (node.readded is not None and key in node.readded)
    ]
    seen = set(keys)
    if node.writeKey is not None and node.writeKey not in seen:
        keys.append(node.writeKey)
        seen.add(node.writeKey)
    if node.writes is not None:
        for key in node.writes:
            if key in seen:
                continue
            keys.append(key)
            seen.add(key)
    # MISAKA fork: a dict keeps insertion order for every key. JavaScript lists array-index keys first, in
    # ascending order, which neither a Python dict nor the value the batch builds does.
    return keys


def hasObjectWrite(node: _OverlayNode, key: str) -> bool:
    return node.writeKey == key or (node.writes is not None and key in node.writes)


def setObjectWrite(node: _OverlayNode, key: str, value: Any) -> None:
    if node.writes is not None:
        reference = node.writes.get(key, UNDEFINED)
        node.writes[key] = (
            storeValue(node.context, value)
            if reference is UNDEFINED
            else replaceStoredValue(node.context, reference, value)
        )
        return
    if node.writeKey is None or node.writeKey == key:
        node.writeKey = key
        node.writeValue = (
            storeValue(node.context, value)
            if node.writeValue is UNDEFINED
            else replaceStoredValue(node.context, node.writeValue, value)
        )
        return
    node.writes = {}
    node.writes[node.writeKey] = node.writeValue
    node.writes[key] = storeValue(node.context, value)
    node.writeKey = None
    node.writeValue = UNDEFINED


def deleteObjectWrite(node: _OverlayNode, key: str) -> None:
    if node.writeKey == key:
        node.writeKey = None
        releaseStoredValue(node.context, node.writeValue)
        node.writeValue = UNDEFINED
    else:
        reference = node.writes.get(key, UNDEFINED) if node.writes is not None else UNDEFINED
        if reference is not UNDEFINED:
            releaseStoredValue(node.context, reference)
        if node.writes is not None:
            node.writes.pop(key, None)


def isObjectDeleted(node: _OverlayNode, key: str) -> bool:
    return node.deleteKey == key or (node.deletes is not None and key in node.deletes)


def setObjectDeletion(node: _OverlayNode, key: str) -> None:
    if node.deletes is not None:
        node.deletes[key] = None
        return
    if node.deleteKey is None or node.deleteKey == key:
        node.deleteKey = key
        return
    node.deletes = {node.deleteKey: None, key: None}
    node.deleteKey = None


def deleteObjectDeletion(node: _OverlayNode, key: str) -> None:
    if node.deleteKey == key:
        node.deleteKey = None
    elif node.deletes is not None:
        node.deletes.pop(key, None)


def objectHas(node: _OverlayNode, key: str) -> bool:
    if isObjectDeleted(node, key):
        return False
    return hasObjectWrite(node, key) or key in nodeBase(node)


def objectValue(node: _OverlayNode, key: str) -> Any:
    if node.writeKey == key:
        return storedValue(node.context, node.writeValue)
    reference = node.writes.get(key, UNDEFINED) if node.writes is not None else UNDEFINED
    if reference is not UNDEFINED:
        return storedValue(node.context, reference)
    return nodeBase(node).get(key, UNDEFINED)


def markDirty(node: _OverlayNode) -> None:
    if node.dirty:
        return
    node.dirty = True
    node.context.dirty.append(node)
    parent = nodeParent(node)
    while parent is not None:
        parent.subtreeDirty = True
        parent = nodeParent(parent)


# ─── Array overlays: a treap of pieces over the base array and the inserted runs ─────────────────────


def arrayOverlay(node: _OverlayNode) -> _ArrayOverlay:
    if node.array is not None:
        return node.array
    base = nodeBase(node)
    overlay = _ArrayOverlay()
    if len(base) > 0:
        overlay.root = createPieceNode(overlay, _Piece("base", 0, len(base), 1))
    node.array = overlay
    return overlay


def baseOverridesForWrite(overlay: _ArrayOverlay) -> dict[int, Any]:
    if overlay.baseOverrides is None:
        overlay.baseOverrides = {}
    return overlay.baseOverrides


def insertOverridesForWrite(overlay: _ArrayOverlay) -> dict[_InsertSource, dict[int, Any]]:
    if overlay.insertOverrides is None:
        overlay.insertOverrides = {}
    return overlay.insertOverrides


def nextPiecePriority(overlay: _ArrayOverlay) -> int:
    # xorshift32 over unsigned 32-bit values: the low 32 bits equal the signed arithmetic upstream runs.
    value = overlay.seed
    value ^= (value << 13) & 0xFFFFFFFF
    value ^= value >> 17
    value ^= (value << 5) & 0xFFFFFFFF
    overlay.seed = value & 0xFFFFFFFF
    return overlay.seed


def treeElements(node: _PieceNode | None) -> int:
    return node.elements if node is not None else 0


def updatePieceNode(node: _PieceNode) -> None:
    node.elements = treeElements(node.left) + node.piece.length + treeElements(node.right)


def createPieceNode(overlay: _ArrayOverlay, piece: _Piece) -> _PieceNode:
    return _PieceNode(piece, nextPiecePriority(overlay))


def mergePieceTrees(left: _PieceNode | None, right: _PieceNode | None) -> _PieceNode | None:
    if left is None:
        return right
    if right is None:
        return left
    if left.priority >= right.priority:
        left.right = mergePieceTrees(left.right, right)
        updatePieceNode(left)
        return left
    right.left = mergePieceTrees(left, right.left)
    updatePieceNode(right)
    return right


def splitPieceTree(
    overlay: _ArrayOverlay, root: _PieceNode | None, index: int
) -> tuple[_PieceNode | None, _PieceNode | None]:
    if root is None:
        return None, None
    leftLength = treeElements(root.left)
    if index < leftLength:
        left, right = splitPieceTree(overlay, root.left, index)
        root.left = right
        updatePieceNode(root)
        return left, root
    pieceEnd = leftLength + root.piece.length
    if index > pieceEnd:
        left, right = splitPieceTree(overlay, root.right, index - pieceEnd)
        root.right = left
        updatePieceNode(root)
        return root, right
    if index == leftLength:
        left = root.left
        root.left = None
        updatePieceNode(root)
        return left, root
    if index == pieceEnd:
        right = root.right
        root.right = None
        updatePieceNode(root)
        return root, right
    offset = index - leftLength
    first = root.piece.copy(length=offset)
    second = root.piece.copy(start=root.piece.start + root.piece.step * offset, length=root.piece.length - offset)
    return (
        mergePieceTrees(root.left, createPieceNode(overlay, first)),
        mergePieceTrees(createPieceNode(overlay, second), root.right),
    )


def leftmostPieceNode(node: _PieceNode) -> _PieceNode:
    while node.left is not None:
        node = node.left
    return node


def rightmostPieceNode(node: _PieceNode) -> _PieceNode:
    while node.right is not None:
        node = node.right
    return node


def mergeablePieces(left: _Piece, right: _Piece) -> bool:
    if left.kind != right.kind:
        return False
    if left.kind == "insert" and left.source is not right.source:
        return False
    if left.length == 1 and right.length == 1:
        return abs(right.start - left.start) == 1
    return left.step == right.step and left.start + left.step * left.length == right.start


def joinNormalized(overlay: _ArrayOverlay, left: _PieceNode | None, right: _PieceNode | None) -> _PieceNode | None:
    if left is None:
        return right
    if right is None:
        return left
    leftPiece = rightmostPieceNode(left).piece
    rightPiece = leftmostPieceNode(right).piece
    if not mergeablePieces(leftPiece, rightPiece):
        return mergePieceTrees(left, right)
    leftRest, _ = splitPieceTree(overlay, left, treeElements(left) - leftPiece.length)
    _, rightRest = splitPieceTree(overlay, right, rightPiece.length)
    step = rightPiece.start - leftPiece.start if leftPiece.length == 1 else leftPiece.step
    combined = _Piece(
        leftPiece.kind, leftPiece.start, leftPiece.length + rightPiece.length, step, leftPiece.source
    )
    return joinNormalized(overlay, joinNormalized(overlay, leftRest, createPieceNode(overlay, combined)), rightRest)


def flattenPieceTree(node: _PieceNode | None, output: list[_Piece]) -> None:
    if node is None:
        return
    flattenPieceTree(node.left, output)
    output.append(node.piece)
    flattenPieceTree(node.right, output)


def piecesOf(overlay: _ArrayOverlay) -> list[_Piece]:
    if overlay.pieces is None:
        overlay.pieces = []
        flattenPieceTree(overlay.root, overlay.pieces)
    return overlay.pieces


def treeFromPieces(overlay: _ArrayOverlay, pieces: list[_Piece]) -> _PieceNode | None:
    mergePieces(pieces)
    root: _PieceNode | None = None
    for piece in pieces:
        root = mergePieceTrees(root, createPieceNode(overlay, piece))
    return root


def replaceAllPieces(overlay: _ArrayOverlay, pieces: list[_Piece]) -> None:
    overlay.root = treeFromPieces(overlay, pieces)
    overlay.pieces = None
    overlay.baseLocations = None
    overlay.insertLocations = None


def arrayLength(overlay: _ArrayOverlay) -> int:
    return treeElements(overlay.root)


def locatePiece(overlay: _ArrayOverlay, index: int) -> _Piece:
    node = overlay.root
    while node is not None:
        leftLength = treeElements(node.left)
        if index < leftLength:
            node = node.left
        elif index >= leftLength + node.piece.length:
            index -= leftLength + node.piece.length
            node = node.right
        else:
            overlay.locatedOffset = index - leftLength
            return node.piece
    raise RangeError("Array overlay index is out of range")


def arrayIndex(property: Any) -> int | None:
    """The array index a property names, or ``None``: an integer below 2**32 - 1, as the canonical digits of
    a string or as an ``int``."""
    if isinstance(property, bool):
        return None
    if isinstance(property, int):
        return property if 0 <= property < 4_294_967_295 else None
    if not isinstance(property, str) or len(property) == 0 or len(property) > 10:
        return None
    if property == "0":
        return 0
    if not ("1" <= property[0] <= "9") or not property.isascii() or not property.isdigit():
        return None
    index = int(property)
    return index if index < 4_294_967_295 else None


def entryValueAt(node: _OverlayNode, piece: _Piece, sourceIndex: int) -> Any:
    overlay = arrayOverlay(node)
    if piece.kind == "base":
        valueIndex = overlay.baseOverrides.get(sourceIndex, UNDEFINED) if overlay.baseOverrides is not None else UNDEFINED
        return nodeBase(node)[sourceIndex] if valueIndex is UNDEFINED else storedValue(node.context, valueIndex)
    assert piece.source is not None
    overrides = overlay.insertOverrides.get(piece.source) if overlay.insertOverrides is not None else None
    reference = (
        overrides[sourceIndex] if overrides is not None and sourceIndex in overrides else piece.source.refs[sourceIndex]
    )
    return storedValue(node.context, reference)


def hasEntryOverrideAt(overlay: _ArrayOverlay, piece: _Piece, sourceIndex: int) -> bool:
    if piece.kind == "base":
        return overlay.baseOverrides is not None and sourceIndex in overlay.baseOverrides
    if overlay.insertOverrides is None:
        return False
    overrides = overlay.insertOverrides.get(piece.source)  # type: ignore[arg-type]
    return overrides is not None and sourceIndex in overrides


def extendRightmostPiece(node: _PieceNode, amount: int) -> None:
    if node.right is not None:
        extendRightmostPiece(node.right, amount)
    else:
        node.piece.length += amount
    updatePieceNode(node)


def invalidatePieceCaches(overlay: _ArrayOverlay) -> None:
    overlay.pieces = None
    overlay.baseLocations = None
    overlay.insertLocations = None
    overlay.plan = None


def replacePieceRange(node: _OverlayNode, index: int, remove: int, inserted: list[_Piece]) -> None:
    if remove == 0 and len(inserted) == 0:
        return
    overlay = arrayOverlay(node)
    if remove == 0 and index == arrayLength(overlay) and len(inserted) == 1 and overlay.root is not None:
        addition = inserted[0]
        tail = rightmostPieceNode(overlay.root).piece
        if (
            addition.kind == "insert"
            and tail.kind == "insert"
            and tail.step == 1
            and tail.start + tail.length == len(tail.source.refs)  # type: ignore[union-attr]
        ):
            for offset in range(addition.length):
                tail.source.refs.append(addition.source.refs[addition.start + offset])  # type: ignore[union-attr]
            extendRightmostPiece(overlay.root, addition.length)
            invalidatePieceCaches(overlay)
            overlay.structural = True
            overlay.generation += 1
            markDirty(node)
            return
    left, rest = splitPieceTree(overlay, overlay.root, index)
    _, right = splitPieceTree(overlay, rest, remove)
    middle = treeFromPieces(overlay, inserted)
    overlay.root = joinNormalized(overlay, joinNormalized(overlay, left, middle), right)
    invalidatePieceCaches(overlay)
    overlay.structural = True
    overlay.generation += 1
    markDirty(node)


def mergePieces(pieces: list[_Piece]) -> None:
    index = 1
    while index < len(pieces):
        left = pieces[index - 1]
        right = pieces[index]
        sameSource = left.kind == right.kind and (left.kind == "base" or left.source is right.source)
        if sameSource and left.length == 1 and right.length == 1 and abs(right.start - left.start) == 1:
            left.step = right.start - left.start
            left.length = 2
            del pieces[index]
        elif sameSource and left.step == right.step and left.start + left.step * left.length == right.start:
            left.length += right.length
            del pieces[index]
        else:
            index += 1


def insertPiece(node: _OverlayNode, items: list[Any], start: int = 0) -> list[_Piece]:
    if len(items) == start:
        return []
    refs = [storeValue(node.context, items[index]) for index in range(start, len(items))]
    return [_Piece("insert", 0, len(refs), 1, _InsertSource(refs))]


def insertPlacementPiece(node: _OverlayNode, values: list[Any], start: int = 0) -> list[_Piece]:
    if len(values) == start:
        return []
    assert node.context.stored is not None
    storedLength = len(node.context.stored)
    try:
        for index in range(start, len(values)):
            values[index] = storeValue(node.context, clonePlacement(values[index]))
    except BaseException:
        del node.context.stored[storedLength:]
        raise
    return [_Piece("insert", start, len(values) - start, 1, _InsertSource(values))]


def setArrayIndex(node: _OverlayNode, index: int, stored: Any) -> None:
    overlay = arrayOverlay(node)
    length = arrayLength(overlay)
    if index == length:
        replacePieceRange(node, length, 0, insertPiece(node, [stored]))
        return
    piece = locatePiece(overlay, index)
    sourceIndex = piece.start + piece.step * overlay.locatedOffset
    current = entryValueAt(node, piece, sourceIndex)
    if not isContainer(stored) and strictEquals(current, stored):
        return
    if piece.kind == "base":
        existingIndex = (
            overlay.baseOverrides.get(sourceIndex, UNDEFINED) if overlay.baseOverrides is not None else UNDEFINED
        )
        if not isContainer(stored) and strictEquals(stored, nodeBase(node)[sourceIndex]):
            if existingIndex is not UNDEFINED:
                releaseStoredValue(node.context, existingIndex)
            if overlay.baseOverrides is not None:
                overlay.baseOverrides.pop(sourceIndex, None)
        elif existingIndex is UNDEFINED:
            baseOverridesForWrite(overlay)[sourceIndex] = storeValue(node.context, stored)
        else:
            assert overlay.baseOverrides is not None
            overlay.baseOverrides[sourceIndex] = replaceStoredValue(node.context, existingIndex, stored)
    else:
        source = piece.source
        assert source is not None
        overrides = overlay.insertOverrides.get(source) if overlay.insertOverrides is not None else None
        existingIndex = overrides.get(sourceIndex, UNDEFINED) if overrides is not None else UNDEFINED
        if not isContainer(stored) and strictEquals(stored, storedValue(node.context, source.refs[sourceIndex])):
            if existingIndex is not UNDEFINED:
                releaseStoredValue(node.context, existingIndex)
            if overrides is not None:
                overrides.pop(sourceIndex, None)
                if len(overrides) == 0 and overlay.insertOverrides is not None:
                    overlay.insertOverrides.pop(source, None)
        else:
            if overrides is None:
                overrides = {}
                insertOverridesForWrite(overlay)[source] = overrides
            if existingIndex is UNDEFINED:
                overrides[sourceIndex] = storeValue(node.context, stored)
            else:
                overrides[sourceIndex] = replaceStoredValue(node.context, existingIndex, stored)
    markDirty(node)


def setArrayLength(node: _OverlayNode, next: int) -> None:
    current = arrayLength(arrayOverlay(node))
    if next == current:
        return
    if next < current:
        replacePieceRange(node, next, current - next, [])
    else:
        replacePieceRange(node, current, 0, insertPiece(node, [None] * (next - current)))


def toArrayLength(value: Any) -> int:
    number = jsNumber(value)
    if not isinstance(number, int) and not (isinstance(number, float) and number.is_integer()):
        raise RangeError("Invalid array length")
    if number < 0 or number >= 4_294_967_296:
        raise RangeError("Invalid array length")
    return int(number)


def toIntegerOrInfinity(value: Any) -> int | float:
    number = jsNumber(value)
    if isinstance(number, float) and math.isnan(number):
        return 0
    if number == 0:
        return 0
    return math.trunc(number) if math.isfinite(number) else number


def clampIndex(value: float, length: int) -> int:
    if value == -math.inf:
        return 0
    if value < 0:
        return max(length + int(value), 0)
    return int(min(value, length))


def mutatorNode(receiver: DraftArray) -> _OverlayNode:
    node = receiver._target()
    assertWritable(node.context)
    return node


# MISAKA fork: `comparator(left, right)` may answer anything `Number()` accepts; NaN counts as 0.
def _compareResult(value: Any) -> int:
    number = jsNumber(value)
    if isinstance(number, float) and math.isnan(number):
        return 0
    return -1 if number < 0 else 1 if number > 0 else 0


def _defaultSortString(value: Any) -> str:
    """``String(value)`` for the default comparator: primitives as JavaScript prints them, an object as
    ``[object Object]``, an array as its entries joined by commas (``null`` is empty)."""
    if isinstance(value, DraftArray):
        return ",".join("" if entry is None or entry is UNDEFINED else _defaultSortString(entry) for entry in value)
    if isinstance(value, DraftObject):
        return "[object Object]"
    return jsString(value)


# MISAKA fork: the body of upstream's array mutator `sort`, as a function `DraftArray.sort` calls.
def arraySort(node: _OverlayNode, comparator: Callable[[Any, Any], Any] | None) -> DraftArray:
    if comparator is not None and not callable(comparator):
        raise TypeError("Comparator must be a function")
    overlay = arrayOverlay(node)
    insertedSources: list[_InsertSource] = []
    insertedIndices: list[int] = []
    insertedValues: list[Any] = []
    baseValues: list[Any] = [UNDEFINED] * len(nodeBase(node))
    order: list[int] = []
    for piece in piecesOf(overlay):
        for offset in range(piece.length):
            sourceIndex = piece.start + piece.step * offset
            if piece.kind == "base":
                order.append(sourceIndex)
                baseValues[sourceIndex] = publicSortValue(node, sourceIndex, insertedSources, insertedIndices)
            else:
                assert piece.source is not None
                insertedSources.append(piece.source)
                insertedIndices.append(sourceIndex)
                token = -len(insertedSources)
                order.append(token)
                insertedValues.append(publicSortValue(node, token, insertedSources, insertedIndices))
    baseSnapshot: dict[int, Any] = {}
    for index, valueIndex in (overlay.baseOverrides or {}).items():
        baseSnapshot[index] = storedValue(node.context, valueIndex)
    insertSnapshots: dict[_InsertSource, dict[int, Any]] = {}
    for source, overrides in (overlay.insertOverrides or {}).items():
        snapshot: dict[int, Any] = {}
        for index, valueIndex in overrides.items():
            snapshot[index] = storedValue(node.context, valueIndex)
        insertSnapshots[source] = snapshot
    generation = overlay.generation

    def compare(left: int, right: int) -> int:
        leftValue = insertedValues[-left - 1] if left < 0 else baseValues[left]
        rightValue = insertedValues[-right - 1] if right < 0 else baseValues[right]
        if comparator is not None:
            return _compareResult(comparator(leftValue, rightValue))
        # ``a < b`` on strings compares UTF-16 code units.
        a = toUtf16Units(_defaultSortString(leftValue))
        b = toUtf16Units(_defaultSortString(rightValue))
        return -1 if a < b else 1 if a > b else 0

    order.sort(key=functools.cmp_to_key(compare))
    if (
        len(baseSnapshot) > 0
        or len(insertSnapshots) > 0
        or len(overlay.baseOverrides or {}) > 0
        or len(overlay.insertOverrides or {}) > 0
    ):
        for token in order:
            restoreSortOverride(node, token, insertedSources, insertedIndices, baseSnapshot, insertSnapshots)
    comparatorWasStructural = overlay.generation != generation
    currentLength = arrayLength(overlay)
    samePrefix = currentLength >= len(order) and all(
        sameSortTokenAt(overlay, index, token, insertedSources, insertedIndices) for index, token in enumerate(order)
    )
    if not samePrefix:
        replacePieceRange(
            node, 0, min(len(order), currentLength), piecesFromSortOrder(order, insertedSources, insertedIndices)
        )
    if comparatorWasStructural:
        deduplicateArrayEntries(node)
    return node.proxy  # type: ignore[return-value]


def sortTokenSource(token: int, sources: Sequence[_InsertSource]) -> _InsertSource | None:
    return sources[-token - 1] if token < 0 else None


def sortTokenIndex(token: int, indices: Sequence[int]) -> int:
    return indices[-token - 1] if token < 0 else token


def publicSortValue(node: _OverlayNode, token: int, sources: Sequence[_InsertSource], indices: Sequence[int]) -> Any:
    overlay = arrayOverlay(node)
    source = sortTokenSource(token, sources)
    sourceIndex = sortTokenIndex(token, indices)
    if source is None:
        overrideIndex = (
            overlay.baseOverrides.get(sourceIndex, UNDEFINED) if overlay.baseOverrides is not None else UNDEFINED
        )
    else:
        overrides = overlay.insertOverrides.get(source) if overlay.insertOverrides is not None else None
        overrideIndex = overrides.get(sourceIndex, UNDEFINED) if overrides is not None else UNDEFINED
    if overrideIndex is not UNDEFINED:
        value = storedValue(node.context, overrideIndex)
    elif source is None:
        value = nodeBase(node)[sourceIndex]
    else:
        value = storedValue(node.context, source.refs[sourceIndex])
    if not isContainer(value):
        return value
    return createNode(
        node.context,
        value,
        node,
        1 if source is None else 2,
        sourceIndex,
        source,
        source is not None or (overlay.baseOverrides is not None and sourceIndex in overlay.baseOverrides),
    ).proxy


def restoreSortOverride(
    node: _OverlayNode,
    token: int,
    sources: Sequence[_InsertSource],
    indices: Sequence[int],
    baseSnapshot: dict[int, Any],
    insertSnapshots: dict[_InsertSource, dict[int, Any]],
) -> None:
    overlay = arrayOverlay(node)
    source = sortTokenSource(token, sources)
    sourceIndex = sortTokenIndex(token, indices)
    if source is None:
        existingIndex = (
            overlay.baseOverrides.get(sourceIndex, UNDEFINED) if overlay.baseOverrides is not None else UNDEFINED
        )
        snapshotValue = baseSnapshot.get(sourceIndex, UNDEFINED)
        if snapshotValue is not UNDEFINED:
            if existingIndex is UNDEFINED:
                baseOverridesForWrite(overlay)[sourceIndex] = storeValue(node.context, snapshotValue)
            else:
                assert overlay.baseOverrides is not None
                overlay.baseOverrides[sourceIndex] = replaceStoredValue(node.context, existingIndex, snapshotValue)
        else:
            if existingIndex is not UNDEFINED:
                releaseStoredValue(node.context, existingIndex)
            if overlay.baseOverrides is not None:
                overlay.baseOverrides.pop(sourceIndex, None)
        return
    overrides = overlay.insertOverrides.get(source) if overlay.insertOverrides is not None else None
    existingIndex = overrides.get(sourceIndex, UNDEFINED) if overrides is not None else UNDEFINED
    snapshotValue = insertSnapshots.get(source, {}).get(sourceIndex, UNDEFINED)
    if snapshotValue is not UNDEFINED:
        if overrides is None:
            overrides = {}
            insertOverridesForWrite(overlay)[source] = overrides
        if existingIndex is UNDEFINED:
            overrides[sourceIndex] = storeValue(node.context, snapshotValue)
        else:
            overrides[sourceIndex] = replaceStoredValue(node.context, existingIndex, snapshotValue)
    else:
        if existingIndex is not UNDEFINED:
            releaseStoredValue(node.context, existingIndex)
        if overrides is not None:
            overrides.pop(sourceIndex, None)
            if len(overrides) == 0 and overlay.insertOverrides is not None:
                overlay.insertOverrides.pop(source, None)


def sameSortTokenAt(
    overlay: _ArrayOverlay,
    logicalIndex: int,
    token: int,
    sources: Sequence[_InsertSource],
    indices: Sequence[int],
) -> bool:
    piece = locatePiece(overlay, logicalIndex)
    sourceIndex = piece.start + piece.step * overlay.locatedOffset
    tokenSource = sortTokenSource(token, sources)
    return sourceIndex == sortTokenIndex(token, indices) and (
        (piece.kind == "base" and tokenSource is None) or (piece.kind == "insert" and piece.source is tokenSource)
    )


def deduplicateArrayEntries(node: _OverlayNode) -> None:
    overlay = arrayOverlay(node)
    seenBase: set[int] = set()
    seenInsert: dict[_InsertSource, set[int]] = {}
    next_: list[_Piece] = []
    duplicated = False
    for piece in piecesOf(overlay):
        for offset in range(piece.length):
            sourceIndex = piece.start + piece.step * offset
            if piece.kind == "base":
                seen = sourceIndex in seenBase
                seenBase.add(sourceIndex)
            else:
                assert piece.source is not None
                indices = seenInsert.get(piece.source)
                if indices is None:
                    indices = set()
                    seenInsert[piece.source] = indices
                seen = sourceIndex in indices
                indices.add(sourceIndex)
            if seen:
                duplicated = True
                appendMergedPiece(
                    next_,
                    insertPiece(node, [clonePlacementStored(entryValueAt(node, piece, sourceIndex), node.context)])[0],
                )
            else:
                appendMergedPiece(next_, singletonPiece(piece, sourceIndex))
    if not duplicated:
        return
    replaceAllPieces(overlay, next_)
    overlay.structural = True
    overlay.generation += 1
    overlay.plan = None
    markDirty(node)


def piecesFromSortOrder(order: Sequence[int], sources: Sequence[_InsertSource], indices: Sequence[int]) -> list[_Piece]:
    pieces: list[_Piece] = []
    for token in order:
        source = sources[-token - 1] if token < 0 else None
        sourceIndex = indices[-token - 1] if token < 0 else token
        previous = pieces[-1] if pieces else None
        sameSource = previous is not None and (
            (source is None and previous.kind == "base")
            or (source is not None and previous.kind == "insert" and previous.source is source)
        )
        if previous is not None and sameSource:
            if previous.length == 1:
                step = sourceIndex - previous.start
                if step in (1, -1):
                    previous.step = step
                    previous.length = 2
                    continue
            elif previous.start + previous.step * previous.length == sourceIndex:
                previous.length += 1
                continue
        pieces.append(
            _Piece("base", sourceIndex, 1, 1) if source is None else _Piece("insert", sourceIndex, 1, 1, source)
        )
    return pieces


def appendMergedPiece(pieces: list[_Piece], piece: _Piece) -> None:
    previous = pieces[-1] if pieces else None
    sameSource = (
        previous is not None
        and previous.kind == piece.kind
        and (previous.kind == "base" or previous.source is piece.source)
    )
    if previous is not None and sameSource and previous.length == 1 and piece.length == 1:
        step = piece.start - previous.start
        if step in (1, -1):
            previous.step = step
            previous.length = 2
            return
    if (
        previous is not None
        and sameSource
        and previous.step == piece.step
        and previous.start + previous.step * previous.length == piece.start
    ):
        previous.length += piece.length
    else:
        pieces.append(piece)


def singletonPiece(piece: _Piece, sourceIndex: int) -> _Piece:
    return (
        _Piece("base", sourceIndex, 1, 1)
        if piece.kind == "base"
        else _Piece("insert", sourceIndex, 1, 1, piece.source)
    )


def clonePlacement(value: Any) -> Any:
    if isinstance(value, DraftObject | DraftArray):
        return clonePlacementNode(value._target())
    return copyJson(value)


def cloneNode(node: _OverlayNode) -> Any:
    assertReadable(node.context)
    if isinstance(nodeBase(node), list):
        overlay = arrayOverlay(node)
        items: list[Any] = []
        for piece in piecesOf(overlay):
            for offset in range(piece.length):
                sourceIndex = piece.start + piece.step * offset
                items.append(cloneStored(entryValueAt(node, piece, sourceIndex), node.context))
        return items
    result: dict[str, Any] = {}
    for key in ownKeys(node):
        result[key] = cloneStored(objectValue(node, key), node.context)
    return result


def cloneStored(value: Any, context: _OverlayContext) -> Any:
    if not isContainer(value):
        return value
    assert context.rawNodes is not None
    node = context.rawNodes.get(id(value))
    return value if node is None or (not node.dirty and not node.subtreeDirty) else cloneNode(node)


def clonePlacementNode(node: _OverlayNode) -> Any:
    assertReadable(node.context)
    if isinstance(nodeBase(node), list):
        overlay = arrayOverlay(node)
        items: list[Any] = []
        for piece in piecesOf(overlay):
            for offset in range(piece.length):
                sourceIndex = piece.start + piece.step * offset
                items.append(clonePlacementStored(entryValueAt(node, piece, sourceIndex), node.context))
        return items
    result: dict[str, Any] = {}
    for key in ownKeys(node):
        result[key] = clonePlacementStored(objectValue(node, key), node.context)
    return result


def clonePlacementStored(value: Any, context: _OverlayContext) -> Any:
    if not isContainer(value):
        return copyJson(value)
    assert context.rawNodes is not None
    node = context.rawNodes.get(id(value))
    return copyJson(value) if node is None else clonePlacementNode(node)


def _ancestors(node: _OverlayNode):
    parent = nodeParent(node)
    while parent is not None:
        yield parent
        parent = nodeParent(parent)


def _reservedIndex(path: Path) -> int:
    for index, segment in enumerate(path):
        if isinstance(segment, str) and segment in RESERVED_SEGMENTS:
            return index
    return -1


def emitOperations(context: _OverlayContext) -> list[Op]:
    simple = emitSimpleObjectOperations(context)
    if simple is not None:
        return simple
    operations: list[Op] = []
    forcedFolds: dict[_OverlayNode, Path] = {}
    emissionPaths: dict[_OverlayNode, Path] = {}
    denseIndices: dict[_OverlayNode, _DenseCandidates] = {}
    for node in context.dirty:
        recordDenseArrayPosition(node, denseIndices)
        if isinstance(nodeBase(node), list):
            overlay = arrayOverlay(node)
            if not overlay.structural and overlay.baseOverrides is not None and len(overlay.baseOverrides) > 0:
                candidates = denseIndices.get(node)
                if candidates is None:
                    candidates = _DenseCandidates(len(nodeBase(node)))
                    denseIndices[node] = candidates
                for index in overlay.baseOverrides:
                    addDenseCandidate(candidates, index)
    denseRegions: dict[_OverlayNode, list[_DenseRegion]] = {}
    for array, candidates in denseIndices.items():
        path = resolvePath(array)
        if path is None or _reservedIndex(path) >= 0:
            continue
        regions = buildDenseRegions(candidates)
        if len(regions) > 0:
            denseRegions[array] = regions
    for node in context.dirty:
        path = resolvePath(node)
        if path is None:
            continue
        if hasCoveringDenseRegion(node, path, denseRegions):
            continue
        emissionPaths[node] = path
        if not isinstance(nodeBase(node), list) and hasReservedMutation(node):
            forcedFolds[node] = path
        reservedAt = _reservedIndex(path)
        if reservedAt >= 0:
            ancestor = node
            for _depth in range(len(path), reservedAt, -1):
                ancestor = nodeParent(ancestor)  # type: ignore[assignment]
            forcedFolds[ancestor] = path[:reservedAt]
    for node, foldPath in forcedFolds.items():
        emissionPaths[node] = foldPath
    for node in denseRegions:
        path = resolvePath(node)
        if path is not None and not hasCoveringDenseRegion(node, path, denseRegions):
            emissionPaths[node] = path
    maxDepth = 0
    for emissionPath in emissionPaths.values():
        maxDepth = max(maxDepth, len(emissionPath))
    buckets: list[list[_OverlayNode]] = [[] for _ in range(maxDepth + 1)]
    for node, emissionPath in emissionPaths.items():
        buckets[len(emissionPath)].append(node)
    folded: set[_OverlayNode] = set()
    for bucket in buckets:
        for node in bucket:
            emissionPath = emissionPaths[node]
            if hasPlacementAncestor(node) or hasFoldedAncestor(node, folded):
                continue
            if node in forcedFolds:
                emitSet(operations, emissionPath, cloneNode(node))
                folded.add(node)
                continue
            if isinstance(nodeBase(node), list):
                emitArrayOperations(node, emissionPath, operations, denseRegions.get(node))
            else:
                emitObjectOperations(node, emissionPath, operations)
            if len(operations) > MAX_DELTA_OPERATIONS:
                assert context.root is not None
                return [["r", cloneNode(context.root)]]
    return operations


def emitSimpleObjectOperations(context: _OverlayContext) -> list[Op] | None:
    nodes: list[_OverlayNode] = []
    for node in context.dirty:
        if isinstance(nodeBase(node), list) or hasReservedMutation(node) or hasPlacementAncestor(node):
            return None
        for parent in _ancestors(node):
            if isinstance(nodeBase(parent), list):
                return None
        path = resolvePath(node)
        if path is None:
            continue
        for segment in path:
            if isinstance(segment, str) and segment in RESERVED_SEGMENTS:
                return None
        nodes.append(node)
        if len(nodes) > MAX_SIMPLE_OBJECT_NODES:
            return None
    # Stable insertion sort avoids comparator/bucket allocations for the usual
    # handful of dirty object nodes.
    for index in range(1, len(nodes)):
        node = nodes[index]
        depth = len(node.preparedPath)  # type: ignore[arg-type]
        at = index
        while at > 0 and len(nodes[at - 1].preparedPath) > depth:  # type: ignore[arg-type]
            nodes[at] = nodes[at - 1]
            at -= 1
        nodes[at] = node
    operations: list[Op] = []
    canMaterializeDirectly = True
    for node in nodes:
        assert node.preparedPath is not None
        if emitObjectOperations(node, node.preparedPath, operations):
            canMaterializeDirectly = False
        if len(operations) > MAX_DELTA_OPERATIONS:
            assert context.root is not None
            return [["r", cloneNode(context.root)]]
    context.simpleObjectMaterialization = canMaterializeDirectly
    return operations


# MISAKA fork: upstream answers through two module-level variables (`locatedDenseArray`, `locatedDenseIndex`)
# it clears when a context is released; the pair is returned instead, so nothing retains a context.
def locateDenseArrayPosition(node: _OverlayNode) -> tuple[_OverlayNode, int] | None:
    child = node
    parent = nodeParent(child)
    while parent is not None:
        if isinstance(nodeBase(parent), list):
            overlay = arrayOverlay(parent)
            if child.parentKind != 1 or overlay.structural:
                return None
            sourceIndex = int(child.parentKey)
            valueIndex = overlay.baseOverrides.get(sourceIndex, UNDEFINED) if overlay.baseOverrides is not None else UNDEFINED
            current = (
                nodeBase(parent)[sourceIndex] if valueIndex is UNDEFINED else storedValue(parent.context, valueIndex)
            )
            if current is not nodeBase(child):
                return None
            return parent, sourceIndex
        child = parent
        parent = nodeParent(child)
    return None


def hasCoveringDenseRegion(
    node: _OverlayNode, path: Path, denseRegions: dict[_OverlayNode, list[_DenseRegion]]
) -> bool:
    for parent in _ancestors(node):
        regions = denseRegions.get(parent)
        if regions is None:
            continue
        parentPath = resolvePath(parent)
        if parentPath is None or len(path) <= len(parentPath):
            continue
        matches = True
        for index in range(len(parentPath)):
            if path[index] != parentPath[index]:
                matches = False
                break
        if not matches:
            continue
        segment = path[len(parentPath)]
        if isinstance(segment, int) and regionContaining(regions, segment):
            return True
    return False


def addDenseCandidate(candidates: _DenseCandidates, index: int) -> None:
    if candidates.bits is not None:
        candidates.bits[index] = 1
        return
    candidates.indices.append(index)
    if len(candidates.indices) < 256:
        return
    candidates.bits = bytearray(candidates.length)
    for existing in candidates.indices:
        candidates.bits[existing] = 1
    candidates.indices.clear()


def recordDenseArrayPosition(node: _OverlayNode, groups: dict[_OverlayNode, _DenseCandidates]) -> None:
    located = locateDenseArrayPosition(node)
    if located is None:
        return
    array, index = located
    candidates = groups.get(array)
    if candidates is None:
        candidates = _DenseCandidates(len(nodeBase(array)))
        groups[array] = candidates
    addDenseCandidate(candidates, index)


def buildDenseRegions(candidates: _DenseCandidates) -> list[_DenseRegion]:
    if candidates.bits is None:
        return []
    regions: list[_DenseRegion] = []
    bits = candidates.bits
    at = 0
    while at < len(bits):
        while at < len(bits) and bits[at] == 0:
            at += 1
        if at == len(bits):
            break
        start = at
        end = at
        count = 0
        gap = 0
        while at < len(bits):
            if bits[at] != 0:
                count += 1
                end = at
                gap = 0
            else:
                gap += 1
                if gap > 1:
                    break
            at += 1
        length = end - start + 1
        if count >= 256 and count * 2 >= length:
            regions.append(_DenseRegion(start, length))
    return regions


def regionContaining(regions: Sequence[_DenseRegion], index: int) -> bool:
    for region in regions:
        if region.start <= index < region.start + region.length:
            return True
    return False


def hasReservedMutation(node: _OverlayNode) -> bool:
    if node.writeKey is not None and node.writeKey in RESERVED_SEGMENTS:
        return True
    if node.deleteKey is not None and node.deleteKey in RESERVED_SEGMENTS:
        return True
    if node.writes is not None:
        for key in node.writes:
            if key in RESERVED_SEGMENTS:
                return True
    if node.deletes is not None:
        for key in node.deletes:
            if key in RESERVED_SEGMENTS:
                return True
    return False


def hasFoldedAncestor(node: _OverlayNode, folded: set[_OverlayNode]) -> bool:
    for parent in _ancestors(node):
        if parent in folded:
            return True
    return False


def hasPlacementAncestor(node: _OverlayNode) -> bool:
    current: _OverlayNode | None = node
    while current is not None:
        if nodeParent(current) is not None and current.parentPlacement:
            return True
        current = nodeParent(current)
    return False


def emitObjectWrite(node: _OverlayNode, path: Path, operations: list[Op], key: str, value: Any) -> bool:
    nextPath = [*path, key]
    before = (
        UNDEFINED
        if (node.readded is not None and key in node.readded) or key not in nodeBase(node)
        else nodeBase(node)[key]
    )
    after = cloneStored(value, node.context)
    emitted = emitChangedValue(operations, nextPath, before, after)
    return isContainer(before) and isContainer(after) and not emitted


def emitObjectOperations(node: _OverlayNode, path: Path, operations: list[Op]) -> bool:
    # Immutable replay must encode delete-and-readd explicitly so string-key
    # insertion order matches the draft.
    if node.readded is not None:
        for key in node.readded:
            if key in nodeBase(node):
                operations.append(["d", [*path, key]])
    normalizedContainerWrite = False
    if node.writeKey is not None:
        normalizedContainerWrite = emitObjectWrite(
            node,
            path,
            operations,
            node.writeKey,
            objectValue(node, node.writeKey),
        )
    if node.writes is not None:
        for key, valueIndex in node.writes.items():
            if len(operations) > MAX_DELTA_OPERATIONS:
                return normalizedContainerWrite
            if emitObjectWrite(node, path, operations, key, storedValue(node.context, valueIndex)):
                normalizedContainerWrite = True
    if node.deleteKey is not None and node.deleteKey in nodeBase(node):
        operations.append(["d", [*path, node.deleteKey]])
    if node.deletes is not None:
        for key in node.deletes:
            if len(operations) > MAX_DELTA_OPERATIONS:
                return normalizedContainerWrite
            if key in nodeBase(node):
                operations.append(["d", [*path, key]])
    return normalizedContainerWrite


def buildArrayPlan(node: _OverlayNode) -> _ArrayPlan:
    overlay = arrayOverlay(node)
    if overlay.plan is not None:
        return overlay.plan
    base = nodeBase(node)
    pieces = piecesOf(overlay)
    plan = orderedArrayPlan(len(base), pieces)
    overlay.plan = plan if plan is not None else elementArrayPlan(len(base), pieces)
    return overlay.plan


def orderedArrayPlan(baseLength: int, pieces: Sequence[_Piece]) -> _ArrayPlan | None:
    """The plan when the retained base elements keep their order, the common case of inserts and removals: computed
    from the pieces alone, so appending to a long array costs no walk over its elements. ``None`` when base elements
    moved."""
    # Retained base ranges in piece order, `[first, last]` each; ordered means strictly ascending without overlap.
    ranges: list[int] = []
    for piece in pieces:
        if piece.kind != "base" or piece.length == 0:
            continue
        if piece.step != 1 and piece.length > 1:
            return None
        if len(ranges) > 0 and piece.start <= ranges[-1]:
            return None
        ranges.extend((piece.start, piece.start + piece.length - 1))
    # Unretained runs from the end backwards, as `start, length` pairs: after the last range, between ranges, before
    # the first.
    removeRuns: list[int] = []
    end = baseLength
    for index in range(len(ranges) - 2, -3, -2):
        start = ranges[index + 1] + 1 if index >= 0 else 0
        if end > start:
            removeRuns.extend((start, end - start))
        if index >= 0:
            end = ranges[index]
    return _ArrayPlan(removeRuns, None, insertRunsOf(pieces))


def elementArrayPlan(baseLength: int, pieces: Sequence[_Piece]) -> _ArrayPlan:
    """The plan from a walk over every base element, for pieces that moved base elements."""
    retained = bytearray(baseLength)
    targetBase: list[int] = []
    for piece in pieces:
        if piece.kind != "base":
            continue
        for offset in range(piece.length):
            index = piece.start + piece.step * offset
            retained[index] = 1
            targetBase.append(index)
    removeRuns: list[int] = []
    end = baseLength
    while end > 0:
        if retained[end - 1] != 0:
            end -= 1
            continue
        start = end - 1
        while start > 0 and retained[start - 1] == 0:
            start -= 1
        removeRuns.extend((start, end - start))
        end = start
    retainedBase = [index for index in range(len(retained)) if retained[index] != 0]
    permutation: list[int] | None = None
    if any(index >= len(retainedBase) or value != retainedBase[index] for index, value in enumerate(targetBase)):
        positions = {value: index for index, value in enumerate(retainedBase)}
        permutation = [positions[value] for value in targetBase]
    return _ArrayPlan(removeRuns, permutation, insertRunsOf(pieces))


def insertRunsOf(pieces: Sequence[_Piece]) -> list[int]:
    """Runs of consecutive insert pieces as ``logical index, first piece, end piece`` triples."""
    insertRuns: list[int] = []
    logicalIndex = 0
    pieceIndex = 0
    while pieceIndex < len(pieces):
        piece = pieces[pieceIndex]
        if piece.kind == "base":
            logicalIndex += piece.length
            pieceIndex += 1
            continue
        startPiece = pieceIndex
        while pieceIndex < len(pieces) and pieces[pieceIndex].kind == "insert":
            logicalIndex += pieces[pieceIndex].length
            pieceIndex += 1
        insertRuns.extend((logicalIndex - rangeLength(pieces, startPiece, pieceIndex), startPiece, pieceIndex))
    return insertRuns


def rangeLength(pieces: Sequence[_Piece], start: int, end: int) -> int:
    length = 0
    for index in range(start, end):
        length += pieces[index].length
    return length


def emitArrayOperations(
    node: _OverlayNode, path: Path, operations: list[Op], denseRegions: Sequence[_DenseRegion] | None
) -> None:
    overlay = arrayOverlay(node)
    base = nodeBase(node)
    if denseRegions is not None:
        for region in denseRegions:
            if len(operations) > MAX_DELTA_OPERATIONS:
                return
            operations.append(["p", path, region.start, region.length, cloneArrayRegion(node, region)])
    if overlay.structural:
        plan = buildArrayPlan(node)
        for index in range(0, len(plan.removeRuns), 2):
            if len(operations) > MAX_DELTA_OPERATIONS:
                return
            operations.append(["p", path, plan.removeRuns[index], plan.removeRuns[index + 1], []])
        if plan.permutation is not None:
            operations.append(["m", path, plan.permutation])
        pieces = piecesOf(overlay)
        for run in range(0, len(plan.insertRuns), 3):
            if len(operations) > MAX_DELTA_OPERATIONS:
                return
            logicalIndex = plan.insertRuns[run]
            items: list[Any] = []
            for pieceIndex in range(plan.insertRuns[run + 1], plan.insertRuns[run + 2]):
                piece = pieces[pieceIndex]
                for offset in range(piece.length):
                    sourceIndex = piece.start + piece.step * offset
                    items.append(cloneStored(entryValueAt(node, piece, sourceIndex), node.context))
            operations.append(["p", path, logicalIndex, 0, items])
    if overlay.baseOverrides is not None:
        for baseIndex, valueIndex in list(overlay.baseOverrides.items()):
            if len(operations) > MAX_DELTA_OPERATIONS:
                return
            entryIndex = findEntryIndex(overlay, 1, baseIndex, None)
            if entryIndex is None or (denseRegions is not None and regionContaining(denseRegions, entryIndex)):
                continue
            emitChangedValue(
                operations,
                [*path, entryIndex],
                base[baseIndex],
                cloneStored(storedValue(node.context, valueIndex), node.context),
            )


def cloneArrayRegion(node: _OverlayNode, region: _DenseRegion) -> list[Any]:
    overlay = arrayOverlay(node)
    result: list[Any] = []
    for index in range(region.start, region.start + region.length):
        piece = locatePiece(overlay, index)
        sourceIndex = piece.start + piece.step * overlay.locatedOffset
        result.append(cloneStored(entryValueAt(node, piece, sourceIndex), node.context))
    return result


def emitChangedValue(operations: list[Op], path: NonEmptyPath, before: Any, after: Any) -> bool:
    if not isContainer(after) and strictEquals(before, after):
        return False
    if isContainer(before) and isContainer(after) and equalTrustedJson(before, after):
        return False
    if isinstance(before, str) and isinstance(after, str):
        beforeLength = jsLength(before)
        # Slice/equality stays on V8's rope fast path; startsWith scans the entire
        # accumulated prefix on every append.
        if jsLength(after) > beforeLength and jsSlice(after, 0, beforeLength) == before:
            operations.append(["a", path, jsSlice(after, beforeLength)])
            return True
        shared = overlap(before, after, 65_536)
        if shared > 0:
            operations.append(["t", path, beforeLength - shared])
            if jsLength(after) > shared:
                operations.append(["a", path, jsSlice(after, shared)])
            return True
    operations.append(["s", path, after])
    return True


def emitSet(operations: list[Op], path: Path, value: Any) -> None:
    if len(path) == 0:
        operations.append(["r", value])
    else:
        operations.append(["s", path, value])


def resolvePath(node: _OverlayNode) -> list[Any] | None:
    if node.preparedPath is not None:
        return node.preparedPath
    parent = nodeParent(node)
    if parent is None:
        node.preparedPath = []
        return node.preparedPath
    parentPath = resolvePath(parent)
    if parentPath is None:
        return None
    if node.parentKind == 0:
        key = str(node.parentKey)
        if not objectHas(parent, key) or objectValue(parent, key) is not nodeBase(node):
            return None
        node.preparedPath = [*parentPath, key]
        return node.preparedPath
    overlay = arrayOverlay(parent)
    index = findEntryIndex(overlay, node.parentKind, int(node.parentKey), node.parentSource)
    if index is None:
        return None
    piece = locatePiece(overlay, index)
    if entryValueAt(parent, piece, int(node.parentKey)) is not nodeBase(node):
        return None
    node.preparedPath = [*parentPath, index]
    return node.preparedPath


def ensurePieceLocations(overlay: _ArrayOverlay) -> None:
    if overlay.baseLocations is not None:
        return
    overlay.baseLocations = []
    overlay.insertLocations = {}
    logicalStart = 0
    for piece in piecesOf(overlay):
        last = piece.start + piece.step * (piece.length - 1)
        location = _PieceLocation(piece, logicalStart, min(piece.start, last), max(piece.start, last))
        if piece.kind == "base":
            overlay.baseLocations.append(location)
        else:
            assert piece.source is not None
            locations = overlay.insertLocations.get(piece.source)
            if locations is None:
                locations = []
                overlay.insertLocations[piece.source] = locations
            locations.append(location)
        logicalStart += piece.length
    overlay.baseLocations.sort(key=byMinimum)
    for locations in overlay.insertLocations.values():
        locations.sort(key=byMinimum)


def byMinimum(location: _PieceLocation) -> int:
    return location.minimum


def findEntryIndex(overlay: _ArrayOverlay, kind: int, sourceIndex: int, source: _InsertSource | None) -> int | None:
    if kind == 1 and not overlay.structural:
        return sourceIndex
    ensurePieceLocations(overlay)
    assert overlay.baseLocations is not None and overlay.insertLocations is not None
    locations = overlay.baseLocations if kind == 1 else overlay.insertLocations.get(source)  # type: ignore[arg-type]
    if locations is None:
        return None
    low = 0
    high = len(locations)
    while low < high:
        middle = (low + high) >> 1
        if locations[middle].minimum <= sourceIndex:
            low = middle + 1
        else:
            high = middle
    location = locations[low - 1] if low > 0 else None
    if location is None or sourceIndex > location.maximum:
        return None
    offset = (sourceIndex - location.piece.start) // location.piece.step
    return location.logicalStart + offset if 0 <= offset < location.piece.length else None


def ensureOperations(context: _OverlayContext) -> list[Op]:
    if context.ops is not None:
        return context.ops
    assertReadable(context)
    assert context.root is not None
    if context.replacement:
        tracker = context.tracker()
        base = tracker.replacementBase(context) if tracker is not None else UNDEFINED
        context.replacementNoop = base is not UNDEFINED and equalTrustedJson(base, nodeBase(context.root))
        context.ops = [] if context.replacementNoop else [["r", nodeBase(context.root)]]
    else:
        context.ops = emitOperations(context)
    return context.ops


def equalTrustedJson(left: Any, right: Any) -> bool:
    if strictEquals(left, right):
        return True
    if not isContainer(left) or not isContainer(right) or isinstance(left, list) != isinstance(right, list):
        return False
    if isinstance(left, list):
        if len(left) != len(right):
            return False
        for index in range(len(left)):
            if not equalTrustedJson(left[index], right[index]):
                return False
        return True
    if len(left) != len(right):
        return False
    for key in left:
        if key not in right or not equalTrustedJson(left[key], right[key]):
            return False
    return True


def abortContext(context: _OverlayContext) -> None:
    if context.status.value in ("aborted", "consumed", "stale"):
        return
    context.status.value = "aborted"
    clearContext(context)


def releaseOverlayReferences(context: _OverlayContext) -> None:
    tracker = context.tracker()
    if tracker is not None:
        tracker.releaseContext(context)
    context.overlayReleased = True
    context.dirty.clear()
    context.nodes.clear()
    context.rawNodes = None
    context.ops = None
    context.root = None
    context.bases = None
    context.stored = None
    context.baseValue = UNDEFINED


def clearContext(context: _OverlayContext) -> None:
    tracker = context.tracker()
    if tracker is not None:
        tracker.releaseContext(context)
    for node in context.nodes:
        if node.writes is not None:
            node.writes.clear()
        if node.deletes is not None:
            node.deletes.clear()
        if node.readded is not None:
            node.readded.clear()
        if node.array is not None:
            if node.array.pieces is not None:
                node.array.pieces.clear()
            node.array.root = None
            node.array.baseLocations = None
            node.array.insertLocations = None
            if node.array.baseOverrides is not None:
                node.array.baseOverrides.clear()
            if node.array.insertOverrides is not None:
                node.array.insertOverrides.clear()
        node.parentSource = None
        node.proxy = None
        node.writeKey = None
        node.writeValue = UNDEFINED
        node.writes = None
        node.deleteKey = None
        node.deletes = None
        node.readded = None
        node.array = None
        node.subtreeDirty = False
        node.preparedPath = None
    context.dirty.clear()
    context.nodes.clear()
    context.rawNodes = None
    context.ops = None
    context.root = None
    context.bases = None
    context.stored = None
    context.baseValue = UNDEFINED
    context.overlayReleased = True


def isContainer(value: Any) -> bool:
    return isinstance(value, list | dict)


class _StatusCell:
    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        self.value = value


class _Index:
    """A reference to a container the context stored (upstream's ``{ index }``)."""

    __slots__ = ("index",)

    def __init__(self, index: int) -> None:
        self.index = index


class _InsertSource:
    """The values of one insertion. Compared by identity, as upstream's objects are."""

    __slots__ = ("refs",)

    def __init__(self, refs: list[Any]) -> None:
        self.refs = refs


class _Piece:
    """``BasePiece`` (``kind == "base"``) or ``InsertPiece`` (``kind == "insert"``, with a ``source``)."""

    __slots__ = ("kind", "length", "source", "start", "step")

    def __init__(self, kind: str, start: int, length: int, step: int, source: _InsertSource | None = None) -> None:
        self.kind = kind
        self.start = start
        self.length = length
        self.step = step
        self.source = source

    def copy(self, *, start: int | None = None, length: int | None = None) -> _Piece:
        return _Piece(
            self.kind,
            self.start if start is None else start,
            self.length if length is None else length,
            self.step,
            self.source,
        )


class _PieceNode:
    __slots__ = ("elements", "left", "piece", "priority", "right")

    def __init__(self, piece: _Piece, priority: int) -> None:
        self.piece = piece
        self.left: _PieceNode | None = None
        self.right: _PieceNode | None = None
        self.priority = priority
        self.elements = piece.length


class _PieceLocation:
    __slots__ = ("logicalStart", "maximum", "minimum", "piece")

    def __init__(self, piece: _Piece, logicalStart: int, minimum: int, maximum: int) -> None:
        self.piece = piece
        self.logicalStart = logicalStart
        self.minimum = minimum
        self.maximum = maximum


class _DenseRegion:
    __slots__ = ("length", "start")

    def __init__(self, start: int, length: int) -> None:
        self.start = start
        self.length = length


class _DenseCandidates:
    __slots__ = ("bits", "indices", "length")

    def __init__(self, length: int) -> None:
        self.indices: list[int] = []
        self.bits: bytearray | None = None
        self.length = length


class _ArrayPlan:
    __slots__ = ("insertRuns", "permutation", "removeRuns")

    def __init__(self, removeRuns: list[int], permutation: list[int] | None, insertRuns: list[int]) -> None:
        self.removeRuns = removeRuns
        self.permutation = permutation
        self.insertRuns = insertRuns


class _ArrayOverlay:
    __slots__ = (
        "baseLocations",
        "baseOverrides",
        "generation",
        "insertLocations",
        "insertOverrides",
        "locatedOffset",
        "pieces",
        "plan",
        "root",
        "seed",
        "structural",
    )

    def __init__(self) -> None:
        self.root: _PieceNode | None = None
        self.pieces: list[_Piece] | None = None
        self.baseOverrides: dict[int, Any] | None = None
        self.insertOverrides: dict[_InsertSource, dict[int, Any]] | None = None
        self.structural = False
        self.generation = 0
        self.plan: _ArrayPlan | None = None
        self.seed = 0x9E3779B9
        self.locatedOffset = 0
        self.baseLocations: list[_PieceLocation] | None = None
        self.insertLocations: dict[_InsertSource, list[_PieceLocation]] | None = None


class _OverlayNode:
    """One container the draft has touched. ``writeValue`` is ``UNDEFINED`` while ``writeKey`` is unset."""

    def __init__(
        self,
        context: _OverlayContext,
        baseIndex: int,
        parentIndex: int,
        parentKind: int,
        parentKey: str | int,
        parentPlacement: bool,
    ) -> None:
        self.context = context
        self.baseIndex = baseIndex
        self.parentIndex = parentIndex
        self.parentKind = parentKind
        self.parentKey = parentKey
        self.parentSource: _InsertSource | None = None
        self.parentPlacement = parentPlacement
        self.proxy: DraftObject | DraftArray | None = None
        self.writeKey: str | None = None
        self.writeValue: Any = UNDEFINED
        self.writes: dict[str, Any] | None = None
        self.deleteKey: str | None = None
        # ``Set``s, which iterate in insertion order: the ``d`` operations come out in that order.
        self.deletes: dict[str, None] | None = None
        self.readded: dict[str, None] | None = None
        self.array: _ArrayOverlay | None = None
        self.dirty = False
        self.subtreeDirty = False
        self.preparedPath: list[Any] | None = None


class _OverlayContext:
    def __init__(
        self,
        owner: object,
        tracker: weakref.ref[TrackerImpl],
        baseRevision: int,
        replacement: bool,
        base: Any,
    ) -> None:
        self.owner = owner
        self.tracker = tracker
        self.baseRevision = baseRevision
        self.status = _StatusCell("open")
        self.root: _OverlayNode | None = None
        self.bases: list[Any] | None = []
        self.stored: list[Any] | None = []
        self.dirty: list[_OverlayNode] = []
        self.nodes: list[_OverlayNode] = []
        self.rawNodes: dict[int, _OverlayNode] | None = {}
        self.ops: list[Op] | None = None
        self.replacement = replacement
        self.replacementNoop = False
        self.baseValue: Any = base
        self.overlayReleased = False
        self.simpleObjectMaterialization = False
        self.registryRef: weakref.ref[_OverlayContext] | None = None


# The context each prepared change came from. `WeakMap<object, OverlayContext>`.
PREPARED: weakref.WeakKeyDictionary[Any, _OverlayContext] = weakref.WeakKeyDictionary()


def createContext(
    tracker: weakref.ref[TrackerImpl], owner: object, baseRevision: int, root: Any, replacement: bool, base: Any
) -> _OverlayContext:
    context = _OverlayContext(owner, tracker, baseRevision, replacement, base)
    context.root = createNode(context, root, None)
    return context


# ─── Drafts ──────────────────────────────────────────────────────────────────


def _toKey(property: Any) -> str:
    if isinstance(property, str):
        return property
    if isinstance(property, int) and not isinstance(property, bool):
        return str(property)
    raise TypeError("Overlay objects are addressed by string keys")


class DraftObject:
    """The draft of one JSON object, valid while its change is open."""

    __slots__ = ("_node",)
    _node: _OverlayNode | None

    def __init__(self, node: _OverlayNode) -> None:
        object.__setattr__(self, "_node", node)

    def _target(self) -> _OverlayNode:
        node = self._node
        if node is None:
            raise TypeError("Cannot use a settled overlay")
        return node

    def __getitem__(self, key: Any) -> Any:
        return getProperty(self._target(), _toKey(key))

    def __setitem__(self, key: Any, value: Any) -> None:
        setProperty(self._target(), _toKey(key), value)

    def __delitem__(self, key: Any) -> None:
        deleteProperty(self._target(), _toKey(key))

    def __contains__(self, key: Any) -> bool:
        return hasProperty(self._target(), _toKey(key))

    def __iter__(self):
        return iter(ownKeys(self._target()))

    def __len__(self) -> int:
        return len(ownKeys(self._target()))

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("Defining overlay properties is not supported")

    def keys(self) -> list[str]:
        return list(ownKeys(self._target()))

    def values(self) -> list[Any]:
        node = self._target()
        return [getProperty(node, key) for key in ownKeys(node)]

    def items(self) -> list[tuple[str, Any]]:
        node = self._target()
        return [(key, getProperty(node, key)) for key in ownKeys(node)]

    def get(self, key: Any, default: Any = UNDEFINED) -> Any:
        node = self._target()
        key = _toKey(key)
        return getProperty(node, key) if hasProperty(node, key) else default

    def __repr__(self) -> str:
        return "<overlay object>" if self._node is not None else "<settled overlay>"


class DraftArray:
    """The draft of one JSON array, valid while its change is open. Entries are read and written by index; an
    index at or past the length reads ``UNDEFINED``, and writing exactly the length appends."""

    __slots__ = ("_node",)
    _node: _OverlayNode | None

    def __init__(self, node: _OverlayNode) -> None:
        object.__setattr__(self, "_node", node)

    def _target(self) -> _OverlayNode:
        node = self._node
        if node is None:
            raise TypeError("Cannot use a settled overlay")
        return node

    def __getitem__(self, index: Any) -> Any:
        node = self._target()
        assertReadable(node.context)
        position = arrayIndex(index)
        if position is None:
            return UNDEFINED
        return getArrayIndex(node, position)

    def __setitem__(self, index: Any, value: Any) -> None:
        node = self._target()
        assertWritable(node.context)
        position = arrayIndex(index)
        if position is None:
            raise TypeError("Only array indices and length can be written")
        if position > arrayLength(arrayOverlay(node)):
            raise TypeError("Overlay arrays cannot contain holes")
        setArrayIndex(node, position, clonePlacement(value))

    def __delitem__(self, index: Any) -> None:
        node = self._target()
        assertWritable(node.context)
        raise TypeError("Overlay arrays cannot contain holes")

    def __len__(self) -> int:
        node = self._target()
        assertReadable(node.context)
        return arrayLength(arrayOverlay(node))

    def __iter__(self):
        node = self._target()
        assertReadable(node.context)
        index = 0
        while index < arrayLength(arrayOverlay(node)):
            yield getArrayIndex(node, index)
            index += 1

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "length":
            node = self._target()
            assertWritable(node.context)
            setArrayLength(node, toArrayLength(value))
            return
        raise TypeError("Defining overlay properties is not supported")

    @property
    def length(self) -> int:
        return len(self)

    def has(self, index: Any) -> bool:
        node = self._target()
        assertReadable(node.context)
        position = arrayIndex(index)
        return position is not None and position < arrayLength(arrayOverlay(node))

    def push(self, *items: Any) -> int:
        node = mutatorNode(self)
        length = arrayLength(arrayOverlay(node))
        replacePieceRange(node, length, 0, insertPlacementPiece(node, list(items)))
        return length + len(items)

    def pop(self) -> Any:
        node = mutatorNode(self)
        length = arrayLength(arrayOverlay(node))
        if length == 0:
            return UNDEFINED
        value = getArrayIndex(node, length - 1)
        replacePieceRange(node, length - 1, 1, [])
        return value

    def shift(self) -> Any:
        node = mutatorNode(self)
        length = arrayLength(arrayOverlay(node))
        if length == 0:
            return UNDEFINED
        value = getArrayIndex(node, 0)
        replacePieceRange(node, 0, 1, [])
        return value

    def unshift(self, *items: Any) -> int:
        node = mutatorNode(self)
        replacePieceRange(node, 0, 0, insertPlacementPiece(node, list(items)))
        return arrayLength(arrayOverlay(node))

    def splice(self, *args: Any) -> list[Any]:
        node = mutatorNode(self)
        length = arrayLength(arrayOverlay(node))
        start = 0 if len(args) == 0 else clampIndex(toIntegerOrInfinity(args[0]), length)
        if len(args) == 0:
            remove = 0
        elif len(args) == 1:
            remove = length - start
        else:
            remove = int(min(max(toIntegerOrInfinity(args[1]), 0), length - start))
        removed = [getArrayIndex(node, start + offset) for offset in range(remove)]
        itemStart = min(2, len(args))
        itemCount = max(len(args) - 2, 0)
        replacePieceRange(node, start, remove, insertPlacementPiece(node, list(args), itemStart))
        setArrayLength(node, length - remove + itemCount)
        return removed

    def reverse(self) -> DraftArray:
        node = mutatorNode(self)
        overlay = arrayOverlay(node)
        if arrayLength(overlay) < 2:
            return node.proxy  # type: ignore[return-value]
        pieces = list(reversed(piecesOf(overlay)))
        for piece in pieces:
            piece.start += piece.step * (piece.length - 1)
            piece.step = -1 if piece.step == 1 else 1
        replaceAllPieces(overlay, pieces)
        overlay.structural = True
        overlay.generation += 1
        overlay.plan = None
        markDirty(node)
        return node.proxy  # type: ignore[return-value]

    def sort(self, comparator: Callable[[Any, Any], Any] | None = None) -> DraftArray:
        node = mutatorNode(self)
        return arraySort(node, comparator)

    def fill(self, supplied: Any, startArg: Any = UNDEFINED, endArg: Any = UNDEFINED) -> DraftArray:
        node = mutatorNode(self)
        length = arrayLength(arrayOverlay(node))
        start = 0 if startArg is UNDEFINED else clampIndex(toIntegerOrInfinity(startArg), length)
        end = length if endArg is UNDEFINED else clampIndex(toIntegerOrInfinity(endArg), length)
        if end <= start:
            return node.proxy  # type: ignore[return-value]
        items = [clonePlacement(supplied) for _ in range(end - start)]
        replacePieceRange(node, start, end - start, insertPiece(node, items))
        return node.proxy  # type: ignore[return-value]

    def copyWithin(self, targetArg: Any, startArg: Any, endArg: Any = UNDEFINED) -> DraftArray:
        node = mutatorNode(self)
        length = arrayLength(arrayOverlay(node))
        target = clampIndex(toIntegerOrInfinity(targetArg), length)
        start = clampIndex(toIntegerOrInfinity(startArg), length)
        end = length if endArg is UNDEFINED else clampIndex(toIntegerOrInfinity(endArg), length)
        count = int(min(max(end - start, 0), length - target))
        values = [clonePlacement(getArrayIndex(node, start + offset)) for offset in range(count)]
        replacePieceRange(node, target, count, insertPiece(node, values))
        return node.proxy  # type: ignore[return-value]

    def slice(self, start: Any = 0, end: Any = UNDEFINED) -> list[Any]:
        """``Array.prototype.slice``: the entries, containers as drafts, in a plain list."""
        node = self._target()
        assertReadable(node.context)
        length = arrayLength(arrayOverlay(node))
        first = clampIndex(toIntegerOrInfinity(start), length)
        last = length if end is UNDEFINED else clampIndex(toIntegerOrInfinity(end), length)
        return [getArrayIndex(node, index) for index in range(first, max(first, last))]

    def indexOf(self, value: Any) -> int:
        """``Array.prototype.indexOf``: strict equality, a container by identity (a draft is its own
        identity)."""
        node = self._target()
        assertReadable(node.context)
        for index in range(arrayLength(arrayOverlay(node))):
            entry = getArrayIndex(node, index)
            if entry is value or strictEquals(entry, value):
                return index
        return -1

    def __repr__(self) -> str:
        return "<overlay array>" if self._node is not None else "<settled overlay>"


__all__ = ["Change", "Prepared", "Tracker", "track"]
