"""Native llama.cpp provider, including Pi timeline item 66 autoload semantics."""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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
from misaka.ai.models_runtime import ModelsPublication, RefreshModelsContext
from misaka.ai.models_store import ModelsStoreEntry
from misaka.ai.stream import stream, stream_simple
from misaka.ai.types import Model, ModelCost, OpenAICompletionsCompat
from misaka.utils.values import signal_aborted

from .client import (
    LlamaClient,
    LlamaModelInfo,
    llamaInferenceUrl,
    normalizeLlamaServerUrl,
)

LLAMA_PROVIDER_ID = "llama.cpp"
DEFAULT_LLAMA_SERVER_URL = "http://127.0.0.1:8080"


def _credential_server_url(credential: ApiKeyCredential | None) -> str | None:
    value = (
        (credential.env or {}).get("LLAMA_BASE_URL") if credential is not None else None
    )
    return (
        normalizeLlamaServerUrl(value)
        if isinstance(value, str) and value.strip()
        else None
    )


async def _resolve_server_url(
    ctx: Any, credential: ApiKeyCredential | None
) -> str | None:
    configured = _credential_server_url(credential)
    if configured is None:
        ambient = await ctx.env("LLAMA_BASE_URL")
        configured = ambient.strip() if isinstance(ambient, str) else None
    return normalizeLlamaServerUrl(configured) if configured else None


def modelIsSelectable(model: LlamaModelInfo, routerAutoload: bool) -> bool:
    if model.status.value in {"loaded", "sleeping"}:
        return True
    return (
        routerAutoload
        and model.status.value == "unloaded"
        and not model.status.failed
        and model.source == "preset"
    )


async def _router_autoload_enabled(
    client: LlamaClient, catalog: Sequence[LlamaModelInfo], signal: Any
) -> bool:
    if not any(
        model.status.value == "unloaded" and model.source == "preset"
        for model in catalog
    ):
        return False
    try:
        return (await client.props(signal=signal)).models_autoload is True
    except Exception:  # noqa: BLE001 - old/erroring servers fail closed
        return False


def _to_model(model: LlamaModelInfo, serverUrl: str) -> Model:
    reported = (
        (model.meta.n_ctx if model.meta.n_ctx is not None else model.meta.n_ctx_train)
        if model.meta is not None
        else None
    )
    context_window = reported if reported is not None and reported > 0 else 128_000
    modalities = model.architecture.input_modalities if model.architecture else None
    return Model(
        id=model.id,
        name=model.id,
        api="openai-completions",
        provider=LLAMA_PROVIDER_ID,
        baseUrl=llamaInferenceUrl(serverUrl),
        reasoning=False,
        input=["text", "image"] if modalities and "image" in modalities else ["text"],
        cost=ModelCost(input=0, output=0, cacheRead=0, cacheWrite=0),
        contextWindow=context_window,
        maxTokens=context_window,
        compat=OpenAICompletionsCompat(
            supportsStore=False,
            supportsDeveloperRole=False,
            supportsReasoningEffort=False,
            supportsUsageInStreaming=True,
            supportsStrictMode=False,
            maxTokensField="max_tokens",
        ),
    )


class _LlamaProvider:
    id = LLAMA_PROVIDER_ID
    name = "llama.cpp"
    baseUrl = llamaInferenceUrl(DEFAULT_LLAMA_SERVER_URL)

    def __init__(self) -> None:
        self._models: list[Model] = []

        async def login(interaction: Any) -> ApiKeyCredential:
            entered_url = await interaction.prompt(
                TextPrompt(
                    message="llama.cpp server URL",
                    placeholder=os.environ.get(
                        "LLAMA_BASE_URL", DEFAULT_LLAMA_SERVER_URL
                    ),
                )
            )
            server_url = normalizeLlamaServerUrl(
                entered_url.strip()
                or os.environ.get("LLAMA_BASE_URL")
                or DEFAULT_LLAMA_SERVER_URL
            )
            api_key = (
                await interaction.prompt(SecretPrompt(message="API key (optional)"))
            ).strip()
            await LlamaClient(server_url, api_key or None).list(
                signal=interaction.signal
            )
            return ApiKeyCredential(
                key=api_key or None, env={"LLAMA_BASE_URL": server_url}
            )

        async def check(
            *, ctx: Any, credential: ApiKeyCredential | None, signal: Any
        ) -> AuthCheck | None:
            server_url = await _resolve_server_url(ctx, credential)
            if not server_url:
                return None
            return AuthCheck(
                type="api_key",
                source="stored credential" if credential else "LLAMA_BASE_URL",
            )

        async def resolve(
            *, ctx: Any, credential: ApiKeyCredential | None, signal: Any
        ) -> AuthResult | None:
            server_url = await _resolve_server_url(ctx, credential)
            if not server_url:
                return None
            api_key = credential.key if credential is not None else None
            if api_key is None:
                api_key = await ctx.env("LLAMA_API_KEY")
            if api_key is None:
                api_key = "local"
            return AuthResult(
                auth=ModelAuth(apiKey=api_key, baseUrl=llamaInferenceUrl(server_url)),
                env={
                    **((credential.env or {}) if credential is not None else {}),
                    "LLAMA_BASE_URL": server_url,
                },
                source="stored credential" if credential else "LLAMA_BASE_URL",
            )

        self.auth = ProviderAuth(
            apiKey=ApiKeyAuth(
                name="llama.cpp server", login=login, check=check, resolve=resolve
            )
        )

    def getModels(self) -> list[Model]:
        return list(self._models)

    def setCatalog(
        self,
        catalog: Sequence[LlamaModelInfo],
        serverUrl: str,
        *,
        routerAutoload: bool = False,
    ) -> None:
        self._models = [
            _to_model(model, serverUrl)
            for model in catalog
            if modelIsSelectable(model, routerAutoload)
        ]

    async def refreshModels(self, context: RefreshModelsContext) -> None:
        if context.stored is not None:
            restored = [
                model
                for model in context.stored.models
                if model.provider == LLAMA_PROVIDER_ID
                and model.api == "openai-completions"
            ]

            def apply_restored() -> None:
                self._models = restored

            if not await context.publish(ModelsPublication(update=apply_restored)):
                return

        if (
            not context.allowNetwork
            or signal_aborted(context.signal)
            or not isinstance(context.credential, ApiKeyCredential)
        ):
            return
        server_url = _credential_server_url(context.credential)
        if server_url is None:
            return
        client = LlamaClient(server_url, context.credential.key)
        catalog = await client.list(signal=context.signal)
        if signal_aborted(context.signal):
            return
        router_autoload = await _router_autoload_enabled(
            client, catalog, context.signal
        )
        if signal_aborted(context.signal):
            return
        refreshed = [
            _to_model(model, server_url)
            for model in catalog
            if modelIsSelectable(model, router_autoload)
        ]

        def apply_refreshed() -> None:
            self._models = refreshed

        await context.publish(
            ModelsPublication(
                persist=ModelsStoreEntry(
                    models=refreshed, checkedAt=int(time.time() * 1000)
                ),
                persist_is_set=True,
                update=apply_refreshed,
            )
        )

    def stream(self, model: Model, context: Any, options: Any = None):
        return stream(model, context, options)

    def streamSimple(self, model: Model, context: Any, options: Any = None):
        return stream_simple(model, context, options)


@dataclass(slots=True)
class LlamaProviderController:
    provider: _LlamaProvider

    def setCatalog(
        self,
        models: Sequence[LlamaModelInfo],
        serverUrl: str,
        *,
        routerAutoload: bool = False,
    ) -> None:
        self.provider.setCatalog(models, serverUrl, routerAutoload=routerAutoload)


def createLlamaProvider() -> LlamaProviderController:
    return LlamaProviderController(provider=_LlamaProvider())


__all__ = [
    "DEFAULT_LLAMA_SERVER_URL",
    "LLAMA_PROVIDER_ID",
    "LlamaProviderController",
    "createLlamaProvider",
    "modelIsSelectable",
]
