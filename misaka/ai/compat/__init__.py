"""Translated from pi's ``packages/ai/src/compat.ts`` (v1.1.0).

Temporary compatibility entrypoint preserving the old global pi-ai API
surface: api-dispatch `stream()`/`complete()` with env API key injection,
the api-registry, generated catalog reads (`getModel`/`getModels`/
`getProviders`), per-API lazy stream wrappers, and image generation.

Existing apps switch imports from "@earendil-works/pi-ai" to
"@earendil-works/pi-ai/compat" unchanged; new code uses `createModels()`
and the provider factories. This module is deleted with the coding-agent
ModelManager migration.

MISAKA fork (each is marked where it happens): the registry wrappers meter each request against the token cap unless
the stream meters itself (``api/_meter.py``); the ``Models`` collection behind the builtin dispatch is built on first
use; options are copied whichever shape they arrive in; the handles are ``SimpleNamespace``s.
"""

from __future__ import annotations

import copy
import random
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from types import SimpleNamespace
from typing import Any

from misaka.ai.api._meter import isMetered, metered
from misaka.ai.api.anthropic_messages_lazy import *
from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.api.azure_openai_responses_lazy import *
from misaka.ai.api.azure_openai_responses_lazy import azureOpenAIResponsesApi
from misaka.ai.api.bedrock_converse_stream_lazy import *
from misaka.ai.api.bedrock_converse_stream_lazy import bedrockConverseStreamApi
from misaka.ai.api.google_generative_ai_lazy import *
from misaka.ai.api.google_generative_ai_lazy import googleGenerativeAIApi
from misaka.ai.api.google_vertex_lazy import *
from misaka.ai.api.google_vertex_lazy import googleVertexApi
from misaka.ai.api.mistral_conversations_lazy import *
from misaka.ai.api.mistral_conversations_lazy import mistralConversationsApi
from misaka.ai.api.openai_codex_responses_lazy import *
from misaka.ai.api.openai_codex_responses_lazy import openAICodexResponsesApi
from misaka.ai.api.openai_completions_lazy import *
from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.api.openai_responses_lazy import *
from misaka.ai.api.openai_responses_lazy import openAIResponsesApi
from misaka.ai.api.pi_messages_lazy import *
from misaka.ai.api.pi_messages_lazy import piMessagesApi
from misaka.ai.env_api_keys import *
from misaka.ai.env_api_keys import getEnvApiKey
from misaka.ai.image_models import *
from misaka.ai.images import *
from misaka.ai.images_api_registry import *
from misaka.ai.legacy_api_aliases import *
from misaka.ai.providers.all import BuiltinProvider as BuiltinProvider
from misaka.ai.providers.all import (
    getBuiltinModel,
    getBuiltinModels,
    getBuiltinProviders,
)
from misaka.ai.providers.faux import (
    FauxProviderRegistration,
    RegisterFauxProviderOptions,
    createFauxCore,
)
from misaka.ai.providers.images.register_builtins import *
from misaka.ai.types import (
    Api,
    AssistantMessage,
    Context,
    Model,
    ProviderStreamOptions,
    ProviderStreams,
    SimpleStreamOptions,
    StreamFunction,
    StreamOptions,
    TranscriptContext,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import jsLength, jsTrim
from misaka.utils.values import read_field

# `export * from "./index.ts"`: the index is a lazy module (`misaka/ai/__init__.py`); its names are read through it.

# @deprecated Static catalog read. Use `getBuiltinModel` from "@earendil-works/pi-ai/providers/all" or `Models.getModel()`.
getModel = getBuiltinModel

# @deprecated Static catalog read. Use `getBuiltinModels` from "@earendil-works/pi-ai/providers/all" or `Models.getModels()`.
getModels = getBuiltinModels

# @deprecated Static catalog read. Use `getBuiltinProviders` from "@earendil-works/pi-ai/providers/all" or `Models.getProviders()`.
getProviders = getBuiltinProviders

type ApiStreamFunction = Callable[[Model, TranscriptContext, StreamOptions | None], AssistantMessageEventStream]

type ApiStreamSimpleFunction = Callable[[Model, TranscriptContext, SimpleStreamOptions | None], AssistantMessageEventStream]


@dataclass(slots=True)
class ApiProvider:
    api: Api
    stream: StreamFunction
    streamSimple: StreamFunction


# The registry holds `ApiProvider`s whose streams are the wrapped ones.
_ApiProviderInternal = ApiProvider


@dataclass(slots=True)
class _RegisteredApiProvider:
    provider: _ApiProviderInternal
    sourceId: str | None = None


_apiProviderRegistry: dict[str, _RegisteredApiProvider] = {}


def _metered(stream: Any, model: Model, context: TranscriptContext, options: Any) -> AssistantMessageEventStream:
    # MISAKA fork: the request is metered against the token cap here unless the stream meters its own (`lazyApi`'s).
    if isMetered(stream):
        return stream(model, context, options)
    return metered(model, context, options, lambda sent: stream(model, context, sent))


def _wrapStream(api: Api, stream: StreamFunction) -> ApiStreamFunction:
    def wrapped(model: Model, context: TranscriptContext, options: Any = None) -> AssistantMessageEventStream:
        if model.api != api:
            raise RuntimeError(f"Mismatched api: {model.api} expected {api}")
        return _metered(stream, model, context, options)

    return wrapped


def _wrapStreamSimple(api: Api, streamSimple: StreamFunction) -> ApiStreamSimpleFunction:
    def wrapped(model: Model, context: TranscriptContext, options: Any = None) -> AssistantMessageEventStream:
        if model.api != api:
            raise RuntimeError(f"Mismatched api: {model.api} expected {api}")
        return _metered(streamSimple, model, context, options)

    return wrapped


def registerApiProvider(provider: ApiProvider, sourceId: str | None = None) -> None:
    _apiProviderRegistry[provider.api] = _RegisteredApiProvider(
        provider=ApiProvider(
            api=provider.api,
            stream=_wrapStream(provider.api, provider.stream),
            streamSimple=_wrapStreamSimple(provider.api, provider.streamSimple),
        ),
        sourceId=sourceId,
    )


def getApiProvider(api: Api) -> _ApiProviderInternal | None:
    entry = _apiProviderRegistry.get(api)
    return entry.provider if entry is not None else None


def getApiProviders() -> list[_ApiProviderInternal]:
    return [entry.provider for entry in _apiProviderRegistry.values()]


def unregisterApiProviders(sourceId: str) -> None:
    for api, entry in list(_apiProviderRegistry.items()):
        if entry.sourceId == sourceId:
            del _apiProviderRegistry[api]


def _clearApiProviders() -> None:
    _apiProviderRegistry.clear()


def registerFauxProvider(options: RegisterFauxProviderOptions | None = None) -> FauxProviderRegistration:
    core = createFauxCore(options if options is not None else {})
    sourceId = f"faux-provider-{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}"
    registerApiProvider(ApiProvider(api=core.api, stream=core.stream, streamSimple=core.streamSimple), sourceId)
    return SimpleNamespace(
        api=core.api,
        models=core.models,
        getModel=core.getModel,
        state=core.state,
        setResponses=core.setResponses,
        appendResponses=core.appendResponses,
        getPendingResponseCount=core.getPendingResponseCount,
        unregister=lambda: unregisterApiProviders(sourceId),
    )


_BUILTIN_APIS: list[tuple[Api, ProviderStreams]] = [
    ("anthropic-messages", anthropicMessagesApi()),
    ("openai-completions", openAICompletionsApi()),
    ("openai-responses", openAIResponsesApi()),
    ("openai-codex-responses", openAICodexResponsesApi()),
    ("azure-openai-responses", azureOpenAIResponsesApi()),
    ("google-generative-ai", googleGenerativeAIApi()),
    ("google-vertex", googleVertexApi()),
    ("mistral-conversations", mistralConversationsApi()),
    ("bedrock-converse-stream", bedrockConverseStreamApi()),
    ("pi-messages", piMessagesApi()),
]

_builtinApiProviderInstances: dict[Api, _ApiProviderInternal | None] = {}


def registerBuiltInApiProviders() -> None:
    """Registers the builtin API implementations into the api-registry without
    clobbering existing entries: compat may load after a test or extension has
    already registered an override for a builtin api id.
    """
    for api, streams in _BUILTIN_APIS:
        if not getApiProvider(api):
            registerApiProvider(ApiProvider(api=api, stream=streams.stream, streamSimple=streams.streamSimple))
        _builtinApiProviderInstances[api] = getApiProvider(api)


def resetApiProviders() -> None:
    _clearApiProviders()
    _builtinApiProviderInstances.clear()
    registerBuiltInApiProviders()


registerBuiltInApiProviders()


@cache
def _compatModels() -> Any:
    # MISAKA fork: `const compatModels = builtinModels()` is built on first use; building it at import would run every
    # provider factory whenever any compat name is imported.
    from misaka.ai.providers.all import builtinModels

    return builtinModels()


_AMBIENT_AUTH_MARKER = "<authenticated>"


def _hasExplicitApiKey(apiKey: Any) -> bool:
    return isinstance(apiKey, str) and jsLength(jsTrim(apiKey)) > 0


def _withEnvApiKey(model: Model, options: Any, defaultType: type) -> Any:
    if _hasExplicitApiKey(read_field(options, "apiKey")):
        return options
    apiKey = getEnvApiKey(model.provider, read_field(options, "env"))
    if not apiKey or apiKey == _AMBIENT_AUTH_MARKER:
        return options
    # MISAKA fork: `{ ...options, apiKey }` for whichever shape the options arrive in (`defaultType` when there are
    # none).
    if options is None:
        return defaultType(apiKey=apiKey)
    if isinstance(options, Mapping):
        return {**options, "apiKey": apiKey}
    if hasattr(options, "model_copy"):
        return options.model_copy(update={"apiKey": apiKey})
    copied = copy.copy(options)
    copied.apiKey = apiKey
    return copied


def _hasResolvedCloudflareAuth(options: Any) -> bool:
    headers = read_field(options, "headers")
    return _hasExplicitApiKey(read_field(options, "apiKey")) or (
        isinstance(headers, Mapping) and isinstance(headers.get("cf-aig-authorization"), str)
    )


def _getBuiltinProviderForModel(model: Model) -> Any:
    if getApiProvider(model.api) is not _builtinApiProviderInstances.get(model.api):
        return None
    provider = _compatModels().getProvider(model.provider)
    return provider if provider and any(candidate.api == model.api for candidate in provider.getModels()) else None


def _resolveApiProvider(api: Api) -> _ApiProviderInternal:
    provider = getApiProvider(api)
    if not provider:
        raise RuntimeError(f"No API provider registered for api: {api}")
    return provider


def stream(model: Model, context: Context, options: ProviderStreamOptions | None = None) -> AssistantMessageEventStream:
    transcript = normalizeContext(context)
    builtinProvider = _getBuiltinProviderForModel(model)
    if builtinProvider:
        if model.provider.startswith("cloudflare-") and not _hasResolvedCloudflareAuth(options):
            return _compatModels().stream(model, transcript, options)
        return builtinProvider.stream(model, transcript, _withEnvApiKey(model, options, ProviderStreamOptions))
    provider = _resolveApiProvider(model.api)
    return provider.stream(model, transcript, _withEnvApiKey(model, options, StreamOptions))


async def complete(model: Model, context: Context, options: ProviderStreamOptions | None = None) -> AssistantMessage:
    s = stream(model, context, options)
    return await s.result()


def streamSimple(model: Model, context: Context, options: SimpleStreamOptions | None = None) -> AssistantMessageEventStream:
    transcript = normalizeContext(context)
    builtinProvider = _getBuiltinProviderForModel(model)
    if builtinProvider:
        if model.provider.startswith("cloudflare-") and not _hasResolvedCloudflareAuth(options):
            return _compatModels().streamSimple(model, transcript, options)
        return builtinProvider.streamSimple(model, transcript, _withEnvApiKey(model, options, SimpleStreamOptions))
    provider = _resolveApiProvider(model.api)
    return provider.streamSimple(model, transcript, _withEnvApiKey(model, options, SimpleStreamOptions))


async def completeSimple(model: Model, context: Context, options: SimpleStreamOptions | None = None) -> AssistantMessage:
    s = streamSimple(model, context, options)
    return await s.result()
