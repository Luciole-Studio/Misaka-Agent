"""Translated from pi's ``packages/ai/src/providers/openrouter.ts`` (v1.3.0).

MISAKA fork: ``fetch`` is ``misaka.utils.whatwg_fetch.fetch``, and ``AbortSignal.any``/``AbortSignal.timeout`` are
``AnyAbortSignal``/``TimeoutAbortSignal``; the catalog's models are pydantic records, so a spread is ``model_copy``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from misaka.ai.api.anthropic_messages_lazy import anthropicMessagesApi
from misaka.ai.api.openai_completions_lazy import openAICompletionsApi
from misaka.ai.api.openrouter_images_lazy import openrouterImagesApi
from misaka.ai.api.typesafe_system_one_lazy import typesafeSystemOneApi
from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.oauth.load import loadOpenRouterOAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import (
    CreateProviderOptions,
    Provider,
    RefreshModelsContext,
    createProvider,
)
from misaka.ai.providers.openrouter_api import OpenRouterModel, openRouterCost
from misaka.ai.providers.openrouter_models import (
    OPENROUTER_CLASSIFIER_MODELS,
    OPENROUTER_IMAGE_MODELS,
    OPENROUTER_MODELS,
)
from misaka.ai.types import ModelCost
from misaka.ai.utils.model_operations import isModelType
from misaka.utils._abort_signal import AnyAbortSignal, TimeoutAbortSignal
from misaka.utils.js import jsTruthy
from misaka.utils.values import read_field
from misaka.utils.whatwg_fetch import fetch

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_OPENROUTER_USER_MODELS_TIMEOUT_MS = 15_000


async def personalizeOpenRouterCatalog(catalog: Sequence[Any], context: RefreshModelsContext) -> Sequence[Any]:
    """Restrict an OpenRouter catalog to the chat models the key may use, with the key's limits and
    prices from `GET /models/user`. Image and classifier models are returned unchanged. A missing
    limit keeps the catalog value. The request uses the provider's configured base URL, so regional
    endpoints such as `https://us.openrouter.ai/api/v1` apply their region's filtering. When the
    endpoint rejects the key (401/403) or does not exist (404), for example with a proxy configured
    in models.json, the catalog is returned unchanged.
    """
    key = (
        read_field(context.credential, "access")
        if read_field(context.credential, "type") == "oauth"
        else read_field(context.credential, "key")
    )
    if not key:
        return catalog
    baseUrl = context.baseUrl if context.baseUrl is not None else _OPENROUTER_BASE_URL
    response = await fetch(
        f"{baseUrl.rstrip('/')}/models/user",
        {
            "headers": {"accept": "application/json", "authorization": f"Bearer {key}"},
            "signal": AnyAbortSignal(context.signal, TimeoutAbortSignal(_OPENROUTER_USER_MODELS_TIMEOUT_MS)),
        },
    )
    try:
        if response.status in (401, 403, 404):
            return catalog
        if not response.ok:
            raise RuntimeError(f"OpenRouter model list request failed: {response.status}")
        body: Any = await response.json()
    finally:
        # MISAKA fork: undici collects an unread body; the fetch shim holds its connection until the body is read or
        # cancelled, so the rejected-key, missing-endpoint and error statuses would leak one per refresh.
        if response.body is not None:
            await response.body.cancel()
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise RuntimeError("Invalid OpenRouter model list response")  # noqa: TRY004 - upstream's `throw new Error`
    allowed: dict[str, OpenRouterModel] = {}
    for entry in body["data"]:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            allowed[entry["id"]] = entry  # type: ignore[assignment]
    personalized: list[Any] = []
    for model in catalog:
        if not isModelType(model, "chat"):
            personalized.append(model)
            continue
        entry = allowed.get(model.id)
        if entry is None:
            continue
        topProvider = entry.get("top_provider") or {}
        personalized.append(
            model.model_copy(
                update={
                    # `||`: OpenRouter reports a missing limit as absent, null or 0, like the generator's fallback.
                    "contextWindow": topProvider.get("context_length") or entry.get("context_length") or model.contextWindow,
                    "maxTokens": topProvider.get("max_completion_tokens") or model.maxTokens,
                    "cost": ModelCost.model_validate(openRouterCost(entry["pricing"]))
                    if jsTruthy(entry.get("pricing"))
                    else model.cost,
                }
            )
        )
    return personalized


def openrouterProvider() -> Provider:
    return createProvider(
        CreateProviderOptions(
            id="openrouter",
            name="OpenRouter",
            baseUrl=_OPENROUTER_BASE_URL,
            auth=ProviderAuth(
                apiKey=envApiKeyAuth("OpenRouter API key", ["OPENROUTER_API_KEY"]),
                oauth=lazyOAuth({"name": "OpenRouter OAuth", "loginLabel": "Sign in with OpenRouter", "load": loadOpenRouterOAuth}),
            ),
            models=[
                *OPENROUTER_MODELS.values(),
                *OPENROUTER_IMAGE_MODELS.values(),
                *OPENROUTER_CLASSIFIER_MODELS.values(),
            ],
            api={
                "anthropic-messages": anthropicMessagesApi(),
                "openai-completions": openAICompletionsApi(),
            },
            images={"openrouter-images": openrouterImagesApi()},
            # OpenRouter serves TypeSafe's System One protocol at /api/v1/systemone.
            classifiers={"typesafe-system-one": typesafeSystemOneApi()},
        )
    )


__all__ = ["openrouterProvider", "personalizeOpenRouterCatalog"]
