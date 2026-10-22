"""The stdio transport: a server process speaking newline-delimited JSON-RPC on stdin and stdout.

Translated from pi's ``packages/mcp/src/transports/stdio.ts``.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from types import MappingProxyType
from typing import Any, Final, Literal, Required, TypedDict

from misaka.mcp.protocol.jsonrpc import (
    JsonRpcMessage,
    McpConnectionClosedError,
    parseJsonRpcMessage,
)
from misaka.mcp.transports import _cross_spawn as crossSpawn
from misaka.mcp.transports.transport import (
    DEFAULT_MAX_MESSAGE_BYTES,
    McpTransportSendOptions,
    TransportEvents,
)
from misaka.utils.js import Timeout, jsonParse, jsonStringify, jsTrim, setTimeout

DEFAULT_MAX_STDERR_BYTES: Final = 64 * 1024
DEFAULT_CLOSE_TIMEOUT_MS: Final = 2_000
# How long a server gets to exit on its own after stdin closes, before it is sent SIGTERM.
STDIN_CLOSE_GRACE_MS: Final = 500
USE_PROCESS_GROUPS: Final = sys.platform != "win32"

# Process groups of running servers, killed if the host exits without closing them.
liveProcessGroups: set[int] = set()
exitHookInstalled = False


def _taskkill() -> str:
    # MISAKA fork: the system's own taskkill, not whatever `taskkill` the PATH finds first.
    systemRoot = os.environ.get("SystemRoot") or r"C:\Windows"
    return os.path.join(systemRoot, "System32", "taskkill.exe")


def killProcessTree(child: crossSpawn.ChildProcess, signal: Literal["SIGTERM", "SIGKILL"]) -> None:
    pid = child.pid
    if sys.platform == "win32" and pid is not None:
        if child.exitCode is not None:
            return
        # Windows has no graceful signals, and cross-spawn runs `.cmd` shims through cmd.exe; killing only
        # that would leave the server running.
        try:
            killer = subprocess.Popen(
                [_taskkill(), "/pid", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
            )
        except OSError:
            return
        # Reaped off the loop, as Node lets a fire-and-forget child go.
        threading.Thread(target=killer.wait, name=f"mcp-taskkill-{pid}", daemon=True).start()
        return
    if USE_PROCESS_GROUPS and pid is not None:
        import signal as signals

        try:
            # Negative pid: the whole group, so wrappers like `npx` or `uvx` do not leave the server behind.
            os.killpg(pid, getattr(signals, signal))  # windows-footgun: ok - POSIX branch
            return
        except OSError:
            # The group is gone or was never created; fall back to the direct child.
            pass
    try:
        child.kill(signal)
    except Exception:  # noqa: BLE001, S110 - upstream `catch {}`
        pass


def installExitHook() -> None:
    global exitHookInstalled
    if exitHookInstalled:
        return
    exitHookInstalled = True

    def killGroups() -> None:
        import signal as signals

        for pid in list(liveProcessGroups):
            try:
                os.killpg(pid, signals.SIGTERM)  # windows-footgun: ok - only installed on POSIX
            except OSError:
                pass

    atexit.register(killGroups)


class StdioTransportOptions(TypedDict, total=False):
    command: Required[str]
    args: Sequence[str]
    cwd: str
    env: dict[str, str]
    inheritEnv: bool
    stderr: Literal["pipe", "inherit"]
    onStderr: Callable[[str], None]
    maxMessageBytes: int
    maxStderrBytes: int
    # Time to wait for the server to exit after SIGTERM before sending SIGKILL. Default: 2000.
    closeTimeoutMs: float


class StdioTransport(TransportEvents):
    def __init__(self, options: StdioTransportOptions) -> None:
        super().__init__()
        args = options.get("args")
        self.options = MappingProxyType({**options, "args": list(args) if args is not None else None})
        self._child: crossSpawn.ChildProcess | None = None
        self._stdoutBuffer = bytearray()
        self._stderrBuffer = bytearray()
        self._started = False
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._child.pid if self._child is not None else None

    @property
    def stderr(self) -> str:
        return bytes(self._stderrBuffer).decode("utf-8", "replace")

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("MCP stdio transport already started")
        if self._closed:
            raise McpConnectionClosedError()
        self._started = True
        optionEnv = self.options.get("env") or {}
        env = {**optionEnv} if self.options.get("inheritEnv") is False else {**os.environ, **optionEnv}
        args = self.options.get("args")
        child = crossSpawn.spawn(
            self.options["command"],
            args if args is not None else [],
            {
                "cwd": self.options.get("cwd"),
                "env": env,
                "stderr": "inherit" if self.options.get("stderr") == "inherit" else "pipe",
                # Own process group, so closing the transport can terminate the server's children too.
                "detached": USE_PROCESS_GROUPS,
            },
        )
        self._child = child
        pid = child.pid
        if USE_PROCESS_GROUPS and pid is not None:
            installExitHook()
            liveProcessGroups.add(pid)
            child.once("exit", lambda *_args: liveProcessGroups.discard(pid))
        if child.stdout is not None:
            child.stdout.on("data", self._handleStdout)
            child.stdout.on("error", self._emitError)
        if child.stdin is not None:
            child.stdin.on("error", lambda error: None if self._closed else self._emitError(error))
        if child.stderr is not None:
            child.stderr.on("data", self._handleStderr)
            child.stderr.on("error", self._emitError)

        def onClose(*_args: Any) -> None:
            self._child = None
            if jsTrim(bytes(self._stdoutBuffer).decode("utf-8", "replace")):
                self._emitError(RuntimeError("MCP stdio server closed with an incomplete JSON-RPC message"))
            self._stdoutBuffer = bytearray()
            self._emitClose()

        child.on("close", onClose)

        spawned: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def onSpawn() -> None:
            child.off("error", onError)
            if not spawned.done():
                spawned.set_result(None)

        def onError(error: BaseException) -> None:
            child.off("spawn", onSpawn)
            if not spawned.done():
                spawned.set_exception(error)

        child.once("spawn", onSpawn)
        child.once("error", onError)
        await spawned
        child.on("error", lambda error: None if self._closed else self._emitError(error))

    async def send(self, message: JsonRpcMessage, options: McpTransportSendOptions | None = None) -> None:
        # MISAKA fork: `options` is accepted and ignored, as upstream's `send(message)` ignores it (see `McpTransport`).
        stdin = self._child.stdin if self._child is not None else None
        if not self._started or self._closed or stdin is None or not stdin.writable:
            raise McpConnectionClosedError()
        payload = f"{jsonStringify(message)}\n"
        written: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def onWritten(error: BaseException | None) -> None:
            if written.done():
                return
            if error is not None:
                written.set_exception(error)
            else:
                written.set_result(None)

        stdin.write(payload, onWritten)
        await written

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        child = self._child
        if child is None:
            self._emitClose()
            return
        if child.exitCode is not None or child.signalCode is not None:
            if child.stdin is not None:
                child.stdin.end()
            return
        # Shutdown per the spec: close stdin and let the server exit, then SIGTERM, then SIGKILL.
        closeTimeoutMs = self.options.get("closeTimeoutMs")
        if closeTimeoutMs is None:
            closeTimeoutMs = DEFAULT_CLOSE_TIMEOUT_MS
        loop = asyncio.get_running_loop()
        closed: asyncio.Future[None] = loop.create_future()
        timers: list[Timeout] = []

        def onClose(*_args: Any) -> None:
            for timer in timers:
                timer.cancel()
            # Children of the server that ignored stdin closing would otherwise outlive it.
            killProcessTree(child, "SIGTERM")
            if not closed.done():
                closed.set_result(None)

        child.once("close", onClose)
        grace = min(STDIN_CLOSE_GRACE_MS, closeTimeoutMs)
        timers.append(setTimeout(lambda: killProcessTree(child, "SIGTERM"), grace))
        timers.append(setTimeout(lambda: killProcessTree(child, "SIGKILL"), grace + closeTimeoutMs))
        if child.stdin is not None:
            child.stdin.end()
        await closed

    def _handleStdout(self, chunk: bytes) -> None:
        self._stdoutBuffer += chunk
        maxMessageBytes = self.options.get("maxMessageBytes")
        if maxMessageBytes is None:
            maxMessageBytes = DEFAULT_MAX_MESSAGE_BYTES
        while True:
            newline = self._stdoutBuffer.find(b"\n")
            if newline < 0:
                if len(self._stdoutBuffer) > maxMessageBytes:
                    self._stdoutBuffer = bytearray()
                    self._emitError(RuntimeError(f"MCP stdio message exceeds {maxMessageBytes} bytes"))
                return
            line = bytes(self._stdoutBuffer[:newline])
            del self._stdoutBuffer[: newline + 1]
            if len(line) > maxMessageBytes:
                self._emitError(RuntimeError(f"MCP stdio message exceeds {maxMessageBytes} bytes"))
                continue
            text = line.decode("utf-8", "replace").removesuffix("\r")
            if not jsTrim(text):
                continue
            try:
                self._emitMessage(parseJsonRpcMessage(jsonParse(text)))
            except Exception as error:  # noqa: BLE001 - reported, as upstream
                self._emitError(error)

    def _handleStderr(self, chunk: bytes) -> None:
        buffer = chunk
        maxStderrBytes = self.options.get("maxStderrBytes")
        if maxStderrBytes is None:
            maxStderrBytes = DEFAULT_MAX_STDERR_BYTES
        self._stderrBuffer += buffer
        if len(self._stderrBuffer) > maxStderrBytes:
            self._stderrBuffer = self._stderrBuffer[-maxStderrBytes:]
        onStderr = self.options.get("onStderr")
        if onStderr is not None:
            onStderr(buffer.decode("utf-8", "replace"))


__all__ = ["StdioTransport", "StdioTransportOptions"]
