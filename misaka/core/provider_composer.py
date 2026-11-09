"""Compose built-in, models.json, and extension layers into one provider.

Translated from pi's ``coding-agent/src/core/provider-composer.ts``. Image and classifier
models (``generateImages``/``classify`` and their config types) arrive with those model
types.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from misaka.ai.api.lazy import lazyStream
from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthCheck,
    AuthContext,
    AuthResult,
    AuthUrlEvent,
    DeviceCodeEvent,
    ManualCodePrompt,
    ModelAuth,
    OAuthAuth,
    OAuthCredential,
    OAuthCredentials,
    ProgressEvent,
    ProviderAuth,
    SecretPrompt,
    SelectOption,
    SelectPrompt,
    TextPrompt,
)
from misaka.ai.compat import getApiProvider
from misaka.ai.models import ModelsPublication, RefreshModelsContext
from misaka.ai.types import (
    AnthropicMessagesCompat,
    AnyModel,
    Api,
    AssistantImages,
    AssistantMessageEventStream,
    BedrockCompat,
    ClassifierModel,
    ClassifierResult,
    ImageModel,
    MistralConversationsCompat,
    Model,
    ModelCompat,
    ModelCost,
    ModelInputLimits,
    OpenAICompletionsCompat,
    OpenAIResponsesCompat,
    ProviderClassifier,
    ProviderHeaders,
    ProviderImages,
    SimpleStreamOptions,
    TranscriptContext,
)
from misaka.ai.utils.model_operations import (
    classifierErrorResult,
    getModelType,
    imageErrorResult,
    isModelType,
)
from misaka.core.model_config import (
    ModelConfig,
    ModelsJsonModel,
    ModelsJsonModelOverride,
    ModelsJsonProvider,
)
from misaka.core.resolve_config_value import (
    clearConfigValueCache,
    getConfigValueEnvVarNames,
    isCommandConfigValue,
    isConfigValueConfigured,
    resolveConfigValueOrThrow,
    resolveHeadersOrThrow,
)
from misaka.utils.values import (
    call_with_optional_second_arg,
    read_field,
    signal_aborted,
)


class ExtensionOAuthConfig(TypedDict):
    """MISAKA fork: an object exposing the same members is accepted as well, as misaka's
    own flows are; ``getBaseUrl``/``getAuthHeaders`` are misaka's flow extensions."""

    name: str
    # Whether access through this auth method is backed by a provider subscription.
    isSubscription: NotRequired[bool]
    # Deprecated: retained for extension source compatibility; ignored by canonical auth flows.
    usesCallbackServer: NotRequired[bool]
    login: Callable[[Any], Awaitable[OAuthCredentials]]
    refreshToken: Callable[..., Awaitable[OAuthCredentials]]
    getApiKey: Callable[[OAuthCredentials], str]
    getBaseUrl: NotRequired[Callable[[OAuthCredentials], str | None]]
    getAuthHeaders: NotRequired[Callable[[OAuthCredentials], dict[str, str] | None]]
    modifyModels: NotRequired[Callable[[list[Model], OAuthCredentials], list[Model]]]


class ProviderModelConfigBase(TypedDict):
    id: str
    name: str
    api: NotRequired[str]
    baseUrl: NotRequired[str]
    input: list[Literal["text", "image"]]
    inputLimits: NotRequired[Any]
    cost: Any
    headers: NotRequired[dict[str, str]]


class ProviderChatModelConfig(ProviderModelConfigBase):
    type: NotRequired[Literal["chat"]]
    reasoning: bool
    thinkingLevelMap: NotRequired[dict[str, str | None]]
    promptCache: NotRequired[Any]
    contextWindow: int | float
    maxTokens: int | float
    samplingParams: NotRequired[dict[str, Any]]
    samplingParamsByThinkingLevel: NotRequired[dict[str, dict[str, Any]]]
    compat: NotRequired[Any]


class ProviderImageModelConfig(ProviderModelConfigBase):
    type: Literal["image"]
    output: list[Literal["text", "image"]]


class ProviderClassifierModelConfig(ProviderModelConfigBase):
    type: Literal["classifier"]
    contextWindow: int | float


ProviderModelConfig = ProviderChatModelConfig | ProviderImageModelConfig | ProviderClassifierModelConfig


class ProviderConfigInput(TypedDict, total=False):
    """Input type for the extension registerProvider API."""

    name: str
    baseUrl: str
    apiKey: str
    api: Api
    streamSimple: Callable[[Model, TranscriptContext, SimpleStreamOptions | None], AssistantMessageEventStream]
    images: Mapping[str, ProviderImages]
    classifiers: Mapping[str, ProviderClassifier]
    headers: dict[str, str]
    authHeader: bool
    oauth: ExtensionOAuthConfig | Any
    models: list[ProviderModelConfig]
    refreshModels: Callable[[RefreshModelsContext], Awaitable[list[ProviderModelConfig]]]


type AuthStatusSource = Literal[
    "stored",
    "runtime",
    "environment",
    "fallback",
    "models_json_key",
    "models_json_command",
]


@dataclass(slots=True)
class AuthStatus:
    configured: bool
    source: AuthStatusSource | None = None
    label: str | None = None


clearApiKeyCache = clearConfigValueCache


def _getAllProviderModels(provider: Any) -> list[Model]:
    if provider is None:
        return []
    getAllModels = getattr(provider, "getAllModels", None)
    return list(getAllModels() if getAllModels is not None else provider.getModels())


# MISAKA fork: compat is typed per API in Python. A configured block becomes the compat model
# upstream's `Model<TApi>["compat"]` type names for the model's API, with extra keys allowed.
# Upstream never validates the merged object -- each API reads what it knows of it -- so a
# value this API's type cannot take (another API's spelling of a shared field, a nested key
# the routing types do not declare) is kept as written instead of failing the provider.
# An API with no compat shape of its own gets the OpenAI-compatible one. The subclass lives in the
# schema's module, where the forward references of its fields resolve.
def _configuredCompat(schema: type[BaseModel]) -> type[BaseModel]:
    return type(
        f"Configured{schema.__name__}",
        (schema,),
        {"model_config": ConfigDict(extra="allow"), "__module__": schema.__module__},
    )


_COMPAT_BY_API: dict[str, type[BaseModel]] = {
    "openai-completions": _configuredCompat(OpenAICompletionsCompat),
    "openai-responses": _configuredCompat(OpenAIResponsesCompat),
    "anthropic-messages": _configuredCompat(AnthropicMessagesCompat),
    "bedrock-converse-stream": _configuredCompat(BedrockCompat),
    "mistral-conversations": _configuredCompat(MistralConversationsCompat),
}
_COMPAT_BY_API["azure-openai-responses"] = _COMPAT_BY_API["openai-responses"]
_COMPAT_BY_API["openai-codex-responses"] = _COMPAT_BY_API["openai-responses"]


def _compatDict(compat: Any) -> dict[str, Any]:
    if compat is None:
        return {}
    if isinstance(compat, BaseModel):
        return compat.model_dump(exclude_none=True, warnings=False)
    return dict(compat)


def _runtimeCompat(api: str, compat: Any) -> ModelCompat | None:
    """The runtime compat model for a configured block (a mapping); a model passes through."""
    if compat is None or isinstance(compat, BaseModel):
        return compat
    schema = _COMPAT_BY_API.get(api, _COMPAT_BY_API["openai-completions"])
    try:
        return schema.model_validate(dict(compat))  # type: ignore[return-value]
    except ValidationError:
        pass
    values: dict[str, Any] = {}
    for key, value in dict(compat).items():
        field = schema.model_fields.get(key)
        try:
            values[key] = value if field is None else TypeAdapter(field.annotation).validate_python(value)
        except ValidationError:
            values[key] = value
    return schema.model_construct(**values)  # type: ignore[return-value]


def _mergeCompat(api: str, base: Any, override: Any) -> ModelCompat | None:
    if override is None:
        return _runtimeCompat(api, base)
    baseNested = _compatDict(base)
    overrideNested = _compatDict(override)
    merged = {**baseNested, **overrideNested}
    for key in ("openRouterRouting", "vercelGatewayRouting", "chatTemplateKwargs", "chatTemplateArgs"):
        baseValue = baseNested.get(key)
        overrideValue = overrideNested.get(key)
        if isinstance(baseValue, dict) or isinstance(overrideValue, dict):
            merged[key] = {
                **(baseValue if isinstance(baseValue, dict) else {}),
                **(overrideValue if isinstance(overrideValue, dict) else {}),
            }
    return _runtimeCompat(api, merged)


def _dump(value: Any) -> dict[str, Any]:
    return value.model_dump(exclude_none=True) if isinstance(value, BaseModel) else dict(value or {})


def _mergeInputLimits(base: ModelInputLimits | None, override: Any) -> ModelInputLimits | None:
    if not override:
        return base
    baseLimits = _dump(base)
    overrideLimits = _dump(override)
    baseImages = baseLimits.get("images") or {}
    overrideImages = overrideLimits.get("images")
    return ModelInputLimits.model_validate({
        **baseLimits,
        **overrideLimits,
        "images": (
            {
                **baseImages,
                **overrideImages,
                "resize": (
                    {**(baseImages.get("resize") or {}), **overrideImages["resize"]}
                    if overrideImages.get("resize")
                    else baseImages.get("resize")
                ),
            }
            if overrideImages
            else baseLimits.get("images")
        ),
    })


def _mergeSamplingParamsByThinkingLevel(base: Any, override: Any) -> Any:
    if not override:
        return base
    overrideLevels = _dump(override)
    merged = {**(base or {})}
    for level in ("off", "minimal", "low", "medium", "high", "xhigh", "max"):
        params = overrideLevels.get(level)
        # `if (params)`: an empty object is truthy in JavaScript.
        if params is not None:
            merged[level] = {**((base or {}).get(level) or {}), **params}
    return merged


def _coalesce(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value


def _firstDefined(*values: Any) -> Any:
    """``a ?? b ?? c``: the first value that is not ``None``."""
    return next((value for value in values if value is not None), None)


def _applyModelOverride(model: Model, override: ModelsJsonModelOverride) -> Model:
    cost = model.cost
    if override.cost is not None:
        cost = ModelCost(
            input=_coalesce(override.cost.input, model.cost.input),
            output=_coalesce(override.cost.output, model.cost.output),
            cacheRead=_coalesce(override.cost.cacheRead, model.cost.cacheRead),
            cacheWrite=_coalesce(override.cost.cacheWrite, model.cost.cacheWrite),
            tiers=(
                [tier.model_dump() for tier in override.cost.tiers]
                if override.cost.tiers is not None
                else model.cost.tiers
            ),
        )
    return Model.model_validate({
        **model.model_dump(exclude_unset=True, warnings=False),
        "name": _coalesce(override.name, model.name),
        "reasoning": _coalesce(override.reasoning, model.reasoning),
        "thinkingLevelMap": (
            {**(model.thinkingLevelMap or {}), **override.thinkingLevelMap.model_dump(exclude_unset=True)}
            if override.thinkingLevelMap is not None
            else model.thinkingLevelMap
        ),
        "input": _coalesce(override.input, model.input),
        "inputLimits": _mergeInputLimits(model.inputLimits, override.inputLimits),
        "cost": cost,
        "promptCache": (
            {**_dump(model.promptCache), **_dump(override.promptCache)}
            if override.promptCache is not None
            else model.promptCache
        ),
        "contextWindow": _coalesce(override.contextWindow, model.contextWindow),
        "maxTokens": _coalesce(override.maxTokens, model.maxTokens),
        "samplingParams": (
            {**(model.samplingParams or {}), **override.samplingParams}
            if override.samplingParams is not None
            else model.samplingParams
        ),
        "samplingParamsByThinkingLevel": _mergeSamplingParamsByThinkingLevel(
            model.samplingParamsByThinkingLevel, override.samplingParamsByThinkingLevel
        ),
        "compat": _mergeCompat(model.api, model.compat, override.compat),
    })


def _modelFromJson(
    providerId: str,
    definition: ModelsJsonModel,
    providerConfig: ModelsJsonProvider,
    defaults: Model | None,
) -> Model:
    api = _firstDefined(definition.api, providerConfig.api, defaults.api if defaults is not None else None)
    if not api:
        raise RuntimeError(
            f'Provider {providerId}, model {definition.id}: no "api" specified. Set at provider or model level.'
        )
    baseUrl = _firstDefined(definition.baseUrl, providerConfig.baseUrl, defaults.baseUrl if defaults is not None else None)
    if not baseUrl:
        raise RuntimeError(f'Provider {providerId}: "baseUrl" is required when defining custom models.')
    if definition.contextWindow is not None and definition.contextWindow <= 0:
        raise RuntimeError(f"Provider {providerId}, model {definition.id}: invalid contextWindow")
    if definition.maxTokens is not None and definition.maxTokens <= 0:
        raise RuntimeError(f"Provider {providerId}, model {definition.id}: invalid maxTokens")
    return Model.model_validate({
        "id": definition.id,
        "name": _coalesce(definition.name, definition.id),
        "api": api,
        "provider": providerId,
        "baseUrl": baseUrl,
        "reasoning": _coalesce(definition.reasoning, False),
        "thinkingLevelMap": (
            definition.thinkingLevelMap.model_dump(exclude_unset=True)
            if definition.thinkingLevelMap is not None
            else None
        ),
        "input": _coalesce(definition.input, ["text"]),
        "inputLimits": _dump(definition.inputLimits) if definition.inputLimits is not None else None,
        "cost": _dump(definition.cost) if definition.cost is not None else {
            "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
        },
        "promptCache": _dump(definition.promptCache) if definition.promptCache is not None else None,
        "contextWindow": _coalesce(definition.contextWindow, 128000),
        "maxTokens": _coalesce(definition.maxTokens, 16384),
        "samplingParams": definition.samplingParams,
        "samplingParamsByThinkingLevel": (
            _dump(definition.samplingParamsByThinkingLevel)
            if definition.samplingParamsByThinkingLevel is not None
            else None
        ),
        "headers": None,
        "compat": _mergeCompat(api, providerConfig.compat, definition.compat),
    })


def _findModelDefaults(models: Sequence[Model], modelId: str, api: str | None = None) -> Model | None:
    chatModels = [model for model in models if isModelType(model, "chat")]
    return (
        next((model for model in chatModels if model.id == modelId), None)
        or (next((model for model in chatModels if model.api == api), None) if api else None)
        or next((model for model in chatModels if model.api == "openai-completions"), None)
        or (chatModels[0] if chatModels else None)
    )


def _findExtensionModelDefaults(models: Sequence[Model], definition: Mapping[str, Any]) -> Model | None:
    type = _coalesce(definition.get("type"), "chat")
    candidates = [model for model in models if isModelType(model, type)]
    api = definition.get("api")
    return (
        next((model for model in candidates if model.id == definition.get("id")), None)
        or (next((model for model in candidates if model.api == api), None) if api else None)
        or (next((model for model in candidates if model.api == "openai-completions"), None) if type == "chat" else None)
        or (candidates[0] if candidates else None)
    )


def _extensionModelFromDefinition(
    providerId: str,
    models: Sequence[Model],
    config: ProviderConfigInput,
    definition: Mapping[str, Any],
) -> AnyModel:
    type = _coalesce(definition.get("type"), "chat")
    defaults = _findExtensionModelDefaults(models, definition)
    api = _firstDefined(
        definition.get("api"),
        config.get("api") if type == "chat" else None,
        defaults.api if defaults is not None else None,
    )
    if not api:
        raise RuntimeError(
            f'Provider {providerId}, model {definition.get("id")}: no "api" specified. '
            f"Set it at model level{' or provider level' if type == 'chat' else ''}."
        )
    baseUrl = _firstDefined(
        definition.get("baseUrl"), config.get("baseUrl"), defaults.baseUrl if defaults is not None else None
    )
    if not baseUrl:
        raise RuntimeError(f'Provider {providerId}: "baseUrl" is required when defining custom models.')
    if definition.get("type") == "image":
        return ImageModel.model_validate({**definition, "api": api, "provider": providerId, "baseUrl": baseUrl, "headers": None})
    if definition.get("type") == "classifier":
        return ClassifierModel.model_validate(
            {**definition, "api": api, "provider": providerId, "baseUrl": baseUrl, "headers": None}
        )
    return Model.model_validate({
        **definition,
        "api": api,
        "provider": providerId,
        "baseUrl": baseUrl,
        "headers": None,
        "compat": _runtimeCompat(api, definition.get("compat")),
    })


def _applyModelsJson(
    providerId: str,
    baseModels: Sequence[Model],
    config: ModelsJsonProvider | None,
) -> list[Model]:
    if config is None:
        return list(baseModels)
    if config.oauth is not None and config.baseUrl is None:
        raise RuntimeError(f'Provider {providerId}: "baseUrl" is required when "oauth" is set.')
    hasOverrides = bool(config.modelOverrides)
    if (
        not config.models
        and config.baseUrl is None
        and config.headers is None
        and config.compat is None
        and not hasOverrides
        and config.apiKey is None
        and config.oauth is None
        and config.authHeader is None
    ):
        raise RuntimeError(
            f'Provider {providerId}: must specify "baseUrl", "headers", "compat", "modelOverrides", or "models".'
        )

    models: list[Model] = []
    for model in baseModels:
        baseUrl = model.baseUrl if config.oauth == "radius" else _coalesce(config.baseUrl, model.baseUrl)
        models.append(
            model.model_copy(update={"baseUrl": baseUrl, "compat": _mergeCompat(model.api, model.compat, config.compat)})
            if isModelType(model, "chat")
            else model.model_copy(update={"baseUrl": baseUrl})
        )
    for definition in config.models or []:
        existingIndex = next(
            (
                index
                for index, model in enumerate(models)
                if isModelType(model, "chat") and model.id == definition.id
            ),
            -1,
        )
        defaults = _findModelDefaults(models, definition.id, _coalesce(definition.api, config.api))
        model = _modelFromJson(providerId, definition, config, defaults)
        if existingIndex >= 0:
            models[existingIndex] = model
        else:
            models.append(model)
    return models


def _applyExtension(
    providerId: str,
    models: Sequence[Model],
    config: ProviderConfigInput | None,
) -> list[Model]:
    if config is None:
        return list(models)
    definitions = config.get("models")
    if definitions is None:
        baseUrl = config.get("baseUrl")
        return [model.model_copy(update={"baseUrl": baseUrl}) for model in models] if baseUrl else list(models)
    return [_extensionModelFromDefinition(providerId, models, config, definition) for definition in definitions]


def _adaptOAuth(config: Any) -> OAuthAuth:
    # MISAKA fork: the config may be a mapping or an object, a one-parameter `refreshToken` is called with the
    # credential alone (JavaScript ignores the extra argument), and `getBaseUrl`/`getAuthHeaders` (misaka's flow
    # extensions) shape `toAuth`.
    async def login(callbacks: Any, _options: Any = None) -> OAuthCredential:
        credential = await read_field(config, "login")(
            SimpleNamespace(
                onAuth=lambda info: callbacks.notify(
                    AuthUrlEvent(type="auth_url", url=read_field(info, "url", ""), instructions=read_field(info, "instructions"))
                ),
                onDeviceCode=lambda info: callbacks.notify(
                    DeviceCodeEvent(
                        type="device_code",
                        userCode=read_field(info, "userCode", ""),
                        verificationUri=read_field(info, "verificationUri", ""),
                        intervalSeconds=read_field(info, "intervalSeconds"),
                        expiresInSeconds=read_field(info, "expiresInSeconds"),
                    )
                ),
                onPrompt=lambda prompt: callbacks.prompt(
                    TextPrompt(
                        type="text", message=read_field(prompt, "message", ""), placeholder=read_field(prompt, "placeholder")
                    )
                ),
                onProgress=lambda message: callbacks.notify(ProgressEvent(type="progress", message=message)),
                onManualCodeInput=lambda: callbacks.prompt(
                    ManualCodePrompt(type="manual_code", message="Paste the authorization code")
                ),
                onSelect=lambda prompt: callbacks.prompt(
                    SelectPrompt(
                        type="select",
                        message=read_field(prompt, "message", ""),
                        options=[
                            SelectOption(id=read_field(option, "id", ""), label=read_field(option, "label", ""))
                            for option in read_field(prompt, "options") or []
                        ],
                    )
                ),
                signal=callbacks.signal,
            )
        )
        return _oauthCredential(credential)

    async def refresh(credential: OAuthCredential, signal: Any) -> OAuthCredential:
        return _oauthCredential(
            await call_with_optional_second_arg(read_field(config, "refreshToken"), _flowCredentials(credential), signal)
        )

    async def toAuth(credential: OAuthCredential) -> ModelAuth:
        flowCredentials = _flowCredentials(credential)
        getBaseUrl = read_field(config, "getBaseUrl")
        baseUrl = getBaseUrl(flowCredentials) if callable(getBaseUrl) else None
        getAuthHeaders = read_field(config, "getAuthHeaders")
        headers = getAuthHeaders(flowCredentials) if callable(getAuthHeaders) else None
        if headers:
            return ModelAuth(headers=headers, baseUrl=baseUrl)
        return ModelAuth(apiKey=read_field(config, "getApiKey")(flowCredentials), baseUrl=baseUrl)

    return OAuthAuth(
        name=read_field(config, "name"),
        isSubscription=read_field(config, "isSubscription"),
        login=login,
        refresh=refresh,
        toAuth=toAuth,
    )


def _oauthCredential(credential: Any) -> OAuthCredential:
    """`{ ...credential, type: "oauth" }`."""
    data = credential.model_dump() if hasattr(credential, "model_dump") else dict(credential)
    return OAuthCredential.model_validate({**data, "type": "oauth"})


def _withConfiguredAuth(auth: ModelAuth, headers: dict[str, str] | None, authHeader: bool) -> ModelAuth:
    mergedHeaders: ProviderHeaders | None = (
        {**(auth.headers or {}), **(headers or {})} if auth.headers is not None or headers is not None else None
    )
    if authHeader:
        if not auth.apiKey:
            raise RuntimeError("authHeader requires a resolved API key")
        mergedHeaders = {**(mergedHeaders or {}), "Authorization": f"Bearer {auth.apiKey}"}
    return auth.model_copy(update={"headers": mergedHeaders})


def _configuredApiKey(config: ModelsJsonProvider | None, extension: ProviderConfigInput | None) -> str | None:
    return _firstDefined(
        extension.get("apiKey") if extension is not None else None, config.apiKey if config is not None else None
    )


def _configuredHeaders(
    config: ModelsJsonProvider | None, extension: ProviderConfigInput | None
) -> dict[str, str] | None:
    configHeaders = config.headers if config is not None else None
    extensionHeaders = extension.get("headers") if extension is not None else None
    if configHeaders is None and extensionHeaders is None:
        return None
    return {**(configHeaders or {}), **(extensionHeaders or {})}


def _authHeader(config: ModelsJsonProvider | None, extension: ProviderConfigInput | None) -> bool:
    return _firstDefined(
        extension.get("authHeader") if extension is not None else None,
        config.authHeader if config is not None else None,
        False,
    )


async def _configContextEnv(
    values: Sequence[str],
    ctx: AuthContext,
    explicit: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    env = dict(explicit or {})
    for name in dict.fromkeys(name for value in values for name in getConfigValueEnvVarNames(value)):
        if env.get(name) is not None:
            continue
        value = await ctx.env(name)
        if value is not None:
            env[name] = value
    return env or None


def _composeApiKeyAuth(
    providerId: str,
    base: Any,
    config: ModelsJsonProvider | None,
    extension: ProviderConfigInput | None,
) -> ApiKeyAuth | None:
    inherited: ApiKeyAuth | None = base.auth.apiKey if base is not None else None
    rawKey = _configuredApiKey(config, extension)
    oauth = _firstDefined(
        extension.get("oauth") if extension is not None else None, base.auth.oauth if base is not None else None
    )
    # OAuth-only providers get no fabricated API-key login method.
    if inherited is None and rawKey is None and oauth is not None:
        return None
    rawHeaders = _configuredHeaders(config, extension)
    authHeader = _authHeader(config, extension)

    async def login(interaction: Any) -> ApiKeyCredential:
        return ApiKeyCredential(key=await interaction.prompt(SecretPrompt(message="Enter API key")))

    async def check(*, ctx: AuthContext, credential: ApiKeyCredential | None = None, signal: Any) -> AuthCheck | None:
        if credential is not None:
            if inherited is not None and inherited.check is not None:
                return await inherited.check(ctx=ctx, credential=credential, signal=signal)
            if credential.key:
                return AuthCheck(type="api_key", source="stored credential")
            resolved = (
                await inherited.resolve(ctx=ctx, credential=credential, signal=signal) if inherited is not None else None
            )
            return AuthCheck(type="api_key", source=resolved.source) if resolved is not None else None
        if rawKey is not None:
            if isCommandConfigValue(rawKey):
                return AuthCheck(type="api_key", source="configured API key")
            envNames = getConfigValueEnvVarNames(rawKey)
            for name in envNames:
                if await ctx.env(name) is None:
                    return None
            return AuthCheck(type="api_key", source="configured API key")
        if inherited is not None and inherited.check is not None:
            return await inherited.check(ctx=ctx, credential=credential, signal=signal)
        resolved = await inherited.resolve(ctx=ctx, credential=credential, signal=signal) if inherited is not None else None
        return AuthCheck(type="api_key", source=resolved.source) if resolved is not None else None

    async def resolve(*, ctx: AuthContext, credential: ApiKeyCredential | None = None, signal: Any) -> AuthResult | None:
        result: AuthResult | None
        if credential is not None:
            result = (
                await inherited.resolve(ctx=ctx, credential=credential, signal=signal)
                if inherited is not None
                else AuthResult(auth=ModelAuth(apiKey=credential.key), env=credential.env, source="stored credential")
                if credential.key
                else None
            )
        elif rawKey is not None:
            env = await _configContextEnv([rawKey], ctx)
            key = resolveConfigValueOrThrow(rawKey, f'API key for provider "{providerId}"', env)
            result = (
                await inherited.resolve(ctx=ctx, credential=ApiKeyCredential(key=key), signal=signal)
                if inherited is not None
                else AuthResult(auth=ModelAuth(apiKey=key), source="configured API key")
            )
        else:
            result = (
                await inherited.resolve(ctx=ctx, credential=credential, signal=signal) if inherited is not None else None
            )
        if result is None:
            return None
        explicitEnv = {**((credential.env if credential is not None else None) or {}), **(result.env or {})}
        headerEnv = await _configContextEnv(list((rawHeaders or {}).values()), ctx, explicitEnv)
        headers = resolveHeadersOrThrow(rawHeaders, f'provider "{providerId}"', headerEnv)
        return result.model_copy(update={"auth": _withConfiguredAuth(result.auth, headers, authHeader)})

    return ApiKeyAuth(
        name=inherited.name if inherited is not None else "API key",
        login=inherited.login if inherited is not None and inherited.login is not None else login,
        check=check,
        resolve=resolve,
    )


def _composeOAuthAuth(
    providerId: str,
    base: Any,
    config: ModelsJsonProvider | None,
    extension: ProviderConfigInput | None,
) -> OAuthAuth | None:
    extensionOAuth = extension.get("oauth") if extension is not None else None
    oauth: OAuthAuth | None = (
        _adaptOAuth(extensionOAuth)
        if extensionOAuth is not None
        else (base.auth.oauth if base is not None else None)
    )
    if oauth is None:
        return None
    rawHeaders = _configuredHeaders(config, extension)
    authHeader = _authHeader(config, extension)
    toAuth = oauth.toAuth

    async def composedToAuth(credential: OAuthCredential) -> ModelAuth:
        auth = await toAuth(credential)
        env = getattr(credential, "env", None)
        headers = resolveHeadersOrThrow(
            rawHeaders,
            f'provider "{providerId}"',
            dict(env) if isinstance(env, Mapping) else None,
        )
        return _withConfiguredAuth(auth, headers, authHeader)

    return replace(oauth, toAuth=composedToAuth)


def _rawModelHeaders(
    model: Model,
    config: ModelsJsonProvider | None,
    extension: ProviderConfigInput | None,
) -> dict[str, str] | None:
    # models.json definitions and overrides are chat-only. Extension definitions
    # are matched by operation and id so colliding models cannot share headers.
    chat = isModelType(model, "chat")
    chatDefinition = (
        next((entry for entry in (config.models or []) if entry.id == model.id), None)
        if chat and config is not None
        else None
    )
    extensionModel = next(
        (
            entry
            for entry in ((extension.get("models") if extension is not None else None) or [])
            if _coalesce(entry.get("type"), "chat") == getModelType(model) and entry.get("id") == model.id
        ),
        None,
    )
    override = (config.modelOverrides or {}).get(model.id) if chat and config is not None else None
    headers = {
        **((override.headers if override is not None else None) or {}),
        **((chatDefinition.headers if chatDefinition is not None else None) or {}),
        **((extensionModel.get("headers") if extensionModel is not None else None) or {}),
    }
    return headers or None


def validateExtensionProvider(
    providerId: str,
    base: Any,
    modelsConfig: ModelsJsonProvider | None,
    extension: ProviderConfigInput,
) -> None:
    if extension.get("streamSimple") and not extension.get("api"):
        raise RuntimeError(f'Provider {providerId}: "api" is required when registering streamSimple.')
    _applyExtension(providerId, _applyModelsJson(providerId, _getAllProviderModels(base), modelsConfig), extension)


def _flowCredentials(credential: OAuthCredential) -> OAuthCredentials:
    # MISAKA fork: misaka's flows take their credentials without the `type` tag.
    data = credential.model_dump()
    data.pop("type", None)
    return OAuthCredentials.model_validate(data)


def composeModelProvider(
    providerId: str,
    base: Any,
    modelConfig: ModelConfig,
    extension: ProviderConfigInput | None,
) -> Any:
    """Compose built-in, models.json, and extension layers without reading credentials."""
    config = modelConfig.getProvider(providerId)
    extensionOAuthCredential: OAuthCredential | None = None
    refreshedExtensionModels: list[ProviderModelConfig] | None = None
    extensionOAuth = extension.get("oauth") if extension is not None else None
    modifyModels = read_field(extensionOAuth, "modifyModels") if extensionOAuth is not None else None

    def currentExtension() -> ProviderConfigInput | None:
        if extension is not None and refreshedExtensionModels is not None:
            return {**extension, "models": refreshedExtensionModels}
        return extension

    # models.json modelOverrides are the topmost user-config layer: they apply once,
    # after custom-model upserts, extension model replacement, and legacy OAuth projection.
    def getAllModels() -> list[Model]:
        models = _applyExtension(
            providerId,
            _applyModelsJson(providerId, _getAllProviderModels(base), config),
            currentExtension(),
        )
        if extensionOAuthCredential is not None and modifyModels is not None:
            # The extension hook is chat-only; other model types pass through untouched.
            models = [
                *modifyModels(
                    [model for model in models if isModelType(model, "chat")],
                    _flowCredentials(extensionOAuthCredential),
                ),
                *[model for model in models if not isModelType(model, "chat")],
            ]
        overrides = (config.modelOverrides if config is not None else None) or {}
        return [
            _applyModelOverride(model, override)
            if (override := overrides.get(model.id)) is not None and isModelType(model, "chat")
            else model
            for model in models
        ]

    # Validate eagerly so registration/reload reports structural errors immediately.
    getAllModels()
    apiKey = _composeApiKeyAuth(providerId, base, config, extension)
    oauth = _composeOAuthAuth(providerId, base, config, extension)
    if apiKey is None and oauth is None:
        raise RuntimeError(f"Provider {providerId}: no authentication method configured.")

    def supportsBaseApi(model: Model) -> bool:
        return any(entry.api == model.api for entry in base.getModels()) if base is not None else False

    def streamWith(model: Model, context: TranscriptContext, options: Any, simple: bool) -> AssistantMessageEventStream:
        async def setup() -> AssistantMessageEventStream:
            streamSimple = extension.get("streamSimple") if extension is not None else None
            if streamSimple is not None and model.api == extension.get("api"):
                return streamSimple(model, context, options)
            if base is not None and supportsBaseApi(model):
                return base.streamSimple(model, context, options) if simple else base.stream(model, context, options)
            api = getApiProvider(model.api)
            if api is None:
                raise RuntimeError(f"No API provider registered for api: {model.api}")
            return api.streamSimple(model, context, options) if simple else api.stream(model, context, options)

        return lazyStream(model, setup)

    baseRefreshModels = getattr(base, "refreshModels", None) if base is not None else None
    extensionRefreshModels = extension.get("refreshModels") if extension is not None else None

    async def refreshModels(context: RefreshModelsContext) -> None:
        if baseRefreshModels is not None:
            await baseRefreshModels(context)
        refreshed: list[ProviderModelConfig] | None = None
        if extensionRefreshModels is not None:
            # MISAKA fork: a refreshed definition may be a model object, as at registration (ModelRuntime
            # .registerProvider); the composer reads mappings.
            refreshed = [
                definition.model_dump(exclude_unset=True) if hasattr(definition, "model_dump") else definition
                for definition in await extensionRefreshModels(context)
            ]
        if signal_aborted(context.signal):
            return
        oauthCredential = context.credential if getattr(context.credential, "type", None) == "oauth" else None

        def update() -> None:
            nonlocal refreshedExtensionModels, extensionOAuthCredential
            if refreshed is not None:
                # Validate before publishing the new synchronous list.
                _applyExtension(
                    providerId,
                    _applyModelsJson(providerId, _getAllProviderModels(base), config),
                    {**(extension or {}), "models": refreshed},
                )
                refreshedExtensionModels = refreshed
            extensionOAuthCredential = oauthCredential  # type: ignore[assignment]

        await context.publish(ModelsPublication(update=update))

    baseFilterModels = getattr(base, "filterModels", None) if base is not None else None
    baseFilterAllModels = getattr(base, "filterAllModels", None) if base is not None else None
    provider = SimpleNamespace(
        id=providerId,
        name=_firstDefined(
            extension.get("name") if extension is not None else None,
            config.name if config is not None else None,
            base.name if base is not None else None,
            read_field(extensionOAuth, "name") if extensionOAuth is not None else None,
            providerId,
        ),
        baseUrl=_firstDefined(
            extension.get("baseUrl") if extension is not None else None,
            config.baseUrl if config is not None else None,
            getattr(base, "baseUrl", None) if base is not None else None,
        ),
        headers=getattr(base, "headers", None) if base is not None else None,
        auth=ProviderAuth(apiKey=apiKey, oauth=oauth),
        getModels=lambda: [model for model in getAllModels() if isModelType(model, "chat")],
        getAllModels=getAllModels,
        refreshModels=(
            refreshModels
            if baseRefreshModels is not None or extensionRefreshModels is not None or modifyModels is not None
            else None
        ),
        filterModels=(
            (lambda models, credential: baseFilterModels(models, credential)) if baseFilterModels is not None else None
        ),
        filterAllModels=(
            (lambda models, credential: baseFilterAllModels(models, credential))
            if baseFilterAllModels is not None
            else None
        ),
        stream=lambda model, context, options=None: streamWith(model, context, options, False),
        streamSimple=lambda model, context, options=None: streamWith(model, context, options, True),
    )

    fetchDeferred = getattr(base, "fetchDeferred", None) if base is not None else None
    if fetchDeferred is not None:
        provider.fetchDeferred = lambda model, handle, options=None: fetchDeferred(model, handle, options)
    cancelDeferred = getattr(base, "cancelDeferred", None) if base is not None else None
    if cancelDeferred is not None:
        provider.cancelDeferred = lambda model, handle, options=None: cancelDeferred(model, handle, options)
    extensionImages = extension.get("images") if extension is not None else None
    generateImages = getattr(base, "generateImages", None) if base is not None else None
    if generateImages is not None or extensionImages:

        async def composedGenerateImages(model: ImageModel, context: Any, options: Any = None) -> AssistantImages:
            implementation = (extensionImages or {}).get(model.api)
            if implementation is not None:
                return await implementation.generateImages(model, context, options)
            if generateImages is not None:
                return await generateImages(model, context, options)
            return imageErrorResult(
                model, RuntimeError(f'Provider {providerId} has no image implementation for "{model.api}"')
            )

        provider.generateImages = composedGenerateImages
    extensionClassifiers = extension.get("classifiers") if extension is not None else None
    classify = getattr(base, "classify", None) if base is not None else None
    if classify is not None or extensionClassifiers:

        async def composedClassify(model: ClassifierModel, context: Any, options: Any = None) -> ClassifierResult:
            implementation = (extensionClassifiers or {}).get(model.api)
            if implementation is not None:
                return await implementation.classify(model, context, options)
            if classify is not None:
                return await classify(model, context, options)
            return classifierErrorResult(
                model, RuntimeError(f'Provider {providerId} has no classifier implementation for "{model.api}"')
            )

        provider.classify = composedClassify

    return provider


def resolveConfiguredModelHeaders(
    model: Model,
    config: ModelsJsonProvider | None,
    extension: ProviderConfigInput | None,
    env: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    return resolveHeadersOrThrow(
        _rawModelHeaders(model, config, extension),
        f'model "{model.provider}/{model.id}"',
        env,
    )


@dataclass(slots=True)
class CompatibilityRequestConfig:
    authHeader: bool
    headers: ProviderHeaders | None = None


def resolveCompatibilityRequestConfig(
    model: Model,
    config: ModelsJsonProvider | None,
    extension: ProviderConfigInput | None,
) -> CompatibilityRequestConfig:
    configured = resolveHeadersOrThrow(
        {**(_configuredHeaders(config, extension) or {}), **(_rawModelHeaders(model, config, extension) or {})},
        f'model "{model.provider}/{model.id}"',
    )
    return CompatibilityRequestConfig(
        headers=(
            {**(model.headers or {}), **(configured or {})}
            if model.headers is not None or configured is not None
            else None
        ),
        authHeader=_authHeader(config, extension),
    )


def configuredRequestAuthStatus(
    config: ModelsJsonProvider | None,
    extension: ProviderConfigInput | None,
) -> AuthStatus | None:
    value = _configuredApiKey(config, extension)
    if value is None:
        return None
    if isCommandConfigValue(value):
        return AuthStatus(configured=True, source="models_json_command")
    names = getConfigValueEnvVarNames(value)
    if names:
        return (
            AuthStatus(configured=True, source="environment", label=", ".join(names))
            if isConfigValueConfigured(value)
            else AuthStatus(configured=False)
        )
    return AuthStatus(
        configured=True,
        source="fallback" if extension is not None and extension.get("apiKey") is not None else "models_json_key",
    )


__all__ = [
    "AuthStatus",
    "AuthStatusSource",
    "CompatibilityRequestConfig",
    "ExtensionOAuthConfig",
    "ProviderChatModelConfig",
    "ProviderConfigInput",
    "ProviderModelConfig",
    "clearApiKeyCache",
    "composeModelProvider",
    "configuredRequestAuthStatus",
    "resolveCompatibilityRequestConfig",
    "resolveConfiguredModelHeaders",
    "validateExtensionProvider",
]
