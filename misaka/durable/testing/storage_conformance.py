"""Runner-independent conformance cases for ``Storage`` backends.

Translated from pi's ``packages/durable/src/testing/storage-conformance.ts`` (v1.3.0). A case is ``{"name": ..., "run": ...}``
where ``run()`` returns an awaitable. ``withStorage`` must call and await its callback exactly once per case. The one
JavaScript-only case (prototype-like JSON keys changing object prototypes) is ported as a check that such keys are
ordinary keys and that retained values are detached.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Awaitable, Callable
from typing import Any

from misaka.chord.context import BACKGROUND_CONTEXT

from ..types import ROOT_CONVERSATION_ID, Storage
from .types import StorageConformanceAssertions

context = BACKGROUND_CONTEXT


async def createRoot(storage: Storage) -> int:
    await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], context)
    return ROOT_CONVERSATION_ID


def pendingTask(id: int, conversationId: int, phase: str = "ready") -> dict[str, Any]:
    return {
        "id": id,
        "conversationId": conversationId,
        "kind": "test.task",
        "version": 1,
        "input": {"value": id},
        "state": {"status": "pending", "checkpoint": {"phase": phase}},
        "background": False,
        "abortRequested": False,
    }


def entry(id: int, conversationId: int, kind: str = "message", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": id, "conversationId": conversationId, "kind": kind, **(extra or {})}


type ConformanceTest = Callable[[Storage], Awaitable[None]]


class _Rejects:
    def __init__(self, assertions: StorageConformanceAssertions, actual: Any) -> None:
        self._assertions = assertions
        self._actual = actual

    async def toThrow(self, messageIncludes: str) -> None:
        await self._assertions.rejects(self._actual, messageIncludes)


class _Resolves:
    def __init__(self, assertions: StorageConformanceAssertions, actual: Any) -> None:
        self._assertions = assertions
        self._actual = actual

    async def toBe(self, expected: Any) -> None:
        self._assertions.strictEqual(await self._actual, expected)


class AssertionResult:
    def __init__(self, assertions: StorageConformanceAssertions, actual: Any) -> None:
        self._assertions = assertions
        self._actual = actual
        self.rejects = _Rejects(assertions, actual)
        self.resolves = _Resolves(assertions, actual)

    def toBe(self, expected: Any) -> None:
        self._assertions.strictEqual(self._actual, expected)

    def toBeDefined(self) -> None:
        self._assertions.ok(self._actual is not None, "Expected value to be defined")

    def toBeGreaterThan(self, expected: float) -> None:
        self._assertions.greaterThan(self._actual, expected)

    def toBeUndefined(self) -> None:
        self._assertions.strictEqual(self._actual, None)

    def toEqual(self, expected: Any) -> None:
        self._assertions.deepEqual(self._actual, expected)

    def toHaveLength(self, expected: int) -> None:
        self._assertions.strictEqual(len(self._actual), expected)

    def toMatchObject(self, expected: Any) -> None:
        self._assertions.partialDeepEqual(self._actual, expected)


def assertionFacade(assertions: StorageConformanceAssertions) -> Callable[[Any], AssertionResult]:
    return lambda actual: AssertionResult(assertions, actual)


def createCase(options: dict[str, Any], name: str, test: ConformanceTest) -> dict[str, Any]:
    return {"name": name, "run": lambda: options["withStorage"](test)}


def createStorageConformance(options: dict[str, Any]) -> list[dict[str, Any]]:
    """Creates runner-independent cases. ``withStorage`` must call and await its callback exactly once per case."""
    expect = assertionFacade(options["assertions"])
    cases: list[dict[str, Any]] = []

    def case(name: str) -> Callable[[ConformanceTest], ConformanceTest]:
        def register(test: ConformanceTest) -> ConformanceTest:
            cases.append(createCase(options, name, test))
            return test

        return register

    @case("reserves ID 1 for the immutable root conversation")
    async def _(storage: Storage) -> None:
        expect(await storage.mintId()).toBe(2)
        await expect(createRoot(storage)).resolves.toBe(ROOT_CONVERSATION_ID)
        expect(await storage.conversation(ROOT_CONVERSATION_ID, context)).toEqual({"id": ROOT_CONVERSATION_ID})
        await expect(
            storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], context)
        ).rejects.toThrow(f"ID {ROOT_CONVERSATION_ID} already belongs to conversation")

    @case("commits mixed table writes atomically and rolls all of them back on failure")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        entryId = await storage.mintId()
        taskId = await storage.mintId()
        submissionId = await storage.mintId()
        task = pendingTask(taskId, rootId)
        input = {
            "id": submissionId,
            "conversationId": rootId,
            "requestId": "request-1",
            "type": "input",
            "status": "placed",
            "entry": entryId,
        }
        initialSeq = await storage.commit(
            [
                {"type": "entry", "value": entry(entryId, rootId, "user", {"data": {"text": "hello"}})},
                {"type": "task", "value": task},
                {"type": "submission", "value": input},
            ],
            context,
        )

        expect(await storage.entry(entryId, context)).toEqual(
            {"entry": entry(entryId, rootId, "user", {"data": {"text": "hello"}}), "commitSeq": initialSeq}
        )
        expect(await storage.task(taskId, context)).toEqual(task)
        expect(await storage.submission(submissionId, context)).toEqual(input)

        transientEntryId = await storage.mintId()
        runningTask = {**task, "state": {"status": "running", "checkpoint": {"phase": "effect"}}}
        doneInput = {**input, "status": "done", "answer": transientEntryId}
        await expect(
            storage.commit(
                [
                    {"type": "task", "value": runningTask},
                    {"type": "submission", "value": doneInput},
                    {"type": "entry", "value": entry(transientEntryId, rootId, "assistant")},
                    {"type": "conversation", "value": {"id": rootId}},
                ],
                context,
            )
        ).rejects.toThrow(f"ID {rootId} already belongs to conversation")

        expect(await storage.task(taskId, context)).toEqual(task)
        expect(await storage.submission(submissionId, context)).toEqual(input)
        expect(await storage.entry(transientEntryId, context)).toBeUndefined()
        afterRollbackSeq = await storage.commit(
            [{"type": "entry", "value": entry(await storage.mintId(), rootId, "after-rollback")}], context
        )
        expect(afterRollbackSeq).toBeGreaterThan(initialSeq)

    @case("detaches retained writes and every returned record")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        entryId = await storage.mintId()
        taskId = await storage.mintId()
        submissionId = await storage.mintId()
        entryData: dict[str, Any] = {"nested": [1, 2]}
        checkpoint: dict[str, Any] = {"phase": "ready", "nested": {"count": 1}}
        detail: dict[str, Any] = {"codes": ["initial"]}
        storedEntry = entry(entryId, rootId, "note", {"data": entryData})
        storedTask = {**pendingTask(taskId, rootId), "state": {"status": "pending", "checkpoint": checkpoint}}
        storedInput = {
            "id": submissionId,
            "conversationId": rootId,
            "type": "input",
            "status": "unanswered",
            "reason": "failed",
            "detail": detail,
        }
        await storage.commit(
            [
                {"type": "entry", "value": storedEntry},
                {"type": "task", "value": storedTask},
                {"type": "submission", "value": storedInput},
            ],
            context,
        )

        entryData["nested"].append(3)
        checkpoint["nested"]["count"] = 2
        detail["codes"].append("mutated")
        expect((await storage.entry(entryId, context))["entry"]["data"]).toEqual({"nested": [1, 2]})  # type: ignore[index]
        expect((await storage.task(taskId, context))["state"]).toEqual(  # type: ignore[index]
            {"status": "pending", "checkpoint": {"phase": "ready", "nested": {"count": 1}}}
        )
        expect((await storage.submission(submissionId, context))["detail"]).toEqual({"codes": ["initial"]})  # type: ignore[index]

        readEntry = (await storage.entry(entryId, context))["entry"]  # type: ignore[index]
        readEntry["data"]["nested"].append(9)
        readTask = await storage.task(taskId, context)
        assert readTask is not None
        if readTask["state"]["status"] != "terminal":
            readTask["state"]["checkpoint"]["nested"]["count"] = 9
        readInput = await storage.submission(submissionId, context)
        assert readInput is not None
        readInput["detail"]["codes"].append("read mutation")

        expect((await storage.entry(entryId, context))["entry"]["data"]).toEqual({"nested": [1, 2]})  # type: ignore[index]
        expect((await storage.task(taskId, context))["state"]).toEqual(  # type: ignore[index]
            {"status": "pending", "checkpoint": {"phase": "ready", "nested": {"count": 1}}}
        )
        expect((await storage.submission(submissionId, context))["detail"]).toEqual({"codes": ["initial"]})  # type: ignore[index]

    @case("detaches prototype-like JSON keys without changing object prototypes")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        entryId = await storage.mintId()
        data = json.loads('{"__proto__":{"polluted":false},"constructor":{"label":"stored"},"toString":"value"}')
        await storage.commit([{"type": "entry", "value": entry(entryId, rootId, "note", {"data": data})}], context)

        data["__proto__"]["polluted"] = True
        data["constructor"]["label"] = "mutated"
        firstRead = (await storage.entry(entryId, context))["entry"]["data"]  # type: ignore[index]
        expect("__proto__" in firstRead).toBe(True)
        expect(firstRead["__proto__"]).toEqual({"polluted": False})
        expect(firstRead["constructor"]).toEqual({"label": "stored"})
        expect(firstRead["toString"]).toBe("value")
        expect(dict[str, Any]().get("polluted")).toBeUndefined()

        firstRead["__proto__"]["polluted"] = True
        secondRead = (await storage.entry(entryId, context))["entry"]["data"]  # type: ignore[index]
        expect(secondRead["__proto__"]).toEqual({"polluted": False})
        expect(secondRead["constructor"]).toEqual({"label": "stored"})
        expect(secondRead["toString"]).toBe("value")

    @case("indexes entries committed out of ID order")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        await storage.commit(
            [
                {"type": "entry", "value": entry(30, rootId)},
                {"type": "entry", "value": entry(10, rootId)},
                {"type": "entry", "value": entry(20, rootId, "marker", {"head": 10})},
            ],
            context,
        )

        expect(
            [item["id"] for item in (await storage.scanEntries({"conversationId": rootId}, 10, None, context))["items"]]
        ).toEqual([30, 20, 10])
        expect((await storage.findLatestHeadMarker(rootId, None, context))["id"]).toBe(20)  # type: ignore[index]

    @case("continues an entry cursor below its last item after a newer commit")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        oldestId = await storage.mintId()
        middleId = await storage.mintId()
        newestId = await storage.mintId()
        await storage.commit(
            [
                {"type": "entry", "value": entry(oldestId, rootId)},
                {"type": "entry", "value": entry(middleId, rootId)},
                {"type": "entry", "value": entry(newestId, rootId)},
            ],
            context,
        )

        first = await storage.scanEntries({"conversationId": rootId}, 2, None, context)
        expect([item["id"] for item in first["items"]]).toEqual([newestId, middleId])
        appendedId = await storage.mintId()
        await storage.commit([{"type": "entry", "value": entry(appendedId, rootId)}], context)
        second = await storage.scanEntries({"conversationId": rootId}, 2, first.get("next"), context)
        expect([item["id"] for item in second["items"]]).toEqual([oldestId])
        expect(second.get("next")).toBeUndefined()

    @case("paginates conversations by opaque cursor in ascending ID order")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        secondId = await storage.mintId()
        thirdId = await storage.mintId()
        await storage.commit(
            [
                {"type": "conversation", "value": {"id": thirdId}},
                {"type": "conversation", "value": {"id": secondId}},
            ],
            context,
        )

        first = await storage.scanConversations({}, 2, None, context)
        expect([item["id"] for item in first["items"]]).toEqual([rootId, secondId])
        expect(first.get("next")).toBeDefined()
        roundTrippedCursor = json.loads(json.dumps(first["next"]))
        second = await storage.scanConversations({}, 2, roundTrippedCursor, context)
        expect([item["id"] for item in second["items"]]).toEqual([thirdId])
        expect(second.get("next")).toBeUndefined()

    @case("filters and pages conversations by durable owner edges")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        otherOwnerId = await storage.mintId()
        firstTaskId = await storage.mintId()
        secondTaskId = await storage.mintId()
        firstId = await storage.mintId()
        secondId = await storage.mintId()
        thirdId = await storage.mintId()
        await storage.commit(
            [
                {"type": "conversation", "value": {"id": otherOwnerId}},
                {
                    "type": "conversation",
                    "value": {"id": firstId, "owner": {"conversationId": rootId, "taskId": firstTaskId}},
                },
                {
                    "type": "conversation",
                    "value": {"id": secondId, "owner": {"conversationId": rootId, "taskId": secondTaskId}},
                },
                {
                    "type": "conversation",
                    "value": {"id": thirdId, "owner": {"conversationId": otherOwnerId, "taskId": firstTaskId}},
                },
            ],
            context,
        )

        first = await storage.scanConversations({"ownerConversationId": rootId}, 1, None, context)
        expect([item["id"] for item in first["items"]]).toEqual([firstId])
        expect(first.get("next")).toBeDefined()
        second = await storage.scanConversations({"ownerConversationId": rootId}, 1, first.get("next"), context)
        expect([item["id"] for item in second["items"]]).toEqual([secondId])
        expect(second.get("next")).toBeUndefined()
        expect(
            [item["id"] for item in (await storage.scanConversations({"ownerTaskId": firstTaskId}, 10, None, context))["items"]]
        ).toEqual([firstId, thirdId])
        expect(
            [
                item["id"]
                for item in (
                    await storage.scanConversations(
                        {"ownerConversationId": rootId, "ownerTaskId": firstTaskId}, 10, None, context
                    )
                )["items"]
            ]
        ).toEqual([firstId])

    @case("scans deep fork history newest-first through every ancestor cap")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        rootFirst = await storage.mintId()
        rootForkPoint = await storage.mintId()
        rootExcludedSameCommit = await storage.mintId()
        rootEntriesSeq = await storage.commit(
            [
                {"type": "entry", "value": entry(rootFirst, rootId)},
                {"type": "entry", "value": entry(rootForkPoint, rootId, "marker", {"head": rootFirst})},
                {"type": "entry", "value": entry(rootExcludedSameCommit, rootId)},
            ],
            context,
        )
        childId = await storage.mintId()
        await storage.commit(
            [{"type": "conversation", "value": {"id": childId, "parent": {"conversationId": rootId, "at": rootForkPoint}}}],
            context,
        )
        childForkPoint = await storage.mintId()
        childExcluded = await storage.mintId()
        await storage.commit(
            [
                {"type": "entry", "value": entry(childForkPoint, childId, "note")},
                {"type": "entry", "value": entry(childExcluded, childId)},
            ],
            context,
        )
        rootExcludedLater = await storage.mintId()
        await storage.commit([{"type": "entry", "value": entry(rootExcludedLater, rootId)}], context)
        grandchildId = await storage.mintId()
        await storage.commit(
            [
                {
                    "type": "conversation",
                    "value": {"id": grandchildId, "parent": {"conversationId": childId, "at": childForkPoint}},
                }
            ],
            context,
        )
        grandchildHead = await storage.mintId()
        grandchildTail = await storage.mintId()
        grandchildEntriesSeq = await storage.commit(
            [
                {"type": "entry", "value": entry(grandchildHead, grandchildId, "marker", {"head": grandchildHead})},
                {"type": "entry", "value": entry(grandchildTail, grandchildId)},
            ],
            context,
        )
        childExcludedLater = await storage.mintId()
        await storage.commit([{"type": "entry", "value": entry(childExcludedLater, childId)}], context)

        ids = lambda page: [item["id"] for item in page["items"]]
        first = await storage.scanEntries({"conversationId": grandchildId}, 2, None, context)
        expect(ids(first)).toEqual([grandchildTail, grandchildHead])
        second = await storage.scanEntries({"conversationId": grandchildId}, 2, first.get("next"), context)
        expect(ids(second)).toEqual([childForkPoint, rootForkPoint])
        third = await storage.scanEntries({"conversationId": grandchildId}, 2, second.get("next"), context)
        expect(ids(third)).toEqual([rootFirst])
        expect(third.get("next")).toBeUndefined()

        # Oldest first: the root's segment, then each fork's, each capped at the next fork point.
        ascending = {"conversationId": grandchildId, "order": "ascending"}
        firstUp = await storage.scanEntries(ascending, 2, None, context)
        expect(ids(firstUp)).toEqual([rootFirst, rootForkPoint])
        secondUp = await storage.scanEntries(ascending, 2, firstUp.get("next"), context)
        expect(ids(secondUp)).toEqual([childForkPoint, grandchildHead])
        thirdUp = await storage.scanEntries({"conversationId": grandchildId}, 2, secondUp.get("next"), context)
        expect(ids(thirdUp)).toEqual([grandchildTail])
        expect(thirdUp.get("next")).toBeUndefined()
        expect(
            ids(
                await storage.scanEntries(
                    {**ascending, "minEntryId": rootForkPoint, "maxEntryId": childForkPoint}, 10, None, context
                )
            )
        ).toEqual([rootForkPoint, childForkPoint])
        await expect(
            storage.scanEntries({"conversationId": grandchildId, "order": "descending"}, 2, firstUp.get("next"), context)
        ).rejects.toThrow("cursor")

        currentMarker = await storage.findLatestHeadMarker(grandchildId, None, context)
        assert currentMarker is not None
        expect(currentMarker["id"]).toBe(grandchildHead)
        expect(currentMarker["head"]).toBe(grandchildHead)
        historicalMarker = await storage.findLatestHeadMarker(grandchildId, childForkPoint, context)
        assert historicalMarker is not None
        expect(historicalMarker["id"]).toBe(rootForkPoint)
        expect(historicalMarker["head"]).toBe(rootFirst)
        expect(await storage.findLatestHeadMarker(grandchildId, rootFirst, context)).toBeUndefined()

        activeFirst = await storage.scanEntries(
            {"conversationId": grandchildId, "minEntryId": currentMarker["head"]}, 1, None, context
        )
        expect(ids(activeFirst)).toEqual([grandchildTail])
        expect(activeFirst.get("next")).toBeDefined()
        activeSecond = await storage.scanEntries(
            {"conversationId": grandchildId, "minEntryId": currentMarker["head"]}, 1, activeFirst.get("next"), context
        )
        expect(ids(activeSecond)).toEqual([grandchildHead])
        expect(activeSecond.get("next")).toBeUndefined()

        expect(
            ids(
                await storage.scanEntries(
                    {
                        "conversationId": grandchildId,
                        "minEntryId": historicalMarker["head"],
                        "maxEntryId": childForkPoint,
                    },
                    10,
                    None,
                    context,
                )
            )
        ).toEqual([childForkPoint, rootForkPoint, rootFirst])

        expect(await storage.entry(rootFirst, context)).toEqual(
            {"entry": entry(rootFirst, rootId), "commitSeq": rootEntriesSeq}
        )
        expect((await storage.entry(rootForkPoint, context))["commitSeq"]).toBe(rootEntriesSeq)  # type: ignore[index]
        expect((await storage.entry(grandchildHead, context))["commitSeq"]).toBe(grandchildEntriesSeq)  # type: ignore[index]
        expect((await storage.entry(grandchildTail, context))["commitSeq"]).toBe(grandchildEntriesSeq)  # type: ignore[index]
        expect(await storage.entry(999_999, context)).toBeUndefined()

        expect(await storage.entry(grandchildId, rootFirst, context)).toEqual(
            {"entry": entry(rootFirst, rootId), "commitSeq": rootEntriesSeq}
        )
        expect((await storage.entry(grandchildId, childForkPoint, context))["entry"]["conversationId"]).toBe(childId)  # type: ignore[index]
        expect((await storage.entry(grandchildId, grandchildTail, context))["commitSeq"]).toBe(grandchildEntriesSeq)  # type: ignore[index]
        expect(await storage.entry(grandchildId, rootExcludedSameCommit, context)).toBeUndefined()
        expect(await storage.entry(grandchildId, rootExcludedLater, context)).toBeUndefined()
        expect(await storage.entry(grandchildId, childExcluded, context)).toBeUndefined()
        expect(await storage.entry(grandchildId, childExcludedLater, context)).toBeUndefined()
        expect(await storage.entry(rootId, grandchildHead, context)).toBeUndefined()
        expect(await storage.entry(grandchildId, 999_999, context)).toBeUndefined()
        await expect(storage.entry(999_999, rootFirst, context)).rejects.toThrow("Unknown conversation")
        await expect(storage.scanEntries({"conversationId": 999_999}, 10, None, context)).rejects.toThrow(
            "Unknown conversation"
        )

    @case("scans tables in either ID order and continues a cursor in its order")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        conversationIds = [rootId]
        taskIds: list[int] = []
        submissionIds: list[int] = []
        for _index in range(3):
            conversationId = await storage.mintId()
            taskId = await storage.mintId()
            submissionId = await storage.mintId()
            await storage.commit(
                [
                    {"type": "conversation", "value": {"id": conversationId}},
                    {"type": "task", "value": pendingTask(taskId, rootId)},
                    {
                        "type": "submission",
                        "value": {"id": submissionId, "conversationId": rootId, "type": "input", "status": "queued"},
                    },
                ],
                context,
            )
            conversationIds.append(conversationId)
            taskIds.append(taskId)
            submissionIds.append(submissionId)

        def query(order: str | None) -> dict[str, Any]:
            # `{ order }` with `order` undefined: the query has no order.
            return {} if order is None else {"order": order}

        scans: list[tuple[Callable[[str | None, Any], Any], list[int]]] = [
            (lambda order, cursor: storage.scanConversations(query(order), 2, cursor, context), conversationIds),
            (lambda order, cursor: storage.scanTasks(query(order), 2, cursor, context), taskIds),
            (lambda order, cursor: storage.scanSubmissions(query(order), 2, cursor, context), submissionIds),
        ]

        async def all_(scan: Callable[[str | None, Any], Any], order: str | None) -> list[int]:
            found: list[int] = []
            page = await scan(order, None)
            found.extend(item["id"] for item in page["items"])
            while page.get("next") is not None:
                # A cursor carries its order; the query may omit it.
                page = await scan(None, json.loads(json.dumps(page["next"])))
                found.extend(item["id"] for item in page["items"])
            return found

        for scan, idList in scans:
            reversedIds = list(reversed(idList))
            expect(await all_(scan, None)).toEqual(idList)
            expect(await all_(scan, "ascending")).toEqual(idList)
            expect(await all_(scan, "descending")).toEqual(reversedIds)
            descending = await scan("descending", None)
            expect([item["id"] for item in (await scan("descending", descending["next"]))["items"]]).toEqual(
                reversedIds[2:4]
            )
            await expect(scan("ascending", descending["next"])).rejects.toThrow("cursor")

    @case("replaces complete task records and pages filtered task scans")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        firstId = await storage.mintId()
        secondId = await storage.mintId()
        thirdId = await storage.mintId()
        first = {**pendingTask(firstId, rootId), "memos": {"winner": "first"}}
        second = {**pendingTask(secondId, rootId), "background": True}
        third = {**pendingTask(thirdId, rootId), "abortRequested": True}
        await storage.commit(
            [
                {"type": "task", "value": first},
                {"type": "task", "value": second},
                {"type": "task", "value": third},
            ],
            context,
        )

        running = {
            **first,
            "state": {"status": "running", "checkpoint": {"phase": "effect", "attempt": 1}},
            "abortRequested": True,
        }
        await storage.commit([{"type": "task", "value": running}], context)
        expect(await storage.task(firstId, context)).toEqual(running)
        terminal = {
            "id": firstId,
            "conversationId": rootId,
            "kind": first["kind"],
            "version": first["version"],
            "input": first["input"],
            "state": {"status": "terminal", "outcome": {"status": "completed", "result": {"entryId": 99}}},
            "background": False,
            "abortRequested": True,
        }
        await storage.commit([{"type": "task", "value": terminal}], context)
        expect(await storage.task(firstId, context)).toEqual(terminal)

        pendingPage = await storage.scanTasks({"status": "pending"}, 1, None, context)
        expect([item["id"] for item in pendingPage["items"]]).toEqual([secondId])
        expect(pendingPage.get("next")).toBeDefined()
        expect(
            [item["id"] for item in (await storage.scanTasks({"status": "pending"}, 1, pendingPage.get("next"), context))["items"]]
        ).toEqual([thirdId])
        expect(
            (await storage.scanTasks({"status": "terminal", "abortRequested": True}, 10, None, context))["items"]
        ).toEqual([terminal])
        expect(
            [item["id"] for item in (await storage.scanTasks({"background": True}, 10, None, context))["items"]]
        ).toEqual([secondId])

    @case("stores owners and scans waiting and completing tasks by status")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        ownerId = await storage.mintId()
        waitingId = await storage.mintId()
        completingId = await storage.mintId()
        owner = pendingTask(ownerId, rootId)
        waiting = {
            **pendingTask(waitingId, rootId),
            "owner": ownerId,
            "state": {"status": "waiting", "checkpoint": {"phase": "next"}, "on": [ownerId], "policy": "allSettled"},
            "memos": {"kept": True},
        }
        base = {key: value for key, value in pendingTask(completingId, rootId).items() if key != "state"}
        completing = {
            **base,
            "owner": ownerId,
            "state": {"status": "completing", "outcome": {"status": "failed", "error": {"message": "held"}}},
        }
        await storage.commit([{"type": "task", "value": value} for value in (owner, waiting, completing)], context)
        expect(await storage.task(waitingId, context)).toEqual(waiting)
        expect(await storage.task(completingId, context)).toEqual(completing)

        async def scan(status: str) -> list[dict[str, Any]]:
            return (await storage.scanTasks({"status": status}, 10, None, context))["items"]

        expect(await scan("waiting")).toEqual([waiting])
        expect(await scan("completing")).toEqual([completing])
        expect([item["id"] for item in await scan("pending")]).toEqual([ownerId])
        terminal = {**completing, "state": {"status": "terminal", "outcome": completing["state"]["outcome"]}}
        await storage.commit([{"type": "task", "value": terminal}], context)
        expect(await scan("completing")).toEqual([])
        expect(await scan("terminal")).toEqual([terminal])

    @case("indexes request IDs per conversation and replaces complete submission records")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        secondConversationId = await storage.mintId()
        await storage.commit([{"type": "conversation", "value": {"id": secondConversationId}}], context)
        firstId = await storage.mintId()
        secondId = await storage.mintId()
        otherConversationId = await storage.mintId()
        first = {"id": firstId, "conversationId": rootId, "requestId": "same", "type": "input", "status": "queued"}
        second = {"id": secondId, "conversationId": rootId, "requestId": "other", "type": "input", "status": "queued"}
        otherConversation = {
            "id": otherConversationId,
            "conversationId": secondConversationId,
            "requestId": "same",
            "type": "input",
            "status": "queued",
        }
        await storage.commit(
            [
                {"type": "submission", "value": first},
                {"type": "submission", "value": second},
                {"type": "submission", "value": otherConversation},
            ],
            context,
        )
        expect(await storage.submissionByRequest(rootId, "same", context)).toEqual(first)
        expect(await storage.submissionByRequest(secondConversationId, "same", context)).toEqual(otherConversation)

        placedSecond = {**second, "status": "placed", "entry": await storage.mintId()}
        await storage.commit([{"type": "submission", "value": placedSecond}], context)
        expect(await storage.submission(secondId, context)).toEqual(placedSecond)
        expect(await storage.submissionByRequest(rootId, "other", context)).toEqual(placedSecond)

        async def ids(query: dict[str, Any]) -> list[int]:
            found: list[int] = []
            cursor = None
            while True:
                page = await storage.scanSubmissions(query, 1, cursor, context)
                found.extend(item["id"] for item in page["items"])
                cursor = page.get("next")
                if cursor is None:
                    break
            return found

        expect(await ids({})).toEqual([firstId, secondId, otherConversationId])
        expect(await ids({"conversationId": rootId})).toEqual([firstId, secondId])
        # A status change moves the record between status scans.
        expect(await ids({"status": "queued"})).toEqual([firstId, otherConversationId])
        expect(await ids({"status": "placed"})).toEqual([secondId])
        expect(await ids({"conversationId": secondConversationId, "status": "queued"})).toEqual([otherConversationId])
        expect(await ids({"conversationId": secondConversationId, "status": "placed"})).toEqual([])
        expect((await storage.scanSubmissions({"status": "placed"}, 10, None, context))["items"]).toEqual([placedSecond])

    @case("stores passive write submissions without input-only lifecycle states")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        doneId = await storage.mintId()
        failedId = await storage.mintId()
        queuedDone = {
            "id": doneId,
            "conversationId": rootId,
            "requestId": "passive-done",
            "type": "write",
            "status": "queued",
        }
        queuedFailed = {
            "id": failedId,
            "conversationId": rootId,
            "requestId": "passive-failed",
            "type": "write",
            "status": "queued",
        }
        await storage.commit(
            [{"type": "submission", "value": queuedDone}, {"type": "submission", "value": queuedFailed}], context
        )

        done = {**queuedDone, "status": "done", "entry": await storage.mintId()}
        unanswered = {**queuedFailed, "status": "unanswered", "reason": "closed", "detail": {"retryable": False}}
        await storage.commit([{"type": "submission", "value": done}, {"type": "submission", "value": unanswered}], context)
        expect(await storage.submission(doneId, context)).toEqual(done)
        expect(await storage.submissionByRequest(rootId, "passive-done", context)).toEqual(done)
        expect(await storage.submission(failedId, context)).toEqual(unanswered)
        expect(await storage.submissionByRequest(rootId, "passive-failed", context)).toEqual(unanswered)

    @case("reconstructs rewindable documents and preserves half-open incarnations")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        firstId = await storage.mintId()
        firstRecord = {
            "id": firstId,
            "kind": "conversation.notes",
            "scope": {"kind": "conversation", "conversationId": rootId},
            "history": "rewindable",
            "fork": "asOf",
        }
        initial: dict[str, Any] = {"items": ["a"], "nested": {"count": 1}}
        createdAt = await storage.commit(
            [{"type": "document.create", "record": firstRecord, "content": {"kind": "base", "version": 1, "value": initial}}],
            context,
        )
        appended = ["b"]
        ops = [["p", ["items"], 1, 0, appended], ["s", ["nested", "count"], 2]]
        changedAt = await storage.commit(
            [{"type": "document.change", "id": firstId, "content": {"kind": "delta", "version": 1, "ops": ops}}], context
        )

        initial["items"].append("caller mutation")
        appended.append("caller mutation")
        expect(await storage.document(firstId, createdAt, context)).toMatchObject(
            {"version": 1, "value": {"items": ["a"], "nested": {"count": 1}}, "deltasSinceBase": 0}
        )
        changed = await storage.document(firstId, changedAt, context)
        assert changed is not None
        expect(changed["value"]).toEqual({"items": ["a", "b"], "nested": {"count": 2}})
        expect(changed["deltasSinceBase"]).toBe(1)
        changed["value"]["items"].append("read mutation")
        expect((await storage.document(firstId, "current", context))["value"]).toEqual(  # type: ignore[index]
            {"items": ["a", "b"], "nested": {"count": 2}}
        )

        checkpointAt = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": firstId,
                    "content": {"kind": "base", "version": 2, "value": {"items": ["checkpoint"], "nested": {"count": 3}}},
                }
            ],
            context,
        )
        replacedAt = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": firstId,
                    "content": {
                        "kind": "delta",
                        "version": 2,
                        "ops": [["r", {"items": ["replacement"], "nested": {"count": 4}}]],
                    },
                }
            ],
            context,
        )
        expect(await storage.document(firstId, changedAt, context)).toMatchObject(
            {"version": 1, "value": {"items": ["a", "b"], "nested": {"count": 2}}}
        )
        expect(await storage.document(firstId, checkpointAt, context)).toMatchObject(
            {"version": 2, "value": {"items": ["checkpoint"], "nested": {"count": 3}}, "deltasSinceBase": 0}
        )
        expect(await storage.document(firstId, replacedAt, context)).toMatchObject(
            {"value": {"items": ["replacement"], "nested": {"count": 4}}, "deltasSinceBase": 1}
        )
        expect((await storage.document(firstId, "current", context))["deltasSinceBase"]).toBe(1)  # type: ignore[index]

        secondId = await storage.mintId()
        retiredAt = await storage.commit(
            [
                {
                    "type": "document.create",
                    "record": {**firstRecord, "id": secondId},
                    "content": {"kind": "base", "version": 1, "value": {"items": ["new"]}},
                },
                {"type": "document.retire", "id": firstId},
                {
                    "type": "document.change",
                    "id": firstId,
                    "content": {"kind": "delta", "version": 2, "ops": [["s", ["retiring"], True]]},
                },
            ],
            context,
        )
        address = {"kind": firstRecord["kind"], "scope": firstRecord["scope"]}
        expect((await storage.findDocument(address, changedAt, context))["id"]).toBe(firstId)  # type: ignore[index]
        expect(await storage.findDocument(address, retiredAt, context)).toMatchObject(
            {"id": secondId, "createdAt": retiredAt}
        )
        expect(
            [
                item["id"]
                for item in (
                    await storage.scanDocuments({"scope": firstRecord["scope"], "at": changedAt}, 10, None, context)
                )["items"]
            ]
        ).toEqual([firstId])
        expect(
            [
                item["id"]
                for item in (
                    await storage.scanDocuments({"scope": firstRecord["scope"], "at": retiredAt}, 10, None, context)
                )["items"]
            ]
        ).toEqual([secondId])
        expect(await storage.document(firstId, retiredAt, context)).toBeUndefined()
        expect((await storage.document(secondId, "current", context))["value"]).toEqual({"items": ["new"]})  # type: ignore[index]

    @case("streams long document tails across root replacement deltas")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        id = await storage.mintId()
        record = {
            "id": id,
            "kind": "conversation.long-tail",
            "scope": {"kind": "conversation", "conversationId": rootId},
            "history": "rewindable",
            "fork": "asOf",
        }
        initial: dict[str, Any] = {
            "revision": 0,
            "rows": [{"value": value, "stable": f"row-{value}"} for value in range(512)],
        }
        createdAt = await storage.commit(
            [{"type": "document.create", "record": record, "content": {"kind": "base", "version": 1, "value": initial}}],
            context,
        )
        beforeReplacement = copy.deepcopy(initial)
        beforeReplacementAt = createdAt
        for revision in range(1, 25):
            index = (revision * 17) % len(beforeReplacement["rows"])
            beforeReplacement["rows"][index]["value"] = -revision
            beforeReplacement["revision"] = revision
            beforeReplacementAt = await storage.commit(
                [
                    {
                        "type": "document.change",
                        "id": id,
                        "content": {
                            "kind": "delta",
                            "version": 1,
                            "ops": [["s", ["rows", index, "value"], -revision], ["s", ["revision"], revision]],
                        },
                    }
                ],
                context,
            )

        replacement: dict[str, Any] = {
            "revision": 100,
            "rows": [{"value": 10_000 + value, "stable": f"new-{value}"} for value in range(512)],
        }
        replacementSnapshot = copy.deepcopy(replacement)
        replacementAt = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": id,
                    "content": {"kind": "delta", "version": 1, "ops": [["r", replacement]]},
                }
            ],
            context,
        )
        replacement["rows"][0]["value"] = -999

        current = copy.deepcopy(replacementSnapshot)
        for revision in range(101, 125):
            index = (revision * 19) % len(current["rows"])
            current["rows"][index]["value"] = -revision
            current["revision"] = revision
            await storage.commit(
                [
                    {
                        "type": "document.change",
                        "id": id,
                        "content": {
                            "kind": "delta",
                            "version": 1,
                            "ops": [["s", ["rows", index, "value"], -revision], ["s", ["revision"], revision]],
                        },
                    }
                ],
                context,
            )

        expect((await storage.document(id, createdAt, context))["value"]).toEqual(initial)  # type: ignore[index]
        expect((await storage.document(id, beforeReplacementAt, context))["value"]).toEqual(beforeReplacement)  # type: ignore[index]
        expect((await storage.document(id, replacementAt, context))["value"]).toEqual(replacementSnapshot)  # type: ignore[index]
        read = await storage.document(id, "current", context)
        assert read is not None
        expect(read["value"]).toEqual(current)
        read["value"]["rows"][0]["value"] = -1_000
        expect((await storage.document(id, "current", context))["value"]).toEqual(current)  # type: ignore[index]

    @case("copies stored document bases independently and rejects ambiguous sources")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        childId = await storage.mintId()
        secondChildId = await storage.mintId()
        await storage.commit(
            [
                {"type": "conversation", "value": {"id": childId}},
                {"type": "conversation", "value": {"id": secondChildId}},
            ],
            context,
        )
        sourceId = await storage.mintId()
        sourceRecord = {
            "id": sourceId,
            "kind": "copy.source",
            "scope": {"kind": "conversation", "conversationId": rootId},
            "history": "rewindable",
            "fork": "asOf",
        }
        createdAt = await storage.commit(
            [
                {
                    "type": "document.create",
                    "record": sourceRecord,
                    "content": {"kind": "base", "version": 2, "value": {"count": 1, "rows": [{"value": "base"}]}},
                }
            ],
            context,
        )
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": sourceId,
                    "content": {
                        "kind": "delta",
                        "version": 2,
                        "ops": [["s", ["count"], 2], ["p", ["rows"], 1, 0, [{"value": "current"}]]],
                    },
                }
            ],
            context,
        )
        historicalCopyId = await storage.mintId()
        currentCopyId = await storage.mintId()
        retiredCopyId = await storage.mintId()

        def childRecord(id: int, conversationId: int) -> dict[str, Any]:
            return {
                "id": id,
                "kind": sourceRecord["kind"],
                "scope": {"kind": "conversation", "conversationId": conversationId},
                "history": "rewindable",
                "fork": "asOf",
            }

        await storage.commit(
            [
                {
                    "type": "document.copy",
                    "record": childRecord(historicalCopyId, childId),
                    "source": {"id": sourceId, "at": createdAt},
                },
                {
                    "type": "document.copy",
                    "record": childRecord(currentCopyId, secondChildId),
                    "source": {"id": sourceId, "at": "current"},
                },
                {
                    "type": "document.copy",
                    "record": childRecord(retiredCopyId, rootId),
                    "source": {"id": sourceId, "at": "current"},
                },
                {"type": "document.retire", "id": retiredCopyId},
            ],
            context,
        )
        expect(await storage.document(historicalCopyId, "current", context)).toMatchObject(
            {"version": 2, "value": {"count": 1, "rows": [{"value": "base"}]}}
        )
        expect(await storage.document(currentCopyId, "current", context)).toMatchObject(
            {"version": 2, "value": {"count": 2, "rows": [{"value": "base"}, {"value": "current"}]}}
        )
        expect(await storage.document(retiredCopyId, "current", context)).toBeUndefined()

        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": sourceId,
                    "content": {"kind": "base", "version": 2, "value": {"count": 99, "rows": []}},
                },
                {"type": "document.retire", "id": sourceId},
            ],
            context,
        )
        expect((await storage.document(currentCopyId, "current", context))["value"]).toEqual(  # type: ignore[index]
            {"count": 2, "rows": [{"value": "base"}, {"value": "current"}]}
        )

        latestSourceId = await storage.mintId()
        latestCopyId = await storage.mintId()
        latestSource = {
            "id": latestSourceId,
            "kind": "copy.latest",
            "scope": {"kind": "conversation", "conversationId": rootId},
            "history": "latest",
            "fork": "current",
        }
        await storage.commit(
            [
                {
                    "type": "document.create",
                    "record": latestSource,
                    "content": {"kind": "base", "version": 4, "value": {"retained": "copy"}},
                }
            ],
            context,
        )
        await storage.commit(
            [
                {
                    "type": "document.copy",
                    "record": {
                        **latestSource,
                        "id": latestCopyId,
                        "scope": {"kind": "conversation", "conversationId": childId},
                    },
                    "source": {"id": latestSourceId, "at": "current"},
                }
            ],
            context,
        )
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": latestSourceId,
                    "content": {"kind": "base", "version": 4, "value": {"retained": "source-only"}},
                },
                {"type": "document.retire", "id": latestSourceId},
            ],
            context,
        )
        expect(await storage.document(latestCopyId, "current", context)).toMatchObject(
            {"version": 4, "value": {"retained": "copy"}}
        )

        conflictId = await storage.mintId()
        conflictError: Exception | None = None
        try:
            await storage.commit(
                [
                    {
                        "type": "document.copy",
                        "record": childRecord(conflictId, childId),
                        "source": {"id": currentCopyId, "at": "current"},
                    },
                    {"type": "document.retire", "id": currentCopyId},
                ],
                context,
            )
        except Exception as error:  # noqa: BLE001
            conflictError = error
        # Rejected without effect: the copy and the retirement in the same batch are both absent.
        expect(isinstance(conflictError, Exception)).toBe(True)
        expect(await storage.document(conflictId, "current", context)).toBeUndefined()
        expect((await storage.document(currentCopyId, "current", context))["value"]).toEqual(  # type: ignore[index]
            {"count": 2, "rows": [{"value": "base"}, {"value": "current"}]}
        )

        mismatchId = await storage.mintId()
        mismatchError: Exception | None = None
        try:
            await storage.commit(
                [
                    {
                        "type": "document.copy",
                        "record": {**childRecord(mismatchId, childId), "kind": "copy.mismatch"},
                        "source": {"id": currentCopyId, "at": "current"},
                    }
                ],
                context,
            )
        except Exception as error:  # noqa: BLE001
            mismatchError = error
        expect(isinstance(mismatchError, Exception)).toBe(True)
        expect(await storage.document(mismatchId, "current", context)).toBeUndefined()

    @case("uses bases for version transitions and rejects historical reads of current-only documents")
    async def _(storage: Storage) -> None:
        await createRoot(storage)
        id = await storage.mintId()
        record = {"id": id, "kind": "session.settings", "scope": {"kind": "session"}}
        await storage.commit(
            [{"type": "document.create", "record": record, "content": {"kind": "base", "version": 1, "value": {"count": 1}}}],
            context,
        )
        await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], 2]]}}],
            context,
        )
        migratedAt = await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 2, "value": {"count": 3}}}],
            context,
        )
        expect(await storage.document(id, "current", context)).toMatchObject({"version": 2, "value": {"count": 3}})
        await expect(storage.document(id, migratedAt, context)).rejects.toThrow("does not retain historical content")

        await expect(
            storage.commit(
                [{"type": "document.change", "id": id, "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], 4]]}}],
                context,
            )
        ).rejects.toThrow("version transition requires a base")
        expect((await storage.document(id, "current", context))["value"]).toEqual({"count": 3})  # type: ignore[index]
        await storage.commit([{"type": "document.retire", "id": id}], context)
        expect(await storage.document(id, "current", context)).toBeUndefined()

    @case("indexes logical addresses and exact-scope scans independently")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        firstId = await storage.mintId()
        secondId = await storage.mintId()
        conversationId = await storage.mintId()
        taskId = await storage.mintId()
        taskSingletonId = await storage.mintId()
        taskFamilyId = await storage.mintId()
        taskOtherKindId = await storage.mintId()
        createdAt = await storage.commit(
            [
                {"type": "task", "value": pendingTask(taskId, rootId)},
                {
                    "type": "document.create",
                    "record": {"id": firstId, "kind": "cache", "scope": {"kind": "session"}, "key": "__proto__"},
                    "content": {"kind": "base", "version": 1, "value": {"owner": "first"}},
                },
                {
                    "type": "document.create",
                    "record": {"id": secondId, "kind": "cache", "scope": {"kind": "session"}, "key": "constructor"},
                    "content": {"kind": "base", "version": 1, "value": {"owner": "second"}},
                },
                {
                    "type": "document.create",
                    "record": {
                        "id": conversationId,
                        "kind": "cache",
                        "scope": {"kind": "conversation", "conversationId": rootId},
                        "history": "latest",
                        "fork": "current",
                        "key": "__proto__",
                    },
                    "content": {"kind": "base", "version": 1, "value": {"owner": "conversation"}},
                },
                {
                    "type": "document.create",
                    "record": {"id": taskSingletonId, "kind": "task.cache", "scope": {"kind": "task", "taskId": taskId}},
                    "content": {"kind": "base", "version": 1, "value": {"owner": "singleton"}},
                },
                {
                    "type": "document.create",
                    "record": {
                        "id": taskFamilyId,
                        "kind": "task.cache",
                        "scope": {"kind": "task", "taskId": taskId},
                        "key": "member",
                    },
                    "content": {"kind": "base", "version": 1, "value": {"owner": "family"}},
                },
                {
                    "type": "document.create",
                    "record": {"id": taskOtherKindId, "kind": "task.other", "scope": {"kind": "task", "taskId": taskId}},
                    "content": {"kind": "base", "version": 1, "value": {"owner": "other"}},
                },
            ],
            context,
        )

        expect(
            (await storage.findDocument({"kind": "cache", "scope": {"kind": "session"}, "key": "__proto__"}, "current", context))["id"]  # type: ignore[index]
        ).toBe(firstId)
        expect(
            (await storage.scanDocuments({"scope": {"kind": "session"}, "at": "current"}, 1, None, context))["items"]
        ).toHaveLength(1)
        first = await storage.scanDocuments({"scope": {"kind": "session"}, "at": "current"}, 1, None, context)
        second = await storage.scanDocuments({"scope": {"kind": "session"}, "at": "current"}, 1, first.get("next"), context)
        expect([item["id"] for item in [*first["items"], *second["items"]]]).toEqual([firstId, secondId])
        expect(
            [
                item["id"]
                for item in (
                    await storage.scanDocuments(
                        {"scope": {"kind": "conversation", "conversationId": rootId}, "at": "current"}, 10, None, context
                    )
                )["items"]
            ]
        ).toEqual([conversationId])
        expect(
            (await storage.findDocument({"kind": "task.cache", "scope": {"kind": "task", "taskId": taskId}}, "current", context))["id"]  # type: ignore[index]
        ).toBe(taskSingletonId)
        expect(
            (
                await storage.findDocument(
                    {"kind": "task.cache", "scope": {"kind": "task", "taskId": taskId}, "key": "member"}, "current", context
                )
            )["id"]  # type: ignore[index]
        ).toBe(taskFamilyId)
        expect(
            [
                item["id"]
                for item in (
                    await storage.scanDocuments(
                        {"scope": {"kind": "task", "taskId": taskId}, "at": "current", "kind": "task.cache"}, 10, None, context
                    )
                )["items"]
            ]
        ).toEqual([taskSingletonId, taskFamilyId])
        await expect(storage.document(taskSingletonId, createdAt, context)).rejects.toThrow(
            "does not retain historical content"
        )

    @case("keeps document lifecycle failures atomic and gives create-plus-retire an empty lifetime")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        firstId = await storage.mintId()
        secondId = await storage.mintId()
        record = {"id": firstId, "kind": "singleton", "scope": {"kind": "session"}}
        await storage.commit(
            [{"type": "document.create", "record": record, "content": {"kind": "base", "version": 1, "value": {"value": 1}}}],
            context,
        )
        await expect(
            storage.commit(
                [
                    {
                        "type": "document.create",
                        "record": {**record, "id": secondId},
                        "content": {"kind": "base", "version": 1, "value": {"value": 2}},
                    },
                    {"type": "document.change", "id": firstId, "content": {"kind": "delta", "version": 1, "ops": []}},
                ],
                context,
            )
        ).rejects.toThrow("already has a current incarnation")
        expect((await storage.document(firstId, "current", context))["value"]).toEqual({"value": 1})  # type: ignore[index]
        expect(await storage.document(secondId, "current", context)).toBeUndefined()

        emptyId = await storage.mintId()
        emptyAt = await storage.commit(
            [
                {
                    "type": "document.create",
                    "record": {
                        "id": emptyId,
                        "kind": record["kind"],
                        "key": "empty",
                        "scope": {"kind": "conversation", "conversationId": rootId},
                        "history": "rewindable",
                        "fork": "initial",
                    },
                    "content": {"kind": "base", "version": 1, "value": {}},
                },
                {"type": "document.retire", "id": emptyId},
            ],
            context,
        )
        expect(await storage.document(emptyId, "current", context)).toBeUndefined()
        expect(await storage.document(emptyId, emptyAt, context)).toBeUndefined()
        expect(
            await storage.findDocument(
                {"kind": record["kind"], "scope": {"kind": "conversation", "conversationId": rootId}, "key": "empty"},
                emptyAt,
                context,
            )
        ).toBeUndefined()

    @case("rolls back record tables and secondary indexes when a document command fails")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        taskId = await storage.mintId()
        submissionId = await storage.mintId()
        documentId = await storage.mintId()
        task = pendingTask(taskId, rootId)
        submission = {
            "id": submissionId,
            "conversationId": rootId,
            "requestId": "atomic",
            "type": "input",
            "status": "queued",
        }
        record = {"id": documentId, "kind": "atomic", "scope": {"kind": "session"}}
        baselineSeq = await storage.commit(
            [
                {"type": "task", "value": task},
                {"type": "submission", "value": submission},
                {"type": "document.create", "record": record, "content": {"kind": "base", "version": 1, "value": {"count": 1}}},
            ],
            context,
        )

        entryId = await storage.mintId()
        conflictingDocumentId = await storage.mintId()
        await expect(
            storage.commit(
                [
                    {
                        "type": "task",
                        "value": {**task, "state": {"status": "running", "checkpoint": {"phase": "effect"}}},
                    },
                    {"type": "submission", "value": {**submission, "status": "unanswered", "reason": "failed"}},
                    {"type": "entry", "value": entry(entryId, rootId, "transient")},
                    {
                        "type": "document.create",
                        "record": {**record, "id": conflictingDocumentId},
                        "content": {"kind": "base", "version": 1, "value": {"count": 2}},
                    },
                ],
                context,
            )
        ).rejects.toThrow("already has a current incarnation")

        expect(await storage.task(taskId, context)).toEqual(task)
        expect((await storage.scanTasks({"status": "pending"}, 10, None, context))["items"]).toEqual([task])
        expect(await storage.submissionByRequest(rootId, "atomic", context)).toEqual(submission)
        expect(await storage.entry(entryId, context)).toBeUndefined()
        expect(await storage.document(conflictingDocumentId, "current", context)).toBeUndefined()
        expect(
            (await storage.findDocument({"kind": record["kind"], "scope": record["scope"]}, "current", context))["id"]  # type: ignore[index]
        ).toBe(documentId)
        afterRollbackSeq = await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": documentId,
                    "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], 3]]},
                }
            ],
            context,
        )
        expect(afterRollbackSeq).toBeGreaterThan(baselineSeq)

    @case("keeps indexed string identities lossless")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        first = chr(0xD800)
        second = chr(0xD801)
        firstTaskId = await storage.mintId()
        secondTaskId = await storage.mintId()
        firstSubmissionId = await storage.mintId()
        secondSubmissionId = await storage.mintId()
        firstKindDocumentId = await storage.mintId()
        secondKindDocumentId = await storage.mintId()
        firstKeyDocumentId = await storage.mintId()
        secondKeyDocumentId = await storage.mintId()
        await storage.commit(
            [
                {"type": "task", "value": {**pendingTask(firstTaskId, rootId), "kind": first}},
                {"type": "task", "value": {**pendingTask(secondTaskId, rootId), "kind": second}},
                {
                    "type": "submission",
                    "value": {
                        "id": firstSubmissionId,
                        "conversationId": rootId,
                        "requestId": first,
                        "type": "input",
                        "status": "queued",
                    },
                },
                {
                    "type": "submission",
                    "value": {
                        "id": secondSubmissionId,
                        "conversationId": rootId,
                        "requestId": second,
                        "type": "input",
                        "status": "queued",
                    },
                },
                {
                    "type": "document.create",
                    "record": {"id": firstKindDocumentId, "kind": first, "scope": {"kind": "session"}},
                    "content": {"kind": "base", "version": 1, "value": {"identity": "first kind"}},
                },
                {
                    "type": "document.create",
                    "record": {"id": secondKindDocumentId, "kind": second, "scope": {"kind": "session"}},
                    "content": {"kind": "base", "version": 1, "value": {"identity": "second kind"}},
                },
                {
                    "type": "document.create",
                    "record": {"id": firstKeyDocumentId, "kind": "family", "key": first, "scope": {"kind": "session"}},
                    "content": {"kind": "base", "version": 1, "value": {"identity": "first key"}},
                },
                {
                    "type": "document.create",
                    "record": {"id": secondKeyDocumentId, "kind": "family", "key": second, "scope": {"kind": "session"}},
                    "content": {"kind": "base", "version": 1, "value": {"identity": "second key"}},
                },
            ],
            context,
        )

        expect([item["id"] for item in (await storage.scanTasks({"kind": first}, 10, None, context))["items"]]).toEqual(
            [firstTaskId]
        )
        expect([item["id"] for item in (await storage.scanTasks({"kind": second}, 10, None, context))["items"]]).toEqual(
            [secondTaskId]
        )
        expect((await storage.task(firstTaskId, context))["kind"]).toBe(first)  # type: ignore[index]
        expect((await storage.task(secondTaskId, context))["kind"]).toBe(second)  # type: ignore[index]
        expect((await storage.submissionByRequest(rootId, first, context))["requestId"]).toBe(first)  # type: ignore[index]
        expect((await storage.submissionByRequest(rootId, first, context))["id"]).toBe(firstSubmissionId)  # type: ignore[index]
        expect((await storage.submissionByRequest(rootId, second, context))["id"]).toBe(secondSubmissionId)  # type: ignore[index]
        expect((await storage.findDocument({"kind": first, "scope": {"kind": "session"}}, "current", context))["id"]).toBe(  # type: ignore[index]
            firstKindDocumentId
        )
        expect((await storage.findDocument({"kind": second, "scope": {"kind": "session"}}, "current", context))["id"]).toBe(  # type: ignore[index]
            secondKindDocumentId
        )
        expect(
            (await storage.findDocument({"kind": "family", "key": first, "scope": {"kind": "session"}}, "current", context))["id"]  # type: ignore[index]
        ).toBe(firstKeyDocumentId)
        expect(
            (await storage.findDocument({"kind": "family", "key": second, "scope": {"kind": "session"}}, "current", context))["id"]  # type: ignore[index]
        ).toBe(secondKeyDocumentId)
        expect(
            [
                item["id"]
                for item in (
                    await storage.scanDocuments({"scope": {"kind": "session"}, "at": "current", "kind": first}, 10, None, context)
                )["items"]
            ]
        ).toEqual([firstKindDocumentId])

    @case("keeps one global record ID namespace and rejects exhausted ID minting")
    async def _(storage: Storage) -> None:
        rootId = await createRoot(storage)
        explicitEntryId = 100
        await storage.commit([{"type": "entry", "value": entry(explicitEntryId, rootId)}], context)
        expect(await storage.mintId()).toBe(101)
        await expect(
            storage.commit([{"type": "task", "value": pendingTask(explicitEntryId, rootId)}], context)
        ).rejects.toThrow(f"ID {explicitEntryId} already belongs to entry")

        await storage.commit([{"type": "entry", "value": entry(2**53 - 1, rootId, "last-id")}], context)
        await expect(storage.mintId()).rejects.toThrow("ID space is exhausted")
        await expect(storage.mintId()).rejects.toThrow("ID space is exhausted")

    @case("rejects every operation after close")
    async def _(storage: Storage) -> None:
        await createRoot(storage)
        await storage.close(context)
        await expect(storage.conversation(ROOT_CONVERSATION_ID, context)).rejects.toThrow("closed")
        await expect(storage.commit([], context)).rejects.toThrow("closed")
        await expect(storage.mintId()).rejects.toThrow("closed")

    return cases


__all__ = ["createStorageConformance"]
