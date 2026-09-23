"""Model matching, scoping, and initial selection helpers."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Literal

from wcmatch import glob

from misaka.ai.models import models_are_equal
from misaka.ai.types import Model
from misaka.cli.args import isValidThinkingLevel as _isValidThinkingLevel
from misaka.config import home
from misaka.core.defaults import DEFAULT_THINKING_LEVEL
from misaka.utils.values import maybe_await

type ResolvedThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
_MINIMATCH_FLAGS = glob.IGNORECASE | glob.GLOBSTAR | glob.BRACE | glob.EXTMATCH | glob.FORCEUNIX

defaultModelPerProvider: dict[str, str] = {
    "amazon-bedrock": "us.anthropic.claude-opus-4-6-v1",
    "ant-ling": "Ring-2.6-1T",
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-5.5",
    "azure-openai-responses": "gpt-5.4",
    "openai-codex": "gpt-5.5",
    "radius": "auto",
    "nvidia": "nvidia/nemotron-3-super-120b-a12b",
    "deepseek": "deepseek-v4-pro",
    "google": "gemini-3.1-pro-preview",
    "google-vertex": "gemini-3.1-pro-preview",
    "github-copilot": "gpt-5.4",
    "openrouter": "moonshotai/kimi-k2.6",
    "vercel-ai-gateway": "zai/glm-5.1",
    "xai": "grok-4.6",
    "groq": "openai/gpt-oss-120b",
    "cerebras": "gpt-oss-120b",
    "zai": "glm-5.3",
    "zai-coding-cn": "glm-5.3",
    "mistral": "devstral-medium-latest",
    "minimax": "MiniMax-M2.7",
    "minimax-cn": "MiniMax-M2.7",
    "moonshotai": "kimi-k2.6",
    "moonshotai-cn": "kimi-k2.6",
    "huggingface": "moonshotai/Kimi-K2.6",
    "fireworks": "accounts/fireworks/models/kimi-k2p6",
    "together": "moonshotai/Kimi-K2.6",
    "baseten": "zai-org/GLM-5.2",
    "opencode": "kimi-k2.6",
    "opencode-go": "kimi-k2.6",
    "kimi-coding": "kimi-for-coding",
    "cloudflare-workers-ai": "@cf/moonshotai/kimi-k2.6",
    "cloudflare-ai-gateway": "workers-ai/@cf/moonshotai/kimi-k2.6",
    "qwen-token-plan": "qwen3.7-max",
    "qwen-token-plan-cn": "qwen3.7-max",
    "qwen-token-plan-individual": "qwen3.8-max",
    "xiaomi": "mimo-v2.5-pro",
    "xiaomi-token-plan-cn": "mimo-v2.5-pro",
    "xiaomi-token-plan-ams": "mimo-v2.5-pro",
    "xiaomi-token-plan-sgp": "mimo-v2.5-pro",
}


@dataclass(slots=True)
class ScopedModel:
    model: Model
    thinkingLevel: ResolvedThinkingLevel | None = None


@dataclass(slots=True)
class ModelScopeDiagnostic:
    code: Literal["no-match", "invalid-thinking-level"]
    message: str
    pattern: str
    type: Literal["warning"] = "warning"


@dataclass(slots=True)
class ResolveModelScopeResult:
    scopedModels: list[ScopedModel]
    diagnostics: list[ModelScopeDiagnostic]


@dataclass(slots=True)
class ParsedModelResult:
    model: Model | None
    thinkingLevel: ResolvedThinkingLevel | None
    warning: str | None


@dataclass(slots=True)
class ResolveCliModelResult:
    model: Model | None
    warning: str | None
    error: str | None
    thinkingLevel: ResolvedThinkingLevel | None = None


@dataclass(slots=True)
class InitialModelResult:
    model: Model | None
    thinkingLevel: ResolvedThinkingLevel
    fallbackMessage: str | None


def _color(message: str, code: str) -> str:
    return f"\x1b[{code}m{message}\x1b[0m"


def _dim(message: str) -> str:
    return _color(message, "2")


def _yellow(message: str) -> str:
    return _color(message, "33")


def _red(message: str) -> str:
    return _color(message, "31")


def _minimatch(value: str, pattern: str) -> bool:
    return glob.globmatch(value, pattern, flags=_MINIMATCH_FLAGS)


def _isAlias(model_id: str) -> bool:
    if model_id.endswith("-latest"):
        return True
    return not bool(re.search(r"-\d{8}$", model_id))


def findExactModelReferenceMatch(modelReference: str, availableModels: list[Model]) -> Model | None:
    trimmed_reference = modelReference.strip()
    if not trimmed_reference:
        return None

    normalized_reference = trimmed_reference.lower()
    canonical_matches = [
        model for model in availableModels if f"{model.provider}/{model.id}".lower() == normalized_reference
    ]
    if len(canonical_matches) == 1:
        return canonical_matches[0]
    if len(canonical_matches) > 1:
        return None

    slash_index = trimmed_reference.find("/")
    if slash_index != -1:
        provider = trimmed_reference[:slash_index].strip()
        model_id = trimmed_reference[slash_index + 1 :].strip()
        if provider and model_id:
            provider_matches = [
                model
                for model in availableModels
                if model.provider.lower() == provider.lower() and model.id.lower() == model_id.lower()
            ]
            if len(provider_matches) == 1:
                return provider_matches[0]
            if len(provider_matches) > 1:
                return None

    id_matches = [model for model in availableModels if model.id.lower() == normalized_reference]
    return id_matches[0] if len(id_matches) == 1 else None


def _tryMatchModel(modelPattern: str, availableModels: list[Model]) -> Model | None:
    exact_match = findExactModelReferenceMatch(modelPattern, availableModels)
    if exact_match is not None:
        return exact_match

    lowered = modelPattern.lower()
    matches = [
        model
        for model in availableModels
        if lowered in model.id.lower() or lowered in (model.name or "").lower()
    ]
    if not matches:
        return None

    aliases = sorted((model for model in matches if _isAlias(model.id)), key=lambda item: item.id, reverse=True)
    if aliases:
        return aliases[0]

    dated_versions = sorted(
        (model for model in matches if not _isAlias(model.id)),
        key=lambda item: item.id,
        reverse=True,
    )
    return dated_versions[0] if dated_versions else None


def _buildFallbackModel(provider: str, modelId: str, availableModels: list[Model]) -> Model | None:
    provider_models = [model for model in availableModels if model.provider == provider]
    if not provider_models:
        return None

    default_id = defaultModelPerProvider.get(provider)
    base_model = next((model for model in provider_models if model.id == default_id), provider_models[0])
    return base_model.model_copy(update={"id": modelId, "name": modelId})


def parseModelPattern(
    pattern: str,
    availableModels: list[Model],
    options: dict[str, Any] | None = None,
) -> ParsedModelResult:
    exact_match = _tryMatchModel(pattern, availableModels)
    if exact_match is not None:
        return ParsedModelResult(model=exact_match, thinkingLevel=None, warning=None)

    last_colon_index = pattern.rfind(":")
    if last_colon_index == -1:
        return ParsedModelResult(model=None, thinkingLevel=None, warning=None)

    prefix = pattern[:last_colon_index]
    suffix = pattern[last_colon_index + 1 :]

    if _isValidThinkingLevel(suffix):
        result = parseModelPattern(prefix, availableModels, options)
        if result.model is not None:
            return ParsedModelResult(
                model=result.model,
                thinkingLevel=None if result.warning else suffix,  # type: ignore[arg-type]
                warning=result.warning,
            )
        return result

    allow_fallback = True if options is None else options.get("allowInvalidThinkingLevelFallback", True)
    if not allow_fallback:
        return ParsedModelResult(model=None, thinkingLevel=None, warning=None)

    result = parseModelPattern(prefix, availableModels, options)
    if result.model is not None:
        return ParsedModelResult(
            model=result.model,
            thinkingLevel=None,
            warning=f'Invalid thinking level "{suffix}" in pattern "{pattern}". Using default instead.',
        )
    return result


def resolveModelScopeFromModels(patterns: list[str], models: list[Model]) -> ResolveModelScopeResult:
    available_models = list(models)
    scoped_models: list[ScopedModel] = []
    diagnostics: list[ModelScopeDiagnostic] = []

    def add(model: Model, thinking_level: ResolvedThinkingLevel | None) -> None:
        if not any(models_are_equal(item.model, model) for item in scoped_models):
            scoped_models.append(ScopedModel(model=model, thinkingLevel=thinking_level))

    def no_match(pattern: str) -> None:
        diagnostics.append(
            ModelScopeDiagnostic(code="no-match", message=f'No models match pattern "{pattern}"', pattern=pattern)
        )

    for pattern in patterns:
        if any(token in pattern for token in ("*", "?", "[")):
            colon_index = pattern.rfind(":")
            glob_pattern = pattern
            thinking_level: ResolvedThinkingLevel | None = None
            if colon_index != -1:
                suffix = pattern[colon_index + 1 :]
                if _isValidThinkingLevel(suffix):
                    thinking_level = suffix  # type: ignore[assignment]
                    glob_pattern = pattern[:colon_index]

            exact_match = findExactModelReferenceMatch(glob_pattern, available_models)
            if exact_match is not None:
                add(exact_match, thinking_level)
                continue

            matching_models = [
                model
                for model in available_models
                if _minimatch(f"{model.provider}/{model.id}".lower(), glob_pattern.lower())
                or _minimatch(model.id.lower(), glob_pattern.lower())
            ]
            if not matching_models:
                no_match(pattern)
                continue
            for model in matching_models:
                add(model, thinking_level)
            continue

        result = parseModelPattern(pattern, available_models)
        if result.warning:
            diagnostics.append(
                ModelScopeDiagnostic(code="invalid-thinking-level", message=result.warning, pattern=pattern)
            )
        if result.model is None:
            no_match(pattern)
            continue
        add(result.model, result.thinkingLevel)

    return ResolveModelScopeResult(scopedModels=scoped_models, diagnostics=diagnostics)


async def resolveModelScopeWithDiagnostics(patterns: list[str], modelRegistry: Any) -> ResolveModelScopeResult:
    return resolveModelScopeFromModels(patterns, list(await maybe_await(modelRegistry.getAvailable())))


async def resolveModelScope(patterns: list[str], modelRegistry: Any) -> list[ScopedModel]:
    result = await resolveModelScopeWithDiagnostics(patterns, modelRegistry)
    for diagnostic in result.diagnostics:
        print(_yellow(f"Warning: {diagnostic.message}"), file=sys.stderr)
    return result.scopedModels


def resolveCliModel(options: dict[str, Any]) -> ResolveCliModelResult:
    cli_provider = options.get("cliProvider")
    cli_model = options.get("cliModel")
    model_registry = options["modelRegistry"]

    if not cli_model:
        return ResolveCliModelResult(model=None, warning=None, error=None)

    available_models = list(model_registry.getAll())
    if not available_models:
        return ResolveCliModelResult(
            model=None,
            warning=None,
            error="No models available. Check your installation or add models to models.json.",
        )

    provider_map = {model.provider.lower(): model.provider for model in available_models}
    provider = provider_map.get(cli_provider.lower()) if cli_provider else None
    if cli_provider and provider is None:
        return ResolveCliModelResult(
            model=None,
            warning=None,
            error=f'Unknown provider "{cli_provider}". Use --list-models to see available providers/models; custom providers are defined in {home.display(home.path("models"))} and the default is defaultProvider in settings.json.',
        )

    pattern = cli_model
    inferred_provider = False
    if provider is None:
        slash_index = cli_model.find("/")
        if slash_index != -1:
            maybe_provider = cli_model[:slash_index]
            canonical = provider_map.get(maybe_provider.lower())
            if canonical:
                provider = canonical
                pattern = cli_model[slash_index + 1 :]
                inferred_provider = True

    if provider is None:
        # pi model-resolver.ts:469-501 -- bare ids exist in several providers; never pick by
        # catalog order, only by uniqueness or the sole authenticated provider.
        lower = cli_model.lower()
        exact_matches = [
            model
            for model in available_models
            if model.id.lower() == lower or f"{model.provider}/{model.id}".lower() == lower
        ]
        if len(exact_matches) == 1:
            return ResolveCliModelResult(model=exact_matches[0], warning=None, error=None)
        if len(exact_matches) > 1:
            authenticated = [model for model in exact_matches if model_registry.hasConfiguredAuth(model)]
            if len(authenticated) == 1:
                return ResolveCliModelResult(model=authenticated[0], warning=None, error=None)
            matches = ", ".join(sorted(f"{model.provider}/{model.id}" for model in exact_matches))
            auth_hint = (
                "No matching provider is authenticated."
                if not authenticated
                else "More than one matching provider is authenticated."
            )
            return ResolveCliModelResult(
                model=None,
                warning=None,
                error=f'Model "{cli_model}" is ambiguous across providers: {matches}. {auth_hint} Use --provider or provider/model.',
            )

    if cli_provider and provider:
        prefix = f"{provider}/"
        if cli_model.lower().startswith(prefix.lower()):
            pattern = cli_model[len(prefix) :]

    candidates = [model for model in available_models if model.provider == provider] if provider else available_models
    parsed = parseModelPattern(pattern, candidates, {"allowInvalidThinkingLevelFallback": False})
    if parsed.model is not None:
        if inferred_provider:
            # pi model-resolver.ts:518-539 -- keep "provider/model" syntax unless that provider is
            # unauthenticated and the whole string is the raw id of the one authenticated provider.
            lower = cli_model.lower()
            raw_matches = [
                model
                for model in available_models
                if model.id.lower() == lower and not models_are_equal(model, parsed.model)
            ]
            if raw_matches and not model_registry.hasConfiguredAuth(parsed.model):
                authenticated = [model for model in raw_matches if model_registry.hasConfiguredAuth(model)]
                if len(authenticated) == 1:
                    return ResolveCliModelResult(model=authenticated[0], warning=None, error=None)
        return ResolveCliModelResult(
            model=parsed.model,
            thinkingLevel=parsed.thinkingLevel,
            warning=parsed.warning,
            error=None,
        )

    if inferred_provider:
        lower = cli_model.lower()
        exact = next(
            (
                model
                for model in available_models
                if model.id.lower() == lower or f"{model.provider}/{model.id}".lower() == lower
            ),
            None,
        )
        if exact is not None:
            return ResolveCliModelResult(model=exact, warning=None, error=None, thinkingLevel=None)
        fallback = parseModelPattern(cli_model, available_models, {"allowInvalidThinkingLevelFallback": False})
        if fallback.model is not None:
            return ResolveCliModelResult(
                model=fallback.model,
                warning=fallback.warning,
                error=None,
                thinkingLevel=fallback.thinkingLevel,
            )

    if provider:
        # pi model-resolver.ts:569-595 -- a valid ":level" suffix belongs to the thinking level,
        # not to the custom model id, unless --thinking was given explicitly.
        cli_thinking = options.get("cliThinking")
        fallback_pattern, fallback_thinking = pattern, None
        last_colon = pattern.rfind(":")
        if not cli_thinking and last_colon != -1 and _isValidThinkingLevel(pattern[last_colon + 1 :]):
            fallback_pattern, fallback_thinking = pattern[:last_colon], pattern[last_colon + 1 :]
        fallback_model = _buildFallbackModel(provider, fallback_pattern, available_models)
        if fallback_model is not None:
            requested_thinking = cli_thinking or fallback_thinking
            if requested_thinking and requested_thinking != "off":
                fallback_model = fallback_model.model_copy(update={"reasoning": True})
            not_found = f'Model "{fallback_pattern}" not found for provider "{provider}". Using custom model id.'
            warning = f"{parsed.warning} {not_found}" if parsed.warning else not_found
            return ResolveCliModelResult(
                model=fallback_model, thinkingLevel=fallback_thinking, warning=warning, error=None
            )

    display = f"{provider}/{pattern}" if provider else cli_model
    return ResolveCliModelResult(
        model=None,
        thinkingLevel=None,
        warning=parsed.warning,
        error=f'Model "{display}" not found. Use --list-models to see available models.',
    )


async def findInitialModel(options: dict[str, Any]) -> InitialModelResult:
    cli_provider = options.get("cliProvider")
    cli_model = options.get("cliModel")
    scoped_models: list[ScopedModel] = options.get("scopedModels", [])
    is_continuing = options.get("isContinuing", False)
    default_provider = options.get("defaultProvider")
    default_model_id = options.get("defaultModelId")
    default_thinking_level = options.get("defaultThinkingLevel")
    model_thinking_levels: dict[str, ResolvedThinkingLevel] = options.get("modelThinkingLevels") or {}
    model_registry = options["modelRegistry"]

    if cli_provider and cli_model:
        resolved = resolveCliModel(
            {
                "cliProvider": cli_provider,
                "cliModel": cli_model,
                "modelRegistry": model_registry,
            }
        )
        if resolved.error:
            print(_red(resolved.error), file=sys.stderr)
            raise SystemExit(1)
        if resolved.model is not None:
            return InitialModelResult(
                model=resolved.model,
                thinkingLevel=DEFAULT_THINKING_LEVEL,
                fallbackMessage=None,
            )

    if scoped_models and not is_continuing:
        scoped_model = scoped_models[0]
        per_model = model_thinking_levels.get(f"{scoped_model.model.provider}/{scoped_model.model.id}")
        return InitialModelResult(
            model=scoped_model.model,
            thinkingLevel=(
                scoped_model.thinkingLevel
                if scoped_model.thinkingLevel is not None
                else per_model if per_model is not None
                else default_thinking_level if default_thinking_level is not None
                else DEFAULT_THINKING_LEVEL
            ),
            fallbackMessage=None,
        )

    if default_provider and default_model_id:
        # pi model-resolver.ts:673-676 -- the saved default only wins while its auth is still
        # configured; otherwise fall through to the available list instead of handing back a
        # model whose first request would 401.
        found = model_registry.find(default_provider, default_model_id)
        if found is not None and model_registry.hasConfiguredAuth(found):
            per_model = model_thinking_levels.get(f"{default_provider}/{default_model_id}")
            return InitialModelResult(
                model=found,
                thinkingLevel=(
                    per_model
                    if per_model is not None
                    else default_thinking_level if default_thinking_level is not None
                    else DEFAULT_THINKING_LEVEL
                ),
                fallbackMessage=None,
            )

    available_models = list(await maybe_await(model_registry.getAvailable()))
    if available_models:
        for provider, default_id in defaultModelPerProvider.items():
            match = next(
                (model for model in available_models if model.provider == provider and model.id == default_id),
                None,
            )
            if match is not None:
                return InitialModelResult(model=match, thinkingLevel=DEFAULT_THINKING_LEVEL, fallbackMessage=None)
        return InitialModelResult(model=available_models[0], thinkingLevel=DEFAULT_THINKING_LEVEL, fallbackMessage=None)

    return InitialModelResult(model=None, thinkingLevel=DEFAULT_THINKING_LEVEL, fallbackMessage=None)


__all__ = [
    "InitialModelResult",
    "ModelScopeDiagnostic",
    "ParsedModelResult",
    "ResolveCliModelResult",
    "ResolveModelScopeResult",
    "ScopedModel",
    "defaultModelPerProvider",
    "findExactModelReferenceMatch",
    "findInitialModel",
    "parseModelPattern",
    "resolveCliModel",
    "resolveModelScope",
    "resolveModelScopeFromModels",
    "resolveModelScopeWithDiagnostics",
]
