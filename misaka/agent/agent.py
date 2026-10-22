"""Translated from pi's ``packages/agent/src/agent.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, overload

from pydantic import BaseModel

from misaka.agent.agent_loop import runAgentLoop, runAgentLoopContinue
from misaka.agent.stream_fn import getDefaultStreamFn
from misaka.agent.types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentLoopTurnUpdate,
    AgentMessage,
    AgentState,
    BeforeToolCallContext,
    BeforeToolCallResult,
    FinishTurn,
    MessageEndEvent,
    MessageStartEvent,
    PrepareNextTurnContext,
    PrepareRequest,
    QueueMode,
    StreamFn,
    ToolExecutionMode,
    TurnEndEvent,
)
from misaka.ai.types import (
    AssistantMessage,
    ImageContent,
    MessageValue,
    Model,
    ProviderResponse,
    TextContent,
    ThinkingBudgets,
    Transport,
    Usage,
    validate_message,
)
from misaka.ai.utils.transcript import (
    createInitialSystemMessage,
    getCurrentSystemMessage,
    toToolDeclaration,
)
from misaka.utils.js import AbortController, AbortSignal, errorMessage, liveKeys
from misaka.utils.values import maybe_await, read_field


def _defaultConvertToLlm(messages: list[AgentMessage]) -> list[MessageValue]:
    return [
        message  # type: ignore[misc]
        for message in messages
        if read_field(message, "role") in ("system", "user", "assistant", "toolResult")
    ]


_EMPTY_USAGE = {
    "input": 0,
    "output": 0,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 0,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}

_DEFAULT_MODEL = Model(
    id="unknown",
    name="unknown",
    api="unknown",
    provider="unknown",
    baseUrl="",
    reasoning=False,
    input=[],
    cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    contextWindow=0,
    maxTokens=0,
)


# Initial state for `Agent`. `systemPrompt` and `tools` become the leading system message unless `messages` already
# starts with one. MISAKA fork: a mapping or any object with those attributes (such as an `AgentState`).
type AgentInitialState = Mapping[str, Any] | Any


def _normalizeStandardMessage(message: Any) -> AgentMessage:
    """MISAKA fork: an LLM message given as a mapping becomes its message model; message models and custom messages
    pass through."""
    if isinstance(message, Mapping) and message.get("role") in ("system", "user", "assistant", "toolResult"):
        return validate_message(dict(message))
    return message


def _createMutableAgentState(initialState: AgentInitialState | None = None) -> AgentState:
    tools = list(read_field(initialState, "tools") or [])
    messages = [_normalizeStandardMessage(message) for message in read_field(initialState, "messages") or []]
    initialMessage = createInitialSystemMessage(
        read_field(initialState, "systemPrompt"), [toToolDeclaration(tool) for tool in tools]
    )
    if (len(messages) == 0 or read_field(messages[0], "role") != "system") and initialMessage:
        messages.insert(0, initialMessage)

    model = read_field(initialState, "model")
    return AgentState(
        model=(model if isinstance(model, Model) else Model.model_validate(model)) if model is not None else _DEFAULT_MODEL,
        thinkingLevel=read_field(initialState, "thinkingLevel") or "off",
        tools=tools,
        messages=messages,
        isStreaming=False,
        streamingMessage=None,
        pendingToolCalls=set(),
        errorMessage=None,
    )


@dataclass(slots=True)
class AgentOptions:
    """Options for constructing an `Agent`."""

    initialState: AgentInitialState | None = None
    convertToLlm: Callable[[list[AgentMessage]], list[MessageValue] | Awaitable[list[MessageValue]]] | None = None
    transformContext: Callable[[list[AgentMessage], Any | None], Awaitable[list[AgentMessage]]] | None = None
    streamFn: StreamFn | None = None
    getApiKey: Callable[[str], str | None | Awaitable[str | None]] | None = None
    onPayload: Callable[[dict[str, Any], Model], Any] | None = None
    onResponse: Callable[[ProviderResponse | dict[str, Any], Model], Any] | None = None
    onProviderStreamEvent: Callable[..., Any] | None = None
    beforeToolCall: Callable[[BeforeToolCallContext, Any | None], Awaitable[BeforeToolCallResult | None]] | None = None
    afterToolCall: Callable[[AfterToolCallContext, Any | None], Awaitable[AfterToolCallResult | None]] | None = None
    finishTurn: FinishTurn | None = None
    prepareRequest: PrepareRequest | None = None
    prepareNextTurn: Callable[[Any | None], AgentLoopTurnUpdate | None | Awaitable[AgentLoopTurnUpdate | None]] | None = None
    prepareNextTurnWithContext: (
        Callable[[PrepareNextTurnContext, Any | None], AgentLoopTurnUpdate | None | Awaitable[AgentLoopTurnUpdate | None]]
        | None
    ) = None
    steeringMode: QueueMode | None = None
    followUpMode: QueueMode | None = None
    sessionId: str | None = None
    thinkingBudgets: ThinkingBudgets | None = None
    transport: Transport | None = None
    maxRetryDelayMs: int | None = None
    toolExecution: ToolExecutionMode | None = None


class _PendingMessageQueue:
    def __init__(self, mode: QueueMode) -> None:
        self._messages: list[AgentMessage] = []
        self.mode = mode

    def enqueue(self, message: AgentMessage) -> None:
        self._messages.append(message)

    def hasItems(self) -> bool:
        return len(self._messages) > 0

    def peek(self) -> list[AgentMessage]:
        if self.mode == "all":
            return self._messages[:]
        first = self._messages[0] if self._messages else None
        return [first] if first is not None else []

    def drain(self) -> list[AgentMessage]:
        drained = self.peek()
        self._messages = self._messages[len(drained) :]
        return drained

    def clear(self) -> None:
        self._messages = []

    def take(self, predicate: Callable[[AgentMessage], bool]) -> list[AgentMessage]:
        """MISAKA fork: remove and return the messages ``predicate`` selects, in queue order."""
        taken = [message for message in self._messages if predicate(message)]
        self._messages = [message for message in self._messages if not predicate(message)]
        return taken


@dataclass(slots=True)
class _ActiveRun:
    promise: asyncio.Future[None]
    abortController: AbortController


type AgentListener = Callable[[AgentEvent, AbortSignal], Awaitable[None] | None]


class Agent:
    """Stateful wrapper around the low-level agent loop.

    `Agent` owns the current transcript, emits lifecycle events, executes tools,
    and exposes queueing APIs for steering and follow-up messages.
    """

    def __init__(self, options: AgentOptions | Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        # Older compiled consumers may omit options or streamFn even though the current API requires them.
        # MISAKA fork: the options may also be given as a mapping or as keyword arguments.
        runtimeOptions = _agentOptions(options, kwargs)
        self._state = _createMutableAgentState(runtimeOptions.initialState)
        self._listeners: dict[AgentListener, None] = {}
        self.convertToLlm = runtimeOptions.convertToLlm or _defaultConvertToLlm
        self.transformContext = runtimeOptions.transformContext
        self.streamFunction: StreamFn = runtimeOptions.streamFn or getDefaultStreamFn()
        self.getApiKey = runtimeOptions.getApiKey
        self.onPayload = runtimeOptions.onPayload
        self.onResponse = runtimeOptions.onResponse
        self.onProviderStreamEvent = runtimeOptions.onProviderStreamEvent
        self.beforeToolCall = runtimeOptions.beforeToolCall
        self.afterToolCall = runtimeOptions.afterToolCall
        self.finishTurn = runtimeOptions.finishTurn
        self.prepareRequest = runtimeOptions.prepareRequest
        self.prepareNextTurn = runtimeOptions.prepareNextTurn
        self.prepareNextTurnWithContext = runtimeOptions.prepareNextTurnWithContext
        self._steeringQueue = _PendingMessageQueue(runtimeOptions.steeringMode or "one-at-a-time")
        self._followUpQueue = _PendingMessageQueue(runtimeOptions.followUpMode or "one-at-a-time")
        self._activeRun: _ActiveRun | None = None
        # Session identifier forwarded to providers for cache-aware backends.
        self.sessionId = runtimeOptions.sessionId
        # Optional per-level thinking token budgets forwarded to the stream function.
        self.thinkingBudgets = runtimeOptions.thinkingBudgets
        # Preferred transport forwarded to the stream function.
        self.transport: Transport = runtimeOptions.transport or "auto"
        # Optional cap for provider-requested retry delays.
        self.maxRetryDelayMs = runtimeOptions.maxRetryDelayMs
        # Tool execution strategy for assistant messages that contain multiple tool calls.
        self.toolExecution: ToolExecutionMode = runtimeOptions.toolExecution or "parallel"

    def subscribe(self, listener: AgentListener) -> Callable[[], bool]:
        """Subscribe to agent lifecycle events.

        Listener promises are awaited in subscription order and are included in
        the current run's settlement. Listeners also receive the active abort
        signal for the current run.

        `agent_end` is the final emitted event for a run, but the agent does not
        become idle until all awaited listeners for that event have settled.
        """
        self._listeners[listener] = None

        def unsubscribe() -> bool:
            if listener not in self._listeners:
                return False
            del self._listeners[listener]
            return True

        return unsubscribe

    @property
    def state(self) -> AgentState:
        """Current agent state.

        Assigning `state.tools` or `state.messages` copies the provided top-level array.
        """
        return self._state

    @property
    def steeringMode(self) -> QueueMode:
        """Controls how queued steering messages are drained."""
        return self._steeringQueue.mode

    @steeringMode.setter
    def steeringMode(self, mode: QueueMode) -> None:
        self._steeringQueue.mode = mode

    @property
    def followUpMode(self) -> QueueMode:
        """Controls how queued follow-up messages are drained."""
        return self._followUpQueue.mode

    @followUpMode.setter
    def followUpMode(self, mode: QueueMode) -> None:
        self._followUpQueue.mode = mode

    def steer(self, message: AgentMessage) -> None:
        """Queue a message to be injected after the current assistant turn finishes."""
        self._steeringQueue.enqueue(message)

    def followUp(self, message: AgentMessage) -> None:
        """Queue a message to run only after the agent would otherwise stop."""
        self._followUpQueue.enqueue(message)

    def clearSteeringQueue(self) -> None:
        """Remove all queued steering messages."""
        self._steeringQueue.clear()

    def clearFollowUpQueue(self) -> None:
        """Remove all queued follow-up messages."""
        self._followUpQueue.clear()

    def clearAllQueues(self) -> None:
        """Remove all queued steering and follow-up messages."""
        self.clearSteeringQueue()
        self.clearFollowUpQueue()

    def hasQueuedMessages(self) -> bool:
        """Returns true when either queue still contains pending messages."""
        return self._steeringQueue.hasItems() or self._followUpQueue.hasItems()

    def peekQueuedMessages(self) -> list[AgentMessage]:
        """Preview the messages selected for the next turn without consuming them."""
        steering = self._steeringQueue.peek()
        return steering if len(steering) > 0 else self._followUpQueue.peek()

    def takeQueuedMessages(self, predicate: Callable[[AgentMessage], bool]) -> list[AgentMessage]:
        """MISAKA fork: remove the queued messages ``predicate`` selects and return them, steering first. The
        session takes back what was queued into a run that then never started (``AgentSession._startRun``)."""
        return [*self._steeringQueue.take(predicate), *self._followUpQueue.take(predicate)]

    @property
    def signal(self) -> AbortSignal | None:
        """Active abort signal for the current run, if any."""
        return self._activeRun.abortController.signal if self._activeRun is not None else None

    def abort(self) -> None:
        """Abort the current run, if one is active."""
        if self._activeRun is not None:
            self._activeRun.abortController.abort()

    def waitForIdle(self) -> asyncio.Future[None]:
        """Resolve when the current run and all awaited event listeners have finished.

        This resolves after `agent_end` listeners settle.
        """
        if self._activeRun is not None:
            # MISAKA fork: shielded, so a waiter that is cancelled does not cancel the run's promise.
            return asyncio.shield(self._activeRun.promise)
        resolved: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        resolved.set_result(None)
        return resolved

    def reset(self) -> None:
        """Clear conversation state and queues while retaining the replayed prompt/tool baseline."""
        if self._activeRun is not None:
            raise RuntimeError("Agent is already processing. Wait for completion before resetting.")

        baseline = getCurrentSystemMessage(self._state.messages)
        self._state.messages = [baseline] if baseline else []
        self._state.isStreaming = False
        self._state.streamingMessage = None
        self._state.pendingToolCalls = set()
        self._state.errorMessage = None
        self.clearFollowUpQueue()
        self.clearSteeringQueue()

    @overload
    async def prompt(self, message: AgentMessage | Sequence[AgentMessage]) -> None: ...

    @overload
    async def prompt(self, input: str, images: Sequence[ImageContent] | None = None) -> None: ...

    async def prompt(
        self, input: str | AgentMessage | Sequence[AgentMessage], images: Sequence[ImageContent] | None = None
    ) -> None:
        """Start a new prompt from text, a single message, or a batch of messages."""
        if self._activeRun is not None:
            raise RuntimeError(
                "Agent is already processing a prompt. Use steer() or followUp() to queue messages, or wait for completion."
            )
        messages = self._normalizePromptInput(input, images)
        await self._runPromptMessages(messages)

    async def continue_(self) -> None:
        """Continue from the current transcript. The last message must be a user or tool-result message.

        MISAKA fork: upstream's `continue()`; `continue` is a Python keyword.
        """
        if self._activeRun is not None:
            raise RuntimeError("Agent is already processing. Wait for completion before continuing.")

        lastMessage = self._state.messages[-1] if self._state.messages else None
        if lastMessage is None or all(read_field(message, "role") == "system" for message in self._state.messages):
            raise RuntimeError("No messages to continue from")

        if read_field(lastMessage, "role") == "assistant":
            queuedSteering = self._steeringQueue.drain()
            if len(queuedSteering) > 0:
                await self._runPromptMessages(queuedSteering, skipInitialSteeringPoll=True)
                return

            queuedFollowUps = self._followUpQueue.drain()
            if len(queuedFollowUps) > 0:
                await self._runPromptMessages(queuedFollowUps)
                return

            raise RuntimeError("Cannot continue from message role: assistant")

        await self._runContinuation()

    def _normalizePromptInput(
        self, input: str | AgentMessage | Sequence[AgentMessage], images: Sequence[ImageContent] | None = None
    ) -> list[AgentMessage]:
        # MISAKA fork: LLM messages given as mappings become message models (`_normalizeStandardMessage`).
        if isinstance(input, Sequence) and not isinstance(input, str | BaseModel):
            return [_normalizeStandardMessage(message) for message in input]

        if not isinstance(input, str):
            return [_normalizeStandardMessage(input)]

        content: list[TextContent | ImageContent] = [TextContent(text=input)]
        if images and len(images) > 0:
            content.extend(images)
        return [_normalizeStandardMessage({"role": "user", "content": content, "timestamp": int(time.time() * 1000)})]

    async def _runPromptMessages(self, messages: list[AgentMessage], *, skipInitialSteeringPoll: bool = False) -> None:
        async def executor(signal: AbortSignal) -> None:
            await runAgentLoop(
                messages,
                self._createContextSnapshot(),
                self._createLoopConfig(skipInitialSteeringPoll=skipInitialSteeringPoll),
                self._processEvents,
                signal,
                self.streamFunction,
            )

        await self._runWithLifecycle(executor)

    async def _runContinuation(self) -> None:
        async def executor(signal: AbortSignal) -> None:
            await runAgentLoopContinue(
                self._createContextSnapshot(),
                self._createLoopConfig(),
                self._processEvents,
                signal,
                self.streamFunction,
            )

        await self._runWithLifecycle(executor)

    def _createContextSnapshot(self) -> AgentContext:
        return AgentContext(messages=self._state.messages[:], tools=self._state.tools[:])

    def _createLoopConfig(self, *, skipInitialSteeringPoll: bool = False) -> AgentLoopConfig:
        skipPoll = skipInitialSteeringPoll

        async def prepareNextTurn(context: PrepareNextTurnContext) -> AgentLoopTurnUpdate | None:
            if self.prepareNextTurnWithContext:
                return await maybe_await(self.prepareNextTurnWithContext(context, self.signal))
            return await maybe_await(self.prepareNextTurn(self.signal)) if self.prepareNextTurn else None

        async def getSteeringMessages() -> list[AgentMessage]:
            nonlocal skipPoll
            if skipPoll:
                skipPoll = False
                return []
            return self._steeringQueue.drain()

        async def getFollowUpMessages() -> list[AgentMessage]:
            return self._followUpQueue.drain()

        return AgentLoopConfig(
            model=self._state.model,
            reasoning=None if self._state.thinkingLevel == "off" else self._state.thinkingLevel,
            sessionId=self.sessionId,
            onPayload=self.onPayload,
            onResponse=self.onResponse,
            onProviderStreamEvent=self.onProviderStreamEvent,
            transport=self.transport,
            thinkingBudgets=self.thinkingBudgets,
            maxRetryDelayMs=self.maxRetryDelayMs,
            toolExecution=self.toolExecution,
            beforeToolCall=self.beforeToolCall,
            afterToolCall=self.afterToolCall,
            finishTurn=self.finishTurn,
            prepareRequest=self.prepareRequest,
            prepareNextTurn=prepareNextTurn if self.prepareNextTurnWithContext or self.prepareNextTurn else None,
            convertToLlm=self.convertToLlm,
            transformContext=self.transformContext,
            getApiKey=self.getApiKey,
            getSteeringMessages=getSteeringMessages,
            getFollowUpMessages=getFollowUpMessages,
        )

    async def _runWithLifecycle(self, executor: Callable[[AbortSignal], Awaitable[None]]) -> None:
        if self._activeRun is not None:
            raise RuntimeError("Agent is already processing.")

        abortController = AbortController()
        self._activeRun = _ActiveRun(promise=asyncio.get_running_loop().create_future(), abortController=abortController)

        self._state.isStreaming = True
        self._state.streamingMessage = None
        self._state.errorMessage = None

        try:
            await executor(abortController.signal)
        except BaseException as error:
            # MISAKA fork: KeyboardInterrupt and SystemExit are not the model erroring (JavaScript has no such exits),
            # and a cancellation that is not our own abort belongs to the caller, so none of them is answered here.
            if isinstance(error, KeyboardInterrupt | SystemExit):
                raise
            if isinstance(error, asyncio.CancelledError) and not abortController.signal.aborted:
                raise
            await self._handleRunFailure(error, abortController.signal.aborted)
        finally:
            self._finishRun()

    async def _handleRunFailure(self, error: BaseException, aborted: bool) -> None:
        failureMessage = AssistantMessage(
            content=[TextContent(text="")],
            api=self._state.model.api,
            provider=self._state.model.provider,
            model=self._state.model.id,
            usage=Usage.model_validate(_EMPTY_USAGE),
            stopReason="aborted" if aborted else "error",
            errorMessage=errorMessage(error),
            timestamp=int(time.time() * 1000),
        )
        await self._processEvents(MessageStartEvent(message=failureMessage))
        await self._processEvents(MessageEndEvent(message=failureMessage))
        await self._processEvents(TurnEndEvent(message=failureMessage, toolResults=[]))
        await self._processEvents(AgentEndEvent(messages=[failureMessage]))

    def _finishRun(self) -> None:
        self._state.isStreaming = False
        self._state.streamingMessage = None
        self._state.pendingToolCalls = set()
        activeRun = self._activeRun
        self._activeRun = None
        if activeRun is not None and not activeRun.promise.done():
            activeRun.promise.set_result(None)

    async def _processEvents(self, event: AgentEvent) -> None:
        """Reduce internal state for a loop event, then await listeners.

        `agent_end` only means no further loop events will be emitted. The run is
        considered idle later, after all awaited listeners for `agent_end` finish
        and `finishRun()` clears runtime-owned state.
        """
        match event.type:
            case "message_start":
                self._state.streamingMessage = event.message

            case "message_update":
                self._state.streamingMessage = event.message

            case "message_end":
                self._state.streamingMessage = None
                # MISAKA fork: an LLM message given as a mapping enters the transcript as its message model.
                self._state.messages.append(_normalizeStandardMessage(event.message))

            case "tool_execution_start":
                pendingToolCalls = set(self._state.pendingToolCalls)
                pendingToolCalls.add(event.toolCallId)
                self._state.pendingToolCalls = pendingToolCalls

            case "tool_execution_end":
                pendingToolCalls = set(self._state.pendingToolCalls)
                pendingToolCalls.discard(event.toolCallId)
                self._state.pendingToolCalls = pendingToolCalls

            case "turn_end":
                if read_field(event.message, "role") == "assistant" and read_field(event.message, "errorMessage"):
                    self._state.errorMessage = read_field(event.message, "errorMessage")

            case "agent_end":
                self._state.streamingMessage = None

        signal = self._activeRun.abortController.signal if self._activeRun is not None else None
        if not signal:
            raise RuntimeError("Agent listener invoked outside active run")
        for listener in liveKeys(self._listeners):
            await maybe_await(listener(event, signal))


def _agentOptions(options: AgentOptions | Mapping[str, Any] | None, kwargs: dict[str, Any]) -> AgentOptions:
    """MISAKA fork: `AgentOptions`, a mapping of its members, or keyword arguments -- one of the three."""
    if isinstance(options, AgentOptions):
        if kwargs:
            raise TypeError("Use either an AgentOptions object or keyword arguments, not both.")
        return options
    if options is None:
        return AgentOptions(**kwargs)
    if kwargs:
        raise TypeError("Use either an options mapping or keyword arguments, not both.")
    return AgentOptions(**dict(options))


__all__ = ["Agent", "AgentInitialState", "AgentListener", "AgentOptions", "QueueMode"]
