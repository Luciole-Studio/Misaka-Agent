"""Translated from pi's ``packages/ai/test/providers.test.ts`` (v1.1.0).

MISAKA fork: the helper ``testModel`` is ``_testModel``, so pytest does not collect it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from misaka.ai.api.lazy import lazyApi
from misaka.ai.auth.helpers import envApiKeyAuth
from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthResult,
    ModelAuth,
    ProviderAuth,
)
from misaka.ai.compat import getModel as getCompatModel
from misaka.ai.compat import getModels as getCompatModels
from misaka.ai.models import (
    CreateModelsOptions,
    CreateProviderOptions,
    ModelsRefreshOptions,
    createModels,
    createProvider,
    getSupportedThinkingLevels,
)
from misaka.ai.models_store import InMemoryModelsStore
from misaka.ai.providers.all import (
    builtinModels,
    builtinProviders,
    getAllBuiltinModels,
    getBuiltinClassifierModel,
    getBuiltinClassifierModels,
    getBuiltinImageModel,
    getBuiltinImageModels,
    getBuiltinModel,
    getBuiltinModels,
    getBuiltinProviders,
)
from misaka.ai.providers.amazon_bedrock import amazonBedrockProvider
from misaka.ai.providers.anthropic import anthropicProvider
from misaka.ai.providers.cloudflare_ai_gateway import cloudflareAIGatewayProvider
from misaka.ai.providers.cloudflare_workers_ai import cloudflareWorkersAIProvider
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider
from misaka.ai.providers.google_vertex import googleVertexProvider
from misaka.ai.types import (
    Context,
    DeferredHandle,
    DoneEvent,
    Model,
    SimpleStreamOptions,
    StartEvent,
    TextContent,
    UserMessage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import normalizeContext
from misaka.utils.js import AbortController
from misaka.utils.values import read_field


def fakeAuthContext(env: dict[str, str], files: list[str] | None = None) -> Any:
    files = files or []

    async def readEnv(name: str) -> str | None:
        return env.get(name)

    async def fileExists(path: str) -> bool:
        return path in files

    return SimpleNamespace(env=readEnv, fileExists=fileExists)


neverAbortedSignal = AbortController().signal

context = normalizeContext(Context(messages=[UserMessage(content="hi", timestamp=0)]))

DEFAULT_IMAGE_RESIZE = {
    "maxWidth": 2000,
    "maxHeight": 2000,
    "maxBytes": 4.5 * 1024 * 1024,
    "jpegQuality": 80,
}


def compat(model: Any, name: str) -> Any:
    """`model.compat?.[name]` (`None` for absent)."""
    return getattr(model.compat, name, None) if model is not None and model.compat is not None else None


def interaction(answers: list[str], events: list[Any] | None = None) -> Any:
    async def prompt(_prompt: Any) -> str:
        return answers.pop(0)

    return SimpleNamespace(
        signal=neverAbortedSignal,
        prompt=prompt,
        notify=(lambda event: events.append(event)) if events is not None else (lambda _event: None),
    )


# --- builtin providers ------------------------------------------------------------------------


def test_builtin_models_registers_every_builtin_provider_with_models():
    models = builtinModels()
    providers = models.getProviders()
    assert len(providers) == len(builtinProviders())
    assert "anthropic" in [p.id for p in providers]

    anthropic = models.getModel("anthropic", "claude-haiku-4-5")
    assert anthropic.api == "anthropic-messages"

    assert len(models.getModels()) > 500

    for provider in providers:
        listed = models.getAllModels(provider.id)
        assert len(listed) > 0, provider.id
        assert all(m.provider == provider.id for m in listed)
    radius = getBuiltinModel("radius", "balanced")
    assert (radius.api, radius.provider) == ("pi-messages", "radius")


def test_returns_empty_results_for_unknown_provider_ids():
    unknownProvider = "not-a-provider"
    unknownModel = "x"

    assert getBuiltinModel(unknownProvider, unknownModel) is None
    assert getBuiltinImageModel(unknownProvider, unknownModel) is None
    assert getBuiltinClassifierModel(unknownProvider, unknownModel) is None
    assert getBuiltinModels(unknownProvider) == []
    assert getBuiltinImageModels(unknownProvider) == []
    assert getBuiltinClassifierModels(unknownProvider) == []
    assert getAllBuiltinModels(unknownProvider) == []
    assert getCompatModel(unknownProvider, unknownModel) is None
    assert getCompatModels(unknownProvider) == []


def test_stores_native_constrained_sampling_capabilities_in_model_metadata():
    gpt4o = getBuiltinModel("openai", "gpt-4o")
    assert compat(gpt4o, "supportsStrictMode") is True
    assert compat(gpt4o, "supportsOpenAIGrammarTools") is None
    gpt54 = getBuiltinModel("openai", "gpt-5.4")
    assert (compat(gpt54, "supportsStrictMode"), compat(gpt54, "supportsOpenAIGrammarTools")) == (True, True)
    assert compat(getBuiltinModel("anthropic", "claude-haiku-4-5"), "supportsStrictTools") is True


def test_keeps_the_conservative_resize_profile_on_every_vision_model():
    visionModels = [
        model for provider in getBuiltinProviders() for model in getBuiltinModels(provider) if "image" in model.input
    ]
    assert len(visionModels) > 0
    for model in visionModels:
        assert model.inputLimits.images.resize.model_dump(exclude_none=True) == DEFAULT_IMAGE_RESIZE, model.id


def test_records_known_direct_provider_image_request_limits():
    haiku = getBuiltinModel("anthropic", "claude-haiku-4-5").inputLimits
    assert (haiku.maxRequestBytes, haiku.images.maxPerRequest) == (32 * 1024 * 1024, 100)
    assert getBuiltinModel("anthropic", "claude-opus-5").inputLimits.images.maxPerRequest == 600
    assert (
        getBuiltinModel("amazon-bedrock", "anthropic.claude-haiku-4-5-20251001-v1:0").inputLimits.images.maxPerMessage
        == 20
    )
    gpt4o = getBuiltinModel("openai", "gpt-4o").inputLimits
    assert (gpt4o.maxRequestBytes, gpt4o.images.maxPerRequest) == (512 * 1024 * 1024, 1500)
    flash = getBuiltinModel("google", "gemini-2.5-flash").inputLimits
    assert (flash.maxRequestBytes, flash.images.maxPerRequest) == (20 * 1024 * 1024, 3600)


def test_does_not_infer_image_limits_from_gateway_api_compatibility():
    openRouterModel = next(model for model in getBuiltinModels("openrouter") if "image" in model.input)
    assert openRouterModel.inputLimits.model_dump(exclude_none=True) == {"images": {"resize": DEFAULT_IMAGE_RESIZE}}


def test_uses_models_dev_effort_levels_for_google_thinking_models():
    # Regression test for https://github.com/earendil-works/pi/issues/9455
    for provider in ["google", "google-vertex"]:
        assert "minimal" in getSupportedThinkingLevels(getBuiltinModel(provider, "gemini-3.6-flash"))
        assert getSupportedThinkingLevels(getBuiltinModel(provider, "gemini-3.8-flash")) == ["low", "medium", "high"]
        assert getSupportedThinkingLevels(getBuiltinModel(provider, "gemini-3.1-pro-preview")) == [
            "low",
            "medium",
            "high",
        ]
    assert getSupportedThinkingLevels(getBuiltinModel("opencode", "gemini-3.8-flash")) == ["low", "medium", "high"]
    assert getSupportedThinkingLevels(getBuiltinModel("google", "gemma-4-31b-it")) == ["minimal", "high"]


def test_enables_mid_conversation_system_messages_only_for_verified_models():
    models = builtinModels()
    supported = [
        ("moonshotai", "kimi-k2.6"),
        ("moonshotai", "kimi-k2.7-code"),
        ("moonshotai", "kimi-k2.7-code-highspeed"),
        ("moonshotai", "kimi-k3"),
        ("moonshotai-cn", "kimi-k2.6"),
        ("moonshotai-cn", "kimi-k2.7-code"),
        ("moonshotai-cn", "kimi-k2.7-code-highspeed"),
        ("moonshotai-cn", "kimi-k3"),
        ("fireworks", "accounts/fireworks/models/kimi-k3"),
        ("fireworks", "accounts/fireworks/routers/kimi-k3-fast"),
        ("openai", "gpt-5.4"),
        ("openai", "gpt-5.5"),
        ("openai", "gpt-6-astra"),
        ("openai-codex", "gpt-5.5"),
        ("anthropic", "claude-opus-5"),
        ("opencode", "gpt-5.4"),
        ("opencode", "gpt-5.6-terra"),
        ("opencode-go", "gpt-5.6-luna"),
        ("opencode", "claude-opus-4-8"),
        ("opencode", "claude-opus-5"),
        ("opencode", "kimi-k3"),
        ("opencode-go", "kimi-k3"),
        ("github-copilot", "gpt-5.6-terra"),
        ("github-copilot", "claude-opus-5"),
        ("github-copilot", "claude-opus-4.8"),
        ("github-copilot", "kimi-k3"),
        ("deepseek", "deepseek-v4-pro"),
        ("openrouter", "openai/gpt-5.6-terra"),
    ]
    unsupported = [
        ("fireworks", "accounts/fireworks/models/nemotron-3-ultra-nvfp4"),
        ("openai", "gpt-4.1"),
        ("openai", "gpt-5.2"),
        ("anthropic", "claude-sonnet-4-5"),
        ("google", "gemini-2.5-pro"),
        ("opencode", "gpt-5.2"),
        ("opencode", "claude-sonnet-4-5"),
        ("github-copilot", "claude-sonnet-4.6"),
        ("deepseek", "deepseek-flash"),
        ("openrouter", "anthropic/claude-opus-5"),
        ("openrouter", "moonshotai/kimi-k3"),
        ("openrouter", "openai/gpt-5.6-terra:batch"),
    ]
    for provider, modelId in supported:
        assert compat(models.getModel(provider, modelId), "supportsMidConvoSystemMessages") is True, f"{provider}/{modelId}"
    for provider, modelId in unsupported:
        model = models.getModel(provider, modelId)
        assert model is not None, f"{provider}/{modelId}"
        assert compat(model, "supportsMidConvoSystemMessages") is None, f"{provider}/{modelId}"


def test_routes_proxied_tool_changes_through_verified_transports_only():
    models = builtinModels()
    for provider, modelId in [("opencode", "gpt-5.6-terra"), ("github-copilot", "gpt-5.6-terra")]:
        # Proxies pass `additional_tools` through to OpenAI but are not verified for tool search.
        model = models.getModel(provider, modelId)
        assert compat(model, "supportsAdditionalTools") is True, f"{provider}/{modelId}"
        assert compat(model, "supportsToolSearch") is None, f"{provider}/{modelId}"
    # Proxied Anthropic endpoints reject `tool_addition`/`tool_removal` blocks.
    for provider in ["opencode", "github-copilot"]:
        assert compat(models.getModel(provider, "claude-opus-5"), "supportsMidConvoToolChanges") is None, provider
    assert compat(models.getModel("anthropic", "claude-opus-5"), "supportsMidConvoToolChanges") is True
    # Kimi-style tool-bearing system messages survive Moonshot and OpenCode but not Copilot.
    for provider in ["moonshotai", "moonshotai-cn", "opencode", "opencode-go"]:
        assert compat(models.getModel(provider, "kimi-k3"), "supportsMidConvoToolAdditions") is True, provider
    for provider in ["moonshotai", "moonshotai-cn"]:
        for modelId in ["kimi-k2.6", "kimi-k2.7-code", "kimi-k2.7-code-highspeed"]:
            assert compat(models.getModel(provider, modelId), "supportsMidConvoToolAdditions") is None
    assert compat(models.getModel("github-copilot", "kimi-k3"), "supportsMidConvoToolAdditions") is None
    assert compat(models.getModel("openrouter", "openai/gpt-5.6-terra"), "supportsMidConvoToolAdditions") is None


def test_uses_official_kimi_k3_pricing_for_moonshot_providers():
    models = builtinModels()
    for provider in ["moonshotai", "moonshotai-cn"]:
        assert models.getModel(provider, "kimi-k3").cost.model_dump(exclude_none=True) == {
            "input": 3,
            "output": 15,
            "cacheRead": 0.3,
            "cacheWrite": 0,
        }


def test_uses_api_equivalent_implied_pricing_for_kimi_coding_subscription_models():
    models = builtinModels()
    expectedCosts = {
        "k3": {"input": 3, "output": 15, "cacheRead": 0.3, "cacheWrite": 0},
        "kimi-for-coding-highspeed": {"input": 1.9, "output": 8, "cacheRead": 0.38, "cacheWrite": 0},
    }

    for modelId, cost in expectedCosts.items():
        assert models.getModel("kimi-coding", modelId).cost.model_dump(exclude_none=True) == cost


async def test_resolves_anthropic_bearer_auth_from_env_with_auth_token_precedence():
    models = createModels(
        CreateModelsOptions(authContext=fakeAuthContext(
            {
                "ANTHROPIC_AUTH_TOKEN": "auth-token",
                "ANTHROPIC_OAUTH_TOKEN": "oauth-token",
                "ANTHROPIC_API_KEY": "api-key",
            }
        ))
    )
    models.setProvider(anthropicProvider())

    assert await models.getAuth("anthropic") == AuthResult(
        auth=ModelAuth(headers={"Authorization": "Bearer auth-token"}),
        source="ANTHROPIC_AUTH_TOKEN",
    )


async def test_preserves_anthropic_oauth_token_precedence_over_the_api_key():
    models = createModels(CreateModelsOptions(authContext=fakeAuthContext({"ANTHROPIC_API_KEY": "key", "ANTHROPIC_OAUTH_TOKEN": "oauth-token"})))
    models.setProvider(anthropicProvider())

    result = await models.getAuth("anthropic")
    assert result.auth.apiKey == "oauth-token"
    assert result.source == "ANTHROPIC_OAUTH_TOKEN"


async def test_runs_provider_owned_bedrock_bearer_token_and_aws_profile_login_flows():
    auth = amazonBedrockProvider().auth.apiKey
    assert await auth.login(interaction(["bearer-token", "bedrock-token"])) == ApiKeyCredential(
        type="api_key", key="bedrock-token"
    )

    events: list[Any] = []
    assert await auth.login(interaction(["aws-profile", "work"], events)) == ApiKeyCredential(
        type="api_key", env={"AWS_PROFILE": "work"}
    )
    assert [event.type for event in events] == ["info"]
    assert [link.label for link in events[0].links] == ["AWS credential provider chain"]
    resolved = await auth.resolve(
        ctx=fakeAuthContext({}),
        credential=ApiKeyCredential(type="api_key", env={"AWS_PROFILE": "work"}),
        signal=neverAbortedSignal,
    )
    assert (resolved.auth, resolved.env) == (ModelAuth(), {"AWS_PROFILE": "work"})


async def test_reports_bedrock_as_configured_from_ambient_aws_credentials_without_an_api_key():
    models = createModels(CreateModelsOptions(authContext=fakeAuthContext({"AWS_PROFILE": "dev"})))
    models.setProvider(amazonBedrockProvider())
    model = models.getModels("amazon-bedrock")[0]

    result = await models.getAuth(model.provider)
    assert result.auth == ModelAuth()
    assert result.source == "AWS_PROFILE"

    unconfigured = createModels(CreateModelsOptions(authContext=fakeAuthContext({})))
    unconfigured.setProvider(amazonBedrockProvider())
    assert await unconfigured.getAuth(model.provider) is None


async def test_requires_cloudflare_workers_ai_account_config_and_returns_scoped_env():
    missingAccount = createModels(CreateModelsOptions(authContext=fakeAuthContext({"CLOUDFLARE_API_KEY": "cf-key"})))
    missingAccount.setProvider(cloudflareWorkersAIProvider())
    model = missingAccount.getModels("cloudflare-workers-ai")[0]
    assert await missingAccount.getAuth(model.provider) is None

    configured = createModels(
        CreateModelsOptions(authContext=fakeAuthContext({"CLOUDFLARE_API_KEY": "cf-key", "CLOUDFLARE_ACCOUNT_ID": "account-id"}))
    )
    configured.setProvider(cloudflareWorkersAIProvider())
    result = await configured.getAuth(model.provider)
    assert result.auth == ModelAuth(apiKey="cf-key")
    assert result.env == {"CLOUDFLARE_ACCOUNT_ID": "account-id"}


async def test_requires_cloudflare_ai_gateway_account_and_gateway_config_and_returns_scoped_env_headers():
    missingGateway = createModels(
        CreateModelsOptions(authContext=fakeAuthContext({"CLOUDFLARE_API_KEY": "cf-key", "CLOUDFLARE_ACCOUNT_ID": "account-id"}))
    )
    missingGateway.setProvider(cloudflareAIGatewayProvider())
    model = missingGateway.getModels("cloudflare-ai-gateway")[0]
    assert await missingGateway.getAuth(model.provider) is None

    configured = createModels(
        CreateModelsOptions(authContext=fakeAuthContext(
            {
                "CLOUDFLARE_API_KEY": "cf-key",
                "CLOUDFLARE_ACCOUNT_ID": "account-id",
                "CLOUDFLARE_GATEWAY_ID": "gateway-id",
            }
        ))
    )
    configured.setProvider(cloudflareAIGatewayProvider())
    result = await configured.getAuth(model.provider)
    assert result.auth == ModelAuth(
        headers={
            "cf-aig-authorization": "Bearer cf-key",
            "Authorization": None,
            "x-api-key": None,
        }
    )
    assert result.env == {
        "CLOUDFLARE_ACCOUNT_ID": "account-id",
        "CLOUDFLARE_GATEWAY_ID": "gateway-id",
    }


async def test_runs_provider_owned_vertex_api_key_and_adc_login_flows():
    auth = googleVertexProvider().auth.apiKey
    assert await auth.login(interaction(["api-key", "vertex-key"])) == ApiKeyCredential(type="api_key", key="vertex-key")

    events: list[Any] = []
    assert await auth.login(interaction(["adc", "project-id", "us-central1"], events)) == ApiKeyCredential(
        type="api_key",
        env={"GOOGLE_CLOUD_PROJECT": "project-id", "GOOGLE_CLOUD_LOCATION": "us-central1"},
    )
    assert [event.type for event in events] == ["info"]
    assert [link.label for link in events[0].links] == ["Application Default Credentials"]
    resolved = await auth.resolve(
        ctx=fakeAuthContext({}, ["~/.config/gcloud/application_default_credentials.json"]),
        credential=ApiKeyCredential(
            type="api_key",
            env={"GOOGLE_CLOUD_PROJECT": "project-id", "GOOGLE_CLOUD_LOCATION": "us-central1"},
        ),
        signal=neverAbortedSignal,
    )
    assert (resolved.auth, resolved.env) == (
        ModelAuth(),
        {"GOOGLE_CLOUD_PROJECT": "project-id", "GOOGLE_CLOUD_LOCATION": "us-central1"},
    )


async def test_resolves_vertex_via_adc_file_plus_project_and_location():
    adc = "~/.config/gcloud/application_default_credentials.json"
    configured = createModels(
        CreateModelsOptions(authContext=fakeAuthContext({"GOOGLE_CLOUD_PROJECT": "proj", "GOOGLE_CLOUD_LOCATION": "us-central1"}, [adc]))
    )
    configured.setProvider(googleVertexProvider())
    model = configured.getModels("google-vertex")[0]

    result = await configured.getAuth(model.provider)
    assert result.auth == ModelAuth()
    assert "application default" in result.source

    # ADC without project/location is not configured
    partial = createModels(CreateModelsOptions(authContext=fakeAuthContext({"GOOGLE_CLOUD_PROJECT": "proj"}, [adc])))
    partial.setProvider(googleVertexProvider())
    assert await partial.getAuth(model.provider) is None

    # explicit key wins over ADC
    keyed = createModels(CreateModelsOptions(authContext=fakeAuthContext({"GOOGLE_CLOUD_API_KEY": "vertex-key"})))
    keyed.setProvider(googleVertexProvider())
    assert (await keyed.getAuth(model.provider)).auth.apiKey == "vertex-key"


# --- envApiKeyAuth ----------------------------------------------------------------------------


async def test_prefers_the_stored_credential_key_and_falls_back_through_env_vars_in_order():
    auth = envApiKeyAuth("Test key", ["FIRST_KEY", "SECOND_KEY"])

    stored = await auth.resolve(
        ctx=fakeAuthContext({"FIRST_KEY": "env"}),
        credential=ApiKeyCredential(type="api_key", key="stored"),
        signal=neverAbortedSignal,
    )
    assert stored.auth.apiKey == "stored"
    assert stored.source == "stored credential"

    second = await auth.resolve(ctx=fakeAuthContext({"SECOND_KEY": "second"}), credential=None, signal=neverAbortedSignal)
    assert second.auth.apiKey == "second"
    assert second.source == "SECOND_KEY"

    assert await auth.resolve(ctx=fakeAuthContext({}), credential=None, signal=neverAbortedSignal) is None


async def test_login_prompts_for_a_secret_and_returns_an_api_key_credential():
    auth = envApiKeyAuth("Test key", ["TEST_KEY"])

    async def prompt(request: Any) -> str:
        assert request.type == "secret"
        return "entered-key"

    credential = await auth.login(SimpleNamespace(signal=neverAbortedSignal, prompt=prompt, notify=lambda _event: None))
    assert credential == ApiKeyCredential(type="api_key", key="entered-key")


# --- createProvider ---------------------------------------------------------------------------


def recordingStreams(label: str, calls: list[str]) -> Any:
    def respond(model: Model, *_args: Any) -> AssistantMessageEventStream:
        calls.append(f"{label}:{model.id}")
        stream = AssistantMessageEventStream()
        message = fauxAssistantMessage("ok")
        stream.push(StartEvent(type="start", partial=message))
        stream.push(DoneEvent(type="done", reason="stop", message=message))
        stream.end(message)
        return stream

    return SimpleNamespace(stream=respond, streamSimple=respond)


def _testModel(api: str, id: str, provider: str = "mixed") -> Model:
    return Model.model_validate(
        {
            "id": id,
            "name": id,
            "api": api,
            "provider": provider,
            "baseUrl": "https://example.test/v1",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 10000,
            "maxTokens": 1000,
        }
    )


async def _emptyAuth(*, ctx: Any, credential: Any, signal: Any) -> AuthResult:
    return AuthResult(auth=ModelAuth())


async def test_lazily_exposes_only_declared_deferred_capabilities():
    loads: list[bool] = []
    streams = recordingStreams("deferred", [])
    streams.fetchDeferred = lambda model, *_args: streams.streamSimple(model, context)

    async def load() -> Any:
        loads.append(True)
        return streams

    api = lazyApi(load, {"fetchDeferred": True})
    model = _testModel("api-a", "model-a")
    handle = DeferredHandle(provider=model.provider, modelId=model.id, api=model.api, id="response-1")

    assert loads == []
    assert getattr(api, "cancelDeferred", None) is None
    assert (await api.fetchDeferred(model, handle).result()).stopReason == "stop"
    assert len(loads) == 1


async def test_dispatches_on_model_api_for_mixed_api_providers():
    calls: list[str] = []
    provider = createProvider(
        CreateProviderOptions(
            id="mixed",
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=_emptyAuth)),
            models=[_testModel("api-a", "model-a"), _testModel("api-b", "model-b")],
            api={"api-a": recordingStreams("a", calls), "api-b": recordingStreams("b", calls)},
        )
    )
    models = createModels()
    models.setProvider(provider)

    await models.completeSimple(_testModel("api-a", "model-a"), context)
    await models.completeSimple(_testModel("api-b", "model-b"), context)
    assert calls == ["a:model-a", "b:model-b"]


async def test_merges_provider_resolved_env_into_stream_options():
    captured: dict[str, Any] = {}
    envModel = _testModel("api-a", "model-a", provider="env-provider")

    async def resolve(*, ctx: Any, credential: Any, signal: Any) -> AuthResult:
        return AuthResult(auth=ModelAuth(apiKey="provider-key"), env={"PROVIDER_ONLY": "provider", "SHARED": "provider"})

    def stream(model: Model, context_: Any, options: Any = None) -> Any:
        captured["env"] = read_field(options, "env")
        captured["apiKey"] = read_field(options, "apiKey")
        return recordingStreams("a", []).stream(model, context_, options)

    provider = createProvider(
        CreateProviderOptions(
            id="env-provider",
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=resolve)),
            models=[envModel],
            api=SimpleNamespace(stream=stream, streamSimple=stream),
        )
    )
    models = createModels()
    models.setProvider(provider)

    await models.completeSimple(
        envModel, context, SimpleStreamOptions(apiKey="request-key", env={"REQUEST_ONLY": "request", "SHARED": "request"})
    )

    assert captured["apiKey"] == "request-key"
    assert captured["env"] == {"PROVIDER_ONLY": "provider", "REQUEST_ONLY": "request", "SHARED": "request"}


async def test_applies_resolved_request_options_to_deferred_fetch_and_cancellation():
    captured: dict[str, Any] = {}
    deferredModel = _testModel("api-a", "model-a", provider="deferred-provider")
    streams = recordingStreams("deferred", [])

    def fetchDeferred(model: Model, _handle: Any, options: Any = None) -> Any:
        captured["fetchedModel"] = model
        captured["fetchedOptions"] = options
        return streams.streamSimple(model, context)

    async def cancelDeferred(_model: Model, _handle: Any, options: Any = None) -> None:
        captured["cancelledOptions"] = options

    streams.fetchDeferred = fetchDeferred
    streams.cancelDeferred = cancelDeferred

    async def resolve(*, ctx: Any, credential: Any, signal: Any) -> AuthResult:
        return AuthResult(
            auth=ModelAuth(
                apiKey="provider-key",
                baseUrl="https://resolved.test/v1",
                headers={"Authorization": "Bearer provider", "X-Shared": "provider"},
            ),
            env={"PROVIDER_ONLY": "provider", "SHARED": "provider"},
        )

    provider = createProvider(
        CreateProviderOptions(
            id="deferred-provider",
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=resolve)),
            models=[deferredModel],
            api=streams,
        )
    )
    models = createModels()
    models.setProvider(provider)
    handle = DeferredHandle(provider=deferredModel.provider, modelId=deferredModel.id, api=deferredModel.api, id="response-1")

    await models.fetchDeferred(
        deferredModel,
        handle,
        {
            "wait": 50,
            "timeoutMs": 100,
            "apiKey": "request-key",
            "headers": {"X-Request": "request", "x-shared": "request"},
            "env": {"REQUEST_ONLY": "request", "SHARED": "request"},
            "transformHeaders": lambda headers: {**headers, "X-Transformed": "yes"},
        },
    )
    await models.cancelDeferred(
        deferredModel,
        handle,
        {"timeoutMs": 200, "transformHeaders": lambda headers: {**headers, "X-Cancel": "yes"}},
    )

    assert captured["fetchedModel"].baseUrl == "https://resolved.test/v1"
    fetched = captured["fetchedOptions"]
    assert (read_field(fetched, "wait"), read_field(fetched, "timeoutMs"), read_field(fetched, "apiKey")) == (
        50,
        100,
        "request-key",
    )
    assert read_field(fetched, "headers") == {
        "Authorization": "Bearer provider",
        "X-Request": "request",
        "x-shared": "request",
        "X-Transformed": "yes",
    }
    assert read_field(fetched, "env") == {"PROVIDER_ONLY": "provider", "REQUEST_ONLY": "request", "SHARED": "request"}
    cancelled = captured["cancelledOptions"]
    assert (read_field(cancelled, "timeoutMs"), read_field(cancelled, "apiKey")) == (200, "provider-key")
    assert read_field(cancelled, "headers") == {
        "Authorization": "Bearer provider",
        "X-Shared": "provider",
        "X-Cancel": "yes",
    }
    assert read_field(cancelled, "env") == {"PROVIDER_ONLY": "provider", "SHARED": "provider"}


async def test_produces_a_stream_error_for_a_model_whose_api_has_no_implementation():
    provider = createProvider(
        CreateProviderOptions(
            id="mixed",
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=_emptyAuth)),
            models=[_testModel("api-a", "model-a")],
            api={"api-a": recordingStreams("a", [])},
        )
    )
    result = await provider.streamSimple(_testModel("api-ghost", "model-x"), context).result()
    assert result.stopReason == "error"
    assert "no API implementation" in result.errorMessage


async def test_lets_a_newer_dynamic_refresh_bypass_and_supersede_older_network_work():
    fetches: list[int] = []
    firstStarted, firstBlocked = asyncio.Event(), asyncio.Event()

    async def fetchModels(_context: Any) -> list[Model]:
        fetches.append(len(fetches) + 1)
        current = len(fetches)
        if current == 1:
            firstStarted.set()
            await firstBlocked.wait()
        return [_testModel("api-a", f"listed-{current}")]

    provider = createProvider(
        CreateProviderOptions(
            id="dynamic",
            auth=ProviderAuth(apiKey=ApiKeyAuth(name="Test", resolve=_emptyAuth)),
            models=[],
            fetchModels=fetchModels,
            api=recordingStreams("a", []),
        )
    )

    store = InMemoryModelsStore()
    models = createModels(CreateModelsOptions(modelsStore=store))
    models.setProvider(provider)
    assert provider.getModels() == []

    first = asyncio.ensure_future(models.refresh(ModelsRefreshOptions(providers=["dynamic"])))
    await firstStarted.wait()
    second = await models.refresh(ModelsRefreshOptions(providers=["dynamic"]))
    assert second.aborted is False
    assert (await first).aborted is False
    assert len(fetches) == 2
    assert [model.id for model in provider.getModels()] == ["listed-2"]
    assert [model.id for model in (await store.read("dynamic")).models] == ["listed-2"]

    firstBlocked.set()
    await asyncio.sleep(0)
    assert [model.id for model in provider.getModels()] == ["listed-2"]
    assert [model.id for model in (await store.read("dynamic")).models] == ["listed-2"]


# --- fauxProvider -----------------------------------------------------------------------------


async def test_streams_queued_responses_through_a_models_collection():
    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)
    faux.setResponses([fauxAssistantMessage("hello from faux")])

    model = models.getModels(faux.provider.id)[0]
    result = await models.completeSimple(model, context)
    assert result.stopReason == "stop"
    assert result.content == [TextContent(type="text", text="hello from faux")]
    assert faux.state.callCount == 1


async def test_submits_polls_and_redeems_deferred_responses():
    faux = fauxProvider({"deferred": {"pendingFetches": 1, "pollAfterMs": 25}})
    models = createModels()
    models.setProvider(faux.provider)
    faux.setResponses([fauxAssistantMessage("ready")])
    model = faux.getModel()

    submission = models.streamSimple(model, context, SimpleStreamOptions(deferred={"window": "1h"}))
    eventTypes = [event.type async for event in submission]
    deferred = await submission.result()
    assert eventTypes == ["start", "done"]
    assert (deferred.stopReason, deferred.content) == ("deferred", [])
    assert deferred.deferred.model_dump(exclude_none=True) == {
        "provider": model.provider,
        "modelId": model.id,
        "api": model.api,
        "id": deferred.deferred.id,
        "pollAfterMs": 25,
    }
    assert isinstance(deferred.deferred.id, str)

    pending = await models.fetchDeferred(model, deferred.deferred)
    assert pending.stopReason == "deferred"
    assert pending.deferred == deferred.deferred

    ready = await models.fetchDeferred(model, deferred.deferred, {"wait": 0})
    assert ready.stopReason == "stop"
    assert ready.content == [TextContent(type="text", text="ready")]
    assert ready.usage.totalTokens > 0
    assert (faux.state.callCount, faux.state.deferredFetchCount) == (1, 2)


async def test_records_cancellation_and_returns_deferred_fetch_failures_in_band():
    faux = fauxProvider()
    models = createModels()
    models.setProvider(faux.provider)

    async def fail(*_args: Any) -> Any:
        raise RuntimeError("deferred failed")

    faux.setResponses([fail, fauxAssistantMessage("cancelled")])
    model = faux.getModel()

    failedSubmission = await models.completeSimple(model, context, SimpleStreamOptions(deferred=True))
    assert failedSubmission.deferred is not None
    failed = await models.fetchDeferred(model, failedSubmission.deferred)
    assert (failed.stopReason, failed.errorMessage) == ("error", "deferred failed")

    cancelledSubmission = await models.completeSimple(model, context, SimpleStreamOptions(deferred=True))
    assert cancelledSubmission.deferred is not None
    await models.cancelDeferred(model, cancelledSubmission.deferred)
    assert faux.state.cancelledDeferred == [cancelledSubmission.deferred]
    cancelled = await models.fetchDeferred(model, cancelledSubmission.deferred)
    assert cancelled.stopReason == "error"
    assert "was cancelled" in cancelled.errorMessage

