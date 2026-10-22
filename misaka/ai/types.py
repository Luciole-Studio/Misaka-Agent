# ruff: noqa: UP040 - this module spells its aliases with the explicit TypeAlias form.

"""Translated from pi's ``packages/ai/src/types.ts`` (v1.3.0).

MISAKA fork: the interfaces are pydantic models (records are validated where they enter: session files, catalogs, the
LLM wire), field names stay camelCase, and the order follows upstream. Python-only pieces are marked where they stand:
the pydantic bases, discriminated-union aliases, the runtime ``ModelTypeMap`` table, the event classes and the
validation entry points at the end. TypeScript-only type-level helpers (``ApiOptionsMap``, ``ApiStreamOptions``,
``IsJsonCompatible`` and friends, ``JsonRepresentation``, the transcript brand) have no counterpart.

Upstream moved the compat and model-metadata types to ``providers/compat-schema.ts`` and ``providers/model-schema.ts``
(``Static<>`` of the schemas there) and re-exports them from here. MISAKA fork: the type aliases come from
``misaka.ai.providers.{compat_schema,model_schema}`` the same way; the object types (``OpenAICompletionsCompat``,
``ModelCost``, ``OpenRouterRouting``, ...) stay records defined below, because those modules hold the configuration
schemas, which validate with TypeBox's rules, and the records validate catalog and session data with this module's.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, TypeAlias

# MISAKA fork: pi names the fields every catalog entry shares `BaseModel`; pydantic's own `BaseModel` is imported under
# another name so that one keeps pi's.
from pydantic import BaseModel as PydanticBaseModel
from pydantic import (
    ConfigDict,
    Discriminator,
    Field,
    SerializeAsAny,
    Tag,
    TypeAdapter,
    ValidationInfo,
    field_validator,
)

from misaka.ai.providers.compat_schema import (
    ChatTemplateKwargValue,
    SessionAffinityFormat,
    ThinkingTokenBudgetField,
)
from misaka.ai.providers.model_schema import (
    CacheRetention,
    ModelInputModality,
    ModelPromptCache,
    ModelThinkingLevel,
    ThinkingLevel,
    ThinkingLevelMap,
)
from misaka.ai.utils.diagnostics import AssistantMessageDiagnostic

KnownApi: TypeAlias = Literal[
    "openai-completions",
    "mistral-conversations",
    "openai-responses",
    "azure-openai-responses",
    "openai-codex-responses",
    "anthropic-messages",
    "bedrock-converse-stream",
    "google-generative-ai",
    "google-vertex",
    "pi-messages",
]

Api: TypeAlias = str

KnownImageApi: TypeAlias = Literal["openrouter-images"]

ImageApi: TypeAlias = str

KnownClassifierApi: TypeAlias = Literal[
    "typesafe-system-one",
    "cloudflare-workers-ai-system-one",
    "llama-cpp-classify",
    "openai-decisions",
]

ClassifierApi: TypeAlias = str

KnownProvider: TypeAlias = Literal[
    "amazon-bedrock",
    "ant-ling",
    "anthropic",
    "google",
    "google-vertex",
    "openai",
    "azure",
    "openai-codex",
    "radius",
    "typesafe",
    "nvidia",
    "deepseek",
    "github-copilot",
    "xai",
    "groq",
    "cerebras",
    "openrouter",
    "vercel-ai-gateway",
    "zai",
    "zai-coding-cn",
    "mistral",
    "minimax",
    "minimax-cn",
    "moonshotai",
    "moonshotai-cn",
    "huggingface",
    "fireworks",
    "together",
    "baseten",
    "opencode",
    "opencode-go",
    "kimi-coding",
    "meta",
    "cloudflare-workers-ai",
    "cloudflare-ai-gateway",
    "qwen-token-plan",
    "qwen-token-plan-cn",
    "qwen-token-plan-individual",
    "xiaomi",
    "xiaomi-token-plan-cn",
    "xiaomi-token-plan-ams",
    "xiaomi-token-plan-sgp",
]
ProviderId: TypeAlias = str

ToolChoice: TypeAlias = Literal["auto", "none"]
SamplingParams: TypeAlias = dict[str, Any]
SamplingParamsByThinkingLevel: TypeAlias = dict[ModelThinkingLevel, SamplingParams]

class SchemaModel(PydanticBaseModel):
    """MISAKA fork: the pydantic base of the record interfaces; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class RuntimeModel(SchemaModel):
    """MISAKA fork: the pydantic base of option objects, which carry callables and signals."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)


class _CompatObject(SchemaModel):
    """MISAKA fork: a compat sub-object. Adapters send these to the service as written (openai-completions forwards the
    routing object whole as ``provider``), and upstream types them as plain interfaces, so keys this version does not
    declare are kept, not rejected."""

    model_config = ConfigDict(extra="allow")


class ThinkingBudgets(SchemaModel):
    """Token budgets for each thinking level (token-based providers only)"""

    # MISAKA fork: a key besides these four (a settings file carrying "xhigh" or "off") is simply not read, as pi spreads
    # the object and never looks it up; forbidding it failed every request.
    model_config = ConfigDict(extra="ignore")

    minimal: int | None = None
    low: int | None = None
    medium: int | None = None
    high: int | None = None


# Base options all providers share
Transport: TypeAlias = Literal["sse", "websocket", "websocket-cached", "auto"]

# Provider-scoped environment overrides. Values take precedence over process.env.
ProviderEnv: TypeAlias = dict[str, str]
ProviderHeaders: TypeAlias = dict[str, str | None]
# MISAKA fork: no `FetchFunction`; the `fetch` option is not ported, requests go through httpx.


class ProviderResponse(SchemaModel):
    status: int
    headers: dict[str, str]


class ProviderRequestOptions(RuntimeModel):
    """Authentication, HTTP transport, and lifecycle callbacks shared by provider requests.

    MISAKA fork: pi's ``fetch`` option (an injected fetch implementation) is not ported; requests go through httpx.
    """

    signal: Any | None = None
    # Explicit parent context for telemetry produced by this logical request.
    telemetryContext: Any | None = None
    apiKey: str | None = None
    # Provider-scoped environment values. These take precedence over process.env for
    # provider configuration such as regional settings, endpoint placeholders, and
    # proxy variables.
    env: ProviderEnv | None = None
    # Optional callback for inspecting or replacing provider payloads before sending.
    # Return undefined to keep the payload unchanged.
    onPayload: Any | None = None
    # Optional callback invoked after an HTTP response is received.
    onResponse: Any | None = None
    # Optional custom HTTP headers to include in API requests.
    # Merged with provider defaults; caller values override default headers.
    # On AWS Bedrock these are injected via a Smithy `build`-step middleware so
    # they are covered by SigV4 signing; reserved headers (`x-amz-*`,
    # `authorization`, `host`) are silently ignored to preserve SigV4 / bearer auth.
    # A null value suppresses a provider/API default header with the same name.
    headers: ProviderHeaders | None = None
    # HTTP request timeout in milliseconds for providers/SDKs that support it.
    # For example, OpenAI and Anthropic SDK clients default to 10 minutes.
    timeoutMs: int | None = None
    # Maximum retry attempts for providers/SDKs that support client-side retries.
    # For example, OpenAI and Anthropic SDK clients default to 2.
    maxRetries: int | None = None
    # Maximum delay in milliseconds to wait for a retry when the server requests a long wait.
    # If the server's requested delay exceeds this value, the request fails immediately
    # with an error containing the requested delay, allowing higher-level retry logic
    # to handle it with user visibility.
    # Default: 60000 (60 seconds). Set to 0 to disable the cap.
    maxRetryDelayMs: int | None = None


class StreamOptions(ProviderRequestOptions):
    # `onResponse` (inherited): invoked after an HTTP response is received and before its body stream is consumed.
    # Optional observer for each parsed provider stream event before Pi normalization.
    # Event data is adapter-owned and must be treated as read-only.
    # Adapter support is explicit; unsupported adapters do not invoke it.
    onProviderStreamEvent: Any | None = None
    temperature: float | None = None
    # Arbitrary sampling parameters merged into the request body as-is, after the named request
    # fields, so keys here override them. Lets custom OpenAI-compatible servers (llama.cpp, vLLM,
    # SGLang, ...) receive parameters pi does not model, e.g. `top_p`, `top_k`, `min_p`,
    # `repetition_penalty`. Merged over `Model.samplingParams` per key. Only applied by
    # OpenAI-compatible adapters (completions, responses, Azure responses); other APIs ignore it.
    samplingParams: SamplingParams | None = None
    maxTokens: int | None = None
    # Preferred transport for providers that support multiple transports.
    # Providers that do not support this option ignore it.
    transport: Transport | None = None
    # Prompt cache retention preference. Providers map this to their supported values.
    # Default: "short".
    cacheRetention: CacheRetention | None = None
    # Optional session identifier for providers that support session-based caching.
    # Providers can use this to enable prompt caching, request routing, or other
    # session-aware features. Ignored by providers that don't support it.
    sessionId: str | None = None
    # WebSocket connect timeout in milliseconds for providers that support
    # WebSocket transports. This covers the connection/open handshake only;
    # stream idleness after connection uses timeoutMs.
    websocketConnectTimeoutMs: int | None = None
    # Optional metadata to include in API requests.
    # Providers extract the fields they understand and ignore the rest.
    # For example, Anthropic uses `user_id` for abuse tracking and rate limiting.
    metadata: dict[str, Any] | None = None


# `ProviderStreamOptions` (upstream `StreamOptions & Record<string, unknown>`) follows `SimpleStreamOptions` below.


class DeferredFetchOptions(ProviderRequestOptions):
    # Maximum provider long-poll duration in milliseconds.
    # Defaults to 0, which performs one status check.
    wait: int | None = None


# Request options for best-effort deferred-response cancellation.
DeferredCancelOptions: TypeAlias = ProviderRequestOptions

# `ApiOptionsMap` and `ApiStreamOptions` are TypeScript type-level maps; the options stay untyped here.


class ProviderStreams(Protocol):
    """The uniform stream contract of an API implementation module: every module under ``src/api/`` exports
    ``stream`` and ``streamSimple``; capable modules may also export deferred-response methods (``fetchDeferred``,
    ``cancelDeferred``). Lazy wrappers (``lazyApi()``) and provider factories pass these around as values. This is the
    untyped dispatch shape; per-API option typing lives on the implementation modules themselves."""

    def stream(
        self, model: Model, context: TranscriptContext, options: StreamOptions | None = None
    ) -> AssistantMessageEventStream: ...

    def streamSimple(
        self, model: Model, context: TranscriptContext, options: SimpleStreamOptions | None = None
    ) -> AssistantMessageEventStream: ...


class ProviderImages(Protocol):
    """The uniform contract of an image-generation API implementation module: every image API module under
    ``src/api/`` exports exactly ``generateImages``, so the module itself satisfies this interface. Lazy wrappers and
    ``createProvider({ images })`` pass these around as values."""

    async def generateImages(
        self, model: ImageModel, context: ImagesContext, options: ImagesOptions | None = None
    ) -> AssistantImages: ...


class ProviderClassifier(Protocol):
    """The uniform contract implemented by classifier API modules."""

    async def classify(
        self, model: ClassifierModel, context: ClassifierContext, options: ClassifierOptions | None = None
    ) -> ClassifierResult: ...


class ClassifierOptions(ProviderRequestOptions):
    # Divides the answer logits by this value before they are normalized into probabilities.
    # Values above 1 soften the distribution; values below 1 sharpen it. Must be positive.
    # APIs that cannot apply it ignore it.
    temperature: float | None = None


class ProviderClassifierOptions(ClassifierOptions):
    """MISAKA fork: pi's classifier options are an open object; the request options a runtime hands an implementation
    carry the caller's other keys as well."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class ImagesOptions(ProviderRequestOptions):
    # Optional metadata to include in API requests.
    # Providers extract the fields they understand and ignore the rest.
    metadata: dict[str, Any] | None = None


class ProviderImagesOptions(ImagesOptions):
    """``ImagesOptions & Record<string, unknown>``."""

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


class AnthropicAllowedFallbackModel(_CompatObject):
    """An Anthropic server-side refusal fallback model with local pricing metadata."""

    provider: ProviderId
    model: str
    cost: ModelCost


class SimpleStreamOptions(StreamOptions):
    """Unified options with reasoning passed to streamSimple() and completeSimple()"""

    # Provider-neutral tool selection for simple requests. When omitted, adapters use provider-specific behavior.
    toolChoice: ToolChoice | None = None
    reasoning: ThinkingLevel | None = None
    # Ask a capable provider to return a durable handle and continue the request asynchronously.
    # `True`, or `{"window"?: "15m" | "1h" | "24h"}`.
    deferred: bool | dict[str, Any] | None = None
    # Custom token budgets for thinking levels (token-based providers only)
    thinkingBudgets: ThinkingBudgets | None = None


class ProviderStreamOptions(SimpleStreamOptions):
    """Upstream ``StreamOptions & Record<string, unknown>``: the options object the model runtime hands a provider
    implementation.

    MISAKA fork: extends ``SimpleStreamOptions`` rather than ``StreamOptions`` because that is what the providers
    dereference: ``streamSimple`` reads ``options.reasoning`` and ``options.thinkingBudgets`` with plain attribute
    access, and upstream's merged type is ``SimpleStreamOptions & ModelsRequestTransforms`` (``models.ts:118``). With
    the narrower base, a ``streamSimple`` call that passed no options at all died with ``AttributeError: reasoning``.
    ``extra="allow"`` carries transform hooks and other provider-owned fields through to whichever seam reads them.
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)


# Generic StreamFunction with typed options.
#
# Contract:
# - Receives a normalized transcript: the system prompt and tools live in the
#   leading system message, never on the context itself.
# - Must return an AssistantMessageEventStream.
# - Direct streamSimple() calls may throw synchronously when request auth is
#   missing. Once a stream is returned, request/model/runtime failures should
#   be encoded in that stream.
# - Error termination must produce an AssistantMessage with stopReason
#   "error" or "aborted" and errorMessage, emitted via the stream protocol.
StreamFunction: TypeAlias = Callable[["Model", "TranscriptContext", Any], "AssistantMessageEventStream"]

ImagesFunction: TypeAlias = Callable[["ImageModel", "ImagesContext", Any], Awaitable["AssistantImages"]]

ClassifierFunction: TypeAlias = Callable[["ClassifierModel", "ClassifierContext", Any], Awaitable["ClassifierResult"]]


class TextSignatureV1(SchemaModel):
    v: Literal[1]
    id: str
    phase: Literal["commentary", "final_answer"] | None = None


class TextContent(SchemaModel):
    type: Literal["text"] = "text"
    text: str
    textSignature: str | None = None  # e.g., for OpenAI responses, message metadata (legacy id string or TextSignatureV1 JSON)


class ThinkingContent(SchemaModel):
    type: Literal["thinking"] = "thinking"
    thinking: str
    thinkingSignature: str | None = None  # Provider-specific opaque or serialized reasoning replay data
    # When true, the thinking content was redacted by safety filters. The opaque
    # encrypted payload is stored in `thinkingSignature` so it can be passed back
    # to the API for multi-turn continuity.
    redacted: bool | None = None


class ImageContent(SchemaModel):
    type: Literal["image"] = "image"
    data: str  # base64 encoded image data
    mimeType: str  # e.g., "image/jpeg", "image/png"


class ToolCall(SchemaModel):
    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, Any]
    # Set when the model's complete arguments text was not a valid JSON object. `arguments` is then `{}`.
    # Describes the problem and includes the text the model sent; tool argument validation fails with it.
    # (Upstream declares it last; it stays where MISAKA has stored it since before, so dumps keep their key order.)
    argumentsError: str | None = None
    thoughtSignature: str | None = None  # Google-specific: opaque signature for reusing thought context
    # OpenAI Responses namespace for calls to dynamically loaded or namespaced tools.
    namespace: str | None = None


# MISAKA fork: discriminated unions of the content blocks, as pydantic reads them.
UserContentValue: TypeAlias = TextContent | ImageContent
UserContent: TypeAlias = Annotated[UserContentValue, Field(discriminator="type")]
AssistantContentValue: TypeAlias = TextContent | ThinkingContent | ToolCall
AssistantContent: TypeAlias = Annotated[AssistantContentValue, Field(discriminator="type")]


class UsageCost(SchemaModel):
    """MISAKA fork: ``Usage.cost``, an inline object upstream."""

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
    # Subset of `cacheWrite` written with 1h retention. Only Anthropic reports this split.
    cacheWrite1h: int | None = None
    # Reasoning/thinking tokens, when the provider reports them. This is a subset of
    # `output`: `output` already includes these tokens. Set to a number (possibly 0) by
    # providers that expose a reasoning breakdown; left undefined by providers that don't.
    reasoning: int | None = None
    totalTokens: int
    cost: UsageCost


StopReason: TypeAlias = Literal["pending", "stop", "length", "toolUse", "error", "aborted", "deferred"]

JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


class DeferredHandle(SchemaModel):
    provider: str
    modelId: str
    api: str
    # Provider token, such as a response id or batch id plus row id.
    id: str
    expiresAt: int | None = None
    pollAfterMs: int | None = None
    # Provider conversion data required to reconstruct the final assistant message.
    data: Any | None = None


class SystemMessage(SchemaModel):
    """System instructions and tool declarations at one point in the transcript.

    The leading system message is the system prompt. Later system messages change it:
    `content` adds instructions from that point on, `sections` replace or remove named
    prompt sections, and `toolsAdded`/`toolsRemoved` change the tool set. Replaying
    every system message in order yields the current prompt and tools. Providers that
    accept system messages mid-conversation send each one in place; other providers
    rebuild the leading system message from the replayed state.
    """

    role: Literal["system"] = "system"
    # Instruction text. On the leading message this is the base prompt; later, additional instructions.
    content: str | list[TextContent]
    # Named, ordered prompt sections rendered verbatim after `content`. The leading message
    # declares them; later messages replace sections by name, and `null` removes one. Keep
    # each section self-delimiting (a tag, a heading) so the model can relate an update to
    # the original. Avoid integer-like names; JSON objects reorder those.
    sections: dict[str, str | None] | None = None
    # Complete definitions of tools that become available at this point.
    toolsAdded: list[Tool] | None = None
    # Tools that stop being available at this point.
    toolsRemoved: list[ToolReference] | None = None
    timestamp: int  # Unix timestamp in milliseconds


def _content_never_null(value: Any) -> Any:
    """MISAKA fork: a stored ``content: null`` reads back as an empty list.

    Upstream normalizes the same shape at the top of ``transformMessages``, for "untyped callers (custom tools,
    hand-built histories, old session files)". Those callers exist here too, but they cannot reach that function:
    pydantic rejects the null first, so a session row containing one fails to load at all rather than arriving empty.
    Doing it during validation puts the normalization where this port needs it.
    """
    return [] if value is None else value


class UserMessage(SchemaModel):
    role: Literal["user"] = "user"
    content: str | list[UserContent]
    timestamp: int  # Unix timestamp in milliseconds

    _normalize_null_content = field_validator("content", mode="before")(_content_never_null)


class AssistantMessage(SchemaModel):
    role: Literal["assistant"] = "assistant"
    content: list[AssistantContent]
    api: Api
    provider: ProviderId
    model: str
    responseModel: str | None = None  # Concrete model reported by the provider when different from the requested `model`
    responseId: str | None = None  # Provider-specific response/message identifier when the upstream API exposes one
    # Exact provider-native effort level used for this response. Absent for legacy or unmanaged responses.
    providerThinkingLevel: str | None = None
    # Pi thinking level the agent loop requested for this response. Absent outside the agent loop and for legacy responses.
    thinkingLevel: ModelThinkingLevel | None = None
    diagnostics: list[AssistantMessageDiagnostic] | None = None  # Redacted provider/runtime diagnostics for failures and recoveries.
    usage: Usage
    stopReason: StopReason
    deferred: DeferredHandle | None = None
    errorMessage: str | None = None
    rawStopReason: str | None = None
    # Provider indication of whether the model explicitly ended its turn.
    # Preserved for debugging and does not currently affect agent control flow.
    endTurn: bool | None = None
    # Unix timestamp in milliseconds when the request started.
    timestamp: int
    # Milliseconds from `timestamp` until the response ended, measured with a monotonic clock. Set by
    # `AssistantMessageEventStream` on the final message of a response it saw start; absent for legacy messages and
    # for deferred results fetched later.
    durationMs: int | None = None

    _normalize_null_content = field_validator("content", mode="before")(_content_never_null)


class NestedToolCallRecord(SchemaModel):
    """A tool call that another tool made while it ran, for example from a codemode script."""

    id: str
    name: str
    # Omitted when over the size limits; `argumentsBytes` then gives their size.
    # MISAKA fork: `Any`, not a JSON object: upstream never checks, `ctx.executeTool` takes any value, and a call with
    # arguments that are not an object is still recorded (it fails in the tool pipeline).
    arguments: Any | None = None
    # UTF-8 size of the arguments as JSON, set when `arguments` is omitted.
    argumentsBytes: int | None = None
    # `unfinished`: the call was still running when the calling tool finished.
    status: Literal["ok", "error", "unfinished"]
    durationMs: int | None = None
    # Error text, truncated.
    error: str | None = None


class NestedToolCalls(SchemaModel):
    """Bounded record of the nested calls a tool made. Results are not recorded."""

    calls: list[NestedToolCallRecord]
    # False when calls were dropped, arguments omitted, or calls had not finished.
    complete: bool


class ToolResultMessage(SchemaModel):
    role: Literal["toolResult"] = "toolResult"
    toolCallId: str
    toolName: str
    content: list[UserContent]  # Supports text and images
    details: Any | None = None
    # Usage from the tool execution itself, if available. Not part of main LLM context accounting.
    usage: Usage | None = None
    # Calls this tool made to other tools. Kept for the session record; not sent to the model.
    nestedCalls: NestedToolCalls | None = None
    isError: bool
    # Unix timestamp in milliseconds when the result was created.
    timestamp: int
    # Milliseconds the tool's execution took, measured with a monotonic clock. Absent for legacy results.
    durationMs: int | None = None

    _normalize_null_content = field_validator("content", mode="before")(_content_never_null)


# MISAKA fork: `MessageValue` is the plain union; `Message` is the discriminated one pydantic validates.
MessageValue: TypeAlias = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage
Message: TypeAlias = Annotated[MessageValue, Field(discriminator="role")]

ImagesInputContent: TypeAlias = UserContent
ImagesOutputContentValue: TypeAlias = TextContent | ImageContent
ImagesOutputContent: TypeAlias = Annotated[ImagesOutputContentValue, Field(discriminator="type")]


class ImagesContext(SchemaModel):
    input: list[ImagesInputContent]


ImagesStopReason: TypeAlias = Literal["stop", "error", "aborted"]


class AssistantImages(SchemaModel):
    api: ImageApi
    provider: ProviderId
    model: str
    output: list[ImagesOutputContent]
    responseId: str | None = None
    usage: Usage | None = None
    stopReason: ImagesStopReason
    errorMessage: str | None = None
    timestamp: int  # Unix timestamp in milliseconds


class ClassifierChoiceQuestion(SchemaModel):
    type: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, str]


class ClassifierScoreQuestion(SchemaModel):
    type: Literal["score"] = "score"
    instructions: str
    criteria: list[str]


class ClassifierBoolCriteria(SchemaModel):
    """MISAKA fork: pi types ``criteria`` inline as ``{ true: string; false: string }``."""

    true: str
    false: str


class ClassifierBoolQuestion(SchemaModel):
    type: Literal["bool"] = "bool"
    instructions: str
    criteria: ClassifierBoolCriteria


ClassifierQuestionValue: TypeAlias = ClassifierChoiceQuestion | ClassifierScoreQuestion | ClassifierBoolQuestion
ClassifierQuestion: TypeAlias = Annotated[ClassifierQuestionValue, Field(discriminator="type")]


class ClassifierContext(SchemaModel):
    state: dict[str, Any]
    # Images judged together with `state`. Only models whose `input` includes `"image"` accept them;
    # other models return an error result.
    images: list[ImageContent] | None = None
    questions: dict[str, ClassifierQuestion]


class ClassifierChoiceAnswer(SchemaModel):
    type: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, float]
    confidence: float


class ClassifierScoreAnswer(SchemaModel):
    type: Literal["score"] = "score"
    score: float
    confidence: float


class ClassifierBoolAnswer(SchemaModel):
    type: Literal["bool"] = "bool"
    probability: float


ClassifierAnswerValue: TypeAlias = ClassifierChoiceAnswer | ClassifierScoreAnswer | ClassifierBoolAnswer
ClassifierAnswer: TypeAlias = Annotated[ClassifierAnswerValue, Field(discriminator="type")]
ClassifierStopReason: TypeAlias = Literal["stop", "error", "aborted"]


class ClassifierResult(SchemaModel):
    api: ClassifierApi
    provider: ProviderId
    model: str
    answers: dict[str, ClassifierAnswer]
    # Token usage and its cost at the model's catalog price, when the service reports token counts.
    usage: Usage | None = None
    stopReason: ClassifierStopReason
    errorMessage: str | None = None
    timestamp: int  # Unix timestamp in milliseconds


# OpenAI grammar variants for constrained sampling.
GrammarFormat: TypeAlias = Literal["openai_lark", "openai_regex"]

GrammarVariants: TypeAlias = dict[GrammarFormat, str]


class JsonSchemaConstrainedSamplingConfig(SchemaModel):
    """``{ type: "json_schema", strict: "prefer" | "require" }`` (one member of ``ConstrainedSamplingConfig``)."""

    # MISAKA fork: `extra="ignore"`, overriding the record base. TypeScript erases object types, so a config carrying a
    # field this port does not model reaches pi and is simply not read; forbidding it here turned a forward-compatible
    # caller into a ValidationError raised from the middle of request construction.
    model_config = ConfigDict(extra="ignore")

    type: Literal["json_schema"] = "json_schema"
    # MISAKA fork: optional, so a config that omits `strict` validates instead of raising; the resolver compares it
    # against "require".
    strict: str | None = None


class GrammarConstrainedSamplingConfig(SchemaModel):
    """``{ type: "grammar", variants: GrammarVariants }`` (one member of ``ConstrainedSamplingConfig``)."""

    # MISAKA fork: `extra="ignore"`, as above.
    model_config = ConfigDict(extra="ignore")

    type: Literal["grammar"] = "grammar"
    # MISAKA fork: `Any`, not `GrammarVariants`: the resolver re-tests each value at runtime and reads only the two keys
    # it knows, so an unknown key is ignored and a non-string definition falls back to the other variant; typing it
    # would reject both at validation time.
    variants: Any = Field(default_factory=dict)


# Optional provider-side constrained sampling configs for a tool.
#
# The `json_schema` value roughly maps to the concept of `strict` in APIs which is
# implemented as json-schema constrained sampling by APIs. Grammar variants let
# callers provide provider-specific encodings of the same intended language.
ConstrainedSamplingConfig: TypeAlias = JsonSchemaConstrainedSamplingConfig | GrammarConstrainedSamplingConfig


def _is_pydantic_model_type(value: Any) -> bool:
    return isinstance(value, type) and issubclass(value, PydanticBaseModel)


def _is_json_schema_mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


class Tool(RuntimeModel):
    name: str
    description: str
    # MISAKA fork: a JSON schema mapping or a pydantic model class (TypeBox schemas upstream).
    parameters: Any
    # `False` is upstream's explicit opt-out and reads the same as an absent config.
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
        """MISAKA fork: the parameters as a JSON schema, whichever form they were given in."""
        if _is_pydantic_model_type(self.parameters):
            from misaka.ai.utils.typebox_helpers import typeboxSchema

            return typeboxSchema(self.parameters)
        return deepcopy(self.parameters)


class ToolReference(SchemaModel):
    name: str


class Context(SchemaModel):
    """Request input accepted by the public stream entry points (`Models.stream()`,
    `streamSimple()`, ...). `systemPrompt` and `tools` are shorthand for a leading
    system message; `normalizeContext()` folds them into one before the request
    reaches a provider."""

    systemPrompt: str | None = None
    messages: list[Message]
    tools: list[Tool] | None = None


class TranscriptContext(SchemaModel):
    """Normalized request context passed to providers and API implementations. The
    prompt and tool declarations are carried by the transcript's system messages.
    Only `normalizeContext()` produces this type, so a raw `Context` cannot reach
    provider code by accident.

    MISAKA fork: pi brands the type with a unique symbol; here it is a distinct class, which is what the brand buys in
    TypeScript: a `Context` is not a `TranscriptContext`."""

    messages: list[Message]


# MISAKA fork: one class per event shape, so pydantic can validate the discriminated union below.
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
    reason: Literal["stop", "length", "toolUse", "deferred"]
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
# Event protocol for AssistantMessageEventStream.
#
# Successful streams emit `start` before partial updates and terminate with
# `done`. A stream may terminate directly with `error` when request setup fails
# before generation starts; after `start`, failures also terminate with `error`.
# Direct `streamSimple()` calls throw synchronously when request auth is missing.
# Updates and `done` must never appear before `start`.
#
# `partial` is the shared live response-so-far helper, not an event-time
# snapshot. Text and thinking blocks are empty when their `*_start` event is
# emitted and grow only through their corresponding `*_delta` events until the
# authoritative `*_end`. Redacted thinking may be complete at start and emit no
# deltas. Tool-call arguments at `toolcall_start` are provider-specific;
# `toolcall_delta` carries subsequent JSON updates.
AssistantMessageEvent: TypeAlias = Annotated[AssistantMessageEventValue, Field(discriminator="type")]


class OpenAICompletionsCompat(SchemaModel):
    """Compatibility settings for OpenAI-compatible completions APIs.
    Use this to override URL-based auto-detection for custom providers."""

    # Whether the provider supports the `store` field. Default: auto-detected from URL.
    supportsStore: bool | None = None
    # Whether the provider supports the `developer` role (vs `system`). Default: auto-detected from URL.
    supportsDeveloperRole: bool | None = None
    # Whether the provider supports `reasoning_effort`. Default: auto-detected from URL.
    supportsReasoningEffort: bool | None = None
    # Whether the provider supports `stream_options: { include_usage: true }` for token usage in streaming responses. Default: true.
    supportsUsageInStreaming: bool | None = None
    # Whether streamed responses include `finish_reason`. When false, pi infers `stop` or `toolUse` when the stream ends. Default: true.
    supportsFinishReason: bool | None = None
    # Which field to use for max tokens. Default: auto-detected from URL.
    maxTokensField: Literal["max_completion_tokens", "max_tokens"] | None = None
    # Whether tool results require the `name` field. Default: auto-detected from URL.
    requiresToolResultName: bool | None = None
    # Whether a user message after tool results requires an assistant message in between. Default: auto-detected from URL.
    requiresAssistantAfterToolResult: bool | None = None
    # Whether thinking blocks must be converted to text blocks with <thinking> delimiters. Default: auto-detected from URL.
    requiresThinkingAsText: bool | None = None
    # Whether all replayed assistant messages must include an empty reasoning_content field when reasoning is enabled. Default: auto-detected from URL.
    requiresReasoningContentOnAssistantMessages: bool | None = None
    # Format for reasoning/thinking parameter. "openai" uses reasoning_effort, "openrouter" uses reasoning: { effort }, "deepseek" uses thinking: { type } plus reasoning_effort when supported, "together" uses reasoning: { enabled } plus reasoning_effort when supported, "baseten" uses configurable chat_template_args plus reasoning_effort when supported, "zai" uses thinking: { type }, "qwen" uses top-level enable_thinking: boolean, "qwen-chat-template" uses chat_template_kwargs.enable_thinking and preserve_thinking, "chat-template" uses configurable chat_template_kwargs, "string-thinking" uses top-level thinking: string, and "ant-ling" uses reasoning: { effort } only when the mapped effort is non-null. When omitted, pi auto-detects the format from the provider URL.
    thinkingFormat: (
        Literal[
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
        ]
        | None
    ) = None
    # Kwargs to send as `chat_template_kwargs` when `thinkingFormat` is `chat-template`. Use `{ "$var": "thinking.enabled" }`, `{ "$var": "thinking.effort" }`, or `{ "$var": "thinking.budget" }` for pi-controlled thinking values.
    chatTemplateKwargs: dict[str, ChatTemplateKwargValue] | None = None
    # Arguments to send as `chat_template_args` when `thinkingFormat` is `baseten`. Use `{ "$var": "thinking.enabled" }`, `{ "$var": "thinking.effort" }`, or `{ "$var": "thinking.budget" }` for pi-controlled thinking values.
    chatTemplateArgs: dict[str, ChatTemplateKwargValue] | None = None
    # OpenRouter-compatible routing preferences sent as the `provider` request field.
    openRouterRouting: OpenRouterRouting | None = None
    # Vercel AI Gateway routing preferences. Only used when baseUrl points to Vercel AI Gateway.
    vercelGatewayRouting: VercelGatewayRouting | None = None
    # Whether z.ai supports top-level `tool_stream: true` for streaming tool call deltas. Default: false.
    zaiToolStream: bool | None = None
    # Top-level request field used to cap reasoning tokens from `thinkingBudgets`.
    # Reasoning and the answer share `max_tokens` on these endpoints, so without a budget a
    # reasoning-heavy turn can consume the whole response and emit no answer.
    # `"thinking_token_budget"` is vLLM, `"thinking_budget"` is Qwen/DashScope/SGLang,
    # `"thinking_budget_tokens"` is llama.cpp. Off by default; not set on the generated catalog.
    thinkingTokenBudgetField: ThinkingTokenBudgetField | None = None
    # Alias for `thinkingTokenBudgetField: "thinking_token_budget"` (vLLM). Prefer `thinkingTokenBudgetField`. Default: false.
    supportsThinkingTokenBudget: bool | None = None
    # Whether the provider supports OpenAI custom tools with Lark/regex grammar formats. When false, grammar-constrained tools fall back to normal function tools. Default: false; the generated model catalog enables it for capable models.
    supportsOpenAIGrammarTools: bool | None = None
    # Whether the exact model accepts system or developer messages after the conversation has started. When false, later system messages are folded into the leading system message. Default: false; the generated model catalog enables it for verified models.
    supportsMidConvoSystemMessages: bool | None = None
    # Whether system messages can introduce additional tools mid-conversation. Requires `supportsMidConvoSystemMessages`. Default: false; the generated model catalog enables it for capable models.
    supportsMidConvoToolAdditions: bool | None = None
    # Whether the provider supports the `strict` field in tool definitions. Default: false; generated capable models enable it explicitly.
    supportsStrictMode: bool | None = None
    # Cache control convention for prompt caching. "anthropic" applies Anthropic-style `cache_control` markers to the system prompt, last tool definition, and last user, assistant, or tool-result text content.
    cacheControlFormat: Literal["anthropic"] | None = None
    # Whether to send session-affinity data from `options.sessionId`. Default: true for OpenRouter endpoints, false otherwise.
    sendSessionAffinityHeaders: bool | None = None
    # Session-affinity header format: `openai` sends `session_id`, `x-client-request-id`, and `x-session-affinity`; `openai-nosession` sends `x-client-request-id` and `x-session-affinity`; `openrouter` sends `x-session-id`. Does not affect the `prompt_cache_key` body param, which is governed by cache retention. Default: auto-detected.
    sessionAffinityFormat: SessionAffinityFormat | None = None
    # Whether the provider supports long prompt cache retention (`prompt_cache_retention: "24h"` or Anthropic-style `cache_control.ttl: "1h"`, depending on format). Default: auto-detected from provider and URL.
    supportsLongCacheRetention: bool | None = None
    # vLLM scheduler priority sent as the top-level `priority` request field (lower values are
    # handled earlier; server default 0). Only meaningful when vLLM runs with
    # `--scheduling-policy priority`; useful for keeping background/batch work from stalling
    # interactive sessions. Off by default; not set on the generated catalog.
    vllmPriority: int | float | None = None


class OpenAIResponsesCompat(SchemaModel):
    """Compatibility settings for OpenAI Responses APIs."""

    # Whether the provider supports the `developer` role (vs `system`). Default: true.
    supportsDeveloperRole: bool | None = None
    # Whether the exact model accepts developer or system messages after the conversation has started. When false, later system messages are folded into the leading system message. Default: false; the generated model catalog enables it for verified models.
    supportsMidConvoSystemMessages: bool | None = None
    # Session-affinity header format: `openai` sends `session_id` and `x-client-request-id`; `openai-nosession` sends `x-client-request-id`; `openrouter` sends `x-session-id`. Does not affect the `prompt_cache_key` body param, which is governed by cache retention. Default: auto-detected.
    sessionAffinityFormat: SessionAffinityFormat | None = None
    # Whether the provider supports long prompt cache retention. This uses `prompt_cache_options.ttl: "30m"` on GPT-5.6+ and `prompt_cache_retention: "24h"` on earlier models. Default: true.
    supportsLongCacheRetention: bool | None = None
    # Whether the provider supports strict JSON-schema function tools. Defaults are API-specific; generated OpenAI models enable it explicitly.
    supportsStrictMode: bool | None = None
    # Whether to emit OpenAI custom tools with Lark/regex grammar formats. When false, grammar-constrained tools fall back to normal function tools. Default: false; the generated model catalog enables it for capable models.
    supportsOpenAIGrammarTools: bool | None = None
    # Whether the model supports message-anchored `additional_tools` input items. Default: false.
    supportsAdditionalTools: bool | None = None
    # Whether the model supports client-executed tool search for transcript-anchored additions. Default: false.
    supportsToolSearch: bool | None = None
    # Whether the model accepts `prompt_cache_options` (OpenAI GPT-5.6+ prompt caching). Older OpenAI models reject the parameter. Default: false.
    supportsExplicitPromptCacheMode: bool | None = None
    # Whether thinking level changes are sent as `configuration_update` input items instead of changing `reasoning.effort`, so a change keeps the prompt cache. Only used by the openai-responses API, and ignored with Sign in with ChatGPT on the openai provider. Default: false.
    supportsMidConvoEffort: bool | None = None
    # Whether the provider accepts the `max_output_tokens` parameter. Some Codex-protocol gateways reject it. Default: true.
    supportsMaxOutputTokens: bool | None = None


class AnthropicMessagesCompat(SchemaModel):
    """Compatibility settings for Anthropic Messages-compatible APIs."""

    # Whether the provider accepts per-tool `eager_input_streaming`.
    # When false, the Anthropic provider omits `tools[].eager_input_streaming`
    # and sends the legacy `fine-grained-tool-streaming-2025-05-14` beta header
    # for tool-enabled requests.
    # Default: true.
    supportsEagerToolInputStreaming: bool | None = None
    # Whether the provider supports Anthropic long cache retention (`cache_control.ttl: "1h"`). Default: true.
    supportsLongCacheRetention: bool | None = None
    # Whether to send the `x-session-affinity` header from `options.sessionId`
    # when caching is enabled. Required for providers like Fireworks that use
    # session affinity for prompt cache routing (requests to the same replica
    # maximize cache hits).
    # Default: true for OpenRouter endpoints, false otherwise.
    sendSessionAffinityHeaders: bool | None = None
    # Session-affinity format. `"openrouter"` sends `x-session-id`; when unset, sends `x-session-affinity`.
    sessionAffinityFormat: Literal["openrouter"] | None = None
    # Whether the provider supports Anthropic-style `cache_control` markers on
    # tool definitions. When false, `cache_control` is omitted from tool params.
    # Some Anthropic-compatible providers (e.g., Fireworks) do not support this
    # field on tools and may reject or ignore it.
    # Default: true.
    supportsCacheControlOnTools: bool | None = None
    # Whether the model accepts the Anthropic `temperature` request field.
    # Claude Opus 4.7+ rejects non-default temperature values.
    # Default: true.
    supportsTemperature: bool | None = None
    # Whether to force adaptive thinking (`thinking.type: "adaptive"` plus
    # `output_config.effort`) regardless of the model id. Built-in models that
    # require adaptive thinking set this in generated metadata. Custom
    # Anthropic-compatible providers can set this to `true` for any model whose
    # upstream requires the adaptive format. Set to `false` to
    # opt out on overridden built-in models.
    # Default: false.
    forceAdaptiveThinking: bool | None = None
    # Whether to replay empty thinking signatures as `signature: ""` instead of converting thinking to text. Default: false.
    allowEmptySignature: bool | None = None
    # Whether the provider supports Anthropic strict tool schemas. Default: false; generated Anthropic models enable it explicitly.
    supportsStrictTools: bool | None = None
    # Whether the exact model transport supports effort-only system messages and thinking binding controls. Default: false.
    supportsMidConvoEffort: bool | None = None
    # Whether the exact model accepts system-role messages inside the conversation. When false, later system messages are folded into the top-level system prompt. Default: false.
    supportsMidConvoSystemMessages: bool | None = None
    # Whether the exact model accepts mid-conversation `tool_addition` blocks with inline tool definitions (`inline-tools-2026-09-15`) and `tool_removal` blocks. Requires `supportsMidConvoSystemMessages`. Default: false.
    supportsMidConvoToolChanges: bool | None = None
    # Models Anthropic accepts in `fallbacks` for server-side refusal fallback,
    # with local pricing metadata for returned fallback responses. When absent or
    # empty, callers must omit `fallbacks`; Anthropic rejects the field for models
    # with no permitted fallback targets.
    allowedFallbackModels: list[AnthropicAllowedFallbackModel] | None = None


class BedrockCompat(SchemaModel):
    """Compatibility settings for Amazon Bedrock models."""

    # Whether the model supports Bedrock strict tool schemas. Default: false.
    supportsStrictMode: bool | None = None


class MistralConversationsCompat(SchemaModel):
    """Compatibility settings for the Mistral chat API."""

    # Whether the exact model accepts system messages after the conversation has started. When false, later system messages are folded into the leading system message. Default: false.
    supportsMidConvoSystemMessages: bool | None = None


# MISAKA fork: `Model.compat` depends on the API at the type level upstream; here it is the union of the five shapes,
# and `Model` picks the member for its `api` (`_MODEL_COMPAT_BY_API`).
ModelCompat: TypeAlias = (
    OpenAICompletionsCompat | OpenAIResponsesCompat | AnthropicMessagesCompat | BedrockCompat | MistralConversationsCompat
)


def _catalogCompat(schema: type[SchemaModel]) -> type[SchemaModel]:
    """The API's compat shape with undeclared keys kept: TypeScript does not check catalog data at runtime, and pi's
    own catalog carries keys another API declares (an `openai-responses` row with `supportsReasoningEffort`)."""
    return type(schema.__name__, (schema,), {"model_config": ConfigDict(extra="allow"), "__module__": __name__})


_OPENAI_RESPONSES_CATALOG_COMPAT = _catalogCompat(OpenAIResponsesCompat)
_MODEL_COMPAT_BY_API: dict[str, tuple[type[SchemaModel], type[SchemaModel]]] = {
    "openai-completions": (OpenAICompletionsCompat, _catalogCompat(OpenAICompletionsCompat)),
    "openai-responses": (OpenAIResponsesCompat, _OPENAI_RESPONSES_CATALOG_COMPAT),
    "azure-openai-responses": (OpenAIResponsesCompat, _OPENAI_RESPONSES_CATALOG_COMPAT),
    "openai-codex-responses": (OpenAIResponsesCompat, _OPENAI_RESPONSES_CATALOG_COMPAT),
    "anthropic-messages": (AnthropicMessagesCompat, _catalogCompat(AnthropicMessagesCompat)),
    "bedrock-converse-stream": (BedrockCompat, _catalogCompat(BedrockCompat)),
    "mistral-conversations": (MistralConversationsCompat, _catalogCompat(MistralConversationsCompat)),
}


class OpenRouterRoutingSort(_CompatObject):
    """MISAKA fork: ``OpenRouterRouting.sort`` in its object form."""

    # The sorting metric: "price", "throughput", "latency".
    by: str | None = None
    # Partitioning strategy: "model" (default) or "none".
    partition: str | None = None


class OpenRouterRoutingMaxPrice(_CompatObject):
    """MISAKA fork: ``OpenRouterRouting.max_price``."""

    # Price per million prompt tokens.
    prompt: float | str | None = None
    # Price per million completion tokens.
    completion: float | str | None = None
    # Price per image.
    image: float | str | None = None
    # Price per audio unit.
    audio: float | str | None = None
    # Price per request.
    request: float | str | None = None


class OpenRouterRoutingThroughput(_CompatObject):
    """MISAKA fork: ``OpenRouterRouting.preferred_min_throughput`` in its object form."""

    # Minimum tokens/second at the 50th percentile.
    p50: float | None = None
    # Minimum tokens/second at the 75th percentile.
    p75: float | None = None
    # Minimum tokens/second at the 90th percentile.
    p90: float | None = None
    # Minimum tokens/second at the 99th percentile.
    p99: float | None = None


class OpenRouterRoutingLatency(_CompatObject):
    """MISAKA fork: ``OpenRouterRouting.preferred_max_latency`` in its object form."""

    # Maximum latency in seconds at the 50th percentile.
    p50: float | None = None
    # Maximum latency in seconds at the 75th percentile.
    p75: float | None = None
    # Maximum latency in seconds at the 90th percentile.
    p90: float | None = None
    # Maximum latency in seconds at the 99th percentile.
    p99: float | None = None


class OpenRouterRouting(_CompatObject):
    """OpenRouter provider routing preferences.
    Controls which upstream providers OpenRouter routes requests to.
    Sent as the `provider` field in the OpenRouter API request body.
    See https://openrouter.ai/docs/guides/routing/provider-selection"""

    # Whether to allow backup providers to serve requests. Default: true.
    allow_fallbacks: bool | None = None
    # Whether to filter providers to only those that support all parameters in the request. Default: false.
    require_parameters: bool | None = None
    # Data collection setting. "allow" (default): allow providers that may store/train on data. "deny": only use providers that don't collect user data.
    data_collection: Literal["deny", "allow"] | None = None
    # Whether to restrict routing to only ZDR (Zero Data Retention) endpoints.
    zdr: bool | None = None
    # Whether to restrict routing to only models that allow text distillation.
    enforce_distillable_text: bool | None = None
    # An ordered list of provider names/slugs to try in sequence, falling back to the next if unavailable.
    order: list[str] | None = None
    # List of provider names/slugs to exclusively allow for this request.
    only: list[str] | None = None
    # List of provider names/slugs to skip for this request.
    ignore: list[str] | None = None
    # A list of quantization levels to filter providers by (e.g., ["fp16", "bf16", "fp8", "fp6", "int8", "int4", "fp4", "fp32"]).
    quantizations: list[str] | None = None
    # Sorting strategy. Can be a string (e.g., "price", "throughput", "latency") or an object with `by` and `partition`.
    sort: str | OpenRouterRoutingSort | None = None
    # Maximum price per million tokens (USD).
    max_price: OpenRouterRoutingMaxPrice | None = None
    # Preferred minimum throughput (tokens/second). Can be a number (applies to p50) or an object with percentile-specific cutoffs.
    preferred_min_throughput: float | OpenRouterRoutingThroughput | None = None
    # Preferred maximum latency (seconds). Can be a number (applies to p50) or an object with percentile-specific cutoffs.
    preferred_max_latency: float | OpenRouterRoutingLatency | None = None


class VercelGatewayRouting(_CompatObject):
    """Vercel AI Gateway routing preferences.
    Controls which upstream providers the gateway routes requests to.
    See https://vercel.com/docs/ai-gateway/models-and-providers/provider-options"""

    # List of provider slugs to exclusively use for this request (e.g., ["bedrock", "anthropic"]).
    only: list[str] | None = None
    # List of provider slugs to try in order (e.g., ["anthropic", "openai"]).
    order: list[str] | None = None


class ModelCostRates(SchemaModel):
    input: float  # $/million tokens
    output: float  # $/million tokens
    cacheRead: float  # $/million tokens
    cacheWrite: float  # $/million tokens


class ModelCostTier(ModelCostRates):
    # Use this tier for requests whose total input usage exceeds this token count.
    inputTokensAbove: int


class ModelCost(ModelCostRates):
    # Request-wide pricing tiers. The highest matching input threshold applies to the full request.
    tiers: list[ModelCostTier] | None = None


class ModelImageResizeOptions(SchemaModel):
    maxWidth: int | None = None
    maxHeight: int | None = None
    # Maximum base64-encoded payload size in bytes.
    maxBytes: int | None = None
    jpegQuality: int | None = None


class ModelImageInputLimits(SchemaModel):
    # Cache-safe resize profile applied before a new image enters conversation history.
    resize: ModelImageResizeOptions | None = None
    # Maximum images accepted in one provider message.
    maxPerMessage: int | None = None
    # Maximum images accepted across one provider request.
    maxPerRequest: int | None = None


class ModelInputLimits(SchemaModel):
    # Maximum serialized provider request size in bytes.
    maxRequestBytes: int | None = None
    images: ModelImageInputLimits | None = None


# MISAKA fork: the name callers used before upstream named it `ModelInputModality`.
InputModality: TypeAlias = ModelInputModality


class RadiusModelProvider(SchemaModel):
    """MISAKA fork: one backend a Radius catalog model routes to (``providers/data/radius.json``)."""

    id: str
    name: str
    credential: str
    source: str


class BaseModel(SchemaModel):
    """Fields shared by every catalog entry, regardless of what you can do with it."""

    id: str
    name: str
    api: str
    provider: ProviderId
    baseUrl: str
    input: list[ModelInputModality]
    # Provider input limits and cache-safe preprocessing metadata.
    inputLimits: ModelInputLimits | None = None
    cost: ModelCost
    headers: dict[str, str] | None = None


class Model(BaseModel):
    """Chat model: usable with ``stream()`` and friends."""

    api: Api
    # Optional: chat is the default model type, so models without `type` are chat
    # models. Narrow mixed model lists with `isModelType()` instead of comparing
    # `type` directly.
    type: Literal["chat"] | None = None
    reasoning: bool
    # Maps pi thinking levels to provider/model-specific values.
    # Missing keys use provider defaults. null marks a level as unsupported.
    thinkingLevelMap: ThinkingLevelMap | None = None
    # Prompt cache lifetimes per retention tier. Unset when the provider's cache behavior is unknown.
    promptCache: ModelPromptCache | None = None
    contextWindow: int
    maxTokens: int
    # Default sampling parameters for this model. See StreamOptions.samplingParams; per-request keys override these.
    samplingParams: SamplingParams | None = None
    # Sampling parameter overrides selected by the effective pi thinking level.
    samplingParamsByThinkingLevel: SamplingParamsByThinkingLevel | None = None
    # Compatibility overrides for OpenAI-compatible APIs. If not set, auto-detected from baseUrl.
    # MISAKA fork: serialized as the object it is, so the undeclared keys `_catalogCompat` keeps are written back.
    compat: SerializeAsAny[ModelCompat] | None = None
    # MISAKA fork: the offline Radius catalog carries gateway metadata on its entries: the lab that made the model,
    # whether the gateway offers it now, and the backends it routes to. Transcribed as shipped; only the Radius provider
    # reads them.
    lab: str | None = None
    enabled: bool | None = None
    providers: list[RadiusModelProvider] | None = None

    @field_validator("compat", mode="before")
    @classmethod
    def _compat_for_api(cls, value: Any, info: ValidationInfo) -> Any:
        """MISAKA fork: ``Model<TApi>["compat"]`` is the shape for the model's API (keeping keys it does not declare,
        see ``_catalogCompat``). Left to the union, pydantic takes the first member whose fields fit, so an Anthropic
        model's ``{sendSessionAffinityHeaders}`` read as OpenAI compat. An API with no shape of its own keeps the
        union."""
        schemas = _MODEL_COMPAT_BY_API.get(info.data.get("api"))  # type: ignore[arg-type]
        if value is None or schemas is None or isinstance(value, schemas[0]):
            return value
        if isinstance(value, PydanticBaseModel):
            value = value.model_dump(exclude_none=True)
        return schemas[1].model_validate(value)


class ImageModel(BaseModel):
    """Image-generation model: usable with ``generateImages()`` only."""

    api: ImageApi
    type: Literal["image"]
    # Output modalities. Always includes `"image"`; `"text"` means the model can also return text blocks.
    output: list[InputModality]


class ClassifierModel(BaseModel):
    """Structured classifier model: usable with ``classify()`` only."""

    api: ClassifierApi
    type: Literal["classifier"]
    contextWindow: int


# Model shape for each model type.
# MISAKA fork: pi's `ModelTypeMap` is a type-level map; pydantic needs the class of each type to read one, so this is a
# runtime table from type to model class.
ModelTypeMap: dict[str, type[BaseModel]] = {"chat": Model, "image": ImageModel, "classifier": ClassifierModel}

# What a catalog entry is for. Decides which `Models` operation accepts it.
ModelType: TypeAlias = Literal["chat", "image", "classifier"]


def _anyModelType(value: Any) -> str:
    """The type tag of a model or a mapping; models without ``type`` are chat models."""
    type = value.get("type") if isinstance(value, Mapping) else getattr(value, "type", None)
    return "chat" if type is None else type


# Anything a provider can list. Narrow with `isModelType()`.
# MISAKA fork: chat's `type` is optional, so the union is told apart by a callable discriminator rather than a field
# one. A type this version does not know fails validation; callers reading stored or fetched data drop those first, as
# pi's `withKnownModelTypes` does.
AnyModel: TypeAlias = Annotated[
    Annotated[Model, Tag("chat")] | Annotated[ImageModel, Tag("image")] | Annotated[ClassifierModel, Tag("classifier")],
    Discriminator(_anyModelType),
]

# MISAKA fork: runtime validation entry points.
ANY_MODEL_ADAPTER: TypeAdapter[Model | ImageModel | ClassifierModel] = TypeAdapter(AnyModel)
USER_CONTENT_ADAPTER = TypeAdapter(UserContent)
MESSAGE_ADAPTER = TypeAdapter(Message)


def validate_user_content(value: Any) -> UserContentValue:
    return USER_CONTENT_ADAPTER.validate_python(value)


# MISAKA fork: pi dropped ToolResultMessage.addedToolNames and, having no runtime validation, reads older transcripts
# unchanged. The strict model here would refuse whole sessions carrying that key, at every validator entry: session
# load, agent state, the LLM wire, the LCM's snapshot restore. The one retired key is dropped from a copy of the payload,
# as pi effectively ignores it; any other unknown key still fails, so real shape bugs stay visible.
_RETIRED_MESSAGE_KEYS: dict[str, tuple[str, ...]] = {"toolResult": ("addedToolNames",)}


def validate_message(value: Any) -> MessageValue:
    if isinstance(value, dict):
        retired = _RETIRED_MESSAGE_KEYS.get(value.get("role"))
        if retired and any(key in value for key in retired):
            value = {key: item for key, item in value.items() if key not in retired}
    return MESSAGE_ADAPTER.validate_python(value)


# MISAKA fork: upstream re-exports the stream class (`export type`); importing it at runtime would be a cycle
# (utils.event_stream imports this module, and an import of utils.event_stream first would find it half made), so the
# name resolves on first access.
if TYPE_CHECKING:
    from misaka.ai.utils.event_stream import AssistantMessageEventStream


def __getattr__(name: str) -> Any:
    if name == "AssistantMessageEventStream":
        from misaka.ai.utils.event_stream import AssistantMessageEventStream

        return AssistantMessageEventStream
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ANY_MODEL_ADAPTER",
    "AnthropicAllowedFallbackModel",
    "AnthropicMessagesCompat",
    "AnyModel",
    "Api",
    "AssistantImages",
    "AssistantMessage",
    "AssistantMessageEvent",
    "AssistantMessageEventStream",
    "BaseModel",
    "BedrockCompat",
    "CacheRetention",
    "ChatTemplateKwargValue",
    "ClassifierAnswer",
    "ClassifierApi",
    "ClassifierBoolAnswer",
    "ClassifierBoolCriteria",
    "ClassifierBoolQuestion",
    "ClassifierChoiceAnswer",
    "ClassifierChoiceQuestion",
    "ClassifierContext",
    "ClassifierFunction",
    "ClassifierModel",
    "ClassifierOptions",
    "ClassifierQuestion",
    "ClassifierResult",
    "ClassifierScoreAnswer",
    "ClassifierScoreQuestion",
    "ClassifierStopReason",
    "ConstrainedSamplingConfig",
    "Context",
    "DeferredCancelOptions",
    "DeferredFetchOptions",
    "DeferredHandle",
    "GrammarFormat",
    "GrammarVariants",
    "ImageApi",
    "ImageContent",
    "ImageModel",
    "ImagesContext",
    "ImagesFunction",
    "ImagesInputContent",
    "ImagesOptions",
    "ImagesOutputContent",
    "ImagesStopReason",
    "JsonObject",
    "JsonValue",
    "KnownApi",
    "KnownClassifierApi",
    "KnownImageApi",
    "KnownProvider",
    "Message",
    "MistralConversationsCompat",
    "Model",
    "ModelCost",
    "ModelCostRates",
    "ModelCostTier",
    "ModelImageInputLimits",
    "ModelImageResizeOptions",
    "ModelInputLimits",
    "ModelInputModality",
    "ModelPromptCache",
    "ModelThinkingLevel",
    "ModelType",
    "ModelTypeMap",
    "NestedToolCallRecord",
    "NestedToolCalls",
    "OpenAICompletionsCompat",
    "OpenAIResponsesCompat",
    "OpenRouterRouting",
    "ProviderClassifier",
    "ProviderClassifierOptions",
    "ProviderEnv",
    "ProviderHeaders",
    "ProviderId",
    "ProviderImages",
    "ProviderImagesOptions",
    "ProviderRequestOptions",
    "ProviderResponse",
    "ProviderStreamOptions",
    "ProviderStreams",
    "SamplingParams",
    "SamplingParamsByThinkingLevel",
    "SessionAffinityFormat",
    "SimpleStreamOptions",
    "StopReason",
    "StreamFunction",
    "StreamOptions",
    "SystemMessage",
    "TextContent",
    "TextSignatureV1",
    "ThinkingBudgets",
    "ThinkingContent",
    "ThinkingLevel",
    "ThinkingLevelMap",
    "ThinkingTokenBudgetField",
    "Tool",
    "ToolCall",
    "ToolChoice",
    "ToolReference",
    "ToolResultMessage",
    "TranscriptContext",
    "Transport",
    "Usage",
    "UserMessage",
    "VercelGatewayRouting",
]
