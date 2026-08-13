"""Subprocess helpers shared across coding-agent modules."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

StdioValue = Literal["ignore", "inherit", "pipe"]


@dataclass(slots=True)
class SpawnProcessSyncResult:
    status: int | None
    stdout: str
    stderr: str
    error: OSError | None = None


SpawnProcess = subprocess.Popen[str]


def _resolve_stdio(value: StdioValue) -> int | None:
    if value == "ignore":
        return subprocess.DEVNULL
    if value == "pipe":
        return subprocess.PIPE
    return None


def spawn_process(
    command: str,
    args: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    encoding: str | None = "utf-8",
    stdio: tuple[StdioValue, StdioValue, StdioValue] = ("ignore", "pipe", "pipe"),
) -> SpawnProcess:
    return subprocess.Popen(
        [command, *args],
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdin=_resolve_stdio(stdio[0]),
        stdout=_resolve_stdio(stdio[1]),
        stderr=_resolve_stdio(stdio[2]),
        text=encoding is not None,
        encoding=encoding,
    )


def spawn_process_sync(
    command: str,
    args: Sequence[str],
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    encoding: str | None = "utf-8",
    stdio: tuple[StdioValue, StdioValue, StdioValue] = ("ignore", "pipe", "pipe"),
) -> SpawnProcessSyncResult:
    try:
        completed = subprocess.run(
            [command, *args],
            check=False,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdin=_resolve_stdio(stdio[0]),
            stdout=_resolve_stdio(stdio[1]),
            stderr=_resolve_stdio(stdio[2]),
            text=encoding is not None,
            encoding=encoding,
        )
    except OSError as error:
        return SpawnProcessSyncResult(status=None, stdout="", stderr="", error=error)

    return SpawnProcessSyncResult(
        status=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


async def wait_for_child_process(child: subprocess.Popen[str] | subprocess.Popen[bytes]) -> int | None:
    try:
        return await asyncio.to_thread(child.wait)
    finally:
        for stream in (child.stdout, child.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                continue


spawnProcess = spawn_process
spawnProcessSync = spawn_process_sync
waitForChildProcess = wait_for_child_process

__all__ = [
    "spawnProcess",
    "spawnProcessSync",
    "waitForChildProcess",
]
