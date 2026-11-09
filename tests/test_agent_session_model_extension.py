"""Translated from pi's ``packages/coding-agent/test/suite/agent-session-model-extension.test.ts`` (v1.1.0)."""

from __future__ import annotations

import re
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent, Usage
from misaka.ai.utils.transcript import getCurrentSystemPrompt
from misaka.utils.values import read_field
from tests.suite_harness import createHarness, getAssistantTexts, getMessageText

TWO = [{"id": "faux-1", "name": "One", "reasoning": True}, {"id": "faux-2", "name": "Two", "reasoning": True}]


def _echo(execute: Any) -> AgentTool:
    return AgentTool(
        name="echo",
        label="Echo",
        description="Echo text back",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        execute=execute,
    )


def _toolResultText(context: Any) -> str:
    toolResult = next((message for message in context.messages if read_field(message, "role") == "toolResult"), None)
    return getMessageText(toolResult) if toolResult is not None else ""


def _userText(context: Any) -> str:
    user = next((message for message in context.messages if read_field(message, "role") == "user"), None)
    if user is None or isinstance(read_field(user, "content"), str):
        return ""
    return getMessageText(user)


# --- AgentSession model and extension characterization -----------------------------------------


async def test_set_model_saves_the_model_to_the_session_and_emits_model_select():
    modelEvents: list[str] = []

    def extension(pi: Any) -> None:
        async def onModelSelect(event: Any, _ctx: Any) -> None:
            previous = read_field(event, "previousModel")
            modelEvents.append(
                f"{read_field(previous, 'id') if previous is not None else 'none'}->{read_field(read_field(event, 'model'), 'id')}:"
                f"{read_field(event, 'source')}"
            )

        pi.on("model_select", onModelSelect)

    async with createHarness(models=TWO, extensionFactories=[extension]) as harness:
        nextModel = harness.getModel("faux-2")

        await harness.session.setModel(nextModel)

        assert harness.session.model.id == "faux-2"
        assert modelEvents == ["faux-1->faux-2:set"]
        assert [
            f"{read_field(entry, 'provider')}/{read_field(entry, 'modelId')}"
            for entry in harness.sessionManager.getEntries()
            if read_field(entry, "type") == "model_change"
        ] == [f"{nextModel.provider}/{nextModel.id}"]
        assert harness.settingsManager.getDefaultProvider() is None
        assert harness.settingsManager.getDefaultModel() is None


async def test_only_persists_model_and_thinking_defaults_when_requested():
    async with createHarness(models=TWO) as harness:
        nextModel = harness.getModel("faux-2")

        await harness.session.setModel(nextModel)
        assert harness.settingsManager.getDefaultProvider() is None
        assert harness.settingsManager.getDefaultModel() is None

        harness.session.setThinkingLevel("low")
        assert harness.settingsManager.getDefaultThinkingLevel() is None

        await harness.session.setModel(nextModel, {"persist": True})
        assert harness.settingsManager.getDefaultProvider() == nextModel.provider
        assert harness.settingsManager.getDefaultModel() == nextModel.id

        harness.session.setThinkingLevel("high", {"persist": True})
        assert harness.settingsManager.getDefaultThinkingLevel() == "high"


async def test_persists_the_requested_default_thinking_level_even_when_the_current_model_clamps_it():
    async with createHarness(models=[{"id": "faux-1", "reasoning": True}]) as harness:
        harness.session.setThinkingLevel("max", {"persist": True})

        assert harness.session.thinkingLevel == "high"
        assert harness.settingsManager.getDefaultThinkingLevel() == "max"


async def test_cycle_model_and_cycle_thinking_level_are_session_only_by_default():
    settings = {"defaultProvider": "faux", "defaultModel": "faux-1", "defaultThinkingLevel": "low"}
    async with createHarness(models=TWO, settings=settings) as harness:
        await harness.session.cycleModel()
        assert harness.session.model.id == "faux-2"
        assert harness.settingsManager.getDefaultModel() == "faux-1"

        harness.session.setThinkingLevel("off")
        assert harness.session.cycleThinkingLevel() == "minimal"
        assert harness.settingsManager.getDefaultThinkingLevel() == "low"


async def test_applies_per_model_thinking_level_override_on_model_switch():
    async with createHarness(models=TWO, settings={"defaultThinkingLevel": "medium"}) as harness:
        # Set a per-model override for faux-2
        harness.settingsManager.setModelThinkingLevel("faux", "faux-2", "low")

        # Session starts on faux-1 with default thinking
        harness.session.setThinkingLevel("high")
        assert harness.session.thinkingLevel == "high"

        # Switch to faux-2 -> per-model override should apply
        await harness.session.setModel(harness.getModel("faux-2"))
        assert harness.session.thinkingLevel == "low"

        # Switch back to faux-1 -> no per-model override, uses global default
        await harness.session.setModel(harness.getModel("faux-1"))
        assert harness.session.thinkingLevel == "medium"


async def test_falls_back_to_current_session_thinking_level_when_no_per_model_or_global_default_is_configured():
    async with createHarness(models=TWO) as harness:
        harness.session.setThinkingLevel("high")
        await harness.session.setModel(harness.getModel("faux-2"))
        assert harness.session.thinkingLevel == "high"


async def test_per_model_override_takes_priority_over_global_default_during_model_switch():
    settings = {"defaultThinkingLevel": "high", "modelThinkingLevels": {"faux/faux-2": "minimal"}}
    async with createHarness(models=TWO, settings=settings) as harness:
        # Start on a non-thinking model, then switch to faux-2
        await harness.session.setModel(harness.getModel("faux-2"))
        assert harness.session.thinkingLevel == "minimal"


async def test_cycles_through_scoped_models_and_preserves_the_scoped_thinking_preference():
    async with createHarness(
        models=[{"id": "faux-1", "name": "One", "reasoning": True}, {"id": "faux-2", "name": "Two", "reasoning": False}]
    ) as harness:
        modelOne = harness.getModel("faux-1")
        modelTwo = harness.getModel("faux-2")
        harness.session.setScopedModels([{"model": modelOne, "thinkingLevel": "high"}, {"model": modelTwo}])
        harness.session.setThinkingLevel("high")

        await harness.session.cycleModel()
        assert harness.session.model.id == "faux-2"
        assert harness.session.thinkingLevel == "off"

        await harness.session.cycleModel()
        assert harness.session.model.id == "faux-1"
        assert harness.session.thinkingLevel == "high"


async def test_clamps_thinking_levels_to_model_capabilities_and_cycles_available_levels():
    async with createHarness(models=[{"id": "faux-1", "reasoning": False}]) as harness:
        harness.session.setThinkingLevel("high")
        assert harness.session.thinkingLevel == "off"
        assert harness.session.cycleThinkingLevel() is None


async def test_cycles_xhigh_before_max_when_both_are_supported():
    async with createHarness(models=[{"id": "faux-1", "reasoning": True}]) as harness:
        harness.getModel().thinkingLevelMap = {"xhigh": "xhigh", "max": "max"}

        assert harness.session.getAvailableThinkingLevels() == ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
        harness.session.setThinkingLevel("high")
        assert harness.session.cycleThinkingLevel() == "xhigh"
        assert harness.session.cycleThinkingLevel() == "max"
        assert harness.session.cycleThinkingLevel() == "off"


async def test_throws_when_set_model_is_called_without_configured_auth():
    async with createHarness(models=TWO, withConfiguredAuth=False) as harness:
        with pytest.raises(Exception, match=re.escape(f"No API key for {harness.getModel().provider}/faux-2")):
            await harness.session.setModel(harness.getModel("faux-2"))


async def test_allows_extension_tool_call_handlers_to_block_tool_execution():
    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        raise RuntimeError("tool should have been blocked")

    def extension(pi: Any) -> None:
        async def onToolCall(_event: Any, _ctx: Any) -> Any:
            return {"block": True, "reason": "Blocked by test"}

        pi.on("tool_call", onToolCall)

    async with createHarness(tools=[_echo(execute)], extensionFactories=[extension]) as harness:
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("echo", {"text": "hello"})], {"stopReason": "toolUse"}),
            lambda context, *_: fauxAssistantMessage(_toolResultText(context)),
        ])

        await harness.session.prompt("hi")

        assert "Blocked by test" in getAssistantTexts(harness)
        assert any(
            read_field(message, "role") == "toolResult" and read_field(message, "isError")
            for message in harness.session.messages
        )


async def test_allows_extension_tool_result_handlers_to_modify_tool_results():
    toolUsage = Usage(
        input=1, output=2, cacheRead=3, cacheWrite=4, totalTokens=10,
        cost={"input": 0.1, "output": 0.2, "cacheRead": 0.3, "cacheWrite": 0.4, "total": 1},
    )
    patchedToolUsage = Usage(
        input=5, output=6, cacheRead=7, cacheWrite=8, totalTokens=26,
        cost={"input": 0.5, "output": 0.6, "cacheRead": 0.7, "cacheWrite": 0.8, "total": 2.6},
    )
    observed: dict[str, Any] = {}

    async def execute(_toolCallId: Any, params: Any, *_args: Any, **_kwargs: Any) -> AgentToolResult:
        text = str(read_field(params, "text") or "")
        return AgentToolResult(content=[TextContent(text=text)], details={"text": text}, usage=toolUsage)

    def extension(pi: Any) -> None:
        async def onToolResult(event: Any, _ctx: Any) -> Any:
            observed["usage"] = read_field(event, "usage")
            return {"content": [{"type": "text", "text": "patched result"}], "details": {"patched": True}, "usage": patchedToolUsage}

        pi.on("tool_result", onToolResult)

    async with createHarness(tools=[_echo(execute)], extensionFactories=[extension]) as harness:
        harness.setResponses([
            fauxAssistantMessage([fauxToolCall("echo", {"text": "hello"})], {"stopReason": "toolUse"}),
            lambda context, *_: fauxAssistantMessage(_toolResultText(context)),
        ])

        await harness.session.prompt("hi")

        assert "patched result" in getAssistantTexts(harness)
        toolResult = next(
            (
                message
                for message in harness.session.messages
                if read_field(message, "role") == "toolResult"
                and isinstance(read_field(message, "details"), dict)
                and read_field(message, "details").get("patched") is True
            ),
            None,
        )
        assert Usage.model_validate(observed["usage"]) == toolUsage
        assert toolResult is not None
        assert Usage.model_validate(read_field(toolResult, "usage")) == patchedToolUsage


async def test_allows_extension_context_handlers_to_modify_messages_before_the_llm_call():
    def extension(pi: Any) -> None:
        async def onContext(event: Any, _ctx: Any) -> Any:
            def rewrite(message: Any) -> Any:
                if read_field(message, "role") != "user":
                    return message
                return {
                    **(message.model_dump() if hasattr(message, "model_dump") else dict(message)),
                    "content": [{"type": "text", "text": "rewritten"}],
                    "timestamp": read_field(message, "timestamp"),
                }

            return {"messages": [rewrite(message) for message in read_field(event, "messages")]}

        pi.on("context", onContext)

    async with createHarness(extensionFactories=[extension]) as harness:
        seen = {"text": ""}

        def first(context: Any, *_: Any) -> Any:
            seen["text"] = _userText(context)
            return fauxAssistantMessage("done")

        harness.setResponses([first])

        await harness.session.prompt("original")

        assert seen["text"] == "rewritten"
        stored = next(message for message in harness.session.messages if read_field(message, "role") == "user")
        assert [(read_field(part, "type"), read_field(part, "text")) for part in read_field(stored, "content")] == [
            ("text", "original")
        ]


async def test_allows_extension_input_handlers_to_transform_or_handle_input():
    api: dict[str, Any] = {}

    def extension(pi: Any) -> None:
        api["pi"] = pi

        async def onInput(event: Any, _ctx: Any) -> Any:
            if read_field(event, "text") == "ping":
                return {"action": "handled"}
            return {"action": "transform", "text": f"transformed:{read_field(event, 'text')}"}

        pi.on("input", onInput)

    async with createHarness(extensionFactories=[extension]) as harness:
        seen = {"text": ""}

        def first(context: Any, *_: Any) -> Any:
            seen["text"] = _userText(context)
            return fauxAssistantMessage("done")

        harness.setResponses([first])

        await harness.session.prompt("hello")
        await harness.session.prompt("ping")

        assert seen["text"] == "transformed:hello"
        assert len([message for message in harness.session.messages if read_field(message, "role") == "user"]) == 1
        assert api.get("pi") is not None


async def test_allows_extension_commands_to_inspect_live_system_prompt_options():
    seenOptions: list[Any] = []

    def extension(pi: Any) -> None:
        async def handler(_args: str, ctx: Any) -> None:
            options = ctx.getSystemPromptOptions()
            seenOptions.append(options)
            selected = read_field(options, "selectedTools")
            if selected is not None:
                selected.append("mutated_tool")

        pi.registerCommand("inspect-options", {"description": "Inspect system prompt options", "handler": handler})

    async with createHarness(extensionFactories=[extension]) as harness:
        await harness.session.prompt("/inspect-options")
        await harness.session.prompt("/inspect-options")

        assert len(seenOptions) == 2
        assert seenOptions[0] is seenOptions[1]
        assert read_field(seenOptions[0], "cwd") == harness.tempDir
        assert "read" in read_field(seenOptions[0], "selectedTools")
        assert "mutated_tool" in read_field(seenOptions[1], "selectedTools")


async def test_allows_before_agent_start_handlers_to_inject_custom_messages_and_modify_the_system_prompt():
    def extension(pi: Any) -> None:
        async def onBeforeAgentStart(event: Any, _ctx: Any) -> Any:
            return {
                "message": {"customType": "before-start", "content": "injected", "display": True, "details": {"injected": True}},
                "systemPrompt": f"{read_field(event, 'systemPrompt')}\n\nextra instructions",
            }

        pi.on("before_agent_start", onBeforeAgentStart)

    async with createHarness(extensionFactories=[extension]) as harness:
        seen: dict[str, Any] = {"system": "", "injected": False}

        def first(context: Any, *_: Any) -> Any:
            seen["system"] = getCurrentSystemPrompt(context.messages)
            seen["injected"] = any(
                read_field(message, "role") == "user"
                and not isinstance(read_field(message, "content"), str)
                and any(read_field(part, "type") == "text" and read_field(part, "text") == "injected" for part in read_field(message, "content"))
                for message in context.messages
            )
            return fauxAssistantMessage("done")

        harness.setResponses([first])

        await harness.session.prompt("hello")

        assert "extra instructions" in seen["system"]
        assert seen["injected"] is True
        assert any(
            read_field(message, "role") == "custom" and read_field(message, "customType") == "before-start"
            for message in harness.session.messages
        )


async def test_bind_extensions_emits_session_start_and_reload_emits_session_shutdown_then_session_start():
    lifecycleEvents: list[str] = []

    def extension(pi: Any) -> None:
        async def onStart(event: Any, _ctx: Any) -> None:
            lifecycleEvents.append(f"start:{read_field(event, 'reason')}")

        async def onShutdown(event: Any, _ctx: Any) -> None:
            lifecycleEvents.append(f"shutdown:{read_field(event, 'reason')}")

        pi.on("session_start", onStart)
        pi.on("session_shutdown", onShutdown)

    async with createHarness(extensionFactories=[extension]) as harness:
        await harness.session.bindExtensions({"shutdownHandler": lambda: None})
        await harness.session.reload()

        assert lifecycleEvents == ["start:startup", "shutdown:reload", "start:reload"]
