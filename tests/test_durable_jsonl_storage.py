"""pi durable's ``test/jsonl-storage.test.ts``."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.env import FileError, err
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.storage.jsonl import JsonlStorage
from misaka.durable.storage.jsonl.node import openNodeJsonlStorage
from misaka.durable.testing import createAssertions, createStorageConformance
from misaka.durable.types import ROOT_CONVERSATION_ID

context = BACKGROUND_CONTEXT


def throws(message: str):
    return pytest.raises(Exception, match=re.escape(message))


class Workspace:
    def __init__(self) -> None:
        self.directories: set[str] = set()
        self.storages: set[Any] = set()

    def tempDirectory(self, prefix: str = "pi-durable-jsonl-") -> str:
        directory = tempfile.mkdtemp(prefix=prefix)
        self.directories.add(directory)
        return directory

    async def openStorage(self, directory: str, fs: Any = None, options: dict[str, Any] | None = None) -> JsonlStorage:
        storage = await JsonlStorage.open(directory, fs or NodeExecutionEnv({"cwd": directory}), context, options or {})
        self.storages.add(storage)
        return storage

    async def createStorage(self) -> JsonlStorage:
        return await self.openStorage(self.tempDirectory())


@pytest.fixture
async def workspace() -> AsyncIterator[Workspace]:
    value = Workspace()
    yield value
    for storage in value.storages:
        await storage.close(context)
    for directory in value.directories:
        shutil.rmtree(directory, ignore_errors=True)


class ReopeningStorage:
    """Wraps a JSONL storage and reopens it from disk after every commit."""

    def __init__(self, current: JsonlStorage, directory: str) -> None:
        self.current = current
        self.directory = directory
        self.closed = False

    async def commit(self, writes, commitContext):
        if self.closed:
            return await self.current.commit(writes, commitContext)
        try:
            return await self.current.commit(writes, commitContext)
        finally:
            await self.current.close(context)
            self.current = await JsonlStorage.open(self.directory, NodeExecutionEnv({"cwd": self.directory}), context)

    async def close(self, closeContext):
        if self.closed:
            return
        self.closed = True
        await self.current.close(closeContext)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.current, name)


async def withPlainStorage(use):
    value = Workspace()
    try:
        await use(await value.createStorage())
    finally:
        for storage in value.storages:
            await storage.close(context)
        for directory in value.directories:
            shutil.rmtree(directory, ignore_errors=True)


async def withReopeningStorage(use):
    directory = tempfile.mkdtemp(prefix="pi-durable-jsonl-conformance-")
    current = await JsonlStorage.open(directory, NodeExecutionEnv({"cwd": directory}), context)
    storage = ReopeningStorage(current, directory)
    try:
        await use(storage)
    finally:
        await storage.close(context)
        shutil.rmtree(directory, ignore_errors=True)


plainCases = createStorageConformance({"assertions": createAssertions(), "withStorage": withPlainStorage})
reopenCases = createStorageConformance({"assertions": createAssertions(), "withStorage": withReopeningStorage})


@pytest.mark.parametrize("case", plainCases, ids=[case["name"] for case in plainCases])
async def test_jsonl_storage_conformance(case):
    await case["run"]()


@pytest.mark.parametrize("case", reopenCases, ids=[case["name"] for case in reopenCases])
async def test_jsonl_storage_across_reopen_conformance(case):
    await case["run"]()


def pendingTask(id: int, phase: str = "ready") -> dict[str, Any]:
    return {
        "id": id,
        "conversationId": ROOT_CONVERSATION_ID,
        "kind": "test.task",
        "version": 1,
        "input": None,
        "state": {"status": "pending", "checkpoint": {"phase": phase}},
        "background": False,
        "abortRequested": False,
    }


def terminalTask(id: int) -> dict[str, Any]:
    return {
        "id": id,
        "conversationId": ROOT_CONVERSATION_ID,
        "kind": "test.task",
        "version": 1,
        "input": None,
        "state": {"status": "terminal", "outcome": {"status": "completed", "result": None}},
        "background": False,
        "abortRequested": False,
    }


async def createRoot(storage) -> None:
    await storage.commit([{"type": "conversation", "value": {"id": ROOT_CONVERSATION_ID}}], context)


def sessionDocument(id: int, kind: str = "test.document") -> dict[str, Any]:
    return {"id": id, "kind": kind, "scope": {"kind": "session"}}


def base(id: int, record: dict[str, Any], value: Any, version: int = 1) -> dict[str, Any]:
    return {"type": "document.create", "record": record, "content": {"kind": "base", "version": version, "value": value}}


class InstrumentedEnv(NodeExecutionEnv):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.operations: list[str] = []
        self.failure: dict[str, Any] | None = None
        self.counts = {"append": 0, "flush": 0, "write": 0, "rename": 0, "remove": 0}

    def fail(self, failure: dict[str, Any]) -> None:
        self.failure = failure
        self._resetObservations()

    def clear(self) -> None:
        self.failure = None
        self._resetObservations()

    def _resetObservations(self) -> None:
        for key in self.counts:
            self.counts[key] = 0
        self.operations.clear()

    def _failing(self, operation: str) -> dict[str, Any] | None:
        failure = self.failure
        if failure is None or failure["operation"] != operation or failure["call"] != self.counts[operation]:
            return None
        return failure

    async def appendFile(self, path, content, appendContext):
        self.counts["append"] += 1
        self.operations.append(f"append:{os.path.basename(path)}")
        failure = self._failing("append")
        if failure is None:
            return await super().appendFile(path, content, appendContext)
        if failure["mode"] == "before":
            return err(FileError("unknown", "injected append failure", path))
        if failure["mode"] == "short":
            data = content.encode("utf-8") if isinstance(content, str) else content
            partial = data[: max(1, len(data) // 2)]
            written = await super().appendFile(path, partial, appendContext)
            if not written["ok"]:
                return written
            return err(FileError("unknown", "injected short append", path))
        written = await super().appendFile(path, content, appendContext)
        if not written["ok"]:
            return written
        return err(FileError("unknown", "injected post-append failure", path))

    async def flushFile(self, path, flushContext):
        self.counts["flush"] += 1
        self.operations.append(f"flush:{os.path.basename(path)}")
        failure = self._failing("flush")
        if failure is None:
            return await super().flushFile(path, flushContext)
        if failure["mode"] == "after":
            flushed = await super().flushFile(path, flushContext)
            if not flushed["ok"]:
                return flushed
        return err(FileError("unknown", "injected flush failure", path))

    async def writeFile(self, path, content, writeContext):
        self.counts["write"] += 1
        self.operations.append(f"write:{os.path.basename(path)}")
        failure = self._failing("write")
        if failure is None:
            return await super().writeFile(path, content, writeContext)
        if failure["mode"] == "before":
            return err(FileError("unknown", "injected write failure", path))
        if failure["mode"] == "short":
            data = content.encode("utf-8") if isinstance(content, str) else content
            partial = data[: max(1, len(data) // 2)]
            written = await super().writeFile(path, partial, writeContext)
            if not written["ok"]:
                return written
            return err(FileError("unknown", "injected short write", path))
        written = await super().writeFile(path, content, writeContext)
        if not written["ok"]:
            return written
        return err(FileError("unknown", "injected post-write failure", path))

    async def renameFile(self, sourcePath, destinationPath, renameContext):
        self.counts["rename"] += 1
        self.operations.append(f"rename:{os.path.basename(sourcePath)}->{os.path.basename(destinationPath)}")
        failure = self._failing("rename")
        if failure is None:
            return await super().renameFile(sourcePath, destinationPath, renameContext)
        if failure["mode"] == "after":
            renamed = await super().renameFile(sourcePath, destinationPath, renameContext)
            if not renamed["ok"]:
                return renamed
        return err(FileError("unknown", "injected rename failure", sourcePath))

    async def remove(self, path, options, removeContext):
        self.counts["remove"] += 1
        self.operations.append(f"remove:{os.path.basename(path)}")
        failure = self._failing("remove")
        if failure is None:
            return await super().remove(path, options, removeContext)
        if failure["mode"] == "after":
            removed = await super().remove(path, options, removeContext)
            if not removed["ok"]:
                return removed
        return err(FileError("unknown", "injected remove failure", path))


def readLines(path: str) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")
    return [] if text == "" else text.rstrip().split("\n")


def appendBytes(path: str, data: bytes) -> None:
    with open(path, "ab") as file:
        file.write(data)


def fileExists(path: str) -> bool:
    return os.path.exists(path)


def reclaimFiles(directory: str) -> list[str]:
    return [name for name in os.listdir(directory) if name.endswith(".reclaim")]


class TestPicoJsonlStoragePublicationAndRecovery:
    async def test_opens_through_the_node_adapter(self, workspace):
        storage = await openNodeJsonlStorage(workspace.tempDirectory(), context)
        workspace.storages.add(storage)
        await createRoot(storage)
        assert await storage.conversation(ROOT_CONVERSATION_ID, context) == {"id": ROOT_CONVERSATION_ID}

    async def test_publishes_every_commit_before_reclaiming_current_only_document_and_terminal_task_sidecars(
        self, workspace
    ):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        taskId = await storage.mintId()
        documentId = await storage.mintId()
        await storage.commit([{"type": "task", "value": pendingTask(taskId)}], context)
        await storage.commit([base(documentId, sessionDocument(documentId), {"count": 0})], context)
        await storage.commit(
            [{"type": "document.change", "id": documentId, "content": {"kind": "delta", "version": 1, "ops": []}}], context
        )
        await storage.commit(
            [{"type": "document.change", "id": documentId, "content": {"kind": "base", "version": 1, "value": {"count": 1}}}],
            context,
        )
        assert len(readLines(os.path.join(directory, f"task-{taskId}.jsonl"))) == 1
        assert len(readLines(os.path.join(directory, f"doc-{documentId}.jsonl"))) == 1

        await storage.commit([{"type": "document.retire", "id": documentId}], context)
        await storage.commit([{"type": "task", "value": terminalTask(taskId)}], context)

        assert len(readLines(os.path.join(directory, "main.jsonl"))) == 7
        assert not fileExists(os.path.join(directory, f"task-{taskId}.jsonl"))
        assert not fileExists(os.path.join(directory, f"doc-{documentId}.jsonl"))
        markerTypes = [json.loads(line)["type"] for line in readLines(os.path.join(directory, "main.jsonl"))]
        assert markerTypes == ["commit"] * 7

    async def test_orders_multiple_live_task_replacements_in_one_sidecar_by_commit_ordinal(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        taskId = await storage.mintId()
        await storage.commit(
            [
                {"type": "task", "value": pendingTask(taskId, "first")},
                {"type": "task", "value": pendingTask(taskId, "second")},
            ],
            context,
        )
        assert len(readLines(os.path.join(directory, f"task-{taskId}.jsonl"))) == 2

        reopened = await workspace.openStorage(directory)
        assert await reopened.task(taskId, context) == pendingTask(taskId, "second")

    async def test_removes_a_complete_plus_torn_unconfirmed_multi_record_sidecar_append(self, workspace):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env)
        await createRoot(storage)
        taskId = await storage.mintId()
        env.fail({"operation": "append", "call": 1, "mode": "short"})
        with throws("poisoned"):
            await storage.commit(
                [
                    {"type": "task", "value": pendingTask(taskId, "first")},
                    {"type": "task", "value": pendingTask(taskId, f"second-{'x' * 512}")},
                ],
                context,
            )
        partial = Path(directory, f"task-{taskId}.jsonl").read_bytes()
        assert partial[-1] != 0x0A
        assert partial.count(b"\n") == 1

        reopened = await workspace.openStorage(directory)
        assert await reopened.task(taskId, context) is None
        assert os.path.getsize(os.path.join(directory, f"task-{taskId}.jsonl")) == 0

    async def test_serializes_the_complete_candidate_before_io_and_leaves_preparation_failures_usable(self, workspace):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env)
        await createRoot(storage)
        env.clear()
        id = await storage.mintId()
        with pytest.raises(Exception):  # noqa: B017
            await storage.commit(
                [{"type": "entry", "value": {"id": id, "conversationId": ROOT_CONVERSATION_ID, "kind": "bad", "data": object()}}],
                context,
            )
        assert env.operations == []
        assert await storage.entry(id, context) is None
        assert (
            await storage.commit(
                [{"type": "entry", "value": {"id": id, "conversationId": ROOT_CONVERSATION_ID, "kind": "good"}}], context
            )
            == 2
        )

    @pytest.mark.parametrize(
        ("call", "mode"),
        [(1, "before"), (1, "after"), (1, "short"), (2, "before"), (2, "after"), (3, "before"), (3, "short"), (3, "after")],
    )
    async def test_poisons_after_append_failures_and_recovers_only_confirmed_state(self, workspace, call, mode):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env)
        await createRoot(storage)
        firstId = await storage.mintId()
        secondId = await storage.mintId()
        env.fail({"operation": "append", "call": call, "mode": mode})
        with throws("poisoned"):
            await storage.commit(
                [
                    base(firstId, sessionDocument(firstId, "first"), {"text": "α"}),
                    base(secondId, sessionDocument(secondId, "second"), {"text": "β"}),
                ],
                context,
            )
        with throws("poisoned"):
            await storage.document(firstId, "current", context)

        reopened = await workspace.openStorage(directory)
        markerSurvived = call == 3 and mode == "after"
        first = await reopened.document(firstId, "current", context)
        second = await reopened.document(secondId, "current", context)
        if markerSurvived:
            assert first["value"] == {"text": "α"}
            assert second["value"] == {"text": "β"}
        else:
            assert first is None
            assert second is None

    @pytest.mark.parametrize(("call", "mode"), [(1, "before"), (1, "after"), (2, "before"), (2, "after")])
    async def test_poisons_after_flush_failures_and_never_writes_a_marker(self, workspace, call, mode):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env, {"fsync": True})
        await createRoot(storage)
        firstId = await storage.mintId()
        secondId = await storage.mintId()
        env.fail({"operation": "flush", "call": call, "mode": mode})
        with throws("poisoned"):
            await storage.commit(
                [
                    base(firstId, sessionDocument(firstId, "flush.first"), {}),
                    base(secondId, sessionDocument(secondId, "flush.second"), {}),
                ],
                context,
            )
        reopened = await workspace.openStorage(directory)
        assert await reopened.document(firstId, "current", context) is None
        assert await reopened.document(secondId, "current", context) is None

    @pytest.mark.parametrize("fsync", [False, True])
    async def test_orders_publication_flushes_exactly_and_flushes_main_only_to_authorize_reclamation(
        self, workspace, fsync
    ):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env, {"fsync": fsync})
        await createRoot(storage)
        firstId = await storage.mintId()
        secondId = await storage.mintId()
        env.clear()
        await storage.commit(
            [base(secondId, sessionDocument(secondId, "second"), {}), base(firstId, sessionDocument(firstId, "first"), {})],
            context,
        )
        expected = [
            f"append:doc-{secondId}.jsonl",
            f"append:doc-{firstId}.jsonl",
            *([f"flush:doc-{secondId}.jsonl", f"flush:doc-{firstId}.jsonl"] if fsync else []),
            "append:main.jsonl",
        ]
        assert env.operations == expected

        env.clear()
        await storage.commit(
            [{"type": "document.change", "id": firstId, "content": {"kind": "base", "version": 1, "value": {"checkpoint": True}}}],
            context,
        )
        assert env.operations == [
            f"append:doc-{firstId}.jsonl",
            *([f"flush:doc-{firstId}.jsonl"] if fsync else []),
            "append:main.jsonl",
            *(["flush:main.jsonl"] if fsync else []),
            f"write:doc-{firstId}.jsonl.reclaim",
            *([f"flush:doc-{firstId}.jsonl.reclaim"] if fsync else []),
            f"rename:doc-{firstId}.jsonl.reclaim->doc-{firstId}.jsonl",
        ]

        env.clear()
        await storage.commit(
            [{"type": "entry", "value": {"id": await storage.mintId(), "conversationId": ROOT_CONVERSATION_ID, "kind": "main-only"}}],
            context,
        )
        assert env.operations == ["append:main.jsonl"]

        taskId = await storage.mintId()
        await storage.commit([{"type": "task", "value": pendingTask(taskId)}], context)
        env.clear()
        await storage.commit([{"type": "task", "value": terminalTask(taskId)}], context)
        assert env.operations == ["append:main.jsonl", *(["flush:main.jsonl"] if fsync else []), f"remove:task-{taskId}.jsonl"]

    @pytest.mark.parametrize(
        ("operation", "mode"),
        [("write", "before"), ("write", "short"), ("write", "after"), ("rename", "before"), ("rename", "after")],
    )
    async def test_recovers_a_committed_base_across_reclaim_failures(self, workspace, operation, mode):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env)
        await createRoot(storage)
        id = await storage.mintId()
        await storage.commit([base(id, sessionDocument(id), {"count": 0})], context)
        await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], 1]]}}],
            context,
        )

        env.fail({"operation": operation, "call": 1, "mode": mode})
        seq = await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"count": 2}}}], context
        )
        assert seq == 4
        assert (await storage.document(id, "current", context))["value"] == {"count": 2}
        await storage.close(context)

        reopened = await workspace.openStorage(directory)
        assert (await reopened.document(id, "current", context))["value"] == {"count": 2}
        assert len(readLines(os.path.join(directory, f"doc-{id}.jsonl"))) == 1
        assert reclaimFiles(directory) == []

    @pytest.mark.parametrize("mode", ["before", "after"])
    async def test_recovers_document_retirement_reclamation_across_remove_failures(self, workspace, mode):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env)
        await createRoot(storage)
        taskId = await storage.mintId()
        id = await storage.mintId()
        await storage.commit(
            [
                {"type": "task", "value": pendingTask(taskId)},
                base(id, {"id": id, "kind": "task.document", "scope": {"kind": "task", "taskId": taskId}}, {"count": 1}),
            ],
            context,
        )
        env.fail({"operation": "remove", "call": 1, "mode": mode})
        assert await storage.commit([{"type": "document.retire", "id": id}], context) == 3
        assert await storage.document(id, "current", context) is None
        await storage.close(context)

        reopened = await workspace.openStorage(directory)
        assert await reopened.document(id, "current", context) is None
        assert await reopened.task(taskId, context) == pendingTask(taskId)
        assert not fileExists(os.path.join(directory, f"doc-{id}.jsonl"))
        assert reclaimFiles(directory) == []

    @pytest.mark.parametrize("mode", ["before", "after"])
    async def test_defers_reclamation_after_authorizing_main_flush_failure(self, workspace, mode):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env, {"fsync": True})
        await createRoot(storage)
        id = await storage.mintId()
        await storage.commit([base(id, sessionDocument(id), {"count": 0})], context)
        env.fail({"operation": "flush", "call": 2, "mode": mode})
        seq = await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"count": 2}}}], context
        )
        assert seq == 3
        assert env.operations == [f"append:doc-{id}.jsonl", f"flush:doc-{id}.jsonl", "append:main.jsonl", "flush:main.jsonl"]
        assert (await storage.document(id, "current", context))["value"] == {"count": 2}
        assert len(readLines(os.path.join(directory, f"doc-{id}.jsonl"))) == 2
        await storage.close(context)

        recoveryEnv = InstrumentedEnv({"cwd": directory})
        recoveryEnv.fail({"operation": "flush", "call": 1, "mode": mode})
        deferred = await workspace.openStorage(directory, recoveryEnv, {"fsync": True})
        assert (await deferred.document(id, "current", context))["value"] == {"count": 2}
        assert recoveryEnv.operations == ["flush:main.jsonl"]
        assert len(readLines(os.path.join(directory, f"doc-{id}.jsonl"))) == 2
        await deferred.close(context)

        reclaimed = await workspace.openStorage(directory, NodeExecutionEnv({"cwd": directory}), {"fsync": True})
        assert (await reclaimed.document(id, "current", context))["value"] == {"count": 2}
        assert len(readLines(os.path.join(directory, f"doc-{id}.jsonl"))) == 1

    @pytest.mark.parametrize("mode", ["before", "after"])
    async def test_keeps_a_committed_base_usable_after_reclaim_temp_flush_failure(self, workspace, mode):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env, {"fsync": True})
        await createRoot(storage)
        id = await storage.mintId()
        await storage.commit([base(id, sessionDocument(id), {"count": 0})], context)
        env.fail({"operation": "flush", "call": 3, "mode": mode})
        seq = await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"count": 2}}}], context
        )
        assert seq == 3
        env.clear()
        await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], 3]]}}],
            context,
        )
        await storage.close(context)

        reopened = await workspace.openStorage(directory, NodeExecutionEnv({"cwd": directory}), {"fsync": True})
        assert (await reopened.document(id, "current", context))["value"] == {"count": 3}
        assert len(readLines(os.path.join(directory, f"doc-{id}.jsonl"))) == 2

    @pytest.mark.parametrize("mode", ["before", "after"])
    async def test_recovers_terminal_task_reclamation_across_remove_failures(self, workspace, mode):
        directory = workspace.tempDirectory()
        env = InstrumentedEnv({"cwd": directory})
        storage = await workspace.openStorage(directory, env)
        await createRoot(storage)
        id = await storage.mintId()
        await storage.commit([{"type": "task", "value": pendingTask(id)}], context)
        env.fail({"operation": "remove", "call": 1, "mode": mode})
        assert await storage.commit([{"type": "task", "value": terminalTask(id)}], context) == 3
        assert await storage.task(id, context) == terminalTask(id)
        await storage.close(context)

        reopened = await workspace.openStorage(directory)
        assert await reopened.task(id, context) == terminalTask(id)
        assert not fileExists(os.path.join(directory, f"task-{id}.jsonl"))
        assert reclaimFiles(directory) == []

    async def test_appends_later_deltas_to_the_replacement_sidecar_after_a_current_only_base(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        id = await storage.mintId()
        await storage.commit([base(id, sessionDocument(id), {"count": 0})], context)
        await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"count": 10}}}], context
        )
        await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], 11]]}}],
            context,
        )
        assert len(readLines(os.path.join(directory, f"doc-{id}.jsonl"))) == 2
        reopened = await workspace.openStorage(directory)
        assert (await reopened.document(id, "current", context))["value"] == {"count": 11}

    async def test_never_reclaims_rewindable_document_history_including_after_a_base_and_retirement(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        id = await storage.mintId()
        record = {
            "id": id,
            "kind": "rewindable",
            "scope": {"kind": "conversation", "conversationId": ROOT_CONVERSATION_ID},
            "history": "rewindable",
            "fork": "asOf",
        }
        createdAt = await storage.commit([base(id, record, {"count": 0})], context)
        changedAt = await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], 1]]}}],
            context,
        )
        await storage.commit(
            [{"type": "document.change", "id": id, "content": {"kind": "base", "version": 1, "value": {"count": 2}}}], context
        )
        await storage.commit([{"type": "document.retire", "id": id}], context)

        assert len(readLines(os.path.join(directory, f"doc-{id}.jsonl"))) == 3
        reopened = await workspace.openStorage(directory)
        assert (await reopened.document(id, createdAt, context))["value"] == {"count": 0}
        assert (await reopened.document(id, changedAt, context))["value"] == {"count": 1}
        assert await reopened.document(id, "current", context) is None
        assert len(readLines(os.path.join(directory, f"doc-{id}.jsonl"))) == 3

    async def test_reclaims_retired_task_session_and_latest_conversation_document_sidecars(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        taskId = await storage.mintId()
        sessionId = await storage.mintId()
        latestId = await storage.mintId()
        taskDocumentId = await storage.mintId()
        createdAt = await storage.commit(
            [
                {"type": "task", "value": pendingTask(taskId)},
                base(sessionId, sessionDocument(sessionId, "session"), {}),
                base(
                    latestId,
                    {
                        "id": latestId,
                        "kind": "latest",
                        "scope": {"kind": "conversation", "conversationId": ROOT_CONVERSATION_ID},
                        "history": "latest",
                        "fork": "current",
                    },
                    {},
                ),
                base(taskDocumentId, {"id": taskDocumentId, "kind": "task", "scope": {"kind": "task", "taskId": taskId}}, {}),
            ],
            context,
        )
        retiredAt = await storage.commit(
            [
                {"type": "document.retire", "id": sessionId},
                {"type": "document.retire", "id": latestId},
                {"type": "document.retire", "id": taskDocumentId},
                {"type": "task", "value": terminalTask(taskId)},
            ],
            context,
        )
        for file in [f"doc-{sessionId}.jsonl", f"doc-{latestId}.jsonl", f"doc-{taskDocumentId}.jsonl", f"task-{taskId}.jsonl"]:
            assert not fileExists(os.path.join(directory, file))

        reopened = await workspace.openStorage(directory)
        assert await reopened.task(taskId, context) == terminalTask(taskId)
        assert await reopened.document(sessionId, "current", context) is None
        assert await reopened.document(latestId, "current", context) is None
        assert await reopened.document(taskDocumentId, "current", context) is None
        found = await reopened.findDocument({"kind": "session", "scope": {"kind": "session"}}, createdAt, context)
        assert found["id"] == sessionId
        assert found["createdAt"] == createdAt
        assert found["retiredAt"] == retiredAt
        assert await reopened.findDocument({"kind": "session", "scope": {"kind": "session"}}, retiredAt, context) is None

    async def test_truncates_torn_utf8_tails_at_exact_byte_offsets_and_reuses_the_unconfirmed_sequence(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        documentId = await storage.mintId()
        await storage.commit([base(documentId, sessionDocument(documentId), {"text": "kept"})], context)
        sidecarPath = os.path.join(directory, f"doc-{documentId}.jsonl")
        mainPath = os.path.join(directory, "main.jsonl")
        sidecarSize = os.path.getsize(sidecarPath)
        mainSize = os.path.getsize(mainPath)
        torn = '{"text":"€'.encode()
        for path in (sidecarPath, mainPath):
            appendBytes(path, torn[:-1])

        reopened = await workspace.openStorage(directory)
        assert os.path.getsize(sidecarPath) == sidecarSize
        assert os.path.getsize(mainPath) == mainSize
        assert (
            await reopened.commit(
                [{"type": "document.change", "id": documentId, "content": {"kind": "delta", "version": 1, "ops": []}}], context
            )
            == 3
        )

    async def test_removes_complete_unconfirmed_sidecar_tails_without_resurrecting_terminal_tasks(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        taskId = await storage.mintId()
        await storage.commit([{"type": "task", "value": pendingTask(taskId)}], context)
        await storage.commit([{"type": "task", "value": terminalTask(taskId)}], context)
        sidecarPath = os.path.join(directory, f"task-{taskId}.jsonl")
        assert not fileExists(sidecarPath)
        stale = {
            "format": 1,
            "type": "record",
            "seq": 4,
            "ordinal": 0,
            "payload": {"type": "task", "value": pendingTask(taskId, "stale")},
        }
        appendBytes(sidecarPath, f"{json.dumps(stale, separators=(',', ':'))}\n".encode())

        reopened = await workspace.openStorage(directory)
        assert not fileExists(sidecarPath)
        assert await reopened.task(taskId, context) == terminalTask(taskId)
        assert await reopened.commit([], context) == 4

    async def test_fails_open_when_confirmed_sidecar_data_is_missing(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        documentId = await storage.mintId()
        await storage.commit([base(documentId, sessionDocument(documentId), {})], context)
        Path(directory, f"doc-{documentId}.jsonl").write_text("")
        with throws("Missing confirmed sidecar record"):
            await JsonlStorage.open(directory, NodeExecutionEnv({"cwd": directory}), context)

    async def test_rejects_a_confirmed_record_after_an_unconfirmed_sidecar_record(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        documentId = await storage.mintId()
        await storage.commit([base(documentId, sessionDocument(documentId), {"count": 0})], context)
        await storage.commit(
            [
                {
                    "type": "document.change",
                    "id": documentId,
                    "content": {"kind": "delta", "version": 1, "ops": [["s", ["count"], 1]]},
                }
            ],
            context,
        )
        path = os.path.join(directory, f"doc-{documentId}.jsonl")
        first, second = readLines(path)
        unconfirmed = json.dumps(
            {
                "format": 1,
                "type": "record",
                "seq": 2,
                "ordinal": 999,
                "payload": {"type": "document", "id": documentId, "content": {"kind": "delta", "version": 1, "ops": []}},
            },
            separators=(",", ":"),
        )
        Path(path).write_text(f"{first}\n{unconfirmed}\n{second}\n")
        with throws("Confirmed record follows an unconfirmed tail"):
            await JsonlStorage.open(directory, NodeExecutionEnv({"cwd": directory}), context)

    async def test_rejects_non_increasing_main_commit_sequences(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        path = Path(directory, "main.jsonl")
        appendBytes(str(path), path.read_bytes())

        with throws("Commit sequence does not strictly increase"):
            await JsonlStorage.open(directory, NodeExecutionEnv({"cwd": directory}), context)

    async def test_rejects_structurally_invalid_confirmed_document_content(self, workspace):
        directory = workspace.tempDirectory()
        storage = await workspace.openStorage(directory)
        await createRoot(storage)
        documentId = await storage.mintId()
        await storage.commit([base(documentId, sessionDocument(documentId), {})], context)
        path = Path(directory, f"doc-{documentId}.jsonl")
        record = json.loads(path.read_text(encoding="utf-8").strip())
        del record["payload"]["content"]["value"]
        path.write_text(f"{json.dumps(record, separators=(',', ':'))}\n")

        with throws("Invalid document content"):
            await JsonlStorage.open(directory, NodeExecutionEnv({"cwd": directory}), context)

    async def test_rejects_malformed_complete_main_and_sidecar_lines(self, workspace):
        mainDirectory = workspace.tempDirectory()
        mainStorage = await workspace.openStorage(mainDirectory)
        await createRoot(mainStorage)
        appendBytes(os.path.join(mainDirectory, "main.jsonl"), b"{bad}\n")
        with throws("Malformed complete main.jsonl"):
            await JsonlStorage.open(mainDirectory, NodeExecutionEnv({"cwd": mainDirectory}), context)

        sidecarDirectory = workspace.tempDirectory()
        sidecarStorage = await workspace.openStorage(sidecarDirectory)
        await createRoot(sidecarStorage)
        Path(sidecarDirectory, "doc-99.jsonl").write_text("{bad}\n")
        with throws("Malformed complete doc-99.jsonl"):
            await JsonlStorage.open(sidecarDirectory, NodeExecutionEnv({"cwd": sidecarDirectory}), context)
