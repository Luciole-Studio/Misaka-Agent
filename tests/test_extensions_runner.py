"""Translated from pi's ``packages/coding-agent/test/extensions-runner.test.ts`` (v1.1.0): ExtensionRunner conflict
detection, error handling, tool wrapping.

pi's extensions are TypeScript modules; these are the same factories as Python modules (``def default(pi)``), and
``console.warn`` output is what MISAKA prints to stderr.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.core.auth_storage import AuthStorage
from misaka.core.event_bus import createEventBus
from misaka.core.extensions.loader import (
    createExtensionRuntime,
    discoverAndLoadExtensions,
    loadExtensionFromFactory,
    loadExtensions,
)
from misaka.core.extensions.runner import ExtensionRunner, emitProjectTrustEvent
from misaka.core.keybindings import KeybindingsManager
from misaka.core.session_manager import SessionManager
from misaka.core.system_prompt import buildSystemPrompt
from misaka.utils.js import AbortController
from misaka.utils.values import read_field
from tests.model_runtime_test_utils import createInMemoryModelRegistry

defaultKeybindings = KeybindingsManager().getEffectiveConfig()

extensionActions = {
    "sendMessage": lambda *_: None,
    "sendUserMessage": lambda *_: None,
    "appendEntry": lambda *_: None,
    "setSessionName": lambda *_: None,
    "getSessionName": lambda: None,
    "setLabel": lambda *_: None,
    "getActiveTools": list,
    "getAllTools": list,
    "getSettings": dict,
    "setActiveTools": lambda *_: None,
    "refreshTools": lambda: None,
    "getCommands": list,
    "setModel": lambda *_: False,
    "getThinkingLevel": lambda: "off",
    "setThinkingLevel": lambda *_: None,
}

extensionContextActions = {
    "getModel": lambda: None,
    "isIdle": lambda: True,
    "isProjectTrusted": lambda: True,
    "getSignal": lambda: None,
    "abort": lambda: None,
    "hasPendingMessages": lambda: False,
    "shutdown": lambda: None,
    "getContextUsage": lambda: None,
    "compact": lambda *_: None,
    "getSystemPrompt": lambda: "",
    "getScopedModels": list,
}


@pytest.fixture
async def env(tmp_path: Path) -> SimpleNamespace:
    extensionsDir = tmp_path / "extensions"
    extensionsDir.mkdir()
    sessionManager = SessionManager.inMemory()
    modelRegistry = await createInMemoryModelRegistry(AuthStorage.inMemory())

    def write(name: str, factoryBody: str, header: str = "") -> Path:
        """Write ``extensions/<name>.py`` whose ``default(pi)`` runs ``factoryBody``."""
        body = textwrap.indent(textwrap.dedent(factoryBody).strip("\n"), "    ")
        path = extensionsDir / f"{name}.py"
        path.write_text(f"{textwrap.dedent(header)}\n\ndef default(pi):\n{body}\n", encoding="utf-8")
        return path

    async def runner() -> ExtensionRunner:
        result = await discoverAndLoadExtensions([], str(tmp_path), str(tmp_path))
        return ExtensionRunner(result.extensions, result.runtime, str(tmp_path), sessionManager, modelRegistry)

    return SimpleNamespace(
        tempDir=str(tmp_path),
        extensionsDir=extensionsDir,
        sessionManager=sessionManager,
        modelRegistry=modelRegistry,
        write=write,
        runner=runner,
    )


# --- scopedModels -------------------------------------------------------------------------------


async def test_reflects_the_get_scoped_models_context_action_on_ctx_scoped_models(env: SimpleNamespace):
    runner = await env.runner()

    # Before bindCore the default is an empty list (never undefined).
    assert runner.createContext().scopedModels == []

    # After bindCore wires a getScopedModels action, ctx.scopedModels returns it live (same reference, lazy getter).
    scoped = [{"model": {"id": "scoped-test"}, "thinkingLevel": "high"}]
    runner.bindCore(extensionActions, {**extensionContextActions, "getScopedModels": lambda: scoped})
    assert runner.createContext().scopedModels is scoped


# --- project_trust ------------------------------------------------------------------------------


async def test_continues_past_undecided_handlers_and_returns_the_first_yes_no_decision(env: SimpleNamespace):
    undecidedPath = env.write(
        "undecided", 'pi.on("project_trust", lambda _event, _ctx: {"trusted": "undecided", "remember": True})'
    )
    decidedPath = env.write("decided", 'pi.on("project_trust", lambda _event, _ctx: {"trusted": "no", "remember": True})')

    async def select(*_: Any) -> None:
        return None

    async def confirm(*_: Any) -> bool:
        return False

    extensionsResult = await loadExtensions([str(undecidedPath), str(decidedPath)], env.tempDir)
    result = await emitProjectTrustEvent(
        extensionsResult,
        {"type": "project_trust", "cwd": env.tempDir},
        {
            "cwd": env.tempDir,
            "mode": "tui",
            "hasUI": False,
            "ui": {"select": select, "confirm": confirm, "input": select, "notify": lambda *_: None},
        },
    )

    assert {key: read_field(read_field(result, "result"), key) for key in ("trusted", "remember")} == {
        "trusted": "no",
        "remember": True,
    }
    assert read_field(result, "errors") == []


# --- shortcut conflicts -------------------------------------------------------------------------


def shortcut(key: str, description: str) -> str:
    return f"""
        async def handler(*_args):
            pass

        pi.registerShortcut("{key}", {{"description": "{description}", "handler": handler}})
    """


def firstKey(binding: Any) -> str:
    return (binding[0] if binding else "") if isinstance(binding, list) else binding


async def test_warns_when_extension_shortcut_conflicts_with_built_in(env: SimpleNamespace, capsys: pytest.CaptureFixture):
    env.write("conflict", shortcut("ctrl+c", "Conflicts with built-in"))

    shortcuts = (await env.runner()).getShortcuts(defaultKeybindings)

    assert "conflicts with built-in" in capsys.readouterr().err
    assert "ctrl+c" not in shortcuts


async def test_allows_a_shortcut_when_the_reserved_set_no_longer_contains_the_default_key(
    env: SimpleNamespace, capsys: pytest.CaptureFixture
):
    env.write("rebinding", shortcut("ctrl+p", "Uses freed default"))

    keybindings = {**defaultKeybindings, "app.model.cycleForward": "ctrl+n"}
    shortcuts = (await env.runner()).getShortcuts(keybindings)

    assert "ctrl+p" in shortcuts
    assert "conflicts with built-in" not in capsys.readouterr().err


async def test_warns_but_allows_when_extension_uses_non_reserved_built_in_shortcut(
    env: SimpleNamespace, capsys: pytest.CaptureFixture
):
    pasteImageKey = firstKey(defaultKeybindings["app.clipboard.pasteImage"])
    env.write("non-reserved", shortcut(pasteImageKey, "Overrides non-reserved"))

    shortcuts = (await env.runner()).getShortcuts(defaultKeybindings)

    assert "built-in shortcut for app.clipboard.pasteImage" in capsys.readouterr().err
    assert pasteImageKey in shortcuts


async def test_blocks_shortcuts_for_reserved_actions_even_when_rebound(env: SimpleNamespace, capsys: pytest.CaptureFixture):
    env.write("rebound-reserved", shortcut("ctrl+x", "Conflicts with rebound reserved"))

    keybindings = {**defaultKeybindings, "app.interrupt": "ctrl+x"}
    shortcuts = (await env.runner()).getShortcuts(keybindings)

    assert "conflicts with built-in" in capsys.readouterr().err
    assert "ctrl+x" not in shortcuts


async def test_blocks_shortcuts_when_reserved_key_is_also_bound_to_non_reserved_actions(
    env: SimpleNamespace, capsys: pytest.CaptureFixture
):
    env.write("shared-reserved", shortcut("ctrl+p", "Conflicts with shared reserved default"))

    shortcuts = (await env.runner()).getShortcuts(defaultKeybindings)

    assert "conflicts with built-in" in capsys.readouterr().err
    assert "ctrl+p" not in shortcuts


async def test_blocks_shortcuts_when_reserved_action_has_multiple_keys(env: SimpleNamespace, capsys: pytest.CaptureFixture):
    env.write("multi-reserved", shortcut("ctrl+y", "Conflicts with multi-key reserved"))

    keybindings = {**defaultKeybindings, "app.clear": ["ctrl+x", "ctrl+y"]}
    shortcuts = (await env.runner()).getShortcuts(keybindings)

    assert "conflicts with built-in" in capsys.readouterr().err
    assert "ctrl+y" not in shortcuts


async def test_warns_but_allows_when_non_reserved_action_has_multiple_keys(
    env: SimpleNamespace, capsys: pytest.CaptureFixture
):
    env.write("multi-non-reserved", shortcut("ctrl+y", "Overrides multi-key non-reserved"))

    keybindings = {**defaultKeybindings, "app.clipboard.pasteImage": ["ctrl+x", "ctrl+y"]}
    shortcuts = (await env.runner()).getShortcuts(keybindings)

    assert "built-in shortcut for app.clipboard.pasteImage" in capsys.readouterr().err
    assert "ctrl+y" in shortcuts


async def test_warns_when_two_extensions_register_same_shortcut(env: SimpleNamespace, capsys: pytest.CaptureFixture):
    # Use a non-reserved shortcut
    env.write("ext1", shortcut("ctrl+shift+x", "First extension"))
    env.write("ext2", shortcut("ctrl+shift+x", "Second extension"))

    shortcuts = (await env.runner()).getShortcuts(defaultKeybindings)

    assert "shortcut conflict" in capsys.readouterr().err
    # Last one wins
    assert "ctrl+shift+x" in shortcuts


# --- tool collection ----------------------------------------------------------------------------


TOOL_HEADER = """
    from misaka.agent.types import AgentToolResult
    from misaka.ai.types import TextContent
    from misaka.core.extensions.types import ToolDefinition


    async def _ok(*_args, **_kwargs):
        return AgentToolResult(content=[TextContent(text="ok")], details={})
"""


def toolCode(name: str, description: str = "Test tool") -> str:
    return f"""
        pi.registerTool(ToolDefinition(
            name="{name}",
            label="{name}",
            description="{description}",
            parameters={{"type": "object", "properties": {{}}}},
            execute=_ok,
        ))
    """


async def test_collects_tools_from_multiple_extensions(env: SimpleNamespace):
    env.write("tool-a", toolCode("tool_a"), TOOL_HEADER)
    env.write("tool-b", toolCode("tool_b"), TOOL_HEADER)

    tools = (await env.runner()).getAllRegisteredTools()

    assert len(tools) == 2
    assert sorted(tool.definition.name for tool in tools) == ["tool_a", "tool_b"]


# Regression test for #9300.
async def test_rejects_extension_tools_without_a_parameter_schema(env: SimpleNamespace):
    extensionPath = env.write(
        "missing-parameters",
        """
        pi.registerTool({"name": "noop", "label": "No-op", "description": "Do nothing", "execute": _ok})
        """,
        TOOL_HEADER,
    )

    result = await loadExtensions([str(extensionPath)], env.tempDir)

    assert result.extensions == []
    assert result.errors == [
        {
            "path": str(extensionPath),
            "error": (
                f'Failed to load extension: Tool "noop" registered by extension "{extensionPath}" must define an '
                "object parameter schema."
            ),
        }
    ]


async def test_keeps_first_tool_when_two_extensions_register_the_same_name(env: SimpleNamespace):
    env.write("a-first", toolCode("shared", "first"), TOOL_HEADER)
    env.write("b-second", toolCode("shared", "second"), TOOL_HEADER)

    tools = (await env.runner()).getAllRegisteredTools()

    assert len(tools) == 1
    assert tools[0].definition.description == "first"


# --- command collection -------------------------------------------------------------------------


def commandCode(name: str, description: str = "Test command") -> str:
    return f"""
        async def handler(*_args):
            pass

        pi.registerCommand("{name}", {{"description": "{description}", "handler": handler}})
    """


async def test_collects_commands_from_multiple_extensions(env: SimpleNamespace):
    env.write("cmd-a", commandCode("cmd-a"))
    env.write("cmd-b", commandCode("cmd-b"))

    commands = (await env.runner()).getRegisteredCommands()

    assert len(commands) == 2
    assert sorted(command.name for command in commands) == ["cmd-a", "cmd-b"]
    assert sorted(command.invocationName for command in commands) == ["cmd-a", "cmd-b"]


async def test_gets_command_by_invocation_name(env: SimpleNamespace):
    env.write("cmd", commandCode("my-cmd", "My command"))

    runner = await env.runner()

    cmd = runner.getCommand("my-cmd")
    assert cmd is not None
    assert cmd.name == "my-cmd"
    assert cmd.invocationName == "my-cmd"
    assert cmd.description == "My command"

    assert runner.getCommand("not-exists") is None


async def test_suffixes_duplicate_extension_commands_in_insertion_order(env: SimpleNamespace):
    env.write("cmd-a", commandCode("shared-cmd", "First command"))
    env.write("cmd-b", commandCode("shared-cmd", "Second command"))

    runner = await env.runner()
    commands = runner.getRegisteredCommands()
    diagnostics = runner.getCommandDiagnostics()

    assert [command.name for command in commands] == ["shared-cmd", "shared-cmd"]
    assert [command.invocationName for command in commands] == ["shared-cmd:1", "shared-cmd:2"]
    assert [command.description for command in commands] == ["First command", "Second command"]
    assert diagnostics == []
    assert runner.getCommand("shared-cmd:1").description == "First command"
    assert runner.getCommand("shared-cmd:2").description == "Second command"


# --- context creation ---------------------------------------------------------------------------


async def test_exposes_the_current_abort_signal_on_extension_context(env: SimpleNamespace):
    runner = await env.runner()
    controller = AbortController()

    runner.bindCore(extensionActions, {**extensionContextActions, "getSignal": lambda: controller.signal})

    ctx = runner.createContext()
    assert ctx.signal is controller.signal
    assert ctx.signal.aborted is False

    controller.abort()
    assert ctx.signal.aborted is True


async def test_exposes_print_mode_and_has_ui_false_by_default(env: SimpleNamespace):
    runner = await env.runner()
    runner.bindCore(extensionActions, extensionContextActions)

    ctx = runner.createContext()
    assert ctx.mode == "print"
    assert ctx.hasUI is False


async def test_exposes_project_trust_state_on_extension_context(env: SimpleNamespace):
    runner = await env.runner()
    runner.bindCore(extensionActions, {**extensionContextActions, "isProjectTrusted": lambda: False})

    assert runner.createContext().isProjectTrusted() is False


async def test_exposes_rpc_mode_with_has_ui_true_when_an_rpc_ui_context_is_provided(env: SimpleNamespace):
    runner = await env.runner()
    runner.bindCore(extensionActions, extensionContextActions)
    runner.setUIContext(SimpleNamespace(), "rpc")

    ctx = runner.createContext()
    assert ctx.mode == "rpc"
    assert ctx.hasUI is True


async def test_exposes_tui_mode_with_has_ui_true_when_a_tui_ui_context_is_provided(env: SimpleNamespace):
    runner = await env.runner()
    runner.bindCore(extensionActions, extensionContextActions)
    runner.setUIContext(SimpleNamespace(), "tui")

    ctx = runner.createContext()
    assert ctx.mode == "tui"
    assert ctx.hasUI is True


# --- error handling -----------------------------------------------------------------------------


async def test_calls_error_listeners_when_handler_throws(env: SimpleNamespace):
    env.write(
        "throws",
        """
        async def handler(*_args):
            raise RuntimeError("Handler error!")

        pi.on("context", handler)
        """,
    )

    runner = await env.runner()

    errors: list[Any] = []
    runner.onError(errors.append)

    # Emit context event which will trigger the throwing handler
    await runner.emitContext([])

    assert len(errors) == 1
    assert "Handler error!" in read_field(errors[0], "error")
    assert read_field(errors[0], "event") == "context"


def userBashEvent(env: SimpleNamespace, command: str = "pwd") -> dict[str, Any]:
    return {"type": "user_bash", "command": command, "excludeFromContext": False, "cwd": env.tempDir}


# Regression test for #9068.
async def test_fails_closed_when_a_user_bash_handler_throws(env: SimpleNamespace):
    env.write(
        "throws",
        """
        async def handler(*_args):
            raise RuntimeError("Routing failed")

        pi.on("user_bash", handler)
        """,
    )

    runner = await env.runner()
    errors: list[Any] = []
    runner.onError(errors.append)

    with pytest.raises(Exception, match="Routing failed"):
        await runner.emitUserBash(userBashEvent(env))
    assert [(read_field(error, "event"), read_field(error, "error")) for error in errors] == [
        ("user_bash", "Routing failed")
    ]


INVALID_USER_BASH_RESULTS = [
    ("an empty object", "{}"),
    ("null operations", '{"operations": None}'),
    ("operations without exec", '{"operations": {}}'),
    ("a null result", '{"result": None}'),
    ("an incomplete result", '{"result": {"output": "handled"}}'),
    (
        "operations and a result",
        (
            '{"operations": {"exec": _exec}, '
            '"result": {"output": "handled", "exitCode": 0, "cancelled": False, "truncated": False}}'
        ),
    ),
]


# Regression test for #9068.
@pytest.mark.parametrize(("_description", "handlerResult"), INVALID_USER_BASH_RESULTS)
async def test_fails_closed_when_a_user_bash_handler_returns_an_invalid_result(
    env: SimpleNamespace, _description: str, handlerResult: str
):
    env.write(
        "invalid-result",
        "async def _exec(*_args):\n"
        '    return {"exitCode": 0}\n'
        "\n"
        "async def handler(*_args):\n"
        f"    return {handlerResult}\n"
        "\n"
        'pi.on("user_bash", handler)\n',
    )

    runner = await env.runner()
    errors: list[Any] = []
    runner.onError(errors.append)

    with pytest.raises(Exception, match="Invalid user_bash handler result"):
        await runner.emitUserBash(userBashEvent(env))
    assert len(errors) == 1
    assert read_field(errors[0], "event") == "user_bash"
    assert "Invalid user_bash handler result" in read_field(errors[0], "error")


async def test_accepts_valid_user_bash_operations_and_result_overrides(env: SimpleNamespace):
    env.write(
        "valid-results",
        """
        async def _exec(*_args):
            return {"exitCode": 0}

        async def handler(event, _ctx):
            if event["command"] == "operations":
                return {"operations": {"exec": _exec}}
            return {"result": {"output": "handled", "exitCode": 0, "cancelled": False, "truncated": False}}

        pi.on("user_bash", handler)
        """,
    )

    runner = await env.runner()

    operations = await runner.emitUserBash(userBashEvent(env, "operations"))
    assert list(operations) == ["operations"]
    assert callable(read_field(read_field(operations, "operations"), "exec"))
    assert await runner.emitUserBash(userBashEvent(env, "result")) == {
        "result": {"output": "handled", "exitCode": 0, "cancelled": False, "truncated": False}
    }


# --- message and entry renderers ----------------------------------------------------------------


async def test_gets_markdown_transformers_in_extension_load_order(env: SimpleNamespace):
    env.write("markdown-renderer-a", "pi.registerMarkdownTransformer(lambda markdown, *_: markdown)")
    env.write("markdown-renderer-b", "pi.registerMarkdownTransformer(lambda markdown, *_: markdown)")

    assert len((await env.runner()).getMarkdownTransformers()) == 2


async def test_gets_message_renderer_by_type(env: SimpleNamespace):
    env.write("renderer", 'pi.registerMessageRenderer("my-type", lambda message, options, theme: None)')

    runner = await env.runner()

    assert runner.getMessageRenderer("my-type") is not None
    assert runner.getMessageRenderer("not-exists") is None


async def test_gets_entry_renderer_by_type(env: SimpleNamespace):
    env.write("entry-renderer", 'pi.registerEntryRenderer("my-entry", lambda entry, options, theme: None)')

    runner = await env.runner()

    assert runner.getEntryRenderer("my-entry") is not None
    assert runner.getEntryRenderer("not-exists") is None


# --- flags --------------------------------------------------------------------------------------


async def test_collects_flags_from_extensions(env: SimpleNamespace):
    env.write(
        "with-flag",
        """
        async def handler(*_args):
            pass

        pi.registerFlag("my-flag", {"description": "My flag", "handler": handler})
        """,
    )

    assert "my-flag" in (await env.runner()).getFlags()


async def test_keeps_first_flag_when_two_extensions_register_the_same_name(env: SimpleNamespace, tmp_path: Path):
    env.write("a-first", 'pi.registerFlag("shared-flag", {"description": "first", "type": "boolean", "default": True})')
    env.write(
        "b-second", 'pi.registerFlag("shared-flag", {"description": "second", "type": "boolean", "default": False})'
    )

    result = await discoverAndLoadExtensions([], str(tmp_path), str(tmp_path))
    runner = ExtensionRunner(result.extensions, result.runtime, str(tmp_path), env.sessionManager, env.modelRegistry)

    assert runner.getFlags()["shared-flag"].description == "first"
    assert result.runtime.flagValues.get("shared-flag") is True


async def test_rejects_default_values_that_do_not_match_the_flag_type(env: SimpleNamespace, tmp_path: Path):
    env.write("bad-flag-default", 'pi.registerFlag("safe-mode", {"type": "boolean", "default": "false"})')

    result = await discoverAndLoadExtensions([], str(tmp_path), str(tmp_path))

    assert result.extensions == []
    assert 'Invalid default for flag "safe-mode": expected boolean, got string' in result.errors[0]["error"]
    assert "safe-mode" not in result.runtime.flagValues


async def test_can_set_flag_values(env: SimpleNamespace, tmp_path: Path):
    env.write(
        "flag",
        """
        async def handler(*_args):
            pass

        pi.registerFlag("test-flag", {"description": "Test flag", "handler": handler})
        """,
    )

    result = await discoverAndLoadExtensions([], str(tmp_path), str(tmp_path))
    runner = ExtensionRunner(result.extensions, result.runtime, str(tmp_path), env.sessionManager, env.modelRegistry)

    # Setting a flag value should not throw
    runner.setFlagValue("--test-flag", True)

    # The flag values are stored in the shared runtime
    assert result.runtime.flagValues.get("--test-flag") is True


# --- before_agent_start -------------------------------------------------------------------------


async def test_keeps_ctx_get_system_prompt_in_sync_with_chained_system_prompt_updates(
    env: SimpleNamespace, tmp_path: Path
):
    for index, label in enumerate(["first", "second"], start=1):
        env.write(
            f"before-agent-start-{index}",
            "async def handler(_event, ctx):\n"
            f'    return {{"systemPrompt": ctx.getSystemPrompt() + "\\n{label}"}}\n'
            "\n"
            'pi.on("before_agent_start", handler)\n',
        )

    result = await discoverAndLoadExtensions([], str(tmp_path), str(tmp_path))
    assert result.errors == []
    assert len(result.extensions) == 2
    runner = ExtensionRunner(result.extensions, result.runtime, str(tmp_path), env.sessionManager, env.modelRegistry)
    errors: list[str] = []
    runner.onError(lambda error: errors.append(read_field(error, "error")))
    runner.bindCore(extensionActions, extensionContextActions)

    chained = await runner.emitBeforeAgentStart(
        {"messageType": "user", "prompt": "hello"},
        {"cwd": str(tmp_path), "customPrompt": "base"},
    )

    assert errors == []
    assert read_field(chained, "messages") == []
    assert re.search(r"base[\s\S]*\nfirst\nsecond$", buildSystemPrompt(read_field(chained, "systemPromptOptions")))


# Issue #10285: the MCP extension renders calls to tools that are not registered.
async def test_resolves_tool_renderers_in_extension_load_order_each_able_to_defer_to_the_next(
    env: SimpleNamespace, tmp_path: Path
):
    runtime = createExtensionRuntime()
    eventBus = createEventBus()
    renderCall = SimpleNamespace(render=lambda *_: [], invalidate=lambda: None)

    def firstResolver(toolName: str, next: Any) -> Any:
        return {"renderCall": renderCall} if toolName == "a" else next()

    def secondResolver(_toolName: str, next: Any) -> Any:
        resolved = next()
        return resolved if resolved is not None else {"renderShell": "self"}

    first = await loadExtensionFromFactory(
        lambda pi: pi.registerToolRenderer(firstResolver), str(tmp_path), eventBus, runtime
    )
    second = await loadExtensionFromFactory(
        lambda pi: pi.registerToolRenderer(secondResolver), str(tmp_path), eventBus, runtime
    )
    runner = ExtensionRunner([first, second], runtime, str(tmp_path), env.sessionManager, env.modelRegistry)

    assert runner.resolveToolRenderers("a", lambda: None) == {"renderCall": renderCall}
    assert runner.resolveToolRenderers("b", lambda: None) == {"renderShell": "self"}
    assert runner.resolveToolRenderers("b", lambda: {"renderCall": renderCall}) == {"renderCall": renderCall}


# --- boundary chaining --------------------------------------------------------------------------


def emptyBoundaryContext(*_: Any) -> dict[str, Any]:
    return {"contextEntries": [], "contextMessages": [], "llmMessages": [], "pendingMessages": [], "canContinue": False}


async def test_chains_shared_draft_proposals_and_preserves_omitted_result_fields(env: SimpleNamespace, tmp_path: Path):
    runtime = createExtensionRuntime()
    eventBus = createEventBus()
    observations: list[dict[str, Any]] = []

    def observe(event: Any) -> None:
        observations.append({
            "entries": len(read_field(event, "entries")),
            "continuation": read_field(event, "continue"),
            "preview": len(read_field(read_field(event, "context"), "contextEntries")),
        })

    def firstFactory(pi: Any) -> None:
        def handler(event: Any, _ctx: Any) -> Any:
            observe(event)
            read_field(event, "entries").append({"type": "custom", "customType": "first", "data": 1})
            return {"continue": True}

        pi.on("agent_before_settle", handler)

    def secondFactory(pi: Any) -> None:
        def handler(event: Any, _ctx: Any) -> Any:
            observe(event)
            return {"entries": []}

        pi.on("agent_before_settle", handler)

    first = await loadExtensionFromFactory(firstFactory, str(tmp_path), eventBus, runtime, "<inline:first>")
    second = await loadExtensionFromFactory(secondFactory, str(tmp_path), eventBus, runtime, "<inline:second>")
    runner = ExtensionRunner([first, second], runtime, str(tmp_path), env.sessionManager, env.modelRegistry)

    def buildContext(entries: list[Any]) -> dict[str, Any]:
        return {
            "contextEntries": [
                {
                    "sourceEntry": {
                        "type": "custom",
                        "id": f"draft-{index}",
                        "parentId": None,
                        "timestamp": "",
                        "customType": read_field(entry, "type"),
                    },
                    "messages": [],
                }
                for index, entry in enumerate(entries)
            ],
            "contextMessages": [],
            "llmMessages": [],
            "pendingMessages": [],
            "canContinue": False,
        }

    result = await runner.emitBoundary({"type": "agent_before_settle", "outcome": "completed"}, buildContext)

    assert observations == [
        {"entries": 0, "continuation": False, "preview": 0},
        {"entries": 1, "continuation": True, "preview": 1},
    ]
    assert read_field(result, "entries") == []
    assert read_field(result, "continue") is True


async def test_reports_invalid_boundary_previews_and_lets_later_handlers_repair_the_proposal(
    env: SimpleNamespace, tmp_path: Path
):
    runtime = createExtensionRuntime()
    state = {"secondRan": False}

    def invalidFactory(pi: Any) -> None:
        pi.on(
            "agent_before_settle",
            lambda _event, _ctx: {"entries": [{"type": "context_edit", "targetId": "missing", "replacement": None}]},
        )

    def repairFactory(pi: Any) -> None:
        def handler(event: Any, _ctx: Any) -> Any:
            state["secondRan"] = True
            assert len(read_field(event, "entries")) == 1
            return {"entries": []}

        pi.on("agent_before_settle", handler)

    first = await loadExtensionFromFactory(invalidFactory, str(tmp_path), createEventBus(), runtime, "<inline:invalid>")
    second = await loadExtensionFromFactory(repairFactory, str(tmp_path), createEventBus(), runtime, "<inline:repair>")
    runner = ExtensionRunner([first, second], runtime, str(tmp_path), env.sessionManager, env.modelRegistry)
    errors: list[str] = []
    runner.onError(lambda error: errors.append(read_field(error, "error")))

    def buildContext(entries: list[Any]) -> dict[str, Any]:
        if any(read_field(entry, "type") == "context_edit" for entry in entries):
            raise RuntimeError("Entry missing not found")
        return emptyBoundaryContext()

    result = await runner.emitBoundary({"type": "agent_before_settle", "outcome": "completed"}, buildContext)

    assert state["secondRan"] is True
    assert "Invalid boundary entries: Entry missing not found" in errors
    assert read_field(result, "entries") == []
    assert read_field(result, "valid") is True


async def test_keeps_shared_mutations_made_before_a_handler_throws(env: SimpleNamespace, tmp_path: Path):
    runtime = createExtensionRuntime()

    def factory(pi: Any) -> None:
        def handler(event: Any, _ctx: Any) -> Any:
            read_field(event, "entries").append({"type": "custom", "customType": "kept"})
            raise RuntimeError("boundary failed")

        pi.on("agent_before_settle", handler)

    extension = await loadExtensionFromFactory(factory, str(tmp_path), createEventBus(), runtime)
    runner = ExtensionRunner([extension], runtime, str(tmp_path), env.sessionManager, env.modelRegistry)
    errors: list[str] = []
    runner.onError(lambda error: errors.append(read_field(error, "error")))

    result = await runner.emitBoundary({"type": "agent_before_settle", "outcome": "completed"}, emptyBoundaryContext)

    entries = read_field(result, "entries")
    assert [(read_field(entry, "type"), read_field(entry, "customType")) for entry in entries] == [("custom", "kept")]
    assert errors == ["boundary failed"]


# --- tool_result chaining -----------------------------------------------------------------------


def toolResultEvent(toolCallId: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "toolName": "my_tool",
        "toolCallId": toolCallId,
        "input": {},
        "content": [{"type": "text", "text": "base"}],
        "details": {"initial": True},
        "isError": False,
    }


async def test_chains_content_modifications_across_handlers(env: SimpleNamespace):
    for index in (1, 2):
        env.write(
            f"tool-result-{index}",
            "async def handler(event, _ctx):\n"
            f'    return {{"content": [*event["content"], {{"type": "text", "text": "ext{index}"}}]}}\n'
            "\n"
            'pi.on("tool_result", handler)\n',
        )

    chained = await (await env.runner()).emitToolResult(toolResultEvent("call-1"))

    assert chained is not None
    chainedContent = read_field(chained, "content")
    assert chainedContent is not None
    assert (read_field(chainedContent[0], "type"), read_field(chainedContent[0], "text")) == ("text", "base")
    assert len(chainedContent) == 3
    appendedText = [read_field(item, "text") for item in chainedContent[1:] if read_field(item, "type") == "text"]
    assert sorted(appendedText) == ["ext1", "ext2"]


async def test_preserves_previous_modifications_when_later_handlers_return_partial_patches(env: SimpleNamespace):
    env.write(
        "tool-result-partial-1",
        """
        async def handler(*_args):
            return {"content": [{"type": "text", "text": "first"}], "details": {"source": "ext1"}}

        pi.on("tool_result", handler)
        """,
    )
    env.write(
        "tool-result-partial-2",
        """
        async def handler(*_args):
            return {"isError": True}

        pi.on("tool_result", handler)
        """,
    )

    chained = await (await env.runner()).emitToolResult(toolResultEvent("call-2"))

    assert [(read_field(item, "type"), read_field(item, "text")) for item in read_field(chained, "content")] == [
        ("text", "first")
    ]
    assert read_field(chained, "details") == {"source": "ext1"}
    assert read_field(chained, "isError") is True


# --- provider registration ----------------------------------------------------------------------

providerModelConfig = {
    "baseUrl": "https://provider.test/v1",
    "apiKey": "provider-test-key",
    "api": "openai-completions",
    "models": [
        {
            "id": "instant-model",
            "name": "Instant Model",
            "reasoning": False,
            "input": ["text"],
            "cost": {
                "input": 1,
                "output": 2,
                "cacheRead": 0.1,
                "cacheWrite": 1.25,
                "tiers": [{"inputTokensAbove": 272000, "input": 2, "output": 3, "cacheRead": 0.2, "cacheWrite": 2.5}],
            },
            "contextWindow": 128000,
            "maxTokens": 4096,
        }
    ],
}


async def test_bind_core_ignores_invalid_queued_registrations_and_reports_extension_error(
    env: SimpleNamespace, tmp_path: Path
):
    runtime = createExtensionRuntime()

    def broken(*_: Any) -> Any:
        raise RuntimeError("should not run")

    runtime.registerProvider("broken-provider", {"streamSimple": broken}, "/tmp/broken-extension.ts")

    runner = ExtensionRunner([], runtime, str(tmp_path), env.sessionManager, env.modelRegistry)
    errors: list[str] = []
    runner.onError(lambda error: errors.append(f"{read_field(error, 'extensionPath')}: {read_field(error, 'error')}"))

    runner.bindCore(extensionActions, extensionContextActions)
    assert errors == [
        '/tmp/broken-extension.ts: Provider broken-provider: "api" is required when registering streamSimple.'
    ]
    assert read_field(await env.modelRegistry.refresh(), "aborted") is False


def test_pre_bind_unregister_removes_all_queued_registrations_for_a_provider():
    runtime = createExtensionRuntime()

    runtime.registerProvider("queued-provider", providerModelConfig)
    runtime.registerProvider("queued-provider", {
        **providerModelConfig,
        "models": [
            {
                "id": "instant-model-2",
                "name": "Instant Model 2",
                "reasoning": False,
                "input": ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 128000,
                "maxTokens": 4096,
            }
        ],
    })
    assert len(runtime.pendingProviderRegistrations) == 2

    runtime.unregisterProvider("queued-provider")
    assert len(runtime.pendingProviderRegistrations) == 0


async def test_post_bind_register_and_unregister_take_effect_immediately(env: SimpleNamespace, tmp_path: Path):
    runtime = createExtensionRuntime()
    runner = ExtensionRunner([], runtime, str(tmp_path), env.sessionManager, env.modelRegistry)

    runner.bindCore(extensionActions, extensionContextActions)
    assert len(runtime.pendingProviderRegistrations) == 0

    runtime.registerProvider("instant-provider", providerModelConfig)
    assert len(runtime.pendingProviderRegistrations) == 0
    model = env.modelRegistry.find("instant-provider", "instant-model")
    assert [
        {key: read_field(tier, key) for key in ("inputTokensAbove", "input", "output", "cacheRead", "cacheWrite")}
        for tier in read_field(model.cost, "tiers")
    ] == [{"inputTokensAbove": 272000, "input": 2, "output": 3, "cacheRead": 0.2, "cacheWrite": 2.5}]

    runtime.unregisterProvider("instant-provider")
    assert env.modelRegistry.find("instant-provider", "instant-model") is None


# --- command context ----------------------------------------------------------------------------


async def test_passes_fork_options_through_to_the_bound_handler(env: SimpleNamespace, tmp_path: Path):
    runtime = createExtensionRuntime()
    runner = ExtensionRunner([], runtime, str(tmp_path), env.sessionManager, env.modelRegistry)
    forkCalls: list[tuple[Any, ...]] = []

    async def fork(*args: Any) -> Any:
        forkCalls.append(args)
        return {"cancelled": False}

    async def cancelledFalse(*_: Any) -> Any:
        return {"cancelled": False}

    async def nothing(*_: Any) -> None:
        return None

    runner.bindCommandContext({
        "waitForIdle": nothing,
        "newSession": cancelledFalse,
        "fork": fork,
        "navigateTree": cancelledFalse,
        "switchSession": cancelledFalse,
        "reload": nothing,
    })

    commandContext = runner.createCommandContext()
    await commandContext.fork("entry-1")
    assert forkCalls[-1] == ("entry-1", None)

    await commandContext.fork("entry-2", {"position": "at"})
    assert forkCalls[-1] == ("entry-2", {"position": "at"})


# --- event subscriptions (#8967: event handler unsubscription must not disturb other registrations) ---


async def loadSubscriptionExtension(env: SimpleNamespace, tmp_path: Path, factory: Any) -> tuple[Any, ExtensionRunner]:
    runtime = createExtensionRuntime()
    extension = await loadExtensionFromFactory(factory, str(tmp_path), createEventBus(), runtime)
    runner = ExtensionRunner([extension], runtime, str(tmp_path), env.sessionManager, env.modelRegistry)
    return extension, runner


AGENT_END = {"type": "agent_end", "messages": []}


async def test_allows_self_removal_without_skipping_neighboring_handlers(env: SimpleNamespace, tmp_path: Path):
    calls: list[str] = []

    def factory(pi: Any) -> None:
        def first(*_: Any) -> None:
            calls.append("A")
            unsubscribe()

        unsubscribe = pi.on("agent_end", first)
        pi.on("agent_end", lambda *_: calls.append("B"))

    _, runner = await loadSubscriptionExtension(env, tmp_path, factory)

    await runner.emit(AGENT_END)
    assert calls == ["A", "B"]

    await runner.emit(AGENT_END)
    assert calls == ["A", "B", "B"]


async def test_removes_duplicate_registrations_independently_and_cleans_up_the_last_handler(
    env: SimpleNamespace, tmp_path: Path
):
    calls: list[str] = []
    unsubscribers: list[Any] = []

    def factory(pi: Any) -> None:
        def shared(*_: Any) -> None:
            calls.append("shared")

        unsubscribers.append(pi.on("agent_end", shared))
        unsubscribers.append(pi.on("agent_end", lambda *_: calls.append("B")))
        unsubscribers.append(pi.on("agent_end", shared))

    extension, runner = await loadSubscriptionExtension(env, tmp_path, factory)
    stopFirst, stopB, stopSecond = unsubscribers

    stopSecond()
    stopSecond()
    await runner.emit(AGENT_END)
    assert calls == ["shared", "B"]

    stopFirst()
    await runner.emit(AGENT_END)
    assert calls == ["shared", "B", "B"]

    stopB()
    assert "agent_end" not in extension.handlers


async def test_keeps_removed_pending_handlers_in_the_current_dispatch(env: SimpleNamespace, tmp_path: Path):
    calls: list[str] = []
    stops: dict[str, Any] = {}

    def factory(pi: Any) -> None:
        def first(*_: Any) -> None:
            calls.append("A")
            stops["B"]()

        pi.on("agent_end", first)
        stops["B"] = pi.on("agent_end", lambda *_: calls.append("B"))
        pi.on("agent_end", lambda *_: calls.append("C"))

    _, runner = await loadSubscriptionExtension(env, tmp_path, factory)

    await runner.emit(AGENT_END)
    assert calls == ["A", "B", "C"]
    await runner.emit(AGENT_END)
    assert calls == ["A", "B", "C", "A", "C"]


async def test_defers_registrations_made_during_dispatch_until_the_next_dispatch(env: SimpleNamespace, tmp_path: Path):
    calls: list[str] = []

    def factory(pi: Any) -> None:
        def first(*_: Any) -> None:
            calls.append("A")
            pi.on("agent_end", lambda *_: calls.append("C"))

        pi.on("agent_end", first)
        pi.on("agent_end", lambda *_: calls.append("B"))

    _, runner = await loadSubscriptionExtension(env, tmp_path, factory)

    await runner.emit(AGENT_END)
    assert calls == ["A", "B"]
    await runner.emit(AGENT_END)
    assert calls == ["A", "B", "A", "B", "C"]


async def test_uses_a_fresh_handler_list_for_nested_dispatches(env: SimpleNamespace, tmp_path: Path):
    calls: list[str] = []
    refs: dict[str, Any] = {}

    def factory(pi: Any) -> None:
        async def first(*_: Any) -> None:
            calls.append("A")
            refs["stopA"]()
            refs["stopB"]()
            pi.on("agent_end", lambda *_: calls.append("C"))
            await refs["runner"].emit(AGENT_END)

        refs["stopA"] = pi.on("agent_end", first)
        refs["stopB"] = pi.on("agent_end", lambda *_: calls.append("B"))

    _, runner = await loadSubscriptionExtension(env, tmp_path, factory)
    refs["runner"] = runner

    await runner.emit(AGENT_END)
    assert calls == ["A", "C", "B"]


# --- hasHandlers --------------------------------------------------------------------------------


async def test_returns_true_when_handlers_exist_for_event_type(env: SimpleNamespace):
    env.write(
        "handler",
        """
        async def handler(*_args):
            return None

        pi.on("tool_call", handler)
        """,
    )

    runner = await env.runner()

    assert runner.hasHandlers("tool_call") is True
    assert runner.hasHandlers("agent_end") is False


# --- before_provider_headers --------------------------------------------------------------------


async def test_lets_a_handler_mutate_headers_in_place_and_preserves_existing_headers(env: SimpleNamespace):
    env.write(
        "headers",
        """
        def handler(event, _ctx):
            event["headers"]["X-Turn-Index"] = "3"

        pi.on("before_provider_headers", handler)
        """,
    )

    runner = await env.runner()

    assert runner.hasHandlers("before_provider_headers") is True

    headers = await runner.emitBeforeProviderHeaders({"User-Agent": "kimchi/1.0"})
    assert headers["X-Turn-Index"] == "3"
    assert headers["User-Agent"] == "kimchi/1.0"


async def test_isolates_a_throwing_handler_and_still_applies_the_others(env: SimpleNamespace):
    env.write(
        "a-throwing",
        """
        def handler(*_args):
            raise RuntimeError("header handler boom")

        pi.on("before_provider_headers", handler)
        """,
    )
    env.write(
        "b-good",
        """
        def handler(event, _ctx):
            event["headers"]["X-Good"] = "yes"

        pi.on("before_provider_headers", handler)
        """,
    )

    runner = await env.runner()
    errors: list[Any] = []
    runner.onError(errors.append)

    headers = await runner.emitBeforeProviderHeaders({"User-Agent": "x"})

    assert headers["X-Good"] == "yes"
    assert headers["User-Agent"] == "x"
    assert len(errors) == 1
    assert read_field(errors[0], "event") == "before_provider_headers"
    assert "header handler boom" in read_field(errors[0], "error")
