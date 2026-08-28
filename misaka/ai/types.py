# ruff: noqa: UP040 - this module spells its aliases with the explicit TypeAlias form.

"""Pydantic schema surface for the harn AI runtime."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from misaka.ai.utils.diagnostics import AssistantMessageDiagnostic

Api: TypeAlias = str

ImagesApi: TypeAlias = str

Provider: TypeAlias = str

ImagesProvider: TypeAlias = str

ThinkingLevel: TypeAlias = Literal["minimal", "low", "medium", "high", "xhigh", "max"]
ModelThinkingLevel: TypeAlias = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
ThinkingLevelMap: TypeAlias = dict[ModelThinkingLevel, str | None]

CacheRetention: TypeAlias = Literal["none", "short", "long"]
Transport: TypeAlias = Literal["sse", "websocket", "websocket-cached", "auto"]
StopReason: TypeAlias = Literal["stop", "length", "toolUse", "error", "aborted"]
ImagesStopReason: TypeAlias = Literal["stop", "error", "aborted"]
InputModality: TypeAlias = Literal["text", "image"]


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeModel(SchemaModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class ThinkingBudgets(SchemaModel):
    minimal: int | None = None
    low: int | None = None
    medium: int | None = None
    high: int | None = None


class ProviderResponse(SchemaModel):
    status: int
    headers: dict[str, str]


class StreamOptions(RuntimeModel):
    temperature: float | None = None
    maxTokens: int | None = None
    signal: Any | None = None
    apiKey: str | None = None
    transport: Transport | None = None
    cacheRetention: CacheRetention | None = None
    sessionId: str | None = None
    onPayload: Any | None = None
    onResponse: Any | None = None
    # A ``None`` value means "do not send this header" -- resolved auth uses it to
    # displace a provider's own credential header (the Cloudflare AI Gateway nulls out
    # ``Authorization`` and ``x-api-key``). Each SDK seam drops the ``None`` entries
    # before handing headers to an HTTP client; typing them away here instead made every
    # request through the gateway fail validation before it went out.
    headers: dict[str, str | None] | None = None
    timeoutMs: int | None = None
    maxRetries: int | None = None
    maxRetryDelayMs: int | None = None
    metadata: dict[str, Any] | None = None
    # Arbitrary OpenAI-compatible sampling params: Model.samplingParams provides
    # defaults, this field overrides per key.
    samplingParams: dict[str, Any] | None = None


class ImagesOptions(RuntimeModel):
    signal: Any | None = None
    apiKey: str | None = None
    onPayload: Any | None = None
    onResponse: Any | None = None
    # A ``None`` value means "do not send this header" -- resolved auth uses it to
    # displace a provider's own credential header (the Cloudflare AI Gateway nulls out
    # ``Authorization`` and ``x-api-key``). Each SDK seam drops the ``None`` entries
    # before handing headers to an HTTP client; typing them away here instead made every
    # request through the gateway fail validation before it went out.
    headers: dict[str, str | None] | None = None
    timeoutMs: int | None = None
    maxRetries: int | None = None
    maxRetryDelayMs: int | None = None
    metadata: dict[str, Any] | None = None


class ProviderImagesOptions(ImagesOptions):
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class DeferredHandle(SchemaModel):
    """A ticket for a response the provider parked, to be collected later.

    The provider identity travels with it because a handle outlives the call that made it:
    a batch fetched tomorrow has to reach the same api on the same provider, and a handle
    read back from disk carries no other context.
    """

    provider: str
    modelId: str
    api: str
    # The provider's own token: a response id, or a batch id plus a row id.
    id: str
    expiresAt: int | None = None
    pollAfterMs: int | None = None
    # Whatever the provider needs to rebuild the final assistant message from its own side.
    data: Any | None = None


class DeferredFetchOptions(StreamOptions):
    # How long the provider may long-poll, in milliseconds. Zero -- the default -- makes
    # one status check and returns whatever is there.
    wait: int | None = None


DeferredCancelOptions: TypeAlias = StreamOptions


class SimpleStreamOptions(StreamOptions):
    reasoning: ThinkingLevel | None = None
    thinkingBudgets: ThinkingBudgets | None = None


class ProviderStreamOptions(SimpleStreamOptions):
    """The options object the model runtime hands a provider implementation.

    Extends ``SimpleStreamOptions`` rather than ``StreamOptions`` because that is what the
    providers dereference: ``stream_simple_*`` reads ``options.reasoning`` and
    ``options.thinkingBudgets`` with plain attribute access, and upstream's merged type is
    ``SimpleStreamOptions & ModelsRequestTransforms`` (``models.ts:84``). With the narrower
    base, a ``streamSimple`` call that passed no options at all died with
    ``AttributeError: reasoning`` on seven of the ten registered APIs.

    ``extra="allow"`` carries the fields no options model declares -- ``env``, transform
    hooks -- through to whichever seam reads them.
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class TextSignatureV1(SchemaModel):
    v: Literal[1]
    id: str
    phase: Literal["commentary", "final_answer"] | None = None


class TextContent(SchemaModel):
    type: Literal["text"] = "text"
    text: str
    textSignature: str | None = None


class ThinkingContent(SchemaModel):
    type: Literal["thinking"] = "thinking"
    thinking: str
    thinkingSignature: str | None = None
    redacted: bool | None = None


class ImageContent(SchemaModel):
    type: Literal["image"] = "image"
    data: str
    mimeType: str


class ToolCall(SchemaModel):
    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, Any]
    thoughtSignature: str | None = None


UserContentValue: TypeAlias = TextContent | ImageContent
UserContent: TypeAlias = Annotated[UserContentValue, Field(discriminator="type")]
AssistantContentValue: TypeAlias = TextContent | ThinkingContent | ToolCall
AssistantContent: TypeAlias = Annotated[AssistantContentValue, Field(discriminator="type")]
ImagesInputContent: TypeAlias = UserContent
ImagesOutputContentValue: TypeAlias = TextContent | ImageContent
ImagesOutputContent: TypeAlias = Annotated[ImagesOutputContentValue, Field(discriminator="type")]


class UsageCost(SchemaModel):
    input: float
    output: float
    cacheRead: float
    cacheWrite: float
    total: float


class Usage(SchemaModel):
    input: int
    output: int
    cacheRead: int
    cacheWrite: int
    # Subset of `cacheWrite` written with 1h retention, priced differently -- see
    # `calculate_cost`. Upstream reads `cache_creation.ephemeral_1h_input_tokens`
    # (pi api/anthropic-messages.ts:606); no misaka adapter reads that field yet, so
    # today nothing ever sets this.
    cacheWrite1h: int | None = None
    # Reasoning tokens, when the provider reports them. A *subset* of `output`, not an
    # addition to it: adding the two would double-count. Optional rather than zero because
    # providers that expose no breakdown leave it unset -- and no misaka adapter fills it
    # in yet either, so it is currently always unset.
    reasoning: int | None = None
    totalTokens: int
    cost: UsageCost


def _content_never_null(value: Any) -> Any:
    """A stored ``content: null`` reads back as an empty list.

    Upstream normalizes the same shape at the top of ``transformMessages``, for "untyped
    callers (custom tools, hand-built histories, old session files)". Those callers exist
    here too, but they cannot reach that function: pydantic rejects the null first, so an
    old session row containing one fails to load at all rather than arriving empty. Doing
    it during validation puts the normalization where this port actually needs it, and
    leaves the rest of the pipeline with the same guarantee upstream gives it.
    """
    return [] if value is None else value


class UserMessage(SchemaModel):
    role: Literal["user"] = "user"
    content: str | list[UserContent]
    timestamp: int

    _normalize_null_content = field_validator("content", mode="before")(_content_never_null)


class AssistantMessage(SchemaModel):
    role: Literal["assistant"] = "assistant"
    content: list[AssistantContent]
    api: Api
    provider: Provider
    model: str
    responseModel: str | None = None
    responseId: str | None = None
    diagnostics: list[AssistantMessageDiagnostic] | None = None
    usage: Usage
    stopReason: StopReason
    errorMessage: str | None = None
    timestamp: int

    _normalize_null_content = field_validator("content", mode="before")(_content_never_null)


class ToolResultMessage(SchemaModel):
    role: Literal["toolResult"] = "toolResult"
    toolCallId: str
    toolName: str
    content: list[UserContent]
    details: Any | None = None
    # Usage from the tool execution itself, if available.  Not part of main LLM context
    # accounting (pi packages/ai/src/types.ts ToolResultMessage.usage).
    usage: Usage | None = None
    # Names from the tool list that became available after this result.  Providers with
    # native deferred tool loading use this as the load point; other providers ignore it
    # (pi ToolResultMessage.addedToolNames).
    addedToolNames: list[str] | None = None
    isError: bool
    timestamp: int

    _normalize_null_content = field_validator("content", mode="before")(_content_never_null)


MessageValue: TypeAlias = UserMessage | AssistantMessage | ToolResultMessage
Message: TypeAlias = Annotated[MessageValue, Field(discriminator="role")]


class ImagesContext(SchemaModel):
    input: list[ImagesInputContent]


class AssistantImages(SchemaModel):
    api: ImagesApi
    provider: ImagesProvider
    model: str
    output: list[ImagesOutputContent]
    responseId: str | None = None
    usage: Usage | None = None
    stopReason: ImagesStopReason
    errorMessage: str | None = None
    timestamp: int


def _is_pydantic_model_type(value: Any) -> bool:
    return isinstance(value, type) and issubclass(value, BaseModel)


def _is_json_schema_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


GrammarFormat: TypeAlias = Literal["openai_lark", "openai_regex"]


class JsonSchemaConstrainedSamplingConfig(SchemaModel):
    """``{ type: "json_schema", strict: "prefer" | "require" }``."""

    # `extra="ignore"`, overriding the repo-wide `extra="forbid"` base. TypeScript erases
    # object types, so a config carrying a field this port does not model reaches pi and is
    # simply not read; forbidding it here turns a forward-compatible caller into a
    # ValidationError raised from the middle of request construction.
    model_config = ConfigDict(extra="ignore")

    type: Literal["json_schema"] = "json_schema"
    # Optional here: a config that omits `strict` validates instead of raising. The value is
    # read by `resolve_json_schema_strict_sampling` below, which compares it against
    # "require" (`config.strict != "require"`, `config.strict == "require"`).
    strict: str | None = None


class GrammarConstrainedSamplingConfig(SchemaModel):
    """``{ type: "grammar", variants: { openai_lark?, openai_regex? } }``.

    Variants are provider-specific encodings of one intended language; the caller supplies
    whichever it has and the resolver picks one the target provider understands.
    """

    # `extra="ignore"`, overriding the repo-wide `extra="forbid"` base. TypeScript erases
    # object types, so a config carrying a field this port does not model reaches pi and is
    # simply not read; forbidding it here turns a forward-compatible caller into a
    # ValidationError raised from the middle of request construction.
    model_config = ConfigDict(extra="ignore")

    type: Literal["grammar"] = "grammar"
    # pi's declared type is `Partial<Record<GrammarFormat, string>>`, but the resolver re-tests
    # each value at runtime with `typeof === "string"` and reads only the two keys it knows
    # (constrained-sampling.ts:243-246): an unknown key is ignored and a non-string definition
    # falls back to the other variant. Declaring `dict[GrammarFormat, str]` would instead reject
    # both at validation time -- and would make the `isinstance(..., str)` guards in
    # :func:`_grammar_variant` vacuously true.
    # `Any`, not `dict`: pi reads `config.variants.openai_lark` / `.openai_regex` with no
    # shape check of its own (constrained-sampling.ts:243-244), so keeping this field
    # unvalidated lets a caller that sent a non-mapping fall through to this module's own
    # "no supported grammar variant" error rather than to a ValidationError raised out of
    # request construction. The reader below already tests each value it pulls.
    variants: Any = Field(default_factory=dict)


ConstrainedSamplingConfig: TypeAlias = JsonSchemaConstrainedSamplingConfig | GrammarConstrainedSamplingConfig


class Tool(RuntimeModel):
    name: str
    description: str
    parameters: Any
    # ``False`` is upstream's explicit opt-out and reads the same as an absent config.
    # Declared here, as upstream declares it: while it was only reachable by attribute
    # lookup, every resolver in ``providers/constrained_sampling.py`` was inert, because
    # ``extra="forbid"`` meant no caller could set it in the first place.
    constrainedSampling: Literal[False] | ConstrainedSamplingConfig | None = None

    @field_validator("parameters")
    @classmethod
    def _validate_parameters(cls, value: Any) -> Any:
        if _is_pydantic_model_type(value):
            return value
        if _is_json_schema_mapping(value):
            return deepcopy(dict(value))
        raise TypeError("Tool.parameters must be a Pydantic model class or JSON schema mapping")

    def parameters_json_schema(self) -> dict[str, Any]:
        if _is_pydantic_model_type(self.parameters):
            return self.parameters.model_json_schema()
        return deepcopy(self.parameters)


class Context(SchemaModel):
    systemPrompt: str | None = None
    messages: list[Message]
    tools: list[Tool] | None = None


class OpenRouterRoutingSort(SchemaModel):
    by: str | None = None
    partition: str | None = None


class OpenRouterRoutingMaxPrice(SchemaModel):
    prompt: float | str | None = None
    completion: float | str | None = None
    image: float | str | None = None
    audio: float | str | None = None
    request: float | str | None = None


class OpenRouterRoutingThroughput(SchemaModel):
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    p99: float | None = None


class OpenRouterRoutingLatency(SchemaModel):
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    p99: float | None = None


class OpenRouterRouting(SchemaModel):
    allow_fallbacks: bool | None = None
    require_parameters: bool | None = None
    data_collection: Literal["deny", "allow"] | None = None
    zdr: bool | None = None
    enforce_distillable_text: bool | None = None
    order: list[str] | None = None
    only: list[str] | None = None
    ignore: list[str] | None = None
    quantizations: list[str] | None = None
    sort: str | OpenRouterRoutingSort | None = None
    max_price: OpenRouterRoutingMaxPrice | None = None
    preferred_min_throughput: float | OpenRouterRoutingThroughput | None = None
    preferred_max_latency: float | OpenRouterRoutingLatency | None = None


class VercelGatewayRouting(SchemaModel):
    only: list[str] | None = None
    order: list[str] | None = None


ThinkingTokenBudgetField: TypeAlias = Literal[
    "thinking_token_budget", "thinking_budget", "thinking_budget_tokens"
]
SessionAffinityFormat: TypeAlias = Literal["openai", "openai-nosession", "openrouter"]


class AnthropicAllowedFallbackModel(SchemaModel):
    """A model Anthropic permits in ``fallbacks``, with local pricing for its responses."""

    provider: Provider
    model: str
    cost: ModelCost


class OpenAICompletionsCompat(SchemaModel):
    supportsStore: bool | None = None
    supportsDeveloperRole: bool | None = None
    supportsReasoningEffort: bool | None = None
    supportsUsageInStreaming: bool | None = None
    maxTokensField: Literal["max_completion_tokens", "max_tokens"] | None = None
    requiresToolResultName: bool | None = None
    requiresAssistantAfterToolResult: bool | None = None
    requiresThinkingAsText: bool | None = None
    requiresReasoningContentOnAssistantMessages: bool | None = None
    # All ten values upstream defines (types.ts:579-590). The four added last -- baseten,
    # chat-template, string-thinking, ant-ling -- were missing, so any catalog entry using
    # one failed `Model.model_validate` outright rather than being sent the way its
    # provider expects.
    thinkingFormat: Literal[
        "openai",
        "openrouter",
        "deepseek",
        "together",
        "baseten",
        "zai",
        "qwen",
        "qwen-chat-template",
        "chat-template",
        "string-thinking",
        "ant-ling",
    ] | None = None
    # Sent as `chat_template_kwargs` when thinkingFormat is `chat-template`, and as
    # `chat_template_args` when it is `baseten`. Values may be literals or a `{"$var": ...}`
    # reference to a thinking value the caller controls.
    chatTemplateKwargs: dict[str, Any] | None = None
    chatTemplateArgs: dict[str, Any] | None = None
    openRouterRouting: OpenRouterRouting | None = None
    vercelGatewayRouting: VercelGatewayRouting | None = None
    zaiToolStream: bool | None = None
    supportsStrictMode: bool | None = None
    cacheControlFormat: Literal["anthropic"] | None = None
    sendSessionAffinityHeaders: bool | None = None
    supportsLongCacheRetention: bool | None = None
    supportsFinishReason: bool | None = None
    thinkingTokenBudgetField: ThinkingTokenBudgetField | None = None
    supportsThinkingTokenBudget: bool | None = None
    supportsOpenAIGrammarTools: bool | None = None
    deferredToolsMode: Literal["kimi"] | None = None
    sessionAffinityFormat: SessionAffinityFormat | None = None


class OpenAIResponsesCompat(SchemaModel):
    supportsLongCacheRetention: bool | None = None
    supportsDeveloperRole: bool | None = None
    sessionAffinityFormat: SessionAffinityFormat | None = None
    supportsStrictMode: bool | None = None
    supportsOpenAIGrammarTools: bool | None = None
    supportsAdditionalTools: bool | None = None
    supportsToolSearch: bool | None = None
    supportsExplicitPromptCacheMode: bool | None = None


class AnthropicMessagesCompat(SchemaModel):
    supportsEagerToolInputStreaming: bool | None = None
    supportsLongCacheRetention: bool | None = None
    sendSessionAffinityHeaders: bool | None = None
    supportsCacheControlOnTools: bool | None = None
    forceAdaptiveThinking: bool | None = None
    supportsTemperature: bool | None = None
    allowEmptySignature: bool | None = None
    supportsStrictTools: bool | None = None
    allowedFallbackModels: list[AnthropicAllowedFallbackModel] | None = None
    supportsToolReferences: bool | None = None


class BedrockCompat(SchemaModel):
    """Compatibility settings for Amazon Bedrock models (pi ``BedrockCompat``)."""

    supportsStrictMode: bool | None = None


ModelCompat: TypeAlias = OpenAICompletionsCompat | OpenAIResponsesCompat | AnthropicMessagesCompat | BedrockCompat


class ModelCostTier(SchemaModel):
    """One pricing band. Rates are $/million tokens, as everywhere else in the catalog."""

    input: float
    output: float
    cacheRead: float
    cacheWrite: float
    # Use this tier for requests whose total input usage exceeds this token count.
    inputTokensAbove: int


class ModelCost(SchemaModel):
    input: float
    output: float
    cacheRead: float
    cacheWrite: float
    # Request-wide pricing bands: the highest matching threshold applies to the *whole*
    # request, not just the tokens above it. No model in the generated catalog carries
    # bands today; they only arrive with user-supplied model definitions.
    tiers: list[ModelCostTier] | None = None


class Model(SchemaModel):
    id: str
    name: str
    api: Api
    provider: Provider
    baseUrl: str
    reasoning: bool
    thinkingLevelMap: ThinkingLevelMap | None = None
    input: list[InputModality]
    cost: ModelCost
    contextWindow: int
    maxTokens: int
    headers: dict[str, str] | None = None
    compat: ModelCompat | None = None
    samplingParams: dict[str, Any] | None = None   # sampling params configurable in models.json


class ImagesModel(SchemaModel):
    id: str
    name: str
    api: ImagesApi
    provider: ImagesProvider
    baseUrl: str
    input: list[InputModality]
    cost: ModelCost
    headers: dict[str, str] | None = None
    output: list[InputModality]


class StartEvent(SchemaModel):
    type: Literal["start"] = "start"
    partial: AssistantMessage


class TextStartEvent(SchemaModel):
    type: Literal["text_start"] = "text_start"
    contentIndex: int
    partial: AssistantMessage


class TextDeltaEvent(SchemaModel):
    type: Literal["text_delta"] = "text_delta"
    contentIndex: int
    delta: str
    partial: AssistantMessage


class TextEndEvent(SchemaModel):
    type: Literal["text_end"] = "text_end"
    contentIndex: int
    content: str
    partial: AssistantMessage


class ThinkingStartEvent(SchemaModel):
    type: Literal["thinking_start"] = "thinking_start"
    contentIndex: int
    partial: AssistantMessage


class ThinkingDeltaEvent(SchemaModel):
    type: Literal["thinking_delta"] = "thinking_delta"
    contentIndex: int
    delta: str
    partial: AssistantMessage


class ThinkingEndEvent(SchemaModel):
    type: Literal["thinking_end"] = "thinking_end"
    contentIndex: int
    content: str
    partial: AssistantMessage


class ToolCallStartEvent(SchemaModel):
    type: Literal["toolcall_start"] = "toolcall_start"
    contentIndex: int
    partial: AssistantMessage


class ToolCallDeltaEvent(SchemaModel):
    type: Literal["toolcall_delta"] = "toolcall_delta"
    contentIndex: int
    delta: str
    partial: AssistantMessage


class ToolCallEndEvent(SchemaModel):
    type: Literal["toolcall_end"] = "toolcall_end"
    contentIndex: int
    toolCall: ToolCall
    partial: AssistantMessage


class DoneEvent(SchemaModel):
    type: Literal["done"] = "done"
    reason: Literal["stop", "length", "toolUse"]
    message: AssistantMessage


class ErrorEvent(SchemaModel):
    type: Literal["error"] = "error"
    reason: Literal["aborted", "error"]
    error: AssistantMessage


AssistantMessageEventValue: TypeAlias = (
    StartEvent
    | TextStartEvent
    | TextDeltaEvent
    | TextEndEvent
    | ThinkingStartEvent
    | ThinkingDeltaEvent
    | ThinkingEndEvent
    | ToolCallStartEvent
    | ToolCallDeltaEvent
    | ToolCallEndEvent
    | DoneEvent
    | ErrorEvent
)
AssistantMessageEvent: TypeAlias = Annotated[AssistantMessageEventValue, Field(discriminator="type")]

USER_CONTENT_ADAPTER = TypeAdapter(UserContent)
MESSAGE_ADAPTER = TypeAdapter(Message)
def validate_user_content(value: Any) -> UserContentValue:
    return USER_CONTENT_ADAPTER.validate_python(value)


def validate_message(value: Any) -> MessageValue:
    return MESSAGE_ADAPTER.validate_python(value)


from misaka.ai.utils.event_stream import AssistantMessageEventStream

__all__ = [
    "AnthropicMessagesCompat",
    "Api",
    "AssistantImages",
    "AssistantMessage",
    "AssistantMessageEvent",
    "AssistantMessageEventStream",
    "CacheRetention",
    "Context",
    "DeferredCancelOptions",
    "DeferredFetchOptions",
    "DeferredHandle",
    "ImageContent",
    "ImagesApi",
    "ImagesContext",
    "ImagesInputContent",
    "ImagesModel",
    "ImagesOptions",
    "ImagesOutputContent",
    "ImagesProvider",
    "ImagesStopReason",
    "Message",
    "Model",
    "ModelThinkingLevel",
    "OpenAICompletionsCompat",
    "OpenAIResponsesCompat",
    "OpenRouterRouting",
    "Provider",
    "ProviderImagesOptions",
    "ProviderResponse",
    "ProviderStreamOptions",
    "SimpleStreamOptions",
    "StopReason",
    "StreamOptions",
    "TextContent",
    "TextSignatureV1",
    "ThinkingBudgets",
    "ThinkingContent",
    "ThinkingLevel",
    "ThinkingLevelMap",
    "Tool",
    "ToolCall",
    "ToolResultMessage",
    "Transport",
    "Usage",
    "UserMessage",
    "VercelGatewayRouting",
]
