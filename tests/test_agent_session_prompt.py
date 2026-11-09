"""Translated from pi's ``packages/coding-agent/test/suite/agent-session-prompt.test.ts`` (v1.3.0)."""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.ai.utils.transcript import getCurrentSystemPrompt
from misaka.core import agent_session
from misaka.core.prompt_templates import PromptTemplate
from misaka.core.source_info import createSyntheticSourceInfo
from misaka.utils.image_process import ProcessImageSuccess
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, createTestResourceLoader, getMessageText

TINY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="


@pytest.fixture
def processImage(monkeypatch):
    """``vi.mock("../../src/utils/image-process.ts", () => ({ processImage }))``."""
    calls: list[tuple[Any, ...]] = []

    async def fake(data: bytes, mimeType: str, options: Any = None) -> Any:
        calls.append((data, mimeType, options))
        return ProcessImageSuccess(data=base64.b64encode(b"normalized").decode(), mimeType=mimeType, hints=[])

    monkeypatch.setattr(agent_session, "processImage", fake)
    return calls


def _roles(harness: Any) -> list[str]:
    return [read_field(message, "role") for message in harness.session.messages]


def _tool(name: str, execute: Any, properties: dict[str, Any] | None = None) -> AgentTool:
    return AgentTool(
        name=name,
        label=name,
        description=f"{name} tool",
        parameters={"type": "object", "properties": properties or {}},
        execute=execute,
    )


def _waitTool(released: asyncio.Event) -> AgentTool:
    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        await released.wait()
        return AgentToolResult(content=[TextContent(text="released")], details={})

    return _tool("wait", execute)


def _firstUserText(context: Any) -> str:
    user = next((message for message in context.messages if read_field(message, "role") == "user"), None)
    return getMessageText(user) if user is not None else ""


def _template() -> PromptTemplate:
    return PromptTemplate(
        name="review",
        description="Review template",
        content="Review this code: $1",
        filePath="/virtual/review.md",
        sourceInfo=createSyntheticSourceInfo(
            "/virtual/review.md", {"source": "local", "scope": "temporary", "origin": "top-level"}
        ),
    )


def _withPrompts(prompts: list[PromptTemplate]) -> Any:
    loader = createTestResourceLoader()
    loader.getPrompts = lambda: {"prompts": prompts, "diagnostics": []}
    return loader


def _command(pi: Any, runs: Any) -> None:
    async def handler(args: str, _ctx: Any = None) -> None:
        runs(args)

    pi.registerCommand("testcmd", {"description": "Test command", "handler": handler})


# --- AgentSession prompt characterization -------------------------------------------------------


async def test_prompts_while_idle_and_records_a_single_text_response():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("hello")])

        await harness.session.prompt("hi")

        assert _roles(harness) == ["system", "user", "assistant"]
        assert getMessageText(harness.session.messages[1]) == "hi"
        assert harness.getPendingResponseCount() == 0


async def test_handles_a_tool_call_turn_and_waits_for_the_follow_up_llm_response():
    toolRuns: list[str] = []

    async def execute(_toolCallId: Any, params: Any, *_args: Any, **_kwargs: Any) -> AgentToolResult:
        text = str(read_field(params, "text") or "")
        toolRuns.append(text)
        return AgentToolResult(content=[TextContent(text=f"echo:{text}")], details={"text": text})

    async with createHarness(tools=[_tool("echo", execute, {"text": {"type": "string"}})]) as harness:
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("echo", {"text": "hello"}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])

        await harness.session.prompt("start")

        assert toolRuns == ["hello"]
        assert _roles(harness) == ["system", "user", "assistant", "toolResult", "assistant"]


async def test_executes_multiple_tool_calls_from_one_response_and_continues_with_a_single_follow_up_response():
    toolRuns: list[str] = []

    def makeTool(name: str, delayMs: float) -> AgentTool:
        async def execute(_toolCallId: Any, params: Any, *_args: Any, **_kwargs: Any) -> AgentToolResult:
            value = str(read_field(params, "value") or "")
            await asyncio.sleep(delayMs / 1000)
            toolRuns.append(f"{name}:{value}")
            return AgentToolResult(content=[TextContent(text=f"{name}:{value}")], details={"value": value})

        return _tool(name, execute, {"value": {"type": "string"}})

    async with createHarness(tools=[makeTool("slow", 25), makeTool("fast", 0)]) as harness:

        def second(context: Any, *_: Any) -> Any:
            toolResults = [message for message in context.messages if read_field(message, "role") == "toolResult"]
            return fauxAssistantMessage(f"tool results: {len(toolResults)}")

        harness.setResponses([
            fauxAssistantMessage(
                [fauxToolCall("slow", {"value": "a"}), fauxToolCall("fast", {"value": "b"})], {"stopReason": "toolUse"}
            ),
            second,
        ])

        await harness.session.prompt("run tools")

        assert sorted(toolRuns) == ["fast:b", "slow:a"]
        assert len([message for message in harness.session.messages if read_field(message, "role") == "toolResult"]) == 2
        assert read_field(harness.session.messages[-1], "role") == "assistant"


async def test_preserves_image_attachments_in_the_provider_context(processImage):
    async with createHarness() as harness:
        seen = {"image": False}

        def first(context: Any, *_: Any) -> Any:
            user = next((message for message in context.messages if read_field(message, "role") == "user"), None)
            content = read_field(user, "content")
            seen["image"] = not isinstance(content, str) and any(read_field(part, "type") == "image" for part in content)
            return fauxAssistantMessage("ok")

        harness.setResponses([first])

        await harness.session.prompt("describe", {"images": [{"type": "image", "mimeType": "image/png", "data": "ZmFrZQ=="}]})

        assert seen["image"] is True


# Regression test for https://github.com/earendil-works/pi/issues/9631
async def test_uses_the_model_selected_by_before_agent_start_for_image_normalization(processImage):
    holder: dict[str, Any] = {}

    def extension(pi: Any) -> None:
        async def onBeforeAgentStart(_event: Any, _ctx: Any) -> None:
            if holder.get("strict") is None:
                raise RuntimeError("Expected strict model")
            await pi.setModel(holder["strict"])

        pi.on("before_agent_start", onBeforeAgentStart)

    async with createHarness(models=[{"id": "wide"}, {"id": "strict"}], extensionFactories=[extension]) as harness:
        strictModel = harness.getModel("strict")
        resizeOptions = {"maxWidth": 1000, "maxHeight": 1000, "maxBytes": 500000, "jpegQuality": 70}
        strictModel.inputLimits = {"images": {"resize": resizeOptions}}
        holder["strict"] = strictModel
        harness.setResponses([fauxAssistantMessage("done")])

        await harness.session.prompt("inspect", {"images": [{"type": "image", "data": TINY_PNG_BASE64, "mimeType": "image/png"}]})

        assert harness.session.model.id == "strict"
        [(data, mimeType, options)] = processImage
        assert isinstance(data, bytes) and mimeType == "image/png"
        assert read_field(options, "autoResizeImages") is True
        assert {key: read_field(read_field(options, "resizeOptions"), key) for key in resizeOptions} == resizeOptions
        user = next(message for message in harness.session.messages if read_field(message, "role") == "user")
        images = [part for part in read_field(user, "content") if read_field(part, "type") == "image"]
        assert [(read_field(part, "data"), read_field(part, "mimeType")) for part in images] == [
            (base64.b64encode(b"normalized").decode(), "image/png")
        ]


async def test_expands_prompt_templates_before_sending_the_prompt():
    async with createHarness(resourceLoader=_withPrompts([_template()])) as harness:
        expanded = {"prompt": ""}

        def first(context: Any, *_: Any) -> Any:
            expanded["prompt"] = _firstUserText(context)
            return fauxAssistantMessage("ok")

        harness.setResponses([first])

        await harness.session.prompt("/review src/index.ts")

        assert expanded["prompt"] == "Review this code: src/index.ts"


async def test_send_user_message_can_opt_into_prompt_template_expansion():
    async with createHarness(resourceLoader=_withPrompts([_template()])) as harness:
        expanded = {"prompt": ""}

        def first(context: Any, *_: Any) -> Any:
            expanded["prompt"] = _firstUserText(context)
            return fauxAssistantMessage("ok")

        harness.setResponses([first])

        await harness.session.sendUserMessage("/review src/index.ts", {"expandPromptTemplates": True})

        assert expanded["prompt"] == "Review this code: src/index.ts"


async def test_dispatches_extension_commands_without_consuming_a_provider_response():
    commandRuns: list[str] = []
    async with createHarness(extensionFactories=[lambda pi: _command(pi, commandRuns.append)]) as harness:
        harness.setResponses([fauxAssistantMessage("should stay queued")])

        await harness.session.prompt("/testcmd hello world")

        assert commandRuns == ["hello world"]
        assert harness.session.messages == []
        assert harness.getPendingResponseCount() == 1


async def test_extension_send_user_message_can_opt_into_extension_command_dispatch():
    api: dict[str, Any] = {}
    commandRun: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def extension(pi: Any) -> None:
        api["pi"] = pi
        _command(pi, lambda args: commandRun.done() or commandRun.set_result(args))

    async with createHarness(extensionFactories=[extension]) as harness:
        assert api.get("pi") is not None

        api["pi"].sendUserMessage("/testcmd hello world", {"expandPromptTemplates": True})

        assert await asyncio.wait_for(commandRun, 5) == "hello world"
        assert harness.session.messages == []
        assert harness.getPendingResponseCount() == 0


async def test_send_user_message_while_idle_triggers_a_turn():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("response")])

        await harness.session.sendUserMessage("from extension")

        assert _roles(harness) == ["system", "user", "assistant"]
        assert getMessageText(harness.session.messages[1]) == "from extension"


async def test_does_not_report_streaming_behavior_to_input_handlers_while_idle():
    inputEvents: list[Any] = []
    async with createHarness(extensionFactories=[lambda pi: pi.on("input", lambda event, _ctx: inputEvents.append(event))]) as harness:
        harness.setResponses([fauxAssistantMessage("ok")])

        await harness.session.prompt("idle", {"streamingBehavior": "followUp"})

        assert len(inputEvents) == 1
        assert read_field(inputEvents[0], "streamingBehavior") is None


async def test_reports_streaming_behavior_to_input_handlers_while_streaming():
    released = asyncio.Event()
    inputEvents: list[Any] = []
    async with createHarness(
        tools=[_waitTool(released)],
        extensionFactories=[lambda pi: pi.on("input", lambda event, _ctx: inputEvents.append(event))],
    ) as harness:
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])
        started = asyncio.Event()
        unsubscribe = harness.session.subscribe(
            lambda event: started.set() if read_field(event, "type") == "tool_execution_start" else None
        )

        promptPromise = asyncio.ensure_future(harness.session.prompt("start"))
        await started.wait()
        unsubscribe()
        await harness.session.prompt("queued", {"streamingBehavior": "followUp"})

        assert [read_field(event, "streamingBehavior") for event in inputEvents] == [None, "followUp"]

        released.set()
        await promptPromise


async def test_throws_when_prompted_during_streaming_without_a_streaming_behavior():
    released = asyncio.Event()
    async with createHarness(tools=[_waitTool(released)]) as harness:
        harness.setResponses([
            fauxAssistantMessage(fauxToolCall("wait", {}), {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])
        started = asyncio.Event()
        unsubscribe = harness.session.subscribe(
            lambda event: started.set() if read_field(event, "type") == "tool_execution_start" else None
        )

        promptPromise = asyncio.ensure_future(harness.session.prompt("start"))
        await started.wait()
        unsubscribe()

        with pytest.raises(Exception, match=re.escape(
            "Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') to queue the message."
        )):
            await harness.session.prompt("second")

        released.set()
        await promptPromise


async def test_throws_when_prompted_during_manual_compaction():
    compactionStarted = asyncio.Event()
    compactionReleased = asyncio.Event()

    def extension(pi: Any) -> None:
        async def onBeforeCompact(event: Any, _ctx: Any) -> Any:
            compactionStarted.set()
            await compactionReleased.wait()
            preparation = read_field(event, "preparation")
            return {
                "compaction": {
                    "summary": "manual compacted",
                    "firstKeptEntryId": read_field(preparation, "firstKeptEntryId"),
                    "tokensBefore": read_field(preparation, "tokensBefore"),
                    "details": {},
                }
            }

        pi.on("session_before_compact", onBeforeCompact)

    async with createHarness(settings={"compaction": {"keepRecentTokens": 1}}, extensionFactories=[extension]) as harness:
        harness.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two")])
        await harness.session.prompt("first")
        await harness.session.prompt("second")

        compactPromise = asyncio.ensure_future(harness.session.compact())
        await compactionStarted.wait()

        try:
            with pytest.raises(Exception, match=re.escape(
                "Cannot submit a prompt while compaction is in progress. Wait for compaction to finish and retry."
            )):
                await harness.session.prompt("third")
        finally:
            compactionReleased.set()
            await compactPromise


async def test_throws_when_prompting_without_a_model():
    async with createHarness() as harness:
        harness.session.agent.state.model = None

        with pytest.raises(Exception, match=re.escape("No model selected.")):
            await harness.session.prompt("hi")


async def test_throws_when_prompting_without_configured_auth():
    async with createHarness(withConfiguredAuth=False) as harness:
        with pytest.raises(Exception, match=re.escape(f"No API key found for {harness.getModel().provider}.")):
            await harness.session.prompt("hi")



# MISAKA fork: "expands skill commands before sending the prompt" is not ported -- skills are MISAKA's own layer
# system (core/skills), which reads no `getSkills()` from the resource loader; tests/test_skill_command_delivery.py
# covers `/skill:<name>` expansion.


# Issue #10267, #5581: a run started by sendMessage(..., { triggerTurn: true }) skipped
# before_agent_start, so its second request patched away the sections handlers had added.
async def test_custom_message_run_keeps_the_same_system_prompt_for_every_request_of_the_run():
    events: list[Any] = []

    async def noop(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text="ok")], details={})

    def extension(pi: Any) -> None:
        def onBeforeAgentStart(event: Any, _ctx: Any) -> None:
            events.append(event)
            event["systemPromptOptions"]["sections"]["extension_rules"] = "Answer in one word."

        pi.on("before_agent_start", onBeforeAgentStart)

    async with createHarness(tools=[_tool("noop", noop)], extensionFactories=[extension]) as harness:
        prompts: list[str] = []

        def record(response: Any) -> Any:
            def step(context: Any, *_: Any) -> Any:
                prompts.append(getCurrentSystemPrompt(context.messages))
                return response

            return step

        harness.setResponses([
            record(fauxAssistantMessage("hi")),
            record(fauxAssistantMessage([fauxToolCall("noop", {})], {"stopReason": "toolUse"})),
            record(fauxAssistantMessage("done")),
        ])

        await harness.session.prompt("hello")
        await harness.session.sendCustomMessage(
            {"customType": "task-done", "content": "background task finished", "display": True},
            {"triggerTurn": True},
        )

        assert len(prompts) == 3
        assert "Answer in one word." in prompts[0]
        assert len(set(prompts)) == 1
        assert [event["messageType"] for event in events] == ["user", "custom"]
        assert read_field(events[1]["message"], "customType") == "task-done"


async def test_queues_a_custom_message_sent_while_before_agent_start_runs_into_the_starting_run():
    handlerStarted = asyncio.Event()
    releaseHandler = asyncio.Event()

    def extension(pi: Any) -> None:
        async def onBeforeAgentStart(event: Any, _ctx: Any) -> None:
            if event["messageType"] != "custom" or read_field(event["message"], "customType") != "first":
                return
            handlerStarted.set()
            await releaseHandler.wait()

        pi.on("before_agent_start", onBeforeAgentStart)

    async with createHarness(extensionFactories=[extension]) as harness:
        requests: list[Any] = []

        def respond(context: Any, *_: Any) -> Any:
            requests.append(context)
            return fauxAssistantMessage("handled both")

        harness.setResponses([respond])

        first = asyncio.ensure_future(harness.session.sendCustomMessage(
            {"customType": "first", "content": "task one finished", "display": True},
            {"triggerTurn": True},
        ))
        await handlerStarted.wait()
        assert harness.session.isIdle is False
        second = asyncio.ensure_future(harness.session.sendCustomMessage(
            {"customType": "second", "content": "task two finished", "display": True},
            {"triggerTurn": True},
        ))
        releaseHandler.set()
        await asyncio.gather(first, second)

        assert len(requests) == 1
        texts = [getMessageText(message) for message in requests[0].messages if read_field(message, "role") == "user"]
        assert texts == ["task one finished", "task two finished"]


async def test_includes_a_custom_message_sent_without_trigger_turn_while_the_run_starts_in_its_first_request():
    def extension(pi: Any) -> None:
        def onBeforeAgentStart(_event: Any, _ctx: Any) -> None:
            pi.sendMessage(
                {"customType": "note", "content": "context from handler", "display": True},
                {"triggerTurn": False},
            )

        pi.on("before_agent_start", onBeforeAgentStart)

    async with createHarness(extensionFactories=[extension]) as harness:
        requests: list[Any] = []

        def respond(context: Any, *_: Any) -> Any:
            requests.append(context)
            return fauxAssistantMessage("done")

        harness.setResponses([respond])

        await harness.session.prompt("hello")

        assert len(requests) == 1
        assert "context from handler" in [getMessageText(message) for message in requests[0].messages]


async def test_aborts_a_run_when_abort_is_requested_while_before_agent_start_runs():
    handlerStarted = asyncio.Event()
    releaseHandler = asyncio.Event()

    def extension(pi: Any) -> None:
        async def onBeforeAgentStart(_event: Any, _ctx: Any) -> None:
            handlerStarted.set()
            await releaseHandler.wait()

        pi.on("before_agent_start", onBeforeAgentStart)

    async with createHarness(extensionFactories=[extension]) as harness:
        harness.setResponses([fauxAssistantMessage("should not complete")])

        run = asyncio.ensure_future(harness.session.sendCustomMessage(
            {"customType": "task-done", "content": "background task finished", "display": True},
            {"triggerTurn": True},
        ))
        await handlerStarted.wait()
        aborted = asyncio.ensure_future(harness.session.abort())
        await asyncio.sleep(0)
        releaseHandler.set()
        await asyncio.gather(run, aborted)

        last = harness.session.messages[-1]
        assert read_field(last, "role") == "assistant"
        assert read_field(last, "stopReason") == "aborted"
        assert harness.session.isIdle is True
