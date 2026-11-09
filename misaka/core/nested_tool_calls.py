"""Tool calls that a tool makes while it runs (``ctx.executeTool()``), for example from codemode
scripts. Translated from pi's ``coding-agent/src/core/nested-tool-calls.ts``.

The agent loop does not know about them: the session runs each one through the agent's tool
pipeline (``runToolCall``) with its own hooks, emits ``tool_execution_*`` events with
``parentToolCallId``, and records the calls and their usage on the model-issued call's tool
result message.

Nothing here runs until a tool calls ``ctx.executeTool()``.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from misaka.agent.types import (
    AgentTool,
    AgentToolCall,
    AgentToolCallOutcome,
    AgentToolResult,
    AgentToolUpdateCallback,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
)
from misaka.ai.types import NestedToolCallRecord, NestedToolCalls, ToolCall, Usage
from misaka.core.usage_totals import combineUsage
from misaka.utils.values import read_field


@dataclass(frozen=True, slots=True)
class _NestedCallLimits:
    maxCalls: int = 256
    maxArgumentBytesPerCall: int = 8 * 1024
    maxArgumentBytesTotal: int = 32 * 1024
    maxErrorChars: int = 500


# Limits of the nested-call record on a tool result: arguments over the per-call or total size
# are omitted, calls beyond the count are dropped, and the record is marked incomplete when any
# of that happens.
NESTED_CALL_LIMITS = _NestedCallLimits()


def _now() -> float:
    """``performance.now()``: milliseconds on a monotonic clock."""
    return time.perf_counter() * 1000


@dataclass(slots=True)
class NestedCallSummary:
    """What the nested calls of one model-issued tool call leave on its tool result message."""

    # Becomes `nestedCalls`. None when no nested call was made.
    calls: NestedToolCalls | None
    # Summed `usage` of the nested results, added to the message's `usage`.
    usage: Usage | None


class NestedCallRecorder:
    """Collects the nested calls of one model-issued tool call, including calls made by nested
    tools. The snapshot becomes ``nestedCalls`` on the tool result message."""

    def __init__(self) -> None:
        self._calls: list[NestedToolCallRecord] = []
        # MISAKA fork: keyed by identity; pydantic records are not hashable.
        self._startedAt: dict[int, float] = {}
        self._complete = True
        self._argumentBytes = 0
        # Summed usage of every nested result, including calls dropped from the record.
        self._usage: Usage | None = None

    def start(self, toolCall: AgentToolCall) -> NestedToolCallRecord | None:
        """Record a call as it starts. Returns None when the call is dropped."""
        if len(self._calls) >= NESTED_CALL_LIMITS.maxCalls:
            self._complete = False
            return None
        record = NestedToolCallRecord(id=toolCall.id, name=toolCall.name, status="unfinished")
        arguments = toolCall.arguments if toolCall.arguments is not None else {}
        # `JSON.stringify`: compact, non-ASCII left as is.
        encoded = json.dumps(arguments, separators=(",", ":"), ensure_ascii=False, default=str)
        # surrogatepass: a lone surrogate in a string argument must not fail the call.
        size = len(encoded.encode("utf-8", errors="surrogatepass"))
        if (
            size > NESTED_CALL_LIMITS.maxArgumentBytesPerCall
            or self._argumentBytes + size > NESTED_CALL_LIMITS.maxArgumentBytesTotal
        ):
            record.argumentsBytes = size
            self._complete = False
        else:
            record.arguments = json.loads(encoded)
            self._argumentBytes += size
        self._calls.append(record)
        self._startedAt[id(record)] = _now()
        return record

    def finish(self, record: NestedToolCallRecord | None, isError: bool, errorText: str) -> None:
        if record is None:
            return
        record.status = "error" if isError else "ok"
        started = self._startedAt.pop(id(record), None)
        # `Math.round`: halves round up.
        record.durationMs = math.floor(_now() - (started if started is not None else _now()) + 0.5)
        if isError and errorText:
            record.error = errorText[: NESTED_CALL_LIMITS.maxErrorChars]

    def addUsage(self, usage: Usage) -> None:
        usage = Usage.model_validate(usage) if isinstance(usage, Mapping) else usage
        self._usage = combineUsage(self._usage, usage) if self._usage is not None else usage

    @property
    def totalUsage(self) -> Usage | None:
        return self._usage

    def snapshot(self) -> NestedToolCalls | None:
        """Copy of the record so far, or None when no nested call was made."""
        if len(self._calls) == 0 and self._complete:
            return None
        calls = [call.model_copy(deep=True) for call in self._calls]
        return NestedToolCalls(
            calls=calls, complete=self._complete and all(call.status != "unfinished" for call in calls)
        )


@dataclass(slots=True)
class NestedToolCallOptions:
    # Defaults to the calling tool's signal.
    signal: Any = None
    # Receives partial results of the nested tool, in addition to `tool_execution_update` events.
    onUpdate: AgentToolUpdateCallback | None = None


# `tool_execution_*` events of nested calls, with `parentToolCallId` set.
type NestedToolExecutionEvent = ToolExecutionStartEvent | ToolExecutionUpdateEvent | ToolExecutionEndEvent


class NestedToolCallHost(Protocol):
    def getTools(self) -> Sequence[AgentTool]:
        """Tools nested calls resolve against."""
        ...

    def isSequential(self) -> bool:
        """Whether every nested call runs exclusively, as when the agent executes tool calls
        sequentially."""
        ...

    def runToolCall(
        self,
        toolCall: AgentToolCall,
        parentToolCallId: str,
        signal: Any,
        onUpdate: Callable[[AgentToolResult], Awaitable[None]],
    ) -> Awaitable[AgentToolCallOutcome]:
        """Run the call through the tool pipeline, with hooks that report ``parentToolCallId``."""
        ...

    def emit(self, event: NestedToolExecutionEvent) -> Awaitable[None]: ...


@dataclass(slots=True)
class _CallScope:
    """Calls below one model-issued call share its recorder."""

    recorder: NestedCallRecorder
    nextId: int
    # Set inside a call that holds the exclusive queue, so its own nested calls do not wait on it.
    holdsQueue: bool


def _executionMode(tools: Sequence[AgentTool], name: str) -> str | None:
    tool = next((tool for tool in tools if tool.name == name), None)
    return tool.executionMode if tool is not None else None


def _textOf(result: AgentToolResult) -> str:
    return "\n".join(
        read_field(block, "text", "") for block in (result.content or []) if read_field(block, "type") == "text"
    )


class NestedToolCallRunner:
    def __init__(self, host: NestedToolCallHost) -> None:
        self._host = host
        # Scopes by the id of the calling tool call.
        self._scopes: dict[str, _CallScope] = {}
        # Serializes nested calls that must not run concurrently.
        self._queueTail: asyncio.Future[None] | None = None

    async def execute(
        self,
        callerId: str,
        name: str,
        args: Any,
        options: NestedToolCallOptions | None = None,
    ) -> AgentToolCallOutcome:
        """Run ``name`` on behalf of the call ``callerId``. The nested call gets the id
        ``<callerId>/<n>``. Never raises for tool failures: they come back as ``isError: True``."""
        options = options if options is not None else NestedToolCallOptions()
        scope = self._scopes.get(callerId)
        if scope is None:
            scope = _CallScope(recorder=NestedCallRecorder(), nextId=1, holdsQueue=False)
            self._scopes[callerId] = scope
        # Constructed unvalidated, as upstream casts: arguments that are not an object fail in the
        # tool pipeline as a tool error, not here.
        toolCall = ToolCall.model_construct(
            type="toolCall", id=f"{callerId}/{scope.nextId}", name=name, arguments=args if args is not None else {}
        )
        scope.nextId += 1
        record = scope.recorder.start(toolCall)
        await self._host.emit(
            ToolExecutionStartEvent(
                toolCallId=toolCall.id, toolName=name, args=toolCall.arguments, parentToolCallId=callerId
            )
        )

        exclusive = not scope.holdsQueue and (
            self._host.isSequential() or _executionMode(self._host.getTools(), name) == "sequential"
        )
        release: Callable[[], None] | None = None
        if exclusive:
            previous = self._queueTail
            released: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._queueTail = released

            def release() -> None:
                if not released.done():
                    released.set_result(None)

            if previous is not None and not previous.done():
                try:
                    await asyncio.shield(previous)
                except asyncio.CancelledError:
                    # MISAKA fork: a JavaScript await cannot be cancelled. Hand the queue on once
                    # the previous holder releases it, so later calls keep their order.
                    previous.add_done_callback(lambda _done: release())
                    raise
        self._scopes[toolCall.id] = _CallScope(
            recorder=scope.recorder, nextId=1, holdsQueue=scope.holdsQueue or exclusive
        )

        async def onUpdate(partialResult: AgentToolResult) -> None:
            if options.onUpdate is not None:
                options.onUpdate(partialResult)
            await self._host.emit(
                ToolExecutionUpdateEvent(
                    toolCallId=toolCall.id,
                    toolName=name,
                    args=toolCall.arguments,
                    partialResult=partialResult,
                    parentToolCallId=callerId,
                )
            )

        try:
            outcome = await self._host.runToolCall(toolCall, callerId, options.signal, onUpdate)
        finally:
            self._scopes.pop(toolCall.id, None)
            if release is not None:
                release()

        scope.recorder.finish(record, outcome.isError, _textOf(outcome.result))
        # Nested results are not persisted, so their usage is only counted through the recorder.
        if outcome.result.usage is not None:
            scope.recorder.addUsage(outcome.result.usage)
        await self._host.emit(
            ToolExecutionEndEvent(
                toolCallId=toolCall.id,
                toolName=name,
                result=outcome.result,
                isError=outcome.isError,
                durationMs=outcome.durationMs,
                parentToolCallId=callerId,
            )
        )
        return outcome

    def takeRecord(self, toolCallId: str) -> NestedCallSummary | None:
        """Remove and return the record of the nested calls a model-issued call made."""
        scope = self._scopes.pop(toolCallId, None)
        if scope is None:
            return None
        return NestedCallSummary(calls=scope.recorder.snapshot(), usage=scope.recorder.totalUsage)

    def clear(self) -> None:
        self._scopes.clear()


__all__ = [
    "NESTED_CALL_LIMITS",
    "NestedCallRecorder",
    "NestedCallSummary",
    "NestedToolCallHost",
    "NestedToolCallOptions",
    "NestedToolCallRunner",
    "NestedToolExecutionEvent",
]
