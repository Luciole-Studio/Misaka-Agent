"""What every built-in tool needs to read its arguments and honour an abort.

The seven tool modules were ported one file at a time, so each arrived with its own copy
of the same handful of ideas. The copies had drifted: the abort predicate went by two
names, and one of the three abort-task spellings passed a Future to
``asyncio.create_task``, which raises. Written once here, they cannot drift again.

The helpers keep the leading underscore they were ported with. The module is already
package-private, and the alternative was renaming a hundred and thirty call sites in a
change whose whole point is that nothing about them should differ.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any


def abort_wait_task(signal: Any | None) -> asyncio.Task[None] | None:
    """A task that finishes when the caller aborts, or None if this signal cannot say.

    ``misaka.agent.agent.AbortSignal`` is the only signal in the project and it wraps an
    ``asyncio.Event``, so awaiting it is free. A signal that cannot be awaited gets None
    and the caller runs unraced, rather than polling a flag at 100Hz for the length of
    the tool call -- which is what the ported fallback did.
    """
    if signal is None:
        return None
    wait = getattr(signal, "wait", None)
    if not callable(wait):
        return None
    pending = wait()
    if not isinstance(pending, Awaitable):
        return None
    return asyncio.ensure_future(pending)


@asynccontextmanager
async def abort_race(signal: Any | None) -> AsyncIterator[asyncio.Task[None] | None]:
    """Hold an abort task for the body, and always retire it on the way out.

    Every tool wrote the same teardown by hand in a ``finally``; the one that forgot
    would leak a task waiting on an Event for the life of the process.
    """
    task = abort_wait_task(signal)
    try:
        yield task
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _string_arg(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return None


def _ignore_background_task_result(task: asyncio.Task[Any]) -> None:
    def _consume(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            return
        try:
            done.result()
        except Exception:  # noqa: BLE001 - the background task's outcome is intentionally discarded
            return

    task.add_done_callback(_consume)


async def _drain_worker(task: asyncio.Task[Any]) -> bool:
    """Wait through repeated caller cancellation; return whether another cancel arrived."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001 - draining must survive every worker outcome
            break
    if task.done():
        try:
            task.result()
        except BaseException:  # noqa: BLE001, S110 - observing the drained outcome is sufficient
            pass
    return cancelled
