"""Serialization of ``edit`` and ``write`` mutations of one file.

Translated from pi's ``packages/durable/src/tools/file-mutation-queue.ts``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from misaka.chord.context import Context
from misaka.utils.js import startTask

from ..env import getOrThrow

_queues: dict[str, asyncio.Future[None]] = {}
"""Tail of the mutation chain of each file, keyed by file system id and canonical path."""


async def mutationKey(env: Any, path: str, context: Context) -> str:
    absolutePath = getOrThrow(await env.absolutePath(path, context))
    return f"{env.id}\0{await canonical(env, absolutePath, context)}"


async def canonical(env: Any, absolutePath: str, context: Context) -> str:
    """The canonical path; for a file that does not exist yet, its canonical parent joined with its name, so a ``write``
    that creates a file and a later mutation of it share one key even under a symlinked directory."""
    result = await env.canonicalPath(absolutePath, context)
    if result["ok"]:
        return result["value"]  # type: ignore[no-any-return]
    if result["error"].code == "not_supported":
        return absolutePath
    if result["error"].code != "not_found":
        raise result["error"]
    # The file system splits the path, so a name may contain characters that are separators elsewhere.
    parent = getOrThrow(await env.joinPath([absolutePath, ".."], context))
    if parent == absolutePath or not absolutePath.startswith(parent):
        return absolutePath
    name = absolutePath[len(parent) + (0 if re.search(r"[/\\]$", parent) else 1) :]
    return getOrThrow(await env.joinPath([await canonical(env, parent, context), name], context))  # type: ignore[no-any-return]


async def withFileMutationQueue[T](env: Any, path: str, fn: Callable[[], Awaitable[T]], context: Context) -> T:
    """Serialize ``edit`` and ``write`` mutations of one file within this process: same file system id and canonical path,
    whichever environment object the call got. Other files, and other file systems, never wait. Concurrent calls on one
    file run in the order their keys resolve. Not a lock against ``bash`` or other processes."""
    key = await mutationKey(env, path, context)
    # Take the slot without awaiting, so no other call can take it in between.
    previous = _queues.get(key)
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def chain() -> None:
        if previous is not None:
            await asyncio.shield(previous)
        await done

    tail = startTask(chain())
    _queues[key] = tail
    try:
        if previous is not None:
            # The shield keeps a cancelled caller from cancelling the tail the next caller waits on.
            await asyncio.shield(previous)
        return await fn()
    finally:
        if not done.done():
            done.set_result(None)
        if _queues.get(key) is tail:
            del _queues[key]


__all__ = ["withFileMutationQueue"]
