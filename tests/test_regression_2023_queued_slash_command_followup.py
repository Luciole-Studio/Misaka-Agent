"""Translated from pi's ``packages/coding-agent/test/suite/regressions/2023-queued-slash-command-followup.test.ts``
(v1.1.0)."""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, getAssistantTexts, getUserTexts


async def test_treats_extension_origin_queued_slash_command_follow_ups_as_raw_user_text_instead_of_dispatching_the_command():
    api: dict[str, Any] = {}
    commandRuns: list[str] = []
    released = asyncio.Event()

    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        await released.wait()
        return AgentToolResult(content=[TextContent(text="released")], details={})

    waitTool = AgentTool(
        name="wait",
        label="Wait",
        description="Wait for the test to release execution",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )

    def extension(pi: Any) -> None:
        api["pi"] = pi

        async def handler(args: str, _ctx: Any = None) -> None:
            commandRuns.append(args)

        pi.registerCommand("testcmd", {"description": "Test command", "handler": handler})

    async with createHarness(tools=[waitTool], extensionFactories=[extension]) as harness:
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("first turn complete"),
            fauxAssistantMessage("queued follow-up handled by model"),
        ])
        sawToolStart = asyncio.Event()
        unsubscribe = harness.session.subscribe(
            lambda event: sawToolStart.set()
            if read_field(event, "type") == "tool_execution_start" and read_field(event, "toolName") == "wait"
            else None
        )

        promptPromise = asyncio.ensure_future(harness.session.prompt("start"))
        await sawToolStart.wait()
        unsubscribe()
        await asyncio.sleep(0)

        api["pi"].sendUserMessage("/testcmd queued", {"deliverAs": "followUp"})
        released.set()
        await promptPromise

        assert commandRuns == []
        assert getUserTexts(harness) == ["start", "/testcmd queued"]
        assert "queued follow-up handled by model" in getAssistantTexts(harness)
