"""Ported from pi's ``packages/coding-agent/test/model-resolver.test.ts`` (v1.3.0). ``console.warn`` spies read
stderr; the registry stand-ins are namespaces with the methods each case calls (``getAvailable`` is awaited)."""

from __future__ import annotations

import shutil
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.agent import Agent
from misaka.ai.compat import getModel, streamSimple
from misaka.ai.providers.all import getBuiltinModels, getBuiltinProviders
from misaka.ai.types import Model
from misaka.core.agent_session import AgentSession, AgentSessionConfig
from misaka.core.auth_storage import AuthStorage
from misaka.core.model_resolver import (
    ModelReferenceRequest,
    defaultModelPerProvider,
    findInitialModel,
    resolveCliModel,
    resolveModelReference,
    resolveModelScope,
    resolveModelScopeWithDiagnostics,
)
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import SettingsManager
from tests.model_runtime_test_utils import createModelRegistry, getModelRuntime
from tests.suite_harness import createTestResourceLoader


def _model(**fields: Any) -> Model:
    return Model.model_validate(fields)


# Mock models for testing
mockModels = [
    _model(
        id="claude-sonnet-4-5",
        name="Claude Sonnet 4.5",
        api="anthropic-messages",
        provider="anthropic",
        baseUrl="https://api.anthropic.com",
        reasoning=True,
        input=["text", "image"],
        cost={"input": 3, "output": 15, "cacheRead": 0.3, "cacheWrite": 3.75},
        contextWindow=200000,
        maxTokens=8192,
    ),
    _model(
        id="gpt-4o",
        name="GPT-4o",
        api="anthropic-messages",  # Using same type for simplicity
        provider="openai",
        baseUrl="https://api.openai.com",
        reasoning=False,
        input=["text", "image"],
        cost={"input": 5, "output": 15, "cacheRead": 0.5, "cacheWrite": 5},
        contextWindow=128000,
        maxTokens=4096,
    ),
]

# Mock OpenRouter models with colons in IDs
mockOpenRouterModels = [
    _model(
        id="qwen/qwen3-coder:exacto",
        name="Qwen3 Coder Exacto",
        api="anthropic-messages",
        provider="openrouter",
        baseUrl="https://openrouter.ai/api/v1",
        reasoning=True,
        input=["text"],
        cost={"input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 1},
        contextWindow=128000,
        maxTokens=8192,
    ),
    _model(
        id="openai/gpt-4o:extended",
        name="GPT-4o Extended",
        api="anthropic-messages",
        provider="openrouter",
        baseUrl="https://openrouter.ai/api/v1",
        reasoning=False,
        input=["text", "image"],
        cost={"input": 5, "output": 15, "cacheRead": 0.5, "cacheWrite": 5},
        contextWindow=128000,
        maxTokens=4096,
    ),
]

allModels = [*mockModels, *mockOpenRouterModels]

THINKING_LEVELS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"]


def _registry(**methods: Any) -> SimpleNamespace:
    return SimpleNamespace(**methods)


def _available(models: list[Model]):
    async def getAvailable(*_args: Any) -> list[Model]:
        return models

    return getAvailable


def referenceModel(provider: str, id: str) -> Model:
    return _model(
        id=id,
        name=id,
        api="openai-completions",
        provider=provider,
        baseUrl="https://example.invalid",
        reasoning=True,
        input=["text"],
        cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        contextWindow=128000,
        maxTokens=8192,
    )


referenceCatalog = [
    referenceModel("anthropic", "claude-sonnet-4-5"),
    referenceModel("anthropic", "claude-sonnet-4-5-20250929"),
    referenceModel("anthropic", "claude-opus-4-8"),
    referenceModel("anthropic", "exact"),
    # Fuzzy-matches "shared"; an ambiguous exact "shared" must not fall through to it.
    referenceModel("anthropic", "shared-pro"),
    referenceModel("openai", "gpt-4o"),
    referenceModel("openrouter", "qwen/qwen3-coder:exacto"),
    referenceModel("openrouter", "openai/gpt-4o:extended"),
    referenceModel("gw-a", "shared"),
    referenceModel("gw-a", "anthropic/future"),
    referenceModel("gw-a", "anthropic/exact"),
    referenceModel("gw-a", "anthropic/claude-opus-4.8"),
    referenceModel("gw-a", "anthropic/claude-opus"),
    referenceModel("gw-b", "shared"),
    referenceModel("gw-b", "anthropic/future"),
    referenceModel("gw-b", "claude-opus-4-8"),
    referenceModel("gw-b", "gw-b/free"),
    referenceModel("gw-b", "zz:free"),
]

# (name, text, expected, options): `expected` is {"model", "thinkingLevel"?, "warning"?}, {"ambiguous", "usableCount"}
# or "not-found"; `options` holds `usable`, `provider`, `thinkingSuffix` and `fuzzy`.
referenceCases: list[tuple[str, str, Any, dict[str, Any]]] = [
    # Reference syntax
    ("exact id", "claude-sonnet-4-5", {"model": "anthropic/claude-sonnet-4-5"}, {}),
    ("exact id is case-insensitive", "GPT-4O", {"model": "openai/gpt-4o"}, {}),
    ("fuzzy prefers aliases over dated ids", "sonnet", {"model": "anthropic/claude-sonnet-4-5"}, {}),
    ("thinking suffix", "sonnet:high", {"model": "anthropic/claude-sonnet-4-5", "thinkingLevel": "high"}, {}),
    (
        "colon in a literal id is not a thinking suffix",
        "qwen/qwen3-coder:exacto",
        {"model": "openrouter/qwen/qwen3-coder:exacto"},
        {},
    ),
    (
        "provider prefix, colon id, and thinking suffix",
        "openrouter/qwen/qwen3-coder:exacto:high",
        {"model": "openrouter/qwen/qwen3-coder:exacto", "thinkingLevel": "high"},
        {},
    ),
    (
        "raw id wins when the provider prefix names no matching model",
        "openai/gpt-4o:extended",
        {"model": "openrouter/openai/gpt-4o:extended"},
        {},
    ),
    ("fuzzy searches the prefixed provider first", "openrouter/qwen", {"model": "openrouter/qwen/qwen3-coder:exacto"}, {}),
    ("strict keeps an invalid suffix in the id", "gpt-4o:random", "not-found", {}),
    (
        "lenient drops an invalid suffix with a warning",
        "gpt-4o:random",
        {"model": "openai/gpt-4o", "warning": 'Invalid thinking level "random"'},
        {"thinkingSuffix": "lenient"},
    ),
    (
        "lenient invalid outer suffix discards the inner thinking level",
        "qwen/qwen3-coder:exacto:high:random",
        {"model": "openrouter/qwen/qwen3-coder:exacto", "warning": 'Invalid thinking level "random"'},
        {"thinkingSuffix": "lenient"},
    ),
    ("none keeps the suffix in the id", "gpt-4o:high", "not-found", {"thinkingSuffix": "none"}),
    ("exact-only skips fuzzy matches", "sonnet", "not-found", {"fuzzy": False}),
    ("empty text", "  ", "not-found", {}),
    # Exact matches across providers
    # #7327
    ("sole usable provider wins", "shared", {"model": "gw-b/shared"}, {"usable": ["gw-b"]}),
    (
        "no usable provider is ambiguous instead of falling through to fuzzy",
        "shared",
        {"ambiguous": ["gw-a/shared", "gw-b/shared"], "usableCount": 0},
        {},
    ),
    (
        "several usable providers are ambiguous",
        "shared",
        {"ambiguous": ["gw-a/shared", "gw-b/shared"], "usableCount": 2},
        {"usable": ["gw-a", "gw-b"]},
    ),
    (
        "thinking suffix does not change provider selection",
        "shared:high",
        {"model": "gw-b/shared", "thinkingLevel": "high"},
        {"usable": ["gw-b"]},
    ),
    (
        "thinking suffix does not bypass ambiguity",
        "shared:high",
        {"ambiguous": ["gw-a/shared", "gw-b/shared"], "usableCount": 2},
        {"usable": ["gw-a", "gw-b"]},
    ),
    # #10160
    (
        "provider prefix without a direct model selects the usable raw id",
        "anthropic/future",
        {"model": "gw-b/anthropic/future"},
        {"usable": ["gw-b"]},
    ),
    # #5823
    (
        "usable qualified match beats a usable raw id",
        "anthropic/exact",
        {"model": "anthropic/exact"},
        {"usable": ["anthropic", "gw-a"]},
    ),
    # #5643
    (
        "usable raw id beats an unusable qualified match",
        "anthropic/exact",
        {"model": "gw-a/anthropic/exact"},
        {"usable": ["gw-a"]},
    ),
    ("unusable qualified match is kept when nothing is usable", "anthropic/exact", {"model": "anthropic/exact"}, {}),
    # Like `mistral/codestral` -> mistral/codestral-latest despite gateway id `mistral/codestral`
    (
        "named provider's usable fuzzy match beats a raw id elsewhere",
        "anthropic/claude-opus",
        {"model": "anthropic/claude-opus-4-8"},
        {"usable": ["anthropic"]},
    ),
    (
        "usable raw id beats the named provider's unusable fuzzy match",
        "anthropic/claude-opus",
        {"model": "gw-a/anthropic/claude-opus"},
        {"usable": ["gw-a"]},
    ),
    (
        "named provider's fuzzy match is kept when nothing is usable",
        "anthropic/claude-opus",
        {"model": "anthropic/claude-opus-4-8"},
        {},
    ),
    ("named provider's literal id beats its fuzzy matches", "gw-b/free", {"model": "gw-b/gw-b/free"}, {}),
    # Fuzzy matches across providers
    ("fuzzy prefers usable providers", "opus", {"model": "gw-a/anthropic/claude-opus-4.8"}, {"usable": ["gw-a"]}),
    (
        "fuzzy id tie across usable providers is ambiguous",
        "opus",
        {"ambiguous": ["anthropic/claude-opus-4-8", "gw-b/claude-opus-4-8"], "usableCount": 2},
        {"usable": ["anthropic", "gw-a", "gw-b"]},
    ),
    (
        "fuzzy id tie resolves to the sole usable provider",
        "opus",
        {"model": "anthropic/claude-opus-4-8"},
        {"usable": ["anthropic"]},
    ),
    # Explicit provider constraint
    (
        "explicit provider ignores usable providers elsewhere",
        "shared",
        {"model": "gw-a/shared"},
        {"provider": "gw-a", "usable": ["gw-b"]},
    ),
    ("explicit provider accepts its own prefix", "GW-B/shared", {"model": "gw-b/shared"}, {"provider": "gw-b"}),
    ("explicit provider allows fuzzy matches", "4o", {"model": "openai/gpt-4o"}, {"provider": "openai"}),
    ("explicit provider never leaves the provider", "gpt-4o", "not-found", {"provider": "gw-a"}),
]


def modelRef(m: Model) -> str:
    return f"{m.provider}/{m.id}"


# resolveModelReference


@pytest.mark.parametrize(("name", "text", "expected", "options"), referenceCases, ids=[case[0] for case in referenceCases])
def test_resolve_model_reference(name: str, text: str, expected: Any, options: dict[str, Any]):
    usable = set(options.get("usable", []))
    request = ModelReferenceRequest(
        text=text,
        provider=options.get("provider"),
        thinkingSuffix=options.get("thinkingSuffix", "strict"),
        fuzzy=options.get("fuzzy", True),
    )
    # Catalog order must never decide the result.
    for models in (referenceCatalog, list(reversed(referenceCatalog))):
        result = resolveModelReference(request, models, lambda provider: provider in usable)
        if expected == "not-found":
            assert result.status == "not-found"
        elif "ambiguous" in expected:
            assert result.status == "ambiguous"
            assert sorted(map(modelRef, result.candidates)) == expected["ambiguous"]
            assert result.usableCount == expected["usableCount"]
        else:
            assert result.status == "resolved"
            assert modelRef(result.model) == expected["model"]
            assert result.thinkingLevel == expected.get("thinkingLevel")
            if expected.get("warning"):
                assert expected["warning"] in result.warning
            else:
                assert result.warning is None


# resolveModelScopeWithDiagnostics


async def test_returns_scoped_models_and_structured_diagnostics_without_writing_console_warnings(capsys):
    registry = _registry(getAvailable=_available(allModels))

    result = await resolveModelScopeWithDiagnostics(["sonnet:high", "gpt-4o:invalid", "missing"], registry)

    assert [scoped.model.id for scoped in result.scopedModels] == ["claude-sonnet-4-5", "gpt-4o"]
    assert result.scopedModels[0].thinkingLevel == "high"
    assert result.scopedModels[1].thinkingLevel is None
    assert [(d.type, d.message, d.code, d.pattern) for d in result.diagnostics] == [
        (
            "warning",
            'Invalid thinking level "invalid" in pattern "gpt-4o:invalid". Using default instead.',
            "invalid-thinking-level",
            "gpt-4o:invalid",
        ),
        ("warning", 'No models match pattern "missing"', "no-match", "missing"),
    ]
    assert capsys.readouterr().err == ""


async def test_resolve_model_scope_preserves_cli_warning_output(capsys):
    registry = _registry(getAvailable=_available(allModels))

    scopedModels = await resolveModelScope(["missing"], registry)

    assert scopedModels == []
    warnings = capsys.readouterr().err.splitlines()
    assert len(warnings) == 1
    assert 'Warning: No models match pattern "missing"' in warnings[0]


async def test_adds_every_provider_for_a_pattern_matching_the_same_id_on_several_providers_with_a_warning():
    registry = _registry(getAvailable=_available([
        *allModels,
        mockModels[1].model_copy(update={"provider": "azure"}),
        mockModels[1].model_copy(update={"provider": "openai-codex", "id": "gpt-4o-mini"}),
    ]))

    for pattern, thinkingLevel in (("gpt-4o:high", "high"), ("GPT-4o", None)):
        result = await resolveModelScopeWithDiagnostics([pattern], registry)

        assert sorted(
            (f"{scoped.model.provider}/{scoped.model.id}", scoped.thinkingLevel) for scoped in result.scopedModels
        ) == [("azure/gpt-4o", thinkingLevel), ("openai/gpt-4o", thinkingLevel)]
        assert [(d.type, d.code, d.message, d.pattern) for d in result.diagnostics] == [(
            "warning",
            "ambiguous",
            f'Pattern "{pattern}" matches several providers: azure, openai. Use provider/id to choose one.',
            pattern,
        )]


def _bracketedModel() -> Model:
    return _model(
        id="bracketed-model[1m]",
        name="Bracketed Model",
        api="anthropic-messages",
        provider="custom",
        baseUrl="https://example.invalid",
        reasoning=True,
        input=["text"],
        cost={"input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 1},
        contextWindow=128000,
        maxTokens=8192,
    )


async def test_resolves_bracketed_model_ids_as_exact_references_before_glob_matching():
    registry = _registry(getAvailable=_available([*allModels, _bracketedModel()]))

    result = await resolveModelScopeWithDiagnostics(["custom/bracketed-model[1m]"], registry)

    assert [scoped.model.id for scoped in result.scopedModels] == ["bracketed-model[1m]"]
    assert result.diagnostics == []


async def test_resolves_bracketed_model_ids_with_thinking_levels_as_exact_references_before_glob_matching():
    registry = _registry(getAvailable=_available([*allModels, _bracketedModel()]))

    result = await resolveModelScopeWithDiagnostics(["custom/bracketed-model[1m]:high"], registry)

    assert [scoped.model.id for scoped in result.scopedModels] == ["bracketed-model[1m]"]
    assert result.scopedModels[0].thinkingLevel == "high"
    assert result.diagnostics == []


# resolveCliModel


def test_does_not_strip_invalid_suffix_as_thinking_level_in_model_treat_as_raw_id():
    registry = _registry(getModels=lambda: allModels)

    result = resolveCliModel({"cliProvider": "openai", "cliModel": "gpt-4o:extended", "modelRuntime": registry})

    assert result.error is None
    assert result.model.provider == "openai"
    assert result.model.id == "gpt-4o:extended"


def test_allows_custom_model_ids_for_explicit_providers_without_double_prefixing():
    registry = _registry(getModels=lambda: allModels)

    result = resolveCliModel(
        {"cliProvider": "openrouter", "cliModel": "openrouter/openai/ghost-model", "modelRuntime": registry}
    )

    assert result.error is None
    assert result.model.provider == "openrouter"
    assert result.model.id == "openai/ghost-model"


def test_returns_a_clear_error_when_there_are_no_models():
    registry = _registry(getModels=list)

    result = resolveCliModel({"cliProvider": "openai", "cliModel": "gpt-4o", "modelRuntime": registry})

    assert result.model is None
    assert "No models available" in result.error


# resolveCliModel -- duplicate raw ids behind a known provider prefix (#10160)

_direct = mockModels[0].model_copy(update={"id": "claude-sonnet-4-5", "provider": "anthropic"})
_openrouter = mockModels[0].model_copy(update={"id": "anthropic/claude-opus-5.5", "provider": "openrouter"})
_vercel = mockModels[0].model_copy(update={"id": "anthropic/claude-opus-5.5", "provider": "vercel-ai-gateway"})


def _duplicateRegistry(configured: list[str]) -> SimpleNamespace:
    return _registry(
        getModels=lambda: [_direct, _openrouter, _vercel],
        hasConfiguredAuth=lambda provider: provider in configured,
    )


def test_selects_the_authenticated_gateway_with_or_without_a_thinking_suffix():
    for cliModel, thinkingLevel in (("anthropic/claude-opus-5.5", None), ("anthropic/claude-opus-5.5:high", "high")):
        result = resolveCliModel({"cliModel": cliModel, "modelRuntime": _duplicateRegistry(["vercel-ai-gateway"])})

        assert result.error is None
        assert result.model.provider == "vercel-ai-gateway"
        assert result.thinkingLevel == thinkingLevel


def test_reports_ambiguity_instead_of_choosing_by_catalog_order():
    result = resolveCliModel({"cliModel": "anthropic/claude-opus-5.5", "modelRuntime": _duplicateRegistry([])})

    assert result.model is None
    assert "openrouter/anthropic/claude-opus-5.5, vercel-ai-gateway/anthropic/claude-opus-5.5" in result.error
    assert "No matching provider is authenticated. Use --provider or provider/model." in result.error


def test_does_not_let_stored_credentials_choose_the_provider_for_api_key():
    result = resolveCliModel({
        "cliModel": "anthropic/claude-opus-5.5",
        "inlineApiKey": True,
        "modelRuntime": _duplicateRegistry(["vercel-ai-gateway"]),
    })

    assert result.model is None
    assert "--api-key does not choose a provider." in result.error


# resolveCliModel -- custom model fallback with :thinking suffix (#5552)

# Models for a provider that has registered models but the specific model ID
# is not in the registry (triggers buildFallbackModel path).
neuralwattModel = _model(
    id="some-base-model",
    name="Some Base Model",
    api="anthropic-messages",
    provider="neuralwatt",
    baseUrl="https://api.neuralwatt.com",
    reasoning=False,
    input=["text"],
    cost={"input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 1},
    contextWindow=128000,
    maxTokens=8192,
)

modelsWithNeuralwatt = [*allModels, neuralwattModel]


def test_strips_thinking_suffix_from_custom_model_id_in_fallback_path():
    registry = _registry(getModels=lambda: modelsWithNeuralwatt)

    result = resolveCliModel({"cliModel": "neuralwatt/zai-org/GLM-5.1-FP8:high", "modelRuntime": registry})

    assert result.error is None
    assert result.model.provider == "neuralwatt"
    # The :high suffix must NOT leak into the model id sent to the API
    assert result.model.id == "zai-org/GLM-5.1-FP8"
    assert result.model.reasoning is True
    assert result.thinkingLevel == "high"


def test_custom_model_without_thinking_suffix_works_normally_in_fallback_path():
    registry = _registry(getModels=lambda: modelsWithNeuralwatt)

    result = resolveCliModel({"cliModel": "neuralwatt/zai-org/GLM-5.1-FP8", "modelRuntime": registry})

    assert result.error is None
    assert result.model.provider == "neuralwatt"
    assert result.model.id == "zai-org/GLM-5.1-FP8"
    assert result.thinkingLevel is None


def test_all_valid_thinking_levels_work_in_fallback_path():
    registry = _registry(getModels=lambda: modelsWithNeuralwatt)

    for level in THINKING_LEVELS:
        result = resolveCliModel({"cliModel": f"neuralwatt/zai-org/GLM-5.1-FP8:{level}", "modelRuntime": registry})

        assert result.error is None
        assert result.model.id == "zai-org/GLM-5.1-FP8"
        assert result.thinkingLevel == level


def test_invalid_thinking_suffix_on_custom_model_is_treated_as_part_of_model_id():
    registry = _registry(getModels=lambda: modelsWithNeuralwatt)

    result = resolveCliModel({"cliModel": "neuralwatt/zai-org/GLM-5.1-FP8:banana", "modelRuntime": registry})

    assert result.error is None
    assert result.model.provider == "neuralwatt"
    # Invalid suffix stays in the id (it's not a thinking level)
    assert result.model.id == "zai-org/GLM-5.1-FP8:banana"
    assert result.thinkingLevel is None


def test_explicit_provider_with_custom_model_thinking_strips_suffix_correctly():
    registry = _registry(getModels=lambda: modelsWithNeuralwatt)

    result = resolveCliModel(
        {"cliProvider": "neuralwatt", "cliModel": "zai-org/GLM-5.1-FP8:high", "modelRuntime": registry}
    )

    assert result.error is None
    assert result.model.provider == "neuralwatt"
    assert result.model.id == "zai-org/GLM-5.1-FP8"
    assert result.thinkingLevel == "high"


def test_with_explicit_thinking_suffix_is_kept_as_part_of_model_id():
    registry = _registry(getModels=lambda: modelsWithNeuralwatt)

    result = resolveCliModel(
        {"cliModel": "neuralwatt/zai-org/GLM-5.1-FP8:high", "cliThinking": "medium", "modelRuntime": registry}
    )

    assert result.error is None
    assert result.model.provider == "neuralwatt"
    # :high is kept as part of the model id since --thinking was explicit
    assert result.model.id == "zai-org/GLM-5.1-FP8:high"
    assert result.thinkingLevel is None


# default model selection


def test_openai_defaults_track_current_models():
    assert defaultModelPerProvider["openai"] == "gpt-5.5"
    assert defaultModelPerProvider["openai-codex"] == "gpt-6.1-sol"


def test_zai_minimax_cerebras_and_ant_ling_defaults_track_current_models():
    assert defaultModelPerProvider["zai"] == "glm-5.3"
    assert defaultModelPerProvider["zai-coding-cn"] == "glm-5.3"
    assert defaultModelPerProvider["minimax"] == "MiniMax-M2.7"
    assert defaultModelPerProvider["minimax-cn"] == "MiniMax-M2.7"
    assert defaultModelPerProvider["cerebras"] == "gpt-oss-120b"
    assert defaultModelPerProvider["ant-ling"] == "Ring-2.6-1T"


def test_built_in_chat_providers_have_defaults_in_their_generated_catalogs():
    for provider in getBuiltinProviders():
        chatModels = getBuiltinModels(provider)
        defaultId = defaultModelPerProvider.get(provider)
        if len(chatModels) == 0:
            assert defaultId is None, f"{provider} has no chat models and should have no chat default"
            continue
        assert any(model.id == defaultId for model in chatModels), (
            f"{provider} default {defaultId} should exist in its generated catalog"
        )


def test_ai_gateway_default_tracks_current_model():
    assert defaultModelPerProvider["vercel-ai-gateway"] == "zai/glm-5.1"


def test_xai_default_tracks_current_model():
    assert defaultModelPerProvider["xai"] == "grok-4.7"


def test_qwen_token_plan_individual_default_tracks_current_model():
    assert defaultModelPerProvider["qwen-token-plan-individual"] == "qwen3.8-max"


async def test_find_initial_model_selects_ai_gateway_default_when_available():
    aiGatewayModel = _model(
        id="anthropic/claude-opus-4-6",
        name="Claude Opus 4.6",
        api="anthropic-messages",
        provider="vercel-ai-gateway",
        baseUrl="https://ai-gateway.vercel.sh",
        reasoning=True,
        input=["text", "image"],
        cost={"input": 5, "output": 15, "cacheRead": 0.5, "cacheWrite": 5},
        contextWindow=200000,
        maxTokens=8192,
    )

    registry = _registry(getAvailableSnapshot=lambda: [aiGatewayModel])

    result = await findInitialModel({"scopedModels": [], "isContinuing": False, "modelRuntime": registry})

    assert result.model.provider == "vercel-ai-gateway"
    assert result.model.id == "anthropic/claude-opus-4-6"


async def test_find_initial_model_ignores_an_unauthenticated_saved_default():
    savedDeepSeekModel = _model(
        id="deepseek-v4-flash",
        name="DeepSeek V4 Flash",
        api="anthropic-messages",
        provider="deepseek",
        baseUrl="https://api.deepseek.com",
        reasoning=True,
        input=["text"],
        cost={"input": 1, "output": 2, "cacheRead": 0.1, "cacheWrite": 1},
        contextWindow=128000,
        maxTokens=8192,
    )
    localDeepSeekModel = savedDeepSeekModel.model_copy(
        update={"provider": "spark-two", "baseUrl": "http://spark-two:8000/v1"}
    )
    registry = _registry(
        getModel=lambda provider, modelId: (
            savedDeepSeekModel
            if provider == savedDeepSeekModel.provider and modelId == savedDeepSeekModel.id
            else None
        ),
        hasConfiguredAuth=lambda provider: provider == "spark-two",
        getAvailableSnapshot=lambda: [localDeepSeekModel],
    )

    result = await findInitialModel({
        "scopedModels": [],
        "isContinuing": False,
        "defaultProvider": "deepseek",
        "defaultModelId": "deepseek-v4-flash",
        "modelRuntime": registry,
    })

    assert result.model.provider == "spark-two"
    assert result.model.id == "deepseek-v4-flash"


# default model selection -- persisted default model scoping

sonnet = getModel("anthropic", "claude-sonnet-4-5")
opus = getModel("anthropic", "claude-opus-4-8")


@pytest.fixture
def tempDirs():
    dirs: list[str] = []
    yield dirs
    for directory in dirs:
        shutil.rmtree(directory, ignore_errors=True)


async def _createSession(tempDirs: list[str], *, scoped: bool, persistedScope: list[str] | None = None):
    tempDir = tempfile.mkdtemp(prefix="pi-default-scope-")
    tempDirs.append(tempDir)

    settingsManager = SettingsManager.create(tempDir, tempDir)
    if persistedScope:
        settingsManager.setEnabledModels(persistedScope)

    authStorage = AuthStorage.inMemory({"anthropic": {"type": "api_key", "key": "test-key"}})
    modelRuntime = getModelRuntime(await createModelRegistry(authStorage, f"{tempDir}/models.json"))
    agent = Agent(initialState={"model": sonnet, "systemPrompt": "test", "tools": []}, streamFn=streamSimple)
    session = AgentSession(AgentSessionConfig(
        agent=agent,
        sessionManager=SessionManager.inMemory(tempDir),
        settingsManager=settingsManager,
        cwd=tempDir,
        modelRuntime=modelRuntime,
        resourceLoader=createTestResourceLoader(),
        scopedModels=[{"model": sonnet}] if scoped else [],
    ))

    return session, settingsManager


def _references(session: AgentSession) -> list[str]:
    return [f"{scoped['model'].provider}/{scoped['model'].id}" for scoped in session.scopedModels]


async def test_adds_a_persisted_default_to_an_existing_scoped_model_list(tempDirs):
    session, settingsManager = await _createSession(
        tempDirs, scoped=True, persistedScope=[f"{sonnet.provider}/{sonnet.id}"]
    )

    await session.setModel(opus, {"persist": True})

    assert settingsManager.getDefaultProvider() == opus.provider
    assert settingsManager.getDefaultModel() == opus.id
    assert _references(session) == [f"{sonnet.provider}/{sonnet.id}", f"{opus.provider}/{opus.id}"]
    assert settingsManager.getEnabledModels() == [f"{sonnet.provider}/{sonnet.id}", f"{opus.provider}/{opus.id}"]


async def test_does_not_create_a_scoped_model_list_when_all_models_are_available(tempDirs):
    session, settingsManager = await _createSession(tempDirs, scoped=False)

    await session.setModel(opus, {"persist": True})

    assert session.scopedModels == []
    assert settingsManager.getEnabledModels() is None


async def test_keeps_session_only_model_changes_out_of_scope(tempDirs):
    session, settingsManager = await _createSession(
        tempDirs, scoped=True, persistedScope=[f"{sonnet.provider}/{sonnet.id}"]
    )

    await session.setModel(opus, {"persist": False})

    assert _references(session) == [f"{sonnet.provider}/{sonnet.id}"]
    assert settingsManager.getEnabledModels() == [f"{sonnet.provider}/{sonnet.id}"]


def test_is_alias_reads_a_dated_suffix_as_javascript_does():
    # `/-\d{8}$/`: ASCII digits only, and no match before a trailing newline.
    from misaka.core.model_resolver import isAlias

    assert not isAlias("claude-sonnet-4-5-20250929")
    assert isAlias("claude-sonnet-4-5")
    assert isAlias("claude-latest-20250929-latest")
    assert isAlias("model-٢٠٢٥٠٩٢٩")  # Arabic-Indic digits are not `\d` in JavaScript
    assert isAlias("model-20250929\n")
