"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``5109-exclude-tools.test.ts``,
``5208-late-bash-output.test.ts``, ``5217-compaction-reason.test.ts`` and ``5303-bash-output-truncation.test.ts``
(v1.1.0)."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools.bash import createBashTool
from misaka.utils.child_process import (
    ChildProcess,
    _ChildProcessProtocol,
    waitForChildProcess,
)
from misaka.utils.js import setTimeout
from misaka.utils.values import read_field
from tests.oauth_fetch import useFakeTimers
from tests.suite_harness import createHarness

# --- regression #5109: exclude tools ------------------------------------------------------------


def toolNames(tools: list[Any]) -> list[str]:
    return sorted(tool.name for tool in tools)


def _registerTools(pi: Any) -> None:
    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text="ok")], details={})

    def onSessionStart(_event: Any, _ctx: Any) -> None:
        pi.registerTool(ToolDefinition(
            name="ask_question",
            label="Ask Question",
            description="Ask a question",
            promptSnippet="Ask a question",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        ))
        pi.registerTool(ToolDefinition(
            name="dynamic_tool",
            label="Dynamic Tool",
            description="Dynamic test tool",
            promptSnippet="Run dynamic test behavior",
            parameters={"type": "object", "properties": {}},
            execute=execute,
        ))

    pi.on("session_start", onSessionStart)


extensionFactories = [_registerTools]


async def test_filters_built_in_and_extension_tools_from_available_and_active_tools():
    async with createHarness(excludedToolNames=["read", "ask_question"], extensionFactories=extensionFactories) as harness:
        await harness.session.bindExtensions({})

        allToolNames = toolNames(harness.session.getAllTools())
        assert "read" not in allToolNames
        assert "ask_question" not in allToolNames
        assert "bash" in allToolNames
        assert "dynamic_tool" in allToolNames
        # MISAKA fork: MISAKA's default tools add office/grep/find/ls to pi's read/bash/edit/write.
        assert sorted(harness.session.getActiveToolNames()) == [
            "bash",
            "dynamic_tool",
            "edit",
            "find",
            "grep",
            "ls",
            "office",
            "write",
        ]
        assert "- read:" not in harness.session.systemPrompt
        assert "ask_question" not in harness.session.systemPrompt
        assert "- dynamic_tool: Run dynamic test behavior" in harness.session.systemPrompt


async def test_lets_excluded_tools_override_the_allowlist():
    async with createHarness(
        allowedToolNames=["read", "bash", "ask_question"],
        excludedToolNames=["read", "ask_question"],
        initialActiveToolNames=["read", "bash", "ask_question"],
        extensionFactories=extensionFactories,
    ) as harness:
        await harness.session.bindExtensions({})

        assert toolNames(harness.session.getAllTools()) == ["bash"]
        assert harness.session.getActiveToolNames() == ["bash"]
        assert "- bash:" in harness.session.systemPrompt
        assert "- read:" not in harness.session.systemPrompt
        assert "ask_question" not in harness.session.systemPrompt


async def test_matches_allowlist_and_denylist_patterns():
    async with createHarness(
        allowedToolNames=["*_tool", "ask_*", "re*"],
        excludedToolNames=["ask*"],
        initialActiveToolNames=["*_tool", "ask_*", "re*"],
        extensionFactories=extensionFactories,
    ) as harness:
        await harness.session.bindExtensions({})

        # Patterns that match a tool activate it like its name.
        assert toolNames(harness.session.getAllTools()) == ["dynamic_tool", "read"]
        assert sorted(harness.session.getActiveToolNames()) == ["dynamic_tool", "read"]


# --- regression #5208: late bash output callbacks -----------------------------------------------


def getTextOutput(result: Any) -> str:
    return "\n".join(
        read_field(block, "text") or ""
        for block in read_field(result, "content") or []
        if read_field(block, "type") == "text"
    )


async def test_ignores_output_callbacks_after_bash_operations_resolve():
    class Operations:
        async def exec(self, _command: str, _cwd: str, options: Any) -> dict[str, int | None]:
            onData = options["onData"]
            onData(b"before\n")
            setTimeout(lambda: onData(b"late\n"), 0)
            return {"exitCode": 0}

    bash = createBashTool(os.getcwd(), {"operations": Operations()})

    result = await bash.execute("test-call-late-output", {"command": "late-output"}, None, None)
    await asyncio.sleep(0.02)

    assert getTextOutput(result).strip() == "before"


# --- issue #5217 compaction reason on extension events ------------------------------------------


def recordingExtension(recorded: list[dict[str, Any]]) -> Any:
    def factory(pi: Any) -> None:
        async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            recorded.append({
                "type": read_field(event, "type"),
                "reason": read_field(event, "reason"),
                "willRetry": read_field(event, "willRetry"),
            })
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": "summary from extension",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "details": {},
                }
            }

        async def onCompact(event: Any, _ctx: Any) -> None:
            recorded.append({
                "type": read_field(event, "type"),
                "reason": read_field(event, "reason"),
                "willRetry": read_field(event, "willRetry"),
            })

        pi.on("session_before_compact", onBeforeCompact)
        pi.on("session_compact", onCompact)

    return factory


async def _promptTwice(harness: Any) -> None:
    harness.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two")])
    await harness.session.prompt("first")
    await harness.session.prompt("second")


async def test_reports_manual_reason_for_compact():
    recorded: list[dict[str, Any]] = []
    async with createHarness(
        settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[recordingExtension(recorded)]
    ) as harness:
        await _promptTwice(harness)

        await harness.session.compact()

        assert recorded == [
            {"type": "session_before_compact", "reason": "manual", "willRetry": False},
            {"type": "session_compact", "reason": "manual", "willRetry": False},
        ]


async def test_reports_threshold_reason_for_auto_compaction():
    recorded: list[dict[str, Any]] = []
    async with createHarness(
        settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[recordingExtension(recorded)]
    ) as harness:
        await _promptTwice(harness)

        await harness.session._runAutoCompaction("threshold", False)

        assert recorded == [
            {"type": "session_before_compact", "reason": "threshold", "willRetry": False},
            {"type": "session_compact", "reason": "threshold", "willRetry": False},
        ]


async def test_reports_overflow_reason_and_will_retry_for_overflow_recovery():
    recorded: list[dict[str, Any]] = []
    async with createHarness(
        settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[recordingExtension(recorded)]
    ) as harness:
        await _promptTwice(harness)

        await harness.session._runAutoCompaction("overflow", True)

        assert recorded == [
            {"type": "session_before_compact", "reason": "overflow", "willRetry": True},
            {"type": "session_compact", "reason": "overflow", "willRetry": True},
        ]


# --- issue #5303 bash output truncation past exit -----------------------------------------------
#
# MISAKA fork: the child process is MISAKA's asyncio subprocess protocol, so the stand-in child drives that protocol
# (data arrives through ``pipe_data_received``, exit through ``process_exited``) where pi's test emits events on an
# EventEmitter with PassThrough streams.


class _FakeTransport:
    def get_returncode(self) -> int:
        return 0

    def get_pipe_transport(self, _fd: int) -> None:
        return None

    def close(self) -> None:
        pass


def createChild(onData: Any = None) -> SimpleNamespace:
    protocol = _ChildProcessProtocol(onData if onData is not None else (lambda _fd, _data: None))
    transport = _FakeTransport()
    protocol.connection_made(transport)  # type: ignore[arg-type]
    return SimpleNamespace(
        child=ChildProcess(transport, protocol),  # type: ignore[arg-type]
        write=lambda text: protocol.pipe_data_received(1, text.encode()),
        exit=protocol.process_exited,
    )


async def test_captures_output_emitted_after_exit_while_a_descendant_holds_stdout_open(monkeypatch: pytest.MonkeyPatch):
    timers = useFakeTimers(monkeypatch)
    output: list[str] = []
    fake = createChild(lambda _fd, chunk: output.append(chunk.decode()))
    state = {"resolved": False}

    async def wait() -> int | None:
        exitCode = await waitForChildProcess(fake.child)
        state["resolved"] = True
        return exitCode

    waiting = asyncio.ensure_future(wait())

    fake.write("HEAD\n")
    fake.exit()
    for index in range(1, 7):
        await timers.advanceAsync(50)
        fake.write(f"TICK{index}\n")
    await timers.advanceAsync(99)
    assert state["resolved"] is False
    await timers.advanceAsync(1)

    assert await waiting == 0
    assert "HEAD" in "".join(output)
    assert "TICK6" in "".join(output)


async def test_resolves_after_the_grace_when_a_descendant_holds_stdout_open_but_stays_quiet(
    monkeypatch: pytest.MonkeyPatch,
):
    timers = useFakeTimers(monkeypatch)
    fake = createChild()
    state = {"resolved": False}

    async def wait() -> int | None:
        exitCode = await waitForChildProcess(fake.child)
        state["resolved"] = True
        return exitCode

    waiting = asyncio.ensure_future(wait())

    fake.write("DONE\n")
    fake.exit()
    await timers.advanceAsync(99)
    assert state["resolved"] is False
    await timers.advanceAsync(1)

    assert await waiting == 0
