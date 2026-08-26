"""Subprocess helpers shared across coding-agent modules."""

from __future__ import annotations

import asyncio
import subprocess
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


__all__ = [
    ]
