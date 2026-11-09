"""Serialize file mutations per target path."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class _MutationQueueEntry:
    lock: asyncio.Lock
    users: int = 0


fileMutationQueues: dict[str, _MutationQueueEntry] = {}


def getMutationQueueKey(file_path: str) -> str:
    resolved_path = os.path.abspath(file_path)
    try:
        return os.path.realpath(resolved_path)
    except OSError:
        return resolved_path


async def withFileMutationQueue[T](file_path: str, fn: Callable[[], Awaitable[T]]) -> T:
    key = getMutationQueueKey(file_path)
    entry = fileMutationQueues.get(key)
    if entry is None:
        entry = _MutationQueueEntry(lock=asyncio.Lock())
        fileMutationQueues[key] = entry
    entry.users += 1

    try:
        async with entry.lock:
            return await fn()
    finally:
        entry.users -= 1
        if entry.users == 0 and not entry.lock.locked():
            fileMutationQueues.pop(key, None)



__all__ = ["withFileMutationQueue"]
