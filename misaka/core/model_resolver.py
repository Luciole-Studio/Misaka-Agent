"""Translated from pi's ``packages/coding-agent/src/core/model-resolver.ts`` (v1.3.0).

Model resolution, scoping, and initial selection.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from wcmatch import glob

from misaka.ai.models import modelsAreEqual
from misaka.ai.types import Model
from misaka.cli.args import isValidThinkingLevel
from misaka.core.defaults import DEFAULT_THINKING_LEVEL
from misaka.utils.collation import localeSortKey
from misaka.utils.js import jsTrim

type ResolvedThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]

# Default chat model IDs for providers with built-in chat models.
defaultModelPerProvider: dict[str, str] = {
    "amazon-bedrock": "us.anthropic.claude-opus-4-6-v1",
    "ant-ling": "Ring-2.6-1T",
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-5.5",
    "azure": "gpt-5.4",
    "openai-codex": "gpt-6.1-sol",
    "radius": "balanced",
    "nvidia": "nvidia/nemotron-3-ultra-550b-a55b",
    "deepseek": "deepseek-v4-pro",
    "google": "gemini-3.1-pro-preview",
    "google-vertex": "gemini-3.1-pro-preview",
    "github-copilot": "gpt-5.4",
    "openrouter": "moonshotai/kimi-k2.6",
    "vercel-ai-gateway": "zai/glm-5.1",
    "xai": "grok-4.7",
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
    "fireworks": "accounts/fireworks/models/kimi-k3",
    "together": "moonshotai/Kimi-K3",
    "baseten": "zai-org/GLM-5.2",
    "opencode": "kimi-k2.6",
    "opencode-go": "kimi-k3",
    "kimi-coding": "kimi-for-coding",
    "meta": "muse-spark-1.3",
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
    # Thinking level if explicitly specified in pattern (e.g., "model:high"), undefined otherwise
    thinkingLevel: ResolvedThinkingLevel | None = None


def _chalk(code: str, message: str) -> str:
    return f"\x1b[{code}m{message}\x1b[0m"


def _dim(message: str) -> str:
    return _chalk("2", message)


def _yellow(message: str) -> str:
    return _chalk("33", message)


# `minimatch(path, pattern, { nocase })`: globstar, braces and extglobs on, as minimatch has them by default.
_MINIMATCH_FLAGS = glob.GLOBSTAR | glob.BRACE | glob.EXTMATCH | glob.FORCEUNIX


def _minimatch(path: str, pattern: str, *, nocase: bool = False) -> bool:
    return glob.globmatch(path, pattern, flags=_MINIMATCH_FLAGS | (glob.IGNORECASE if nocase else 0))


# Model reference resolution. Candidates are collected before a provider is chosen, so catalog order
# never decides the provider and an ambiguous match is reported instead of falling through to a
# weaker one.


@dataclass(slots=True, kw_only=True)
class ModelReferenceRequest:
    # Bare model id or `provider/id`, optionally followed by `:<thinking>`.
    text: str
    # Only consider models from this provider.
    provider: str | None = None
    # A trailing `:<suffix>` is only considered when the full text matches no model, since ids such as
    # OpenRouter's `qwen/qwen3-coder:exacto` contain colons.
    # - `none`: the suffix stays part of the id
    # - `strict`: a valid thinking level is parsed; any other suffix stays part of the id
    # - `lenient`: like `strict`, but an invalid suffix is dropped with a warning
    thinkingSuffix: Literal["none", "strict", "lenient"]
    # Match id and name substrings when no exact match exists.
    fuzzy: bool


# MISAKA fork: the arms of upstream's `ModelReferenceResult` union are named dataclasses.
@dataclass(slots=True)
class ModelReferenceResolved:
    model: Model
    thinkingLevel: ResolvedThinkingLevel | None = None
    warning: str | None = None
    status: Literal["resolved"] = "resolved"


@dataclass(slots=True)
class ModelReferenceAmbiguous:
    candidates: list[Model]
    usableCount: int
    thinkingLevel: ResolvedThinkingLevel | None = None
    warning: str | None = None
    status: Literal["ambiguous"] = "ambiguous"


@dataclass(slots=True)
class ModelReferenceTarget:
    provider: str
    # Never empty.
    providerModels: list[Model]
    # Text after the provider prefix as typed, including any `:<suffix>`.
    modelText: str
    # `modelText` without a valid thinking suffix (unless `thinkingSuffix` is `none`).
    modelId: str
    thinkingLevel: ResolvedThinkingLevel | None = None


@dataclass(slots=True)
class ModelReferenceNotFound:
    # Set when a provider is explicit or named by a `provider/` prefix, for building a custom model id.
    target: ModelReferenceTarget | None = None
    status: Literal["not-found"] = "not-found"


type ModelReferenceResult = ModelReferenceResolved | ModelReferenceAmbiguous | ModelReferenceNotFound
type _ResolvedOrAmbiguous = ModelReferenceResolved | ModelReferenceAmbiguous


@dataclass(slots=True)
class _ThinkingSuffix:
    base: str
    thinkingLevel: ResolvedThinkingLevel


@dataclass(slots=True)
class _ProviderPrefix:
    provider: str
    modelText: str


def _splitThinkingSuffix(text: str) -> _ThinkingSuffix | None:
    colonIndex = text.rfind(":")
    suffix = text[colonIndex + 1 :]
    if colonIndex == -1 or not isValidThinkingLevel(suffix):
        return None
    return _ThinkingSuffix(base=text[:colonIndex], thinkingLevel=suffix)  # type: ignore[arg-type]


def _splitProviderPrefix(text: str, models: Sequence[Model]) -> _ProviderPrefix | None:
    slashIndex = text.find("/")
    if slashIndex == -1:
        return None
    prefix = jsTrim(text[:slashIndex]).lower()
    modelText = jsTrim(text[slashIndex + 1 :])
    if not prefix or not modelText:
        return None
    provider = next((model.provider for model in models if model.provider.lower() == prefix), None)
    return _ProviderPrefix(provider=provider, modelText=modelText) if provider else None


def resolveModelReference(
    request: ModelReferenceRequest,
    models: Sequence[Model],
    usable: Callable[[str], bool],
) -> ModelReferenceResult:
    """``usable`` only breaks ties between providers, so a unique match resolves even without credentials
    (``--api-key`` first-time setup)."""
    text = jsTrim(request.text)
    if not text:
        return ModelReferenceNotFound()
    constraint = request.provider.lower() if request.provider else None
    pool = [model for model in models if model.provider.lower() == constraint] if constraint else list(models)
    result = _resolveText(text, request, pool, usable)
    if result.status != "not-found":
        return result

    split = _splitProviderPrefix(text, pool)
    anchor = (
        (pool[0] if pool else None)
        if constraint
        else next((model for model in pool if split is not None and model.provider == split.provider), None)
    )
    if anchor is None:
        return result
    modelText = split.modelText if split is not None else text
    suffix = None if request.thinkingSuffix == "none" else _splitThinkingSuffix(modelText)
    return ModelReferenceNotFound(
        target=ModelReferenceTarget(
            provider=anchor.provider,
            providerModels=[
                anchor,
                *(model for model in pool if model.provider == anchor.provider and model is not anchor),
            ],
            modelText=modelText,
            modelId=suffix.base if suffix is not None else modelText,
            thinkingLevel=suffix.thinkingLevel if suffix is not None else None,
        )
    )


def findExactModelReferenceMatch(modelReference: str, models: Sequence[Model]) -> Model | None:
    """Exact ``provider/id`` or bare id lookup without thinking suffixes or fuzzy matching."""
    result = resolveModelReference(
        ModelReferenceRequest(text=modelReference, thinkingSuffix="none", fuzzy=False),
        models,
        lambda _provider: True,
    )
    return result.model if result.status == "resolved" else None


def _resolveText(
    text: str,
    request: ModelReferenceRequest,
    pool: Sequence[Model],
    usable: Callable[[str], bool],
) -> ModelReferenceResult:
    reference = _selectReference(text, pool, request.fuzzy, usable)
    if reference is not None:
        return reference

    if request.fuzzy:
        fuzzy = _selectFuzzy(_fuzzyMatches(text, pool), usable)
        if fuzzy is not None:
            return fuzzy

    if request.thinkingSuffix == "none":
        return ModelReferenceNotFound()
    thinking = _splitThinkingSuffix(text)
    if thinking is not None:
        inner = _resolveText(thinking.base, request, pool, usable)
        if inner.status == "not-found":
            return inner
        # Only use this thinking level if no warning from inner recursion
        return replace(inner, thinkingLevel=None if inner.warning else thinking.thinkingLevel)
    # Keep an invalid suffix as part of the id and fail, rather than resolve a different model
    if request.thinkingSuffix == "strict":
        return ModelReferenceNotFound()
    colonIndex = text.rfind(":")
    if colonIndex == -1:
        return ModelReferenceNotFound()

    suffix = text[colonIndex + 1 :]
    inner = _resolveText(text[:colonIndex], request, pool, usable)
    if inner.status == "not-found":
        return inner
    return replace(
        inner,
        thinkingLevel=None,
        warning=f'Invalid thinking level "{suffix}" in pattern "{text}". Using default instead.',
    )


def _selectReference(
    text: str,
    pool: Sequence[Model],
    fuzzy: bool,
    usable: Callable[[str], bool],
) -> _ResolvedOrAmbiguous | None:
    """``provider/rest`` is only a hint: the whole text may also be a model id on another provider, such as
    gateway id ``zai/glm-5`` or commandcode id ``xiaomi/mimo-v2.5-pro``. The named provider wins when usable,
    since that is what the syntax usually means; otherwise a sole usable raw match wins, since the named
    provider could not serve the request. An unusable named match is still preferred over unusable raw
    matches, so ``--api-key`` can set up the named provider."""
    lowerText = text.lower()
    raw = [model for model in pool if model.id.lower() == lowerText]
    split = _splitProviderPrefix(text, pool)
    named: Model | None = None
    if split is not None:
        providerModels = [model for model in pool if model.provider == split.provider]
        lowerModelText = split.modelText.lower()
        # The provider's own literal id (OpenRouter's `openrouter/free`) beats its fuzzy matches
        named = (
            next((model for model in providerModels if model.id.lower() == lowerModelText), None)
            or next((model for model in raw if model.provider == split.provider), None)
            or (_bestFuzzyMatch(_fuzzyMatches(split.modelText, providerModels)) if fuzzy else None)
        )

    others = [model for model in raw if model is not named]
    candidates = [named, *others] if named is not None else others
    if len(candidates) == 0:
        return None
    if len({model.provider for model in candidates}) == 1:
        return ModelReferenceResolved(model=candidates[0])
    if named is not None and usable(named.provider):
        return ModelReferenceResolved(model=named)
    usableOthers = [model for model in others if usable(model.provider)]
    if len(usableOthers) == 1:
        return ModelReferenceResolved(model=usableOthers[0])
    if len(usableOthers) == 0 and named is not None:
        return ModelReferenceResolved(model=named)
    return ModelReferenceAmbiguous(candidates=candidates, usableCount=len(usableOthers))


def _fuzzyMatches(text: str, models: Sequence[Model]) -> list[Model]:
    lowerText = text.lower()
    return [
        model
        for model in models
        if lowerText in model.id.lower() or (model.name is not None and lowerText in model.name.lower())
    ]


def _selectFuzzy(matches: list[Model], usable: Callable[[str], bool]) -> _ResolvedOrAmbiguous | None:
    if len(matches) == 0:
        return None
    multipleProviders = len({model.provider for model in matches}) > 1
    usableMatches = [model for model in matches if usable(model.provider)] if multipleProviders else []
    pool = usableMatches if len(usableMatches) > 0 else matches
    best = _bestFuzzyMatch(pool)
    if best is None:
        return None
    # The same id on several providers would otherwise be decided by catalog order
    tied = [model for model in pool if model.id == best.id]
    if len({model.provider for model in tied}) == 1:
        return ModelReferenceResolved(model=best)
    return ModelReferenceAmbiguous(
        candidates=tied,
        usableCount=len(tied) if len(usableMatches) > 0 else 0,
    )


def _bestFuzzyMatch(matches: Sequence[Model]) -> Model | None:
    """Prefer aliases (``claude-sonnet-4-5``) over dated versions (``claude-sonnet-4-5-20250929``), then the latest."""
    aliases = [model for model in matches if isAlias(model.id)]
    ordered = sorted(aliases if len(aliases) > 0 else matches, key=lambda model: localeSortKey(model.id), reverse=True)
    return ordered[0] if ordered else None


def isAlias(id: str) -> bool:
    # JS `/-\d{8}$/`: ASCII digits, and `$` only at the very end (Python's also matches before a final newline).
    return id.endswith("-latest") or re.search(r"-[0-9]{8}\Z", id) is None


def buildFallbackModel(providerModels: Sequence[Model], modelId: str) -> Model:
    """``providerModels`` is never empty: it is a ``ModelReferenceTarget``'s."""
    defaultId = defaultModelPerProvider.get(providerModels[0].provider)
    baseModel = (
        next((m for m in providerModels if m.id == defaultId), providerModels[0]) if defaultId else providerModels[0]
    )

    return baseModel.model_copy(update={"id": modelId, "name": modelId})


@dataclass(slots=True)
class ModelScopeDiagnostic:
    code: Literal["no-match", "invalid-thinking-level", "ambiguous"]
    message: str
    pattern: str
    type: Literal["warning"] = "warning"


@dataclass(slots=True)
class ResolveModelScopeResult:
    scopedModels: list[ScopedModel]
    diagnostics: list[ModelScopeDiagnostic]


def resolveModelScopeFromModels(patterns: list[str], models: Sequence[Model]) -> ResolveModelScopeResult:
    """Resolve scope patterns (``--models``, ``enabledModels``) to models with optional thinking levels.

    Every model in ``models`` is usable, so credentials cannot choose between providers offering the same
    id. All of them are added, with a warning so the user can pick one with ``provider/id``.
    """
    scopedModels: list[ScopedModel] = []
    diagnostics: list[ModelScopeDiagnostic] = []

    def add(model: Model, thinkingLevel: ResolvedThinkingLevel | None) -> None:
        if not any(modelsAreEqual(scoped.model, model) for scoped in scopedModels):
            scopedModels.append(ScopedModel(model=model, thinkingLevel=thinkingLevel))

    def noMatch(pattern: str) -> None:
        diagnostics.append(
            ModelScopeDiagnostic(code="no-match", message=f'No models match pattern "{pattern}"', pattern=pattern)
        )

    def ambiguous(pattern: str, candidates: Sequence[Model]) -> None:
        providers = sorted({m.provider for m in candidates}, key=localeSortKey)
        diagnostics.append(
            ModelScopeDiagnostic(
                code="ambiguous",
                message=f'Pattern "{pattern}" matches several providers: {", ".join(providers)}. Use provider/id to choose one.',
                pattern=pattern,
            )
        )

    for pattern in patterns:
        # Check if pattern contains glob characters
        if "*" in pattern or "?" in pattern or "[" in pattern:
            # Extract optional thinking level suffix (e.g., "provider/*:high")
            thinking = _splitThinkingSuffix(pattern)
            globPattern = thinking.base if thinking is not None else pattern
            thinkingLevel = thinking.thinkingLevel if thinking is not None else None

            # Ids such as `bracketed-model[1m]` are not globs
            exact = resolveModelReference(
                ModelReferenceRequest(text=globPattern, thinkingSuffix="none", fuzzy=False),
                models,
                lambda _provider: True,
            )
            matches: Sequence[Model] = []
            if exact.status == "resolved":
                matches = [exact.model]
            elif exact.status == "ambiguous":
                matches = exact.candidates
                ambiguous(pattern, matches)
            else:
                # Match against "provider/modelId" format OR just model ID
                # This allows "*sonnet*" to match without requiring "anthropic/*sonnet*"
                matches = [
                    m
                    for m in models
                    if _minimatch(f"{m.provider}/{m.id}", globPattern, nocase=True)
                    or _minimatch(m.id, globPattern, nocase=True)
                ]
            if len(matches) == 0:
                noMatch(pattern)
            for model in matches:
                add(model, thinkingLevel)
            continue

        result = resolveModelReference(
            ModelReferenceRequest(text=pattern, thinkingSuffix="lenient", fuzzy=True),
            models,
            lambda _provider: True,
        )
        if result.status == "not-found":
            noMatch(pattern)
            continue
        if result.warning:
            diagnostics.append(
                ModelScopeDiagnostic(code="invalid-thinking-level", message=result.warning, pattern=pattern)
            )
        if result.status == "ambiguous":
            ambiguous(pattern, result.candidates)
        for model in [result.model] if result.status == "resolved" else result.candidates:
            add(model, result.thinkingLevel)

    return ResolveModelScopeResult(scopedModels=scopedModels, diagnostics=diagnostics)


async def resolveModelScopeWithDiagnostics(
    patterns: list[str], modelRuntime: Any, options: Any = None
) -> ResolveModelScopeResult:
    return resolveModelScopeFromModels(patterns, list(await modelRuntime.getAvailable(None, options)))


async def resolveModelScope(patterns: list[str], modelRuntime: Any, options: Any = None) -> list[ScopedModel]:
    result = await resolveModelScopeWithDiagnostics(patterns, modelRuntime, options)
    for diagnostic in result.diagnostics:
        print(_yellow(f"Warning: {diagnostic.message}"), file=sys.stderr)
    return result.scopedModels


@dataclass(slots=True)
class ResolveCliModelResult:
    model: Model | None
    warning: str | None
    # Error message suitable for CLI display.
    # When set, model will be undefined.
    error: str | None
    thinkingLevel: ResolvedThinkingLevel | None = None


def resolveCliModel(options: dict[str, Any]) -> ResolveCliModelResult:
    """Resolve a single model from CLI flags.

    Supports:
    - --provider <provider> --model <pattern>
    - --model <provider>/<pattern>
    - Fuzzy matching (same rules as model scoping: exact id, then partial id/name)

    Note: This does not apply the thinking level by itself, but it may *parse* and
    return a thinking level from "<pattern>:<thinking>" so the caller can apply it.

    ``options``: ``cliProvider``, ``cliModel``, ``cliThinking``, ``inlineApiKey`` (``--api-key`` was supplied
    for the selected provider) and ``modelRuntime``.
    """
    cliProvider = options.get("cliProvider")
    cliModel = options.get("cliModel")
    cliThinking = options.get("cliThinking")
    inlineApiKey = options.get("inlineApiKey")
    modelRuntime = options["modelRuntime"]

    if not cliModel:
        return ResolveCliModelResult(model=None, warning=None, error=None)

    # Important: use *all* models here, not just models with pre-configured auth.
    # This allows "--api-key" to be used for first-time setup.
    models = list(modelRuntime.getModels())
    if len(models) == 0:
        return ResolveCliModelResult(
            model=None,
            warning=None,
            error="No models available. Check your installation or add models to models.json.",
        )

    provider = (
        next((m.provider for m in models if m.provider.lower() == cliProvider.lower()), None) if cliProvider else None
    )
    if cliProvider and not provider:
        return ResolveCliModelResult(
            model=None,
            warning=None,
            error=f'Unknown provider "{cliProvider}". Use --list-models to see available providers/models.',
        )

    result = resolveModelReference(
        ModelReferenceRequest(text=cliModel, provider=provider, thinkingSuffix="strict", fuzzy=True),
        models,
        # An inline key goes to whichever provider is selected, so stored credentials must not choose it
        (lambda _provider: False) if inlineApiKey else (lambda providerId: modelRuntime.hasConfiguredAuth(providerId)),
    )
    if result.status == "resolved":
        return ResolveCliModelResult(
            model=result.model, thinkingLevel=result.thinkingLevel, warning=result.warning, error=None
        )
    if result.status == "ambiguous":
        matches = ", ".join(sorted((f"{m.provider}/{m.id}" for m in result.candidates), key=localeSortKey))
        authHint = (
            "--api-key does not choose a provider."
            if inlineApiKey
            else "No matching provider is authenticated."
            if result.usableCount == 0
            else "More than one matching provider is authenticated."
        )
        return ResolveCliModelResult(
            model=None,
            warning=None,
            error=f'Model "{cliModel}" is ambiguous across providers: {matches}. {authHint} Use --provider or provider/model.',
        )

    target = result.target
    if target is None:
        return ResolveCliModelResult(
            model=None,
            warning=None,
            error=f'Model "{cliModel}" not found. Use --list-models to see available models.',
        )

    # Use the thinking level suffix only when --thinking is not explicitly provided; otherwise it
    # stays part of the custom model id.
    # e.g. "zai-org/GLM-5.1-FP8:high" → modelId="zai-org/GLM-5.1-FP8", fallbackThinking="high"
    modelId = target.modelText if cliThinking else target.modelId
    fallbackThinking = None if cliThinking else target.thinkingLevel
    fallbackModel = buildFallbackModel(target.providerModels, modelId)
    requestedThinking = cliThinking or fallbackThinking
    return ResolveCliModelResult(
        model=(
            fallbackModel.model_copy(update={"reasoning": True})
            if requestedThinking and requestedThinking != "off"
            else fallbackModel
        ),
        thinkingLevel=fallbackThinking,
        warning=f'Model "{modelId}" not found for provider "{target.provider}". Using custom model id.',
        error=None,
    )


@dataclass(slots=True)
class InitialModelResult:
    model: Model | None
    thinkingLevel: ResolvedThinkingLevel
    fallbackMessage: str | None


async def findInitialModel(options: dict[str, Any]) -> InitialModelResult:
    """Find the initial model to use based on priority:
    1. First model from scoped models (if not continuing/resuming)
    2. Saved default from settings
    3. First available model with valid API key
    """
    scopedModels: list[ScopedModel] = options["scopedModels"]
    isContinuing = options["isContinuing"]
    defaultProvider = options.get("defaultProvider")
    defaultModelId = options.get("defaultModelId")
    defaultThinkingLevel = options.get("defaultThinkingLevel")
    modelThinkingLevels: dict[str, ResolvedThinkingLevel] | None = options.get("modelThinkingLevels")
    modelRuntime = options["modelRuntime"]

    model: Model | None = None
    thinkingLevel: ResolvedThinkingLevel = DEFAULT_THINKING_LEVEL

    # 1. Use first model from scoped models (skip if continuing/resuming)
    if len(scopedModels) > 0 and not isContinuing:
        scopedModel = scopedModels[0]
        perModel = (modelThinkingLevels or {}).get(f"{scopedModel.model.provider}/{scopedModel.model.id}")
        return InitialModelResult(
            model=scopedModel.model,
            thinkingLevel=next(
                (level for level in (scopedModel.thinkingLevel, perModel, defaultThinkingLevel) if level is not None),
                DEFAULT_THINKING_LEVEL,
            ),
            fallbackMessage=None,
        )

    # 2. Try saved default from settings if auth is configured.
    if defaultProvider and defaultModelId:
        found = modelRuntime.getModel(defaultProvider, defaultModelId)
        if found is not None and modelRuntime.hasConfiguredAuth(found.provider):
            model = found
            perModel = (modelThinkingLevels or {}).get(f"{defaultProvider}/{defaultModelId}")
            if perModel:
                thinkingLevel = perModel
            elif defaultThinkingLevel:
                thinkingLevel = defaultThinkingLevel
            return InitialModelResult(model=model, thinkingLevel=thinkingLevel, fallbackMessage=None)

    # 3. Try first available model with valid API key
    availableModels = list(modelRuntime.getAvailableSnapshot())

    if len(availableModels) > 0:
        # Try to find a default model from known providers
        for provider, defaultId in defaultModelPerProvider.items():
            match = next((m for m in availableModels if m.provider == provider and m.id == defaultId), None)
            if match is not None:
                return InitialModelResult(model=match, thinkingLevel=DEFAULT_THINKING_LEVEL, fallbackMessage=None)

        # If no default found, use first available
        return InitialModelResult(model=availableModels[0], thinkingLevel=DEFAULT_THINKING_LEVEL, fallbackMessage=None)

    # 4. No model found
    return InitialModelResult(model=None, thinkingLevel=DEFAULT_THINKING_LEVEL, fallbackMessage=None)


async def restoreModelFromSession(
    savedProvider: str,
    savedModelId: str,
    currentModel: Model | None,
    shouldPrintMessages: bool,
    modelRuntime: Any,
) -> dict[str, Any]:
    """Restore model from session, with fallback to available models. Resolves ``{model, fallbackMessage}``."""
    restoredModel = modelRuntime.getModel(savedProvider, savedModelId)

    # Check if restored model exists and still has auth configured
    hasConfiguredAuth = modelRuntime.hasConfiguredAuth(restoredModel.provider) if restoredModel is not None else False

    if restoredModel is not None and hasConfiguredAuth:
        if shouldPrintMessages:
            print(_dim(f"Restored model: {savedProvider}/{savedModelId}"))
        return {"model": restoredModel, "fallbackMessage": None}

    # Model not found or no API key - fall back
    reason = "model no longer exists" if restoredModel is None else "no auth configured"

    if shouldPrintMessages:
        print(_yellow(f"Warning: Could not restore model {savedProvider}/{savedModelId} ({reason})."), file=sys.stderr)

    # If we already have a model, use it as fallback
    if currentModel is not None:
        if shouldPrintMessages:
            print(_dim(f"Falling back to: {currentModel.provider}/{currentModel.id}"))
        return {
            "model": currentModel,
            "fallbackMessage": f"Could not restore model {savedProvider}/{savedModelId} ({reason}). Using {currentModel.provider}/{currentModel.id}.",
        }

    # Try to find any available model
    availableModels = list(modelRuntime.getAvailableSnapshot())

    if len(availableModels) > 0:
        # Try to find a default model from known providers
        fallbackModel: Model | None = None
        for provider, defaultId in defaultModelPerProvider.items():
            match = next((m for m in availableModels if m.provider == provider and m.id == defaultId), None)
            if match is not None:
                fallbackModel = match
                break

        # If no default found, use first available
        if fallbackModel is None:
            fallbackModel = availableModels[0]

        if shouldPrintMessages:
            print(_dim(f"Falling back to: {fallbackModel.provider}/{fallbackModel.id}"))

        return {
            "model": fallbackModel,
            "fallbackMessage": f"Could not restore model {savedProvider}/{savedModelId} ({reason}). Using {fallbackModel.provider}/{fallbackModel.id}.",
        }

    # No models available
    return {"model": None, "fallbackMessage": None}


__all__ = [
    "InitialModelResult",
    "ModelReferenceAmbiguous",
    "ModelReferenceNotFound",
    "ModelReferenceRequest",
    "ModelReferenceResolved",
    "ModelReferenceResult",
    "ModelReferenceTarget",
    "ModelScopeDiagnostic",
    "ResolveCliModelResult",
    "ResolveModelScopeResult",
    "ScopedModel",
    "defaultModelPerProvider",
    "findExactModelReferenceMatch",
    "findInitialModel",
    "resolveCliModel",
    "resolveModelReference",
    "resolveModelScope",
    "resolveModelScopeFromModels",
    "resolveModelScopeWithDiagnostics",
    "restoreModelFromSession",
]
