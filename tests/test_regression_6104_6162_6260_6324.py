"""Translated from pi's ``packages/coding-agent/test/suite/regressions/`` ``6104-find-root-relativization.test.ts``,
``6162-extension-active-tools-next-turn.test.ts``, ``6260-inline-extension-naming.test.ts`` and
``6324-branch-summary-ambient-auth.test.ts`` (v1.1.0)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from misaka.agent.types import AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import AssistantMessage, DoneEvent, TextContent
from misaka.ai.utils.event_stream import createAssistantMessageEventStream
from misaka.ai.utils.transcript import getCurrentSystemPrompt, getCurrentTools
from misaka.core.extensions.types import ToolDefinition
from misaka.core.resource_loader import DefaultResourceLoader
from misaka.core.tools.find import createFindToolDefinition, relativizeFindResultPath
from misaka.utils import node_path
from misaka.utils.values import read_field
from tests.suite_harness import createHarness
from tests.utilities import assistantMsg, userMsg

# --- issue #6104 find relativizes root search paths ---------------------------------------------
#
# path.resolve() preserves a trailing separator when resolving to a root path, such as POSIX / or Windows I:\. The
# find tool used to slice absolute results at searchPath.length + 1, which dropped the first character of the first
# path segment for root searches. Windows directory results that ended in backslash also gained an extra slash.

win32 = node_path.win32
posix = node_path.posix
searchRoot = "I:\\"


def test_windows_drive_root_preserves_the_first_segment_and_emits_one_trailing_slash_for_fd_directory_output():
    assert relativizeFindResultPath("I:\\AI\\Models\\TextGen\\gemma4\\", searchRoot, win32) == "AI/Models/TextGen/gemma4/"


def test_windows_drive_root_handles_fd_output_that_uses_forward_slashes_under_a_drive_root():
    assert relativizeFindResultPath("I:/AI/Models/TextGen/gemma4/", searchRoot, win32) == "AI/Models/TextGen/gemma4/"


def test_windows_drive_root_keeps_deeper_search_paths_unchanged():
    assert relativizeFindResultPath("I:\\AI\\Models\\", "I:\\AI", win32) == "Models/"


def test_windows_drive_root_does_not_relativize_a_sibling_directory_that_shares_a_name_prefix():
    assert relativizeFindResultPath("I:\\AI\\Models2\\file.txt", "I:\\AI\\Models", win32) == "../Models2/file.txt"


def test_windows_drive_root_normalizes_relative_custom_glob_results_without_corrupting_them():
    assert relativizeFindResultPath("AI\\Models\\TextGen\\gemma4\\", searchRoot, win32) == "AI/Models/TextGen/gemma4/"


def test_posix_root_preserves_the_first_segment_for_files_under_root():
    assert relativizeFindResultPath("/home/user/file.txt", "/", posix) == "home/user/file.txt"


def test_posix_root_preserves_the_first_segment_and_one_trailing_slash_for_directories_under_root():
    assert relativizeFindResultPath("/home/user/project/", "/", posix) == "home/user/project/"


def test_posix_root_preserves_backslashes_in_posix_filenames():
    assert relativizeFindResultPath("/home/user/file\\", "/home/user", posix) == "file\\"


def test_falls_back_to_path_relative_when_the_absolute_paths_do_not_share_a_prefix():
    assert (
        relativizeFindResultPath("/tmp/results/file.txt", "/workspace/project", posix) == "../../tmp/results/file.txt"
    )


def test_keeps_a_trailing_slash_on_directories_resolved_through_path_relative():
    assert relativizeFindResultPath("/tmp/results/dir/", "/workspace/project", posix) == "../../tmp/results/dir/"


async def test_relativizes_custom_glob_results_against_a_root_search_path():
    definition = createFindToolDefinition("/", {
        "operations": SimpleNamespace(
            exists=lambda _path: True,
            glob=lambda *_args: ["/home/user/project/", "/home/user/project/file.txt"],
        )
    })
    # The find tool implementation does not touch ctx; pass a minimal stub.
    result = await definition.execute("call-1", {"pattern": "**"}, None, None, None)
    assert read_field(read_field(result, "content")[0], "text") == "home/user/project/\nhome/user/project/file.txt"


# --- extension active tools next-turn refresh ---------------------------------------------------


def getProviderToolNames(context: Any) -> list[str]:
    return sorted(tool.name for tool in getCurrentTools(context.messages))


def registerSwitchTools(pi: Any) -> None:
    """Register `switch_tools`, which swaps the active set to `after_switch` when executed."""

    async def switch(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        pi.setActiveTools(["after_switch"])
        return AgentToolResult(content=[TextContent(text="switched")], details={})

    async def after(*_args: Any, **_kwargs: Any) -> AgentToolResult:
        return AgentToolResult(content=[TextContent(text="after")], details={})

    pi.registerTool(ToolDefinition(
        name="switch_tools",
        label="Switch Tools",
        description="Switch the active extension tool set",
        promptSnippet="Switch to the next extension tool",
        parameters={"type": "object", "properties": {}},
        execute=switch,
    ))
    pi.registerTool(ToolDefinition(
        name="after_switch",
        label="After Switch",
        description="Tool that should be available after switching",
        promptSnippet="Run after the active tool set changes",
        parameters={"type": "object", "properties": {}},
        execute=after,
    ))


# Regression #6162
async def test_applies_pi_set_active_tools_before_the_next_provider_request_in_the_same_run():
    async with createHarness(extensionFactories=[registerSwitchTools]) as harness:
        harness.session.setActiveToolsByName(["switch_tools"])

        providerToolNames: list[list[str]] = []

        def first(context: Any, *_: Any) -> Any:
            providerToolNames.append(getProviderToolNames(context))
            return fauxAssistantMessage(fauxToolCall("switch_tools", {}), {"stopReason": "toolUse"})

        def second(context: Any, *_: Any) -> Any:
            providerToolNames.append(getProviderToolNames(context))
            return fauxAssistantMessage("done")

        harness.setResponses([first, second])

        assert harness.session.getActiveToolNames() == ["switch_tools"]

        await harness.session.prompt("start")

        assert harness.session.getActiveToolNames() == ["after_switch"]
        assert providerToolNames == [["switch_tools"], ["after_switch"]]


async def test_reports_the_refreshed_system_prompt_during_the_run():
    async with createHarness(extensionFactories=[registerSwitchTools]) as harness:
        harness.session.setActiveToolsByName(["switch_tools"])
        providerPrompts: list[str] = []
        sessionPrompts: list[str] = []

        def first(context: Any, *_: Any) -> Any:
            providerPrompts.append(getCurrentSystemPrompt(context.messages))
            sessionPrompts.append(harness.session.systemPrompt)
            return fauxAssistantMessage(fauxToolCall("switch_tools", {}), {"stopReason": "toolUse"})

        def second(context: Any, *_: Any) -> Any:
            providerPrompts.append(getCurrentSystemPrompt(context.messages))
            sessionPrompts.append(harness.session.systemPrompt)
            return fauxAssistantMessage("done")

        harness.setResponses([first, second])

        await harness.session.prompt("start")

        assert len(providerPrompts) == 2
        assert providerPrompts[0] != providerPrompts[1]
        assert sessionPrompts == providerPrompts


async def test_preserves_before_agent_start_system_prompt_overrides_when_tools_change_mid_run():
    def extension(pi: Any) -> None:
        async def beforeAgentStart(event: Any, _ctx: Any) -> Any:
            return {"systemPrompt": f"{read_field(event, 'systemPrompt')}\n\nkeep this run override"}

        pi.on("before_agent_start", beforeAgentStart)
        registerSwitchTools(pi)

    async with createHarness(extensionFactories=[extension]) as harness:
        harness.session.setActiveToolsByName(["switch_tools"])

        providerSystemPrompts: list[str] = []
        providerToolNames: list[list[str]] = []

        def captureSystemPrompt(context: Any) -> None:
            providerSystemPrompts.append(getCurrentSystemPrompt(context.messages))

        def first(context: Any, *_: Any) -> Any:
            captureSystemPrompt(context)
            providerToolNames.append(getProviderToolNames(context))
            return fauxAssistantMessage(fauxToolCall("switch_tools", {}), {"stopReason": "toolUse"})

        def second(context: Any, *_: Any) -> Any:
            captureSystemPrompt(context)
            providerToolNames.append(getProviderToolNames(context))
            return fauxAssistantMessage("done")

        harness.setResponses([first, second])

        await harness.session.prompt("start")

        assert providerToolNames == [["switch_tools"], ["after_switch"]]
        assert len(providerSystemPrompts) == 2
        assert "keep this run override" in providerSystemPrompts[0]
        assert "keep this run override" in providerSystemPrompts[1]


# --- inline extension naming --------------------------------------------------------------------


def noop(_pi: Any) -> None:
    pass


def fixture(tmp_path: Path) -> tuple[str, str]:
    cwd = tmp_path / "project"
    agentDir = tmp_path / "agent"
    cwd.mkdir(parents=True)
    agentDir.mkdir(parents=True)
    return str(cwd), str(agentDir)


async def _loadExtensions(tmp_path: Path, extensionFactories: list[Any]) -> Any:
    cwd, agentDir = fixture(tmp_path)
    loader = DefaultResourceLoader({
        "cwd": cwd,
        "agentDir": agentDir,
        "noSkills": True,
        "noPromptTemplates": True,
        "noThemes": True,
        "extensionFactories": extensionFactories,
    })

    await loader.reload()

    return loader.getExtensions()


def _paths(result: Any) -> list[str]:
    return [read_field(extension, "path") for extension in read_field(result, "extensions")]


async def test_displays_bare_factories_as_inline_n(tmp_path: Path):
    result = await _loadExtensions(tmp_path, [noop, noop])

    assert _paths(result) == ["<inline:1>", "<inline:2>"]


async def test_displays_named_wrappers_as_inline_name(tmp_path: Path):
    result = await _loadExtensions(
        tmp_path, [{"name": "my-provider", "factory": noop}, {"name": "my-commands", "factory": noop}]
    )

    assert _paths(result) == ["<inline:my-provider>", "<inline:my-commands>"]


async def test_preserves_hidden_state_for_named_factories(tmp_path: Path):
    result = await _loadExtensions(tmp_path, [{"name": "built-in", "factory": noop, "hidden": True}])

    assert _paths(result) == ["<inline:built-in>"]
    assert read_field(read_field(result, "extensions")[0], "hidden") is True


async def test_supports_mixed_bare_and_named_factories(tmp_path: Path):
    result = await _loadExtensions(tmp_path, [noop, {"name": "named-ext", "factory": noop}, noop])

    assert _paths(result) == ["<inline:1>", "<inline:named-ext>", "<inline:3>"]


# --- issue #6324 branch summary ambient auth ----------------------------------------------------


async def test_summarizes_tree_branches_when_request_auth_has_no_api_key():
    async with createHarness(withConfiguredAuth=False) as harness:
        streamCalls: list[Any] = []

        def streamFunction(model: Any, _context: Any, options: Any = None) -> Any:
            streamCalls.append(read_field(options, "apiKey"))

            stream = createAssistantMessageEventStream()
            stream.push(DoneEvent(
                reason="stop",
                message=AssistantMessage.model_validate({
                    "role": "assistant",
                    "content": [{"type": "text", "text": "branch summary text"}],
                    "api": model.api,
                    "provider": model.provider,
                    "model": model.id,
                    "usage": {
                        "input": 1,
                        "output": 1,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "totalTokens": 2,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0.25},
                    },
                    "stopReason": "stop",
                    "timestamp": int(time.time() * 1000),
                }),
            ))
            return stream

        harness.session.agent.streamFunction = streamFunction

        targetId = harness.sessionManager.appendMessage(userMsg("first branch"))
        harness.sessionManager.appendMessage(assistantMsg("first reply"))
        harness.sessionManager.appendMessage(userMsg("abandoned branch work"))
        harness.sessionManager.appendMessage(assistantMsg("abandoned reply"))

        result = await harness.session.navigateTree(targetId, {"summarize": True})

        assert read_field(result, "cancelled") is False
        assert streamCalls == [None]
        summaryEntry = read_field(result, "summaryEntry")
        assert read_field(summaryEntry, "type") == "branch_summary"
        assert "branch summary text" in read_field(summaryEntry, "summary")
        assert read_field(read_field(read_field(summaryEntry, "usage"), "cost"), "total") == 0.25
