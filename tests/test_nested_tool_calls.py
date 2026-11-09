"""pi coding-agent's ``nested-tool-calls.test.ts`` and ``compaction-nested-calls.test.ts``."""

from __future__ import annotations

import asyncio

import pytest

from misaka.agent.types import AgentTool, AgentToolCallOutcome, AgentToolResult
from misaka.ai.types import (
    NestedToolCalls,
    TextContent,
    ToolCall,
    ToolResultMessage,
    Usage,
)
from misaka.core.compaction.utils import (
    computeFileLists,
    createFileOps,
    extractFileOpsFromMessage,
)
from misaka.core.nested_tool_calls import (
    NESTED_CALL_LIMITS,
    NestedCallRecorder,
    NestedToolCallRunner,
)

EMPTY = {"type": "object", "properties": {}}


def usage(input: int, cost: float) -> Usage:
    return Usage.model_validate({
        "input": input, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": input,
        "cost": {"input": cost, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": cost},
    })


def tool(name: str, execute, **extra) -> AgentTool:
    return AgentTool(name=name, label=name, description=name, parameters=EMPTY, execute=execute, **extra)


def createRunner(tools: list[AgentTool], *, sequential: bool = False):
    events: list = []

    class Host:
        def getTools(self):
            return tools

        def isSequential(self):
            return sequential

        async def runToolCall(self, toolCall, _parentId, signal, onUpdate):
            found = next((candidate for candidate in tools if candidate.name == toolCall.name), None)
            if found is None:
                return AgentToolCallOutcome(
                    toolCall=toolCall,
                    result=AgentToolResult(content=[TextContent(text=f"Tool {toolCall.name} not found")], details={}),
                    isError=True,
                )
            pending: list = []
            result = await found.execute(
                toolCall.id, toolCall.arguments, signal, lambda partial: pending.append(asyncio.ensure_future(onUpdate(partial)))
            )
            await asyncio.gather(*pending)
            return AgentToolCallOutcome(toolCall=toolCall, result=result, isError=result.isError is True)

        async def emit(self, event):
            events.append(event)

    return NestedToolCallRunner(Host()), events


def _dump(calls: NestedToolCalls | None):
    return calls.model_dump(exclude_none=True) if calls is not None else None


# --- NestedToolCallRunner ----------------------------------------------------------------------


async def test_assigns_ids_below_the_caller_emits_events_with_the_parent_id_and_records_the_calls():
    async def echo(_id, _params, _signal, onUpdate):
        onUpdate(AgentToolResult(content=[TextContent(text="partial")], details={}))
        return AgentToolResult(content=[TextContent(text="ok")], details={})

    runner, events = createRunner([tool("echo", echo)])
    updates: list = []

    first = await runner.execute("call", "echo", {"a": 1}, _options(onUpdate=updates.append))
    missing = await runner.execute("call", "missing", {})

    assert first.toolCall.id == "call/1"
    assert (missing.toolCall.id, missing.isError) == ("call/2", True)
    assert len(updates) == 1
    assert [(event.type, event.toolCallId, event.parentToolCallId) for event in events] == [
        ("tool_execution_start", "call/1", "call"),
        ("tool_execution_update", "call/1", "call"),
        ("tool_execution_end", "call/1", "call"),
        ("tool_execution_start", "call/2", "call"),
        ("tool_execution_end", "call/2", "call"),
    ]
    recorded = _dump(runner.takeRecord("call").calls)
    for call in recorded["calls"]:
        assert isinstance(call.pop("durationMs"), int)
    assert recorded == {
        "calls": [
            {"id": "call/1", "name": "echo", "arguments": {"a": 1}, "status": "ok"},
            {"id": "call/2", "name": "missing", "arguments": {}, "status": "error", "error": "Tool missing not found"},
        ],
        "complete": True,
    }
    # The record is taken once.
    assert runner.takeRecord("call") is None
    assert runner.takeRecord("other") is None


def _options(**kwargs):
    from misaka.core.nested_tool_calls import NestedToolCallOptions

    return NestedToolCallOptions(**kwargs)


async def test_records_calls_of_nested_tools_on_the_model_issued_call():
    async def leaf(*_args):
        return AgentToolResult(content=[], details={})

    tools = [tool("leaf", leaf)]
    runner, _events = createRunner(tools)

    async def middle(toolCallId, *_args):
        await runner.execute(toolCallId, "leaf", {})
        return AgentToolResult(content=[], details={})

    tools.append(tool("middle", middle))

    await runner.execute("call", "middle", {})

    assert [call.id for call in runner.takeRecord("call").calls.calls] == ["call/1", "call/1/1"]


async def test_sums_the_usage_of_nested_results_at_every_depth():
    async def leaf(*_args):
        return AgentToolResult(content=[], details={}, usage=usage(10, 0.01))

    async def plain(*_args):
        return AgentToolResult(content=[], details={})

    tools = [tool("leaf", leaf), tool("plain", plain)]
    runner, _events = createRunner(tools)

    async def middle(toolCallId, *_args):
        await runner.execute(toolCallId, "leaf", {})
        # Its own usage only: the leaf's usage is counted once, by the recorder.
        return AgentToolResult(content=[], details={}, usage=usage(5, 0.005))

    tools.append(tool("middle", middle))

    await runner.execute("call", "middle", {})
    await runner.execute("call", "leaf", {})
    await runner.execute("call", "plain", {})
    await runner.execute("free", "plain", {})

    summary = runner.takeRecord("call")
    assert summary.usage.input == 25
    assert summary.usage.cost.total == pytest.approx(0.025, abs=1e-10)
    free = runner.takeRecord("free")
    assert free.calls.complete is True
    assert free.usage is None


async def test_serializes_concurrent_calls_to_sequential_tools():
    active = 0
    maxActive = {"sequential": 0, "parallel": 0}

    def makeTool(name: str) -> AgentTool:
        async def execute(*_args):
            nonlocal active
            active += 1
            maxActive[name] = max(maxActive[name], active)
            await asyncio.sleep(0.005)
            active -= 1
            return AgentToolResult(content=[], details={})

        return tool(name, execute, executionMode="sequential" if name == "sequential" else None)

    runner, _events = createRunner([makeTool("sequential"), makeTool("parallel")])

    await asyncio.gather(*(runner.execute("call", "sequential", {}) for _ in range(3)))
    await asyncio.gather(*(runner.execute("call", "parallel", {}) for _ in range(3)))

    assert maxActive == {"sequential": 1, "parallel": 3}


async def test_a_cancelled_waiter_hands_the_queue_on_in_order():
    # MISAKA fork: an awaiting task can be cancelled here, a JavaScript await cannot.
    order: list[str] = []
    gate = asyncio.Event()

    async def execute(toolCallId, *_args):
        order.append(toolCallId)
        if toolCallId == "call/1":
            await gate.wait()
        return AgentToolResult(content=[], details={})

    runner, _events = createRunner([tool("seq", execute, executionMode="sequential")])
    first = asyncio.ensure_future(runner.execute("call", "seq", {}))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(runner.execute("call", "seq", {}))
    third = asyncio.ensure_future(runner.execute("call", "seq", {}))
    await asyncio.sleep(0.01)
    second.cancel()
    gate.set()
    await first
    await asyncio.wait_for(third, 5)

    assert order == ["call/1", "call/3"]


# --- NestedCallRecorder ------------------------------------------------------------------------


def call(id: str, args) -> ToolCall:
    return ToolCall(id=id, name="t", arguments=args)


def test_omits_oversized_arguments_and_drops_calls_beyond_the_limit():
    recorder = NestedCallRecorder()
    assert recorder.snapshot() is None
    small = recorder.start(call("a", {"x": 1}))
    recorder.finish(small, False, "")
    snapshot = _dump(recorder.snapshot())
    assert isinstance(snapshot["calls"][0].pop("durationMs"), int)
    assert snapshot == {"calls": [{"id": "a", "name": "t", "arguments": {"x": 1}, "status": "ok"}], "complete": True}

    big = recorder.start(call("b", {"text": "x" * NESTED_CALL_LIMITS.maxArgumentBytesPerCall}))
    recorder.finish(big, True, "e" * 1000)
    snapshot = recorder.snapshot()
    assert snapshot.complete is False
    assert (snapshot.calls[1].id, snapshot.calls[1].status) == ("b", "error")
    assert snapshot.calls[1].arguments is None
    assert snapshot.calls[1].argumentsBytes > NESTED_CALL_LIMITS.maxArgumentBytesPerCall
    assert len(snapshot.calls[1].error) == NESTED_CALL_LIMITS.maxErrorChars

    for i in range(NESTED_CALL_LIMITS.maxCalls):
        recorder.finish(recorder.start(call(f"c{i}", {})), False, "")
    assert len(recorder.snapshot().calls) == NESTED_CALL_LIMITS.maxCalls


def test_caps_the_total_argument_size_and_marks_unfinished_calls_incomplete():
    recorder = NestedCallRecorder()
    chunk = {"text": "x" * 7000}
    records = [recorder.start(call(f"c{i}", chunk)) for i in range(6)]
    snapshot = recorder.snapshot()
    # 32 KiB fits four 7000-byte argument objects.
    assert len([entry for entry in snapshot.calls if entry.arguments is not None]) == 4
    assert all(entry.status == "unfinished" for entry in snapshot.calls)
    assert snapshot.complete is False
    assert len(records) == 6


# --- compaction-nested-calls.test.ts ----------------------------------------------------------


def test_compaction_file_operations_include_files_touched_by_nested_calls():
    result = ToolResultMessage.model_validate({
        "role": "toolResult", "toolCallId": "codemode-1", "toolName": "codemode", "content": [], "isError": False,
        "timestamp": 0,
        "nestedCalls": {
            "calls": [
                {"id": "codemode-1/1", "name": "read", "arguments": {"path": "a.ts"}, "status": "ok"},
                {"id": "codemode-1/2", "name": "edit", "arguments": {"path": "b.ts", "edits": []}, "status": "ok"},
                {"id": "codemode-1/3", "name": "write", "argumentsBytes": 40000, "status": "ok"},
            ],
            "complete": False,
        },
    })
    fileOps = createFileOps()
    extractFileOpsFromMessage(result, fileOps)
    assert computeFileLists(fileOps) == {"readFiles": ["a.ts"], "modifiedFiles": ["b.ts"]}
