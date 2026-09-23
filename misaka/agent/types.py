"""Public type surface for the agent runtime."""

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
from misaka.ai.utils.transcript import (
    create_initial_system_message,
    get_current_system_prompt,
    to_tool_declaration,
)

# Stream function type: the loop passes a normalized transcript: the system prompt and tool
# declarations are carried by the transcript's system messages, never by
# `context.systemPrompt` or `context.tools`.
type StreamFn = Callable[
    [Model, TranscriptContext, SimpleStreamOptions | dict[str, Any] | None],
    AssistantMessageEventStream | Awaitable[AssistantMessageEventStream],
]
type ToolExecutionMode = Literal["sequential", "parallel"]
type QueueMode = Literal["all", "one-at-a-time"]
type ThinkingLevel = ModelThinkingLevel
type AgentToolCall = ToolCall
type AssistantMessageEvent = AssistantMessageEventValue


@runtime_checkable
class CustomAgentMessage(Protocol):
    role: str


type AgentMessage = MessageValue | CustomAgentMessage


@dataclass(slots=True)
class BeforeToolCallResult:
    block: bool | None = None
    reason: str | None = None
    # Hint that the agent should stop after the current tool batch when this call is
    # blocked.  Early termination only happens when every finalized tool result in the
    # batch sets this to true.  (pi 1eb988c #7715)
    terminate: bool | None = None
    # MISAKA adapter for CCB PreToolUse.updatedInput; validated again before use.
    updatedInput: dict[str, Any] | None = None


@dataclass(slots=True)
class AfterToolCallResult:
    content: list[TextContent | ImageContent] | None = None
    details: Any | None = None
    isError: bool | None = None
    # Replaces the tool result usage when provided (pi packages/agent/src/types.ts
    # AfterToolCallResult.usage).  There is no deep merge.
    usage: Usage | None = None
    terminate: bool | None = None


@dataclass(slots=True)
class AgentToolResult:
    content: list[TextContent | ImageContent]
    details: Any
    # Usage from the final tool execution itself, if available.  Not used for main LLM
    # context accounting (pi packages/agent/src/types.ts AgentToolResult.usage).
    usage: Usage | None = None
    terminate: bool | None = None


type AgentToolUpdateCallback = Callable[[AgentToolResult], None]


@dataclass(slots=True)
class AgentContext:
    # Conversation transcript. System messages in the transcript carry the prompt and tool
    # declarations.
    messages: list[AgentMessage]
    # Tools available for execution in this run.
    tools: list[AgentTool] | None = None


@dataclass(slots=True)
class BeforeToolCallContext:
    assistantMessage: AssistantMessage
    toolCall: AgentToolCall
    args: Any
    context: AgentContext


@dataclass(slots=True)
class AfterToolCallContext:
    assistantMessage: AssistantMessage
    toolCall: AgentToolCall
    args: Any
    result: AgentToolResult
    isError: bool
    context: AgentContext


@dataclass(slots=True)
class AgentTurnContext:
    """Context passed to completed-turn callbacks."""

    message: AssistantMessage
    # Tool result messages emitted for the completed turn.
    toolResults: list[ToolResultMessage]
    context: AgentContext
    newMessages: list[AgentMessage]


@dataclass(slots=True)
class AgentTurnDecision:
    """Decision returned by a `FinishTurn`. Returning None preserves normal scheduling."""

    action: Literal["continue", "end"]


# Called after a completed assistant turn and all of its tool-result messages, but before `turn_end`.
# On a normal turn, `{ action: "continue" }` ensures one next provider request. Tool-result, steering, or
# follow-up scheduling can satisfy that request and adds no extra request; otherwise the loop continues once
# with the current context. Error and aborted responses remain hard exits.
type FinishTurn = Callable[
    [AgentTurnContext, Any | None],
    AgentTurnDecision | None | Awaitable[AgentTurnDecision | None],
]


type PrepareNextTurnContext = AgentTurnContext


@dataclass(slots=True)
class AgentLoopTurnUpdate:
    context: AgentContext | None = None
    model: Model | None = None
    thinkingLevel: ThinkingLevel | None = None
    # Messages to append before the next provider request, with normal lifecycle events.
    messages: list[AgentMessage] | None = None


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


@dataclass(slots=True)
class AgentLoopConfig:
    model: Model
    convertToLlm: Callable[
        [list[AgentMessage]],
        list[MessageValue] | Awaitable[list[MessageValue]],
    ]
    transformContext: Callable[[list[AgentMessage], Any | None], Awaitable[list[AgentMessage]]] | None = None
    getApiKey: Callable[[str], str | None | Awaitable[str | None]] | None = None
    # Called after the assistant message and all tool-result messages have been emitted, immediately before `turn_end`.
    # `{ action: "end" }` ends the run without polling queues or preparing another request.
    # On a normal turn, `{ action: "continue" }` ensures one next provider request. Tool-result, steering, or
    # follow-up scheduling can satisfy that request and adds no extra request; otherwise the loop continues once
    # with the current context. Returning None preserves normal scheduling. Error and aborted responses remain
    # hard exits.
    finishTurn: FinishTurn | None = None
    # Called immediately before every conversational provider request, including the first.
    # Pending messages have already been appended. The returned context, model, and thinking level
    # replace the runtime values for this and later requests in the run. This hook does not poll queues.
    prepareRequest: PrepareRequest | None = None
    prepareNextTurn: (
        Callable[
            [PrepareNextTurnContext],
            AgentLoopTurnUpdate | None | Awaitable[AgentLoopTurnUpdate | None],
        ]
        | None
    ) = None
    getSteeringMessages: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    getFollowUpMessages: Callable[[], Awaitable[list[AgentMessage]]] | None = None
    toolExecution: ToolExecutionMode = "parallel"
    beforeToolCall: (
        Callable[[BeforeToolCallContext, Any | None], Awaitable[BeforeToolCallResult | None]]
        | None
    ) = None
    afterToolCall: (
        Callable[[AfterToolCallContext, Any | None], Awaitable[AfterToolCallResult | None]]
        | None
    ) = None
    reasoning: ThinkingLevel | None = None
    apiKey: str | None = None
    sessionId: str | None = None
    temperature: float | None = None
    maxTokens: int | None = None
    onPayload: Callable[[dict[str, Any], Model], Any] | None = None
    onResponse: Callable[[ProviderResponse | dict[str, Any], Model], Any] | None = None
    transport: Transport | None = None
    thinkingBudgets: Any | None = None
    maxRetryDelayMs: int | None = None
    headers: dict[str, str] | None = None
    timeoutMs: int | None = None
    maxRetries: int | None = None
    metadata: dict[str, Any] | None = None


class AgentTool(Tool):
    label: str
    # Runtime-only legacy names. Never advertise duplicate provider tool schemas.
    aliases: tuple[str, ...] = Field(default=(), exclude=True)
    prepareArguments: Callable[[Any], Any] | None = None
    execute: Callable[[str, Any, Any | None, AgentToolUpdateCallback | None], Awaitable[AgentToolResult]]
    executionMode: ToolExecutionMode | None = None


class AgentState:
    def __init__(
        self,
        *,
        systemPrompt: str = "",
        model: Model,
        thinkingLevel: ThinkingLevel = "off",
        tools: list[AgentTool] | None = None,
        messages: list[AgentMessage] | None = None,
        isStreaming: bool = False,
        streamingMessage: AgentMessage | None = None,
        pendingToolCalls: set[str] | None = None,
        errorMessage: str | None = None,
    ) -> None:
        self.model = model
        self.thinkingLevel = thinkingLevel
        self._tools = list(tools or [])
        self._messages = list(messages or [])
        # In `initialState`, `systemPrompt` seeds the leading system message
        # (pi `createMutableAgentState`).
        initial_message = create_initial_system_message(systemPrompt, [to_tool_declaration(tool) for tool in self._tools])
        if (not self._messages or getattr(self._messages[0], "role", None) != "system") and initial_message:
            self._messages.insert(0, initial_message)
        self.isStreaming = isStreaming
        self.streamingMessage = streamingMessage
        self.pendingToolCalls = set(pendingToolCalls or set())
        self.errorMessage = errorMessage

    @property
    def systemPrompt(self) -> str:
        """Current system prompt, replayed from the transcript's system messages.

        Read-only: to change the prompt, append a system message with `content` or `sections`.
        In `initialState`, this seeds the leading system message.
        """
        return get_current_system_prompt(self._messages)

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
        return self._messages

    @messages.setter
    def messages(self, value: list[AgentMessage]) -> None:
        self._messages = list(value)


@dataclass(slots=True)
class AgentStartEvent:
    type: Literal["agent_start"] = "agent_start"


@dataclass(slots=True)
class AgentEndEvent:
    messages: list[AgentMessage]
    type: Literal["agent_end"] = "agent_end"


@dataclass(slots=True)
class TurnStartEvent:
    type: Literal["turn_start"] = "turn_start"


@dataclass(slots=True)
class TurnEndEvent:
    message: AgentMessage
    toolResults: list[ToolResultMessage]
    type: Literal["turn_end"] = "turn_end"


@dataclass(slots=True)
class MessageStartEvent:
    message: AgentMessage
    type: Literal["message_start"] = "message_start"


@dataclass(slots=True)
class MessageUpdateEvent:
    message: AgentMessage
    assistantMessageEvent: AssistantMessageEvent
    type: Literal["message_update"] = "message_update"


@dataclass(slots=True)
class MessageEndEvent:
    message: AgentMessage
    type: Literal["message_end"] = "message_end"


@dataclass(slots=True)
class ToolExecutionStartEvent:
    toolCallId: str
    toolName: str
    args: Any
    type: Literal["tool_execution_start"] = "tool_execution_start"


@dataclass(slots=True)
class ToolExecutionUpdateEvent:
    toolCallId: str
    toolName: str
    args: Any
    partialResult: Any
    type: Literal["tool_execution_update"] = "tool_execution_update"


@dataclass(slots=True)
class ToolExecutionEndEvent:
    toolCallId: str
    toolName: str
    result: Any
    isError: bool
    type: Literal["tool_execution_end"] = "tool_execution_end"


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
