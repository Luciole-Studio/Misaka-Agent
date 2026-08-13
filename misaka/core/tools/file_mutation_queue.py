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


_file_mutation_queues: dict[str, _MutationQueueEntry] = {}


def _get_mutation_queue_key(file_path: str) -> str:
    resolved_path = os.path.abspath(file_path)
    try:
        return os.path.realpath(resolved_path)
    except OSError:
        return resolved_path


async def with_file_mutation_queue(file_path: str, fn: Callable[[], Awaitable[T]]) -> T:
    key = _get_mutation_queue_key(file_path)
    entry = _file_mutation_queues.get(key)
    if entry is None:
        entry = _MutationQueueEntry(lock=asyncio.Lock())
        _file_mutation_queues[key] = entry
    entry.users += 1

    try:
        async with entry.lock:
            return await fn()
    finally:
        entry.users -= 1
        if entry.users == 0 and not entry.lock.locked():
            _file_mutation_queues.pop(key, None)


withFileMutationQueue = with_file_mutation_queue

__all__ = ["withFileMutationQueue"]
