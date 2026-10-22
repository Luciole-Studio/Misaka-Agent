"""pi-ai's ``models-runtime.test.ts`` cases (v1.3.0) for the provider collection (``misaka/ai/models.py``)."""

from __future__ import annotations

import asyncio
import copy
import time
from types import SimpleNamespace

import pytest

from misaka.ai.auth.credential_store import InMemoryCredentialStore
from misaka.ai.auth.resolve import AuthResolutionOverrides
from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthCheck,
    AuthOperationOptions,
    AuthResult,
    CredentialInfo,
    LoginOptions,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProviderAuth,
)
from misaka.ai.models import (
    CreateModelsOptions,
    CreateProviderOptions,
    ModelsPublication,
    ModelsRefreshOptions,
    calculateCost,
    createModels,
    createProvider,
    hasApi,
    modelsAreEqual,
)
from misaka.ai.models_store import InMemoryModelsStore, ModelsStoreEntry
from misaka.ai.types import (
    AssistantMessage,
    Context,
    DoneEvent,
    Model,
    StartEvent,
    Usage,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.models_error import ModelsError
from misaka.utils.js import AbortController, AbortSignal


def _model(provider: str, id: str, **overrides) -> Model:
    return Model.model_validate({
        "id": id, "name": id, "api": "test-api", "provider": provider, "baseUrl": "https://example.test/v1",
        "reasoning": False, "input": ["text"], "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 10000, "maxTokens": 1000, **overrides,
    })


def _done(model: Model, text: str) -> AssistantMessage:
    return AssistantMessage.model_validate({
        "content": [{"type": "text", "text": text}], "api": model.api, "provider": model.provider, "model": model.id,
        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
        "stopReason": "stop", "timestamp": 0,
    })


async def _ambient_resolve(*, ctx, credential, signal):
    return AuthResult(auth=ModelAuth())


AMBIENT = ApiKeyAuth(name="Ambient", resolve=_ambient_resolve)


def _env_key_auth(key: str | None) -> ApiKeyAuth:
    async def resolve(*, ctx, credential, signal):
        resolved = (credential.key if credential is not None else None) or key
        if not resolved:
            return None
        return AuthResult(auth=ModelAuth(apiKey=resolved), source="stored" if credential else "env")

    return ApiKeyAuth(name="Test API key", resolve=resolve)


class _Provider:
    def __init__(self, id: str, models=None, auth=None, getModels=None, refreshModels=None, calls=None):
        self.id = id
        self.name = id
        self.auth = auth or ProviderAuth(apiKey=AMBIENT)
        self._models = models if models is not None else [_model(id, "model-a")]
        self._getModels = getModels
        self.refreshModels = refreshModels
        self._calls = calls

    def getModels(self):
        return self._getModels() if self._getModels else self._models

    def _respond(self, model, options):
        if self._calls is not None:
            self._calls.append(SimpleNamespace(model=model, options=options))
        stream = AssistantMessageEventStream()
        message = _done(model, "ok")
        stream.push(StartEvent(partial=message))
        stream.push(DoneEvent(reason="stop", message=message))
        stream.end(message)
        return stream

    def stream(self, model, context, options=None):
        return self._respond(model, options)

    def streamSimple(self, model, context, options=None):
        return self._respond(model, options)


CONTEXT = Context(messages=[UserMessage(content="hi", timestamp=0)])


def test_registers_replaces_and_deletes_providers():
    models = createModels()
    models.setProvider(_Provider("p1"))
    models.setProvider(_Provider("p2"))
    assert [p.id for p in models.getProviders()] == ["p1", "p2"]

    replacement = _Provider("p1")
    models.setProvider(replacement)
    assert models.getProvider("p1") is replacement and len(models.getProviders()) == 2

    models.deleteProvider("p1")
    assert models.getProvider("p1") is None
    models.clearProviders()
    assert models.getProviders() == []


def test_lists_and_finds_models_per_provider_and_narrows_with_has_api():
    models = createModels()
    models.setProvider(_Provider("p1", models=[_model("p1", "m1"), _model("p1", "m2")]))
    models.setProvider(_Provider("p2", models=[_model("p2", "m3")]))

    assert [m.id for m in models.getModels()] == ["m1", "m2", "m3"]
    assert [m.id for m in models.getModels("p1")] == ["m1", "m2"]
    assert models.getModels("nope") == []
    found = models.getModel("p2", "m3")
    assert found.id == "m3" and models.getModel("p2", "missing") is None
    assert hasApi(found, "openai-completions") is False and hasApi(found, "test-api") is True
    assert modelsAreEqual(found, _model("p2", "m3")) and not modelsAreEqual(found, None)


def test_swallows_provider_source_failures():
    def boom():
        raise RuntimeError("boom")

    models = createModels()
    models.setProvider(_Provider("broken", getModels=boom))
    models.setProvider(_Provider("ok", models=[_model("ok", "m1")]))

    assert [m.id for m in models.getModels()] == ["m1"]
    assert models.getModels("broken") == []


async def test_refresh_updates_dynamic_providers_and_reports_failures():
    state = {"list": [_model("dyn", "before")], "refreshes": 0}

    async def refresh(context):
        if not context.allowNetwork:
            return
        state["refreshes"] += 1

        def update():
            state["list"] = [_model("dyn", "after")]

        await context.publish(ModelsPublication(update=update))

    models = createModels()
    models.setProvider(_Provider("dyn", getModels=lambda: state["list"], refreshModels=refresh))
    models.setProvider(_Provider("static", models=[_model("static", "s1")]))

    first = await models.refresh()
    assert first.errors == {} and state["refreshes"] == 1
    assert models.getModel("dyn", "after") is not None and models.getModel("dyn", "before") is None

    async def flaky(context):
        if context.allowNetwork:
            raise RuntimeError("fetch failed")

    models.setProvider(_Provider("flaky", refreshModels=flaky))
    second = await models.refresh()
    assert state["refreshes"] == 2 and str(second.errors["flaky"]) == "fetch failed"


async def test_restricts_refresh_work_to_selected_providers():
    calls: list[str] = []
    models = createModels()
    for id in ("one", "two"):
        async def refresh(context, id=id):
            calls.append(f"{id}:{'network' if context.allowNetwork else 'cache'}")

        models.setProvider(_Provider(id, refreshModels=refresh))

    result = await models.refresh(ModelsRefreshOptions(providers=["two", "unknown"]))

    assert result.errors == {} and calls == ["two:cache", "two:network"]


async def test_stops_waiting_on_abort_when_a_provider_ignores_its_signal():
    controller = AbortController()
    started = asyncio.Event()
    stalled: asyncio.Future = asyncio.get_running_loop().create_future()
    calls = {"n": 0}

    async def refresh(context):
        calls["n"] += 1
        if calls["n"] != 1:
            return
        started.set()
        await stalled

    models = createModels()
    models.setProvider(_Provider("dynamic", refreshModels=refresh))

    pending = asyncio.ensure_future(models.refresh(ModelsRefreshOptions(signal=controller.signal)))
    await started.wait()
    controller.abort()
    result = await asyncio.wait_for(pending, 5)

    assert result.aborted is True and result.errors == {}
    stalled.set_exception(RuntimeError("late provider failure"))
    await asyncio.sleep(0)
    assert result.errors == {}


async def test_rejects_late_publication_from_a_superseded_non_cooperative_provider():
    store = InMemoryModelsStore()
    state = {"value": "initial", "calls": 0}
    first_started = asyncio.Event()
    first_blocked = asyncio.Event()

    async def refresh(context):
        if not context.allowNetwork:
            return
        state["calls"] += 1
        current = state["calls"]
        if current == 1:
            first_started.set()
            await first_blocked.wait()
        value = f"generation-{current}"

        def update():
            state["value"] = value

        await context.publish(ModelsPublication(
            persist=ModelsStoreEntry(models=[_model("dynamic", value)]), persist_is_set=True, update=update
        ))

    models = createModels(CreateModelsOptions(modelsStore=store))
    models.setProvider(_Provider("dynamic", refreshModels=refresh))

    first = asyncio.ensure_future(models.refresh(ModelsRefreshOptions(providers=["dynamic"])))
    await first_started.wait()
    await models.refresh(ModelsRefreshOptions(providers=["dynamic"]))
    await asyncio.wait_for(first, 5)
    first_blocked.set()
    await asyncio.sleep(0.01)

    assert state["value"] == "generation-2"
    assert (await store.read("dynamic")).models[0].id == "generation-2"


async def test_runs_login_and_logout_through_the_credential_store_and_forwards_login_options():
    credentials = InMemoryCredentialStore()
    received = {}
    apiKey = _env_key_auth(None)

    async def login(interaction, options=None):
        received["options"] = options
        return ApiKeyCredential(key="logged-in")

    apiKey.login = login
    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=apiKey)))
    interaction = SimpleNamespace(signal=None, prompt=None, notify=lambda event: None)
    options = LoginOptions(getDeviceId=lambda: "device")

    credential = await models.login("p1", "api_key", interaction, options)

    assert credential.key == "logged-in" and received["options"] is options
    assert (await credentials.read("p1")).key == "logged-in"
    await models.logout("p1")
    assert await credentials.read("p1") is None

    async def interaction_only_login(interaction):
        return ApiKeyCredential(key="legacy")

    apiKey.login = interaction_only_login
    assert (await models.login("p1", "api_key", interaction, options)).key == "legacy"


async def test_merges_resolved_auth_into_stream_options_explicit_options_win():
    calls: list = []

    async def resolve(*, ctx, credential, signal):
        return AuthResult(auth=ModelAuth(
            apiKey="resolved-key",
            headers={"Authorization": "Bearer resolved-key", "x-a": "auth", "x-b": "auth"},
            baseUrl="https://auth.test/v1",
        ))

    models = createModels()
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=resolve)), calls=calls))
    model = _model("p1", "model-a")

    result = await models.completeSimple(
        model, CONTEXT, {"apiKey": "explicit-key", "headers": {"authorization": "Explicit token", "x-b": "explicit"}}
    )
    assert result.stopReason == "stop"
    assert calls[0].options.apiKey == "explicit-key"
    assert calls[0].options.headers == {"authorization": "Explicit token", "x-a": "auth", "x-b": "explicit"}
    assert calls[0].model.baseUrl == "https://auth.test/v1"

    await models.completeSimple(model, CONTEXT)
    assert calls[1].options.apiKey == "resolved-key"


async def test_unknown_providers_and_non_chat_models_produce_error_streams():
    models = createModels()
    result = await models.completeSimple(_model("ghost", "model-a"), CONTEXT)
    assert result.stopReason == "error" and "Unknown provider: ghost" in result.errorMessage

    models.setProvider(_Provider("p1"))
    image = _model("p1", "pic").model_construct(**{**_model("p1", "pic").__dict__, "type": "image"})
    result = await models.completeSimple(image, CONTEXT)
    assert result.errorMessage == "Model p1/pic is not a chat model"


async def test_streams_through_the_provider():
    models = createModels()
    models.setProvider(_Provider("p1"))
    stream = models.streamSimple(_model("p1", "model-a"), CONTEXT)
    events = [event.type async for event in stream]
    assert events == ["start", "done"] and (await stream.result()).stopReason == "stop"


async def test_deferred_responses_are_forwarded_only_when_an_api_supports_them():
    fetched = []

    class Api:
        def stream(self, model, context, options=None):
            raise AssertionError("not used")

        streamSimple = stream

    class DeferredApi(Api):
        def fetchDeferred(self, model, handle, options=None):
            fetched.append(handle)
            return _Provider("p1")._respond(model, options)

    plain = createProvider(CreateProviderOptions(id="plain", auth=ProviderAuth(apiKey=AMBIENT), api=Api(),
                                                 models=[_model("plain", "m")]))
    deferred = createProvider(CreateProviderOptions(id="deferred", auth=ProviderAuth(apiKey=AMBIENT), api=DeferredApi(),
                                                    models=[_model("deferred", "m")]))
    assert plain.fetchDeferred is None and deferred.fetchDeferred is not None

    models = createModels()
    models.setProvider(plain)
    models.setProvider(deferred)
    handle = SimpleNamespace(id="h")
    assert (await models.fetchDeferred(_model("deferred", "m"), handle)).stopReason == "stop"
    assert fetched == [handle]
    result = await models.fetchDeferred(_model("plain", "m"), handle)
    assert result.errorMessage == "Provider plain does not support deferred responses"


def test_openrouter_maps_both_of_its_catalog_apis():
    from misaka.ai.providers.all import builtinProviders

    openrouter = next(provider for provider in builtinProviders() if provider.id == "openrouter")
    apis = {model.api for model in openrouter.getModels()}
    assert apis <= set(openrouter._byApi) and "anthropic-messages" in apis


@pytest.mark.parametrize("expires_in_ms", [60 * 60_000])
async def test_credential_metadata_lists_without_secrets(expires_in_ms):
    from misaka.ai.auth.types import OAuthCredential

    credentials = InMemoryCredentialStore()

    async def api_key(_current):
        return ApiKeyCredential(key="secret")

    async def oauth(_current):
        return OAuthCredential(access="a", refresh="r", expires=int(time.time() * 1000) + expires_in_ms)

    await credentials.modify("api-provider", api_key)
    await credentials.modify("oauth-provider", oauth)
    listed = [(info.providerId, info.type) for info in await credentials.list()]
    assert listed == [("api-provider", "api_key"), ("oauth-provider", "oauth")]


# --- MISAKA regressions (B4 review) ---------------------------------------------------------


async def test_a_provider_deleted_as_its_refresh_starts_does_not_publish():
    # upstream's `map(async ...)` begins every provider's generation before refresh() yields.
    store = InMemoryModelsStore()
    published: list[str] = []

    async def refresh(context):
        await asyncio.sleep(0.01)
        await context.publish(ModelsPublication(
            persist=ModelsStoreEntry(models=[_model("dynamic", "fresh")]),
            persist_is_set=True,
            update=lambda: published.append("fresh"),
        ))

    models = createModels(CreateModelsOptions(modelsStore=store))
    models.setProvider(_Provider("dynamic", refreshModels=refresh))
    task = asyncio.ensure_future(models.refresh(ModelsRefreshOptions(providers=["dynamic"])))
    await asyncio.sleep(0)
    models.deleteProvider("dynamic")
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0.05)

    assert published == []
    assert await store.read("dynamic") is None


async def test_transform_headers_may_return_any_awaitable():
    calls: list = []
    models = createModels()
    models.setProvider(_Provider("p1", calls=calls))

    def transform(headers):
        future = asyncio.get_running_loop().create_future()
        future.set_result({**headers, "x-transformed": "yes"})
        return future

    await models.completeSimple(_model("p1", "model-a"), CONTEXT, {"transformHeaders": transform})

    assert calls[0].options.headers == {"x-transformed": "yes"}


def _testOAuth(refresh) -> OAuthAuth:
    async def login(_interaction, _options=None):
        raise RuntimeError("not used")

    async def unchanged(credential, _signal):
        return credential

    async def toAuth(credential):
        return ModelAuth(apiKey=credential.access)

    return OAuthAuth(name="Test OAuth", login=login, refresh=refresh or unchanged, toAuth=toAuth)


async def _storedOAuth(credentials: InMemoryCredentialStore) -> None:
    async def write(_current):
        return OAuthCredential(type="oauth", access="old", refresh="old-refresh", expires=0)

    await credentials.modify("p1", write)


async def _waitForRefreshToken(credentials: InMemoryCredentialStore, refresh: str) -> None:
    for _ in range(200):
        stored = await credentials.read("p1")
        if stored is not None and stored.refresh == refresh:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"credential never reached refresh={refresh!r}")


async def test_persists_an_oauth_refresh_that_started_before_the_model_refresh_was_cancelled():
    credentials = InMemoryCredentialStore()
    await _storedOAuth(credentials)
    controller = AbortController()
    models = createModels(CreateModelsOptions(credentials=credentials))

    async def refresh(credential, _signal):
        controller.abort()
        return credential.model_copy(
            update={"access": "new", "refresh": "new-refresh", "expires": int(time.time() * 1000) + 60_000}
        )

    async def refreshModels(_context):
        return None

    models.setProvider(
        _Provider("p1", auth=ProviderAuth(oauth=_testOAuth(refresh)), refreshModels=refreshModels)
    )

    assert (await models.refresh(ModelsRefreshOptions(signal=controller.signal))).aborted is True
    await _waitForRefreshToken(credentials, "new-refresh")


# Radius bug report 01a10855-43d8-7447-a710-2acf5ee0b2ee: refresh_token_invalidated after a cancelled refresh.
async def test_persists_an_oauth_refresh_that_started_before_the_request_was_cancelled():
    credentials = InMemoryCredentialStore()
    await _storedOAuth(credentials)
    controller = AbortController()
    models = createModels(CreateModelsOptions(credentials=credentials))

    async def refresh(credential, _signal):
        # The provider has rotated old-refresh by the time the request is cancelled.
        controller.abort()
        return credential.model_copy(
            update={"access": "new", "refresh": "new-refresh", "expires": int(time.time() * 1000) + 60_000}
        )

    models.setProvider(_Provider("p1", auth=ProviderAuth(oauth=_testOAuth(refresh))))

    with pytest.raises(Exception) as error:
        await models.getAuth("p1", AuthResolutionOverrides(signal=controller.signal))
    assert getattr(error.value, "name", None) == "AbortError"
    await _waitForRefreshToken(credentials, "new-refresh")


# --- models-runtime.test.ts: credentials and OAuth --------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _store(credentials, providerId: str, credential) -> None:
    async def write(_current):
        return credential

    await credentials.modify(providerId, write)


def _oauthCredential(**fields) -> OAuthCredential:
    return OAuthCredential.model_validate({"type": "oauth", **fields})


async def test_cancels_queued_credential_mutations_without_running_them_later():
    credentials = InMemoryCredentialStore()
    firstBlocked = asyncio.Event()
    secondRan: list[bool] = []

    async def firstFn(_current):
        await firstBlocked.wait()
        return ApiKeyCredential(type="api_key", key="first")

    first = credentials.modify("p1", firstFn)
    controller = AbortController()

    async def secondFn(_current):
        secondRan.append(True)
        return ApiKeyCredential(type="api_key", key="second")

    second = credentials.modify("p1", secondFn, AuthOperationOptions(signal=controller.signal))

    controller.abort()
    with pytest.raises(Exception):  # noqa: B017 - upstream matches only the AbortError name
        await second
    firstBlocked.set()
    await first
    await asyncio.sleep(0)

    assert secondRan == []
    assert await credentials.read("p1") == ApiKeyCredential(type="api_key", key="first")


async def test_resolves_auth_stored_credential_owns_the_provider_ambient_only_when_nothing_stored():
    credentials = InMemoryCredentialStore()
    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=_env_key_auth("env-key"), oauth=_testOAuth(None))))
    model = _model("p1", "model-a")

    # model and provider-id overloads resolve the same provider-scoped auth
    assert (await models.getAuth(model)).auth.apiKey == "env-key"
    assert (await models.getAuth(model.provider)).auth.apiKey == "env-key"
    assert (await models.getAuth(model, AuthResolutionOverrides(apiKey="explicit-key"))).auth.apiKey == "explicit-key"

    # stored oauth credential (persisted via the single write path): beats ambient env
    await _store(credentials, "p1", _oauthCredential(access="oauth-token", refresh="r", expires=_now_ms() + 10 * 60_000))
    resolution = await models.getAuth(model.provider)
    assert resolution.auth.apiKey == "oauth-token"
    assert resolution.source == "OAuth"

    # stored api-key credential resolves through apiKey auth, beats env
    await _store(credentials, "p1", ApiKeyCredential(type="api_key", key="stored-key"))
    apiKeyResolution = await models.getAuth(model.provider)
    assert apiKeyResolution.auth.apiKey == "stored-key"
    assert apiKeyResolution.source == "stored"


async def test_checks_provider_auth_without_refreshing_oauth_and_filters_available_models():
    credentials = InMemoryCredentialStore()
    refreshes: list[bool] = []
    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("ambient", auth=ProviderAuth(apiKey=_env_key_auth("env-key"))))
    models.setProvider(_Provider("missing", auth=ProviderAuth(apiKey=_env_key_auth(None))))

    async def refresh(credential, _signal):
        refreshes.append(True)
        return credential

    models.setProvider(_Provider("oauth", auth=ProviderAuth(oauth=_testOAuth(refresh))))
    await _store(credentials, "oauth", _oauthCredential(access="expired", refresh="refresh", expires=0))

    assert await models.checkAuth("ambient") == AuthCheck(source="env", type="api_key")
    assert await models.checkAuth("missing") is None
    assert await models.checkAuth("oauth") == AuthCheck(source="OAuth", type="oauth")
    assert refreshes == []
    assert [model.provider for model in await models.getAvailable()] == ["ambient", "oauth"]
    assert [model.provider for model in await models.getAvailable("ambient")] == ["ambient"]


async def test_runs_provider_login_and_logout_through_the_credential_store():
    credentials = InMemoryCredentialStore()

    async def login(_interaction):
        return ApiKeyCredential(type="api_key", key="logged-in")

    apiKey = _env_key_auth(None)
    apiKey.login = login
    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=apiKey)))

    async def prompt(_prompt):
        return "unused"

    credential = await models.login("p1", "api_key", SimpleNamespace(prompt=prompt, notify=lambda _event: None))
    assert credential == ApiKeyCredential(type="api_key", key="logged-in")
    assert await credentials.read("p1") == credential

    await models.logout("p1")
    assert await credentials.read("p1") is None


async def test_a_stored_credential_without_a_matching_handler_blocks_ambient_fallback():
    credentials = InMemoryCredentialStore()
    models = createModels(CreateModelsOptions(credentials=credentials))
    # provider has only apiKey auth, but an oauth credential is stored (stale config)
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=_env_key_auth("env-key"))))
    await _store(credentials, "p1", _oauthCredential(access="a", refresh="r", expires=0))

    assert await models.getAuth("p1") is None


async def test_refreshes_expired_oauth_credentials_and_persists_the_rotated_credential():
    credentials = InMemoryCredentialStore()

    async def refresh(credential, _signal):
        return credential.model_copy(update={"access": "new-token", "expires": _now_ms() + 60 * 60_000})

    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("p1", auth=ProviderAuth(oauth=_testOAuth(refresh))))
    await _store(credentials, "p1", _oauthCredential(access="old-token", refresh="r", expires=0))

    resolution = await models.getAuth("p1")
    assert resolution.auth.apiKey == "new-token"
    assert (await credentials.read("p1")).access == "new-token"


def _countingRefresh(calls: list[bool]):
    async def refresh(credential, _signal):
        calls.append(True)
        return credential.model_copy(update={"access": "new-token", "expires": _now_ms() + 60 * 60_000})

    return refresh


async def test_refreshes_oauth_credentials_with_less_than_five_minutes_remaining():
    credentials = InMemoryCredentialStore()
    calls: list[bool] = []
    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("p1", auth=ProviderAuth(oauth=_testOAuth(_countingRefresh(calls)))))
    await _store(credentials, "p1", _oauthCredential(access="old-token", refresh="r", expires=_now_ms() + 60_000))

    assert (await models.getAuth("p1")).auth.apiKey == "new-token"
    assert len(calls) == 1


async def test_honors_a_callers_longer_oauth_minimum_validity():
    credentials = InMemoryCredentialStore()
    calls: list[bool] = []
    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("p1", auth=ProviderAuth(oauth=_testOAuth(_countingRefresh(calls)))))
    await _store(credentials, "p1", _oauthCredential(access="old-token", refresh="r", expires=_now_ms() + 10 * 60_000))

    resolution = await models.getAuth("p1", AuthResolutionOverrides(minOAuthValidityMs=30 * 60_000))
    assert resolution.auth.apiKey == "new-token"
    assert len(calls) == 1


async def test_rejects_with_code_oauth_when_refresh_fails_preserving_the_stored_credential():
    credentials = InMemoryCredentialStore()

    async def refresh(_credential, _signal):
        raise RuntimeError("invalid_grant")

    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("p1", auth=ProviderAuth(oauth=_testOAuth(refresh))))
    await _store(credentials, "p1", _oauthCredential(access="old", refresh="r", expires=0))

    with pytest.raises(ModelsError) as caught:
        await models.getAuth("p1")
    assert caught.value.code == "oauth"
    # credential preserved for retry / re-login
    assert (await credentials.read("p1")).access == "old"


async def test_serializes_concurrent_oauth_refreshes_through_store_modify_no_double_refresh():
    credentials = InMemoryCredentialStore()
    await _store(credentials, "p1", _oauthCredential(access="old", refresh="r1", expires=0))

    refreshes: list[bool] = []

    async def refresh(_credential, _signal):
        refreshes.append(True)
        await asyncio.sleep(0.01)
        return _oauthCredential(access=f"new-{len(refreshes)}", refresh="r2", expires=_now_ms() + 60 * 60_000)

    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("p1", auth=ProviderAuth(oauth=_testOAuth(refresh))))
    model = _model("p1", "model-a")

    a, b = await asyncio.gather(models.getAuth(model.provider), models.getAuth(model.provider))
    assert len(refreshes) == 1
    assert a.auth.apiKey == "new-1"
    assert b.auth.apiKey == "new-1"


async def test_valid_oauth_tokens_resolve_without_touching_modify():
    modifies: list[str] = []
    base = InMemoryCredentialStore()

    class CountingStore:
        def read(self, pid, options=None):
            return base.read(pid)

        def list(self, options=None):
            return base.list()

        def modify(self, pid, fn, options=None):
            modifies.append(pid)
            return base.modify(pid, fn)

        def delete(self, pid, options=None):
            return base.delete(pid)

    await _store(base, "p1", _oauthCredential(access="valid", refresh="r", expires=_now_ms() + 10 * 60_000))
    models = createModels(CreateModelsOptions(credentials=CountingStore()))
    models.setProvider(_Provider("p1", auth=ProviderAuth(oauth=_testOAuth(None))))

    assert (await models.getAuth("p1")).auth.apiKey == "valid"
    assert modifies == []


async def test_wraps_credential_store_failures_in_models_error():
    # read failure
    class ReadFailing:
        async def read(self, _pid, options=None):
            raise RuntimeError("disk on fire")

        async def list(self, options=None):
            return []

        async def modify(self, _pid, _fn, options=None):
            return None

        async def delete(self, _pid, options=None):
            return None

    models = createModels(CreateModelsOptions(credentials=ReadFailing()))
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=_env_key_auth("env-key"))))
    with pytest.raises(ModelsError) as caught:
        await models.getAuth("p1")
    assert caught.value.code == "auth"

    # modify failure during refresh
    class ModifyFailing(ReadFailing):
        async def read(self, _pid, options=None):
            return _oauthCredential(access="old", refresh="r", expires=0)

        async def list(self, options=None):
            return [CredentialInfo(providerId="p1", type="oauth")]

        async def modify(self, _pid, _fn, options=None):
            raise RuntimeError("disk on fire")

    oauthModels = createModels(CreateModelsOptions(credentials=ModifyFailing()))
    oauthModels.setProvider(_Provider("p1", auth=ProviderAuth(oauth=_testOAuth(None))))
    with pytest.raises(ModelsError) as caught:
        await oauthModels.getAuth("p1")
    assert caught.value.code == "auth"


async def test_keeps_the_underlying_reason_in_wrapped_oauth_refresh_errors():
    credentials = InMemoryCredentialStore()
    await _store(credentials, "p1", _oauthCredential(access="old", refresh="r", expires=0))

    async def refresh(_credential, _signal):
        raise RuntimeError("token refresh failed (400): invalid_grant")

    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("p1", auth=ProviderAuth(oauth=_testOAuth(refresh))))

    with pytest.raises(ModelsError, match=r"OAuth refresh failed for p1: token refresh failed \(400\): invalid_grant"):
        await models.getAuth("p1")


async def test_wraps_api_key_auth_failures_in_models_error():
    async def resolve(*, ctx, credential, signal):
        raise RuntimeError("nope")

    models = createModels()
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=ApiKeyAuth(name="Failing", resolve=resolve))))
    with pytest.raises(ModelsError) as caught:
        await models.getAuth("p1")
    assert caught.value.code == "auth"


# --- models-runtime.test.ts (v1.1.0): pricing, catalog reads, refresh and auth signals -----------


def test_applies_request_wide_pricing_tiers_above_the_configured_input_threshold():
    model = _model("openai", "gpt-5.6-sol", cost={
        "input": 5, "output": 30, "cacheRead": 0.5, "cacheWrite": 6.25,
        "tiers": [{"inputTokensAbove": 272000, "input": 10, "output": 45, "cacheRead": 1, "cacheWrite": 12.5}],
    })

    def createUsage(cacheWrite: int) -> Usage:
        return Usage.model_validate({
            "input": 200000, "output": 100000, "cacheRead": 72000, "cacheWrite": cacheWrite,
            "totalTokens": 372000 + cacheWrite,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        })

    short = calculateCost(model, createUsage(0))
    assert (short.input, short.output, short.cacheRead, short.cacheWrite) == (1, 3, 0.036, 0)

    long = calculateCost(model, createUsage(1))
    assert long.input == 2
    assert long.output == 4.5
    assert long.cacheRead == 0.072
    assert long.cacheWrite == 0.0000125


async def test_keeps_chat_reads_independent_from_the_all_model_catalog():
    provider = _Provider("chat-only")

    def unavailable():
        raise RuntimeError("all models unavailable")

    provider.getAllModels = unavailable
    models = createModels()
    models.setProvider(provider)

    assert [model.id for model in models.getModels("chat-only")] == ["model-a"]
    assert models.getModel("chat-only", "model-a").id == "model-a"
    assert [model.id for model in await models.getAvailable("chat-only")] == ["model-a"]
    assert models.getAllModels("chat-only") == []


def _streams() -> SimpleNamespace:
    return SimpleNamespace(
        stream=lambda *_args: AssistantMessageEventStream(),
        streamSimple=lambda *_args: AssistantMessageEventStream(),
    )


async def test_restores_cached_models_before_waiting_for_network_auth():
    store = InMemoryModelsStore()
    await store.write("dynamic", ModelsStoreEntry(models=[_model("dynamic", "cached")]))
    authStarted = asyncio.Event()
    blockedAuth = asyncio.Event()

    async def resolve(*, ctx, credential, signal):
        authStarted.set()
        await blockedAuth.wait()
        return AuthResult(auth=ModelAuth(apiKey="key"))

    async def fetchModels(_context):
        raise RuntimeError("must not fetch")

    provider = createProvider(CreateProviderOptions(
        id="dynamic",
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Blocked auth", resolve=resolve)),
        models=[],
        fetchModels=fetchModels,
        api=_streams(),
    ))
    models = createModels(CreateModelsOptions(modelsStore=store))
    models.setProvider(provider)
    controller = AbortController()
    pending = asyncio.ensure_future(models.refresh(ModelsRefreshOptions(providers=["dynamic"], signal=controller.signal)))
    await authStarted.wait()

    assert models.getModel("dynamic", "cached") is not None
    controller.abort()
    assert (await pending).aborted is True
    blockedAuth.set()


async def test_lets_providers_choose_persistent_deletion_and_ephemeral_publication_atomically():
    state = {"entry": ModelsStoreEntry(models=[_model("dynamic", "stored")]), "value": "initial"}

    class Store:
        async def read(self, _providerId, _options=None):
            return state["entry"]

        async def write(self, _providerId, next, _options=None):
            state["entry"] = next

        async def delete(self, _providerId, _options=None):
            state["entry"] = None

    async def refresh(context):
        assert context.stored.models[0].id == "stored"

        def deleted():
            assert state["entry"] is None
            state["value"] = "deleted"

        await context.publish(ModelsPublication(persist=None, persist_is_set=True, update=deleted))

        def ephemeral():
            state["value"] = "ephemeral"

        await context.publish(ModelsPublication(update=ephemeral))

    models = createModels(CreateModelsOptions(modelsStore=Store()))
    models.setProvider(_Provider("dynamic", refreshModels=refresh))

    result = await models.refresh(ModelsRefreshOptions(allowNetwork=False))

    assert result.errors == {}
    assert state["entry"] is None
    assert state["value"] == "ephemeral"


async def test_persists_dynamic_catalogs_and_restores_them_without_network_access():
    credentials = InMemoryCredentialStore()
    modelsStore = InMemoryModelsStore()
    await _store(credentials, "dynamic", ApiKeyCredential(type="api_key", key="key"))

    def createDynamicProvider(fetchModels):
        return createProvider(CreateProviderOptions(
            id="dynamic",
            auth=ProviderAuth(apiKey=_env_key_auth(None)),
            models=[],
            fetchModels=(lambda _context: fetchModels()) if fetchModels else None,
            api=_streams(),
        ))

    async def fetched():
        return [_model("dynamic", "fetched")]

    online = createModels(CreateModelsOptions(credentials=credentials, modelsStore=modelsStore))
    online.setProvider(createDynamicProvider(fetched))
    assert (await online.refresh()).errors == {}
    assert online.getModel("dynamic", "fetched") is not None

    async def mustNotFetch():
        raise RuntimeError("must not fetch")

    offline = createModels(CreateModelsOptions(credentials=credentials, modelsStore=modelsStore))
    offline.setProvider(createDynamicProvider(mustNotFetch))
    assert (await offline.refresh(ModelsRefreshOptions(allowNetwork=False))).errors == {}
    assert offline.getModel("dynamic", "fetched") is not None


async def test_passes_effective_api_key_credentials_and_refresh_options_while_skipping_unconfigured_providers():
    seen: dict = {"unconfiguredRefreshes": 0}
    models = createModels()

    async def configured(context):
        if not context.allowNetwork:
            return
        seen["effectiveCredential"] = context.credential
        seen["forceRefresh"] = context.force

    async def unconfigured(context):
        if context.allowNetwork:
            seen["unconfiguredRefreshes"] += 1

    models.setProvider(_Provider("configured", auth=ProviderAuth(apiKey=_env_key_auth("ambient-key")),
                                 refreshModels=configured))
    models.setProvider(_Provider("unconfigured", auth=ProviderAuth(apiKey=_env_key_auth(None)),
                                 refreshModels=unconfigured))

    await models.refresh(ModelsRefreshOptions(force=True))
    assert seen["effectiveCredential"] == ApiKeyCredential(type="api_key", key="ambient-key", env=None)
    assert seen["forceRefresh"] is True
    assert seen["unconfiguredRefreshes"] == 0


async def test_passes_the_registered_providers_base_url_including_wrapper_overrides_to_refresh_models():
    # A models.json baseUrl (e.g. a regional OpenRouter host) is applied by a wrapper around the
    # built-in provider. The built-in refresh must still request that URL.
    baseUrls: list[str | None] = []

    async def refreshModels(context):
        baseUrls.append(context.baseUrl)

    inner = _Provider("dynamic", refreshModels=refreshModels)
    inner.baseUrl = "https://default.example.test/v1"
    # `{ ...inner, baseUrl: "https://regional.example.test/v1" }`.
    wrapped = copy.copy(inner)
    wrapped.baseUrl = "https://regional.example.test/v1"
    models = createModels()
    models.setProvider(wrapped)

    await models.refresh()
    assert baseUrls == ["https://regional.example.test/v1", "https://regional.example.test/v1"]


async def test_refreshes_expired_oauth_before_refreshing_models():
    credentials = InMemoryCredentialStore()
    seen: dict = {}
    await _store(credentials, "oauth-dynamic", _oauthCredential(access="expired", refresh="refresh", expires=0))

    async def refresh(_credential, _signal):
        return _oauthCredential(access="fresh", refresh="rotated", expires=_now_ms() + 60_000)

    async def refreshModels(context):
        if context.allowNetwork:
            seen["modelRefreshCredential"] = context.credential

    models = createModels(CreateModelsOptions(credentials=credentials))
    models.setProvider(_Provider("oauth-dynamic", auth=ProviderAuth(oauth=_testOAuth(refresh)),
                                 refreshModels=refreshModels))

    assert (await models.refresh()).errors == {}
    credential = seen["modelRefreshCredential"]
    assert (credential.type, credential.access, credential.refresh) == ("oauth", "fresh", "rotated")
    stored = await credentials.read("oauth-dynamic")
    assert (stored.access, stored.refresh) == ("fresh", "rotated")


async def test_always_gives_providers_a_concrete_signal():
    seen: dict = {}

    async def refresh(context):
        seen["signal"] = context.signal

    models = createModels()
    models.setProvider(_Provider("dynamic", refreshModels=refresh))

    result = await models.refresh()
    assert result.aborted is False
    assert isinstance(seen["signal"], AbortSignal)
    assert seen["signal"].aborted is False


async def test_binds_model_store_waits_to_the_provider_refresh_signal():
    storageSignals: list = []

    class Store:
        async def read(self, _providerId, options=None):
            storageSignals.append(options.signal if options is not None else None)

        async def write(self, _providerId, _entry, options=None):
            storageSignals.append(options.signal if options is not None else None)

        async def delete(self, _providerId, options=None):
            storageSignals.append(options.signal if options is not None else None)

    seen: dict = {}

    async def refresh(context):
        seen["providerSignal"] = context.signal
        if not context.allowNetwork:
            return
        await context.publish(ModelsPublication(
            persist=ModelsStoreEntry(models=[_model("dynamic", "fresh")]), persist_is_set=True
        ))

    models = createModels(CreateModelsOptions(modelsStore=Store()))
    models.setProvider(_Provider("dynamic", auth=ProviderAuth(apiKey=_env_key_auth("key")), refreshModels=refresh))

    result = await models.refresh(ModelsRefreshOptions(providers=["dynamic"]))

    assert result.errors == {}
    assert len(storageSignals) == 3
    assert all(signal is seen["providerSignal"] for signal in storageSignals)


async def test_returns_aborted_state_without_reporting_cancellation_as_a_provider_error():
    controller = AbortController()

    async def refresh(context):
        controller.abort()
        if context.signal.aborted:
            return

    models = createModels()
    models.setProvider(_Provider("dynamic", refreshModels=refresh))

    result = await models.refresh(ModelsRefreshOptions(signal=controller.signal))
    assert result.aborted is True
    assert result.errors == {}


async def test_passes_caller_signals_to_provider_auth_callbacks():
    controller = AbortController()
    received: list = []

    async def login(interaction):
        received.append(interaction.signal)
        return ApiKeyCredential(type="api_key", key="saved")

    async def check(*, ctx, credential, signal):
        received.append(signal)
        return AuthCheck(type="api_key")

    async def resolve(*, ctx, credential, signal):
        received.append(signal)
        return AuthResult(auth=ModelAuth(apiKey="resolved"))

    apiKey = ApiKeyAuth(name="Signal auth", resolve=resolve, login=login, check=check)
    models = createModels()
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=apiKey)))

    async def prompt(_prompt):
        return "unused"

    await models.checkAuth("p1", AuthOperationOptions(signal=controller.signal))
    await models.getAuth("p1", AuthResolutionOverrides(signal=controller.signal))
    await models.login("p1", "api_key", SimpleNamespace(signal=controller.signal, prompt=prompt, notify=lambda _event: None))

    assert received == [controller.signal, controller.signal, controller.signal]
    assert all(signal is controller.signal for signal in received)


async def test_stops_waiting_for_non_cooperative_auth_callbacks():
    checkStarted = asyncio.Event()
    blockedCheck = asyncio.Event()
    resolveStarted = asyncio.Event()
    blockedResolve = asyncio.Event()

    async def check(*, ctx, credential, signal):
        checkStarted.set()
        await blockedCheck.wait()
        return AuthCheck(type="api_key")

    async def resolve(*, ctx, credential, signal):
        resolveStarted.set()
        await blockedResolve.wait()
        return AuthResult(auth=ModelAuth(apiKey="key"))

    models = createModels()
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=ApiKeyAuth(name="Blocked auth", resolve=resolve,
                                                                           check=check))))

    availableController = AbortController()
    available = asyncio.ensure_future(models.getAvailable(None, AuthOperationOptions(signal=availableController.signal)))
    await checkStarted.wait()
    availableController.abort()
    with pytest.raises(Exception) as caught:
        await available
    assert getattr(caught.value, "name", None) == "AbortError"

    authController = AbortController()
    auth = asyncio.ensure_future(models.getAuth("p1", AuthResolutionOverrides(signal=authController.signal)))
    await resolveStarted.wait()
    authController.abort()
    with pytest.raises(Exception) as caught:
        await auth
    assert getattr(caught.value, "name", None) == "AbortError"

    blockedCheck.set()
    blockedResolve.set()


async def test_uses_explicit_request_api_key_and_env_during_provider_auth_resolution():
    calls: list = []

    async def resolve(*, ctx, credential, signal):
        account = (credential.env or {}).get("ACCOUNT_ID") if credential is not None else None
        if account is None:
            account = await ctx.env("ACCOUNT_ID")
        if credential is None or not credential.key or not account:
            return None
        return AuthResult(
            auth=ModelAuth(apiKey=credential.key, baseUrl=f"https://example.test/{account}"),
            env={"ACCOUNT_ID": account},
        )

    models = createModels()
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=ApiKeyAuth(name="Scoped", resolve=resolve)),
                                 calls=calls))
    model = _model("p1", "model-a")

    await models.completeSimple(model, CONTEXT, {"apiKey": "explicit-key", "env": {"ACCOUNT_ID": "acct"}})

    assert calls[0].model.baseUrl == "https://example.test/acct"
    assert calls[0].options.apiKey == "explicit-key"
    assert calls[0].options.env == {"ACCOUNT_ID": "acct"}


async def test_adds_model_headers_only_for_model_auth_and_transforms_assembled_headers_once():
    calls: list = []
    models = createModels()
    models.setProvider(_Provider("p1", auth=ProviderAuth(apiKey=_env_key_auth("key")), calls=calls))
    model = _model("p1", "model-a", headers={"x-model": "model", "x-shared": "model"})

    assert (await models.getAuth("p1")).auth.headers is None
    assert (await models.getAuth(model)).auth.headers == {"x-model": "model", "x-shared": "model"}

    transforms: list = []

    async def transformHeaders(headers):
        transforms.append(headers)
        assert headers == {"x-model": "model", "x-explicit": "explicit", "X-Shared": "explicit"}
        return {**headers, "x-transformed": "yes"}

    await models.completeSimple(model, CONTEXT, {
        "headers": {"x-explicit": "explicit", "X-Shared": "explicit"},
        "transformHeaders": transformHeaders,
    })

    assert len(transforms) == 1
    assert calls[0].options.headers == {
        "x-model": "model",
        "x-explicit": "explicit",
        "X-Shared": "explicit",
        "x-transformed": "yes",
    }
    assert "transformHeaders" not in (calls[0].options.model_extra or {})
    assert getattr(calls[0].options, "transformHeaders", None) is None
