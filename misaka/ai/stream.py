"""Shared stream facade for lazily registered AI providers."""

from __future__ import annotations

from misaka.ai.api_registry import get_api_provider
from misaka.ai.env_api_keys import get_env_api_key
from misaka.ai.providers import register_builtins as _register_builtins  # noqa: F401
from misaka.ai.types import (
    AssistantMessage,
    Context,
    Model,
    ProviderStreamOptions,
    SimpleStreamOptions,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream


def _resolve_api_provider(api: str):
    provider = get_api_provider(api)
    if provider is None:
        raise RuntimeError(f"No API provider registered for api: {api}")
    return provider


def stream(model: Model, context: Context, options: ProviderStreamOptions | None = None) -> AssistantMessageEventStream:
    provider = _resolve_api_provider(model.api)
    resolved_context = context if isinstance(context, Context) else Context.model_validate(context)
    return provider.stream(model, resolved_context, options)


async def complete(model: Model, context: Context, options: ProviderStreamOptions | None = None) -> AssistantMessage:
    return await stream(model, context, options).result()


def stream_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessageEventStream:
    provider = _resolve_api_provider(model.api)
    resolved_context = context if isinstance(context, Context) else Context.model_validate(context)
    return provider.streamSimple(model, resolved_context, options)


async def complete_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessage:
    return await stream_simple(model, context, options).result()


streamSimple = stream_simple
getEnvApiKey = get_env_api_key

__all__ = [
    "complete",
    "getEnvApiKey",
    "stream",
    "streamSimple",
    ]
