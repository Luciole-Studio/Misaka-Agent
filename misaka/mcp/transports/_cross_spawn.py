"""MISAKA fork: cross-spawn 7.0.6 and the slice of Node's ``ChildProcess`` that pi's stdio transport uses.

pi spawns stdio servers with the ``cross-spawn`` npm package, which on Windows resolves the command
(``which`` 2.0.2, ``isexe`` 2.0.0, ``path-key`` 3.1.1), honours shebangs (``shebang-command`` 2.0.0)
and runs anything that is not an ``.exe`` or ``.com`` -- ``npx.cmd``, ``uvx`` shims -- through
``cmd.exe /d /s /c`` with every argument escaped for it. The parsing below is translated from those
packages (MIT/ISC, see THIRD_PARTY_NOTICES.md); only what the stdio transport calls is ported (no ``spawnSync``,
no ``shell`` or ``forceShell`` options). The process itself is a ``subprocess.Popen`` with
threads that read its pipes and wait for it, handing everything to the event loop the way Node's
``ChildProcess`` emits ``spawn``, ``data``, ``exit`` and ``close``.
"""

from __future__ import annotations

import asyncio
import errno
import os
import queue
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from misaka.utils import node_path
from misaka.utils.shell import trackDetachedChildPid, untrackDetachedChildPid

isWin = sys.platform == "win32"

_IS_EXECUTABLE = re.compile(r"\.(?:com|exe)\Z", re.IGNORECASE)
_IS_CMD_SHIM = re.compile(r"node_modules[\\/].bin[\\/][^\\/]+\.cmd\Z", re.IGNORECASE)
# See http://www.robvanderwoude.com/escapechars.php
_META_CHARS = re.compile(r'([()\][%!^"`<>&|;, *?])')
# `shebang-regex`: `/^#!(.*)/`, where a JavaScript `.` stops at every line terminator.
_SHEBANG = re.compile("^#!([^\n\r  ]*)")


# -- path-key ------------------------------------------------------------------------------------


def pathKey(env: Mapping[str, str] | None = None) -> str:
    environment = env if env is not None else os.environ
    if not isWin:
        return "PATH"
    return next((key for key in reversed(list(environment)) if key.upper() == "PATH"), "Path")


# -- process.env ---------------------------------------------------------------------------------


def _startupNames() -> list[str]:
    """Windows only: the environment's names as the process started with them; ``nt.environ`` keeps their
    spelling, which ``os.environ`` turns to capitals."""
    import nt  # type: ignore[import-not-found]

    return list(nt.environ)


def processEnv(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """``{...process.env}`` for a child's environment. On Windows Node spells each name as the process started
    with it (``Path``), where Python's ``os.environ`` capitalises every name (``PATH``); spelled as Node spells
    it, a server ``env`` that names ``Path`` replaces the inherited one, as in pi, instead of standing beside it
    and losing to it in ``_windowsEnv``. A name set after startup keeps ``os.environ``'s capitals."""
    environ = os.environ if environ is None else environ
    if not isWin:
        return dict(environ)
    spelling = {name.upper(): name for name in _startupNames()}
    return {spelling.get(name.upper(), name): value for name, value in environ.items()}


# -- isexe (windows.js) and which ----------------------------------------------------------------


def _checkPathExt(path: str, pathExt: str | None) -> bool:
    pathext = pathExt if pathExt is not None else os.environ.get("PATHEXT")
    if not pathext:
        return True
    extensions = pathext.split(";")
    if "" in extensions:
        return True
    return any(extension and path[-len(extension) :].lower() == extension.lower() for extension in extensions)


def _isexe(path: str, pathExt: str | None) -> bool:
    if isWin:
        # `fs.statSync` follows links; `isSymbolicLink()` on its result is never true.
        return os.path.isfile(path) and _checkPathExt(path, pathExt)
    try:
        stat = os.stat(path)
    except OSError:
        return False
    if not os.path.isfile(path):
        return False
    return bool(stat.st_mode & 0o111) and os.access(path, os.X_OK)


def _which(cmd: str, path: str | None, pathExt: str | None, cwd: str) -> str | None:
    """``which.sync(cmd, {path, pathExt, nothrow})``, with the process cwd ``cross-spawn`` switches to
    passed in instead (a process-wide ``chdir`` would race every other task)."""
    colon = ";" if isWin else ":"
    if "/" in cmd or (isWin and "\\" in cmd):
        pathEnv = [""]
    else:
        # `opt.path || process.env.PATH || ""`: an empty PATH falls back to the process's.
        pathEnv = [*([cwd] if isWin else []), *(path or os.environ.get("PATH") or "").split(colon)]
    pathExtExe = (pathExt or os.environ.get("PATHEXT") or ".EXE;.CMD;.BAT;.COM") if isWin else ""
    extensions = pathExtExe.split(colon) if isWin else [""]
    if isWin and "." in cmd and extensions[0] != "":
        extensions.insert(0, "")
    for ppRaw in pathEnv:
        pathPart = ppRaw[1:-1] if len(ppRaw) >= 2 and ppRaw.startswith('"') and ppRaw.endswith('"') else ppRaw
        pCmd = node_path.join(pathPart, cmd)
        p = cmd[:2] + pCmd if not pathPart and re.match(r"^\.[\\/]", cmd) else pCmd
        for extension in extensions:
            current = p + extension
            candidate = current if os.path.isabs(current) else os.path.join(cwd, current)
            if _isexe(candidate, pathExtExe):
                return current
    return None


# -- cross-spawn lib/util ------------------------------------------------------------------------


def _resolveCommandAttempt(parsed: _Parsed, withoutPathExt: bool) -> str | None:
    env = parsed.options.get("env") or os.environ
    cwdOption = parsed.options.get("cwd")
    # `process.chdir(cwd)` fails quietly for a directory that does not exist, and `which` then searches the
    # process's own cwd.
    cwd = os.path.abspath(cwdOption) if cwdOption is not None and os.path.isdir(cwdOption) else os.getcwd()
    resolved = _which(parsed.command, env.get(pathKey(env)), os.pathsep if withoutPathExt else None, cwd)
    # If we successfully resolved, ensure that an absolute path is returned.
    if resolved:
        resolved = os.path.abspath(os.path.join(cwdOption if cwdOption is not None else "", resolved))
    return resolved


def _resolveCommand(parsed: _Parsed) -> str | None:
    return _resolveCommandAttempt(parsed, False) or _resolveCommandAttempt(parsed, True)


def _shebangCommand(string: str = "") -> str | None:
    match = _SHEBANG.match(string)
    if not match:
        return None
    parts = re.sub(r"#! ?", "", match.group(0), count=1).split(" ")
    path, argument = parts[0], parts[1] if len(parts) > 1 else None
    binary = path.split("/")[-1]
    if binary == "env":
        return argument
    return f"{binary} {argument}" if argument else binary


def _readShebang(command: str) -> str | None:
    # Read the first 150 bytes from the file, zero-padded like `Buffer.alloc`.
    buffer = bytearray(150)
    try:
        with open(command, "rb") as handle:
            data = handle.read(150)
        buffer[: len(data)] = data
    except OSError:
        pass
    return _shebangCommand(bytes(buffer).decode("utf-8", "replace"))


def escapeCommand(arg: str) -> str:
    return _META_CHARS.sub(r"^\1", arg)


def escapeArgument(arg: str, doubleEscapeMetaChars: bool) -> str:
    # Algorithm below is based on https://qntm.org/cmd (cross-spawn's JavaScript regexes match one
    # backslash before a quote and at the end, the empty group matching empty where Python's would fail).
    arg = f"{arg}"
    # Sequence of backslashes followed by a double quote: double up the backslash and escape the quote.
    arg = re.sub(r'(\\?)"', lambda match: match.group(1) * 2 + '\\"', arg)
    # A backslash at the end of the string (which will become a double quote later): double it up.
    arg = re.sub(r"(\\?)\Z", lambda match: match.group(1) * 2, arg, count=1)
    # Quote the whole thing:
    arg = f'"{arg}"'
    # Escape meta chars
    arg = _META_CHARS.sub(r"^\1", arg)
    # Double escape meta chars if necessary
    if doubleEscapeMetaChars:
        arg = _META_CHARS.sub(r"^\1", arg)
    return arg


# -- cross-spawn lib/parse -----------------------------------------------------------------------


@dataclass(slots=True)
class _Parsed:
    command: str
    args: list[str]
    options: dict[str, Any]
    original: tuple[str, list[str]]
    file: str | None = None
    # Set when the arguments are already escaped for cmd.exe (`windowsVerbatimArguments`).
    verbatim: bool = False
    # The resolved program to start, where libuv would search for it itself.
    executable: str | None = None


def _detectShebang(parsed: _Parsed) -> str | None:
    parsed.file = _resolveCommand(parsed)
    shebang = _readShebang(parsed.file) if parsed.file else None
    if shebang:
        parsed.args.insert(0, parsed.file)  # type: ignore[arg-type]
        parsed.command = shebang
        return _resolveCommand(parsed)
    return parsed.file


def _parseNonShell(parsed: _Parsed) -> _Parsed:
    if not isWin:
        return parsed
    # Detect & add support for shebangs
    commandFile = _detectShebang(parsed)
    # We don't need a shell if the command filename is an executable. (`RegExp.test(undefined)` tests
    # the string "undefined".)
    needsShell = not _IS_EXECUTABLE.search(commandFile if commandFile is not None else "undefined")
    if needsShell:
        # Need to double escape meta chars if the command is a cmd-shim located in `node_modules/.bin/`.
        needsDoubleEscapeMetaChars = bool(_IS_CMD_SHIM.search(commandFile or "undefined"))
        # Normalize posix paths into OS compatible paths (e.g.: foo/bar -> foo\bar)
        parsed.command = node_path.win32Normalize(parsed.command)
        parsed.command = escapeCommand(parsed.command)
        parsed.args = [escapeArgument(arg, needsDoubleEscapeMetaChars) for arg in parsed.args]
        shellCommand = " ".join([parsed.command, *parsed.args])
        parsed.args = ["/d", "/s", "/c", f'"{shellCommand}"']
        parsed.command = os.environ.get("COMSPEC") or os.environ.get("comspec") or "cmd.exe"
        parsed.verbatim = True
    else:
        # Node hands the command to libuv, which searches the cwd and then the child's PATH, the order
        # `which` resolved `commandFile` in. CreateProcess would search misaka's own directory first.
        parsed.executable = commandFile
    return parsed


def parse(command: str, args: Sequence[str] | None, options: Mapping[str, Any]) -> _Parsed:
    argList = list(args) if args else []
    parsed = _Parsed(command=command, args=argList, options=dict(options), original=(command, list(argList)))
    return _parseNonShell(parsed)


# -- Node's ChildProcess -------------------------------------------------------------------------


class _Emitter:
    """The ``on``/``once``/``off``/``emit`` slice of Node's ``EventEmitter``."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[tuple[Callable[..., Any], bool]]] = {}

    def on(self, event: str, listener: Callable[..., Any]) -> _Emitter:
        self._listeners.setdefault(event, []).append((listener, False))
        return self

    def once(self, event: str, listener: Callable[..., Any]) -> _Emitter:
        self._listeners.setdefault(event, []).append((listener, True))
        return self

    def off(self, event: str, listener: Callable[..., Any]) -> _Emitter:
        entries = self._listeners.get(event, [])
        for index, (registered, _once) in enumerate(entries):
            if registered is listener:
                del entries[index]
                break
        return self

    def emit(self, event: str, *args: Any) -> bool:
        entries = list(self._listeners.get(event, []))
        for listener, once in entries:
            if once:
                self.off(event, listener)
            listener(*args)
        return bool(entries)


class _Readable(_Emitter):
    pass


class Writable(_Emitter):
    """``child.stdin``: ``write(data, callback)`` and ``end()`` through one writer thread, in order."""

    def __init__(self, child: ChildProcess) -> None:
        super().__init__()
        self._child = child
        self.writable = True
        self.destroyed = False
        self._queue: queue.Queue[tuple[bytes, Callable[[BaseException | None], None] | None] | None] = queue.Queue()

    def write(self, data: str | bytes, callback: Callable[[BaseException | None], None] | None = None) -> None:
        if self.destroyed:
            if callback is not None:
                self._child._call(callback, _streamDestroyedError("write"))
            return
        self._queue.put((data.encode("utf-8") if isinstance(data, str) else data, callback))

    def end(self) -> None:
        if not self.writable:
            return
        self.writable = False
        self._queue.put(None)

    def destroy(self, error: BaseException | None = None) -> None:
        """Node's ``destroy``: the pipe closes once the writes already queued fail, and ``error`` is emitted once."""
        if self.destroyed:
            return
        self.destroyed = True
        if self.writable:
            self.writable = False
            self._queue.put(None)
        if error is not None:
            self.emit("error", error)


def _streamDestroyedError(method: str) -> Exception:
    failure = Exception(f"Cannot call {method} after a stream was destroyed")
    failure.code = "ERR_STREAM_DESTROYED"  # type: ignore[attr-defined]
    return failure


@dataclass
class ChildProcess(_Emitter):
    """The ``ChildProcess`` pi's stdio transport sees: ``pid``, ``exitCode``, ``signalCode``, ``stdin``,
    ``stdout``, ``stderr``, ``kill()``, and the ``spawn``, ``error``, ``exit`` and ``close`` events, all
    delivered on the event loop."""

    _parsed: _Parsed
    pid: int | None = None
    exitCode: int | None = None
    signalCode: str | None = None
    stdin: Writable | None = None
    stdout: _Readable | None = None
    stderr: _Readable | None = None
    _process: subprocess.Popen[bytes] | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _openStreams: int = 0
    _exited: bool = False
    _threads: list[threading.Thread] = field(default_factory=list)

    def __post_init__(self) -> None:
        _Emitter.__init__(self)

    def kill(self, signal: str = "SIGTERM") -> bool:
        process = self._process
        if process is None or process.returncode is not None:
            return False
        try:
            if isWin:
                process.kill()
            else:
                import signal as signals

                process.send_signal(getattr(signals, signal))
        except ProcessLookupError:
            # Already dead.
            return False
        except OSError as error:
            code = errno.errorcode.get(error.errno or 0, "UNKNOWN")
            failure = Exception(f"kill {code}")
            failure.code = code  # type: ignore[attr-defined]
            failure.errno = code  # type: ignore[attr-defined]
            failure.syscall = "kill"  # type: ignore[attr-defined]
            self.emit("error", failure)
            return False
        return True

    # Called on the event loop by the threads below.

    def _call(self, callback: Callable[..., Any], *args: Any) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            # The loop closed while the process still ran; nobody is listening any more.
            pass

    def _onExit(self, returncode: int) -> None:
        if returncode < 0 and not isWin:
            import signal as signals

            try:
                self.signalCode = signals.Signals(-returncode).name
            except ValueError:
                self.signalCode = str(-returncode)
        else:
            self.exitCode = returncode
        if self.pid is not None:
            untrackDetachedChildPid(self.pid)
        # Node destroys stdin when the process exits; the writer thread and the pipe end with it.
        if self.stdin is not None:
            self.stdin.destroy()
        self._exited = True
        error = _verifyENOENT(self.exitCode, self._parsed)
        if error is not None:
            self.emit("error", error)
        else:
            self.emit("exit", self.exitCode, self.signalCode)
        self._maybeClose()

    def _onStreamEnd(self) -> None:
        self._openStreams -= 1
        self._maybeClose()

    def _maybeClose(self) -> None:
        if self._exited and self._openStreams == 0:
            self._openStreams = -1
            self.emit("close", self.exitCode, self.signalCode)


def _nodeSystemError(error: OSError, command: str, args: list[str]) -> Exception:
    """The error Node's ``spawn`` reports: ``spawn <command> ENOENT``."""
    code = errno.errorcode.get(error.errno or 0, "UNKNOWN")
    failure = Exception(f"spawn {command} {code}")
    failure.code = code  # type: ignore[attr-defined]
    failure.errno = code  # type: ignore[attr-defined]
    failure.syscall = f"spawn {command}"  # type: ignore[attr-defined]
    failure.path = command  # type: ignore[attr-defined]
    failure.spawnargs = args  # type: ignore[attr-defined]
    return failure


def _verifyENOENT(status: int | None, parsed: _Parsed) -> Exception | None:
    if isWin and status == 1 and not parsed.file:
        command, args = parsed.original
        failure = Exception(f"spawn {command} ENOENT")
        failure.code = "ENOENT"  # type: ignore[attr-defined]
        failure.errno = "ENOENT"  # type: ignore[attr-defined]
        failure.syscall = f"spawn {command}"  # type: ignore[attr-defined]
        failure.path = command  # type: ignore[attr-defined]
        failure.spawnargs = args  # type: ignore[attr-defined]
        return failure
    return None


# libuv adds these from the parent when a child's environment lacks them on Windows.
_REQUIRED_WINDOWS_VARS = (
    "HOMEDRIVE", "HOMEPATH", "LOGONSERVER", "PATH", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "USERDOMAIN",
    "USERNAME", "USERPROFILE", "WINDIR",
)


def _windowsEnv(env: Mapping[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    sawKey: set[str] = set()
    # Env keys are case insensitive on Windows: the first of each name in sorted order (UTF-16 code units) is kept.
    for key in sorted(env, key=lambda name: name.encode("utf-16-be")):
        uppercaseKey = key.upper()
        if uppercaseKey in sawKey:
            continue
        sawKey.add(uppercaseKey)
        merged[key] = env[key]
    upper = {key.upper() for key in merged}
    for name in _REQUIRED_WINDOWS_VARS:
        if name not in upper and name in os.environ:
            merged[name] = os.environ[name]
    return merged


def spawn(command: str, args: Sequence[str], options: Mapping[str, Any]) -> ChildProcess:
    """``crossSpawn(command, args, {cwd, env, stdio: ["pipe", "pipe", "pipe" | "inherit"], windowsHide,
    detached})``. A spawn failure is reported through the ``error`` event, as Node does."""
    parsed = parse(command, args, options)
    child = ChildProcess(_parsed=parsed)
    child._loop = asyncio.get_running_loop()
    inheritStderr = options.get("stderr") == "inherit"
    env = options.get("env")
    popenOptions: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": None if inheritStderr else subprocess.PIPE,
        "cwd": options.get("cwd"),
        "env": _windowsEnv(env) if isWin and env is not None else env,
    }
    if isWin:
        startupinfo = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        startupinfo.wShowWindow = 0  # SW_HIDE
        popenOptions["startupinfo"] = startupinfo
        # libuv adds CREATE_NO_WINDOW only when no stdio is inherited.
        popenOptions["creationflags"] = 0 if inheritStderr else subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        if parsed.verbatim:
            popenArgs: Any = " ".join([parsed.command, *parsed.args])
            if " " in parsed.command:
                popenOptions["executable"] = parsed.command
        else:
            popenArgs = [parsed.command, *parsed.args]
            if parsed.executable:
                popenOptions["executable"] = parsed.executable
    else:
        popenArgs = [parsed.command, *parsed.args]
        popenOptions["start_new_session"] = bool(options.get("detached"))
    try:
        process = subprocess.Popen(popenArgs, **popenOptions)
    except OSError as error:
        # Node's `onErrorNT` runs `onexit(err, null)`: the exit code is the negative errno, the
        # error is emitted in place of `exit`, and `close` follows at once, there being no streams.
        child.exitCode = -(error.errno or 0)
        child._exited = True
        failure = _nodeSystemError(error, parsed.command, parsed.args)

        def onErrorNT() -> None:
            child.emit("error", failure)
            child._maybeClose()

        child._loop.call_soon(onErrorNT)
        return child
    child._process = process
    child.pid = process.pid
    if isWin:
        from misaka.utils import libuv_windows

        # libuv puts every child into one job that ends them when this process exits, however it exits.
        libuv_windows.assignToJob(process)
    # MISAKA fork: misaka's signal-time cleanup kills the servers a session started.
    trackDetachedChildPid(process.pid)
    child.stdin = Writable(child)
    child.stdout = _Readable()
    child._openStreams = 1
    if process.stderr is not None:
        child.stderr = _Readable()
        child._openStreams += 1
    _startThreads(child, process)
    child._loop.call_soon(child.emit, "spawn")
    return child


def _startThreads(child: ChildProcess, process: subprocess.Popen[bytes]) -> None:
    def read(stream: Any, readable: _Readable) -> None:
        fd = stream.fileno()
        try:
            while True:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as error:
                    child._call(readable.emit, "error", error)
                    break
                if not chunk:
                    break
                child._call(readable.emit, "data", chunk)
        finally:
            stream.close()
            child._call(child._onStreamEnd)

    def wait() -> None:
        returncode = process.wait()
        child._call(child._onExit, returncode)

    def write() -> None:
        assert child.stdin is not None and process.stdin is not None
        stdin, pipe = child.stdin, process.stdin
        failed = False
        while True:
            item = stdin._queue.get()
            if item is None:
                try:
                    pipe.close()
                except OSError:
                    pass
                return
            data, callback = item
            if failed or stdin.destroyed:
                if callback is not None:
                    child._call(callback, _streamDestroyedError("write"))
                continue
            try:
                pipe.write(data)
                pipe.flush()
            except (OSError, ValueError) as error:
                # A failed write destroys the stream: its callback gets the error, the stream emits it once,
                # and the writes queued behind it fail until the pipe closes.
                failed = True
                if callback is not None:
                    child._call(callback, error)
                child._call(stdin.destroy, error)
                continue
            if callback is not None:
                child._call(callback, None)

    targets = [(read, (process.stdout, child.stdout)), (wait, ()), (write, ())]
    if process.stderr is not None:
        targets.append((read, (process.stderr, child.stderr)))
    for target, args in targets:
        thread = threading.Thread(target=target, args=args, name=f"mcp-stdio-{process.pid}", daemon=True)
        child._threads.append(thread)
        thread.start()


__all__ = ["ChildProcess", "Writable", "escapeArgument", "escapeCommand", "parse", "pathKey", "spawn"]
