"""chord/delta: immutable revision tracking and operations over plain JSON.

Translated from pi's ``packages/chord/src/delta/index.ts``.

Depends on nothing else in the harness. Session storage, the runtime and the facet host consume it; keep
the arrows pointing that way.

Operations are lists, as upstream's tuples are arrays: ``["r", value]``, ``["s", path, value]`` and so on.
A path is a list of ``str`` keys and non-negative ``int`` array indices. A JSON value is what
``misaka.chord.json`` accepts. Strings count in UTF-16 code units wherever upstream's do (``t`` counts, the
overlap probe), so a replica in this language and one in TypeScript agree on every batch.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, cast

from misaka.chord.json import JsonValue
from misaka.utils.js import (
    UNDEFINED,
    jsIsInteger,
    jsonStringify,
    jsSlice,
    jsString,
    toUtf16Units,
)


def isObj(value: Any) -> bool:
    return isinstance(value, list | dict)


def _key(segment: Seg) -> str:
    """The property key a segment names: ``String(segment)``, which prints 1e21 as ``1e+21``."""
    return segment if isinstance(segment, str) else jsString(segment)


def _hasOwn(node: Any, segment: Seg) -> bool:
    """`Object.hasOwn(node, segment)` for a container: an array index in range, an object key (a number
    names the key written with its digits)."""
    if isinstance(node, list):
        if isinstance(segment, str):
            # An array owns its index keys and `length`, so a string spelling of either is found here; the
            # caller refuses it, as an array is addressed by number.
            return segment == "length" or (
                segment.isascii() and segment.isdigit() and str(int(segment)) == segment and int(segment) < len(node)
            )
        return isinstance(segment, int) and not isinstance(segment, bool) and 0 <= segment < len(node)
    return _key(segment) in node


def _own(node: Any, segment: Seg) -> Any:
    if isinstance(node, list):
        return node[segment]  # type: ignore[index]
    return node[_key(segment)]


def _integralNumber(value: Any) -> Any:
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _integral(op: Op) -> Op:
    """The op with every integral float as an ``int``. JavaScript has one number type, so a peer may spell
    an index or a count ``2.0`` or ``1e9``; Python slices and indexes with ``int`` only."""
    verb = op[0]
    if verb == "r":
        return op
    path = [_integralNumber(seg) for seg in op[1]]
    if verb == "p":
        return [verb, path, _integralNumber(op[2]), _integralNumber(op[3]), op[4]]
    if verb == "m":
        return [verb, path, [_integralNumber(index) for index in op[2]]]
    if verb == "t":
        return [verb, path, _integralNumber(op[2])]
    return [verb, path, *op[2:]]

type Seg = str | int
type Path = Sequence[Seg]
type NonEmptyPath = Sequence[Seg]

# A path inline, or an id assigned by the encoder on second use.
type PathRef = Path | int

# Tuples are the form: in memory, on the wire, on disk.
#
# `r` is the ONLY op that replaces a whole value. `s`/`d`/`a`/`t` cannot target the root: the type
# forbids it. `p` and `m` may, because a tracked value can itself be an array. Operation shape is not
# canonical: an array may be emptied by either a replacement or a root splice.
#
# `Op` knows nothing about the path dictionary. Interning, id references and omitted paths live in
# `WireOp` and exist only between `encode` and `decode`.
#
#   ["r", value]
#   ["s", path, value]
#   ["d", path]
#   ["a", path, text]               append to a string
#   ["t", path, count]              remove `count` UTF-16 code units from a string's front
#   ["p", path, index, remove, items]
#   ["m", path, permutation]        reorder an array in place: `new[i] = old[permutation[i]]`
type Op = list[Any]

# What crosses a boundary. Adds two compressions and nothing else:
#
#   ["#", id, path]    defines an id, emitted on a path's SECOND use
#   a numeric PathRef  references a previously defined id
#   a shortened tuple  reuses the previous op's path; arity disambiguates
#
# ["r", value] carries no path, so it encodes to itself, which is why isBase works unchanged on either
# vocabulary.
type WireOp = list[Any]


# ─── Classification ──────────────────────────────────────────────────────────


def isReplace(op: Sequence[Any]) -> bool:
    return op[0] == "r"


def isBase(ops: Sequence[Sequence[Any]]) -> bool:
    """A batch begins with a replacement. Flush guarantees `r` is at index 0 or absent, so this is exact
    rather than a heuristic."""
    return len(ops) > 0 and ops[0][0] == "r"


# ─── Overlap ─────────────────────────────────────────────────────────────────


def overlap(a: str, b: str, scan: int, probe: int = 64, maxCandidates: int = 8) -> int:
    """Longest suffix of ``a`` that is a prefix of ``b``. Probes with ``find`` and verifies exact substring
    equality, so the hot loops are native. A hand-written KMP is asymptotically equivalent and much slower in
    practice.

    Always correct: the returned n satisfies ``a.slice(a.length - n) === b.slice(0, n)``, in UTF-16 code units.
    """
    a, b = toUtf16Units(a), toUtf16Units(b)
    if len(a) == 0 or len(b) == 0 or scan == 0:
        return 0
    tail = a[len(a) - scan :] if len(a) > scan else a

    # A probe of length h can only find overlaps of at least h: the head must actually occur in `a`. So
    # try a long head first (few candidates, and it catches the large overlaps a rolling window produces),
    # then fall back to one character, which finds any overlap at the cost of more candidates.
    #
    # Candidates are bounded because repetitive output (a build log, or any run of one character) makes a
    # long head match at thousands of positions. Giving up returns 0, which emits a set: larger, never wrong.
    for h in (min(probe, len(b)), 1):
        head = b[:h]
        tried = 0
        k = tail.find(head)
        while k != -1:
            tried += 1
            if tried > maxCandidates:
                break
            n = len(tail) - k
            if n <= len(b) and tail[k:] == b[:n]:
                return n
            k = tail.find(head, k + 1)
        if h == 1:
            break
    return 0


# ─── Path safety ─────────────────────────────────────────────────────────────

# Segments that reach the prototype chain.
#
# `JSON.parse` is safe on its own: it makes `__proto__` an own property. What is not safe is
# `parent[key] = value`, which is exactly what an applier does, and paths are data:
# `["s", ["__proto__", "isAdmin"], true]` pollutes `Object.prototype` for the whole process.
#
# Ops arrive from a facet, a plugin compartment, or a tool whose details may echo model output, so none of
# it is trusted input.
#
# MISAKA fork: a Python container has no prototype to pollute, but the segments stay reserved so a batch
# means the same thing here as on a TypeScript peer.
RESERVED_SEGMENTS: frozenset[str] = frozenset({"__proto__", "constructor", "prototype"})


class UnsafePathError(Exception):
    def __init__(self, segment: Seg) -> None:
        super().__init__(f"unsafe path segment: {jsString(segment)}")
        self.segment = segment
        self.name = "UnsafePathError"


def _isArray(value: Any) -> bool:
    return isinstance(value, list | tuple)


def assertValidOp(op: Any) -> None:
    """Verb, arity and payload shape for a **decoded** op: paths inline, no `#`, no short forms. `apply`
    uses this.

    Validating `Op` against the wire grammar would be laxer than the type: a two-element `["s", value]`
    would pass, and `apply` would then read the value as a path. Each vocabulary gets the validator that
    matches it.
    """
    if not _isArray(op) or len(op) == 0:
        raise TypeError("op is not a tuple")
    verb = op[0]
    if verb == "r":
        if len(op) != 2:
            raise TypeError("r arity")
        return
    if verb == "s":
        if len(op) != 3:
            raise TypeError("s arity")
        assertPathArg(op[1], True)
        return
    if verb == "d":
        if len(op) != 2:
            raise TypeError("d arity")
        assertPathArg(op[1], True)
        return
    if verb == "a":
        if len(op) != 3 or not isinstance(op[2], str):
            raise TypeError("a shape")
        assertPathArg(op[1], True)
        return
    if verb == "t":
        if len(op) != 3 or not jsIsInteger(op[2]) or op[2] < 0:
            raise TypeError("t shape")
        assertPathArg(op[1], True)
        return
    if verb == "p":
        if len(op) != 5:
            raise TypeError("p arity")
        assertPathArg(op[1])
        if not jsIsInteger(op[2]) or op[2] < 0:
            raise TypeError("p index")
        if not jsIsInteger(op[3]) or op[3] < 0:
            raise TypeError("p remove")
        if not isinstance(op[4], list):
            raise TypeError("p items")
        return
    if verb == "m":
        if len(op) != 3:
            raise TypeError("m arity")
        assertPathArg(op[1])
        assertPermutation(op[2])
        return
    # Silently skipping an unknown verb is how a newer producer's op vanishes.
    raise TypeError(f"unknown op verb: {jsString(verb)}")


def assertPathArg(p: Any, nonEmpty: bool = False) -> None:
    if not _isArray(p):
        raise TypeError("path is not an array")
    if nonEmpty and len(p) == 0:
        raise TypeError("path is empty")
    assertSafePath(p)


def assertPermutation(value: Any) -> None:
    if not isinstance(value, list):
        raise TypeError("m permutation is not an array")
    seen = bytearray(len(value))
    for index in value:
        if not jsIsInteger(index) or index < 0 or index >= len(value) or seen[int(index)] != 0:
            raise TypeError("m permutation is not a bijection")
        seen[int(index)] = 1


def assertValidWireOp(op: Any) -> None:
    """The same, for the wire grammar: ids and short forms are legal here."""
    if not _isArray(op) or len(op) == 0:
        raise TypeError("op is not a tuple")
    verb = op[0]

    def okRef(ref: Any) -> None:
        if isinstance(ref, int | float) and not isinstance(ref, bool):
            if not jsIsInteger(ref) or ref < 0:
                raise TypeError("bad path id")
            return
        # A string is not a path. Unchecked, `"a".slice(0, -1)` is `""`, so it resolves to the ROOT and
        # writes there: a path that is not a path, accepted.
        if not _isArray(ref):
            raise TypeError("path is not an array")
        assertSafePath(ref)

    if verb == "r":
        if len(op) != 2:
            raise TypeError("r arity")
        return
    if verb == "s":
        if len(op) == 3:
            okRef(op[1])
        elif len(op) != 2:
            raise TypeError("s arity")
        return
    if verb == "d":
        if len(op) == 2:
            okRef(op[1])
        elif len(op) != 1:
            raise TypeError("d arity")
        return
    if verb == "a":
        if len(op) == 3:
            okRef(op[1])
            if not isinstance(op[2], str):
                raise TypeError("a value")
        elif len(op) == 2:
            if not isinstance(op[1], str):
                raise TypeError("a value")
        else:
            raise TypeError("a arity")
        return
    if verb == "t":
        if len(op) == 3:
            okRef(op[1])
            if not jsIsInteger(op[2]) or op[2] < 0:
                raise TypeError("t count")
        elif len(op) == 2:
            if not jsIsInteger(op[1]) or op[1] < 0:
                raise TypeError("t count")
        else:
            raise TypeError("t arity")
        return
    if verb == "p":
        if len(op) == 5:
            i, r, items = op[2], op[3], op[4]
        elif len(op) == 4:
            i, r, items = op[1], op[2], op[3]
        else:
            raise TypeError("p arity")
        if len(op) == 5:
            okRef(op[1])
        if not jsIsInteger(i) or i < 0:
            raise TypeError("p index")
        if not jsIsInteger(r) or r < 0:
            raise TypeError("p remove")
        if not isinstance(items, list):
            raise TypeError("p items")
        return
    if verb == "m":
        if len(op) == 3:
            okRef(op[1])
        elif len(op) != 2:
            raise TypeError("m arity")
        assertPermutation(op[len(op) - 1])
        return
    if verb == "#":
        if len(op) != 3 or not jsIsInteger(op[1]) or op[1] < 0 or not _isArray(op[2]):
            raise TypeError("# shape")
        assertSafePath(op[2])
        return
    # Silently skipping an unknown verb is how a newer producer's op vanishes.
    raise TypeError(f"unknown op verb: {jsString(verb)}")


def assertSafePath(path: Path) -> None:
    for seg in path:
        if isinstance(seg, str):
            if seg in RESERVED_SEGMENTS:
                raise UnsafePathError(seg)
        elif not jsIsInteger(seg) or seg < 0:
            raise UnsafePathError(seg)


def assertIndexInRange(parent: Sequence[Any], index: int) -> None:
    """An index may address an existing element or append exactly one past the end.

    This is not an arbitrary cap: it is what keeps the value a `JsonValue`. A sparse array does not
    survive a JSON round trip: holes serialise to `null` and return as real properties, so `arr[7] = x`
    on a length-3 array already produces state a replica cannot match. Rejecting the write is more honest
    than silently diverging.

    It also removes the denial of service it would otherwise permit: `["s", ["xs", 4294967290], 1]`
    allocates a 4.29-billion-entry array from one op. Growth stays possible and stays proportional: the
    tracker already emits `arr.length = n` as a splice of explicit nulls, whose op size grows with the gap,
    so a large growth costs a large op rather than a small one.
    """
    if index > len(parent):
        raise UnsafePathError(index)


# ─── Applier ─────────────────────────────────────────────────────────────────


class PathError(Exception):
    def __init__(self, path: Path | int) -> None:
        super().__init__(f"unresolvable path: {jsonStringify(list(cast("Sequence[Any]", path)) if _isArray(path) else path)}")
        self.path = path
        self.name = "PathError"


def apply(target: Any, ops: Sequence[Op]) -> Any:
    """Apply ops to a plain mutable value. Returns the value, because `r` replaces it outright and cannot
    be done in place.

    Takes decoded ops. Path ids and omitted paths are a wire concern: run `decode` first if the ops came
    from a boundary.
    """
    return applyOps(target, ops)


def applyOps(target: Any, ops: Sequence[Op]) -> Any:
    root = target

    for op in ops:
        assertValidOp(op)
        op = _integral(op)
        if op[0] == "r":
            # Adopted, not copied. The consumer owns the batch it was handed.
            #
            # Fanning one batch out to several consumers in-process therefore makes their replicas alias
            # each other. That is an ownership rule, not a defect: copy the batch at the fan-out point, or
            # let each consumer decode its own. A batch that crosses a real boundary is already distinct,
            # because serialisation produces fresh objects.
            root = op[1]
            continue

        path = op[1]
        assertSafePath(path)

        if op[0] == "p":
            target_ = root if len(path) == 0 else resolve(root, path)
            if not isinstance(target_, list):
                raise PathError(path)
            del target_[op[2] : op[2] + op[3]]
            chunkSize = 10_000
            items = op[4]
            for offset in range(0, len(items), chunkSize):
                at = op[2] + offset
                target_[at:at] = items[offset : offset + chunkSize]
            continue
        if op[0] == "m":
            target_ = root if len(path) == 0 else resolve(root, path)
            if not isinstance(target_, list) or len(target_) != len(op[2]):
                raise PathError(path)
            previous = list(target_)
            for index in range(len(op[2])):
                target_[index] = previous[op[2][index]]
            continue

        # s/d/a/t can never target the root: the type forbids it.
        parent = resolve(root, path[:-1])
        key = path[-1]
        if isinstance(parent, list):
            if not isinstance(key, int) or isinstance(key, bool):
                raise UnsafePathError(key)
            assertIndexInRange(parent, key)

        # defineProperty rather than assignment: a setter inherited from the prototype
        # chain would otherwise run on write.
        def write(value: Any, parent: Any = parent, key: Seg = key) -> None:
            _defineProperty(parent, key, value)

        def read(parent: Any = parent, key: Seg = key) -> Any:
            return _own(parent, key) if _hasOwn(parent, key) else UNDEFINED

        match op[0]:
            case "s":
                write(op[2])
            case "d":
                if isinstance(parent, list):
                    if not isinstance(key, int) or key >= len(parent):
                        raise PathError(path)
                    del parent[key]
                else:
                    parent.pop(_key(key), None)
            case "a":
                current = read()
                if not isinstance(current, str):
                    raise PathError(path)
                write(f"{current}{op[2]}")
            case "t":
                current = read()
                if not isinstance(current, str):
                    raise PathError(path)
                write(jsSlice(current, op[2]))
    return root


def _defineProperty(target: Any, key: Seg, value: Any) -> None:
    """MISAKA fork: ``Object.defineProperty(target, key, {value, writable, enumerable, configurable})`` on a list
    or dict, where nothing inherited can run on write; a list grows by one when the key is its length."""
    if isinstance(target, list):
        if key == len(target):
            target.append(value)
        else:
            target[cast(int, key)] = value
    else:
        target[_key(key)] = value


def applyImmutable(target: Any, ops: Sequence[Op]) -> Any:
    """Apply one decoded operation batch without mutating the previous immutable value."""
    return applyImmutableBatches(target, [ops])


def applyImmutableBatches(target: Any, batches: Iterable[Sequence[Op]]) -> Any:
    """Apply decoded operation batches as one final-result-only replay.

    Containers copied for an earlier batch may be mutated while applying a later batch, so this
    deliberately exposes no intermediate revisions.
    """
    root = target
    # MISAKA fork: `WeakSet<object>` becomes the ids of the copies, which the map keeps alive so that no
    # id is reused while the call runs (a plain `dict` or `list` cannot be weakly referenced).
    owned: dict[int, Any] = {}
    # MISAKA fork: `for (const ops of batches)` closes an iterator it leaves early; a Python `for` does not.
    iterator = iter(batches)
    try:
        for ops in iterator:
            for op in ops:
                assertValidOp(op)
                op = _integral(op)
                if op[0] == "r":
                    root = op[1]
                    continue
                if op[0] == "p":
                    appended = appendCopy(root, op, owned)
                    if appended is not UNDEFINED:
                        root = appended
                        continue
                root = copyContainers(root, op[1] if op[0] in ("p", "m") else op[1][:-1], owned)
                root = applyOps(root, [op])
    finally:
        close = getattr(iterator, "close", None)
        if close is not None:
            close()
    return root


def appendCopy(root: Any, op: Op, owned: dict[int, Any]) -> Any:
    """An append to an array not yet copied in this replay, built as one copy with the items (``concat``) instead of a
    copy that the insert then grows, which copies it again. ``UNDEFINED`` for any other splice and for any path that is
    not a plain walk over own properties, which takes the general path and so fails with its errors."""
    _, path, index, deleteCount, items = op
    if deleteCount != 0:
        return UNDEFINED
    target = root
    for segment in path:
        valid = (
            segment not in RESERVED_SEGMENTS and isinstance(target, dict)
            if isinstance(segment, str)
            else jsIsInteger(segment) and segment >= 0 and isinstance(target, list)
        )
        if not valid or not _hasOwn(target, segment):
            return UNDEFINED
        target = _own(target, segment)
    if not isinstance(target, list) or id(target) in owned or index != len(target):
        return UNDEFINED
    appended = [*target, *items]
    owned[id(appended)] = appended
    if len(path) == 0:
        return appended
    parentPath = path[:-1]
    copiedRoot = copyContainers(root, parentPath, owned)
    _defineProperty(resolve(copiedRoot, parentPath), path[-1], appended)
    return copiedRoot


def copyContainers(root: Any, path: Path, owned: dict[int, Any]) -> Any:
    def copy(value: Any) -> Any:
        if isinstance(value, list):
            return list(value)
        if not isinstance(value, dict):
            raise PathError(path)
        return dict(value)

    if not isObj(root):
        raise PathError(path)
    if id(root) in owned:
        copiedRoot = root
    else:
        copiedRoot = copy(root)
        owned[id(copiedRoot)] = copiedRoot
    destination = copiedRoot
    for segment in path:
        if not _hasOwn(destination, segment):
            raise PathError(path)
        if isinstance(destination, list) and (not isinstance(segment, int) or isinstance(segment, bool)):
            raise UnsafePathError(segment)
        child = _own(destination, segment)
        if not isObj(child):
            raise PathError(path)
        if id(child) in owned:
            destination = child
            continue
        copiedChild = copy(child)
        _defineProperty(destination, segment, copiedChild)
        owned[id(copiedChild)] = copiedChild
        destination = copiedChild
    return copiedRoot


def resolveValue(root: Any, path: Path) -> Any:
    node = root
    for seg in path:
        if not isObj(node):
            raise PathError(path)
        if isinstance(node, list) and (not isinstance(seg, int) or isinstance(seg, bool)):
            raise UnsafePathError(seg)
        # Own properties only: an inherited getter must not run, and a walk must not escape the value into
        # the prototype chain.
        if not _hasOwn(node, seg):
            raise PathError(path)
        node = _own(node, seg)
    return node


def resolve(root: Any, path: Path) -> Any:
    node = resolveValue(root, path)
    if not isObj(node):
        raise PathError(path)
    return node


# ─── Codec ───────────────────────────────────────────────────────────────────
#
# Path interning and arity omission live between the tracker and a boundary; `Op` and `apply` know nothing
# about them.
#
# Consecutive batches of one encoder may be concatenated into one batch (links coalesce waiting state this way):
# only path ids span batches, and every batch starts with an explicit path.
#
# ONE PAIR PER INDEPENDENT STATE STREAM. Every decoder must observe exactly the batches encoded by its
# matching encoder, beginning with that state's base. Sharing a transport connection does not make
# separately hydrated states one stream.


def pathKey(path: Path) -> str:
    return jsonStringify(list(path))


class Encoder:
    """Intern on SECOND use. A definition costs more than the path it replaces, so interning on first use
    loses on the many paths written exactly once."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._ids: dict[str, int] = {}
        self._nextId = 0
        self._previous: str | None = None  # last path in THIS batch

    def encode(self, ops: Sequence[Op]) -> list[WireOp]:
        # Arity omission is scoped to a batch. Letting it span batches would make a batch's first op depend
        # on the previous batch's last one, so a reader that skips or reorders a batch decodes into the wrong
        # path. Ids are the only cross-batch state, and the dictionary makes those explicit.
        self._previous = None
        out: list[WireOp] = []
        for op in ops:
            verb = op[0]
            if verb == "r":
                out.append(op)
                # A base batch is a RECOVERY POINT: a reader replays from the last one with a fresh decoder.
                # So everything after it must be self-contained. Keeping ids across a replacement emits
                # references to definitions the reader never saw: recovery fails with an unresolvable path id.
                self._seen.clear()
                self._ids.clear()
                self._nextId = 0
                self._previous = None
                continue
            path = op[1]
            key = pathKey(path)

            # Same path as the previous op: drop the ref entirely.
            if key == self._previous:
                if verb == "s":
                    out.append(["s", op[2]])
                elif verb == "d":
                    out.append(["d"])
                elif verb == "a":
                    out.append(["a", op[2]])
                elif verb == "t":
                    out.append(["t", op[2]])
                elif verb == "p":
                    out.append(["p", op[2], op[3], op[4]])
                elif verb == "m":
                    out.append(["m", op[2]])
                continue

            ref: PathRef = path
            existing = self._ids.get(key)
            if existing is not None:
                ref = existing
            elif key in self._seen:
                id = self._nextId
                self._nextId += 1
                self._ids[key] = id
                out.append(["#", id, path])  # second use: define, then reference
                ref = id
            else:
                self._seen.add(key)  # first use: inline

            if verb == "s":
                out.append(["s", ref, op[2]])
            elif verb == "d":
                out.append(["d", ref])
            elif verb == "a":
                out.append(["a", ref, op[2]])
            elif verb == "t":
                out.append(["t", ref, op[2]])
            elif verb == "p":
                out.append(["p", ref, op[2], op[3], op[4]])
            elif verb == "m":
                out.append(["m", ref, op[2]])
            self._previous = key
        return out


def encoder() -> Encoder:
    return Encoder()


class Decoder:
    def __init__(self) -> None:
        self._paths: dict[int, Path] = {}

    def decode(self, wire: Sequence[Any]) -> list[Op]:
        """Ops as received: each is validated (``assertValidWireOp``) before use."""
        previous: Path | None = None  # scoped to the batch, as in encode
        out: list[Op] = []
        for op in wire:
            assertValidWireOp(op)
            verb = op[0]
            if verb == "#":
                assertSafePath(op[2])
                self._paths[op[1]] = op[2]
                continue
            if verb == "r":
                out.append(op)
                self._paths.clear()
                previous = None
                continue

            # Arity tells us whether a ref is present: the short forms omit it.
            short = (
                (verb == "d" and len(op) == 1)
                or (verb not in ("d", "p") and len(op) == 2)
                or (verb == "p" and len(op) == 4)
            )

            path: Path
            if short:
                if previous is None:
                    raise PathError([])
                path = previous
            else:
                ref = op[1]
                if isinstance(ref, int | float) and not isinstance(ref, bool):
                    resolved = self._paths.get(int(ref))
                    if resolved is None:
                        raise PathError(int(ref))
                    path = resolved
                else:
                    path = ref
                previous = path

            if verb not in ("p", "m") and len(path) == 0:
                raise PathError(path)
            if verb == "s":
                out.append(["s", path, op[1] if short else op[2]])
            elif verb == "d":
                out.append(["d", path])
            elif verb == "a":
                out.append(["a", path, op[1] if short else op[2]])
            elif verb == "t":
                out.append(["t", path, op[1] if short else op[2]])
            elif verb == "p":
                i, r, items = (op[1], op[2], op[3]) if short else (op[2], op[3], op[4])
                out.append(["p", path, i, r, items])
            elif verb == "m":
                out.append(["m", path, op[1] if short else op[2]])
        return out


def decoder() -> Decoder:
    return Decoder()


# ─── Immutable revision tracking ─────────────────────────────────────────────

from .diff import diffRevisions
from .draft import Draft
from .tracker import Change, Prepared, Tracker, track

__all__ = [
    "RESERVED_SEGMENTS",
    "Change",
    "Decoder",
    "Draft",
    "Encoder",
    "JsonValue",
    "NonEmptyPath",
    "Op",
    "Path",
    "PathError",
    "PathRef",
    "Prepared",
    "Seg",
    "Tracker",
    "UnsafePathError",
    "WireOp",
    "apply",
    "applyImmutable",
    "applyImmutableBatches",
    "assertSafePath",
    "assertValidOp",
    "assertValidWireOp",
    "decoder",
    "diffRevisions",
    "encoder",
    "isBase",
    "isReplace",
    "overlap",
    "track",
]
