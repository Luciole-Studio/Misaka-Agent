"""Agent loop that works with AgentMessage throughout.
Transforms to Message[] only at the LLM call boundary.

Translated from pi's ``packages/agent/src/agent-loop.ts`` (v1.3.0).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from types import SimpleNamespace
from typing import Any, Literal

from misaka.agent.stream_fn import getDefaultStreamFn
from misaka.agent.types import (
    AfterToolCallContext,
    AfterToolCallResult,
    AgentContext,
    AgentEndEvent,
    AgentEvent,
    AgentLoopConfig,
    AgentMessage,
    AgentStartEvent,
    AgentTool,
    AgentToolCall,
    AgentToolCallOutcome,
    AgentToolResult,
    AgentTurnContext,
    BeforeToolCallContext,
    BeforeToolCallResult,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    PrepareNextTurnContext,
    PrepareRequestContext,
    StreamFn,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from misaka.ai.types import (
    AssistantMessage,
    Context,
    MessageValue,
    SystemMessage,
    TextContent,
    ToolResultMessage,
    validate_message,
    validate_user_content,
)
from misaka.ai.utils.event_stream import EventStream, spawn_stream_task
from misaka.ai.utils.transcript import (
    ToolStateChanges,
    getCurrentTools,
    getToolStateChanges,
    normalizeContext,
    toToolDeclaration,
)
from misaka.ai.utils.validation import validateToolArguments
from misaka.utils.js import UNDEFINED, errorMessage, jsRound, startTask
from misaka.utils.values import maybe_await, read_field, signal_aborted

type AgentEventSink = Callable[[AgentEvent], Awaitable[None] | None]


def agentLoop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any | None,
    streamFn: StreamFn | None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """Start an agent loop with a new prompt message.
    The prompt is added to the context and events are emitted for it.
    """
    stream = _createAgentStream()

    async def push(event: AgentEvent) -> None:
        stream.push(event)

    async def run() -> None:
        try:
            stream.end(await runAgentLoop(prompts, context, config, push, signal, streamFn))
        except BaseException as error:  # noqa: BLE001 - handed to the stream's consumer
            _failStream(stream, error)

    # MISAKA fork: the task is held until it finishes (the event loop keeps only a weak reference to a task).
    spawn_stream_task(run())
    return stream


def agentLoopContinue(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any | None,
    streamFn: StreamFn | None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    """Continue an agent loop from the current context without adding a new message.
    Used for retries - context already has user message or tool results.

    **Important:** The last message in context must convert to a `user` or `toolResult` message
    via `convertToLlm`. If it doesn't, the LLM provider will reject the request.
    This cannot be validated here since `convertToLlm` is only called once per turn.
    """
    if len(context.messages) == 0:
        raise RuntimeError("Cannot continue: no messages in context")

    if _role(context.messages[-1]) == "assistant":
        raise RuntimeError("Cannot continue from message role: assistant")

    stream = _createAgentStream()

    async def push(event: AgentEvent) -> None:
        stream.push(event)

    async def run() -> None:
        try:
            stream.end(await runAgentLoopContinue(context, config, push, signal, streamFn))
        except BaseException as error:  # noqa: BLE001 - handed to the stream's consumer
            _failStream(stream, error)

    spawn_stream_task(run())
    return stream


async def runAgentLoop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any | None,
    streamFn: StreamFn | None,
) -> list[AgentMessage]:
    initialMessages = _declareToolChanges(context, prompts)
    newMessages: list[AgentMessage] = [*initialMessages]
    currentContext = AgentContext(messages=[*context.messages, *initialMessages], tools=context.tools)

    await _emit(emit, AgentStartEvent())
    await _emit(emit, TurnStartEvent())
    for message in initialMessages:
        await _emit(emit, MessageStartEvent(message=message))
        await _emit(emit, MessageEndEvent(message=message))

    await _runLoop(currentContext, newMessages, config, signal, emit, streamFn or getDefaultStreamFn())
    return newMessages


async def runAgentLoopContinue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any | None,
    streamFn: StreamFn | None,
) -> list[AgentMessage]:
    if len(context.messages) == 0:
        raise RuntimeError("Cannot continue: no messages in context")

    if _role(context.messages[-1]) == "assistant":
        raise RuntimeError("Cannot continue from message role: assistant")

    newMessages: list[AgentMessage] = []
    currentContext = AgentContext(messages=context.messages, tools=context.tools)

    await _emit(emit, AgentStartEvent())
    await _emit(emit, TurnStartEvent())

    await _runLoop(currentContext, newMessages, config, signal, emit, streamFn or getDefaultStreamFn())
    return newMessages


def _failStream(stream: EventStream[AgentEvent, list[AgentMessage]], error: BaseException) -> None:
    """MISAKA fork: upstream leaves the stream open when the loop rejects (an unhandled rejection); here the failure
    reaches `result()` and the stream ends, so a consumer does not wait forever."""
    result = stream.result()
    if not result.done():
        result.set_exception(error)
    stream.end([])


def _createAgentStream() -> EventStream[AgentEvent, list[AgentMessage]]:
    return EventStream(
        lambda event: event.type == "agent_end",
        lambda event: event.messages if event.type == "agent_end" else [],
    )


def _thinkingLevelOf(update: Any, current: Any) -> Any:
    """``update.thinkingLevel === undefined ? config.reasoning : update.thinkingLevel === "off" ? undefined : ...``."""
    if update.thinkingLevel is None:
        return current
    return None if update.thinkingLevel == "off" else update.thinkingLevel


async def _runLoop(
    initialContext: AgentContext,
    newMessages: list[AgentMessage],
    initialConfig: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
    streamFunction: StreamFn,
) -> None:
    """Main loop logic shared by agentLoop and agentLoopContinue."""
    currentContext = initialContext
    config = initialConfig
    lastCompletedTurn: PrepareNextTurnContext | None = None
    explicitContinuation = False
    # Check for steering messages at start (user may have typed while waiting)
    pendingMessages: list[AgentMessage] = list(
        (await maybe_await(config.getSteeringMessages()) if config.getSteeringMessages else None) or []
    )

    # Outer loop: continues when queued follow-up messages arrive after agent would stop
    while True:
        hasMoreToolCalls = True

        # Inner loop: process tool calls and steering messages
        while hasMoreToolCalls or len(pendingMessages) > 0:
            preparedMessages: list[AgentMessage] = []
            if lastCompletedTurn is not None:
                nextTurnSnapshot = (
                    await maybe_await(config.prepareNextTurn(lastCompletedTurn)) if config.prepareNextTurn else None
                )
                if nextTurnSnapshot:
                    currentContext = nextTurnSnapshot.context or currentContext
                    preparedMessages = list(nextTurnSnapshot.messages or [])
                    config = replace(
                        config,
                        model=nextTurnSnapshot.model or config.model,
                        reasoning=_thinkingLevelOf(nextTurnSnapshot, config.reasoning),
                    )
                # Preparation can be long-running (for example, compaction). Pick up steering
                # queued while it ran. Only poll again if the earlier poll returned nothing;
                # otherwise one-at-a-time mode would deliver two messages in this turn.
                if len(pendingMessages) == 0:
                    pendingMessages = list(
                        (await maybe_await(config.getSteeringMessages()) if config.getSteeringMessages else None) or []
                    )
                await _emit(emit, TurnStartEvent())

            # Process prepared and queued messages before the next assistant response.
            for message in _declareToolChanges(currentContext, [*preparedMessages, *pendingMessages]):
                await _emit(emit, MessageStartEvent(message=message))
                await _emit(emit, MessageEndEvent(message=message))
                currentContext.messages.append(message)
                newMessages.append(message)
            pendingMessages = []

            requestUpdate = (
                await maybe_await(
                    config.prepareRequest(
                        PrepareRequestContext(
                            context=currentContext,
                            model=config.model,
                            thinkingLevel=config.reasoning if config.reasoning is not None else "off",
                        ),
                        signal,
                    )
                )
                if config.prepareRequest
                else None
            )
            if requestUpdate:
                currentContext = requestUpdate.context or currentContext
                config = replace(
                    config,
                    model=requestUpdate.model or config.model,
                    reasoning=_thinkingLevelOf(requestUpdate, config.reasoning),
                )

            # Stream assistant response
            message = await _streamAssistantResponse(currentContext, config, signal, emit, streamFunction)
            newMessages.append(message)

            if message.stopReason in ("error", "aborted"):
                lastCompletedTurn = AgentTurnContext(
                    message=message,
                    toolResults=[],
                    context=currentContext,
                    newMessages=newMessages,
                )
                if config.finishTurn:
                    await maybe_await(config.finishTurn(lastCompletedTurn, signal))
                await _emit(emit, TurnEndEvent(message=message, toolResults=[]))
                await _emit(emit, AgentEndEvent(messages=newMessages))
                return

            # Check for tool calls
            toolCalls = [content for content in message.content if content.type == "toolCall"]

            toolResults: list[ToolResultMessage] = []
            hasMoreToolCalls = False
            if len(toolCalls) > 0:
                # A "length" stop means the output was cut off by the token limit, so
                # every tool call in the message may carry truncated arguments. Fail
                # them all instead of executing potentially borked calls.
                executedToolBatch = (
                    await _failToolCallsFromTruncatedMessage(toolCalls, emit)
                    if message.stopReason == "length"
                    else await _executeToolCalls(currentContext, message, config, signal, emit)
                )
                toolResults.extend(executedToolBatch.messages)
                hasMoreToolCalls = not executedToolBatch.terminate

                for result in toolResults:
                    currentContext.messages.append(result)
                    newMessages.append(result)

            lastCompletedTurn = AgentTurnContext(
                message=message,
                toolResults=toolResults,
                context=currentContext,
                newMessages=newMessages,
            )
            decision = await maybe_await(config.finishTurn(lastCompletedTurn, signal)) if config.finishTurn else None
            await _emit(emit, TurnEndEvent(message=message, toolResults=toolResults))

            if read_field(decision, "action") == "end":
                await _emit(emit, AgentEndEvent(messages=newMessages))
                return

            explicitContinuation = read_field(decision, "action") == "continue"
            pendingMessages = list(
                (await maybe_await(config.getSteeringMessages()) if config.getSteeringMessages else None) or []
            )
            if hasMoreToolCalls or len(pendingMessages) > 0:
                explicitContinuation = False

        # Agent would stop here. Check for follow-up messages.
        followUpMessages = list(
            (await maybe_await(config.getFollowUpMessages()) if config.getFollowUpMessages else None) or []
        )
        if len(followUpMessages) > 0:
            # Set as pending so inner loop processes them
            explicitContinuation = False
            pendingMessages = followUpMessages
            continue

        # No natural request was selected, so fulfill the continuation decision with one context-only turn.
        if explicitContinuation:
            explicitContinuation = False
            continue

        # No more messages, exit
        break

    await _emit(emit, AgentEndEvent(messages=newMessages))


def _declareToolChanges(context: AgentContext, pendingMessages: list[AgentMessage]) -> list[AgentMessage]:
    """Declare tool loadout changes to the model.

    `context.tools` is what the runtime can execute; the transcript's system messages declare
    what the model may call. Before each request the difference becomes `toolsAdded` and
    `toolsRemoved` on a system message. When a pending system message exists, its tool fields
    are treated as intent and replaced with the delta between the committed transcript and
    the executable set, so replay always yields exactly `context.tools`. Otherwise a new
    system message is inserted before the first non-system pending message.
    """
    systemIndex = -1
    for i in range(len(pendingMessages) - 1, -1, -1):
        if _role(pendingMessages[i]) == "system":
            systemIndex = i
            break
    pending: Any = pendingMessages[systemIndex] if systemIndex >= 0 else None
    baseline = (
        [
            _withToolChanges(pending, _NO_CHANGES) if index == systemIndex else message
            for index, message in enumerate(pendingMessages)
        ]
        if pending is not None
        else pendingMessages
    )
    changes = getToolStateChanges(
        getCurrentTools([*context.messages, *baseline]),
        [toToolDeclaration(tool) for tool in context.tools or []],
    )
    unchanged = len(changes.toolsAdded) == 0 and len(changes.toolsRemoved) == 0

    if pending is not None:
        # Keep the caller's message object when it already declares no tool changes.
        if unchanged and not pending.toolsAdded and not pending.toolsRemoved:
            return pendingMessages
        return [_withToolChanges(pending, changes) if index == systemIndex else message for index, message in enumerate(baseline)]
    if unchanged:
        return pendingMessages
    update = _withToolChanges(SystemMessage(content="", timestamp=int(time.time() * 1000)), changes)
    insertIndex = next((index for index, message in enumerate(pendingMessages) if _role(message) != "system"), -1)
    index = len(pendingMessages) if insertIndex == -1 else insertIndex
    return [*pendingMessages[:index], update, *pendingMessages[index:]]


_NO_CHANGES = ToolStateChanges(toolsAdded=[], toolsRemoved=[])


def _withToolChanges(message: SystemMessage, changes: ToolStateChanges) -> SystemMessage:
    """Copy a system message with its tool fields replaced by `changes`; empty lists omit the field."""
    return message.model_copy(
        update={
            "toolsAdded": list(changes.toolsAdded) if len(changes.toolsAdded) > 0 else None,
            "toolsRemoved": list(changes.toolsRemoved) if len(changes.toolsRemoved) > 0 else None,
        }
    )


class _StreamOptions(SimpleNamespace):
    """MISAKA fork: ``{ ...config, apiKey, signal }`` as an object the adapters read by attribute, which also spreads
    like a mapping (``dict(options)``, ``**options``) as the JavaScript object does.

    A missing option is missing: providers read ``getattr(options, name, default)``, so there is no ``__getattr__``
    answering None.
    """

    def keys(self):
        return self.__dict__.keys()

    def __getitem__(self, key: str) -> Any:
        try:
            return self.__dict__[key]
        except KeyError:
            raise KeyError(key) from None

    def __contains__(self, key: object) -> bool:
        return key in self.__dict__

    def __iter__(self):
        return iter(self.__dict__)

    def __len__(self) -> int:
        return len(self.__dict__)

    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
        payload = dict(self.__dict__)
        if exclude_none:
            return {key: value for key, value in payload.items() if value is not None}
        return payload


def _spread(value: Any) -> dict[str, Any]:
    """MISAKA fork: ``{ ...config }`` for the loop config (a dataclass) or a mapping; a member left ``UNDEFINED`` is
    not spread."""
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if item is not UNDEFINED}
    if is_dataclass(value):
        return {field.name: getattr(value, field.name) for field in fields(value) if getattr(value, field.name) is not UNDEFINED}
    return {key: item for key, item in vars(value).items() if item is not UNDEFINED}


async def _streamAssistantResponse(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
    streamFunction: StreamFn,
) -> AssistantMessage:
    """Stream an assistant response from the LLM.
    This is where AgentMessage[] gets transformed to Message[] for the LLM.
    """
    # Apply context transform if configured (AgentMessage[] → AgentMessage[])
    messages = context.messages
    if config.transformContext:
        messages = list(await maybe_await(config.transformContext(messages, signal)))

    # Convert to LLM-compatible messages (AgentMessage[] → Message[])
    llmMessages = list(await maybe_await(config.convertToLlm(messages)))

    # MISAKA fork: `convertToLlm` may hand back mappings or messages of other model classes; they are validated into
    # the message models the adapters read (message models pass through as they are).
    llmContext = normalizeContext(
        Context(
            messages=[
                message if isinstance(message, MessageValue) else validate_message(_modelDump(message))
                for message in llmMessages
            ]
        )
    )

    # Resolve API key (important for expiring tokens)
    resolvedApiKey = (
        await maybe_await(config.getApiKey(config.model.provider)) if config.getApiKey else None
    ) or config.apiKey

    response = await maybe_await(
        streamFunction(config.model, llmContext, _StreamOptions(**{**_spread(config), "apiKey": resolvedApiKey, "signal": signal}))
    )

    # Record the requested level, whichever stream function answered.
    async def result() -> AssistantMessage:
        message = await response.result()
        message.thinkingLevel = config.reasoning if config.reasoning is not None else "off"
        return message

    partialMessage: AssistantMessage | None = None
    addedPartial = False

    async for event in response:
        match event.type:
            case "start":
                partialMessage = event.partial
                context.messages.append(partialMessage)
                addedPartial = True
                await _emit(emit, MessageStartEvent(message=partialMessage.model_copy()))

            case (
                "text_start"
                | "text_delta"
                | "text_end"
                | "thinking_start"
                | "thinking_delta"
                | "thinking_end"
                | "toolcall_start"
                | "toolcall_delta"
                | "toolcall_end"
            ):
                if partialMessage is not None:
                    partialMessage = event.partial
                    context.messages[-1] = partialMessage
                    await _emit(
                        emit,
                        MessageUpdateEvent(
                            assistantMessageEvent=event,
                            message=partialMessage.model_copy(),
                        ),
                    )

            case "done" | "error":
                finalMessage = _uniquifyToolCallIds(await result())
                if addedPartial:
                    context.messages[-1] = finalMessage
                else:
                    context.messages.append(finalMessage)
                if not addedPartial:
                    await _emit(emit, MessageStartEvent(message=finalMessage.model_copy()))
                await _emit(emit, MessageEndEvent(message=finalMessage))
                return finalMessage

    finalMessage = _uniquifyToolCallIds(await result())
    if addedPartial:
        context.messages[-1] = finalMessage
    else:
        context.messages.append(finalMessage)
        await _emit(emit, MessageStartEvent(message=finalMessage.model_copy()))
    await _emit(emit, MessageEndEvent(message=finalMessage))
    return finalMessage


def _uniquifyToolCallIds(message: AssistantMessage) -> AssistantMessage:
    """MISAKA fork: Hermes' per-turn tool-call ID normalization (not action deduplication), applied before the
    message is persisted at `message_end`. A duplicate ID gets a `_d<n>` suffix; Responses item-id suffixes after `|`
    are kept."""
    seen: set[str] = set()
    for index, block in enumerate(message.content):
        if block.type != "toolCall":
            continue
        callId = block.id.strip().split("|", 1)[0]
        if not callId:
            continue
        if callId in seen:
            callId = next(f"{callId}_d{n}" for n in range(2, len(seen) + 3) if f"{callId}_d{n}" not in seen)
            _, separator, itemId = block.id.partition("|")
            message.content[index] = block.model_copy(update={"id": callId + separator + itemId})
        seen.add(callId)
    return message


async def _failToolCallsFromTruncatedMessage(
    toolCalls: list[AgentToolCall],
    emit: AgentEventSink,
) -> _ExecutedToolCallBatch:
    """Fail all tool calls from an assistant message that was truncated by the
    output token limit. Streamed tool-call arguments are finalized with a
    best-effort JSON salvage parser, so a truncated message can yield tool calls
    whose arguments parse and validate but are silently incomplete. None of them
    are safe to execute; report each as an error so the model can re-issue them.
    """
    messages: list[ToolResultMessage] = []
    for toolCall in toolCalls:
        await _emit(
            emit,
            ToolExecutionStartEvent(toolCallId=toolCall.id, toolName=toolCall.name, args=toolCall.arguments),
        )
        finalized = _FinalizedToolCallOutcome(
            toolCall=toolCall,
            result=_createErrorToolResult(
                f'Tool call "{toolCall.name}" was not executed: the response hit the output token limit, so its '
                "arguments may be truncated. Re-issue the tool call with complete arguments."
            ),
            isError=True,
        )
        await _emitToolExecutionEnd(finalized, emit)
        toolResultMessage = _createToolResultMessage(finalized)
        await _emitToolResultMessage(toolResultMessage, emit)
        messages.append(toolResultMessage)
    return _ExecutedToolCallBatch(messages=messages, terminate=False)


async def _executeToolCalls(
    currentContext: AgentContext,
    assistantMessage: AssistantMessage,
    config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
) -> _ExecutedToolCallBatch:
    """Execute tool calls from an assistant message."""
    # MISAKA fork: duplicate tool-call IDs are normalized before any result is keyed by them.
    _uniquifyToolCallIds(assistantMessage)
    toolCalls = [content for content in assistantMessage.content if content.type == "toolCall"]
    hasSequentialToolCall = any(
        getattr(next((t for t in currentContext.tools or [] if t.name == tc.name), None), "executionMode", None)
        == "sequential"
        for tc in toolCalls
    )
    if config.toolExecution == "sequential" or hasSequentialToolCall:
        return await _executeToolCallsSequential(currentContext, assistantMessage, toolCalls, config, signal, emit)
    return await _executeToolCallsParallel(currentContext, assistantMessage, toolCalls, config, signal, emit)


@dataclass(slots=True)
class _ExecutedToolCallBatch:
    messages: list[ToolResultMessage]
    terminate: bool


async def _executeToolCallsSequential(
    currentContext: AgentContext,
    assistantMessage: AssistantMessage,
    toolCalls: list[AgentToolCall],
    config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
) -> _ExecutedToolCallBatch:
    finalizedCalls: list[_FinalizedToolCallOutcome] = []
    messages: list[ToolResultMessage] = []

    for toolCall in toolCalls:
        await _emit(
            emit,
            ToolExecutionStartEvent(toolCallId=toolCall.id, toolName=toolCall.name, args=toolCall.arguments),
        )

        preparation = await _prepareToolCall(currentContext, assistantMessage, toolCall, config, signal)
        if preparation.kind == "immediate":
            finalized = _FinalizedToolCallOutcome(
                toolCall=toolCall,
                result=preparation.result,
                isError=preparation.isError,
            )
        else:
            executed = await _executePreparedToolCall(preparation, signal, _emitToolExecutionUpdate(toolCall, emit))
            finalized = await _finalizeExecutedToolCall(
                currentContext,
                assistantMessage,
                preparation,
                executed,
                config,
                signal,
            )

        await _emitToolExecutionEnd(finalized, emit)
        toolResultMessage = _createToolResultMessage(finalized)
        await _emitToolResultMessage(toolResultMessage, emit)
        finalizedCalls.append(finalized)
        messages.append(toolResultMessage)

        if signal_aborted(signal):
            break

    return _ExecutedToolCallBatch(
        messages=messages,
        terminate=_shouldTerminateToolBatch(finalizedCalls),
    )


async def _executeToolCallsParallel(
    currentContext: AgentContext,
    assistantMessage: AssistantMessage,
    toolCalls: list[AgentToolCall],
    config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
) -> _ExecutedToolCallBatch:
    finalizedCalls: list[_FinalizedToolCallEntry] = []

    for toolCall in toolCalls:
        await _emit(
            emit,
            ToolExecutionStartEvent(toolCallId=toolCall.id, toolName=toolCall.name, args=toolCall.arguments),
        )

        preparation = await _prepareToolCall(currentContext, assistantMessage, toolCall, config, signal)
        if preparation.kind == "immediate":
            finalized = _FinalizedToolCallOutcome(
                toolCall=toolCall,
                result=preparation.result,
                isError=preparation.isError,
            )
            await _emitToolExecutionEnd(finalized, emit)
            finalizedCalls.append(finalized)
            if signal_aborted(signal):
                break
            continue

        async def entry(
            preparation: _PreparedToolCall = preparation, toolCall: AgentToolCall = toolCall
        ) -> _FinalizedToolCallOutcome:
            if signal_aborted(signal):
                finalized = _FinalizedToolCallOutcome(
                    toolCall=toolCall,
                    result=_createErrorToolResult("Operation aborted"),
                    isError=True,
                )
                await _emitToolExecutionEnd(finalized, emit)
                return finalized
            executed = await _executePreparedToolCall(preparation, signal, _emitToolExecutionUpdate(toolCall, emit))
            finalized = await _finalizeExecutedToolCall(
                currentContext,
                assistantMessage,
                preparation,
                executed,
                config,
                signal,
            )
            await _emitToolExecutionEnd(finalized, emit)
            return finalized

        finalizedCalls.append(entry)
        if signal_aborted(signal):
            break

    orderedFinalizedCalls = await asyncio.gather(
        *(entry() if callable(entry) else _resolved(entry) for entry in finalizedCalls)
    )
    messages: list[ToolResultMessage] = []
    for finalized in orderedFinalizedCalls:
        toolResultMessage = _createToolResultMessage(finalized)
        await _emitToolResultMessage(toolResultMessage, emit)
        messages.append(toolResultMessage)

    return _ExecutedToolCallBatch(
        messages=messages,
        terminate=_shouldTerminateToolBatch(orderedFinalizedCalls),
    )


async def _resolved(value: _FinalizedToolCallOutcome) -> _FinalizedToolCallOutcome:
    """``Promise.resolve(entry)``."""
    return value


@dataclass(slots=True)
class _PreparedToolCall:
    toolCall: AgentToolCall
    tool: AgentTool
    args: Any
    kind: Literal["prepared"] = "prepared"


@dataclass(slots=True)
class _ImmediateToolCallOutcome:
    result: AgentToolResult
    isError: bool
    kind: Literal["immediate"] = "immediate"


@dataclass(slots=True)
class _ExecutedToolCallOutcome:
    result: AgentToolResult
    isError: bool
    durationMs: int


_FinalizedToolCallOutcome = AgentToolCallOutcome

# The `beforeToolCall` and `afterToolCall` hooks of `AgentLoopConfig`.
# MISAKA fork: upstream's `Pick<AgentLoopConfig, "beforeToolCall" | "afterToolCall">` is anything carrying the two.
type ToolCallHooks = Any

type _ToolUpdateSink = Callable[[AgentToolResult], Awaitable[None] | None]

type _FinalizedToolCallEntry = _FinalizedToolCallOutcome | Callable[[], Awaitable[_FinalizedToolCallOutcome]]


def _shouldTerminateToolBatch(finalizedCalls: list[_FinalizedToolCallOutcome]) -> bool:
    return len(finalizedCalls) > 0 and all(finalized.result.terminate is True for finalized in finalizedCalls)


def _prepareToolCallArguments(tool: AgentTool, toolCall: AgentToolCall) -> AgentToolCall:
    if not tool.prepareArguments:
        return toolCall
    preparedArguments = tool.prepareArguments(toolCall.arguments)
    if preparedArguments is toolCall.arguments:
        return toolCall
    return toolCall.model_copy(update={"arguments": preparedArguments})


def _reraiseIfNotToolFailure(error: BaseException, signal: Any | None) -> None:
    """MISAKA fork: let interpreter-level exits and a caller's cancellation through.

    The loop turns any failure into an error tool result, which is right for a tool that failed but wrong for
    ``CancelledError``: swallowing it means the caller's ``task.cancel()`` never arrives and the loop keeps running.
    When our own abort signal is set the cancellation is ours, and the aborted result is the honest answer.
    ``KeyboardInterrupt`` and ``SystemExit`` are never a tool failure either (JavaScript has no equivalent of the
    two), so Ctrl-C inside a tool does not turn into "the model errored" while the process keeps running.
    """
    if isinstance(error, KeyboardInterrupt | SystemExit):
        raise error
    if isinstance(error, asyncio.CancelledError) and not signal_aborted(signal):
        raise error


async def _prepareToolCall(
    currentContext: AgentContext,
    assistantMessage: AssistantMessage,
    toolCall: AgentToolCall,
    config: ToolCallHooks,
    signal: Any | None,
    tools: list[AgentTool] | None = None,
) -> _PreparedToolCall | _ImmediateToolCallOutcome:
    if tools is None:
        tools = currentContext.tools or []
    tool = next((t for t in tools if t.name == toolCall.name), None)
    if tool is None:
        # MISAKA fork: a legacy alias resolves to its tool. Exact names win; permission and schema checks see the
        # canonical tool, while the transcript keeps its historical wire call and call ID.
        tool = next((t for t in tools if toolCall.name in getattr(t, "aliases", ())), None)
        if tool is not None:
            toolCall = toolCall.model_copy(update={"name": tool.name})
    if tool is None:
        return _ImmediateToolCallOutcome(
            result=_createErrorToolResult(f"Tool {toolCall.name} not found"),
            isError=True,
        )

    try:
        preparedToolCall = _prepareToolCallArguments(tool, toolCall)
        validatedArgs = validateToolArguments(tool, preparedToolCall)
        if config.beforeToolCall:
            beforeResult = _coerceBeforeToolCallResult(
                await maybe_await(
                    config.beforeToolCall(
                        BeforeToolCallContext(
                            assistantMessage=assistantMessage,
                            toolCall=toolCall,
                            args=validatedArgs,
                            context=currentContext,
                        ),
                        signal,
                    )
                )
            )
            if signal_aborted(signal):
                return _ImmediateToolCallOutcome(
                    result=_createErrorToolResult("Operation aborted"),
                    isError=True,
                )
            if beforeResult is not None and beforeResult.block:
                result = _createErrorToolResult(beforeResult.reason or "Tool execution was blocked")
                if beforeResult.terminate is True:
                    result.terminate = True
                return _ImmediateToolCallOutcome(result=result, isError=True)
            # MISAKA fork: CCB PreToolUse.updatedInput replaces the arguments, validated again. A call whose own
            # arguments failed to parse never gets here: validateToolArguments refused it above.
            if beforeResult is not None and beforeResult.updatedInput is not None:
                validatedArgs = validateToolArguments(
                    tool, toolCall.model_copy(update={"arguments": beforeResult.updatedInput})
                )
        if signal_aborted(signal):
            return _ImmediateToolCallOutcome(
                result=_createErrorToolResult("Operation aborted"),
                isError=True,
            )
        return _PreparedToolCall(toolCall=toolCall, tool=tool, args=validatedArgs)
    except BaseException as error:  # noqa: BLE001 - `catch (error)`; see `_reraiseIfNotToolFailure`
        _reraiseIfNotToolFailure(error, signal)
        return _ImmediateToolCallOutcome(result=_createErrorToolResult(errorMessage(error)), isError=True)


def _emitToolExecutionUpdate(toolCall: AgentToolCall, emit: AgentEventSink) -> _ToolUpdateSink:
    def sink(partialResult: AgentToolResult) -> Awaitable[None] | None:
        return emit(
            ToolExecutionUpdateEvent(
                toolCallId=toolCall.id,
                toolName=toolCall.name,
                args=toolCall.arguments,
                partialResult=partialResult,
            )
        )

    return sink


@dataclass(slots=True, kw_only=True)
class RunToolCallOptions:
    """Options for `runToolCall`."""

    # Tools the call resolves against.
    tools: list[AgentTool]
    # Passed to the hooks as the message that issued the call.
    assistantMessage: AssistantMessage
    # Passed to the hooks as the current agent context.
    context: AgentContext
    beforeToolCall: Any | None = None
    afterToolCall: Any | None = None
    signal: Any | None = None
    onUpdate: _ToolUpdateSink | None = None


async def runToolCall(toolCall: AgentToolCall, options: RunToolCallOptions) -> AgentToolCallOutcome:
    """Run one tool call through the same steps as a model-issued call: argument preparation, schema
    validation, `beforeToolCall`, execution, and `afterToolCall`. Emits no events and adds no
    messages. Tools that call other tools use this so the hooks (for example permission checks)
    apply to those calls too.

    Never rejects for tool failures: unknown tools, validation errors, blocked calls, and thrown
    errors come back as `isError: true`.
    """
    assistantMessage, context, signal = options.assistantMessage, options.context, options.signal
    preparation = await _prepareToolCall(context, assistantMessage, toolCall, options, signal, options.tools)
    if preparation.kind == "immediate":
        return AgentToolCallOutcome(toolCall=toolCall, result=preparation.result, isError=preparation.isError)
    executed = await _executePreparedToolCall(
        preparation, signal, options.onUpdate if options.onUpdate is not None else (lambda _partialResult: None)
    )
    return await _finalizeExecutedToolCall(context, assistantMessage, preparation, executed, options, signal)


async def _executePreparedToolCall(
    prepared: _PreparedToolCall,
    signal: Any | None,
    onUpdate: _ToolUpdateSink,
) -> _ExecutedToolCallOutcome:
    updateEvents: list[asyncio.Future[Any]] = []
    acceptingUpdates = True
    startedAt = time.perf_counter()

    def elapsed() -> int:
        return jsRound((time.perf_counter() - startedAt) * 1000)

    def update(partialResult: AgentToolResult) -> None:
        if not acceptingUpdates:
            return
        updateEvents.append(startTask(maybe_await(onUpdate(partialResult))))

    try:
        result = _coerceAgentToolResult(
            await maybe_await(prepared.tool.execute(prepared.toolCall.id, prepared.args, signal, update))
        )
        durationMs = elapsed()
        acceptingUpdates = False
        await asyncio.gather(*updateEvents)
        return _ExecutedToolCallOutcome(result=result, isError=result.isError is True, durationMs=durationMs)
    except BaseException as error:  # noqa: BLE001 - `catch (error)`; see `_reraiseIfNotToolFailure`
        durationMs = elapsed()
        acceptingUpdates = False
        await asyncio.gather(*updateEvents, return_exceptions=True)
        _reraiseIfNotToolFailure(error, signal)
        return _ExecutedToolCallOutcome(
            result=_createErrorToolResult(errorMessage(error)),
            isError=True,
            durationMs=durationMs,
        )
    finally:
        acceptingUpdates = False


async def _finalizeExecutedToolCall(
    currentContext: AgentContext,
    assistantMessage: AssistantMessage,
    prepared: _PreparedToolCall,
    executed: _ExecutedToolCallOutcome,
    config: ToolCallHooks,
    signal: Any | None,
) -> _FinalizedToolCallOutcome:
    result = executed.result
    isError = executed.isError

    if config.afterToolCall:
        try:
            afterResult = await maybe_await(
                config.afterToolCall(
                    AfterToolCallContext(
                        assistantMessage=assistantMessage,
                        toolCall=prepared.toolCall,
                        args=prepared.args,
                        result=result,
                        isError=isError,
                        context=currentContext,
                    ),
                    signal,
                )
            )
            if afterResult:
                afterResult = _coerceAfterToolCallResult(afterResult)
                # Structured content not replaced along with the content may no longer match it.
                structuredContent = (
                    afterResult.structuredContent
                    if afterResult.structuredContent is not None
                    else (None if afterResult.content is not None else result.structuredContent)
                )
                result = replace(
                    result,
                    content=afterResult.content if afterResult.content is not None else result.content,
                    details=afterResult.details if afterResult.details is not None else result.details,
                    usage=afterResult.usage if afterResult.usage is not None else result.usage,
                    terminate=afterResult.terminate if afterResult.terminate is not None else result.terminate,
                    structuredContent=structuredContent,
                )
                isError = afterResult.isError if afterResult.isError is not None else isError
        except BaseException as error:  # noqa: BLE001 - `catch (error)`; see `_reraiseIfNotToolFailure`
            _reraiseIfNotToolFailure(error, signal)
            result = _createErrorToolResult(errorMessage(error))
            isError = True

    return _FinalizedToolCallOutcome(
        toolCall=prepared.toolCall,
        result=result,
        isError=isError,
        durationMs=executed.durationMs,
    )


def _createErrorToolResult(message: str) -> AgentToolResult:
    return AgentToolResult(
        content=[TextContent(text=message)],
        details={},
    )


async def _emitToolExecutionEnd(finalized: _FinalizedToolCallOutcome, emit: AgentEventSink) -> None:
    await _emit(
        emit,
        ToolExecutionEndEvent(
            toolCallId=finalized.toolCall.id,
            toolName=finalized.toolCall.name,
            result=finalized.result,
            isError=finalized.isError,
            durationMs=finalized.durationMs,
        ),
    )


def _createToolResultMessage(finalized: _FinalizedToolCallOutcome) -> ToolResultMessage:
    return ToolResultMessage(
        toolCallId=finalized.toolCall.id,
        toolName=finalized.toolCall.name,
        # Untyped tools (JS extensions) can return results without content; normalize
        # so the null never enters session history or provider payloads.
        content=[validate_user_content(_modelDump(block)) for block in finalized.result.content or []],
        details=finalized.result.details,
        usage=finalized.result.usage,
        isError=finalized.isError,
        durationMs=finalized.durationMs,
        timestamp=int(time.time() * 1000),
    )


async def _emitToolResultMessage(toolResultMessage: ToolResultMessage, emit: AgentEventSink) -> None:
    await _emit(emit, MessageStartEvent(message=toolResultMessage))
    await _emit(emit, MessageEndEvent(message=toolResultMessage))


async def _emit(emit: AgentEventSink, event: AgentEvent) -> None:
    """MISAKA fork: ``await emit(event)`` for a sink that may be synchronous."""
    await maybe_await(emit(event))


def _role(message: Any) -> Any:
    """MISAKA fork: ``message.role`` for a message model or a custom message mapping."""
    return read_field(message, "role")


def _modelDump(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump() if callable(dump) else value


def _coerceAgentToolResult(value: AgentToolResult | dict[str, Any]) -> AgentToolResult:
    """MISAKA fork: a tool (an extension's, an MCP bridge's) may return a plain mapping."""
    if isinstance(value, AgentToolResult):
        return value
    return AgentToolResult(
        content=[validate_user_content(_modelDump(block)) for block in value.get("content") or []],
        details=value.get("details"),
        structuredContent=value.get("structuredContent"),
        usage=value.get("usage"),
        isError=value.get("isError"),
        terminate=value.get("terminate"),
    )


def _coerceAfterToolCallResult(value: AfterToolCallResult | dict[str, Any]) -> AfterToolCallResult:
    """MISAKA fork: a hook may return a plain mapping."""
    if isinstance(value, AfterToolCallResult):
        return value
    content = value.get("content")
    return AfterToolCallResult(
        content=None if content is None else [validate_user_content(_modelDump(block)) for block in content],
        details=value.get("details"),
        structuredContent=value.get("structuredContent"),
        isError=value.get("isError"),
        usage=value.get("usage"),
        terminate=value.get("terminate"),
    )


def _coerceBeforeToolCallResult(value: BeforeToolCallResult | dict[str, Any] | None) -> BeforeToolCallResult | None:
    """MISAKA fork: a hook may return a plain mapping."""
    if value is None or isinstance(value, BeforeToolCallResult):
        return value
    return BeforeToolCallResult(
        block=value.get("block"),
        reason=value.get("reason"),
        terminate=value.get("terminate"),
        updatedInput=value.get("updatedInput"),
    )


__all__ = [
    "AgentEventSink",
    "RunToolCallOptions",
    "ToolCallHooks",
    "agentLoop",
    "agentLoopContinue",
    "runAgentLoop",
    "runAgentLoopContinue",
    "runToolCall",
]
