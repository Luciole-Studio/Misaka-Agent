"""pi durable's ``test/session-support.ts``: a Session over controlled memory storage."""

from __future__ import annotations

import asyncio
import copy
import os
import time
from collections.abc import Sequence
from typing import Any

from misaka.chord.context import BACKGROUND_CONTEXT, Context
from misaka.durable.session.session import SessionImpl, setDefaultDocumentRetention
from misaka.durable.storage.memory import MemoryStorage
from misaka.utils.js import jsNumber

# MISAKA fork: pi's ``test/setup-document-retention.ts``, a vitest setup file loaded for every test file. misaka has no
# durable conftest, so this runs when this helper is first imported (directly or through the chat, harness, and task
# helpers), which pytest's collection does before any test runs: ``MISAKA_DURABLE_DOCUMENT_RETENTION_MS`` (upstream
# ``PI_DURABLE_DOCUMENT_RETENTION_MS``) sets the document retention of every Session that keeps the default, so the
# suite can run with each unattached document evicted right after use.
_forcedRetention = os.environ.get("MISAKA_DURABLE_DOCUMENT_RETENTION_MS")
if _forcedRetention is not None:
    setDefaultDocumentRetention(jsNumber(_forcedRetention))

context: Context = BACKGROUND_CONTEXT


class Deferred:
    def __init__(self) -> None:
        self.promise: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def resolve(self) -> None:
        if not self.promise.done():
            self.promise.set_result(None)


class Gate:
    """A gate that holds calls until released and reports when the first held call arrives."""

    def __init__(self, entered: Deferred, release: Any) -> None:
        self.entered = entered.promise
        self.release = release


class ControlledStorage(MemoryStorage):
    """Memory storage with observable commits, held calls, and injected commit failures."""

    def __init__(self) -> None:
        super().__init__()
        self.admittedCommits: list[Sequence[dict[str, Any]]] = []  # Exact borrowed batches admitted by Session.
        self.commits: list[Sequence[dict[str, Any]]] = []  # Detached batches for value assertions.
        self.mintCount = 0
        self.documentReadCount = 0
        self._commitGate: dict[str, Deferred] | None = None
        self._findGate: dict[str, Deferred] | None = None
        self._commitFailure: Exception | None = None

    def holdCommits(self) -> Gate:
        held = {"gate": Deferred(), "entered": Deferred()}
        self._commitGate = held
        return Gate(held["entered"], lambda: self._release("commit", held))

    def holdFindDocument(self) -> Gate:
        held = {"gate": Deferred(), "entered": Deferred()}
        self._findGate = held
        return Gate(held["entered"], lambda: self._release("find", held))

    def crash(self) -> None:
        """Simulate a crash during the held commit: it never reaches storage, and later commits proceed."""
        self._commitGate = None

    def failNextCommit(self, error: Exception) -> None:
        self._commitFailure = error

    def reopen(self) -> ControlledStorage:
        """Reopen after ``close()``, as a fresh process would reopen the same database, keeping every committed record."""
        self._closed = False
        return self

    def _release(self, kind: str, held: dict[str, Deferred]) -> None:
        if kind == "commit" and self._commitGate is held:
            self._commitGate = None
        if kind == "find" and self._findGate is held:
            self._findGate = None
        held["gate"].resolve()

    async def commit(self, writes: Sequence[dict[str, Any]], context: Context) -> int:  # type: ignore[override]
        self.admittedCommits.append(writes)
        self.commits.append(copy.deepcopy(writes))
        held = self._commitGate
        if held is not None:
            held["entered"].resolve()
            await held["gate"].promise
        failure = self._commitFailure
        if failure is not None:
            self._commitFailure = None
            raise failure
        return await super().commit(writes, context)

    def mintId(self) -> Any:  # type: ignore[override]
        self.mintCount += 1
        return super().mintId()

    def document(self, id: int, at: Any, context: Context) -> Any:  # type: ignore[override]
        self.documentReadCount += 1
        return super().document(id, at, context)

    async def findDocument(self, address: dict[str, Any], at: Any, context: Context) -> Any:  # type: ignore[override]
        held = self._findGate
        if held is not None:
            held["entered"].resolve()
            await held["gate"].promise
        return await super().findDocument(address, at, context)


class TestSession:
    """Session kernel plus its controlled storage and every committed publication."""

    __test__ = False

    def __init__(self) -> None:
        self.storage = ControlledStorage()
        # These tests pin the cache's identities and loads, so documents stay as long as the default keeps them.
        self.session = SessionImpl(self.storage, lambda: int(time.time() * 1000), lambda: 600_000)
        self.publications: list[dict[str, Any]] = []
        self.session.subscribeCommits(lambda publication, _context: self.publications.append(publication))


def openTestSession() -> TestSession:
    return TestSession()


def documentChanges(publication: dict[str, Any]) -> list[dict[str, Any]]:
    return [change for change in publication["changes"] if change["type"] == "document"]


def documentCopyChanges(publication: dict[str, Any]) -> list[dict[str, Any]]:
    return [change for change in publication["changes"] if change["type"] == "document.copy"]


async def createConversation(session: SessionImpl) -> int:
    """Create one conversation and return its ID."""

    async def create(tx: Any) -> int:
        return (await tx.createConversation({"ownership": {"kind": "ownerless"}}))["id"]

    return await session.commit(create, context)


async def flush() -> None:
    """Resolve after pending callbacks have run and one short timer turn."""
    await asyncio.sleep(0.002)


class AnyOf:
    """vitest ``expect.any(Type)``: equal to any instance of the given types."""

    def __init__(self, *types: type) -> None:
        self._types = types

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self._types) and not (isinstance(other, bool) and bool not in self._types)

    def __repr__(self) -> str:
        return "AnyOf(" + ", ".join(kind.__name__ for kind in self._types) + ")"


def toMatchObject(actual: Any, expected: Any, path: str = "$") -> None:
    """vitest ``toMatchObject``: ``expected`` is a subset of ``actual``, recursively; arrays match by length and element."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected an object, got {actual!r}"
        for key, value in expected.items():
            assert key in actual, f"{path}: missing key {key!r} in {actual!r}"
            toMatchObject(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected an array, got {actual!r}"
        assert len(actual) == len(expected), f"{path}: length {len(actual)} != {len(expected)}: {actual!r}"
        for index, value in enumerate(expected):
            toMatchObject(actual[index], value, f"{path}[{index}]")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


class ArrayContaining:
    """vitest ``expect.arrayContaining``: equal to any list that holds every given item."""

    def __init__(self, items: Sequence[Any]) -> None:
        self._items = list(items)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, list) and all(item in other for item in self._items)

    def __repr__(self) -> str:
        return f"ArrayContaining({self._items!r})"
