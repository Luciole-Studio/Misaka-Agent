"""Portable JSONL implementation of the storage contract.

Translated from pi's ``packages/durable/src/storage/jsonl/storage.ts``. State lives in a ``MemoryStorage``; every commit
is appended to ``main.jsonl`` (a marker per commit) and to per-document and per-task sidecar files, and ``open``
rebuilds the memory state by replaying the main file. A write goes through the ``FileSystem`` of
``misaka.durable.env`` (results are ``{"ok": ..., "value"/"error": ...}``). Markers and records are ``dict`` objects
with the upstream key order, so the files have the upstream bytes.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any

from misaka.chord.context import Context
from misaka.utils.js import eager, jsIsInteger, jsonParse, jsonStringify, startTask

from ...env import FileError
from ..memory import MemoryStorage

FORMAT_VERSION = 1
MAIN_FILE = "main.jsonl"
RECLAIM_SUFFIX = ".reclaim"
_MAX_SAFE_INTEGER = 2**53 - 1
# ``\d`` is the ASCII digits only, as in JavaScript.
_SIDECAR_NAME = re.compile(r"(?:doc|task)-(?:0|[1-9]\d*)\.jsonl", re.ASCII)
_RECLAIM_NAME = re.compile(r"(?:doc|task)-(?:0|[1-9]\d*)\.jsonl\.reclaim", re.ASCII)


class JsonlCorruptionError(Exception):
    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.name = "JsonlCorruptionError"
        self.message = message
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause


class JsonlStoragePoisonedError(Exception):
    def __init__(self, cause: BaseException) -> None:
        super().__init__("JSONL storage is poisoned and must be reopened")
        self.name = "JsonlStoragePoisonedError"
        self.message = "JSONL storage is poisoned and must be reopened"
        self.cause = cause
        self.__cause__ = cause


def isObject(value: Any) -> bool:
    return isinstance(value, dict)


def isSafeInteger(value: Any) -> bool:
    return jsIsInteger(value) and abs(value) <= _MAX_SAFE_INTEGER


def sidecarFileName(kind: str, id: int) -> str:
    return f"{kind}-{id}.jsonl"


def isSidecarFileName(name: str) -> bool:
    return _SIDECAR_NAME.fullmatch(name) is not None


def isReclaimFileName(name: str) -> bool:
    return _RECLAIM_NAME.fullmatch(name) is not None


def isCurrentOnly(record: dict[str, Any]) -> bool:
    return record["scope"]["kind"] != "conversation" or record["history"] == "latest"


def sidecarKey(file: str, seq: int, ordinal: int) -> str:
    return jsonStringify([file, seq, ordinal])


def jsonLine(value: dict[str, Any]) -> str:
    return f"{jsonStringify(value)}\n"


def errorFromFile(action: str, error: FileError) -> Exception:
    failure = Exception(f"JSONL {action} failed: {error.message}")
    failure.__cause__ = error
    return failure


def parseJson(text: str, description: str) -> Any:
    try:
        return jsonParse(text)
    except (ValueError, RecursionError) as error:
        raise JsonlCorruptionError(f"Malformed complete {description}", error) from error


def validateMainOperation(value: Any, description: str) -> dict[str, Any]:
    if not isObject(value) or not isinstance(value.get("type"), str):
        raise JsonlCorruptionError(f"Invalid write in {description}")
    kind = value["type"]
    if kind in ("conversation", "entry", "submission"):
        if not isObject(value.get("value")) or not isSafeInteger(value["value"].get("id")):
            raise JsonlCorruptionError(f"Invalid {kind} write in {description}")
        return value
    if kind == "task":
        task = value.get("value")
        if (
            not isObject(task)
            or not isSafeInteger(task.get("id"))
            or not isObject(task.get("state"))
            or task["state"].get("status") != "terminal"
        ):
            raise JsonlCorruptionError(f"Invalid terminal task write in {description}")
        return value
    if kind == "document.retire":
        if not isSafeInteger(value.get("id")):
            raise JsonlCorruptionError(f"Invalid document retirement in {description}")
        return value
    if kind == "task.sidecar":
        if not isSafeInteger(value.get("id")) or not isSafeInteger(value.get("ordinal")) or value["ordinal"] < 0:
            raise JsonlCorruptionError(f"Invalid task sidecar write in {description}")
        return value
    if kind == "document.create":
        record = value.get("record")
        if (
            not isObject(record)
            or not isSafeInteger(record.get("id"))
            or not isSafeInteger(value.get("ordinal"))
            or value["ordinal"] < 0
        ):
            raise JsonlCorruptionError(f"Invalid document creation in {description}")
        return value
    if kind == "document.change":
        if not isSafeInteger(value.get("id")) or not isSafeInteger(value.get("ordinal")) or value["ordinal"] < 0:
            raise JsonlCorruptionError(f"Invalid document change in {description}")
        return value
    raise JsonlCorruptionError(f"Unknown write type in {description}")


def isFormatVersion(value: Any) -> bool:
    """``value === FORMAT_VERSION``: ``true`` is not 1."""
    return not isinstance(value, bool) and value == FORMAT_VERSION


def parseMainMarker(text: str, line: int) -> dict[str, Any]:
    description = f"{MAIN_FILE} line {line}"
    value = parseJson(text, description)
    if (
        not isObject(value)
        or not isFormatVersion(value.get("format"))
        or value.get("type") != "commit"
        or not isSafeInteger(value.get("seq"))
        or value["seq"] < 1
        or not isinstance(value.get("writes"), list)
    ):
        raise JsonlCorruptionError(f"Invalid commit marker in {description}")
    return {
        "format": FORMAT_VERSION,
        "type": "commit",
        "seq": value["seq"],
        "writes": [validateMainOperation(write, description) for write in value["writes"]],
    }


def validateDocumentContent(value: Any, description: str) -> dict[str, Any]:
    if not isObject(value) or not isSafeInteger(value.get("version")) or value["version"] < 1:
        raise JsonlCorruptionError(f"Invalid document content in {description}")
    if value.get("kind") == "base" and isObject(value.get("value")):
        return value
    if value.get("kind") == "delta" and isinstance(value.get("ops"), list):
        return value
    raise JsonlCorruptionError(f"Invalid document content in {description}")


def parseSidecarRecord(text: str, file: str, line: int) -> dict[str, Any]:
    description = f"{file} line {line}"
    value = parseJson(text, description)
    if (
        not isObject(value)
        or not isFormatVersion(value.get("format"))
        or value.get("type") != "record"
        or not isSafeInteger(value.get("seq"))
        or value["seq"] < 1
        or not isSafeInteger(value.get("ordinal"))
        or value["ordinal"] < 0
        or not isObject(value.get("payload"))
        or not isinstance(value["payload"].get("type"), str)
    ):
        raise JsonlCorruptionError(f"Invalid sidecar record in {description}")
    payload = value["payload"]
    if payload["type"] == "task":
        task = payload.get("value")
        if (
            not isObject(task)
            or not isSafeInteger(task.get("id"))
            or not isObject(task.get("state"))
            or task["state"].get("status") == "terminal"
        ):
            raise JsonlCorruptionError(f"Invalid live task record in {description}")
    elif payload["type"] == "document":
        if not isSafeInteger(payload.get("id")):
            raise JsonlCorruptionError(f"Invalid document record in {description}")
        validateDocumentContent(payload.get("content"), description)
    else:
        raise JsonlCorruptionError(f"Unknown sidecar record type in {description}")
    return value


def _sidecarParser(file: str) -> Callable[[str, int], dict[str, Any]]:
    return lambda text, line: parseSidecarRecord(text, file, line)


class _ParsedLine:
    __slots__ = ("start", "value")

    def __init__(self, value: dict[str, Any], start: int) -> None:
        self.value = value
        self.start = start


class _ParsedFile:
    __slots__ = ("lines", "path")

    def __init__(self, path: str, lines: list[_ParsedLine]) -> None:
        self.path = path
        self.lines = lines


class JsonlStorage:
    """Portable JSONL implementation of the storage contract."""

    def __init__(self, fs: Any, directory: str, mainPath: str, options: dict[str, Any]) -> None:
        self._fs = fs
        self._directory = directory
        self._mainPath = mainPath
        self._fsync = bool(options.get("fsync", False))  # Flush every affected sidecar before the main marker.
        self._memory = MemoryStorage()
        self._currentOnlyDocuments: set[int] = set()
        self._liveTaskSidecars: set[int] = set()
        self._closed = False
        self._poisonError: JsonlStoragePoisonedError | None = None

    @staticmethod
    @eager
    async def open(directory: str, fs: Any, context: Context, options: dict[str, Any] | None = None) -> JsonlStorage:
        """Open or create a JSONL storage directory using the supplied filesystem."""
        options = options or {}
        absolute = await fs.absolutePath(directory, context)
        if not absolute["ok"]:
            raise errorFromFile("path resolution", absolute["error"])
        created = await fs.createDir(absolute["value"], {"recursive": True}, context)
        if not created["ok"]:
            raise errorFromFile("directory creation", created["error"])
        mainPathResult = await fs.joinPath([absolute["value"], MAIN_FILE], context)
        if not mainPathResult["ok"]:
            raise errorFromFile("path join", mainPathResult["error"])
        storage = JsonlStorage(fs, absolute["value"], mainPathResult["value"], options)
        await storage._recover(context)
        return storage

    @eager
    async def commit(self, writes: Any, context: Context) -> int:
        self._assertUsable()
        prepared = self._memory.prepareCommit(writes)
        encoded = self._encodeCommit(prepared.seq, prepared.writes)
        reclamations = self._planReclamations(prepared.writes, encoded)

        async def resolve(file: str, content: str) -> dict[str, str]:
            return {"file": file, "content": content, "path": await self._resolveFile(file, context)}

        sidecars = await asyncio.gather(*(resolve(file, content) for file, content in encoded["sidecars"].items()))
        # A JavaScript commit cannot be interrupted once its I/O has started: it applies or poisons. A cancelled caller
        # must not leave bytes on disk that memory never applied.
        return await asyncio.shield(startTask(self._admit(prepared, encoded, reclamations, sidecars, context)))

    async def _admit(
        self, prepared: Any, encoded: dict[str, Any], reclamations: dict[str, str], sidecars: list[dict[str, str]], context: Context
    ) -> int:
        for sidecar in sidecars:
            result = await self._fs.appendFile(sidecar["path"], sidecar["content"], context)
            if not result["ok"]:
                raise self._poison(errorFromFile(f"append to {sidecar['file']}", result["error"]))
        if self._fsync:
            for sidecar in sidecars:
                result = await self._fs.flushFile(sidecar["path"], context)
                if not result["ok"]:
                    raise self._poison(errorFromFile(f"flush of {sidecar['file']}", result["error"]))
        marker = await self._fs.appendFile(self._mainPath, encoded["marker"], context)
        if not marker["ok"]:
            raise self._poison(errorFromFile(f"append to {MAIN_FILE}", marker["error"]))
        seq = prepared.apply()
        self._adoptSidecarState(prepared.writes)
        await self._reclaimSidecars(reclamations, context)
        return seq

    @eager
    async def mintId(self) -> int:
        return await self._store.mintId()

    @eager
    async def conversation(self, id: int, context: Context) -> Any:
        return await self._store.conversation(id, context)

    @eager
    async def scanConversations(self, query: Any, limit: int, cursor: Any, context: Context) -> Any:
        return await self._store.scanConversations(query, limit, cursor, context)

    @eager
    async def entry(self, *args: Any) -> Any:
        return await self._store.entry(*args)

    @eager
    async def findLatestHeadMarker(self, conversationId: int, atOrBeforeEntryId: int | None, context: Context) -> Any:
        return await self._store.findLatestHeadMarker(conversationId, atOrBeforeEntryId, context)

    @eager
    async def scanEntries(self, query: Any, limit: int, cursor: Any, context: Context) -> Any:
        return await self._store.scanEntries(query, limit, cursor, context)

    @eager
    async def task(self, id: int, context: Context) -> Any:
        return await self._store.task(id, context)

    @eager
    async def scanTasks(self, query: Any, limit: int, cursor: Any, context: Context) -> Any:
        return await self._store.scanTasks(query, limit, cursor, context)

    @eager
    async def submission(self, id: int, context: Context) -> Any:
        return await self._store.submission(id, context)

    @eager
    async def scanSubmissions(self, query: Any, limit: int, cursor: Any, context: Context) -> Any:
        return await self._store.scanSubmissions(query, limit, cursor, context)

    @eager
    async def submissionByRequest(self, conversationId: int, requestId: str, context: Context) -> Any:
        return await self._store.submissionByRequest(conversationId, requestId, context)

    @eager
    async def findDocument(self, address: Any, at: Any, context: Context) -> Any:
        return await self._store.findDocument(address, at, context)

    @eager
    async def document(self, id: int, at: Any, context: Context) -> Any:
        return await self._store.document(id, at, context)

    @eager
    async def scanDocuments(self, query: Any, limit: int, cursor: Any, context: Context) -> Any:
        return await self._store.scanDocuments(query, limit, cursor, context)

    @eager
    async def close(self, context: Context) -> None:
        if self._closed:
            return
        self._closed = True
        await self._memory.close(context)

    def _encodeCommit(self, seq: int, writes: Any) -> dict[str, Any]:
        mainWrites: list[dict[str, Any]] = []
        records: dict[str, list[dict[str, Any]]] = {}
        nextOrdinal = 0

        def addSidecar(file: str, payload: dict[str, Any]) -> int:
            nonlocal nextOrdinal
            ordinal = nextOrdinal
            nextOrdinal += 1
            records.setdefault(file, []).append(
                {"format": FORMAT_VERSION, "type": "record", "seq": seq, "ordinal": ordinal, "payload": payload}
            )
            return ordinal

        for write in writes:
            kind = write["type"]
            if kind in ("conversation", "entry", "submission", "document.retire"):
                mainWrites.append(write)
            elif kind == "task":
                if write["value"]["state"]["status"] == "terminal":
                    mainWrites.append(write)
                else:
                    ordinal = addSidecar(sidecarFileName("task", write["value"]["id"]), {"type": "task", "value": write["value"]})
                    mainWrites.append({"type": "task.sidecar", "id": write["value"]["id"], "ordinal": ordinal})
            elif kind == "document.create":
                ordinal = addSidecar(
                    sidecarFileName("doc", write["record"]["id"]),
                    {"type": "document", "id": write["record"]["id"], "content": write["content"]},
                )
                mainWrites.append({"type": "document.create", "record": write["record"], "ordinal": ordinal})
            elif kind == "document.change":
                ordinal = addSidecar(
                    sidecarFileName("doc", write["id"]), {"type": "document", "id": write["id"], "content": write["content"]}
                )
                mainWrites.append({"type": "document.change", "id": write["id"], "ordinal": ordinal})

        sidecars = {file: "".join(jsonLine(record) for record in fileRecords) for file, fileRecords in records.items()}
        marker = {"format": FORMAT_VERSION, "type": "commit", "seq": seq, "writes": mainWrites}
        return {"marker": jsonLine(marker), "sidecars": sidecars}

    def _planReclamations(self, writes: Any, encoded: dict[str, Any]) -> dict[str, str]:
        # ``Set``s: reclamation follows the order of the writes.
        createdCurrentOnlyDocuments: dict[int, None] = {}
        retiredDocuments: dict[int, None] = {}
        baseDocuments: dict[int, None] = {}
        finalTasks: dict[int, dict[str, Any]] = {}
        for write in writes:
            kind = write["type"]
            if kind == "document.create":
                if isCurrentOnly(write["record"]):
                    createdCurrentOnlyDocuments[write["record"]["id"]] = None
            elif kind == "document.change":
                if write["content"]["kind"] == "base":
                    baseDocuments[write["id"]] = None
            elif kind == "document.retire":
                retiredDocuments[write["id"]] = None
            elif kind == "task":
                finalTasks[write["value"]["id"]] = write["value"]

        replacements: dict[str, str] = {}

        def isCurrentOnlyDocument(id: int) -> bool:
            return id in self._currentOnlyDocuments or id in createdCurrentOnlyDocuments

        for id in retiredDocuments:
            if isCurrentOnlyDocument(id):
                replacements[sidecarFileName("doc", id)] = ""
        for id in baseDocuments:
            if not isCurrentOnlyDocument(id) or id in retiredDocuments:
                continue
            file = sidecarFileName("doc", id)
            content = encoded["sidecars"].get(file)
            if content is not None:
                replacements[file] = content
        for id, task in finalTasks.items():
            if task["state"]["status"] == "terminal" and (
                id in self._liveTaskSidecars or sidecarFileName("task", id) in encoded["sidecars"]
            ):
                replacements[sidecarFileName("task", id)] = ""
        return replacements

    def _adoptSidecarState(self, writes: Any) -> None:
        for write in writes:
            if write["type"] == "document.create":
                if isCurrentOnly(write["record"]):
                    self._currentOnlyDocuments.add(write["record"]["id"])
            elif write["type"] == "task":
                if write["value"]["state"]["status"] == "terminal":
                    self._liveTaskSidecars.discard(write["value"]["id"])
                else:
                    self._liveTaskSidecars.add(write["value"]["id"])

    async def _reclaimSidecars(self, replacements: dict[str, str], context: Context) -> None:
        """The marker already published this state, so reclamation is retryable best-effort maintenance."""
        if not replacements:
            return
        if self._fsync:
            flushed = await self._fs.flushFile(self._mainPath, context)
            if not flushed["ok"]:
                return
        for file, content in replacements.items():
            await self._replaceSidecar(file, content, context)

    async def _replaceSidecar(self, file: str, content: str, context: Context) -> None:
        path = await self._fs.joinPath([self._directory, file], context)
        if not path["ok"]:
            return
        if content == "":
            await self._fs.remove(path["value"], {"force": True}, context)
            return
        temporaryPath = await self._fs.joinPath([self._directory, f"{file}{RECLAIM_SUFFIX}"], context)
        if not temporaryPath["ok"]:
            return
        written = await self._fs.writeFile(temporaryPath["value"], content, context)
        if not written["ok"]:
            return
        if self._fsync:
            flushed = await self._fs.flushFile(temporaryPath["value"], context)
            if not flushed["ok"]:
                return
        await self._fs.renameFile(temporaryPath["value"], path["value"], context)

    async def _recover(self, context: Context) -> None:
        fs = self._fs
        directory = self._directory
        memory = self._memory
        main = await JsonlStorage._readLines(fs, self._mainPath, MAIN_FILE, context, parseMainMarker)
        previousSeq = 0
        for markerLine in main.lines:
            if markerLine.value["seq"] <= previousSeq:
                raise JsonlCorruptionError(f"Commit sequence does not strictly increase in {MAIN_FILE}")
            previousSeq = markerLine.value["seq"]

        listed = await fs.listDir(directory, context)
        if not listed["ok"]:
            raise errorFromFile("directory listing", listed["error"])
        for info in listed["value"]:
            if info["kind"] == "file" and isReclaimFileName(info["name"]):
                await fs.remove(info["path"], {"force": True}, context)
        sidecarFiles = sorted(
            info["name"] for info in listed["value"] if info["kind"] == "file" and isSidecarFileName(info["name"])
        )
        parsedFiles: dict[str, _ParsedFile] = {}
        recordByKey: dict[str, _ParsedLine] = {}
        for file in sidecarFiles:
            pathResult = await fs.joinPath([directory, file], context)
            if not pathResult["ok"]:
                raise errorFromFile("path join", pathResult["error"])
            parsed = await JsonlStorage._readLines(
                fs,
                pathResult["value"],
                file,
                context,
                _sidecarParser(file),
            )
            parsedFiles[file] = parsed
            previous: dict[str, Any] | None = None
            for line in parsed.lines:
                if previous is not None and (
                    line.value["seq"] < previous["seq"]
                    or (line.value["seq"] == previous["seq"] and line.value["ordinal"] <= previous["ordinal"])
                ):
                    raise JsonlCorruptionError(f"Sidecar records are out of order in {file}")
                previous = line.value
                recordByKey[sidecarKey(file, line.value["seq"], line.value["ordinal"])] = line

        currentOnlyDocuments: set[int] = set()
        retiredDocuments: set[int] = set()
        finalTaskIsLive: dict[int, bool] = {}
        for line in main.lines:
            for operation in line.value["writes"]:
                kind = operation["type"]
                if kind == "document.create":
                    if isCurrentOnly(operation["record"]):
                        currentOnlyDocuments.add(operation["record"]["id"])
                elif kind == "document.retire":
                    retiredDocuments.add(operation["id"])
                elif kind == "task":
                    finalTaskIsLive[operation["value"]["id"]] = False
                elif kind == "task.sidecar":
                    finalTaskIsLive[operation["id"]] = True
        retiredCurrentOnlyDocuments = {id for id in retiredDocuments if id in currentOnlyDocuments}

        latestBases: dict[int, dict[str, Any]] = {}
        for line in main.lines:
            marker = line.value
            for operation in marker["writes"]:
                if operation["type"] not in ("document.create", "document.change"):
                    continue
                id = operation["record"]["id"] if operation["type"] == "document.create" else operation["id"]
                if id not in currentOnlyDocuments:
                    continue
                found = recordByKey.get(sidecarKey(sidecarFileName("doc", id), marker["seq"], operation["ordinal"]))
                record = None if found is None else found.value
                if (
                    record is None
                    or record["payload"]["type"] != "document"
                    or record["payload"]["id"] != id
                    or record["payload"]["content"]["kind"] != "base"
                ):
                    continue
                previousBase = latestBases.get(id)
                if (
                    previousBase is None
                    or record["seq"] > previousBase["seq"]
                    or (record["seq"] == previousBase["seq"] and record["ordinal"] > previousBase["ordinal"])
                ):
                    latestBases[id] = record

        def isBeforeLatestBase(id: int, seq: int, ordinal: int) -> bool:
            base = latestBases.get(id)
            return base is not None and (seq < base["seq"] or (seq == base["seq"] and ordinal < base["ordinal"]))

        terminalTasks = {id for id, live in finalTaskIsLive.items() if not live}
        confirmed: set[str] = set()
        for line in main.lines:
            marker = line.value
            writes: list[dict[str, Any]] = []
            for operation in marker["writes"]:
                kind = operation["type"]
                if kind in ("conversation", "entry", "submission", "task", "document.retire"):
                    writes.append(operation)
                elif kind == "task.sidecar":
                    optional = operation["id"] in terminalTasks
                    record = JsonlStorage._confirmRecord(
                        marker, operation["ordinal"], sidecarFileName("task", operation["id"]), recordByKey, confirmed, optional
                    )
                    if record is not None:
                        if record["payload"]["type"] != "task" or record["payload"]["value"]["id"] != operation["id"]:
                            raise JsonlCorruptionError(f"Confirmed task sidecar data does not match commit {marker['seq']}")
                        if not optional:
                            writes.append({"type": "task", "value": record["payload"]["value"]})
                elif kind in ("document.create", "document.change"):
                    id = operation["record"]["id"] if kind == "document.create" else operation["id"]
                    reclaimed = id in retiredCurrentOnlyDocuments or isBeforeLatestBase(id, marker["seq"], operation["ordinal"])
                    record = JsonlStorage._confirmRecord(
                        marker, operation["ordinal"], sidecarFileName("doc", id), recordByKey, confirmed, reclaimed
                    )
                    content: dict[str, Any] | None = None
                    if record is not None:
                        if record["payload"]["type"] != "document" or record["payload"]["id"] != id:
                            raise JsonlCorruptionError(
                                f"Confirmed document sidecar data does not match commit {marker['seq']}"
                            )
                        content = record["payload"]["content"]
                    if kind == "document.create":
                        if content is not None and content["kind"] != "base":
                            raise JsonlCorruptionError(f"Document creation lacks a confirmed base in commit {marker['seq']}")
                        writes.append(
                            {
                                "type": "document.create",
                                "record": operation["record"],
                                "content": {"kind": "base", "version": 1, "value": {}} if reclaimed or content is None else content,
                            }
                        )
                    elif not reclaimed and content is not None:
                        writes.append({"type": "document.change", "id": id, "content": content})
            try:
                memory.prepareCommit(writes, marker["seq"]).apply()
            except Exception as error:
                raise JsonlCorruptionError(f"Invalid committed state at sequence {marker['seq']}", error) from error

        reclamations: dict[str, str] = {}
        for file, parsed in parsedFiles.items():
            unconfirmedAt: int | None = None
            for line in parsed.lines:
                key = sidecarKey(file, line.value["seq"], line.value["ordinal"])
                if key in confirmed:
                    if unconfirmedAt is not None:
                        raise JsonlCorruptionError(f"Confirmed record follows an unconfirmed tail in {file}")
                elif unconfirmedAt is None:
                    unconfirmedAt = line.start
            if unconfirmedAt is not None:
                truncated = await fs.truncateFile(parsed.path, unconfirmedAt, context)
                if not truncated["ok"]:
                    raise errorFromFile(f"tail truncation of {file}", truncated["error"])

            numericId = int(file[file.index("-") + 1 : -len(".jsonl")])
            confirmedLines = [
                line for line in parsed.lines if sidecarKey(file, line.value["seq"], line.value["ordinal"]) in confirmed
            ]
            retainedLines: list[_ParsedLine] | None = None
            if file.startswith("task-") and numericId in terminalTasks:
                retainedLines = []
            elif file.startswith("doc-"):
                if numericId in retiredCurrentOnlyDocuments:
                    retainedLines = []
                elif numericId in latestBases:
                    retainedLines = [
                        line
                        for line in confirmedLines
                        if not isBeforeLatestBase(numericId, line.value["seq"], line.value["ordinal"])
                    ]
            if retainedLines is not None and (len(retainedLines) < len(confirmedLines) or len(retainedLines) == 0):
                reclamations[file] = "".join(jsonLine(line.value) for line in retainedLines)
        await self._reclaimSidecars(reclamations, context)

        for id in currentOnlyDocuments:
            self._currentOnlyDocuments.add(id)
        for id, live in finalTaskIsLive.items():
            if live:
                self._liveTaskSidecars.add(id)

    @staticmethod
    def _confirmRecord(
        marker: dict[str, Any],
        ordinal: int,
        file: str,
        recordByKey: dict[str, _ParsedLine],
        confirmed: set[str],
        optional: bool,
    ) -> dict[str, Any] | None:
        key = sidecarKey(file, marker["seq"], ordinal)
        if key in confirmed:
            raise JsonlCorruptionError("Sidecar record is confirmed more than once")
        line = recordByKey.get(key)
        if line is None:
            if optional:
                return None
            raise JsonlCorruptionError(f"Missing confirmed sidecar record {file} at sequence {marker['seq']}")
        confirmed.add(key)
        return line.value

    @staticmethod
    async def _readLines(
        fs: Any, path: str, name: str, context: Context, parse: Callable[[str, int], dict[str, Any]]
    ) -> _ParsedFile:
        read = await fs.readBinaryFile(path, context)
        if not read["ok"]:
            if read["error"].code == "not_found":
                return _ParsedFile(path, [])
            raise errorFromFile(f"read of {name}", read["error"])
        data = read["value"]
        completeSize = len(data)
        if completeSize > 0 and data[completeSize - 1] != 0x0A:
            completeSize = data.rfind(b"\n") + 1
            truncated = await fs.truncateFile(path, completeSize, context)
            if not truncated["ok"]:
                raise errorFromFile(f"torn-line truncation of {name}", truncated["error"])
        lines: list[_ParsedLine] = []
        start = 0
        lineNumber = 1
        for end in range(completeSize):
            if data[end] != 0x0A:
                continue
            try:
                text = data[start:end].decode("utf-8")
                # A ``TextDecoder`` drops a leading byte order mark from every non-streaming decode.
                text = text.removeprefix("\ufeff")
            except UnicodeDecodeError as error:
                raise JsonlCorruptionError(f"Invalid UTF-8 in complete {name} line {lineNumber}", error) from error
            lines.append(_ParsedLine(parse(text, lineNumber), start))
            start = end + 1
            lineNumber += 1
        return _ParsedFile(path, lines)

    async def _resolveFile(self, file: str, context: Context) -> str:
        path = await self._fs.joinPath([self._directory, file], context)
        if not path["ok"]:
            raise errorFromFile("path join", path["error"])
        return path["value"]

    @property
    def _store(self) -> MemoryStorage:
        self._assertUsable()
        return self._memory

    def _poison(self, cause: Exception) -> JsonlStoragePoisonedError:
        if self._poisonError is None:
            self._poisonError = JsonlStoragePoisonedError(cause)
        return self._poisonError

    def _assertUsable(self) -> None:
        if self._closed:
            raise RuntimeError("JsonlStorage is closed")
        if self._poisonError is not None:
            raise self._poisonError


__all__ = ["JsonlCorruptionError", "JsonlStorage", "JsonlStoragePoisonedError"]
