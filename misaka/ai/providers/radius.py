"""Translated from pi's ``packages/ai/src/providers/radius.ts`` (v1.1.0).

MISAKA fork: the returned ``Provider`` is a ``SimpleNamespace``, the plain object upstream builds; a publication that
persists says so with ``persist_is_set`` (``ModelsPublication``), because Python has one word for ``undefined`` and
``null``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from misaka.ai.api.pi_messages_lazy import piMessagesApi
from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.oauth.load import loadRadiusOAuth
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models import ModelsPublication, Provider, RefreshModelsContext
from misaka.ai.models_store import ModelsStoreEntry
from misaka.ai.providers.radius_config import (
    DEFAULT_RADIUS_GATEWAY,
    getRadiusModels,
    getRadiusModelsFromConfig,
    loadRadiusGatewayConfig,
    normalizeRadiusGatewayUrl,
)
from misaka.ai.providers.radius_models import RADIUS_MODELS
from misaka.ai.types import Model
from misaka.utils.values import read_field, signal_aborted


@dataclass(slots=True)
class RadiusProviderOptions:
    id: str | None = None
    name: str | None = None
    gateway: str | None = None


def _now() -> int:
    """``Date.now()``."""
    return int(time.time() * 1000)


def radiusProvider(options: RadiusProviderOptions | None = None) -> Provider:
    """Radius gateway provider with a persisted, dynamically refreshed catalog."""
    options = options if options is not None else RadiusProviderOptions()
    id = options.id if options.id is not None else "radius"
    name = options.name if options.name is not None else "Radius"
    gateway = normalizeRadiusGatewayUrl(options.gateway if options.gateway is not None else DEFAULT_RADIUS_GATEWAY)
    baselineModels: list[Model] = (
        [model.model_copy(update={"provider": id}) for model in RADIUS_MODELS.values()]
        if gateway == normalizeRadiusGatewayUrl(DEFAULT_RADIUS_GATEWAY)
        else []
    )
    # Gateway catalog for this account. Radius org owners can disable models, so once known it replaces
    # the shipped baseline instead of overlaying it. The baseline only covers the time before any catalog exists.
    dynamicModels: list[Model] | None = None
    streams = piMessagesApi()

    def _loadOAuth() -> Any:
        from misaka.ai.auth.oauth.radius import RadiusOAuthOptions

        return loadRadiusOAuth(RadiusOAuthOptions(name=name, gateway=gateway))

    async def refreshModels(context: RefreshModelsContext) -> None:
        nonlocal dynamicModels
        stored = context.stored
        if stored:
            restored = [model for model in stored.models if model.provider == id]

            def restore() -> None:
                nonlocal dynamicModels
                dynamicModels = restored

            if not await context.publish(ModelsPublication(update=restore)):
                return

        # Import catalogs cached by the pre-ModelsStore Radius implementation.
        if not stored and read_field(context.credential, "type") == "oauth":
            legacy = getRadiusModels(id, context.credential)  # type: ignore[arg-type]
            if len(legacy) > 0:

                def importLegacy() -> None:
                    nonlocal dynamicModels
                    dynamicModels = legacy

                if not await context.publish(
                    ModelsPublication(
                        persist=ModelsStoreEntry(models=legacy, checkedAt=_now()),
                        persist_is_set=True,
                        update=importLegacy,
                    )
                ):
                    return

        if not context.allowNetwork or signal_aborted(context.signal):
            return
        apiKey = (
            read_field(context.credential, "access")
            if read_field(context.credential, "type") == "oauth"
            else read_field(context.credential, "key")
        )
        config = await loadRadiusGatewayConfig(gateway, apiKey, context.signal)
        if signal_aborted(context.signal):
            return
        refreshed = getRadiusModelsFromConfig(id, config)

        def update() -> None:
            nonlocal dynamicModels
            dynamicModels = refreshed

        await context.publish(
            ModelsPublication(
                persist=ModelsStoreEntry(models=refreshed, checkedAt=_now()),
                persist_is_set=True,
                update=update,
            )
        )

    return SimpleNamespace(  # type: ignore[return-value]
        id=id,
        name=name,
        auth=ProviderAuth(
            apiKey=envApiKeyAuth("Radius API key", ["RADIUS_API_KEY"]),
            oauth=lazyOAuth({"name": name, "load": _loadOAuth}),
        ),
        getModels=lambda: dynamicModels if dynamicModels is not None else baselineModels,
        refreshModels=refreshModels,
        stream=lambda model, context, streamOptions=None: streams.stream(model, context, streamOptions),
        streamSimple=lambda model, context, streamOptions=None: streams.streamSimple(model, context, streamOptions),
    )


__all__ = ["RadiusProviderOptions", "radiusProvider"]
