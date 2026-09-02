"""Shared subprocess execution helpers for extensions and session services."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.utils.child_process import ChildProcess, spawn_child_process
from misaka.utils.values import signal_aborted

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


async def _wait_for_abort(signal: Any) -> None:
    wait = getattr(signal, "wait", None)
    if callable(wait):
        result = wait()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            await result
            return
    while not signal_aborted(signal):
        await asyncio.sleep(0.01)


async def _drain_task(task: asyncio.Task[Any]) -> bool:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001 - cleanup must outlive its caller
            break
    if task.done():
        try:
            task.result()
        except BaseException:  # noqa: BLE001, S110 - observing cleanup is sufficient
            pass
    return cancelled


def _resolve_timeout_seconds(options: ExecOptions) -> float | None:
    timeout = options.get("timeout")
    if timeout is None:
        return None
    return float(timeout) / 1000


async def _force_kill_after_delay(process: ChildProcess) -> None:
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
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        def collect_output(fd: int, data: bytes) -> None:
            (stdout_chunks if fd == 1 else stderr_chunks).append(data)

        process = await spawn_child_process(
            command,
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            on_data=collect_output,
        )
    except OSError:
        return ExecResult(stdout="", stderr="", code=1, killed=False)

    wait_task = asyncio.create_task(process.wait())
    abort_task = asyncio.create_task(_wait_for_abort(signal)) if signal is not None else None
    timeout_task = (
        asyncio.create_task(asyncio.sleep(timeout))
        if timeout is not None and timeout > 0
        else None
    )
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
        if signal_aborted(signal):
            kill_process()

        pending = [wait_task]
        if abort_task is not None:
            pending.append(abort_task)
        if timeout_task is not None:
            pending.append(timeout_task)
        done, _ = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )

        abort_won = abort_task is not None and abort_task in done
        timeout_won = timeout_task is not None and timeout_task in done
        if abort_won or timeout_won or signal_aborted(signal):
            kill_process()

        try:
            await wait_task
        except Exception:  # noqa: BLE001 - the process is being torn down; a failed wait is recorded as wait_failed
            wait_failed = True

        return ExecResult(
            stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
            stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
            code=_normalize_exit_code(process.returncode, killed=killed, failed=wait_failed),
            killed=killed,
        )
    except asyncio.CancelledError:
        try:
            kill_process()
        finally:
            process.close()
        raise
    finally:
        async def cleanup() -> None:
            tasks: list[asyncio.Task[Any]] = [wait_task]
            for task in (abort_task, timeout_task, force_kill_task):
                if task is None:
                    continue
                if not task.done():
                    task.cancel()
                tasks.append(task)
            await asyncio.gather(*tasks, return_exceptions=True)

        if await _drain_task(asyncio.create_task(cleanup())):
            raise asyncio.CancelledError


__all__ = [
    "ExecOptions",
    "ExecResult",
    ]
