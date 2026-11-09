"""Translated from pi's ``packages/ai/test/compat-schema.test.ts`` (v1.3.0).

MISAKA fork: TypeBox's ``Compile(schema).Default({})`` fills the top-level ``default`` annotations, which here are
``json_schema_extra`` (``schemaDefault``); ``_defaults`` reads them the same way. The cases after the upstream ones pin
the pydantic stand-in for TypeBox's semantics, the superset merge, and the MISAKA wording.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from misaka.ai.providers import compat_schema
from misaka.ai.providers.compat_schema import (
    AnthropicMessagesCompatSchema,
    BedrockCompatSchema,
    MistralConversationsCompatSchema,
    OpenAICompletionsCompatSchema,
    OpenAIResponsesCompatSchema,
    ProviderCompatSchema,
)
from misaka.ai.providers.model_schema import (
    MODEL_THINKING_LEVELS,
    THINKING_LEVELS,
    ModelInputLimitsSchema,
    ModelPromptCacheSchema,
    ThinkingLevelMapSchema,
)
from misaka.ai.utils.typebox_helpers import typeboxSchema


def _defaults(schema: type[BaseModel]) -> dict[str, Any]:
    """``Compile(schema).Default({})``."""
    return {
        name: field.json_schema_extra["default"]
        for name, field in schema.model_fields.items()
        if isinstance(field.json_schema_extra, dict) and "default" in field.json_schema_extra
    }


def _plain(annotation: Any) -> Any:
    """MISAKA: the type behind `TypeBoxLiteralUnion`'s order marker (`Annotated[Literal[...], ...]`)."""
    return get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation


def test_preserves_property_types_in_the_provider_superset():
    fields = ProviderCompatSchema.model_fields
    assert fields["supportsStore"].annotation == bool | None
    members = [_plain(member) for member in get_args(fields["sessionAffinityFormat"].annotation)]
    assert set(members) == {Literal["openai", "openai-nosession", "openrouter"], type(None)}
    # `Literal` equality ignores order; the schema keeps upstream's.
    assert get_args(members[0]) == ("openai", "openai-nosession", "openrouter")


def test_preserves_api_specific_defaults():
    completionsDefaults = _defaults(OpenAICompletionsCompatSchema)
    assert "thinkingFormat" not in completionsDefaults
    assert "supportsLongCacheRetention" not in completionsDefaults
    assert _defaults(OpenAIResponsesCompatSchema)["supportsDeveloperRole"] is True
    anthropicDefaults = _defaults(AnthropicMessagesCompatSchema)
    assert "sendSessionAffinityHeaders" not in anthropicDefaults
    assert anthropicDefaults["supportsLongCacheRetention"] is True
    assert _defaults(BedrockCompatSchema)["supportsStrictMode"] is False


def test_does_not_assign_api_specific_defaults_to_the_provider_superset():
    defaults = _defaults(ProviderCompatSchema)
    assert "supportsDeveloperRole" not in defaults
    assert "sendSessionAffinityHeaders" not in defaults
    assert "supportsLongCacheRetention" not in defaults
    assert "supportsStrictMode" not in defaults


# --- MISAKA: the superset ------------------------------------------------------------------------


def test_the_provider_superset_holds_every_key_of_the_five_apis_once():
    expected: list[str] = []
    for schema in (
        OpenAICompletionsCompatSchema,
        OpenAIResponsesCompatSchema,
        AnthropicMessagesCompatSchema,
        BedrockCompatSchema,
        MistralConversationsCompatSchema,
    ):
        expected.extend(name for name in schema.model_fields if name not in expected)
    assert list(ProviderCompatSchema.model_fields) == expected
    assert len(expected) == 40
    assert "supportsMidConvoEffort" in OpenAIResponsesCompatSchema.model_fields


def test_merging_requires_an_override_for_every_duplicate_and_a_duplicate_for_every_override():
    a = {"shared": (bool | None, compat_schema._optional("a")), "onlyA": (bool | None, compat_schema._optional("a"))}
    b = {"shared": (bool | None, compat_schema._optional("b"))}
    override = {"shared": (bool | None, compat_schema._optional("generic"))}

    assert compat_schema._mergeCompatProperties([a, b], override)["shared"] is override["shared"]
    with pytest.raises(RuntimeError, match="requires an override: shared"):
        compat_schema._mergeCompatProperties([a, b], {})
    with pytest.raises(RuntimeError, match="does not resolve a duplicate property: onlyA"):
        compat_schema._mergeCompatProperties([a, b], {**override, "onlyA": override["shared"]})


@pytest.mark.parametrize(
    "value",
    [
        {"supportsMidConvoEffort": True, "zaiToolStream": False, "future": {"any": "thing"}},
        {"chatTemplateKwargs": {"budget": {"$var": "thinking.budget", "omitWhenOff": True}, "flag": None}},
        {"thinkingTokenBudgetField": "thinking_budget_tokens", "supportsMidConvoToolAdditions": True},
        {"sessionAffinityFormat": "openrouter", "supportsMidConvoToolChanges": True},
        {"openRouterRouting": {"sort": {"by": "price", "partition": None}, "preferred_min_throughput": {"p90": 10}}},
        {"allowedFallbackModels": [{"provider": "anthropic", "model": "m", "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0}}]},
    ],
)
def test_the_provider_superset_accepts_every_apis_keys(value):
    ProviderCompatSchema.model_validate(value)


@pytest.mark.parametrize(
    "value",
    [
        # `Type.Optional` is absent, never null.
        {"supportsMidConvoEffort": None},
        # Every declared key is type-checked, whichever API declares it.
        {"zaiToolStream": "yes"},
        {"supportsMidConvoToolAdditions": 1},
        {"thinkingTokenBudgetField": "budget"},
        {"chatTemplateKwargs": {"x": {"$var": "thinking.level"}}},
        {"allowedFallbackModels": [{"provider": "", "model": "m", "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0}}]},
        {"allowedFallbackModels": [{"provider": "p", "model": "m", "cost": {"input": 1, "output": 2, "cacheRead": 0, "cacheWrite": 0}}] * 4},
    ],
)
def test_the_provider_superset_rejects_null_and_mistyped_values(value):
    with pytest.raises(ValidationError):
        ProviderCompatSchema.model_validate(value)


# --- MISAKA: model-schema ------------------------------------------------------------------------


def test_model_schema_constants_and_enums_agree():
    assert MODEL_THINKING_LEVELS == ("off", *THINKING_LEVELS)
    assert set(ThinkingLevelMapSchema.model_fields) == set(MODEL_THINKING_LEVELS)
    ThinkingLevelMapSchema.model_validate({"off": None, "high": "max"})
    with pytest.raises(ValidationError):
        ModelPromptCacheSchema.model_validate({"short": 0})
    assert ModelInputLimitsSchema.model_validate({"maxRequestBytes": 2.0}).maxRequestBytes == 2
    with pytest.raises(ValidationError):
        ModelInputLimitsSchema.model_validate({"maxRequestBytes": 2.5})


# --- MISAKA: what a generated JSON schema shows --------------------------------------------------


def test_the_json_schema_carries_typebox_metadata():
    schema = typeboxSchema(ProviderCompatSchema)
    assert schema["additionalProperties"] is True
    assert schema["description"] == "Provider and model compatibility overrides."
    assert schema["properties"]["supportsMidConvoEffort"]["type"] == "boolean"
    assert schema["properties"]["supportsMidConvoEffort"]["default"] is False
    assert "default" not in schema["properties"]["supportsDeveloperRole"]
    assert schema["properties"]["allowedFallbackModels"]["maxItems"] == 3
    limits = typeboxSchema(ModelInputLimitsSchema)
    assert limits["properties"]["maxRequestBytes"] == {
        "type": "integer",
        "minimum": 1,
        "description": "Maximum serialized provider request size in bytes.",
    }


def test_descriptions_name_misaka_not_pi():
    text = json.dumps([typeboxSchema(ProviderCompatSchema), typeboxSchema(ModelPromptCacheSchema)])
    assert "MISAKA" in text
    assert " Pi " not in text and " pi " not in text and "pi-controlled" not in text
