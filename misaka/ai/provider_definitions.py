"""Built-in provider definitions, translated from pi's ``packages/ai/src/providers/``.

Each definition is data plus a little auth logic: who the provider is, where it lives,
which environment variables count as configured, and which API implementation streams its
models. Upstream keeps one file per vendor; they are gathered here because the Python
side has one catalog module and one API registry to point them at, and forty files of
five lines each would be ceremony rather than structure.

Three things about this module are worth knowing before reading it:

* **Models come from misaka's own catalog.** Upstream imports a generated
  ``*.models.ts`` per provider; here ``_catalog(id)`` reads the same data out of
  ``ai/models_generated.py``, which is already keyed by provider id. That catalog is
  seven providers behind upstream's -- ``ant-ling``, ``baseten``, ``nvidia``, the three
  ``qwen-token-plan`` variants and ``zai-coding-cn`` have no entry -- so those definitions
  are correct and their model lists are empty until the catalog is re-ported. The
  definitions stay: dropping them would hide the gap instead of naming it.
* **API implementations are looked up, not imported.** ``_api(name)`` looks the
  implementation up in ``ai/api_registry.py`` on every stream call, so a provider
  definition does not import an API module.
* **OAuth goes through the bridge.** misaka's flows (``ai/utils/oauth/``) speak the
  previous generation's callback contract, so ``ai/auth/oauth_bridge.py`` adapts them to
  ``login``/``refresh``/``toAuth``. All six OAuth providers defined here -- ``anthropic``,
  ``github-copilot``, ``kimi-coding``, ``openai-codex``, ``openrouter`` and ``xai`` -- have
  a registered flow. (Upstream gives OAuth to a seventh text provider, ``radius``, which is
  not defined in this module; ``ai/radius_provider.py`` builds its flow directly.)
  ``_oauth`` falls back to a declaration that names what is missing if a future provider
  does not.
"""

from __future__ import annotations

from typing import Any

from misaka.ai.api_registry import get_api_provider
from misaka.ai.auth.helpers import envApiKeyAuth, lazyOAuth
from misaka.ai.auth.oauth_bridge import oauth_auth_for_provider
from misaka.ai.auth.resolve import ModelsError
from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthContext,
    AuthResult,
    ModelAuth,
    OAuthCredential,
    ProviderAuth,
    ProviderEnv,
    SecretPrompt,
    SelectOption,
    SelectPrompt,
    TextPrompt,
)
from misaka.ai.models_generated import MODELS
from misaka.ai.models_runtime import (
    CreateProviderOptions,
    _BuiltProvider,
    createModels,
    createProvider,
)
from misaka.ai.providers.opencode_headers import withOpenCodeSessionHeader
from misaka.ai.radius_provider import radiusProvider
from misaka.ai.types import Model
from misaka.utils.values import signal_aborted

ANTHROPIC_AUTH_TOKEN_ENV = "ANTHROPIC_AUTH_TOKEN"
ANTHROPIC_OAUTH_TOKEN_ENV = "ANTHROPIC_OAUTH_TOKEN"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

CLOUDFLARE_API_KEY = "CLOUDFLARE_API_KEY"
CLOUDFLARE_ACCOUNT_ID = "CLOUDFLARE_ACCOUNT_ID"
CLOUDFLARE_GATEWAY_ID = "CLOUDFLARE_GATEWAY_ID"

VERTEX_ADC_PATH = "~/.config/gcloud/application_default_credentials.json"


def _catalog(providerId: str) -> list[Model]:
    """This provider's slice of the generated catalog."""
    return list(MODELS.get(providerId, {}).values())


class _RegistryApi:
    """A ProviderStreams that looks its implementation up in the registry per call.

    Upstream gets this from ``*.lazy.ts`` wrappers around dynamic imports. Here the
    registry is already the indirection: ``register_builtins`` fills it, and looking the
    entry up per call means a provider definition costs nothing until someone streams.
    """

    def __init__(self, api: str) -> None:
        self.api = api

    def _resolve(self):
        provider = get_api_provider(self.api)
        if provider is None:
            raise ModelsError("provider", f"No API implementation registered for {self.api}")
        return provider

    def stream(self, model, context, options=None):
        return self._resolve().stream(model, context, options)

    def streamSimple(self, model, context, options=None):
        return self._resolve().streamSimple(model, context, options)


def _api(name: str) -> _RegistryApi:
    return _RegistryApi(name)


def _unwiredOAuth(providerId: str, upstreamLoader: str):
    """A ``load`` that says what is missing instead of pretending to log anyone in."""

    async def load():
        raise ModelsError(
            "oauth",
            f"OAuth for {providerId} is declared but not wired: upstream loads "
            f"{upstreamLoader}, and misaka has no flow for this provider in "
            "ai/utils/oauth/",
        )

    return load


def _oauth(
    providerId: str,
    *,
    name: str,
    upstreamLoader: str,
    isSubscription: bool | None = None,
    loginLabel: str | None = None,
):
    """The real flow when misaka has one, a declaration that says so when it does not.

    ``name`` is always passed, so upstream's display wording wins over the flow's own and
    the two projects present the same login screen. Of the six flows this matters for
    exactly once today: ``openai-codex``'s flow calls itself ``ChatGPT Plus/Pro (Codex
    Subscription)`` while upstream says ``OpenAI (ChatGPT Plus/Pro)``; the other five
    already agree word for word.
    """
    bridged = oauth_auth_for_provider(
        providerId, name=name, isSubscription=isSubscription, loginLabel=loginLabel
    )
    if bridged is not None:
        return bridged
    return lazyOAuth(
        name=name,
        load=_unwiredOAuth(providerId, upstreamLoader),
        isSubscription=isSubscription,
        loginLabel=loginLabel,
    )


async def _env(ctx: AuthContext, name: str, signal: Any) -> str | None:
    _throwIfAborted(signal)
    value = await ctx.env(name)
    _throwIfAborted(signal)
    return value


def _throwIfAborted(signal: Any) -> None:
    if signal_aborted(signal):
        raise RuntimeError("Request was aborted")


def resolveCloudflareModel(model: Model, env: ProviderEnv | None) -> Model:
    """Materialise ``{CLOUDFLARE_ACCOUNT_ID}`` / ``{CLOUDFLARE_GATEWAY_ID}`` in the base URL.

    The catalog stores the endpoint with placeholders because the account and gateway are
    per-install, and they arrive with the resolved auth rather than the model.
    """
    if not env:
        return model
    baseUrl = model.baseUrl
    for name in (CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_GATEWAY_ID):
        baseUrl = baseUrl.replace("{" + name + "}", env.get(name, "{" + name + "}"))
    return model if baseUrl == model.baseUrl else model.model_copy(update={"baseUrl": baseUrl})


class _CloudflareStreams:
    """Wrap an implementation so the endpoint materialises before dispatch."""

    def __init__(self, streams: Any) -> None:
        self._streams = streams

    def stream(self, model, context, options=None):
        env = (options or {}).get("env") if isinstance(options, dict) else getattr(options, "env", None)
        return self._streams.stream(resolveCloudflareModel(model, env), context, options)

    def streamSimple(self, model, context, options=None):
        env = (options or {}).get("env") if isinstance(options, dict) else getattr(options, "env", None)
        return self._streams.streamSimple(resolveCloudflareModel(model, env), context, options)


def cloudflareStreams(streams: Any) -> _CloudflareStreams:
    return _CloudflareStreams(streams)


async def _cloudflareValue(
    name: str, ctx: AuthContext, credential: ApiKeyCredential | None, signal: Any
) -> str | None:
    """Per-field merge: the credential's value wins, ambient env fills the rest.

    A credential carrying only the API key must still pick up the account and gateway id
    from the environment, which is why this is per field rather than all-or-nothing.
    """
    if credential is not None:
        fromCredential = credential.key if name == CLOUDFLARE_API_KEY else (credential.env or {}).get(name)
        if fromCredential is not None:
            return fromCredential
    return await _env(ctx, name, signal)


async def _resolveCloudflareEnv(kind: str, ctx, credential, signal):
    apiKey = await _cloudflareValue(CLOUDFLARE_API_KEY, ctx, credential, signal)
    accountId = await _cloudflareValue(CLOUDFLARE_ACCOUNT_ID, ctx, credential, signal)
    gatewayId = (
        await _cloudflareValue(CLOUDFLARE_GATEWAY_ID, ctx, credential, signal)
        if kind == "ai-gateway"
        else None
    )
    if not apiKey or not accountId or (kind == "ai-gateway" and not gatewayId):
        return None
    env: ProviderEnv = {CLOUDFLARE_ACCOUNT_ID: accountId}
    if gatewayId:
        env[CLOUDFLARE_GATEWAY_ID] = gatewayId
    return apiKey, env, ("stored credential" if credential else CLOUDFLARE_API_KEY)


def cloudflareWorkersAIAuth() -> ApiKeyAuth:
    async def login(interaction):
        key = await interaction.prompt(SecretPrompt(message="Enter Cloudflare API key"))
        accountId = await interaction.prompt(TextPrompt(message="Enter Cloudflare account ID"))
        return ApiKeyCredential(key=key, env={CLOUDFLARE_ACCOUNT_ID: accountId})

    async def resolve(*, ctx, credential, signal):
        resolved = await _resolveCloudflareEnv("workers-ai", ctx, credential, signal)
        if resolved is None:
            return None
        apiKey, env, source = resolved
        return AuthResult(auth=ModelAuth(apiKey=apiKey), env=env, source=source)

    return ApiKeyAuth(name="Cloudflare API key", resolve=resolve, login=login)


def cloudflareAIGatewayAuth() -> ApiKeyAuth:
    async def login(interaction):
        key = await interaction.prompt(SecretPrompt(message="Enter Cloudflare API key"))
        accountId = await interaction.prompt(TextPrompt(message="Enter Cloudflare account ID"))
        gatewayId = await interaction.prompt(TextPrompt(message="Enter Cloudflare AI Gateway ID"))
        return ApiKeyCredential(
            key=key, env={CLOUDFLARE_ACCOUNT_ID: accountId, CLOUDFLARE_GATEWAY_ID: gatewayId}
        )

    async def resolve(*, ctx, credential, signal):
        resolved = await _resolveCloudflareEnv("ai-gateway", ctx, credential, signal)
        if resolved is None:
            return None
        apiKey, env, source = resolved
        # Upstream sends the key as the gateway's own `cf-aig-authorization` and pins
        # `Authorization` and `x-api-key` to None
        # (pi `packages/ai/src/providers/cloudflare-auth.ts:90-96`).
        return AuthResult(
            auth=ModelAuth(
                headers={
                    "cf-aig-authorization": f"Bearer {apiKey}",
                    "Authorization": None,
                    "x-api-key": None,
                }
            ),
            env=env,
            source=source,
        )

    return ApiKeyAuth(name="Cloudflare API key", resolve=resolve, login=login)


def anthropicApiKeyAuth() -> ApiKeyAuth:
    """Stored key, then an auth token as a bearer header, then two key variables.

    ``ANTHROPIC_AUTH_TOKEN`` is not an API key and must not be sent as one: it goes out as
    ``Authorization: Bearer``. misaka's ``env_api_keys`` does not know this variable at
    all, so an install configured that way currently reads as unconfigured.
    """

    async def login(interaction):
        _throwIfAborted(interaction.signal)
        key = await interaction.prompt(SecretPrompt(message="Enter Anthropic API key"))
        _throwIfAborted(interaction.signal)
        return ApiKeyCredential(key=key)

    async def resolve(*, ctx, credential, signal):
        _throwIfAborted(signal)
        if credential is not None and credential.key:
            return AuthResult(
                auth=ModelAuth(apiKey=credential.key), env=credential.env, source="stored credential"
            )

        authToken = await _env(ctx, ANTHROPIC_AUTH_TOKEN_ENV, signal)
        if authToken:
            return AuthResult(
                auth=ModelAuth(headers={"Authorization": f"Bearer {authToken}"}),
                source=ANTHROPIC_AUTH_TOKEN_ENV,
            )

        for envVar in (ANTHROPIC_OAUTH_TOKEN_ENV, ANTHROPIC_API_KEY_ENV):
            apiKey = await _env(ctx, envVar, signal)
            if apiKey:
                return AuthResult(auth=ModelAuth(apiKey=apiKey), source=envVar)
        return None

    return ApiKeyAuth(name="Anthropic API key", resolve=resolve, login=login)


def bedrockAuth() -> ApiKeyAuth:
    """A bearer token, or any of the AWS credential chain's ambient sources.

    The ambient branches return an empty ``ModelAuth``: no value is copied out of the
    environment, and the branch records only which source was found.
    """

    async def login(interaction):
        _throwIfAborted(interaction.signal)
        method = await interaction.prompt(
            SelectPrompt(
                message="Select Amazon Bedrock authentication method:",
                options=[
                    SelectOption(id="bearer-token", label="Bearer token"),
                    SelectOption(id="aws-profile", label="AWS profile"),
                    SelectOption(id="credential-chain", label="Existing AWS credential chain"),
                ],
            )
        )
        _throwIfAborted(interaction.signal)
        if method == "bearer-token":
            key = await interaction.prompt(SecretPrompt(message="Enter Amazon Bedrock bearer token"))
            return ApiKeyCredential(key=key)
        if method == "aws-profile":
            profile = await interaction.prompt(TextPrompt(message="Enter AWS profile name"))
            return ApiKeyCredential(env={"AWS_PROFILE": profile})
        if method != "credential-chain":
            raise ModelsError("auth", f"Unknown Amazon Bedrock auth method: {method}")
        await interaction.prompt(
            TextPrompt(message="Configure AWS credentials, then press Enter to continue")
        )
        return ApiKeyCredential()

    async def resolve(*, ctx, credential, signal):
        if credential is not None and credential.key:
            return AuthResult(
                auth=ModelAuth(apiKey=credential.key), env=credential.env, source="stored credential"
            )
        if await _env(ctx, "AWS_BEARER_TOKEN_BEDROCK", signal):
            return AuthResult(auth=ModelAuth(), source="AWS_BEARER_TOKEN_BEDROCK")
        storedProfile = (credential.env or {}).get("AWS_PROFILE") if credential else None
        if storedProfile or await _env(ctx, "AWS_PROFILE", signal):
            return AuthResult(
                auth=ModelAuth(),
                env=credential.env if credential else None,
                source="stored credential" if storedProfile else "AWS_PROFILE",
            )
        if await _env(ctx, "AWS_ACCESS_KEY_ID", signal) and await _env(
            ctx, "AWS_SECRET_ACCESS_KEY", signal
        ):
            return AuthResult(auth=ModelAuth(), source="AWS access keys")
        for name, source in (
            ("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "ECS task role"),
            ("AWS_CONTAINER_CREDENTIALS_FULL_URI", "ECS task role"),
            ("AWS_WEB_IDENTITY_TOKEN_FILE", "web identity token"),
        ):
            if await _env(ctx, name, signal):
                return AuthResult(auth=ModelAuth(), source=source)
        return None

    return ApiKeyAuth(name="AWS credentials or bearer token", resolve=resolve, login=login)


def vertexAuth() -> ApiKeyAuth:
    """An explicit API key, or Application Default Credentials plus project and location.

    ADC alone is not enough: without a project and a location there is no endpoint to
    call, so the provider stays unconfigured rather than failing later at request time.
    """

    async def login(interaction):
        _throwIfAborted(interaction.signal)
        method = await interaction.prompt(
            SelectPrompt(
                message="Select Google Vertex AI authentication method:",
                options=[
                    SelectOption(id="api-key", label="API key"),
                    SelectOption(id="adc", label="Application Default Credentials"),
                    SelectOption(id="service-account", label="Service account credentials file"),
                ],
            )
        )
        _throwIfAborted(interaction.signal)
        if method == "api-key":
            key = await interaction.prompt(SecretPrompt(message="Enter Google Cloud API key"))
            return ApiKeyCredential(key=key)
        if method not in ("adc", "service-account"):
            raise ModelsError("auth", f"Unknown Google Vertex AI auth method: {method}")
        project = await interaction.prompt(TextPrompt(message="Enter Google Cloud project ID"))
        location = await interaction.prompt(TextPrompt(message="Enter Google Cloud location"))
        env: ProviderEnv = {"GOOGLE_CLOUD_PROJECT": project, "GOOGLE_CLOUD_LOCATION": location}
        if method == "service-account":
            env["GOOGLE_APPLICATION_CREDENTIALS"] = await interaction.prompt(
                TextPrompt(message="Enter service account credentials file path")
            )
        return ApiKeyCredential(env=env)

    async def resolve(*, ctx, credential, signal):
        credentialEnv = (credential.env or {}) if credential else {}
        key = (credential.key if credential else None) or await _env(ctx, "GOOGLE_CLOUD_API_KEY", signal)
        if key:
            return AuthResult(
                auth=ModelAuth(apiKey=key),
                source="stored credential" if (credential and credential.key) else "GOOGLE_CLOUD_API_KEY",
            )

        adcPath = credentialEnv.get("GOOGLE_APPLICATION_CREDENTIALS") or await _env(
            ctx, "GOOGLE_APPLICATION_CREDENTIALS", signal
        )
        _throwIfAborted(signal)
        hasCredentials = await ctx.fileExists(adcPath or VERTEX_ADC_PATH)
        _throwIfAborted(signal)
        project = (
            credentialEnv.get("GOOGLE_CLOUD_PROJECT")
            or await _env(ctx, "GOOGLE_CLOUD_PROJECT", signal)
            or await _env(ctx, "GCLOUD_PROJECT", signal)
        )
        location = credentialEnv.get("GOOGLE_CLOUD_LOCATION") or await _env(
            ctx, "GOOGLE_CLOUD_LOCATION", signal
        )
        if hasCredentials and project and location:
            return AuthResult(
                auth=ModelAuth(),
                env=credential.env if credential else None,
                source="stored credential" if credential else "gcloud application default credentials",
            )
        return None

    return ApiKeyAuth(name="Google Cloud credentials", resolve=resolve, login=login)


def anthropicProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="anthropic",
            name="Anthropic",
            baseUrl="https://api.anthropic.com",
            auth=ProviderAuth(
                apiKey=anthropicApiKeyAuth(),
                oauth=_oauth(
                    "anthropic",
                    name="Anthropic (Claude Pro/Max)",
                    upstreamLoader="loadAnthropicOAuth",
                    isSubscription=True,
                ),
            ),
            models=_catalog("anthropic"),
            api=_api("anthropic-messages"),
        )
    )


def amazonBedrockProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="amazon-bedrock",
            name="Amazon Bedrock",
            auth=ProviderAuth(apiKey=bedrockAuth()),
            models=_catalog("amazon-bedrock"),
            api=_api("bedrock-converse-stream"),
        )
    )


def googleVertexProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="google-vertex",
            name="Google Vertex AI",
            auth=ProviderAuth(apiKey=vertexAuth()),
            models=_catalog("google-vertex"),
            api=_api("google-vertex"),
        )
    )


def cloudflareWorkersAIProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="cloudflare-workers-ai",
            name="Cloudflare Workers AI",
            auth=ProviderAuth(apiKey=cloudflareWorkersAIAuth()),
            models=_catalog("cloudflare-workers-ai"),
            api=cloudflareStreams(_api("openai-completions")),
        )
    )


def cloudflareAIGatewayProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="cloudflare-ai-gateway",
            name="Cloudflare AI Gateway",
            auth=ProviderAuth(apiKey=cloudflareAIGatewayAuth()),
            models=_catalog("cloudflare-ai-gateway"),
            api={
                "anthropic-messages": cloudflareStreams(_api("anthropic-messages")),
                "openai-completions": cloudflareStreams(_api("openai-completions")),
                "openai-responses": cloudflareStreams(_api("openai-responses")),
            },
        )
    )


def antLingProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="ant-ling",
            name="Ant Ling",
            baseUrl="https://api.ant-ling.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Ant Ling API key", ['ANT_LING_API_KEY'])),
            models=_catalog("ant-ling"),
            api=_api("openai-completions"),
        )
    )


def azureOpenAIResponsesProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="azure-openai-responses",
            name="Azure OpenAI",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Azure OpenAI API key", ['AZURE_OPENAI_API_KEY'])),
            models=_catalog("azure-openai-responses"),
            api=_api("azure-openai-responses"),
        )
    )


def basetenProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="baseten",
            name="Baseten",
            baseUrl="https://inference.baseten.co/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Baseten API key", ['BASETEN_API_KEY'])),
            models=_catalog("baseten"),
            api=_api("openai-completions"),
        )
    )


def cerebrasProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="cerebras",
            name="Cerebras",
            baseUrl="https://api.cerebras.ai/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Cerebras API key", ['CEREBRAS_API_KEY'])),
            models=_catalog("cerebras"),
            api=_api("openai-completions"),
        )
    )


def deepseekProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="deepseek",
            name="DeepSeek",
            baseUrl="https://api.deepseek.com",
            auth=ProviderAuth(apiKey=envApiKeyAuth("DeepSeek API key", ['DEEPSEEK_API_KEY'])),
            models=_catalog("deepseek"),
            api=_api("openai-completions"),
        )
    )


def fireworksProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="fireworks",
            name="Fireworks",
            baseUrl="https://api.fireworks.ai/inference",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Fireworks API key", ['FIREWORKS_API_KEY'])),
            models=_catalog("fireworks"),
            api={"anthropic-messages": _api("anthropic-messages"), "openai-completions": _api("openai-completions")},
        )
    )


def _copilotAvailableModels(models, credential):
    """Upstream's Copilot filter: keep only the model ids the seat's credential lists.

    A credential that is not OAuth, or whose list is missing or malformed, filters nothing.

    pi's Copilot flow writes ``availableModelIds`` onto the credential
    (``packages/ai/src/auth/oauth/github-copilot.ts:483``); misaka's port
    (``ai/utils/oauth/github_copilot.py``) does not.
    """
    if not isinstance(credential, OAuthCredential):
        return models
    availableModelIds = getattr(credential, "availableModelIds", None)
    if not isinstance(availableModelIds, list) or not all(
        isinstance(entry, str) for entry in availableModelIds
    ):
        return models
    available = set(availableModelIds)
    return [model for model in models if model.id in available]


def githubCopilotProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="github-copilot",
            name="GitHub Copilot",
            baseUrl="https://api.individual.githubcopilot.com",
            auth=ProviderAuth(apiKey=envApiKeyAuth("GitHub Copilot token", ['COPILOT_GITHUB_TOKEN']),
            oauth=_oauth("github-copilot", name="GitHub Copilot", upstreamLoader="loadGitHubCopilotOAuth", isSubscription=True)),
            models=_catalog("github-copilot"),
            filterModels=_copilotAvailableModels,
            api={"anthropic-messages": _api("anthropic-messages"), "openai-completions": _api("openai-completions"), "openai-responses": _api("openai-responses")},
        )
    )


def googleProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="google",
            name="Google",
            baseUrl="https://generativelanguage.googleapis.com/v1beta",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Gemini API key", ['GEMINI_API_KEY'])),
            models=_catalog("google"),
            api=_api("google-generative-ai"),
        )
    )


def groqProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="groq",
            name="Groq",
            baseUrl="https://api.groq.com/openai/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Groq API key", ['GROQ_API_KEY'])),
            models=_catalog("groq"),
            api=_api("openai-completions"),
        )
    )


def huggingfaceProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="huggingface",
            name="Hugging Face",
            baseUrl="https://router.huggingface.co/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Hugging Face token", ['HF_TOKEN'])),
            models=_catalog("huggingface"),
            api=_api("openai-completions"),
        )
    )


def kimiCodingProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="kimi-coding",
            name="Kimi For Coding",
            baseUrl="https://api.kimi.com/coding",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Kimi API key", ['KIMI_API_KEY']),
            oauth=_oauth("kimi-coding", name="Kimi Code (subscription)", upstreamLoader="loadKimiCodingOAuth", isSubscription=True, loginLabel="Sign in with Kimi Code")),
            models=_catalog("kimi-coding"),
            api=_api("anthropic-messages"),
        )
    )


def metaProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="meta",
            name="Meta",
            baseUrl="https://api.meta.ai/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Meta Model API key", ['META_API_KEY']),
            oauth=_oauth("meta", name="Meta (Muse subscription)", upstreamLoader="loadMetaOAuth", isSubscription=True, loginLabel="Sign in with Meta")),
            models=_catalog("meta"),
            api=_api("openai-responses"),
        )
    )


def minimaxCnProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="minimax-cn",
            name="MiniMax CN",
            baseUrl="https://api.minimaxi.com/anthropic",
            auth=ProviderAuth(apiKey=envApiKeyAuth("MiniMax CN API key", ['MINIMAX_CN_API_KEY'])),
            models=_catalog("minimax-cn"),
            api=_api("anthropic-messages"),
        )
    )


def minimaxProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="minimax",
            name="MiniMax",
            baseUrl="https://api.minimax.io/anthropic",
            auth=ProviderAuth(apiKey=envApiKeyAuth("MiniMax API key", ['MINIMAX_API_KEY'])),
            models=_catalog("minimax"),
            api=_api("anthropic-messages"),
        )
    )


def mistralProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="mistral",
            name="Mistral",
            baseUrl="https://api.mistral.ai",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Mistral API key", ['MISTRAL_API_KEY'])),
            models=_catalog("mistral"),
            api=_api("mistral-conversations"),
        )
    )


def moonshotaiCnProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="moonshotai-cn",
            name="Moonshot AI CN",
            baseUrl="https://api.moonshot.cn/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Moonshot AI API key", ['MOONSHOT_API_KEY'])),
            models=_catalog("moonshotai-cn"),
            api=_api("openai-completions"),
        )
    )


def moonshotaiProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="moonshotai",
            name="Moonshot AI",
            baseUrl="https://api.moonshot.ai/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Moonshot AI API key", ['MOONSHOT_API_KEY'])),
            models=_catalog("moonshotai"),
            api=_api("openai-completions"),
        )
    )


def nvidiaProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="nvidia",
            name="NVIDIA",
            baseUrl="https://integrate.api.nvidia.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("NVIDIA API key", ['NVIDIA_API_KEY'])),
            models=_catalog("nvidia"),
            api=_api("openai-completions"),
        )
    )


def openaiCodexProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="openai-codex",
            name="OpenAI Codex",
            baseUrl="https://chatgpt.com/backend-api",
            auth=ProviderAuth(
            oauth=_oauth("openai-codex", name="OpenAI (ChatGPT Plus/Pro)", upstreamLoader="loadOpenAICodexOAuth", isSubscription=True)),
            models=_catalog("openai-codex"),
            api=_api("openai-codex-responses"),
        )
    )


def openaiProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="openai",
            name="OpenAI",
            baseUrl="https://api.openai.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("OpenAI API key", ['OPENAI_API_KEY'])),
            models=_catalog("openai"),
            api=_api("openai-responses"),
        )
    )


def opencodeGoProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="opencode-go",
            name="OpenCode Go",
            auth=ProviderAuth(apiKey=envApiKeyAuth("OpenCode API key", ['OPENCODE_API_KEY'])),
            models=_catalog("opencode-go"),
            api={
                "anthropic-messages": withOpenCodeSessionHeader(_api("anthropic-messages")),
                "openai-completions": withOpenCodeSessionHeader(_api("openai-completions")),
                "openai-responses": withOpenCodeSessionHeader(_api("openai-responses")),
            },
        )
    )


def opencodeProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="opencode",
            name="OpenCode Zen",
            auth=ProviderAuth(apiKey=envApiKeyAuth("OpenCode API key", ['OPENCODE_API_KEY'])),
            models=_catalog("opencode"),
            api={
                "anthropic-messages": withOpenCodeSessionHeader(_api("anthropic-messages")),
                "google-generative-ai": withOpenCodeSessionHeader(_api("google-generative-ai")),
                "openai-completions": withOpenCodeSessionHeader(_api("openai-completions")),
                "openai-responses": withOpenCodeSessionHeader(_api("openai-responses")),
            },
        )
    )


def openrouterProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="openrouter",
            name="OpenRouter",
            baseUrl="https://openrouter.ai/api/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("OpenRouter API key", ['OPENROUTER_API_KEY']),
            oauth=_oauth("openrouter", name="OpenRouter OAuth", upstreamLoader="loadOpenRouterOAuth", loginLabel="Sign in with OpenRouter")),
            models=_catalog("openrouter"),
            api=_api("openai-completions"),
        )
    )


def qwenTokenPlanCnProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="qwen-token-plan-cn",
            name="Qwen Token Plan CN",
            baseUrl="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Qwen Token Plan CN API key", ['QWEN_TOKEN_PLAN_CN_API_KEY'])),
            models=_catalog("qwen-token-plan-cn"),
            api=_api("openai-completions"),
        )
    )


def qwenTokenPlanIndividualProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="qwen-token-plan-individual",
            name="Qwen Token Plan Individual",
            baseUrl="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Qwen Token Plan Individual API key", ['QWEN_TOKEN_PLAN_API_KEY'])),
            models=_catalog("qwen-token-plan-individual"),
            api=_api("openai-completions"),
        )
    )


def qwenTokenPlanProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="qwen-token-plan",
            name="Qwen Token Plan",
            baseUrl="https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Qwen Token Plan API key", ['QWEN_TOKEN_PLAN_API_KEY'])),
            models=_catalog("qwen-token-plan"),
            api=_api("openai-completions"),
        )
    )


def togetherProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="together",
            name="Together",
            baseUrl="https://api.together.ai/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Together API key", ['TOGETHER_API_KEY'])),
            models=_catalog("together"),
            api=_api("openai-completions"),
        )
    )


def vercelAIGatewayProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="vercel-ai-gateway",
            name="Vercel AI Gateway",
            baseUrl="https://ai-gateway.vercel.sh",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Vercel AI Gateway API key", ['AI_GATEWAY_API_KEY'])),
            models=_catalog("vercel-ai-gateway"),
            api=_api("anthropic-messages"),
        )
    )


def xaiProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="xai",
            name="xAI",
            baseUrl="https://api.x.ai/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("xAI API key", ['XAI_API_KEY']),
            oauth=_oauth("xai", name="xAI (Grok/X subscription)", upstreamLoader="loadXaiOAuth", isSubscription=True, loginLabel="Sign in with SuperGrok or X Premium")),
            models=_catalog("xai"),
            api=_api("openai-responses"),
        )
    )


def xiaomiTokenPlanAmsProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="xiaomi-token-plan-ams",
            name="Xiaomi Token Plan AMS",
            baseUrl="https://token-plan-ams.xiaomimimo.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Xiaomi Token Plan AMS API key", ['XIAOMI_TOKEN_PLAN_AMS_API_KEY'])),
            models=_catalog("xiaomi-token-plan-ams"),
            api=_api("openai-completions"),
        )
    )


def xiaomiTokenPlanCnProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="xiaomi-token-plan-cn",
            name="Xiaomi Token Plan CN",
            baseUrl="https://token-plan-cn.xiaomimimo.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Xiaomi Token Plan CN API key", ['XIAOMI_TOKEN_PLAN_CN_API_KEY'])),
            models=_catalog("xiaomi-token-plan-cn"),
            api=_api("openai-completions"),
        )
    )


def xiaomiTokenPlanSgpProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="xiaomi-token-plan-sgp",
            name="Xiaomi Token Plan SGP",
            baseUrl="https://token-plan-sgp.xiaomimimo.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Xiaomi Token Plan SGP API key", ['XIAOMI_TOKEN_PLAN_SGP_API_KEY'])),
            models=_catalog("xiaomi-token-plan-sgp"),
            api=_api("openai-completions"),
        )
    )


def xiaomiProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="xiaomi",
            name="Xiaomi",
            baseUrl="https://api.xiaomimimo.com/v1",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Xiaomi API key", ['XIAOMI_API_KEY'])),
            models=_catalog("xiaomi"),
            api=_api("openai-completions"),
        )
    )


def zaiCodingCnProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="zai-coding-cn",
            name="Z.AI Coding CN",
            baseUrl="https://open.bigmodel.cn/api/coding/paas/v4",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Z.AI Coding CN API key", ['ZAI_CODING_CN_API_KEY'])),
            models=_catalog("zai-coding-cn"),
            api=_api("openai-completions"),
        )
    )


def zaiProvider() -> _BuiltProvider:
    return createProvider(
        CreateProviderOptions(
            id="zai",
            name="Z.AI",
            baseUrl="https://api.z.ai/api/coding/paas/v4",
            auth=ProviderAuth(apiKey=envApiKeyAuth("Z.AI API key", ['ZAI_API_KEY'])),
            models=_catalog("zai"),
            api=_api("openai-completions"),
        )
    )


def builtinProviders() -> list[_BuiltProvider]:
    """Every built-in provider, freshly constructed.

    ``radius`` has no entry in the generated catalog: its models come from the gateway's
    own config and arrive on refresh, so it starts with an empty list and fills in.
    """
    return [
        amazonBedrockProvider(),
        antLingProvider(),
        anthropicProvider(),
        azureOpenAIResponsesProvider(),
        basetenProvider(),
        cerebrasProvider(),
        cloudflareAIGatewayProvider(),
        cloudflareWorkersAIProvider(),
        deepseekProvider(),
        fireworksProvider(),
        githubCopilotProvider(),
        googleProvider(),
        googleVertexProvider(),
        groqProvider(),
        huggingfaceProvider(),
        kimiCodingProvider(),
        metaProvider(),
        minimaxProvider(),
        minimaxCnProvider(),
        mistralProvider(),
        moonshotaiProvider(),
        moonshotaiCnProvider(),
        nvidiaProvider(),
        openaiProvider(),
        openaiCodexProvider(),
        opencodeProvider(),
        opencodeGoProvider(),
        openrouterProvider(),
        qwenTokenPlanProvider(),
        qwenTokenPlanCnProvider(),
        qwenTokenPlanIndividualProvider(),
        radiusProvider(),
        togetherProvider(),
        vercelAIGatewayProvider(),
        xaiProvider(),
        xiaomiProvider(),
        xiaomiTokenPlanAmsProvider(),
        xiaomiTokenPlanCnProvider(),
        xiaomiTokenPlanSgpProvider(),
        zaiProvider(),
        zaiCodingCnProvider(),
    ]


def builtinModels(**options: Any):
    """A ``Models`` collection with every built-in provider registered."""
    models = createModels(**options)
    for provider in builtinProviders():
        models.setProvider(provider)
    return models


def getBuiltinProviders() -> list[str]:
    """Provider ids present in the generated catalog."""
    return list(MODELS.keys())


def getBuiltinModels(provider: str) -> list[Model]:
    return _catalog(provider)


def getBuiltinModel(provider: str, modelId: str) -> Model | None:
    return MODELS.get(provider, {}).get(modelId)


__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "ANTHROPIC_AUTH_TOKEN_ENV",
    "ANTHROPIC_OAUTH_TOKEN_ENV",
    "anthropicApiKeyAuth",
    "bedrockAuth",
    "builtinModels",
    "builtinProviders",
    "cloudflareAIGatewayAuth",
    "cloudflareStreams",
    "cloudflareWorkersAIAuth",
    "getBuiltinModel",
    "getBuiltinModels",
    "getBuiltinProviders",
    "resolveCloudflareModel",
    "vertexAuth",
]
