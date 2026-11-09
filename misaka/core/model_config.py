"""Immutable, credential-blind models.json snapshot.

Translated from pi's ``coding-agent/src/core/model-config.ts`` (v1.3.0). The schema is TypeBox's there: object
schemas that allow extra keys, ``Type.Optional`` fields that reject an explicit ``null``, and values that are never
coerced. The shared pieces (compat, cost, input limits, prompt cache, thinking-level map) are the ai package's
(``misaka.ai.providers.model_schema`` / ``compat_schema``, ``TypeBoxObject``); any schema error discards the whole file,
as upstream's does.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    Field,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    create_model,
)

from misaka.ai.providers.compat_schema import ProviderCompatSchema
from misaka.ai.providers.model_schema import (
    MODEL_INPUT_MODALITIES,
    ModelCostSchema,
    ModelInputLimitsSchema,
    ModelPromptCacheSchema,
    ThinkingLevelMapSchema,
    TypeBoxLiteralUnion,
    TypeBoxObject,
)
from misaka.utils.json import stripJsonComments
from misaka.utils.paths import normalizePath

type _SamplingParamsSchema = dict[str, Any]
# MISAKA fork: `ModelInputModalitySchema` as a field, in its declared order (`TypeBoxLiteralUnion`).
_ModelInputModalityField = TypeBoxLiteralUnion(*MODEL_INPUT_MODALITIES)


class _SamplingParamsByThinkingLevelSchema(TypeBoxObject):
    off: _SamplingParamsSchema | None = None
    minimal: _SamplingParamsSchema | None = None
    low: _SamplingParamsSchema | None = None
    medium: _SamplingParamsSchema | None = None
    high: _SamplingParamsSchema | None = None
    xhigh: _SamplingParamsSchema | None = None
    max: _SamplingParamsSchema | None = None


# `Type.Number({ exclusiveMinimum: 0 })`.
_PositiveTokenCountSchema = Annotated[float, Field(gt=0)]


def _keepAsWritten(value: Any, handler: ValidatorFunctionWrapHandler) -> Any:
    handler(value)
    return value


# `compat: Type.Optional(ProviderCompatSchema)`. MISAKA fork: the block is checked against the schema and kept as
# written, a mapping with its custom keys; the composer builds the runtime compat model from it.
_CompatBlock = Annotated[ProviderCompatSchema | None, WrapValidator(_keepAsWritten)]  # type: ignore[valid-type]


def _partial(schema: type[TypeBoxObject]) -> type[TypeBoxObject]:
    """``Type.Partial(schema)``: every field optional, with its own description."""
    return create_model(  # type: ignore[call-overload, no-any-return]
        f"_Partial{schema.__name__}",
        __base__=TypeBoxObject,
        __module__=__name__,
        __doc__=schema.__doc__,
        **{
            name: (field.annotation | None, Field(default=None, description=field.description))
            for name, field in schema.model_fields.items()
        },
    )


class _ModelDefinitionSchema(TypeBoxObject):
    id: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1)
    api: str | None = Field(default=None, min_length=1)
    baseUrl: str | None = Field(default=None, min_length=1)
    reasoning: bool | None = None
    thinkingLevelMap: ThinkingLevelMapSchema | None = None
    input: list[_ModelInputModalityField] | None = None
    inputLimits: ModelInputLimitsSchema | None = None
    cost: ModelCostSchema | None = None
    promptCache: ModelPromptCacheSchema | None = None
    contextWindow: _PositiveTokenCountSchema | None = None
    maxTokens: _PositiveTokenCountSchema | None = None
    samplingParams: _SamplingParamsSchema | None = None
    samplingParamsByThinkingLevel: _SamplingParamsByThinkingLevelSchema | None = None
    headers: dict[str, str] | None = None
    compat: _CompatBlock = None


_PartialModelCostSchema = _partial(ModelCostSchema)


class _ModelOverrideSchema(TypeBoxObject):
    name: str | None = Field(default=None, min_length=1)
    reasoning: bool | None = None
    thinkingLevelMap: ThinkingLevelMapSchema | None = None
    input: list[_ModelInputModalityField] | None = None
    inputLimits: ModelInputLimitsSchema | None = None
    cost: _PartialModelCostSchema | None = None  # type: ignore[valid-type]
    promptCache: ModelPromptCacheSchema | None = None
    contextWindow: _PositiveTokenCountSchema | None = None
    maxTokens: _PositiveTokenCountSchema | None = None
    samplingParams: _SamplingParamsSchema | None = None
    samplingParamsByThinkingLevel: _SamplingParamsByThinkingLevelSchema | None = None
    headers: dict[str, str] | None = None
    compat: _CompatBlock = None


class _ProviderConfigSchema(TypeBoxObject):
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


class ModelsConfigSchema(TypeBoxObject):
    # `{ $schema: Type.Optional(Type.String()), ...ModelsConfigProperties }`.
    schema_: str | None = Field(default=None, alias="$schema")
    providers: dict[str, _ProviderConfigSchema]


ModelsJsonModel = _ModelDefinitionSchema
ModelsJsonModelOverride = _ModelOverrideSchema
ModelsJsonProvider = _ProviderConfigSchema


def _formatValidationPath(location: tuple[Any, ...]) -> str:
    path = ".".join(str(part) for part in location if part != "")
    return path or "root"


def _schemaErrors(error: ValidationError) -> str:
    return "\n".join(
        f"  - {_formatValidationPath(tuple(item.get('loc', ())))}: {item['msg']}"
        for item in error.errors(include_url=False)
    ) or "Unknown schema error"


class ModelConfig:
    """One immutable load of models.json."""

    def __init__(self, providers: dict[str, ModelsJsonProvider], error: str | None = None) -> None:
        # Private by convention: build one with `ModelConfig.load`.
        self._providers = providers
        self._error = error

    @classmethod
    async def load(cls, modelsJsonPath: str | None) -> ModelConfig:
        if not modelsJsonPath:
            return cls({})
        path = normalizePath(modelsJsonPath)
        try:
            content = await asyncio.to_thread(Path(path).read_text, encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return cls({})
        except OSError as error:
            return cls({}, f"Failed to load models.json: {error}\n\nFile: {path}")

        try:
            parsed = json.loads(stripJsonComments(content.removeprefix("﻿")))
        except ValueError as error:
            return cls({}, f"Failed to parse models.json: {error}\n\nFile: {path}")

        try:
            config = ModelsConfigSchema.model_validate(parsed)
        except ValidationError as error:
            return cls({}, f"Invalid models.json schema:\n{_schemaErrors(error)}\n\nFile: {path}")

        return cls(dict(config.providers))

    def getProvider(self, providerId: str) -> ModelsJsonProvider | None:
        return self._providers.get(providerId)

    def getProviderIds(self) -> list[str]:
        return list(self._providers)

    def getError(self) -> str | None:
        return self._error


__all__ = [
    "ModelConfig",
    "ModelsConfigSchema",
    "ModelsJsonModel",
    "ModelsJsonModelOverride",
    "ModelsJsonProvider",
]
