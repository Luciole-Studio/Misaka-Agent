"""Hidden built-in llama.cpp provider and router model manager."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from misaka.ai.models_runtime import AbortController
from misaka.core.wiring import KINDS

from .client import (
    LlamaClient,
    LlamaModelInfo,
    formatBytes,
    normalizeLlamaServerUrl,
)
from .huggingface import HuggingFaceClient, findHuggingFaceToken
from .provider import LLAMA_PROVIDER_ID, createLlamaProvider
from .ui import LlamaUi, runWithProgress, showLlamaUi

EXTENSION_NAME = LLAMA_PROVIDER_ID
HIDDEN = True
SESSION_KINDS = KINDS


def _model_is_loaded(model: LlamaModelInfo) -> bool:
    return model.status.value in {"loaded", "sleeping"}


def _is_connection_error(error: BaseException) -> bool:
    if isinstance(error, (httpx.NetworkError, httpx.TimeoutException)):
        return True
    message = f"{type(error).__name__} {error}".lower()
    return any(word in message for word in ("connect", "timeout", "network"))


def _connection_error_message(error: BaseException) -> str:
    return (
        "Could not connect to the server."
        if _is_connection_error(error)
        else str(error)
    )


def _parse_hugging_face_model(value: str) -> tuple[str, str | None]:
    slash = value.find("/")
    colon = value.find(":", slash + 1)
    return (value, None) if colon < 0 else (value[:colon], value[colon + 1 :])


async def _configured_client(ctx: Any) -> LlamaClient | None:
    result = await ctx.modelRegistry.getProviderAuth(LLAMA_PROVIDER_ID)
    if result is None:
        ctx.ui.notify(f"Configure llama.cpp with /login {LLAMA_PROVIDER_ID}", "warning")
        return None
    configured_url = (result.env or {}).get("LLAMA_BASE_URL")
    server_url = normalizeLlamaServerUrl(
        configured_url
        if isinstance(configured_url, str) and configured_url
        else (result.auth.baseUrl or "")
    )
    return LlamaClient(server_url, result.auth.apiKey)


def register(harn: Any) -> None:
    controller = createLlamaProvider()
    harn.registerProvider(controller.provider)

    async def sync_catalog(
        ctx: Any,
        client: LlamaClient,
        catalog: list[LlamaModelInfo] | None = None,
    ) -> list[LlamaModelInfo]:
        signal = AbortController()

        async def expire() -> None:
            await asyncio.sleep(15)
            signal.abort()

        timeout = asyncio.create_task(expire())
        try:
            current = (
                catalog if catalog is not None else await client.list(signal=signal)
            )
            controller.setCatalog(current, client.serverUrl)
            result = await ctx.modelRegistry.refresh(
                {
                    "providers": [LLAMA_PROVIDER_ID],
                    "allowNetwork": True,
                    "signal": signal,
                }
            )
        finally:
            timeout.cancel()
            await asyncio.gather(timeout, return_exceptions=True)
        if result.aborted:
            raise RuntimeError("Model catalog refresh timed out.")
        if LLAMA_PROVIDER_ID in result.errors:
            raise result.errors[LLAMA_PROVIDER_ID]
        return current

    async def load_model(
        ctx: Any,
        ui: LlamaUi,
        client: LlamaClient,
        catalog: list[LlamaModelInfo],
        target: LlamaModelInfo,
    ) -> None:
        loaded = [
            model
            for model in catalog
            if model.id != target.id and _model_is_loaded(model)
        ]
        replace = False
        if loaded:
            choice = await ui.select(
                f"{len(loaded)} model{' is' if len(loaded) == 1 else 's are'} loaded",
                ["Unload all and load", "Keep loaded and load", "Cancel"],
            )
            if not choice or choice == "Cancel":
                return
            replace = choice == "Unload all and load"

        async def restore_loaded() -> None:
            ctx.ui.notify("Restoring previously loaded models")
            for model in loaded:
                await client.loadAndWait(model.id, lambda _progress: None)
            await sync_catalog(ctx, client)

        if replace:
            for model in loaded:
                await client.unloadAndWait(model.id)
        try:
            result = await runWithProgress(
                ui,
                title="Loading model",
                model=target.id,
                initialMessage="Starting…",
                cancelTitle="Stop loading?",
                cancelMessage=target.id,
                run=lambda signal, update: client.loadAndWait(
                    target.id, update, signal
                ),
                cancel=lambda: client.unload(target.id),
            )
            if result.cancelled:
                if replace:
                    await restore_loaded()
                return
            refreshed = await sync_catalog(ctx, client)
            loaded_model = next(
                (model for model in refreshed if model.id == target.id), None
            )
            ctx.ui.notify(
                f"Loaded {target.id}"
                if loaded_model is not None and loaded_model.status.value == "loaded"
                else f"Load started for {target.id}"
            )
        except Exception:
            if replace:
                try:
                    await restore_loaded()
                except Exception:  # noqa: BLE001,S110 - preserve the original load error
                    pass
            raise

    async def unload_model(
        ctx: Any, ui: LlamaUi, client: LlamaClient, model: LlamaModelInfo
    ) -> None:
        if not await ui.confirm("Unload model?", model.id):
            return
        await client.unloadAndWait(model.id)
        await sync_catalog(ctx, client)
        ctx.ui.notify(f"Unloaded {model.id}")

    async def download_model(ctx: Any, ui: LlamaUi, client: LlamaClient) -> None:
        hugging_face = HuggingFaceClient(await findHuggingFaceToken())
        selected = await ui.searchModels(hugging_face.search)
        if not selected:
            return
        repository, quantization = _parse_hugging_face_model(selected)
        ui.showStatus("Loading model details", repository)
        details = await hugging_face.details(repository)
        if details.gated:
            approval = (
                "Manual approval is required"
                if details.gated == "manual"
                else "Accept the access terms"
            )
            choice = await ui.select(
                "\n".join(
                    [
                        "Hugging Face access required",
                        details.id,
                        "",
                        f"{approval} at:",
                        f"https://huggingface.co/{details.id}",
                        "",
                        "The llama.cpp server needs HF_TOKEN with access.",
                    ]
                ),
                ["Continue", "Back"],
            )
            if choice != "Continue":
                return
        if not quantization and details.quantizations:
            options: list[str] = []
            for entry in details.quantizations:
                annotations = [
                    formatBytes(entry.size) if entry.size is not None else None,
                    "recommended" if entry.name == "Q4_K_M" else None,
                ]
                detail = " · ".join(value for value in annotations if value)
                options.append(f"{entry.name} · {detail}" if detail else entry.name)
            choice = await ui.select(f"Select quantization\n{details.id}", options)
            if not choice:
                return
            quantization = details.quantizations[options.index(choice)].name
        model = f"{details.id}:{quantization}" if quantization else details.id
        result = await runWithProgress(
            ui,
            title="Downloading model",
            model=model,
            initialMessage="Starting…",
            cancelTitle="Stop download?",
            cancelMessage=model,
            run=lambda signal, update: client.downloadAndWait(model, update, signal),
            cancel=lambda: client.unload(model),
        )
        if result.cancelled:
            return
        await sync_catalog(ctx, client, result.value)
        ctx.ui.notify(f"Downloaded {model}")

    async def command(_args: str, ctx: Any) -> None:
        if ctx.mode != "tui":
            ctx.ui.notify("/llama is available in interactive mode", "warning")
            return
        client = await _configured_client(ctx)
        if client is None:
            return

        async def run(ui: LlamaUi) -> None:
            async def read_catalog() -> list[LlamaModelInfo] | None:
                while True:
                    try:
                        return await sync_catalog(ctx, client)
                    except Exception as error:  # noqa: BLE001 - retry belongs to the UI
                        if (
                            await ui.connectionError(
                                client.serverUrl, _connection_error_message(error)
                            )
                            == "close"
                        ):
                            return None

            catalog = await read_catalog()
            if catalog is None:
                return
            while True:
                action = await ui.showModels(client.serverUrl, catalog)
                if action.type == "close":
                    return
                action_error: Exception | None = None
                try:
                    if action.type == "download":
                        await download_model(ctx, ui, client)
                    elif action.model is not None and _model_is_loaded(action.model):
                        await unload_model(ctx, ui, client, action.model)
                    elif (
                        action.model is not None
                        and action.model.status.value == "unloaded"
                    ):
                        await load_model(ctx, ui, client, catalog, action.model)
                    elif action.model is not None:
                        ctx.ui.notify(
                            f"{action.model.id} is {action.model.status.value}",
                            "warning",
                        )
                except Exception as error:  # noqa: BLE001 - refresh before showing action error
                    action_error = error
                refreshed = await read_catalog()
                if refreshed is None:
                    return
                catalog = refreshed
                if action_error is not None and not _is_connection_error(action_error):
                    ctx.ui.notify(str(action_error), "error")

        await showLlamaUi(ctx, run)

    harn.registerCommand(
        "llama",
        {
            "description": "Manage llama.cpp router models",
            "handler": command,
        },
    )


def activate(_spec: Any):
    return register


__all__ = ["EXTENSION_NAME", "HIDDEN", "SESSION_KINDS", "activate", "register"]
