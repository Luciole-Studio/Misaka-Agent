"""Translated from pi's ``packages/coding-agent/test/suite/agent-session-bash-persistence.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.core.bash_executor import BashResult
from misaka.utils.values import read_field
from tests.suite_harness import createHarness


def getEntryTypes(harness: Any) -> list[str]:
    return [read_field(entry, "type") for entry in harness.sessionManager.getEntries()]


def roles(messages: list[Any]) -> list[str]:
    return [read_field(message, "role") for message in messages]


def start(awaitable: Awaitable[Any]) -> asyncio.Task[Any]:
    """A JavaScript async call: it runs at once, up to its first suspension."""
    return asyncio.Task(awaitable, loop=asyncio.get_running_loop(), eager_start=True)  # type: ignore[arg-type]


class Operations:
    def __init__(self, exec: Callable[[str, str, Any], Awaitable[dict[str, Any]]]) -> None:
        self.exec = exec


def createControlledBashOperations(invocations: list[SimpleNamespace]) -> Operations:
    async def exec(_command: str, _cwd: str, options: Any) -> dict[str, Any]:
        finished: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        invocations.append(SimpleNamespace(
            signal=options.get("signal"),
            finish=lambda: finished.done() or finished.set_result({"exitCode": 0}),
        ))
        return await finished

    return Operations(exec)


def _hiResult() -> BashResult:
    return BashResult(output="hi", exitCode=0, cancelled=False, truncated=False)


async def test_records_bash_results_immediately_while_idle():
    async with createHarness() as harness:
        harness.session.recordBashResult("echo hi", _hiResult())

        assert harness.session.hasPendingBashMessages is False
        assert read_field(harness.session.messages[-1], "role") == "bashExecution"
        assert "message" in getEntryTypes(harness)


async def test_defers_bash_results_while_streaming_and_flushes_them_before_the_next_prompt():
    toolRelease: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        await toolRelease
        return AgentToolResult(content=[TextContent(text="released")], details={})

    waitTool = AgentTool(
        name="wait",
        label="Wait",
        description="Wait for release",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )
    async with createHarness(tools=[waitTool]) as harness:
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("wait", {})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
            fauxAssistantMessage("after flush"),
        ])

        sawToolStart: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        unsubscribeRef: dict[str, Any] = {}

        def onEvent(event: Any) -> None:
            if read_field(event, "type") == "tool_execution_start" and not sawToolStart.done():
                unsubscribeRef["unsubscribe"]()
                sawToolStart.set_result(None)

        unsubscribeRef["unsubscribe"] = harness.session.subscribe(onEvent)

        firstPrompt = start(harness.session.prompt("start"))
        await sawToolStart
        harness.session.recordBashResult("echo hi", _hiResult())

        assert harness.session.hasPendingBashMessages is True
        assert "bashExecution" not in roles(harness.session.messages)

        toolRelease.set_result(None)
        await firstPrompt

        assert harness.session.hasPendingBashMessages is False
        assert "bashExecution" in roles(harness.session.messages)

        await harness.session.prompt("next turn")

        assert harness.session.hasPendingBashMessages is False
        assert "bashExecution" in roles(harness.session.messages)
        assert len([kind for kind in getEntryTypes(harness) if kind == "message"]) > 0


async def test_executes_bash_commands_and_records_the_result():
    async with createHarness() as harness:
        result = await harness.session.executeBash("printf 'hello'")

        assert "hello" in result.output
        assert read_field(harness.session.messages[-1], "role") == "bashExecution"


async def test_cancels_running_bash_commands_with_abort_bash():
    async with createHarness() as harness:

        async def exec(_command: str, _cwd: str, options: Any) -> dict[str, Any]:
            rejected: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            signal = options.get("signal")
            if signal is not None:
                signal.addEventListener(
                    "abort", lambda: rejected.done() or rejected.set_exception(RuntimeError("aborted")), once=True
                )
            return await rejected

        bashPromise = start(harness.session.executeBash("sleep", None, {"operations": Operations(exec)}))
        await asyncio.sleep(0)
        assert harness.session.isBashRunning is True
        harness.session.abortBash()

        result = await bashPromise
        assert result.cancelled is True
        assert harness.session.isBashRunning is False


async def test_keeps_newer_bash_execution_tracked_when_an_older_execution_finishes():
    async with createHarness() as harness:
        invocations: list[SimpleNamespace] = []
        operations = createControlledBashOperations(invocations)

        firstBash = start(harness.session.executeBash("first", None, {"operations": operations}))
        secondBash = start(harness.session.executeBash("second", None, {"operations": operations}))

        invocations[0].finish()
        firstResult = await firstBash
        runningAfterFirstSettles = harness.session.isBashRunning

        harness.session.abortBash()
        secondWasAborted = invocations[1].signal.aborted
        invocations[1].finish()
        secondResult = await secondBash

        assert firstResult.cancelled is False
        assert runningAfterFirstSettles is True
        assert secondWasAborted is True
        assert secondResult.cancelled is True
        assert harness.session.isBashRunning is False


async def test_aborts_all_active_bash_executions():
    async with createHarness() as harness:
        invocations: list[SimpleNamespace] = []
        operations = createControlledBashOperations(invocations)

        firstBash = start(harness.session.executeBash("first", None, {"operations": operations}))
        secondBash = start(harness.session.executeBash("second", None, {"operations": operations}))

        harness.session.abortBash()
        abortedSignals = [invocation.signal.aborted for invocation in invocations]
        for invocation in invocations:
            invocation.finish()
        results = await asyncio.gather(firstBash, secondBash)

        assert abortedSignals == [True, True]
        assert [result.cancelled for result in results] == [True, True]
        assert harness.session.isBashRunning is False


async def test_persists_user_assistant_tool_result_and_custom_messages_in_order():
    async def execute(_toolCallId: str, params: Any, *_: Any) -> AgentToolResult:
        text = str(read_field(params, "text")) if read_field(params, "text") is not None else ""
        return AgentToolResult(content=[TextContent(text=f"echo:{text}")], details={"text": text})

    echoTool = AgentTool(
        name="echo",
        label="Echo",
        description="Echo text back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        execute=execute,
    )
    async with createHarness(tools=[echoTool]) as harness:
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("echo", {"text": "hello"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])

        await harness.session.sendCustomMessage(
            {"customType": "note", "content": "hello", "display": True, "details": {"a": 1}}
        )
        await harness.session.prompt("start")

        # The prompt is declared by the first request, after the queued custom message.
        assert getEntryTypes(harness) == ["custom_message", "message", "message", "message", "message", "message"]
        assert roles(harness.session.messages) == ["custom", "system", "user", "assistant", "toolResult", "assistant"]


async def test_does_not_emit_message_end_for_bash_execution_messages():
    async with createHarness() as harness:
        messageEndRoles: list[str] = []

        def onEvent(event: Any) -> None:
            if read_field(event, "type") == "message_end":
                messageEndRoles.append(read_field(read_field(event, "message"), "role"))

        harness.session.subscribe(onEvent)

        harness.session.recordBashResult("echo hi", _hiResult())

        assert messageEndRoles == []


async def test_persists_aborted_assistant_messages():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("x" * 20_000)])

        sawMessageUpdate: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        unsubscribeRef: dict[str, Any] = {}

        def onEvent(event: Any) -> None:
            if read_field(event, "type") == "message_update" and not sawMessageUpdate.done():
                unsubscribeRef["unsubscribe"]()
                sawMessageUpdate.set_result(None)

        unsubscribeRef["unsubscribe"] = harness.session.subscribe(onEvent)

        promptPromise = start(harness.session.prompt("hi"))
        await sawMessageUpdate
        await harness.session.abort()
        await promptPromise

        lastEntry = harness.sessionManager.getEntries()[-1]
        assert read_field(lastEntry, "type") == "message"
        assert read_field(read_field(lastEntry, "message"), "role") == "assistant"
        assert read_field(read_field(lastEntry, "message"), "stopReason") == "aborted"


async def test_records_bash_output_through_custom_operations():
    async with createHarness() as harness:

        async def exec(_command: str, _cwd: str, options: Any) -> dict[str, Any]:
            options["onData"](b"hello from custom ops")
            return {"exitCode": 0}

        result = await harness.session.executeBash("custom", None, {"operations": Operations(exec)})

        assert "hello from custom ops" in result.output
        assert read_field(harness.session.messages[-1], "role") == "bashExecution"


async def test_streams_bash_output_to_the_callback_and_session_events():
    async with createHarness() as harness:
        callbackDeltas: list[str] = []
        eventUpdates: list[dict[str, Any]] = []

        def onEvent(event: Any) -> None:
            if read_field(event, "type") == "bash_execution_update":
                eventUpdates.append({"id": read_field(event, "id"), "delta": read_field(event, "delta")})

        unsubscribe = harness.session.subscribe(onEvent)

        async def exec(_command: str, _cwd: str, options: Any) -> dict[str, Any]:
            options["onData"](b"hello ")
            options["onData"](b"world")
            return {"exitCode": 0}

        await harness.session.executeBash("custom", callbackDeltas.append, {"id": "bash-1", "operations": Operations(exec)})
        unsubscribe()

        assert callbackDeltas == ["hello ", "world"]
        assert eventUpdates == [{"id": "bash-1", "delta": "hello "}, {"id": "bash-1", "delta": "world"}]


# --- Regression tests for https://github.com/earendil-works/pi/issues/10504: escape sequences split across output chunks


async def runChunks(chunks: list[str | bytes], beforeExit: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Runs a user bash command whose fake shell emits the given chunks; `beforeExit` sees what was streamed by then."""
    async with createHarness() as harness:
        deltas: list[str] = []

        async def exec(_command: str, _cwd: str, options: Any) -> dict[str, Any]:
            for chunk in chunks:
                options["onData"](chunk.encode() if isinstance(chunk, str) else chunk)
            if beforeExit is not None:
                beforeExit("".join(deltas))
            return {"exitCode": 0}

        result = await harness.session.executeBash("custom", deltas.append, {"operations": Operations(exec)})
        recorded = harness.session.messages[-1]
        return {
            "output": result.output,
            "streamed": "".join(deltas),
            "recorded": read_field(recorded, "output") if read_field(recorded, "role") == "bashExecution" else None,
        }


async def test_strips_a_color_reset_split_inside_its_parameters():
    result = await runChunks(["\x1b[31mERROR: file.py:1\x1b[0", "m\n"])
    assert result["output"] == "ERROR: file.py:1\n"
    assert result["streamed"] == "ERROR: file.py:1\n"
    assert result["recorded"] == "ERROR: file.py:1\n"


async def test_strips_a_color_code_split_right_after_esc():
    result = await runChunks(["before\x1b", "[32mafter\n"])
    assert result["output"] == "beforeafter\n"
    assert result["streamed"] == "beforeafter\n"


async def test_strips_an_osc_sequence_split_before_its_terminator():
    result = await runChunks(["a\x1b]0;window ", "title\x1b", "\\b\n"])
    assert result["output"] == "ab\n"


async def test_flushes_an_incomplete_multi_byte_character_at_the_end_of_output():
    result = await runChunks(["ok", "\u00e9".encode()[:1]])
    assert result["output"] == "ok\ufffd"
    assert result["streamed"] == "ok\ufffd"


async def test_does_not_hold_back_output_behind_a_long_unterminated_sequence():
    long = "x" * 300
    streamedBeforeExit: dict[str, str] = {"value": ""}

    def beforeExit(streamed: str) -> None:
        streamedBeforeExit["value"] = streamed

    await runChunks([f"\x1b]{long}"], beforeExit)
    assert streamedBeforeExit["value"] == f"]{long}"

