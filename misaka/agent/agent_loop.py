"""Core agent loop, tool execution, and continuation helpers."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, fields, is_dataclass, replace
from types import SimpleNamespace
from typing import Any

from misaka.agent.stream_fn import get_default_stream_fn
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
from misaka.ai.types import (
    AssistantMessage,
    Context,
    TextContent,
    ToolResultMessage,
    validate_message,
    validate_user_content,
)
from misaka.ai.utils.event_stream import EventStream, spawn_stream_task
from misaka.ai.utils.validation import validate_tool_arguments
from misaka.utils.values import maybe_await, signal_aborted

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

    It deliberately does *not* answer None for a missing attribute. Providers read their
    options with ``getattr(options, name, default)``, and a ``__getattr__`` that returns
    None intercepts that call before the default can apply -- so every option a provider
    tried to default was silently None instead. Missing now means missing.
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

    # Not a bare `create_task`: the event loop holds only a weak reference, so the task
    # can be collected mid-run ("Task was destroyed but it is pending"). pi has no such
    # hazard -- V8 keeps the promise `void run().then()` returns alive. This is the
    # repo's own helper for exactly this shape.
    spawn_stream_task(run())
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

    resolved_stream_fn = stream_fn if stream_fn is not None else get_default_stream_fn()
    await _run_loop(current_context, new_messages, config, signal, emit, resolved_stream_fn)
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
    resolved_stream_fn = stream_fn if stream_fn is not None else get_default_stream_fn()
    await _run_loop(current_context, new_messages, config, signal, emit, resolved_stream_fn)
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
    last_completed_turn: ShouldStopAfterTurnContext | None = None
    pending_messages = list(await maybe_await(config.getSteeringMessages()) if config.getSteeringMessages else [])

    while True:
        has_more_tool_calls = True

        while has_more_tool_calls or pending_messages:
            if last_completed_turn is not None:
                next_turn_snapshot = (
                    await maybe_await(config.prepareNextTurn(last_completed_turn))
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

                # Preparation may take long enough for new steering to arrive. Do not
                # drain twice when the earlier poll already yielded a message in
                # one-at-a-time mode.
                if not pending_messages:
                    pending_messages = list(
                        await maybe_await(config.getSteeringMessages())
                        if config.getSteeringMessages
                        else []
                    )
                await _emit(emit, TurnStartEvent())

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
                executed_tool_batch = (
                    await _fail_tool_calls_from_truncated_message(tool_calls, emit)
                    if message.stopReason == "length"
                    else await execute_tool_calls(
                        current_context, message, config, signal, emit
                    )
                )
                tool_results.extend(executed_tool_batch.messages)
                has_more_tool_calls = not executed_tool_batch.terminate

                for result in tool_results:
                    current_context.messages.append(result)
                    new_messages.append(result)

            await _emit(emit, TurnEndEvent(message=message, toolResults=tool_results))

            last_completed_turn = ShouldStopAfterTurnContext(
                message=message,
                toolResults=tool_results,
                context=current_context,
                newMessages=new_messages,
            )
            should_stop = (
                await maybe_await(config.shouldStopAfterTurn(last_completed_turn))
                if config.shouldStopAfterTurn
                else False
            )
            if should_stop:
                await _emit(emit, AgentEndEvent(messages=new_messages[:]))
                return

            pending_messages = list(
                await maybe_await(config.getSteeringMessages()) if config.getSteeringMessages else []
            )

        follow_up_messages = list(
            await maybe_await(config.getFollowUpMessages()) if config.getFollowUpMessages else []
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
        messages = list(await maybe_await(config.transformContext(messages, signal)))

    llm_messages = list(await maybe_await(config.convertToLlm(messages)))
    validated_messages = [validate_message(_model_dump(message)) for message in llm_messages]
    llm_context = Context(
        systemPrompt=context.systemPrompt,
        messages=validated_messages,
        tools=_copy_tools(context.tools),
    )
    stream_function = stream_fn if stream_fn is not None else get_default_stream_fn()
    resolved_api_key = (
        await maybe_await(config.getApiKey(config.model.provider))
        if config.getApiKey
        else None
    ) or config.apiKey
    response_options_payload = _to_mapping(config)
    response_options_payload["apiKey"] = resolved_api_key
    response_options_payload["signal"] = signal
    response_options = StreamOptionsNamespace(**response_options_payload)
    response = await maybe_await(stream_function(config.model, llm_context, response_options))

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
                # One deep copy per event, and it goes to the event -- not to the context.
                # The copy exists to isolate consumers, which is where the risk is: the
                # extension runner hands ``message`` straight to third-party handlers, and
                # the TUI keeps it as ``streamingMessage`` and passes an inner ``arguments``
                # dict to a component by reference. What the *context* needs is the message
                # as it currently stands, which the provider's own ``partial`` already is --
                # and is what ``response.result()`` puts there at the end of the stream
                # anyway. So the second copy that used to feed the context is gone, and the
                # remaining one is the boundary a consumer cannot write through.
                partial_message = event.partial
                context.messages[-1] = partial_message
                await _emit(
                    emit,
                    MessageUpdateEvent(
                        assistantMessageEvent=event,
                        message=event.partial.model_copy(deep=True),
                    ),
                )
            continue

        if event.type in {"done", "error"}:
            final_message = _uniquify_tool_call_ids(await response.result())
            if added_partial:
                context.messages[-1] = final_message
            else:
                context.messages.append(final_message)
                await _emit(emit, MessageStartEvent(message=final_message.model_copy(deep=True)))
            await _emit(emit, MessageEndEvent(message=final_message))
            return final_message

    final_message = _uniquify_tool_call_ids(await response.result())
    if added_partial:
        context.messages[-1] = final_message
    else:
        context.messages.append(final_message)
        await _emit(emit, MessageStartEvent(message=final_message.model_copy(deep=True)))
    await _emit(emit, MessageEndEvent(message=final_message))
    return final_message


def _uniquify_tool_call_ids(message: AssistantMessage) -> AssistantMessage:
    """Hermes per-turn ID normalization, not action deduplication.

    Normalize before message_end persistence; keep Responses item-id suffixes.
    """
    seen = set()
    for index, block in enumerate(message.content):
        if block.type != "toolCall":
            continue
        call_id = block.id.strip().split("|", 1)[0]
        if not call_id:
            continue
        if call_id in seen:
            call_id = next(f"{call_id}_d{n}" for n in range(2, len(seen) + 3)
                           if f"{call_id}_d{n}" not in seen)
            _, separator, item_id = block.id.partition("|")
            message.content[index] = block.model_copy(update={"id": call_id + separator + item_id})
        seen.add(call_id)
    return message


async def execute_tool_calls(
    current_context: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    signal: Any | None,
    emit: AgentEventSink,
) -> ExecutedToolCallBatch:
    _uniquify_tool_call_ids(assistant_message)
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

        if signal_aborted(signal):
            break

    for finalized in await _answer_unreached_tool_calls(tool_calls, len(finalized_calls), emit):
        tool_result_message = create_tool_result_message(finalized)
        await emit_tool_result_message(tool_result_message, emit)
        finalized_calls.append(finalized)
        messages.append(tool_result_message)

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
            if signal_aborted(signal):
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
        if signal_aborted(signal):
            break

    finalized_entries.extend(
        await _answer_unreached_tool_calls(tool_calls, len(finalized_entries), emit)
    )

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


def _reraise_if_not_tool_failure(error: BaseException, signal: Any | None) -> None:
    """Let interpreter-level exits and a caller's cancellation through.

    These handlers turn any exception into an error tool result, which is right for a
    tool that failed but wrong for ``CancelledError``: swallowing it means the caller's
    ``task.cancel()`` never arrives and the loop keeps running. When our own abort
    signal is set the cancellation is ours, and the aborted result is the honest answer.

    ``KeyboardInterrupt`` / ``SystemExit`` are never a tool failure either. pi catches
    ``Exception`` here (agent.ts:502), and JS has no equivalent of these two; catching
    ``BaseException`` without letting them out turns Ctrl-C inside a tool into a
    "the model errored" message while the process keeps running.
    """
    if isinstance(error, KeyboardInterrupt | SystemExit):
        raise error
    if isinstance(error, asyncio.CancelledError) and not signal_aborted(signal):
        raise error


def _aborted_outcome(tool_call: AgentToolCall) -> FinalizedToolCallOutcome:
    return FinalizedToolCallOutcome(
        toolCall=tool_call,
        result=create_error_tool_result("Operation aborted"),
        isError=True,
    )


async def _answer_unreached_tool_calls(
    tool_calls: list[AgentToolCall],
    done: int,
    emit: AgentEventSink,
) -> list[FinalizedToolCallOutcome]:
    """Every tool call in the assistant message needs a result, abort or not.

    Breaking out of the batch used to leave the tail unanswered, and a conversation whose
    assistant message has tool calls without matching results is rejected by the provider
    on the next request -- the abort would surface as a broken session one turn later.
    """
    answered: list[FinalizedToolCallOutcome] = []
    for tool_call in tool_calls[done:]:
        await _emit(
            emit,
            ToolExecutionStartEvent(
                toolCallId=tool_call.id, toolName=tool_call.name, args=tool_call.arguments
            ),
        )
        finalized = _aborted_outcome(tool_call)
        await emit_tool_execution_end(finalized, emit)
        answered.append(finalized)
    return answered


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
        tool = next((candidate for candidate in current_context.tools or []
                     if tool_call.name in getattr(candidate, "aliases", ())), None)
        if tool is not None:
            # Exact names win. Permission/schema checks see the canonical tool,
            # while the transcript retains its historical wire call and call ID.
            tool_call = tool_call.model_copy(update={"name": tool.name})
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
            before_result = await maybe_await(
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
            if signal_aborted(signal):
                return ImmediateToolCallOutcome(
                    kind="immediate",
                    result=create_error_tool_result("Operation aborted"),
                    isError=True,
                )
            if before_result and before_result.block:
                result = create_error_tool_result(before_result.reason or "Tool execution was blocked")
                if before_result.terminate is True:  # pi 1eb988c: blocked calls can still end the batch early
                    result.terminate = True
                return ImmediateToolCallOutcome(
                    kind="immediate",
                    result=result,
                    isError=True,
                )
            if before_result and before_result.updatedInput is not None:
                updated_call = tool_call.model_copy(update={"arguments": before_result.updatedInput})
                validated_args = validate_tool_arguments(tool, updated_call)
        if signal_aborted(signal):
            return ImmediateToolCallOutcome(
                kind="immediate",
                result=create_error_tool_result("Operation aborted"),
                isError=True,
            )
        return PreparedToolCall(kind="prepared", toolCall=tool_call, tool=tool, args=validated_args)
    except BaseException as error:  # noqa: BLE001
        _reraise_if_not_tool_failure(error, signal)
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
    accepting_updates = True

    def on_update(partial_result: AgentToolResult) -> None:
        if not accepting_updates:
            return

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
        result = await maybe_await(
            prepared.tool.execute(prepared.toolCall.id, prepared.args, signal, on_update)
        )
        accepting_updates = False
        if update_tasks:
            await asyncio.gather(*update_tasks)
        return ExecutedToolCallOutcome(result=_coerce_agent_tool_result(result), isError=False)
    except BaseException as error:  # noqa: BLE001
        accepting_updates = False
        if update_tasks:
            await asyncio.gather(*update_tasks, return_exceptions=True)
        _reraise_if_not_tool_failure(error, signal)
        return ExecutedToolCallOutcome(
            result=create_error_tool_result(str(error)),
            isError=True,
        )
    finally:
        accepting_updates = False


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
            after_result = await maybe_await(
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
                # pi spreads the executed result first ({...result, content, details, usage,
                # terminate}), so every field the hook leaves undefined survives — including
                # addedToolNames, which the hook cannot set at all.
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
                    usage=(
                        normalized_after_result.usage
                        if normalized_after_result.usage is not None
                        else result.usage
                    ),
                    addedToolNames=result.addedToolNames,
                    terminate=(
                        normalized_after_result.terminate
                        if normalized_after_result.terminate is not None
                        else result.terminate
                    ),
                )
                is_error = normalized_after_result.isError if normalized_after_result.isError is not None else is_error
        except BaseException as error:  # noqa: BLE001
            _reraise_if_not_tool_failure(error, signal)
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
    added_tool_names = finalized.result.addedToolNames
    return ToolResultMessage(
        toolCallId=finalized.toolCall.id,
        toolName=finalized.toolCall.name,
        # pi agent-loop.ts:780 `content: finalized.result.content ?? []` -- untyped tools
        # (extensions, MCP) can return a result with no content; normalize so the null
        # never enters session history or a provider payload.
        content=[validate_user_content(_model_dump(block)) for block in finalized.result.content or []],
        details=finalized.result.details,
        usage=finalized.result.usage,
        # pi only spreads the key when the list is non-empty, so an empty diff leaves the
        # transcript entry exactly as it was before addedToolNames existed.
        addedToolNames=list(added_tool_names) if added_tool_names else None,
        isError=finalized.isError,
        timestamp=int(time.time() * 1000),
    )


async def emit_tool_result_message(tool_result_message: ToolResultMessage, emit: AgentEventSink) -> None:
    await _emit(emit, MessageStartEvent(message=tool_result_message))
    await _emit(emit, MessageEndEvent(message=tool_result_message))


async def _fail_tool_calls_from_truncated_message(
    tool_calls: list[AgentToolCall],
    emit: AgentEventSink,
) -> ExecutedToolCallBatch:
    """Answer, but never execute, tool calls from a length-truncated response."""
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
        finalized = FinalizedToolCallOutcome(
            toolCall=tool_call,
            result=create_error_tool_result(
                f'Tool call "{tool_call.name}" was not executed: the response hit the '
                "output token limit, so its arguments may be truncated. Re-issue the "
                "tool call with complete arguments; split large payloads into smaller "
                "calls if necessary."
            ),
            isError=True,
        )
        await emit_tool_execution_end(finalized, emit)
        message = create_tool_result_message(finalized)
        await emit_tool_result_message(message, emit)
        messages.append(message)
    return ExecutedToolCallBatch(messages=messages, terminate=False)


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
    await maybe_await(emit(event))


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
    # Shallow, not deep. pi hands `context.tools` straight through (agent-loop.ts:105,
    # 296) and copies nothing; Misaka keeps a per-pass object so a stray attribute write
    # cannot reach the caller's tool list, but the deep walk was doing real damage:
    #   - it re-copied every tool's `parameters` JSON schema on *every* LLM round trip,
    #     which for MCP/extension tools grows with the schema;
    #   - `copy.deepcopy` of a bound method copies `__self__` too, so a tool whose
    #     `execute`/`prepareArguments` is a bound method (the passthrough at
    #     tool_definition_wrapper.py:39) ran against a duplicate of its own object and
    #     any state it recorded was invisible to the original.
    # A shallow copy shares both the schema and the callables, which is what the loop
    # actually needs.
    return [tool.model_copy() for tool in tools]


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


def _coerce_agent_tool_result(value: AgentToolResult | dict[str, Any]) -> AgentToolResult:
    if isinstance(value, AgentToolResult):
        return value
    content = [
        validate_user_content(_model_dump(block))
        for block in value.get("content") or []  # explicit `content: None` is the same as absent
    ]
    added_tool_names = value.get("addedToolNames")
    return AgentToolResult(
        content=content,
        details=value.get("details"),
        usage=value.get("usage"),
        addedToolNames=list(added_tool_names) if added_tool_names else None,
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
        usage=value.get("usage"),
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
        # pi agent-loop.ts:637-644 honours `terminate` on a blocked call regardless of how
        # the hook spelled its result; dropping it here silently disarmed dict-returning hooks.
        terminate=value.get("terminate"),
    )


__all__ = [
    "AgentEventSink",
    "agent_loop",
    "create_tool_result_message",
    "emit_tool_result_message",
    "execute_tool_calls",
    "run_agent_loop",
    "run_agent_loop_continue",
    "stream_assistant_response",
]
