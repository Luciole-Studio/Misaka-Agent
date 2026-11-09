"""Translated from pi's ``packages/ai/src/models.ts`` (v1.3.0).

MISAKA fork (each is marked where it happens): providers, models and options are Python objects, so a provider's
optional members are read with ``getattr``, options are read whether they are mappings or objects, and resolved auth
is folded into an options object (``_mergeRequestOptions``) rather than a spread; ``ModelsPublication`` says whether
``persist`` was given (``persist_is_set``), because Python has one word for ``undefined`` and ``null``; ``login`` takes
a ``credentialId`` keyword for MISAKA's per-account credential records.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Protocol, TypedDict

from misaka.ai.api.lazy import lazyStream
from misaka.ai.auth.context import defaultProviderAuthContext as defaultAuthContext
from misaka.ai.auth.credential_store import InMemoryCredentialStore
from misaka.ai.auth.resolve import (
    AuthResolutionOverrides,
    ModelsError,
    refreshStoredOAuthCredential,
    resolveProviderAuth,
)
from misaka.ai.auth.types import (
    ApiKeyCredential,
    AuthCheck,
    AuthContext,
    AuthInteraction,
    AuthOperationOptions,
    AuthResult,
    AuthType,
    Credential,
    CredentialStore,
    LoginOptions,
    ProviderAuth,
)
from misaka.ai.models_store import (
    InMemoryModelsStore,
    ModelsStore,
    ModelsStoreEntry,
    ModelsStoreOperationOptions,
)
from misaka.ai.types import (
    AnyModel,
    AssistantImages,
    AssistantMessage,
    ClassifierContext,
    ClassifierModel,
    ClassifierResult,
    Context,
    DeferredHandle,
    ImageModel,
    ImagesContext,
    Model,
    ModelCostRates,
    ModelThinkingLevel,
    ModelType,
    ProviderClassifier,
    ProviderClassifierOptions,
    ProviderEnv,
    ProviderHeaders,
    ProviderImages,
    ProviderImagesOptions,
    ProviderStreamOptions,
    ProviderStreams,
    TranscriptContext,
    Usage,
    UsageCost,
)
from misaka.ai.utils.abort import _abortReason, operationSignal, raceWithAbortSignal
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.model_operations import (
    assertChatModel,
    assertClassifierInputSupported,
    assertClassifierModel,
    assertImageModel,
    classifierErrorResult,
    getModelType,
    imageErrorResult,
    isModelType,
)
from misaka.ai.utils.models_error import ModelsErrorCode
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import (
    AbortController,
    AbortSignal,
    addAbortListener,
    throwIfAborted,
)
from misaka.utils.values import (
    call_with_optional_second_arg,
    read_field,
    signal_aborted,
)


@dataclass(slots=True)
class ModelsPublication:
    # Provider-selected persisted catalog. Omit to leave storage unchanged; null deletes it.
    persist: ModelsStoreEntry | None = None
    # MISAKA fork: whether `persist` was given; `persist=None` deletes only when it was.
    persist_is_set: bool = False
    # Optional synchronous update of provider-private in-memory catalog state.
    update: Callable[[], None] | None = None


@dataclass(slots=True, kw_only=True)
class RefreshModelsContext:
    # Effective configured credential. OAuth credentials are refreshed before network access.
    credential: Credential | None = None
    # Immutable provider-scoped catalog snapshot captured before this refresh phase.
    stored: ModelsStoreEntry | None = None
    # Generation-checked publication. Persistence policy remains provider-owned;
    # the update runs synchronously only after the selected persistence mutation.
    publish: Callable[[ModelsPublication], Awaitable[bool]]
    # False during offline/cache-only initialization.
    allowNetwork: bool
    # Bypass provider freshness checks and fetch immediately when network access is allowed.
    force: bool | None = None
    # Always present, including when the public refresh caller omits its optional signal.
    signal: Any
    # Base URL of the registered provider, including configuration overrides that wrappers apply
    # outside the provider receiving this context.
    baseUrl: str | None = None


@dataclass(slots=True)
class ModelsRefreshOptions:
    allowNetwork: bool | None = None
    # Restrict refresh to these provider IDs. Unknown and static providers are ignored.
    providers: Sequence[str] | None = None
    # Bypass provider freshness checks and fetch immediately when network access is allowed.
    force: bool | None = None
    signal: Any = None


@dataclass(slots=True)
class ModelsRefreshResult:
    aborted: bool
    errors: dict[str, BaseException] = field(default_factory=dict)


class ModelsRequestTransforms(TypedDict, total=False):
    # Transform fully assembled model/auth/request headers before provider dispatch.
    transformHeaders: Callable[[ProviderHeaders], ProviderHeaders | Awaitable[ProviderHeaders]]


# MISAKA fork: the request options are open objects or mappings (`ApiStreamOptions<TApi> & ModelsRequestTransforms`
# and its siblings are intersections of closed pydantic option classes).
type ModelsApiStreamOptions = Any
type ModelsSimpleStreamOptions = Any
type ModelsDeferredFetchOptions = Any
type ModelsDeferredCancelOptions = Any
type ModelsImagesOptions = Any
type ModelsClassifierOptions = Any

_KNOWN_MODEL_TYPES: dict[ModelType, bool] = {"chat": True, "image": True, "classifier": True}


def _hasKnownModelType(model: AnyModel) -> bool:
    """Models from stores and remote sources may have types that only newer versions know."""
    modelType = getModelType(model)
    # MISAKA fork: `Object.hasOwn` coerces any key to a string and never throws; a stored type
    # that is not a string (a list, an object) is unknown here instead of unhashable.
    return isinstance(modelType, str) and modelType in _KNOWN_MODEL_TYPES


def _withKnownModelTypes(entry: ModelsStoreEntry) -> ModelsStoreEntry:
    """Drops stored models whose type this version does not know."""
    return entry.model_copy(update={"models": [model for model in entry.models if _hasKnownModelType(model)]})



class Provider(Protocol):
    """A provider is the concrete runtime unit. It owns id/name/base metadata,
    auth methods, model listing, and the operations its models support
    (streaming, image generation, classification).

    MISAKA fork: a Protocol cannot mark members optional, so ``Models`` reads these with ``getattr``:

    - ``baseUrl``, ``headers``.
    - ``getAllModels()``: current known models of every type, sync, with the same contract as
      ``getModels()``. Providers with only chat models may omit it; ``Models`` then uses
      ``getModels()``. Model ids are unique within each type; one upstream model may have separate
      entries for different operations.
    - ``refreshModels(context)``: dynamic providers only: restore ``context.stored`` and optionally
      fetch a newer list using the effective credential. Implementations retain their previous list
      on failure, publish persistence and synchronous state changes through ``context.publish()``,
      and honor the shared abort signal for blocking work.
    - ``filterModels(models, credential)``: optional provider policy for credential-specific model
      availability. ``getModels()`` remains the complete synchronous chat catalog;
      ``Models.getAvailable()`` applies this filter after confirming that provider auth is configured.
    - ``filterAllModels(models, credential)``: optional credential-specific availability policy across
      every model type. Without it, ``Models.getAllAvailable()`` applies ``filterModels`` to chat models
      and keeps every other model.
    - ``fetchDeferred(model, handle, options)``, ``cancelDeferred(model, handle, options)``.
    - ``generateImages(model, context, options)``: present when the provider supports dedicated image
      models. Never rejects.
    - ``classify(model, context, options)``: present when the provider supports structured classifier
      models. Never rejects.
    """

    id: str
    name: str

    # Required: at least one of `apiKey`/`oauth`. Every provider has auth
    # semantics — even providers with only ambient credentials (env vars, AWS
    # profiles, ADC files) and keyless local servers provide `apiKey` auth
    # whose `resolve()` reports whether the provider is configured.
    # `Models.getAuth()` returns undefined when the provider is unconfigured.
    auth: ProviderAuth

    def getModels(self) -> Sequence[Model]:
        """Current known chat models, sync. Static providers return their catalog;
        dynamic providers return the list as of the last `refreshModels()` (empty
        before the first). Must not throw; `Models` treats a throwing
        implementation as having no models.
        """
        ...

    def stream(self, model: Model, context: TranscriptContext, options: Any = None) -> AssistantMessageEventStream:
        """Stream a normalized transcript. `Models` normalizes the caller's `Context` before dispatching here."""
        ...

    def streamSimple(
        self, model: Model, context: TranscriptContext, options: Any = None
    ) -> AssistantMessageEventStream: ...


class Models(Protocol):
    """Runtime collection of providers plus auth application and request
    convenience. Providers own request behavior; `Models` resolves auth and
    delegates each request to the provider that owns the model.

    Read accessors come in three flavors: the unqualified ones (`getModels`,
    `getModel`, `getAvailable`) return chat models, the `*OfType` accessors
    return one model type, and `getAllModels`/`getAllAvailable` return every type.
    """

    def getProviders(self) -> list[Provider]: ...

    def getProvider(self, id: str) -> Provider | None: ...

    def getModels(self, provider: str | None = None) -> list[Model]:
        """Sync read of last-known chat models from one provider or all providers.
        Best-effort: a provider whose `getModels()` throws yields no models.
        """
        ...

    def getModel(self, provider: str, id: str) -> Model | None:
        """Sync runtime chat model lookup against last-known lists. Dynamic model lists
        are typed as `Model<Api>`; narrow with the `hasApi()` type guard.
        """
        ...

    def getModelsOfType(self, type: ModelType, provider: str | None = None) -> list[Any]:
        """Sync read of last-known models of one type from one provider or all providers."""
        ...

    def getModelOfType(self, type: ModelType, provider: str, id: str) -> Any:
        """Sync runtime lookup of a model of one type against last-known lists."""
        ...

    def getAllModels(self, provider: str | None = None) -> list[AnyModel]:
        """Sync read of last-known models of every type from one provider or all providers."""
        ...

    async def refresh(self, options: ModelsRefreshOptions | None = None) -> ModelsRefreshResult:
        """Refresh selected configured dynamic providers concurrently (all when `providers` is omitted).
        Provider errors and cancellation are returned without rejecting; static, unknown, and
        unconfigured providers are skipped.
        """
        ...

    async def checkAuth(self, providerId: str, options: AuthOperationOptions | None = None) -> AuthCheck | None:
        """Check whether a provider has complete auth configuration without refreshing OAuth."""
        ...

    async def getAvailable(
        self, providerId: str | None = None, options: AuthOperationOptions | None = None
    ) -> list[Model]:
        """Return chat models whose providers have complete auth configuration."""
        ...

    async def getAvailableOfType(
        self, type: ModelType, providerId: str | None = None, options: AuthOperationOptions | None = None
    ) -> list[Any]:
        """Return models of one type whose providers have complete auth configuration."""
        ...

    async def getAllAvailable(
        self, providerId: str | None = None, options: AuthOperationOptions | None = None
    ) -> list[AnyModel]:
        """Return models of every type whose providers have complete auth configuration."""
        ...

    async def getAuth(
        self, providerOrModel: str | AnyModel, overrides: AuthResolutionOverrides | None = None
    ) -> AuthResult | None:
        """Resolve provider-scoped auth by provider id, or provider auth plus static
        model headers when passed a model. Includes a source label for status UI.
        Resolves `undefined` when the provider is unknown or unconfigured.
        Rejects with `ModelsError`: code "oauth" when a token refresh fails (the
        stored credential is preserved for retry; re-login fixes it), code "auth"
        when api-key resolution or the credential store fails. Request paths
        surface rejections as stream errors.
        """
        ...

    async def login(
        self, providerId: str, type: AuthType, interaction: AuthInteraction, options: LoginOptions | None = None
    ) -> Credential:
        """Run a provider-owned login flow and persist its returned credential."""
        ...

    async def logout(self, providerId: str, options: AuthOperationOptions | None = None) -> None:
        """Remove the stored credential for a provider."""
        ...

    def stream(
        self, model: Model, context: Context, options: ModelsApiStreamOptions = None
    ) -> AssistantMessageEventStream: ...

    async def complete(self, model: Model, context: Context, options: ModelsApiStreamOptions = None) -> AssistantMessage: ...

    def streamSimple(
        self, model: Model, context: Context, options: ModelsSimpleStreamOptions = None
    ) -> AssistantMessageEventStream: ...

    async def completeSimple(
        self, model: Model, context: Context, options: ModelsSimpleStreamOptions = None
    ) -> AssistantMessage: ...

    def streamDeferred(
        self, model: Model, handle: DeferredHandle, options: ModelsDeferredFetchOptions = None
    ) -> AssistantMessageEventStream: ...

    async def fetchDeferred(
        self, model: Model, handle: DeferredHandle, options: ModelsDeferredFetchOptions = None
    ) -> AssistantMessage: ...

    async def cancelDeferred(
        self, model: Model, handle: DeferredHandle, options: ModelsDeferredCancelOptions = None
    ) -> None: ...

    async def generateImages(
        self, model: ImageModel, context: ImagesContext, options: ModelsImagesOptions = None
    ) -> AssistantImages:
        """Generate images through the owning provider with auth resolved like
        `stream()`. Never rejects: unknown providers, unconfigured auth, and
        providers without `generateImages` return an error `AssistantImages`.
        """
        ...

    async def classify(
        self, model: ClassifierModel, context: ClassifierContext, options: ModelsClassifierOptions = None
    ) -> ClassifierResult:
        """Classify structured state through the owning provider. Never rejects."""
        ...


class MutableModels(Models, Protocol):
    def setProvider(self, provider: Provider) -> None:
        """Upsert/replace by provider.id. Provider ids are unique."""
        ...

    def deleteProvider(self, id: str) -> None: ...

    def clearProviders(self) -> None: ...


@dataclass(slots=True, kw_only=True)
class CreateModelsOptions:
    credentials: CredentialStore | None = None
    modelsStore: ModelsStore | None = None
    authContext: AuthContext | None = None


def _mergeHeaders(base: ProviderHeaders | None, override: ProviderHeaders | None) -> ProviderHeaders | None:
    # MISAKA fork: `!base && !override` is "both absent": an empty object is truthy in JavaScript, falsy in Python.
    if base is None and override is None:
        return None
    merged: ProviderHeaders = {**(base or {})}
    for name, value in (override or {}).items():
        lowerName = name.lower()
        for existingName in [key for key in merged if key.lower() == lowerName]:
            del merged[existingName]
        merged[name] = value
    return merged


class _ModelsImpl:
    def __init__(self, options: CreateModelsOptions | Mapping[str, Any] | None = None) -> None:
        self._providers: dict[str, Provider] = {}
        self._refreshGenerations: dict[str, int] = {}
        self._refreshControllers: dict[str, AbortController] = {}
        self._publicationChains: dict[str, asyncio.Future[None]] = {}
        credentials = read_field(options, "credentials")
        modelsStore = read_field(options, "modelsStore")
        authContext = read_field(options, "authContext")
        self._credentials: CredentialStore = credentials if credentials is not None else InMemoryCredentialStore()
        self._modelsStore: ModelsStore = modelsStore if modelsStore is not None else InMemoryModelsStore()
        self._authContext: AuthContext = authContext if authContext is not None else defaultAuthContext()

    def setProvider(self, provider: Provider) -> None:
        self._supersedeProviderRefresh(provider.id)
        self._providers[provider.id] = provider

    def deleteProvider(self, id: str) -> None:
        self._supersedeProviderRefresh(id)
        self._providers.pop(id, None)

    def clearProviders(self) -> None:
        for id in dict.fromkeys([*self._providers, *self._refreshControllers]):
            self._supersedeProviderRefresh(id)
        self._providers.clear()

    def getProviders(self) -> list[Provider]:
        return list(self._providers.values())

    def getProvider(self, id: str) -> Provider | None:
        return self._providers.get(id)

    def getModels(self, provider: str | None = None) -> list[Model]:
        if provider is not None:
            entry = self._providers.get(provider)
            if not entry:
                return []
            try:
                return list(entry.getModels())
            except Exception:  # noqa: BLE001 - `catch { return []; }`
                return []

        models: list[Model] = []
        for entry in self._providers.values():
            try:
                models.extend(entry.getModels())
            except Exception:  # noqa: BLE001, S110 - best-effort: ill-behaved providers yield no models
                pass
        return models

    def getAllModels(self, provider: str | None = None) -> list[AnyModel]:
        if provider is not None:
            entry = self._providers.get(provider)
            if not entry:
                return []
            try:
                return list(_allModelsOf(entry))
            except Exception:  # noqa: BLE001 - `catch { return []; }`
                return []

        models: list[AnyModel] = []
        for entry in self._providers.values():
            try:
                models.extend(_allModelsOf(entry))
            except Exception:  # noqa: BLE001, S110 - best-effort: ill-behaved providers yield no models
                pass
        return models

    def getModelsOfType(self, type: ModelType, provider: str | None = None) -> list[Any]:
        return [model for model in self.getAllModels(provider) if isModelType(model, type)]

    def getModel(self, provider: str, id: str) -> Model | None:
        return next((model for model in self.getModels(provider) if model.id == id), None)

    def getModelOfType(self, type: ModelType, provider: str, id: str) -> Any:
        return next((model for model in self.getModelsOfType(type, provider) if model.id == id), None)

    def _supersedeProviderRefresh(self, providerId: str) -> int:
        generation = self._refreshGenerations.get(providerId, 0) + 1
        self._refreshGenerations[providerId] = generation
        previous = self._refreshControllers.get(providerId)
        if previous:
            del self._refreshControllers[providerId]
            previous.abort()
        return generation

    def _beginProviderRefresh(self, providerId: str) -> tuple[int, AbortController]:
        generation = self._supersedeProviderRefresh(providerId)
        controller = AbortController()
        self._refreshControllers[providerId] = controller
        return generation, controller

    def _publishProviderModels(
        self,
        providerId: str,
        generation: int,
        signal: Any,
        publication: ModelsPublication,
    ) -> asyncio.Future[bool]:
        previous = self._publicationChains.get(providerId)

        async def run() -> bool:
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            if signal_aborted(signal) or self._refreshGenerations.get(providerId) != generation:
                return False

            # MISAKA fork: `persist === null` / `!== undefined` is `persist_is_set` (see ModelsPublication).
            if publication.persist_is_set and publication.persist is None:
                await self._modelsStore.delete(providerId, ModelsStoreOperationOptions(signal=signal))
            elif publication.persist_is_set:
                await self._modelsStore.write(
                    providerId, publication.persist.model_copy(deep=True), ModelsStoreOperationOptions(signal=signal)
                )

            if signal_aborted(signal) or self._refreshGenerations.get(providerId) != generation:
                return False
            if publication.update is not None:
                publication.update()
            return True

        queued = asyncio.ensure_future(run())
        tail: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def settle(done: asyncio.Future[bool]) -> None:
            if not done.cancelled():
                done.exception()
            tail.set_result(None)
            if self._publicationChains.get(providerId) is tail:
                del self._publicationChains[providerId]

        queued.add_done_callback(settle)
        self._publicationChains[providerId] = tail
        # MISAKA fork: shielded, so a cancelled caller abandons the publication as an aborted one does, without
        # cancelling it under the chain (a promise cannot be cancelled).
        return raceWithAbortSignal(asyncio.shield(queued), signal)

    async def _runProviderRefreshPhase(
        self,
        provider: Provider,
        credential: Credential | None,
        allowNetwork: bool,
        force: bool | None,
        generation: int,
        signal: Any,
    ) -> None:
        stored = await self._modelsStore.read(provider.id, ModelsStoreOperationOptions(signal=signal))
        await provider.refreshModels(  # type: ignore[attr-defined]
            RefreshModelsContext(
                credential=credential,
                stored=_withKnownModelTypes(stored.model_copy(deep=True)) if stored else None,
                publish=lambda publication: self._publishProviderModels(provider.id, generation, signal, publication),
                allowNetwork=allowNetwork,
                force=force if allowNetwork else None,
                signal=signal,
                # MISAKA fork: `getattr`, since a provider's optional members may be absent (module docstring).
                baseUrl=getattr(provider, "baseUrl", None),
            )
        )

    async def refresh(self, options: ModelsRefreshOptions | None = None) -> ModelsRefreshResult:
        options = options if options is not None else ModelsRefreshOptions()
        allowNetwork = options.allowNetwork if options.allowNetwork is not None else True
        callerSignal = operationSignal(options.signal)
        errors: dict[str, BaseException] = {}
        if signal_aborted(callerSignal):
            return ModelsRefreshResult(aborted=True, errors=errors)
        selected = set(options.providers) if options.providers is not None else None
        refreshable = [
            provider
            for provider in self._providers.values()
            if getattr(provider, "refreshModels", None) is not None and (not selected or provider.id in selected)
        ]

        async def refreshOne(provider: Provider) -> None:
            generation, controller = self._beginProviderRefresh(provider.id)
            signal = AbortSignal.any([callerSignal, controller.signal])

            async def operation() -> None:
                storedCredential: Credential | None = None
                credentialError: BaseException | None = None
                try:
                    storedCredential = await self._readCredential(provider.id, signal)
                except Exception as error:  # noqa: BLE001 - rethrown after the cache restore
                    credentialError = error

                # Restore cached provider state before auth resolution or network access.
                await self._runProviderRefreshPhase(provider, storedCredential, False, None, generation, signal)
                if credentialError is not None:
                    raise credentialError
                if not allowNetwork or signal.aborted:
                    return

                credential = await self._resolveRefreshCredential(provider, storedCredential, signal)
                if not credential:
                    return
                await self._runProviderRefreshPhase(provider, credential, True, options.force, generation, signal)

            try:
                await raceWithAbortSignal(operation(), signal)
            except Exception as error:  # noqa: BLE001 - collected, never raised out of refresh
                if not signal.aborted:
                    errors[provider.id] = error
            finally:
                if self._refreshControllers.get(provider.id) is controller:
                    del self._refreshControllers[provider.id]

        # MISAKA fork: each provider's refresh starts eagerly, running up to its first suspension as the JavaScript
        # `map(async ...)` does, so its generation begins before refresh() yields.
        loop = asyncio.get_running_loop()
        refresh = asyncio.gather(
            *(asyncio.Task(refreshOne(provider), loop=loop, eager_start=True) for provider in refreshable)
        )

        try:
            await raceWithAbortSignal(refresh, callerSignal)
        except Exception:
            if not signal_aborted(callerSignal):
                raise

        return ModelsRefreshResult(aborted=signal_aborted(callerSignal), errors=dict(errors))

    async def _resolveRefreshCredential(
        self,
        provider: Provider,
        stored: Credential | None,
        signal: Any,
    ) -> Credential | None:
        if read_field(stored, "type") == "oauth":
            oauth = provider.auth.oauth
            if not oauth:
                return None
            if time.time() * 1000 < stored.expires:  # type: ignore[union-attr]
                return stored
            if signal_aborted(signal):
                return None
            # A refresh that has started survives cancellation or a superseding model refresh, so a
            # rotated refresh token is always persisted. A newer refresh then sees the fresh credential.
            return await refreshStoredOAuthCredential(
                self._credentials,
                provider.id,
                oauth,
                lambda current: time.time() * 1000 >= current.expires,
                signal,
            )

        apiKey = provider.auth.apiKey
        if not apiKey:
            return None
        credential = stored if read_field(stored, "type") == "api_key" else None
        result = await apiKey.resolve(ctx=self._authContext, credential=credential, signal=signal)
        if not result:
            return None
        return ApiKeyCredential(type="api_key", key=result.auth.apiKey, env=result.env)

    async def _readCredential(self, providerId: str, signal: Any) -> Credential | None:
        try:
            return await self._credentials.read(providerId, AuthOperationOptions(signal=signal))
        except Exception as error:
            raise ModelsError("auth", f"Credential store read failed for {providerId}", {"cause": error}) from error

    async def _checkProviderAuth(
        self,
        provider: Provider,
        credential: Credential | None,
        signal: Any,
    ) -> AuthCheck | None:
        if read_field(credential, "type") == "oauth":
            return AuthCheck(source="OAuth", type="oauth") if provider.auth.oauth else None
        apiKey = provider.auth.apiKey
        if not apiKey:
            return None
        if apiKey.check:
            try:
                return await apiKey.check(
                    ctx=self._authContext,
                    credential=credential if read_field(credential, "type") == "api_key" else None,
                    signal=signal,
                )
            except Exception as error:
                raise ModelsError(
                    "auth", f"API key auth check failed for provider {provider.id}", {"cause": error}
                ) from error

        resolution = await resolveProviderAuth(
            provider, self._credentials, self._authContext, AuthResolutionOverrides(signal=signal)
        )
        return AuthCheck(source=resolution.source, type="api_key") if resolution else None

    def checkAuth(self, providerId: str, options: AuthOperationOptions | None = None) -> asyncio.Future[AuthCheck | None]:
        signal = operationSignal(read_field(options, "signal"))

        async def check() -> AuthCheck | None:
            throwIfAborted(signal)
            provider = self._providers.get(providerId)
            if not provider:
                return None
            return await self._checkProviderAuth(provider, await self._readCredential(providerId, signal), signal)

        return raceWithAbortSignal(check(), signal)

    async def _getAuthenticatedProviders(
        self, providerId: str | None, signal: Any
    ) -> list[tuple[Provider, Credential | None, AuthCheck]]:
        throwIfAborted(signal)
        providers = (
            [entry for entry in [self._providers.get(providerId)] if entry is not None]
            if providerId
            else self.getProviders()
        )

        async def checkOne(provider: Provider) -> tuple[Provider, Credential | None, AuthCheck | None]:
            credential = await self._readCredential(provider.id, signal)
            return provider, credential, await self._checkProviderAuth(provider, credential, signal)

        checks = await asyncio.gather(*(checkOne(provider) for provider in providers))
        return [(provider, credential, auth) for provider, credential, auth in checks if auth is not None]

    def getAvailable(
        self, providerId: str | None = None, options: AuthOperationOptions | None = None
    ) -> asyncio.Future[list[Model]]:
        signal = operationSignal(read_field(options, "signal"))

        async def available() -> list[Model]:
            providers = await self._getAuthenticatedProviders(providerId, signal)
            models: list[Model] = []
            for provider, credential, _auth in providers:
                providerModels = provider.getModels()
                filterModels = getattr(provider, "filterModels", None)
                filtered = filterModels(providerModels, credential) if filterModels else None
                models.extend(filtered if filtered is not None else providerModels)
            return models

        return raceWithAbortSignal(available(), signal)

    async def getAvailableOfType(
        self,
        type: ModelType,
        providerId: str | None = None,
        options: AuthOperationOptions | None = None,
    ) -> list[Any]:
        return [model for model in await self.getAllAvailable(providerId, options) if isModelType(model, type)]

    def getAllAvailable(
        self, providerId: str | None = None, options: AuthOperationOptions | None = None
    ) -> asyncio.Future[list[AnyModel]]:
        signal = operationSignal(read_field(options, "signal"))

        async def available() -> list[AnyModel]:
            providers = await self._getAuthenticatedProviders(providerId, signal)
            models: list[AnyModel] = []
            for provider, credential, _auth in providers:
                providerModels = list(_allModelsOf(provider))
                filterAllModels = getattr(provider, "filterAllModels", None)
                filterModels = getattr(provider, "filterModels", None)
                if filterAllModels:
                    models.extend(filterAllModels(providerModels, credential))
                    continue
                if not filterModels:
                    models.extend(providerModels)
                    continue
                availableChatIds = {model.id for model in filterModels(provider.getModels(), credential)}
                models.extend(
                    model for model in providerModels if not isModelType(model, "chat") or model.id in availableChatIds
                )
            return models

        return raceWithAbortSignal(available(), signal)

    async def getAuth(
        self,
        providerOrModel: str | AnyModel,
        overrides: AuthResolutionOverrides | None = None,
    ) -> AuthResult | None:
        signal = operationSignal(read_field(overrides, "signal"))
        providerId = providerOrModel if isinstance(providerOrModel, str) else providerOrModel.provider
        provider = self._providers.get(providerId)
        if not provider:
            return None
        result = await resolveProviderAuth(
            provider,
            self._credentials,
            self._authContext,
            AuthResolutionOverrides(
                apiKey=read_field(overrides, "apiKey"),
                env=read_field(overrides, "env"),
                minOAuthValidityMs=read_field(overrides, "minOAuthValidityMs"),
                signal=signal,
            ),
        )
        if not result or isinstance(providerOrModel, str) or providerOrModel.headers is None:
            return result
        return result.model_copy(
            update={
                "auth": result.auth.model_copy(
                    update={"headers": _mergeHeaders(result.auth.headers, providerOrModel.headers)}
                ),
            }
        )

    async def login(
        self,
        providerId: str,
        type: AuthType,
        interaction: AuthInteraction,
        options: LoginOptions | None = None,
        *,
        credentialId: str | None = None,
    ) -> Credential:
        # MISAKA fork: `credentialId` stores the credential under a named record of this store (per-account login).
        signal = operationSignal(read_field(interaction, "signal"))
        throwIfAborted(signal)
        provider = self._providers.get(providerId)
        if not provider:
            raise ModelsError("provider", f"Unknown provider: {providerId}")
        method = provider.auth.oauth if type == "oauth" else provider.auth.apiKey
        if not getattr(method, "login", None):
            raise ModelsError("auth", f"{provider.name} does not support {type} login")
        # MISAKA fork: `{...interaction, signal}` copies the interaction's members; a login that takes the interaction
        # only is called with that alone (a JavaScript function ignores an argument it does not declare).
        loginOperation = call_with_optional_second_arg(
            method.login, _withSignal(interaction, signal), options  # type: ignore[union-attr]
        )
        credential = await raceWithAbortSignal(loginOperation, signal)
        mutationStarted = False
        started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def persist(_current: Credential | None) -> Credential:
            nonlocal mutationStarted
            mutationStarted = True
            if not started.done():
                started.set_result(None)
            return credential

        mutation = asyncio.ensure_future(
            self._credentials.modify(
                credentialId if credentialId is not None else providerId,
                persist,
                AuthOperationOptions(signal=signal),
            )
        )
        mutation.add_done_callback(lambda done: done.cancelled() or done.exception())
        try:
            waiting: asyncio.Future[None] = asyncio.get_running_loop().create_future()

            def onAbort() -> None:
                if not mutationStarted and not waiting.done():
                    waiting.set_exception(_abortReason(signal))

            removeAbortListener = addAbortListener(signal, onAbort)

            def onSettled(done: asyncio.Future[Any]) -> None:
                removeAbortListener()
                if waiting.done():
                    return
                if done.cancelled():
                    waiting.cancel()
                elif done is mutation and done.exception() is not None:
                    waiting.set_exception(done.exception())  # type: ignore[arg-type]
                else:
                    waiting.set_result(None)

            started.add_done_callback(onSettled)
            mutation.add_done_callback(onSettled)
            if signal_aborted(signal):
                onAbort()
            await waiting
            await mutation
        except Exception as error:
            throwIfAborted(signal)
            raise ModelsError("auth", f"Credential store modify failed for {providerId}", {"cause": error}) from error
        return credential

    async def logout(self, providerId: str, options: AuthOperationOptions | None = None) -> None:
        signal = operationSignal(read_field(options, "signal"))
        throwIfAborted(signal)
        try:
            await self._credentials.delete(providerId, AuthOperationOptions(signal=signal))
        except Exception as error:
            throwIfAborted(signal)
            raise ModelsError("auth", f"Credential store delete failed for {providerId}", {"cause": error}) from error

    def _requireProvider(self, model: AnyModel) -> Provider:
        provider = self._providers.get(model.provider)
        if not provider:
            raise ModelsError("provider", f"Unknown provider: {model.provider}")
        return provider

    def _requireChatProvider(self, model: Model) -> Provider:
        assertChatModel(model)
        return self._requireProvider(model)

    async def _applyAuth(self, model: Any, options: Any, into: type = ProviderStreamOptions) -> tuple[Any, Any]:
        # MISAKA fork: `into` is the options class the operation's implementation reads.
        self._requireProvider(model)
        resolution = await self.getAuth(
            model,
            AuthResolutionOverrides(
                apiKey=read_field(options, "apiKey"),
                env=read_field(options, "env"),
                signal=read_field(options, "signal"),
            ),
        )
        if not resolution:
            raise ModelsError("auth", f"Provider is not configured: {model.provider}")
        auth = resolution.auth

        # Explicit request options win per-field; the Models-only transform runs last.
        optionApiKey = read_field(options, "apiKey")
        apiKey = optionApiKey if optionApiKey is not None else auth.apiKey
        headers = _mergeHeaders(auth.headers, read_field(options, "headers"))
        transformHeaders = read_field(options, "transformHeaders")
        if transformHeaders:
            produced = transformHeaders(headers if headers is not None else {})
            headers = await produced if inspect.isawaitable(produced) else produced
        optionEnv: ProviderEnv | None = read_field(options, "env")
        env = {**(resolution.env or {}), **(optionEnv or {})} if resolution.env is not None or optionEnv is not None else None
        requestModel = model.model_copy(update={"baseUrl": auth.baseUrl}) if auth.baseUrl else model
        requestOptions = _mergeRequestOptions(options, apiKey=apiKey, headers=headers, env=env, into=into)

        return requestModel, requestOptions

    def stream(self, model: Model, context: Context, options: ModelsApiStreamOptions = None) -> AssistantMessageEventStream:
        transcript = normalizeContext(context)

        async def setup() -> AssistantMessageEventStream:
            provider = self._requireChatProvider(model)
            requestModel, requestOptions = await self._applyAuth(model, options)
            return provider.stream(requestModel, transcript, requestOptions)

        return lazyStream(model, setup)

    async def complete(self, model: Model, context: Context, options: ModelsApiStreamOptions = None) -> AssistantMessage:
        return await self.stream(model, context, options).result()

    def streamSimple(
        self, model: Model, context: Context, options: ModelsSimpleStreamOptions = None
    ) -> AssistantMessageEventStream:
        transcript = normalizeContext(context)

        async def setup() -> AssistantMessageEventStream:
            provider = self._requireChatProvider(model)
            requestModel, requestOptions = await self._applyAuth(model, options)
            return provider.streamSimple(requestModel, transcript, requestOptions)

        return lazyStream(model, setup)

    async def completeSimple(
        self,
        model: Model,
        context: Context,
        options: ModelsSimpleStreamOptions = None,
    ) -> AssistantMessage:
        return await self.streamSimple(model, context, options).result()

    def streamDeferred(
        self,
        model: Model,
        handle: DeferredHandle,
        options: ModelsDeferredFetchOptions = None,
    ) -> AssistantMessageEventStream:
        async def setup() -> AssistantMessageEventStream:
            provider = self._requireChatProvider(model)
            fetchDeferred = getattr(provider, "fetchDeferred", None)
            if not fetchDeferred:
                raise ModelsError("provider", f"Provider {model.provider} does not support deferred responses")
            requestModel, requestOptions = await self._applyAuth(model, options)
            return fetchDeferred(requestModel, handle, requestOptions)

        return lazyStream(model, setup)

    async def fetchDeferred(
        self,
        model: Model,
        handle: DeferredHandle,
        options: ModelsDeferredFetchOptions = None,
    ) -> AssistantMessage:
        return await self.streamDeferred(model, handle, options).result()

    async def cancelDeferred(
        self,
        model: Model,
        handle: DeferredHandle,
        options: ModelsDeferredCancelOptions = None,
    ) -> None:
        provider = self._requireChatProvider(model)
        cancelDeferred = getattr(provider, "cancelDeferred", None)
        if not cancelDeferred:
            raise ModelsError("provider", f"Provider {model.provider} does not support deferred responses")
        requestModel, requestOptions = await self._applyAuth(model, options)
        await cancelDeferred(requestModel, handle, requestOptions)

    async def generateImages(
        self,
        model: ImageModel,
        context: ImagesContext,
        options: ModelsImagesOptions = None,
    ) -> AssistantImages:
        try:
            assertImageModel(model)
            provider = self._requireProvider(model)
            generateImages = getattr(provider, "generateImages", None)
            if not generateImages:
                raise ModelsError("provider", f"Provider {model.provider} does not support image generation")
            requestModel, requestOptions = await self._applyAuth(model, options, ProviderImagesOptions)
            return await generateImages(requestModel, context, requestOptions)
        except Exception as error:  # noqa: BLE001 - `catch (error)`: reported as the result
            return imageErrorResult(model, error, signal_aborted(read_field(options, "signal")))

    async def classify(
        self,
        model: ClassifierModel,
        context: ClassifierContext,
        options: ModelsClassifierOptions = None,
    ) -> ClassifierResult:
        try:
            assertClassifierModel(model)
            assertClassifierInputSupported(model, context)
            provider = self._requireProvider(model)
            classify = getattr(provider, "classify", None)
            if not classify:
                raise ModelsError("provider", f"Provider {model.provider} does not support classification")
            requestModel, requestOptions = await self._applyAuth(model, options, ProviderClassifierOptions)
            return await classify(requestModel, context, requestOptions)
        except Exception as error:  # noqa: BLE001 - `catch (error)`: reported as the result
            return classifierErrorResult(model, error, signal_aborted(read_field(options, "signal")))


def _allModelsOf(provider: Any) -> Sequence[AnyModel]:
    """``provider.getAllModels?.() ?? provider.getModels()``."""
    getAllModels = getattr(provider, "getAllModels", None)
    models = getAllModels() if getAllModels is not None else None
    return models if models is not None else provider.getModels()


def _withSignal(interaction: Any, signal: Any) -> Any:
    """``{...interaction, signal}``: the interaction's public members with ``signal`` set."""
    if read_field(interaction, "signal") is signal:
        return interaction
    if isinstance(interaction, Mapping):
        return {**interaction, "signal": signal}
    members = {name: getattr(interaction, name) for name in dir(interaction) if not name.startswith("_")}
    return SimpleNamespace(**{**members, "signal": signal})


def _mergeRequestOptions(
    options: Any,
    *,
    apiKey: str | None,
    headers: ProviderHeaders | None,
    env: ProviderEnv | None,
    into: type = ProviderStreamOptions,
) -> Any:
    """MISAKA fork: ``const { transformHeaders, ...providerOptions } = options ?? {}; { ...providerOptions, apiKey,
    headers, env }`` as an options object: the adapters read options with dot access (``api/simple_options.py`` reads
    ``options.temperature``), so a plain dict would fail on the first attribute.

    Fields are copied by attribute, never through ``model_dump()``: dumping turns ``thinkingBudgets`` into a plain
    dict, and the adapters hand ``options.thinkingBudgets`` on as it is. ``ProviderStreamOptions`` is the target
    because it is the only stream options model with ``extra="allow"`` (``env`` is declared on none of them, nor
    ``SimpleStreamOptions``' ``reasoning``/``thinkingBudgets``); image and classifier requests pass their own open
    options class as ``into``.
    """
    merged: dict[str, Any] = {}
    if options is not None:
        if isinstance(options, Mapping):
            merged = dict(options)
        elif getattr(type(options), "model_fields", None) is None:
            # A plain object, such as the agent loop's options namespace.
            merged = dict(vars(options))
        else:
            for name in type(options).model_fields:
                merged[name] = getattr(options, name, None)
            merged.update(getattr(options, "model_extra", None) or {})
    merged.pop("transformHeaders", None)
    merged.update({"apiKey": apiKey, "headers": headers, "env": env})
    return into(**merged)


def createModels(options: CreateModelsOptions | Mapping[str, Any] | None = None) -> MutableModels:
    return _ModelsImpl(options)  # type: ignore[return-value]


@dataclass(slots=True, kw_only=True)
class CreateProviderOptions:
    id: str
    # Display name. Default: `id`.
    name: str | None = None
    baseUrl: str | None = None
    headers: ProviderHeaders | None = None
    # Required — every provider has auth semantics, even ambient/keyless ones.
    auth: ProviderAuth
    # Static baseline models of every type (empty for purely dynamic providers).
    # Models without `type` are chat models.
    models: Sequence[AnyModel]
    # Fetch a dynamic model overlay of every type. createProvider restores and
    # publishes it transactionally and drops models of unknown types.
    fetchModels: Callable[[RefreshModelsContext], Awaitable[Sequence[AnyModel]]] | None = None
    # Credential-specific chat model availability. See `Provider.filterModels`.
    filterModels: Callable[[Sequence[Model], Credential | None], Sequence[Model]] | None = None
    # Credential-specific availability across every model type. See `Provider.filterAllModels`.
    filterAllModels: Callable[[Sequence[AnyModel], Credential | None], Sequence[AnyModel]] | None = None
    # Chat implementation: a single one for all chat models, or a map keyed by
    # `model.api` for mixed-API providers. Optional when `images` or
    # `classifiers` is given.
    api: ProviderStreams | Mapping[str, ProviderStreams] | None = None
    # Image-generation implementations keyed by `model.api`.
    images: Mapping[str, ProviderImages] | None = None
    # Classifier implementations keyed by `model.api`.
    classifiers: Mapping[str, ProviderClassifier] | None = None


class _BuiltProvider:
    """MISAKA fork: the provider object ``createProvider`` builds. Its members are upstream's object-literal members;
    a capability upstream leaves undefined is an attribute set to ``None`` here (``Models`` and ``refresh`` read them
    with ``getattr``).
    """

    def __init__(self, input: CreateProviderOptions) -> None:
        api = input.api
        single = api if api is not None and callable(getattr(api, "stream", None)) else None
        byApi = None if single is not None or not api else dict(api)  # type: ignore[arg-type]
        images = dict(input.images) if input.images is not None else None
        classifiers = dict(input.classifiers) if input.classifiers is not None else None
        streams = [single] if single is not None else [entry for entry in (byApi or {}).values() if entry is not None]
        imageImplementations = [entry for entry in (images or {}).values() if entry is not None]
        classifierImplementations = [entry for entry in (classifiers or {}).values() if entry is not None]
        if len(streams) == 0 and len(imageImplementations) == 0 and len(classifierImplementations) == 0:
            raise RuntimeError(f'Provider {input.id}: at least one of "api", "images", or "classifiers" is required.')

        self._input = input
        self._baselineModels: list[AnyModel] = list(input.models)
        self._dynamicModels: list[AnyModel] = []
        self._fetchModels = input.fetchModels
        self._single = single
        self._byApi = byApi
        self._images = images
        self._classifiers = classifiers

        self.id = input.id
        self.name = input.name if input.name is not None else input.id
        self.baseUrl = input.baseUrl
        self.headers = input.headers
        self.auth = input.auth
        if self._fetchModels is None:
            self.refreshModels = None
        self.filterModels = input.filterModels
        self.filterAllModels = input.filterAllModels
        if not any(getattr(entry, "fetchDeferred", None) is not None for entry in streams):
            self.fetchDeferred = None
        if not any(getattr(entry, "cancelDeferred", None) is not None for entry in streams):
            self.cancelDeferred = None
        if not (images and len(imageImplementations) > 0):
            self.generateImages = None
        if not (classifiers and len(classifierImplementations) > 0):
            self.classify = None

    def _currentModels(self) -> list[AnyModel]:
        merged = [*self._baselineModels]
        for model in self._dynamicModels:
            index = next(
                (
                    position
                    for position, entry in enumerate(merged)
                    if getModelType(entry) == getModelType(model) and entry.id == model.id
                ),
                -1,
            )
            if index >= 0:
                merged[index] = model
            else:
                merged.append(model)
        return merged

    def _apiFor(self, model: Model) -> ProviderStreams | None:
        return self._single if self._single is not None else (self._byApi or {}).get(model.api)

    def _dispatch(
        self,
        model: Model,
        run: Callable[[ProviderStreams], AssistantMessageEventStream],
    ) -> AssistantMessageEventStream:
        streams = self._apiFor(model)
        if not streams:

            async def fail() -> Any:
                raise ModelsError("stream", f'Provider {self._input.id} has no API implementation for "{model.api}"')

            return lazyStream(model, fail)
        return run(streams)

    def getModels(self) -> list[Model]:
        return [model for model in self._currentModels() if isModelType(model, "chat")]

    def getAllModels(self) -> list[AnyModel]:
        return self._currentModels()

    async def refreshModels(self, context: RefreshModelsContext) -> None:
        fetchModels = self._fetchModels
        if fetchModels is None:  # pragma: no cover - the attribute is None for static providers
            return
        if context.stored:
            restored = [model for model in context.stored.models if model.provider == self._input.id]

            def restore() -> None:
                self._dynamicModels = restored

            if not await context.publish(ModelsPublication(update=restore)):
                return
        if not context.allowNetwork or signal_aborted(context.signal):
            return
        fetched = await fetchModels(context)
        if signal_aborted(context.signal):
            return
        refreshed = [model for model in fetched if _hasKnownModelType(model)]

        def update() -> None:
            self._dynamicModels = refreshed

        await context.publish(
            ModelsPublication(
                persist=ModelsStoreEntry(models=refreshed, checkedAt=int(time.time() * 1000)),
                persist_is_set=True,
                update=update,
            )
        )

    def stream(self, model: Model, context: TranscriptContext, options: Any = None) -> AssistantMessageEventStream:
        return self._dispatch(model, lambda streams: streams.stream(model, context, options))

    def streamSimple(self, model: Model, context: TranscriptContext, options: Any = None) -> AssistantMessageEventStream:
        return self._dispatch(model, lambda streams: streams.streamSimple(model, context, options))

    def fetchDeferred(self, model: Model, handle: DeferredHandle, options: Any = None) -> AssistantMessageEventStream:
        async def setup() -> AssistantMessageEventStream:
            implementation = self._apiFor(model)
            fetchDeferred = getattr(implementation, "fetchDeferred", None)
            if not fetchDeferred:
                raise ModelsError(
                    "provider",
                    f'Provider {self._input.id} does not support deferred responses for "{model.api}"',
                )
            return fetchDeferred(model, handle, options)

        return lazyStream(model, setup)

    async def cancelDeferred(self, model: Model, handle: DeferredHandle, options: Any = None) -> None:
        implementation = self._apiFor(model)
        cancelDeferred = getattr(implementation, "cancelDeferred", None)
        if not cancelDeferred:
            raise ModelsError(
                "provider",
                f'Provider {self._input.id} cannot cancel deferred responses for "{model.api}"',
            )
        await cancelDeferred(model, handle, options)

    async def generateImages(self, model: ImageModel, context: ImagesContext, options: Any = None) -> AssistantImages:
        implementation = (self._images or {}).get(model.api)
        if not implementation:
            return imageErrorResult(
                model,
                ModelsError(
                    "provider",
                    f'Provider {self._input.id} has no image generation implementation for "{model.api}"',
                ),
            )
        return await implementation.generateImages(model, context, options)

    async def classify(self, model: ClassifierModel, context: ClassifierContext, options: Any = None) -> ClassifierResult:
        implementation = (self._classifiers or {}).get(model.api)
        if not implementation:
            return classifierErrorResult(
                model,
                ModelsError("provider", f'Provider {self._input.id} has no classifier implementation for "{model.api}"'),
            )
        return await implementation.classify(model, context, options)


def createProvider(input: CreateProviderOptions) -> Provider:
    """Builds a provider from parts. Built-in provider factories and models.json
    custom providers both go through this. A single `api` streams all chat
    models; an `api` map dispatches on `model.api`, and a model whose api has
    no entry produces a stream error. One-shot operation maps dispatch on
    `model.api` the same way. At least one concrete implementation across
    `api`/`images`/`classifiers` is required; empty maps are rejected.
    """
    return _BuiltProvider(input)  # type: ignore[return-value]


def hasApi(model: AnyModel, api: str) -> bool:
    """Runtime-checked narrowing for dynamically looked-up models::

        model = models.getModel("anthropic", "claude-opus-4-7")
        if model and hasApi(model, "anthropic-messages"):
            # model: Model<"anthropic-messages">, stream options fully typed
            ...

    Non-chat models never match, even when their api id equals `api`.
    """
    return isModelType(model, "chat") and model.api == api


def calculateCost(model: AnyModel, usage: Usage) -> UsageCost:
    inputTokens = usage.input + usage.cacheRead + usage.cacheWrite
    rates: ModelCostRates = model.cost
    matchedThreshold = -1
    for tier in model.cost.tiers or []:
        if inputTokens > tier.inputTokensAbove and tier.inputTokensAbove > matchedThreshold:
            rates = tier
            matchedThreshold = tier.inputTokensAbove

    # Anthropic charges 2x base input for 1h cache writes.
    longWrite = usage.cacheWrite1h if usage.cacheWrite1h is not None else 0
    shortWrite = usage.cacheWrite - longWrite
    usage.cost.input = (rates.input / 1000000) * usage.input
    usage.cost.output = (rates.output / 1000000) * usage.output
    usage.cost.cacheRead = (rates.cacheRead / 1000000) * usage.cacheRead
    usage.cost.cacheWrite = (rates.cacheWrite * shortWrite + rates.input * 2 * longWrite) / 1000000
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cacheRead + usage.cost.cacheWrite
    return usage.cost


_EXTENDED_THINKING_LEVELS: list[ModelThinkingLevel] = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]

# MISAKA fork: `thinkingLevelMap?.[level]` is `undefined` for a missing key and `null` for a mapped-away level.
_MISSING = object()


def getSupportedThinkingLevels(model: Model) -> list[ModelThinkingLevel]:
    if not model.reasoning:
        return ["off"]

    def supported(level: ModelThinkingLevel) -> bool:
        mapped = (
            model.thinkingLevelMap[level]
            if model.thinkingLevelMap is not None and level in model.thinkingLevelMap
            else _MISSING
        )
        if mapped is None:
            return False
        if level == "xhigh" or level == "max":
            return mapped is not _MISSING
        return True

    return [level for level in _EXTENDED_THINKING_LEVELS if supported(level)]


def clampThinkingLevel(model: Model, level: ModelThinkingLevel) -> ModelThinkingLevel:
    availableLevels = getSupportedThinkingLevels(model)
    if level in availableLevels:
        return level

    requestedIndex = _EXTENDED_THINKING_LEVELS.index(level) if level in _EXTENDED_THINKING_LEVELS else -1
    if requestedIndex == -1:
        return availableLevels[0] if availableLevels else "off"

    for i in range(requestedIndex, len(_EXTENDED_THINKING_LEVELS)):
        candidate = _EXTENDED_THINKING_LEVELS[i]
        if candidate in availableLevels:
            return candidate
    for i in range(requestedIndex - 1, -1, -1):
        candidate = _EXTENDED_THINKING_LEVELS[i]
        if candidate in availableLevels:
            return candidate
    return availableLevels[0] if availableLevels else "off"


def modelsAreEqual(a: AnyModel | None, b: AnyModel | None) -> bool:
    """Check if two models are equal by comparing their type, id, and provider.
    Returns false if either model is null or undefined.
    """
    if not a or not b:
        return False
    return getModelType(a) == getModelType(b) and a.id == b.id and a.provider == b.provider


__all__ = [
    "CreateModelsOptions",
    "CreateProviderOptions",
    "Models",
    "ModelsApiStreamOptions",
    "ModelsClassifierOptions",
    "ModelsDeferredCancelOptions",
    "ModelsDeferredFetchOptions",
    "ModelsError",
    "ModelsErrorCode",
    "ModelsImagesOptions",
    "ModelsPublication",
    "ModelsRefreshOptions",
    "ModelsRefreshResult",
    "ModelsRequestTransforms",
    "ModelsSimpleStreamOptions",
    "MutableModels",
    "Provider",
    "RefreshModelsContext",
    "calculateCost",
    "clampThinkingLevel",
    "createModels",
    "createProvider",
    "getModelType",
    "getSupportedThinkingLevels",
    "hasApi",
    "isModelType",
    "modelsAreEqual",
]
