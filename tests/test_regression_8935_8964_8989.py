"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``8935-parallel-preflight-abort.test.ts``,
``8964-extension-provider-streaming.test.ts`` and ``8989-fork-compaction-label-boundary.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.compat import getApiProvider
from misaka.ai.providers.faux import fauxAssistantMessage, fauxProvider, fauxToolCall
from misaka.ai.types import TextContent
from misaka.core.session_manager import SessionManager
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, getMessageText
from tests.utilities import userMsg

# --- #8935 parallel preflight abort -------------------------------------------------------------


async def test_does_not_start_prepared_tools_after_a_later_preflight_aborts():
    executions: list[str] = []
    preflights: list[str] = []
    resultHooks: list[str] = []

    async def execute(_toolCallId: str, params: Any, *_: Any) -> AgentToolResult:
        value = str(read_field(params, "value")) if read_field(params, "value") is not None else ""
        executions.append(value)
        return AgentToolResult(content=[TextContent(text=value)], details={"value": value})

    externalWrite = AgentTool(
        name="external_write",
        label="External write",
        description="Perform an external write",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
        execute=execute,
    )

    def extension(pi: Any) -> None:
        async def onToolCall(event: Any, ctx: Any) -> None:
            value = read_field(read_field(event, "input"), "value")
            value = "" if value is None else str(value)
            preflights.append(value)
            if value == "second":
                ctx.abort()

        async def onToolResult(event: Any, _ctx: Any) -> None:
            resultHooks.append(read_field(event, "toolCallId"))

        pi.on("tool_call", onToolCall)
        pi.on("tool_result", onToolResult)

    async with createHarness(tools=[externalWrite], extensionFactories=[extension]) as harness:
        harness.setResponses([
            fauxAssistantMessage(
                [
                    fauxToolCall("external_write", {"value": "first"}),
                    fauxToolCall("external_write", {"value": "second"}),
                ],
                {"stopReason": "toolUse"},
            )
        ])

        await harness.session.prompt("run both writes")

        assert preflights == ["first", "second"]
        assert executions == []
        assert resultHooks == []

        starts = harness.eventsOfType("tool_execution_start")
        ends = harness.eventsOfType("tool_execution_end")
        assert len(starts) == 2
        assert len(ends) == 2
        assert {read_field(event, "toolCallId") for event in ends} == {
            read_field(event, "toolCallId") for event in starts
        }
        assert all(read_field(event, "isError") for event in ends)

        toolResults = [message for message in harness.session.messages if read_field(message, "role") == "toolResult"]
        assert [read_field(message, "toolCallId") for message in toolResults] == [
            read_field(event, "toolCallId") for event in starts
        ]
        assert [getMessageText(message) for message in toolResults] == ["Operation aborted", "Operation aborted"]


# --- regression #8964: extensions can stream responses from providers registered with pi.registerProvider() ---


@pytest.mark.parametrize("method", ["stream", "streamSimple"])
async def test_allows_an_extension_command_to_use_ctx_model_registry(method: str):
    faux = fauxProvider({"provider": "extension-provider", "api": "issue-8964-extension-api"})
    received: dict[str, Any] = {"streamedText": ""}

    def respond(_context: Any, options: Any = None, *_: Any) -> Any:
        received["apiKey"] = read_field(options, "apiKey")
        return fauxAssistantMessage("custom provider response")

    faux.setResponses([respond])

    def registerProvider(pi: Any) -> None:
        pi.registerProvider(faux.provider.id, {
            "api": faux.api,
            "baseUrl": faux.getModel().baseUrl,
            "apiKey": "extension-key",
            "models": faux.models,
            "streamSimple": faux.provider.streamSimple,
        })

    def registerCommand(pi: Any) -> None:
        async def handler(_args: str, ctx: Any) -> None:
            model = ctx.modelRegistry.find(faux.provider.id, faux.getModel().id)
            stream = getattr(ctx.modelRegistry, method)(
                model, {"messages": [{"role": "user", "content": "Hello", "timestamp": int(time.time() * 1000)}]}
            )
            async for event in stream:
                if read_field(event, "type") == "text_delta":
                    received["streamedText"] += read_field(event, "delta")
            received["result"] = await stream.result()

        pi.registerCommand("stream-custom", {
            "description": "Stream a response from the custom provider",
            "handler": handler,
        })

    async with createHarness(extensionFactories=[registerProvider, registerCommand]) as harness:
        assert getApiProvider(faux.api) is None
        await harness.session.prompt("/stream-custom")

        assert received.get("apiKey") == "extension-key"
        assert received["streamedText"] == "custom provider response"
        result = received.get("result")
        assert read_field(result, "stopReason") == "stop"
        assert [
            (read_field(block, "type"), read_field(block, "text")) for block in read_field(result, "content")
        ] == [("text", "custom provider response")]
        assert getApiProvider(faux.api) is None


# --- regression #8989 ---------------------------------------------------------------------------


def test_preserves_compaction_context_when_a_fork_removes_the_boundary_label():
    session = SessionManager.inMemory()
    oldId = session.appendMessage(userMsg("old"))
    # findCutPoint() can move a compaction boundary back to this context-invisible label.
    labelId = session.appendLabelChange(oldId, "checkpoint")
    keptId = session.appendMessage(userMsg("kept"))
    compactionId = session.appendCompaction("summary", labelId, 100)
    leafId = session.appendMessage(userMsg("after"))

    session.createBranchedSession(leafId)

    assert read_field(session.getEntry(compactionId), "firstKeptEntryId") == keptId
    messages = session.buildSessionContext().messages
    assert [
        (read_field(message, "role"), read_field(message, "summary") or read_field(message, "content"))
        for message in messages
    ] == [("compactionSummary", "summary"), ("user", "kept"), ("user", "after")]
