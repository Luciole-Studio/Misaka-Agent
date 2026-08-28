"""pi's current model runtime, translated from ``packages/ai/src/models.ts``.

This is the layer misaka never had. ``ai/models.py`` is the static side: a provider is a
string and the catalog is a generated table (``ai/models_generated.py``). Here a provider
is an object that owns its auth, its models, and its stream behaviour, and ``Models`` is
the collection that resolves auth and routes each request to the provider that owns the
model.

It lands beside the existing layer rather than on top of it: ``ai/types.py`` already
binds the name ``Provider`` to a ``str`` alias, so the protocol below keeps to its own
module instead of taking that name over. ``provider_definitions.py`` and
``radius_provider.py`` build on this module; ``ai/models.py`` and the older ``providers/``
dispatch path are untouched by it.

Two mechanisms carry most of the file's weight and neither is decoration:

* **Refresh generations.** Replacing or deleting a provider bumps its generation and
  aborts the refresh in flight. A publication from the superseded refresh is dropped
  rather than written, so a slow catalog fetch cannot resurrect a provider the app has
  already swapped out.
* **Publication chains.** Each provider's publications run one at a time, and each
  re-checks the generation after every await, because the store write and the in-memory
  update must not straddle a supersede.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from misaka.ai.api_lazy import lazy_stream
from misaka.ai.auth.context import defaultProviderAuthContext
from misaka.ai.auth.credential_store import InMemoryCredentialStore
from misaka.ai.auth.resolve import (
    AuthResolutionOverrides,
    ModelsError,
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
    CredentialStore,
    CredentialValue,
    OAuthCredential,
    ProviderAuth,
    ProviderEnv,
    ProviderHeaders,
)
from misaka.ai.models_store import InMemoryModelsStore, ModelsStoreEntry
from misaka.ai.types import AssistantMessage, Context, Model, ProviderStreamOptions
from misaka.ai.utils.abort import (
    AbortController,
    combine_abort_signals,
    race_with_abort_signal,
    wait_for_abort,
)
from misaka.ai.utils.abort import throw_if_aborted as _throwIfAborted
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.utils.values import signal_aborted

# --------------------------------------------------------------------------------------
# Refresh contracts
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ModelsPublication:
    """What a provider hands back from a refresh phase.

    ``persist`` omitted leaves storage untouched; ``persist=None`` is only a deletion when
    ``persist_is_set`` says the provider actually asked for it -- upstream distinguishes
    ``undefined`` from ``null`` and Python has one word for both.
    """

    persist: ModelsStoreEntry | None = None
    persist_is_set: bool = False
    update: Callable[[], None] | None = None


@dataclass(slots=True)
class RefreshModelsContext:
    # Effective configured credential. OAuth credentials are refreshed before network access.
    credential: CredentialValue | None
    # Immutable provider-scoped catalog snapshot captured before this refresh phase.
    stored: ModelsStoreEntry | None
    # Generation-checked publication. Returns False when this refresh has been superseded.
    publish: Callable[[ModelsPublication], Awaitable[bool]]
    # False during offline/cache-only initialization.
    allowNetwork: bool
    signal: Any
    # Bypass provider freshness checks and fetch immediately when network access is allowed.
    force: bool | None = None


@dataclass(slots=True)
class ModelsRefreshOptions:
    allowNetwork: bool = True
    # Restrict refresh to these provider ids. Unknown and static providers are ignored.
    providers: Sequence[str] | None = None
    force: bool | None = None
    signal: Any = None


@dataclass(slots=True)
class ModelsRefreshResult:
    aborted: bool
    errors: dict[str, BaseException] = field(default_factory=dict)


class Provider(Protocol):
    """The concrete runtime unit: identity, auth, model listing, stream behaviour."""

    id: str
    name: str
    auth: ProviderAuth

    def getModels(self) -> list[Model]:
        """Current known models, synchronously.

        Static providers return their catalog; dynamic providers return the list as of the
        last refresh (empty before the first). Must not raise -- ``Models`` treats a
        raising implementation as having no models.
        """
        ...

    def stream(
        self, model: Model, context: Context, options: Any = None
    ) -> AssistantMessageEventStream: ...

    def streamSimple(
        self, model: Model, context: Context, options: Any = None
    ) -> AssistantMessageEventStream: ...


def _mergeHeaders(
    base: ProviderHeaders | None, override: ProviderHeaders | None
) -> ProviderHeaders | None:
    """Override wins, matching header names case-insensitively.

    A base entry whose name matches an override's case-insensitively is deleted from the
    merged mapping before the override's own spelling is written in.

    Only *absent on both sides* yields ``None``. Upstream writes that test as
    ``if (!base && !override)``, which cannot be transcribed as ``if not base and not
    override``: an empty object is truthy in JavaScript and falsy in Python, so the
    literal reading turns ``{}`` into ``None`` where upstream returns ``{}``.
    """
    if base is None and override is None:
        return None
    merged: ProviderHeaders = dict(base or {})
    for name, value in (override or {}).items():
        lowered = name.lower()
        for existing in [key for key in merged if key.lower() == lowered]:
            del merged[existing]
        merged[name] = value
    return merged




class ModelsImpl:
    """The provider collection. Providers own stream behaviour; this resolves auth."""

    def __init__(
        self,
        credentials: CredentialStore | None = None,
        modelsStore: Any = None,
        authContext: AuthContext | None = None,
    ) -> None:
        self._providers: dict[str, Provider] = {}
        self._credentials: CredentialStore = credentials or InMemoryCredentialStore()
        self._modelsStore = modelsStore or InMemoryModelsStore()
        self._authContext: AuthContext = authContext or defaultProviderAuthContext()
        self._refreshGenerations: dict[str, int] = {}
        self._refreshControllers: dict[str, AbortController] = {}
        self._publicationLocks: dict[str, asyncio.Lock] = {}

    # -- collection ---------------------------------------------------------------

    def setProvider(self, provider: Provider) -> None:
        """Upsert by ``provider.id``, superseding any refresh the old one had running."""
        self._supersedeProviderRefresh(provider.id)
        self._providers[provider.id] = provider

    def deleteProvider(self, id: str) -> None:
        self._supersedeProviderRefresh(id)
        self._providers.pop(id, None)

    def clearProviders(self) -> None:
        for id in set(self._providers) | set(self._refreshControllers):
            self._supersedeProviderRefresh(id)
        self._providers.clear()

    def getProviders(self) -> list[Provider]:
        return list(self._providers.values())

    def getProvider(self, id: str) -> Provider | None:
        return self._providers.get(id)

    def getModels(self, provider: str | None = None) -> list[Model]:
        """Last-known models, best-effort: a provider that raises yields none."""
        if provider is not None:
            entry = self._providers.get(provider)
            if entry is None:
                return []
            try:
                return list(entry.getModels())
            except Exception:  # noqa: BLE001 - an ill-behaved provider has no models
                return []

        models: list[Model] = []
        for entry in self._providers.values():
            # Reuse the single-provider path so "a provider that raises has no models"
            # is written once instead of twice.
            models.extend(self.getModels(entry.id))
        return models

    def getModel(self, provider: str, id: str) -> Model | None:
        return next((model for model in self.getModels(provider) if model.id == id), None)

    # -- refresh ------------------------------------------------------------------

    def _supersedeProviderRefresh(self, providerId: str) -> int:
        generation = self._refreshGenerations.get(providerId, 0) + 1
        self._refreshGenerations[providerId] = generation
        previous = self._refreshControllers.pop(providerId, None)
        if previous is not None:
            previous.abort()
        return generation

    def _beginProviderRefresh(self, providerId: str) -> tuple[int, AbortController]:
        generation = self._supersedeProviderRefresh(providerId)
        controller = AbortController()
        self._refreshControllers[providerId] = controller
        return generation, controller

    def _publicationLock(self, providerId: str) -> asyncio.Lock:
        lock = self._publicationLocks.get(providerId)
        if lock is None:
            lock = asyncio.Lock()
            self._publicationLocks[providerId] = lock
        return lock

    async def _publishProviderModels(
        self,
        providerId: str,
        generation: int,
        signal: Any,
        publication: ModelsPublication,
    ) -> bool:
        """Persist then update, dropping the whole thing if this refresh was superseded.

        The generation is re-checked after the store write as well as before it: the write
        awaits, and a supersede that lands in that window must not be followed by an
        in-memory update that belongs to the provider we just replaced.
        """
        async with self._publicationLock(providerId):
            if signal_aborted(signal) or self._refreshGenerations.get(providerId) != generation:
                return False

            if publication.persist_is_set:
                if publication.persist is None:
                    await self._modelsStore.delete(providerId, _storeOptions(signal))
                else:
                    await self._modelsStore.write(
                        providerId, publication.persist.model_copy(deep=True), _storeOptions(signal)
                    )

            if signal_aborted(signal) or self._refreshGenerations.get(providerId) != generation:
                return False
            if publication.update is not None:
                publication.update()
            return True

    async def _runProviderRefreshPhase(
        self,
        provider: Provider,
        credential: CredentialValue | None,
        allowNetwork: bool,
        force: bool | None,
        generation: int,
        signal: Any,
    ) -> None:
        stored = await self._modelsStore.read(provider.id, _storeOptions(signal))

        async def publish(publication: ModelsPublication) -> bool:
            return await self._publishProviderModels(provider.id, generation, signal, publication)

        await provider.refreshModels(
            RefreshModelsContext(
                credential=credential,
                stored=stored.model_copy(deep=True) if stored is not None else None,
                publish=publish,
                allowNetwork=allowNetwork,
                force=force if allowNetwork else None,
                signal=signal,
            )
        )

    async def refresh(self, options: ModelsRefreshOptions | None = None) -> ModelsRefreshResult:
        """Refresh configured dynamic providers concurrently.

        Provider errors and cancellation are reported in the result rather than raised.
        """
        options = options or ModelsRefreshOptions()
        callerSignal = options.signal
        errors: dict[str, BaseException] = {}
        if signal_aborted(callerSignal):
            return ModelsRefreshResult(aborted=True, errors=errors)

        selected = set(options.providers) if options.providers is not None else None
        refreshable = [
            provider
            for provider in self._providers.values()
            if getattr(provider, "refreshModels", None) is not None
            and (selected is None or provider.id in selected)
        ]

        async def refreshOne(provider: Provider) -> None:
            generation, controller = self._beginProviderRefresh(provider.id)
            signal = combine_abort_signals(callerSignal, controller) or controller
            try:
                storedCredential: CredentialValue | None = None
                credentialError: BaseException | None = None
                try:
                    storedCredential = await self._readCredential(provider.id, signal)
                except Exception as error:  # noqa: BLE001 - reported after cache restore
                    credentialError = error

                # Restore cached provider state before auth resolution or network access,
                # so a provider is usable offline and stays usable if the network phase fails.
                await self._runProviderRefreshPhase(
                    provider, storedCredential, False, None, generation, signal
                )
                if credentialError is not None:
                    raise credentialError
                if not options.allowNetwork or signal.aborted:
                    return

                credential = await self._resolveRefreshCredential(provider, storedCredential, signal)
                if credential is None:
                    return
                await self._runProviderRefreshPhase(
                    provider, credential, True, options.force, generation, signal
                )
            except Exception as error:  # noqa: BLE001 - collected, never raised out of refresh
                if not signal.aborted:
                    errors[provider.id] = error
            finally:
                if self._refreshControllers.get(provider.id) is controller:
                    del self._refreshControllers[provider.id]

        sweep = asyncio.gather(*(refreshOne(provider) for provider in refreshable))
        if callerSignal is None:
            await sweep
        else:
            # Upstream races the whole sweep against the caller's signal (models.ts:440).
            # Without it an aborted `refresh()` still waits for every provider's fetch to
            # finish on its own -- the caller asked to stop and keeps hanging anyway. The
            # abandoned work is still observed inside `refreshOne`, which records errors
            # rather than raising.
            try:
                await race_with_abort_signal(sweep, callerSignal)
            except RuntimeError:
                if not signal_aborted(callerSignal):
                    raise
        return ModelsRefreshResult(aborted=signal_aborted(callerSignal), errors=dict(errors))

    async def _resolveRefreshCredential(
        self, provider: Provider, stored: CredentialValue | None, signal: Any
    ) -> CredentialValue | None:
        if isinstance(stored, OAuthCredential):
            oauth = provider.auth.oauth
            if oauth is None:
                return None
            if time.time() * 1000 < stored.expires:
                return stored
            if signal_aborted(signal):
                return None

            async def refreshUnderLock(current: CredentialValue | None) -> CredentialValue | None:
                if not isinstance(current, OAuthCredential) or time.time() * 1000 < current.expires:
                    return None
                return await oauth.refresh(current, signal)

            post = await self._credentials.modify(
                provider.id, refreshUnderLock, _operationOptions(signal)
            )
            return post if isinstance(post, OAuthCredential) else None

        apiKey = provider.auth.apiKey
        if apiKey is None:
            return None
        credential = stored if isinstance(stored, ApiKeyCredential) else None
        result = await apiKey.resolve(ctx=self._authContext, credential=credential, signal=signal)
        if result is None:
            return None
        return ApiKeyCredential(key=result.auth.apiKey, env=result.env)

    # -- auth ---------------------------------------------------------------------

    async def _readCredential(self, providerId: str, signal: Any) -> CredentialValue | None:
        try:
            return await self._credentials.read(providerId, _operationOptions(signal))
        except Exception as error:
            raise ModelsError(
                "auth", f"Credential store read failed for {providerId}", error
            ) from error

    async def _checkProviderAuth(
        self, provider: Provider, credential: CredentialValue | None, signal: Any
    ) -> AuthCheck | None:
        if isinstance(credential, OAuthCredential):
            return (
                AuthCheck(source="OAuth", type="oauth") if provider.auth.oauth is not None else None
            )
        apiKey = provider.auth.apiKey
        if apiKey is None:
            return None
        if apiKey.check is not None:
            try:
                return await apiKey.check(
                    ctx=self._authContext,
                    credential=credential if isinstance(credential, ApiKeyCredential) else None,
                    signal=signal,
                )
            except Exception as error:
                raise ModelsError(
                    "auth", f"API key auth check failed for provider {provider.id}", error
                ) from error

        resolution = await resolveProviderAuth(
            provider,
            self._credentials,
            self._authContext,
            AuthResolutionOverrides(signal=signal),
        )
        return AuthCheck(source=resolution.source, type="api_key") if resolution else None

    async def checkAuth(
        self, providerId: str, options: AuthOperationOptions | None = None
    ) -> AuthCheck | None:
        """Whether a provider has complete auth configuration, without refreshing OAuth."""
        signal = options.signal if options is not None else None
        _throwIfAborted(signal)
        provider = self._providers.get(providerId)
        if provider is None:
            return None
        return await self._checkProviderAuth(
            provider, await self._readCredential(providerId, signal), signal
        )

    async def getAvailable(
        self, providerId: str | None = None, options: AuthOperationOptions | None = None
    ) -> list[Model]:
        """Models whose providers have complete auth configuration."""
        signal = options.signal if options is not None else None
        _throwIfAborted(signal)
        providers = (
            [p for p in [self._providers.get(providerId)] if p is not None]
            if providerId
            else self.getProviders()
        )

        async def checkOne(provider: Provider):
            credential = await self._readCredential(provider.id, signal)
            return provider, credential, await self._checkProviderAuth(provider, credential, signal)

        available: list[Model] = []
        checks = asyncio.gather(*(checkOne(provider) for provider in providers))
        gathered = await (
            race_with_abort_signal(checks, signal) if signal is not None else checks
        )
        for provider, credential, auth in gathered:
            if auth is None:
                continue
            models = provider.getModels()
            filterModels = getattr(provider, "filterModels", None)
            available.extend(filterModels(models, credential) if filterModels else models)
        return available

    async def getAuth(
        self, providerOrModel: str | Model, overrides: AuthResolutionOverrides | None = None
    ) -> AuthResult | None:
        """Provider-scoped auth, plus static model headers when passed a model."""
        providerId = providerOrModel if isinstance(providerOrModel, str) else providerOrModel.provider
        provider = self._providers.get(providerId)
        if provider is None:
            return None
        result = await resolveProviderAuth(
            provider, self._credentials, self._authContext, overrides or AuthResolutionOverrides()
        )
        if result is None or isinstance(providerOrModel, str) or not providerOrModel.headers:
            return result
        return result.model_copy(
            update={
                "auth": result.auth.model_copy(
                    update={"headers": _mergeHeaders(result.auth.headers, providerOrModel.headers)}
                )
            }
        )

    async def login(
        self, providerId: str, type: AuthType, interaction: AuthInteraction
    ) -> CredentialValue:
        """Run a provider-owned login flow and persist the credential it returns.

        The abort handling is upstream's and is deliberate: once the store write has
        begun, an abort no longer fails the login. The credential exists by then, and
        reporting failure would leave the user logged in but told otherwise.
        """
        signal = getattr(interaction, "signal", None)
        _throwIfAborted(signal)
        provider = self._providers.get(providerId)
        if provider is None:
            raise ModelsError("provider", f"Unknown provider: {providerId}")
        method = provider.auth.oauth if type == "oauth" else provider.auth.apiKey
        login = getattr(method, "login", None) if method is not None else None
        if login is None:
            raise ModelsError("auth", f"{provider.name} does not support {type} login")

        credential = await login(interaction)

        started = asyncio.Event()

        async def persist(current: CredentialValue | None) -> CredentialValue | None:
            started.set()
            return credential

        mutation = asyncio.ensure_future(
            self._credentials.modify(providerId, persist, _operationOptions(signal))
        )
        waitStarted = asyncio.ensure_future(started.wait())
        aborting = asyncio.ensure_future(wait_for_abort(signal)) if signal is not None else None
        try:
            watching = {waitStarted, mutation} | ({aborting} if aborting is not None else set())
            await asyncio.wait(watching, return_when=asyncio.FIRST_COMPLETED)
            if not started.is_set() and signal_aborted(signal):
                mutation.cancel()
                raise RuntimeError("Request was aborted")
            await mutation
        except ModelsError:
            raise
        except RuntimeError as error:
            # An abort is a `RuntimeError` here (`throw_if_aborted`), and it must reach the
            # caller as one. Upstream tells the two apart by exception type; this port
            # cannot -- a storage backend raising `RuntimeError` is indistinguishable --
            # so the signal itself is asked. Without this, a store failure that happened to
            # be a `RuntimeError` escaped the `ModelsError` wrapping every other one gets.
            if signal_aborted(signal):
                raise
            raise ModelsError(
                "auth", f"Credential store modify failed for {providerId}", error
            ) from error
        except Exception as error:  # storage failure, reported as auth
            raise ModelsError(
                "auth", f"Credential store modify failed for {providerId}", error
            ) from error
        finally:
            waitStarted.cancel()
            if aborting is not None:
                aborting.cancel()
        return credential

    async def logout(self, providerId: str, options: AuthOperationOptions | None = None) -> None:
        signal = options.signal if options is not None else None
        _throwIfAborted(signal)
        try:
            await self._credentials.delete(providerId, _operationOptions(signal))
        except Exception as error:
            _throwIfAborted(signal)
            raise ModelsError(
                "auth", f"Credential store delete failed for {providerId}", error
            ) from error

    # -- requests -----------------------------------------------------------------

    def _requireProvider(self, model: Model) -> Provider:
        provider = self._providers.get(model.provider)
        if provider is None:
            raise ModelsError("provider", f"Unknown provider: {model.provider}")
        return provider

    async def _applyAuth(self, model: Model, options: Any) -> tuple[Model, ProviderStreamOptions]:
        """Resolve auth into request options. Explicit options win per field."""
        self._requireProvider(model)
        resolution = await self.getAuth(
            model,
            AuthResolutionOverrides(
                apiKey=_option(options, "apiKey"),
                env=_option(options, "env"),
                signal=_option(options, "signal"),
            ),
        )
        if resolution is None:
            raise ModelsError("auth", f"Provider is not configured: {model.provider}")
        auth = resolution.auth

        apiKey = _option(options, "apiKey") or auth.apiKey
        headers = _mergeHeaders(auth.headers, _option(options, "headers"))
        transform = _option(options, "transformHeaders")
        if transform is not None:
            produced = transform(headers or {})
            headers = await produced if asyncio.iscoroutine(produced) else produced
        optionEnv: ProviderEnv | None = _option(options, "env")
        env = {**(resolution.env or {}), **(optionEnv or {})} if (resolution.env or optionEnv) else None

        requestModel = model.model_copy(update={"baseUrl": auth.baseUrl}) if auth.baseUrl else model
        return requestModel, _mergeRequestOptions(options, apiKey=apiKey, headers=headers, env=env)

    def stream(self, model: Model, context: Context, options: Any = None) -> AssistantMessageEventStream:
        async def setup():
            provider = self._requireProvider(model)
            requestModel, requestOptions = await self._applyAuth(model, options)
            return provider.stream(requestModel, context, requestOptions)

        return lazy_stream(model, setup)

    async def complete(self, model: Model, context: Context, options: Any = None):
        return await self.stream(model, context, options).result()

    def streamSimple(
        self, model: Model, context: Context, options: Any = None
    ) -> AssistantMessageEventStream:
        async def setup():
            provider = self._requireProvider(model)
            requestModel, requestOptions = await self._applyAuth(model, options)
            return provider.streamSimple(requestModel, context, requestOptions)

        return lazy_stream(model, setup)

    async def completeSimple(self, model: Model, context: Context, options: Any = None):
        return await self.streamSimple(model, context, options).result()

    async def fetchDeferred(self, model: Model, handle: Any, options: Any = None) -> AssistantMessage:
        """Collect a response the provider parked earlier.

        Deferred (batch) responses are fetched, not streamed, but the fetch still runs
        behind ``lazy_stream``: auth resolution is async and the provider hands back an
        event stream either way, so the result is awaited off the same seam ``stream``
        uses rather than a second one.
        """

        async def setup():
            provider = self._requireProvider(model)
            fetch = getattr(provider, "fetchDeferred", None)
            if fetch is None:
                raise ModelsError(
                    "provider", f"Provider {model.provider} does not support deferred responses"
                )
            requestModel, requestOptions = await self._applyAuth(model, options)
            return fetch(requestModel, handle, requestOptions)

        return await lazy_stream(model, setup).result()

    async def cancelDeferred(self, model: Model, handle: Any, options: Any = None) -> None:
        """Abandon a parked response. Not a stream, so failures raise rather than terminate one."""
        provider = self._requireProvider(model)
        cancel = getattr(provider, "cancelDeferred", None)
        if cancel is None:
            raise ModelsError(
                "provider", f"Provider {model.provider} does not support deferred responses"
            )
        requestModel, requestOptions = await self._applyAuth(model, options)
        await cancel(requestModel, handle, requestOptions)


def _option(options: Any, name: str) -> Any:
    if options is None:
        return None
    if isinstance(options, dict):
        return options.get(name)
    return getattr(options, name, None)


def _mergeRequestOptions(
    options: Any, *, apiKey: str | None, headers: ProviderHeaders | None, env: ProviderEnv | None
) -> ProviderStreamOptions:
    """Fold resolved auth into the caller's options, as an object rather than a mapping.

    Upstream spreads the options object (``{...providerOptions, apiKey, headers, env}``) and
    the result is still read with dot access downstream. misaka's providers do the same --
    ``providers/simple_options.py`` reads ``options.temperature`` and friends -- so handing
    them a plain dict makes every ``streamSimple`` fail on the first attribute.

    Fields are copied by attribute, never through ``model_dump()``: dumping turns
    ``thinkingBudgets`` into a plain dict, and ``simple_options`` calls ``model_dump()`` on
    whatever it is handed there (``providers/anthropic.py`` passes ``options.thinkingBudgets``
    straight into ``adjust_max_tokens_for_thinking``). The non-data fields survive a dump
    unharmed -- ``signal``/``onPayload``/``onResponse`` are ``Any``-typed and come back by
    identity -- so they are not the reason.

    ``ProviderStreamOptions`` is the target because it is the only *stream* options model
    in ``ai/types.py`` with ``extra="allow"``: ``env`` is declared on none of them, and
    neither are ``SimpleStreamOptions``'s ``reasoning``/``thinkingBudgets`` when the caller
    passed those.
    """
    merged: dict[str, Any] = {}
    if options is not None:
        if isinstance(options, dict):
            merged = dict(options)
        else:
            for name in type(options).model_fields:
                merged[name] = getattr(options, name, None)
            merged.update(getattr(options, "model_extra", None) or {})
    merged.pop("transformHeaders", None)
    merged.update({"apiKey": apiKey, "headers": headers, "env": env})
    return ProviderStreamOptions(**merged)


def _operationOptions(signal: Any) -> AuthOperationOptions:
    return AuthOperationOptions(signal=signal)


def _storeOptions(signal: Any):
    from misaka.ai.models_store import ModelsStoreOperationOptions

    return ModelsStoreOperationOptions(signal=signal)


def createModels(
    credentials: CredentialStore | None = None,
    modelsStore: Any = None,
    authContext: AuthContext | None = None,
) -> ModelsImpl:
    """A mutable ``Models`` collection with in-memory defaults for anything not supplied."""
    return ModelsImpl(credentials=credentials, modelsStore=modelsStore, authContext=authContext)


@dataclass(slots=True)
class CreateProviderOptions:
    id: str
    # Required -- every provider has auth semantics, even ambient/keyless ones.
    auth: ProviderAuth
    # Single implementation, or a mapping keyed by ``model.api`` for mixed-API providers.
    api: Any
    # Static baseline model list (empty for purely dynamic providers).
    models: Sequence[Model] = ()
    name: str | None = None
    baseUrl: str | None = None
    headers: ProviderHeaders | None = None
    # Fetch a dynamic model overlay. createProvider restores and publishes it transactionally.
    fetchModels: Callable[[RefreshModelsContext], Awaitable[Sequence[Model]]] | None = None
    filterModels: Callable[[Sequence[Model], CredentialValue | None], Sequence[Model]] | None = None


class _BuiltProvider:
    """What ``createProvider`` returns.

    In misaka only the built-in factories in ``provider_definitions.py`` build one;
    models.json customs go through ``core/model_registry.py``, which merges ``Model``
    objects and never constructs a provider. Upstream routes both here (``models.ts:757``).
    """

    def __init__(self, options: CreateProviderOptions) -> None:
        self._options = options
        self.id = options.id
        self.name = options.name or options.id
        self.baseUrl = options.baseUrl
        self.headers = options.headers
        self.auth = options.auth
        self._baseline: list[Model] = list(options.models)
        self._dynamic: list[Model] = []
        self.filterModels = options.filterModels
        single = options.api if callable(getattr(options.api, "stream", None)) else None
        self._single = single
        self._byApi = None if single is not None else dict(options.api or {})
        if options.fetchModels is None:
            # Static providers must not advertise refreshModels: `Models.refresh` selects
            # on its presence, and an empty implementation would make every refresh look
            # like it covered a provider it never touched.
            self.refreshModels = None

    def getModels(self) -> list[Model]:
        """Baseline overlaid with the dynamic list: same id replaces, new id appends."""
        merged = list(self._baseline)
        index = {model.id: position for position, model in enumerate(merged)}
        for model in self._dynamic:
            if model.id in index:
                merged[index[model.id]] = model
            else:
                index[model.id] = len(merged)
                merged.append(model)
        return merged

    def _apiFor(self, model: Model) -> Any:
        return self._single if self._single is not None else (self._byApi or {}).get(model.api)

    def _dispatch(self, model: Model, run: Callable[[Any], AssistantMessageEventStream]):
        streams = self._apiFor(model)
        if streams is None:
            async def fail():
                raise ModelsError(
                    "stream", f'Provider {self.id} has no API implementation for "{model.api}"'
                )

            return lazy_stream(model, fail)
        return run(streams)

    def stream(self, model: Model, context: Context, options: Any = None):
        return self._dispatch(model, lambda streams: streams.stream(model, context, options))

    def streamSimple(self, model: Model, context: Context, options: Any = None):
        return self._dispatch(model, lambda streams: streams.streamSimple(model, context, options))

    async def refreshModels(self, context: RefreshModelsContext) -> None:
        """Restore the persisted overlay, then fetch a newer one when allowed.

        The restore is published first and on its own, so a provider comes back with its
        last known catalog even when the network phase is skipped or fails. A publication
        that returns False means this refresh was superseded, and the fetch is abandoned
        rather than written over whatever replaced it.
        """
        fetchModels = self._options.fetchModels
        if fetchModels is None:
            return
        if context.stored is not None:
            restored = [model for model in context.stored.models if model.provider == self.id]

            def applyRestored() -> None:
                self._dynamic = restored

            if not await context.publish(ModelsPublication(update=applyRestored)):
                return
        if not context.allowNetwork or signal_aborted(context.signal):
            return
        refreshed = list(await fetchModels(context))
        if signal_aborted(context.signal):
            return

        def applyRefreshed() -> None:
            self._dynamic = refreshed

        await context.publish(
            ModelsPublication(
                persist=ModelsStoreEntry(models=refreshed, checkedAt=int(time.time() * 1000)),
                persist_is_set=True,
                update=applyRefreshed,
            )
        )


def createProvider(options: CreateProviderOptions) -> _BuiltProvider:
    """Build a provider from parts.

    A single ``api`` streams every model; an ``api`` mapping dispatches on ``model.api``,
    and a model whose api has no entry produces a stream error rather than a raise --
    the caller is already holding a stream by then.
    """
    return _BuiltProvider(options)


__all__ = [
    "AbortController",
    "CreateProviderOptions",
    "ModelsPublication",
    "ModelsRefreshOptions",
    "ModelsRefreshResult",
    "Provider",
    "RefreshModelsContext",
    "createModels",
    "createProvider",
]
