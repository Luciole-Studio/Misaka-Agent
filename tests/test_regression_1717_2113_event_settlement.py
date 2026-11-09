"""Translated from pi's ``packages/coding-agent/test/suite/regressions/1717-2113-agent-session-event-settlement.test.ts``
(v1.1.0)."""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.utils.values import read_field
from tests.suite_harness import createHarness


def createEchoTool() -> AgentTool:
    async def execute(_toolCallId: Any, params: Any, *_args: Any, **_kwargs: Any) -> AgentToolResult:
        text = str(read_field(params, "text") or "")
        return AgentToolResult(content=[TextContent(text=text)], details={"text": text})

    return AgentTool(
        name="echo",
        label="Echo",
        description="Echo text back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        execute=execute,
    )


def _branchMessages(harness: Any) -> list[Any]:
    return [read_field(entry, "message") for entry in harness.sessionManager.getBranch() if read_field(entry, "type") == "message"]


# --- regressions #1717/#2113: agent session event settlement ------------------------------------


async def test_keeps_persisted_assistant_tool_result_message_order_when_extension_message_end_handlers_yield():
    def extension(pi: Any) -> None:
        async def onMessageEnd(event: Any, _ctx: Any) -> None:
            if read_field(read_field(event, "message"), "role") == "assistant":
                await asyncio.sleep(0.02)

        pi.on("message_end", onMessageEnd)

    async with createHarness(tools=[createEchoTool()], extensionFactories=[extension]) as harness:
        harness.setResponses([
            fauxAssistantMessage(
                [fauxToolCall("echo", {"text": "one"}), fauxToolCall("echo", {"text": "two"})], {"stopReason": "toolUse"}
            ),
            fauxAssistantMessage("done"),
        ])
        await harness.session.prompt("run tools")

        branchMessages = _branchMessages(harness)
        roles = [read_field(message, "role") for message in branchMessages]
        assert roles == ["system", "user", "assistant", "toolResult", "toolResult", "assistant"]
        firstToolResultIndex = roles.index("toolResult")
        assert firstToolResultIndex > 0
        assert roles[firstToolResultIndex - 1] == "assistant"


async def test_runs_tool_call_handlers_after_the_assistant_tool_use_message_is_settled_in_the_session():
    branchRolesAtToolCall: list[list[str]] = []
    holder: dict[str, Any] = {}

    def extension(pi: Any) -> None:
        def onToolCall(_event: Any, _ctx: Any) -> None:
            branchRolesAtToolCall.append([read_field(message, "role") for message in _branchMessages(holder["harness"])])

        pi.on("tool_call", onToolCall)

    async with createHarness(tools=[createEchoTool()], extensionFactories=[extension]) as harness:
        holder["harness"] = harness
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("echo", {"text": "hello"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])

        await harness.session.prompt("run tool")

        assert branchRolesAtToolCall == [["system", "user", "assistant"]]
