"""Shared subprocess execution helpers for extensions and session services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, TypedDict

_EXIT_STDIO_GRACE_SECONDS = 0.1
_FORCE_KILL_DELAY_SECONDS = 5.0


class ExecOptions(TypedDict, total=False):
    signal: Any
    timeout: int | float
    cwd: str


@dataclass(slots=True)
class ExecResult:
    stdout: str
    stderr: str
    code: int
    killed: bool


def _is_aborted(signal: Any | None) -> bool:
    return bool(getattr(signal, "aborted", False))


async def _wait_for_abort(signal: Any) -> None:
    wait = getattr(signal, "wait", None)
    if callable(wait):
        result = wait()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            await result
            return
    while not _is_aborted(signal):
        await asyncio.sleep(0.01)


async def _read_stream(stream: asyncio.StreamReader | None, chunks: list[bytes]) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        chunks.append(chunk)


def _resolve_timeout_seconds(options: ExecOptions) -> float | None:
    timeout = options.get("timeout")
    if timeout is None:
        return None
    return float(timeout) / 1000


def _destroy_stream(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    transport = getattr(stream, "_transport", None)
    if transport is None:
        return
    abort = getattr(transport, "abort", None)
    if callable(abort):
        abort()
        return
    close = getattr(transport, "close", None)
    if callable(close):
        close()


async def _wait_for_streams(
    stdout_task: asyncio.Task[None],
    stderr_task: asyncio.Task[None],
    stdout: asyncio.StreamReader | None,
    stderr: asyncio.StreamReader | None,
) -> None:
    try:
        await asyncio.wait_for(
            asyncio.gather(stdout_task, stderr_task),
            timeout=_EXIT_STDIO_GRACE_SECONDS,
        )
    except asyncio.TimeoutError:
        _destroy_stream(stdout)
        _destroy_stream(stderr)
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


async def _force_kill_after_delay(process: asyncio.subprocess.Process) -> None:
    await asyncio.sleep(_FORCE_KILL_DELAY_SECONDS)
    if process.returncode is None:
        process.kill()


def _normalize_exit_code(code: int | None, *, killed: bool, failed: bool) -> int:
    if failed:
        return 1
    if code is None:
        return 0
    if killed and code < 0:
        return 0
    return code


async def exec_command(
    command: str,
    args: list[str],
    cwd: str,
    options: ExecOptions | None = None,
) -> ExecResult:
    resolved_options: ExecOptions = dict(options or {})
    timeout = _resolve_timeout_seconds(resolved_options)
    signal = resolved_options.get("signal")

    try:
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return ExecResult(stdout="", stderr="", code=1, killed=False)

    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_task = asyncio.create_task(_read_stream(process.stdout, stdout_chunks))
    stderr_task = asyncio.create_task(_read_stream(process.stderr, stderr_chunks))
    wait_task = asyncio.create_task(process.wait())
    abort_task = asyncio.create_task(_wait_for_abort(signal)) if signal is not None else None
    force_kill_task: asyncio.Task[None] | None = None
    killed = False
    wait_failed = False

    def kill_process() -> None:
        nonlocal force_kill_task, killed
        if killed:
            return
        killed = True
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        force_kill_task = asyncio.create_task(_force_kill_after_delay(process))

    try:
        if _is_aborted(signal):
            kill_process()

        pending = [wait_task]
        if abort_task is not None:
            pending.append(abort_task)
        done, _ = await asyncio.wait(
            pending,
            timeout=timeout if timeout and timeout > 0 else None,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if wait_task not in done:
            kill_process()

        try:
            await wait_task
        except Exception:
            wait_failed = True

        await _wait_for_streams(stdout_task, stderr_task, process.stdout, process.stderr)
        return ExecResult(
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            code=_normalize_exit_code(process.returncode, killed=killed, failed=wait_failed),
            killed=killed,
        )
    finally:
        if abort_task is not None:
            abort_task.cancel()
            await asyncio.gather(abort_task, return_exceptions=True)
        if force_kill_task is not None:
            force_kill_task.cancel()
            await asyncio.gather(force_kill_task, return_exceptions=True)
        if not stdout_task.done():
            stdout_task.cancel()
        if not stderr_task.done():
            stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


execCommand = exec_command

__all__ = [
    "ExecOptions",
    "ExecResult",
    "execCommand",
]
