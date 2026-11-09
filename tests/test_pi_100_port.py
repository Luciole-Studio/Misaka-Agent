"""pi 0.99.1 -> 1.0.0 follow-ups for the finished batches (plan docs/plans/pi-0.99-port-2026-09-30.md §6a)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from misaka.core.virtual_models import VIRTUAL_MODEL_API
from misaka.modes.interactive.components.user_message import UserMessageComponent
from misaka.modes.interactive.components.visual_truncate import (
    VisualLinePreview,
    VisualLinePreviewOptions,
    truncateToVisualLines,
)
from misaka.utils.ansi import stripAnsi
from tests.test_tool_orchestration import harness
from tests.test_tool_search import _assistant, _deferred_tools, _scripted, session_with

# --- B3: TUI --------------------------------------------------------------------------------


def test_quiet_startup_reads_true_header_or_false():
    from misaka.core.settings_manager import InMemorySettingsStorage, SettingsManager

    def manager(value):
        storage = InMemorySettingsStorage()
        storage.global_value = json.dumps({"quietStartup": value})
        return SettingsManager.fromStorage(storage)

    assert manager(True).getQuietStartup() is True
    assert manager("header").getQuietStartup() == "header"
    assert manager(False).getQuietStartup() is False
    assert manager("yes").getQuietStartup() is False
    assert manager(None).getQuietStartup() is False

    settings = manager(False)
    settings.setQuietStartup("header")
    assert settings.getQuietStartup() == "header"


def test_visual_truncation_keeps_the_start_or_the_end_and_the_preview_places_its_hint():
    text = "\n".join(f"line {index}" for index in range(5))

    end = truncateToVisualLines(text, 2, 40)
    start = truncateToVisualLines(text, 2, 40, 0, "start")
    assert [stripAnsi(line).strip() for line in end.visualLines] == ["line 3", "line 4"] and end.skippedCount == 3
    assert [stripAnsi(line).strip() for line in start.visualLines] == ["line 0", "line 1"] and start.skippedCount == 3

    def hint(hidden: int) -> str:
        return f"... {hidden} more lines"

    before = VisualLinePreview(VisualLinePreviewOptions(text=text, maxVisualLines=2, keep="end", formatHint=hint))
    after = VisualLinePreview(VisualLinePreviewOptions(text=text, maxVisualLines=2, keep="start", formatHint=hint))
    assert [stripAnsi(line).strip() for line in before.render(40)] == ["... 3 more lines", "line 3", "line 4"]
    assert [stripAnsi(line).strip() for line in after.render(40)] == ["line 0", "line 1", "... 3 more lines"]
    # Cached per width; a new width or an invalidate renders again.
    assert before.render(40) is before.render(40)
    wide = before.render(80)
    assert wide is not before.render(40)
    before.invalidate()
    assert before.render(80) == wide

    short = VisualLinePreview(VisualLinePreviewOptions(text="one", maxVisualLines=2, keep="end", formatHint=hint))
    assert [stripAnsi(line).strip() for line in short.render(40)] == ["one"]


def _startup_mode(quiet_startup, verbose: bool = False):
    from types import SimpleNamespace

    from misaka.modes.interactive.interactive_mode import InteractiveMode

    mode = SimpleNamespace(
        options=SimpleNamespace(verbose=verbose),
        settingsManager=SimpleNamespace(getQuietStartup=lambda: quiet_startup),
    )
    mode.shouldShowStartupHeader = lambda: InteractiveMode.shouldShowStartupHeader(mode)
    mode.shouldShowStartupDetails = lambda: InteractiveMode.shouldShowStartupDetails(mode)
    return mode


def test_header_only_quiet_startup_keeps_the_header_and_hides_the_details():
    header_only = _startup_mode("header")
    assert header_only.shouldShowStartupHeader() is True
    assert header_only.shouldShowStartupDetails() is False

    class NoListing:
        """The listing reads `session.resourceLoader`, which must not be touched."""

        def __getattr__(self, name):
            raise AssertionError(f"the resource listing must not be built (read {name})")

    from misaka.modes.interactive.interactive_mode import InteractiveMode
    from misaka.ui.tui import Container

    header_only.session = NoListing()
    header_only.loadedResourcesContainer = Container()
    InteractiveMode.showLoadedResources(header_only, {"force": False})
    assert header_only.loadedResourcesContainer.children == []


def test_full_quiet_startup_hides_the_header_unless_verbose():
    assert _startup_mode(True).shouldShowStartupHeader() is False
    assert _startup_mode(True).shouldShowStartupDetails() is False
    assert _startup_mode(False).shouldShowStartupHeader() is True
    assert _startup_mode(False).shouldShowStartupDetails() is True
    verbose = _startup_mode("header", verbose=True)
    assert verbose.shouldShowStartupHeader() is True
    assert verbose.shouldShowStartupDetails() is True


def test_user_message_pads_and_colours_itself_without_a_box():
    from misaka.ui.tui import Markdown

    component = UserMessageComponent("hello **world**", outputPad=1)

    [child] = component.children
    assert isinstance(child, Markdown)
    lines = component.render(40)
    # One padding row above and below the text, each carrying the bubble background.
    assert len(lines) == 3
    assert stripAnsi(lines[1]).strip() == "hello world"
    assert all("\x1b[4" in line for line in lines)
    assert lines[0].startswith("\x1b]133;A\x07") and lines[-1].startswith("\x1b]133;B\x07\x1b]133;C\x07")


# --- B6: virtual models -----------------------------------------------------------------------


def _branch_selection(entries, models: dict[str, str]):
    from misaka.core.virtual_models import getBranchSelection

    lookups: list[str] = []

    def get_model(provider: str, model_id: str):
        lookups.append(f"{provider}/{model_id}")
        api = models.get(f"{provider}/{model_id}")
        return None if api is None else SimpleNamespace(provider=provider, id=model_id, api=api)

    return getBranchSelection(entries, get_model), lookups


def _change(provider: str, model_id: str) -> dict:
    return {"type": "model_change", "provider": provider, "modelId": model_id}


def _answer(provider: str, model_id: str, api: str = "faux-api", stop_reason: str = "stop") -> dict:
    return {"type": "message", "message": {"role": "assistant", "provider": provider, "model": model_id, "api": api,
                                           "stopReason": stop_reason}}


_CATALOG = {"faux/small": "faux-api", "faux/large": "faux-api", "router/auto": VIRTUAL_MODEL_API}


def test_branch_selection_looks_up_only_the_last_model_change():
    # pi #10198: the selection must not cost one catalog lookup per assistant message.
    selection, lookups = _branch_selection([_change("faux", "small"), *([_answer("faux", "large")] * 100)], _CATALOG)
    assert selection == {"provider": "faux", "modelId": "large"} and lookups == ["faux/small"]

    selection, lookups = _branch_selection(
        [_change("faux", "small"), _answer("faux", "small"), _change("router", "auto"), *([_answer("faux", "large")] * 100)],
        _CATALOG,
    )
    assert selection == {"provider": "router", "modelId": "auto"} and lookups == ["router/auto"]


def test_branch_selection_uses_the_last_model_change_without_responses_after_it():
    selection, lookups = _branch_selection(
        [_change("router", "auto"), _answer("faux", "small"), _change("faux", "small")], _CATALOG
    )
    assert selection == {"provider": "faux", "modelId": "small"} and lookups == []


def test_branch_selection_uses_the_latest_physical_response_without_a_model_change():
    selection, lookups = _branch_selection(
        [
            _answer("faux", "small"),
            _answer("faux", "large"),
            # Failed routing leaves the virtual model on its message.
            _answer("router", "auto", api=VIRTUAL_MODEL_API, stop_reason="error"),
        ],
        _CATALOG,
    )
    assert selection == {"provider": "faux", "modelId": "large"} and lookups == []


def test_branch_selection_skips_a_failed_moa_answer_like_a_failed_routing():
    # MISAKA fork: an MoA preset's failure message names the preset, as a failed routing names the virtual model.
    selection, lookups = _branch_selection(
        [_answer("faux", "small"), _answer("faux", "large"), _answer("moa", "default", api="moa", stop_reason="error")],
        {**_CATALOG, "moa/default": "moa"},
    )
    assert selection == {"provider": "faux", "modelId": "large"} and lookups == []


# --- B2 + B5: tools and the extension API ----------------------------------------------------


def test_mcp_server_config_resolves_aliases_and_validates_the_1_0_0_fields():
    from misaka.core.mcp_servers import mcpNamespace, validateMcpServerConfig

    # `codemode-deferred` is an alias for `codemode`; the caller's entry is left untouched.
    raw = {"command": "x", "exposure": "codemode-deferred", "toolExposure": {"a": "codemode-deferred"}, "description": "Docs"}
    assert validateMcpServerConfig("scripts", raw) == {
        "command": "x", "exposure": "codemode", "toolExposure": {"a": "codemode"}, "description": "Docs",
    }
    assert raw["exposure"] == "codemode-deferred"
    assert validateMcpServerConfig("wrong", {"command": "x", "exposure": "model-only"}) == (
        'server "wrong": exposure must be one of "codemode", "deferred", "direct", "hidden"'
    )
    assert validateMcpServerConfig("badDescription", {"command": "x", "description": 1}) == (
        'server "badDescription": description must be a string'
    )

    url = "https://a.example/mcp"
    assert validateMcpServerConfig("named", {"url": url, "oauth": {"clientName": "Claude Code"}}) == {
        "url": url, "oauth": {"clientName": "Claude Code"},
    }
    for blank in (" ", " ﻿"):
        assert validateMcpServerConfig("unnamed", {"url": url, "oauth": {"clientName": blank}}) == (
            'server "unnamed": oauth.clientName must be a non-empty string'
        )
    for metadata in ("https://idp.example/m", "http://localhost:8080/m", "http://[::1]/m"):
        assert isinstance(validateMcpServerConfig("metadata", {"url": url, "oauth": {"authServerMetadataUrl": metadata}}), dict)
    assert validateMcpServerConfig("plainMetadata", {"url": url, "oauth": {"authServerMetadataUrl": "http://idp.example/m"}}) == (
        'server "plainMetadata": oauth.authServerMetadataUrl must be an https URL, or http on localhost, 127.0.0.1, or [::1]'
    )

    assert isinstance(validateMcpServerConfig("radius", {"url": "https://radius.example/mcp", "auth": {"provider": "radius"}}), dict)
    assert isinstance(validateMcpServerConfig("local", {"url": "http://localhost:8788/mcp", "auth": {"provider": "radius-dev"}}), dict)
    assert validateMcpServerConfig("plain", {"url": "http://radius.example/mcp", "auth": {"provider": "radius"}}) == (
        'server "plain": auth requires an https URL, or http on localhost, 127.0.0.1, or [::1]'
    )
    assert validateMcpServerConfig("empty", {"url": "https://radius.example/mcp", "auth": {"provider": ""}}) == (
        'server "empty": auth.provider must be a provider name'
    )
    assert mcpNamespace("my-server") == "mcp__my_server" == mcpNamespace("my_server")


async def test_extension_registrations_are_checked_as_upstream_checks_them(tmp_path):
    errors: list[str] = []

    def first(pi) -> None:
        pi.registerMcpServer("taken", {"url": "http://x.invalid"})
        # Registering again replaces the extension's own registration.
        pi.registerMcpServer("taken", {"url": "http://y.invalid"})
        pi.registerMcpServer("my-server", {"url": "http://x.invalid"})

    def second(pi) -> None:
        for name in ("taken", "my_server"):
            try:
                pi.registerMcpServer(name, {"url": "http://z.invalid"})
            except ValueError as error:
                errors.append(str(error))
        for name, options in (("", {"handler": lambda *_args: None}), ("nohandler", {"description": "x"})):
            try:
                pi.registerCommand(name, options)
            except ValueError as error:
                errors.append(str(error))

    async with harness(tmp_path, [first, second]) as session:
        await session.bindExtensions({"onError": lambda _error: None})

    assert errors[0].startswith('MCP server "taken" is already registered by extension')
    # Names that differ only in - and _ share a namespace (pi #10239).
    assert errors[1] == 'MCP server "my_server" conflicts with registered server "my-server"'
    assert "must have a non-empty string name" in errors[2]
    assert errors[3].startswith('Command "/nohandler" registered by extension') and errors[3].endswith("must define handler().")


def _inactive_tool(pi) -> None:
    from misaka.agent.types import AgentToolResult
    from misaka.ai.types import TextContent
    from misaka.core.extensions.types import ToolDefinition

    async def run(*_args):
        return AgentToolResult(content=[TextContent(text="ok")], details={})

    for name in ("inactive_tool", "other_inactive_tool"):
        pi.registerTool(ToolDefinition(
            name=name, label=name, description="Extension tool registered inactive",
            parameters={"type": "object", "properties": {}}, execute=run, defaultActive=False,
        ))


def _write_settings(session, settings: dict) -> None:
    session.settingsManager.storage.global_value = json.dumps(
        {"compaction": {"enabled": False}, "retry": {"enabled": False}, **settings}
    )


async def test_reload_activates_only_tools_newly_added_to_default_tools(tmp_path):
    from misaka.core.settings_manager import DEFAULT_TOOL_NAMES

    async with session_with(tmp_path, [_inactive_tool]) as session:
        assert session.getActiveToolNames() == list(DEFAULT_TOOL_NAMES)
        without_bash = [name for name in DEFAULT_TOOL_NAMES if name != "bash"]
        session.setActiveToolsByName(without_bash)

        _write_settings(session, {"defaultTools": ["+inactive_tool", "+other_inactive_tool"]})
        await session.reload()
        # bash was disabled during the session and is not newly added, so it stays off.
        added = [*without_bash, "inactive_tool", "other_inactive_tool"]
        assert sorted(session.getActiveToolNames()) == sorted(added)

        # Removing tools from the setting does not disable them.
        _write_settings(session, {"defaultTools": ["-read"]})
        await session.reload()
        assert sorted(session.getActiveToolNames()) == sorted(added)


async def test_reload_keeps_explicit_tool_options(tmp_path):
    from misaka.core.settings_manager import DEFAULT_TOOL_NAMES

    async with session_with(tmp_path, [_inactive_tool], tools=["read"]) as allowlisted:
        _write_settings(allowlisted, {"defaultTools": ["+inactive_tool"]})
        await allowlisted.reload()
        assert allowlisted.getActiveToolNames() == ["read"]

    async with session_with(tmp_path, [_inactive_tool], noTools="builtin") as builtinless:
        _write_settings(builtinless, {"defaultTools": ["+inactive_tool"]})
        await builtinless.reload()
        assert builtinless.getActiveToolNames() == []

    async with session_with(tmp_path, [_inactive_tool], excludeTools=["grep"]) as excluded:
        _write_settings(excluded, {"defaultTools": ["+grep", "+inactive_tool"]})
        await excluded.reload()
        without_grep = [name for name in DEFAULT_TOOL_NAMES if name != "grep"]
        assert sorted(excluded.getActiveToolNames()) == sorted([*without_grep, "inactive_tool"])


def _late_deferred_tools(pi) -> None:
    """Registers the deferred tools only once the session has started, like an MCP server that is still connecting."""
    pi.on("session_start", lambda _event, _ctx: _deferred_tools(pi))


async def test_restored_tools_wait_for_their_registration(tmp_path):
    from misaka.ai.types import TextContent, ToolCall
    from misaka.core.session_manager import SessionManager
    from misaka.extensions.tool_search import createToolSearchExtension

    (tmp_path / "workspace").mkdir(exist_ok=True)
    manager = SessionManager.inMemory(str(tmp_path / "workspace"))
    settings = {"defaultTools": ["read", "tool_search"]}
    async with session_with(
        tmp_path, [createToolSearchExtension(), _deferred_tools], sessionManager=manager, settings=settings
    ) as first:
        first.setActiveToolsByName([*first.getActiveToolNames(), "weather_lookup"])
        first.agent.streamFunction = _scripted([_assistant([TextContent(text="ok")])])
        await first.prompt("hello")
        assert "weather_lookup" in first.getActiveToolNames()

    # Resumed before the tool registers: it waits as pending and is declared once it is registered.
    async with session_with(
        tmp_path, [createToolSearchExtension(), _late_deferred_tools], sessionManager=manager, settings=settings
    ) as second:
        # Since pi 1.2 the SDK path restores the transcript's loadout on construction too (`usesDefaultTools`);
        # restoring again leaves it as it is.
        second._restoreToolsFromTranscript()
        assert "weather_lookup" not in second.getActiveToolNames()
        assert "weather_lookup" in second._pendingToolNames
        await second.bindExtensions({})
        assert "weather_lookup" in second.getActiveToolNames()
        # The restored tool runs, and the transcript records no removal in between.
        second.agent.streamFunction = _scripted([
            _assistant([ToolCall(id="c1", name="weather_lookup", arguments={"city": "x"})], "toolUse"),
            _assistant([TextContent(text="done")]),
        ])
        await second.prompt("use it")
        await second.waitForIdle()
        messages = second.agent.state.messages
        [result] = [m for m in messages if getattr(m, "role", None) == "toolResult"]
        assert result.content[0].text == "sunny"
        assert [m for m in messages if getattr(m, "role", None) == "system" and (getattr(m, "toolsRemoved", None) or [])] == []

    # A loadout that only adds a tool, like plan mode restoring its tools, keeps the pending ones.
    async with session_with(
        tmp_path, [createToolSearchExtension(), _late_deferred_tools], sessionManager=manager, settings=settings
    ) as kept:
        kept._restoreToolsFromTranscript()
        kept.setActiveToolsByName([*kept.getActiveToolNames(), "read"])
        assert "weather_lookup" in kept._pendingToolNames
        await kept.bindExtensions({})
        assert "weather_lookup" in kept.getActiveToolNames()

    # A loadout that deactivates a tool replaces the restored one; its pending tools are dropped.
    async with session_with(
        tmp_path, [createToolSearchExtension(), _late_deferred_tools], sessionManager=manager, settings=settings
    ) as replaced:
        replaced._restoreToolsFromTranscript()
        replaced.setActiveToolsByName(["read"])
        assert replaced._pendingToolNames == {}
        await replaced.bindExtensions({})
        assert "weather_lookup" not in replaced.getActiveToolNames()

    # Tools that register only after the next prompt has started are not activated.
    async with session_with(
        tmp_path, [createToolSearchExtension(), _late_deferred_tools], sessionManager=manager, settings=settings
    ) as late:
        late._restoreToolsFromTranscript()
        late.agent.streamFunction = _scripted([_assistant([TextContent(text="ok")])])
        await late.prompt("go")
        assert late._pendingToolNames == {}
        await late.bindExtensions({})
        assert "weather_lookup" not in late.getActiveToolNames()


async def test_reloaded_sessions_declare_restored_tools_again_once_registered(tmp_path):
    from misaka.extensions.tool_search import createToolSearchExtension

    async with session_with(
        tmp_path, [createToolSearchExtension(), _late_deferred_tools], settings={"defaultTools": ["read", "tool_search"]}
    ) as session:
        # `/reload` emits session_start only to bound extensions.
        await session.bindExtensions({"onError": lambda _error: None})
        session.setActiveToolsByName([*session.getActiveToolNames(), "weather_lookup"])
        assert "weather_lookup" in session.getActiveToolNames()

        await session.reload()

        # The reloaded extension registers the tool only on session_start; it waited as pending until then.
        assert "weather_lookup" in session.getActiveToolNames()
        assert session._pendingToolNames == {}
