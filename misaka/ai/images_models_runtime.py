"""The image-generation runtime, translated from pi's ``packages/ai/src/images-models.ts``.

The image-side counterpart of ``ai/models_runtime.py``, and deliberately simpler: images
are one request rather than a stream, so there is no lazy stream, no publication chain and
no refresh generation. What it keeps from the chat side is the part that matters -- the
collection resolves auth and merges it into the request options, so a provider reached
through here is handed its key rather than looking one up. (``providers/images/openrouter.py``
still keeps its own env-key fallback for callers that reach it directly.)

Two behaviours differ from the chat runtime:

* ``generateImages`` catches ``Exception`` around the whole request path and returns an
  ``AssistantImages`` with ``stopReason="error"`` instead of propagating it.
* An **unconfigured provider is still called.** Chat refuses; images do not.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ConfigDict

from misaka.ai.auth.context import defaultProviderAuthContext
from misaka.ai.auth.credential_store import InMemoryCredentialStore
from misaka.ai.auth.resolve import (
    AuthResolutionOverrides,
    ModelsError,
    resolveProviderAuth,
)
from misaka.ai.auth.types import (
    AuthContext,
    AuthResult,
    CredentialStore,
    ProviderAuth,
    ProviderEnv,
    ProviderHeaders,
)
from misaka.ai.types import AssistantImages, ImagesContext, ImagesModel, ImagesOptions


class ImagesProvider(Protocol):
    """Identity, auth, model listing and generation behaviour."""

    id: str
    name: str
    auth: ProviderAuth

    def getModels(self) -> list[ImagesModel]:
        """Current known models. Must not raise; a raising provider has no models."""
        ...

    async def generateImages(
        self, model: ImagesModel, context: ImagesContext, options: Any = None
    ) -> AssistantImages: ...


class ImagesModelsImpl:
    """The image provider collection."""

    def __init__(
        self,
        credentials: CredentialStore | None = None,
        authContext: AuthContext | None = None,
    ) -> None:
        self._providers: dict[str, ImagesProvider] = {}
        self._credentials: CredentialStore = credentials or InMemoryCredentialStore()
        self._authContext: AuthContext = authContext or defaultProviderAuthContext()

    def setProvider(self, provider: ImagesProvider) -> None:
        self._providers[provider.id] = provider

    def deleteProvider(self, id: str) -> None:
        self._providers.pop(id, None)

    def clearProviders(self) -> None:
        self._providers.clear()

    def getProviders(self) -> list[ImagesProvider]:
        return list(self._providers.values())

    def getProvider(self, id: str) -> ImagesProvider | None:
        return self._providers.get(id)

    def getModels(self, provider: str | None = None) -> list[ImagesModel]:
        if provider is not None:
            entry = self._providers.get(provider)
            if entry is None:
                return []
            try:
                return list(entry.getModels())
            except Exception:  # noqa: BLE001 - an ill-behaved provider has no models
                return []

        models: list[ImagesModel] = []
        for entry in self._providers.values():
            models.extend(self.getModels(entry.id))
        return models

    def getModel(self, provider: str, id: str) -> ImagesModel | None:
        return next((model for model in self.getModels(provider) if model.id == id), None)

    async def refresh(self, provider: str | None = None) -> None:
        """One provider's refresh raises on failure; refreshing all is best-effort.

        The asymmetry is upstream's.
        """
        if provider is not None:
            entry = self._providers.get(provider)
            refreshModels = getattr(entry, "refreshModels", None) if entry else None
            if refreshModels is None:
                return
            try:
                await refreshModels()
            except ModelsError:
                raise
            except Exception as error:
                raise ModelsError(
                    "model_source", f"Model refresh failed for {provider}", error
                ) from error
            return

        async def refreshOne(entry: ImagesProvider) -> None:
            refreshModels = getattr(entry, "refreshModels", None)
            if refreshModels is not None:
                await refreshModels()

        await asyncio.gather(
            *(refreshOne(entry) for entry in self._providers.values()), return_exceptions=True
        )

    async def getAuth(
        self, providerOrModel: str | ImagesModel, overrides: AuthResolutionOverrides | None = None
    ) -> AuthResult | None:
        providerId = (
            providerOrModel if isinstance(providerOrModel, str) else providerOrModel.provider
        )
        provider = self._providers.get(providerId)
        if provider is None:
            return None
        return await resolveProviderAuth(
            provider, self._credentials, self._authContext, overrides or AuthResolutionOverrides()
        )

    async def generateImages(
        self, model: ImagesModel, context: ImagesContext, options: Any = None
    ) -> AssistantImages:
        """Generate through the owning provider, with auth resolved and merged."""
        try:
            provider = self._providers.get(model.provider)
            if provider is None:
                raise ModelsError("provider", f"Unknown provider: {model.provider}")

            resolution = await self.getAuth(
                model,
                AuthResolutionOverrides(
                    apiKey=_option(options, "apiKey"),
                    env=_option(options, "env"),
                    signal=_option(options, "signal"),
                ),
            )
            auth = resolution.auth if resolution is not None else None
            if auth is None:
                # Keyless providers are legitimate here; hand the request over untouched.
                return await provider.generateImages(model, context, options)

            requestModel = (
                model.model_copy(update={"baseUrl": auth.baseUrl}) if auth.baseUrl else model
            )
            optionHeaders: ProviderHeaders | None = _option(options, "headers")
            headers = (
                {**(auth.headers or {}), **(optionHeaders or {})}
                if (auth.headers or optionHeaders)
                else None
            )
            optionEnv: ProviderEnv | None = _option(options, "env")
            env = (
                {**((resolution.env if resolution else None) or {}), **(optionEnv or {})}
                if ((resolution.env if resolution else None) or optionEnv)
                else None
            )
            return await provider.generateImages(
                requestModel,
                context,
                _mergeRequestOptions(
                    options,
                    apiKey=_option(options, "apiKey") or auth.apiKey,
                    headers=headers,
                    env=env,
                ),
            )
        except Exception as error:  # noqa: BLE001 - the caller renders a result, not a raise
            return AssistantImages(
                api=model.api,
                provider=model.provider,
                model=model.id,
                output=[],
                stopReason="error",
                errorMessage=str(error),
                timestamp=int(time.time() * 1000),
            )


def _option(options: Any, name: str) -> Any:
    if options is None:
        return None
    if isinstance(options, dict):
        return options.get(name)
    return getattr(options, name, None)


class ImagesRequestOptions(ImagesOptions):
    """``ImagesOptions`` that can also carry the resolved provider env.

    ``ImagesOptions`` forbids extra keys and has no ``env`` field, but upstream's request
    options do carry one (``images-models.ts:212``), and a keyed gateway needs it. This
    subclass is the smallest way to keep both: every declared field stays where the image
    providers already read it, and ``env`` rides along instead of being dropped in silence.
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


def _mergeRequestOptions(
    options: Any, *, apiKey: str | None, headers: ProviderHeaders | None, env: ProviderEnv | None
) -> ImagesRequestOptions:
    """Fold resolved auth into the caller's options, as an object rather than a mapping.

    The image implementations read ``options.apiKey``, ``options.headers``,
    ``options.onPayload`` and so on (``providers/images/openrouter.py``), so a plain dict
    fails on the first attribute -- and the failure would be swallowed into an error
    result rather than raised. Fields are copied by attribute to mirror the chat side;
    here the choice is not load-bearing, since ``ImagesOptions`` declares no nested models
    and ``model_dump()`` returns ``signal``/``onPayload``/``onResponse`` by identity.
    """
    merged: dict[str, Any] = {}
    if options is not None:
        if isinstance(options, dict):
            merged = dict(options)
        else:
            for name in type(options).model_fields:
                merged[name] = getattr(options, name, None)
            merged.update(getattr(options, "model_extra", None) or {})
    merged.update({"apiKey": apiKey, "headers": headers, "env": env})
    return ImagesRequestOptions(**merged)


def createImagesModels(
    credentials: CredentialStore | None = None, authContext: AuthContext | None = None
) -> ImagesModelsImpl:
    return ImagesModelsImpl(credentials=credentials, authContext=authContext)


@dataclass(slots=True)
class CreateImagesProviderOptions:
    id: str
    auth: ProviderAuth
    api: Any
    models: Sequence[ImagesModel] = ()
    name: str | None = None
    refreshModels: Callable[[], Awaitable[Sequence[ImagesModel]]] | None = None


class _BuiltImagesProvider:
    def __init__(self, options: CreateImagesProviderOptions) -> None:
        self.id = options.id
        self.name = options.name or options.id
        self.auth = options.auth
        self._api = options.api
        self._models: list[ImagesModel] = list(options.models)
        self._fetch = options.refreshModels
        self._inflight: asyncio.Future[None] | None = None
        if options.refreshModels is None:
            self.refreshModels = None

    def getModels(self) -> list[ImagesModel]:
        return list(self._models)

    async def refreshModels(self) -> None:
        """Concurrent callers share one fetch; a failure leaves the last-known list.

        The in-flight future is stored before the first await, so four callers that all
        arrive at startup produce one request rather than four.
        """
        if self._fetch is None:
            return
        if self._inflight is None:
            self._inflight = asyncio.ensure_future(self._runRefresh())
        try:
            await self._inflight
        finally:
            if self._inflight is not None and self._inflight.done():
                self._inflight = None

    async def _runRefresh(self) -> None:
        assert self._fetch is not None
        self._models = list(await self._fetch())

    async def generateImages(
        self, model: ImagesModel, context: ImagesContext, options: Any = None
    ) -> AssistantImages:
        return await self._api.generateImages(model, context, options)


def createImagesProvider(options: CreateImagesProviderOptions) -> _BuiltImagesProvider:
    return _BuiltImagesProvider(options)


__all__ = [
    "CreateImagesProviderOptions",
    "ImagesProvider",
    "ImagesRequestOptions",
    "createImagesModels",
    "createImagesProvider",
]
