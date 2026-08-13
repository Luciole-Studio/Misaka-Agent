"""Abort-aware sleep helper."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


async def sleep(ms: int | float, signal: Any = None) -> None:
    if _is_aborted(signal):
        raise RuntimeError("Aborted")

    sleep_task = asyncio.create_task(asyncio.sleep(ms / 1000))
    abort_task, cleanup = _create_abort_task(signal)
    try:
        if abort_task is None:
            await sleep_task
            return

        done, _pending = await asyncio.wait({sleep_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
        if abort_task in done:
            raise RuntimeError("Aborted")
        await sleep_task
    finally:
        cleanup()
        if abort_task is not None and not abort_task.done():
            abort_task.cancel()
        if not sleep_task.done():
            sleep_task.cancel()


def _is_aborted(signal: Any) -> bool:
    return bool(getattr(signal, "aborted", False) or (hasattr(signal, "is_set") and signal.is_set()))


def _create_abort_task(signal: Any) -> tuple[asyncio.Task[None] | None, Callable[[], None]]:
    if signal is None:
        return None, lambda: None

    wait_method = getattr(signal, "wait", None)
    if callable(wait_method):
        wait_result = wait_method()
        if isinstance(wait_result, Awaitable):
            return asyncio.create_task(wait_result), lambda: None

    add_listener = getattr(signal, "addEventListener", None)
    remove_listener = getattr(signal, "removeEventListener", None)
    if callable(add_listener):
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def _on_abort(*_args: Any, **_kwargs: Any) -> None:
            if not future.done():
                future.set_result(None)

        add_listener("abort", _on_abort, {"once": True})

        def _cleanup() -> None:
            if callable(remove_listener):
                remove_listener("abort", _on_abort)

        return asyncio.create_task(future), _cleanup

    return None, lambda: None


__all__ = ["sleep"]
