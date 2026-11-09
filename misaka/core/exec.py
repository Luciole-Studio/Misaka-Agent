"""Translated from pi's ``packages/coding-agent/src/core/exec.ts`` (v1.1.0).

Shared command execution utilities for extensions and custom tools.

MISAKA fork: ``killProcess`` tears down the process's whole tree (``core.platform.processes.terminate``, which also
works on Windows), where pi sends SIGTERM and then SIGKILL after five seconds to the process alone.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.core.platform.processes import terminate
from misaka.utils.async_lifecycle import settle
from misaka.utils.child_process import spawn_child_process, waitForChildProcess
from misaka.utils.values import signal_aborted


class ExecOptions(TypedDict, total=False):
    """Options for executing shell commands."""

    # AbortSignal to cancel the command
    signal: Any
    # Timeout in milliseconds
    timeout: int | float
    # Working directory
    cwd: str


@dataclass(slots=True)
class ExecResult:
    """Result of executing a shell command."""

    stdout: str
    stderr: str
    code: int
    killed: bool


async def execCommand(
    command: str,
    args: list[str],
    cwd: str,
    options: ExecOptions | None = None,
) -> ExecResult:
    """Execute a shell command and return stdout/stderr/code.
    Supports timeout and abort signal.
    """
    options = options or {}
    stdout: list[bytes] = []
    stderr: list[bytes] = []
    killed = False

    try:
        proc = await spawn_child_process(
            command,
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            on_data=lambda fd, data: (stdout if fd == 1 else stderr).append(data),
        )
    except OSError:
        return ExecResult(stdout="", stderr="", code=1, killed=False)

    terminating: asyncio.Task[None] | None = None

    def killProcess() -> None:
        nonlocal killed, terminating
        if not killed:
            killed = True

            async def terminateTree() -> None:
                try:
                    if proc.pid is not None:
                        await asyncio.to_thread(terminate, proc.pid)
                finally:
                    proc.close()

            terminating = asyncio.create_task(terminateTree())

    # Wait for process termination without hanging on inherited stdio handles
    # held open by detached descendants.
    waitTask = asyncio.create_task(waitForChildProcess(proc))
    timeout = options.get("timeout")
    # Handle timeout
    timeoutId = asyncio.create_task(asyncio.sleep(timeout / 1000)) if timeout and timeout > 0 else None
    try:
        # Handle abort signal
        signal = options.get("signal")
        if signal_aborted(signal):
            killProcess()
        abortTask = asyncio.ensure_future(signal.wait()) if signal is not None and callable(getattr(signal, "wait", None)) else None
        try:
            pending: set[asyncio.Future[Any]] = {waitTask}
            if abortTask is not None:
                pending.add(abortTask)
            if timeoutId is not None:
                pending.add(timeoutId)
            done, _pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if waitTask not in done:
                killProcess()
        finally:
            if abortTask is not None:
                abortTask.cancel()
                await asyncio.gather(abortTask, return_exceptions=True)
        try:
            code = await waitTask
        except Exception:  # noqa: BLE001 - a failed wait resolves with code 1, as upstream's catch does
            return ExecResult(stdout=_text(stdout), stderr=_text(stderr), code=1, killed=killed)
        # A signal death has no exit code in Node (`code ?? 0`); asyncio reports it as the negative signal number.
        return ExecResult(
            stdout=_text(stdout),
            stderr=_text(stderr),
            code=0 if code is None or code < 0 else code,
            killed=killed,
        )
    except asyncio.CancelledError:
        killProcess()
        raise
    finally:
        async def cleanup() -> None:
            # Tree teardown owns its thread until completion, even after caller cancellation.
            if terminating is not None:
                await terminating
            if timeoutId is not None:
                timeoutId.cancel()
            await asyncio.gather(waitTask, *([timeoutId] if timeoutId else []), return_exceptions=True)

        _, cancelled = await settle(asyncio.ensure_future(cleanup()))
        if cancelled is not None:
            raise cancelled


def _text(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", errors="replace")


__all__ = ["ExecOptions", "ExecResult", "execCommand"]
