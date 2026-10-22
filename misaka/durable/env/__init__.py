"""Portable execution-environment contracts: results, file and execution errors, file system, shell.

Translated from pi's ``packages/durable/src/env/index.ts`` (v1.3.0). A ``Result`` is ``{"ok": True, "value": ...}`` or
``{"ok": False, "error": ...}``; a ``FileInfo`` is ``{"name", "path", "kind", "size", "mtimeMs"}``; a text line is
``{"text", "terminated"}``. ``FileSystem`` and ``Shell`` are Protocols; ``NodeExecutionEnv`` (``env.node``) implements
both over the local machine.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NotRequired, Protocol, TypedDict

from misaka.chord.context import Context
from misaka.utils.js import jsonStringify

type Result = dict[str, Any]
"""Result of a fallible operation. Expected failures are returned instead of raised."""


def ok(value: Any = None) -> Result:
    return {"ok": True, "value": value}


def err(error: Any) -> Result:
    return {"ok": False, "error": error}


def getOrThrow(result: Result) -> Any:
    if not result["ok"]:
        raise result["error"]
    return result["value"]


def getOrUndefined(result: Result) -> Any:
    return result["value"] if result["ok"] else None


def toError(error: Any) -> Exception:
    if isinstance(error, Exception):
        return error
    if isinstance(error, str):
        return Exception(error)
    try:
        return Exception(jsonStringify(error))
    except Exception:  # noqa: BLE001
        return Exception(str(error))


type FileKind = Literal["file", "directory", "symlink"]
type FileErrorCode = Literal[
    "aborted", "not_found", "permission_denied", "not_directory", "is_directory", "invalid", "not_supported", "unknown"
]


class FileError(Exception):
    def __init__(self, code: FileErrorCode, message: str, path: str | None = None, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.name = "FileError"
        self.message = message
        self.code = code
        self.path = path
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause
type ExecutionErrorCode = Literal["aborted", "timeout", "shell_unavailable", "spawn_error", "callback_error", "unknown"]


class ExecutionError(Exception):
    def __init__(self, code: ExecutionErrorCode, message: str, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.name = "ExecutionError"
        self.message = message
        self.code = code
        self.cause = cause
        # Spill file of a command that timed out or was aborted after its output crossed the spill thresholds.
        self.spillPath: str | None = None
        if cause is not None:
            self.__cause__ = cause


class FileInfo(TypedDict):
    name: str
    path: str
    kind: FileKind
    size: int
    mtimeMs: float


class TextLine(TypedDict):
    text: str
    terminated: bool


class TextLineReader(Protocol):
    def readLine(self, context: Context) -> Awaitable[Result]: ...

    def close(self, context: Context) -> Awaitable[None]: ...


class BinaryReader(Protocol):
    """Positional reads from one opened regular file; all calls see the same file even if its path is renamed."""

    def info(self, context: Context) -> Awaitable[Result]:
        """Metadata of the opened file, not of whatever its path names now."""
        ...

    def read(self, offset: int, length: int, context: Context) -> Awaitable[Result]:
        """Up to ``length`` bytes at ``offset``; fewer only at end of file."""
        ...

    def scanLines(self, options: dict[str, Any], context: Context) -> Awaitable[Result]:
        """One pass over the file that locates lines ``[startLine, endLine)`` (``endLine`` absent: to the end), 0-based,
        where line ``k`` starts after the ``k``-th newline byte. Decoded sizes are those of the text
        ``new TextDecoder().decode(file)`` would produce for that range, so a byte-order mark at the start of the file is
        not counted."""
        ...

    def close(self, context: Context) -> Awaitable[None]: ...


class WatchExclude(TypedDict, total=False):
    hidden: bool
    names: list[str]


class WatchTarget(TypedDict):
    """A file or directory to watch. It may be missing; creating it is a change. ``recursive``: watch everything below
    a directory, not only its entries (symbolic links below it are not followed). ``exclude``: entries below ``path``
    that are neither watched nor reported: names starting with ``.``, or these names."""

    path: str
    recursive: NotRequired[bool]
    exclude: NotRequired[WatchExclude]


type WatchChange = dict[str, Any]
"""What changed:

- ``{"paths": [...]}``: something at or below each path may have changed (a directory path covers its whole subtree).
  Calls may be spurious; a change is never missed while the watcher is healthy.
- ``{"overflow": True}``: coverage was uncertain for a while (lost events, reconnect); rescan everything that is
  watched.
- ``{"error": FileError}``: the watcher stopped, for example because the watched tree grew past the environment's
  limit; no calls follow.
"""


class FileWatcher(Protocol):
    mode: Literal["native", "polling"]
    """``native``: changes are reported within about two seconds. ``polling``: the environment compares snapshots,
    because the file system does not report changes reliably (network and FUSE file systems); a change undone between
    two snapshots can be missed."""

    def close(self, context: Context) -> Awaitable[None]:
        """Stop watching; no ``onChange`` call starts after this resolves. Idempotent."""
        ...


class LineScan(TypedDict):
    """Where lines of a file are, as ``BinaryReader.scanLines`` found them.

    - ``newlines``: newline bytes in the whole file; it has ``newlines + 1`` lines.
    - ``start``/``end``: byte range of the selected lines: from the start of the first to the end of the last, without
      the newline that ends it. A selection past the last line is empty at the end of the file.
    - ``firstLineEnd``: where the first selected line ends: its newline, or the end of the file.
    - ``lastLineStart``: where the last selected line starts.
    - ``selectedBytes``/``firstLineBytes``: UTF-8 byte length of the decoded selection and of its first line.
    """

    newlines: int
    start: int
    end: int
    firstLineEnd: int
    lastLineStart: int
    selectedBytes: int
    firstLineBytes: int


class DirReader(Protocol):
    """Pages of one directory's entries."""

    def next(self, maxEntries: int, context: Context) -> Awaitable[Result]:
        """Up to ``maxEntries`` entries in the order the file system returns them, continuing where the previous call
        stopped. ``done`` marks the end; it may come with the last entries or with an empty page. An entry that
        disappears before its metadata is read is skipped, as are entries of unsupported kinds. After a failed or
        aborted call, close the reader."""
        ...

    def close(self, context: Context) -> Awaitable[None]: ...


class ListFilesOptions(TypedDict, total=False):
    """What ``FileSystem.listFiles`` lists."""

    hidden: bool
    """Include names starting with ``.`` (on Windows also files with the hidden attribute); default true. Entries named
    ``.git`` are never listed."""
    gitignore: bool
    """Inside a git work tree, skip what git ignores: the ``.gitignore`` files of the directory, its subdirectories and
    its parents, ``.git/info/exclude``, and the global excludes file; default true. Unlike ``git ls-files``, a tracked
    file that matches an ignore rule is skipped too. Outside work trees, ``.gitignore`` files have no effect."""
    exclude: list[str]
    """Names of entries to skip with everything below them, such as ``node_modules``."""
    maxEntries: int
    """Most entries to list; default 50 000."""
    maxBytes: int
    """Most bytes of the listing: each entry's UTF-8 path plus two; default 4 MiB, at most 15.9 MiB."""
    timeoutMs: float
    """Time after which the listing stops; default 10 000 ms."""


class FileListEntry(TypedDict):
    """One entry of a ``FileListing``. ``path`` is relative to the listed directory, with ``/`` separators on every
    system; a symbolic link (``kind``) is listed as such and not followed."""

    path: str
    kind: FileKind


class FileListing(TypedDict):
    """``entries``: depth first, parents before their entries, siblings in byte order of their names. ``truncated``: a
    bound in ``ListFilesOptions`` ended the listing early."""

    entries: list[FileListEntry]
    truncated: bool


class FileSystem(Protocol):
    """Portable filesystem capability. Operations return failures rather than raising. ``id`` is the file namespace:
    equal ids see the same files at the same paths, whatever their ``cwd``."""

    id: str
    cwd: str

    def absolutePath(self, path: str, context: Context) -> Awaitable[Result]: ...

    def joinPath(self, parts: list[str], context: Context) -> Awaitable[Result]: ...

    def readTextFile(self, path: str, context: Context) -> Awaitable[Result]: ...

    def openTextLineReader(self, path: str, context: Context) -> Awaitable[Result]: ...

    def readTextLines(self, path: str, options: dict[str, Any] | None, context: Context) -> Awaitable[Result]: ...

    def readBinaryFile(self, path: str, context: Context) -> Awaitable[Result]: ...

    def openBinaryReader(self, path: str, options: dict[str, Any] | None, context: Context) -> Awaitable[Result]:
        """Open a regular file for bounded positional reads. A directory fails with ``is_directory``, other non-regular
        files with ``invalid``. With ``noFollow``, a symbolic link as the final path component fails with ``invalid``
        instead of being followed; earlier components are still resolved."""
        ...

    def writeFile(self, path: str, content: str | bytes, context: Context) -> Awaitable[Result]: ...

    def appendFile(self, path: str, content: str | bytes, context: Context) -> Awaitable[Result]: ...

    def truncateFile(self, path: str, size: int, context: Context) -> Awaitable[Result]:
        """Truncate or extend a file to exactly ``size`` bytes."""
        ...

    def flushFile(self, path: str, context: Context) -> Awaitable[Result]:
        """Flush file contents and metadata needed to retrieve them from an open file handle."""
        ...

    def renameFile(self, sourcePath: str, destinationPath: str, context: Context) -> Awaitable[Result]: ...

    def fileInfo(self, path: str, context: Context) -> Awaitable[Result]: ...

    def listDir(self, path: str, context: Context) -> Awaitable[Result]: ...

    def listFiles(self, path: str, options: ListFilesOptions | None, context: Context) -> Awaitable[Result]:
        """Optional (an environment may lack the attribute): the files, directories and symbolic links below a directory,
        at any depth, for indexes such as file completion, as a ``FileListing``. Symbolic links are not followed; other
        kinds of files are not listed; entries that cannot be read are skipped. See ``ListFilesOptions`` for hidden and
        ignored files and for the bounds. Fails with ``not_supported`` when the environment cannot list right now (an
        older remote daemon), so callers fall back to their own walk."""
        ...

    def openDirReader(self, path: str, context: Context) -> Awaitable[Result]: ...

    def watch(
        self, targets: list[WatchTarget], onChange: Callable[[WatchChange], None], context: Context
    ) -> Awaitable[Result]:
        """Report changes to files and directories, for hosts that load resources from the environment. When the
        returned watcher exists, coverage is established: a host that watches before it loads cannot miss a change made
        during the load. See ``WatchChange`` for what is reported and ``FileWatcher.mode`` for how reliably."""
        ...

    def canonicalPath(self, path: str, context: Context) -> Awaitable[Result]: ...

    def exists(self, path: str, context: Context) -> Awaitable[Result]: ...

    def createDir(self, path: str, options: dict[str, Any] | None, context: Context) -> Awaitable[Result]: ...

    def remove(self, path: str, options: dict[str, Any] | None, context: Context) -> Awaitable[Result]: ...

    def createTempDir(self, prefix: str | None, context: Context) -> Awaitable[Result]: ...

    def createTempFile(self, options: dict[str, Any] | None, context: Context) -> Awaitable[Result]: ...

    def cleanup(self, context: Context) -> Awaitable[None]: ...


class ShellSpillOptions(TypedDict):
    """Spill the complete output to a temporary file once it exceeds either threshold (``afterLines`` counts complete or
    partial lines)."""

    afterBytes: int
    afterLines: int


class ShellExecResult(TypedDict):
    exitCode: int
    spillPath: NotRequired[str]  # Temporary file holding the complete raw output, when the spill thresholds were exceeded.


class ShellOutputWindow(TypedDict):
    """The tail of the combined output a caller keeps, and how often it samples it. An environment that transfers
    output over a slow link uses it to omit what the caller would drop anyway and to send no faster than the caller
    commits. ``maxBytes``: UTF-8 bytes of decoded text kept at the end of the output; ``maxLines``: lines kept at the end
    of the output; ``minIntervalMs``: minimum pause between the caller's samples of the output; ``bytesPerSecond``: each
    sample also pauses the caller in proportion to its size at this rate."""

    maxBytes: int
    maxLines: int
    minIntervalMs: float
    bytesPerSecond: float


class ShellOutputSkip(TypedDict):
    """Output an environment omitted, measured on the decoded text ``onOutput`` would have received: every U+FFFD counts
    as three bytes, and no sanitizing is applied. The shape of ``OutputSkip`` from ``misaka.chord.output``, which
    ``OutputBuffer.push()`` accepts. ``bytes``: UTF-8 byte length of the omitted text; ``newlines``:
    newlines (U+000A) in the omitted text; ``endsWithNewline``: whether the omitted text ends with a newline."""

    bytes: int
    newlines: int
    endsWithNewline: bool


class ShellOutputInfo(TypedDict):
    """``stream``: the stream the chunk came from. ``skipped``: output omitted immediately before this chunk, only with
    ``window``. The chunk then holds all output after the omission up to its end, and that is more than the window by at
    least one byte or one line: more than ``window.maxBytes`` bytes or more than ``window.maxLines`` newlines. So the
    omitted text can never be in the kept tail. The omission and such a chunk may span both streams in arrival order;
    ``stream`` then names the chunk's last stream. Callers that need the streams apart do not pass ``window``."""

    stream: Literal["stdout", "stderr"]
    skipped: NotRequired[ShellOutputSkip]


class ShellExecOptions(TypedDict, total=False):
    """``onOutput(text, context, info)`` receives every decoded chunk of stdout and stderr as it arrives, in arrival
    order, with the stream it came from: raw, unbounded, and unthrottled. Each stream is decoded separately, so a
    character split across chunks survives. ``window``: the caller keeps only this tail of the output, so the
    environment may omit output outside it and report the omission as ``info.skipped``. Without it, every chunk is
    delivered."""

    cwd: str
    env: dict[str, str]
    inheritEnv: bool
    timeout: float
    onOutput: Callable[[str, Context, ShellOutputInfo], None]
    spill: ShellSpillOptions
    window: ShellOutputWindow


class Shell(Protocol):
    def exec(self, command: str | list[str], options: ShellExecOptions | None, context: Context) -> Awaitable[Result]:
        """Run a command. A string runs through the environment's shell. An array runs ``command[0]`` directly with the
        rest as its arguments, without a shell, so they reach the program unparsed. Aborting the context or a timeout
        kills only this command's processes."""
        ...

    def cleanup(self, context: Context) -> Awaitable[None]:
        """Kill every command this environment still runs; for its owner's shutdown, never for one request."""
        ...


class ExecutionEnv(FileSystem, Shell, Protocol):
    pass


from .decode import StreamDecoder, rangeDecoder, startsWithBom
from .line_scan import LineScanner

__all__ = [
    "BinaryReader",
    "DirReader",
    "ExecutionEnv",
    "ExecutionError",
    "ExecutionErrorCode",
    "FileError",
    "FileErrorCode",
    "FileInfo",
    "FileKind",
    "FileListEntry",
    "FileListing",
    "FileSystem",
    "FileWatcher",
    "LineScan",
    "LineScanner",
    "ListFilesOptions",
    "Result",
    "Shell",
    "ShellExecOptions",
    "ShellExecResult",
    "ShellOutputInfo",
    "ShellOutputSkip",
    "ShellOutputWindow",
    "ShellSpillOptions",
    "StreamDecoder",
    "TextLine",
    "TextLineReader",
    "WatchChange",
    "WatchExclude",
    "WatchTarget",
    "err",
    "getOrThrow",
    "getOrUndefined",
    "ok",
    "rangeDecoder",
    "startsWithBom",
    "toError",
]
