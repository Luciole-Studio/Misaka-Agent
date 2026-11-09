"""Translated from pi's ``packages/ai/src/providers/all.ts`` (v1.1.0).

pi's ``BuiltinChatModelId``/``BuiltinImageModelId``/``BuiltinClassifierModelId``/``CatalogApi`` are type-level only.
Import from this module by name (``from misaka.ai.providers.all import ...``): binding the module as ``all`` would
shadow the builtin.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from misaka.ai.models import CreateModelsOptions, MutableModels, Provider, createModels
from misaka.ai.models_generated import CLASSIFIER_MODELS, IMAGE_MODELS, MODELS
from misaka.ai.providers.amazon_bedrock import amazonBedrockProvider
from misaka.ai.providers.ant_ling import antLingProvider
from misaka.ai.providers.anthropic import anthropicProvider
from misaka.ai.providers.azure import azureProvider
from misaka.ai.providers.baseten import basetenProvider
from misaka.ai.providers.cerebras import cerebrasProvider
from misaka.ai.providers.cloudflare_ai_gateway import cloudflareAIGatewayProvider
from misaka.ai.providers.cloudflare_workers_ai import cloudflareWorkersAIProvider
from misaka.ai.providers.deepseek import deepseekProvider
from misaka.ai.providers.fireworks import fireworksProvider
from misaka.ai.providers.github_copilot import githubCopilotProvider
from misaka.ai.providers.google import googleProvider
from misaka.ai.providers.google_vertex import googleVertexProvider
from misaka.ai.providers.groq import groqProvider
from misaka.ai.providers.huggingface import huggingfaceProvider
from misaka.ai.providers.kimi_coding import kimiCodingProvider
from misaka.ai.providers.meta import metaProvider
from misaka.ai.providers.minimax import minimaxProvider
from misaka.ai.providers.minimax_cn import minimaxCnProvider
from misaka.ai.providers.mistral import mistralProvider
from misaka.ai.providers.moonshotai import moonshotaiProvider
from misaka.ai.providers.moonshotai_cn import moonshotaiCnProvider
from misaka.ai.providers.nvidia import nvidiaProvider
from misaka.ai.providers.openai import openaiProvider
from misaka.ai.providers.openai_codex import openaiCodexProvider
from misaka.ai.providers.opencode import opencodeProvider
from misaka.ai.providers.opencode_go import opencodeGoProvider
from misaka.ai.providers.openrouter import openrouterProvider
from misaka.ai.providers.qwen_token_plan import qwenTokenPlanProvider
from misaka.ai.providers.qwen_token_plan_cn import qwenTokenPlanCnProvider
from misaka.ai.providers.qwen_token_plan_individual import (
    qwenTokenPlanIndividualProvider,
)
from misaka.ai.providers.radius import radiusProvider
from misaka.ai.providers.together import togetherProvider
from misaka.ai.providers.typesafe import typesafeProvider
from misaka.ai.providers.vercel_ai_gateway import vercelAIGatewayProvider
from misaka.ai.providers.xai import xaiProvider
from misaka.ai.providers.xiaomi import xiaomiProvider
from misaka.ai.providers.xiaomi_token_plan_ams import xiaomiTokenPlanAmsProvider
from misaka.ai.providers.xiaomi_token_plan_cn import xiaomiTokenPlanCnProvider
from misaka.ai.providers.xiaomi_token_plan_sgp import xiaomiTokenPlanSgpProvider
from misaka.ai.providers.zai import zaiProvider
from misaka.ai.providers.zai_coding_cn import zaiCodingCnProvider
from misaka.ai.types import AnyModel, ClassifierModel, ImageModel, Model

# MISAKA fork: `import modelDataManifest from "./data/.manifest.json"` reads the JSON.
_modelDataManifest = json.loads((Path(__file__).parent / "data" / ".manifest.json").read_text(encoding="utf-8"))

# Providers present in the generated catalog. `KnownProvider` additionally
# includes purely dynamic providers (e.g. "radius") that have no static
# catalog entry.
type BuiltinProvider = str


def getBuiltinModel(provider: BuiltinProvider, modelId: str) -> Model | None:
    """Typed read of one generated built-in chat model."""
    return (MODELS.get(provider) or {}).get(modelId)


def getBuiltinImageModel(provider: BuiltinProvider, modelId: str) -> ImageModel | None:
    """Typed read of one generated built-in image model."""
    return (IMAGE_MODELS.get(provider) or {}).get(modelId)


def getBuiltinClassifierModel(provider: BuiltinProvider, modelId: str) -> ClassifierModel | None:
    """Typed read of one generated built-in classifier model."""
    return (CLASSIFIER_MODELS.get(provider) or {}).get(modelId)


def getBuiltinProviders() -> list[BuiltinProvider]:
    return list(MODELS.keys())


def _dateParse(value: Any) -> float:
    """MISAKA fork: `Date.parse(value)` for the manifest's ISO timestamp: milliseconds, or `NaN`."""
    try:
        return datetime.fromisoformat(value).timestamp() * 1000
    except (TypeError, ValueError):
        return math.nan


def getBuiltinModelDataGeneratedAt() -> int | None:
    """Generation timestamp shared by all built-in provider catalogs."""
    generatedAt = _dateParse(_modelDataManifest.get("generatedAt"))
    return None if math.isnan(generatedAt) else int(generatedAt)


def getBuiltinModels(provider: BuiltinProvider) -> list[Model]:
    models = MODELS.get(provider)
    return list((models or {}).values())


def getBuiltinImageModels(provider: BuiltinProvider) -> list[ImageModel]:
    models = IMAGE_MODELS.get(provider)
    return list((models or {}).values())


def getBuiltinClassifierModels(provider: BuiltinProvider) -> list[ClassifierModel]:
    models = CLASSIFIER_MODELS.get(provider)
    return list((models or {}).values())


def getAllBuiltinModels(provider: BuiltinProvider) -> list[AnyModel]:
    return [*getBuiltinModels(provider), *getBuiltinImageModels(provider), *getBuiltinClassifierModels(provider)]


def builtinProviders() -> list[Provider]:
    """All built-in providers, freshly constructed."""
    return [
        amazonBedrockProvider(),
        antLingProvider(),
        anthropicProvider(),
        azureProvider(),
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
        typesafeProvider(),
        vercelAIGatewayProvider(),
        xaiProvider(),
        xiaomiProvider(),
        xiaomiTokenPlanAmsProvider(),
        xiaomiTokenPlanCnProvider(),
        xiaomiTokenPlanSgpProvider(),
        zaiProvider(),
        zaiCodingCnProvider(),
    ]


def builtinModels(options: CreateModelsOptions | None = None) -> MutableModels:
    """A `Models` collection with every built-in provider registered."""
    models = createModels(options)
    for provider in builtinProviders():
        models.setProvider(provider)
    return models


__all__ = [
    "BuiltinProvider",
    "builtinModels",
    "builtinProviders",
    "getAllBuiltinModels",
    "getBuiltinClassifierModel",
    "getBuiltinClassifierModels",
    "getBuiltinImageModel",
    "getBuiltinImageModels",
    "getBuiltinModel",
    "getBuiltinModelDataGeneratedAt",
    "getBuiltinModels",
    "getBuiltinProviders",
    "radiusProvider",
]
