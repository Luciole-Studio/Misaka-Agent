"""Hidden built-in llama.cpp provider and router model manager.

Translated from pi's ``coding-agent/src/extensions/llama/index.ts``.
"""

from __future__ import annotations

from typing import Any

from misaka.ai.models import ModelsRefreshOptions
from misaka.core.wiring import KINDS
from misaka.utils.js import AbortSignal, DOMException, errorMessage

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
# `builtin:llama.cpp`, as upstream bundles it: hidden from the startup list, and disabled with
# `-builtin:llama.cpp` in the `extensions` setting.
BUILTIN = True
SESSION_KINDS = KINDS


def modelIsLoaded(model: LlamaModelInfo) -> bool:
    return model.status.value in {"loaded", "sleeping"}


def isConnectionError(error: BaseException) -> bool:
    # `${error.name} ${error.message}`: a DOMException carries its name; another error's name is its class.
    name = error.name if isinstance(error, DOMException) else type(error).__name__
    message = f"{name} {errorMessage(error)}".lower()
    return "fetch failed" in message or "timeout" in message or "network" in message


def connectionErrorMessage(error: BaseException) -> str:
    if isConnectionError(error):
        return "Could not connect to the server."
    return errorMessage(error)


def parseHuggingFaceModel(value: str) -> dict[str, str]:
    """``{"repository", "quantization"?}``."""
    colon = value.find(":", value.find("/") + 1)
    return {"repository": value} if colon < 0 else {"repository": value[:colon], "quantization": value[colon + 1 :]}


async def configuredClient(ctx: Any) -> LlamaClient | None:
    result = await ctx.modelRegistry.getProviderAuth(LLAMA_PROVIDER_ID)
    if result is None:
        ctx.ui.notify(f"Configure llama.cpp with /login {LLAMA_PROVIDER_ID}", "warning")
        return None
    configuredUrl = (result.env or {}).get("LLAMA_BASE_URL")
    serverUrl = normalizeLlamaServerUrl(
        configuredUrl if isinstance(configuredUrl, str) and configuredUrl else (result.auth.baseUrl or "")
    )
    return LlamaClient(serverUrl, result.auth.apiKey)


def register(harn: Any) -> None:
    """MISAKA fork: upstream's default export ``llamaExtension(pi)``, under the bundled extensions' entry name."""
    provider = createLlamaProvider()
    harn.registerProvider(provider.provider)

    async def syncCatalog(ctx: Any, client: LlamaClient, catalog: list[LlamaModelInfo] | None = None) -> list[LlamaModelInfo]:
        signal = AbortSignal.timeout(15_000)
        current = catalog if catalog is not None else await client.list({"signal": signal})
        provider.setCatalog(current, client.serverUrl)
        result = await ctx.modelRegistry.refresh(
            ModelsRefreshOptions(
                providers=[LLAMA_PROVIDER_ID],
                # /llama already contacted the configured llama.cpp server, so keep this refresh live even in PI_OFFLINE.
                allowNetwork=True,
                signal=signal,
            )
        )
        if result.aborted:
            raise RuntimeError("Model catalog refresh timed out.")
        refreshError = result.errors.get(LLAMA_PROVIDER_ID)
        if refreshError:
            raise refreshError
        return current

    async def loadModel(
        ctx: Any,
        ui: LlamaUi,
        client: LlamaClient,
        catalog: list[LlamaModelInfo],
        target: LlamaModelInfo,
    ) -> None:
        loaded = [model for model in catalog if model.id != target.id and modelIsLoaded(model)]
        replace = False
        if len(loaded) > 0:
            choice = await ui.select(
                f"{len(loaded)} model{' is' if len(loaded) == 1 else 's are'} loaded",
                ["Unload all and load", "Keep loaded and load", "Cancel"],
            )
            if not choice or choice == "Cancel":
                return
            replace = choice == "Unload all and load"

        async def restoreLoaded() -> None:
            ctx.ui.notify("Restoring previously loaded models")
            for model in loaded:
                await client.loadAndWait(model.id, lambda _progress: None)
            await syncCatalog(ctx, client)

        if replace:
            for model in loaded:
                await client.unloadAndWait(model.id)

        try:
            result = await runWithProgress(ui, {"title": "Loading model", "model": target.id, "initialMessage": "Starting…", "cancelTitle": "Stop loading?", "cancelMessage": target.id, "run": lambda signal, update: client.loadAndWait(target.id, update, signal), "cancel": lambda: client.unload(target.id)})
            if result.cancelled:
                if replace:
                    await restoreLoaded()
                return
            refreshed = await syncCatalog(ctx, client)
            loadedModel = next((model for model in refreshed if model.id == target.id), None)
            ctx.ui.notify(
                f"Loaded {target.id}"
                if loadedModel is not None and loadedModel.status.value == "loaded"
                else f"Load started for {target.id}"
            )
        except Exception:
            if replace:
                try:
                    await restoreLoaded()
                except Exception:  # noqa: BLE001, S110
                    # Preserve the original load error.
                    pass
            raise

    async def unloadModel(ctx: Any, ui: LlamaUi, client: LlamaClient, model: LlamaModelInfo) -> None:
        if not (await ui.confirm("Unload model?", model.id)):
            return
        await client.unloadAndWait(model.id)
        await syncCatalog(ctx, client)
        ctx.ui.notify(f"Unloaded {model.id}")

    async def downloadModel(ctx: Any, ui: LlamaUi, client: LlamaClient) -> None:
        huggingFace = HuggingFaceClient(await findHuggingFaceToken())
        selected = await ui.searchModels(lambda query, signal: huggingFace.search(query, signal))
        if not selected:
            return
        parsed = parseHuggingFaceModel(selected)
        ui.showStatus("Loading model details", parsed["repository"])
        details = await huggingFace.details(parsed["repository"])
        if details.gated:
            approval = "Manual approval is required" if details.gated == "manual" else "Accept the access terms"
            choice = await ui.select(
                f"Hugging Face access required\n{details.id}\n\n{approval} at:\nhttps://huggingface.co/{details.id}\n\n"
                "The llama.cpp server needs HF_TOKEN with access.",
                ["Continue", "Back"],
            )
            if choice != "Continue":
                return
        quantization = parsed.get("quantization")
        if not quantization and len(details.quantizations) > 0:

            def option(entry: Any) -> str:
                detail = " · ".join(
                    value
                    for value in [
                        None if entry.size is None else formatBytes(entry.size),
                        "recommended" if entry.name == "Q4_K_M" else None,
                    ]
                    if value
                )
                return f"{entry.name} · {detail}" if detail else entry.name

            options = [option(entry) for entry in details.quantizations]
            choice = await ui.select(f"Select quantization\n{details.id}", options)
            if not choice:
                return
            quantization = details.quantizations[options.index(choice)].name
            if not quantization:
                return
        model = f"{details.id}:{quantization}" if quantization else details.id
        result = await runWithProgress(ui, {"title": "Downloading model", "model": model, "initialMessage": "Starting…", "cancelTitle": "Stop download?", "cancelMessage": model, "run": lambda signal, update: client.downloadAndWait(model, update, signal), "cancel": lambda: client.unload(model)})
        if result.cancelled:
            return
        await syncCatalog(ctx, client, result.value)
        ctx.ui.notify(f"Downloaded {model}")

    async def handler(_args: str, ctx: Any) -> None:
        if ctx.mode != "tui":
            ctx.ui.notify("/llama is available in interactive mode", "warning")
            return
        client = await configuredClient(ctx)
        if client is None:
            return

        async def run(ui: LlamaUi) -> None:
            async def readCatalog() -> list[LlamaModelInfo] | None:
                while True:
                    try:
                        return await syncCatalog(ctx, client)
                    except Exception as error:  # noqa: BLE001
                        if (await ui.connectionError(client.serverUrl, connectionErrorMessage(error))) == "close":
                            return None

            catalog = await readCatalog()
            # `!catalog`: an empty catalog is still a catalog.
            if catalog is None:
                return
            while True:
                action = await ui.showModels(client.serverUrl, catalog)
                if action.type == "close":
                    return
                actionError: Exception | None = None
                try:
                    if action.type == "download":
                        await downloadModel(ctx, ui, client)
                    elif modelIsLoaded(action.model):
                        await unloadModel(ctx, ui, client, action.model)
                    elif action.model.status.value == "unloaded":
                        await loadModel(ctx, ui, client, catalog, action.model)
                    else:
                        ctx.ui.notify(f"{action.model.id} is {action.model.status.value}", "warning")
                except Exception as error:  # noqa: BLE001
                    actionError = error
                refreshed = await readCatalog()
                if refreshed is None:
                    return
                catalog = refreshed
                if actionError and not isConnectionError(actionError):
                    ctx.ui.notify(errorMessage(actionError), "error")

        await showLlamaUi(ctx, run)

    harn.registerCommand("llama", {"description": "Manage llama.cpp router models", "handler": handler})


def activate(_spec: Any):
    return register


__all__ = ["BUILTIN", "EXTENSION_NAME", "SESSION_KINDS", "activate", "register"]
