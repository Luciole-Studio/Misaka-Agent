"""Translated from pi's ``packages/ai/src/providers/azure.ts`` (v1.1.0).

MISAKA fork: stream options arrive as pydantic models from the runtime and as plain mappings (or nothing) from
callers; each is copied the way ``{...options}`` copies an object, and a caller's ``onPayload`` may be sync. The
returned ``ProviderStreams`` is a ``SimpleNamespace``, the plain object upstream builds.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from misaka.ai.api._common import _option
from misaka.ai.api.azure_openai_config import resolveAzureBaseUrl, resolveDeploymentName
from misaka.ai.api.azure_openai_responses_lazy import azureOpenAIResponsesApi
from misaka.ai.api.lazy import lazyStream
from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import CreateProviderOptions, Provider, createProvider
from misaka.ai.providers.azure_models import AZURE_MODELS
from misaka.ai.types import Model, ProviderStreams
from misaka.utils.js import nullishCoalesce
from misaka.utils.values import maybe_await


def _resolveAzureModel(model: Model, options: Any) -> Model:
    return model.model_copy(update={"baseUrl": resolveAzureBaseUrl(model, options)})


def _withDeploymentName(model: Model, options: Any) -> Any:
    """Send the deployment name as the request's model, keeping `model.id` as the catalog id."""
    deploymentName = resolveDeploymentName(model, options)
    if deploymentName == model.id:
        return options

    async def onPayload(payload: Any, payloadModel: Any) -> Any:
        params = {**payload, "model": deploymentName}
        callerOnPayload = _option(options, "onPayload")
        return nullishCoalesce(
            await maybe_await(callerOnPayload(params, payloadModel)) if callerOnPayload else None, params
        )

    if options is None or isinstance(options, Mapping):
        return {**(options or {}), "onPayload": onPayload}
    return options.model_copy(update={"onPayload": onPayload})


def _azureStreams(streams: ProviderStreams) -> ProviderStreams:
    """Resolve the Azure endpoint and deployment before dispatch, inside `lazyStream` so an
    unconfigured endpoint errors on the stream instead of throwing out of `stream()`.
    """

    async def stream(model: Model, context: Any, options: Any) -> Any:
        return streams.stream(_resolveAzureModel(model, options), context, _withDeploymentName(model, options))

    async def streamSimple(model: Model, context: Any, options: Any) -> Any:
        return streams.streamSimple(_resolveAzureModel(model, options), context, _withDeploymentName(model, options))

    return SimpleNamespace(  # type: ignore[return-value]
        stream=lambda model, context, options=None: lazyStream(model, lambda: stream(model, context, options)),
        streamSimple=lambda model, context, options=None: lazyStream(
            model, lambda: streamSimple(model, context, options)
        ),
    )


def azureProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="azure",
            name="Azure",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Azure OpenAI API key", ["AZURE_OPENAI_API_KEY"])),
            models=list(AZURE_MODELS.values()),
            api={
                "azure-openai-responses": azureOpenAIResponsesApi(),
                "openai-completions": _azureStreams(openAICompletionsApi()),
            },
        )
    )


__all__ = ["azureProvider"]
