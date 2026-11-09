"""Translated from pi's ``packages/agent/src/types.ts`` (v1.1.0).

MISAKA fork: interfaces are dataclasses (the loop also accepts mappings where upstream takes plain objects), and the
event union is a set of slotted dataclasses.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from misaka.ai.types import (
    AssistantMessage,
    AssistantMessageEventValue,
    ImageContent,
    MessageValue,
    Model,
    ModelThinkingLevel,
    ProviderResponse,
    SimpleStreamOptions,
    StopReason,
    TextContent,
    Tool,
    ToolCall,
    ToolResultMessage,
    TranscriptContext,
    Transport,
    Usage,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import getCurrentSystemPrompt
from misaka.utils.js import UNDEFINED

# Stream function used by the agent loop. `Models.streamSimple` satisfies
# this shape.
#
# The loop passes a normalized transcript: the system prompt and tool
# declarations are carried by the transcript's system messages, never by
# `context.systemPrompt` or `context.tools`.
#
# Contract:
# - Must not throw or return a rejected promise for request/model/runtime failures.
# - Must return an AssistantMessageEventStream.
# - Failures must be encoded in the returned stream via protocol events and a
#   final AssistantMessage with stopReason "error" or "aborted" and errorMessage.
type StreamFn = Callable[
    # MISAKA fork: the options are any object or mapping carrying the stream options (the loop hands over a namespace).
    [Model, TranscriptContext, Any],
    AssistantMessageEventStream | Awaitable[AssistantMessageEventStream],
]

# Configuration for how tool calls from a single assistant message are executed.
#
# - "sequential": each tool call is prepared, executed, and finalized before the next one starts.
# - "parallel": tool calls are prepared sequentially, then allowed tools execute concurrently.
#   `tool_execution_end` is emitted in tool completion order after each tool is finalized,
#   while tool-result message artifacts are emitted later in assistant source order.
type ToolExecutionMode = Literal["sequential", "parallel"]

# Controls how many queued user messages are injected when the agent loop reaches a queue drain point.
#
# - "all": drain and inject every queued message at that point.
# - "one-at-a-time": drain and inject only the oldest queued message, leaving the rest queued for later drain points.
type QueueMode = Literal["all", "one-at-a-time"]

# A single tool call content block emitted by an assistant message.
type AgentToolCall = ToolCall


@dataclass(slots=True)
class BeforeToolCallResult:
    """Result returned from `beforeToolCall`.

    Returning `{ block: true }` prevents the tool from executing. The loop emits an error tool result instead.
    `reason` becomes the text shown in that error result. If omitted, a default blocked message is used.
    """

    block: bool | None = None
    reason: str | None = None
    # Hint that the agent should stop after the current tool batch when this call is blocked.
    # Early termination only happens when every finalized tool result in the batch sets this to true.
    terminate: bool | None = None
    # MISAKA fork: CCB PreToolUse.updatedInput; the replacement arguments are validated again before use.
    updatedInput: dict[str, Any] | None = None


@dataclass(slots=True)
class AfterToolCallResult:
    """Partial override returned from `afterToolCall`.

    Merge semantics are field-by-field:
    - `content`: if provided, replaces the tool result content array in full
    - `details`: if provided, replaces the tool result details value in full
    - `isError`: if provided, replaces the tool result error flag
    - `usage`: if provided, replaces the tool result usage
    - `terminate`: if provided, replaces the early-termination hint
    - `structuredContent`: if provided, replaces the structured content. If `content` is provided
      without it, the structured content is dropped, because it may no longer match the content.
      Return it along with `content` to keep it.

    Other omitted fields keep the original executed tool result values.
    There is no deep merge for `content`, `details`, or `usage`.
    """

    content: list[TextContent | ImageContent] | None = None
    details: Any | None = None
    structuredContent: Any | None = None
    isError: bool | None = None
    # Usage from the final tool execution itself, if available. Not used for main LLM context accounting.
    usage: Usage | None = None
    # Hint that the agent should stop after the current tool batch.
    # Early termination only happens when every finalized tool result in the batch sets this to true.
    terminate: bool | None = None


@dataclass(slots=True)
class BeforeToolCallContext:
    """Context passed to `beforeToolCall`."""

    # The assistant message that requested the tool call.
    assistantMessage: AssistantMessage
    # The raw tool call block from `assistantMessage.content`.
    toolCall: AgentToolCall
    # Validated tool arguments for the target tool schema.
    args: Any
    # Current agent context at the time the tool call is prepared.
    context: AgentContext


@dataclass(slots=True)
class AfterToolCallContext:
    """Context passed to `afterToolCall`."""

    # The assistant message that requested the tool call.
    assistantMessage: AssistantMessage
    # The raw tool call block from `assistantMessage.content`.
    toolCall: AgentToolCall
    # Validated tool arguments for the target tool schema.
    args: Any
    # The executed tool result before any `afterToolCall` overrides are applied.
    result: AgentToolResult
    # Whether the executed tool result is currently treated as an error.
    isError: bool
    # Current agent context at the time the tool call is finalized.
    context: AgentContext


@dataclass(slots=True)
class AgentTurnContext:
    """Context passed to completed-turn callbacks."""

    # The assistant message that completed the turn.
    message: AssistantMessage
    # Tool result messages emitted for the completed turn.
    toolResults: list[ToolResultMessage]
    # Current agent context after the turn's assistant message and tool results have been appended.
    context: AgentContext
    # Messages that this loop invocation will return if it exits at this point. Prompt runs include the initial prompt
    # messages; continuation runs do not include pre-existing context messages.
    newMessages: list[AgentMessage]


@dataclass(slots=True)
class AgentTurnDecision:
    """Decision returned by `FinishTurn`. Returning None preserves normal scheduling."""

    action: Literal["continue", "end"]


# Called after a completed assistant turn and all of its tool-result messages, but before `turn_end`.
# On a normal turn, `{ action: "continue" }` ensures one next provider request. Tool-result, steering, or
# follow-up scheduling can satisfy that request and adds no extra request; otherwise the loop continues once
# with the current context. Error and aborted responses remain hard exits.
type FinishTurn = Callable[
    [AgentTurnContext, Any | None],
    AgentTurnDecision | None | Awaitable[AgentTurnDecision | None],
]


@dataclass(slots=True)
class AgentLoopTurnUpdate:
    """Replacement runtime state used by the agent loop before starting another provider request."""

    # Context for the next provider request.
    context: AgentContext | None = None
    # Messages to append before the next provider request, with normal lifecycle events.
    messages: list[AgentMessage] | None = None
    # Model for the next provider request.
    model: Model | None = None
    # Thinking level for the next provider request.
    thinkingLevel: ThinkingLevel | None = None


@dataclass(slots=True)
class PrepareRequestContext:
    """Runtime state available immediately before a conversational provider request."""

    context: AgentContext
    model: Model
    thinkingLevel: ThinkingLevel


@dataclass(slots=True)
class AgentRequestUpdate:
    """Replacement runtime state for the provider request being prepared."""

    context: AgentContext | None = None
    model: Model | None = None
    thinkingLevel: ThinkingLevel | None = None


# Called immediately before every conversational provider request, including the first.
# Pending messages have already been appended and emitted when this callback runs.
type PrepareRequest = Callable[
    [PrepareRequestContext, Any | None],
    AgentRequestUpdate | None | Awaitable[AgentRequestUpdate | None],
]

type PrepareNextTurnContext = AgentTurnContext


@dataclass(slots=True)
class AgentLoopConfig:
    """MISAKA fork: upstream's `AgentLoopConfig extends SimpleStreamOptions`; the stream options are listed after the
    loop's own members."""

    model: Model

    # Converts AgentMessage[] to LLM-compatible Message[] before each LLM call.
    #
    # Each AgentMessage must be converted to a SystemMessage, UserMessage, AssistantMessage, or ToolResultMessage
    # that the LLM can understand. AgentMessages that cannot be converted (e.g., UI-only notifications,
    # status messages) should be filtered out.
    #
    # Contract: must not throw or reject. Return a safe fallback value instead.
    # Throwing interrupts the low-level agent loop without producing a normal event sequence.
    convertToLlm: Callable[[list[AgentMessage]], list[MessageValue] | Awaitable[list[MessageValue]]]

    # Optional transform applied to the context before `convertToLlm`.
    #
    # Use this for operations that work at the AgentMessage level:
    # - Context window management (pruning old messages)
    # - Injecting context from external sources
    #
    # Contract: must not throw or reject. Return the original messages or another
    # safe fallback value instead.
    transformContext: Callable[[list[AgentMessage], Any | None], Awaitable[list[AgentMessage]]] | None = None

    # Resolves an API key dynamically for each LLM call.
    #
    # Useful for short-lived OAuth tokens (e.g., GitHub Copilot) that may expire
    # during long-running tool execution phases.
    #
    # Contract: must not throw or reject. Return undefined when no key is available.
    getApiKey: Callable[[str], str | None | Awaitable[str | None]] | None = None

    # Called after the assistant message and all tool-result messages have been emitted, immediately before `turn_end`.
    # `{ action: "end" }` ends the run without polling queues or preparing another request.
    # On a normal turn, `{ action: "continue" }` ensures one next provider request. Tool-result, steering, or
    # follow-up scheduling can satisfy that request and adds no extra request; otherwise the loop continues once
    # with the current context. Returning undefined preserves normal scheduling. Error and aborted responses remain
    # hard exits.
    finishTurn: FinishTurn | None = None

    # Called immediately before every conversational provider request, including the first.
    # Pending messages have already been appended. The returned context, model, and thinking level
    # replace the runtime values for this and later requests in the run. This hook does not poll queues.
    prepareRequest: PrepareRequest | None = None

    # Called after `turn_end` when the loop will continue, immediately before the next turn starts.
    # Return replacement context/model/thinking state or messages to append to affect that turn.
    # Return undefined to keep using the current context/config.
    prepareNextTurn: (
        Callable[[PrepareNextTurnContext], AgentLoopTurnUpdate | None | Awaitable[AgentLoopTurnUpdate | None]] | None
    ) = None

    # Returns steering messages to inject into the conversation mid-run.
    #
    # Called after the current assistant turn finishes executing its tool calls, unless `finishTurn` ends the run.
    # If messages are returned, they are added to the context before the next LLM call.
    # Tool calls from the current assistant message are not skipped.
    #
    # Use this for "steering" the agent while it's working.
    #
    # Contract: must not throw or reject. Return [] when no steering messages are available.
    getSteeringMessages: Callable[[], Awaitable[list[AgentMessage]]] | None = None

    # Returns follow-up messages to process after the agent would otherwise stop.
    #
    # Called when the agent has no more tool calls and no steering messages.
    # If messages are returned, they're added to the context and the agent
    # continues with another turn.
    #
    # Use this for follow-up messages that should wait until the agent finishes.
    #
    # Contract: must not throw or reject. Return [] when no follow-up messages are available.
    getFollowUpMessages: Callable[[], Awaitable[list[AgentMessage]]] | None = None

    # Tool execution mode.
    # - "sequential": execute tool calls one by one
    # - "parallel": preflight tool calls sequentially, then execute allowed tools concurrently;
    #   emit `tool_execution_end` in tool completion order after each tool is finalized,
    #   then emit tool-result message artifacts later in assistant source order
    #
    # Default: "parallel"
    toolExecution: ToolExecutionMode = "parallel"

    # Called before a tool is executed, after arguments have been validated.
    #
    # Return `{ block: true }` to prevent execution. The loop emits an error tool result instead.
    # A blocked result can also set `terminate: true` to participate in the batch early-termination rule.
    # The hook receives the agent abort signal and is responsible for honoring it.
    beforeToolCall: (
        Callable[[BeforeToolCallContext, Any | None], Awaitable[BeforeToolCallResult | None]] | None
    ) = None

    # Called after a tool finishes executing, before `tool_execution_end` and tool-result message events are emitted.
    #
    # Return an `AfterToolCallResult` to override parts of the executed tool result:
    # - `content` replaces the full content array
    # - `details` replaces the full details payload
    # - `isError` replaces the error flag
    # - `usage` replaces the tool result usage
    # - `terminate` replaces the early-termination hint
    #
    # Any omitted fields keep their original values. No deep merge is performed.
    # The hook receives the agent abort signal and is responsible for honoring it.
    afterToolCall: (
        Callable[[AfterToolCallContext, Any | None], Awaitable[AfterToolCallResult | None]] | None
    ) = None

    # The `SimpleStreamOptions` members the loop forwards to the stream function.
    reasoning: ThinkingLevel | None = None
    apiKey: str | None = None
    sessionId: str | None = None
    temperature: float | None = None
    maxTokens: int | None = None
    onPayload: Callable[[dict[str, Any], Model], Any] | None = None
    onResponse: Callable[[ProviderResponse | dict[str, Any], Model], Any] | None = None
    onProviderStreamEvent: Callable[..., Any] | None = None
    transport: Transport | None = None
    thinkingBudgets: Any | None = None
    maxRetryDelayMs: int | None = None
    headers: dict[str, str] | None = None
    timeoutMs: int | None = None
    maxRetries: int | None = None
    metadata: dict[str, Any] | None = None
    # The rest of `SimpleStreamOptions`, absent unless set: the loop's spread leaves an unset one out, as
    # `{ ...config }` does with a property that was never assigned. MISAKA fork: no `fetch` (the adapters own their
    # HTTP clients) and no `signal` (the loop is handed one).
    telemetryContext: Any = UNDEFINED  # TelemetryContext
    env: Any = UNDEFINED  # dict[str, str | None]
    samplingParams: Any = UNDEFINED  # SamplingParams
    cacheRetention: Any = UNDEFINED  # CacheRetention
    websocketConnectTimeoutMs: Any = UNDEFINED  # int
    wait: Any = UNDEFINED  # int
    toolChoice: Any = UNDEFINED  # ToolChoice
    deferred: Any = UNDEFINED  # bool | dict[str, Any]


# Thinking/reasoning level for models that support it.
# Note: "xhigh" and "max" are only supported by selected model families. Use model
# thinking-level metadata from pi-ai to detect support for a concrete model.
type ThinkingLevel = ModelThinkingLevel


@runtime_checkable
class CustomAgentMessage(Protocol):
    """MISAKA fork: upstream's `CustomAgentMessages` is extended through declaration merging; here a custom app message
    is anything with a `role` that is not one of the LLM roles."""

    role: str


# AgentMessage: Union of LLM messages + custom messages.
# This abstraction allows apps to add custom message types while maintaining
# type safety and compatibility with the base LLM messages.
type AgentMessage = MessageValue | CustomAgentMessage


class AgentState:
    """Public agent state.

    `tools` and `messages` use accessor properties so implementations can copy
    assigned arrays before storing them.

    MISAKA fork: upstream's interface is a class here; `createMutableAgentState` (agent.py) builds the one an `Agent`
    owns.
    """

    def __init__(
        self,
        *,
        model: Model,
        thinkingLevel: ThinkingLevel = "off",
        tools: list[AgentTool] | None = None,
        messages: list[AgentMessage] | None = None,
        isStreaming: bool = False,
        streamingMessage: AgentMessage | None = None,
        pendingToolCalls: set[str] | None = None,
        errorMessage: str | None = None,
    ) -> None:
        # Active model used for future turns.
        self.model = model
        # Requested reasoning level for future turns.
        self.thinkingLevel = thinkingLevel
        self._tools = list(tools or [])
        self._messages = list(messages or [])
        # True while the agent is processing a prompt or continuation.
        #
        # This remains true until awaited `agent_end` listeners settle.
        self.isStreaming = isStreaming
        # Partial assistant message for the current streamed response, if any.
        self.streamingMessage = streamingMessage
        # Tool call ids currently executing.
        self.pendingToolCalls = set(pendingToolCalls or set())
        # Error message from the most recent failed or aborted assistant turn, if any.
        self.errorMessage = errorMessage

    @property
    def systemPrompt(self) -> str:
        """Current system prompt, replayed from the transcript's system messages.

        Read-only: to change the prompt, append a system message with `content` or `sections`.
        In `initialState`, this seeds the leading system message.
        """
        return getCurrentSystemPrompt(self._messages)

    @property
    def tools(self) -> list[AgentTool]:
        """Executable tools. Assigning a new array copies the top-level array.

        Differences from the tools declared in the transcript are announced to the model
        with a system message before the next request.
        """
        return self._tools

    @tools.setter
    def tools(self, value: list[AgentTool]) -> None:
        self._tools = list(value)

    @property
    def messages(self) -> list[AgentMessage]:
        """Conversation transcript. Assigning a new array copies the top-level array.

        System messages in the transcript carry the prompt and tool declarations.
        """
        return self._messages

    @messages.setter
    def messages(self, value: list[AgentMessage]) -> None:
        self._messages = list(value)


@dataclass(slots=True)
class AgentToolResult:
    """Final or partial result produced by a tool."""

    # Text or image content returned to the model.
    content: list[TextContent | ImageContent]
    # Arbitrary structured details for logs or UI rendering.
    details: Any
    # Machine-readable result matching the tool's `outputSchema`, for programmatic callers. Not sent
    # to the model; `content` remains the model-facing result.
    structuredContent: Any | None = None
    # Usage from the final tool execution itself, if available. Not used for main LLM context accounting.
    usage: Usage | None = None
    # Report a failure without throwing. The model sees `content` as an error result, like a thrown
    # error, but `details` and `structuredContent` are kept for the UI and programmatic callers.
    isError: bool | None = None
    # Hint that the agent should stop after the current tool batch.
    # Early termination only happens when every finalized tool result in the batch sets this to true.
    terminate: bool | None = None


@dataclass(slots=True)
class AgentToolCallOutcome:
    """Final outcome of a tool call after hooks ran."""

    toolCall: AgentToolCall
    result: AgentToolResult
    isError: bool
    # Milliseconds `execute()` took, measured with a monotonic clock; absent when the tool did not run.
    durationMs: int | None = None


# Callback used by tools to stream partial execution updates.
#
# The callback is scoped to the current `execute()` invocation. Calls made after
# the tool promise settles are ignored.
type AgentToolUpdateCallback = Callable[[AgentToolResult], None]


class AgentTool(Tool):
    """Tool definition used by the agent runtime."""

    # Human-readable label for UI display.
    label: str
    # MISAKA fork: runtime-only legacy names a call may still use; never advertised as duplicate provider tool schemas.
    aliases: tuple[str, ...] = Field(default=(), exclude=True)
    # Optional compatibility shim for raw tool-call arguments before schema validation.
    # Must return an object that matches `TParameters`.
    prepareArguments: Callable[[Any], Any] | None = None
    # JSON Schema of `structuredContent` in successful results. Tools that declare it should always
    # set `structuredContent`.
    outputSchema: Any | None = None
    # Execute the tool call. Throw on failure, or return a result with `isError: true`; do not only
    # describe the failure in `content`.
    execute: Callable[[str, Any, Any | None, AgentToolUpdateCallback | None], Awaitable[AgentToolResult]]
    # Recovery policy for an effect whose durable intent exists but whose outcome is unknown.
    replay: Literal["never", "safe"] | None = None
    # Per-tool execution mode override.
    # - "sequential": this tool must execute one at a time with other tool calls.
    # - "parallel": this tool can execute concurrently with other tool calls.
    #
    # If omitted, the default execution mode applies.
    executionMode: ToolExecutionMode | None = None


@dataclass(slots=True)
class AgentContext:
    """Context snapshot passed into the low-level agent loop."""

    # Transcript visible to the model.
    messages: list[AgentMessage]
    # Tools available for execution in this run.
    tools: list[AgentTool] | None = None


# Events emitted by the Agent for UI updates.
#
# `agent_end` is the last event emitted for a run, but awaited `Agent.subscribe()`
# listeners for that event are still part of run settlement. The agent becomes
# idle only after those listeners finish.


@dataclass(slots=True)
class AgentStartEvent:
    type: Literal["agent_start"] = "agent_start"


@dataclass(slots=True)
class AgentEndEvent:
    messages: list[AgentMessage]
    type: Literal["agent_end"] = "agent_end"


# Turn lifecycle - a turn is one assistant response + any tool calls/results
@dataclass(slots=True)
class TurnStartEvent:
    type: Literal["turn_start"] = "turn_start"


@dataclass(slots=True)
class TurnEndEvent:
    message: AgentMessage
    toolResults: list[ToolResultMessage]
    type: Literal["turn_end"] = "turn_end"


# Message lifecycle - emitted for system, user, assistant, and toolResult messages
@dataclass(slots=True)
class MessageStartEvent:
    message: AgentMessage
    type: Literal["message_start"] = "message_start"


# Only emitted for assistant messages during streaming
@dataclass(slots=True)
class MessageUpdateEvent:
    message: AgentMessage
    assistantMessageEvent: AssistantMessageEvent
    type: Literal["message_update"] = "message_update"


@dataclass(slots=True)
class MessageEndEvent:
    message: AgentMessage
    type: Literal["message_end"] = "message_end"


# Tool execution lifecycle
@dataclass(slots=True)
class ToolExecutionStartEvent:
    toolCallId: str
    toolName: str
    args: Any
    type: Literal["tool_execution_start"] = "tool_execution_start"
    # MISAKA fork: upstream widens the session's event type with `parentToolCallId` for calls a
    # tool made through `ctx.executeTool()`; here the field lives on the event, unset for
    # model-issued calls.
    parentToolCallId: str | None = None


@dataclass(slots=True)
class ToolExecutionUpdateEvent:
    toolCallId: str
    toolName: str
    args: Any
    partialResult: Any
    type: Literal["tool_execution_update"] = "tool_execution_update"
    # MISAKA fork: see `ToolExecutionStartEvent.parentToolCallId`.
    parentToolCallId: str | None = None


@dataclass(slots=True)
class ToolExecutionEndEvent:
    toolCallId: str
    toolName: str
    result: Any
    isError: bool
    # Milliseconds `execute()` took, measured with a monotonic clock; absent when the tool did not run.
    durationMs: int | None = None
    type: Literal["tool_execution_end"] = "tool_execution_end"
    # MISAKA fork: see `ToolExecutionStartEvent.parentToolCallId`.
    parentToolCallId: str | None = None


type AgentEvent = (
    AgentStartEvent
    | AgentEndEvent
    | TurnStartEvent
    | TurnEndEvent
    | MessageStartEvent
    | MessageUpdateEvent
    | MessageEndEvent
    | ToolExecutionStartEvent
    | ToolExecutionUpdateEvent
    | ToolExecutionEndEvent
)

type AssistantMessageEvent = AssistantMessageEventValue

__all__ = [
    "AfterToolCallContext",
    "AfterToolCallResult",
    "AgentContext",
    "AgentEndEvent",
    "AgentEvent",
    "AgentLoopConfig",
    "AgentLoopTurnUpdate",
    "AgentMessage",
    "AgentRequestUpdate",
    "AgentStartEvent",
    "AgentState",
    "AgentTool",
    "AgentToolCall",
    "AgentToolCallOutcome",
    "AgentToolResult",
    "AgentToolUpdateCallback",
    "AgentTurnContext",
    "AgentTurnDecision",
    "AssistantMessageEvent",
    "BeforeToolCallContext",
    "BeforeToolCallResult",
    "CustomAgentMessage",
    "FinishTurn",
    "MessageEndEvent",
    "MessageStartEvent",
    "MessageUpdateEvent",
    "PrepareNextTurnContext",
    "PrepareRequest",
    "PrepareRequestContext",
    "QueueMode",
    "SimpleStreamOptions",
    "StopReason",
    "StreamFn",
    "ThinkingLevel",
    "ToolExecutionEndEvent",
    "ToolExecutionMode",
    "ToolExecutionStartEvent",
    "ToolExecutionUpdateEvent",
    "TurnEndEvent",
    "TurnStartEvent",
]
