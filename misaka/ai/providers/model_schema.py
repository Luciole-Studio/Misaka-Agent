# ruff: noqa: UP040 - TypeAlias spelling kept in step with misaka/ai/types.py.

"""Translated from pi's ``packages/ai/src/providers/model-schema.ts`` (v1.3.0).

MISAKA fork: a TypeBox object schema is a pydantic model on ``TypeBoxObject``, which keeps TypeBox's validation
semantics (extra keys allowed, ``Type.Optional`` fields absent but never ``null``, no coercion); an enum schema is a
``Literal`` alias. A schema's ``description`` is the class docstring or the field's ``Field(description=...)``, and a
TypeBox ``default`` annotation is ``json_schema_extra``, so ``typeboxSchema`` and the schema generator emit what
TypeBox emits. ``Static<typeof X>`` of an enum is the same ``Literal``; of an object it is the runtime record of the same
name in ``misaka.ai.types`` (``ModelCost``, ``ModelInputLimits``, ...), which this module cannot import (``types``
imports this one).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal, TypeAlias, get_args

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
)

THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
MODEL_THINKING_LEVELS = ("off", *THINKING_LEVELS)
MODEL_INPUT_MODALITIES = ("text", "image")
CACHE_RETENTIONS = ("none", "short", "long")
MODEL_PROMPT_CACHE_RETENTIONS = ("short", "long")

ThinkingLevelSchema: TypeAlias = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ModelThinkingLevelSchema: TypeAlias = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
ModelInputModalitySchema: TypeAlias = Literal["text", "image"]
CacheRetentionSchema: TypeAlias = Literal["none", "short", "long"]
# MISAKA fork: `Type.Enum(MODEL_PROMPT_CACHE_RETENTIONS)`, named.
_ModelPromptCacheRetentionSchema: TypeAlias = Literal["short", "long"]

# MISAKA fork: the `Literal`s spell the constants out; they must name the same values in the same order.
for _schema, _values in (
    (ThinkingLevelSchema, THINKING_LEVELS),
    (ModelThinkingLevelSchema, MODEL_THINKING_LEVELS),
    (ModelInputModalitySchema, MODEL_INPUT_MODALITIES),
    (CacheRetentionSchema, CACHE_RETENTIONS),
    (_ModelPromptCacheRetentionSchema, MODEL_PROMPT_CACHE_RETENTIONS),
):
    if get_args(_schema) != _values:
        raise AssertionError(f"{_schema} does not match {_values}")


class TypeBoxObject(BaseModel):
    """MISAKA fork: the pydantic base of a TypeBox ``Type.Object`` schema that validates a configuration file.

    Extra keys are allowed (and not kept), ``Type.Optional`` fields may be absent but not ``null`` unless the field
    lists ``null`` itself (``_nullable``), and a value of the wrong type is an error rather than converted (strict).
    Frozen stands in for upstream's ``deepFreeze`` of the validated value.
    """

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    # Field names whose TypeBox type includes `Type.Null()`.
    _nullable: ClassVar[frozenset[str]] = frozenset()

    @field_validator("*", mode="before")
    @classmethod
    def _reject_null(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None and info.field_name not in cls._nullable:
            raise ValueError("Expected a value, not null")
        return value


def _integral(value: Any) -> Any:
    # `Type.Integer` is `Number.isInteger`: JSON's `2.0` is the integer 2 in JavaScript.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def TypeBoxInteger(**constraints: Any) -> Any:
    """MISAKA fork: ``Type.Integer(constraints)``. The constraints come before the validator so pydantic applies them
    to the integer itself, and a JSON schema shows them as ``minimum``/``maximum``."""
    return Annotated[int, Field(**constraints), BeforeValidator(_integral)]


# `Type.Integer()`.
Integer = TypeBoxInteger()


@dataclass(frozen=True, slots=True)
class _InDeclaredOrder:
    values: tuple[Any, ...]


def TypeBoxLiteralUnion(*values: Any) -> Any:
    """MISAKA fork: ``Type.Union([Type.Literal(a), Type.Literal(b), ...])``, a ``Literal`` of ``values`` whose schema
    lists them in this order whatever the process imported before. ``typing`` caches ``Union[...]`` by equality, and a
    ``Literal``'s equality ignores the order of its values, so ``Literal["a", "b"] | None`` can come back holding the
    ``Literal["b", "a"]`` some other module built first, and a generated ``enum`` would follow import order. Carried in
    ``Annotated``, the values in their order keep two such unions apart unless the orders agree. Validation is the
    ``Literal``'s."""
    return Annotated[Literal[values], _InDeclaredOrder(values)]


def schemaDefault(default: Any) -> dict[str, Any]:
    """MISAKA fork: a TypeBox ``default`` annotation, as ``Field(json_schema_extra=...)``. It documents the value the
    reader assumes; validation never fills it in, as TypeBox's ``Check`` does not."""
    return {"default": default}


# `Static<typeof ...>` of the enum schemas.
ThinkingLevel: TypeAlias = ThinkingLevelSchema
ModelThinkingLevel: TypeAlias = ModelThinkingLevelSchema
ModelInputModality: TypeAlias = ModelInputModalitySchema
CacheRetention: TypeAlias = CacheRetentionSchema
ThinkingLevelMap: TypeAlias = dict[ModelThinkingLevel, str | None]
# Best-effort prompt cache lifetime in seconds for each retention tier a request can ask for.
# A missing tier means the lifetime is unknown; pi does not warm such caches.
ModelPromptCache: TypeAlias = dict[_ModelPromptCacheRetentionSchema, int | float]


class ThinkingLevelMapSchema(TypeBoxObject):
    # `Type.Partial(Type.Record(ModelThinkingLevelSchema, Type.Union([Type.String(), Type.Null()])))`.
    _nullable: ClassVar[frozenset[str]] = frozenset(MODEL_THINKING_LEVELS)

    off: str | None = None
    minimal: str | None = None
    low: str | None = None
    medium: str | None = None
    high: str | None = None
    xhigh: str | None = None
    max: str | None = None


class ModelPromptCacheSchema(TypeBoxObject):
    """Best-effort prompt cache lifetime in seconds for each retention tier. A missing tier means the lifetime is unknown, so MISAKA does not warm it."""

    # `Type.Partial(Type.Record(Type.Enum(MODEL_PROMPT_CACHE_RETENTIONS), Type.Number({ exclusiveMinimum: 0 })))`.
    short: Annotated[float, Field(gt=0)] | None = None
    long: Annotated[float, Field(gt=0)] | None = None


class ModelCostRatesSchema(TypeBoxObject):
    # `ModelCostRatesProperties`.
    input: float = Field(description="Input cost in USD per million tokens.")
    output: float = Field(description="Output cost in USD per million tokens.")
    cacheRead: float = Field(description="Cache-read cost in USD per million tokens.")
    cacheWrite: float = Field(description="Cache-write cost in USD per million tokens.")


class ModelCostTierSchema(TypeBoxObject):
    inputTokensAbove: float = Field(description="Use this tier when total request input exceeds this token count.")
    # `...ModelCostRatesProperties`.
    input: float = Field(description="Input cost in USD per million tokens.")
    output: float = Field(description="Output cost in USD per million tokens.")
    cacheRead: float = Field(description="Cache-read cost in USD per million tokens.")
    cacheWrite: float = Field(description="Cache-write cost in USD per million tokens.")


class ModelCostSchema(ModelCostRatesSchema):
    # `...ModelCostRatesProperties`, then:
    tiers: list[ModelCostTierSchema] | None = Field(
        default=None,
        description="Request-wide pricing tiers. The highest matching input threshold applies to the full request.",
    )


class ModelImageResizeOptionsSchema(TypeBoxObject):
    # `modelImageResizeOptions()`: the call sites that pass options give them as the field's `Field(...)`.
    maxWidth: TypeBoxInteger(ge=1) | None = None
    maxHeight: TypeBoxInteger(ge=1) | None = None
    maxBytes: TypeBoxInteger(ge=1) | None = Field(
        default=None, description="Maximum base64-encoded payload size in bytes."
    )
    jpegQuality: TypeBoxInteger(ge=1, le=100) | None = None


class ModelImageInputLimitsSchema(TypeBoxObject):
    resize: ModelImageResizeOptionsSchema | None = Field(
        default=None,
        description="Cache-safe resize profile applied before a new image enters conversation history.",
    )
    maxPerMessage: TypeBoxInteger(ge=1) | None = Field(
        default=None, description="Maximum images accepted in one provider message."
    )
    maxPerRequest: TypeBoxInteger(ge=1) | None = Field(
        default=None, description="Maximum images accepted across one provider request."
    )


class ModelInputLimitsSchema(TypeBoxObject):
    maxRequestBytes: TypeBoxInteger(ge=1) | None = Field(
        default=None, description="Maximum serialized provider request size in bytes."
    )
    images: ModelImageInputLimitsSchema | None = None


__all__ = [
    "CACHE_RETENTIONS",
    "MODEL_INPUT_MODALITIES",
    "MODEL_PROMPT_CACHE_RETENTIONS",
    "MODEL_THINKING_LEVELS",
    "THINKING_LEVELS",
    "CacheRetention",
    "CacheRetentionSchema",
    "Integer",
    "ModelCostRatesSchema",
    "ModelCostSchema",
    "ModelCostTierSchema",
    "ModelImageInputLimitsSchema",
    "ModelImageResizeOptionsSchema",
    "ModelInputLimitsSchema",
    "ModelInputModality",
    "ModelInputModalitySchema",
    "ModelPromptCache",
    "ModelPromptCacheSchema",
    "ModelThinkingLevel",
    "ModelThinkingLevelSchema",
    "ThinkingLevel",
    "ThinkingLevelMap",
    "ThinkingLevelMapSchema",
    "ThinkingLevelSchema",
    "TypeBoxInteger",
    "TypeBoxLiteralUnion",
    "TypeBoxObject",
    "schemaDefault",
]
