"""The built-in image-generation providers, as ``providers/all.ts`` assembles them.

``images_models_runtime`` had the factory and the collection but nothing to put in them:
``builtinImagesProviders`` and ``builtinImagesModels`` were never ported, so
``createImagesModels()`` handed back an empty registry and the one image provider misaka
ships was reachable only by calling its api function directly.

The api goes in as the lazily-registered one rather than the module: importing
``providers/images/openrouter`` pulls the OpenAI SDK in, and a provider list is built at
startup to be *looked at*, long before anyone generates an image.
"""

from __future__ import annotations

from typing import Any

from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.image_models_generated import IMAGE_MODELS
from misaka.ai.images_models_runtime import (
    CreateImagesProviderOptions,
    createImagesModels,
    createImagesProvider,
)
from misaka.ai.providers.images.register_builtins import generate_images_openrouter
from misaka.ai.types import ImagesModel


class _LazyImagesApi:
    """The api object ``createImagesProvider`` expects, over the lazy registration seam."""

    @staticmethod
    async def generateImages(model: ImagesModel, context: Any, options: Any = None) -> Any:
        return await generate_images_openrouter(model, context, options)


async def _load_openrouter_oauth() -> Any:
    """Imported on first login, not at provider construction: the OAuth module pulls in a
    local callback server that a provider list has no use for."""
    from misaka.ai.utils.oauth import openrouterOAuthProvider

    return openrouterOAuthProvider


def openrouterImagesProvider() -> Any:
    return createImagesProvider(
        CreateImagesProviderOptions(
            id="openrouter",
            name="OpenRouter",
            auth=ProviderAuth(
                apiKey=envApiKeyAuth("OpenRouter API key", ["OPENROUTER_API_KEY"]),
                oauth=lazyOAuth(
                    name="OpenRouter OAuth",
                    loginLabel="Sign in with OpenRouter",
                    load=_load_openrouter_oauth,
                ),
            ),
            models=list(IMAGE_MODELS["openrouter"].values()),
            api=_LazyImagesApi(),
        )
    )


def builtinImagesProviders() -> list[Any]:
    """Every built-in image-generation provider, freshly constructed."""
    return [openrouterImagesProvider()]


def builtinImagesModels(options: Any = None) -> Any:
    """An images collection with every built-in provider already registered."""
    models = createImagesModels(options)
    for provider in builtinImagesProviders():
        models.setProvider(provider)
    return models


__all__ = ["builtinImagesModels", "builtinImagesProviders", "openrouterImagesProvider"]
