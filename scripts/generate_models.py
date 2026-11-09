#!/usr/bin/env python3
"""Generate the built-in model catalog from the upstream catalogs (models.dev, OpenRouter, the Vercel AI Gateway,
Radius, NVIDIA NIM) and pi's corrections.

Translated from pi's ``packages/ai/scripts/generate-models.ts`` (v1.3.0), function by function::

    python scripts/generate_models.py --strict
    python scripts/generate_models.py --strict --data-only
    python scripts/generate_models.py --strict --json-only --json-output .artifacts/model-catalog

MISAKA fork: misaka ships the data exactly as pi published it (``scripts/refresh_model_catalog.py`` copies a release),
so a run of this script is how to reproduce pi's generation from the same sources, or to write the JSON catalog. The
layout is misaka's (``model_data.py``): the data under ``misaka/ai/providers/data``, the per-provider modules
``misaka/ai/providers/<module>_models.py`` and the aggregator ``misaka/ai/models_generated.py``, which pi writes as
``.models.ts`` files and ``models.generated.ts``. Records are ``dict`` objects whose keys follow the upstream object
literals, so ``jsonStringify`` writes the bytes ``JSON.stringify`` writes; a property upstream copies from a source that
lacks it is ``UNDEFINED`` and left out.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import sys
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Sibling scripts, as pi's scripts import `./model-data.ts`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ai_gateway_pricing import getAiGatewayCost
from model_data import (
    AGGREGATOR,
    MODEL_DATA_MANIFEST_FILE,
    PROVIDERS,
    _sortStrings,
    assertExactModelIds,
    createModelDataManifest,
    groupProviderModelData,
    providerModule,
    readModelDataProviderIds,
    validateGeneratedModelData,
    validateModelDataDirectory,
)
from models_dev_reasoning_options import getEffortThinkingLevelMap
from openrouter_catalog import buildOpenRouterCatalog

from misaka.ai.api.cloudflare import (
    CLOUDFLARE_AI_GATEWAY_ANTHROPIC_BASE_URL,
    CLOUDFLARE_AI_GATEWAY_COMPAT_BASE_URL,
    CLOUDFLARE_AI_GATEWAY_OPENAI_BASE_URL,
    CLOUDFLARE_WORKERS_AI_BASE_URL,
    CLOUDFLARE_WORKERS_AI_REST_BASE_URL,
)
from misaka.utils import whatwg_fetch
from misaka.utils.collation import localeSortKey
from misaka.utils.js import (
    UNDEFINED,
    jsLength,
    jsObjectOrder,
    jsonStringify,
    jsSlice,
    jsTrim,
    parseFloat,
    toFixed,
)
from misaka.utils.whatwg_url import URL

packageRoot = Path(__file__).resolve().parent.parent

type Model = dict[str, Any]


def readGeneratorOptions(args: list[str]) -> dict[str, Any]:
    strict = False
    dataOnly = False
    jsonOnly = False
    jsonOutputDir: str | None = None
    pretty = False

    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--strict":
            strict = True
        elif arg == "--data-only":
            dataOnly = True
        elif arg == "--json-only":
            jsonOnly = True
        elif arg == "--pretty":
            pretty = True
        elif arg == "--json-output":
            index += 1
            value = args[index] if index < len(args) else None
            if not value:
                raise RuntimeError("--json-output requires a directory")
            jsonOutputDir = str(Path(value).resolve())
        else:
            raise RuntimeError(f"Unknown argument: {arg}")
        index += 1

    if jsonOnly and not jsonOutputDir:
        raise RuntimeError("--json-only requires --json-output")
    if dataOnly and (jsonOnly or jsonOutputDir):
        raise RuntimeError("--data-only cannot be combined with JSON catalog output")
    return {"strict": strict, "dataOnly": dataOnly, "jsonOnly": jsonOnly, "jsonOutputDir": jsonOutputDir, "pretty": pretty}


# MISAKA fork: read at the start of `main`, so that importing the module parses no arguments.
generatorOptions: dict[str, Any] = {"strict": False, "dataOnly": False, "jsonOnly": False, "jsonOutputDir": None, "pretty": False}

COPILOT_STATIC_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}

TOGETHER_BASE_URL = "https://api.together.ai/v1"
TOGETHER_BASE_COMPAT = {
    "supportsStore": False,
    "supportsDeveloperRole": False,
    "supportsReasoningEffort": False,
    "maxTokensField": "max_tokens",
    "supportsStrictMode": False,
    "supportsLongCacheRetention": False,
}
TOGETHER_TOGGLE_REASONING_COMPAT = {
    **TOGETHER_BASE_COMPAT,
    "thinkingFormat": "together",
}
TOGETHER_REASONING_EFFORT_COMPAT = {
    **TOGETHER_BASE_COMPAT,
    "supportsReasoningEffort": True,
    "thinkingFormat": "openai",
}
TOGETHER_TOGGLE_REASONING_EFFORT_COMPAT = {
    **TOGETHER_TOGGLE_REASONING_COMPAT,
    "supportsReasoningEffort": True,
}
TOGETHER_REASONING_ONLY_MODELS = {
    "deepseek-ai/DeepSeek-R1",
    "MiniMaxAI/MiniMax-M2.7",
}
TOGETHER_REASONING_EFFORT_MODELS = {"openai/gpt-oss-20b", "openai/gpt-oss-120b"}
TOGETHER_TOGGLE_REASONING_EFFORT_MODELS = {"deepseek-ai/DeepSeek-V4-Pro-0813"}
TOGETHER_FIXED_REASONING_LEVEL_MAP = {
    "off": None,
    "minimal": None,
    "low": None,
    "medium": None,
}
TOGETHER_REASONING_EFFORT_LEVEL_MAP = {
    "off": None,
    "minimal": None,
}
TOGETHER_DEEPSEEK_V4_THINKING_LEVEL_MAP = {
    "minimal": None,
    "low": None,
    "medium": None,
    "high": "high",
    "xhigh": None,
}
TOGETHER_TOGGLE_REASONING_LEVEL_MAP = {
    "minimal": None,
    "low": None,
    "medium": None,
}

AI_GATEWAY_MODELS_URL = "https://ai-gateway.vercel.sh/v1"
AI_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh"
# TypeSafe-compatible System One endpoint for evaluation models.
# https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe
AI_GATEWAY_TYPESAFE_BASE_URL = "https://ai-gateway.vercel.sh/typesafe/v1"
VERTEX_BASE_URL = "https://{location}-aiplatform.googleapis.com"
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_HEADERS = {
    "NVCF-POLL-SECONDS": "3600",
}
NVIDIA_OPENAI_COMPAT = {
    "supportsStore": False,
    "supportsDeveloperRole": False,
    "supportsReasoningEffort": False,
    "maxTokensField": "max_tokens",
    "supportsStrictMode": False,
    "supportsLongCacheRetention": False,
}
NVIDIA_NIM_UNSUPPORTED_MODELS = {
    "abacusai/dracarys-llama-3.1-70b-instruct",
    "bytedance/seed-oss-36b-instruct",
    "deepseek-ai/deepseek-v4-flash",
    "deepseek-ai/deepseek-v4-pro",
    "google/gemma-2-2b-it",
    "google/gemma-3n-e2b-it",
    "google/gemma-3n-e4b-it",
    "google/gemma-4-31b-it",
    "meta/llama-3.2-1b-instruct",
    "meta/llama-4-maverick-17b-128e-instruct",
    "microsoft/phi-4-mini-instruct",
    "minimaxai/minimax-m2.7",
    "mistralai/mistral-nemotron",
    "nvidia/nemotron-mini-4b-instruct",
    "qwen/qwen3-next-80b-a3b-instruct",
    "qwen/qwen3.5-397b-a17b",
    "sarvamai/sarvam-m",
    "upstage/solar-10.7b-instruct",
}
ZAI_TOOL_STREAM_UNSUPPORTED_MODELS = {"glm-4.5", "glm-4.5-air", "glm-4.5-flash", "glm-4.5v"}
OPENCODE_GO_GLM52_THINKING_LEVEL_MAP = {
    "off": None,
    "minimal": None,
    "low": None,
    "medium": None,
    "high": "high",
    "max": "max",
}
EAGER_TOOL_INPUT_STREAMING_UNSUPPORTED_ANTHROPIC_MODELS = {
    "github-copilot:claude-haiku-4.5",
    "github-copilot:claude-sonnet-4",
    "github-copilot:claude-sonnet-4.5",
}
ANTHROPIC_ALLOWED_FALLBACK_MODELS = {
    "claude-fable-5": ["claude-opus-4-8", "claude-opus-5"],
    "claude-opus-5": ["claude-opus-4-8"],
}

DEEPSEEK_V4_THINKING_LEVEL_MAP = {
    "minimal": None,
    "low": None,
    "medium": None,
    "high": "high",
    "max": "max",
}
DEEPSEEK_V4_FLASH_THINKING_LEVEL_MAP = {
    **DEEPSEEK_V4_THINKING_LEVEL_MAP,
    "low": "low",
}
# Azure Foundry rejects DeepSeek's own max effort.
AZURE_DEEPSEEK_V4_THINKING_LEVEL_MAP = {
    "minimal": None,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": None,
    "max": None,
}
# Verified against Fireworks Messages raw_output on 2026-09-10 (#9323).
# Fall back to verified support when models.dev omits effort metadata; this is
# not an allowlist. Any Fireworks Messages model advertising effort uses adaptive thinking.
FIREWORKS_ADAPTIVE_THINKING_FALLBACK_MODELS = {
    "accounts/fireworks/models/deepseek-v4-flash-0731",
    "accounts/fireworks/models/deepseek-v4-flash-vision-exp",
    "accounts/fireworks/models/deepseek-v4-pro-0813",
    "accounts/fireworks/models/qwen3p8-max",
    "accounts/fireworks/models/qwen3p8-2p4t-a95b",
}
QWEN_TOKEN_PLAN_FALLBACK_THINKING_LEVEL_MAP = {
    "minimal": None,
    "low": None,
    "medium": None,
    "high": "high",
    "xhigh": None,
    "max": "max",
}
QWEN_TOKEN_PLAN_REASONING_EFFORT_FALLBACK_MODEL_IDS = {"glm-5", "glm-5.1"}
# Retired preview id — models.dev may still list it after GA ships.
QWEN_TOKEN_PLAN_EXCLUDED_MODEL_IDS = {"qwen3.8-max-preview"}
QWEN_TOKEN_PLAN_PROVIDER_IDS = {
    "qwen-token-plan",
    "qwen-token-plan-cn",
    "qwen-token-plan-individual",
}
# QwenCloud Token Plan Individual text-model allowlist, verified 2026-09-03.
# Retired models remain excluded above even if the public catalog lags.
# https://docs.qwencloud.com/token-plan/personal/token-plan-personal-overview
QWEN_TOKEN_PLAN_INDIVIDUAL_MODEL_IDS = [
    "deepseek-v4-flash-0731",
    "deepseek-v4-pro",
    "deepseek-v4-pro-0813",
    "glm-5.2",
    "qwen3.6-flash",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.8-flash",
    "qwen3.8-max",
]

KIMI_K3_MAX_TOKENS = 131072
KIMI_K3_COST = {
    "input": 3,
    "output": 15,
    "cacheRead": 0.3,
    "cacheWrite": 0,
}
# Kimi Chat Completions settings shared by Moonshot and Kimi Coding.
KIMI_OPENAI_COMPAT: dict[str, Any] = {
    "supportsStore": False,
    "supportsDeveloperRole": False,
    "supportsReasoningEffort": False,
    "maxTokensField": "max_tokens",
    "supportsStrictMode": False,
    "thinkingFormat": "deepseek",
}
# Kimi K3-style models take reasoning_effort and need earlier reasoning sent back.
KIMI_K3_OPENAI_COMPAT: dict[str, Any] = {
    **KIMI_OPENAI_COMPAT,
    "requiresReasoningContentOnAssistantMessages": True,
    "thinkingFormat": "openai",
    "supportsReasoningEffort": True,
}
OPENROUTER_KIMI_K3_MODEL_IDS = {"moonshotai/kimi-k3", "~moonshotai/kimi-latest"}

ANT_LING_RING_THINKING_LEVEL_MAP = {
    "off": None,
    "minimal": None,
    "low": None,
    "medium": None,
    "high": "high",
    "xhigh": "xhigh",
}

BEDROCK_INFERENCE_PROFILE_ONLY_MODEL_IDS = {"anthropic.claude-opus-5"}
MODELS_DEV_OPENAI_UNSUPPORTED_MODEL_IDS = {"gpt-5.6"}
OPENAI_TOOL_SEARCH_MODEL_IDS = {
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-pro",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-6.1-sol",
}
OPENAI_ADDITIONAL_TOOLS_MODEL_IDS = OPENAI_TOOL_SEARCH_MODEL_IDS
OPENAI_MID_CONVO_SYSTEM_MESSAGE_MODEL_IDS = OPENAI_TOOL_SEARCH_MODEL_IDS
OPENAI_CODEX_ADDITIONAL_TOOLS_MODEL_IDS = {
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-6.1-sol",
}
# OpenAI documents configuration_update for the GPT-6 family.
# https://developers.openai.com/api/docs/guides/reasoning
OPENAI_MID_CONVO_EFFORT_MODEL_IDS = {"gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol"}
OPENAI_LONG_CONTEXT_INPUT_THRESHOLD = 272000
OPENAI_SHORT_CONTEXT_CAPPED_MODEL_IDS = {
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-6.1-sol",
}
OPENAI_LONG_CONTEXT_PRICING_MODEL_IDS = {
    "gpt-5.4",
    "gpt-5.4-pro",
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-6.1-sol",
}

# Keep the generated default no less restrictive than coding-agent's historical
# image preprocessing. Provider limits can narrow this profile, but unknown
# providers retain the cache-safe 2000px / 4.5 MiB behavior.
DEFAULT_IMAGE_RESIZE = {
    "maxWidth": 2000,
    "maxHeight": 2000,
    "maxBytes": 4.5 * 1024 * 1024,
    "jpegQuality": 80,
}


def _get(value: Any, key: str) -> Any:
    """``value?.[key]``: ``None`` for a missing value or property."""
    return value.get(key) if isinstance(value, dict) else None


def _includes(value: Any, item: Any) -> bool:
    """``value?.includes(item) === true``: an array or a string that holds ``item``."""
    return isinstance(value, list | str) and item in value


def withOpenAiLongContextPricing(cost: dict[str, Any]) -> dict[str, Any]:
    return {
        **cost,
        "tiers": [
            {
                "inputTokensAbove": OPENAI_LONG_CONTEXT_INPUT_THRESHOLD,
                "input": roundCost(cost["input"] * 2),
                "output": roundCost(cost["output"] * 1.5),
                "cacheRead": roundCost(cost["cacheRead"] * 2),
                "cacheWrite": roundCost(cost["cacheWrite"] * 2),
            },
        ],
    }


# Keep current OpenAI prices authoritative until models.dev and passthrough
# catalogs catch up.
# https://developers.openai.com/api/docs/pricing
OPENAI_STANDARD_COSTS: dict[str, dict[str, Any]] = {
    "gpt-5.6-luna": {"input": 0.2, "output": 1.2, "cacheRead": 0.02, "cacheWrite": 0.25},
    "gpt-5.6-sol": {"input": 4, "output": 20, "cacheRead": 0.4, "cacheWrite": 5},
    "gpt-5.6-terra": {"input": 2, "output": 12, "cacheRead": 0.2, "cacheWrite": 2.5},
    "gpt-6-astra": {"input": 10, "output": 50, "cacheRead": 1, "cacheWrite": 12.5},
    "gpt-6-luna": {"input": 0.1, "output": 0.5, "cacheRead": 0.01, "cacheWrite": 0.125},
    "gpt-6-sol": {"input": 2, "output": 10, "cacheRead": 0.2, "cacheWrite": 2.5},
    "gpt-6.1-sol": {"input": 2, "output": 10, "cacheRead": 0.1, "cacheWrite": 2.5},
}

OPENAI_RESPONSES_NONE_REASONING_MODELS = {
    "gpt-5.1",
    "gpt-5.2",
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-6-sol",
    "gpt-6-luna",
}
XAI_BUILTIN_EXCLUDED_MODEL_IDS = {
    "grok-3",
    "grok-3-fast",
    "grok-4.20-0309-non-reasoning",
    "grok-4.20-0309-reasoning",
    "grok-build-0.1",
    "grok-code-fast-1",
}
XAI_RESPONSES_COMPAT = {
    "supportsLongCacheRetention": False,
}

OPENCODE_OPENAI_COMPLETIONS_LONG_CACHE_RETENTION_UNSUPPORTED_MODELS = {
    "opencode:deepseek-v4-flash",
    "opencode:deepseek-v4-pro",
    "opencode:kimi-k2.5",
    "opencode:kimi-k2.6",
    "opencode:minimax-m2.7",
    "opencode-go:kimi-k2.6",
}

# GitHub's "Models with extended capabilities" table lists these Copilot models as supporting
# the extended 1 million token context window.
GITHUB_COPILOT_EXTENDED_CONTEXT_MODELS = {
    "claude-fable-5",
    "claude-opus-4.6",
    "claude-opus-4.7",
    "claude-opus-4.8",
    "claude-opus-5",
    "claude-opus-5.5",
    "claude-sonnet-4.6",
    "claude-sonnet-5",
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-6-astra",
    "gpt-6-luna",
    "gpt-6-sol",
}

# Checked manually against the authenticated GitHub Copilot /models endpoint on 2026-06-15.
# Keep this to narrow corrections over models.dev metadata instead of snapshotting Copilot's catalog.
GITHUB_COPILOT_THINKING_LEVEL_OVERRIDES: dict[str, dict[str, Any]] = {
    "claude-opus-4.7": {"minimal": "low"},
    "claude-opus-4.8": {"minimal": "low"},
    "claude-opus-5": {"minimal": "low"},
    "claude-sonnet-4.6": {"minimal": "low", "max": "max"},
}


def mergeThinkingLevelMap(model: Model, map: dict[str, Any]) -> None:
    model["thinkingLevelMap"] = {**(model.get("thinkingLevelMap") or {}), **map}


modelsDevReasoningOptions: dict[str, list[dict[str, Any]]] = {}


def getModelKey(model: Model) -> str:
    return f"{model['provider']}:{model['id']}"


def recordModelsDevReasoningOptions(provider: str, id: str, sourceModel: dict[str, Any]) -> None:
    if "reasoning_options" in sourceModel:
        modelsDevReasoningOptions[f"{provider}:{id}"] = sourceModel["reasoning_options"]


def supportsDirectReasoningEffort(model: Model) -> bool:
    if model["api"] == "anthropic-messages":
        return _get(model.get("compat"), "forceAdaptiveThinking") is True
    if model["api"] in ("openai-responses", "azure-openai-responses", "openai-codex-responses"):
        return True
    if model["api"] != "openai-completions":
        return False

    compat = {
        **detectOpenAICompletionsCompat(model),
        **(model.get("compat") or {}),
    }
    return compat.get("thinkingFormat") == "openai" and bool(compat.get("supportsReasoningEffort"))


def applyModelsDevReasoningOptionMetadata(model: Model) -> None:
    reasoningOptions = modelsDevReasoningOptions.get(getModelKey(model))
    if not reasoningOptions or not supportsDirectReasoningEffort(model):
        return
    thinkingLevelMap = getEffortThinkingLevelMap(reasoningOptions)
    if thinkingLevelMap:
        mergeThinkingLevelMap(model, thinkingLevelMap)


def getTogetherCompat(modelId: str, reasoning: bool) -> dict[str, Any]:
    if not reasoning:
        return TOGETHER_BASE_COMPAT
    if modelId in TOGETHER_REASONING_EFFORT_MODELS:
        return TOGETHER_REASONING_EFFORT_COMPAT
    if modelId in TOGETHER_TOGGLE_REASONING_EFFORT_MODELS:
        return TOGETHER_TOGGLE_REASONING_EFFORT_COMPAT
    if modelId in TOGETHER_REASONING_ONLY_MODELS:
        return TOGETHER_BASE_COMPAT
    return TOGETHER_TOGGLE_REASONING_COMPAT


def getTogetherThinkingLevelMap(modelId: str, reasoning: bool) -> dict[str, Any] | None:
    if not reasoning:
        return None
    if modelId in TOGETHER_REASONING_EFFORT_MODELS:
        return {**TOGETHER_REASONING_EFFORT_LEVEL_MAP}
    if modelId in TOGETHER_TOGGLE_REASONING_EFFORT_MODELS:
        return {**TOGETHER_DEEPSEEK_V4_THINKING_LEVEL_MAP}
    if modelId in TOGETHER_REASONING_ONLY_MODELS:
        return {**TOGETHER_FIXED_REASONING_LEVEL_MAP}
    return {**TOGETHER_TOGGLE_REASONING_LEVEL_MAP}


def supportsOpenAiXhigh(modelId: str) -> bool:
    return (
        "gpt-5.2" in modelId
        or "gpt-5.3" in modelId
        or "gpt-5.4" in modelId
        or "gpt-5.5" in modelId
        or "gpt-5.6" in modelId
        or "gpt-6" in modelId
    )


def supportsOpenAiMax(model: Model) -> bool:
    return ("gpt-5.6" in model["id"] or "gpt-6" in model["id"]) and model["api"] in (
        "openai-responses",
        "azure-openai-responses",
        "openai-codex-responses",
        "openai-completions",
    )


VERIFIED_ANTHROPIC_MID_CONVO_EFFORT_PROVIDERS = {"anthropic", "openrouter"}
# OpenRouter rejects `configuration_update` system messages on Opus 5 ("Mid-conversation
# reasoning effort (configuration_update) is not supported on anthropic/claude-opus-5-20260723")
# while accepting them on Fable 5.1, so gate that model there.
MID_CONVO_EFFORT_UNSUPPORTED_ANTHROPIC_MODELS = {"openrouter:anthropic/claude-opus-5"}


def _test(pattern: str, value: str) -> bool:
    """``/pattern/.test(value)`` without the ``m`` flag: ``$`` matches only at the very end (``\\Z``)."""
    return re.search(pattern.replace("$", r"\Z"), value) is not None


def supportsAnthropicMidConvoEffort(modelId: str) -> bool:
    id = re.sub(r"^~?anthropic/", "", modelId.lower(), count=1)
    return (
        _test(r"^claude-opus-(?:5|5[.-]5)(?:-\d{8})?$", id)
        or _test(r"^claude-(?:sonnet|haiku)-5[.-]5(?:-\d{8})?$", id)
        or _test(r"^claude-(?:fable|mythos)-5(?:[.-]1)(?:-\d{8})?$", id)
    )


# Claude models that accept `thinking: {type: "disabled"}` alongside per-message effort.
# Opus 5.5, Sonnet 5.5, Fable 5.1, and Mythos 5.1 reject it.
# https://platform.claude.com/docs/en/build-with-claude/thinking#configuring-thinking
ANTHROPIC_DISABLED_THINKING_MODELS = {"claude-opus-5", "claude-haiku-5-5", "claude-haiku-5.5"}


def supportsAnthropicDisabledThinkingWithMidConvoEffort(modelId: str) -> bool:
    # Strip OpenRouter's "anthropic/" or "~anthropic/" prefix and any dated snapshot suffix.
    id = re.sub(r"-\d{8}\Z", "", re.sub(r"^~?anthropic/", "", modelId.lower(), count=1), count=1)
    return id in ANTHROPIC_DISABLED_THINKING_MODELS


def supportsAnthropicMidConvoSystemMessages(modelId: str) -> bool:
    return (
        _test(r"^claude-opus-(?:4[.-]8|5(?:[.-]5)?)(?:-\d{8})?$", modelId)
        or _test(r"^claude-(?:sonnet|haiku)-5[.-]5(?:-\d{8})?$", modelId)
        or _test(r"^claude-(?:fable|mythos)-5(?:[.-]1)?(?:-\d{8})?$", modelId)
    )


def isAnthropicAdaptiveThinkingModel(modelId: str) -> bool:
    return (
        "opus-4-6" in modelId
        or "opus-4.6" in modelId
        or "opus-4-7" in modelId
        or "opus-4.7" in modelId
        or "opus-4-8" in modelId
        or "opus-4.8" in modelId
        or "opus-5" in modelId
        or "opus.5" in modelId
        or "sonnet-4-6" in modelId
        or "sonnet-4.6" in modelId
        or "sonnet-5" in modelId
        or "sonnet.5" in modelId
        or "haiku-5" in modelId
        or "haiku.5" in modelId
        or "fable-5" in modelId
        or "mythos-5" in modelId
    )


def isAnthropicTemperatureUnsupportedModel(modelId: str) -> bool:
    id = modelId.lower()
    return (
        "opus-4-7" in id
        or "opus-4.7" in id
        or "opus-4-8" in id
        or "opus-4.8" in id
        or "opus-5" in id
        or "opus.5" in id
        or "sonnet-5-5" in id
        or "sonnet-5.5" in id
        or "haiku-5-5" in id
        or "haiku-5.5" in id
    )


OPENAI_COMPLETIONS_DEFAULT_COMPAT: dict[str, Any] = {
    "supportsStore": True,
    "supportsDeveloperRole": True,
    "supportsReasoningEffort": True,
    "supportsUsageInStreaming": True,
    "supportsFinishReason": True,
    "maxTokensField": "max_completion_tokens",
    "requiresToolResultName": False,
    "requiresAssistantAfterToolResult": False,
    "requiresThinkingAsText": False,
    "requiresReasoningContentOnAssistantMessages": False,
    "thinkingFormat": "openai",
    "openRouterRouting": {},
    "vercelGatewayRouting": {},
    "chatTemplateKwargs": {},
    "chatTemplateArgs": {},
    "zaiToolStream": False,
    "supportsStrictMode": False,
    "supportsOpenAIGrammarTools": False,
    "supportsMidConvoSystemMessages": False,
    "supportsMidConvoToolAdditions": False,
    "sendSessionAffinityHeaders": False,
    "supportsLongCacheRetention": True,
}


def mergeAnthropicMessagesCompat(model: Model, compat: dict[str, Any]) -> None:
    model["compat"] = {**(model.get("compat") or {}), **compat}


def detectOpenAICompletionsCompat(model: Model) -> dict[str, Any]:
    provider = model["provider"]
    baseUrl = model["baseUrl"]

    isZai = provider == "zai" or provider == "zai-coding-cn" or "api.z.ai" in baseUrl or "open.bigmodel.cn" in baseUrl
    isTogether = provider == "together" or "api.together.ai" in baseUrl or "api.together.xyz" in baseUrl
    isMoonshot = provider == "moonshotai" or provider == "moonshotai-cn" or "api.moonshot." in baseUrl
    isOpenRouter = provider == "openrouter" or "openrouter.ai" in baseUrl
    isCloudflareWorkersAI = provider == "cloudflare-workers-ai" or "api.cloudflare.com" in baseUrl
    isCloudflareAiGateway = provider == "cloudflare-ai-gateway" or "gateway.ai.cloudflare.com" in baseUrl
    isNvidia = provider == "nvidia" or "integrate.api.nvidia.com" in baseUrl
    isAntLing = provider == "ant-ling" or "api.ant-ling.com" in baseUrl
    isCerebras = provider == "cerebras" or "cerebras.ai" in baseUrl
    isTogetherReasoningOnly = isTogether and model["id"] in TOGETHER_REASONING_ONLY_MODELS
    isDeepSeek = provider == "deepseek" or "deepseek.com" in baseUrl.lower()

    isNonStandard = (
        isNvidia
        or isCerebras
        or provider == "xai"
        or "api.x.ai" in baseUrl
        or isTogether
        or "chutes.ai" in baseUrl
        or isDeepSeek
        or isZai
        or isMoonshot
        or provider == "opencode"
        or "opencode.ai" in baseUrl
        or isCloudflareWorkersAI
        or isCloudflareAiGateway
        or isAntLing
    )

    useMaxTokens = (
        "chutes.ai" in baseUrl
        or isDeepSeek
        or isMoonshot
        or isCloudflareAiGateway
        or isTogether
        or isNvidia
        or isAntLing
        or isZai
    )

    isGrok = provider == "xai" or "api.x.ai" in baseUrl
    isOpenRouterDeveloperRoleModel = isOpenRouter and (
        model["id"].startswith("anthropic/") or model["id"].startswith("openai/")
    )
    cacheControlFormat = (
        "anthropic" if provider == "openrouter" and re.match(r"^~?anthropic/", model["id"]) else None
    )

    if isDeepSeek:
        thinkingFormat = "deepseek"
    elif isZai:
        thinkingFormat = "zai"
    elif isTogether and not isTogetherReasoningOnly:
        thinkingFormat = "together"
    elif isAntLing:
        thinkingFormat = "ant-ling"
    elif isOpenRouter:
        thinkingFormat = "openrouter"
    else:
        thinkingFormat = "openai"

    return {
        "supportsStore": not isNonStandard,
        "supportsDeveloperRole": isOpenRouterDeveloperRoleModel or (not isNonStandard and not isOpenRouter),
        "supportsReasoningEffort": (
            not isGrok
            and not isZai
            and not isMoonshot
            and not isTogether
            and not isCloudflareAiGateway
            and not isNvidia
            and not isAntLing
        ),
        "supportsUsageInStreaming": True,
        "supportsFinishReason": True,
        "maxTokensField": "max_tokens" if useMaxTokens else "max_completion_tokens",
        "requiresToolResultName": False,
        "requiresAssistantAfterToolResult": False,
        "requiresThinkingAsText": False,
        "requiresReasoningContentOnAssistantMessages": isDeepSeek,
        "thinkingFormat": thinkingFormat,
        "openRouterRouting": {},
        "vercelGatewayRouting": {},
        "chatTemplateKwargs": {},
        "chatTemplateArgs": {},
        "zaiToolStream": False,
        # Preserve built-in behavior as explicit metadata against the conservative runtime default.
        "supportsStrictMode": not isMoonshot and not isTogether and not isCloudflareAiGateway and not isNvidia and not isCerebras,
        "supportsOpenAIGrammarTools": False,
        "supportsMidConvoSystemMessages": False,
        "supportsMidConvoToolAdditions": False,
        **({"cacheControlFormat": cacheControlFormat} if cacheControlFormat else {}),
        "sendSessionAffinityHeaders": isOpenRouter,
        "supportsLongCacheRetention": not (
            isTogether or isCloudflareWorkersAI or isCloudflareAiGateway or isNvidia or isAntLing
        ),
    }


def isPlainEmptyObject(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 0


def _strictEqual(a: Any, b: Any) -> bool:
    """``a === b`` for the values compat records hold: objects only equal themselves, ``true`` is not ``1``."""
    if isinstance(a, dict | list) or isinstance(b, dict | list):
        return a is b
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a is b if a is UNDEFINED or b is UNDEFINED else a == b


def openAICompletionsCompatDelta(compat: dict[str, Any]) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    for key, value in compat.items():
        defaultValue = OPENAI_COMPLETIONS_DEFAULT_COMPAT.get(key, UNDEFINED)
        if isPlainEmptyObject(value) and isPlainEmptyObject(defaultValue):
            continue
        if not _strictEqual(value, defaultValue):
            delta[key] = value
    return delta


def mergeOpenAICompletionsCompat(model: Model, compat: dict[str, Any]) -> None:
    model["compat"] = {**(model.get("compat") or {}), **compat}


def applyOpenAICompletionsCompatMetadata(model: Model) -> None:
    if model["api"] != "openai-completions":
        return
    detected = openAICompletionsCompatDelta(detectOpenAICompletionsCompat(model))
    model["compat"] = {**detected, **(model.get("compat") or {})}
    if len(model["compat"]) == 0:
        del model["compat"]


def applyAnthropicMessagesCompatMetadata(model: Model) -> None:
    if model["api"] != "anthropic-messages":
        return
    compat = getAnthropicMessagesCompat(model["provider"], model["id"])
    if compat:
        mergeAnthropicMessagesCompat(model, compat)
        if compat.get("supportsMidConvoEffort") and not supportsAnthropicDisabledThinkingWithMidConvoEffort(model["id"]):
            mergeThinkingLevelMap(model, {"off": None})


def isAnthropicFallbackMetadataModel(model: Model) -> bool:
    if model["provider"] != "anthropic" or model["api"] != "anthropic-messages":
        return False
    return model["id"] in ANTHROPIC_ALLOWED_FALLBACK_MODELS or any(
        model["id"] in fallbackModelIds for fallbackModelIds in ANTHROPIC_ALLOWED_FALLBACK_MODELS.values()
    )


def applyAnthropicAllowedFallbackModelMetadata(models: list[Model]) -> None:
    modelsById = {model["id"]: model for model in models}
    for modelId, fallbackModelIds in ANTHROPIC_ALLOWED_FALLBACK_MODELS.items():
        model = modelsById.get(modelId)
        if not model:
            continue

        compatibleFallbackModelIds = (
            [fallbackModelId for fallbackModelId in fallbackModelIds if supportsAnthropicMidConvoEffort(fallbackModelId)]
            if _get(model.get("compat"), "supportsMidConvoEffort")
            else fallbackModelIds
        )
        allowedFallbackModels = []
        for fallbackModelId in compatibleFallbackModelIds:
            fallbackModel = modelsById.get(fallbackModelId)
            if fallbackModel:
                allowedFallbackModels.append(
                    {"provider": fallbackModel["provider"], "model": fallbackModel["id"], "cost": fallbackModel["cost"]}
                )
        if len(allowedFallbackModels) > 0:
            mergeAnthropicMessagesCompat(model, {"allowedFallbackModels": allowedFallbackModels})


def applyStrictToolCompatMetadata(model: Model) -> None:
    if model["provider"] in ("openai", "cloudflare-ai-gateway") and model["api"] == "openai-responses":
        model["compat"] = {**(model.get("compat") or {}), "supportsStrictMode": True}
    elif model["provider"] == "anthropic" and model["api"] == "anthropic-messages":
        mergeAnthropicMessagesCompat(model, {"supportsStrictTools": True})


# Responses endpoints verified (OpenAI, ChatGPT Codex backend, GitHub Copilot,
# opencode zen) or documented (Azure OpenAI, Cloudflare AI Gateway) to pass
# OpenAI custom grammar tools through. OpenAI rejects `type: "custom"` tools
# for pre-GPT-5 models (gpt-4.x, gpt-4o, o-series).
OPENAI_GRAMMAR_TOOL_PROVIDERS = {
    "openai",
    "openai-codex",
    "azure",
    "github-copilot",
    "opencode",
    "cloudflare-ai-gateway",
}
OPENAI_GRAMMAR_TOOL_APIS = {
    "openai-responses",
    "azure-openai-responses",
    "openai-codex-responses",
}


def applyOpenAIGrammarToolCompatMetadata(model: Model) -> None:
    if model["api"] not in OPENAI_GRAMMAR_TOOL_APIS or model["provider"] not in OPENAI_GRAMMAR_TOOL_PROVIDERS:
        return
    match = re.match(r"^gpt-(\d+)", model["id"])
    if not match or int(match.group(1)) < 5:
        return
    model["compat"] = {**(model.get("compat") or {}), "supportsOpenAIGrammarTools": True}


def applyOpenAIToolSearchMetadata(model: Model) -> None:
    isOpenAIResponses = model["provider"] == "openai" and model["api"] == "openai-responses"
    isOpenAICodex = model["provider"] == "openai-codex" and model["api"] == "openai-codex-responses"
    if not (isOpenAIResponses or isOpenAICodex) or model["id"] not in OPENAI_TOOL_SEARCH_MODEL_IDS:
        return
    supportsAdditionalTools = (isOpenAIResponses and model["id"] in OPENAI_ADDITIONAL_TOOLS_MODEL_IDS) or (
        isOpenAICodex and model["id"] in OPENAI_CODEX_ADDITIONAL_TOOLS_MODEL_IDS
    )
    model["compat"] = {
        **(model.get("compat") or {}),
        **({"supportsAdditionalTools": True} if supportsAdditionalTools else {}),
        "supportsToolSearch": True,
    }


# Moonshot Kimi K2.6/K2.7 accept system text after the conversation starts but reject
# tool-bearing system messages. Kimi K3 accepts both forms; Fireworks and OpenCode pass
# its tool-bearing form through. GitHub Copilot forwards K3 text but silently drops its
# tool-bearing message. DeepSeek V4 Pro and OpenAI models behind OpenRouter also accept
# plain system text in place. Kimi Coding sets these flags in createKimiCodingModels().
def applyOpenAICompletionsTranscriptMetadata(model: Model) -> None:
    if model["api"] != "openai-completions":
        return
    provider, id = model["provider"], model["id"]
    isKimiK3 = (
        (provider.startswith("moonshot") and id == "kimi-k3")
        or (provider == "fireworks" and "kimi-k3" in id)
        or ((provider == "opencode" or provider == "opencode-go") and id == "kimi-k3")
    )
    isMoonshotKimiK2 = provider.startswith("moonshot") and id in ("kimi-k2.6", "kimi-k2.7-code", "kimi-k2.7-code-highspeed")
    isTextOnly = (
        isMoonshotKimiK2
        or (provider == "github-copilot" and id == "kimi-k3")
        or (provider == "deepseek" and id == "deepseek-v4-pro")
        or (
            provider == "openrouter"
            and id.startswith("openai/")
            and id[len("openai/") :] in OPENAI_MID_CONVO_SYSTEM_MESSAGE_MODEL_IDS
        )
    )
    if not isKimiK3 and not isTextOnly:
        return
    model["compat"] = {
        **(model.get("compat") or {}),
        "supportsMidConvoSystemMessages": True,
        **({"supportsMidConvoToolAdditions": True} if isKimiK3 else {}),
    }


# Newer OpenAI Responses models accept developer messages after the conversation has started.
# OpenCode Zen, OpenCode Go, and GitHub Copilot pass both those messages and
# `additional_tools` items through to OpenAI unchanged; tool search is not verified
# through those proxies.
OPENAI_RESPONSES_PROXY_PROVIDERS = {"opencode", "opencode-go", "github-copilot"}


def applyOpenAIResponsesTranscriptMetadata(model: Model) -> None:
    isOpenAIResponses = model["provider"] == "openai" and model["api"] == "openai-responses"
    isOpenAICodex = model["provider"] == "openai-codex" and model["api"] == "openai-codex-responses"
    isProxiedResponses = model["provider"] in OPENAI_RESPONSES_PROXY_PROVIDERS and model["api"] == "openai-responses"
    if not (isOpenAIResponses or isOpenAICodex or isProxiedResponses) or (
        model["id"] not in OPENAI_MID_CONVO_SYSTEM_MESSAGE_MODEL_IDS
    ):
        return
    model["compat"] = {
        **(model.get("compat") or {}),
        "supportsMidConvoSystemMessages": True,
        **({"supportsAdditionalTools": True} if isProxiedResponses else {}),
    }


# OpenAI charges prompt-cache writes starting with the GPT-5.6 family, and exactly
# those models accept `prompt_cache_options`; older models reject the parameter.
# https://developers.openai.com/api/docs/guides/prompt-caching
def applyOpenAIExplicitPromptCacheMetadata(model: Model) -> None:
    if model["provider"] != "openai" or model["api"] != "openai-responses":
        return
    if not (model["cost"]["cacheWrite"] > 0):
        return
    model["compat"] = {
        **(model.get("compat") or {}),
        "supportsExplicitPromptCacheMode": True,
    }


# GPT-6 models accept `configuration_update` input items that change the reasoning effort
# without changing the cached prompt prefix. Not enabled for openai-codex (Codex CLI keeps it
# behind a flag that is off by default) or proxies.
def applyOpenAIMidConvoEffortMetadata(model: Model) -> None:
    if model["provider"] != "openai" or model["api"] != "openai-responses":
        return
    if model["id"] not in OPENAI_MID_CONVO_EFFORT_MODEL_IDS:
        return
    model["compat"] = {
        **(model.get("compat") or {}),
        "supportsMidConvoEffort": True,
    }


# Anthropic ephemeral entries have a hard five-minute lifetime; `ttl: "1h"`
# extends it to one hour. Only direct Anthropic is annotated so cache warming
# does not assume equivalent behavior through proxies.
# https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
ANTHROPIC_PROMPT_CACHE = {"short": 300, "long": 3600}


def applyPromptCacheMetadata(model: Model) -> None:
    if model["provider"] == "anthropic" and model["api"] == "anthropic-messages":
        model["promptCache"] = ANTHROPIC_PROMPT_CACHE
    # Do not add OpenAI lifetimes yet. Before enabling warming for explicit
    # OpenAI caches, re-evaluate it using observed expiry, replay, and billing
    # behavior; a documented TTL alone does not establish full cache loss.


def applyImageInputMetadata(model: Model) -> None:
    if "image" not in model["input"]:
        return

    if model["provider"] == "anthropic":
        providerLimits: dict[str, Any] | None = {
            "maxRequestBytes": 32 * 1024 * 1024,
            "images": {
                "maxPerRequest": 100 if model.get("type") != "image" and model.get("contextWindow") == 200000 else 600
            },
        }
    elif model["provider"] == "amazon-bedrock":
        providerLimits = {"images": {"maxPerMessage": 20}}
    elif model["provider"] == "openai":
        providerLimits = {"maxRequestBytes": 512 * 1024 * 1024, "images": {"maxPerRequest": 1500}}
    elif model["provider"] == "google":
        providerLimits = {"maxRequestBytes": 20 * 1024 * 1024, "images": {"maxPerRequest": 3600}}
    else:
        providerLimits = None
    configuredImages = _get(model.get("inputLimits"), "images")
    model["inputLimits"] = {
        **(providerLimits or {}),
        **(model.get("inputLimits") or {}),
        "images": {
            **(_get(providerLimits, "images") or {}),
            **(configuredImages or {}),
            "resize": {**DEFAULT_IMAGE_RESIZE, **(_get(configuredImages, "resize") or {})},
        },
    }


def isGemma4Model(modelId: str) -> bool:
    return re.search(r"gemma-?4", modelId.lower()) is not None


def getGoogleThinkingLevelMap(modelId: str, reasoningOptions: list[dict[str, Any]]) -> dict[str, Any] | None:
    effortMap = getEffortThinkingLevelMap(reasoningOptions)
    if effortMap:
        return effortMap
    if isGemma4Model(modelId):
        return {"off": None, "minimal": "MINIMAL", "low": None, "medium": None, "high": "HIGH"}
    return None


def applyThinkingLevelMetadata(model: Model) -> None:
    id, api, provider = model["id"], model["api"], model["provider"]
    if api in ("openai-responses", "azure-openai-responses") and id.startswith("gpt-5"):
        mergeThinkingLevelMap(model, {"off": None})
    if id in ("gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-6.1-sol") and api in (
        "openai-responses",
        "azure-openai-responses",
        "openai-codex-responses",
    ):
        mergeThinkingLevelMap(
            model,
            {
                # GPT-6 Astra and GPT-6.1 Sol reject reasoning.effort "none".
                "off": None if id in ("gpt-6-astra", "gpt-6.1-sol") else "none",
                "minimal": None,
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "xhigh",
                "max": "max",
            },
        )
    if provider == "github-copilot" and id.startswith("gpt-5"):
        mergeThinkingLevelMap(model, {"minimal": "low"})
    if api == "openai-responses" and provider == "openai" and id in OPENAI_RESPONSES_NONE_REASONING_MODELS:
        mergeThinkingLevelMap(model, {"off": "none"})
    # xAI models without verified effort options must not send the undocumented
    # "none"/"minimal" efforts.
    if provider == "xai" and api == "openai-responses" and model.get("thinkingLevelMap") is None:
        mergeThinkingLevelMap(model, {"off": None, "minimal": None})
    if supportsOpenAiXhigh(id):
        mergeThinkingLevelMap(model, {"xhigh": "xhigh"})
    if supportsOpenAiMax(model):
        mergeThinkingLevelMap(model, {"max": "max"})
    if provider == "openai" and id == "gpt-5.5":
        mergeThinkingLevelMap(model, {"minimal": None})
    if id.endswith("gpt-5.5-pro"):
        mergeThinkingLevelMap(model, {"off": None, "minimal": None, "low": None})
    # Anthropic adaptive-thinking effort support (per Anthropic adaptive thinking docs):
    # - "max" is available on all adaptive-thinking Claude models.
    # - "xhigh" is only available on Opus 4.7/4.8/5, Sonnet 5, Haiku 5.5, and Fable 5.
    if "opus-4-6" in id or "opus-4.6" in id or "sonnet-4-6" in id or "sonnet-4.6" in id:
        mergeThinkingLevelMap(model, {"max": "max"})
    if (
        "opus-4-7" in id
        or "opus-4.7" in id
        or "opus-4-8" in id
        or "opus-4.8" in id
        or "opus-5" in id
        or "opus.5" in id
        or "sonnet-5" in id
        or "sonnet.5" in id
        or "haiku-5" in id
        or "haiku.5" in id
    ):
        mergeThinkingLevelMap(model, {"xhigh": "xhigh", "max": "max"})
    if "fable-5" in id:
        mergeThinkingLevelMap(model, {"off": None, "xhigh": "xhigh", "max": "max"})
    if api == "anthropic-messages" and isAnthropicAdaptiveThinkingModel(id):
        mergeAnthropicMessagesCompat(model, {"forceAdaptiveThinking": True})
    if api == "anthropic-messages" and isAnthropicTemperatureUnsupportedModel(id):
        mergeAnthropicMessagesCompat(model, {"supportsTemperature": False})
    if api == "openai-completions" and "deepseek-v4" in id and model.get("thinkingLevelMap") is None:
        if provider == "openrouter":
            deepseekMap: dict[str, Any] = {**DEEPSEEK_V4_THINKING_LEVEL_MAP, "xhigh": "xhigh", "max": None}
        elif provider == "azure":
            deepseekMap = AZURE_DEEPSEEK_V4_THINKING_LEVEL_MAP
        elif provider in ("deepseek", "opencode", "opencode-go") and "deepseek-v4-flash" in id:
            deepseekMap = DEEPSEEK_V4_FLASH_THINKING_LEVEL_MAP
        else:
            deepseekMap = DEEPSEEK_V4_THINKING_LEVEL_MAP
        mergeThinkingLevelMap(model, deepseekMap)
    if provider == "groq" and id == "qwen/qwen3.6-27b":
        mergeThinkingLevelMap(model, {"minimal": None, "low": None, "medium": None, "high": "default"})
    if provider == "openai-codex" and supportsOpenAiXhigh(id):
        mergeThinkingLevelMap(model, {"minimal": "low"})
    if provider in ("moonshotai", "moonshotai-cn") and id in ("kimi-k2.7-code", "kimi-k2.7-code-highspeed"):
        # Kimi K2.7 Code is always-thinking. Official docs say
        # `thinking: { type: "disabled" }` is rejected, and callers can omit
        # the thinking parameter to use the enabled default.
        mergeThinkingLevelMap(model, {"off": None})
    if provider == "openrouter" and id.startswith("inception/mercury-2"):
        # Mercury 2 in instant mode (reasoning_effort: "none") disables tool calling.
        # Mark "off" unsupported so the openai-completions provider omits the reasoning param
        # instead of defaulting to {reasoning:{effort:"none"}} (see openai-completions.ts:575).
        # Pi's low/medium/high pass through verbatim; OpenRouter normalizes to Mercury's vocabulary.
        mergeThinkingLevelMap(model, {"off": None})
    if provider == "openrouter" and id == "z-ai/glm-5.2":
        mergeThinkingLevelMap(model, {"xhigh": "xhigh"})
    if provider == "fireworks":
        if api == "anthropic-messages" and _get(model.get("compat"), "forceAdaptiveThinking"):
            # Qwen Max currently advertises only a toggle. Prefer upstream effort
            # metadata once available instead of replacing it with this fallback.
            if id == "accounts/fireworks/models/qwen3p8-max" and not model.get("thinkingLevelMap"):
                model["thinkingLevelMap"] = getEffortThinkingLevelMap(
                    [
                        {"type": "effort", "values": ["low", "medium", "xhigh"]},
                    ]
                )
            reasoningOptions = modelsDevReasoningOptions.get(getModelKey(model))
            if (
                any(option.get("type") == "toggle" for option in reasoningOptions or [])
                # The 2.4T alias omits the verified toggle in models.dev.
                or id == "accounts/fireworks/models/qwen3p8-2p4t-a95b"
            ):
                mergeThinkingLevelMap(model, {"off": "none"})
            if id == "accounts/fireworks/models/deepseek-v4-pro-0813":
                mergeThinkingLevelMap(model, {"low": "low"})
        if "glm-5p2" in id:
            # GLM 5.2 and its fast router support off/high/max. Fireworks maps low
            # and medium to high, so do not expose those aliases as distinct levels.
            mergeThinkingLevelMap(model, {"off": "none", "minimal": None, "low": None, "medium": None, "max": "max"})
        if "kimi-k3" in id:
            # Fireworks maps medium to high on both APIs; do not expose it as a distinct level.
            mergeThinkingLevelMap(model, {"medium": None})
    if provider == "opencode-go" and id == "glm-5.2":
        mergeThinkingLevelMap(model, OPENCODE_GO_GLM52_THINKING_LEVEL_MAP)
    if provider == "opencode-go" and id == "kimi-k2.6":
        # OpenCode Go exposes Kimi K2.6 thinking as on/off, not distinct effort tiers.
        mergeThinkingLevelMap(model, {"minimal": None, "low": None, "medium": None})
    if provider == "opencode" and id == "grok-build-0.1":
        # OpenCode Zen Grok Build reasons by default but rejects explicit reasoningEffort.
        mergeThinkingLevelMap(model, {"off": None, "minimal": None, "low": None, "medium": None})
    if provider == "ant-ling" and model.get("reasoning"):
        # Ring reasons by default. Only high/xhigh have documented explicit effort controls.
        mergeThinkingLevelMap(model, ANT_LING_RING_THINKING_LEVEL_MAP)
    if provider == "github-copilot":
        override = GITHUB_COPILOT_THINKING_LEVEL_OVERRIDES.get(id)
        if override:
            mergeThinkingLevelMap(model, override)


def getAnthropicMessagesCompat(provider: str, modelId: str) -> dict[str, Any] | None:
    compat: dict[str, Any] = {}
    if (
        provider in VERIFIED_ANTHROPIC_MID_CONVO_EFFORT_PROVIDERS
        and supportsAnthropicMidConvoEffort(modelId)
        and f"{provider}:{modelId}" not in MID_CONVO_EFFORT_UNSUPPORTED_ANTHROPIC_MODELS
    ):
        compat["supportsMidConvoEffort"] = True
    if provider == "anthropic" and supportsAnthropicMidConvoSystemMessages(modelId):
        compat["supportsMidConvoSystemMessages"] = True
        compat["supportsMidConvoToolChanges"] = True
    # OpenCode Zen and GitHub Copilot forward mid-conversation system messages but reject
    # `tool_addition`/`tool_removal` blocks, so tool changes stay top-level there.
    if provider in ("opencode", "github-copilot") and supportsAnthropicMidConvoSystemMessages(modelId):
        compat["supportsMidConvoSystemMessages"] = True
    if f"{provider}:{modelId}" in EAGER_TOOL_INPUT_STREAMING_UNSUPPORTED_ANTHROPIC_MODELS:
        compat["supportsEagerToolInputStreaming"] = False
    if provider == "xiaomi" or provider.startswith("xiaomi-token-plan-"):
        compat["allowEmptySignature"] = True
    # OpenCode Qwen 3.8 Flash emits and accepts thinking blocks with empty signatures.
    if provider in ("opencode", "opencode-go") and modelId == "qwen3.8-flash":
        compat["allowEmptySignature"] = True
    return compat if len(compat) > 0 else None


def getBedrockBaseUrl(modelId: str) -> str:
    return (
        "https://bedrock-runtime.eu-central-1.amazonaws.com"
        if modelId.startswith("eu.")
        else "https://bedrock-runtime.us-east-1.amazonaws.com"
    )


def normalizeNvidiaModelId(modelId: str) -> str:
    return modelId.lower().replace("_", ".")


def roundCost(value: float) -> float:
    return float(toFixed(value, 6))


def _truthy(value: Any) -> bool:
    """JavaScript truthiness: objects and arrays are true even when empty."""
    if value is None or value is UNDEFINED or value is False:
        return False
    if isinstance(value, int | float):
        return value != 0 and not math.isnan(value)
    if isinstance(value, str):
        return value != ""
    return True


def _or(*values: Any) -> Any:
    """``a || b || ...``: the first truthy value, else the last."""
    for value in values[:-1]:
        if _truthy(value):
            return value
    return values[-1]


def _nullish(value: Any, fallback: Any) -> Any:
    """``value ?? fallback``."""
    return fallback if value is None or value is UNDEFINED else value


def getModelsDevCost(cost: Any) -> dict[str, Any]:
    base = {
        "input": _or(_get(cost, "input"), 0),
        "output": _or(_get(cost, "output"), 0),
        "cacheRead": _or(_get(cost, "cache_read"), 0),
        "cacheWrite": _or(_get(cost, "cache_write"), 0),
    }
    # Rates a tier does not list keep the base price.
    sourceTiers = _get(cost, "tiers")
    tiers: list[dict[str, Any]] | None = None
    if sourceTiers is not None:
        tiers = []
        for tier in sourceTiers:
            context = _get(tier, "tier")
            if _get(context, "type") != "context" or "size" not in context:
                continue
            tiers.append(
                {
                    "inputTokensAbove": context["size"],
                    "input": _nullish(tier.get("input"), base["input"]),
                    "output": _nullish(tier.get("output"), base["output"]),
                    "cacheRead": _nullish(tier.get("cache_read"), base["cacheRead"]),
                    "cacheWrite": _nullish(tier.get("cache_write"), base["cacheWrite"]),
                }
            )

    return {**base, "tiers": tiers} if tiers and len(tiers) > 0 else base


async def _json(response: Any) -> Any:
    """``await response.json()``, with the objects' keys in JavaScript's order."""
    return jsObjectOrder(await response.json())


def _error(error: BaseException) -> str:
    """What ``console.error(error)`` prints of an error: its traceback."""
    return "".join(traceback.format_exception(error)).rstrip()


def _log(*parts: Any) -> None:
    print(*parts, flush=True)


def _logError(message: str, error: BaseException) -> None:
    print(message, _error(error), file=sys.stderr, flush=True)


async def fetchNvidiaNimModelIds() -> dict[str, str]:
    try:
        _log("Fetching models from NVIDIA NIM API...")
        response = await whatwg_fetch.fetch(f"{NVIDIA_BASE_URL}/models")
        if not response.ok:
            raise RuntimeError(f"NVIDIA NIM API returned {response.status}")
        data = await _json(response)
        modelIds: dict[str, str] = {}

        for model in _nullish(_get(data, "data"), []):
            modelIds[model["id"]] = model["id"]
            modelIds[normalizeNvidiaModelId(model["id"])] = model["id"]

        listed = _get(data, "data")
        _log(f"Fetched {len(listed) if listed is not None else 0} model IDs from NVIDIA NIM")
        return modelIds
    except Exception as error:
        _logError("Failed to fetch NVIDIA NIM models:", error)
        if generatorOptions["strict"]:
            raise
        return {}


async def fetchOpenRouterList(query: str) -> list[dict[str, Any]]:
    response = await whatwg_fetch.fetch(f"https://openrouter.ai/api/v1/models{query}")
    if not response.ok:
        raise RuntimeError(f"OpenRouter API returned {response.status}")
    data = await _json(response)
    return _nullish(_get(data, "data"), [])


async def fetchOpenRouterModels() -> dict[str, list[Model]]:
    try:
        _log("Fetching models from OpenRouter API...")
        listed, imageListed, decisionListed = await asyncio.gather(
            fetchOpenRouterList(""),
            fetchOpenRouterList("?output_modalities=image"),
            fetchOpenRouterList("?output_modalities=decisions"),
        )
        catalog = buildOpenRouterCatalog(listed, imageListed, decisionListed)
        _log(
            f"Fetched {len(catalog['chat'])} tool-capable, {len(catalog['images'])} image, and "
            f"{len(catalog['classifiers'])} classifier models from OpenRouter"
        )
        if generatorOptions["strict"] and len(catalog["images"]) == 0:
            raise RuntimeError("OpenRouter API returned no usable image models")
        return catalog
    except Exception as error:
        _logError("Failed to fetch OpenRouter models:", error)
        if generatorOptions["strict"]:
            raise
        return {"chat": [], "images": [], "classifiers": []}


# MISAKA fork: misaka's runtime `radius_config` validates gateway models into pydantic models, which keep neither the
# fields `Model` does not declare nor the gateway's key order. The catalog keeps the gateway's entries as upstream's
# spread does, so these are upstream's radius-config.ts functions over the raw JSON.
DEFAULT_RADIUS_GATEWAY = "https://radius.pi.dev"


def isRadiusGatewayModel(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    model = value
    return (
        isinstance(model.get("id"), str)
        and isinstance(model.get("name"), str)
        and isinstance(model.get("reasoning"), bool)
        and isinstance(model.get("input"), list)
        and isinstance(model.get("cost"), dict)
        and isinstance(model.get("contextWindow"), int | float)
        and not isinstance(model.get("contextWindow"), bool)
        and isinstance(model.get("maxTokens"), int | float)
        and not isinstance(model.get("maxTokens"), bool)
    )


def sanitizeRadiusGatewayConfig(config: Any) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        return None
    baseUrl, models = config.get("baseUrl"), config.get("models")
    if not isinstance(baseUrl, str) or not isinstance(models, list):
        return None
    return {
        "baseUrl": baseUrl,
        "models": [{**model} for model in models if isRadiusGatewayModel(model)],
    }


def getRadiusModelsFromConfig(providerId: str, config: dict[str, Any]) -> list[Model]:
    return [
        {
            **model,
            "api": "pi-messages",
            "provider": providerId,
            "baseUrl": config["baseUrl"],
        }
        for model in config["models"]
    ]


def truncateHttpBody(body: str) -> str:
    trimmed = jsTrim(body)
    return f"{jsSlice(trimmed, 0, 512)}…" if jsLength(trimmed) > 512 else trimmed


async def loadRadiusGatewayConfig(gateway: str, apiKey: str | None = None) -> dict[str, Any]:
    headers = {"accept": "application/json"}
    if apiKey:
        headers["authorization"] = f"Bearer {apiKey}"
    response = await whatwg_fetch.fetch(URL("/v1/config", gateway), {"headers": headers})
    if not response.ok:
        raise RuntimeError(
            f"Could not load Radius config from {gateway}: {response.status}: {truncateHttpBody(await response.text())}"
        )
    config = sanitizeRadiusGatewayConfig(await _json(response))
    if not config:
        raise RuntimeError(f"Invalid Radius config from {gateway}")
    return config


async def fetchRadiusModels() -> list[Model]:
    try:
        _log("Fetching models from Radius API...")
        config = await loadRadiusGatewayConfig(DEFAULT_RADIUS_GATEWAY)
        models = getRadiusModelsFromConfig("radius", config)
        if len(models) == 0:
            raise RuntimeError("Radius API returned no models")
        _log(f"Fetched {len(models)} models from Radius")
        return models
    except Exception as error:
        _logError("Failed to fetch Radius models:", error)
        if generatorOptions["strict"]:
            raise
        return []


async def fetchAiGatewayModels() -> dict[str, list[Model]]:
    try:
        _log("Fetching models from Vercel AI Gateway API...")
        response = await whatwg_fetch.fetch(f"{AI_GATEWAY_MODELS_URL}/models")
        if not response.ok:
            raise RuntimeError(f"Vercel AI Gateway API returned {response.status}")
        data = await _json(response)
        models: list[Model] = []
        classifiers: list[Model] = []

        def toNumber(value: Any) -> float:
            if isinstance(value, int | float) and not isinstance(value, bool):
                return value if math.isfinite(value) else 0
            parsed = parseFloat("0" if value is None else value)
            return parsed if math.isfinite(parsed) else 0

        items = data["data"] if isinstance(_get(data, "data"), list) else []
        for model in items:
            # Evaluation models such as TypeSafe's Jev are served through the
            # TypeSafe-compatible System One endpoint.
            if model.get("type") == "evaluation":
                classifiers.append(
                    {
                        "type": "classifier",
                        "id": model["id"],
                        "name": model.get("name") or model["id"],
                        "api": "typesafe-system-one",
                        "provider": "vercel-ai-gateway",
                        "baseUrl": AI_GATEWAY_TYPESAFE_BASE_URL,
                        "input": ["text"],
                        "cost": {
                            "input": roundCost(toNumber(_get(model.get("pricing"), "input")) * 1_000_000),
                            "output": roundCost(toNumber(_get(model.get("pricing"), "output")) * 1_000_000),
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                        "contextWindow": model.get("context_window") or 4096,
                    }
                )
                continue
            tags = model["tags"] if isinstance(model.get("tags"), list) else []
            # Only include models that support tools
            if "tool-use" not in tags:
                continue

            input = ["text"]
            if "vision" in tags:
                input.append("image")

            models.append(
                {
                    "id": model["id"],
                    "name": model.get("name") or model["id"],
                    "api": "anthropic-messages",
                    "baseUrl": AI_GATEWAY_BASE_URL,
                    "provider": "vercel-ai-gateway",
                    "reasoning": "reasoning" in tags,
                    "input": input,
                    "compat": {"allowEmptySignature": True},
                    "cost": getAiGatewayCost(model.get("pricing")),
                    "contextWindow": model.get("context_window") or 4096,
                    "maxTokens": model.get("max_tokens") or 4096,
                }
            )

        _log(f"Fetched {len(models)} tool-capable and {len(classifiers)} classifier models from Vercel AI Gateway")
        return {"chat": models, "classifiers": classifiers}
    except Exception as error:
        _logError("Failed to fetch Vercel AI Gateway models:", error)
        if generatorOptions["strict"]:
            raise
        return {"chat": [], "classifiers": []}


def _models(data: Any, key: str) -> dict[str, Any] | None:
    """``data[key]?.models``."""
    return _get(_get(data, key), "models")


def _limit(model: dict[str, Any], key: str, fallback: int) -> Any:
    """``m.limit?.[key] || fallback``."""
    return _or(_get(model.get("limit"), key), fallback)


def _inputModalities(model: dict[str, Any]) -> list[str]:
    """``m.modalities?.input?.includes("image") ? ["text", "image"] : ["text"]``."""
    return ["text", "image"] if _includes(_get(model.get("modalities"), "input"), "image") else ["text"]


def processZaiModels(data: dict[str, Any]) -> list[Model]:
    variants = [
        {
            "source": "zai-coding-plan",
            "provider": "zai",
            "baseUrl": "https://api.z.ai/api/coding/paas/v4",
        },
        {
            "source": "zhipuai-coding-plan",
            "provider": "zai-coding-cn",
            "baseUrl": "https://open.bigmodel.cn/api/coding/paas/v4",
        },
    ]
    models: list[Model] = []

    for variant in variants:
        source, provider, baseUrl = variant["source"], variant["provider"], variant["baseUrl"]
        for modelId, m in (_models(data, source) or {}).items():
            if m.get("tool_call") is not True:
                continue
            supportsImage = _includes(_get(m.get("modalities"), "input"), "image")

            thinkingLevelMap = getEffortThinkingLevelMap(_nullish(m.get("reasoning_options"), []))
            isGlm52 = modelId in ("glm-5.2", "glm-5.2-highspeed")
            if thinkingLevelMap and isGlm52:
                thinkingLevelMap["off"] = "none"
            supportsReasoningEffort = thinkingLevelMap is not None
            referenceCost = _nullish(_get(_get(_models(data, "zai"), modelId), "cost"), m.get("cost"))

            models.append(
                {
                    "id": modelId,
                    "name": m.get("name") or modelId,
                    "api": "openai-completions",
                    "provider": provider,
                    "baseUrl": baseUrl,
                    "reasoning": m.get("reasoning") is True,
                    **({"thinkingLevelMap": thinkingLevelMap} if thinkingLevelMap else {}),
                    "input": ["text", "image"] if supportsImage else ["text"],
                    "cost": getModelsDevCost(referenceCost),
                    "compat": {
                        "supportsDeveloperRole": False,
                        "thinkingFormat": "zai",
                        **({"supportsReasoningEffort": True} if supportsReasoningEffort else {}),
                        **({"zaiToolStream": True} if modelId not in ZAI_TOOL_STREAM_UNSUPPORTED_MODELS else {}),
                    },
                    "contextWindow": _limit(m, "context", 4096),
                    "maxTokens": _limit(m, "output", 4096),
                }
            )
            recordModelsDevReasoningOptions(provider, modelId, m)

    return models


def processBasetenModels(provider: dict[str, Any] | None) -> list[Model]:
    if not _get(provider, "models"):
        return []

    baseUrl = "https://inference.baseten.co/v1"
    baseCompat = {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "supportsUsageInStreaming": True,
        "maxTokensField": "max_tokens",
        "supportsStrictMode": True,
        # Baseten automatic prompt caching needs session affinity so related
        # requests land on the same replica. See:
        # https://docs.baseten.co/inference/model-apis/pricing-and-limits
        "sendSessionAffinityHeaders": True,
        "supportsLongCacheRetention": False,
    }
    reasoningEffortCompat = {
        **baseCompat,
        "supportsReasoningEffort": True,
        "thinkingFormat": "openai",
    }
    toggleReasoningCompat = {
        **baseCompat,
        "thinkingFormat": "baseten",
        "chatTemplateArgs": {"enable_thinking": {"$var": "thinking.enabled"}},
    }
    toggleReasoningEffortCompat = {
        **reasoningEffortCompat,
        "thinkingFormat": "baseten",
        "chatTemplateArgs": {"enable_thinking": {"$var": "thinking.enabled"}},
    }
    toggleThinkingLevelMap = {
        "off": "off",
        "minimal": None,
        "low": None,
        "medium": None,
        "high": "high",
        "xhigh": None,
        "max": None,
    }
    glm52ThinkingLevelMap = {
        "off": "none",
        "minimal": None,
        "low": None,
        "medium": None,
        "high": "high",
        "xhigh": None,
        "max": "max",
    }
    models: list[Model] = []

    for modelId, model in provider["models"].items():  # type: ignore[index]
        if model.get("status") == "deprecated":
            continue

        reasoning = model.get("reasoning") is True
        reasoningOptions = _nullish(model.get("reasoning_options"), [])
        isGlm52 = modelId in ("zai-org/GLM-5.2", "zai-org/GLM-5.2-Fast")
        supportsToggle = any(option.get("type") == "toggle" for option in reasoningOptions) or isGlm52
        supportsEffort = any(option.get("type") == "effort" for option in reasoningOptions) or isGlm52
        if supportsToggle and supportsEffort:
            compat = toggleReasoningEffortCompat
        elif supportsToggle:
            compat = toggleReasoningCompat
        elif supportsEffort:
            compat = reasoningEffortCompat
        else:
            compat = baseCompat
        thinkingLevelMap = (
            glm52ThinkingLevelMap
            if isGlm52
            else toggleThinkingLevelMap
            if supportsToggle
            else getEffortThinkingLevelMap(reasoningOptions)
        )
        # Baseten's GLM-5.2 endpoints are text-only despite models.dev reporting image input.
        supportsImageInput = not isGlm52 and _includes(_get(model.get("modalities"), "input"), "image")

        models.append(
            {
                "id": modelId,
                "name": model.get("name") or modelId,
                "api": "openai-completions",
                "provider": "baseten",
                "baseUrl": baseUrl,
                "reasoning": reasoning,
                **({"thinkingLevelMap": thinkingLevelMap} if thinkingLevelMap else {}),
                "input": ["text", "image"] if supportsImageInput else ["text"],
                "cost": getModelsDevCost(model.get("cost")),
                "compat": compat,
                "contextWindow": _limit(model, "context", 4096),
                "maxTokens": _limit(model, "output", 4096),
            }
        )

    return models


def processGoogleModels(data: dict[str, Any]) -> list[Model]:
    models: list[Model] = []
    googleModels = _models(data, "google")
    if googleModels:
        for modelId, model in googleModels.items():
            if model.get("tool_call") is not True:
                continue
            if modelId == "gemini-flash-latest":
                source = _nullish(googleModels.get("gemini-3.5-flash"), model)
            elif modelId == "gemini-flash-lite-latest":
                source = _nullish(googleModels.get("gemini-3.1-flash-lite"), model)
            else:
                source = model
            thinkingLevelMap = getGoogleThinkingLevelMap(modelId, _nullish(source.get("reasoning_options"), []))

            models.append(
                {
                    "id": modelId,
                    "name": model.get("name") or modelId,
                    "api": "google-generative-ai",
                    "provider": "google",
                    "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
                    "reasoning": source.get("reasoning") is True,
                    **({"thinkingLevelMap": thinkingLevelMap} if thinkingLevelMap else {}),
                    "input": _inputModalities(source),
                    "cost": getModelsDevCost(source.get("cost")),
                    "contextWindow": _limit(source, "context", 4096),
                    "maxTokens": _limit(source, "output", 4096),
                }
            )

    # The google-vertex models.dev catalog also includes Claude, OpenAI, and other
    # MaaS models that do not use the @google/genai Gemini streaming path.
    vertexModels = _models(data, "google-vertex")
    if vertexModels:
        for modelId, model in vertexModels.items():
            if model.get("tool_call") is not True or not modelId.startswith("gemini-"):
                continue
            if modelId == "gemini-3.1-flash-lite-preview":
                continue
            if modelId == "gemini-flash-latest":
                source = _nullish(vertexModels.get("gemini-3.5-flash"), model)
            elif modelId == "gemini-flash-lite-latest":
                source = _nullish(vertexModels.get("gemini-3.1-flash-lite"), model)
            else:
                source = model
            thinkingLevelMap = getGoogleThinkingLevelMap(modelId, _nullish(source.get("reasoning_options"), []))
            # models.dev reports Vertex cache_read/cache_write values for Gemini 2.5 Flash that
            # do not match the official Gemini API standard pricing table. pi only accounts
            # cachedContentTokenCount as cacheRead.
            cacheRead = 0.03 if modelId == "gemini-2.5-flash" else _or(_get(source.get("cost"), "cache_read"), 0)

            models.append(
                {
                    "id": modelId,
                    "name": model.get("name") or modelId,
                    "api": "google-vertex",
                    "provider": "google-vertex",
                    "baseUrl": VERTEX_BASE_URL,
                    "reasoning": source.get("reasoning") is True,
                    **({"thinkingLevelMap": thinkingLevelMap} if thinkingLevelMap else {}),
                    "input": _inputModalities(source),
                    "cost": {
                        "input": _or(_get(source.get("cost"), "input"), 0),
                        "output": _or(_get(source.get("cost"), "output"), 0),
                        "cacheRead": cacheRead,
                        "cacheWrite": 0,
                    },
                    "contextWindow": _limit(source, "context", 4096),
                    "maxTokens": _limit(source, "output", 4096),
                }
            )

    return models


def processFireworksModels(provider: dict[str, Any] | None) -> list[Model]:
    if not _get(provider, "models"):
        return []

    anthropicCompat = {
        "allowEmptySignature": True,
        "sendSessionAffinityHeaders": True,
        "supportsEagerToolInputStreaming": False,
        "supportsCacheControlOnTools": False,
        "supportsLongCacheRetention": False,
    }
    openAICompat = {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "sendSessionAffinityHeaders": True,
        "supportsLongCacheRetention": False,
    }
    kimiK3Compat = {
        **openAICompat,
        "requiresReasoningContentOnAssistantMessages": True,
        "thinkingFormat": "openai",
    }
    models: list[Model] = []

    for modelId, model in provider["models"].items():  # type: ignore[index]
        if model.get("tool_call") is not True:
            continue

        input = _inputModalities(model)
        common = {
            "id": modelId,
            "name": model.get("name") or modelId,
            "provider": "fireworks",
            "reasoning": model.get("reasoning") is True,
            "input": input,
            "cost": getModelsDevCost(model.get("cost")),
            "contextWindow": _limit(model, "context", 4096),
            "maxTokens": _limit(model, "output", 4096),
        }

        if "glm-" in modelId:
            models.append(
                {
                    **common,
                    "api": "openai-completions",
                    "baseUrl": "https://api.fireworks.ai/inference/v1",
                    "compat": openAICompat,
                }
            )
        elif "kimi-k3" in modelId:
            models.append(
                {
                    **common,
                    "api": "openai-completions",
                    "baseUrl": "https://api.fireworks.ai/inference/v1",
                    "compat": kimiK3Compat,
                }
            )
        else:
            effort = any(option.get("type") == "effort" for option in model.get("reasoning_options") or [])
            models.append(
                {
                    **common,
                    "api": "anthropic-messages",
                    # Fireworks Anthropic-compatible API - SDK appends /v1/messages.
                    "baseUrl": "https://api.fireworks.ai/inference",
                    # Fireworks prompt caching uses automatic prefix matching + session affinity.
                    # x-session-affinity routes requests to the same replica for cache hits.
                    # cache_control on tools and eager_input_streaming are not supported.
                    # See: https://docs.fireworks.ai/tools-sdks/anthropic-compatibility
                    # Use adaptive thinking for cataloged effort controls, with verified
                    # fallbacks where models.dev is incomplete. New models need no allowlist entry.
                    "compat": {
                        **anthropicCompat,
                        **(
                            {"forceAdaptiveThinking": True}
                            if effort or modelId in FIREWORKS_ADAPTIVE_THINKING_FALLBACK_MODELS
                            else {}
                        ),
                    },
                }
            )
        recordModelsDevReasoningOptions("fireworks", modelId, model)

    return models


# Kimi Coding is subscription-backed, so models.dev reports zero cost. Use the
# equivalent Moonshot API rates to estimate the value of subscription usage.
KIMI_CODING_IMPLIED_COSTS: dict[str, dict[str, Any]] = {
    "k3": KIMI_K3_COST,
    "kimi-for-coding": {"input": 0.95, "output": 4, "cacheRead": 0.19, "cacheWrite": 0},
    "kimi-for-coding-highspeed": {"input": 1.9, "output": 8, "cacheRead": 0.38, "cacheWrite": 0},
}
# Models whose https://api.kimi.com/coding/v1/models entry reports effort levels and
# dynamic tool loading (tools declared in later system messages). Highspeed reports
# neither and rejects tool-bearing system messages with a 400.
KIMI_CODING_K3_STYLE_MODEL_IDS = {"k3", "k3-256k", "kimi-for-coding"}


# Kimi Coding uses Chat Completions, the same protocol as the official Kimi Code client.
def createKimiCodingModels(kimiModels: dict[str, Any]) -> list[Model]:
    models: list[Model] = []
    for modelId, m in kimiModels.items():
        if m.get("tool_call") is not True:
            continue

        isK3Style = modelId in KIMI_CODING_K3_STYLE_MODEL_IDS
        impliedCost = KIMI_CODING_IMPLIED_COSTS.get(modelId)
        # K3-style models send reasoning_effort, so expose the effort values models.dev lists.
        effortLevels = (
            getEffortThinkingLevelMap(m["reasoning_options"])
            if isK3Style and _truthy(m.get("reasoning_options"))
            else None
        )
        cost = m.get("cost")

        models.append(
            {
                "id": modelId,
                "name": _or(m.get("name"), modelId),
                "api": "openai-completions",
                "provider": "kimi-coding",
                "baseUrl": "https://api.kimi.com/coding/v1",
                "compat": {
                    **(KIMI_K3_OPENAI_COMPAT if isK3Style else KIMI_OPENAI_COMPAT),
                    # All models accept system text after the conversation starts.
                    "supportsMidConvoSystemMessages": True,
                    **({"supportsMidConvoToolAdditions": True} if isK3Style else {}),
                },
                "reasoning": m.get("reasoning") is True,
                "input": _inputModalities(m),
                "cost": {
                    "input": _or(_get(cost, "input"), _get(impliedCost, "input"), 0),
                    "output": _or(_get(cost, "output"), _get(impliedCost, "output"), 0),
                    "cacheRead": _or(_get(cost, "cache_read"), _get(impliedCost, "cacheRead"), 0),
                    "cacheWrite": _or(_get(cost, "cache_write"), _get(impliedCost, "cacheWrite"), 0),
                },
                "contextWindow": _limit(m, "context", 4096),
                "maxTokens": _limit(m, "output", 4096),
                # Kimi Code reports every model as always-thinking.
                "thinkingLevelMap": {**(effortLevels or {}), "off": None},
            }
        )
    return models


async def loadModelsDevData() -> list[Model]:
    try:
        _log("Fetching models from models.dev API...")
        response = await whatwg_fetch.fetch("https://models.dev/api.json")
        if not response.ok:
            raise RuntimeError(f"models.dev API returned {response.status}")
        data = await _json(response)

        models: list[Model] = []
        nvidiaNimModelIds = await fetchNvidiaNimModelIds() if _truthy(_models(data, "nvidia")) else {}

        # Process Amazon Bedrock models
        if _truthy(_models(data, "amazon-bedrock")):
            for modelId, m in data["amazon-bedrock"]["models"].items():
                if m.get("tool_call") is not True:
                    continue
                if modelId in BEDROCK_INFERENCE_PROFILE_ONLY_MODEL_IDS:
                    continue

                id = modelId

                if id.startswith("ai21.jamba"):
                    # These models doesn't support tool use in streaming mode
                    continue

                if id.startswith("mistral.mistral-7b-instruct-v0"):
                    # These models doesn't support system messages
                    continue

                models.append(
                    {
                        "id": id,
                        "name": _or(m.get("name"), id),
                        "api": "bedrock-converse-stream",
                        "provider": "amazon-bedrock",
                        "baseUrl": getBedrockBaseUrl(id),
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        # Includes models.dev pricing tiers, e.g. the long-context tier for OpenAI models (#10326).
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                        **({"compat": {"supportsStrictMode": True}} if m.get("structured_output") is True else {}),
                    }
                )
                recordModelsDevReasoningOptions("amazon-bedrock", id, m)

        # Process Anthropic models
        if _truthy(_models(data, "anthropic")):
            for modelId, m in data["anthropic"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "anthropic-messages",
                        "provider": "anthropic",
                        "baseUrl": "https://api.anthropic.com",
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions("anthropic", modelId, m)

        models.extend(processGoogleModels(data))

        # Process OpenAI models
        if _truthy(_models(data, "openai")):
            for modelId, m in data["openai"]["models"].items():
                if m.get("tool_call") is not True:
                    continue
                # models.dev lists this alias, but it is not accepted by OpenAI APIs.
                if modelId in MODELS_DEV_OPENAI_UNSUPPORTED_MODEL_IDS:
                    continue

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-responses",
                        "provider": "openai",
                        "baseUrl": "https://api.openai.com/v1",
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions("openai", modelId, m)

        # Process Groq models
        if _truthy(_models(data, "groq")):
            for modelId, m in data["groq"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-completions",
                        "provider": "groq",
                        "baseUrl": "https://api.groq.com/openai/v1",
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions("groq", modelId, m)

        # Process Cerebras models
        if _truthy(_models(data, "cerebras")):
            for modelId, m in data["cerebras"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-completions",
                        "provider": "cerebras",
                        "baseUrl": "https://api.cerebras.ai/v1",
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions("cerebras", modelId, m)

        # Process Cloudflare Workers AI models
        if _truthy(_models(data, "cloudflare-workers-ai")):
            for modelId, m in data["cloudflare-workers-ai"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-completions",
                        "provider": "cloudflare-workers-ai",
                        "baseUrl": CLOUDFLARE_WORKERS_AI_BASE_URL,
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                        "compat": {"sendSessionAffinityHeaders": True},
                    }
                )
                recordModelsDevReasoningOptions("cloudflare-workers-ai", modelId, m)

        # Process Cloudflare AI Gateway models
        cloudflareAIGatewayModelIds: set[str] = set()
        if _truthy(_models(data, "cloudflare-ai-gateway")):
            for prefixedId, m in data["cloudflare-ai-gateway"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                slashIdx = prefixedId.find("/")
                if slashIdx == -1:
                    continue
                upstream = prefixedId[:slashIdx]
                nativeId = prefixedId[slashIdx + 1 :]

                if upstream == "openai":
                    api = "openai-responses"
                    baseUrl = CLOUDFLARE_AI_GATEWAY_OPENAI_BASE_URL
                    id = nativeId
                elif upstream == "anthropic":
                    api = "anthropic-messages"
                    baseUrl = CLOUDFLARE_AI_GATEWAY_ANTHROPIC_BASE_URL
                    # The /anthropic passthrough forwards the model ID to Anthropic unchanged.
                    # models.dev lists dotted versions (claude-opus-5.5), but Anthropic only
                    # accepts dashed IDs (claude-opus-5-5).
                    id = nativeId.replace(".", "-")
                elif upstream == "workers-ai":
                    api = "openai-completions"
                    baseUrl = CLOUDFLARE_AI_GATEWAY_COMPAT_BASE_URL
                    id = prefixedId
                else:
                    continue

                # Gateway passthroughs forward session affinity headers to upstreams that
                # use them for cache/routing affinity.
                gatewayCompat = (
                    {"sendSessionAffinityHeaders": True} if upstream in ("anthropic", "workers-ai") else None
                )

                cloudflareAIGatewayModelIds.add(id)
                models.append(
                    {
                        "id": id,
                        "name": _or(m.get("name"), id),
                        "api": api,
                        "provider": "cloudflare-ai-gateway",
                        "baseUrl": baseUrl,
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                        **({"compat": gatewayCompat} if gatewayCompat else {}),
                    }
                )
                recordModelsDevReasoningOptions("cloudflare-ai-gateway", id, m)

        # The gateway proxies Workers AI through its OpenAI-compatible /compat endpoint,
        # but models.dev may omit or intermittently drop those `workers-ai/*` entries
        # from the AI Gateway catalog. Mirror the Workers AI catalog under the documented
        # prefix so the gateway keeps its OpenAI-compatible models stable.
        if _truthy(_models(data, "cloudflare-workers-ai")):
            for modelId, m in data["cloudflare-workers-ai"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                id = f"workers-ai/{modelId}"
                if id in cloudflareAIGatewayModelIds:
                    continue
                cloudflareAIGatewayModelIds.add(id)

                models.append(
                    {
                        "id": id,
                        "name": _or(m.get("name"), id),
                        "api": "openai-completions",
                        "provider": "cloudflare-ai-gateway",
                        "baseUrl": CLOUDFLARE_AI_GATEWAY_COMPAT_BASE_URL,
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                        "compat": {"sendSessionAffinityHeaders": True},
                    }
                )
                recordModelsDevReasoningOptions("cloudflare-ai-gateway", id, m)

        # Process xAi models
        if _truthy(_models(data, "xai")):
            for modelId, m in data["xai"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-responses",
                        "provider": "xai",
                        "baseUrl": "https://api.x.ai/v1",
                        "compat": {**XAI_RESPONSES_COMPAT},
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions("xai", modelId, m)

        # Process Meta models
        if _truthy(_models(data, "meta")):
            for modelId, m in data["meta"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-responses",
                        "provider": "meta",
                        "baseUrl": "https://api.meta.ai/v1",
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions("meta", modelId, m)

        models.extend(processZaiModels(data))

        # Process Mistral models
        if _truthy(_models(data, "mistral")):
            for modelId, m in data["mistral"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                # Models with effort values use `reasoning_effort` with these levels.
                # Reasoning models without them (Magistral) use `prompt_mode`.
                thinkingLevelMap = getEffortThinkingLevelMap(_nullish(m.get("reasoning_options"), []))
                cost = m.get("cost")

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "mistral-conversations",
                        "provider": "mistral",
                        "baseUrl": "https://api.mistral.ai",
                        "reasoning": m.get("reasoning") is True,
                        **({"thinkingLevelMap": thinkingLevelMap} if thinkingLevelMap else {}),
                        "input": _inputModalities(m),
                        "cost": {
                            "input": _or(_get(cost, "input"), 0),
                            "output": _or(_get(cost, "output"), 0),
                            "cacheRead": _nullish(
                                _get(cost, "cache_read"),
                                roundCost(cost["input"] * 0.1) if _truthy(_get(cost, "input")) else 0,
                            ),
                            "cacheWrite": _or(_get(cost, "cache_write"), 0),
                        },
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )

        # Process Hugging Face models
        if _truthy(_models(data, "huggingface")):
            for modelId, m in data["huggingface"]["models"].items():
                if m.get("tool_call") is not True:
                    continue

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-completions",
                        "provider": "huggingface",
                        "baseUrl": "https://router.huggingface.co/v1",
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "compat": {
                            "supportsDeveloperRole": False,
                        },
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions("huggingface", modelId, m)

        models.extend(processFireworksModels(_get(data, "fireworks-ai")))

        # Process NVIDIA NIM models
        if _truthy(_models(data, "nvidia")):
            for modelId, m in data["nvidia"]["models"].items():
                if m.get("tool_call") is not True:
                    continue
                if not _includes(_get(m.get("modalities"), "input"), "text"):
                    continue
                if not _includes(_get(m.get("modalities"), "output"), "text"):
                    continue

                liveModelId = _nullish(
                    nvidiaNimModelIds.get(modelId), nvidiaNimModelIds.get(normalizeNvidiaModelId(modelId))
                )
                if not liveModelId:
                    continue
                if liveModelId in NVIDIA_NIM_UNSUPPORTED_MODELS:
                    continue

                models.append(
                    {
                        "id": liveModelId,
                        "name": _or(m.get("name"), liveModelId),
                        "api": "openai-completions",
                        "provider": "nvidia",
                        "baseUrl": NVIDIA_BASE_URL,
                        "headers": {**NVIDIA_HEADERS},
                        "reasoning": m.get("reasoning") is True,
                        "input": ["text", "image"] if "image" in m["modalities"]["input"] else ["text"],
                        "cost": getModelsDevCost(m.get("cost")),
                        "compat": NVIDIA_OPENAI_COMPAT,
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions("nvidia", liveModelId, m)

        # Process Together AI models
        togetherProvider = _nullish(_nullish(_get(data, "together"), _get(data, "togetherai")), _get(data, "together-ai"))
        if _truthy(_get(togetherProvider, "models")):
            for modelId, m in togetherProvider["models"].items():
                if m.get("tool_call") is not True:
                    continue
                if m.get("status") == "deprecated":
                    continue

                reasoning = m.get("reasoning") is True
                togetherLevelMap = getTogetherThinkingLevelMap(modelId, reasoning)
                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-completions",
                        "provider": "together",
                        "baseUrl": TOGETHER_BASE_URL,
                        "reasoning": reasoning,
                        **({"thinkingLevelMap": togetherLevelMap} if togetherLevelMap else {}),
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "compat": getTogetherCompat(modelId, reasoning),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions("together", modelId, m)

        models.extend(processBasetenModels(_get(data, "baseten")))

        # Process OpenCode models (Zen and Go)
        # API mapping based on provider.npm field:
        # - @ai-sdk/openai → openai-responses
        # - @ai-sdk/anthropic → anthropic-messages
        # - @ai-sdk/google → google-generative-ai
        # - null/undefined/@ai-sdk/openai-compatible → openai-completions
        opencodeVariants = [
            {"key": "opencode", "provider": "opencode", "basePath": "https://opencode.ai/inference"},
            {"key": "opencode-go", "provider": "opencode-go", "basePath": "https://opencode.ai/inference/go"},
        ]

        for variant in opencodeVariants:
            if not _truthy(_models(data, variant["key"])):
                continue

            for modelId, m in data[variant["key"]]["models"].items():
                if m.get("tool_call") is not True:
                    continue
                if m.get("status") == "deprecated":
                    continue

                npm = _get(m.get("provider"), "npm")
                compat: dict[str, Any] | None = None

                if npm == "@ai-sdk/openai":
                    api = "openai-responses"
                    baseUrl = f"{variant['basePath']}/v1"
                    compat = {"sessionAffinityFormat": "openai-nosession"}
                elif npm == "@ai-sdk/anthropic":
                    api = "anthropic-messages"
                    # Anthropic SDK appends /v1/messages to baseURL
                    baseUrl = variant["basePath"]
                elif npm == "@ai-sdk/google":
                    api = "google-generative-ai"
                    baseUrl = f"{variant['basePath']}/v1"
                elif npm == "@ai-sdk/alibaba":
                    api = "openai-completions"
                    baseUrl = f"{variant['basePath']}/v1"
                    compat = {"cacheControlFormat": "anthropic"}
                else:
                    # null, undefined, or @ai-sdk/openai-compatible
                    api = "openai-completions"
                    baseUrl = f"{variant['basePath']}/v1"

                if variant["provider"] == "opencode" and modelId == "grok-build-0.1":
                    compat = {**(compat or {}), "supportsReasoningEffort": False}

                if variant["provider"] in ("opencode", "opencode-go") and modelId == "kimi-k2.6":
                    # OpenCode Kimi K2.6 accepts Anthropic-style thinking objects
                    # and rejects string thinking values or combined reasoning_effort.
                    compat = {**(compat or {}), "thinkingFormat": "deepseek", "supportsReasoningEffort": False}

                # Fix known mismatches between models.dev npm data and actual
                # OpenCode Go endpoint behaviour. models.dev reports these models
                # as @ai-sdk/anthropic, but OpenCode Go serves them through the
                # OpenAI-compatible /v1/chat/completions path.
                if variant["provider"] == "opencode-go" and modelId in ("qwen3.5-plus", "qwen3.6-plus"):
                    api = "openai-completions"
                    baseUrl = f"{variant['basePath']}/v1"
                    # Qwen/DashScope uses enable_thinking at the top level.
                    compat = {**(compat or {}), "thinkingFormat": "qwen"}

                if api == "openai-completions":
                    compat = {**(compat or {}), "maxTokensField": "max_tokens"}
                    if (
                        f"{variant['provider']}:{modelId}"
                        in OPENCODE_OPENAI_COMPLETIONS_LONG_CACHE_RETENTION_UNSUPPORTED_MODELS
                    ):
                        compat = {**compat, "supportsLongCacheRetention": False}

                opencodeLevelMap: dict[str, Any] | None = None
                if api == "google-generative-ai":
                    opencodeLevelMap = getGoogleThinkingLevelMap(modelId, _nullish(m.get("reasoning_options"), []))
                elif variant["provider"] == "opencode-go" and modelId == "deepseek-v4.1-flash":
                    opencodeLevelMap = getEffortThinkingLevelMap(_nullish(m.get("reasoning_options"), []))
                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": api,
                        "provider": variant["provider"],
                        "baseUrl": baseUrl,
                        "reasoning": m.get("reasoning") is True,
                        **({"thinkingLevelMap": opencodeLevelMap} if opencodeLevelMap else {}),
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        **({"compat": compat} if compat is not None else {}),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                recordModelsDevReasoningOptions(variant["provider"], modelId, m)

        # Process GitHub Copilot models
        if _truthy(_models(data, "github-copilot")):
            for modelId, m in data["github-copilot"]["models"].items():
                if m.get("tool_call") is not True:
                    continue
                if m.get("status") == "deprecated":
                    continue

                # Claude 4.x and 5.x models route to Anthropic Messages API
                isCopilotClaude = _test(r"^claude-(haiku|sonnet|opus|fable)-[45]([.\-]|$)", modelId)
                # GPT, Grok, OSWE, and MAI-Code models are only served through
                # the Copilot /responses endpoint.
                needsResponsesApi = modelId.startswith(("gpt-", "grok-", "oswe", "mai-"))

                api = (
                    "anthropic-messages"
                    if isCopilotClaude
                    else "openai-responses"
                    if needsResponsesApi
                    else "openai-completions"
                )

                anthropicCompat = (
                    getAnthropicMessagesCompat("github-copilot", modelId) if api == "anthropic-messages" else None
                )

                copilotModel: Model = {
                    "id": modelId,
                    "name": _or(m.get("name"), modelId),
                    "api": api,
                    "provider": "github-copilot",
                    "baseUrl": "https://api.individual.githubcopilot.com",
                    "reasoning": m.get("reasoning") is True,
                    "input": _inputModalities(m),
                    "cost": getModelsDevCost(m.get("cost")),
                    "contextWindow": _limit(m, "context", 128000),
                    "maxTokens": _limit(m, "output", 8192),
                    "headers": {**COPILOT_STATIC_HEADERS},
                    **({"compat": anthropicCompat} if anthropicCompat else {}),
                    # compat only applies to openai-completions
                    **(
                        {
                            "compat": {
                                "supportsStore": False,
                                "supportsDeveloperRole": False,
                                "supportsReasoningEffort": False,
                            },
                        }
                        if api == "openai-completions"
                        else {}
                    ),
                }

                models.append(copilotModel)
                recordModelsDevReasoningOptions("github-copilot", modelId, m)

        # Process MiniMax models
        minimaxVariants = [
            {"key": "minimax", "provider": "minimax", "baseUrl": "https://api.minimax.io/anthropic"},
            {"key": "minimax-cn", "provider": "minimax-cn", "baseUrl": "https://api.minimaxi.com/anthropic"},
        ]

        for variant in minimaxVariants:
            key, provider, baseUrl = variant["key"], variant["provider"], variant["baseUrl"]
            if _truthy(_models(data, key)):
                for modelId, m in data[key]["models"].items():
                    if m.get("tool_call") is not True:
                        continue

                    models.append(
                        {
                            "id": modelId,
                            "name": _or(m.get("name"), modelId),
                            "api": "anthropic-messages",
                            "provider": provider,
                            # MiniMax's Anthropic-compatible API - SDK appends /v1/messages
                            "baseUrl": baseUrl,
                            "reasoning": m.get("reasoning") is True,
                            "input": _inputModalities(m),
                            "cost": getModelsDevCost(m.get("cost")),
                            "contextWindow": _limit(m, "context", 4096),
                            "maxTokens": _limit(m, "output", 4096),
                        }
                    )
                    recordModelsDevReasoningOptions(provider, modelId, m)

        # Process Kimi For Coding models
        if _truthy(_models(data, "kimi-code-plan-global")):
            models.extend(createKimiCodingModels(data["kimi-code-plan-global"]["models"]))

        # Process Moonshot AI models
        moonshotVariants = [
            {"key": "moonshotai", "provider": "moonshotai", "baseUrl": "https://api.moonshot.ai/v1"},
            {"key": "moonshotai-cn", "provider": "moonshotai-cn", "baseUrl": "https://api.moonshot.cn/v1"},
        ]
        def getMoonshotProviderModels(key: str) -> dict[str, Any]:
            providerModels: Any = _models(data, key)
            return {**providerModels} if _truthy(providerModels) else {}

        moonshotModels = {
            "moonshotai": getMoonshotProviderModels("moonshotai"),
            "moonshotai-cn": getMoonshotProviderModels("moonshotai-cn"),
        }

        for variant in moonshotVariants:
            key, provider, baseUrl = variant["key"], variant["provider"], variant["baseUrl"]
            for modelId, m in moonshotModels[key].items():
                if m.get("tool_call") is not True:
                    continue
                isKimiK3 = modelId == "kimi-k3"
                compat = {**(KIMI_K3_OPENAI_COMPAT if isKimiK3 else KIMI_OPENAI_COMPAT)}
                cost = m.get("cost")
                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-completions",
                        "provider": provider,
                        "baseUrl": baseUrl,
                        "reasoning": isKimiK3 or m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        # Moonshot does not bill cache writes; models.dev lists the input rate as cache_write for Kimi K3.
                        "cost": (
                            {**KIMI_K3_COST}
                            if isKimiK3
                            else {
                                "input": _or(_get(cost, "input"), 0),
                                "output": _or(_get(cost, "output"), 0),
                                "cacheRead": _or(_get(cost, "cache_read"), 0),
                                "cacheWrite": _or(_get(cost, "cache_write"), 0),
                            }
                        ),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                        "compat": compat,
                    }
                )
                recordModelsDevReasoningOptions(provider, modelId, m)

        # Process Xiaomi MiMo models
        # Built-in `xiaomi` targets the API billing endpoint (single stable URL,
        # keys from platform.xiaomimimo.com). The three `xiaomi-token-plan-*`
        # providers cover prepaid Token Plan endpoints in cn / ams / sgp.
        # MiMo Chat Completions only documents thinking.type enabled/disabled and
        # rejects some reasoning_effort values (e.g. "minimal"). Xiaomi documents
        # all effort levels as identical, so expose only off and high.
        xiaomiCompat = {
            "supportsReasoningEffort": False,
            "requiresReasoningContentOnAssistantMessages": True,
            "thinkingFormat": "deepseek",
        }
        xiaomiThinkingLevelMap = {
            "off": "off",
            "minimal": None,
            "low": None,
            "medium": None,
            "high": "high",
            "xhigh": None,
            "max": None,
        }
        xiaomiVariants = [
            {"source": "xiaomi", "provider": "xiaomi", "baseUrl": "https://api.xiaomimimo.com/v1"},
            {
                "source": "xiaomi-token-plan-cn",
                "provider": "xiaomi-token-plan-cn",
                "baseUrl": "https://token-plan-cn.xiaomimimo.com/v1",
            },
            {
                "source": "xiaomi-token-plan-ams",
                "provider": "xiaomi-token-plan-ams",
                "baseUrl": "https://token-plan-ams.xiaomimimo.com/v1",
            },
            {
                "source": "xiaomi-token-plan-sgp",
                "provider": "xiaomi-token-plan-sgp",
                "baseUrl": "https://token-plan-sgp.xiaomimimo.com/v1",
            },
        ]

        for variant in xiaomiVariants:
            source, provider, baseUrl = variant["source"], variant["provider"], variant["baseUrl"]
            providerModels = _models(data, source)
            if not _truthy(providerModels):
                continue

            for modelId, m in providerModels.items():  # type: ignore[union-attr]
                if m.get("tool_call") is not True:
                    continue
                if m.get("status") == "deprecated":
                    continue

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-completions",
                        "provider": provider,
                        "baseUrl": baseUrl,
                        "compat": xiaomiCompat,
                        "reasoning": m.get("reasoning") is True,
                        **({"thinkingLevelMap": xiaomiThinkingLevelMap} if m.get("reasoning") is True else {}),
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )

        # Process Alibaba Cloud Model Studio Token Plan models. International and
        # China use separate endpoints and API keys (sk-sp- prefix). The Individual
        # provider reuses the international source and endpoint with a narrower catalog.
        # models.dev keys are "alibaba-token-plan[-cn]"; pi exposes them as
        # "qwen-token-plan[-cn]" plus the Individual catalog view.
        qwenTokenPlanCompat = {
            "thinkingFormat": "qwen",
            "supportsDeveloperRole": False,
            "supportsStore": False,
            "supportsReasoningEffort": True,
        }
        qwenTokenPlanVariants: list[dict[str, Any]] = [
            {
                "source": "alibaba-token-plan",
                "provider": "qwen-token-plan",
                "baseUrl": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
                "modelIds": None,
            },
            {
                "source": "alibaba-token-plan",
                "provider": "qwen-token-plan-individual",
                "baseUrl": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
                "modelIds": QWEN_TOKEN_PLAN_INDIVIDUAL_MODEL_IDS,
            },
            {
                "source": "alibaba-token-plan-cn",
                "provider": "qwen-token-plan-cn",
                "baseUrl": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                "modelIds": None,
            },
        ]

        for variant in qwenTokenPlanVariants:
            source, provider, baseUrl, modelIds = (
                variant["source"],
                variant["provider"],
                variant["baseUrl"],
                variant["modelIds"],
            )
            providerModels = _models(data, source)
            emittedModelIds: set[str] | None = set() if modelIds is not None else None

            for modelId, m in _nullish(providerModels, {}).items():
                if m.get("tool_call") is not True:
                    continue
                if modelId in QWEN_TOKEN_PLAN_EXCLUDED_MODEL_IDS:
                    continue
                if modelIds is not None and modelId not in modelIds:
                    continue
                qwenLevelMap = _nullish(
                    getEffortThinkingLevelMap(_nullish(m.get("reasoning_options"), [])),
                    (
                        QWEN_TOKEN_PLAN_FALLBACK_THINKING_LEVEL_MAP
                        if modelId in QWEN_TOKEN_PLAN_REASONING_EFFORT_FALLBACK_MODEL_IDS
                        else None
                    ),
                )

                models.append(
                    {
                        "id": modelId,
                        "name": _or(m.get("name"), modelId),
                        "api": "openai-completions",
                        "provider": provider,
                        "baseUrl": baseUrl,
                        "compat": (
                            qwenTokenPlanCompat
                            if qwenLevelMap
                            else {**qwenTokenPlanCompat, "supportsReasoningEffort": False}
                        ),
                        **({"thinkingLevelMap": qwenLevelMap} if qwenLevelMap else {}),
                        "reasoning": m.get("reasoning") is True,
                        "input": _inputModalities(m),
                        "cost": getModelsDevCost(m.get("cost")),
                        "contextWindow": _limit(m, "context", 4096),
                        "maxTokens": _limit(m, "output", 4096),
                    }
                )
                if emittedModelIds is not None:
                    emittedModelIds.add(modelId)

            if modelIds is not None and emittedModelIds is not None and generatorOptions["strict"]:
                assertExactModelIds(provider, modelIds, emittedModelIds)

        _log(f"Loaded {len(models)} tool-capable models from models.dev")
        return models
    except Exception as error:
        _logError("Failed to load models.dev data:", error)
        if generatorOptions["strict"]:
            raise
        return []


async def loadModelsDevClassifierModels() -> list[Model]:
    try:
        _log("Fetching classifier models from models.dev API...")
        response = await whatwg_fetch.fetch("https://models.dev/models.json?type=decision")
        if not response.ok:
            raise RuntimeError(f"models.dev classifier API returned {response.status}")
        data = await _json(response)
        metadata = data.get("typesafe/jev-latest")
        if not _truthy(metadata) or _get(metadata, "type") != "decision":
            raise RuntimeError("models.dev did not return decision model typesafe/jev-latest")
        return [
            {
                "type": "classifier",
                "id": "jev-latest",
                "name": metadata.get("name", UNDEFINED),
                "api": "typesafe-system-one",
                "provider": "typesafe",
                "baseUrl": "https://api.typesafe.ai/v1/",
                "input": _inputModalities(metadata),
                # The canonical models.dev entry has no direct-provider pricing. System One reports token usage,
                # so classify() results carry token counts but price them at zero.
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": _limit(metadata, "context", 64000),
            },
        ]
    except Exception as error:
        _logError("Failed to load models.dev classifier data:", error)
        if generatorOptions["strict"]:
            raise
        return []


# Workers AI has no unauthenticated catalog and models.dev does not list its
# System One models yet. Cloudflare publishes pricing only in the dashboard.
# https://developers.cloudflare.com/ai/models/typesafe/jev/
# OpenCode Zen serves Jev through its TypeSafe-compatible System One endpoint.
# Neither its /inference/v1/models listing nor models.dev carries metadata for it.
# https://opencode.ai/docs/zen
OPENCODE_CLASSIFIER_MODELS: list[Model] = [
    {
        "type": "classifier",
        "id": "jev-1.13",
        "name": "Jev 1.13",
        "api": "typesafe-system-one",
        "provider": "opencode",
        "baseUrl": "https://opencode.ai/inference/v1",
        "input": ["text"],
        "cost": {"input": 0.042, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 32000,
    },
    {
        "type": "classifier",
        "id": "jev-1.13-free",
        "name": "Jev 1.13 Free",
        "api": "typesafe-system-one",
        "provider": "opencode",
        "baseUrl": "https://opencode.ai/inference/v1",
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 32000,
    },
]

CLOUDFLARE_WORKERS_AI_CLASSIFIER_MODELS: list[Model] = [
    # Cloudflare-hosted Clef decision models. They accept images, but classifier
    # contexts carry text or JSON state only, so the catalog advertises text.
    # Pricing: https://developers.cloudflare.com/workers-ai/models/clef/
    # and https://developers.cloudflare.com/workers-ai/models/clef-flash/
    {
        "type": "classifier",
        "id": "@cf/cloudflare/clef",
        "name": "Clef",
        "api": "cloudflare-workers-ai-system-one",
        "provider": "cloudflare-workers-ai",
        "baseUrl": CLOUDFLARE_WORKERS_AI_REST_BASE_URL,
        "input": ["text"],
        "cost": {"input": 0.24, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 65536,
    },
    {
        "type": "classifier",
        "id": "@cf/cloudflare/clef-flash",
        "name": "Clef Flash",
        "api": "cloudflare-workers-ai-system-one",
        "provider": "cloudflare-workers-ai",
        "baseUrl": CLOUDFLARE_WORKERS_AI_REST_BASE_URL,
        "input": ["text"],
        "cost": {"input": 0.09, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 65536,
    },
    {
        "type": "classifier",
        "id": "typesafe/jev",
        "name": "Jev",
        "api": "cloudflare-workers-ai-system-one",
        "provider": "cloudflare-workers-ai",
        "baseUrl": CLOUDFLARE_WORKERS_AI_REST_BASE_URL,
        "input": ["text"],
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        "contextWindow": 32000,
    },
]

# OpenAI Decisions API (public beta): gpt-6-luna is its only model. It bills input tokens only,
# with the same long-context multiplier as chat requests. Only API keys work: Sign in with ChatGPT
# tokens are rejected on /v1/decisions, and the Codex backend has no Decisions route.
# The endpoint rejects inputs above 922K tokens (the model's documented maximum input), but
# requests running longer than about five seconds, currently above roughly 600K input tokens,
# fail with a gateway timeout.
# https://developers.openai.com/api/docs/guides/decisions
OPENAI_CLASSIFIER_MODELS: list[Model] = [
    {
        "type": "classifier",
        "id": "gpt-6-luna",
        "name": "GPT-6 Luna",
        "api": "openai-decisions",
        "provider": "openai",
        "baseUrl": "https://api.openai.com/v1",
        "input": ["text", "image"],
        "cost": withOpenAiLongContextPricing({"input": 0.1, "output": 0, "cacheRead": 0, "cacheWrite": 0}),
        "contextWindow": 922000,
    },
]


# MISAKA fork: the generated catalog modules are Python. pi writes `providers/<id>.models.ts`, which imports
# `./data/<id>.json` and flattens it with `../model-catalog.ts`, and `models.generated.ts`, which gathers every provider's
# three catalogs into `MODELS`, `IMAGE_MODELS` and `CLASSIFIER_MODELS`. `scripts/refresh_model_catalog.py` writes the same
# modules around pi's published data.
generatedHeader = """# This file is auto-generated by scripts/generate_models.py
# Do not edit manually - run 'python scripts/refresh_model_catalog.py' to update
"""


def catalogConstName(providerId: str) -> str:
    return f"{re.sub(r'[^A-Z0-9]+', '_', providerId.upper())}_MODELS"


def imageCatalogConstName(providerId: str) -> str:
    return f"{re.sub(r'[^A-Z0-9]+', '_', providerId.upper())}_IMAGE_MODELS"


def classifierCatalogConstName(providerId: str) -> str:
    return f"{re.sub(r'[^A-Z0-9]+', '_', providerId.upper())}_CLASSIFIER_MODELS"


def importLine(source: str, names: list[str]) -> list[str]:
    """``from source import names`` as ruff's isort writes it: one line when it fits in 88 columns."""
    line = f"from {source} import {', '.join(names)}"
    if len(line) <= 88:
        return [line]
    return [f"from {source} import (", *(f"    {name}," for name in names), ")"]


def renderShard(providerId: str) -> str:
    """pi's ``providers/<id>.models.ts``."""
    lines = [
        generatedHeader,
        "import json",
        "from pathlib import Path",
        "",
        *importLine(
            "misaka.ai.model_catalog",
            ["flattenChatModelCatalog", "flattenClassifierModelCatalog", "flattenImageModelCatalog"],
        ),
        "",
        f'# MISAKA fork: `import values from "./data/{providerId}.json"` reads the JSON.',
        f'_values = json.loads((Path(__file__).parent / "data" / "{providerId}.json").read_text(encoding="utf-8"))',
        "",
        f'{catalogConstName(providerId)} = flattenChatModelCatalog("{providerId}", _values)',
        "",
        f'{imageCatalogConstName(providerId)} = flattenImageModelCatalog("{providerId}", _values)',
        "",
        f'{classifierCatalogConstName(providerId)} = flattenClassifierModelCatalog("{providerId}", _values)',
    ]
    return "\n".join(lines) + "\n"


def renderAggregator(providerIds: list[str]) -> str:
    """pi's ``models.generated.ts``."""
    lines = [generatedHeader]
    for providerId in sorted(providerIds, key=providerModule):  # isort orders the imports by module name
        names = sorted(
            [
                catalogConstName(providerId),
                imageCatalogConstName(providerId),
                classifierCatalogConstName(providerId),
            ]
        )
        lines += importLine(f"misaka.ai.providers.{providerModule(providerId)}", names)
    for table, constName in (
        ("MODELS", catalogConstName),
        ("IMAGE_MODELS", imageCatalogConstName),
        ("CLASSIFIER_MODELS", classifierCatalogConstName),
    ):
        lines += ["", f"{table} = {{", *(f'    "{providerId}": {constName(providerId)},' for providerId in providerIds), "}"]
    return "\n".join(lines) + "\n"


def _readFileSync(path: Path) -> str:
    """``readFileSync(path, "utf8")``."""
    with open(path, encoding="utf-8", errors="replace", newline="") as file:
        return file.read()


def _writeFileSync(path: Path, content: str) -> None:
    """``writeFileSync(path, content)``: UTF-8, line endings as given."""
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.write(content)


def _rmSync(path: Path) -> None:
    """``rmSync(path, { recursive: true, force: true })``."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            os.remove(path)
    except FileNotFoundError:
        pass


def _fromEntries(entries: list[tuple[str, Any]]) -> dict[str, Any]:
    """``Object.fromEntries(entries)``: array-index keys come first, as in every JavaScript object."""
    record = dict(entries)
    return {key: record[key] for key in jsObjectOrder(dict.fromkeys(record))}


def _localeCompareSorted(record: dict[str, Any]) -> dict[str, Any]:
    """``Object.fromEntries(Object.entries(record).sort(([left], [right]) => left.localeCompare(right)))``."""
    return _fromEntries(sorted(record.items(), key=lambda entry: localeSortKey(entry[0])))


async def generateModels() -> None:
    # Fetch models from all upstream catalogs.
    # models.dev: Anthropic, Google, OpenAI, Groq, Cerebras, and others
    # OpenRouter: its tool-capable routed catalog
    # AI Gateway: OpenAI-compatible catalog with tool-capable models
    # Radius: its unauthenticated public catalog; authenticated clients overlay it at runtime
    modelsDevModels = await loadModelsDevData()
    modelsDevClassifierModels = await loadModelsDevClassifierModels()
    openRouterCatalog = await fetchOpenRouterModels()
    aiGatewayCatalog = await fetchAiGatewayModels()
    radiusModels = await fetchRadiusModels()

    # Combine chat models (models.dev has priority where sources overlap).
    allModels: list[Model] = [
        model
        for model in [*modelsDevModels, *openRouterCatalog["chat"], *aiGatewayCatalog["chat"], *radiusModels]
        if not (model["provider"] == "xai" and model["id"] in XAI_BUILTIN_EXCLUDED_MODEL_IDS)
        and not (model["provider"] in ("opencode", "opencode-go") and model["id"] == "gpt-5.3-codex-spark")
    ]

    # The authenticated Copilot catalog advertised these models on 2026-09-22,
    # but models.dev did not include them yet.
    missingCopilotModels: list[Model] = [
        {
            "id": "claude-opus-5.5",
            "name": "Claude Opus 5.5",
            "api": "anthropic-messages",
            "provider": "github-copilot",
            "baseUrl": "https://api.individual.githubcopilot.com",
            "reasoning": True,
            "thinkingLevelMap": {
                "off": None,
                "minimal": None,
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "xhigh",
                "max": "max",
            },
            "input": ["text", "image"],
            "cost": {"input": 4, "output": 20, "cacheRead": 0.2, "cacheWrite": 5},
            "contextWindow": 1000000,
            "maxTokens": 128000,
            "headers": {**COPILOT_STATIC_HEADERS},
        },
        *(
            {
                "id": id,
                "name": "GPT-6 Sol" if id == "gpt-6-sol" else "GPT-6 Luna",
                "api": "openai-responses",
                "provider": "github-copilot",
                "baseUrl": "https://api.individual.githubcopilot.com",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS[id]),
                "contextWindow": 1000000,
                "maxTokens": 128000,
                "headers": {**COPILOT_STATIC_HEADERS},
            }
            for id in ("gpt-6-sol", "gpt-6-luna")
        ),
    ]
    for model in missingCopilotModels:
        if not any(
            candidate["provider"] == model["provider"] and candidate["id"] == model["id"] for candidate in allModels
        ):
            allModels.append(model)

    # Temporary overrides until upstream model metadata is corrected.
    for candidate in allModels:
        if candidate["provider"] == "github-copilot" and candidate["id"] in GITHUB_COPILOT_EXTENDED_CONTEXT_MODELS:
            candidate["contextWindow"] = 1000000

        # models.dev may list Opus 5.5, Sonnet 5.5, and Haiku 5.5 before their effort metadata is complete.
        if (
            candidate["provider"] == "anthropic"
            and candidate["id"] in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
        ) or (candidate["provider"] == "github-copilot" and candidate["id"] == "claude-opus-5.5"):
            mergeThinkingLevelMap(
                candidate,
                {
                    # Haiku 5.5 accepts disabled thinking; Opus 5.5 and Sonnet 5.5 reject it.
                    **({} if candidate["id"] == "claude-haiku-5-5" else {"off": None}),
                    "minimal": None,
                    "low": "low",
                    "medium": "medium",
                    "high": "high",
                    "xhigh": "xhigh",
                    "max": "max",
                },
            )

        if candidate["provider"] in ("anthropic", "opencode", "opencode-go") and candidate["id"] in (
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-opus-4.6",
            "claude-sonnet-4.6",
        ):
            candidate["contextWindow"] = 1000000

        # OpenCode variants list Claude Sonnet 4/4.5 with 1M context, actual limit is 200K
        if candidate["provider"] in ("opencode", "opencode-go") and candidate["id"] in (
            "claude-sonnet-4-5",
            "claude-sonnet-4",
        ):
            candidate["contextWindow"] = 200000
        if candidate["provider"] in ("opencode", "opencode-go") and candidate["id"] == "gpt-5.4":
            candidate["contextWindow"] = 272000
            candidate["maxTokens"] = 128000
        # Keep direct OpenAI requests in the short-context pricing tier by default. Users can opt into the
        # larger context through model overrides, so retain long-context cost metadata on the capped models.
        if candidate["provider"] == "openai" and candidate["id"] in OPENAI_SHORT_CONTEXT_CAPPED_MODEL_IDS:
            candidate["contextWindow"] = OPENAI_LONG_CONTEXT_INPUT_THRESHOLD
            candidate["maxTokens"] = 128000
        if candidate["provider"] == "openai" and candidate["id"] in OPENAI_LONG_CONTEXT_PRICING_MODEL_IDS:
            standardCost = OPENAI_STANDARD_COSTS.get(candidate["id"])
            candidate["cost"] = withOpenAiLongContextPricing(_nullish(standardCost, candidate["cost"]))
        # Cloudflare AI Gateway passes OpenAI usage through at OpenAI list prices.
        if candidate["provider"] == "cloudflare-ai-gateway":
            standardCost = OPENAI_STANDARD_COSTS.get(candidate["id"])
            if standardCost:
                candidate["cost"] = withOpenAiLongContextPricing(standardCost)
        # models.dev reports gpt-5-pro output as 272000 (a duplicate of the input sub-limit);
        # the actual max output is 128000. Also propagates to the derived Azure clone.
        if candidate["provider"] == "openai" and candidate["id"] == "gpt-5-pro":
            candidate["maxTokens"] = 128000
        # Keep Kimi K3's canonical output limit when gateway metadata is missing or incorrect.
        if (candidate["provider"] == "openrouter" and candidate["id"] in OPENROUTER_KIMI_K3_MODEL_IDS) or (
            candidate["provider"] == "vercel-ai-gateway" and candidate["id"] == "moonshotai/kimi-k3"
        ):
            candidate["maxTokens"] = KIMI_K3_MAX_TOKENS
        # Keep selected OpenRouter model metadata stable until upstream settles.
        if candidate["provider"] == "openrouter" and candidate["id"] == "moonshotai/kimi-k2.5":
            candidate["cost"]["input"] = 0.41
            candidate["cost"]["output"] = 2.06
            candidate["cost"]["cacheRead"] = 0.07
            candidate["maxTokens"] = 4096
        if candidate["provider"] == "openrouter" and candidate["id"].startswith("moonshotai/kimi-k2.6"):
            candidate["compat"] = {
                **(candidate.get("compat") or {}),
                "supportsDeveloperRole": False,
                "requiresReasoningContentOnAssistantMessages": True,
            }
        if candidate["provider"] == "openrouter" and candidate["id"] == "z-ai/glm-5":
            candidate["cost"]["input"] = 0.6
            candidate["cost"]["output"] = 1.9
            candidate["cost"]["cacheRead"] = 0.119

    # Add missing gpt models
    missingOpenAiModels: list[Model] = [
        {
            "id": "gpt-6.1-sol",
            "name": "GPT-6.1 Sol",
            "api": "openai-responses",
            "baseUrl": "https://api.openai.com/v1",
            "provider": "openai",
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-6.1-sol"]),
            "contextWindow": OPENAI_LONG_CONTEXT_INPUT_THRESHOLD,
            "maxTokens": 128000,
        },
        {
            "id": "gpt-6-astra",
            "name": "GPT-6 Astra",
            "api": "openai-responses",
            "baseUrl": "https://api.openai.com/v1",
            "provider": "openai",
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-6-astra"]),
            "contextWindow": OPENAI_LONG_CONTEXT_INPUT_THRESHOLD,
            "maxTokens": 128000,
        },
        {
            "id": "gpt-6-sol",
            "name": "GPT-6 Sol",
            "api": "openai-responses",
            "baseUrl": "https://api.openai.com/v1",
            "provider": "openai",
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-6-sol"]),
            "contextWindow": OPENAI_LONG_CONTEXT_INPUT_THRESHOLD,
            "maxTokens": 128000,
        },
        {
            "id": "gpt-6-luna",
            "name": "GPT-6 Luna",
            "api": "openai-responses",
            "baseUrl": "https://api.openai.com/v1",
            "provider": "openai",
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-6-luna"]),
            "contextWindow": OPENAI_LONG_CONTEXT_INPUT_THRESHOLD,
            "maxTokens": 128000,
        },
        {
            "id": "gpt-5.6-sol",
            "name": "GPT-5.6 Sol",
            "api": "openai-responses",
            "baseUrl": "https://api.openai.com/v1",
            "provider": "openai",
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-5.6-sol"]),
            "contextWindow": OPENAI_LONG_CONTEXT_INPUT_THRESHOLD,
            "maxTokens": 128000,
        },
        {
            "id": "gpt-5.6-terra",
            "name": "GPT-5.6 Terra",
            "api": "openai-responses",
            "baseUrl": "https://api.openai.com/v1",
            "provider": "openai",
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-5.6-terra"]),
            "contextWindow": OPENAI_LONG_CONTEXT_INPUT_THRESHOLD,
            "maxTokens": 128000,
        },
        {
            "id": "gpt-5.6-luna",
            "name": "GPT-5.6 Luna",
            "api": "openai-responses",
            "baseUrl": "https://api.openai.com/v1",
            "provider": "openai",
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-5.6-luna"]),
            "contextWindow": OPENAI_LONG_CONTEXT_INPUT_THRESHOLD,
            "maxTokens": 128000,
        },
        {
            "id": "gpt-5-chat-latest",
            "name": "GPT-5 Chat Latest",
            "api": "openai-responses",
            "baseUrl": "https://api.openai.com/v1",
            "provider": "openai",
            "reasoning": False,
            "input": ["text", "image"],
            "cost": {
                "input": 1.25,
                "output": 10,
                "cacheRead": 0.125,
                "cacheWrite": 0,
            },
            "contextWindow": 128000,
            "maxTokens": 16384,
        },
    ]
    for model in missingOpenAiModels:
        if not any(m["provider"] == model["provider"] and m["id"] == model["id"] for m in allModels):
            allModels.append(model)

    deepseekCompat = {
        "requiresReasoningContentOnAssistantMessages": True,
        "thinkingFormat": "deepseek",
    }
    deepseekModels: list[Model] = [
        {
            "id": "deepseek-flash",
            "name": "DeepSeek V4.1 Flash",
            "api": "openai-completions",
            "baseUrl": "https://api.deepseek.com",
            "provider": "deepseek",
            "reasoning": True,
            "thinkingLevelMap": DEEPSEEK_V4_FLASH_THINKING_LEVEL_MAP,
            "input": ["text", "image"],
            "cost": {
                # DeepSeek also offers time-based off-peak rates, which the cost schema cannot represent yet.
                "input": 0.3,
                "output": 1.2,
                "cacheRead": 0.006,
                "cacheWrite": 0,
            },
            "contextWindow": 1000000,
            "maxTokens": 384000,
            "compat": deepseekCompat,
        },
        {
            "id": "deepseek-v4-pro",
            "name": "DeepSeek V4 Pro",
            "api": "openai-completions",
            "baseUrl": "https://api.deepseek.com",
            "provider": "deepseek",
            "reasoning": True,
            "input": ["text"],
            "cost": {
                # DeepSeek also offers time-based off-peak rates, which the cost schema cannot represent yet.
                "input": 1.32,
                "output": 3.96,
                "cacheRead": 0.044,
                "cacheWrite": 0,
            },
            "contextWindow": 1000000,
            "maxTokens": 384000,
            "compat": deepseekCompat,
        },
    ]
    allModels.extend(deepseekModels)

    antLingCompat = {
        "supportsStore": False,
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": False,
        "maxTokensField": "max_tokens",
        "supportsLongCacheRetention": False,
    }
    antLingModels: list[Model] = [
        {
            "id": "Ling-2.6-flash",
            "name": "Ling 2.6 Flash",
            "api": "openai-completions",
            "baseUrl": "https://api.ant-ling.com/v1",
            "provider": "ant-ling",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0.01, "output": 0.02, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 262144,
            "maxTokens": 65536,
            "compat": antLingCompat,
        },
        {
            "id": "Ling-2.6-1T",
            "name": "Ling 2.6 1T",
            "api": "openai-completions",
            "baseUrl": "https://api.ant-ling.com/v1",
            "provider": "ant-ling",
            "reasoning": False,
            "input": ["text"],
            "cost": {"input": 0.06, "output": 0.25, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 262144,
            "maxTokens": 65536,
            "compat": antLingCompat,
        },
        {
            "id": "Ring-2.6-1T",
            "name": "Ring 2.6 1T",
            "api": "openai-completions",
            "baseUrl": "https://api.ant-ling.com/v1",
            "provider": "ant-ling",
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 0.06, "output": 0.25, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": 262144,
            "maxTokens": 65536,
            "compat": {**antLingCompat, "thinkingFormat": "ant-ling"},
        },
    ]
    allModels.extend(antLingModels)

    for candidate in allModels:
        if (
            candidate["api"] == "openai-completions"
            and "deepseek-v4" in candidate["id"]
            and candidate["provider"] not in QWEN_TOKEN_PLAN_PROVIDER_IDS
        ):
            preservesNativeReasoningEffort = candidate["provider"] in ("openrouter", "opencode")
            candidate["compat"] = {
                **(candidate.get("compat") or {}),
                **(
                    {
                        "requiresReasoningContentOnAssistantMessages": deepseekCompat[
                            "requiresReasoningContentOnAssistantMessages"
                        ],
                    }
                    if preservesNativeReasoningEffort
                    else deepseekCompat
                ),
            }

    minimaxDirectSupportedIds = {"MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M3"}

    for i in range(len(allModels) - 1, -1, -1):
        candidate = allModels[i]
        if candidate["provider"] in ("minimax", "minimax-cn") and candidate["id"] not in minimaxDirectSupportedIds:
            del allModels[i]

    # OpenAI Codex (ChatGPT OAuth) models
    # NOTE: These are not fetched from models.dev; we keep a small, explicit list to avoid aliases.
    # Older model limits are based on observed server behavior; GPT-5.6 and GPT-6 use Codex's 272k default catalog limit.
    # GPT-6.1 Sol uses the 500k default returned by /backend-api/codex/models.
    CODEX_BASE_URL = "https://chatgpt.com/backend-api"
    CODEX_CONTEXT = 272000
    CODEX_GPT_61_SOL_CONTEXT = 500000
    CODEX_GPT_56_CONTEXT = 272000
    CODEX_SPARK_CONTEXT = 128000
    CODEX_MAX_TOKENS = 128000
    codexModels: list[Model] = [
        {
            "id": "gpt-6.1-sol",
            "name": "GPT-6.1 Sol",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": CODEX_BASE_URL,
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-6.1-sol"]),
            "contextWindow": CODEX_GPT_61_SOL_CONTEXT,
            "maxTokens": CODEX_MAX_TOKENS,
        },
        {
            "id": "gpt-6-astra",
            "name": "GPT-6 Astra",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": CODEX_BASE_URL,
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-6-astra"]),
            "contextWindow": CODEX_CONTEXT,
            "maxTokens": CODEX_MAX_TOKENS,
        },
        {
            "id": "gpt-6-sol",
            "name": "GPT-6 Sol",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": CODEX_BASE_URL,
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-6-sol"]),
            "contextWindow": CODEX_CONTEXT,
            "maxTokens": CODEX_MAX_TOKENS,
        },
        {
            "id": "gpt-6-luna",
            "name": "GPT-6 Luna",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": CODEX_BASE_URL,
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-6-luna"]),
            "contextWindow": CODEX_CONTEXT,
            "maxTokens": CODEX_MAX_TOKENS,
        },
        {
            "id": "gpt-5.3-codex-spark",
            "name": "GPT-5.3 Codex Spark",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": CODEX_BASE_URL,
            "reasoning": True,
            "input": ["text"],
            "cost": {"input": 1.75, "output": 14, "cacheRead": 0.175, "cacheWrite": 0},
            "contextWindow": CODEX_SPARK_CONTEXT,
            "maxTokens": CODEX_MAX_TOKENS,
        },
        {
            "id": "gpt-5.5",
            "name": "GPT-5.5",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": CODEX_BASE_URL,
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing({"input": 5, "output": 30, "cacheRead": 0.5, "cacheWrite": 0}),
            "contextWindow": CODEX_CONTEXT,
            "maxTokens": CODEX_MAX_TOKENS,
        },
        {
            "id": "gpt-5.6-luna",
            "name": "GPT-5.6 Luna",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": CODEX_BASE_URL,
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-5.6-luna"]),
            "contextWindow": CODEX_GPT_56_CONTEXT,
            "maxTokens": CODEX_MAX_TOKENS,
        },
        {
            "id": "gpt-5.6-sol",
            "name": "GPT-5.6 Sol",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": CODEX_BASE_URL,
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-5.6-sol"]),
            "contextWindow": CODEX_GPT_56_CONTEXT,
            "maxTokens": CODEX_MAX_TOKENS,
        },
        {
            "id": "gpt-5.6-terra",
            "name": "GPT-5.6 Terra",
            "api": "openai-codex-responses",
            "provider": "openai-codex",
            "baseUrl": CODEX_BASE_URL,
            "reasoning": True,
            "input": ["text", "image"],
            "cost": withOpenAiLongContextPricing(OPENAI_STANDARD_COSTS["gpt-5.6-terra"]),
            "contextWindow": CODEX_GPT_56_CONTEXT,
            "maxTokens": CODEX_MAX_TOKENS,
        },
    ]
    allModels.extend(codexModels)

    # Add missing Mistral Medium 3.5 model until models.dev includes it
    if not any(m["provider"] == "mistral" and m["id"] == "mistral-medium-3.5" for m in allModels):
        allModels.append(
            {
                "id": "mistral-medium-3.5",
                "name": "Mistral Medium 3.5",
                "api": "mistral-conversations",
                "provider": "mistral",
                "baseUrl": "https://api.mistral.ai",
                "reasoning": True,
                "thinkingLevelMap": getEffortThinkingLevelMap([{"type": "effort", "values": ["none", "high"]}]),
                "input": ["text", "image"],
                "cost": {
                    "input": 1.5,
                    "output": 7.5,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
                "contextWindow": 262144,  # 256k tokens
                "maxTokens": 262144,
            }
        )

    # Add "auto" alias for openrouter/auto
    if not any(m["provider"] == "openrouter" and m["id"] == "auto" for m in allModels):
        allModels.append(
            {
                "id": "auto",
                "name": "Auto",
                "api": "openai-completions",
                "provider": "openrouter",
                "baseUrl": "https://openrouter.ai/api/v1",
                "reasoning": True,
                "input": ["text", "image"],
                "cost": {
                    # we dont know about the costs because OpenRouter auto routes to different models
                    # and then charges you for the underlying used model
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
                "contextWindow": 2000000,
                "maxTokens": 30000,
            }
        )

    # Add "fusion" alias for openrouter/fusion. OpenRouter exposes Fusion as a
    # router alias/plugin entry point; its model metadata does not advertise
    # tools, but the alias resolves to a concrete model that can invoke caller
    # tools and has the openrouter:fusion server tool auto-injected.
    if not any(m["provider"] == "openrouter" and m["id"] == "openrouter/fusion" for m in allModels):
        allModels.append(
            {
                "id": "openrouter/fusion",
                "name": "OpenRouter: Fusion",
                "api": "openai-completions",
                "provider": "openrouter",
                "baseUrl": "https://openrouter.ai/api/v1",
                "reasoning": True,
                "input": ["text"],
                "cost": {
                    # we dont know about the costs because Fusion routes to multiple models
                    # and then charges you for the underlying used models
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                },
                "contextWindow": 1000000,
                "maxTokens": 30000,
            }
        )

    # Azure Foundry deploys these with larger context windows than OpenAI's own short-tier defaults.
    # See models-sold-directly-by-azure docs.
    AZURE_CONTEXT_WINDOW_OVERRIDES = {
        "gpt-5.4": 1050000,
        "gpt-5.5": 1050000,
        "gpt-5.6-luna": 1050000,
        "gpt-5.6-sol": 1050000,
        "gpt-5.6-terra": 1050000,
    }
    azureOpenAiModels: list[Model] = [
        {
            **model,
            "api": "azure-openai-responses",
            "provider": "azure",
            "baseUrl": "",
            "cost": {
                "input": model["cost"]["input"],
                "output": model["cost"]["output"],
                "cacheRead": model["cost"]["cacheRead"],
                "cacheWrite": model["cost"]["cacheWrite"],
            },
            "contextWindow": _nullish(AZURE_CONTEXT_WINDOW_OVERRIDES.get(model["id"]), model["contextWindow"]),
        }
        for model in allModels
        if model["provider"] == "openai" and model["api"] == "openai-responses"
    ]
    allModels.extend(azureOpenAiModels)

    # Azure resells DeepSeek at its own rates. US data zone, checked 2026-09-16.
    # https://azure.microsoft.com/en-us/pricing/details/ai-foundry-models/deepseek/
    AZURE_DEEPSEEK_V4_PRO_COST = {"input": 1.925, "output": 3.828, "cacheRead": 0.165, "cacheWrite": 0}
    # Azure 400s on DeepSeek's `thinking` field and on every prompt cache parameter, discards a
    # `developer` system message unbilled once reasoning_effort is set, and honours mid-convo ones (#9645).
    azureDeepSeekModels: list[Model] = [
        {
            **model,
            "provider": "azure",
            "baseUrl": "",
            "cost": AZURE_DEEPSEEK_V4_PRO_COST,
            "compat": {
                **(model.get("compat") or {}),
                "supportsDeveloperRole": False,
                "supportsMidConvoSystemMessages": True,
                "thinkingFormat": "openai",
                "supportsLongCacheRetention": False,
            },
        }
        for model in allModels
        if model["provider"] == "deepseek" and model["id"] == "deepseek-v4-pro"
    ]
    allModels.extend(azureDeepSeekModels)

    for model in allModels:
        applyOpenAICompletionsCompatMetadata(model)
        applyAnthropicMessagesCompatMetadata(model)
        applyModelsDevReasoningOptionMetadata(model)
        applyThinkingLevelMetadata(model)
        applyStrictToolCompatMetadata(model)
        applyOpenAIGrammarToolCompatMetadata(model)
        applyOpenAIToolSearchMetadata(model)
        applyOpenAICompletionsTranscriptMetadata(model)
        applyOpenAIResponsesTranscriptMetadata(model)
        applyOpenAIExplicitPromptCacheMetadata(model)
        applyOpenAIMidConvoEffortMetadata(model)
        applyPromptCacheMetadata(model)
        applyImageInputMetadata(model)
    applyAnthropicAllowedFallbackModelMetadata([model for model in allModels if isAnthropicFallbackMetadataModel(model)])

    # Keep chat and image catalogs separate so one upstream ID can expose both
    # operations with different API implementations.
    providers: dict[str, dict[str, dict[str, Model]]] = {}
    for model in allModels:
        catalog = providers.setdefault(model["provider"], {"chat": {}, "image": {}, "classifier": {}})
        # Only add if not already present (models.dev takes priority over OpenRouter).
        if catalog["chat"].get(model["id"]) is None:
            catalog["chat"][model["id"]] = {**model, "type": "chat"}
    for model in openRouterCatalog["images"]:
        applyImageInputMetadata(model)
        catalog = providers.setdefault(model["provider"], {"chat": {}, "image": {}, "classifier": {}})
        if catalog["image"].get(model["id"]) is None:
            catalog["image"][model["id"]] = model
    classifierModels: list[Model] = [
        *modelsDevClassifierModels,
        *openRouterCatalog["classifiers"],
        *aiGatewayCatalog["classifiers"],
        *OPENCODE_CLASSIFIER_MODELS,
        *CLOUDFLARE_WORKERS_AI_CLASSIFIER_MODELS,
        *OPENAI_CLASSIFIER_MODELS,
    ]
    for model in classifierModels:
        catalog = providers.setdefault(model["provider"], {"chat": {}, "image": {}, "classifier": {}})
        if catalog["classifier"].get(model["id"]) is None:
            catalog["classifier"][model["id"]] = model

    sortedProviderIds = _sortStrings(providers)
    jsonChatProviders: dict[str, dict[str, Model]] = {}
    jsonImageProviders: dict[str, dict[str, Model]] = {}
    jsonClassifierProviders: dict[str, dict[str, Model]] = {}
    jsonAllProviders: dict[str, list[Model]] = {}
    for providerId in sortedProviderIds:
        jsonChatProviders[providerId] = _localeCompareSorted(providers[providerId]["chat"])
        jsonImageProviders[providerId] = _localeCompareSorted(providers[providerId]["image"])
        jsonClassifierProviders[providerId] = _localeCompareSorted(providers[providerId]["classifier"])
        jsonAllProviders[providerId] = [
            *jsonChatProviders[providerId].values(),
            *jsonImageProviders[providerId].values(),
            *jsonClassifierProviders[providerId].values(),
        ]

    def serializeJson(value: Any) -> str:
        return f"{jsonStringify(value, 2 if generatorOptions['pretty'] else None)}\n"

    def writeJson(path: Path, value: Any) -> None:
        _writeFileSync(path, serializeJson(value))

    generatedDataProviderIds = (
        readModelDataProviderIds(packageRoot) if generatorOptions["dataOnly"] else sortedProviderIds
    )
    missingProviderIds = [providerId for providerId in generatedDataProviderIds if providerId not in jsonAllProviders]
    if len(missingProviderIds) > 0:
        raise RuntimeError(f"Cannot hydrate missing providers: {', '.join(missingProviderIds)}")

    # Only the ignored internal data is grouped by API for type derivation.
    generatedDataProviders: dict[str, dict[str, dict[str, Model]]] = {}
    modelDataStructure: dict[str, dict[str, str]] = {}
    for providerId in generatedDataProviderIds:
        grouped = groupProviderModelData(providerId, jsonAllProviders[providerId])
        generatedDataProviders[providerId] = grouped["groups"]
        modelDataStructure[providerId] = grouped["structure"]

    generatedAt = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    if not generatorOptions["jsonOnly"]:
        # Stage and validate all provider values before replacing the current generated data.
        providersDir = packageRoot / PROVIDERS
        dataDir = providersDir / "data"
        stagingRoot = Path(tempfile.mkdtemp(prefix=".model-generation-", dir=providersDir))
        stagedDataDir = stagingRoot / "data"
        previousDataDir = stagingRoot / "previous-data"
        restoreGeneratedCatalog: Any = None
        try:
            stagedDataDir.mkdir(parents=True, exist_ok=True)
            fileContents: dict[str, str] = {}
            for providerId in generatedDataProviderIds:
                filename = f"{providerId}.json"
                content = serializeJson(generatedDataProviders[providerId])
                fileContents[filename] = content
                _writeFileSync(stagedDataDir / filename, content)
            writeJson(
                stagedDataDir / MODEL_DATA_MANIFEST_FILE,
                createModelDataManifest(modelDataStructure, fileContents, generatedAt),
            )
            validateModelDataDirectory(modelDataStructure, stagedDataDir)

            if not generatorOptions["dataOnly"]:
                previousShardContents = {
                    entry: _readFileSync(providersDir / entry)
                    for entry in os.listdir(providersDir)
                    if entry.endswith("_models.py")
                }
                aggregatorPath = packageRoot / AGGREGATOR
                previousAggregator = _readFileSync(aggregatorPath)

                def restore() -> None:
                    for entry in os.listdir(providersDir):
                        if entry.endswith("_models.py"):
                            os.remove(providersDir / entry)
                    for entry, content in previousShardContents.items():
                        _writeFileSync(providersDir / entry, content)
                    _writeFileSync(aggregatorPath, previousAggregator)

                restoreGeneratedCatalog = restore

                generatedShardFiles: set[str] = set()
                for providerId in sortedProviderIds:
                    filename = f"{providerModule(providerId)}.py"
                    generatedShardFiles.add(filename)
                    _writeFileSync(providersDir / filename, renderShard(providerId))
                for entry in os.listdir(providersDir):
                    if entry.endswith("_models.py") and entry not in generatedShardFiles:
                        os.remove(providersDir / entry)

                _writeFileSync(aggregatorPath, renderAggregator(sortedProviderIds))
                _log("Generated provider catalogs and misaka/ai/models_generated.py")

            hadPreviousData = dataDir.exists()
            if hadPreviousData:
                os.rename(dataDir, previousDataDir)
            try:
                os.rename(stagedDataDir, dataDir)
                validateGeneratedModelData(packageRoot)
            except BaseException:
                _rmSync(dataDir)
                if hadPreviousData and previousDataDir.exists():
                    os.rename(previousDataDir, dataDir)
                raise
            restoreGeneratedCatalog = None
            _log(
                "Hydrated JSON model values under misaka/ai/providers/data/"
                if generatorOptions["dataOnly"]
                else "Generated JSON model values under misaka/ai/providers/data/"
            )
        except BaseException:
            if restoreGeneratedCatalog is not None:
                restoreGeneratedCatalog()
            raise
        finally:
            _rmSync(stagingRoot)

    if generatorOptions["jsonOutputDir"]:
        # `models.json` and `providers/{id}.json` retain the legacy keyed chat catalog.
        # The `.all` variants are arrays so the same upstream id can appear once per type.
        jsonOutputDir = Path(generatorOptions["jsonOutputDir"])
        providerOutputDir = jsonOutputDir / "providers"
        _rmSync(jsonOutputDir)
        providerOutputDir.mkdir(parents=True, exist_ok=True)
        writeJson(jsonOutputDir / "models.json", jsonChatProviders)
        writeJson(jsonOutputDir / "models.all.json", jsonAllProviders)
        writeJson(jsonOutputDir / "providers.json", sortedProviderIds)
        for providerId in sortedProviderIds:
            writeJson(providerOutputDir / f"{providerId}.json", jsonChatProviders[providerId])
            writeJson(providerOutputDir / f"{providerId}.all.json", jsonAllProviders[providerId])
        _log(f"Generated JSON model catalog under {generatorOptions['jsonOutputDir']}")

    # Print statistics
    totalModels = len(allModels)
    reasoningModels = len([model for model in allModels if _truthy(model.get("reasoning"))])

    _log("\nModel Statistics:")
    _log(f"  Total tool-capable models: {totalModels}")
    _log(f"  Reasoning-capable models: {reasoningModels}")

    for provider, catalog in providers.items():
        _log(
            f"  {provider}: {len(catalog['chat'])} chat models, {len(catalog['image'])} image models, "
            f"{len(catalog['classifier'])} classifier models"
        )


def main(argv: list[str] | None = None) -> int:
    global generatorOptions
    generatorOptions = readGeneratorOptions(sys.argv[1:] if argv is None else argv)
    # Run the generator
    try:
        asyncio.run(generateModels())
    except Exception as error:  # noqa: BLE001 - `generateModels().catch(...)`: printed, and the exit status says it failed
        print(_error(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
