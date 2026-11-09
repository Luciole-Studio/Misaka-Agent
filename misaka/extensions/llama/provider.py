"""Native llama.cpp provider.

Translated from pi's ``coding-agent/src/extensions/llama/provider.ts``.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, TypeGuard

from misaka.ai.api.llama_cpp_classify_lazy import llamaCppClassifyApi
from misaka.ai.api.typesafe_system_one_lazy import typesafeSystemOneApi
from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthCheck,
    AuthResult,
    ModelAuth,
    ProviderAuth,
    SecretPrompt,
    TextPrompt,
)
from misaka.ai.compat import stream, streamSimple
from misaka.ai.models import ModelsPublication, RefreshModelsContext, isModelType
from misaka.ai.models_store import ModelsStoreEntry
from misaka.ai.types import (
    AnyModel,
    ClassifierModel,
    Model,
    ModelCost,
    OpenAICompletionsCompat,
)
from misaka.utils.js import jsIsSafeInteger, jsNumber, jsTrim
from misaka.utils.values import signal_aborted

from .client import (
    LlamaClient,
    LlamaModelInfo,
    LlamaServerProps,
    llamaInferenceUrl,
    normalizeLlamaServerUrl,
)

LLAMA_PROVIDER_ID = "llama.cpp"
DEFAULT_LLAMA_SERVER_URL = "http://127.0.0.1:8080"


def credentialServerUrl(credential: ApiKeyCredential | None) -> str | None:
    value = (credential.env or {}).get("LLAMA_BASE_URL") if credential is not None else None
    return normalizeLlamaServerUrl(value) if isinstance(value, str) and jsTrim(value) else None


async def resolveServerUrl(ctx: Any, credential: ApiKeyCredential | None) -> str | None:
    configured = credentialServerUrl(credential)
    if configured is None:
        ambient = await ctx.env("LLAMA_BASE_URL")
        configured = jsTrim(ambient) if ambient is not None else None
    return normalizeLlamaServerUrl(configured) if configured else None


def modelIsSelectable(model: LlamaModelInfo, routerAutoload: bool) -> bool:
    if model.status.value == "loaded":
        return True
    # llama.cpp reports idle-slept models as "sleeping"; requests wake them automatically.
    if model.status.value == "sleeping":
        return True
    # Unloaded presets are routable only when llama.cpp router autoload can load them on first use.
    return routerAutoload and model.status.value == "unloaded" and not model.status.failed and model.source == "preset"


async def routerAutoloadEnabled(
    client: LlamaClient, catalog: Sequence[LlamaModelInfo], signal: Any
) -> bool:
    if not any(
        model.status.value == "unloaded" and model.source == "preset"
        for model in catalog
    ):
        return False
    try:
        return (await client.props({"signal": signal})).models_autoload is True
    except Exception:  # noqa: BLE001 - old/erroring servers fail closed
        return False


def configuredContextWindow(model: LlamaModelInfo) -> int | None:
    args = model.status.args or []
    for index in range(len(args) - 1):
        flag = args[index]
        if flag != "--ctx-size" and flag != "-c" and flag != "-ctx":
            continue
        contextWindow = jsNumber(args[index + 1])
        if jsIsSafeInteger(contextWindow) and contextWindow > 0:
            return int(contextWindow)
    return None


def contextWindowOf(model: LlamaModelInfo, cachedContextWindow: int | None = None) -> int:
    runtimeContextWindow = model.meta.n_ctx if model.meta is not None else None
    if runtimeContextWindow and runtimeContextWindow > 0:
        return runtimeContextWindow
    configuredContext = configuredContextWindow(model)
    if configuredContext:
        return configuredContext
    if cachedContextWindow and cachedContextWindow > 0:
        return cachedContextWindow
    trainingContextWindow = model.meta.n_ctx_train if model.meta is not None else None
    return trainingContextWindow if trainingContextWindow and trainingContextWindow > 0 else 128000


type LlamaClassifierApi = Literal["llama-cpp-classify", "typesafe-system-one"]


def isDecisionModel(model: LlamaModelInfo) -> bool:
    """Whether llama.cpp reports a native decision model. Since llama.cpp 0.6.0, ``GET /models`` lists
    ``decisions`` in ``architecture.output_modalities`` for these models, including unloaded and sleeping ones.
    Older servers report ``["text"]`` or omit ``architecture``, so their models are treated as chat models."""
    modalities = model.architecture.output_modalities if model.architecture is not None else None
    return modalities is not None and "decisions" in modalities


def isChatModel(model: LlamaModelInfo) -> bool:
    """Decision-only models cannot generate text and are not listed for chat."""
    modalities = model.architecture.output_modalities if model.architecture is not None else None
    return not isDecisionModel(model) or (modalities is not None and "text" in modalities)


def toPiClassifierModel(
    model: LlamaModelInfo,
    serverUrl: str,
    cachedContextWindow: int | None = None,
) -> ClassifierModel:
    """A llama.cpp model used as a classifier. Decision models answer natively through llama.cpp's
    System One endpoint (``/v1/systemone``). Chat models fall back to ``llama-cpp-classify``, which reads
    answers from next-token label probabilities."""
    decision = isDecisionModel(model)
    return ClassifierModel(
        type="classifier",
        id=model.id,
        name=model.id,
        api="typesafe-system-one" if decision else "llama-cpp-classify",
        provider=LLAMA_PROVIDER_ID,
        baseUrl=llamaInferenceUrl(serverUrl) if decision else serverUrl,
        input=["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=contextWindowOf(model, cachedContextWindow),
    )


def isLlamaClassifierModel(model: AnyModel) -> TypeGuard[ClassifierModel]:
    return isModelType(model, "classifier") and (
        model.api == "llama-cpp-classify" or model.api == "typesafe-system-one"
    )


def toPiModel(
    model: LlamaModelInfo,
    serverUrl: str,
    props: LlamaServerProps | None = None,
    cachedContextWindow: int | None = None,
) -> Model:
    contextWindow = contextWindowOf(model, cachedContextWindow)
    reasoning = props is not None and props.chat_template is not None and "enable_thinking" in props.chat_template
    modalities = model.architecture.input_modalities if model.architecture else None
    return Model(
        id=model.id,
        name=model.id,
        api="openai-completions",
        provider=LLAMA_PROVIDER_ID,
        baseUrl=llamaInferenceUrl(serverUrl),
        reasoning=reasoning,
        thinkingLevelMap=(
            {"off": "off", "minimal": None, "low": None, "medium": "medium", "high": None, "xhigh": None}
            if reasoning
            else None
        ),
        input=["text", "image"] if modalities and "image" in modalities else ["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=contextWindow,
        maxTokens=contextWindow,
        compat=OpenAICompletionsCompat(
            supportsStore=False,
            supportsDeveloperRole=False,
            supportsReasoningEffort=False,
            supportsUsageInStreaming=True,
            supportsStrictMode=False,
            maxTokensField="max_tokens",
            thinkingFormat="qwen-chat-template" if reasoning else None,
        ),
    )


@dataclass(slots=True)
class LlamaProviderController:
    provider: Any
    setCatalog: Callable[..., None]
    """``setCatalog(models, serverUrl, options={"routerAutoload"?})``."""


def createLlamaProvider() -> LlamaProviderController:
    models: list[Model] = []
    classifiers: list[ClassifierModel] = []
    fallbackClassifier = llamaCppClassifyApi()
    decisionClassifier = typesafeSystemOneApi()

    def setCatalog(catalog: Sequence[LlamaModelInfo], serverUrl: str, options: dict[str, Any] | None = None) -> None:
        nonlocal models, classifiers
        options = options if options is not None else {}
        selectable = [model for model in catalog if modelIsSelectable(model, options.get("routerAutoload") is True)]
        models = [toPiModel(model, serverUrl) for model in selectable if isChatModel(model)]
        classifiers = [toPiClassifierModel(model, serverUrl) for model in selectable]

    async def login(interaction: Any) -> ApiKeyCredential:
        enteredUrl = await interaction.prompt(
            TextPrompt(
                message="llama.cpp server URL",
                placeholder=os.environ.get("LLAMA_BASE_URL", DEFAULT_LLAMA_SERVER_URL),
            )
        )
        serverUrl = normalizeLlamaServerUrl(
            jsTrim(enteredUrl) or os.environ.get("LLAMA_BASE_URL") or DEFAULT_LLAMA_SERVER_URL
        )
        apiKey = jsTrim(await interaction.prompt(SecretPrompt(message="API key (optional)")))
        await LlamaClient(serverUrl, apiKey or None).list({"signal": interaction.signal})
        return ApiKeyCredential(key=apiKey or None, env={"LLAMA_BASE_URL": serverUrl})

    async def check(*, ctx: Any, credential: ApiKeyCredential | None = None, signal: Any = None) -> AuthCheck | None:
        serverUrl = await resolveServerUrl(ctx, credential)
        return (
            AuthCheck(type="api_key", source="stored credential" if credential else "LLAMA_BASE_URL")
            if serverUrl
            else None
        )

    async def resolve(*, ctx: Any, credential: ApiKeyCredential | None = None, signal: Any = None) -> AuthResult | None:
        serverUrl = await resolveServerUrl(ctx, credential)
        if not serverUrl:
            return None
        apiKey = credential.key if credential is not None else None
        if apiKey is None:
            apiKey = await ctx.env("LLAMA_API_KEY")
        if apiKey is None:
            apiKey = "local"
        return AuthResult(
            auth=ModelAuth(apiKey=apiKey, baseUrl=llamaInferenceUrl(serverUrl)),
            env={**((credential.env or {}) if credential is not None else {}), "LLAMA_BASE_URL": serverUrl},
            source="stored credential" if credential else "LLAMA_BASE_URL",
        )

    async def refreshModels(context: RefreshModelsContext) -> None:
        nonlocal models, classifiers
        cachedContextWindows: dict[str, int] = {}
        if context.stored:
            stored = [model for model in context.stored.models if model.provider == LLAMA_PROVIDER_ID]
            restored = [model for model in stored if isModelType(model, "chat") and model.api == "openai-completions"]
            restoredClassifiers = [model for model in stored if isLlamaClassifierModel(model)]
            for model in [*restored, *restoredClassifiers]:
                cachedContextWindows[model.id] = model.contextWindow

            def update() -> None:
                nonlocal models, classifiers
                models = restored
                classifiers = restoredClassifiers

            if not (await context.publish(ModelsPublication(update=update))):
                return

        if (
            not context.allowNetwork
            or signal_aborted(context.signal)
            or not isinstance(context.credential, ApiKeyCredential)
        ):
            return
        serverUrl = credentialServerUrl(context.credential)
        if not serverUrl:
            return
        client = LlamaClient(serverUrl, context.credential.key)
        catalog = await client.list({"signal": context.signal})
        if signal_aborted(context.signal):
            return
        routerAutoload = await routerAutoloadEnabled(client, catalog, context.signal)
        if signal_aborted(context.signal):
            return
        selectable = [model for model in catalog if modelIsSelectable(model, routerAutoload)]

        # Only loaded models expose their chat template without side effects. Unloaded autoload presets would
        # need to be loaded, while querying sleeping models may wake them. Those models remain without thinking
        # support until they are loaded and a later catalog refresh discovers it. Decision models need no
        # template, and llama.cpp reports them in the catalog regardless of their status.
        async def refresh(model: LlamaModelInfo) -> Model:
            cachedContextWindow = cachedContextWindows.get(model.id)
            if model.status.value != "loaded":
                return toPiModel(model, serverUrl, None, cachedContextWindow)
            props = await client.props({"model": model.id, "signal": context.signal})
            return toPiModel(model, serverUrl, props, cachedContextWindow)

        refreshed = list(await asyncio.gather(*(refresh(model) for model in selectable if isChatModel(model))))
        refreshedClassifiers = [
            toPiClassifierModel(model, serverUrl, cachedContextWindows.get(model.id)) for model in selectable
        ]
        if signal_aborted(context.signal):
            return

        def update() -> None:
            nonlocal models, classifiers
            models = refreshed
            classifiers = refreshedClassifiers

        await context.publish(
            ModelsPublication(
                persist=ModelsStoreEntry(models=[*refreshed, *refreshedClassifiers], checkedAt=int(time.time() * 1000)),
                persist_is_set=True,
                update=update,
            )
        )

    async def classify(model: ClassifierModel, context: Any, options: Any = None) -> Any:
        return await (decisionClassifier if model.api == "typesafe-system-one" else fallbackClassifier).classify(
            model, context, options
        )

    # MISAKA fork: `Provider` is a Protocol; the provider is a namespace of upstream's members.
    provider = SimpleNamespace(
        id=LLAMA_PROVIDER_ID,
        name="llama.cpp",
        baseUrl=llamaInferenceUrl(DEFAULT_LLAMA_SERVER_URL),
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="llama.cpp server", login=login, check=check, resolve=resolve)),
        getModels=lambda: models,
        getAllModels=lambda: [*models, *classifiers],
        refreshModels=refreshModels,
        stream=lambda model, context, options=None: stream(model, context, options),
        streamSimple=lambda model, context, options=None: streamSimple(model, context, options),
        classify=classify,
    )

    return LlamaProviderController(provider=provider, setCatalog=setCatalog)


__all__ = [
    "DEFAULT_LLAMA_SERVER_URL",
    "LLAMA_PROVIDER_ID",
    "LlamaProviderController",
    "createLlamaProvider",
    "modelIsSelectable",
]
