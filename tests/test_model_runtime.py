"""pi's ``model-runtime-*.test.ts`` and ``virtual-models.test.ts`` runtime cases
(``misaka/core/model_runtime.py``), plus misaka's B7 recovery and per-account login forks.

The cases that go through ``ModelRegistry`` or ``createAgentSession`` live with those.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from misaka.ai.auth.credential_store import InMemoryCredentialStore
from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthCheck,
    AuthOperationOptions,
    AuthResult,
    CredentialInfo,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    ProviderAuth,
    SecretPrompt,
)
from misaka.ai.models import (
    CreateProviderOptions,
    ModelsRefreshOptions,
    createProvider,
    getSupportedThinkingLevels,
)
from misaka.ai.models_store import InMemoryModelsStore
from misaka.ai.types import (
    AssistantMessage,
    Context,
    DeferredHandle,
    DoneEvent,
    Model,
    StartEvent,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_runtime import (
    CredentialSynchronizationError,
    ModelRuntime,
    is_rejected_credential_error,
)
from misaka.core.virtual_models import ModelRoute, VirtualModelDefinition
from misaka.utils.js import AbortController
from tests.test_pi_099_port import _openai_wire


def _now_ms() -> int:
    return int(time.time() * 1000)


def _interaction(signal=None, prompt=None):
    async def default_prompt(_prompt):
        return "unused"

    return SimpleNamespace(prompt=prompt or default_prompt, notify=lambda _event: None, signal=signal)


def _testModel(id: str) -> dict:
    return {
        "id": id, "name": id, "reasoning": False, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 10000, "maxTokens": 1000,
    }


def _model(provider: str, id: str = "dynamic", **overrides) -> Model:
    return Model.model_validate({
        "id": id, "name": id, "api": "openai-completions", "provider": provider,
        "baseUrl": "https://example.test/v1", "reasoning": False, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000, "maxTokens": 100, **overrides,
    })


def _message(model: Model, text: str = "ok", **overrides) -> AssistantMessage:
    return AssistantMessage.model_validate({
        "content": [{"type": "text", "text": text}], "api": model.api, "provider": model.provider,
        "model": model.id,
        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                  "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
        "stopReason": "stop", "timestamp": 0, **overrides,
    })


def _answer(message: AssistantMessage) -> AssistantMessageEventStream:
    stream = AssistantMessageEventStream()
    stream.push(StartEvent(partial=message))
    stream.push(DoneEvent(reason="stop", message=message))
    stream.end(message)
    return stream


def _unused(*_args):
    raise RuntimeError("unused")


def authOptions(runtime: ModelRuntime, type=None):
    options = []
    for provider in runtime.getProviders():
        if type in (None, "oauth") and provider.auth.oauth is not None:
            options.append(SimpleNamespace(type="oauth", provider=provider, method=provider.auth.oauth))
        if type in (None, "api_key") and provider.auth.apiKey is not None:
            options.append(SimpleNamespace(type="api_key", provider=provider, method=provider.auth.apiKey))
    return options


async def _create(**kwargs) -> ModelRuntime:
    kwargs.setdefault("modelsPath", None)
    return await ModelRuntime.create(kwargs)


# -- model-runtime-auth-options.test.ts ------------------------------------------------------


async def test_accepts_a_credential_store():
    credentials = InMemoryCredentialStore()

    async def stored(_current):
        return ApiKeyCredential(key="stored-key")

    await credentials.modify("anthropic", stored)
    runtime = await _create(credentials=credentials)

    assert (await runtime.getAuth("anthropic")).auth.apiKey == "stored-key"


async def test_scopes_provider_availability_reads_and_records_refresh_failures():
    base = InMemoryCredentialStore()
    reads: list[str] = []
    fail = False

    class Credentials:
        async def read(self, providerId, options=None):
            reads.append(providerId)
            if fail:
                raise RuntimeError(f"read failed for {providerId}")
            return await base.read(providerId)

        async def list(self, options=None):
            return await base.list()

        async def modify(self, providerId, fn, options=None):
            return await base.modify(providerId, fn)

        async def delete(self, providerId, options=None):
            await base.delete(providerId)

    runtime = await _create(credentials=Credentials())

    reads.clear()
    await runtime.getAvailable("anthropic")
    assert set(reads) == {"anthropic"}

    fail = True
    with pytest.raises(Exception, match="Credential store read failed for anthropic"):
        await runtime.getAvailable("anthropic")
    assert "Availability refresh: Credential store read failed for anthropic" in runtime.getError()

    fail = False
    await runtime.getAvailable()
    assert runtime.getError() is None


async def test_projects_provider_owned_methods_names_and_status():
    runtime = await _create(authStorage=AuthStorage.inMemory())
    options = authOptions(runtime)

    def has(type, provider_id, provider_name, method_name=None):
        return any(
            option.type == type
            and option.provider.id == provider_id
            and option.provider.name == provider_name
            and (method_name is None or option.method.name == method_name)
            for option in options
        )

    assert has("api_key", "amazon-bedrock", "Amazon Bedrock", "AWS credentials or bearer token")
    assert has("api_key", "google-vertex", "Google Vertex AI", "Google Cloud credentials")
    assert has("oauth", "anthropic", "Anthropic")
    assert has("api_key", "cloudflare-ai-gateway", "Cloudflare AI Gateway")
    assert has("api_key", "cloudflare-workers-ai", "Cloudflare Workers AI")
    assert all(option.type == "api_key" for option in authOptions(runtime, "api_key"))
    assert all(option.type == "oauth" for option in authOptions(runtime, "oauth"))
    assert not any(option.provider.id == "openai-codex" and option.type == "api_key" for option in options)


async def test_attaches_the_providers_active_auth_status_to_every_method_option():
    runtime = await _create(authStorage=AuthStorage.inMemory({
        "anthropic": {"type": "oauth", "access": "access", "refresh": "refresh", "expires": _now_ms() + 60_000},
    }))

    assert len([option for option in authOptions(runtime) if option.provider.id == "anthropic"]) == 2
    assert (await runtime.checkAuth("anthropic")).type == "oauth"


async def test_distinguishes_subscription_oauth_from_generic_oauth_sign_in():
    runtime = await _create(authStorage=AuthStorage.inMemory({
        "anthropic": {"type": "oauth", "access": "anthropic-access", "refresh": "anthropic-refresh",
                      "expires": _now_ms() + 3_600_000},
        "openrouter": {"type": "oauth", "access": "openrouter-key", "refresh": "", "expires": 2**53 - 1},
        "radius": {"type": "oauth", "access": "radius-access", "refresh": "radius-refresh",
                   "expires": _now_ms() + 3_600_000},
    }))

    assert runtime.isUsingOAuth("anthropic") and runtime.isUsingSubscription("anthropic")
    assert runtime.isUsingOAuth("openrouter") and not runtime.isUsingSubscription("openrouter")
    assert runtime.isUsingOAuth("radius") and not runtime.isUsingSubscription("radius")


async def test_constructs_an_api_key_method_for_an_extension_api_key_provider():
    runtime = await _create(authStorage=AuthStorage.inMemory())
    runtime.registerProvider("extension-api-key", {
        "name": "Extension API Key",
        "baseUrl": "https://example.test/v1",
        "apiKey": "$EXTENSION_TEST_API_KEY",
        "api": "openai-completions",
        "models": [_testModel("extension-model")],
    })

    options = [option for option in authOptions(runtime) if option.provider.id == "extension-api-key"]
    assert len(options) == 1
    assert options[0].type == "api_key"
    assert options[0].provider.name == "Extension API Key" and options[0].method.name == "API key"
    assert callable(options[0].method.login)


async def test_resolves_configured_auth_from_request_scoped_environment_overrides():
    from misaka.ai.auth.resolve import AuthResolutionOverrides

    runtime = await _create(authStorage=AuthStorage.inMemory())
    runtime.registerProvider("request-env-provider", {
        "baseUrl": "https://example.test/v1",
        "apiKey": "$REQUEST_SCOPED_API_KEY",
        "headers": {"x-request-value": "$REQUEST_SCOPED_HEADER"},
        "api": "openai-completions",
        "models": [_testModel("request-env-model")],
    })

    auth = await runtime.getAuth("request-env-provider", AuthResolutionOverrides(
        env={"REQUEST_SCOPED_API_KEY": "request-key", "REQUEST_SCOPED_HEADER": "request-header"},
    ))

    assert auth.auth == ModelAuth(apiKey="request-key", headers={"x-request-value": "request-header"})


async def test_lets_an_explicit_authorization_header_override_auth_header_case_insensitively():
    runtime = await _create(authStorage=AuthStorage.inMemory())
    captured = {}

    def streamSimple(_model, _context, options=None):
        captured["headers"] = options.headers
        raise RuntimeError("captured")

    runtime.registerProvider("auth-header-provider", {
        "baseUrl": "https://example.test/v1",
        "apiKey": "generated-key",
        "authHeader": True,
        "api": "openai-completions",
        "streamSimple": streamSimple,
        "models": [_testModel("auth-header-model")],
    })
    model = runtime.getModel("auth-header-provider", "auth-header-model")
    assert model is not None

    await runtime.completeSimple(model, Context(messages=[]), {"headers": {"authorization": "Explicit token"}})

    assert captured["headers"] == {"authorization": "Explicit token"}


async def test_transforms_fully_assembled_headers_once_without_forwarding_the_transform():
    runtime = await _create(authStorage=AuthStorage.inMemory())
    captured = {}
    transforms = 0

    def streamSimple(_model, _context, options=None):
        assert getattr(options, "transformHeaders", None) is None
        captured["headers"] = options.headers
        raise RuntimeError("captured")

    runtime.registerProvider("header-provider", {
        "baseUrl": "https://example.test/v1",
        "apiKey": "generated-key",
        "authHeader": True,
        "headers": {"x-provider": "provider"},
        "api": "openai-completions",
        "streamSimple": streamSimple,
        "models": [{**_testModel("header-model"), "headers": {"x-model": "model"}}],
    })
    model = runtime.getModel("header-provider", "header-model")
    assert model is not None

    async def transformHeaders(headers):
        nonlocal transforms
        transforms += 1
        assert headers == {
            "Authorization": "Bearer generated-key",
            "x-provider": "provider",
            "x-model": "model",
            "x-explicit": "explicit",
        }
        return {**headers, "x-transformed": "yes"}

    await runtime.completeSimple(
        model, Context(messages=[]), {"headers": {"x-explicit": "explicit"}, "transformHeaders": transformHeaders}
    )

    assert transforms == 1
    assert captured["headers"] == {
        "Authorization": "Bearer generated-key",
        "x-provider": "provider",
        "x-model": "model",
        "x-explicit": "explicit",
        "x-transformed": "yes",
    }


async def test_does_not_cancel_an_extension_oauth_refresh_with_the_request():
    from misaka.ai.auth.resolve import AuthResolutionOverrides

    runtime = await _create(authStorage=AuthStorage.inMemory({
        "extension-oauth": {"type": "oauth", "access": "expired", "refresh": "refresh", "expires": 0},
    }))
    seen = {}

    async def refreshToken(credential, signal):
        seen["signal"] = signal
        return credential.model_copy(update={"expires": _now_ms() + 60_000})

    async def login(_callbacks):
        raise RuntimeError("not used")

    runtime.registerProvider("extension-oauth", {
        "name": "Extension OAuth",
        "baseUrl": "https://example.test/v1",
        "api": "openai-completions",
        "oauth": {
            "name": "Extension subscription",
            "login": login,
            "refreshToken": refreshToken,
            "getApiKey": lambda credential: credential.access,
        },
        "models": [_testModel("extension-model")],
    })
    controller = AbortController()

    await runtime.getAuth("extension-oauth", AuthResolutionOverrides(signal=controller.signal))
    assert seen["signal"] is not None
    controller.abort()
    # A started refresh may already have rotated the refresh token and must be persisted.
    assert seen["signal"].aborted is False


async def test_does_not_fabricate_an_api_key_method_for_an_extension_oauth_only_provider():
    runtime = await _create(authStorage=AuthStorage.inMemory())

    async def login(_callbacks):
        raise RuntimeError("not used")

    async def refreshToken(credential, _signal=None):
        return credential

    runtime.registerProvider("extension-oauth", {
        "name": "Extension OAuth",
        "baseUrl": "https://example.test/v1",
        "api": "openai-completions",
        "oauth": {
            "name": "Extension subscription",
            "isSubscription": True,
            "login": login,
            "refreshToken": refreshToken,
            "getApiKey": lambda credential: credential.access,
        },
        "models": [_testModel("extension-model")],
    })

    options = [option for option in authOptions(runtime) if option.provider.id == "extension-oauth"]
    assert len(options) == 1
    assert options[0].type == "oauth" and options[0].provider.name == "Extension OAuth"
    assert options[0].method.name == "Extension subscription" and options[0].method.isSubscription is True


# -- model-runtime-credential-sync.test.ts ---------------------------------------------------


def provider(id: str, *, login=None, refreshModels=None, check=None):
    providerModel = _model(id)

    async def default_login(_interaction):
        if login is not None:
            return await login()
        return ApiKeyCredential(key=f"{id}-key")

    async def default_check(*, ctx, credential, signal):
        return AuthCheck(type="api_key", source="stored") if credential is not None else None

    async def resolve(*, ctx, credential, signal):
        return AuthResult(auth=ModelAuth(apiKey=credential.key), source="stored") if credential is not None else None

    return SimpleNamespace(
        id=id,
        name=id,
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="API key", login=default_login, check=check or default_check,
                                            resolve=resolve)),
        getModels=lambda: [providerModel],
        refreshModels=refreshModels,
        stream=_unused,
        streamSimple=_unused,
    )


async def runtimeWithProvider(registered, storage: AuthStorage | None = None) -> ModelRuntime:
    runtime = await _create(authStorage=storage or AuthStorage.inMemory(), allowModelNetwork=False)
    runtime.registerNativeProvider(registered)
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False, providers=[registered.id]))
    return runtime


async def test_publishes_locally_consistent_availability_before_login_and_logout_resolve():
    storage = AuthStorage.inMemory()
    credentials = storage
    runtime = await runtimeWithProvider(provider("dynamic"), storage)

    await runtime.login("dynamic", "api_key", _interaction())
    assert runtime.hasConfiguredAuth("dynamic")
    assert "dynamic" in [entry.id for entry in runtime.getAvailableSnapshot()]
    assert await credentials.read("dynamic") == ApiKeyCredential(key="dynamic-key")

    await runtime.logout("dynamic")
    assert not runtime.hasConfiguredAuth("dynamic")
    assert not any(entry.provider == "dynamic" for entry in runtime.getAvailableSnapshot())
    assert await credentials.read("dynamic") is None


async def test_orders_same_provider_credential_operations_through_local_synchronization():
    loginStarted = asyncio.Event()
    finishLogin = asyncio.Event()

    async def blocked():
        loginStarted.set()
        await finishLogin.wait()
        return ApiKeyCredential(key="ordered-key")

    storage = AuthStorage.inMemory()
    credentials = storage
    runtime = await runtimeWithProvider(provider("ordered", login=blocked), storage)

    login = asyncio.ensure_future(runtime.login("ordered", "api_key", _interaction()))
    await loginStarted.wait()
    logout = asyncio.ensure_future(runtime.logout("ordered"))
    await asyncio.sleep(0)
    assert await credentials.read("ordered") is None

    finishLogin.set()
    await asyncio.gather(login, logout)
    assert await credentials.read("ordered") is None
    assert not runtime.hasConfiguredAuth("ordered")


async def test_allows_different_providers_to_run_credential_flows_concurrently():
    first, second, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    def blocking(started, key):
        async def login():
            started.set()
            await finish.wait()
            return ApiKeyCredential(key=key)

        return login

    runtime = await _create(authStorage=AuthStorage.inMemory())
    runtime.registerNativeProvider(provider("one", login=blocking(first, "one")))
    runtime.registerNativeProvider(provider("two", login=blocking(second, "two")))
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False, providers=["one", "two"]))

    one = asyncio.ensure_future(runtime.login("one", "api_key", _interaction()))
    two = asyncio.ensure_future(runtime.login("two", "api_key", _interaction()))
    await asyncio.wait_for(asyncio.gather(first.wait(), second.wait()), 5)
    finish.set()
    await asyncio.gather(one, two)


async def test_does_not_wait_for_unrelated_provider_availability_during_local_synchronization():
    stallUnrelated = False

    async def check(*, ctx, credential, signal):
        if stallUnrelated:
            await asyncio.Event().wait()

    runtime = await _create(authStorage=AuthStorage.inMemory())
    runtime.registerNativeProvider(provider("target"))
    runtime.registerNativeProvider(provider("unrelated", check=check))
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False, providers=["target", "unrelated"]))
    stallUnrelated = True

    await runtime.login("target", "api_key", _interaction())
    assert runtime.hasConfiguredAuth("target")
    result = await runtime.refresh(ModelsRefreshOptions(allowNetwork=False, providers=["target"]))
    assert result.aborted is False


async def test_reports_cancellation_that_occurs_during_provider_scoped_availability():
    blockAvailability = False
    started = asyncio.Event()

    async def check(*, ctx, credential, signal):
        if blockAvailability:
            started.set()
            await asyncio.Event().wait()
        return AuthCheck(type="api_key", source="stored") if credential is not None else None

    registered = provider("cancelled-availability", check=check)
    runtime = await runtimeWithProvider(registered)
    await runtime.setRuntimeApiKey(registered.id, "key")
    blockAvailability = True
    controller = AbortController()
    refresh = asyncio.ensure_future(
        runtime.refresh(ModelsRefreshOptions(allowNetwork=False, providers=[registered.id], signal=controller.signal))
    )
    await started.wait()
    controller.abort()

    assert (await refresh).aborted is True


async def test_does_not_run_network_refresh_inside_the_credential_operation_chain():
    networkRefreshes = 0

    async def refreshModels(context):
        nonlocal networkRefreshes
        if context.allowNetwork:
            networkRefreshes += 1
            await asyncio.Event().wait()

    runtime = await runtimeWithProvider(provider("local-only", refreshModels=refreshModels))

    await runtime.login("local-only", "api_key", _interaction())
    assert networkRefreshes == 0
    assert runtime.hasConfiguredAuth("local-only")


async def test_keeps_provider_scoped_refreshes_from_superseding_unrelated_providers():
    started, finish = asyncio.Event(), asyncio.Event()
    seen = {}

    async def refreshModels(context):
        if not context.allowNetwork:
            return
        seen["signal"] = context.signal
        started.set()
        await finish.wait()

    runtime = await _create(authStorage=AuthStorage.inMemory())
    runtime.registerNativeProvider(provider("one", refreshModels=refreshModels))
    runtime.registerNativeProvider(provider("two"))
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False, providers=["one", "two"]))
    await runtime.setRuntimeApiKey("one", "one-key")
    await runtime.setRuntimeApiKey("two", "two-key")

    first = asyncio.ensure_future(runtime.refresh(ModelsRefreshOptions(allowNetwork=True, providers=["one"])))
    await started.wait()
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=True, providers=["two"]))
    assert not seen["signal"].aborted

    finish.set()
    await first


async def test_waits_for_a_committed_credential_mutation_to_settle_before_reporting_cancellation():
    stored = {}
    committed, mutationFinished = asyncio.Event(), asyncio.Event()

    class Credentials:
        async def read(self, providerId, options=None):
            return stored.get(providerId)

        async def list(self, options=None):
            return [CredentialInfo(providerId=key, type=value.type) for key, value in stored.items()]

        async def modify(self, providerId, update, options=None):
            produced = await update(stored.get(providerId))
            if produced is not None:
                stored[providerId] = produced
            committed.set()
            await mutationFinished.wait()
            return stored.get(providerId)

        async def delete(self, providerId, options=None):
            stored.pop(providerId, None)

    runtime = await _create(credentials=Credentials())
    runtime.registerNativeProvider(provider("delayed-commit"))
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False, providers=["delayed-commit"]))
    controller = AbortController()
    login = asyncio.ensure_future(runtime.login("delayed-commit", "api_key", _interaction(signal=controller.signal)))

    await committed.wait()
    controller.abort()
    await asyncio.sleep(0)
    assert not login.done()

    mutationFinished.set()
    with pytest.raises(CredentialSynchronizationError) as raised:
        await login
    assert raised.value.credential == ApiKeyCredential(key="delayed-commit-key")
    assert stored["delayed-commit"] == ApiKeyCredential(key="delayed-commit-key")


async def test_reports_a_typed_error_when_cancellation_interrupts_post_commit_synchronization():
    blockCacheRefresh = False
    cacheRefreshStarted = asyncio.Event()

    async def refreshModels(context):
        if not context.allowNetwork and blockCacheRefresh:
            cacheRefreshStarted.set()
            await asyncio.Event().wait()

    storage = AuthStorage.inMemory()
    runtime = await runtimeWithProvider(provider("cancelled-sync", refreshModels=refreshModels), storage)
    blockCacheRefresh = True
    controller = AbortController()
    login = asyncio.ensure_future(runtime.login("cancelled-sync", "api_key", _interaction(signal=controller.signal)))
    await cacheRefreshStarted.wait()
    controller.abort()

    with pytest.raises(CredentialSynchronizationError) as raised:
        await login
    assert (raised.value.providerId, raised.value.operation) == ("cancelled-sync", "login")
    assert raised.value.credential == ApiKeyCredential(key="cancelled-sync-key")
    assert await storage.read("cancelled-sync") == ApiKeyCredential(key="cancelled-sync-key")


async def test_reports_committed_credentials_when_local_synchronization_fails():
    failCacheRefresh = False

    async def refreshModels(context):
        if not context.allowNetwork and failCacheRefresh:
            raise RuntimeError("cache restore failed")

    storage = AuthStorage.inMemory()
    runtime = await runtimeWithProvider(provider("broken-sync", refreshModels=refreshModels), storage)
    failCacheRefresh = True

    with pytest.raises(CredentialSynchronizationError) as raised:
        await runtime.login("broken-sync", "api_key", _interaction())
    assert (raised.value.providerId, raised.value.operation) == ("broken-sync", "login")
    assert raised.value.credential == ApiKeyCredential(key="broken-sync-key")
    assert await storage.read("broken-sync") == ApiKeyCredential(key="broken-sync-key")


# -- model-runtime-cloudflare-compat.test.ts -------------------------------------------------

CLOUDFLARE_COMPAT_URL = "https://gateway.ai.cloudflare.com/v1/test-account/test-gateway/compat/chat/completions"


async def test_materializes_the_cloudflare_endpoint_through_model_runtime_streaming(monkeypatch):
    storage = AuthStorage.inMemory()

    async def cloudflare(_current):
        return ApiKeyCredential(key="test-token", env={
            "CLOUDFLARE_ACCOUNT_ID": "test-account", "CLOUDFLARE_GATEWAY_ID": "test-gateway",
        })

    await storage.modify("cloudflare-ai-gateway", cloudflare)
    runtime = await _create(authStorage=storage)
    model = runtime.getModel("cloudflare-ai-gateway", "workers-ai/@cf/moonshotai/kimi-k2.6")
    assert model is not None

    chunk = {
        "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 0, "model": "test",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    requests = _openai_wire(monkeypatch, f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
    result = await runtime.completeSimple(model, Context(messages=[]))

    assert result.stopReason == "stop", result.errorMessage
    [request] = requests
    assert str(request.url) == CLOUDFLARE_COMPAT_URL
    assert request.headers["cf-aig-authorization"] == "Bearer test-token"


async def test_materializes_the_cloudflare_endpoint_after_extension_style_auth_resolution(monkeypatch):
    from misaka.ai.compat import complete, resetApiProviders
    from misaka.core.model_registry import ModelRegistry

    storage = AuthStorage.inMemory()

    async def cloudflare(_current):
        return ApiKeyCredential(key="test-token", env={
            "CLOUDFLARE_ACCOUNT_ID": "test-account", "CLOUDFLARE_GATEWAY_ID": "test-gateway",
        })

    await storage.modify("cloudflare-ai-gateway", cloudflare)
    modelRegistry = ModelRegistry(await _create(authStorage=storage))
    model = modelRegistry.find("cloudflare-ai-gateway", "workers-ai/@cf/moonshotai/kimi-k2.6")
    assert model is not None

    resetApiProviders()
    auth = await modelRegistry.getApiKeyAndHeaders(model)
    assert auth["ok"] is True, auth.get("error")
    headers = auth["headers"]
    assert {key: headers.get(key, "absent") for key in ("cf-aig-authorization", "Authorization", "x-api-key")} == {
        "cf-aig-authorization": "Bearer test-token",
        "Authorization": None,
        "x-api-key": None,
    }

    chunk = {
        "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 0, "model": "test",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    requests = _openai_wire(monkeypatch, f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")
    options ={key: value for key, value in auth.items() if key != "ok"}
    result = await complete(model, Context(messages=[]), options)

    assert result.stopReason == "stop", result.errorMessage
    [request] = requests
    assert str(request.url) == CLOUDFLARE_COMPAT_URL
    assert request.headers["cf-aig-authorization"] == "Bearer test-token"
    assert "authorization" not in request.headers
    assert "x-api-key" not in request.headers


# -- model-runtime-modify-models-compat.test.ts ----------------------------------------------


def _extension_model(id: str) -> dict:
    return {**_model("extension-oauth", id).model_dump(exclude_none=True)}


async def test_registers_native_providers_with_their_auth_implementation():
    runtime = await _create(
        authStorage=AuthStorage.inMemory(), modelsStore=InMemoryModelsStore(), allowModelNetwork=False
    )
    nativeModel = _model("extension-native", "native", baseUrl="https://fallback.test/v1")

    async def login(interaction):
        return ApiKeyCredential(key=await interaction.prompt(SecretPrompt(message="API key")))

    async def check(*, ctx, credential, signal):
        return AuthCheck(type="api_key", source="stored native key") if credential and credential.key else None

    async def resolve(*, ctx, credential, signal):
        if credential is None or not credential.key:
            return None
        return AuthResult(auth=ModelAuth(apiKey=credential.key, baseUrl="https://resolved.test/v1"),
                          source="stored native key")

    registered = SimpleNamespace(
        id="extension-native",
        name="Extension Native",
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Native setup", login=login, check=check, resolve=resolve)),
        getModels=lambda: [nativeModel],
        stream=_unused,
        streamSimple=_unused,
    )

    runtime.registerNativeProvider(registered)
    assert runtime.getProvider("extension-native") is registered
    assert runtime.getRegisteredNativeProvider("extension-native") is registered
    assert "extension-native" in runtime.getRegisteredProviderIds()
    assert runtime.getModel("extension-native", "native") is not None

    async def secret(_prompt):
        return "secret"

    await runtime.login("extension-native", "api_key", _interaction(prompt=secret))
    resolved = await runtime.getAuth("extension-native")
    assert (resolved.auth.apiKey, resolved.auth.baseUrl) == ("secret", "https://resolved.test/v1")

    runtime.unregisterProvider("extension-native")
    assert runtime.getProvider("extension-native") is None


async def test_marks_a_native_provider_with_a_stored_credential_as_configured_when_it_registers():
    runtime = await _create(
        authStorage=AuthStorage.inMemory({
            "extension-native": {
                "type": "oauth",
                "access": "access",
                "refresh": "refresh",
                "expires": _now_ms() + 3_600_000,
            },
        }),
        modelsStore=InMemoryModelsStore(),
        allowModelNetwork=False,
    )
    nativeModel = _model("extension-native", "native")

    async def refresh(credential, _signal):
        return credential

    async def toAuth(credential):
        return ModelAuth(apiKey=credential.access)

    provider = SimpleNamespace(
        id="extension-native",
        name="Extension Native",
        auth=ProviderAuth(oauth=OAuthAuth(name="Native OAuth", login=_unused, refresh=refresh, toAuth=toAuth)),
        getModels=lambda: [nativeModel],
        stream=_unused,
        streamSimple=_unused,
    )

    runtime.registerNativeProvider(provider)

    assert runtime.hasConfiguredAuth("extension-native") is True
    assert runtime.isUsingOAuth("extension-native") is True
    assert "extension-native/native" in [f"{m.provider}/{m.id}" for m in runtime.getAvailableSnapshot()]
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    assert runtime.hasConfiguredAuth("extension-native") is True


async def test_preserves_native_deferred_methods_through_provider_overlays(tmp_path):
    modelsPath = tmp_path / "models.json"
    modelsPath.write_text(json.dumps({
        "providers": {"extension-native-deferred": {"baseUrl": "https://overlay.test/v1"}},
    }), encoding="utf-8")
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": str(modelsPath), "allowModelNetwork": False})
    nativeModel = _model("extension-native-deferred", "native-deferred", baseUrl="https://native.test/v1")
    seen = {}

    async def resolve(*, ctx, credential, signal):
        return AuthResult(auth=ModelAuth(apiKey="key"), source="native")

    def fetchDeferred(requestModel, _handle, options=None):
        seen["fetchedBaseUrl"] = requestModel.baseUrl
        seen["fetchedOptions"] = options
        return _answer(_message(requestModel))

    async def cancelDeferred(_requestModel, handle, options=None):
        seen["cancelledId"] = handle.id
        seen["cancelledOptions"] = options

    registered = SimpleNamespace(
        id="extension-native-deferred",
        name="Extension Native Deferred",
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Native key", resolve=resolve)),
        getModels=lambda: [nativeModel],
        stream=_unused,
        streamSimple=_unused,
        fetchDeferred=fetchDeferred,
        cancelDeferred=cancelDeferred,
    )

    runtime.registerNativeProvider(registered)
    composedModel = runtime.getModel(registered.id, nativeModel.id)
    assert composedModel is not None

    def handle(id):
        return DeferredHandle(provider=registered.id, modelId=nativeModel.id, api=nativeModel.api, id=id)

    await runtime.fetchDeferred(composedModel, handle("fetch-id"), {
        "wait": 25, "headers": {"X-Fetch": "fetch"},
        "transformHeaders": lambda headers: {**headers, "X-Transformed": "fetch"},
    })
    await runtime.cancelDeferred(composedModel, handle("cancel-id"), {
        "timeoutMs": 100, "transformHeaders": lambda headers: {**headers, "X-Transformed": "cancel"},
    })

    assert seen["fetchedBaseUrl"] == "https://overlay.test/v1"
    fetched = seen["fetchedOptions"]
    assert (fetched.apiKey, fetched.wait) == ("key", 25)
    assert fetched.headers == {"X-Fetch": "fetch", "X-Transformed": "fetch"}
    assert seen["cancelledId"] == "cancel-id"
    cancelled = seen["cancelledOptions"]
    assert (cancelled.apiKey, cancelled.timeoutMs) == ("key", 100)
    assert cancelled.headers == {"X-Transformed": "cancel"}


async def test_applies_models_json_overrides_above_native_providers(tmp_path):
    modelsPath = tmp_path / "models.json"
    modelsPath.write_text(json.dumps({
        "providers": {"extension-native": {"modelOverrides": {"native": {"contextWindow": 4242}}}},
    }), encoding="utf-8")
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsStore": InMemoryModelsStore(), "modelsPath": str(modelsPath), "allowModelNetwork": False})
    nativeModel = _model("extension-native", "native", baseUrl="https://native.test/v1")

    async def resolve(*, ctx, credential, signal):
        return AuthResult(auth=ModelAuth(apiKey="key"), source="native")

    runtime.registerNativeProvider(SimpleNamespace(
        id="extension-native",
        name="Extension Native",
        auth=ProviderAuth(apiKey=ApiKeyAuth(name="Native key", resolve=resolve)),
        getModels=lambda: [nativeModel],
        stream=_unused,
        streamSimple=_unused,
    ))

    assert runtime.getModel("extension-native", "native").contextWindow == 4242


async def test_publishes_refresh_models_results_without_forcing_models_store_persistence():
    modelsStore = InMemoryModelsStore()
    runtime = await _create(authStorage=AuthStorage.inMemory(), modelsStore=modelsStore, allowModelNetwork=False)

    async def refreshModels(_context):
        return [{**_extension_model("live"), "provider": "extension-dynamic", "baseUrl": "http://localhost:8080/v1"}]

    runtime.registerProvider("extension-dynamic", {
        "baseUrl": "http://localhost:8080/v1",
        "apiKey": "local",
        "api": "openai-completions",
        "refreshModels": refreshModels,
    })

    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    assert runtime.getModel("extension-dynamic", "live") is not None
    assert await modelsStore.read("extension-dynamic") is None


async def test_publishes_refresh_models_results_given_as_model_objects():
    # MISAKA addition: refreshModels may return model objects, as registerProvider's `models` may (pi #8964 port).
    runtime = await _create(authStorage=AuthStorage.inMemory(), modelsStore=InMemoryModelsStore(), allowModelNetwork=False)
    liveModel = _model("extension-objects", "live")

    async def refreshModels(_context):
        return [liveModel]

    runtime.registerProvider("extension-objects", {
        "baseUrl": liveModel.baseUrl,
        "apiKey": "local",
        "api": liveModel.api,
        "refreshModels": refreshModels,
    })

    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    assert runtime.getModel("extension-objects", "live") is not None


async def test_applies_legacy_oauth_modify_models_after_async_credential_initialization():
    runtime = await _create(
        authStorage=AuthStorage.inMemory({
            "extension-oauth": {"type": "oauth", "access": "access", "refresh": "refresh",
                                "expires": _now_ms() + 60_000},
        }),
        modelsStore=InMemoryModelsStore(),
        allowModelNetwork=False,
    )

    async def login(_callbacks):
        raise RuntimeError("not used")

    async def refreshToken(credential, _signal=None):
        return credential

    def modifyModels(models, credential):
        return [*models, _model("extension-oauth", "credential-model")] if credential.access == "access" else models

    runtime.registerProvider("extension-oauth", {
        "baseUrl": "https://example.test/v1",
        "api": "openai-completions",
        "models": [_extension_model("base")],
        "oauth": {
            "name": "Extension OAuth",
            "login": login,
            "refreshToken": refreshToken,
            "getApiKey": lambda credential: credential.access,
            "modifyModels": modifyModels,
        },
    })

    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    assert runtime.getModel("extension-oauth", "base") is not None
    assert runtime.getModel("extension-oauth", "credential-model") is not None

    await runtime.logout("extension-oauth")
    assert runtime.getModel("extension-oauth", "credential-model") is None


# -- virtual-models.test.ts (runtime cases) --------------------------------------------------


class _Faux:
    """pi-ai's ``fauxProvider``, as far as these cases use it: fixed models and scripted answers."""

    def __init__(self, models: list[dict]) -> None:
        self.responses: list = []
        self.provider = createProvider(CreateProviderOptions(
            id="faux",
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Faux", resolve=self._resolve)),
            api=SimpleNamespace(stream=self._respond, streamSimple=self._respond),
            models=[
                Model.model_validate({
                    "name": model["id"], "api": "faux", "provider": "faux", "baseUrl": "http://faux.test",
                    "reasoning": False, "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}, **model,
                })
                for model in models
            ],
        ))

    @staticmethod
    async def _resolve(*, ctx, credential, signal):
        return AuthResult(auth=ModelAuth(apiKey="faux-key"), source="faux")

    def setResponses(self, responses) -> None:
        self.responses = list(responses)

    def _respond(self, model, context, options=None):
        respond = self.responses.pop(0)
        message = respond(context, options).model_copy(
            update={"api": model.api, "provider": model.provider, "model": model.id}
        )
        return _answer(message)


async def createRuntime(requests=None):
    requests = requests if requests is not None else []
    runtime = await _create(authStorage=AuthStorage.inMemory(), modelsStore=InMemoryModelsStore(),
                            allowModelNetwork=False)
    faux = _Faux([
        {"id": "small", "contextWindow": 1000, "maxTokens": 100, "input": ["text"]},
        {"id": "large", "contextWindow": 50_000, "maxTokens": 5000, "input": ["text", "image"], "reasoning": True},
    ])
    runtime.registerNativeProvider(faux.provider)

    def route(request):
        requests.append(request)
        model = runtime.getModel("faux", "large" if request.thinkingLevel == "high" else "small")
        return ModelRoute(model=model, thinkingLevel="high")

    definition = VirtualModelDefinition(
        provider="router", id="auto", name="Auto", thinkingLevels=["low", "high"], route=route
    )
    runtime.registerVirtualModel(definition)
    await runtime.refresh(ModelsRefreshOptions(allowNetwork=False))
    return SimpleNamespace(runtime=runtime, faux=faux, definition=definition, virtual=runtime.getModel("router", "auto"))


def _user(text: str, timestamp: int) -> UserMessage:
    return UserMessage(content=text, timestamp=timestamp)


def _with(definition: VirtualModelDefinition, **changes) -> VirtualModelDefinition:
    from dataclasses import replace

    return replace(definition, **changes)


async def test_lists_a_virtual_model_and_routes_it_with_a_clamped_thinking_level():
    requests = []
    setup = await createRuntime(requests)
    runtime, virtual = setup.runtime, setup.virtual
    assert (virtual.provider, virtual.id, virtual.contextWindow, virtual.maxTokens) == ("router", "auto", 0, 0)
    assert virtual.input == ["text", "image"]
    assert getSupportedThinkingLevels(virtual) == ["low", "high"]
    large = runtime.getModel("faux", "large")
    messages = [_user("first", 1), _message(large, "answer", thinkingLevel="medium"), _user("second", 2)]

    low = await runtime.resolveModel(virtual, messages, {"reason": "user", "thinkingLevel": "low"})
    assert low.model.id == "small"
    # The router asked for "high", but the small model does not reason.
    assert low.thinkingLevel == "off"
    assert (requests[0].previous.model, requests[0].previous.thinkingLevel) == (large, "medium")

    high = await runtime.resolveModel(virtual, messages, {"reason": "user", "thinkingLevel": "high"})
    assert (high.model, high.thinkingLevel) == (large, "high")


async def test_reports_the_failed_request_of_a_retry_separately_from_the_latest_response():
    requests = []
    setup = await createRuntime(requests)
    runtime, virtual = setup.runtime, setup.virtual
    small, large = runtime.getModel("faux", "small"), runtime.getModel("faux", "large")
    failed = _message(large, "", thinkingLevel="high", stopReason="error", errorMessage="overloaded_error")
    messages = [_user("first", 1), _message(small, "answer"), _user("second", 2)]

    await runtime.resolveModel(virtual, messages, {"reason": "retry", "thinkingLevel": "low", "failed": failed})
    # A routing failure names the virtual model, so there is no failed physical request to report.
    failedRoute = _message(virtual, "", stopReason="error")
    await runtime.resolveModel(virtual, messages, {"reason": "retry", "thinkingLevel": "low", "failed": failedRoute})

    assert requests[0].previous.model is small
    assert (requests[0].failed.model, requests[0].failed.thinkingLevel, requests[0].failed.message) == (
        large, "high", failed,
    )
    assert requests[1].failed is None


async def test_lists_several_virtual_models_under_a_provider_with_physical_models():
    setup = await createRuntime()
    runtime, definition = setup.runtime, setup.definition

    def route(_request):
        return ModelRoute(model=runtime.getModel("faux", "small"), thinkingLevel="off")

    runtime.registerVirtualModel(_with(definition, provider="faux", id="auto", route=route))
    runtime.registerVirtualModel(_with(definition, provider="faux", id="fast", name="Fast", route=route))
    runtime.registerVirtualModel(_with(definition, id="second", name="Second"))

    assert [model.id for model in runtime.getModels("faux")] == ["small", "large", "auto", "fast"]
    assert [model.id for model in runtime.getModels("router")] == ["auto", "second"]
    # Virtual models on a physical provider are available when the provider is.
    available = await runtime.getAvailable()
    assert [model.id for model in available if model.provider == "faux"] == ["small", "large", "auto", "fast"]
    fast = runtime.getModel("faux", "fast")
    routed = await runtime.resolveModel(fast, [], {"reason": "user", "thinkingLevel": "off"})
    assert (routed.model.provider, routed.model.id) == ("faux", "small")

    with pytest.raises(RuntimeError, match="conflicts with a physical model"):
        runtime.registerVirtualModel(_with(definition, provider="faux", id="large"))
    runtime.unregisterVirtualModel("faux", "fast")
    runtime.unregisterVirtualModel("router", "auto")
    assert [model.id for model in runtime.getModels("faux")] == ["small", "large", "auto"]
    assert [model.id for model in runtime.getModels("router")] == ["second"]


async def test_rejects_routes_to_virtual_or_unknown_models():
    setup = await createRuntime()
    runtime, definition, virtual = setup.runtime, setup.definition, setup.virtual
    unknown = virtual.model_copy(update={"provider": "faux", "id": "missing"})

    for model in (virtual, unknown):
        runtime.registerVirtualModel(_with(definition, route=lambda _request, model=model: ModelRoute(
            model=model, thinkingLevel="off")))
        with pytest.raises(RuntimeError, match="which is not a physical model"):
            await runtime.resolveModel(virtual, [], {"reason": "user", "thinkingLevel": "low"})


async def test_routes_direct_stream_simple_calls_within_the_routed_models_limits():
    requests = []
    setup = await createRuntime(requests)
    runtime, faux, virtual = setup.runtime, setup.faux, setup.virtual
    seen = {}

    def respond(_context, options):
        seen["maxTokens"] = options.maxTokens
        return _message(virtual, "hello")

    faux.setResponses([respond])

    # The caller sized the request without knowing the routed model.
    message = await runtime.completeSimple(
        virtual, Context(messages=[_user("hi", 1)]), {"reasoning": "high", "maxTokens": 20_000}
    )

    assert [request.reason for request in requests] == ["direct"]
    assert (message.provider, message.model, message.stopReason) == ("faux", "large", "stop")
    assert seen["maxTokens"] == 5000


async def test_does_not_forward_caller_credentials_to_a_routed_model_of_another_provider():
    setup = await createRuntime()
    runtime, faux, definition, virtual = setup.runtime, setup.faux, setup.definition, setup.virtual
    seen = []

    def respond(_context, options):
        seen.append((options.apiKey, options.headers))
        return _message(virtual, "hello")

    faux.setResponses([respond, respond])
    context = Context(messages=[_user("hi", 1)])
    options = {"apiKey": "caller-key", "headers": {"x-caller": "1"}}
    runtime.registerVirtualModel(_with(definition, provider="faux", id="auto"))

    await runtime.completeSimple(virtual, context, options)
    await runtime.completeSimple(runtime.getModel("faux", "auto"), context, options)

    # The router provider's credentials stay with it; the faux provider's own virtual model keeps them.
    assert seen[0][0] != "caller-key"
    assert "x-caller" not in (seen[0][1] or {})
    assert seen[1] == ("caller-key", {"x-caller": "1"})


async def test_fails_unrouted_stream_calls_on_virtual_models():
    setup = await createRuntime()

    message = await setup.runtime.complete(setup.virtual, Context(messages=[_user("hi", 1)]))

    assert message.stopReason == "error"
    assert "must be routed before streaming" in message.errorMessage


# -- MISAKA forks ----------------------------------------------------------------------------


def _oauth_provider(id: str, refreshed: list):
    """A native OAuth provider whose token is the stored access token."""

    async def refresh(credential, _signal):
        refreshed.append(credential.access)
        return OAuthCredential.model_validate({**credential.model_dump(), "access": f"{credential.access}-new"})

    async def toAuth(credential):
        return ModelAuth(apiKey=credential.access)

    async def login(_interaction):
        return OAuthCredential(type="oauth", access=f"{id}-access", refresh="r", expires=_now_ms() + 3_600_000)

    return SimpleNamespace(
        id=id,
        name=id,
        auth=ProviderAuth(oauth=OAuthAuth(name="OAuth", login=login, refresh=refresh, toAuth=toAuth)),
        getModels=lambda: [_model(id)],
        stream=_unused,
        streamSimple=_unused,
    )


async def test_a_refused_oauth_token_is_replaced_under_the_store_lock():
    refreshed = []
    storage = AuthStorage.inMemory({
        "account": {"type": "oauth", "access": "old", "refresh": "r", "expires": _now_ms() + 3_600_000},
    })
    runtime = await runtimeWithProvider(_oauth_provider("account", refreshed), storage)

    await runtime._prepareRequest(_model("account"), None)
    assert await runtime.recoverRejectedCredential("account") is True
    assert refreshed == ["old"]
    assert (await storage.read("account")).access == "old-new"
    assert (await runtime.getAuth(_model("account"))).auth.apiKey == "old-new"


async def test_a_token_the_refresh_hands_back_unchanged_is_recovered_from_once():
    refreshed = []
    storage = AuthStorage.inMemory({
        "account": {"type": "oauth", "access": "same", "refresh": "r", "expires": _now_ms() + 3_600_000},
    })
    registered = _oauth_provider("account", refreshed)

    async def refresh(credential, _signal):
        refreshed.append(credential.access)
        return credential

    registered.auth.oauth.refresh = refresh
    runtime = await runtimeWithProvider(registered, storage)
    await runtime._prepareRequest(_model("account"), None)

    # The account's answer to the same token: reported, and never asked again.
    assert await runtime.recoverRejectedCredential("account") is False
    assert await runtime.recoverRejectedCredential("account") is False
    assert refreshed == ["same"]


async def test_a_token_another_session_already_replaced_is_read_back_without_refreshing():
    refreshed = []
    storage = AuthStorage.inMemory({
        "account": {"type": "oauth", "access": "old", "refresh": "r", "expires": _now_ms() + 3_600_000},
    })
    runtime = await runtimeWithProvider(_oauth_provider("account", refreshed), storage)
    await runtime._prepareRequest(_model("account"), None)

    async def replaced(current):
        return current.model_copy(update={"access": "rotated"})

    await storage.modify("account", replaced)

    assert await runtime.recoverRejectedCredential("account") is True
    assert refreshed == []


async def test_a_configured_key_is_not_recovered_from():
    runtime = await _create(authStorage=AuthStorage.inMemory({"anthropic": {"type": "api_key", "key": "sk"}}))
    await runtime._prepareRequest(runtime.getModels("anthropic")[0], None)

    assert await runtime.recoverRejectedCredential("anthropic") is False
    assert is_rejected_credential_error("401 Unauthorized") and not is_rejected_credential_error("503 overloaded")


async def test_login_can_store_a_named_account_record_without_touching_the_default():
    storage = AuthStorage.inMemory()
    runtime = await runtimeWithProvider(_oauth_provider("account", []), storage)

    await runtime.login("account", "oauth", _interaction(), credentialId="account:work")

    credentials = storage
    assert (await credentials.read("account:work")).access == "account-access"
    assert await credentials.read("account") is None
    assert await runtime.checkAuth("account") is None


async def test_no_background_refresh_is_left_running_after_registration_settles():
    runtime = await _create(authStorage=AuthStorage.inMemory())
    runtime.registerNativeProvider(provider("settles"))
    await asyncio.gather(*runtime._backgroundRefreshes)
    assert not runtime._backgroundRefreshes


def test_registration_without_a_running_loop_recomposes_synchronously():
    runtime = asyncio.run(_create(authStorage=AuthStorage.inMemory()))

    runtime.registerNativeProvider(provider("sync"))

    assert runtime.getModel("sync", "dynamic") is not None
    assert not runtime._backgroundRefreshes


async def test_operation_options_reach_the_store():
    seen = []

    class Credentials(InMemoryCredentialStore):
        async def list(self, options=None):
            seen.append(options)
            return await super().list(options)

    runtime = await _create(credentials=Credentials())
    await runtime.listCredentials(AuthOperationOptions(signal=AbortController().signal))
    assert seen[-1] is not None


async def test_a_stored_record_the_models_cannot_read_is_no_credential_not_an_error():
    # pi lists and replaces such a record; one of them must not stop every provider.
    storage = AuthStorage.inMemory({
        "noted": {"type": "api_key", "key": "x", "note": "work"},
        "broken": {"type": "oauth", "access": "a"},
        "fine": {"type": "api_key", "key": "y"},
    })
    store = storage

    assert sorted((info.providerId, info.type) for info in await store.list()) == [
        ("broken", "oauth"), ("fine", "api_key"), ("noted", "api_key"),
    ]
    assert (await store.read("noted")).key == "x"
    assert await store.read("broken") is None

    async def replace(current):
        assert current is None
        return OAuthCredential(type="oauth", access="new", refresh="r", expires=_now_ms() + 3_600_000)

    assert (await store.modify("broken", replace)).access == "new"
    assert (await store.read("broken")).access == "new"


async def test_moa_presets_follow_a_login_of_their_aggregators_provider(monkeypatch):
    # MISAKA fork: the MoA presets are published only while the aggregator can run, and a
    # credential change of that provider alone must reach them.
    from misaka.core.moa import provider as moa_provider

    monkeypatch.setattr(moa_provider, "load_moa_config", lambda: {"presets": {
        "default": {"aggregator": {"provider": "anthropic", "model": "claude-sonnet-4-5"}, "references": []},
    }})
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsPath": None})
    assert [model.id for model in runtime.getModels("moa")] == []

    await runtime.setRuntimeApiKey("anthropic", "sk-test")
    assert [model.id for model in runtime.getModels("moa")] == ["default"]

    await runtime.removeRuntimeApiKey("anthropic")
    assert [model.id for model in runtime.getModels("moa")] == []
