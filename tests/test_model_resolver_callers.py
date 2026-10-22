"""MISAKA's callers of the model resolver after pi 1.3's one provider-selection policy (#10913, #10160).

What changed for them: a bare or fuzzy ``--model`` that matches the same id on several authenticated providers is an
error instead of the catalog's first; ``--models``/``enabledModels`` add every provider's model and warn; ``--api-key``
never lets stored credentials choose. What did not: role pins, Sister cards (canonical ``provider/id``), the setup
wizard, sub-agent children (``--provider`` with ``--model``) and the LCM auxiliary model (provider-constrained).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.types import Model
from misaka.cli.args import Args
from misaka.cli.engine import buildSessionOptions
from misaka.config import profiles
from misaka.core.model_resolver import (
    findExactModelReferenceMatch,
    resolveCliModel,
    resolveModelScopeFromModels,
)
from misaka.core.settings_manager import SettingsManager


def _model(provider: str, id: str) -> Model:
    return Model.model_validate({
        "id": id, "name": id, "api": "openai-completions", "provider": provider, "baseUrl": "https://example.invalid",
        "reasoning": True, "input": ["text"], "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 128000, "maxTokens": 8192,
    })


CATALOG = [
    _model("vendor-a", "shared"),
    _model("vendor-b", "shared"),
    _model("vendor-a", "unique"),
    _model("vendor-b", "org/nested"),
    _model("zai", "glm-5"),
    _model("gateway", "zai/glm-5"),
    _model("openrouter", "qwen/qwen3-coder:exacto"),
]


class _Runtime:
    def __init__(self, authenticated: set[str]):
        self.authenticated = authenticated

    def getModels(self, providerId: str | None = None) -> list[Model]:
        return [model for model in CATALOG if providerId is None or model.provider == providerId]

    def getModel(self, provider: str, modelId: str) -> Model | None:
        return next((m for m in CATALOG if (m.provider, m.id) == (provider, modelId)), None)

    def hasConfiguredAuth(self, provider: str) -> bool:
        return provider in self.authenticated


def _session_options(args: Args, authenticated: set[str]) -> Any:
    return buildSessionOptions(args, [], False, _Runtime(authenticated), SettingsManager.inMemory())


def _errors(result: Any) -> list[str]:
    return [diagnostic.message for diagnostic in result.diagnostics if diagnostic.type == "error"]


# `misaka chat --model`, `misaka dm --model` and a foreground child's `--model` (cli/engine.buildSessionOptions)


def test_a_bare_model_on_two_authenticated_providers_is_now_an_error():
    result = _session_options(Args(model="shared"), {"vendor-a", "vendor-b"})

    assert "model" not in result.options
    assert _errors(result) == [(
        'Model "shared" is ambiguous across providers: vendor-a/shared, vendor-b/shared. '
        "More than one matching provider is authenticated. Use --provider or provider/model."
    )]


def test_a_bare_model_resolves_to_the_only_authenticated_provider():
    result = _session_options(Args(model="shared:high"), {"vendor-b"})

    assert _errors(result) == []
    assert (result.options["model"].provider, result.options["model"].id) == ("vendor-b", "shared")
    assert result.options["thinkingLevel"] == "high"


def test_an_inline_api_key_never_lets_stored_credentials_choose():
    result = _session_options(Args(model="shared", apiKey="sk-inline"), {"vendor-b"})

    assert "model" not in result.options
    assert "--api-key does not choose a provider." in _errors(result)[0]


@pytest.mark.parametrize(("args", "expected"), [
    # A Sister card's canonical reference (worker.py) and a sub-agent child's argv (subagent/runtime.py).
    (Args(model="vendor-b/org/nested"), ("vendor-b", "org/nested")),
    (Args(provider="vendor-a", model="shared"), ("vendor-a", "shared")),
    (Args(provider="vendor-b", model="org/nested"), ("vendor-b", "org/nested")),
    # `provider/id` with a matching gateway id: the named provider, as before.
    (Args(model="zai/glm-5"), ("zai", "glm-5")),
    (Args(model="openrouter/qwen/qwen3-coder:exacto:low"), ("openrouter", "qwen/qwen3-coder:exacto")),
])
def test_canonical_and_provider_constrained_models_are_unaffected(args, expected):
    result = _session_options(args, {"vendor-a", "vendor-b", "zai", "gateway", "openrouter"})

    assert _errors(result) == []
    assert (result.options["model"].provider, result.options["model"].id) == expected


def test_the_not_found_error_names_the_model_as_typed():
    result = _session_options(Args(model="nothing-like-it"), {"vendor-a"})
    assert _errors(result) == ['Model "nothing-like-it" not found. Use --list-models to see available models.']


# `enabledModels` / `--models` (cli/engine.py, interactive /scoped-models)


def test_scope_patterns_add_every_provider_of_an_ambiguous_id_and_warn():
    result = resolveModelScopeFromModels(["shared", "vendor-a/unique"], CATALOG)

    assert [(scoped.model.provider, scoped.model.id) for scoped in result.scopedModels] == [
        ("vendor-a", "shared"), ("vendor-b", "shared"), ("vendor-a", "unique"),
    ]
    assert [(d.code, d.message) for d in result.diagnostics] == [(
        "ambiguous",
        'Pattern "shared" matches several providers: vendor-a, vendor-b. Use provider/id to choose one.',
    )]


# Role pins (config/profiles.resolve_model_reference), the setup wizard and `/model <ref>` (findExactModelReferenceMatch)


@pytest.mark.parametrize(("reference", "expected"), [
    ("vendor-a/shared", ("vendor-a", "shared")),
    ("unique", ("vendor-a", "unique")),
    ("org/nested", ("vendor-b", "org/nested")),
    ("vendor-b/org/nested", ("vendor-b", "org/nested")),
    ("zai/glm-5", ("zai", "glm-5")),
    ("gateway/zai/glm-5", ("gateway", "zai/glm-5")),
    ("shared", None),
    ("unique:high", None),
    ("uniq", None),
])
def test_exact_reference_lookup_is_unchanged(reference, expected):
    for models in (CATALOG, list(reversed(CATALOG))):
        found = findExactModelReferenceMatch(reference, models)
        assert (None if found is None else (found.provider, found.id)) == expected


def test_a_role_pin_keeps_misakas_own_ambiguity_and_unknown_errors():
    runtime = _Runtime(set())
    with pytest.raises(ValueError, match="Ambiguous model 'shared'; choose provider/model: vendor-a/shared, vendor-b/shared"):
        profiles.resolve_model_reference("shared", runtime)
    assert profiles.resolve_model_reference("shared", runtime, fallback_provider="vendor-b") == "vendor-b/shared"
    with pytest.raises(ValueError) as unknown:
        profiles.resolve_model_reference("vendor-a/gone", runtime)
    assert "OpenRouter" not in str(unknown.value)


def test_an_openrouter_pin_missing_from_the_keys_list_names_the_keys_restrictions():
    # pi 1.2 narrows OpenRouter's catalog to what the user's key may use (model_runtime.withPersonalizedCatalog).
    with pytest.raises(ValueError) as unknown:
        profiles.resolve_model_reference("openrouter/anthropic/claude-opus-4.8", _Runtime({"openrouter"}))
    message = str(unknown.value)
    assert message.startswith("Unknown model 'openrouter/anthropic/claude-opus-4.8'; choose a registered provider/model.")
    assert "OpenRouter API key may use" in message and "guardrails" in message

    with pytest.raises(ValueError, match="OpenRouter API key"):
        profiles.resolve_model_reference("vanished", _Runtime(set()), fallback_provider="openrouter")


# The LCM auxiliary model and `misaka auth` (provider-constrained resolveCliModel)


def test_a_provider_constrained_unknown_model_is_a_custom_id_with_a_warning():
    result = resolveCliModel({"cliProvider": "openrouter", "cliModel": "vanished/model", "modelRuntime": _Runtime(set())})

    assert result.error is None
    assert (result.model.provider, result.model.id) == ("openrouter", "vanished/model")
    assert result.warning == 'Model "vanished/model" not found for provider "openrouter". Using custom model id.'


# MoA members (core/moa/provider.py)


async def test_an_unavailable_moa_member_names_the_openrouter_keys_restrictions(monkeypatch):
    from misaka.core.moa import provider as moa

    monkeypatch.setattr(moa, "_MODEL_RESOLVER", None)
    label, text, usage, trace = await moa._run_reference(
        {"provider": "openrouter", "model": "gone"}, [], preset={}, options=SimpleNamespace(),
    )
    assert (label, usage, trace) == ("openrouter:gone", None, None)
    assert text.startswith("[failed: model slot is unavailable: openrouter:gone. MISAKA lists only")

    _label, text, _usage, _trace = await moa._run_reference(
        {"provider": "anthropic", "model": "gone"}, [], preset={}, options=SimpleNamespace(),
    )
    assert text == "[failed: model slot is unavailable: anthropic:gone]"
