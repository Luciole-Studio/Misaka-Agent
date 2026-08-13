"""Core agent loop, tool execution, and continuation helpers."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from types import SimpleNamespace
from typing import Any

from misaka.ai.stream import stream_simple
from misaka.ai.types import (
    AssistantMessage,
    Context,
    TextContent,
    ToolResultMessage,
    validate_message,
    validate_user_content,
)
from misaka.ai.utils.event_stream import EventStream
from misaka.ai.utils.validation import validate_tool_arguments

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
    AgentToolResult,
    BeforeToolCallContext,
    BeforeToolCallResult,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ShouldStopAfterTurnContext,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)

type AgentEventSink = Callable[[AgentEvent], Awaitable[None] | None]


@dataclass(slots=True)
class ExecutedToolCallBatch:
    messages: list[ToolResultMessage]
    terminate: bool


@dataclass(slots=True)
class PreparedToolCall:
    kind: str
    toolCall: AgentToolCall
    tool: AgentTool
    args: Any


@dataclass(slots=True)
class ImmediateToolCallOutcome:
    kind: str
    result: AgentToolResult
    isError: bool


@dataclass(slots=True)
class ExecutedToolCallOutcome:
    result: AgentToolResult
    isError: bool


class StreamOptionsNamespace(SimpleNamespace):
    """A SimpleNamespace subclass that supports dict() conversion and ** unpacking.

    In TypeScript, the agent loop spreads ``config`` into a plain object via
    ``{ ...config, apiKey, signal }``.  Downstream code (e.g. ``sdk.py``) then
    spreads that object again with ``{ ...options, ... }``.  JavaScript object
    spread works transparently on any object, but Python's ``dict()`` and ``**``
    unpacking require the target to expose ``keys()`` and ``__getitem__()`` (the
    informal mapping protocol).  Without these methods, ``dict(namespace)`` raises
    ``TypeError: 'StreamOptionsNamespace' object is not iterable``.
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

    def __getattr__(self, name: str) -> Any:
        # Return None for missing attributes instead of raising AttributeError.
        # This is needed because downstream code (e.g. build_base_options) accesses
        # fields like cacheRetention, sessionId, etc. that may not be present when
        # the namespace is constructed from AgentLoopConfig fields.
        return None

    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
        payload = dict(self.__dict__)
        if exclude_none:
            return {key: value for key, value in payload.items() if value is not None}
        return payload


@dataclass(slots=True)
class FinalizedToolCallOutcome:
    toolCall: AgentToolCall
    result: AgentToolResult
    isError: bool


def agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any | None = None,
    stream_fn=None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    stream = _create_agent_stream()

    async def run() -> None:
        try:
            messages = await run_agent_loop(prompts, context, config, _push_event(stream), signal, stream_fn)
            stream.end(messages)
        except BaseException as error:  # noqa: BLE001
            stream.result().set_exception(error)
            stream.end([])

    asyncio.create_task(run())
    return stream


def agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any | None = None,
    stream_fn=None,
) -> EventStream[AgentEvent, list[AgentMessage]]:
    if not context.messages:
        raise RuntimeError("Cannot continue: no messages in context")
    if getattr(context.messages[-1], "role", None) == "assistant":
        raise RuntimeError("Cannot continue from message role: assistant")

    stream = _create_agent_stream()

    async def run() -> None:
        try:
            messages = await run_agent_loop_continue(context, config, _push_event(stream), signal, stream_fn)
            stream.end(messages)
        except BaseException as error:  # noqa: BLE001
            stream.result().set_exception(error)
            stream.end([])

    asyncio.create_task(run())
    return stream


async def run_agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any | None = None,
    stream_fn=None,
) -> list[AgentMessage]:
    new_messages = [_copy_agent_message(prompt) for prompt in prompts]
    current_context = AgentContext(
        systemPrompt=context.systemPrompt,
        messages=[*_copy_agent_messages(context.messages), *new_messages],
        tools=_copy_tools(context.tools),
    )

    await _emit(emit, AgentStartEvent())
    await _emit(emit, TurnStartEvent())
    for prompt in new_messages:
        await _emit(emit, MessageStartEvent(message=prompt))
        await _emit(emit, MessageEndEvent(message=prompt))

    await _run_loop(current_context, new_messages, config, signal, emit, stream_fn)
    return new_messages


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
    signal: Any | None = None,
    stream_fn=None,
) -> list[AgentMessage]:
    if not context.messages:
        raise RuntimeError("Cannot continue: no messages in context")
    if getattr(context.messages[-1], "role", None) == "assistant":
        raise RuntimeError("Cannot continue from message role: assistant")

    new_messages: list[AgentMessage] = []
    current_context = AgentContext(
        systemPrompt=context.systemPrompt,
        messages=_copy_agent_messages(context.messages),
        tools=_copy_tools(context.tools),
    )

    await _emit(emit, AgentStartEvent())
    await _emit(emit, TurnStartEvent())
    await _run_loop(current_context, new_messages, config, signal, emit, stream_fn)
    return new_messages


async def _run_loop(
    initial_context: AgentContext,
    new_messages: list[AgentMessage],
    initial_config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
    stream_fn,
) -> None:
    current_context = initial_context
    config = initial_config
    first_turn = True
    pending_messages = list(await _maybe_await(config.getSteeringMessages()) if config.getSteeringMessages else [])

    while True:
        has_more_tool_calls = True

        while has_more_tool_calls or pending_messages:
            if not first_turn:
                await _emit(emit, TurnStartEvent())
            else:
                first_turn = False

            if pending_messages:
                for message in pending_messages:
                    copied = _copy_agent_message(message)
                    await _emit(emit, MessageStartEvent(message=copied))
                    await _emit(emit, MessageEndEvent(message=copied))
                    current_context.messages.append(copied)
                    new_messages.append(copied)
                pending_messages = []

            message = await stream_assistant_response(current_context, config, signal, emit, stream_fn)
            new_messages.append(message)

            if message.stopReason in {"error", "aborted"}:
                await _emit(emit, TurnEndEvent(message=message, toolResults=[]))
                await _emit(emit, AgentEndEvent(messages=new_messages[:]))
                return

            tool_calls = [block for block in message.content if block.type == "toolCall"]
            tool_results: list[ToolResultMessage] = []
            has_more_tool_calls = False
            if tool_calls:
                executed_tool_batch = await execute_tool_calls(current_context, message, config, signal, emit)
                tool_results.extend(executed_tool_batch.messages)
                has_more_tool_calls = not executed_tool_batch.terminate

                for result in tool_results:
                    current_context.messages.append(result)
                    new_messages.append(result)

            await _emit(emit, TurnEndEvent(message=message, toolResults=tool_results))

            next_turn_context = ShouldStopAfterTurnContext(
                message=message,
                toolResults=tool_results,
                context=current_context,
                newMessages=new_messages,
            )
            next_turn_snapshot = (
                await _maybe_await(config.prepareNextTurn(next_turn_context))
                if config.prepareNextTurn
                else None
            )
            if next_turn_snapshot:
                current_context = next_turn_snapshot.context or current_context
                config = replace(
                    config,
                    model=next_turn_snapshot.model or config.model,
                    reasoning=(
                        config.reasoning
                        if next_turn_snapshot.thinkingLevel is None
                        else None
                        if next_turn_snapshot.thinkingLevel == "off"
                        else next_turn_snapshot.thinkingLevel
                    ),
                )

            should_stop = (
                await _maybe_await(
                    config.shouldStopAfterTurn(
                        ShouldStopAfterTurnContext(
                            message=message,
                            toolResults=tool_results,
                            context=current_context,
                            newMessages=new_messages,
                        )
                    )
                )
                if config.shouldStopAfterTurn
                else False
            )
            if should_stop:
                await _emit(emit, AgentEndEvent(messages=new_messages[:]))
                return

            pending_messages = list(
                await _maybe_await(config.getSteeringMessages()) if config.getSteeringMessages else []
            )

        follow_up_messages = list(
            await _maybe_await(config.getFollowUpMessages()) if config.getFollowUpMessages else []
        )
        if follow_up_messages:
            pending_messages = follow_up_messages
            continue

        break

    await _emit(emit, AgentEndEvent(messages=new_messages[:]))


async def stream_assistant_response(
    context: AgentContext,
    config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
    stream_fn=None,
) -> AssistantMessage:
    messages = context.messages
    if config.transformContext:
        messages = list(await _maybe_await(config.transformContext(messages, signal)))

    llm_messages = list(await _maybe_await(config.convertToLlm(messages)))
    validated_messages = [validate_message(_model_dump(message)) for message in llm_messages]
    llm_context = Context(
        systemPrompt=context.systemPrompt or None,
        messages=validated_messages,
        tools=_copy_tools(context.tools),
    )
    stream_function = stream_fn or stream_simple
    resolved_api_key = (
        await _maybe_await(config.getApiKey(config.model.provider))
        if config.getApiKey
        else None
    ) or config.apiKey
    response_options_payload = _to_mapping(config)
    response_options_payload["apiKey"] = resolved_api_key
    response_options_payload["signal"] = signal
    response_options = StreamOptionsNamespace(**response_options_payload)
    response = await _maybe_await(stream_function(config.model, llm_context, response_options))

    partial_message: AssistantMessage | None = None
    added_partial = False

    async for event in response:
        if event.type == "start":
            partial_message = event.partial.model_copy(deep=True)
            context.messages.append(partial_message)
            added_partial = True
            await _emit(emit, MessageStartEvent(message=partial_message.model_copy(deep=True)))
            continue

        if event.type in {
            "text_start",
            "text_delta",
            "text_end",
            "thinking_start",
            "thinking_delta",
            "thinking_end",
            "toolcall_start",
            "toolcall_delta",
            "toolcall_end",
        }:
            if partial_message is not None:
                partial_message = event.partial.model_copy(deep=True)
                context.messages[-1] = partial_message
                await _emit(
                    emit,
                    MessageUpdateEvent(
                        assistantMessageEvent=event,
                        message=partial_message.model_copy(deep=True),
                    ),
                )
            continue

        if event.type in {"done", "error"}:
            final_message = await response.result()
            if added_partial:
                context.messages[-1] = final_message
            else:
                context.messages.append(final_message)
                await _emit(emit, MessageStartEvent(message=final_message.model_copy(deep=True)))
            await _emit(emit, MessageEndEvent(message=final_message))
            return final_message

    final_message = await response.result()
    if added_partial:
        context.messages[-1] = final_message
    else:
        context.messages.append(final_message)
        await _emit(emit, MessageStartEvent(message=final_message.model_copy(deep=True)))
    await _emit(emit, MessageEndEvent(message=final_message))
    return final_message


async def execute_tool_calls(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
) -> ExecutedToolCallBatch:
    tool_calls = [block for block in assistant_message.content if block.type == "toolCall"]
    has_sequential_tool_call = any(
        any(
            tool.name == tool_call.name and tool.executionMode == "sequential"
            for tool in current_context.tools or []
        )
        for tool_call in tool_calls
    )
    if config.toolExecution == "sequential" or has_sequential_tool_call:
        return await execute_tool_calls_sequential(current_context, assistant_message, tool_calls, config, signal, emit)
    return await execute_tool_calls_parallel(current_context, assistant_message, tool_calls, config, signal, emit)


async def execute_tool_calls_sequential(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[AgentToolCall],
    config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
) -> ExecutedToolCallBatch:
    finalized_calls: list[FinalizedToolCallOutcome] = []
    messages: list[ToolResultMessage] = []

    for tool_call in tool_calls:
        await _emit(
            emit,
            ToolExecutionStartEvent(
                toolCallId=tool_call.id,
                toolName=tool_call.name,
                args=tool_call.arguments,
            ),
        )

        preparation = await prepare_tool_call(current_context, assistant_message, tool_call, config, signal)
        if isinstance(preparation, ImmediateToolCallOutcome):
            finalized = FinalizedToolCallOutcome(
                toolCall=tool_call,
                result=preparation.result,
                isError=preparation.isError,
            )
        else:
            executed = await execute_prepared_tool_call(preparation, signal, emit)
            finalized = await finalize_executed_tool_call(
                current_context,
                assistant_message,
                preparation,
                executed,
                config,
                signal,
            )

        await emit_tool_execution_end(finalized, emit)
        tool_result_message = create_tool_result_message(finalized)
        await emit_tool_result_message(tool_result_message, emit)
        finalized_calls.append(finalized)
        messages.append(tool_result_message)

        if _signal_aborted(signal):
            break

    return ExecutedToolCallBatch(
        messages=messages,
        terminate=should_terminate_tool_batch(finalized_calls),
    )


async def execute_tool_calls_parallel(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[AgentToolCall],
    config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
) -> ExecutedToolCallBatch:
    finalized_entries: list[FinalizedToolCallOutcome | Awaitable[FinalizedToolCallOutcome]] = []

    for tool_call in tool_calls:
        await _emit(
            emit,
            ToolExecutionStartEvent(
                toolCallId=tool_call.id,
                toolName=tool_call.name,
                args=tool_call.arguments,
            ),
        )

        preparation = await prepare_tool_call(current_context, assistant_message, tool_call, config, signal)
        if isinstance(preparation, ImmediateToolCallOutcome):
            finalized = FinalizedToolCallOutcome(
                toolCall=tool_call,
                result=preparation.result,
                isError=preparation.isError,
            )
            await emit_tool_execution_end(finalized, emit)
            finalized_entries.append(finalized)
            if _signal_aborted(signal):
                break
            continue

        async def finalize(prepared: PreparedToolCall = preparation) -> FinalizedToolCallOutcome:
            executed = await execute_prepared_tool_call(prepared, signal, emit)
            finalized = await finalize_executed_tool_call(
                current_context,
                assistant_message,
                prepared,
                executed,
                config,
                signal,
            )
            await emit_tool_execution_end(finalized, emit)
            return finalized

        finalized_entries.append(finalize())
        if _signal_aborted(signal):
            break

    ordered_finalized_calls = await asyncio.gather(
        *[
            entry if inspect.isawaitable(entry) else _return_value(entry)
            for entry in finalized_entries
        ]
    )
    messages: list[ToolResultMessage] = []
    for finalized in ordered_finalized_calls:
        tool_result_message = create_tool_result_message(finalized)
        await emit_tool_result_message(tool_result_message, emit)
        messages.append(tool_result_message)

    return ExecutedToolCallBatch(
        messages=messages,
        terminate=should_terminate_tool_batch(ordered_finalized_calls),
    )


def should_terminate_tool_batch(finalized_calls: list[FinalizedToolCallOutcome]) -> bool:
    return bool(finalized_calls) and all(finalized.result.terminate is True for finalized in finalized_calls)


def prepare_tool_call_arguments(tool: AgentTool, tool_call: AgentToolCall) -> AgentToolCall:
    if tool.prepareArguments is None:
        return tool_call
    prepared_arguments = tool.prepareArguments(tool_call.arguments)
    if prepared_arguments is tool_call.arguments:
        return tool_call
    return tool_call.model_copy(update={"arguments": prepared_arguments})


async def prepare_tool_call(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    tool_call: AgentToolCall,
    config: AgentLoopConfig,
    signal: Any | None,
) -> PreparedToolCall | ImmediateToolCallOutcome:
    tool = next((candidate for candidate in current_context.tools or [] if candidate.name == tool_call.name), None)
    if tool is None:
        return ImmediateToolCallOutcome(
            kind="immediate",
            result=create_error_tool_result(f"Tool {tool_call.name} not found"),
            isError=True,
        )

    try:
        prepared_tool_call = prepare_tool_call_arguments(tool, tool_call)
        validated_args = validate_tool_arguments(tool, prepared_tool_call)
        if config.beforeToolCall:
            before_result = await _maybe_await(
                config.beforeToolCall(
                    BeforeToolCallContext(
                        assistantMessage=assistant_message,
                        toolCall=tool_call,
                        args=validated_args,
                        context=current_context,
                    ),
                    signal,
                )
            )
            before_result = _coerce_before_tool_call_result(before_result)
            if _signal_aborted(signal):
                return ImmediateToolCallOutcome(
                    kind="immediate",
                    result=create_error_tool_result("Operation aborted"),
                    isError=True,
                )
            if before_result and before_result.block:
                result = create_error_tool_result(before_result.reason or "Tool execution was blocked")
                if before_result.terminate is True:  # pi 1eb988c:被拦的调用也能参与整批早停
                    result.terminate = True
                return ImmediateToolCallOutcome(
                    kind="immediate",
                    result=result,
                    isError=True,
                )
            if before_result and before_result.updatedInput is not None:
                prepared_tool_call = tool_call.model_copy(
                    update={"arguments": before_result.updatedInput}
                )
                validated_args = validate_tool_arguments(tool, prepared_tool_call)
        if _signal_aborted(signal):
            return ImmediateToolCallOutcome(
                kind="immediate",
                result=create_error_tool_result("Operation aborted"),
                isError=True,
            )
        return PreparedToolCall(kind="prepared", toolCall=tool_call, tool=tool, args=validated_args)
    except BaseException as error:  # noqa: BLE001
        return ImmediateToolCallOutcome(
            kind="immediate",
            result=create_error_tool_result(str(error)),
            isError=True,
        )


async def execute_prepared_tool_call(
    prepared: PreparedToolCall,
    signal: Any | None,
    emit: AgentEventSink,
) -> ExecutedToolCallOutcome:
    update_tasks: list[asyncio.Task[None]] = []

    def on_update(partial_result: AgentToolResult) -> None:
        async def emit_update() -> None:
            await _emit(
                emit,
                ToolExecutionUpdateEvent(
                    toolCallId=prepared.toolCall.id,
                    toolName=prepared.toolCall.name,
                    args=prepared.toolCall.arguments,
                    partialResult=partial_result,
                ),
            )

        update_tasks.append(asyncio.create_task(emit_update()))

    try:
        result = await _maybe_await(
            prepared.tool.execute(prepared.toolCall.id, prepared.args, signal, on_update)
        )
        if update_tasks:
            await asyncio.gather(*update_tasks)
        return ExecutedToolCallOutcome(result=_coerce_agent_tool_result(result), isError=False)
    except BaseException as error:  # noqa: BLE001
        if update_tasks:
            await asyncio.gather(*update_tasks)
        return ExecutedToolCallOutcome(
            result=create_error_tool_result(str(error)),
            isError=True,
        )


async def finalize_executed_tool_call(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    prepared: PreparedToolCall,
    executed: ExecutedToolCallOutcome,
    config: AgentLoopConfig,
    signal: Any | None,
) -> FinalizedToolCallOutcome:
    result = executed.result
    is_error = executed.isError

    if config.afterToolCall:
        try:
            after_result = await _maybe_await(
                config.afterToolCall(
                    AfterToolCallContext(
                        assistantMessage=assistant_message,
                        toolCall=prepared.toolCall,
                        args=prepared.args,
                        result=result,
                        isError=is_error,
                        context=current_context,
                    ),
                    signal,
                )
            )
            if after_result is not None:
                normalized_after_result = _coerce_after_tool_call_result(after_result)
                result = AgentToolResult(
                    content=(
                        normalized_after_result.content
                        if normalized_after_result.content is not None
                        else result.content
                    ),
                    details=(
                        normalized_after_result.details
                        if normalized_after_result.details is not None
                        else result.details
                    ),
                    terminate=(
                        normalized_after_result.terminate
                        if normalized_after_result.terminate is not None
                        else result.terminate
                    ),
                )
                is_error = normalized_after_result.isError if normalized_after_result.isError is not None else is_error
        except BaseException as error:  # noqa: BLE001
            result = create_error_tool_result(str(error))
            is_error = True

    return FinalizedToolCallOutcome(
        toolCall=prepared.toolCall,
        result=result,
        isError=is_error,
    )


def create_error_tool_result(message: str) -> AgentToolResult:
    return AgentToolResult(
        content=[TextContent(text=message)],
        details={},
    )


async def emit_tool_execution_end(finalized: FinalizedToolCallOutcome, emit: AgentEventSink) -> None:
    await _emit(
        emit,
        ToolExecutionEndEvent(
            toolCallId=finalized.toolCall.id,
            toolName=finalized.toolCall.name,
            result=finalized.result,
            isError=finalized.isError,
        ),
    )


def create_tool_result_message(finalized: FinalizedToolCallOutcome) -> ToolResultMessage:
    return ToolResultMessage(
        toolCallId=finalized.toolCall.id,
        toolName=finalized.toolCall.name,
        content=[validate_user_content(_model_dump(block)) for block in finalized.result.content],
        details=finalized.result.details,
        isError=finalized.isError,
        timestamp=int(time.time() * 1000),
    )


async def emit_tool_result_message(tool_result_message: ToolResultMessage, emit: AgentEventSink) -> None:
    await _emit(emit, MessageStartEvent(message=tool_result_message))
    await _emit(emit, MessageEndEvent(message=tool_result_message))


def _create_agent_stream() -> EventStream[AgentEvent, list[AgentMessage]]:
    return EventStream(
        lambda event: event.type == "agent_end",
        lambda event: event.messages if event.type == "agent_end" else [],
    )


def _push_event(stream: EventStream[AgentEvent, list[AgentMessage]]) -> AgentEventSink:
    async def emit(event: AgentEvent) -> None:
        stream.push(event)

    return emit


async def _emit(emit: AgentEventSink, event: AgentEvent) -> None:
    await _maybe_await(emit(event))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _return_value(value: FinalizedToolCallOutcome) -> FinalizedToolCallOutcome:
    return value


def _copy_agent_message(message: AgentMessage) -> AgentMessage:
    if hasattr(message, "model_copy"):
        return message.model_copy(deep=True)
    if isinstance(message, dict):
        return dict(message)
    return message


def _copy_agent_messages(messages: list[AgentMessage]) -> list[AgentMessage]:
    return [_copy_agent_message(message) for message in messages]


def _copy_tools(tools: list[AgentTool] | None) -> list[AgentTool] | None:
    if tools is None:
        return None
    return [tool.model_copy(deep=True) for tool in tools]


def _model_dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


def _to_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, dict):
            return dict(dumped)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    if is_dataclass(value):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    raise TypeError(f"Cannot convert {type(value).__name__} to mapping")


def _signal_aborted(signal: Any | None) -> bool:
    if signal is None:
        return False
    if hasattr(signal, "aborted"):
        return bool(signal.aborted)
    if hasattr(signal, "is_set"):
        return bool(signal.is_set())
    return False


def _coerce_agent_tool_result(value: AgentToolResult | dict[str, Any]) -> AgentToolResult:
    if isinstance(value, AgentToolResult):
        return value
    content = [
        validate_user_content(_model_dump(block))
        for block in value.get("content", [])
    ]
    return AgentToolResult(
        content=content,
        details=value.get("details"),
        terminate=value.get("terminate"),
    )


def _coerce_after_tool_call_result(value: AfterToolCallResult | dict[str, Any]) -> AfterToolCallResult:
    if isinstance(value, AfterToolCallResult):
        return value
    content = value.get("content")
    normalized_content = None
    if content is not None:
        normalized_content = [validate_user_content(_model_dump(block)) for block in content]
    return AfterToolCallResult(
        content=normalized_content,
        details=value.get("details"),
        isError=value.get("isError"),
        terminate=value.get("terminate"),
    )


def _coerce_before_tool_call_result(
    value: BeforeToolCallResult | dict[str, Any] | None,
) -> BeforeToolCallResult | None:
    if value is None or isinstance(value, BeforeToolCallResult):
        return value
    return BeforeToolCallResult(
        block=value.get("block"),
        reason=value.get("reason"),
        updatedInput=value.get("updatedInput"),
    )


agentLoop = agent_loop
agentLoopContinue = agent_loop_continue
runAgentLoop = run_agent_loop
runAgentLoopContinue = run_agent_loop_continue

__all__ = [
    "AgentEventSink",
    "agentLoop",
    "agentLoopContinue",
    "agent_loop",
    "agent_loop_continue",
    "create_tool_result_message",
    "emit_tool_result_message",
    "execute_tool_calls",
    "runAgentLoop",
    "runAgentLoopContinue",
    "run_agent_loop",
    "run_agent_loop_continue",
    "stream_assistant_response",
]
