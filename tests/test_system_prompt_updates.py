"""Translated from pi's ``packages/coding-agent/test/system-prompt-updates.test.ts`` (v1.1.0): system prompt updates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.compat import getModel
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.ai.utils.text import getSystemMessageText
from misaka.ai.utils.transcript import getCurrentSystemMessage, getCurrentSystemPrompt
from misaka.core.extensions.types import ToolDefinition
from misaka.core.sdk import createAgentSession
from misaka.core.session_manager import SessionManager
from misaka.core.settings_manager import DEFAULT_TOOL_NAMES, SettingsManager
from misaka.core.system_prompt import (
    buildSystemPromptSections,
    buildSystemPromptState,
    diffSystemPromptSections,
)
from misaka.modes.json_event import to_jsonable
from misaka.utils.values import read_field
from tests.suite_harness import createHarness


def roles(messages: list[Any]) -> list[str]:
    return [read_field(message, "role") for message in messages]


def toolNames(tools: Any) -> list[str]:
    return [read_field(tool, "name") for tool in tools or []]


async def test_declares_the_prompt_and_tools_once_and_reuses_them_across_resume():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second")])
        await harness.session.prompt("one")
        await harness.session.prompt("two")
        systemEntries = [
            entry
            for entry in harness.sessionManager.getEntries()
            if read_field(entry, "type") == "message" and read_field(read_field(entry, "message"), "role") == "system"
        ]
        assert len(systemEntries) == 1
        assert roles(harness.session.messages) == ["system", "user", "assistant", "user", "assistant"]
        head = harness.session.messages[0]
        assert read_field(head, "role") == "system", "expected system message"
        assert read_field(head, "content") == ""
        # MISAKA fork: no `docs` section, and MISAKA's eight default tools (pi: read, bash, edit, write).
        assert list(read_field(head, "sections") or {}) == ["preamble", "tools", "rules", "cwd"]
        assert toolNames(read_field(head, "toolsAdded")) == list(DEFAULT_TOOL_NAMES)
        assert getSystemMessageText(head) == harness.session.systemPrompt


async def test_opens_a_transcript_without_a_system_message_and_declares_the_prompt_on_the_first_request(tmp_path: Path):
    sessionManager = SessionManager.inMemory(str(tmp_path))
    sessionManager.appendMessage({"role": "user", "content": "existing", "timestamp": 1})
    created = await createAgentSession({
        "cwd": str(tmp_path),
        "agentDir": str(tmp_path / "agent"),
        "model": getModel("anthropic", "claude-sonnet-4-5"),
        "settingsManager": SettingsManager.inMemory(),
        "sessionManager": sessionManager,
        "noTools": "all",
    })
    session = created["session"]
    try:
        # Nothing is synthesized or persisted until a request needs it.
        assert roles(session.messages) == ["user"]
        assert roles(sessionManager.buildSessionContext().messages) == ["user"]
        assert getCurrentSystemMessage(session.messages) is None
    finally:
        session.dispose()


def test_diffs_sections_into_a_patch():
    previous = buildSystemPromptSections({"cwd": "/tmp", "sections": {"plan_mode": "Plan only."}})
    current = buildSystemPromptSections({"cwd": "/tmp", "sections": {"plan_mode": "Implementation allowed."}})
    assert diffSystemPromptSections(previous, current) == {
        "plan_mode": "<plan_mode>\nImplementation allowed.\n</plan_mode>"
    }
    assert diffSystemPromptSections(previous, previous) is None
    assert diffSystemPromptSections(previous, buildSystemPromptSections({"cwd": "/tmp"})) == {"plan_mode": None}


def test_keeps_the_preamble_untagged_and_replaces_it_like_any_section():
    previous = buildSystemPromptSections({"customPrompt": "You are A.", "cwd": "/tmp"})
    current = buildSystemPromptSections({"customPrompt": "You are B.", "cwd": "/tmp"})
    assert previous["preamble"] == "You are A."
    assert diffSystemPromptSections(previous, current) == {"preamble": "You are B."}

    assert buildSystemPromptState({"forceSystemPrompt": "Exact prompt.", "cwd": "/tmp"}) == {"content": "Exact prompt."}
    assert buildSystemPromptState({"cwd": "/tmp"}) == {
        "content": "",
        "sections": buildSystemPromptSections({"cwd": "/tmp"}),
    }
    with pytest.raises(Exception, match="Invalid system prompt section name"):
        buildSystemPromptSections({"cwd": "/tmp", "sections": {"preamble": "x"}})


def systemMessagesOf(request: Any) -> list[Any]:
    return [message for message in request.messages if read_field(message, "role") == "system"]


async def test_a_forced_prompt_is_sent_as_the_leading_prompt_for_the_run_and_never_recorded():
    state = {"turn": 0}

    def extension(pi: Any) -> None:
        def beforeAgentStart(event: Any, _ctx: Any) -> Any:
            state["turn"] += 1
            if state["turn"] == 3:
                read_field(read_field(event, "systemPromptOptions"), "sections")["plan_mode"] = "Plan only."
            return {"systemPrompt": "Exact prompt."} if state["turn"] in (2, 3) else None

        pi.on("before_agent_start", beforeAgentStart)

    async with createHarness(extensionFactories=[extension]) as harness:
        requests: list[Any] = []

        def respond(text: str) -> Any:
            def factory(providerContext: Any, *_: Any) -> Any:
                requests.append(providerContext)
                return fauxAssistantMessage(text)

            return factory

        harness.setResponses([respond(text) for text in ["one", "two", "three", "four"]])
        for text in ["one", "two", "three", "four"]:
            await harness.session.prompt(text)
        systemMessages = [systemMessagesOf(request) for request in requests]
        # Forced turns collapse to one leading message; the unforced fourth turn passes the
        # recorded head and both plan_mode patches through.
        assert [len(messages) for messages in systemMessages] == [1, 1, 1, 3]

        forced = systemMessages[1][-1]
        head = systemMessages[0][0]
        assert {key: value for key, value in to_jsonable(forced).items() if value is not None} == {
            key: value
            for key, value in {
                "role": "system",
                "content": "Exact prompt.",
                "toolsAdded": to_jsonable(read_field(head, "toolsAdded")),
                "timestamp": read_field(head, "timestamp"),
            }.items()
            if value is not None
        }
        assert to_jsonable(systemMessages[2][-1]) == to_jsonable(forced)
        assert getCurrentSystemPrompt(requests[2].messages) == "Exact prompt."
        assert roles(requests[2].messages) == ["system", "user", "assistant", "user", "assistant", "user"]

        # The transcript only records the structured sections, never the forced text.
        recorded = [
            read_field(message, "sections")
            for message in harness.session.messages
            if read_field(message, "role") == "system"
        ]
        assert recorded == [
            read_field(head, "sections"),
            {"plan_mode": "<plan_mode>\nPlan only.\n</plan_mode>"},
            {"plan_mode": None},
        ]
        assert getCurrentSystemPrompt(harness.session.messages) == harness.session.systemPrompt


def _registerFirstAndSecond(pi: Any, *, withPrompts: bool) -> None:
    for name in ("first", "second"):

        async def execute(*_args: Any, _name: str = name, **_kwargs: Any) -> AgentToolResult:
            return AgentToolResult(content=[TextContent(text=_name)], details={})

        pi.registerTool(ToolDefinition(
            name=name,
            label=name,
            description=f"{name} description",
            parameters={"type": "object", "properties": {}},
            execute=execute,
            **(
                {"promptSnippet": f"{name} prompt snippet", "promptGuidelines": [f"Use {name} carefully."]}
                if withPrompts
                else {}
            ),
        ))


async def test_set_active_tools_emits_prompt_sections_and_tool_changes_before_the_next_request():
    async with createHarness(
        extensionFactories=[lambda pi: _registerFirstAndSecond(pi, withPrompts=True)], initialActiveToolNames=["first"]
    ) as harness:
        # Faux response callbacks swallow thrown assertions, so capture and assert afterwards.
        requests: list[Any] = []

        def capture(response: Any) -> Any:
            def factory(providerContext: Any, *_: Any) -> Any:
                requests.append(providerContext)
                return response

            return factory

        harness.setResponses([
            capture(fauxAssistantMessage("first")),
            capture(fauxAssistantMessage([fauxToolCall("first", {})], {"stopReason": "toolUse"})),
            capture(fauxAssistantMessage("second")),
        ])
        await harness.session.prompt("first")
        harness.session.setActiveToolsByName(["second"])
        await harness.session.prompt("second")
        assert len(requests) == 3

        initial = requests[0].messages[0]
        assert read_field(initial, "role") == "system", "expected initial system message"
        assert toolNames(read_field(initial, "toolsAdded")) == ["first", "second"]
        assert "first prompt snippet" in read_field(initial, "sections")["tools"]

        update = systemMessagesOf(requests[1])[-1]
        assert read_field(update, "role") == "system"
        assert read_field(update, "content") == ""
        sections = read_field(update, "sections")
        assert set(sections) == {"tools", "rules"}
        assert "second prompt snippet" in sections["tools"]
        assert isinstance(sections["rules"], str)
        assert [read_field(tool, "name") for tool in read_field(update, "toolsRemoved")] == ["first"]
        assert read_field(update, "toolsAdded") is None
        assert isinstance(read_field(update, "timestamp"), int)
        assert "first prompt snippet" not in sections["tools"]
        assert "Use first carefully." not in sections["rules"]

        result = [message for message in requests[2].messages if read_field(message, "role") == "toolResult"][-1]
        assert read_field(result, "toolName") == "first"
        assert read_field(result, "isError") is True

        current = getCurrentSystemMessage(harness.session.messages)
        assert toolNames(read_field(current, "toolsAdded")) == ["second"]
        assert getSystemMessageText(current) == harness.session.systemPrompt


async def test_set_active_tools_in_before_agent_start_controls_the_same_request():
    state = {"turn": 0}

    def extension(pi: Any) -> None:
        _registerFirstAndSecond(pi, withPrompts=False)

        def beforeAgentStart(*_: Any) -> None:
            if state["turn"] == 1:
                pi.setActiveTools(["second"])
            state["turn"] += 1

        pi.on("before_agent_start", beforeAgentStart)

    async with createHarness(extensionFactories=[extension], initialActiveToolNames=["first"]) as harness:
        requests: list[Any] = []

        def capture(text: str) -> Any:
            def factory(providerContext: Any, *_: Any) -> Any:
                requests.append(providerContext)
                return fauxAssistantMessage(text)

            return factory

        harness.setResponses([capture("first"), capture("second")])
        await harness.session.prompt("first")
        await harness.session.prompt("second")
        assert len(requests) == 2
        update = systemMessagesOf(requests[1])[-1]
        assert [read_field(tool, "name") for tool in read_field(update, "toolsRemoved")] == ["first"]
        assert read_field(update, "toolsAdded") is None
        assert harness.session.getActiveToolNames() == ["second"]


async def test_keeps_tool_declarations_stable_across_a_session_json_round_trip():
    async def execute(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        return AgentToolResult(content=[], details={})

    executableTool = AgentTool(
        name="plain",
        label="Plain",
        description="Plain tool",
        parameters={"type": "object", "properties": {}},
        execute=execute,
    )
    async with createHarness(tools=[executableTool], initialActiveToolNames=["plain"]) as harness:
        harness.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second")])
        await harness.session.prompt("one")
        head = harness.session.messages[0]
        assert read_field(head, "role") == "system", "expected system message"
        declarations = read_field(head, "toolsAdded")
        assert declarations, "expected tool declaration"
        declaration = to_jsonable(declarations[0])
        assert declaration.get("constrainedSampling") is None
        assert "execute" not in declaration

        # Simulate a resume: the persisted JSON must replay to the same declarations.
        harness.session.agent.state.messages = json.loads(json.dumps(to_jsonable(harness.session.messages)))
        await harness.session.prompt("two")
        assert len([message for message in harness.session.messages if read_field(message, "role") == "system"]) == 1

