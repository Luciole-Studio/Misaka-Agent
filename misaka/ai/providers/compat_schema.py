# ruff: noqa: UP040 - descriptions are upstream's sentences, one per line; TypeAlias spelling as in types.py.

"""Translated from pi's ``packages/ai/src/providers/compat-schema.ts`` (v1.3.0).

MISAKA fork: the TypeBox schemas are pydantic models on ``model_schema.TypeBoxObject`` (TypeBox's validation
semantics; see that module), descriptions are ``Field(description=...)`` or the class docstring, TypeBox ``default``
annotations are ``json_schema_extra`` (``schemaDefault``), and ``additionalProperties: true`` is the compat classes'
``json_schema_extra``. A union schema that takes options (``sessionAffinityFormat``, ``thinkingTokenBudgetField``) is a
``Literal`` written at the field, with the options as that field's ``Field``; ``Type.Optional(...)``,
``optionalCompatBoolean(...)`` and ``optionalSessionAffinityFormat(...)`` are the field's annotation plus ``_optional``.
``Static<typeof X>`` of the object schemas (``OpenRouterRouting``, ``OpenAICompletionsCompat``, ...) are the runtime
records of those names in ``misaka.ai.types``, which this module cannot import (``types`` imports this one). The
descriptions say MISAKA where pi's say Pi.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import copy
from typing import Annotated, Any, ClassVar, Literal, TypeAlias, get_args

from pydantic import ConfigDict, Field, create_model
from pydantic.fields import FieldInfo

from misaka.ai.providers.model_schema import (
    ModelCostSchema,
    TypeBoxLiteralUnion,
    TypeBoxObject,
    schemaDefault,
)

_NO_DEFAULT: Any = object()


def _optional(description: str, default: Any = _NO_DEFAULT) -> Any:
    """MISAKA fork: ``Type.Optional(T({ description, default? }))`` without ``T``, which is the field's annotation."""
    return Field(
        default=None,
        description=description,
        json_schema_extra=None if default is _NO_DEFAULT else schemaDefault(default),
    )


# `sessionAffinityFormat(options)` and `thinkingTokenBudgetField(options)` without their options.
_SessionAffinityFormatLiteral: TypeAlias = Literal["openai", "openai-nosession", "openrouter"]
_ThinkingTokenBudgetFieldLiteral: TypeAlias = Literal[
    "thinking_token_budget", "thinking_budget", "thinking_budget_tokens"
]
# MISAKA fork: the two as schema fields, whose `enum` keeps this order (`TypeBoxLiteralUnion`).
_SessionAffinityFormatField = TypeBoxLiteralUnion(*get_args(_SessionAffinityFormatLiteral))
_ThinkingTokenBudgetFieldField = TypeBoxLiteralUnion(*get_args(_ThinkingTokenBudgetFieldLiteral))

SessionAffinityFormatSchema: TypeAlias = Annotated[
    _SessionAffinityFormatLiteral,
    Field(description="Session-affinity header format used to route related requests consistently."),
]

ThinkingTokenBudgetFieldSchema: TypeAlias = Annotated[
    _ThinkingTokenBudgetFieldLiteral,
    Field(
        description='Top-level request field name used by OpenAI-compatible endpoints to cap reasoning tokens. "thinking_token_budget" is used by vLLM, "thinking_budget" by Qwen, DashScope, or SGLang, and "thinking_budget_tokens" by llama.cpp.'
    ),
]


class _ChatTemplateKwargVariableSchema(TypeBoxObject):
    var: Literal["thinking.enabled", "thinking.effort", "thinking.budget"] = Field(alias="$var")
    omitWhenOff: bool | None = None


ChatTemplateKwargValueSchema: TypeAlias = str | float | bool | None | _ChatTemplateKwargVariableSchema


def _percentileCutoffs(name: str, metric: str) -> type[TypeBoxObject]:
    """``percentileCutoffs(metric)``. MISAKA fork: ``name`` names the class, so the two schemas stay apart."""
    return create_model(
        name,
        __base__=TypeBoxObject,
        __module__=__name__,
        p50=(float | None, _optional(f"{metric} at the 50th percentile.")),
        p75=(float | None, _optional(f"{metric} at the 75th percentile.")),
        p90=(float | None, _optional(f"{metric} at the 90th percentile.")),
        p99=(float | None, _optional(f"{metric} at the 99th percentile.")),
    )


class _OpenRouterRoutingSortSchema(TypeBoxObject):
    _nullable: ClassVar[frozenset[str]] = frozenset({"partition"})

    by: str | None = _optional('The sorting metric, such as "price", "throughput", or "latency".')
    partition: str | None = _optional('Partitioning strategy: "model" or "none".', "model")


class _OpenRouterRoutingMaxPriceSchema(TypeBoxObject):
    """Maximum price per million tokens in USD."""

    prompt: float | str | None = _optional("Price per million prompt tokens.")
    completion: float | str | None = _optional("Price per million completion tokens.")
    image: float | str | None = _optional("Price per image.")
    audio: float | str | None = _optional("Price per audio unit.")
    request: float | str | None = _optional("Price per request.")


_MinimumThroughputCutoffsSchema = _percentileCutoffs("_MinimumThroughputCutoffsSchema", "Minimum tokens per second")
_MaximumLatencyCutoffsSchema = _percentileCutoffs("_MaximumLatencyCutoffsSchema", "Maximum latency in seconds")


class OpenRouterRoutingSchema(TypeBoxObject):
    """OpenRouter provider routing preferences. Controls which upstream providers OpenRouter routes requests to. Sent as the provider field in the OpenRouter API request body. See https://openrouter.ai/docs/guides/routing/provider-selection."""

    allow_fallbacks: bool | None = _optional("Whether to allow backup providers to serve requests.", True)
    require_parameters: bool | None = _optional(
        "Whether to filter providers to only those that support all parameters in the request.", False
    )
    data_collection: TypeBoxLiteralUnion("deny", "allow") | None = _optional(
        'Data collection setting. "allow": allow providers that may store or train on data. "deny": only use providers that do not collect user data.',
        "allow",
    )
    zdr: bool | None = _optional("Whether to restrict routing to only ZDR (Zero Data Retention) endpoints.")
    enforce_distillable_text: bool | None = _optional(
        "Whether to restrict routing to only models that allow text distillation."
    )
    order: list[str] | None = _optional(
        "An ordered list of provider names or slugs to try in sequence, falling back to the next if unavailable."
    )
    only: list[str] | None = _optional("List of provider names or slugs to exclusively allow for this request.")
    ignore: list[str] | None = _optional("List of provider names or slugs to skip for this request.")
    quantizations: list[str] | None = _optional(
        'A list of quantization levels to filter providers by, for example ["fp16", "bf16", "fp8", "fp6", "int8", "int4", "fp4", "fp32"].'
    )
    sort: str | _OpenRouterRoutingSortSchema | None = _optional(
        'Sorting strategy. Can be a string such as "price", "throughput", or "latency", or an object.'
    )
    max_price: _OpenRouterRoutingMaxPriceSchema | None = None
    preferred_min_throughput: float | _MinimumThroughputCutoffsSchema | None = _optional(  # type: ignore[valid-type]
        "Preferred minimum throughput in tokens per second. A number applies to p50."
    )
    preferred_max_latency: float | _MaximumLatencyCutoffsSchema | None = _optional(  # type: ignore[valid-type]
        "Preferred maximum latency in seconds. A number applies to p50."
    )


class VercelGatewayRoutingSchema(TypeBoxObject):
    """Vercel AI Gateway routing preferences. Controls which upstream providers the gateway routes requests to. See https://vercel.com/docs/ai-gateway/models-and-providers/provider-options."""

    only: list[str] | None = _optional(
        'List of provider slugs to exclusively use for this request, for example ["bedrock", "anthropic"].'
    )
    order: list[str] | None = _optional(
        'List of provider slugs to try in order, for example ["anthropic", "openai"].'
    )


class AnthropicAllowedFallbackModelSchema(TypeBoxObject):
    """An Anthropic server-side refusal fallback model with local pricing metadata."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    cost: ModelCostSchema


_optionalCompatBoolean = _optional
_optionalSessionAffinityFormat = _optional


class _CompatSchemaObject(TypeBoxObject):
    # MISAKA fork: the base of the compat schemas, which upstream declares with `additionalProperties: true`. A comment,
    # not a docstring: pydantic would publish a docstring as the schema's description.
    model_config = ConfigDict(json_schema_extra={"additionalProperties": True})


class OpenAICompletionsCompatSchema(_CompatSchemaObject):
    """Compatibility settings for OpenAI-compatible completions APIs. Use this to override URL-based auto-detection for custom providers."""

    supportsStore: bool | None = _optional(
        "Whether the provider supports the store field. Default: auto-detected from URL."
    )
    supportsDeveloperRole: bool | None = _optionalCompatBoolean(
        "Whether the provider supports the developer role instead of system. Default: auto-detected from URL."
    )
    supportsReasoningEffort: bool | None = _optional(
        "Whether the provider supports reasoning_effort. Default: auto-detected from URL."
    )
    supportsUsageInStreaming: bool | None = _optional(
        "Whether the provider supports stream_options.include_usage for token usage in streaming responses.", True
    )
    supportsFinishReason: bool | None = _optional(
        "Whether streamed responses include finish_reason. When false, MISAKA infers stop or toolUse when the stream ends.",
        True,
    )
    maxTokensField: TypeBoxLiteralUnion("max_completion_tokens", "max_tokens") | None = _optional(
        "Which field to use for max tokens. Default: auto-detected from URL."
    )
    requiresToolResultName: bool | None = _optional(
        "Whether tool results require the name field. Default: auto-detected from URL."
    )
    requiresAssistantAfterToolResult: bool | None = _optional(
        "Whether a user message after tool results requires an assistant message in between. Default: auto-detected from URL."
    )
    requiresThinkingAsText: bool | None = _optional(
        "Whether thinking blocks must be converted to text blocks with <thinking> delimiters. Default: auto-detected from URL."
    )
    requiresReasoningContentOnAssistantMessages: bool | None = _optional(
        "Whether all replayed assistant messages must include an empty reasoning_content field when reasoning is enabled. Default: auto-detected from URL."
    )
    thinkingFormat: (
        TypeBoxLiteralUnion(
            "openai",
            "openrouter",
            "deepseek",
            "together",
            "baseten",
            "zai",
            "qwen",
            "chat-template",
            "qwen-chat-template",
            "string-thinking",
            "ant-ling",
        )
        | None
    ) = _optional(
        'Format for reasoning or thinking parameters. When omitted, MISAKA auto-detects the format from the provider URL. "openai" uses reasoning_effort, "openrouter" uses reasoning.effort, "deepseek" uses thinking.type plus reasoning_effort when supported, "together" uses reasoning.enabled plus reasoning_effort when supported, "baseten" uses configurable chat_template_args plus reasoning_effort when supported, "zai" uses thinking.type, "qwen" uses top-level enable_thinking, "qwen-chat-template" uses chat_template_kwargs.enable_thinking and preserve_thinking, "chat-template" uses configurable chat_template_kwargs, "string-thinking" uses top-level thinking, and "ant-ling" uses reasoning.effort only when the mapped effort is non-null.'
    )
    chatTemplateKwargs: dict[str, ChatTemplateKwargValueSchema] | None = _optional(
        'Kwargs sent as chat_template_kwargs when thinkingFormat is "chat-template". Use $var with "thinking.enabled", "thinking.effort", or "thinking.budget" for MISAKA-controlled values.'
    )
    chatTemplateArgs: dict[str, ChatTemplateKwargValueSchema] | None = _optional(
        'Arguments sent as chat_template_args when thinkingFormat is "baseten". Use $var with "thinking.enabled", "thinking.effort", or "thinking.budget" for MISAKA-controlled values.'
    )
    openRouterRouting: OpenRouterRoutingSchema | None = None
    vercelGatewayRouting: VercelGatewayRoutingSchema | None = None
    zaiToolStream: bool | None = _optional(
        "Whether z.ai supports top-level tool_stream for streaming tool call deltas.", False
    )
    thinkingTokenBudgetField: _ThinkingTokenBudgetFieldField | None = _optional(
        'Top-level request field used to cap reasoning tokens from thinkingBudgets. Reasoning and the answer share max_tokens on these endpoints. "thinking_token_budget" is vLLM, "thinking_budget" is Qwen, DashScope, or SGLang, and "thinking_budget_tokens" is llama.cpp. Off by default and not set on the generated catalog.'
    )
    supportsThinkingTokenBudget: bool | None = _optional(
        'Alias for thinkingTokenBudgetField: "thinking_token_budget" (vLLM). Prefer thinkingTokenBudgetField.', False
    )
    supportsOpenAIGrammarTools: bool | None = _optionalCompatBoolean(
        "Whether the provider supports OpenAI custom tools with Lark or regex grammar formats. When false, grammar-constrained tools fall back to normal function tools. The generated catalog enables this for capable models.",
        False,
    )
    supportsMidConvoSystemMessages: bool | None = _optionalCompatBoolean(
        "Whether the exact model accepts system or developer messages after the conversation has started. When false, later system messages are folded into the leading system message. The generated catalog enables this for verified models.",
        False,
    )
    supportsMidConvoToolAdditions: bool | None = _optional(
        "Whether system messages can introduce additional tools mid-conversation. Requires supportsMidConvoSystemMessages. The generated catalog enables this for capable models.",
        False,
    )
    supportsStrictMode: bool | None = _optionalCompatBoolean(
        "Whether the provider supports the strict field in tool definitions. Generated capable models enable it explicitly.",
        False,
    )
    cacheControlFormat: Literal["anthropic"] | None = _optional(
        "Cache control convention for prompt caching. Anthropic applies cache_control markers to the system prompt, last tool definition, and last user, assistant, or tool-result text content."
    )
    sendSessionAffinityHeaders: bool | None = _optionalCompatBoolean(
        "Whether to send session-affinity data from options.sessionId. Default: true for OpenRouter endpoints, false otherwise."
    )
    sessionAffinityFormat: _SessionAffinityFormatField | None = _optionalSessionAffinityFormat(
        "Session-affinity header format. openai sends session_id, x-client-request-id, and x-session-affinity; openai-nosession sends x-client-request-id and x-session-affinity; openrouter sends x-session-id. Does not affect prompt_cache_key. Default: auto-detected."
    )
    supportsLongCacheRetention: bool | None = _optionalCompatBoolean(
        'Whether the provider supports long prompt cache retention (prompt_cache_retention: "24h" or Anthropic-style cache_control.ttl: "1h", depending on format). Default: auto-detected from provider and URL.'
    )
    vllmPriority: float | None = _optional(
        "vLLM scheduler priority sent as the top-level priority request field. Lower values are handled earlier and the server default is 0. Only meaningful with --scheduling-policy priority. Off by default and not set on the generated catalog."
    )


class OpenAIResponsesCompatSchema(_CompatSchemaObject):
    """Compatibility settings for OpenAI Responses APIs."""

    supportsDeveloperRole: bool | None = _optionalCompatBoolean(
        "Whether the provider supports the developer role instead of system.", True
    )
    supportsMidConvoSystemMessages: bool | None = _optionalCompatBoolean(
        "Whether the exact model accepts developer or system messages after the conversation has started. When false, later system messages are folded into the leading system message. The generated catalog enables this for verified models.",
        False,
    )
    sessionAffinityFormat: _SessionAffinityFormatField | None = _optionalSessionAffinityFormat(
        "Session-affinity header format. openai sends session_id and x-client-request-id; openai-nosession sends x-client-request-id; openrouter sends x-session-id. Does not affect prompt_cache_key. Default: auto-detected."
    )
    supportsLongCacheRetention: bool | None = _optionalCompatBoolean(
        'Whether the provider supports long prompt cache retention. This uses prompt_cache_options.ttl: "30m" on GPT-5.6+ and prompt_cache_retention: "24h" on earlier models.',
        True,
    )
    supportsStrictMode: bool | None = _optionalCompatBoolean(
        "Whether the provider supports strict JSON-schema function tools. Defaults are API-specific; generated OpenAI models enable it explicitly."
    )
    supportsOpenAIGrammarTools: bool | None = _optionalCompatBoolean(
        "Whether to emit OpenAI custom tools with Lark or regex grammar formats. When false, grammar-constrained tools fall back to normal function tools. The generated catalog enables this for capable models.",
        False,
    )
    supportsAdditionalTools: bool | None = _optional(
        "Whether the model supports message-anchored additional_tools input items.", False
    )
    supportsToolSearch: bool | None = _optional(
        "Whether the model supports client-executed tool search for transcript-anchored additions.", False
    )
    supportsExplicitPromptCacheMode: bool | None = _optional(
        "Whether the model accepts prompt_cache_options. Older OpenAI models reject the parameter.", False
    )
    supportsMidConvoEffort: bool | None = _optional(
        "Whether thinking level changes are sent as configuration_update input items instead of changing reasoning.effort, so a change keeps the prompt cache. Only used by the openai-responses API, and ignored with Sign in with ChatGPT on the openai provider.",
        False,
    )
    supportsMaxOutputTokens: bool | None = _optional(
        "Whether the provider accepts max_output_tokens. Some Codex-protocol gateways reject it.", True
    )


class AnthropicMessagesCompatSchema(_CompatSchemaObject):
    """Compatibility settings for Anthropic Messages-compatible APIs."""

    supportsEagerToolInputStreaming: bool | None = _optional(
        "Whether the provider accepts per-tool eager_input_streaming. When false, the Anthropic provider omits tools[].eager_input_streaming and sends the legacy fine-grained-tool-streaming-2025-05-14 beta header for tool-enabled requests.",
        True,
    )
    supportsLongCacheRetention: bool | None = _optionalCompatBoolean(
        "Whether the provider supports Anthropic long cache retention through cache_control.ttl.", True
    )
    sendSessionAffinityHeaders: bool | None = _optionalCompatBoolean(
        "Whether to send x-session-affinity from options.sessionId when caching is enabled. Required for providers like Fireworks that use session affinity for prompt cache routing; requests to the same replica maximize cache hits. Default: true for OpenRouter endpoints, false otherwise."
    )
    sessionAffinityFormat: Literal["openrouter"] | None = _optional(
        "Session-affinity format. openrouter sends x-session-id; when unset, sends x-session-affinity."
    )
    supportsCacheControlOnTools: bool | None = _optional(
        "Whether the provider supports Anthropic-style cache_control markers on tool definitions. When false, cache_control is omitted from tool parameters. Some Anthropic-compatible providers, such as Fireworks, do not support this field on tools and may reject or ignore it.",
        True,
    )
    supportsTemperature: bool | None = _optional(
        "Whether the model accepts the Anthropic temperature request field. Claude Opus 4.7+ rejects non-default values.",
        True,
    )
    forceAdaptiveThinking: bool | None = _optional(
        "Whether to force adaptive thinking (thinking.type: adaptive plus output_config.effort) regardless of model ID. Built-in models that require adaptive thinking set this in generated metadata. Custom Anthropic-compatible providers can set this to true for any model whose upstream requires the adaptive format. Set false to opt out on overridden built-in models.",
        False,
    )
    allowEmptySignature: bool | None = _optional(
        "Whether to replay empty thinking signatures instead of converting thinking to text.", False
    )
    supportsStrictTools: bool | None = _optional(
        "Whether the provider supports Anthropic strict tool schemas. Generated Anthropic models enable it explicitly.",
        False,
    )
    supportsMidConvoEffort: bool | None = _optional(
        "Whether the exact model transport supports effort-only system messages and thinking binding controls.", False
    )
    supportsMidConvoSystemMessages: bool | None = _optionalCompatBoolean(
        "Whether the exact model accepts system-role messages inside the conversation. When false, later system messages are folded into the top-level system prompt.",
        False,
    )
    supportsMidConvoToolChanges: bool | None = _optional(
        "Whether the exact model accepts mid-conversation tool_addition and tool_removal blocks. Requires supportsMidConvoSystemMessages.",
        False,
    )
    allowedFallbackModels: list[AnthropicAllowedFallbackModelSchema] | None = Field(
        default=None,
        max_length=3,
        description="Models Anthropic accepts for server-side refusal fallback, with local pricing metadata for returned fallback responses. When absent or empty, callers must omit fallbacks; Anthropic rejects the field for models with no permitted fallback targets.",
    )


class BedrockCompatSchema(_CompatSchemaObject):
    """Compatibility settings for Amazon Bedrock models."""

    supportsStrictMode: bool | None = _optionalCompatBoolean(
        "Whether the model supports Bedrock strict tool schemas.", False
    )


class MistralConversationsCompatSchema(_CompatSchemaObject):
    """Compatibility settings for the Mistral chat API."""

    supportsMidConvoSystemMessages: bool | None = _optionalCompatBoolean(
        "Whether the exact model accepts system messages after the conversation has started. When false, later system messages are folded into the leading system message.",
        False,
    )


# MISAKA fork: a property is `(annotation, FieldInfo)`, what `create_model` takes.
_Property: TypeAlias = tuple[Any, FieldInfo]

_ProviderCompatPropertyOverrides: dict[str, _Property] = {
    "supportsDeveloperRole": (
        bool | None,
        _optionalCompatBoolean(
            "Whether the provider supports the developer role instead of system. Defaults are API-specific."
        ),
    ),
    "supportsMidConvoSystemMessages": (
        bool | None,
        _optionalCompatBoolean(
            "Whether the exact model accepts system or developer messages after the conversation has started. When false, later system messages are folded into the leading system message.",
            False,
        ),
    ),
    "sessionAffinityFormat": (
        _SessionAffinityFormatField | None,
        _optionalSessionAffinityFormat("Session-affinity header format. Defaults are API-specific or auto-detected."),
    ),
    "supportsMidConvoEffort": (
        bool | None,
        _optionalCompatBoolean(
            "Whether thinking level changes are sent inside the conversation instead of changing the request's effort, so a change keeps the prompt cache. Used by the anthropic-messages and openai-responses APIs.",
            False,
        ),
    ),
    "supportsLongCacheRetention": (
        bool | None,
        _optionalCompatBoolean(
            "Whether the provider supports long prompt cache retention. Defaults are API-specific or auto-detected."
        ),
    ),
    "supportsStrictMode": (
        bool | None,
        _optionalCompatBoolean("Whether the provider supports strict tool schemas. Defaults are API-specific."),
    ),
    "supportsOpenAIGrammarTools": (
        bool | None,
        _optionalCompatBoolean(
            "Whether the provider supports OpenAI custom tools with Lark or regex grammar formats. When false, grammar-constrained tools fall back to normal function tools.",
            False,
        ),
    ),
    "sendSessionAffinityHeaders": (
        bool | None,
        _optionalCompatBoolean(
            "Whether to send session-affinity data from options.sessionId. Defaults are API-specific."
        ),
    ),
}


def _mergeCompatProperties(
    propertyGroups: Sequence[Mapping[str, _Property]], overrides: Mapping[str, _Property]
) -> dict[str, _Property]:
    """Provider-level models.json compatibility is API-agnostic, so it needs one superset. Every
    duplicate property requires explicit generic metadata, while API-specific schemas retain their
    own defaults and descriptions.
    """
    merged: dict[str, _Property] = {}
    duplicateNames: set[str] = set()

    for properties in propertyGroups:
        for name, schema in properties.items():
            if name in merged:
                duplicateNames.add(name)
                if name not in overrides:
                    raise RuntimeError(f"Duplicate compatibility schema property requires an override: {name}")
            merged[name] = schema

    for name in overrides:
        if name not in duplicateNames:
            raise RuntimeError(f"Compatibility schema override does not resolve a duplicate property: {name}")

    return {**merged, **overrides}


def _schemaProperties(schema: type[TypeBoxObject]) -> dict[str, _Property]:
    """MISAKA fork: ``schema.properties``. A copy of each field, so the merged model owns its own."""
    return {name: (field.annotation, copy(field)) for name, field in schema.model_fields.items()}


_CompatSchemasByApi: dict[str, type[_CompatSchemaObject]] = {
    "openai-completions": OpenAICompletionsCompatSchema,
    "openai-responses": OpenAIResponsesCompatSchema,
    "anthropic-messages": AnthropicMessagesCompatSchema,
    "bedrock-converse-stream": BedrockCompatSchema,
    "mistral-conversations": MistralConversationsCompatSchema,
}

_ProviderCompatProperties = _mergeCompatProperties(
    [_schemaProperties(schema) for schema in _CompatSchemasByApi.values()],
    _ProviderCompatPropertyOverrides,
)

ProviderCompatSchema: type[_CompatSchemaObject] = create_model(  # type: ignore[call-overload]
    "ProviderCompatSchema",
    __base__=_CompatSchemaObject,
    __module__=__name__,
    __doc__="Provider and model compatibility overrides.",
    **_ProviderCompatProperties,
)

# `Static<typeof ...>` of the union schemas. The object ones are the runtime records in `misaka.ai.types`.
# A literal, or `{"$var": "thinking.enabled" | "thinking.effort" | "thinking.budget", "omitWhenOff"?: bool}`.
ChatTemplateKwargValue: TypeAlias = str | int | float | bool | None | dict[str, Any]
# Top-level request field used to cap reasoning tokens on OpenAI-compatible servers.
ThinkingTokenBudgetField: TypeAlias = _ThinkingTokenBudgetFieldLiteral
SessionAffinityFormat: TypeAlias = _SessionAffinityFormatLiteral


__all__ = [
    "AnthropicAllowedFallbackModelSchema",
    "AnthropicMessagesCompatSchema",
    "BedrockCompatSchema",
    "ChatTemplateKwargValue",
    "ChatTemplateKwargValueSchema",
    "MistralConversationsCompatSchema",
    "OpenAICompletionsCompatSchema",
    "OpenAIResponsesCompatSchema",
    "OpenRouterRoutingSchema",
    "ProviderCompatSchema",
    "SessionAffinityFormat",
    "SessionAffinityFormatSchema",
    "ThinkingTokenBudgetField",
    "ThinkingTokenBudgetFieldSchema",
    "VercelGatewayRoutingSchema",
]
