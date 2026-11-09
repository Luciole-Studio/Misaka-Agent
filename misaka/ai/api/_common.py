"""The helpers every provider needs, written once.

Nine provider modules were ported from separate TypeScript files, so each arrived with
its own copy of "await this if it is awaitable", "has the caller aborted", "read this
option off a Mapping or an object". The copies had drifted: three spellings of the
option reader, two of the stream closer, and one that built a whole AssistantMessage
just to read a zero Usage back off it. Where they differed, the version kept here is
the one that also handles what the others missed.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from typing import Any

from misaka.ai.types import Usage, UsageCost
from misaka.utils._abort_signal import wait_for_abort
from misaka.utils.values import maybe_await, signal_aborted


def _create_abort_wait_task(signal: Any) -> asyncio.Task[None] | None:
    """A task that finishes when ``signal`` aborts. A flag-only signal (the TUI's) is polled, so an abort still
    interrupts the wait."""
    if signal is None:
        return None
    return asyncio.create_task(wait_for_abort(signal))


async def _await_with_signal(
    awaitable: Any, signal: Any, *, on_abort: Any = None, abort_task: asyncio.Task[None] | None = None
) -> Any:
    """Await ``awaitable``, giving up the moment ``signal`` aborts.

    ``abort_task`` lets a caller that awaits in a loop -- every streaming adapter, once per
    SSE item -- build the ``signal.wait()`` task *once* for the whole stream and hand it in.
    Creating and cancelling one per item cost ~56us of pure task churn on the loop thread
    per line of a response, multiplied by every concurrent stream. A borrowed task is the
    caller's to cancel; only the one made here is cancelled here.
    """
    if signal_aborted(signal):
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
        else:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
        if on_abort is not None:
            await maybe_await(on_abort())
        raise RuntimeError("Request was aborted")

    task = asyncio.ensure_future(awaitable)
    borrowed_abort_task = abort_task is not None
    if abort_task is None:
        abort_task = _create_abort_wait_task(signal)
    try:
        if abort_task is not None:
            done, _ = await asyncio.wait({task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if abort_task in done and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if on_abort is not None:
                    await maybe_await(on_abort())
                raise RuntimeError("Request was aborted")
        return await task
    finally:
        if abort_task is not None and not borrowed_abort_task:
            abort_task.cancel()
            await asyncio.gather(abort_task, return_exceptions=True)


async def _await_maybe_with_signal(value: Any, signal: Any, *, on_abort: Any = None) -> Any:
    if hasattr(value, "__await__"):
        return await _await_with_signal(value, signal, on_abort=on_abort)
    if signal_aborted(signal):
        if on_abort is not None:
            await maybe_await(on_abort())
        raise RuntimeError("Request was aborted")
    return value


async def _close_stream(stream_obj: Any) -> None:
    if stream_obj is None:
        return
    for close_name in ("aclose", "close"):
        close = getattr(stream_obj, close_name, None)
        if callable(close):
            try:
                await maybe_await(close())
            except Exception:  # noqa: BLE001
                return
            return


async def _iterate_async_iterable(iterable: Any, signal: Any = None, *, on_abort: Any = None):
    iterator = iterable.__aiter__()
    # One abort task for the whole stream, not one per item: see _await_with_signal.
    abort_task = _create_abort_wait_task(signal)
    try:
        while True:
            try:
                item = await _await_with_signal(
                    iterator.__anext__(), signal, on_abort=on_abort, abort_task=abort_task
                )
            except StopAsyncIteration:
                return
            yield item
    finally:
        # cancel() with nothing awaited after it: a cancelled ``signal.wait()`` has no
        # result or error to collect, and this finally also runs when the generator is
        # closed or finalized, where awaiting is the shape that bites.
        if abort_task is not None:
            abort_task.cancel()


def split_sdk_params(create: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    """Keyword arguments for a vendor SDK's ``create()`` (OpenAI, Anthropic) from a pi-shaped request body.

    MISAKA fork: pi builds the body with every field at the top level and the JS SDK sends
    whatever it is given. The Python SDK's ``create()`` accepts only the fields it declares,
    so the rest travel in ``extra_body`` (merged into the body after the named fields), and
    a ``None`` -- pi's ``undefined`` -- is not sent at all.
    """
    try:
        known = set(inspect.signature(create).parameters)
    except (TypeError, ValueError):
        known = set()
    named: dict[str, Any] = {}
    extra: dict[str, Any] = dict(params.get("extra_body") or {})
    for key, value in params.items():
        if key == "extra_body" or value is None:
            continue
        if key in known:
            named[key] = value
        else:
            extra[key] = value
    if extra:
        named["extra_body"] = extra
    return named


_CANONICAL_AUTH_HEADERS = {"authorization": "Authorization", "x-api-key": "X-Api-Key"}


def split_sdk_headers(headers: Mapping[str, Any], omit: Any) -> tuple[dict[str, str], dict[str, Any]]:
    """The client's default headers, and the headers every request has to carry.

    MISAKA fork: pi hands the SDK ``null`` for a header it must not send. The Python SDKs
    take ``omit`` for that, and honour it only per request: one in ``default_headers`` fails
    their request-time auth check. Names are matched case-insensitively, the way a JS
    ``Headers`` merge collapses ``Authorization`` and ``authorization`` into one.
    """
    merged: dict[str, Any] = {}
    for name, value in headers.items():
        for existing in [key for key in merged if key.lower() == str(name).lower()]:
            del merged[existing]
        merged[str(name)] = value
    default_headers = {name: value for name, value in merged.items() if value is not None}
    request_headers = {
        _CANONICAL_AUTH_HEADERS.get(name.lower(), name): omit for name, value in merged.items() if value is None
    }
    return default_headers, request_headers


def _empty_usage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def _compat_value(model: Any, name: str) -> Any:
    """``model.compat?.[name]``: the compat shapes are models, and an absent field reads as ``None``."""
    return getattr(getattr(model, "compat", None), name, None)


def _option(options: Any, name: str, default: Any = None) -> Any:
    if options is None:
        return default
    if isinstance(options, Mapping):
        value = options.get(name, default)
    else:
        value = getattr(options, name, default)
    return default if value is None else value


