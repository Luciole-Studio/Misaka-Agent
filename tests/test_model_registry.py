"""pi's ``model-registry.test.ts`` (``misaka/core/model_registry.py`` over ``ModelRuntime``)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from misaka.core.auth_storage import AuthStorage
from misaka.core.model_registry import ModelRegistry, clearApiKeyCache
from tests.model_runtime_test_utils import createModelRegistry


def _plain(value: Any) -> Any:
    """A pydantic value as the plain object upstream compares against."""
    if isinstance(value, BaseModel):
        return value.model_dump(exclude_none=True)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


@pytest.fixture(autouse=True)
def _clear_api_key_cache():
    yield
    clearApiKeyCache()


@pytest.fixture
def authStorage() -> AuthStorage:
    return AuthStorage.inMemory()


@pytest.fixture
def modelsJsonPath(tmp_path) -> str:
    return str(tmp_path / "models.json")


def providerConfig(baseUrl: str, models: list[dict], api: str = "anthropic-messages") -> dict:
    """A minimal provider config."""
    return {
        "baseUrl": baseUrl,
        "apiKey": "test-key",
        "api": api,
        "models": [
            {
                "id": model["id"],
                "name": model.get("name", model["id"]),
                "reasoning": False,
                "input": ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 100000,
                "maxTokens": 8000,
            }
            for model in models
        ],
    }


def writeRawModelsJson(path: str, providers: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"providers": providers}, handle)


writeModelsJson = writeRawModelsJson


def getModelsForProvider(registry: ModelRegistry, provider: str) -> list:
    return [model for model in registry.getAll() if model.provider == provider]


def overrideConfig(baseUrl: str, headers: dict | None = None) -> dict:
    """A baseUrl-only override (no custom models)."""
    return {"baseUrl": baseUrl, **({"headers": headers} if headers else {})}


def _find(models: list, id: str):
    return next((model for model in models if model.id == id), None)


# -- baseUrl override (no custom models) -----------------------------------------------------


async def test_overriding_base_url_keeps_all_built_in_models(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"anthropic": overrideConfig("https://my-proxy.example.com/v1")})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    anthropicModels = getModelsForProvider(registry, "anthropic")

    assert len(anthropicModels) > 1
    assert any("claude" in model.id for model in anthropicModels)


async def test_overriding_base_url_changes_url_on_all_built_in_models(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"anthropic": overrideConfig("https://my-proxy.example.com/v1")})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    for model in getModelsForProvider(registry, "anthropic"):
        assert model.baseUrl == "https://my-proxy.example.com/v1"


async def test_overriding_headers_resolves_at_request_time(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {
        "anthropic": overrideConfig("https://my-proxy.example.com/v1", {"X-Custom-Header": "custom-value"}),
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    for model in getModelsForProvider(registry, "anthropic"):
        auth = await registry.getApiKeyAndHeaders(model)
        assert auth["ok"] is True
        assert auth["headers"]["X-Custom-Header"] == "custom-value"


async def test_headers_only_override_resolves_at_request_time(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"anthropic": {"headers": {"X-Custom-Header": "custom-value"}}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    assert registry.getError() is None

    for model in getModelsForProvider(registry, "anthropic"):
        auth = await registry.getApiKeyAndHeaders(model)
        assert auth["ok"] is True
        assert auth["headers"]["X-Custom-Header"] == "custom-value"


async def test_unconfigured_compatibility_auth_includes_static_model_headers(authStorage, modelsJsonPath):
    registry = await createModelRegistry(authStorage, modelsJsonPath)
    model = registry.getAll()[0].model_copy(
        update={"provider": "missing-provider", "headers": {"X-Static-Model": "static-value"}}
    )

    auth = await registry.getApiKeyAndHeaders(model)

    assert auth == {"ok": True, "headers": {"X-Static-Model": "static-value"}}


async def test_base_url_only_override_does_not_affect_other_providers(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"anthropic": overrideConfig("https://my-proxy.example.com/v1")})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    googleModels = getModelsForProvider(registry, "google")

    assert len(googleModels) > 0
    assert googleModels[0].baseUrl != "https://my-proxy.example.com/v1"


async def test_can_mix_base_url_override_and_models_merge(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {
        "anthropic": overrideConfig("https://anthropic-proxy.example.com/v1"),
        "google": providerConfig(
            "https://google-proxy.example.com/v1", [{"id": "gemini-custom"}], "google-generative-ai"
        ),
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    anthropicModels = getModelsForProvider(registry, "anthropic")
    assert len(anthropicModels) > 1
    assert anthropicModels[0].baseUrl == "https://anthropic-proxy.example.com/v1"
    googleModels = getModelsForProvider(registry, "google")
    assert len(googleModels) > 1
    assert any(model.id == "gemini-custom" for model in googleModels)


async def test_refresh_picks_up_base_url_override_changes(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"anthropic": overrideConfig("https://first-proxy.example.com/v1")})
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert getModelsForProvider(registry, "anthropic")[0].baseUrl == "https://first-proxy.example.com/v1"

    writeRawModelsJson(modelsJsonPath, {"anthropic": overrideConfig("https://second-proxy.example.com/v1")})
    await registry.refresh()

    assert getModelsForProvider(registry, "anthropic")[0].baseUrl == "https://second-proxy.example.com/v1"


# -- custom models merge behavior ------------------------------------------------------------


async def test_built_in_provider_custom_models_inherit_api_and_base_url(authStorage, modelsJsonPath):
    # Built-in providers already have api/baseUrl on every model, and auth
    # comes from env vars / auth storage. No need to specify them.
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"models": [
        {"id": "fake-provider/fake-model", "name": "Fake model", "reasoning": True, "input": ["text"]},
    ]}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    assert registry.getError() is None

    model = registry.find("openrouter", "fake-provider/fake-model")
    assert model is not None
    assert model.api == "openai-completions"
    assert model.baseUrl == "https://openrouter.ai/api/v1"


async def test_non_built_in_provider_custom_models_still_require_base_url(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"my-custom-provider": {
        "apiKey": "test-key",
        "models": [{"id": "my-model", "api": "openai-completions", "reasoning": False, "input": ["text"]}],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    assert "baseUrl" in registry.getError()


async def test_reports_every_provider_composition_error(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {
        "broken-one": {"api": "openai-completions", "models": [{"id": "one"}]},
        "broken-two": {"api": "openai-completions", "models": [{"id": "two"}]},
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    error = registry.getError()

    assert 'Provider "broken-one"' in error
    assert 'Provider "broken-two"' in error


async def test_custom_provider_with_same_name_as_built_in_merges_with_built_in_models(authStorage, modelsJsonPath):
    writeModelsJson(modelsJsonPath, {
        "anthropic": providerConfig("https://my-proxy.example.com/v1", [{"id": "claude-custom"}]),
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    anthropicModels = getModelsForProvider(registry, "anthropic")

    assert len(anthropicModels) > 1
    assert any(model.id == "claude-custom" for model in anthropicModels)
    assert any("claude" in model.id for model in anthropicModels)


async def test_custom_model_with_same_id_replaces_built_in_model_by_id(authStorage, modelsJsonPath):
    writeModelsJson(modelsJsonPath, {
        "openrouter": providerConfig(
            "https://my-proxy.example.com/v1", [{"id": "anthropic/claude-sonnet-4.5"}], "openai-completions"
        ),
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    sonnetModels = [
        model for model in getModelsForProvider(registry, "openrouter") if model.id == "anthropic/claude-sonnet-4.5"
    ]

    assert len(sonnetModels) == 1
    assert sonnetModels[0].baseUrl == "https://my-proxy.example.com/v1"


async def test_custom_provider_with_same_name_does_not_affect_other_built_in_providers(authStorage, modelsJsonPath):
    writeModelsJson(modelsJsonPath, {
        "anthropic": providerConfig("https://my-proxy.example.com/v1", [{"id": "claude-custom"}]),
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert len(getModelsForProvider(registry, "google")) > 0
    assert len(getModelsForProvider(registry, "openai")) > 0


async def test_provider_level_base_url_applies_to_both_built_in_and_custom_models(authStorage, modelsJsonPath):
    writeModelsJson(modelsJsonPath, {
        "anthropic": providerConfig("https://merged-proxy.example.com/v1", [{"id": "claude-custom"}]),
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    for model in getModelsForProvider(registry, "anthropic"):
        assert model.baseUrl == "https://merged-proxy.example.com/v1"


def _demo_model(**extra) -> dict:
    return {
        "id": "demo-model", "reasoning": False, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 1000, "maxTokens": 100, **extra,
    }


async def test_provider_level_compat_applies_to_custom_models(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"demo": {
        "baseUrl": "https://example.com/v1", "apiKey": "DEMO_KEY", "api": "openai-completions",
        "compat": {"supportsUsageInStreaming": False, "maxTokensField": "max_tokens"},
        "models": [_demo_model()],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    compat = registry.find("demo", "demo-model").compat

    assert compat.supportsUsageInStreaming is False
    assert compat.maxTokensField == "max_tokens"


async def test_model_level_compat_overrides_provider_level_compat_for_custom_models(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"demo": {
        "baseUrl": "https://example.com/v1", "apiKey": "DEMO_KEY", "api": "openai-completions",
        "compat": {"supportsUsageInStreaming": False, "maxTokensField": "max_tokens"},
        "models": [_demo_model(compat={"supportsUsageInStreaming": True, "maxTokensField": "max_completion_tokens"})],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    compat = registry.find("demo", "demo-model").compat

    assert compat.supportsUsageInStreaming is True
    assert compat.maxTokensField == "max_completion_tokens"


async def test_provider_level_compat_applies_to_built_in_models(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {
        "compat": {"supportsUsageInStreaming": False, "supportsStrictMode": False},
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    models = getModelsForProvider(registry, "openrouter")

    assert len(models) > 0
    for model in models:
        assert model.compat.supportsUsageInStreaming is False
        assert model.compat.supportsStrictMode is False


async def test_model_schema_accepts_thinking_level_map_and_compat_flags(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"demo": {
        "baseUrl": "https://example.com/v1", "apiKey": "DEMO_KEY", "api": "openai-completions",
        "models": [_demo_model(
            reasoning=True,
            thinkingLevelMap={"minimal": None, "high": "max"},
            compat={"supportsStrictMode": False, "cacheControlFormat": "anthropic"},
        )],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    model = registry.find("demo", "demo-model")

    assert registry.getError() is None
    assert model.thinkingLevelMap == {"minimal": None, "high": "max"}
    assert model.compat.supportsStrictMode is False
    assert model.compat.cacheControlFormat == "anthropic"


async def test_compat_schema_accepts_chat_template_thinking_configuration(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"demo": {
        "baseUrl": "https://example.com/v1", "apiKey": "DEMO_KEY", "api": "openai-completions",
        "models": [
            _demo_model(id="kwargs-model", reasoning=True, compat={
                "thinkingFormat": "chat-template",
                "chatTemplateKwargs": {"preserve_thinking": True, "thinking": {"$var": "thinking.enabled"}},
            }),
            _demo_model(id="args-model", reasoning=True, compat={
                "thinkingFormat": "baseten",
                "chatTemplateArgs": {"enable_thinking": {"$var": "thinking.enabled"}},
            }),
        ],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    kwargsCompat = registry.find("demo", "kwargs-model").compat
    argsCompat = registry.find("demo", "args-model").compat

    assert registry.getError() is None
    assert kwargsCompat.thinkingFormat == "chat-template"
    assert _plain(kwargsCompat.chatTemplateKwargs) == {
        "preserve_thinking": True, "thinking": {"$var": "thinking.enabled"},
    }
    assert argsCompat.thinkingFormat == "baseten"
    assert _plain(argsCompat.chatTemplateArgs) == {"enable_thinking": {"$var": "thinking.enabled"}}


async def test_compat_schema_accepts_anthropic_eager_tool_input_streaming_flag(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"demo": {
        "baseUrl": "https://example.com", "apiKey": "DEMO_KEY", "api": "anthropic-messages",
        "compat": {"supportsEagerToolInputStreaming": False},
        "models": [_demo_model(reasoning=True)],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert registry.getError() is None
    assert registry.find("demo", "demo-model").compat.supportsEagerToolInputStreaming is False


async def test_compat_schema_accepts_long_cache_retention_flag(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"demo": {
        "baseUrl": "https://example.com", "apiKey": "DEMO_KEY", "api": "anthropic-messages",
        "compat": {"supportsLongCacheRetention": False},
        "models": [_demo_model(reasoning=True)],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert registry.getError() is None
    assert registry.find("demo", "demo-model").compat.supportsLongCacheRetention is False


async def test_model_level_base_url_overrides_provider_level_base_url_for_custom_models(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"opencode-go": {
        "baseUrl": "https://opencode.ai/zen/go/v1",
        "apiKey": "TEST_KEY",
        "models": [
            {
                "id": "minimax-m2.5", "api": "anthropic-messages", "baseUrl": "https://opencode.ai/zen/go",
                "reasoning": True, "input": ["text"],
                "cost": {"input": 0.3, "output": 1.2, "cacheRead": 0.03, "cacheWrite": 0},
                "contextWindow": 204800, "maxTokens": 131072,
            },
            {
                "id": "glm-5", "api": "openai-completions", "reasoning": True, "input": ["text"],
                "cost": {"input": 1, "output": 3.2, "cacheRead": 0.2, "cacheWrite": 0},
                "contextWindow": 204800, "maxTokens": 131072,
            },
        ],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert registry.find("opencode-go", "minimax-m2.5").baseUrl == "https://opencode.ai/zen/go"
    assert registry.find("opencode-go", "glm-5").baseUrl == "https://opencode.ai/zen/go/v1"


async def test_model_overrides_still_apply_when_provider_also_defines_models(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {
        "baseUrl": "https://my-proxy.example.com/v1",
        "apiKey": "OPENROUTER_API_KEY",
        "api": "openai-completions",
        "models": [{
            "id": "custom/openrouter-model", "name": "Custom OpenRouter Model", "reasoning": False,
            "input": ["text"], "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 128000, "maxTokens": 16384,
        }],
        "modelOverrides": {"anthropic/claude-sonnet-4.5": {"name": "Overridden Built-in Sonnet"}},
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    models = getModelsForProvider(registry, "openrouter")

    assert any(model.id == "custom/openrouter-model" for model in models)
    assert any(
        model.id == "anthropic/claude-sonnet-4.5" and model.name == "Overridden Built-in Sonnet" for model in models
    )


async def test_refresh_reloads_merged_custom_models_from_disk(authStorage, modelsJsonPath):
    writeModelsJson(modelsJsonPath, {
        "anthropic": providerConfig("https://first-proxy.example.com/v1", [{"id": "claude-custom"}]),
    })
    registry = await createModelRegistry(authStorage, modelsJsonPath)
    assert any(model.id == "claude-custom" for model in getModelsForProvider(registry, "anthropic"))

    writeModelsJson(modelsJsonPath, {
        "anthropic": providerConfig("https://second-proxy.example.com/v1", [{"id": "claude-custom-2"}]),
    })
    await registry.refresh()

    anthropicModels = getModelsForProvider(registry, "anthropic")
    assert not any(model.id == "claude-custom" for model in anthropicModels)
    assert any(model.id == "claude-custom-2" for model in anthropicModels)
    assert any("claude" in model.id for model in anthropicModels)


async def test_removing_custom_models_keeps_built_in_provider_models(authStorage, modelsJsonPath):
    writeModelsJson(modelsJsonPath, {
        "anthropic": providerConfig("https://proxy.example.com/v1", [{"id": "claude-custom"}]),
    })
    registry = await createModelRegistry(authStorage, modelsJsonPath)
    assert any(model.id == "claude-custom" for model in getModelsForProvider(registry, "anthropic"))

    writeModelsJson(modelsJsonPath, {})
    await registry.refresh()

    anthropicModels = getModelsForProvider(registry, "anthropic")
    assert not any(model.id == "claude-custom" for model in anthropicModels)
    assert any("claude" in model.id for model in anthropicModels)


# -- modelOverrides (per-model customization) ------------------------------------------------


async def test_model_override_applies_to_a_single_built_in_model(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "anthropic/claude-sonnet-4.5": {"name": "Custom Sonnet Name"},
    }}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    models = getModelsForProvider(registry, "openrouter")

    assert _find(models, "anthropic/claude-sonnet-4.5").name == "Custom Sonnet Name"
    assert _find(models, "anthropic/claude-opus-4.1").name != "Custom Sonnet Name"


async def test_anthropic_model_override_replaces_allowed_fallback_metadata(authStorage, modelsJsonPath):
    allowedFallbackModels = [
        {"provider": "anthropic", "model": "claude-opus-5",
         "cost": {"input": 5, "output": 25, "cacheRead": 0.5, "cacheWrite": 6.25}},
        {"provider": "anthropic", "model": "claude-opus-4-8",
         "cost": {"input": 4, "output": 20, "cacheRead": 0.4, "cacheWrite": 5}},
    ]
    writeRawModelsJson(modelsJsonPath, {"anthropic": {"modelOverrides": {
        "claude-fable-5": {"compat": {"allowedFallbackModels": allowedFallbackModels}},
    }}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    compat = registry.find("anthropic", "claude-fable-5").compat

    assert registry.getError() is None
    assert json.loads(json.dumps(_plain(compat.allowedFallbackModels))) == json.loads(json.dumps(allowedFallbackModels))


async def test_empty_allowed_fallback_model_override_disables_server_side_fallback(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"anthropic": {"modelOverrides": {
        "claude-fable-5": {"compat": {"allowedFallbackModels": []}},
    }}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert registry.getError() is None
    assert registry.find("anthropic", "claude-fable-5").compat.allowedFallbackModels == []


async def test_custom_models_and_model_overrides_carry_sampling_params(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {
        "baseUrl": "https://my-proxy.example.com/v1",
        "api": "openai-completions",
        "models": [{
            "id": "custom/sampling-model",
            "samplingParams": {"temperature": 1, "top_p": 0.95, "top_k": 0},
            "samplingParamsByThinkingLevel": {"low": {"temperature": 0.6, "top_p": 0.95}, "high": {"temperature": 0.8}},
        }],
        "modelOverrides": {
            "custom/sampling-model": {
                "samplingParamsByThinkingLevel": {"low": {"temperature": 0.5, "top_k": 20}, "max": {"temperature": 1}},
            },
            "anthropic/claude-sonnet-4.5": {
                "samplingParams": {"top_p": 0.9},
                "samplingParamsByThinkingLevel": {"high": {"temperature": 0.8}},
            },
        },
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    models = getModelsForProvider(registry, "openrouter")

    custom = _find(models, "custom/sampling-model")
    assert custom.samplingParams == {"temperature": 1, "top_p": 0.95, "top_k": 0}
    assert custom.samplingParamsByThinkingLevel == {
        "low": {"temperature": 0.5, "top_p": 0.95, "top_k": 20},
        "high": {"temperature": 0.8},
        "max": {"temperature": 1},
    }

    sonnet = _find(models, "anthropic/claude-sonnet-4.5")
    assert sonnet.samplingParams == {"top_p": 0.9}
    assert sonnet.samplingParamsByThinkingLevel == {"high": {"temperature": 0.8}}

    # Models without sampling config keep it unset.
    opus = _find(models, "anthropic/claude-opus-4.1")
    assert opus.samplingParams is None
    assert opus.samplingParamsByThinkingLevel is None


async def test_custom_model_and_model_override_carry_prompt_cache_lifetimes(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {
        "openrouter": {
            "baseUrl": "https://my-proxy.example.com/v1",
            "api": "openai-completions",
            "models": [{"id": "custom/cached-model", "promptCache": {"short": 120}}],
            "modelOverrides": {"anthropic/claude-sonnet-4.5": {"promptCache": {"short": 300}}},
        },
        "anthropic": {"modelOverrides": {"claude-sonnet-4-6": {"promptCache": {"long": 1800}}}},
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    openrouter = getModelsForProvider(registry, "openrouter")

    assert registry.getError() is None
    assert _plain(_find(openrouter, "custom/cached-model").promptCache) == {"short": 120}
    assert _plain(_find(openrouter, "anthropic/claude-sonnet-4.5").promptCache) == {"short": 300}
    assert _find(openrouter, "anthropic/claude-opus-4.1").promptCache is None
    # Overrides merge per tier with the built-in catalog.
    assert _plain(registry.find("anthropic", "claude-sonnet-4-6").promptCache) == {"short": 300, "long": 1800}


async def test_model_override_deep_merges_image_resize_limits(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"test": {
        "baseUrl": "https://example.com",
        "apiKey": "test-key",
        "api": "openai-completions",
        "models": [{
            "id": "vision-model",
            "input": ["text", "image"],
            "inputLimits": {
                "maxRequestBytes": 32 * 1024 * 1024,
                "images": {
                    "maxPerRequest": 100,
                    "resize": {"maxWidth": 2000, "maxHeight": 2000, "maxBytes": 4718592, "jpegQuality": 80},
                },
            },
        }],
        "modelOverrides": {"vision-model": {"inputLimits": {
            "images": {"resize": {"maxWidth": 1568, "maxBytes": 524288, "jpegQuality": 75}},
        }}},
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    limits = _plain(registry.find("test", "vision-model").inputLimits)

    assert registry.getError() is None
    assert limits["maxRequestBytes"] == 32 * 1024 * 1024
    assert limits["images"]["maxPerRequest"] == 100
    assert limits["images"]["resize"] == {"maxWidth": 1568, "maxHeight": 2000, "maxBytes": 524288, "jpegQuality": 75}


async def test_model_override_with_compat_open_router_routing(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "anthropic/claude-sonnet-4.5": {"compat": {"openRouterRouting": {"only": ["amazon-bedrock"]}}},
    }}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    sonnet = _find(getModelsForProvider(registry, "openrouter"), "anthropic/claude-sonnet-4.5")

    assert _plain(sonnet.compat.openRouterRouting) == {"only": ["amazon-bedrock"]}


async def test_supports_finish_reason_can_be_configured_at_provider_and_model_levels(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {
        "compat": {"supportsFinishReason": True},
        "modelOverrides": {"anthropic/claude-sonnet-4.5": {"compat": {"supportsFinishReason": False}}},
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    models = getModelsForProvider(registry, "openrouter")

    assert _find(models, "anthropic/claude-sonnet-4.5").compat.supportsFinishReason is False
    assert _find(models, "anthropic/claude-opus-4.1").compat.supportsFinishReason is True


async def test_model_override_deep_merges_compat_settings(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "anthropic/claude-sonnet-4.5": {"compat": {"openRouterRouting": {"order": ["anthropic", "together"]}}},
    }}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    sonnet = _find(getModelsForProvider(registry, "openrouter"), "anthropic/claude-sonnet-4.5")

    assert _plain(sonnet.compat.openRouterRouting) == {"order": ["anthropic", "together"]}


async def test_multiple_model_overrides_on_same_provider(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "anthropic/claude-sonnet-4.5": {"compat": {"openRouterRouting": {"only": ["amazon-bedrock"]}}},
        "anthropic/claude-opus-4.1": {"compat": {"openRouterRouting": {"only": ["anthropic"]}}},
    }}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    models = getModelsForProvider(registry, "openrouter")

    assert _plain(_find(models, "anthropic/claude-sonnet-4.5").compat.openRouterRouting) == {"only": ["amazon-bedrock"]}
    assert _plain(_find(models, "anthropic/claude-opus-4.1").compat.openRouterRouting) == {"only": ["anthropic"]}


async def test_model_override_combined_with_base_url_override(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {
        "baseUrl": "https://my-proxy.example.com/v1",
        "modelOverrides": {"anthropic/claude-sonnet-4.5": {"name": "Proxied Sonnet"}},
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    models = getModelsForProvider(registry, "openrouter")
    sonnet = _find(models, "anthropic/claude-sonnet-4.5")
    opus = _find(models, "anthropic/claude-opus-4.1")

    assert (sonnet.baseUrl, sonnet.name) == ("https://my-proxy.example.com/v1", "Proxied Sonnet")
    assert opus.baseUrl == "https://my-proxy.example.com/v1" and opus.name != "Proxied Sonnet"


async def test_model_override_for_non_existent_model_id_is_ignored(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "nonexistent/model-id": {"name": "This should not appear"},
    }}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert _find(getModelsForProvider(registry, "openrouter"), "nonexistent/model-id") is None
    assert registry.getError() is None


async def test_model_override_can_change_cost_fields_partially(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "anthropic/claude-sonnet-4.5": {"cost": {"input": 99}},
    }}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    sonnet = _find(getModelsForProvider(registry, "openrouter"), "anthropic/claude-sonnet-4.5")

    assert sonnet.cost.input == 99
    assert sonnet.cost.output > 0


async def test_model_override_can_add_headers_at_request_time(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "anthropic/claude-sonnet-4.5": {"headers": {"X-Custom-Model-Header": "value"}},
    }}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    sonnet = _find(getModelsForProvider(registry, "openrouter"), "anthropic/claude-sonnet-4.5")
    assert sonnet is not None

    auth = await registry.getApiKeyAndHeaders(sonnet)
    assert auth["ok"] is True
    assert auth["headers"]["X-Custom-Model-Header"] == "value"


async def test_refresh_picks_up_model_override_changes(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "anthropic/claude-sonnet-4.5": {"name": "First Name"},
    }}})
    registry = await createModelRegistry(authStorage, modelsJsonPath)
    assert _find(getModelsForProvider(registry, "openrouter"), "anthropic/claude-sonnet-4.5").name == "First Name"

    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "anthropic/claude-sonnet-4.5": {"name": "Second Name"},
    }}})
    await registry.refresh()

    assert _find(getModelsForProvider(registry, "openrouter"), "anthropic/claude-sonnet-4.5").name == "Second Name"


async def test_removing_model_override_restores_built_in_values(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"openrouter": {"modelOverrides": {
        "anthropic/claude-sonnet-4.5": {"name": "Custom Name"},
    }}})
    registry = await createModelRegistry(authStorage, modelsJsonPath)
    assert _find(getModelsForProvider(registry, "openrouter"), "anthropic/claude-sonnet-4.5").name == "Custom Name"

    writeRawModelsJson(modelsJsonPath, {})
    await registry.refresh()

    assert _find(getModelsForProvider(registry, "openrouter"), "anthropic/claude-sonnet-4.5").name != "Custom Name"



# -- dynamic provider lifecycle --------------------------------------------------------------


def _extension_model(**extra) -> dict:
    return {
        "id": "demo-model", "name": "Demo Model", "reasoning": False, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 128000, "maxTokens": 4096, **extra,
    }


async def _login(_callbacks):
    return {"access": "access", "refresh": "refresh", "expires": 2**53 - 1}


async def _refresh_token(credentials, _signal=None):
    return credentials


async def test_get_provider_display_name_resolves_registered_oauth_built_in_and_fallback_names(
    authStorage, modelsJsonPath
):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert registry.getProviderDisplayName("openai") == "OpenAI"
    assert registry.getProviderDisplayName("github-copilot") == "GitHub Copilot"
    assert registry.getProviderDisplayName("zai") == "Z.AI"
    assert registry.getProviderDisplayName("unknown-provider") == "unknown-provider"

    registry.registerProvider("named-provider", {
        "name": "Named Provider", "baseUrl": "https://provider.test/v1", "apiKey": "test-key",
        "api": "openai-completions", "models": [_extension_model()],
    })
    assert registry.getProviderDisplayName("named-provider") == "Named Provider"

    registry.registerProvider("oauth-provider", {
        "baseUrl": "https://provider.test/v1",
        "api": "openai-completions",
        "oauth": {
            "name": "OAuth Provider", "login": _login, "refreshToken": _refresh_token,
            "getApiKey": lambda credentials: credentials.access,
        },
        "models": [_extension_model()],
    })
    assert registry.getProviderDisplayName("oauth-provider") == "OAuth Provider"


async def test_model_overrides_apply_to_dynamically_registered_provider_models(authStorage, modelsJsonPath):
    from misaka.ai.models import getSupportedThinkingLevels

    writeRawModelsJson(modelsJsonPath, {"extension-provider": {"modelOverrides": {"extension-model": {
        "name": "Overridden Extension Model",
        "thinkingLevelMap": {"off": None, "minimal": None, "low": None, "medium": None, "xhigh": "max"},
        "headers": {"x-model-override": "enabled"},
    }}}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    registry.registerProvider("extension-provider", {
        "baseUrl": "https://provider.test/v1", "apiKey": "test-key", "api": "openai-completions",
        "models": [_extension_model(id="extension-model", name="Extension Model", reasoning=True)],
    })

    model = registry.find("extension-provider", "extension-model")
    assert model is not None
    assert model.name == "Overridden Extension Model"
    assert model.thinkingLevelMap == {"off": None, "minimal": None, "low": None, "medium": None, "xhigh": "max"}
    assert getSupportedThinkingLevels(model) == ["high", "xhigh"]
    auth = await registry.getApiKeyAndHeaders(model)
    assert auth["ok"] is True and auth["headers"]["x-model-override"] == "enabled"


async def test_stored_api_key_env_propagates_to_request_auth_and_resolves_headers(authStorage, modelsJsonPath):
    from misaka.ai.auth.types import ApiKeyCredential

    async def stored(_current):
        return ApiKeyCredential(key="$CLOUDFLARE_API_KEY", env={
            "CLOUDFLARE_API_KEY": "stored-cf-token",
            "CLOUDFLARE_ACCOUNT_ID": "stored-account",
            "CLOUDFLARE_GATEWAY_ID": "stored-gateway",
        })

    await authStorage.modify("cloudflare-ai-gateway", stored)
    writeRawModelsJson(modelsJsonPath, {"cloudflare-ai-gateway": {"headers": {"x-account": "$CLOUDFLARE_ACCOUNT_ID"}}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    model = next((m for m in registry.getAll() if m.provider == "cloudflare-ai-gateway"), None)
    assert model is not None

    auth = await registry.getApiKeyAndHeaders(model)

    assert auth == {
        "ok": True,
        "apiKey": None,
        "headers": {
            "cf-aig-authorization": "Bearer stored-cf-token",
            "Authorization": None,
            "x-api-key": None,
            "x-account": "stored-account",
        },
        "env": {"CLOUDFLARE_ACCOUNT_ID": "stored-account", "CLOUDFLARE_GATEWAY_ID": "stored-gateway"},
    }


async def test_register_provider_treats_uppercase_api_key_and_headers_as_literals(
    authStorage, modelsJsonPath, monkeypatch
):
    for key in ("CUSTOM_NAME", "BEARER", "MODEL_TOKEN"):
        monkeypatch.setenv(key, f"env-{key}")
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    registry.registerProvider("literal-provider", {
        **providerConfig("https://provider.test/v1", [{"id": "demo-model"}], "openai-completions"),
        "apiKey": "CUSTOM_NAME",
        "headers": {"Authorization": "BEARER"},
        "models": [{
            "id": "demo-model", "name": "demo-model", "reasoning": False, "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 100000, "maxTokens": 8000, "headers": {"x-model-token": "MODEL_TOKEN"},
        }],
    })

    assert await registry.getApiKeyForProvider("literal-provider") == "CUSTOM_NAME"
    model = registry.find("literal-provider", "demo-model")
    assert model is not None
    auth = await registry.getApiKeyAndHeaders(model)
    assert auth["ok"] is True and auth["apiKey"] == "CUSTOM_NAME"
    assert auth["headers"]["Authorization"] == "BEARER" and auth["headers"]["x-model-token"] == "MODEL_TOKEN"


async def test_failed_register_provider_does_not_persist_invalid_stream_simple_config(authStorage, modelsJsonPath):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    def should_not_run(*_args):
        raise RuntimeError("should not run")

    with pytest.raises(RuntimeError, match='Provider broken-provider: "api" is required when registering streamSimple.'):
        registry.registerProvider("broken-provider", {"streamSimple": should_not_run})

    assert (await registry.refresh()).aborted is False


async def test_failed_register_provider_does_not_remove_existing_provider_models(authStorage, modelsJsonPath):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    registry.registerProvider("demo-provider", {
        "baseUrl": "https://provider.test/v1", "apiKey": "test-key", "api": "openai-completions",
        "models": [_extension_model()],
    })
    assert registry.find("demo-provider", "demo-model") is not None

    with pytest.raises(RuntimeError, match='Provider demo-provider, model broken-model: no "api" specified.'):
        registry.registerProvider("demo-provider", {
            "baseUrl": "https://provider.test/v2", "apiKey": "test-key",
            "models": [_extension_model(id="broken-model", name="Broken Model")],
        })

    assert registry.find("demo-provider", "demo-model") is not None
    assert (await registry.refresh()).aborted is False
    assert registry.find("demo-provider", "demo-model") is not None


async def test_unregister_provider_removes_the_runtime_oauth_overlay(authStorage, modelsJsonPath):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    registry.registerProvider("anthropic", {"oauth": {
        "name": "Custom Anthropic OAuth", "login": _login, "refreshToken": _refresh_token,
        "getApiKey": lambda credentials: credentials.access,
    }})

    assert registry.getRegisteredProviderConfig("anthropic")["oauth"]["name"] == "Custom Anthropic OAuth"

    registry.unregisterProvider("anthropic")

    assert registry.getRegisteredProviderConfig("anthropic") is None


async def test_stream_simple_overlays_do_not_mutate_the_global_api_registry(authStorage, modelsJsonPath):
    from misaka.ai.compat import getApiProvider
    from misaka.ai.types import Model
    from misaka.ai.utils.transcript import normalizeContext

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    def override(*_args):
        raise RuntimeError("custom streamSimple override")

    registry.registerProvider("stream-override-provider", {"api": "openai-completions", "streamSimple": override})
    openAiModel = Model.model_validate({
        "id": "test-openai-model", "name": "Test OpenAI Model", "api": "openai-completions", "provider": "openai",
        "baseUrl": "https://api.openai.com/v1", "reasoning": False, "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 128000, "maxTokens": 4096,
    })

    def throws_override() -> bool:
        try:
            getApiProvider("openai-completions").streamSimple(openAiModel, normalizeContext({"messages": []}))
        except RuntimeError as error:
            return str(error) == "custom streamSimple override"
        return False

    assert throws_override() is False
    registry.unregisterProvider("stream-override-provider")
    assert throws_override() is False


async def test_base_url_only_override_keeps_built_in_provider_models_after_refresh(authStorage, modelsJsonPath):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    registry.registerProvider("anthropic", {"baseUrl": "https://proxy.test/anthropic"})
    await registry.refresh()

    anthropicModels = getModelsForProvider(registry, "anthropic")
    assert len(anthropicModels) > 1
    assert all(model.baseUrl == "https://proxy.test/anthropic" for model in anthropicModels)


async def test_models_only_override_replaces_built_in_provider_models_after_refresh(authStorage, modelsJsonPath):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    registry.registerProvider("anthropic", {
        **providerConfig("https://custom.test/anthropic", [{"id": "custom-claude"}], "anthropic-messages"),
        "baseUrl": "https://custom.test/anthropic",
    })
    await registry.refresh()

    assert [model.id for model in getModelsForProvider(registry, "anthropic")] == ["custom-claude"]
    assert registry.find("anthropic", "custom-claude").baseUrl == "https://custom.test/anthropic"


async def test_models_plus_base_url_override_replaces_built_in_provider_models_after_refresh(
    authStorage, modelsJsonPath
):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    registry.registerProvider("anthropic", {
        **providerConfig("https://custom.test/anthropic", [{"id": "custom-claude"}], "anthropic-messages"),
        "baseUrl": "https://custom.test/anthropic",
    })
    registry.registerProvider("anthropic", {"baseUrl": "https://proxy.test/anthropic"})
    await registry.refresh()

    assert [model.id for model in getModelsForProvider(registry, "anthropic")] == ["custom-claude"]
    assert registry.find("anthropic", "custom-claude").baseUrl == "https://proxy.test/anthropic"


def _custom_provider() -> dict:
    return providerConfig("https://custom.test/v1", [{"id": "custom-a"}, {"id": "custom-b"}], "openai-completions")


async def test_models_only_custom_provider_registration_survives_refresh(authStorage, modelsJsonPath):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    registry.registerProvider("custom-provider", _custom_provider())
    await registry.refresh()

    assert [model.id for model in getModelsForProvider(registry, "custom-provider")] == ["custom-a", "custom-b"]


async def test_base_url_only_override_keeps_custom_provider_models_after_refresh(authStorage, modelsJsonPath):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    registry.registerProvider("custom-provider", _custom_provider())
    registry.registerProvider("custom-provider", {"baseUrl": "https://proxy.test/custom"})
    await registry.refresh()

    models = getModelsForProvider(registry, "custom-provider")
    assert [model.id for model in models] == ["custom-a", "custom-b"]
    assert all(model.baseUrl == "https://proxy.test/custom" for model in models)


async def test_headers_only_override_keeps_custom_provider_models_after_refresh(authStorage, modelsJsonPath):
    registry = await createModelRegistry(authStorage, modelsJsonPath)

    registry.registerProvider("custom-provider", _custom_provider())
    registry.registerProvider("custom-provider", {"headers": {"x-proxy": "enabled"}})
    await registry.refresh()

    models = getModelsForProvider(registry, "custom-provider")
    assert [model.id for model in models] == ["custom-a", "custom-b"]
    assert all(model.baseUrl == "https://custom.test/v1" for model in models)
    auth = await registry.getApiKeyAndHeaders(models[0])
    assert auth["ok"] is True and auth["headers"]["x-proxy"] == "enabled"


# -- API key resolution ----------------------------------------------------------------------


def providerWithApiKey(apiKey: str) -> dict:
    """A provider config with a custom apiKey."""
    return {
        "baseUrl": "https://example.com/v1",
        "apiKey": apiKey,
        "api": "anthropic-messages",
        "models": [{
            "id": "test-model", "name": "Test Model", "reasoning": False, "input": ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 100000, "maxTokens": 8000,
        }],
    }


def toShPath(value) -> str:
    return str(value).replace("\\", "/").replace('"', '\\"')


def _defined(auth: dict) -> dict:
    """``toEqual`` ignores properties that are ``undefined``."""
    return {key: value for key, value in auth.items() if value is not None}


async def _api_key(authStorage, modelsJsonPath, apiKey: str):
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey(apiKey)})
    registry = await createModelRegistry(authStorage, modelsJsonPath)
    return await registry.getApiKeyForProvider("custom-provider")


@pytest.mark.parametrize("apiKey,expected", [
    ("!echo test-api-key-from-command", "test-api-key-from-command"),
    ("!echo '  spaced-key  '", "spaced-key"),
    ("!printf 'line1\\nline2'", "line1\nline2"),
    ("!exit 1", None),
    ("!nonexistent-command-12345", None),
    ("!printf ''", None),
    ("!echo 'hello world' | tr ' ' '-'", "hello-world"),
])
async def test_api_key_commands(authStorage, modelsJsonPath, apiKey, expected):
    assert await _api_key(authStorage, modelsJsonPath, apiKey) == expected


async def test_api_key_with_dollar_prefix_resolves_to_env_value(authStorage, modelsJsonPath, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY_12345", "env-api-key-value")
    assert await _api_key(authStorage, modelsJsonPath, "$TEST_API_KEY_12345") == "env-api-key-value"


async def test_api_key_with_braced_env_syntax_resolves_to_env_value(authStorage, modelsJsonPath, monkeypatch):
    monkeypatch.setenv("TEST_BRACED_API_KEY_12345", "braced-env-api-key-value")
    assert await _api_key(authStorage, modelsJsonPath, "${TEST_BRACED_API_KEY_12345}") == "braced-env-api-key-value"


async def test_api_key_interpolates_braced_env_references_inside_literals(authStorage, modelsJsonPath, monkeypatch):
    monkeypatch.setenv("TEST_INTERPOLATED_PART_A_12345", "left")
    monkeypatch.setenv("TEST_INTERPOLATED_PART_B_12345", "right")
    key = "${TEST_INTERPOLATED_PART_A_12345}_${TEST_INTERPOLATED_PART_B_12345}"
    assert await _api_key(authStorage, modelsJsonPath, key) == "left_right"


async def test_api_key_with_double_dollar_prefix_escapes_a_leading_dollar(authStorage, modelsJsonPath):
    assert await _api_key(authStorage, modelsJsonPath, "$$TEST_API_KEY_12345") == "$TEST_API_KEY_12345"


async def test_api_key_with_dollar_bang_escapes_a_literal_bang(authStorage, modelsJsonPath, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY_12345", "env-api-key-value")
    key = "$!literal-$TEST_API_KEY_12345"
    assert await _api_key(authStorage, modelsJsonPath, key) == "!literal-env-api-key-value"


async def test_plain_api_key_is_used_directly_even_when_it_matches_an_env_var(
    authStorage, modelsJsonPath, monkeypatch
):
    monkeypatch.setenv("TEST_API_KEY_12345", "env-api-key-value")
    assert await _api_key(authStorage, modelsJsonPath, "TEST_API_KEY_12345") == "TEST_API_KEY_12345"


async def test_api_key_as_literal_value_is_used_directly(authStorage, modelsJsonPath, monkeypatch):
    monkeypatch.delenv("literal_api_key_value", raising=False)
    assert await _api_key(authStorage, modelsJsonPath, "literal_api_key_value") == "literal_api_key_value"


def _counting_command(counter, *, tail: str = 'echo "key-value"') -> str:
    path = toShPath(counter)
    return f"!sh -c 'count=$(cat \"{path}\"); echo $((count + 1)) > \"{path}\"; {tail}'"


def _count(counter) -> int:
    return int(counter.read_text(encoding="utf-8").strip())


async def test_command_is_executed_on_every_provider_lookup(authStorage, modelsJsonPath, tmp_path):
    counter = tmp_path / "counter"
    counter.write_text("0", encoding="utf-8")
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey(_counting_command(counter))})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    for _ in range(3):
        await registry.getApiKeyForProvider("custom-provider")

    assert _count(counter) == 3


async def test_commands_are_re_executed_across_registry_instances(authStorage, modelsJsonPath, tmp_path):
    counter = tmp_path / "counter"
    counter.write_text("0", encoding="utf-8")
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey(_counting_command(counter))})

    await (await createModelRegistry(authStorage, modelsJsonPath)).getApiKeyForProvider("custom-provider")
    await (await createModelRegistry(authStorage, modelsJsonPath)).getApiKeyForProvider("custom-provider")

    assert _count(counter) == 2


async def test_different_commands_resolve_independently(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {
        "provider-a": providerWithApiKey("!echo key-a"),
        "provider-b": providerWithApiKey("!echo key-b"),
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert await registry.getApiKeyForProvider("provider-a") == "key-a"
    assert await registry.getApiKeyForProvider("provider-b") == "key-b"


async def test_failed_commands_are_retried(authStorage, modelsJsonPath, tmp_path):
    counter = tmp_path / "counter"
    counter.write_text("0", encoding="utf-8")
    writeRawModelsJson(modelsJsonPath, {
        "custom-provider": providerWithApiKey(_counting_command(counter, tail="exit 1")),
    })

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert await registry.getApiKeyForProvider("custom-provider") is None
    assert await registry.getApiKeyForProvider("custom-provider") is None
    assert _count(counter) == 2


async def test_provider_auth_status_reports_api_key_environment_variables(authStorage, modelsJsonPath, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY_STATUS_TEST_98765", "status-test-key")
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey("$TEST_API_KEY_STATUS_TEST_98765")})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    status = registry.getProviderAuthStatus("custom-provider")
    assert (status.configured, status.source, status.label) == (True, "environment", "TEST_API_KEY_STATUS_TEST_98765")


async def test_provider_auth_status_reports_interpolated_api_key_environment_variables(
    authStorage, modelsJsonPath, monkeypatch
):
    monkeypatch.setenv("TEST_API_KEY_STATUS_PART_A_98765", "left")
    monkeypatch.setenv("TEST_API_KEY_STATUS_PART_B_98765", "right")
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey(
        "${TEST_API_KEY_STATUS_PART_A_98765}_${TEST_API_KEY_STATUS_PART_B_98765}"
    )})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    status = registry.getProviderAuthStatus("custom-provider")
    assert (status.configured, status.source, status.label) == (
        True, "environment", "TEST_API_KEY_STATUS_PART_A_98765, TEST_API_KEY_STATUS_PART_B_98765",
    )


async def test_provider_auth_status_reports_non_env_api_key_values_as_a_config_key(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey("literal_api_key_value")})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    status = registry.getProviderAuthStatus("custom-provider")
    assert (status.configured, status.source, status.label) == (True, "models_json_key", None)


async def test_missing_explicit_env_api_key_keeps_provider_unavailable(authStorage, modelsJsonPath, monkeypatch):
    monkeypatch.delenv("TEST_API_KEY_MISSING_TEST_98765", raising=False)
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey("$TEST_API_KEY_MISSING_TEST_98765")})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    status = registry.getProviderAuthStatus("custom-provider")
    assert (status.configured, status.source) == (False, None)
    assert not any(model.provider == "custom-provider" for model in registry.getAvailable())


async def test_provider_auth_status_reports_command_api_keys_without_executing_them(
    authStorage, modelsJsonPath, tmp_path
):
    counter = tmp_path / "status-counter"
    counter.write_text("0", encoding="utf-8")
    command = f"!sh -c 'echo 1 > \"{toShPath(counter)}\"; echo key-value'"
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey(command)})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    status = registry.getProviderAuthStatus("custom-provider")
    assert (status.configured, status.source) == (True, "models_json_command")
    assert counter.read_text(encoding="utf-8") == "0"


async def test_environment_variables_are_not_cached(authStorage, modelsJsonPath, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY_CACHE_TEST_98765", "first-value")
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey("$TEST_API_KEY_CACHE_TEST_98765")})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    assert await registry.getApiKeyForProvider("custom-provider") == "first-value"

    monkeypatch.setenv("TEST_API_KEY_CACHE_TEST_98765", "second-value")
    assert await registry.getApiKeyForProvider("custom-provider") == "second-value"


async def test_get_available_does_not_execute_command_backed_api_key_resolution(authStorage, modelsJsonPath, tmp_path):
    counter = tmp_path / "counter"
    counter.write_text("0", encoding="utf-8")
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey(_counting_command(counter))})

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert any(model.provider == "custom-provider" for model in registry.getAvailable())
    assert _count(counter) == 0


async def test_get_available_filters_github_copilot_oauth_models_to_account_picker(authStorage, modelsJsonPath):
    import time

    from misaka.ai.auth.types import OAuthCredential
    from misaka.ai.compat import getModels

    copilotModel = getModels("github-copilot")[0]

    async def stored(_current):
        return OAuthCredential(
            type="oauth",
            refresh="github-access-token",
            access="tid=test;exp=9999999999;proxy-ep=proxy.individual.githubcopilot.com;",
            expires=int(time.time() * 1000) + 60_000,
            availableModelIds=[copilotModel.id],
        )

    await authStorage.modify("github-copilot", stored)

    registry = await createModelRegistry(authStorage, modelsJsonPath)

    assert [model.id for model in registry.getAvailable() if model.provider == "github-copilot"] == [copilotModel.id]


async def test_get_api_key_and_headers_resolves_auth_header_on_every_request(authStorage, modelsJsonPath, tmp_path):
    token = tmp_path / "token"
    token.write_text("token-1", encoding="utf-8")
    writeRawModelsJson(modelsJsonPath, {"custom-provider": {
        **providerWithApiKey(f"!sh -c 'cat \"{toShPath(token)}\"'"),
        "authHeader": True,
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    model = registry.find("custom-provider", "test-model")
    assert model is not None

    assert _defined(await registry.getApiKeyAndHeaders(model)) == {
        "ok": True, "apiKey": "token-1", "headers": {"Authorization": "Bearer token-1"},
    }
    token.write_text("token-2", encoding="utf-8")
    assert _defined(await registry.getApiKeyAndHeaders(model)) == {
        "ok": True, "apiKey": "token-2", "headers": {"Authorization": "Bearer token-2"},
    }


async def test_get_api_key_and_headers_resolves_configured_auth_exactly_once(authStorage, modelsJsonPath, tmp_path):
    counter = tmp_path / "auth-counter"
    counter.write_text("0", encoding="utf-8")
    path = toShPath(counter)
    command = f"!sh -c 'count=$(cat \"{path}\"); count=$((count + 1)); echo \"$count\" > \"{path}\"; echo \"token-$count\"'"
    writeRawModelsJson(modelsJsonPath, {"custom-provider": {**providerWithApiKey(command), "authHeader": True}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    auth = await registry.getApiKeyAndHeaders(registry.find("custom-provider", "test-model"))

    assert _defined(auth) == {"ok": True, "apiKey": "token-1", "headers": {"Authorization": "Bearer token-1"}}
    assert counter.read_text(encoding="utf-8").strip() == "1"


async def test_stored_credentials_bypass_lower_priority_configured_auth_commands(
    authStorage, modelsJsonPath, tmp_path
):
    from misaka.ai.auth.types import ApiKeyCredential

    counter = tmp_path / "fallback-counter"
    counter.write_text("0", encoding="utf-8")
    command = f"!sh -c 'echo 1 > \"{toShPath(counter)}\"; echo fallback-key'"
    writeRawModelsJson(modelsJsonPath, {"custom-provider": providerWithApiKey(command)})

    async def stored(_current):
        return ApiKeyCredential(key="stored-key")

    await authStorage.modify("custom-provider", stored)

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    auth = await registry.getApiKeyAndHeaders(registry.find("custom-provider", "test-model"))

    assert auth["ok"] is True and auth["apiKey"] == "stored-key"
    assert counter.read_text(encoding="utf-8").strip() == "0"


async def test_get_api_key_and_headers_preserves_the_legacy_missing_key_auth_header_error(
    authStorage, modelsJsonPath
):
    writeRawModelsJson(modelsJsonPath, {"custom-provider": {
        "baseUrl": "https://example.test/v1", "api": "openai-completions", "authHeader": True,
        "models": [{"id": "test-model"}],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    auth = await registry.getApiKeyAndHeaders(registry.find("custom-provider", "test-model"))

    assert auth == {"ok": False, "error": 'No API key found for "custom-provider"'}


async def test_get_api_key_and_headers_returns_an_error_for_failed_auth_header_resolution(
    authStorage, modelsJsonPath
):
    writeRawModelsJson(modelsJsonPath, {"custom-provider": {**providerWithApiKey("!exit 1"), "authHeader": True}})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    model = registry.find("custom-provider", "test-model")
    assert model is not None

    auth = await registry.getApiKeyAndHeaders(model)
    assert auth["ok"] is False
    assert 'Failed to resolve API key for provider "custom-provider"' in auth["error"]


# --- MISAKA: compat is carried whole, as upstream's mergeCompat carries it --------------------


async def test_unknown_nested_routing_keys_are_kept_not_fatal(authStorage, modelsJsonPath):
    writeRawModelsJson(modelsJsonPath, {"demo": {
        "baseUrl": "https://example.com/v1", "apiKey": "DEMO_KEY", "api": "openai-completions",
        "compat": {"openRouterRouting": {"order": ["a"], "notYetKnown": 1}},
        "models": [_demo_model()],
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    model = registry.find("demo", "demo-model")

    assert registry.getError() is None
    assert model.compat.openRouterRouting.model_dump(exclude_none=True) == {"order": ["a"], "notYetKnown": 1}


async def test_provider_compat_another_api_spells_differently_does_not_drop_the_provider(authStorage, modelsJsonPath):
    # github-copilot mixes anthropic-messages and openai models; "openai" is only an OpenAI
    # spelling of sessionAffinityFormat, and vllmPriority is any number upstream.
    writeRawModelsJson(modelsJsonPath, {"github-copilot": {
        "baseUrl": "https://proxy.example.com",
        "compat": {"sessionAffinityFormat": "openai", "vllmPriority": 1.5},
    }})

    registry = await createModelRegistry(authStorage, modelsJsonPath)
    models = getModelsForProvider(registry, "github-copilot")

    assert registry.getError() is None
    assert {model.api for model in models} >= {"anthropic-messages", "openai-completions"}
    assert all(model.baseUrl == "https://proxy.example.com" for model in models)
    assert all(model.compat.sessionAffinityFormat == "openai" for model in models)
