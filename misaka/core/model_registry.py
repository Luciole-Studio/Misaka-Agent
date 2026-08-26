"""Model registry and custom-model loading for coding-agent providers."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, NotRequired, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from misaka.ai.api_registry import ApiProvider, register_api_provider
from misaka.ai.models import get_models, get_providers
from misaka.ai.oauth import (
    OAuthProviderInterface,
    registerOAuthProvider,
    resetOAuthProviders,
)
from misaka.ai.providers.register_builtins import resetApiProviders
from misaka.ai.types import (
    AnthropicMessagesCompat,
    Api,
    AssistantMessageEventStream,
    Context,
    Model,
    ModelCompat,
    ModelCost,
    OpenAICompletionsCompat,
    OpenAIResponsesCompat,
    SimpleStreamOptions,
)
from misaka.ai.utils.oauth.types import OAuthCredentials
from misaka.config import get_agent_dir

logger = logging.getLogger(__name__)
from misaka.core.provider_display_names import BUILT_IN_PROVIDER_DISPLAY_NAMES
from misaka.core.resolve_config_value import (
    clearConfigValueCache,
    resolveConfigValueOrThrow,
    resolveConfigValueUncached,
    resolveHeadersOrThrow,
)
from misaka.utils.paths import normalize_path

from .auth_storage import AuthStatus, AuthStorage

type _ProviderCompat = dict[str, Any] | OpenAICompletionsCompat | OpenAIResponsesCompat | AnthropicMessagesCompat


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _PercentileCutoffsSchema(_ConfigModel):
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    p99: float | None = None


class _OpenRouterRoutingSortSchema(_ConfigModel):
    by: str | None = None
    partition: str | None = None


class _OpenRouterRoutingMaxPriceSchema(_ConfigModel):
    prompt: float | str | None = None
    completion: float | str | None = None
    image: float | str | None = None
    audio: float | str | None = None
    request: float | str | None = None


class _OpenRouterRoutingSchema(_ConfigModel):
    allow_fallbacks: bool | None = None
    require_parameters: bool | None = None
    data_collection: Literal["deny", "allow"] | None = None
    zdr: bool | None = None
    enforce_distillable_text: bool | None = None
    order: list[str] | None = None
    only: list[str] | None = None
    ignore: list[str] | None = None
    quantizations: list[str] | None = None
    sort: str | _OpenRouterRoutingSortSchema | None = None
    max_price: _OpenRouterRoutingMaxPriceSchema | None = None
    preferred_min_throughput: float | _PercentileCutoffsSchema | None = None
    preferred_max_latency: float | _PercentileCutoffsSchema | None = None


class _VercelGatewayRoutingSchema(_ConfigModel):
    only: list[str] | None = None
    order: list[str] | None = None


class _ThinkingLevelMapSchema(_ConfigModel):
    off: str | None = None
    minimal: str | None = None
    low: str | None = None
    medium: str | None = None
    high: str | None = None
    xhigh: str | None = None


class _ModelCostSchema(_ConfigModel):
    input: float
    output: float
    cacheRead: float
    cacheWrite: float


class _PartialModelCostSchema(_ConfigModel):
    input: float | None = None
    output: float | None = None
    cacheRead: float | None = None
    cacheWrite: float | None = None


class _OpenAICompletionsCompatSchema(_ConfigModel):
    supportsStore: bool | None = None
    supportsDeveloperRole: bool | None = None
    supportsReasoningEffort: bool | None = None
    supportsUsageInStreaming: bool | None = None
    maxTokensField: Literal["max_completion_tokens", "max_tokens"] | None = None
    requiresToolResultName: bool | None = None
    requiresAssistantAfterToolResult: bool | None = None
    requiresThinkingAsText: bool | None = None
    requiresReasoningContentOnAssistantMessages: bool | None = None
    thinkingFormat: Literal[
        "openai",
        "openrouter",
        "together",
        "deepseek",
        "zai",
        "qwen",
        "qwen-chat-template",
    ] | None = None
    cacheControlFormat: Literal["anthropic"] | None = None
    openRouterRouting: _OpenRouterRoutingSchema | None = None
    vercelGatewayRouting: _VercelGatewayRoutingSchema | None = None
    supportsStrictMode: bool | None = None
    supportsLongCacheRetention: bool | None = None


class _OpenAIResponsesCompatSchema(_ConfigModel):
    sendSessionIdHeader: bool | None = None
    supportsLongCacheRetention: bool | None = None


class _AnthropicMessagesCompatSchema(_ConfigModel):
    supportsEagerToolInputStreaming: bool | None = None
    supportsLongCacheRetention: bool | None = None
    sendSessionAffinityHeaders: bool | None = None
    supportsCacheControlOnTools: bool | None = None
    forceAdaptiveThinking: bool | None = None


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
    headers: dict[str, str] | None = None
    compat: dict[str, Any] | None = None


class _ModelOverrideSchema(_ConfigModel):
    name: str | None = Field(default=None, min_length=1)
    reasoning: bool | None = None
    thinkingLevelMap: _ThinkingLevelMapSchema | None = None
    input: list[Literal["text", "image"]] | None = None
    cost: _PartialModelCostSchema | None = None
    contextWindow: float | None = None
    maxTokens: float | None = None
    headers: dict[str, str] | None = None
    compat: dict[str, Any] | None = None


class _ProviderConfigSchema(_ConfigModel):
    name: str | None = Field(default=None, min_length=1)
    baseUrl: str | None = Field(default=None, min_length=1)
    apiKey: str | None = Field(default=None, min_length=1)
    api: str | None = Field(default=None, min_length=1)
    headers: dict[str, str] | None = None
    compat: dict[str, Any] | None = None
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
class _CustomModelsResult:
    models: list[Model]
    overrides: dict[str, _ProviderOverride]
    modelOverrides: dict[str, dict[str, dict[str, Any]]]
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


class _ResolvedRequestAuthError(TypedDict):
    ok: Literal[False]
    error: str


type ResolvedRequestAuth = _ResolvedRequestAuthOk | _ResolvedRequestAuthError


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
    if "sendSessionIdHeader" in compat_dict:
        return OpenAIResponsesCompat.model_validate(compat_dict)
    if any(
        key in compat_dict
        for key in (
            "supportsEagerToolInputStreaming",
            "supportsCacheControlOnTools",
            "forceAdaptiveThinking",
            "sendSessionAffinityHeaders",
        )
    ):
        return AnthropicMessagesCompat.model_validate(compat_dict)
    return OpenAICompletionsCompat.model_validate(compat_dict)


def _merge_compat(baseCompat: _ProviderCompat | None, overrideCompat: _ProviderCompat | None) -> _ProviderCompat | None:
    if overrideCompat is None:
        return baseCompat
    base = _dump_compat(baseCompat)
    override = _dump_compat(overrideCompat)
    merged = {**base, **override}
    if base.get("openRouterRouting") or override.get("openRouterRouting"):
        merged["openRouterRouting"] = {
            **(base.get("openRouterRouting") or {}),
            **(override.get("openRouterRouting") or {}),
        }
    if base.get("vercelGatewayRouting") or override.get("vercelGatewayRouting"):
        merged["vercelGatewayRouting"] = {
            **(base.get("vercelGatewayRouting") or {}),
            **(override.get("vercelGatewayRouting") or {}),
        }
    return _coerce_compat(merged)


def _apply_model_override(model: Model, override: dict[str, Any]) -> Model:
    update: dict[str, Any] = {}
    for key in ("name", "reasoning", "input", "contextWindow", "maxTokens"):
        if key in override:
            update[key] = override[key]
    if "thinkingLevelMap" in override:
        update["thinkingLevelMap"] = {**(model.thinkingLevelMap or {}), **(override["thinkingLevelMap"] or {})}
    if "cost" in override and isinstance(override["cost"], dict):
        update["cost"] = ModelCost.model_validate(
            {
                "input": override["cost"].get("input", model.cost.input),
                "output": override["cost"].get("output", model.cost.output),
                "cacheRead": override["cost"].get("cacheRead", model.cost.cacheRead),
                "cacheWrite": override["cost"].get("cacheWrite", model.cost.cacheWrite),
            }
        )
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


def _validate_compat_schema(value: Any, prefix: tuple[Any, ...]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{_format_validation_path(prefix)}: Input should be a valid dictionary"]
    schema: type[BaseModel]
    if any(
        key in value
        for key in (
            "supportsEagerToolInputStreaming",
            "supportsCacheControlOnTools",
            "forceAdaptiveThinking",
            "sendSessionAffinityHeaders",
        )
    ):
        schema = _AnthropicMessagesCompatSchema
    elif "sendSessionIdHeader" in value:
        schema = _OpenAIResponsesCompatSchema
    else:
        schema = _OpenAICompletionsCompatSchema
    try:
        schema.model_validate(value)
    except ValidationError as error:
        return _validation_messages(error, prefix)
    return []


def _validate_models_config(parsed: Any) -> list[str]:
    try:
        _ModelsConfigSchema.model_validate(parsed)
    except ValidationError as error:
        return _validation_messages(error)

    errors: list[str] = []
    providers = parsed.get("providers") if isinstance(parsed, dict) else None
    if not isinstance(providers, dict):
        return errors

    for provider_name, provider_config in providers.items():
        if not isinstance(provider_config, dict):
            continue
        errors.extend(_validate_compat_schema(provider_config.get("compat"), ("providers", provider_name, "compat")))

        models = provider_config.get("models")
        if isinstance(models, list):
            for index, model_def in enumerate(models):
                if isinstance(model_def, dict):
                    errors.extend(
                        _validate_compat_schema(model_def.get("compat"), ("providers", provider_name, "models", index, "compat"))
                    )

        model_overrides = provider_config.get("modelOverrides")
        if isinstance(model_overrides, dict):
            for model_id, model_override in model_overrides.items():
                if isinstance(model_override, dict):
                    errors.extend(
                        _validate_compat_schema(
                            model_override.get("compat"),
                            ("providers", provider_name, "modelOverrides", model_id, "compat"),
                        )
                    )

    return errors


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
    return _CustomModelsResult(models=[], overrides={}, modelOverrides={}, error=error)


def _coalesce[T](value: T | None, fallback: T) -> T:
    return fallback if value is None else value


def _build_oauth_provider(provider_name: str, oauth: OAuthProviderInterface | Mapping[str, Any]) -> OAuthProviderInterface:
    if isinstance(oauth, Mapping):
        name = oauth["name"]
        uses_callback_server = oauth.get("usesCallbackServer")
        modify_models = oauth.get("modifyModels")
        login = oauth["login"]
        refresh_token = oauth["refreshToken"]
        get_api_key = oauth["getApiKey"]
    else:
        name = oauth.name
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


clearApiKeyCache = clearConfigValueCache


class ModelRegistry:
    def __init__(self, authStorage: AuthStorage, modelsJsonPath: str | None):
        self._models: list[Model] = []
        self._providerRequestConfigs: dict[str, _ProviderRequestConfig] = {}
        self._modelRequestHeaders: dict[str, dict[str, str]] = {}
        self._registeredProviders: dict[str, ProviderConfigInput] = {}
        self._loadError: str | None = None
        self.authStorage = authStorage
        self._modelsJsonPath = normalize_path(modelsJsonPath) if modelsJsonPath else None
        self._loadModels()

    @classmethod
    def create(cls, authStorage: AuthStorage, modelsJsonPath: str | None = None) -> ModelRegistry:
        return cls(authStorage, modelsJsonPath or os.path.join(get_agent_dir(), "models.json"))

    @classmethod
    def inMemory(cls, authStorage: AuthStorage) -> ModelRegistry:
        return cls(authStorage, None)

    def refresh(self) -> None:
        self._providerRequestConfigs.clear()
        self._modelRequestHeaders.clear()
        self._loadError = None
        resetApiProviders()
        resetOAuthProviders()
        self._loadModels()
        for provider_name, config in self._registeredProviders.items():
            self._applyProviderConfig(provider_name, config)

    def getError(self) -> str | None:
        return self._loadError

    def _loadModels(self) -> None:
        custom = self._loadCustomModels(self._modelsJsonPath) if self._modelsJsonPath else _empty_custom_models_result()
        if custom.error:
            self._loadError = custom.error

        built_in_models = self._loadBuiltInModels(custom.overrides, custom.modelOverrides)
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

        self._models = combined

    def _loadBuiltInModels(
        self,
        overrides: dict[str, _ProviderOverride],
        modelOverrides: dict[str, dict[str, dict[str, Any]]],
    ) -> list[Model]:
        result: list[Model] = []
        for provider in get_providers():
            models = list(get_models(provider))
            provider_override = overrides.get(provider)
            per_model_overrides = modelOverrides.get(provider, {})
            for model in models:
                updated = model
                if provider_override is not None:
                    updated = updated.model_copy(
                        update={
                            "baseUrl": _coalesce(provider_override.baseUrl, updated.baseUrl),
                            "compat": _merge_compat(updated.compat, provider_override.compat),
                        }
                    )
                model_override = per_model_overrides.get(model.id)
                if model_override is not None:
                    updated = _apply_model_override(updated, model_override)
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

            providers = parsed["providers"]
            self._validateConfig(providers)
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

            return _CustomModelsResult(
                models=self._parseModels(providers),
                overrides=overrides,
                modelOverrides=model_overrides,
                error=None,
            )
        except json.JSONDecodeError as error:
            return _empty_custom_models_result(f"Failed to parse models.json: {error}\n\nFile: {modelsJsonPath}")
        except Exception as error:  # noqa: BLE001
            return _empty_custom_models_result(f"Failed to load models.json: {error}\n\nFile: {modelsJsonPath}")

    def _validateConfig(self, providers: dict[str, Any]) -> None:
        built_in_providers = set(get_providers())
        for provider_name, provider_config in providers.items():
            is_built_in = provider_name in built_in_providers
            has_provider_api = bool(provider_config.get("api"))
            models = provider_config.get("models") or []
            has_model_overrides = bool(provider_config.get("modelOverrides")) and len(provider_config["modelOverrides"]) > 0

            if len(models) == 0:
                if (
                    provider_config.get("baseUrl") is None
                    and provider_config.get("headers") is None
                    and provider_config.get("compat") is None
                    and not has_model_overrides
                ):
                    raise ValueError(
                        f'Provider {provider_name}: must specify "baseUrl", "headers", "compat", "modelOverrides", or "models".'
                    )
            elif not is_built_in:
                if not provider_config.get("baseUrl"):
                    raise ValueError(f'Provider {provider_name}: "baseUrl" is required when defining custom models.')
                if not provider_config.get("apiKey"):
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
                        headers=None,
                        compat=compat,
                    )
                )
        return models

    def getAll(self) -> list[Model]:
        return self._models

    def getAvailable(self) -> list[Model]:
        return [model for model in self._models if self.hasConfiguredAuth(model)]

    def find(self, provider: str, modelId: str) -> Model | None:
        return next((model for model in self._models if model.provider == provider and model.id == modelId), None)

    def hasConfiguredAuth(self, model: Model) -> bool:
        return self.authStorage.hasAuth(model.provider) or (
            self._providerRequestConfigs.get(model.provider, _ProviderRequestConfig()).apiKey is not None
        )

    @staticmethod
    def _getModelRequestKey(provider: str, modelId: str) -> str:
        return f"{provider}:{modelId}"

    def _storeProviderRequestConfig(self, providerName: str, config: Mapping[str, Any]) -> None:
        api_key = config.get("apiKey")
        headers = config.get("headers")
        auth_header = config.get("authHeader")
        if not api_key and not headers and not auth_header:
            return

        self._providerRequestConfigs[providerName] = _ProviderRequestConfig(
            apiKey=cast(str | None, api_key),
            headers=cast(dict[str, str] | None, headers),
            authHeader=cast(bool | None, auth_header),
        )

    def _storeModelHeaders(self, providerName: str, modelId: str, headers: dict[str, str] | None) -> None:
        key = self._getModelRequestKey(providerName, modelId)
        if not headers:
            self._modelRequestHeaders.pop(key, None)
            return
        self._modelRequestHeaders[key] = headers

    async def getApiKeyAndHeaders(self, model: Model) -> ResolvedRequestAuth:
        try:
            provider_config = self._providerRequestConfigs.get(model.provider)
            api_key_from_auth_storage = await self.authStorage.getApiKey(model.provider, {"includeFallback": False})
            api_key = api_key_from_auth_storage
            if api_key is None and provider_config and provider_config.apiKey:
                api_key = resolveConfigValueOrThrow(provider_config.apiKey, f'API key for provider "{model.provider}"')

            provider_headers = resolveHeadersOrThrow(
                provider_config.headers if provider_config else None,
                f'provider "{model.provider}"',
            )
            model_headers = resolveHeadersOrThrow(
                self._modelRequestHeaders.get(self._getModelRequestKey(model.provider, model.id)),
                f'model "{model.provider}/{model.id}"',
            )

            headers: dict[str, str] | None = None
            if model.headers or provider_headers or model_headers:
                headers = {**(model.headers or {}), **(provider_headers or {}), **(model_headers or {})}

            if provider_config and provider_config.authHeader:
                if not api_key:
                    return {"ok": False, "error": f'No API key found for "{model.provider}"'}
                headers = {**(headers or {}), "Authorization": f"Bearer {api_key}"}

            return {
                "ok": True,
                "apiKey": api_key,
                "headers": headers if headers and len(headers) > 0 else None,
            }
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "error": str(error)}

    def getProviderAuthStatus(self, provider: str) -> AuthStatus:
        auth_status = self.authStorage.getAuthStatus(provider)
        if auth_status.source:
            return auth_status

        provider_api_key = self._providerRequestConfigs.get(provider, _ProviderRequestConfig()).apiKey
        if not provider_api_key:
            return auth_status
        if provider_api_key.startswith("!"):
            return AuthStatus(configured=True, source="models_json_command")
        if os.environ.get(provider_api_key):
            return AuthStatus(configured=True, source="environment", label=provider_api_key)
        return AuthStatus(configured=True, source="models_json_key")

    def getProviderDisplayName(self, provider: str) -> str:
        registered_provider = self._registeredProviders.get(provider) or {}
        oauth_provider = next((item for item in self.authStorage.getOAuthProviders() if item.id == provider), None)
        oauth_config = registered_provider.get("oauth") if isinstance(registered_provider, dict) else None
        return (
            registered_provider.get("name")
            or _oauth_name(oauth_config)
            or (oauth_provider.name if oauth_provider else None)
            or BUILT_IN_PROVIDER_DISPLAY_NAMES.get(provider)
            or provider
        )

    async def getApiKeyForProvider(self, provider: str) -> str | None:
        api_key = await self.authStorage.getApiKey(provider, {"includeFallback": False})
        if api_key is not None:
            return api_key
        provider_api_key = self._providerRequestConfigs.get(provider, _ProviderRequestConfig()).apiKey
        return resolveConfigValueUncached(provider_api_key) if provider_api_key else None

    def isUsingOAuth(self, model: Model) -> bool:
        credential = self.authStorage.get(model.provider)
        return isinstance(credential, dict) and credential.get("type") == "oauth"

    def registerProvider(self, providerName: str, config: ProviderConfigInput) -> None:
        self._validateProviderConfig(providerName, config)
        self._applyProviderConfig(providerName, config)
        self._upsertRegisteredProvider(providerName, config)

    def unregisterProvider(self, providerName: str) -> None:
        if providerName not in self._registeredProviders:
            return
        self._registeredProviders.pop(providerName, None)
        self.refresh()

    def _upsertRegisteredProvider(self, providerName: str, config: ProviderConfigInput) -> None:
        existing = self._registeredProviders.get(providerName)
        if existing is None:
            self._registeredProviders[providerName] = config
            return
        for key, value in config.items():
            if value is not None:
                existing[key] = value

    def _validateProviderConfig(self, providerName: str, config: ProviderConfigInput) -> None:
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
        if oauth:
            registerOAuthProvider(_build_oauth_provider(providerName, oauth))

        stream_simple = config.get("streamSimple")
        if stream_simple:
            register_api_provider(
                ApiProvider(
                    api=config["api"],
                    stream=lambda model, context, options=None: stream_simple(
                        model,
                        context,
                        cast(SimpleStreamOptions | None, options),
                    ),
                    streamSimple=stream_simple,
                ),
                f"provider:{providerName}",
            )

        self._storeProviderRequestConfig(providerName, config)

        models = config.get("models") or []
        if models:
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
    "clearApiKeyCache",
]
