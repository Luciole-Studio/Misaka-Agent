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
import json
from collections.abc import Mapping
from typing import Any

from misaka.ai.types import CacheRetention, Model, Usage, UsageCost
from misaka.ai.utils.provider_env import get_provider_env_value
from misaka.utils.values import maybe_await, signal_aborted


def _create_abort_wait_task(signal: Any) -> asyncio.Task[None] | None:
    if signal is None or not hasattr(signal, "wait"):
        return None
    return asyncio.create_task(signal.wait())


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


def _empty_usage() -> Usage:
    return Usage(
        input=0,
        output=0,
        cacheRead=0,
        cacheWrite=0,
        totalTokens=0,
        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0),
    )


def _option(options: Any, name: str, default: Any = None) -> Any:
    if options is None:
        return default
    if isinstance(options, Mapping):
        value = options.get(name, default)
    else:
        value = getattr(options, name, default)
    return default if value is None else value


def _prepare_sdk_params(params: Mapping[str, Any]) -> dict[str, Any]:
    sdk_params = dict(params)
    config = sdk_params.get("config")
    if isinstance(config, Mapping):
        sdk_params["config"] = {
            key: value for key, value in config.items() if key != "abortSignal" and value is not None
        }
    return sdk_params


def resolve_cache_retention(
    cache_retention: CacheRetention | None = None, env: Any = None
) -> CacheRetention:
    """The explicit retention, else the request-scoped ``env``, else the process env.

    ``env`` is the caller's ``options.env`` (auth resolution env merged with the request's
    own), which is where a per-provider ``MISAKA_CACHE_RETENTION`` override lives.
    """
    if cache_retention:
        return cache_retention
    return "long" if get_provider_env_value("MISAKA_CACHE_RETENTION", env) == "long" else "short"


def safe_json_stringify(value: Any) -> str:
    # pi's ``safeJsonStringify`` has to handle ``JSON.stringify(undefined) === undefined``;
    # ``json.dumps`` never returns None, it raises, so only the except branch is needed.
    try:
        return json.dumps(value)
    except Exception:  # noqa: BLE001 - any unserialisable value falls back to repr
        return str(value)


def get_service_tier_cost_multiplier(model: Model, service_tier: str | None) -> float:
    if service_tier == "flex":
        return 0.5
    if service_tier == "priority":
        return 2.5 if model.id == "gpt-5.5" else 2.0
    return 1.0


def apply_service_tier_pricing(usage: Usage, service_tier: str | None, model: Model) -> None:
    multiplier = get_service_tier_cost_multiplier(model, service_tier)
    if multiplier == 1:
        return
    usage.cost.input *= multiplier
    usage.cost.output *= multiplier
    usage.cost.cacheRead *= multiplier
    usage.cost.cacheWrite *= multiplier
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite
