"""The Radius provider, translated from pi's ``providers/radius.ts``.

Radius is upstream's one built-in whose catalog is not a table -- it is the only file in
``packages/ai/src/providers/`` that defines ``refreshModels`` -- because the gateway serves
it, so the model list is empty until a refresh restores it from the store or fetches it.
(misaka does not yet register this provider: ``provider_definitions.builtinProviders``
omits it and nothing outside the tests calls ``radiusProvider``.) That makes
``refreshModels`` the whole file, and its three phases are ordered the way upstream orders
them for reasons that survive the translation:

1. **Restore.** The persisted catalog is published first and on its own, so the provider
   comes back with its last known models even when the network phase is skipped (offline
   start) or fails. ``Models`` runs this phase with ``allowNetwork=False`` before it
   resolves any credential.
2. **Import.** Only when nothing was persisted: an OAuth credential written by the
   pre-ModelsStore Radius implementation carries a ``gatewayConfig``, and that cached
   catalog is worth one publish so the migration is not a blank model list.
3. **Fetch.** With network allowed, ``GET /v1/config`` on the gateway, persisted and
   applied.

Every publish that has a phase after it is checked: ``publish`` returns ``False`` when the
signal aborted or this refresh was superseded -- ``Models`` bumps the provider's refresh
generation on ``setProvider``, ``deleteProvider``, ``clearProviders`` and on starting a
newer refresh for the same id -- and the remaining phases are abandoned rather than written
over whatever replaced it. The final fetch publish has nothing after it, so its result is
discarded, as upstream's is.

This is deliberately *not* ``createProvider(fetchModels=...)``. That helper implements
phases 1 and 3, but not the legacy import in between, and bolting the import onto it
would mean either a fetch that publishes twice or a helper that grows a Radius-shaped
hook. Provider is a Protocol; implementing it directly is the smaller thing.

``load_config`` defaults to ``ai/providers/radius_config.load_radius_gateway_config``. No
caller passes it today -- the tests that stub the gateway fetch assign ``_load_config`` on
the instance instead -- so the parameter is currently unused.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from misaka.ai.api_lazy import lazy_stream
from misaka.ai.api_registry import get_api_provider
from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.oauth_bridge import oauth_auth_from_flow
from misaka.ai.auth.resolve import ModelsError
from misaka.ai.auth.types import ProviderAuth
from misaka.ai.models_runtime import ModelsPublication, RefreshModelsContext
from misaka.ai.models_store import ModelsStoreEntry
from misaka.ai.providers.radius_config import (
    DEFAULT_RADIUS_GATEWAY,
    RADIUS_API,
    get_radius_models,
    get_radius_models_from_config,
    load_radius_gateway_config,
    normalize_radius_gateway_url,
)
from misaka.ai.types import Context, Model
from misaka.ai.utils.oauth.radius import create_radius_oauth
from misaka.utils.values import read_field, signal_aborted

DEFAULT_RADIUS_PROVIDER_ID = "radius"
DEFAULT_RADIUS_PROVIDER_NAME = "Radius"
RADIUS_API_KEY_ENV = "RADIUS_API_KEY"


@dataclass(slots=True)
class RadiusProviderOptions:
    """Upstream's ``RadiusProviderOptions``: everything defaulted."""

    id: str | None = None
    name: str | None = None
    gateway: str | None = None


def _now_ms() -> int:
    """``Date.now()`` in milliseconds."""
    return int(time.time() * 1000)


def _credential_api_key(credential: Any) -> str | None:
    """``credential?.type === "oauth" ? credential.access : credential?.key``."""
    if credential is None:
        return None
    if read_field(credential, "type") == "oauth":
        return read_field(credential, "access")
    return read_field(credential, "key")


class _RadiusProvider:
    """A ``models_runtime.Provider`` whose catalog comes from its gateway."""

    def __init__(self, options: RadiusProviderOptions, load_config: Any = None) -> None:
        self.id = options.id or DEFAULT_RADIUS_PROVIDER_ID
        self.name = options.name or DEFAULT_RADIUS_PROVIDER_NAME
        self.gateway = normalize_radius_gateway_url(options.gateway or DEFAULT_RADIUS_GATEWAY)
        self._load_config = load_config or load_radius_gateway_config
        # Upstream seeds this with `getRadiusModels(id, undefined)`, which is the empty
        # list by construction; the seeding call is what documents where models come from.
        self._models: list[Model] = get_radius_models(self.id, None)
        self.auth = ProviderAuth(
            apiKey=envApiKeyAuth("Radius API key", [RADIUS_API_KEY_ENV]),
            # The flow is built on first use, as upstream's `lazyOAuth` does, and the
            # gateway is baked into the flow here rather than passed at login time.
            oauth=lazyOAuth(name=self.name, load=self._load_oauth),
        )

    async def _load_oauth(self):
        return oauth_auth_from_flow(
            create_radius_oauth(id=self.id, name=self.name, gateway=self.gateway), name=self.name
        )

    def getModels(self) -> list[Model]:
        return self._models

    # -- refresh ------------------------------------------------------------------

    async def refreshModels(self, context: RefreshModelsContext) -> None:
        stored = context.stored
        if stored is not None:
            # The restore keeps this provider's own models; upstream filters the stored
            # entry the same way (pi `packages/ai/src/providers/radius.ts:37`).
            restored = [model for model in stored.models if model.provider == self.id]
            if not await context.publish(ModelsPublication(update=lambda: self._apply(restored))):
                return

        if stored is None and read_field(context.credential, "type") == "oauth":
            legacy = get_radius_models(self.id, context.credential)
            if legacy and not await context.publish(
                ModelsPublication(
                    persist=ModelsStoreEntry(models=legacy, checkedAt=_now_ms()),
                    persist_is_set=True,
                    update=lambda: self._apply(legacy),
                )
            ):
                return

        if not context.allowNetwork or signal_aborted(context.signal):
            return

        config = await self._load_config(
            self.gateway, _credential_api_key(context.credential), context.signal
        )
        # Re-checked after the fetch: an abort that lands mid-request must not be followed
        # by a publish. `publish` checks again under its own lock; this is the cheap check.
        if signal_aborted(context.signal):
            return
        refreshed = get_radius_models_from_config(self.id, config)
        await context.publish(
            ModelsPublication(
                persist=ModelsStoreEntry(models=refreshed, checkedAt=_now_ms()),
                persist_is_set=True,
                update=lambda: self._apply(refreshed),
            )
        )

    def _apply(self, models: list[Model]) -> None:
        self._models = models

    # -- streaming ----------------------------------------------------------------

    def _dispatch(self, model: Model, context: Context, options: Any, simple: bool):
        """Look ``pi-messages`` up in the API registry at call time.

        A missing implementation ends the returned stream with an error rather than
        raising.
        """
        api = get_api_provider(RADIUS_API)
        if api is None:

            async def fail():
                raise ModelsError(
                    "provider", f'Provider {self.id} has no API implementation for "{RADIUS_API}"'
                )

            return lazy_stream(model, fail)
        return (api.streamSimple if simple else api.stream)(model, context, options)

    def stream(self, model: Model, context: Context, options: Any = None):
        return self._dispatch(model, context, options, simple=False)

    def streamSimple(self, model: Model, context: Context, options: Any = None):
        return self._dispatch(model, context, options, simple=True)


def radiusProvider(
    options: RadiusProviderOptions | None = None, *, load_config: Any = None
) -> _RadiusProvider:
    """Upstream's ``radiusProvider(options)``: a gateway-backed pi-messages provider."""
    return _RadiusProvider(options or RadiusProviderOptions(), load_config)


__all__ = [
    "DEFAULT_RADIUS_PROVIDER_ID",
    "DEFAULT_RADIUS_PROVIDER_NAME",
    "RADIUS_API_KEY_ENV",
    "RadiusProviderOptions",
    "radiusProvider",
]
