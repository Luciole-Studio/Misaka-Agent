"""Provider registry for stream-capable AI APIs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from misaka.ai.types import Api, Context, Model, SimpleStreamOptions, StreamOptions
from misaka.ai.utils.event_stream import AssistantMessageEventStream

ApiStreamFunction = Callable[[Model, Context, StreamOptions | None], AssistantMessageEventStream]
ApiStreamSimpleFunction = Callable[[Model, Context, SimpleStreamOptions | None], AssistantMessageEventStream]


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
    def wrapped(model: Model, context: Context, options: StreamOptions | None = None) -> AssistantMessageEventStream:
        if model.api != api:
            raise ValueError(f"Mismatched api: {model.api} expected {api}")
        return stream(model, context, options)

    return wrapped


def _wrap_stream_simple(api: Api, stream_simple: ApiStreamSimpleFunction) -> ApiStreamSimpleFunction:
    def wrapped(
        model: Model,
        context: Context,
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


def clear_api_providers() -> None:
    _api_provider_registry.clear()


