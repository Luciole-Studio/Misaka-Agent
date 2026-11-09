"""An ``ExecutionEnv`` on another machine, reached through a pi-env daemon.

Translated from pi's ``packages/env/src/remote-env.ts`` (v1.3.0). Records are plain ``dict`` objects with the upstream
keys, as in ``misaka.durable.env``; ``Buffer.toString("utf8")`` is ``bytes.decode("utf-8", "replace")`` (the same WHATWG
decoding, byte-order mark kept).
"""

from __future__ import annotations

import asyncio
import collections
import math
import re
import uuid
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, NotRequired, TypedDict

from misaka.chord.context import Context
from misaka.durable.env import (
    ExecutionError,
    FileError,
    Result,
    StreamDecoder,
    WatchChange,
    WatchTarget,
    err,
    ok,
)
from misaka.durable.env.node import fileUrlToPath
from misaka.utils import node_path
from misaka.utils.js import (
    AbortController,
    StringDecoder,
    eager,
    errorMessage,
    formatNumber,
    jsIsSafeInteger,
    toWellFormed,
)

from .connection import Connection, Json, RemoteError, RequestOptions, nodePlatform
from .errors import abortResult, toFileError
from .list_files import listFilesOver
from .watch import RemoteWatcher, RemoteWatchOptions

MAX_TIMEOUT_MS = 2_147_483_647
MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_MS / 1000
# Bytes per `pread` request of a binary reader; several are in flight at once.
READ_CHUNK = 256 * 1024
READ_DEPTH = 8
# Node's `writeFile` writes at most this much per call and checks for an abort before each.
WRITE_CHUNK = 512 * 1024
WRITE_DEPTH = 8
# Node's `readFile`: chunk of known-size reads, chunk of unknown-size reads, and the largest file it reads.
READ_FILE_CHUNK = 512 * 1024
READ_FILE_UNKNOWN_CHUNK = 64 * 1024
READ_FILE_MAX = 2**31 - 1
# `NodeTextLineReader`'s read size.
LINE_CHUNK = 64 * 1024


class RemoteExecutionEnvOptions(TypedDict):
    """``id``: the file namespace: equal ids see the same files (``FileSystem.id``), e.g. ``pi-env:<env name>``.
    ``shellEnv``: added to the remote environment of every command that inherits it, like ``NodeExecutionEnv``'s
    ``shellEnv``. ``watch``: how the daemon watches, like ``NodeExecutionEnv``'s ``watch`` option."""

    connection: Connection
    id: str
    cwd: str
    shellPath: NotRequired[str]
    shellEnv: NotRequired[dict[str, str]]
    watch: NotRequired[RemoteWatchOptions]


def _observe(future: asyncio.Future[Any]) -> None:
    """``promise.catch(() => {})``: a failure nobody may wait for is not reported as unretrieved."""
    if not future.cancelled():
        future.exception()


async def remotePath(connection: Connection) -> SimpleNamespace:
    """Node's path rules of the remote system."""
    return node_path.win32 if nodePlatform(await connection.info())["platform"] == "win32" else node_path.posix


def toInfo(path: str, remote: Json, paths: SimpleNamespace) -> Result:
    if remote["kind"] == "other":
        return err(FileError("invalid", "Unsupported file type", path))
    return ok(
        {
            "name": paths.basename(path),
            "path": path,
            "kind": remote["kind"],
            "size": remote["size"],
            "mtimeMs": remote["mtimeSec"] * 1000 + remote["mtimeNsec"] / 1e6,
        }
    )


def concat(chunks: list[bytes], total: int) -> bytes:
    if len(chunks) == 1:
        return chunks[0]
    return b"".join(chunks)


async def readRange(
    read: Callable[[int, int], asyncio.Future[bytes]],
    offset: int,
    length: int,
    chunk: int,
    aborted: Callable[[], bool],
) -> dict[str, Any] | None:
    """Read ``length`` bytes from ``offset`` as consecutive reads of at most ``chunk`` bytes, several in flight. Ends
    early at the end of the file. A short read continues from where it ended, as sequential reads would. ``aborted`` is
    checked after each read; ``None`` means aborted."""
    chunks: list[bytes] = []
    total = 0
    next = offset
    inFlight: collections.deque[tuple[int, asyncio.Future[bytes]]] = collections.deque()
    while total < length:
        while len(inFlight) < READ_DEPTH and next < offset + length:
            size = min(offset + length - next, chunk)
            data = read(next, size)
            data.add_done_callback(_observe)
            inFlight.append((size, data))
            next += size
        headLength, headData = inFlight.popleft()
        data_ = await headData
        if aborted():
            return None
        if len(data_) == 0:
            break
        chunks.append(data_)
        total += len(data_)
        if len(data_) < headLength:
            # Reads already in flight assumed a full read; continue right after this one.
            inFlight.clear()
            next = offset + total
    return {"chunks": chunks, "total": total}


class Handle:
    """A daemon handle: valid only in the session that opened it."""

    def __init__(self, env: RemoteExecutionEnv, id: int, session: int, path: str) -> None:
        self.env = env
        self.id = id
        self.session = session
        self.path = path

    def request(
        self, op: str, json: Json | None = None, options: RequestOptions | None = None
    ) -> asyncio.Future[Any]:
        return self.env.connection.request(op, {**(json or {}), "handle": self.id}, {**(options or {}), "session": self.session})

    @eager
    async def pread(self, offset: int | None, length: int) -> bytes:
        return (await self.request("pread", {**({} if offset is None else {"offset": offset}), "length": length}))[
            "payload"
        ]

    @eager
    async def close(self) -> None:
        try:
            await self.request("close")
        except Exception:  # noqa: BLE001, S110 - closing a handle of a lost connection is a no-op
            pass


class RemoteBinaryReader:
    def __init__(self, handle: Handle) -> None:
        self._handle = handle
        self._closed = False

    @property
    def _path(self) -> str:
        return self._handle.path

    def _closedResult(self) -> Result | None:
        return err(FileError("invalid", "Binary reader is closed", self._path)) if self._closed else None

    async def info(self, context: Context) -> Result:
        early = abortResult(context.abortSignal, self._path) or self._closedResult()
        if early:
            return early
        try:
            json = (await self._handle.request("fstat"))["json"]
            return toInfo(self._path, json, await remotePath(self._handle.env.connection))
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, self._path))

    async def read(self, offset: int, length: int, context: Context) -> Result:
        early = abortResult(context.abortSignal, self._path) or self._closedResult()
        if early:
            return early
        if not jsIsSafeInteger(offset) or offset < 0 or not jsIsSafeInteger(length) or length < 0:
            return err(FileError("invalid", "Offset and length must be non-negative safe integers", self._path))
        try:
            signal = context.abortSignal
            result = await readRange(
                lambda at, size: self._handle.pread(at, size),
                int(offset),
                int(length),
                READ_CHUNK,
                lambda: signal is not None and signal.aborted,
            )
            if result is None:
                return err(FileError("aborted", "aborted", self._path))
            return ok(concat(result["chunks"], result["total"]))
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, self._path))

    async def scanLines(self, options: dict[str, Any], context: Context) -> Result:
        early = abortResult(context.abortSignal, self._path) or self._closedResult()
        if early:
            return early
        startLine: Any = options.get("startLine")
        endLine: Any = options.get("endLine")
        if (
            not jsIsSafeInteger(startLine)
            or startLine < 0
            or (endLine is not None and (not jsIsSafeInteger(endLine) or endLine <= startLine))
        ):
            return err(FileError("invalid", "Invalid line range", self._path))
        try:
            json = (
                await self._handle.request(
                    "scanLines",
                    {"startLine": startLine, **({} if endLine is None else {"endLine": endLine})},
                    {"signal": context.abortSignal} if context.abortSignal is not None else {},
                )
            )["json"]
            return ok(json)
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, self._path))

    async def close(self, _context: Context) -> None:
        if self._closed:
            return
        self._closed = True
        await self._handle.close()


class RemoteDirReader:
    def __init__(self, handle: Handle) -> None:
        self._handle = handle
        self._done = False
        self._closed = False

    async def next(self, maxEntries: int, context: Context) -> Result:
        path = self._handle.path
        aborted = abortResult(context.abortSignal, path)
        if aborted:
            return aborted
        if self._closed:
            return err(FileError("invalid", "Directory reader is closed", path))
        if not jsIsSafeInteger(maxEntries) or maxEntries <= 0:
            return err(FileError("invalid", "maxEntries must be a positive safe integer", path))
        entries: list[Any] = []
        try:
            paths = await remotePath(self._handle.env.connection)
            # Like `NodeDirReader`, count only entries that are part of the listing.
            while not self._done and len(entries) < maxEntries:
                json = (await self._handle.request("readdir", {"max": int(maxEntries) - len(entries)}))["json"]
                loopAbort = abortResult(context.abortSignal, path)
                if loopAbort:
                    return loopAbort
                for entry in json["entries"]:
                    entryPath = paths.resolve(path, entry["name"])
                    if entry.get("error") is not None:
                        # Removed between enumeration and lstat: not part of the listing any more.
                        if entry["error"].get("code") == "ENOENT":
                            continue
                        return err(toFileError(RemoteError(entry["error"]), entryPath))
                    info = toInfo(entryPath, entry["info"], paths)
                    if info["ok"]:
                        entries.append(info["value"])
                self._done = json.get("done") is True
            return ok({"entries": entries, "done": self._done})
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, path))

    async def close(self, _context: Context) -> None:
        if self._closed:
            return
        self._closed = True
        await self._handle.close()


class RemoteTextLineReader:
    """``NodeTextLineReader``: positional reads of a file opened like ``open(path, "r")``."""

    def __init__(self, handle: Handle) -> None:
        self._handle = handle
        self._decoder = StreamDecoder()
        self._offset = 0
        self._buffered = ""
        self._ended = False
        self._closed = False

    async def readLine(self, context: Context) -> Result:
        path = self._handle.path
        aborted = abortResult(context.abortSignal, path)
        if aborted:
            return aborted
        if self._closed:
            return err(FileError("invalid", "Text line reader is closed", path))
        try:
            while True:
                newline = self._buffered.find("\n")
                if newline != -1:
                    text = self._buffered[:newline]
                    self._buffered = self._buffered[newline + 1 :]
                    return ok({"text": text, "terminated": True})
                if self._ended:
                    if len(self._buffered) == 0:
                        return ok(None)
                    text = self._buffered
                    self._buffered = ""
                    return ok({"text": text, "terminated": False})
                data = await self._handle.pread(self._offset, LINE_CHUNK)
                afterReadAbort = abortResult(context.abortSignal, path)
                if afterReadAbort:
                    return afterReadAbort
                self._offset += len(data)
                if len(data) == 0:
                    self._buffered += self._decoder.decode()
                    self._ended = True
                else:
                    self._buffered += self._decoder.decode(data)
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, path))

    async def close(self, _context: Context) -> None:
        if self._closed:
            return
        self._closed = True
        self._buffered = ""
        await self._handle.close()


class RemoteExecutionEnv:
    """An ``ExecutionEnv`` on another machine, reached through a pi-env daemon. Results match ``NodeExecutionEnv``
    running on that machine: the daemon performs the system calls, and this class applies Node's path, error, and result
    rules."""

    def __init__(self, options: RemoteExecutionEnvOptions) -> None:
        self.id = options["id"]
        self.cwd = options["cwd"]
        self.connection = options["connection"]
        self._shellPath = options.get("shellPath")
        self._shellEnv = options.get("shellEnv")
        self._watchOptions: RemoteWatchOptions = options.get("watch") or {}
        # Running commands this environment started, for `cleanup()`.
        self._running: dict[int, int] = {}

    async def _resolve(self, path: str) -> str:
        """Node's ``resolvePath`` on the remote system, with its home directory and working directories."""
        info = await self.connection.info()
        windows = nodePlatform(info)["platform"] == "win32"
        paths = node_path.win32 if windows else node_path.posix
        normalized = path
        if normalized == "~":
            normalized = info["home"]
        elif normalized.startswith("~/") or (windows and normalized.startswith("~\\")):
            normalized = paths.join(info["home"], normalized[2:])
        elif normalized.startswith("file://"):
            converted = fileUrlToPath(normalized, windows)
            # Keep malformed URLs as ordinary paths, as Node does.
            if converted is not None:
                normalized = converted
        if paths.isAbsolute(normalized):
            return paths.resolve(normalized)
        if windows:
            # A drive-relative path on another drive: Node on Windows resolves it against that drive's working directory
            # (`=D:`), else its own working directory if on that drive, else the drive's root.
            match = re.match(r"^([a-zA-Z]:)(?![\\/])", normalized)
            drive = match.group(1) if match is not None else None
            if drive is not None and drive.lower() != self.cwd[0:2].lower():
                base = info["driveCwds"].get(drive.upper())
                if base is None:
                    base = info["cwd"]
                if base[0:2].lower() != drive.lower() and base[2:3] == "\\":
                    base = f"{drive}\\"
                return node_path.win32.resolve(base, normalized)
        return paths.resolve(self.cwd, normalized)

    async def _fileOp(
        self,
        path: str,
        context: Context,
        run: Callable[[str], Awaitable[Any]],
        check: str = "before",
    ) -> Result:
        try:
            resolved = await self._resolve(path)
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, path))
        aborted = abortResult(context.abortSignal, resolved)
        if aborted:
            return aborted
        try:
            value = await run(resolved)
            if check == "both":
                after = abortResult(context.abortSignal, resolved)
                if after:
                    return after
            return ok(value)
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, resolved))

    async def _open(self, resolved: str, json: Json) -> tuple[Handle, Json]:
        reply = await self.connection.request("open", {"path": resolved, **json})
        return Handle(self, reply["json"]["handle"], reply["session"], resolved), reply["json"]

    async def absolutePath(self, path: str, _context: Context) -> Result:
        try:
            return ok(await self._resolve(path))
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, path))

    async def joinPath(self, parts: list[str], _context: Context) -> Result:
        try:
            return ok((await remotePath(self.connection)).join(*parts))
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error))

    async def _readFile(self, path: str, encoding: bool, context: Context) -> Result:
        """Node's ``readFile``, with or without UTF-8 decoding, over a file opened like ``open(path, "r")``."""
        signal = context.abortSignal

        async def run(resolved: str) -> bytes | str:
            handle, json = await self._open(resolved, {"mode": "read"})

            def aborted() -> bool:
                return signal is not None and signal.aborted is True

            def abort() -> FileError:
                return FileError("aborted", "The operation was aborted", resolved)

            try:
                if json.get("statError") is not None:
                    raise RemoteError(json["statError"])
                if aborted():
                    raise abort()
                stat = json["stat"]
                size = stat["size"] if stat["kind"] == "file" else 0
                if size > READ_FILE_MAX:
                    raise FileError("unknown", f"File size ({size}) is greater than 2 GiB", resolved)
                if size == 0:
                    # Unknown size: sequential reads until the end, as for FIFOs and devices.
                    decoder = StringDecoder("utf8")
                    chunks: list[bytes] = []
                    total = 0
                    text = ""
                    while True:
                        if aborted():
                            raise abort()
                        data = await handle.pread(None, READ_FILE_UNKNOWN_CHUNK)
                        if len(data) == 0:
                            break
                        total += len(data)
                        chunks.append(data)
                        if encoding:
                            text += decoder.write(data)
                    if not encoding:
                        return b"".join(chunks)
                    return "" if total == 0 else text + decoder.end()
                if not encoding:
                    # A single read of the whole size, or reads of 512 KiB until the size or the end of the file.
                    if size <= READ_FILE_CHUNK:
                        return bytes(await handle.pread(0, size))
                    result = await readRange(lambda at, length: handle.pread(at, length), 0, size, READ_FILE_CHUNK, aborted)
                    if result is None:
                        raise abort()
                    return b"".join(result["chunks"])
                # Decoding: reads of min(size, 512 KiB) until the size, a short read, or the end of the file.
                length = min(size, READ_FILE_CHUNK)
                decoder = StringDecoder("utf8")
                text = ""
                total = 0
                first = True
                inFlight: collections.deque[asyncio.Future[bytes]] = collections.deque()
                next = 0
                while True:
                    while len(inFlight) < READ_DEPTH:
                        pending = handle.pread(next, length)
                        pending.add_done_callback(_observe)
                        inFlight.append(pending)
                        next += length
                    data = await inFlight.popleft()
                    if aborted():
                        raise abort()
                    total += len(data)
                    if len(data) == 0 or total == size or len(data) != length:
                        return data.decode("utf-8", "replace") if first else text + decoder.end(data)
                    text += decoder.write(data)
                    first = False
            finally:
                await handle.close()

        return await self._fileOp(path, context, run)

    async def readTextFile(self, path: str, context: Context) -> Result:
        return await self._readFile(path, True, context)

    async def readBinaryFile(self, path: str, context: Context) -> Result:
        return await self._readFile(path, False, context)

    async def openBinaryReader(self, path: str, options: dict[str, Any] | None, context: Context) -> Result:
        async def run(resolved: str) -> Handle:
            return (await self._open(resolved, {"noFollow": (options or {}).get("noFollow") is True}))[0]

        opened = await self._fileOp(path, context, run)
        if not opened["ok"]:
            return opened
        aborted = abortResult(context.abortSignal, opened["value"].path)
        if aborted:
            await opened["value"].close()
            return aborted
        return ok(RemoteBinaryReader(opened["value"]))

    async def openTextLineReader(self, path: str, context: Context) -> Result:
        async def run(resolved: str) -> Handle:
            return (await self._open(resolved, {"mode": "read"}))[0]

        opened = await self._fileOp(path, context, run)
        if not opened["ok"]:
            return opened
        aborted = abortResult(context.abortSignal, opened["value"].path)
        if aborted:
            await opened["value"].close()
            return aborted
        return ok(RemoteTextLineReader(opened["value"]))

    async def readTextLines(self, path: str, options: dict[str, Any] | None, context: Context) -> Result:
        maxLines = (options or {}).get("maxLines")
        if maxLines is not None and maxLines <= 0:
            return ok([])
        opened = await self.openTextLineReader(path, context)
        if not opened["ok"]:
            return opened
        lines: list[str] = []
        try:
            while maxLines is None or len(lines) < maxLines:
                line = await opened["value"].readLine(context)
                if not line["ok"]:
                    return line
                if line["value"] is None:
                    break
                lines.append(line["value"]["text"])
            return ok(lines)
        finally:
            await opened["value"].close(context)

    async def _write(self, path: str, content: str | bytes, append: bool, context: Context) -> Result:
        """Node's ``writeFile`` (abort checked before each 512 KiB write) or ``appendFile`` (no checks, one after): the
        first request creates parents, opens, and writes the first chunk; later chunks go to the same open file."""
        data = toWellFormed(content).encode("utf-8") if isinstance(content, str) else bytes(content)
        signal = None if append else context.abortSignal

        async def run(resolved: str) -> None:
            first = data[:WRITE_CHUNK]
            keep = len(data) > WRITE_CHUNK
            reply = await self.connection.request(
                "write", {"path": resolved, "append": append, "keep": keep}, {"payload": first}
            )
            if not keep:
                return
            handle = Handle(self, reply["json"]["handle"], reply["session"], resolved)
            inFlight: list[asyncio.Future[Any]] = []
            try:
                for offset in range(WRITE_CHUNK, len(data), WRITE_CHUNK):
                    if signal is not None and signal.aborted:
                        # A chunk that failed before this check fails the write, as it would have in sequence.
                        outcomes = await asyncio.gather(*inFlight, return_exceptions=True)
                        failed = next((outcome for outcome in outcomes if isinstance(outcome, BaseException)), None)
                        if failed is not None:
                            raise failed
                        raise FileError("aborted", "The operation was aborted", resolved)
                    written = handle.request("writeChunk", {}, {"payload": data[offset : offset + WRITE_CHUNK]})
                    written.add_done_callback(_observe)
                    inFlight.append(written)
                    if len(inFlight) >= WRITE_DEPTH:
                        await inFlight.pop(0)
                # The first failure; later chunks are refused by the daemon, so the file has no gaps.
                for pending in inFlight:
                    await pending
            finally:
                await asyncio.gather(*inFlight, return_exceptions=True)
                await handle.close()

        return await self._fileOp(path, context, run, "both" if append else "before")

    async def writeFile(self, path: str, content: str | bytes, context: Context) -> Result:
        return await self._write(path, content, False, context)

    async def appendFile(self, path: str, content: str | bytes, context: Context) -> Result:
        return await self._write(path, content, True, context)

    async def truncateFile(self, path: str, size: int, context: Context) -> Result:
        async def run(resolved: str) -> None:
            if not jsIsSafeInteger(size) or size < 0:
                raise FileError("invalid", "File size must be a non-negative safe integer", resolved)
            await self.connection.request("truncate", {"path": resolved, "size": size})

        return await self._fileOp(path, context, run, "both")

    async def flushFile(self, path: str, context: Context) -> Result:
        async def run(resolved: str) -> None:
            await self.connection.request("fsync", {"path": resolved})

        return await self._fileOp(path, context, run, "both")

    async def renameFile(self, sourcePath: str, destinationPath: str, context: Context) -> Result:
        try:
            source = await self._resolve(sourcePath)
            destination = await self._resolve(destinationPath)
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, sourcePath))
        aborted = abortResult(context.abortSignal, destination)
        if aborted:
            return aborted
        try:
            await self.connection.request("rename", {"path": source, "to": destination})
            return ok(None)
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, source))

    async def fileInfo(self, path: str, context: Context) -> Result:
        async def run(resolved: str) -> Result:
            json = (await self.connection.request("lstat", {"path": resolved}))["json"]
            return toInfo(resolved, json, await remotePath(self.connection))

        result = await self._fileOp(path, context, run)
        return result["value"] if result["ok"] else result

    async def listDir(self, path: str, context: Context) -> Result:
        async def run(resolved: str) -> list[Any]:
            handle = await self._openDir(resolved)
            entries: list[Json] = []
            try:
                done = False
                while not done:
                    json = (await handle.request("readdir", {"max": 1000}))["json"]
                    entries.extend(json["entries"])
                    done = json.get("done") is True
            finally:
                await handle.close()
            # Node's `readdir` fails on any entry it cannot lstat. libuv sorts names by their bytes on POSIX; on Windows
            # it keeps the file system's order, which the daemon reports.
            paths = await remotePath(self.connection)
            if paths is node_path.posix:
                entries.sort(key=lambda entry: bytes(entry["raw"]) if entry.get("raw") is not None else entry["name"].encode())
            infos: list[Any] = []
            for entry in entries:
                if context.abortSignal is not None and context.abortSignal.aborted:
                    raise FileError("aborted", "aborted", resolved)
                entryPath = paths.resolve(resolved, entry["name"])
                if entry.get("error") is not None:
                    raise toFileError(RemoteError(entry["error"]), entryPath)
                info = toInfo(entryPath, entry["info"], paths)
                if info["ok"]:
                    infos.append(info["value"])
            return infos

        return await self._fileOp(path, context, run)

    async def listFiles(self, path: str, options: dict[str, Any] | None, context: Context) -> Result:
        """Through the daemon; ``not_supported`` with a daemon released before ``listFiles``."""
        try:
            resolved = await self._resolve(path)
        except Exception as error:  # noqa: BLE001 - failures are returned, not raised
            return err(toFileError(error, path))
        return await listFilesOver(self.connection, resolved, options, context)

    async def _openDir(self, resolved: str) -> Handle:
        reply = await self.connection.request("opendir", {"path": resolved})
        return Handle(self, reply["json"]["handle"], reply["session"], resolved)

    async def openDirReader(self, path: str, context: Context) -> Result:
        opened = await self._fileOp(path, context, self._openDir)
        if not opened["ok"]:
            return opened
        aborted = abortResult(context.abortSignal, opened["value"].path)
        if aborted:
            await opened["value"].close()
            return aborted
        return ok(RemoteDirReader(opened["value"]))

    async def canonicalPath(self, path: str, context: Context) -> Result:
        async def run(resolved: str) -> str:
            return (await self.connection.request("realpath", {"path": resolved}))["json"]["path"]

        return await self._fileOp(path, context, run)

    async def exists(self, path: str, context: Context) -> Result:
        result = await self.fileInfo(path, context)
        if result["ok"]:
            return ok(True)
        if result["error"].code == "not_found":
            return ok(False)
        return err(result["error"])

    async def createDir(self, path: str, options: dict[str, Any] | None, context: Context) -> Result:
        recursive = (options or {}).get("recursive")

        async def run(resolved: str) -> None:
            await self.connection.request("mkdir", {"path": resolved, "recursive": True if recursive is None else recursive})

        return await self._fileOp(path, context, run)

    async def remove(self, path: str, options: dict[str, Any] | None, context: Context) -> Result:
        recursive = (options or {}).get("recursive")
        force = (options or {}).get("force")

        async def run(resolved: str) -> None:
            await self.connection.request(
                "rm",
                {
                    "path": resolved,
                    "recursive": False if recursive is None else recursive,
                    "force": False if force is None else force,
                },
            )

        return await self._fileOp(path, context, run)

    async def createTempDir(self, prefix: str | None, context: Context) -> Result:
        aborted = abortResult(context.abortSignal)
        if aborted:
            return aborted
        try:
            tmpdir = (await self.connection.info())["tmpdir"]
            paths = await remotePath(self.connection)
            json = (
                await self.connection.request("mkdtemp", {"path": paths.join(tmpdir, "tmp-" if prefix is None else prefix)})
            )["json"]
            return ok(json["path"])
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error))

    async def createTempFile(self, options: dict[str, Any] | None, context: Context) -> Result:
        directory = await self.createTempDir("tmp-", context)
        if not directory["ok"]:
            return directory
        filePath = ""
        try:
            paths = await remotePath(self.connection)
            prefix = (options or {}).get("prefix") or ""
            suffix = (options or {}).get("suffix") or ""
            filePath = paths.join(directory["value"], f"{prefix}{uuid.uuid4()}{suffix}")
            # Node's `writeFile(filePath, "")`, which does not create parents.
            await self.connection.request("write", {"path": filePath, "append": False, "parents": False}, {"payload": b""})
            return ok(filePath)
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error, filePath))

    async def watch(
        self, targets: list[WatchTarget], onChange: Callable[[WatchChange], None], context: Context
    ) -> Result:
        aborted = abortResult(context.abortSignal)
        if aborted:
            return aborted
        try:
            resolved: list[WatchTarget] = [{**target, "path": await self._resolve(target["path"])} for target in targets]
            afterResolve = abortResult(context.abortSignal)
            if afterResolve:
                return afterResolve
            watcher = await RemoteWatcher.open(self.connection, resolved, onChange, self._watchOptions)
            afterOpen = abortResult(context.abortSignal)
            if afterOpen:
                await watcher.close(context)
                return afterOpen
            return ok(watcher)
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(toFileError(error))

    async def exec(self, command: str | list[str], options: dict[str, Any] | None, context: Context) -> Result:
        signal = context.abortSignal
        if signal is not None and signal.aborted:
            return err(ExecutionError("aborted", "aborted"))
        options = options or {}
        timeout = options.get("timeout")
        if timeout is not None:
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, int | float)
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                return err(ExecutionError("timeout", "Invalid timeout: must be a finite number of seconds"))
            if timeout * 1000 > MAX_TIMEOUT_MS:
                return err(
                    ExecutionError("timeout", f"Invalid timeout: maximum is {formatNumber(MAX_TIMEOUT_SECONDS)} seconds")
                )
        inheritEnv = options.get("inheritEnv")
        if inheritEnv is None:
            inheritEnv = True
        try:
            cwd = await self._resolve(options["cwd"]) if options.get("cwd") else self.cwd
        except Exception as error:  # noqa: BLE001 - a failure is the result
            return err(ExecutionError("unknown", errorMessage(error)))
        if signal is not None and signal.aborted:
            return err(ExecutionError("aborted", "aborted"))
        env: dict[str, str] = {}
        variables = {**(self._shellEnv or {}), **(options.get("env") or {})} if inheritEnv else {**(options.get("env") or {})}
        for key, value in variables.items():
            if value is not None:
                env[toWellFormed(key)] = value
        callbackError: ExecutionError | None = None
        settled = False
        running: int | None = None
        controller = AbortController()

        def onAbort() -> None:
            controller.abort()

        if signal is not None:
            signal.addEventListener("abort", onAbort, once=True)
        onOutput = options.get("onOutput")

        def onStart(requestId: int, session: int) -> None:
            nonlocal running
            running = requestId
            self._running[requestId] = session

        def onEvent(event: Json, payload: bytes) -> None:
            nonlocal callbackError
            if settled or callbackError is not None or event.get("kind") != "output":
                return
            text = payload.decode("utf-8", "replace")
            if text == "" or onOutput is None:
                return
            info: dict[str, Any] = {"stream": "stderr" if event.get("stream") == "stderr" else "stdout"}
            if "skipped" in event:
                info["skipped"] = event["skipped"]
            try:
                onOutput(text, context, info)
            except Exception as error:  # noqa: BLE001 - the command fails with the callback's error
                callbackError = ExecutionError("callback_error", errorMessage(error), error)
                controller.abort()

        try:
            json = (
                await self.connection.request(
                    "exec",
                    {
                        **({"command": command} if isinstance(command, str) else {"argv": list(command)}),
                        "cwd": cwd,
                        "env": env,
                        "inheritEnv": inheritEnv,
                        **({} if self._shellPath is None else {"shellPath": self._shellPath}),
                        **({} if timeout is None else {"timeoutMs": timeout * 1000}),
                        **({} if options.get("spill") is None else {"spill": options["spill"]}),
                        **({} if options.get("window") is None else {"window": options["window"]}),
                    },
                    {"signal": controller.signal, "onStart": onStart, "onEvent": onEvent},
                )
            )["json"]
            if callbackError is not None:
                return err(callbackError)
            return ok(
                {
                    "exitCode": json["exitCode"],
                    **({"spillPath": json["spillPath"]} if isinstance(json.get("spillPath"), str) else {}),
                }
            )
        except Exception as error:  # noqa: BLE001 - a failure is the result
            if callbackError is not None:
                return err(callbackError)
            if not isinstance(error, RemoteError):
                return err(ExecutionError("unknown", errorMessage(error)))
            if error.code == "timeout":
                failure = ExecutionError("timeout", f"timeout:{formatNumber(timeout) if timeout is not None else 'undefined'}")
            elif error.code == "aborted":
                failure = ExecutionError("aborted", "aborted")
            elif error.code in ("shell_unavailable", "spawn_error"):
                failure = ExecutionError(error.code, error.message)  # type: ignore[arg-type]
            else:
                failure = ExecutionError("unknown", error.message)
            if isinstance(error.fields.get("spillPath"), str):
                failure.spillPath = error.fields["spillPath"]
            return err(failure)
        finally:
            settled = True
            if signal is not None:
                signal.removeEventListener("abort", onAbort)
            if running is not None:
                self._running.pop(running, None)

    async def cleanup(self, _context: Context) -> None:
        # Kill without aborting, so the commands settle with their killed status, as `NodeExecutionEnv` does.
        for id, session in self._running.items():
            self.connection.kill(id, session)
        self._running.clear()


__all__ = ["RemoteExecutionEnv", "RemoteExecutionEnvOptions", "remotePath"]
