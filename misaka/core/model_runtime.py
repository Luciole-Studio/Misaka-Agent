"""The configured ``Models`` collection misaka's sessions, tools and CLI share.

Translated from pi's ``coding-agent/src/core/model-runtime.ts`` (v1.3.0).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, NotRequired, TypedDict

from misaka.ai.api.lazy import lazyStream
from misaka.ai.auth.resolve import AuthResolutionOverrides
from misaka.ai.auth.types import (
    ApiKeyCredential,
    AuthCheck,
    AuthInteraction,
    AuthOperationOptions,
    AuthResult,
    AuthType,
    Credential,
    CredentialInfo,
    CredentialStore,
    LoginOptions,
    ModelAuth,
    OAuthCredential,
)
from misaka.ai.models import (
    CreateModelsOptions,
    ModelsRefreshOptions,
    ModelsRefreshResult,
    _mergeHeaders,
    _mergeRequestOptions,
    clampThinkingLevel,
    createModels,
)
from misaka.ai.providers.all import builtinProviders, getBuiltinModelDataGeneratedAt
from misaka.ai.providers.openrouter import personalizeOpenRouterCatalog
from misaka.ai.providers.radius import RadiusProviderOptions, radiusProvider
from misaka.ai.types import (
    AnyModel,
    AssistantImages,
    AssistantMessage,
    AssistantMessageEventStream,
    ClassifierContext,
    ClassifierModel,
    ClassifierResult,
    Context,
    DeferredHandle,
    ImageModel,
    ImagesContext,
    Message,
    Model,
    ModelThinkingLevel,
    ModelType,
    ProviderClassifierOptions,
    ProviderImagesOptions,
    ProviderStreamOptions,
)
from misaka.ai.utils.model_operations import (
    assertChatModel,
    assertClassifierInputSupported,
    assertClassifierModel,
    assertImageModel,
    classifierErrorResult,
    imageErrorResult,
)
from misaka.ai.utils.models_error import ModelsError
from misaka.ai.utils.transcript import normalizeContext
from misaka.config import home
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_config import ModelConfig
from misaka.core.models_store import FileModelsStore, InMemoryCodingAgentModelsStore
from misaka.core.provider_composer import (
    AuthStatus,
    CompatibilityRequestConfig,
    ProviderConfigInput,
    composeModelProvider,
    configuredRequestAuthStatus,
    resolveCompatibilityRequestConfig,
    resolveConfiguredModelHeaders,
    validateExtensionProvider,
)
from misaka.core.remote_catalog_provider import (
    withPersonalizedCatalog,
    withRemoteCatalog,
)
from misaka.core.runtime_credentials import RuntimeCredentials
from misaka.core.virtual_models import (
    FailedRoute,
    ModelRoute,
    ModelRouteReason,
    ModelRouteRequest,
    PreviousRoute,
    VirtualModelDefinition,
    createVirtualModel,
    findLatestResponse,
    isVirtualModel,
    withVirtualModels,
)
from misaka.utils._abort_signal import (
    AnyAbortSignal,
    TimeoutAbortSignal,
    throw_if_aborted,
)
from misaka.utils.abort import operationSignal, raceWithAbortSignal
from misaka.utils.js import ErrorOptions, errorMessage, jsTrim
from misaka.utils.values import read_field, signal_aborted

_UNSET: Any = object()


@dataclass(slots=True)
class _RegisteredVirtualModel:
    model: Model
    route: Callable[[ModelRouteRequest], ModelRoute | Awaitable[ModelRoute]]


@dataclass(slots=True, frozen=True)
class _ModelRuntimeSnapshot:
    all: Sequence[Model] = ()
    available: Sequence[Model] = ()
    configuredProviders: frozenset[str] = frozenset()
    storedProviders: frozenset[str] = frozenset()
    auth: Mapping[str, AuthCheck | None] = field(default_factory=dict)

class CreateModelRuntimeOptions(TypedDict, total=False):
    credentials: CredentialStore
    """Credential storage. Defaults to the file at authPath."""
    authPath: str
    modelsPath: str | None
    modelsStore: Any
    modelsStorePath: str
    allowModelNetwork: bool
    """Allow create() to refresh model catalogs over the network. Defaults to false."""
    modelRefreshTimeoutMs: float
    """Timeout for the create-time network model refresh."""
    catalogBaseUrl: str
    signal: Any
    """Optional caller cancellation for initial cache restoration and availability checks."""
    refreshOnCreate: bool
    """Skip initial catalog and availability refresh. Static models remain available."""
    authStorage: AuthStorage
    """MISAKA fork: the auth.json store kept as ``runtime.authStorage``."""


ModelRuntimeAuthOverrides = AuthResolutionOverrides

# MISAKA fork: "recoverRejectedCredential" is the B7 recovery below.
type CredentialSynchronizationOperation = Literal[
    "login", "logout", "setRuntimeApiKey", "removeRuntimeApiKey", "recoverRejectedCredential"
]


class CredentialSynchronizationError(Exception):
    """Credentials changed successfully, but the local model/auth snapshot could not be synchronized.

    The ``cause`` of ``options`` is also chained as ``__cause__``.
    """

    def __init__(
        self,
        providerId: str,
        operation: CredentialSynchronizationOperation,
        credential: Credential | None,
        options: ErrorOptions,
    ) -> None:
        super().__init__(f"Credential {operation} committed for {providerId}, but local synchronization failed")
        self.cause = options.get("cause")
        if self.cause is not None:
            self.__cause__ = self.cause
        self.name = "CredentialSynchronizationError"
        self.providerId = providerId
        self.operation = operation
        self.credential = credential


mergeHeaders = _mergeHeaders


def _optionFields(options: Any) -> dict[str, Any]:
    """``{...options}`` for an options object or mapping, fields copied by attribute."""
    if options is None:
        return {}
    if isinstance(options, Mapping):
        return dict(options)
    modelFields = getattr(type(options), "model_fields", None)
    if modelFields is None:
        # A plain object, such as the agent loop's options namespace.
        return dict(vars(options))
    fields = {name: getattr(options, name, None) for name in modelFields}
    fields.update(getattr(options, "model_extra", None) or {})
    return fields


class _InteractionWithSignal:
    """``{ ...interaction, signal }``."""

    def __init__(self, interaction: Any, signal: Any) -> None:
        self._interaction = interaction
        self.signal = signal

    def __getattr__(self, name: str) -> Any:
        return getattr(self._interaction, name)


# MISAKA fork (B7): what a provider says when the credential itself was refused, as opposed to
# a transient failure: the account is fine, the token in hand is not. An OAuth access token
# rotated by another client on the same account reads exactly like this (2026-09-18: the Codex
# CLI refreshed the shared refresh token, and four cards' first request came back "Encountered
# invalidated oauth token for user" while misaka's copy was still inside its own expiry window,
# so nothing refreshed and a person had to restart each card by hand).
# The status only counts where it is a status (leading, "error code: 401", "status 401", "(401)"),
# never as a count or an index ("max_tokens: 401", "messages.401", "input[401]", "~401 min").
_REJECTED_CREDENTIAL_PATTERN = re.compile(
    r"(?:^|error\s+code:?\s*|status(?:\s+code)?:?\s*|http\s*|\()401(?![\w\].])|"
    r"\b(?:unauthorized|invalidated\s+oauth|invalid[_ -]?api[_ -]?key|invalid_token|"
    r"invalid[ -](?:access|bearer|oauth)[ -]token|invalid[ -]token(?=\s*$|[.,;:!)\"'])|"
    r"token[_ -]?(?:is[_ -]?)?(?:expired|revoked|invalid)|"
    r"expired[_ -]?token|authentication[_ -]?(?:failed|error))\b",
    re.IGNORECASE,
)


def is_rejected_credential_error(text: str | None) -> bool:
    """Whether a failed request says the credential was refused rather than the service."""
    return bool(text) and bool(_REJECTED_CREDENTIAL_PATTERN.search(str(text)))


def _authToken(auth: ModelAuth) -> str | None:
    """The credential a request carries: its API key, or the bearer token a flow that
    authenticates by header sends instead."""
    if auth.apiKey:
        return auth.apiKey
    for name, value in (auth.headers or {}).items():
        if name.lower() == "authorization" and isinstance(value, str) and value.startswith("Bearer "):
            return value[len("Bearer "):]
    return None


def personalizedCatalogHint(providerId: str | None) -> str:
    """MISAKA fork: what an error about a model missing from ``providerId``'s catalog adds when that catalog is the
    user's key's own list (``withPersonalizedCatalog`` in ``ModelRuntime.create``): the model may well exist, and the
    key may not use it. Empty for every other provider."""
    if (providerId or "").lower() != "openrouter":
        return ""
    return (
        " MISAKA lists only the OpenRouter models your OpenRouter API key may use: if the key restricts its models"
        " (its guardrails on openrouter.ai), allow this one there, or choose another model."
    )


def _modelsStorePath(modelsPath: str) -> str:
    # MISAKA fork: pi keeps the store beside models.json. The home keeps it with its state; a
    # models.json somewhere else still gets its store as a sibling.
    if os.path.realpath(modelsPath) == os.path.realpath(home.path("models")):
        return str(home.path("models_store"))
    return os.path.join(os.path.dirname(modelsPath), "models-store.json")




class ResolveModelOptions(TypedDict):
    reason: ModelRouteReason
    thinkingLevel: ModelThinkingLevel
    signal: NotRequired[Any]
    failed: NotRequired[AssistantMessage | None]
    state: NotRequired[Any]

class ModelRuntime:
    """Configured ``Models`` collection used by misaka's sessions and SDK consumers."""

    def __init__(
        self,
        credentials: RuntimeCredentials,
        config: ModelConfig,
        modelsPath: str | None,
        modelsStore: Any,
        providers: Sequence[Any],
        modelNetworkEnabled: bool,
        authStorage: AuthStorage | None = None,
    ) -> None:
        # Private by convention: build one with `ModelRuntime.create`.
        # MISAKA fork: `authStorage` is the auth.json store kept reachable as `runtime.authStorage`.
        self._credentials = credentials
        self._config = config
        self._modelsPath = modelsPath
        self._modelNetworkEnabled = modelNetworkEnabled
        self._defaultBuiltins: dict[str, Any] = {provider.id: provider for provider in providers}
        self._builtins: dict[str, Any] = dict(self._defaultBuiltins)
        self._nativeExtensionProviders: dict[str, Any] = {}
        self._extensionProviders: dict[str, ProviderConfigInput] = {}
        # Virtual models by provider id, then model id.
        self._virtualModels: dict[str, dict[str, _RegisteredVirtualModel]] = {}
        self._compositionErrors: dict[str, str] = {}
        self._snapshot = _ModelRuntimeSnapshot()
        self._availabilityRefreshSeq = 0
        self._availabilityErrorSeq = 0
        self._providerAvailabilitySeq: dict[str, int] = {}
        self._availabilityError: str | None = None
        self._credentialOperations: dict[str, asyncio.Future[Any]] = {}
        # MISAKA fork: the auth.json store, for the code that reads its cached records directly (LCM pools, the
        # nous gateway); None when the credentials are another CredentialStore.
        self.authStorage = authStorage
        # MISAKA fork (B7): the stored token each session's last request to a provider carried,
        # keyed by (provider, sessionId), and the tokens already replaced after a provider
        # refused them.
        self._lastResolvedApiKey: dict[tuple[str, str | None], str] = {}
        self._recoveredCredentials: dict[str, set[str]] = {}
        self._backgroundRefreshes: set[asyncio.Task[Any]] = set()
        # MISAKA fork: core's own providers (core/wiring.py), registered by create().
        self._coreProviderIds: list[str] = []
        self._models = createModels(CreateModelsOptions(credentials=credentials, modelsStore=modelsStore))
        self._rebuildProviders()

    @classmethod
    async def create(cls, options: CreateModelRuntimeOptions | None = None) -> ModelRuntime:
        """MISAKA fork: the auth.json store stays reachable as ``runtime.authStorage``, whether it was handed in as
        ``credentials``, as ``authStorage``, or made here from ``authPath``.
        """
        options = options if options is not None else {}
        credentials = options.get("credentials")
        authStorage = options.get("authStorage")
        authPath = options.get("authPath")
        # A present `None` is upstream's `null` (no models.json); an absent key takes the default.
        modelsPath = options.get("modelsPath", _UNSET)
        modelsStore = options.get("modelsStore")
        modelsStorePath = options.get("modelsStorePath")
        catalogBaseUrl = options.get("catalogBaseUrl")
        signal = options.get("signal")
        modelRefreshTimeoutMs = options.get("modelRefreshTimeoutMs")
        if credentials is None:
            authStorage = authStorage if authStorage is not None else AuthStorage.create(authPath)
            credentials = authStorage
        elif authStorage is None and isinstance(credentials, AuthStorage):
            authStorage = credentials
        runtimeCredentials = RuntimeCredentials(credentials)
        # MISAKA fork: the default models.json is the home's.
        resolvedModelsPath = str(home.path("models")) if modelsPath is _UNSET else modelsPath
        config = await ModelConfig.load(resolvedModelsPath)
        if modelsStore is None:
            modelsStore = (
                FileModelsStore(modelsStorePath if modelsStorePath is not None else _modelsStorePath(resolvedModelsPath))
                if resolvedModelsPath
                else InMemoryCodingAgentModelsStore()
            )
        builtinModelDataGeneratedAt = getBuiltinModelDataGeneratedAt()

        def wrap(provider: Any) -> Any:
            if provider.id == "radius":
                return provider
            # OpenRouter narrows the catalog to the models, limits and prices of the user's key.
            if provider.id == "openrouter":
                # MISAKA fork: without a `catalogBaseUrl` the built-in catalog alone is personalized: the one
                # request is OpenRouter's own `/models/user`, with the user's key (remote_catalog_provider.py).
                return withPersonalizedCatalog(
                    provider,
                    personalizeOpenRouterCatalog,
                    catalogBaseUrl,
                    builtinModelDataGeneratedAt,
                )
            # MISAKA fork: the remote catalog overlay needs an endpoint named by the caller; upstream
            # defaults to its own service, which misaka does not send users' traffic to (no endpoint:
            # `withRemoteCatalog` returns the provider as it is).
            return withRemoteCatalog(provider, catalogBaseUrl, builtinModelDataGeneratedAt)

        providers = [wrap(provider) for provider in builtinProviders()]
        runtime = cls(
            runtimeCredentials,
            config,
            resolvedModelsPath,
            modelsStore,
            providers,
            # MISAKA fork: offline mode is `MISAKA_OFFLINE`, where upstream reads `PI_OFFLINE`.
            os.environ.get("MISAKA_OFFLINE") is None,
            authStorage,
        )
        runtime._configureRadiusProviders()
        runtime._rebuildProviders()
        # MISAKA fork: core's own providers (core/wiring.py) are part of every runtime, as
        # built-ins are; their refreshModels keeps what they publish in step with the credentials.
        from misaka.core.wiring import core_providers

        for providerId, providerConfig in core_providers(runtime):
            runtime._extensionProviders[providerId] = providerConfig  # type: ignore[assignment]
            runtime._coreProviderIds.append(providerId)
            runtime._recomposeProvider(providerId)
        runtime._updateModelSnapshot()
        refreshFromNetwork = runtime._modelNetworkEnabled and options.get("allowModelNetwork") is True
        timeout = (
            TimeoutAbortSignal(modelRefreshTimeoutMs)
            if refreshFromNetwork and modelRefreshTimeoutMs is not None
            else None
        )
        if options.get("refreshOnCreate") is not False:
            await runtime.refresh(
                ModelsRefreshOptions(
                    allowNetwork=refreshFromNetwork,
                    signal=AnyAbortSignal(signal, timeout) if signal is not None and timeout is not None else signal or timeout,
                )
            )
        return runtime

    def _configureRadiusProviders(self) -> None:
        self._builtins.clear()
        self._builtins.update(self._defaultBuiltins)
        for providerId in self._config.getProviderIds():
            config = self._config.getProvider(providerId)
            if config is None or config.oauth != "radius" or not config.baseUrl:
                continue
            self._builtins[providerId] = radiusProvider(
                RadiusProviderOptions(
                    id=providerId,
                    name=config.name if config.name is not None else providerId,
                    gateway=re.sub(r"/v1/?\Z", "", config.baseUrl),
                )
            )

    def _providerIds(self) -> list[str]:
        return list(
            dict.fromkeys([
                *self._builtins,
                *self._nativeExtensionProviders,
                *self._config.getProviderIds(),
                *self._extensionProviders,
                *self._virtualModels,
            ])
        )

    def _recomposeProvider(self, providerId: str) -> Any:
        """Returns the provider without virtual models, or None when only virtual models define it."""
        provider = self._composeProvider(providerId)
        virtualModels = [entry.model for entry in self._virtualModels.get(providerId, {}).values()]
        if virtualModels:
            self._models.setProvider(withVirtualModels(providerId, provider, virtualModels))
        elif provider is not None:
            self._models.setProvider(provider)
        else:
            self._models.deleteProvider(providerId)
        return provider

    def _composeProvider(self, providerId: str) -> Any:
        """The provider without virtual models, or None when nothing defines it."""
        base = self._nativeExtensionProviders.get(providerId)
        if base is None:
            base = self._builtins.get(providerId)
        extension = self._extensionProviders.get(providerId)
        if self._config.getProvider(providerId) is None and extension is None:
            # No overlays: use the builtin untouched so its auth/login/stream behavior is exact.
            self._compositionErrors.pop(providerId, None)
            return base
        try:
            provider = composeModelProvider(providerId, base, self._config, extension)
            self._compositionErrors.pop(providerId, None)
            return provider
        except Exception as error:  # noqa: BLE001 - reported through getError(); the base stays usable
            self._compositionErrors[providerId] = errorMessage(error)
            return base

    def _rebuildProviders(self) -> None:
        self._models.clearProviders()
        self._compositionErrors.clear()
        for providerId in self._providerIds():
            self._recomposeProvider(providerId)
        self._updateModelSnapshot()

    def _updateModelSnapshot(self) -> None:
        all = list(self._models.getModels())
        self._snapshot = replace(
            self._snapshot,
            all=all,
            available=[model for model in all if model.provider in self._snapshot.configuredProviders],
        )

    async def _runAvailabilityRefresh(self, seq: int, errorSeq: int, signal: Any) -> None:
        providers = self._models.getProviders()
        options = AuthOperationOptions(signal=signal)

        async def check(provider: Any) -> tuple[str, AuthCheck | None]:
            return provider.id, await self._models.checkAuth(provider.id, options)

        available, checks, credentials = await asyncio.gather(
            self._models.getAvailable(None, options),
            asyncio.gather(*(check(provider) for provider in providers)),
            self._credentials.list(options),
        )
        if seq != self._availabilityRefreshSeq:
            return
        self._snapshot = _ModelRuntimeSnapshot(
            all=list(self._models.getModels()),
            available=list(available),
            configuredProviders=frozenset(providerId for providerId, auth in checks if auth is not None),
            storedProviders=frozenset(entry.providerId for entry in credentials),
            auth=dict(checks),
        )
        if errorSeq == self._availabilityErrorSeq:
            self._availabilityError = None

    def _queueAvailabilityRefresh(self, signal: Any = None) -> Awaitable[None]:
        self._availabilityRefreshSeq += 1
        seq = self._availabilityRefreshSeq
        for providerId, providerSeq in list(self._providerAvailabilitySeq.items()):
            self._providerAvailabilitySeq[providerId] = providerSeq + 1
        self._availabilityErrorSeq += 1
        errorSeq = self._availabilityErrorSeq
        effectiveSignal = operationSignal(signal)

        async def run() -> None:
            try:
                await self._runAvailabilityRefresh(seq, errorSeq, effectiveSignal)
            except Exception as error:
                if errorSeq == self._availabilityErrorSeq and not signal_aborted(effectiveSignal):
                    self._availabilityError = errorMessage(error)
                raise

        return run()

    async def _refreshProviderAvailability(self, providerId: str, signal: Any) -> None:
        # Invalidate any full availability pass that started before this credential change.
        self._availabilityRefreshSeq += 1
        providerSeq = self._providerAvailabilitySeq.get(providerId, 0) + 1
        self._providerAvailabilitySeq[providerId] = providerSeq
        self._availabilityErrorSeq += 1
        errorSeq = self._availabilityErrorSeq
        options = AuthOperationOptions(signal=signal)
        try:
            available, auth, credential = await asyncio.gather(
                self._models.getAvailable(providerId, options),
                self._models.checkAuth(providerId, options),
                self._credentials.read(providerId, options),
            )
            throw_if_aborted(signal)
            if self._providerAvailabilitySeq.get(providerId) != providerSeq:
                return
            configuredProviders = set(self._snapshot.configuredProviders)
            storedProviders = set(self._snapshot.storedProviders)
            authByProvider = dict(self._snapshot.auth)
            if auth is not None:
                configuredProviders.add(providerId)
                authByProvider[providerId] = auth
            else:
                configuredProviders.discard(providerId)
                authByProvider.pop(providerId, None)
            if credential is not None:
                storedProviders.add(providerId)
            else:
                storedProviders.discard(providerId)
            all = list(self._models.getModels())
            availableById = {
                (model.provider, model.id): model
                for model in [
                    *[model for model in self._snapshot.available if model.provider != providerId],
                    *available,
                ]
            }
            self._snapshot = _ModelRuntimeSnapshot(
                all=all,
                available=[
                    availableById[(model.provider, model.id)]
                    for model in all
                    if (model.provider, model.id) in availableById
                ],
                configuredProviders=frozenset(configuredProviders),
                storedProviders=frozenset(storedProviders),
                auth=authByProvider,
            )
            if errorSeq == self._availabilityErrorSeq:
                self._availabilityError = None
        except Exception as error:
            if (
                self._providerAvailabilitySeq.get(providerId) == providerSeq
                and errorSeq == self._availabilityErrorSeq
                and not signal_aborted(signal)
            ):
                self._availabilityError = errorMessage(error)
            raise

    def getProviders(self) -> list[Any]:
        return self._models.getProviders()

    def getProvider(self, providerId: str) -> Any:
        return self._models.getProvider(providerId)

    def getModels(self, providerId: str | None = None) -> list[Model]:
        return self._models.getModels(providerId)

    def getModel(self, providerId: str, modelId: str) -> Model | None:
        return self._models.getModel(providerId, modelId)

    def getModelsOfType(self, type: ModelType, providerId: str | None = None) -> list[Any]:
        return self._models.getModelsOfType(type, providerId)

    def getModelOfType(self, type: ModelType, providerId: str, modelId: str) -> Any:
        return self._models.getModelOfType(type, providerId, modelId)

    def getAllModels(self, providerId: str | None = None) -> list[AnyModel]:
        return self._models.getAllModels(providerId)

    async def getAvailableOfType(
        self, type: ModelType, providerId: str | None = None, options: AuthOperationOptions | None = None
    ) -> list[Any]:
        return await self._models.getAvailableOfType(type, providerId, options)

    async def getAllAvailable(
        self, providerId: str | None = None, options: AuthOperationOptions | None = None
    ) -> list[AnyModel]:
        return await self._models.getAllAvailable(providerId, options)

    async def checkAuth(self, providerId: str, options: AuthOperationOptions | None = None) -> AuthCheck | None:
        return await self._models.checkAuth(providerId, options)

    async def getAvailable(
        self, providerId: str | None = None, options: AuthOperationOptions | None = None
    ) -> list[Model]:
        if providerId:
            self._availabilityErrorSeq += 1
            errorSeq = self._availabilityErrorSeq
            try:
                available = await self._models.getAvailable(providerId, options)
                if errorSeq == self._availabilityErrorSeq:
                    self._availabilityError = None
                return available
            except Exception as error:
                if errorSeq == self._availabilityErrorSeq and not signal_aborted(
                    options.signal if options is not None else None
                ):
                    self._availabilityError = errorMessage(error)
                raise
        await self._queueAvailabilityRefresh(options.signal if options is not None else None)
        return list(self._snapshot.available)

    def getAvailableSnapshot(self) -> list[Model]:
        return list(self._snapshot.available)

    def getError(self) -> str | None:
        errors: list[str] = []
        configError = self._config.getError()
        if configError:
            errors.append(configError)
        for providerId, error in self._compositionErrors.items():
            errors.append(f'Provider "{providerId}": {error}')
        if self._availabilityError:
            errors.append(f"Availability refresh: {self._availabilityError}")
        return "\n\n".join(errors) if errors else None

    def getRegisteredProviderConfig(self, providerId: str) -> ProviderConfigInput | None:
        return self._extensionProviders.get(providerId)

    def getRegisteredProviderIds(self) -> list[str]:
        return list(dict.fromkeys([*self._extensionProviders, *self._nativeExtensionProviders]))

    def getRegisteredNativeProvider(self, providerId: str) -> Any:
        return self._nativeExtensionProviders.get(providerId)

    def getCompatibilityRequestConfig(self, model: Model) -> CompatibilityRequestConfig:
        """Compatibility fallback for ModelRegistry when provider auth is unconfigured."""
        return resolveCompatibilityRequestConfig(
            model,
            self._config.getProvider(model.provider),
            self._extensionProviders.get(model.provider),
        )

    def isUsingOAuth(self, providerId: str) -> bool:
        auth = self._snapshot.auth.get(providerId)
        return auth is not None and auth.type == "oauth"

    def isUsingSubscription(self, providerId: str) -> bool:
        provider = self._models.getProvider(providerId)
        oauth = provider.auth.oauth if provider is not None else None
        return self.isUsingOAuth(providerId) and oauth is not None and oauth.isSubscription is True

    def hasConfiguredAuth(self, providerId: str) -> bool:
        return providerId in self._snapshot.configuredProviders

    async def getAuth(
        self, providerOrModel: str | Model, overrides: ModelRuntimeAuthOverrides | None = None
    ) -> AuthResult | None:
        overrides = overrides if overrides is not None else ModelRuntimeAuthOverrides()
        if isinstance(providerOrModel, str):
            return await self._models.getAuth(providerOrModel, overrides)
        resolution = await self._models.getAuth(providerOrModel, overrides)
        if resolution is None:
            return None
        configuredHeaders = resolveConfiguredModelHeaders(
            providerOrModel,
            self._config.getProvider(providerOrModel.provider),
            self._extensionProviders.get(providerOrModel.provider),
            {**(resolution.env or {}), **(overrides.env or {})},
        )
        return resolution.model_copy(
            update={
                "auth": resolution.auth.model_copy(
                    update={"headers": mergeHeaders(resolution.auth.headers, configuredHeaders)}
                )
            }
        )

    def _enqueueCredentialOperation[T](
        self, providerId: str, signal: Any, task: Callable[[], Awaitable[T]]
    ) -> Awaitable[T]:
        previous = self._credentialOperations.get(providerId)
        started: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def run() -> T:
            if previous is not None:
                await asyncio.wait([previous])
            throw_if_aborted(signal)
            started.set_result(None)
            return await task()

        operation = asyncio.ensure_future(run())

        async def settle() -> None:
            try:
                await operation
            except BaseException:  # noqa: BLE001, S110 - the tail only orders later operations
                pass

        tail = asyncio.ensure_future(settle())
        self._credentialOperations[providerId] = tail

        def release(_: asyncio.Future[None]) -> None:
            if self._credentialOperations.get(providerId) is tail:
                del self._credentialOperations[providerId]

        tail.add_done_callback(release)

        async def result() -> T:
            await raceWithAbortSignal(asyncio.shield(started), signal)
            return await asyncio.shield(operation)

        return result()

    async def _synchronizeCredentialState(
        self,
        providerId: str,
        operation: CredentialSynchronizationOperation,
        credential: Credential | None,
        signal: Any,
    ) -> None:
        try:
            throw_if_aborted(signal)
            self._recomposeProvider(providerId)
            compositionError = self._compositionErrors.get(providerId)
            if compositionError:
                raise RuntimeError(compositionError)
            result = await self._models.refresh(
                ModelsRefreshOptions(allowNetwork=False, providers=[providerId], signal=signal)
            )
            if result.aborted:
                throw_if_aborted(signal)
            refreshError = result.errors.get(providerId)
            if refreshError is not None:
                raise refreshError
            self._updateModelSnapshot()
            await self._refreshProviderAvailability(providerId, signal)
        except Exception as cause:
            raise CredentialSynchronizationError(providerId, operation, credential, {"cause": cause}) from cause
        await self._refreshCoreProviders(providerId, signal)

    async def _refreshCoreProviders(self, providerId: str, signal: Any) -> None:
        """MISAKA fork: a core provider's catalog follows other providers' credentials (see
        ``refresh``), so a credential change reaches it. Its failure is its own, not the change's."""
        dependents = [coreId for coreId in self._coreProviderIds if coreId != providerId]
        if not dependents:
            return
        try:
            await self._models.refresh(ModelsRefreshOptions(allowNetwork=False, providers=dependents, signal=signal))
            self._updateModelSnapshot()
            for dependent in dependents:
                await self._refreshProviderAvailability(dependent, signal)
        except Exception:  # noqa: BLE001, S110 - the next refresh retries it
            pass

    async def setRuntimeApiKey(
        self, providerId: str, apiKey: str, options: AuthOperationOptions | None = None
    ) -> None:
        signal = operationSignal(options.signal if options is not None else None)

        async def task() -> None:
            self._credentials.setRuntimeApiKey(providerId, apiKey)
            await self._synchronizeCredentialState(
                providerId, "setRuntimeApiKey", ApiKeyCredential(key=apiKey), signal
            )

        await self._enqueueCredentialOperation(providerId, signal, task)

    async def removeRuntimeApiKey(self, providerId: str, options: AuthOperationOptions | None = None) -> None:
        signal = operationSignal(options.signal if options is not None else None)

        async def task() -> None:
            self._credentials.removeRuntimeApiKey(providerId)
            await self._synchronizeCredentialState(providerId, "removeRuntimeApiKey", None, signal)

        await self._enqueueCredentialOperation(providerId, signal, task)

    async def listCredentials(self, options: AuthOperationOptions | None = None) -> list[CredentialInfo]:
        return await self._credentials.list(options)

    def getProviderAuthStatus(self, providerId: str) -> AuthStatus:
        if self._credentials.hasRuntimeApiKey(providerId):
            return AuthStatus(configured=True, source="runtime")
        if providerId in self._snapshot.storedProviders:
            return AuthStatus(configured=True, source="stored")
        configured = configuredRequestAuthStatus(
            self._config.getProvider(providerId),
            self._extensionProviders.get(providerId),
        )
        if configured is not None:
            return configured
        check = self._snapshot.auth.get(providerId)
        return (
            AuthStatus(configured=True, source="environment", label=check.source)
            if check is not None
            else AuthStatus(configured=False)
        )

    async def _prepareRequest(
        self, model: Any, options: Any, into: type = ProviderStreamOptions
    ) -> tuple[Any, Any, Any]:
        """MISAKA fork: ``into`` is the options class the operation's implementation reads
        (upstream spreads whatever options object the caller passed)."""
        provider = self._models.getProvider(model.provider)
        if provider is None:
            raise ModelsError("provider", f"Unknown provider: {model.provider}")
        resolution = await self.getAuth(
            model,
            ModelRuntimeAuthOverrides(
                apiKey=read_field(options, "apiKey"),
                env=read_field(options, "env"),
                signal=read_field(options, "signal"),
            ),
        )
        if resolution is None:
            raise ModelsError("auth", f"Provider is not configured: {model.provider}")

        headers = mergeHeaders(resolution.auth.headers, read_field(options, "headers"))
        transformHeaders = read_field(options, "transformHeaders")
        if transformHeaders is not None:
            produced = transformHeaders(headers or {})
            headers = await produced if inspect.isawaitable(produced) else produced
        optionEnv = read_field(options, "env")
        env = {**(resolution.env or {}), **(optionEnv or {})} if resolution.env or optionEnv else None
        requestModel = (
            model.model_copy(update={"baseUrl": resolution.auth.baseUrl}) if resolution.auth.baseUrl else model
        )
        apiKey = read_field(options, "apiKey")
        # MISAKA fork (B7): remember the stored token this session's request carries, for
        # recoverRejectedCredential. A key the caller passed in is not the store's to replace.
        tokenKey = (model.provider, read_field(options, "sessionId"))
        token = _authToken(ModelAuth(apiKey=resolution.auth.apiKey, headers=headers)) if apiKey is None else None
        if token:
            self._lastResolvedApiKey[tokenKey] = token
        else:
            self._lastResolvedApiKey.pop(tokenKey, None)
        return (
            provider,
            requestModel,
            _mergeRequestOptions(
                options,
                apiKey=apiKey if apiKey is not None else resolution.auth.apiKey,
                headers=headers,
                env=env,
                into=into,
            ),
        )

    def stream(self, model: Model, context: Context, options: Any = None) -> AssistantMessageEventStream:
        transcript = normalizeContext(context)

        async def setup() -> AssistantMessageEventStream:
            assertChatModel(model)
            provider, requestModel, requestOptions = await self._prepareRequest(model, options)
            return provider.stream(requestModel, transcript, requestOptions)

        return lazyStream(model, setup)

    async def complete(self, model: Model, context: Context, options: Any = None) -> AssistantMessage:
        return await self.stream(model, context, options).result()

    def streamSimple(self, model: Model, context: Context, options: Any = None) -> AssistantMessageEventStream:
        transcript = normalizeContext(context)
        if isVirtualModel(model):
            # Requests outside the agent loop are routed here. Callers sized them before routing, so
            # cap the output budget to the routed model.
            async def routed() -> AssistantMessageEventStream:
                route = await self.resolveModel(model, transcript.messages, {"reason": "direct", "thinkingLevel": read_field(options, "reasoning") or "off", "signal": read_field(options, "signal")})
                limit = route.model.maxTokens
                requested = read_field(options, "maxTokens")
                maxTokens = min(requested, limit) if requested and limit > 0 else requested
                reasoning = None if route.thinkingLevel == "off" else route.thinkingLevel
                # Caller credentials were resolved for the virtual model's provider. Another provider
                # resolves its own, so they are not sent to the wrong vendor.
                rest = _optionFields(options)
                auth = {name: rest.pop(name, None) for name in ("apiKey", "headers", "env")}
                return self.streamSimple(
                    route.model,
                    context,
                    {
                        **rest,
                        **(auth if route.model.provider == model.provider else {}),
                        "maxTokens": maxTokens,
                        "reasoning": reasoning,
                    },
                )

            return lazyStream(model, routed)

        async def setup() -> AssistantMessageEventStream:
            assertChatModel(model)
            provider, requestModel, requestOptions = await self._prepareRequest(model, options)
            return provider.streamSimple(requestModel, transcript, requestOptions)

        return lazyStream(model, setup)

    async def completeSimple(self, model: Model, context: Context, options: Any = None) -> AssistantMessage:
        return await self.streamSimple(model, context, options).result()

    def streamDeferred(self, model: Model, handle: DeferredHandle, options: Any = None) -> AssistantMessageEventStream:
        async def setup() -> AssistantMessageEventStream:
            assertChatModel(model)
            provider, requestModel, requestOptions = await self._prepareRequest(model, options)
            fetchDeferred = getattr(provider, "fetchDeferred", None)
            if fetchDeferred is None:
                raise ModelsError("provider", f"Provider {model.provider} does not support deferred responses")
            return fetchDeferred(requestModel, handle, requestOptions)

        return lazyStream(model, setup)

    async def fetchDeferred(self, model: Model, handle: DeferredHandle, options: Any = None) -> AssistantMessage:
        return await self.streamDeferred(model, handle, options).result()

    async def cancelDeferred(self, model: Model, handle: DeferredHandle, options: Any = None) -> None:
        assertChatModel(model)
        provider, requestModel, requestOptions = await self._prepareRequest(model, options)
        cancelDeferred = getattr(provider, "cancelDeferred", None)
        if cancelDeferred is None:
            raise ModelsError("provider", f"Provider {model.provider} does not support deferred responses")
        await cancelDeferred(requestModel, handle, requestOptions)

    async def generateImages(self, model: ImageModel, context: ImagesContext, options: Any = None) -> AssistantImages:
        try:
            assertImageModel(model)
            provider, requestModel, requestOptions = await self._prepareRequest(model, options, ProviderImagesOptions)
            generateImages = getattr(provider, "generateImages", None)
            if generateImages is None:
                raise ModelsError("provider", f"Provider {model.provider} does not support image generation")
            return await generateImages(requestModel, context, requestOptions)
        except Exception as error:  # noqa: BLE001 - reported as the result
            return imageErrorResult(model, error, signal_aborted(read_field(options, "signal")))

    async def classify(self, model: ClassifierModel, context: ClassifierContext, options: Any = None) -> ClassifierResult:
        try:
            assertClassifierModel(model)
            assertClassifierInputSupported(model, context)
            provider, requestModel, requestOptions = await self._prepareRequest(
                model, options, ProviderClassifierOptions
            )
            classify = getattr(provider, "classify", None)
            if classify is None:
                raise ModelsError("provider", f"Provider {model.provider} does not support classification")
            return await classify(requestModel, context, requestOptions)
        except Exception as error:  # noqa: BLE001 - reported as the result
            return classifierErrorResult(model, error, signal_aborted(read_field(options, "signal")))

    async def login(
        self,
        providerId: str,
        type: AuthType,
        interaction: AuthInteraction,
        options: LoginOptions | None = None,
        *,
        credentialId: str | None = None,
    ) -> Credential:
        """MISAKA fork: ``credentialId`` stores the credential under a named record of the same
        provider (``provider:account``); the queue and synchronization stay on ``providerId``."""
        signal = operationSignal(getattr(interaction, "signal", None))

        async def task() -> Credential:
            credential = await self._models.login(
                providerId, type, _InteractionWithSignal(interaction, signal), options, credentialId=credentialId
            )
            await self._synchronizeCredentialState(providerId, "login", credential, signal)
            return credential

        return await self._enqueueCredentialOperation(providerId, signal, task)

    async def logout(self, providerId: str, options: AuthOperationOptions | None = None) -> None:
        signal = operationSignal(options.signal if options is not None else None)

        async def task() -> None:
            await self._models.logout(providerId, AuthOperationOptions(signal=signal))
            await self._synchronizeCredentialState(providerId, "logout", None, signal)

        await self._enqueueCredentialOperation(providerId, signal, task)

    async def refresh(self, options: ModelsRefreshOptions | None = None) -> ModelsRefreshResult:
        options = options if options is not None else ModelsRefreshOptions()
        if options.providers is not None and self._coreProviderIds:
            # MISAKA fork: a core provider's catalog follows other providers' credentials (a MoA
            # preset runs only while its aggregator's provider is configured), so it refreshes
            # with any of them: a /login or /logout reaches it.
            options = replace(options, providers=[*options.providers, *self._coreProviderIds])
        self._config = await ModelConfig.load(self._modelsPath)
        self._configureRadiusProviders()
        if options.providers is not None:
            for providerId in dict.fromkeys(options.providers):
                self._recomposeProvider(providerId)
            self._updateModelSnapshot()
        else:
            self._rebuildProviders()
        result = await self._models.refresh(
            replace(
                options,
                allowNetwork=options.allowNetwork if options.allowNetwork is not None else self._modelNetworkEnabled,
            )
        )
        errors = dict(result.errors)
        self._updateModelSnapshot()
        if options.providers is not None:

            async def refreshAvailability(providerId: str) -> None:
                try:
                    await self._refreshProviderAvailability(providerId, operationSignal(options.signal))
                except Exception as error:  # noqa: BLE001 - reported in the result
                    if not signal_aborted(options.signal):
                        errors[providerId] = error

            await asyncio.gather(*(refreshAvailability(providerId) for providerId in dict.fromkeys(options.providers)))
        else:
            try:
                await self._queueAvailabilityRefresh(options.signal)
            except Exception:  # noqa: BLE001, S110
                # Availability errors are recorded by the latest pass; refreshed models remain usable.
                pass
        return ModelsRefreshResult(aborted=result.aborted or signal_aborted(options.signal), errors=errors)

    def _spawnRefresh(self) -> None:
        """``void this.refresh({ allowNetwork: false })``.

        MISAKA fork: a background refresh needs a running event loop; without one, the
        synchronous recomposition the caller already did is what there is until the next refresh.
        It starts eagerly, running up to its first suspension as a JavaScript call does, so a
        refresh the caller awaits right after it still finishes its availability pass last.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = asyncio.Task(self.refresh(ModelsRefreshOptions(allowNetwork=False)), loop=loop, eager_start=True)
        if task.done():
            self._backgroundRefreshDone(task)
            return
        self._backgroundRefreshes.add(task)
        task.add_done_callback(self._backgroundRefreshDone)

    def _backgroundRefreshDone(self, task: asyncio.Task[Any]) -> None:
        self._backgroundRefreshes.discard(task)
        if not task.cancelled():
            task.exception()

    def registerNativeProvider(self, provider: Any) -> None:
        if not jsTrim(provider.id):
            raise RuntimeError("Provider id must not be empty.")
        self._extensionProviders.pop(provider.id, None)
        self._nativeExtensionProviders[provider.id] = provider
        self._recomposeProvider(provider.id)
        self._updateModelSnapshot()
        self._markProvisionallyConfigured(
            provider.id,
            configuredRequestAuthStatus(self._config.getProvider(provider.id), None),
            "oauth" if provider.auth.oauth and not provider.auth.apiKey else "api_key",
        )
        self._spawnRefresh()

    def _markProvisionallyConfigured(
        self, providerId: str, configuredStatus: AuthStatus | None, type: AuthType
    ) -> None:
        """Mark a newly registered provider as configured when it has a stored credential or a configured
        API key. Availability checks run asynchronously, and callers such as initial model selection
        read the snapshot before they finish. The next availability pass replaces this entry.
        """
        if providerId not in self._snapshot.storedProviders and not (configuredStatus and configuredStatus.configured):
            return
        configuredProviders = self._snapshot.configuredProviders | {providerId}
        auth = dict(self._snapshot.auth)
        # Never clobber a real check result.
        if not auth.get(providerId):
            auth[providerId] = AuthCheck(type=type, source="configured provider")
        self._snapshot = replace(
            self._snapshot,
            auth=auth,
            configuredProviders=configuredProviders,
            available=[model for model in self._snapshot.all if model.provider in configuredProviders],
        )

    def registerProvider(self, providerId: str, config: ProviderConfigInput) -> None:
        # MISAKA fork: a model definition may be a model object (pi's models are plain objects, so a
        # registered provider's `models` can be another provider's models); the composer reads mappings.
        if config.get("models") is not None:
            config = {
                **config,
                "models": [
                    definition.model_dump(exclude_unset=True) if hasattr(definition, "model_dump") else definition
                    for definition in config["models"]
                ],
            }
        # Validate the incoming registration on its own, like the legacy registry:
        # a broken re-registration must throw without touching the stored config.
        validateExtensionProvider(providerId, self._builtins.get(providerId), self._config.getProvider(providerId), config)
        self._nativeExtensionProviders.pop(providerId, None)
        # Re-registration merges defined values over the previous registration and
        # preserves undefined ones, matching the legacy ModelRegistry contract.
        effective: dict[str, Any] = dict(self._extensionProviders.get(providerId) or {})
        for key, value in config.items():
            if value is not None:
                effective[key] = value
        self._extensionProviders[providerId] = effective  # type: ignore[assignment]
        self._recomposeProvider(providerId)
        self._updateModelSnapshot()
        self._markProvisionallyConfigured(
            providerId,
            configuredRequestAuthStatus(self._config.getProvider(providerId), effective),  # type: ignore[arg-type]
            "oauth" if effective.get("oauth") and not effective.get("apiKey") else "api_key",
        )
        self._spawnRefresh()

    def unregisterProvider(self, providerId: str) -> None:
        self._extensionProviders.pop(providerId, None)
        self._nativeExtensionProviders.pop(providerId, None)
        self._recomposeProvider(providerId)
        self._updateModelSnapshot()
        self._spawnRefresh()

    def registerVirtualModel(self, definition: VirtualModelDefinition) -> None:
        """Register a virtual model under ``definition.provider``, which may also list physical
        models or several virtual models. Re-registering the same provider and id replaces the
        virtual model. Raises when the id belongs to a physical model of that provider."""
        providerId, id = definition.provider, definition.id
        if not jsTrim(providerId) or not jsTrim(id):
            raise RuntimeError("Virtual model provider and id must not be empty.")
        existing = self._models.getModel(providerId, id)
        if existing is not None and not isVirtualModel(existing):
            raise RuntimeError(f"Virtual model {providerId}/{id} conflicts with a physical model.")
        models = self._virtualModels.get(providerId, {})
        models[id] = _RegisteredVirtualModel(model=createVirtualModel(definition), route=definition.route)
        self._virtualModels[providerId] = models
        if self._recomposeProvider(providerId) is None and providerId not in self._snapshot.configuredProviders:
            # A provider of only virtual models needs no credentials. Mark it configured now: session
            # restore checks auth before the refresh below lands.
            self._snapshot = replace(
                self._snapshot,
                auth={**self._snapshot.auth, providerId: AuthCheck(type="api_key", source="virtual")},
                configuredProviders=self._snapshot.configuredProviders | {providerId},
            )
        self._updateModelSnapshot()
        self._spawnRefresh()

    def unregisterVirtualModel(self, providerId: str, id: str) -> None:
        models = self._virtualModels.get(providerId)
        if models is None or models.pop(id, None) is None:
            return
        if not models:
            del self._virtualModels[providerId]
        self._recomposeProvider(providerId)
        self._updateModelSnapshot()
        self._spawnRefresh()

    async def resolveModel(self, model: Model, messages: Sequence[Message], options: ResolveModelOptions) -> ModelRoute:
        """Ask a virtual model's router for the model and thinking level of one request. The
        router must return a physical catalog model whose provider has credentials; the thinking
        level is clamped to that model. Raises when routing fails.

        ``previous`` reports the latest successful response in ``messages``. A retry passes the
        failed response as ``failed``; ``messages`` no longer contains it. ``state`` is the router
        state stored by the caller, which also stores the returned state.
        """
        name = f"Virtual model {model.provider}/{model.id}"
        virtual = self._virtualModels.get(model.provider, {}).get(model.id)
        if virtual is None:
            raise RuntimeError(f"{name} is not registered.")
        failed = options.get("failed")
        latest = findLatestResponse(messages)
        previousModel = (
            self.getPhysicalModel(read_field(latest, "provider"), read_field(latest, "model"))
            if latest is not None
            else None
        )
        # A failed routing attempt names the virtual model; there is no physical request to report.
        failedModel = self.getPhysicalModel(failed.provider, failed.model) if failed is not None else None
        routed = virtual.route(
            ModelRouteRequest(
                model=model,
                thinkingLevel=options["thinkingLevel"],
                reason=options["reason"],
                messages=messages,
                previous=(
                    PreviousRoute(model=previousModel, thinkingLevel=read_field(latest, "thinkingLevel"))
                    if previousModel is not None
                    else None
                ),
                failed=(
                    FailedRoute(model=failedModel, message=failed, thinkingLevel=failed.thinkingLevel)
                    if failedModel is not None and failed is not None
                    else None
                ),
                state=options.get("state"),
                signal=options.get("signal"),
            )
        )
        route = await routed if inspect.isawaitable(routed) else routed
        target = self.getPhysicalModel(route.model.provider, route.model.id)
        description = f"{name} routed to {route.model.provider}/{route.model.id}"
        if target is None:
            raise RuntimeError(f"{description}, which is not a physical model.")
        if not self.hasConfiguredAuth(target.provider):
            raise RuntimeError(f"{description}, which has no credentials.")
        return ModelRoute(
            model=target, thinkingLevel=clampThinkingLevel(target, route.thinkingLevel), state=route.state
        )

    def getPhysicalModel(self, providerId: str, modelId: str) -> Model | None:
        """A catalog chat model that is not virtual."""
        model = self._models.getModel(providerId, modelId)
        return model if model is not None and not isVirtualModel(model) else None

    # -- MISAKA fork (B7) -------------------------------------------------------------------

    async def recoverRejectedCredential(self, providerId: str, sessionId: str | None = None) -> bool:
        """Replace a stored OAuth token this provider has just refused. True if it changed.

        The refused token is the one the session's last request to the provider carried. The
        refresh runs under the credential store's own lock and only when the stored token
        is still the one that was refused, so several sessions hitting the same rejection refresh
        once between them and the rest read the replacement. A token is recovered from once: a
        second refusal of the same token is the account's answer, not a stale copy, and must
        reach the caller instead of looping, though a session still holding it reads back the
        replacement another session stored. A refresh that could not run does not spend it.
        """
        tokenKey = (providerId, sessionId)
        rejected = self._lastResolvedApiKey.get(tokenKey)
        if not rejected:
            return False
        seen = self._recoveredCredentials.setdefault(providerId, set())
        spent = rejected in seen
        provider = self._models.getProvider(providerId)
        oauth = provider.auth.oauth if provider is not None else None
        if oauth is None or self._credentials.hasRuntimeApiKey(providerId):
            return False
        signal = operationSignal()
        replacement: str | None = None

        async def refreshRejected(current: Credential | None) -> Credential | None:
            nonlocal replacement
            if not isinstance(current, OAuthCredential):
                return None  # a configured key is the user's to replace, not ours
            token = _authToken(await oauth.toAuth(current))
            if token != rejected:
                replacement = token  # another session already replaced it
                return None
            if spent:
                return None  # refused again after its one refresh: the account's answer
            refreshed = await oauth.refresh(current, signal)
            replacement = _authToken(await oauth.toAuth(refreshed))
            return refreshed

        async def task() -> None:
            credential = await self._credentials.modify(
                providerId, refreshRejected, AuthOperationOptions(signal=signal)
            )
            if replacement and replacement != rejected:
                await self._synchronizeCredentialState(providerId, "recoverRejectedCredential", credential, signal)

        try:
            await self._enqueueCredentialOperation(providerId, signal, task)
        except CredentialSynchronizationError:
            pass  # the replacement is stored; the snapshot catches up on the next refresh
        except Exception:  # noqa: BLE001 - a refresh that could not run is not the account's answer
            return False
        seen.add(rejected)  # the store answered; this token is spent whatever it said
        if not replacement or replacement == rejected:
            return False
        self._lastResolvedApiKey[tokenKey] = replacement
        return True


__all__ = [
    "CredentialSynchronizationError",
    "CredentialSynchronizationOperation",
    "ModelRuntime",
    "ModelRuntimeAuthOverrides",
    "is_rejected_credential_error",
    "mergeHeaders",
]
