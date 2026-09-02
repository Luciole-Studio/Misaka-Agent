"""Stateful public Agent API surface."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, overload

from pydantic import BaseModel

from misaka.agent.agent_loop import run_agent_loop, run_agent_loop_continue
from misaka.agent.stream_fn import get_default_stream_fn
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
    MessageEndEvent,
    MessageStartEvent,
    PrepareNextTurnContext,
    QueueMode,
    ShouldStopAfterTurnContext,
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
from misaka.utils.values import call_with_optional_second_arg, maybe_await


class MutableAgentState(AgentState):
    def __setattr__(self, name: str, value: Any) -> None:
        if name == "tools" and value is not None or name == "messages" and value is not None:
            value = list(value)
        elif name == "pendingToolCalls" and value is not None:
            value = set(value)
        super().__setattr__(name, value)


EMPTY_USAGE = Usage(
    input=0,
    output=0,
    cacheRead=0,
    cacheWrite=0,
    totalTokens=0,
    cost={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
)

DEFAULT_MODEL = Model(
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

AgentListener = Callable[["AgentEvent", "AbortSignal"], Awaitable[None] | None]
ConvertToLlmFn = Callable[[list[AgentMessage]], list[MessageValue] | Awaitable[list[MessageValue]]]
TransformContextFn = Callable[
    [list[AgentMessage], "AbortSignal | None"],
    list[AgentMessage] | Awaitable[list[AgentMessage]],
]
ApiKeyFn = Callable[[str], str | None | Awaitable[str | None]]
PrepareNextTurnFn = Callable[
    ["AbortSignal | None"],
    AgentLoopTurnUpdate | None | Awaitable[AgentLoopTurnUpdate | None],
]
PrepareNextTurnWithContextFn = Callable[
    [PrepareNextTurnContext, "AbortSignal | None"],
    AgentLoopTurnUpdate | None | Awaitable[AgentLoopTurnUpdate | None],
]
ShouldStopAfterTurnFn = (
    Callable[
        [ShouldStopAfterTurnContext, "AbortSignal | None"],
        bool | Awaitable[bool],
    ]
    | Callable[[ShouldStopAfterTurnContext], bool | Awaitable[bool]]
)


def _copy_empty_usage() -> Usage:
    return EMPTY_USAGE.model_copy(deep=True)


def _maybe_model_dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump()
    return value


def _normalize_standard_message(message: AgentMessage) -> AgentMessage:
    role = getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("role")
    if role in {"user", "assistant", "toolResult"}:
        return validate_message(_maybe_model_dump(message))
    return message


def default_convert_to_llm(messages: list[AgentMessage]) -> list[MessageValue]:
    converted: list[MessageValue] = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role not in {"user", "assistant", "toolResult"}:
            continue
        converted.append(validate_message(_maybe_model_dump(message)))
    return converted


def _create_mutable_agent_state(initial_state: AgentState | dict[str, Any] | None = None) -> AgentState:
    initial = dict(initial_state.model_dump() if isinstance(initial_state, AgentState) else (initial_state or {}))
    model = initial.get("model", DEFAULT_MODEL)
    if not isinstance(model, Model):
        model = Model.model_validate(model)

    messages = initial.get("messages") or []
    normalized_messages = [_normalize_standard_message(message) for message in messages]
    tools = list(initial.get("tools") or [])

    return MutableAgentState(
        systemPrompt=initial.get("systemPrompt", ""),
        model=model,
        thinkingLevel=initial.get("thinkingLevel", "off"),
        tools=tools,
        messages=normalized_messages,
        isStreaming=False,
        streamingMessage=None,
        pendingToolCalls=set(),
        errorMessage=None,
    )


class AbortSignal:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class AbortController:
    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self) -> None:
        self.signal._event.set()


class PendingMessageQueue:
    def __init__(self, mode: QueueMode) -> None:
        self.mode = mode
        self._messages: list[AgentMessage] = []

    def enqueue(self, message: AgentMessage) -> None:
        self._messages.append(message)

    def has_items(self) -> bool:
        return bool(self._messages)

    def drain(self) -> list[AgentMessage]:
        if self.mode == "all":
            drained = self._messages[:]
            self._messages.clear()
            return drained

        if not self._messages:
            return []

        first = self._messages[0]
        self._messages = self._messages[1:]
        return [first]

    def clear(self) -> None:
        self._messages.clear()


@dataclass(slots=True)
class AgentOptions:
    initialState: AgentState | dict[str, Any] | None = None
    convertToLlm: ConvertToLlmFn | None = None
    transformContext: TransformContextFn | None = None
    streamFn: StreamFn | None = None
    getApiKey: ApiKeyFn | None = None
    onPayload: Callable[[dict[str, Any], Model], Any] | None = None
    onResponse: Callable[[ProviderResponse | dict[str, Any], Model], Any] | None = None
    beforeToolCall: (
        Callable[[BeforeToolCallContext, AbortSignal | None], Awaitable[BeforeToolCallResult | None]]
        | None
    ) = None
    afterToolCall: (
        Callable[[AfterToolCallContext, AbortSignal | None], Awaitable[AfterToolCallResult | None]]
        | None
    ) = None
    prepareNextTurn: PrepareNextTurnFn | None = None
    prepareNextTurnWithContext: PrepareNextTurnWithContextFn | None = None
    shouldStopAfterTurn: ShouldStopAfterTurnFn | None = None
    steeringMode: QueueMode = "one-at-a-time"
    followUpMode: QueueMode = "one-at-a-time"
    sessionId: str | None = None
    thinkingBudgets: ThinkingBudgets | None = None
    transport: Transport = "auto"
    maxRetryDelayMs: int | None = None
    toolExecution: ToolExecutionMode = "parallel"


@dataclass(slots=True)
class _ActiveRun:
    completion: asyncio.Future[None]
    abort_controller: AbortController


class Agent:
    def __init__(self, options: AgentOptions | dict[str, Any] | None = None, **kwargs: Any) -> None:
        resolved = self._normalize_options(options, kwargs)
        self._state = _create_mutable_agent_state(resolved.initialState)
        self._listeners: list[AgentListener] = []
        self._steering_queue = PendingMessageQueue(resolved.steeringMode)
        self._follow_up_queue = PendingMessageQueue(resolved.followUpMode)
        self._active_run: _ActiveRun | None = None

        self.convertToLlm = resolved.convertToLlm or default_convert_to_llm
        self.transformContext = resolved.transformContext
        self.streamFn = (
            resolved.streamFn
            if resolved.streamFn is not None
            else get_default_stream_fn()
        )
        self.getApiKey = resolved.getApiKey
        self.onPayload = resolved.onPayload
        self.onResponse = resolved.onResponse
        self.beforeToolCall = resolved.beforeToolCall
        self.afterToolCall = resolved.afterToolCall
        self.prepareNextTurn = resolved.prepareNextTurn
        self.prepareNextTurnWithContext = resolved.prepareNextTurnWithContext
        self.shouldStopAfterTurn = resolved.shouldStopAfterTurn
        self.sessionId = resolved.sessionId
        self.thinkingBudgets = resolved.thinkingBudgets
        self.transport = resolved.transport
        self.maxRetryDelayMs = resolved.maxRetryDelayMs
        self.toolExecution = resolved.toolExecution

    @staticmethod
    def _normalize_options(options: AgentOptions | dict[str, Any] | None, kwargs: dict[str, Any]) -> AgentOptions:
        if isinstance(options, AgentOptions):
            if kwargs:
                raise TypeError("Use either an AgentOptions object or keyword arguments, not both.")
            return options
        if options is None:
            return AgentOptions(**kwargs)
        if kwargs:
            raise TypeError("Use either an options mapping or keyword arguments, not both.")
        return AgentOptions(**dict(options))

    @property
    def state(self) -> AgentState:
        return self._state

    @property
    def steeringMode(self) -> QueueMode:
        return self._steering_queue.mode

    @steeringMode.setter
    def steeringMode(self, mode: QueueMode) -> None:
        self._steering_queue.mode = mode

    @property
    def followUpMode(self) -> QueueMode:
        return self._follow_up_queue.mode

    @followUpMode.setter
    def followUpMode(self, mode: QueueMode) -> None:
        self._follow_up_queue.mode = mode

    @property
    def signal(self) -> AbortSignal | None:
        return self._active_run.abort_controller.signal if self._active_run is not None else None

    def subscribe(self, listener: AgentListener) -> Callable[[], None]:
        if listener not in self._listeners:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def steer(self, message: AgentMessage) -> None:
        self._steering_queue.enqueue(message)

    def followUp(self, message: AgentMessage) -> None:
        self._follow_up_queue.enqueue(message)

    def follow_up(self, message: AgentMessage) -> None:
        self.followUp(message)

    def clearSteeringQueue(self) -> None:
        self._steering_queue.clear()

    def clearFollowUpQueue(self) -> None:
        self._follow_up_queue.clear()

    def clearAllQueues(self) -> None:
        self.clearSteeringQueue()
        self.clearFollowUpQueue()

    def hasQueuedMessages(self) -> bool:
        return self._steering_queue.has_items() or self._follow_up_queue.has_items()

    def abort(self) -> None:
        if self._active_run is not None:
            self._active_run.abort_controller.abort()

    async def waitForIdle(self) -> None:
        active_run = self._active_run
        if active_run is None:
            return
        await asyncio.shield(active_run.completion)

    async def wait_for_idle(self) -> None:
        await self.waitForIdle()

    def reset(self) -> None:
        if self._active_run is not None:  # pi 1532c99 #7717: resetting mid-run would wipe the active run state
            raise RuntimeError("Agent is already processing. Wait for completion before resetting.")
        self._state.messages = []
        self._state.isStreaming = False
        self._state.streamingMessage = None
        self._state.pendingToolCalls = set()
        self._state.errorMessage = None
        self.clearAllQueues()

    @overload
    async def prompt(self, message: AgentMessage | Sequence[AgentMessage]) -> None: ...

    @overload
    async def prompt(self, input: str, images: Sequence[ImageContent] | None = None) -> None: ...

    async def prompt(
        self,
        input: str | AgentMessage | Sequence[AgentMessage],
        images: Sequence[ImageContent] | None = None,
    ) -> None:
        if self._active_run is not None:
            raise RuntimeError(
                "Agent is already processing a prompt. "
                "Use steer() or followUp() to queue messages, or wait for completion."
            )

        prompt_messages = self._normalize_prompt_input(input, images)
        await self._run_prompt_messages(prompt_messages)

    async def continue_(self) -> None:
        if self._active_run is not None:
            raise RuntimeError("Agent is already processing. Wait for completion before continuing.")

        last_message = self._state.messages[-1] if self._state.messages else None
        if last_message is None:
            raise RuntimeError("No messages to continue from")

        if getattr(last_message, "role", None) == "assistant":
            queued_steering = self._steering_queue.drain()
            if queued_steering:
                await self._run_prompt_messages(queued_steering, skip_initial_steering_poll=True)
                return

            queued_follow_ups = self._follow_up_queue.drain()
            if queued_follow_ups:
                await self._run_prompt_messages(queued_follow_ups)
                return

            raise RuntimeError("Cannot continue from message role: assistant")

        await self._run_continuation()

    async def continue_run(self) -> None:
        await self.continue_()

    def _normalize_prompt_input(
        self,
        input: str | AgentMessage | Sequence[AgentMessage],
        images: Sequence[ImageContent] | None,
    ) -> list[AgentMessage]:
        if isinstance(input, str):
            content: list[TextContent | ImageContent] = [TextContent(text=input)]
            if images:
                content.extend(images)
            return [
                _normalize_standard_message(
                    {"role": "user", "content": content, "timestamp": int(time.time() * 1000)}
                )
            ]
        if isinstance(input, Sequence) and not isinstance(input, BaseModel):
            return [_normalize_standard_message(message) for message in input]
        return [_normalize_standard_message(input)]

    async def _run_prompt_messages(
        self,
        prompt_messages: list[AgentMessage],
        *,
        skip_initial_steering_poll: bool = False,
    ) -> None:
        async def executor(signal: AbortSignal) -> None:
            await run_agent_loop(
                prompt_messages,
                self._create_context_snapshot(),
                self._create_loop_config(skip_initial_steering_poll=skip_initial_steering_poll),
                self._emit_event,
                signal,
                self.streamFn,
            )

        await self._run_with_lifecycle(executor)

    async def _run_continuation(self) -> None:
        async def executor(signal: AbortSignal) -> None:
            await run_agent_loop_continue(
                self._create_context_snapshot(),
                self._create_loop_config(),
                self._emit_event,
                signal,
                self.streamFn,
            )

        await self._run_with_lifecycle(executor)

    def _create_context_snapshot(self) -> AgentContext:
        return AgentContext(
            systemPrompt=self._state.systemPrompt,
            messages=self._state.messages[:],
            tools=self._state.tools[:],
        )

    def _create_loop_config(self, *, skip_initial_steering_poll: bool = False) -> AgentLoopConfig:
        skip_poll = skip_initial_steering_poll
        should_stop_after_turn = self.shouldStopAfterTurn

        async def get_steering_messages() -> list[AgentMessage]:
            nonlocal skip_poll
            if skip_poll:
                skip_poll = False
                return []
            return self._steering_queue.drain()

        async def get_follow_up_messages() -> list[AgentMessage]:
            return self._follow_up_queue.drain()

        async def should_stop(context: ShouldStopAfterTurnContext) -> bool:
            return bool(
                await maybe_await(
                    call_with_optional_second_arg(
                        should_stop_after_turn,
                        context,
                        self.signal,
                    )
                )
            )

        async def prepare_next_turn(next_turn_context: Any) -> AgentLoopTurnUpdate | None:
            if self.prepareNextTurnWithContext is not None:
                return await maybe_await(
                    self.prepareNextTurnWithContext(next_turn_context, self.signal)
                )
            if self.prepareNextTurn is not None:
                return await maybe_await(self.prepareNextTurn(self.signal))
            return None

        return AgentLoopConfig(
            model=self._state.model,
            convertToLlm=self.convertToLlm,
            transformContext=self.transformContext,
            getApiKey=self.getApiKey,
            prepareNextTurn=(
                prepare_next_turn
                if self.prepareNextTurnWithContext is not None
                or self.prepareNextTurn is not None
                else None
            ),
            shouldStopAfterTurn=should_stop if should_stop_after_turn is not None else None,
            getSteeringMessages=get_steering_messages,
            getFollowUpMessages=get_follow_up_messages,
            toolExecution=self.toolExecution,
            beforeToolCall=self.beforeToolCall,
            afterToolCall=self.afterToolCall,
            reasoning=None if self._state.thinkingLevel == "off" else self._state.thinkingLevel,
            sessionId=self.sessionId,
            onPayload=self.onPayload,
            onResponse=self.onResponse,
            transport=self.transport,
            thinkingBudgets=self.thinkingBudgets,
            maxRetryDelayMs=self.maxRetryDelayMs,
        )

    async def _run_with_lifecycle(self, executor: Callable[[AbortSignal], Awaitable[None]]) -> None:
        if self._active_run is not None:
            raise RuntimeError("Agent is already processing.")

        loop = asyncio.get_running_loop()
        self._active_run = _ActiveRun(completion=loop.create_future(), abort_controller=AbortController())
        self._state.isStreaming = True
        self._state.streamingMessage = None
        self._state.errorMessage = None

        try:
            await executor(self._active_run.abort_controller.signal)
        except BaseException as error:
            # pi catches `Exception` here (agent.ts:502); the two BaseException-only
            # exits have no JS equivalent and are not the model erroring -- turning
            # Ctrl-C into a stopReason="error" message leaves the process running.
            if isinstance(error, KeyboardInterrupt | SystemExit):
                raise
            aborted = self.signal is not None and self.signal.aborted
            if isinstance(error, asyncio.CancelledError) and not aborted:
                raise                      # the caller cancelled us: do not answer for them
            await self._handle_run_failure(error)
        finally:
            self._finish_run()

    async def _handle_run_failure(self, error: BaseException) -> None:
        aborted = self.signal.aborted if self.signal is not None else False
        failure_message = AssistantMessage(
            content=[TextContent(text="")],
            api=self._state.model.api,
            provider=self._state.model.provider,
            model=self._state.model.id,
            usage=_copy_empty_usage(),
            stopReason="aborted" if aborted else "error",
            errorMessage=str(error),
            timestamp=int(time.time() * 1000),
        )
        await self._emit_event(MessageStartEvent(message=failure_message))
        await self._emit_event(MessageEndEvent(message=failure_message))
        await self._emit_event(TurnEndEvent(message=failure_message, toolResults=[]))
        await self._emit_event(AgentEndEvent(messages=[failure_message]))

    def _finish_run(self) -> None:
        active_run = self._active_run
        self._state.isStreaming = False
        self._state.streamingMessage = None
        self._state.pendingToolCalls = set()
        self._active_run = None
        if active_run is not None and not active_run.completion.done():
            active_run.completion.set_result(None)

    async def _emit_event(self, event: AgentEvent) -> None:
        self._reduce_state(event)
        signal = self.signal
        if signal is None:
            raise RuntimeError("Agent listener invoked outside active run")
        for listener in tuple(self._listeners):
            await maybe_await(listener(event, signal))

    def _reduce_state(self, event: AgentEvent) -> None:
        if event.type == "message_start":
            self._state.streamingMessage = self._copy_agent_message(event.message)
            return

        if event.type == "message_update":
            self._state.streamingMessage = self._copy_agent_message(event.message)
            return

        if event.type == "message_end":
            self._state.streamingMessage = None
            self._state.messages = [
                *self._state.messages,
                _normalize_standard_message(self._copy_agent_message(event.message)),
            ]
            return

        if event.type == "tool_execution_start":
            pending = set(self._state.pendingToolCalls)
            pending.add(event.toolCallId)
            self._state.pendingToolCalls = pending
            return

        if event.type == "tool_execution_end":
            pending = set(self._state.pendingToolCalls)
            pending.discard(event.toolCallId)
            self._state.pendingToolCalls = pending
            return

        if event.type == "turn_end" and getattr(event.message, "role", None) == "assistant":
            error_message = getattr(event.message, "errorMessage", None)
            if error_message:
                self._state.errorMessage = error_message
            return

        if event.type == "agent_end":
            self._state.streamingMessage = None

    def _copy_agent_message(self, message: AgentMessage) -> AgentMessage:
        if isinstance(message, BaseModel):
            return message.model_copy(deep=True)
        if isinstance(message, dict):
            return dict(message)
        return message


__all__ = [
    "DEFAULT_MODEL",
    "EMPTY_USAGE",
    "AbortController",
    "AbortSignal",
    "Agent",
    "AgentListener",
    "AgentOptions",
    "MutableAgentState",
    "PendingMessageQueue",
    "QueueMode",
    "default_convert_to_llm",
]
