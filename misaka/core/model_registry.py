"""Model registry and custom-model loading for coding-agent providers."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from misaka.ai.api_registry import get_api_provider
from misaka.ai.auth.oauth_bridge import oauth_auth_from_flow
from misaka.ai.auth.resolve import AuthResolutionOverrides
from misaka.ai.auth.types import (
    ApiKeyAuth,
    ApiKeyCredential,
    AuthCheck,
    AuthInteraction,
    AuthOperationOptions,
    AuthResult,
    AuthType,
    CredentialValue,
    ModelAuth,
    OAuthCredential,
    ProviderAuth,
)
from misaka.ai.models import get_models, get_providers
from misaka.ai.models_runtime import (
    ModelsRefreshOptions,
    ModelsRefreshResult,
)
from misaka.ai.models_runtime import (
    Provider as RuntimeProvider,
)
from misaka.ai.models_store import InMemoryModelsStore
from misaka.ai.oauth import (
    OAuthProviderInterface,
)
from misaka.ai.provider_definitions import builtinModels
from misaka.ai.providers import register_builtins as _register_builtins  # noqa: F401
from misaka.ai.radius_provider import RadiusProviderOptions, radiusProvider
from misaka.ai.types import (
    AnthropicMessagesCompat,
    Api,
    AssistantMessage,
    AssistantMessageEventStream,
    Context,
    DeferredHandle,
    Model,
    ModelCompat,
    ModelCost,
    OpenAICompletionsCompat,
    OpenAIResponsesCompat,
    SimpleStreamOptions,
)
from misaka.ai.utils.headers import resolve_provider_headers
from misaka.ai.utils.oauth.types import OAuthCredentials
from misaka.config import get_agent_dir
from misaka.core.models_store import FileModelsStore
from misaka.core.provider_display_names import BUILT_IN_PROVIDER_DISPLAY_NAMES
from misaka.core.resolve_config_value import (
    get_config_value_env_var_names,
    is_command_config_value,
    is_config_value_configured,
    resolveConfigValueOrThrow,
    resolveConfigValueUncached,
    resolveHeadersOrThrow,
)
from misaka.utils.paths import normalize_path
from misaka.utils.values import signal_aborted

from .auth_storage import AuthStatus, AuthStorage, AuthStorageCredentialStore

type _ProviderCompat = dict[str, Any] | OpenAICompletionsCompat | OpenAIResponsesCompat | AnthropicMessagesCompat


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _ThinkingLevelMapSchema(_ConfigModel):
    off: str | None = None
    minimal: str | None = None
    low: str | None = None
    medium: str | None = None
    high: str | None = None
    xhigh: str | None = None


class _ModelCostTierSchema(_ConfigModel):
    inputTokensAbove: int
    input: float
    output: float
    cacheRead: float
    cacheWrite: float


class _ModelCostSchema(_ConfigModel):
    input: float
    output: float
    cacheRead: float
    cacheWrite: float
    tiers: list[_ModelCostTierSchema] | None = None


class _PartialModelCostSchema(_ConfigModel):
    input: float | None = None
    output: float | None = None
    cacheRead: float | None = None
    cacheWrite: float | None = None
    tiers: list[_ModelCostTierSchema] | None = None


# Which compat shape a block is, decided once. The config validator and the parser both
# asked this question, in opposite orders: a block carrying both marker sets was checked
# against the Anthropic schema and then built as an OpenAI-responses one.
_COMPAT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("anthropic-messages", (
        "supportsEagerToolInputStreaming",
        "supportsCacheControlOnTools",
        "forceAdaptiveThinking",
        "sendSessionAffinityHeaders",
    )),
    # Keys only OpenAIResponsesCompat declares. `sessionAffinityFormat` and the other
    # shared names cannot mark it: openai-completions carries them too.
    ("openai-responses", (
        "supportsAdditionalTools",
        "supportsToolSearch",
        "supportsExplicitPromptCacheMode",
    )),
)


def _compat_kind(value: Mapping[str, Any]) -> str:
    for kind, markers in _COMPAT_MARKERS:
        if any(marker in value for marker in markers):
            return kind
    return "openai-completions"


# The runtime models are the definition of a compat block, so the config validator uses
# them directly. The three mirror schemas that used to live here had already drifted: the
# openai-completions one was missing two fields the runtime model accepts, and all three
# ignored unknown keys the runtime models forbid -- so a typo passed validation and then
# failed the load with a generic message instead of a path to the offending key.
_COMPAT_SCHEMAS: dict[str, type[BaseModel]] = {
    "anthropic-messages": AnthropicMessagesCompat,
    "openai-responses": OpenAIResponsesCompat,
    "openai-completions": OpenAICompletionsCompat,
}


def _check_compat_block(value: Any) -> Any:
    """Validate a compat block against the shape its own markers select.

    Declaring it on the field means pydantic reports the path, and means the document is
    not walked a second time afterwards just to reach these blocks.
    """
    if isinstance(value, dict):
        _COMPAT_SCHEMAS[_compat_kind(value)].model_validate(value)
    return value


_CompatBlock = Annotated[dict[str, Any] | None, BeforeValidator(_check_compat_block)]


class _ModelDefinitionSchema(_ConfigModel):
    id: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1)
    api: str | None = Field(default=None, min_length=1)
    baseUrl: str | None = Field(default=None, min_length=1)
    reasoning: bool | None = None
    thinkingLevelMap: _ThinkingLevelMapSchema | None = None
    input: list[Literal["text", "image"]] | None = None
    cost: _ModelCostSchema | None = None
    contextWindow: float | None = None
    maxTokens: float | None = None
    samplingParams: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    compat: _CompatBlock = None


class _ModelOverrideSchema(_ConfigModel):
    name: str | None = Field(default=None, min_length=1)
    reasoning: bool | None = None
    thinkingLevelMap: _ThinkingLevelMapSchema | None = None
    input: list[Literal["text", "image"]] | None = None
    cost: _PartialModelCostSchema | None = None
    contextWindow: float | None = None
    maxTokens: float | None = None
    samplingParams: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    compat: _CompatBlock = None


class _ProviderConfigSchema(_ConfigModel):
    name: str | None = Field(default=None, min_length=1)
    baseUrl: str | None = Field(default=None, min_length=1)
    apiKey: str | None = Field(default=None, min_length=1)
    api: str | None = Field(default=None, min_length=1)
    oauth: Literal["radius"] | None = None
    headers: dict[str, str] | None = None
    compat: _CompatBlock = None
    authHeader: bool | None = None
    models: list[_ModelDefinitionSchema] | None = None
    modelOverrides: dict[str, _ModelOverrideSchema] | None = None


class _ModelsConfigSchema(_ConfigModel):
    providers: dict[str, _ProviderConfigSchema]


@dataclass(slots=True)
class _ProviderOverride:
    baseUrl: str | None = None
    compat: _ProviderCompat | None = None


@dataclass(slots=True)
class _ProviderRequestConfig:
    apiKey: str | None = None
    headers: dict[str, str] | None = None
    authHeader: bool | None = None


@dataclass(slots=True)
class _ProviderAuthPlan:
    overrides: AuthResolutionOverrides | None
    owner: Literal["explicit", "runtime", "stored", "configured", "ambient"]
    credentialType: AuthType | None = None
    credentialEnv: dict[str, str] | None = None
    configuredApiKey: str | None = None


@dataclass(slots=True)
class _CustomModelsResult:
    models: list[Model]
    overrides: dict[str, _ProviderOverride]
    modelOverrides: dict[str, dict[str, dict[str, Any]]]
    providers: dict[str, dict[str, Any]]
    error: str | None = None


class _ProviderModelInput(TypedDict):
    id: str
    name: str
    reasoning: bool
    input: list[Literal["text", "image"]]
    cost: dict[str, float]
    contextWindow: int | float
    maxTokens: int | float
    api: NotRequired[Api]
    baseUrl: NotRequired[str]
    thinkingLevelMap: NotRequired[dict[str, str | None]]
    headers: NotRequired[dict[str, str]]
    compat: NotRequired[ModelCompat | None]


class ProviderConfigInput(TypedDict, total=False):
    name: str
    baseUrl: str
    apiKey: str
    api: Api
    streamSimple: Callable[[Model, Context, SimpleStreamOptions | None], AssistantMessageEventStream]
    headers: dict[str, str]
    authHeader: bool
    oauth: OAuthProviderInterface | Mapping[str, Any]
    models: list[_ProviderModelInput]


class _ResolvedRequestAuthOk(TypedDict):
    ok: Literal[True]
    apiKey: str | None
    headers: dict[str, str] | None
    baseUrl: str | None
    env: dict[str, str] | None


class _ResolvedRequestAuthError(TypedDict):
    ok: Literal[False]
    error: str


type ResolvedRequestAuth = _ResolvedRequestAuthOk | _ResolvedRequestAuthError


def _coerce_refresh_options(
    options: ModelsRefreshOptions | Mapping[str, Any] | None,
) -> ModelsRefreshOptions:
    if options is None:
        return ModelsRefreshOptions()
    if isinstance(options, ModelsRefreshOptions):
        return options
    unknown = set(options) - {"allowNetwork", "providers", "force", "signal"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ValueError(f"Unknown model refresh option(s): {names}")
    providers = options.get("providers")
    if providers is not None and (
        not isinstance(providers, Sequence)
        or isinstance(providers, (str, bytes))
        or not all(isinstance(provider, str) for provider in providers)
    ):
        raise TypeError("providers must be a sequence of provider ids")
    allow_network = options.get("allowNetwork", True)
    if not isinstance(allow_network, bool):
        raise TypeError("allowNetwork must be a boolean")
    force = options.get("force")
    if force is not None and not isinstance(force, bool):
        raise TypeError("force must be a boolean")
    return ModelsRefreshOptions(
        allowNetwork=allow_network,
        providers=list(providers) if providers is not None else None,
        force=force,
        signal=options.get("signal"),
    )


def _dump_compat(compat: _ProviderCompat | None) -> dict[str, Any]:
    if compat is None:
        return {}
    if hasattr(compat, "model_dump"):
        return compat.model_dump(exclude_none=True)
    return dict(compat)


def _coerce_compat(compat: _ProviderCompat | None) -> _ProviderCompat | None:
    if compat is None:
        return None
    if hasattr(compat, "model_dump"):
        return compat
    if not isinstance(compat, dict):
        return compat
    compat_dict = dict(compat)
    runtime_model = {
        "anthropic-messages": AnthropicMessagesCompat,
        "openai-responses": OpenAIResponsesCompat,
        "openai-completions": OpenAICompletionsCompat,
    }[_compat_kind(compat_dict)]
    return runtime_model.model_validate(compat_dict)


def _merge_compat(baseCompat: _ProviderCompat | None, overrideCompat: _ProviderCompat | None) -> _ProviderCompat | None:
    if overrideCompat is None:
        return baseCompat
    base = _dump_compat(baseCompat)
    override = _dump_compat(overrideCompat)
    merged = {**base, **override}
    # pi provider-composer.ts:90-98: these four nested maps merge one level deep; the
    # rest of the block is replaced key by key.
    for key in ("openRouterRouting", "vercelGatewayRouting", "chatTemplateKwargs", "chatTemplateArgs"):
        base_value, override_value = base.get(key), override.get(key)
        if isinstance(base_value, dict) or isinstance(override_value, dict):
            merged[key] = {**(base_value or {}), **(override_value or {})}
    return _coerce_compat(merged)


def _apply_model_override(model: Model, override: dict[str, Any]) -> Model:
    update: dict[str, Any] = {}
    for key in ("name", "reasoning", "input", "contextWindow", "maxTokens"):
        if key in override:
            update[key] = override[key]
    if "thinkingLevelMap" in override:
        update["thinkingLevelMap"] = {**(model.thinkingLevelMap or {}), **(override["thinkingLevelMap"] or {})}
    if "cost" in override and isinstance(override["cost"], dict):
        # pi provider-composer.ts:112-120: every rate, and `tiers`, falls back to the base
        # cost on its own. Rebuilding from the four rates dropped inherited tiers.
        cost = model.cost.model_dump()
        for key in ("input", "output", "cacheRead", "cacheWrite", "tiers"):
            if override["cost"].get(key) is not None:
                cost[key] = override["cost"][key]
        update["cost"] = ModelCost.model_validate(cost)
    if override.get("samplingParams"):
        update["samplingParams"] = {**(model.samplingParams or {}), **override["samplingParams"]}
    update["compat"] = _merge_compat(model.compat, override.get("compat"))
    return model.model_copy(update=update)


def _format_validation_path(parts: tuple[Any, ...]) -> str:
    path = ".".join(str(part) for part in parts if part != "")
    return path or "root"


def _validation_messages(error: ValidationError, prefix: tuple[Any, ...] = ()) -> list[str]:
    messages: list[str] = []
    for item in error.errors(include_url=False):
        loc = prefix + tuple(item.get("loc", ()))
        messages.append(f"{_format_validation_path(loc)}: {item['msg']}")
    return messages


def _validate_models_config(parsed: Any) -> list[str]:
    try:
        _ModelsConfigSchema.model_validate(parsed)
    except ValidationError as error:
        return _validation_messages(error)

    return []


def _strip_json_comments(value: str) -> str:
    without_comments = re.sub(
        r'"(?:\\.|[^"\\])*"|//[^\n]*',
        lambda match: match.group(0) if match.group(0).startswith('"') else "",
        value,
    )
    return re.sub(
        r'"(?:\\.|[^"\\])*"|,(\s*[}\]])',
        lambda match: match.group(0) if match.group(0).startswith('"') else match.group(1),
        without_comments,
    )


def _empty_custom_models_result(error: str | None = None) -> _CustomModelsResult:
    return _CustomModelsResult(
        models=[], overrides={}, modelOverrides={}, providers={}, error=error
    )


def _coalesce[T](value: T | None, fallback: T) -> T:
    return fallback if value is None else value


def _build_oauth_provider(provider_name: str, oauth: OAuthProviderInterface | Mapping[str, Any]) -> OAuthProviderInterface:
    if isinstance(oauth, Mapping):
        name = oauth["name"]
        is_subscription = oauth.get("isSubscription")
        uses_callback_server = oauth.get("usesCallbackServer")
        modify_models = oauth.get("modifyModels")
        login = oauth["login"]
        refresh_token = oauth["refreshToken"]
        get_api_key = oauth["getApiKey"]
    else:
        name = oauth.name
        is_subscription = getattr(oauth, "isSubscription", None)
        uses_callback_server = getattr(oauth, "usesCallbackServer", None)
        modify_models = getattr(oauth, "modifyModels", None)
        login = oauth.login
        refresh_token = oauth.refreshToken
        get_api_key = oauth.getApiKey
    return cast(
        OAuthProviderInterface,
        SimpleNamespace(
            id=provider_name,
            name=name,
            isSubscription=is_subscription,
            usesCallbackServer=uses_callback_server,
            modifyModels=modify_models,
            login=login,
            refreshToken=refresh_token,
            getApiKey=get_api_key,
        ),
    )


def _oauth_name(value: Any) -> str | None:
    if isinstance(value, Mapping):
        name = value.get("name")
    else:
        name = getattr(value, "name", None)
    return name if isinstance(name, str) else None


def _compose_legacy_api_key_auth(
    provider_name: str,
    base: RuntimeProvider | None,
    config: ProviderConfigInput,
) -> ApiKeyAuth | None:
    base_auth = getattr(base, "auth", None)
    inherited = getattr(base_auth, "apiKey", None)
    raw_key = config.get("apiKey")
    oauth = config.get("oauth") or getattr(base_auth, "oauth", None)
    if inherited is None and raw_key is None and oauth is not None:
        return None

    async def login(interaction: AuthInteraction) -> ApiKeyCredential:
        inherited_login = getattr(inherited, "login", None)
        if inherited_login is not None:
            return await inherited_login(interaction)
        return ApiKeyCredential(
            key=await interaction.prompt(
                SimpleNamespace(
                    type="secret", message="Enter API key", placeholder=None
                )
            )
        )

    async def check(
        *, ctx: Any, credential: ApiKeyCredential | None, signal: Any
    ) -> AuthCheck | None:
        inherited_check = getattr(inherited, "check", None)
        if credential is not None:
            if inherited_check is not None:
                return await inherited_check(
                    ctx=ctx, credential=credential, signal=signal
                )
            if credential.key:
                return AuthCheck(type="api_key", source="stored credential")
            inherited_resolve = getattr(inherited, "resolve", None)
            if inherited_resolve is not None:
                resolved = await inherited_resolve(
                    ctx=ctx, credential=credential, signal=signal
                )
                return (
                    AuthCheck(type="api_key", source=resolved.source)
                    if resolved is not None
                    else None
                )
            return None
        if raw_key is not None:
            if is_command_config_value(raw_key):
                return AuthCheck(type="api_key", source="configured API key")
            for name in get_config_value_env_var_names(raw_key):
                if await ctx.env(name) is None:
                    return None
            return AuthCheck(type="api_key", source="configured API key")
        if inherited_check is not None:
            return await inherited_check(ctx=ctx, credential=None, signal=signal)
        inherited_resolve = getattr(inherited, "resolve", None)
        if inherited_resolve is None:
            return None
        resolved = await inherited_resolve(
            ctx=ctx, credential=None, signal=signal
        )
        return (
            AuthCheck(type="api_key", source=resolved.source)
            if resolved is not None
            else None
        )

    async def resolve(
        *, ctx: Any, credential: ApiKeyCredential | None, signal: Any
    ) -> AuthResult | None:
        inherited_resolve = getattr(inherited, "resolve", None)
        if credential is not None:
            if inherited_resolve is not None:
                return await inherited_resolve(
                    ctx=ctx, credential=credential, signal=signal
                )
            if not credential.key:
                return None
            return AuthResult(
                auth=ModelAuth(apiKey=credential.key),
                env=credential.env,
                source="stored credential",
            )
        if raw_key is not None:
            env = {
                name: value
                for name in get_config_value_env_var_names(raw_key)
                if (value := await ctx.env(name)) is not None
            }
            key = resolveConfigValueOrThrow(
                raw_key,
                f'API key for provider "{provider_name}"',
                env or None,
            )
            if inherited_resolve is not None:
                return await inherited_resolve(
                    ctx=ctx,
                    credential=ApiKeyCredential(key=key),
                    signal=signal,
                )
            return AuthResult(
                auth=ModelAuth(apiKey=key), source="configured API key"
            )
        if inherited_resolve is None:
            return None
        return await inherited_resolve(ctx=ctx, credential=None, signal=signal)

    return ApiKeyAuth(
        name=getattr(inherited, "name", "API key"),
        resolve=resolve,
        login=login,
        check=check,
    )


class _LegacyRuntimeProvider:
    """Instance-owned provider composed from one legacy extension registration."""

    def __init__(
        self,
        registry: ModelRegistry,
        provider_name: str,
        base: RuntimeProvider | None,
        models_config: Mapping[str, Any] | None,
        extension: ProviderConfigInput,
    ) -> None:
        self.id = provider_name
        self._registry = registry
        self._base = base
        self._extension = extension
        config: ProviderConfigInput = cast(
            ProviderConfigInput, dict(models_config or {})
        )
        model_headers = config.get("headers")
        extension_headers = extension.get("headers")
        for key, value in extension.items():
            if value is not None:
                config[key] = value
        if model_headers is not None or extension_headers is not None:
            config["headers"] = {
                **(model_headers or {}),
                **(extension_headers or {}),
            }
        oauth_config = extension.get("oauth")
        base_auth = getattr(base, "auth", None)
        oauth = (
            oauth_auth_from_flow(
                _build_oauth_provider(provider_name, oauth_config),
                isSubscription=(
                    oauth_config.get("isSubscription")
                    if isinstance(oauth_config, Mapping)
                    else getattr(oauth_config, "isSubscription", None)
                ),
            )
            if oauth_config is not None
            else getattr(base_auth, "oauth", None)
        )
        api_key = _compose_legacy_api_key_auth(provider_name, base, config)
        self.auth = ProviderAuth(apiKey=api_key, oauth=oauth)
        self.name = (
            config.get("name")
            or getattr(base, "name", None)
            or _oauth_name(oauth_config)
            or provider_name
        )
        self.baseUrl = (
            config["baseUrl"]
            if config.get("baseUrl") is not None
            else getattr(base, "baseUrl", None)
        )
        self.headers = getattr(base, "headers", None)
        base_filter = getattr(base, "filterModels", None)
        if base_filter is not None:
            self.filterModels = base_filter
        base_refresh = getattr(base, "refreshModels", None)
        if base_refresh is not None:
            self.refreshModels = base_refresh

    def getModels(self) -> list[Model]:
        return [
            model for model in self._registry._models if model.provider == self.id
        ]

    def _supports_base_api(self, model: Model) -> bool:
        if self._base is None:
            return False
        try:
            return any(item.api == model.api for item in self._base.getModels())
        except Exception:  # noqa: BLE001 - an invalid base has no usable API
            return False

    def _stream(
        self,
        model: Model,
        context: Context,
        options: Any,
        *,
        simple: bool,
    ) -> AssistantMessageEventStream:
        stream_simple = self._extension.get("streamSimple")
        if stream_simple is not None and model.api == self._extension.get("api"):
            return stream_simple(
                model, context, cast(SimpleStreamOptions | None, options)
            )
        if self._base is not None and self._supports_base_api(model):
            method = self._base.streamSimple if simple else self._base.stream
            return method(model, context, options)
        api = get_api_provider(model.api)
        if api is None:
            raise RuntimeError(f"No API provider registered for api: {model.api}")
        method = api.streamSimple if simple else api.stream
        return method(model, context, options)

    def stream(
        self, model: Model, context: Context, options: Any = None
    ) -> AssistantMessageEventStream:
        return self._stream(model, context, options, simple=False)

    def streamSimple(
        self, model: Model, context: Context, options: Any = None
    ) -> AssistantMessageEventStream:
        return self._stream(model, context, options, simple=True)


class ModelRegistry:
    def __init__(
        self,
        authStorage: AuthStorage,
        modelsJsonPath: str | None,
        modelsStore: Any | None = None,
    ):
        self._models: list[Model] = []
        self._providerRequestConfigs: dict[str, _ProviderRequestConfig] = {}
        self._modelRequestHeaders: dict[str, dict[str, str]] = {}
        self._registeredProviders: dict[str, ProviderConfigInput] = {}
        self._legacyPreviousProviders: dict[str, RuntimeProvider | None] = {}
        self._nativeProviderIds: dict[str, None] = {}
        self._nativePreviousProviders: dict[str, RuntimeProvider | None] = {}
        self._nativeAuthChecks: dict[str, AuthCheck] = {}
        # models.json `oauth: "radius"` blocks, keyed by id, valued by the provider they
        # displaced (None when there was none) so a reload can put it back.
        self._radiusProviders: dict[str, RuntimeProvider | None] = {}
        self._offlineRefreshDirty = False
        self._offlineRefreshTask: asyncio.Task[ModelsRefreshResult] | None = None
        self._offlineRefreshJoiners: dict[
            asyncio.Task[ModelsRefreshResult], int
        ] = {}
        self._offlineRefreshObserved: set[asyncio.Task[ModelsRefreshResult]] = set()
        self._refreshContext = ContextVar[object | None](
            f"model_registry_refresh_{id(self)}", default=None
        )
        self._activeRefreshes: set[object] = set()
        self._modelOverrides: dict[str, dict[str, dict[str, Any]]] = {}
        self._modelsJsonProviders: dict[str, dict[str, Any]] = {}
        self._loadError: str | None = None
        self.authStorage = authStorage
        self._modelsJsonPath = normalize_path(modelsJsonPath) if modelsJsonPath else None
        if modelsStore is None:
            modelsStore = (
                FileModelsStore(
                    os.path.join(os.path.dirname(self._modelsJsonPath), "models-store.json")
                )
                if self._modelsJsonPath
                else InMemoryModelsStore()
            )
        self._authModels = builtinModels(
            credentials=AuthStorageCredentialStore(authStorage),
            modelsStore=modelsStore,
        )
        self._reloadLegacy()

    @classmethod
    def create(cls, authStorage: AuthStorage, modelsJsonPath: str | None = None) -> ModelRegistry:
        return cls(authStorage, modelsJsonPath or os.path.join(get_agent_dir(), "models.json"))

    @classmethod
    def inMemory(cls, authStorage: AuthStorage) -> ModelRegistry:
        return cls(authStorage, None, InMemoryModelsStore())

    def _reloadLegacy(self) -> None:
        for provider_name in list(self._legacyPreviousProviders):
            if provider_name not in self._registeredProviders:
                self._restoreLegacyProvider(provider_name)
        self._providerRequestConfigs.clear()
        self._modelRequestHeaders.clear()
        self._modelOverrides.clear()
        self._loadError = None
        self._loadModels()
        for provider_name, config in self._registeredProviders.items():
            self._applyProviderConfig(provider_name, config)
            self._recomposeLegacyProvider(
                provider_name,
                self._modelsJsonProviders.get(provider_name),
                config,
            )

    async def refresh(
        self,
        options: ModelsRefreshOptions | Mapping[str, Any] | None = None,
    ) -> ModelsRefreshResult:
        """Reload static configuration and refresh native provider catalogs."""
        resolved = _coerce_refresh_options(options)
        refresh_context = self._refreshContext.get()
        if (
            refresh_context is not None
            and refresh_context in self._activeRefreshes
        ):
            raise RuntimeError(
                "A provider cannot refresh the model registry from its own "
                "refreshModels callback"
            )
        scheduled = await self._joinOfflineRefreshes()
        if (
            scheduled is not None
            and not resolved.allowNetwork
            and resolved.providers is None
            and resolved.signal is None
        ):
            previous_auth = self.authStorage.data
            latest_auth = await self.authStorage.readLatestData()
            if latest_auth is previous_auth:
                return scheduled
        return await self._refreshOnce(resolved)

    async def _refreshOnce(
        self,
        resolved: ModelsRefreshOptions,
    ) -> ModelsRefreshResult:
        refresh_context = object()
        self._activeRefreshes.add(refresh_context)
        token = self._refreshContext.set(refresh_context)
        try:
            try:
                await self.authStorage.readLatestData(
                    AuthOperationOptions(signal=resolved.signal)
                )
            except Exception:
                if signal_aborted(resolved.signal):
                    return ModelsRefreshResult(aborted=True)
                raise
            self._reloadLegacy()
            result = await self._authModels.refresh(resolved)

            selected = (
                set(resolved.providers) if resolved.providers is not None else None
            )
            native_providers = [
                (provider_id, self._authModels.getProvider(provider_id))
                for provider_id in list(self._nativeProviderIds)
            ]
            for provider_id, provider in native_providers:
                if selected is not None and provider_id not in selected:
                    continue
                check_error: Exception | None = None
                try:
                    check = await self._authModels.checkAuth(
                        provider_id, AuthOperationOptions(signal=resolved.signal)
                    )
                except Exception as error:  # noqa: BLE001 - provider failures are returned
                    check_error = error
                    check = None
                if (
                    provider_id not in self._nativeProviderIds
                    or self._authModels.getProvider(provider_id) is not provider
                ):
                    continue
                if check_error is not None:
                    result.errors.setdefault(provider_id, check_error)
                if check is None:
                    self._nativeAuthChecks.pop(provider_id, None)
                else:
                    self._nativeAuthChecks[provider_id] = check
            return result
        finally:
            self._activeRefreshes.discard(refresh_context)
            self._refreshContext.reset(token)

    def _scheduleOfflineRefresh(self) -> None:
        self._offlineRefreshDirty = True
        task = self._offlineRefreshTask
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._drainOfflineRefresh())
        self._offlineRefreshTask = task
        task.add_done_callback(self._finishOfflineRefresh)

    async def _drainOfflineRefresh(self) -> ModelsRefreshResult:
        result = ModelsRefreshResult(aborted=False)
        while self._offlineRefreshDirty:
            self._offlineRefreshDirty = False
            result = await self._refreshOnce(ModelsRefreshOptions(allowNetwork=False))
        return result

    def _finishOfflineRefresh(
        self, task: asyncio.Task[ModelsRefreshResult]
    ) -> None:
        if self._offlineRefreshTask is task:
            self._offlineRefreshTask = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as error:  # noqa: BLE001 - report unexpected task failure
            if (
                task not in self._offlineRefreshObserved
                and self._offlineRefreshJoiners.get(task, 0) == 0
            ):
                task.get_loop().call_exception_handler(
                    {
                        "message": "Background model refresh failed",
                        "exception": error,
                        "task": task,
                    }
                )
        finally:
            self._offlineRefreshObserved.discard(task)
            if self._offlineRefreshDirty:
                self._scheduleOfflineRefresh()

    async def _joinOfflineRefreshes(self) -> ModelsRefreshResult | None:
        result: ModelsRefreshResult | None = None
        task = self._offlineRefreshTask
        if task is not None and task.done() and not self._offlineRefreshDirty:
            self._offlineRefreshObserved.add(task)
            return task.result()
        while True:
            if self._offlineRefreshDirty:
                self._scheduleOfflineRefresh()
            task = self._offlineRefreshTask
            if task is None:
                return result
            if task is asyncio.current_task():
                raise RuntimeError(
                    "A provider cannot refresh the model registry from its own "
                    "refreshModels callback"
                )
            self._offlineRefreshJoiners[task] = (
                self._offlineRefreshJoiners.get(task, 0) + 1
            )
            try:
                result = await asyncio.shield(task)
            finally:
                joiners = self._offlineRefreshJoiners[task] - 1
                if joiners:
                    self._offlineRefreshJoiners[task] = joiners
                else:
                    self._offlineRefreshJoiners.pop(task)
            if self._offlineRefreshTask is task and task.done():
                self._offlineRefreshTask = None
            if not self._offlineRefreshDirty and self._offlineRefreshTask is None:
                return result

    def getError(self) -> str | None:
        return self._loadError

    def _loadModels(self) -> None:
        custom = self._loadCustomModels(self._modelsJsonPath) if self._modelsJsonPath else _empty_custom_models_result()
        if custom.error:
            self._loadError = custom.error

        self._modelsJsonProviders = custom.providers
        self._modelOverrides = custom.modelOverrides
        self._configureRadiusProviders()
        built_in_models = self._loadBuiltInModels(custom.overrides)
        combined = self._mergeCustomModels(built_in_models, custom.models)

        for oauth_provider in self.authStorage.getOAuthProviders():
            credential = self.authStorage.get(oauth_provider.id)
            if (
                isinstance(credential, dict)
                and credential.get("type") == "oauth"
                and getattr(oauth_provider, "modifyModels", None)
            ):
                combined = oauth_provider.modifyModels(
                    combined,
                    OAuthCredentials.model_validate({key: value for key, value in credential.items() if key != "type"}),
                )

        # pi provider-composer.ts:431 -- modelOverrides are the topmost user-config layer:
        # they apply once, after custom-model upserts and the legacy OAuth projection.
        # Applying them to the built-in models instead let a same-id custom model, or a
        # provider that rewrites its models on login, drop them silently.
        self._models = self._applyModelOverrides(combined)

    def _configureRadiusProviders(self) -> None:
        """pi model-runtime.ts:219-233: compose a radius provider around a configured gateway.

        A native extension provider with the same id keeps precedence, as pi's
        ``recomposeProvider`` prefers ``nativeExtensionProviders`` over builtins.
        """
        for provider_id, previous in self._radiusProviders.items():
            if previous is None:
                self._authModels.deleteProvider(provider_id)
            else:
                self._authModels.setProvider(previous)
        self._radiusProviders.clear()
        for provider_id, config in self._modelsJsonProviders.items():
            if (
                config.get("oauth") != "radius"
                or not config.get("baseUrl")
                or provider_id in self._nativeProviderIds
            ):
                continue
            self._radiusProviders[provider_id] = self._authModels.getProvider(provider_id)
            self._authModels.setProvider(
                radiusProvider(
                    RadiusProviderOptions(
                        id=provider_id,
                        name=config.get("name") or provider_id,
                        gateway=re.sub(r"/v1/?$", "", config["baseUrl"]),
                    )
                )
            )

    def _applyModelOverrides(self, models: list[Model], provider: str | None = None) -> list[Model]:
        result: list[Model] = []
        for model in models:
            per_model = self._modelOverrides.get(model.provider, {})
            override = per_model.get(model.id) if (provider is None or model.provider == provider) else None
            result.append(_apply_model_override(model, override) if override is not None else model)
        return result

    def _loadBuiltInModels(self, overrides: dict[str, _ProviderOverride]) -> list[Model]:
        result: list[Model] = []
        for provider in get_providers():
            models = list(get_models(provider))
            provider_override = overrides.get(provider)
            for model in models:
                updated = model
                if provider_override is not None:
                    updated = updated.model_copy(
                        update={
                            "baseUrl": _coalesce(provider_override.baseUrl, updated.baseUrl),
                            "compat": _merge_compat(updated.compat, provider_override.compat),
                        }
                    )
                result.append(updated)
        return result

    @staticmethod
    def _mergeCustomModels(builtInModels: list[Model], customModels: list[Model]) -> list[Model]:
        merged = list(builtInModels)
        for custom_model in customModels:
            existing_index = next(
                (
                    index
                    for index, model in enumerate(merged)
                    if model.provider == custom_model.provider and model.id == custom_model.id
                ),
                None,
            )
            if existing_index is None:
                merged.append(custom_model)
            else:
                merged[existing_index] = custom_model
        return merged

    def _loadCustomModels(self, modelsJsonPath: str) -> _CustomModelsResult:
        if not os.path.exists(modelsJsonPath):
            return _empty_custom_models_result()

        try:
            with open(modelsJsonPath, encoding="utf-8-sig") as handle:
                parsed = json.loads(_strip_json_comments(handle.read()))

            errors = _validate_models_config(parsed)
            if errors:
                rendered = "\n".join(f"  - {message}" for message in errors)
                return _empty_custom_models_result(f"Invalid models.json schema:\n{rendered}\n\nFile: {modelsJsonPath}")

            # pi keeps a `compositionErrors` map keyed by provider (provider-composer):
            # a provider that fails to compose falls back to its built-in definition and
            # the rest of the file still loads. Validating the whole document as one unit
            # threw away every other provider's models over one typo.
            providers: dict[str, Any] = {}
            provider_errors: list[str] = []
            for provider_name, provider_config in parsed["providers"].items():
                try:
                    self._validateProviderConfig(provider_name, provider_config)
                except ValueError as error:
                    provider_errors.append(str(error))
                    continue
                providers[provider_name] = provider_config

            overrides: dict[str, _ProviderOverride] = {}
            model_overrides: dict[str, dict[str, dict[str, Any]]] = {}

            for provider_name, provider_config in providers.items():
                if provider_config.get("baseUrl") is not None or provider_config.get("compat") is not None:
                    overrides[provider_name] = _ProviderOverride(
                        baseUrl=provider_config.get("baseUrl"),
                        compat=_coerce_compat(provider_config.get("compat")),
                    )

                self._storeProviderRequestConfig(provider_name, provider_config)

                raw_model_overrides = provider_config.get("modelOverrides")
                if isinstance(raw_model_overrides, dict):
                    model_overrides[provider_name] = dict(raw_model_overrides)
                    for model_id, model_override in raw_model_overrides.items():
                        if isinstance(model_override, dict):
                            self._storeModelHeaders(provider_name, model_id, model_override.get("headers"))

            error: str | None = None
            if provider_errors:
                rendered = "\n".join(f"  - {message}" for message in provider_errors)
                error = f"Invalid models.json providers (ignored):\n{rendered}\n\nFile: {modelsJsonPath}"

            return _CustomModelsResult(
                models=self._parseModels(providers),
                overrides=overrides,
                modelOverrides=model_overrides,
                providers={
                    provider_name: dict(provider_config)
                    for provider_name, provider_config in providers.items()
                },
                error=error,
            )
        except json.JSONDecodeError as error:
            return _empty_custom_models_result(f"Failed to parse models.json: {error}\n\nFile: {modelsJsonPath}")
        except Exception as error:  # noqa: BLE001
            return _empty_custom_models_result(f"Failed to load models.json: {error}\n\nFile: {modelsJsonPath}")

    def _validateProviderConfig(self, provider_name: str, provider_config: dict[str, Any]) -> None:
        """Structural checks for one provider block; raises ValueError naming the provider."""
        is_built_in = provider_name in set(get_providers())
        has_provider_api = bool(provider_config.get("api"))
        models = provider_config.get("models") or []
        has_model_overrides = bool(provider_config.get("modelOverrides")) and len(provider_config["modelOverrides"]) > 0
        oauth = provider_config.get("oauth")

        if oauth and not provider_config.get("baseUrl"):
            raise ValueError(f'Provider {provider_name}: "baseUrl" is required when "oauth" is set.')
        if len(models) == 0:
            # pi provider-composer.ts:178-190: a block carrying only auth settings
            # (`apiKey`, `oauth`, `authHeader`) is a complete overlay too.
            if (
                provider_config.get("baseUrl") is None
                and provider_config.get("headers") is None
                and provider_config.get("compat") is None
                and not has_model_overrides
                and provider_config.get("apiKey") is None
                and oauth is None
                and provider_config.get("authHeader") is None
            ):
                raise ValueError(
                    f'Provider {provider_name}: must specify "baseUrl", "headers", "compat", "modelOverrides", or "models".'
                )
        elif not is_built_in:
            if not provider_config.get("baseUrl"):
                raise ValueError(f'Provider {provider_name}: "baseUrl" is required when defining custom models.')
            if not provider_config.get("apiKey") and oauth is None:
                raise ValueError(f'Provider {provider_name}: "apiKey" is required when defining custom models.')

        for model_def in models:
            has_model_api = bool(model_def.get("api"))
            if not has_provider_api and not has_model_api and not is_built_in:
                raise ValueError(
                    f'Provider {provider_name}, model {model_def.get("id")}: no "api" specified. Set at provider or model level.'
                )
            if not model_def.get("id"):
                raise ValueError(f'Provider {provider_name}: model missing "id"')
            if model_def.get("contextWindow") is not None and model_def["contextWindow"] <= 0:
                raise ValueError(f'Provider {provider_name}, model {model_def["id"]}: invalid contextWindow')
            if model_def.get("maxTokens") is not None and model_def["maxTokens"] <= 0:
                raise ValueError(f'Provider {provider_name}, model {model_def["id"]}: invalid maxTokens')

    def _parseModels(self, providers: dict[str, Any]) -> list[Model]:
        models: list[Model] = []
        built_in_providers = set(get_providers())
        defaults_cache: dict[str, dict[str, str]] = {}

        def get_built_in_defaults(provider_name: str) -> dict[str, str] | None:
            if provider_name not in built_in_providers:
                return None
            if provider_name in defaults_cache:
                return defaults_cache[provider_name]
            built_in_models = list(get_models(provider_name))
            if not built_in_models:
                return None
            defaults = {"api": built_in_models[0].api, "baseUrl": built_in_models[0].baseUrl}
            defaults_cache[provider_name] = defaults
            return defaults

        for provider_name, provider_config in providers.items():
            model_defs = provider_config.get("models") or []
            if not model_defs:
                continue

            built_in_defaults = get_built_in_defaults(provider_name)
            for model_def in model_defs:
                api = _coalesce(model_def.get("api"), _coalesce(provider_config.get("api"), (built_in_defaults or {}).get("api")))
                if api is None:
                    continue
                base_url = _coalesce(
                    model_def.get("baseUrl"),
                    _coalesce(provider_config.get("baseUrl"), (built_in_defaults or {}).get("baseUrl")),
                )
                if base_url is None:
                    continue

                compat = _merge_compat(
                    _coerce_compat(provider_config.get("compat")),
                    _coerce_compat(model_def.get("compat")),
                )
                self._storeModelHeaders(provider_name, model_def["id"], model_def.get("headers"))
                cost = model_def.get("cost")
                if cost is None:
                    cost = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}

                models.append(
                    Model(
                        id=model_def["id"],
                        name=_coalesce(model_def.get("name"), model_def["id"]),
                        api=api,
                        provider=provider_name,
                        baseUrl=base_url,
                        reasoning=_coalesce(model_def.get("reasoning"), False),
                        thinkingLevelMap=model_def.get("thinkingLevelMap"),
                        input=_coalesce(model_def.get("input"), ["text"]),
                        cost=ModelCost.model_validate(cost),
                        contextWindow=_coalesce(model_def.get("contextWindow"), 128000),
                        maxTokens=_coalesce(model_def.get("maxTokens"), 16384),
                        samplingParams=model_def.get("samplingParams"),
                        headers=None,
                        compat=compat,
                    )
                )
        return models

    def getAll(self) -> list[Model]:
        if not self._nativeProviderIds and not self._radiusProviders:
            return list(self._models)
        static = [
            model for model in self._models if model.provider not in self._nativeProviderIds
        ]
        dynamic: list[Model] = []
        for provider_id in (*self._nativeProviderIds, *self._radiusProviders):
            dynamic.extend(self._authModels.getModels(provider_id))
        dynamic = self._applyModelOverrides(dynamic)
        if not self._radiusProviders:
            return static + dynamic
        # pi provider-composer.ts:193-205: models.json definitions upsert onto the base
        # (here: gateway) catalog rather than replacing it.
        radius_static = [model for model in static if model.provider in self._radiusProviders]
        return [
            model for model in static if model.provider not in self._radiusProviders
        ] + self._mergeCustomModels(dynamic, radius_static)

    def getAvailable(self) -> list[Model]:
        return [model for model in self.getAll() if self.hasConfiguredAuth(model)]

    def find(self, provider: str, modelId: str) -> Model | None:
        return next(
            (
                model
                for model in self.getAll()
                if model.provider == provider and model.id == modelId
            ),
            None,
        )

    def hasConfiguredAuth(self, model: Model) -> bool:
        if (
            self.authStorage.runtimeOverrides.get(model.provider)
            or self.authStorage.has(model.provider)
        ):
            return True
        api_key = self._providerRequestConfigs.get(model.provider, _ProviderRequestConfig()).apiKey
        if api_key is not None:
            # A configured key owns this provider even when it is incomplete; ambient
            # credentials must not make an unusable config look ready.
            return is_command_config_value(api_key) or is_config_value_configured(api_key)
        if model.provider in self._nativeProviderIds:
            return model.provider in self._nativeAuthChecks
        return self.authStorage.hasAuth(model.provider)

    async def checkConfiguredAuth(self, model: Model) -> bool:
        """Check current auth while retaining legacy custom-provider configuration."""
        return await self.checkProviderAuth(model.provider) is not None

    @staticmethod
    def _getModelRequestKey(provider: str, modelId: str) -> str:
        return f"{provider}:{modelId}"

    def _storeProviderRequestConfig(self, providerName: str, config: Mapping[str, Any]) -> None:
        api_key = config.get("apiKey")
        headers = config.get("headers")
        auth_header = config.get("authHeader")
        existing = self._providerRequestConfigs.get(providerName)
        if (
            existing is None
            and api_key is None
            and headers is None
            and auth_header is None
        ):
            return
        self._providerRequestConfigs[providerName] = _ProviderRequestConfig(
            apiKey=(
                cast(str, api_key)
                if api_key is not None
                else (existing.apiKey if existing is not None else None)
            ),
            headers=(
                {
                    **(
                        (existing.headers or {})
                        if existing is not None
                        else {}
                    ),
                    **cast(dict[str, str], headers),
                }
                if headers is not None
                else (existing.headers if existing is not None else None)
            ),
            authHeader=(
                cast(bool, auth_header)
                if auth_header is not None
                else (existing.authHeader if existing is not None else None)
            ),
        )

    def _storeModelHeaders(self, providerName: str, modelId: str, headers: dict[str, str] | None) -> None:
        if headers is None:
            return
        key = self._getModelRequestKey(providerName, modelId)
        merged = {**self._modelRequestHeaders.get(key, {}), **headers}
        if merged:
            self._modelRequestHeaders[key] = merged

    def _resolveConfiguredHeaders(
        self,
        model: Model,
        apiKey: str | None = None,
        env: Mapping[str, str] | None = None,
        providerEnv: Mapping[str, str] | None = None,
    ) -> dict[str, str] | None:
        provider_config = self._providerRequestConfigs.get(model.provider)
        provider_headers = resolveHeadersOrThrow(
            provider_config.headers if provider_config else None,
            f'provider "{model.provider}"',
            providerEnv,
        )
        model_headers = resolveHeadersOrThrow(
            self._modelRequestHeaders.get(
                self._getModelRequestKey(model.provider, model.id)
            ),
            f'model "{model.provider}/{model.id}"',
            env,
        )
        headers = resolve_provider_headers(
            model.headers, provider_headers, model_headers
        )
        if provider_config and provider_config.authHeader:
            if not apiKey:
                raise RuntimeError("authHeader requires a resolved API key")
            headers = resolve_provider_headers(
                headers, {"Authorization": f"Bearer {apiKey}"}
            )
        return headers or None

    def _resolveProviderConfiguredHeaders(
        self,
        provider: str,
        apiKey: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, str] | None:
        provider_config = self._providerRequestConfigs.get(provider)
        headers = resolveHeadersOrThrow(
            provider_config.headers if provider_config else None,
            f'provider "{provider}"',
            env,
        )
        if provider_config and provider_config.authHeader:
            if not apiKey:
                raise RuntimeError("authHeader requires a resolved API key")
            headers = resolve_provider_headers(
                headers, {"Authorization": f"Bearer {apiKey}"}
            )
        return headers or None

    @staticmethod
    def _mergeAuthEnv(
        *values: Mapping[str, str] | None,
    ) -> dict[str, str] | None:
        merged: dict[str, str] = {}
        for value in values:
            if value is not None:
                merged.update(value)
        return merged or None

    @staticmethod
    def _credentialEnv(credential: Any) -> dict[str, str] | None:
        value = credential.get("env") if isinstance(credential, Mapping) else None
        if not isinstance(value, Mapping):
            return None
        resolved = {
            key: item
            for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)
        }
        return resolved or None

    async def _prepareProviderAuth(
        self,
        provider: str,
        overrides: AuthResolutionOverrides | None,
    ) -> _ProviderAuthPlan:
        if overrides is not None and overrides.apiKey is not None:
            return _ProviderAuthPlan(
                overrides=overrides,
                owner="explicit",
                credentialType="api_key",
                credentialEnv=overrides.env,
            )

        if self.authStorage.runtimeOverrides.get(provider):
            return _ProviderAuthPlan(
                overrides=overrides,
                owner="runtime",
                credentialType="api_key",
                credentialEnv=overrides.env if overrides is not None else None,
            )

        signal = overrides.signal if overrides is not None else None
        await self.authStorage.readLatestData(
            AuthOperationOptions(signal=signal) if signal is not None else None
        )
        if self.authStorage.has(provider):
            credential = self.authStorage.get(provider)
            if not isinstance(credential, Mapping):
                raise ValueError(f'Invalid stored credential for provider "{provider}"')
            credential_type = credential.get("type")
            if credential_type == "api_key":
                ApiKeyCredential.model_validate(credential)
            elif credential_type == "oauth":
                OAuthCredential.model_validate(credential)
            else:
                raise ValueError(f'Invalid stored credential for provider "{provider}"')
            return _ProviderAuthPlan(
                overrides=overrides,
                owner="stored",
                credentialType=cast(AuthType, credential_type),
                credentialEnv=self._credentialEnv(credential),
            )

        provider_config = self._providerRequestConfigs.get(provider)
        raw_key = provider_config.apiKey if provider_config is not None else None
        if raw_key is not None:
            configured_key = resolveConfigValueOrThrow(
                raw_key,
                f'API key for provider "{provider}"',
                overrides.env if overrides is not None else None,
            )
            prepared = (overrides or AuthResolutionOverrides()).model_copy(
                update={"apiKey": configured_key}
            )
            return _ProviderAuthPlan(
                overrides=prepared,
                owner="configured",
                credentialType="api_key",
                credentialEnv=prepared.env,
                configuredApiKey=configured_key,
            )

        return _ProviderAuthPlan(overrides=overrides, owner="ambient")

    async def _planApiKey(
        self,
        provider: str,
        plan: _ProviderAuthPlan,
    ) -> str | None:
        if plan.credentialType != "api_key":
            return None
        if plan.owner in {"explicit", "configured"}:
            return plan.overrides.apiKey if plan.overrides is not None else None
        auth_options: dict[str, Any] = {"includeFallback": False}
        if plan.overrides is not None and plan.overrides.signal is not None:
            auth_options["signal"] = plan.overrides.signal
        return await self.authStorage.getApiKey(provider, auth_options)

    def _providerHeaderEnv(
        self,
        provider: str,
        plan: _ProviderAuthPlan,
        resolution: AuthResult,
    ) -> dict[str, str] | None:
        if plan.owner == "stored" and plan.credentialType == "oauth":
            current = self.authStorage.get(provider)
            if isinstance(current, Mapping) and current.get("type") == "oauth":
                return self._credentialEnv(current)
            return plan.credentialEnv
        return self._mergeAuthEnv(
            plan.credentialEnv,
            plan.overrides.env if plan.overrides is not None else None,
            resolution.env,
        )

    async def _getLegacyRequestAuth(
        self,
        model: Model,
        plan: _ProviderAuthPlan,
    ) -> ResolvedRequestAuth:
        auth_options: dict[str, Any] = {"includeFallback": False}
        if plan.overrides is not None and plan.overrides.signal is not None:
            auth_options["signal"] = plan.overrides.signal
        api_key = await self._planApiKey(model.provider, plan)
        if plan.owner == "ambient":
            api_key = await self.authStorage.getApiKey(model.provider, auth_options)

        env = self._mergeAuthEnv(
            plan.credentialEnv,
            plan.overrides.env if plan.overrides is not None else None,
        )

        return {
            "ok": True,
            "apiKey": api_key,
            "headers": self._resolveConfiguredHeaders(
                model, api_key, env, providerEnv=env
            ),
            "baseUrl": None,
            "env": env,
        }

    async def getAuth(
        self,
        model: Model,
        overrides: AuthResolutionOverrides | None = None,
    ) -> AuthResult | None:
        """Resolve request auth with pi's native auth contract and legacy fallback."""
        plan = await self._prepareProviderAuth(model.provider, overrides)
        provider = self._authModels.getProvider(model.provider)
        resolution = await self._authModels.getAuth(model, plan.overrides)

        if resolution is None:
            provider_auth = getattr(provider, "auth", None)
            raw_config_key = self._providerRequestConfigs.get(
                model.provider, _ProviderRequestConfig()
            ).apiKey
            if (
                raw_config_key is not None
                and getattr(provider_auth, "apiKey", None) is None
                and plan.credentialType == "api_key"
            ):
                api_key = await self._planApiKey(model.provider, plan)
                if not api_key:
                    return None
                resolution = AuthResult(
                    auth=ModelAuth(apiKey=api_key),
                    source=(
                        "configured API key"
                        if plan.owner == "configured"
                        else "stored credential"
                    ),
                )
            elif provider is not None:
                return None
            else:
                legacy = await self._getLegacyRequestAuth(model, plan)
                api_key = legacy.get("apiKey")
                if not api_key:
                    return None
                return AuthResult(
                    auth=ModelAuth(
                        apiKey=api_key,
                        headers=legacy.get("headers"),
                    ),
                    env=legacy.get("env"),
                    source="compatibility",
                )

        provider_env = self._providerHeaderEnv(model.provider, plan, resolution)
        model_env = self._mergeAuthEnv(
            resolution.env,
            overrides.env if overrides is not None else None,
        )
        configured_headers = self._resolveConfiguredHeaders(
            model,
            resolution.auth.apiKey,
            model_env,
            providerEnv=provider_env,
        )
        return resolution.model_copy(
            update={
                "auth": resolution.auth.model_copy(
                    update={
                        "headers": resolve_provider_headers(
                            resolution.auth.headers, configured_headers
                        )
                        or None
                    }
                )
            }
        )

    def hasProvider(self, provider: str) -> bool:
        """Whether the runtime or static model catalog knows this provider."""
        return self._authModels.getProvider(provider) is not None or any(
            model.provider == provider for model in self.getAll()
        )

    def getProvider(self, provider: str) -> RuntimeProvider | None:
        return self._authModels.getProvider(provider)

    def getProviders(self) -> list[RuntimeProvider]:
        return list(self._authModels.getProviders())

    def getRegisteredProviderConfig(
        self, provider: str
    ) -> ProviderConfigInput | None:
        return self._registeredProviders.get(provider)

    def getRegisteredNativeProvider(
        self, provider: str
    ) -> RuntimeProvider | None:
        return (
            self._authModels.getProvider(provider)
            if provider in self._nativeProviderIds
            else None
        )

    def getRegisteredProviderIds(self) -> list[str]:
        return list(
            dict.fromkeys(
                [*self._registeredProviders, *self._nativeProviderIds]
            )
        )

    def getOAuthProviders(self) -> list[OAuthProviderInterface]:
        providers = {
            provider.id: provider
            for provider in self.authStorage.getOAuthProviders()
        }
        for provider_name, config in self._registeredProviders.items():
            oauth = config.get("oauth")
            if oauth is not None:
                providers[provider_name] = _build_oauth_provider(
                    provider_name, oauth
                )
        return list(providers.values())

    def streamSimple(
        self,
        model: Model,
        context: Context,
        options: SimpleStreamOptions | None = None,
    ) -> AssistantMessageEventStream:
        resolved_context = (
            context if isinstance(context, Context) else Context.model_validate(context)
        )
        provider = self._authModels.getProvider(model.provider)
        instance_owned = (
            model.provider in self._registeredProviders
            or model.provider in self._nativeProviderIds
        )
        if (
            instance_owned
            and provider is not None
            and self.find(model.provider, model.id) is not None
        ):
            return provider.streamSimple(model, resolved_context, options)
        api = get_api_provider(model.api)
        if api is None:
            raise RuntimeError(f"No API provider registered for api: {model.api}")
        return api.streamSimple(model, resolved_context, options)

    async def complete(
        self, model: Model, context: Context, options: Any = None
    ) -> AssistantMessage:
        return await self._authModels.complete(model, context, options)

    async def fetchDeferred(
        self, model: Model, handle: DeferredHandle, options: Any = None
    ) -> AssistantMessage:
        return await self._authModels.fetchDeferred(model, handle, options)

    async def cancelDeferred(
        self, model: Model, handle: DeferredHandle, options: Any = None
    ) -> None:
        await self._authModels.cancelDeferred(model, handle, options)

    async def checkProviderAuth(
        self,
        provider: str,
        options: AuthOperationOptions | None = None,
    ) -> AuthCheck | None:
        """Validate provider auth without refreshing OAuth credentials."""
        await self.authStorage.readLatestData(options)
        runtime_key = self.authStorage.runtimeOverrides.get(provider)
        has_stored = self.authStorage.has(provider)
        credential = self.authStorage.get(provider)
        runtime_provider = self._authModels.getProvider(provider)
        if runtime_key or has_stored:
            if has_stored and not isinstance(credential, Mapping):
                raise ValueError(f'Invalid stored credential for provider "{provider}"')
            check = await self._authModels.checkAuth(provider, options)
            if runtime_provider is not None:
                provider_auth = getattr(runtime_provider, "auth", None)
                configured_key = self._providerRequestConfigs.get(
                    provider, _ProviderRequestConfig()
                ).apiKey
                if (
                    check is None
                    and configured_key is not None
                    and getattr(provider_auth, "apiKey", None) is None
                    and (
                        bool(runtime_key)
                        or (
                            isinstance(credential, Mapping)
                            and credential.get("type") == "api_key"
                            and isinstance(credential.get("key"), str)
                            and bool(credential.get("key"))
                        )
                    )
                ):
                    return AuthCheck(source="stored credential", type="api_key")
                return check
            if runtime_key:
                return AuthCheck(source="runtime", type="api_key")
            credential_type = credential.get("type") if isinstance(credential, Mapping) else None
            if credential_type != "api_key":
                return None
            ApiKeyCredential.model_validate(credential)
            raw_key = credential.get("key")
            if not isinstance(raw_key, str):
                return None
            credential_env = self._credentialEnv(credential)
            if not (
                is_command_config_value(raw_key)
                or is_config_value_configured(raw_key, credential_env)
            ):
                return None
            return AuthCheck(source="stored credential", type="api_key")

        provider_config = self._providerRequestConfigs.get(provider)
        raw_key = provider_config.apiKey if provider_config is not None else None
        if raw_key is not None:
            if is_command_config_value(raw_key) or is_config_value_configured(raw_key):
                return AuthCheck(source="configured API key", type="api_key")
            return None

        check = await self._authModels.checkAuth(provider, options)
        if runtime_provider is not None or check is not None or not self.hasProvider(provider):
            return check
        signal = options.signal if options is not None else None
        api_key = await self.authStorage.getApiKey(
            provider,
            {
                "includeFallback": False,
                **({"signal": signal} if signal is not None else {}),
            },
        )
        return (
            AuthCheck(source="compatibility", type="api_key")
            if api_key
            else None
        )

    async def getProviderAuth(
        self,
        provider: str,
        overrides: AuthResolutionOverrides | None = None,
    ) -> AuthResult | None:
        """Resolve provider-scoped auth without model-level headers."""
        plan = await self._prepareProviderAuth(provider, overrides)
        runtime_provider = self._authModels.getProvider(provider)
        resolution = await self._authModels.getAuth(provider, plan.overrides)
        if resolution is None:
            provider_auth = getattr(runtime_provider, "auth", None)
            raw_config_key = self._providerRequestConfigs.get(
                provider, _ProviderRequestConfig()
            ).apiKey
            if (
                raw_config_key is not None
                and getattr(provider_auth, "apiKey", None) is None
                and plan.credentialType == "api_key"
            ):
                api_key = await self._planApiKey(provider, plan)
                if not api_key:
                    return None
                resolution = AuthResult(
                    auth=ModelAuth(apiKey=api_key),
                    source=(
                        "configured API key"
                        if plan.owner == "configured"
                        else "stored credential"
                    ),
                )
            elif runtime_provider is not None:
                return None
            else:
                auth_options: dict[str, Any] = {"includeFallback": False}
                if plan.overrides is not None and plan.overrides.signal is not None:
                    auth_options["signal"] = plan.overrides.signal
                api_key = await self._planApiKey(provider, plan)
                if plan.owner == "ambient":
                    api_key = await self.authStorage.getApiKey(
                        provider, auth_options
                    )
                if not api_key:
                    return None
                env = self._mergeAuthEnv(
                    plan.credentialEnv,
                    plan.overrides.env if plan.overrides is not None else None,
                )
                resolution = AuthResult(
                    auth=ModelAuth(apiKey=api_key),
                    env=env,
                    source="compatibility",
                )

        configured_headers = self._resolveProviderConfiguredHeaders(
            provider,
            resolution.auth.apiKey,
            self._providerHeaderEnv(provider, plan, resolution),
        )
        return resolution.model_copy(
            update={
                "auth": resolution.auth.model_copy(
                    update={
                        "headers": resolve_provider_headers(
                            resolution.auth.headers, configured_headers
                        )
                        or None
                    }
                )
            }
        )

    def getNativeProviders(self) -> list[RuntimeProvider]:
        return [
            provider
            for provider_id in self._nativeProviderIds
            if (provider := self._authModels.getProvider(provider_id)) is not None
        ]

    async def login(
        self, provider: str, type: AuthType, interaction: AuthInteraction
    ) -> CredentialValue:
        if (
            provider not in self._nativeProviderIds
            and provider not in self._registeredProviders
            and provider not in self._radiusProviders
        ):
            raise ValueError(f"Provider {provider} does not use native authentication")
        return await self._authModels.login(provider, type, interaction)

    async def getApiKeyAndHeaders(self, model: Model) -> ResolvedRequestAuth:
        try:
            resolution = await self.getAuth(model)
            if resolution is None:
                return {
                    "ok": True,
                    "apiKey": None,
                    "headers": self._resolveConfiguredHeaders(model),
                    "baseUrl": None,
                    "env": None,
                }
            return {
                "ok": True,
                "apiKey": resolution.auth.apiKey,
                "headers": resolve_provider_headers(resolution.auth.headers) or None,
                "baseUrl": resolution.auth.baseUrl,
                "env": resolution.env,
            }
        except Exception as error:  # noqa: BLE001
            message = str(error)
            if message == "authHeader requires a resolved API key":
                message = f'No API key found for "{model.provider}"'
            return {"ok": False, "error": message}

    def getProviderAuthStatus(self, provider: str) -> AuthStatus:
        auth_status = self.authStorage.getAuthStatus(provider)
        if auth_status.source:
            return auth_status

        native_check = self._nativeAuthChecks.get(provider)
        if native_check is not None:
            return AuthStatus(
                configured=True,
                source="environment",
                label=native_check.source,
            )

        provider_api_key = self._providerRequestConfigs.get(provider, _ProviderRequestConfig()).apiKey
        if not provider_api_key:
            return auth_status
        # pi provider-composer.ts:558-571 `configuredRequestAuthStatus`: the status has to be
        # read with the same template grammar `resolve_config_value` uses, or the two disagree.
        # A bare name is a literal key, `$NAME`/`${NAME}` is a reference, and a reference whose
        # variable is unset is *not* configured -- reporting it as configured is what turns a
        # typo'd models.json into an upstream 401 instead of a config error.
        if is_command_config_value(provider_api_key):
            return AuthStatus(configured=True, source="models_json_command")
        env_var_names = get_config_value_env_var_names(provider_api_key)
        if env_var_names:
            if not is_config_value_configured(provider_api_key):
                return AuthStatus(configured=False)
            return AuthStatus(configured=True, source="environment", label=", ".join(env_var_names))
        return AuthStatus(configured=True, source="models_json_key")

    def getProviderDisplayName(self, provider: str) -> str:
        native_provider = (
            self._authModels.getProvider(provider)
            if provider in self._nativeProviderIds
            else None
        )
        registered_provider = self._registeredProviders.get(provider) or {}
        oauth_provider = next((item for item in self.authStorage.getOAuthProviders() if item.id == provider), None)
        oauth_config = registered_provider.get("oauth") if isinstance(registered_provider, dict) else None
        # pi provider-composer.ts:478: extension name, then the models.json block's
        # `name`, then the base (native/built-in) provider's, then the extension OAuth name.
        return (
            registered_provider.get("name")
            or (self._modelsJsonProviders.get(provider) or {}).get("name")
            or (native_provider.name if native_provider is not None else None)
            or _oauth_name(oauth_config)
            or (oauth_provider.name if oauth_provider else None)
            or BUILT_IN_PROVIDER_DISPLAY_NAMES.get(provider)
            or provider
        )

    async def getApiKeyForProvider(self, provider: str) -> str | None:
        if (
            provider in self._nativeProviderIds
            or provider in self._registeredProviders
        ):
            resolution = await self.getProviderAuth(provider)
            return resolution.auth.apiKey if resolution is not None else None
        api_key = await self.authStorage.getApiKey(provider, {"includeFallback": False})
        if api_key is not None:
            return api_key
        credential = self.authStorage.get(provider)
        if (
            provider not in self.authStorage.runtimeOverrides
            and isinstance(credential, Mapping)
            and credential.get("type") == "api_key"
            and credential.get("key") is None
        ):
            return None
        provider_api_key = self._providerRequestConfigs.get(provider, _ProviderRequestConfig()).apiKey
        return resolveConfigValueUncached(provider_api_key) if provider_api_key else None

    def isUsingOAuth(self, model: Model) -> bool:
        credential = self.authStorage.get(model.provider)
        return isinstance(credential, dict) and credential.get("type") == "oauth"

    def isUsingSubscription(self, model: Model) -> bool:
        if not self.isUsingOAuth(model):
            return False

        registered = self._registeredProviders.get(model.provider)
        extension_oauth = registered.get("oauth") if registered is not None else None
        if extension_oauth is not None:
            marker = (
                extension_oauth.get("isSubscription")
                if isinstance(extension_oauth, Mapping)
                else getattr(extension_oauth, "isSubscription", None)
            )
            return marker is True

        provider = self._authModels.getProvider(model.provider)
        auth = getattr(provider, "auth", None)
        oauth = getattr(auth, "oauth", None)
        return getattr(oauth, "isSubscription", None) is True

    def _recomposeLegacyProvider(
        self,
        providerName: str,
        modelsConfig: Mapping[str, Any] | None,
        extension: ProviderConfigInput,
    ) -> None:
        has_previous = providerName in self._legacyPreviousProviders
        previous = (
            self._legacyPreviousProviders[providerName]
            if has_previous
            else self._authModels.getProvider(providerName)
        )
        candidate = _LegacyRuntimeProvider(
            self, providerName, previous, modelsConfig, extension
        )
        if not has_previous:
            self._legacyPreviousProviders[providerName] = previous
        self._authModels.setProvider(candidate)

    def _restoreLegacyProvider(self, providerName: str) -> bool:
        if providerName not in self._legacyPreviousProviders:
            return False
        previous = self._legacyPreviousProviders.pop(providerName)
        if previous is None:
            self._authModels.deleteProvider(providerName)
        else:
            self._authModels.setProvider(previous)
        return True

    def registerProvider(
        self,
        providerOrName: RuntimeProvider | str,
        config: ProviderConfigInput | None = None,
    ) -> None:
        if isinstance(providerOrName, str):
            if config is None:
                raise TypeError("config is required for legacy provider registration")
            self._validateExtensionProviderConfig(providerOrName, config)
            old_config = self._registeredProviders.get(providerOrName)
            old_config = dict(old_config) if old_config is not None else None
            had_native = providerOrName in self._nativeProviderIds
            native_provider = (
                self._authModels.getProvider(providerOrName) if had_native else None
            )
            native_previous = (
                self._nativePreviousProviders[providerOrName]
                if had_native
                else None
            )
            had_native_check = providerOrName in self._nativeAuthChecks
            native_check = self._nativeAuthChecks.get(providerOrName)
            self._removeNativeProvider(providerOrName)
            self._upsertRegisteredProvider(providerOrName, config)
            try:
                self._reloadLegacy()
            except Exception as registration_error:
                if old_config is None:
                    self._registeredProviders.pop(providerOrName, None)
                else:
                    self._registeredProviders[providerOrName] = old_config
                if had_native:
                    self._restoreNativeProvider(
                        providerOrName,
                        native_provider,
                        native_previous,
                        native_check,
                        had_native_check,
                    )
                try:
                    self._reloadLegacy()
                except Exception as rollback_error:
                    raise RuntimeError(
                        f'Provider "{providerOrName}" registration failed '
                        f"({registration_error}) and rollback failed"
                    ) from rollback_error
                self._scheduleOfflineRefresh()
                raise
            self._scheduleOfflineRefresh()
            return

        if config is not None:
            raise TypeError("native provider registration takes one provider object")
        self.registerNativeProvider(providerOrName)

    def registerNativeProvider(self, provider: RuntimeProvider) -> None:
        provider_id = getattr(provider, "id", None)
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("Native provider id must be a non-empty string")
        for field_name in ("name", "auth", "getModels", "stream", "streamSimple"):
            if getattr(provider, field_name, None) is None:
                raise ValueError(
                    f'Native provider "{provider_id}" is missing {field_name}'
                )
        old_config = self._registeredProviders.pop(provider_id, None)
        if old_config is not None:
            try:
                self._reloadLegacy()
            except Exception as registration_error:
                self._registeredProviders[provider_id] = old_config
                try:
                    self._reloadLegacy()
                except Exception as rollback_error:
                    raise RuntimeError(
                        f'Provider "{provider_id}" registration failed '
                        f"({registration_error}) and rollback failed"
                    ) from rollback_error
                self._scheduleOfflineRefresh()
                raise
        if provider_id not in self._nativeProviderIds:
            self._nativePreviousProviders[provider_id] = self._authModels.getProvider(
                provider_id
            )
        self._nativeProviderIds[provider_id] = None
        self._nativeAuthChecks.pop(provider_id, None)
        self._authModels.setProvider(provider)
        self._scheduleOfflineRefresh()

    def unregisterProvider(self, providerName: str) -> None:
        had_native = providerName in self._nativeProviderIds
        native_provider = (
            self._authModels.getProvider(providerName) if had_native else None
        )
        native_previous = (
            self._nativePreviousProviders[providerName] if had_native else None
        )
        had_native_check = providerName in self._nativeAuthChecks
        native_check = self._nativeAuthChecks.get(providerName)
        old_config = self._registeredProviders.pop(providerName, None)
        removed = self._removeNativeProvider(providerName) or old_config is not None
        if removed:
            try:
                self._reloadLegacy()
            except Exception as registration_error:
                if old_config is not None:
                    self._registeredProviders[providerName] = old_config
                if had_native:
                    self._restoreNativeProvider(
                        providerName,
                        native_provider,
                        native_previous,
                        native_check,
                        had_native_check,
                    )
                try:
                    self._reloadLegacy()
                except Exception as rollback_error:
                    raise RuntimeError(
                        f'Provider "{providerName}" unregistration failed '
                        f"({registration_error}) and rollback failed"
                    ) from rollback_error
                self._scheduleOfflineRefresh()
                raise
        self._scheduleOfflineRefresh()

    def _removeNativeProvider(self, providerName: str) -> bool:
        if providerName not in self._nativeProviderIds:
            return False
        self._nativeProviderIds.pop(providerName)
        self._nativeAuthChecks.pop(providerName, None)
        previous = self._nativePreviousProviders.pop(providerName)
        if previous is None:
            self._authModels.deleteProvider(providerName)
        else:
            self._authModels.setProvider(previous)
        return True

    def _restoreNativeProvider(
        self,
        providerName: str,
        provider: RuntimeProvider | None,
        previous: RuntimeProvider | None,
        authCheck: AuthCheck | None,
        hadAuthCheck: bool,
    ) -> None:
        self._nativeProviderIds[providerName] = None
        self._nativePreviousProviders[providerName] = previous
        if provider is None:
            self._authModels.deleteProvider(providerName)
        else:
            self._authModels.setProvider(provider)
        if hadAuthCheck and authCheck is not None:
            self._nativeAuthChecks[providerName] = authCheck
        else:
            self._nativeAuthChecks.pop(providerName, None)

    def _upsertRegisteredProvider(self, providerName: str, config: ProviderConfigInput) -> None:
        existing = self._registeredProviders.get(providerName)
        if existing is None:
            self._registeredProviders[providerName] = config
            return
        for key, value in config.items():
            if value is not None:
                existing[key] = value

    def _validateExtensionProviderConfig(self, providerName: str, config: ProviderConfigInput) -> None:
        if config.get("streamSimple") and not config.get("api"):
            raise ValueError(f'Provider {providerName}: "api" is required when registering streamSimple.')

        models = config.get("models") or []
        if not models:
            return
        if not config.get("baseUrl"):
            raise ValueError(f'Provider {providerName}: "baseUrl" is required when defining models.')
        if not config.get("apiKey") and not config.get("oauth"):
            raise ValueError(f'Provider {providerName}: "apiKey" or "oauth" is required when defining models.')

        for model_def in models:
            if not (model_def.get("api") or config.get("api")):
                raise ValueError(f'Provider {providerName}, model {model_def["id"]}: no "api" specified.')

    def _applyProviderConfig(self, providerName: str, config: ProviderConfigInput) -> None:
        oauth = config.get("oauth")
        self._storeProviderRequestConfig(providerName, config)

        models = config.get("models")
        if models is not None:
            self._models = [model for model in self._models if model.provider != providerName]
            for model_def in models:
                api = model_def.get("api") or config.get("api")
                self._storeModelHeaders(providerName, model_def["id"], model_def.get("headers"))
                cost = model_def.get("cost")
                self._models.append(
                    Model.model_construct(
                        id=model_def["id"],
                        name=model_def.get("name"),
                        api=api,
                        provider=providerName,
                        baseUrl=_coalesce(model_def.get("baseUrl"), config["baseUrl"]),
                        reasoning=model_def.get("reasoning"),
                        thinkingLevelMap=model_def.get("thinkingLevelMap"),
                        input=model_def.get("input"),
                        cost=ModelCost.model_validate(cost) if cost is not None else None,
                        contextWindow=model_def.get("contextWindow"),
                        maxTokens=model_def.get("maxTokens"),
                        headers=None,
                        compat=_coerce_compat(model_def.get("compat")),
                    )
                )

            modify_models = oauth.get("modifyModels") if isinstance(oauth, Mapping) else getattr(oauth, "modifyModels", None)
            if oauth and modify_models:
                credential = self.authStorage.get(providerName)
                if isinstance(credential, dict) and credential.get("type") == "oauth" and modify_models:
                    self._models = modify_models(
                        self._models,
                        OAuthCredentials.model_validate({key: value for key, value in credential.items() if key != "type"}),
                    )

            # This provider's models were just replaced wholesale, so the models.json
            # modelOverrides layer has to be re-applied on top of them (pi
            # provider-composer.ts:431-446 re-runs it after extension replacement).
            self._models = self._applyModelOverrides(self._models, providerName)
        elif config.get("baseUrl") or config.get("headers"):
            self._models = [
                model.model_copy(update={"baseUrl": _coalesce(config.get("baseUrl"), model.baseUrl)})
                if model.provider == providerName
                else model
                for model in self._models
            ]


__all__ = [
    "ModelRegistry",
    "ProviderConfigInput",
    "ResolvedRequestAuth",
]
