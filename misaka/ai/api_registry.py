"""Provider registry for stream-capable AI APIs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from misaka.ai.types import (
    Api,
    Model,
    SimpleStreamOptions,
    StreamOptions,
    TranscriptContext,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream

ApiStreamFunction = Callable[[Model, TranscriptContext, StreamOptions | None], AssistantMessageEventStream]
ApiStreamSimpleFunction = Callable[[Model, TranscriptContext, SimpleStreamOptions | None], AssistantMessageEventStream]


@dataclass(slots=True)
class ApiProvider:
    api: Api
    stream: ApiStreamFunction
    streamSimple: ApiStreamSimpleFunction


@dataclass(slots=True)
class RegisteredApiProvider:
    provider: ApiProvider
    source_id: str | None = None


_api_provider_registry: dict[str, RegisteredApiProvider] = {}


def _wrap_stream(api: Api, stream: ApiStreamFunction) -> ApiStreamFunction:
    def wrapped(model: Model, context: TranscriptContext, options: StreamOptions | None = None) -> AssistantMessageEventStream:
        if model.api != api:
            raise ValueError(f"Mismatched api: {model.api} expected {api}")
        return stream(model, context, options)

    return wrapped


def _wrap_stream_simple(api: Api, stream_simple: ApiStreamSimpleFunction) -> ApiStreamSimpleFunction:
    def wrapped(
        model: Model,
        context: TranscriptContext,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        if model.api != api:
            raise ValueError(f"Mismatched api: {model.api} expected {api}")
        return stream_simple(model, context, options)

    return wrapped


def register_api_provider(provider: ApiProvider, source_id: str | None = None) -> None:
    _api_provider_registry[provider.api] = RegisteredApiProvider(
        provider=ApiProvider(
            api=provider.api,
            stream=_wrap_stream(provider.api, provider.stream),
            streamSimple=_wrap_stream_simple(provider.api, provider.streamSimple),
        ),
        source_id=source_id,
    )


def get_api_provider(api: Api) -> ApiProvider | None:
    entry = _api_provider_registry.get(api)
    return entry.provider if entry else None


def get_api_providers() -> list[ApiProvider]:
    """Every registered api provider, in registration order."""
    return [entry.provider for entry in _api_provider_registry.values()]


def unregister_api_providers(source_id: str) -> None:
    """Remove only what ``source_id`` registered.

    ``register_api_provider`` has always recorded the source; nothing read it, so an
    extension that registered an api had no way to take just its own back out -- the only
    lever was ``clear_api_providers``, which takes everyone's.
    """
    for api in [api for api, entry in _api_provider_registry.items() if entry.source_id == source_id]:
        del _api_provider_registry[api]


def clear_api_providers() -> None:
    _api_provider_registry.clear()


