"""Subprocess helpers shared across coding-agent modules."""

from __future__ import annotations

import asyncio
import subprocess


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
