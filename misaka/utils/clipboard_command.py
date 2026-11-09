"""Translated from pi's ``coding-agent/src/utils/clipboard-command.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import TypedDict


class ClipboardCommandOptions(TypedDict, total=False):
    input: str
    timeoutMs: float
    maxBufferBytes: int


async def runClipboardCommand(
    command: str, args: list[str], options: ClipboardCommandOptions | None = None
) -> bytes | None:
    """None means the command failed; empty bytes is a successful result."""
    options = options or {}
    return await asyncio.to_thread(
        _run,
        command,
        list(args),
        options.get("input"),
        options.get("timeoutMs", 3000) / 1000,
        options.get("maxBufferBytes", 50 * 1024 * 1024),
    )


def _run(command: str, args: list[str], input: str | None, timeout: float, maxBufferBytes: int) -> bytes | None:
    # MISAKA fork: a thread runs the child to completion, as the event loop of a Node process would.
    try:
        # Clipboard writers can daemonize. Do not give them output pipes to retain.
        child = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if input is None else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError:
        return None
    try:
        # A writer may exit before consuming all input.
        stdout, _stderr = child.communicate(None if input is None else input.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        child.kill()
        child.communicate()
        return None
    except OSError:
        child.kill()
        child.wait()
        return None
    if child.returncode != 0:
        return None
    output = stdout or b""
    return None if len(output) > maxBufferBytes else output


__all__ = ["ClipboardCommandOptions", "runClipboardCommand"]
