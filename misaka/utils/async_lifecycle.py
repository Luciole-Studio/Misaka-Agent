"""Drain owned work before propagating cancellation; cancelling a waiter is not stopping I/O."""
import asyncio
import contextvars
from functools import partial


async def settle(future):
    """Wait through caller cancellation; return the result and first cancellation."""
    cancelled = None
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError as error:
            if cancelled is None:
                cancelled = error
    return future.result(), cancelled


async def settle_thread_call(function, /, *args, **kwargs):
    """Finish a stateful thread call, letting its owner handle deferred cancellation."""
    # Unlike a wrapper Task, this executor Future is not cancelled by loop
    # shutdown. Preserve async-local context just as asyncio.to_thread does.
    call = partial(contextvars.copy_context().run, function, *args, **kwargs)
    return await settle(asyncio.get_running_loop().run_in_executor(None, call))


async def run_in_thread(function, /, *args, **kwargs):
    """Offload work but retain ownership until it ends, then propagate cancellation.

    This drains, not interrupts, the worker. Its own operation timeouts still apply.
    """
    result, cancelled = await settle_thread_call(function, *args, **kwargs)
    if cancelled is not None:
        raise cancelled
    return result
