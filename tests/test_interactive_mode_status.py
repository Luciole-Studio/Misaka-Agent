"""Translated from pi's ``coding-agent/test/interactive-mode-status.test.ts`` (v1.3.0).

MISAKA forks in the expectations:
- no ``showManagedToolStatus`` (fd and rg are never downloaded), so its case is not ported;
- extension dialogs run one at a time, so the custom-UI case checks that the second component waits for the overlay,
  and in the #10648 case the overlay opened above the custom UI's is not another custom UI;
- skills are listed by MISAKA's core parts through ``startup_sections`` (pi reads ``resourceLoader.getSkills()``), and
  there is no ``[Skill conflicts]`` section; prompt conflicts stand in for it;
- git package sources are not parsed into ``owner/repo`` labels (MISAKA has no git package sources).
"""

from __future__ import annotations

import asyncio
import os
import re
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from misaka.core.extensions import startup_sections
from misaka.core.source_info import SourceInfo
from misaka.modes.interactive.components.oauth_selector import AuthSelectorProvider
from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import CombinedAutocompleteProvider, Container
from misaka.ui.tui.tui_main_screen import TuiMainScreen
from misaka.utils.js import AbortController
from tests.tui_virtual_terminal import VirtualTerminal


def renderLastLine(container: Container, width: int = 120) -> str:
    last = container.children[-1] if container.children else None
    if not last:
        return ""
    return "\n".join(last.render(width))


def renderAll(container: Container, width: int = 120) -> str:
    return "\n".join(line for child in container.children for line in child.render(width))


class TestFocusableComponent:
    __test__ = False

    def __init__(self, label: str) -> None:
        self.focused = False
        self.inputs: list[str] = []
        self.label = label
        self.text = ""

    def handleInput(self, data: str) -> None:
        self.inputs.append(data)

    def getText(self) -> str:
        return self.text

    def setText(self, text: str) -> None:
        self.text = text

    def render(self, _width: int) -> list[str]:
        return [self.label]

    def invalidate(self) -> None:
        pass


async def flushTui(tui: TuiMainScreen, terminal: VirtualTerminal) -> None:
    tui.requestRender(True)
    await asyncio.sleep(0)
    await terminal.waitForRender()


def normalizeRenderedOutput(container: Container, width: int = 220) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", renderAll(container, width)).replace("\\", "/")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


@pytest.fixture(autouse=True)
def darkTheme():
    initTheme("dark")


# --- InteractiveMode.showStatus ------------------------------------------------------------------------------------


def statusContext():
    context = SimpleNamespace(
        chatContainer=Container(),
        ui=SimpleNamespace(requestRender=Mock()),
        lastStatusSpacer=None,
        lastStatusText=None,
        lastStatusMessage="",
    )
    return context


def test_coalesces_immediately_sequential_status_messages():
    fakeThis = statusContext()

    InteractiveMode.showStatus(fakeThis, "STATUS_ONE")
    assert len(fakeThis.chatContainer.children) == 2
    assert "STATUS_ONE" in renderLastLine(fakeThis.chatContainer)

    InteractiveMode.showStatus(fakeThis, "STATUS_TWO")
    # second status updates the previous line instead of appending
    assert len(fakeThis.chatContainer.children) == 2
    assert "STATUS_TWO" in renderLastLine(fakeThis.chatContainer)
    assert "STATUS_ONE" not in renderLastLine(fakeThis.chatContainer)


def test_appends_a_new_status_line_if_something_else_was_added_in_between():
    fakeThis = statusContext()

    InteractiveMode.showStatus(fakeThis, "STATUS_ONE")
    assert len(fakeThis.chatContainer.children) == 2

    # Something else gets added to the chat in between status updates
    fakeThis.chatContainer.addChild(SimpleNamespace(render=lambda _width: ["OTHER"], invalidate=lambda: None))
    assert len(fakeThis.chatContainer.children) == 3

    InteractiveMode.showStatus(fakeThis, "STATUS_TWO")
    # adds spacer + text
    assert len(fakeThis.chatContainer.children) == 5
    assert "STATUS_TWO" in renderLastLine(fakeThis.chatContainer)


# --- InteractiveMode.setToolsExpanded ------------------------------------------------------------------------------


def test_applies_expansion_state_to_the_active_header_and_chat_entries():
    header = SimpleNamespace(setExpanded=Mock())
    loadedResourcesChild = SimpleNamespace(setExpanded=Mock())
    chatChild = SimpleNamespace(setExpanded=Mock())
    fakeThis = SimpleNamespace(
        toolOutputExpanded=False,
        customHeader=None,
        builtInHeader=header,
        loadedResourcesContainer=SimpleNamespace(children=[loadedResourcesChild]),
        chatContainer=SimpleNamespace(children=[chatChild]),
        ui=SimpleNamespace(requestRender=Mock()),
        showStatus=Mock(),
    )

    InteractiveMode.setToolsExpanded(fakeThis, True)

    assert fakeThis.toolOutputExpanded is True
    header.setExpanded.assert_called_with(True)
    loadedResourcesChild.setExpanded.assert_called_with(True)
    chatChild.setExpanded.assert_called_with(True)
    fakeThis.showStatus.assert_called_with("Tool output: expanded")


# --- InteractiveMode.createExtensionUIContext setTheme -------------------------------------------------------------


def test_persists_theme_changes_to_settings_manager():
    state = {"currentTheme": "dark"}

    def setTheme(theme: str) -> None:
        state["currentTheme"] = theme

    settingsManager = SimpleNamespace(getTheme=Mock(side_effect=lambda: state["currentTheme"]), setTheme=Mock(side_effect=setTheme))

    def setThemeName(_name):
        fakeThis.ui.requestRender()
        return {"success": True}

    fakeThis = SimpleNamespace(
        session=SimpleNamespace(settingsManager=settingsManager),
        settingsManager=settingsManager,
        themeController=SimpleNamespace(
            setThemeInstance=Mock(return_value={"success": True}), setThemeName=Mock(side_effect=setThemeName)
        ),
        ui=SimpleNamespace(requestRender=Mock()),
    )

    uiContext = InteractiveMode.createExtensionUIContext(fakeThis)
    result = uiContext.setTheme("light")

    assert result["success"] is True
    fakeThis.themeController.setThemeName.assert_called_with("light")
    settingsManager.setTheme.assert_called_with("light")
    assert state["currentTheme"] == "light"
    assert fakeThis.ui.requestRender.call_count == 1


def test_does_not_persist_invalid_theme_names():
    settingsManager = SimpleNamespace(getTheme=Mock(return_value="dark"), setTheme=Mock())
    fakeThis = SimpleNamespace(
        session=SimpleNamespace(settingsManager=settingsManager),
        settingsManager=settingsManager,
        themeController=SimpleNamespace(
            setThemeInstance=Mock(return_value={"success": True}),
            setThemeName=Mock(return_value={"success": False, "error": "Theme not found"}),
        ),
        ui=SimpleNamespace(requestRender=Mock()),
    )

    uiContext = InteractiveMode.createExtensionUIContext(fakeThis)
    result = uiContext.setTheme("__missing_theme__")

    assert result["success"] is False
    fakeThis.themeController.setThemeName.assert_called_with("__missing_theme__")
    settingsManager.setTheme.assert_not_called()
    fakeThis.ui.requestRender.assert_not_called()


# --- InteractiveMode.showExtensionCustom ---------------------------------------------------------------------------


class _CustomHost(InteractiveMode):
    """The members `showExtensionCustom` reads; the extension dialog lock is MISAKA's own state."""

    def __init__(self, ui, editorContainer, editor) -> None:
        self.editor = editor
        self.editorContainer = editorContainer
        self.keybindings = {}
        self.ui = ui
        self.disposeActiveSelector = Mock()
        self.programStatus = SimpleNamespace(setBlocked=Mock())
        self._extensionPromptLock = asyncio.Lock()
        self._extensionPromptOwner = None
        self._extensionPromptTask = None
        self._extensionPromptCancel = None
        self._extensionPromptGeneration = 0
        import contextvars

        self._extensionPromptContext = contextvars.ContextVar(f"test_prompt_{id(self)}", default=None)
        self._extensionPromptContextToken = None


async def test_a_second_custom_ui_waits_for_the_open_overlay_and_input_follows_focus():
    # MISAKA fork: pi lets the inline component replace the editor while the overlay stays open; here it waits for its
    # turn, so the overlay keeps the input until it closes.
    terminal = VirtualTerminal(80, 24)
    ui = TuiMainScreen(terminal)
    editorContainer = Container()
    editor = TestFocusableComponent("EDITOR")
    palette = TestFocusableComponent("PALETTE")
    overlay = TestFocusableComponent("OVERLAY")
    replacement = TestFocusableComponent("REPLACEMENT")
    closers: dict[str, object] = {}
    fakeThis = _CustomHost(ui, editorContainer, editor)

    def overlayFactory(_tui, _theme, _keybindings, done):
        closers["overlay"] = done
        return overlay

    def replacementFactory(_tui, _theme, _keybindings, done):
        closers["replacement"] = done
        return replacement

    editorContainer.addChild(editor)
    ui.addChild(editorContainer)
    ui.addChild(palette)
    ui.setFocus(palette)
    ui.start()
    try:
        overlayPromise = asyncio.ensure_future(fakeThis.showExtensionCustom(overlayFactory, {"overlay": True}))
        await flushTui(ui, terminal)
        assert overlay.focused is True

        replacementPromise = asyncio.ensure_future(fakeThis.showExtensionCustom(replacementFactory))
        await flushTui(ui, terminal)
        assert "replacement" not in closers
        terminal.sendInput("x")
        await flushTui(ui, terminal)
        assert overlay.inputs == ["x"]

        closers["overlay"]("closed")
        assert await overlayPromise == "closed"
        await flushTui(ui, terminal)
        assert replacement.focused is True

        closers["replacement"]("done")
        assert await replacementPromise == "done"
        await flushTui(ui, terminal)
        terminal.sendInput("y")
        await flushTui(ui, terminal)

        assert editor.inputs == ["y"]
        assert editor.focused is True
    finally:
        ui.stop()


# Regression test for #10648: closing a lower overlay removed the topmost overlay instead.
@pytest.mark.parametrize("nonCapturing", [False, True], ids=["capturing", "non-capturing"])
async def test_closing_a_lower_overlay_custom_ui_removes_its_own_overlay_not_the_topmost_one(nonCapturing):
    # MISAKA fork: a second custom UI waits for the first, so the overlay above it is opened on the TUI directly
    # (as a selector or another component of the window would).
    terminal = VirtualTerminal(80, 24)
    ui = TuiMainScreen(terminal)
    editorContainer = Container()
    editor = TestFocusableComponent("EDITOR")
    lower = TestFocusableComponent("LOWER")
    upper = TestFocusableComponent("UPPER")
    closers: dict[str, object] = {}
    fakeThis = _CustomHost(ui, editorContainer, editor)

    def lowerFactory(_tui, _theme, _keybindings, done):
        closers["lower"] = done
        return lower

    editorContainer.addChild(editor)
    ui.addChild(editorContainer)
    ui.setFocus(editor)
    ui.start()
    try:
        lowerPromise = asyncio.ensure_future(
            fakeThis.showExtensionCustom(lowerFactory, {"overlay": True, "overlayOptions": {"nonCapturing": nonCapturing}})
        )
        await flushTui(ui, terminal)
        upperHandle = ui.showOverlay(upper)
        await flushTui(ui, terminal)
        assert upper.focused is True

        closers["lower"]("lower closed")
        assert await lowerPromise == "lower closed"
        await flushTui(ui, terminal)

        viewport = "\n".join(terminal.getViewport())
        assert "UPPER" in viewport
        assert "LOWER" not in viewport
        terminal.sendInput("x")
        await flushTui(ui, terminal)
        assert upper.inputs == ["x"]
        assert lower.inputs == []

        upperHandle.hide()
        await flushTui(ui, terminal)
        assert ui.hasOverlay() is False
        assert editor.focused is True
    finally:
        ui.stop()


# --- InteractiveMode compaction status (#10809) --------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        {"type": "compaction_start", "reason": "manual"},
        {"type": "summarization_retry_attempt_start", "source": "compaction", "reason": "threshold"},
    ],
    ids=["compaction_start", "summarization_retry_attempt_start"],
)
@pytest.mark.parametrize("embedded", [True, False], ids=["embedded", "standalone"])
async def test_colors_the_compaction_indicator_like_an_embedding_editors_border(event, embedded):
    from misaka.modes.interactive.components.status_indicator import (
        CompactionStatusIndicator,
    )
    from misaka.modes.interactive.theme.theme import theme

    shown: list[object] = []
    editor = SimpleNamespace(embedWorkingStatus=True, setWorkingStatusIndicator=lambda _indicator: None)
    if embedded:
        editor.borderColor = None  # falls back to the thinking level's border color
    else:
        del editor.embedWorkingStatus
    fakeThis = SimpleNamespace(
        isInitialized=True,
        footer=SimpleNamespace(invalidate=lambda: None),
        programStatus=SimpleNamespace(handleEvent=lambda _event: None),
        settingsManager=SimpleNamespace(getShowTerminalProgress=lambda: False),
        defaultEditor=SimpleNamespace(onEscape=None),
        editor=editor,
        session=SimpleNamespace(thinkingLevel="high", abortCompaction=lambda: None),
        ui=SimpleNamespace(requestRender=lambda *_args: None),
        showStatusIndicator=shown.append,
        clearStatusIndicator=lambda _kind=None: None,
        autoCompactionEscapeHandler=None,
    )

    await InteractiveMode.handleEvent(fakeThis, event)

    [indicator] = shown
    assert isinstance(indicator, CompactionStatusIndicator)
    try:
        line = indicator.render(80)[1]
        if embedded:
            assert theme.getFgAnsi("thinkingHigh") in line
            assert theme.getFgAnsi("muted") not in line
        else:
            assert theme.getFgAnsi("accent") in line
            assert theme.getFgAnsi("muted") in line
    finally:
        indicator.dispose()


# --- InteractiveMode.createExtensionUIContext addAutocompleteProvider ----------------------------------------------


def test_stores_wrapper_factories_and_rebuilds_autocomplete_immediately():
    def wrapper(current):
        return current

    fakeThis = SimpleNamespace(autocompleteProviderWrappers=[], setupAutocompleteProvider=Mock())

    uiContext = InteractiveMode.createExtensionUIContext(fakeThis)
    uiContext.addAutocompleteProvider(wrapper)

    assert fakeThis.autocompleteProviderWrappers == [wrapper]
    assert fakeThis.setupAutocompleteProvider.call_count == 1


# --- InteractiveMode.setupAutocompleteProvider ---------------------------------------------------------------------


def test_stacks_wrapper_factories_over_a_fresh_base_provider():
    defaultEditor = SimpleNamespace(setAutocompleteProvider=Mock())
    customEditor = SimpleNamespace(setAutocompleteProvider=Mock())
    calls: list[str] = []

    def makeWrapper(name: str):
        def wrap(current):
            async def getSuggestions(lines, cursorLine, cursorCol, options):
                calls.append(f"getSuggestions:{name}")
                return await current.getSuggestions(lines, cursorLine, cursorCol, options)

            def applyCompletion(lines, cursorLine, cursorCol, item, prefix):
                calls.append(f"applyCompletion:{name}")
                return current.applyCompletion(lines, cursorLine, cursorCol, item, prefix)

            def shouldTriggerFileCompletion(lines, cursorLine, cursorCol):
                calls.append(f"shouldTrigger:{name}")
                shouldTrigger = getattr(current, "shouldTriggerFileCompletion", None)
                result = shouldTrigger(lines, cursorLine, cursorCol) if shouldTrigger else None
                return True if result is None else result

            return SimpleNamespace(
                getSuggestions=getSuggestions,
                applyCompletion=applyCompletion,
                shouldTriggerFileCompletion=shouldTriggerFileCompletion,
            )

        return wrap

    fakeThis = SimpleNamespace(
        createBaseAutocompleteProvider=lambda: CombinedAutocompleteProvider([], "/tmp/project", None),
        defaultEditor=defaultEditor,
        editor=customEditor,
        autocompleteProviderWrappers=[makeWrapper("wrap1"), makeWrapper("wrap2")],
    )

    InteractiveMode.setupAutocompleteProvider(fakeThis)

    assert defaultEditor.setAutocompleteProvider.call_count == 1
    assert customEditor.setAutocompleteProvider.call_count == 1
    provider = defaultEditor.setAutocompleteProvider.call_args.args[0]
    assert provider is customEditor.setAutocompleteProvider.call_args.args[0]
    assert provider.shouldTriggerFileCompletion(["foo"], 0, 3) is True
    assert calls == ["shouldTrigger:wrap2", "shouldTrigger:wrap1"]


def test_merges_trigger_characters_from_wrapper_factories():
    defaultEditor = SimpleNamespace(setAutocompleteProvider=Mock())
    customEditor = SimpleNamespace(setAutocompleteProvider=Mock())

    def passThrough(triggerCharacters: list[str]):
        def wrap(current):
            return SimpleNamespace(
                triggerCharacters=triggerCharacters,
                getSuggestions=current.getSuggestions,
                applyCompletion=current.applyCompletion,
            )

        return wrap

    fakeThis = SimpleNamespace(
        createBaseAutocompleteProvider=lambda: CombinedAutocompleteProvider([], "/tmp/project", None),
        defaultEditor=defaultEditor,
        editor=customEditor,
        autocompleteProviderWrappers=[passThrough(["$"]), passThrough(["!"])],
    )

    InteractiveMode.setupAutocompleteProvider(fakeThis)

    provider = defaultEditor.setAutocompleteProvider.call_args.args[0]
    assert provider.triggerCharacters == ["$", "!"]


# --- InteractiveMode.createBaseAutocompleteProvider ----------------------------------------------------------------


def autocompleteContext(models: list, getLoginProviderOptions=None):
    fakeThis = SimpleNamespace(
        session=SimpleNamespace(
            scopedModels=[],
            modelRuntime=SimpleNamespace(getAvailableSnapshot=lambda: models),
            promptTemplates=[],
            extensionRunner=SimpleNamespace(getRegisteredCommands=list),
            # MISAKA fork: skills and the other core commands come from the session's parts.
            moments=SimpleNamespace(commands=list),
        ),
        sessionManager=SimpleNamespace(getCwd=lambda: "/tmp"),
        fdPath=None,
    )
    if getLoginProviderOptions is not None:
        fakeThis.getLoginProviderOptions = getLoginProviderOptions
    return fakeThis


async def test_matches_model_command_arguments_across_provider_model_order():
    models = [
        SimpleNamespace(id="gpt-5.2-codex", provider="github-copilot", name="GPT-5.2 Codex"),
        SimpleNamespace(id="gpt-5.5", provider="openai-codex", name="GPT-5.5"),
    ]
    provider = InteractiveMode.createBaseAutocompleteProvider(autocompleteContext(models))
    line = "/model codexgpt"
    suggestions = await provider.getSuggestions([line], 0, len(line), {"signal": AbortController().signal})

    assert [item.value for item in suggestions.items] == ["openai-codex/gpt-5.5", "github-copilot/gpt-5.2-codex"]


async def test_matches_login_command_arguments_by_provider_id_and_name():
    fakeThis = autocompleteContext(
        [],
        lambda: [
            AuthSelectorProvider(id="anthropic", name="Anthropic", authType="oauth", subscription=True),
            AuthSelectorProvider(id="anthropic", name="Anthropic", authType="api_key", subscription=True),
            AuthSelectorProvider(id="openai", name="OpenAI", authType="api_key"),
            AuthSelectorProvider(id="radius", name="Radius", authType="oauth", subscription=False),
        ],
    )
    provider = InteractiveMode.createBaseAutocompleteProvider(fakeThis)
    line = "/login subscription anthrop"
    suggestions = await provider.getSuggestions([line], 0, len(line), {"signal": AbortController().signal})

    assert [(item.value, item.label, item.description) for item in suggestions.items] == [
        ("anthropic", "anthropic", "Anthropic · subscription/API key")
    ]

    # OAuth sign-in without a subscription, such as Radius, is an account.
    radiusLine = "/login radius"
    radiusSuggestions = await provider.getSuggestions(
        [radiusLine], 0, len(radiusLine), {"signal": AbortController().signal}
    )
    assert [(item.value, item.label, item.description) for item in radiusSuggestions.items] == [
        ("radius", "radius", "Radius · account")
    ]


# --- InteractiveMode.showLoadedResources ---------------------------------------------------------------------------

_BOUND = (
    "formatDisplayPath",
    "formatExtensionDisplayPath",
    "formatContextPath",
    "getStartupExpansionState",
    "shouldShowStartupDetails",
    "isPackageSource",
    "getShortPath",
    "getCompactPathLabel",
    "getCompactPackageSourceLabel",
    "getCompactExtensionLabel",
    "getCompactDisplayPathSegments",
    "getCompactNonPackageExtensionLabel",
    "getCompactExtensionLabels",
)


@pytest.fixture
def skillSection():
    """MISAKA fork: the [Skills] section a core part registers: the names when collapsed, the full listing expanded."""
    saved = list(startup_sections.SECTIONS)
    startup_sections.SECTIONS.clear()

    def register(names: list[str]) -> None:
        startup_sections.register("Skills", lambda: f"  {', '.join(names)}", lambda: "resource-list")

    yield register
    startup_sections.SECTIONS[:] = saved


@pytest.fixture(autouse=True)
def noRegisteredSections():
    saved = list(startup_sections.SECTIONS)
    startup_sections.SECTIONS.clear()
    yield
    startup_sections.SECTIONS[:] = saved


def createShowLoadedResourcesThis(
    *,
    quietStartup,
    verbose: bool = False,
    toolOutputExpanded: bool = False,
    cwd: str = "/tmp/project",
    contextFiles: list[dict] | None = None,
    systemPromptSource: dict | None = None,
    appendSystemPromptSources: list[dict] | None = None,
    extensions: list[SimpleNamespace] | None = None,
    promptDiagnostics: list[dict] | None = None,
    useRealScopeGroups: bool = False,
):
    fakeThis = SimpleNamespace(
        options=SimpleNamespace(verbose=verbose),
        toolOutputExpanded=toolOutputExpanded,
        loadedResourcesContainer=Container(),
        chatContainer=Container(),
        settingsManager=SimpleNamespace(getQuietStartup=lambda: quietStartup),
        sessionManager=SimpleNamespace(getCwd=lambda: cwd),
        session=SimpleNamespace(
            promptTemplates=[],
            extensionRunner=SimpleNamespace(getCommandDiagnostics=list, getShortcutDiagnostics=list),
            resourceLoader=SimpleNamespace(
                getAgentsFiles=lambda: {"agentsFiles": contextFiles or []},
                getSystemPromptSource=lambda: systemPromptSource,
                getAppendSystemPromptSources=lambda: appendSystemPromptSources or [],
                getPrompts=lambda: {"prompts": [], "diagnostics": promptDiagnostics or []},
                getExtensions=lambda: SimpleNamespace(extensions=extensions or [], errors=[], warnings=[]),
                getThemes=lambda: {"themes": [], "diagnostics": []},
            ),
        ),
        buildScopeGroups=lambda _items: [],
        formatScopeGroups=lambda _groups, _options: "resource-list",
        formatDiagnostics=lambda _diagnostics, _sourceInfos: "diagnostics",
        getBuiltInCommandConflictDiagnostics=lambda _runner: [],
    )
    for name in _BOUND:
        setattr(fakeThis, name, MethodType(getattr(InteractiveMode, name), fakeThis))
    if useRealScopeGroups:
        for name in ("getScopeGroup", "buildScopeGroups", "formatScopeGroups"):
            setattr(fakeThis, name, MethodType(getattr(InteractiveMode, name), fakeThis))
    return fakeThis


def createSourceInfo(filePath: str, *, source: str, scope: str, origin: str, baseDir: str | None = None) -> SourceInfo:
    return SourceInfo(path=filePath, source=source, scope=scope, origin=origin, baseDir=baseDir)  # type: ignore[arg-type]


def extensionFixture(path: str, sourceInfo: SourceInfo | None = None) -> SimpleNamespace:
    return SimpleNamespace(path=path, sourceInfo=sourceInfo)


def createExtensionFixtures() -> list[SimpleNamespace]:
    return [
        extensionFixture(
            "/tmp/project/.pi/extensions/answer.ts",
            createSourceInfo(
                "/tmp/project/.pi/extensions/answer.ts",
                source="local",
                scope="project",
                origin="top-level",
                baseDir="/tmp/project/.pi/extensions",
            ),
        ),
        extensionFixture(
            "/tmp/project/.pi/extensions/local-index/index.ts",
            createSourceInfo(
                "/tmp/project/.pi/extensions/local-index/index.ts",
                source="local",
                scope="project",
                origin="top-level",
                baseDir="/tmp/project/.pi/extensions",
            ),
        ),
        extensionFixture(
            "/tmp/agent/extensions/user-index/index.ts",
            createSourceInfo(
                "/tmp/agent/extensions/user-index/index.ts",
                source="local",
                scope="user",
                origin="top-level",
                baseDir="/tmp/agent/extensions",
            ),
        ),
        extensionFixture(
            "/tmp/project/.pi/npm/node_modules/pi-markdown-preview/extensions/index.ts",
            createSourceInfo(
                "/tmp/project/.pi/npm/node_modules/pi-markdown-preview/extensions/index.ts",
                source="npm:pi-markdown-preview",
                scope="project",
                origin="package",
                baseDir="/tmp/project/.pi/npm/node_modules/pi-markdown-preview",
            ),
        ),
        extensionFixture(
            "/tmp/project/.pi/npm/node_modules/@scope/pi-scoped/extensions/index.ts",
            createSourceInfo(
                "/tmp/project/.pi/npm/node_modules/@scope/pi-scoped/extensions/index.ts",
                source="npm:@scope/pi-scoped",
                scope="project",
                origin="package",
                baseDir="/tmp/project/.pi/npm/node_modules/@scope/pi-scoped",
            ),
        ),
        extensionFixture(
            "/tmp/project/.pi/git/github.com/HazAT/pi-interactive-subagents/extensions/index.ts",
            createSourceInfo(
                "/tmp/project/.pi/git/github.com/HazAT/pi-interactive-subagents/extensions/index.ts",
                source="git:github.com/HazAT/pi-interactive-subagents",
                scope="project",
                origin="package",
                baseDir="/tmp/project/.pi/git/github.com/HazAT/pi-interactive-subagents",
            ),
        ),
        extensionFixture(
            "/tmp/project/.pi/git/github.com/HazAT/pi-interactive-subagents/extensions/subagents/index.ts",
            createSourceInfo(
                "/tmp/project/.pi/git/github.com/HazAT/pi-interactive-subagents/extensions/subagents/index.ts",
                source="git:github.com/HazAT/pi-interactive-subagents",
                scope="project",
                origin="package",
                baseDir="/tmp/project/.pi/git/github.com/HazAT/pi-interactive-subagents",
            ),
        ),
        extensionFixture(
            "/tmp/temp/cli-extension.ts",
            createSourceInfo(
                "/tmp/temp/cli-extension.ts", source="cli", scope="temporary", origin="top-level", baseDir="/tmp/temp"
            ),
        ),
    ]


def showLoaded(fakeThis, **options) -> None:
    InteractiveMode.showLoadedResources(fakeThis, {"force": False, **options})


def test_shows_a_compact_resource_listing_by_default(skillSection):
    skillSection(["commit"])
    fakeThis = createShowLoadedResourcesThis(quietStartup=False)

    showLoaded(fakeThis)

    output = renderAll(fakeThis.loadedResourcesContainer)
    assert "[Skills]" in output
    assert "commit" in output
    assert "resource-list" not in output


def test_shows_full_resource_listing_when_expanded(skillSection):
    skillSection(["commit"])
    fakeThis = createShowLoadedResourcesThis(quietStartup=False, toolOutputExpanded=True)

    showLoaded(fakeThis)

    output = renderAll(fakeThis.loadedResourcesContainer)
    assert "[Skills]" in output
    assert "resource-list" in output
    assert "commit" not in output


def test_shows_full_resource_listing_on_verbose_startup_even_when_tool_output_is_collapsed(skillSection):
    skillSection(["commit"])
    fakeThis = createShowLoadedResourcesThis(quietStartup=True, verbose=True, toolOutputExpanded=False)

    showLoaded(fakeThis)

    output = renderAll(fakeThis.loadedResourcesContainer)
    assert "[Skills]" in output
    assert "resource-list" in output
    assert "commit" not in output


def test_abbreviates_extensions_in_compact_listing():
    fakeThis = createShowLoadedResourcesThis(
        quietStartup=False,
        extensions=[extensionFixture("/tmp/extensions/answer.ts"), extensionFixture("/tmp/extensions/btw.ts")],
    )

    showLoaded(fakeThis)

    output = renderAll(fakeThis.loadedResourcesContainer)
    assert "[Extensions]" in output
    assert "answer.ts, btw.ts" in output
    assert "extensions/answer.ts" not in output


def test_captures_mixed_extension_layouts_in_compact_output():
    fakeThis = createShowLoadedResourcesThis(
        quietStartup=False, extensions=createExtensionFixtures(), useRealScopeGroups=True
    )

    showLoaded(fakeThis)

    # MISAKA fork: a git source keeps its whole name (pi shows `HazAT/pi-interactive-subagents`).
    assert normalizeRenderedOutput(fakeThis.loadedResourcesContainer) == (
        "[Extensions]\n"
        "  @scope/pi-scoped, answer.ts, cli-extension.ts, git:github.com/HazAT/pi-interactive-subagents, "
        "git:github.com/HazAT/pi-interactive-subagents:subagents, local-index, pi-markdown-preview, user-index"
    )


def localExtension(path: str, baseDir: str, *, source: str = "local", scope: str = "project") -> SimpleNamespace:
    origin = "top-level"
    return extensionFixture(path, createSourceInfo(path, source=source, scope=scope, origin=origin, baseDir=baseDir))


def cliExtension(path: str, baseDir: str) -> SimpleNamespace:
    return localExtension(path, baseDir, source="cli", scope="temporary")


@pytest.mark.parametrize(
    ("extensions", "expected"),
    [
        pytest.param(
            [
                cliExtension("/tmp/alpha/one/index.ts", "/tmp/alpha"),
                cliExtension("/tmp/beta/one/index.ts", "/tmp/beta"),
                cliExtension("/tmp/gamma/one/index.ts", "/tmp/gamma"),
            ],
            "alpha/one, beta/one, gamma/one",
            id="adds more parent folders until local extension labels are unique",
        ),
        pytest.param(
            [localExtension("/tmp/extensions/plan-mode/index.ts", "/tmp/extensions")],
            "plan-mode",
            id="strips index.ts from local extension label, showing parent dir",
        ),
        pytest.param(
            [localExtension("/tmp/extensions/plan-mode/index.js", "/tmp/extensions")],
            "plan-mode",
            id="strips index.js from local extension label, showing parent dir",
        ),
        pytest.param(
            # MISAKA fork: a Python directory extension's entry is `index.py`.
            [localExtension("/tmp/extensions/plan-mode/index.py", "/tmp/extensions")],
            "plan-mode",
            id="strips index.py from local extension label, showing parent dir",
        ),
        pytest.param(
            [
                localExtension("/tmp/extensions/webfetch.ts", "/tmp/extensions"),
                localExtension("/tmp/extensions/plan-mode/index.ts", "/tmp/extensions"),
            ],
            "plan-mode, webfetch.ts",
            id="mixed single-file and subdirectory index.ts extensions strip index.ts",
        ),
        pytest.param(
            [
                localExtension("/tmp/extensions/foo/index.ts", "/tmp/extensions"),
                localExtension("/tmp/extensions/bar/index.ts", "/tmp/extensions"),
            ],
            "bar, foo",
            id="multiple index.ts with unique parent dirs need no disambiguation",
        ),
        pytest.param(
            [
                cliExtension("/tmp/alpha/tools/index.ts", "/tmp/alpha"),
                cliExtension("/tmp/beta/tools/index.ts", "/tmp/beta"),
            ],
            "alpha/tools, beta/tools",
            id="multiple index.ts with same parent dir name disambiguated with grandparent",
        ),
        pytest.param(
            [localExtension("/tmp/extensions/my-ext/main.ts", "/tmp/extensions")],
            "main.ts",
            id="non-index file in subdirectory stays as filename",
        ),
        pytest.param(
            [
                extensionFixture(
                    "/tmp/project/.pi/npm/node_modules/pi-markdown-preview/extensions/index.ts",
                    createSourceInfo(
                        "/tmp/project/.pi/npm/node_modules/pi-markdown-preview/extensions/index.ts",
                        source="npm:pi-markdown-preview",
                        scope="project",
                        origin="package",
                        baseDir="/tmp/project/.pi/npm/node_modules/pi-markdown-preview",
                    ),
                )
            ],
            "pi-markdown-preview",
            id="package extensions still strip index.ts correctly (regression guard)",
        ),
        pytest.param(
            [
                extensionFixture(
                    "/tmp/project/.pi/npm/node_modules/primary-package/index.ts",
                    createSourceInfo(
                        "/tmp/project/.pi/npm/node_modules/primary-package/index.ts",
                        source="npm:primary-package",
                        scope="project",
                        origin="package",
                        baseDir="/tmp/project/.pi/npm/node_modules/primary-package",
                    ),
                ),
                extensionFixture(
                    "/tmp/project/.pi/npm/node_modules/sibling-package/index.ts",
                    createSourceInfo(
                        "/tmp/project/.pi/npm/node_modules/sibling-package/index.ts",
                        source="npm:primary-package",
                        scope="project",
                        origin="package",
                        baseDir="/tmp/project/.pi/npm/node_modules/primary-package",
                    ),
                ),
            ],
            "primary-package, primary-package:../sibling-package",
            id="labels npm sibling extensions relative to the declaring package",
        ),
    ],
)
def test_compact_extension_labels(extensions, expected):
    fakeThis = createShowLoadedResourcesThis(quietStartup=False, extensions=extensions, useRealScopeGroups=True)

    showLoaded(fakeThis)

    assert normalizeRenderedOutput(fakeThis.loadedResourcesContainer) == f"[Extensions]\n  {expected}"


def test_labels_windows_npm_sibling_extensions_relative_to_the_declaring_package():
    primaryPath = "C:\\Users\\me\\.pi\\agent\\npm\\node_modules\\primary-package\\index.ts"
    siblingPath = "C:\\Users\\me\\.pi\\agent\\npm\\node_modules\\sibling-package\\index.ts"
    baseDir = "C:\\Users\\me\\.pi\\agent\\npm\\node_modules\\primary-package"
    extensions = [
        extensionFixture(
            primaryPath,
            createSourceInfo(primaryPath, source="npm:primary-package", scope="user", origin="package", baseDir=baseDir),
        ),
        extensionFixture(
            siblingPath,
            createSourceInfo(siblingPath, source="npm:primary-package", scope="user", origin="package", baseDir=baseDir),
        ),
    ]
    fakeThis = createShowLoadedResourcesThis(quietStartup=False, extensions=extensions, useRealScopeGroups=True)

    showLoaded(fakeThis)

    assert normalizeRenderedOutput(fakeThis.loadedResourcesContainer) == (
        "[Extensions]\n  primary-package, primary-package:../sibling-package"
    )


def test_captures_mixed_extension_layouts_in_expanded_output():
    fakeThis = createShowLoadedResourcesThis(
        quietStartup=False, toolOutputExpanded=True, extensions=createExtensionFixtures(), useRealScopeGroups=True
    )

    showLoaded(fakeThis)

    assert normalizeRenderedOutput(fakeThis.loadedResourcesContainer) == (
        "[Extensions]\n"
        "  project\n"
        "    /tmp/project/.pi/extensions/answer.ts\n"
        "    /tmp/project/.pi/extensions/local-index\n"
        "    git:github.com/HazAT/pi-interactive-subagents\n"
        "      extensions\n"
        "      extensions/subagents\n"
        "    npm:@scope/pi-scoped\n"
        "      extensions\n"
        "    npm:pi-markdown-preview\n"
        "      extensions\n"
        "  user\n"
        "    /tmp/agent/extensions/user-index\n"
        "  path\n"
        "    /tmp/temp/cli-extension.ts"
    )


def test_shows_context_paths_relative_to_cwd_while_preserving_full_external_paths():
    home = os.path.expanduser("~")
    cwd = os.path.join(home, "Development", "pi-mono")
    fakeThis = createShowLoadedResourcesThis(
        quietStartup=False,
        cwd=cwd,
        contextFiles=[{"path": os.path.join(home, ".pi", "agent", "AGENTS.md")}, {"path": os.path.join(cwd, "AGENTS.md")}],
    )

    showLoaded(fakeThis)

    output = renderAll(fakeThis.loadedResourcesContainer).replace("\\", "/")
    assert "[Context]" in output
    assert "~/.pi/agent/AGENTS.md, AGENTS.md" in output
    assert f"{cwd.replace(chr(92), '/')}/AGENTS.md" not in output


def test_shows_system_prompt_context_paths_before_project_context_files():
    cwd = os.path.abspath("/tmp/project")
    fakeThis = createShowLoadedResourcesThis(
        quietStartup=False,
        cwd=cwd,
        systemPromptSource={"path": os.path.join(cwd, ".pi", "SYSTEM.md")},
        appendSystemPromptSources=[{"path": os.path.join(cwd, ".pi", "APPEND_SYSTEM.md")}],
        contextFiles=[{"path": os.path.join(cwd, "AGENTS.md")}],
    )

    showLoaded(fakeThis)

    output = renderAll(fakeThis.loadedResourcesContainer).replace("\\", "/")
    assert "[Context]" in output
    assert ".pi/SYSTEM.md, .pi/APPEND_SYSTEM.md, AGENTS.md" in output


def test_shows_full_context_paths_when_expanded():
    home = os.path.expanduser("~")
    cwd = os.path.join(home, "Development", "pi-mono")
    fakeThis = createShowLoadedResourcesThis(
        quietStartup=False,
        toolOutputExpanded=True,
        cwd=cwd,
        contextFiles=[{"path": os.path.join(home, ".pi", "agent", "AGENTS.md")}, {"path": os.path.join(cwd, "AGENTS.md")}],
    )

    showLoaded(fakeThis)

    output = renderAll(fakeThis.loadedResourcesContainer).replace("\\", "/")
    assert "[Context]" in output
    assert "~/.pi/agent/AGENTS.md" in output
    assert "~/Development/pi-mono/AGENTS.md" in output
    assert "~/.pi/agent/AGENTS.md, AGENTS.md" not in output


def test_does_not_show_verbose_listing_on_quiet_startup_during_reload(skillSection):
    skillSection(["commit"])
    fakeThis = createShowLoadedResourcesThis(quietStartup=True)

    showLoaded(fakeThis, extensions=[{"path": "/tmp/ext/index.ts"}], showDiagnosticsWhenQuiet=True)

    assert len(fakeThis.loadedResourcesContainer.children) == 0


def test_hides_resource_listing_but_keeps_the_startup_header_with_header_only_quiet_startup(skillSection):
    skillSection(["commit"])
    fakeThis = createShowLoadedResourcesThis(quietStartup="header")

    showLoaded(fakeThis)

    assert len(fakeThis.loadedResourcesContainer.children) == 0
    assert InteractiveMode.shouldShowStartupHeader(fakeThis) is True
    assert InteractiveMode.shouldShowStartupDetails(fakeThis) is False


def test_hides_the_startup_header_with_full_quiet_startup_unless_verbose():
    quiet = createShowLoadedResourcesThis(quietStartup=True)
    assert InteractiveMode.shouldShowStartupHeader(quiet) is False
    verbose = createShowLoadedResourcesThis(quietStartup="header", verbose=True)
    assert InteractiveMode.shouldShowStartupDetails(verbose) is True


def test_still_shows_diagnostics_on_quiet_startup_when_requested(skillSection):
    # MISAKA fork: no skill conflicts (skills are not resources of the loader); prompt conflicts show the same path.
    skillSection(["commit"])
    fakeThis = createShowLoadedResourcesThis(
        quietStartup=True, promptDiagnostics=[{"type": "warning", "message": "duplicate prompt name"}]
    )

    showLoaded(fakeThis, showDiagnosticsWhenQuiet=True)

    output = renderAll(fakeThis.loadedResourcesContainer)
    assert "[Prompt conflicts]" in output
    assert "[Skills]" not in output


# --- InteractiveMode.handleDebugCommand ----------------------------------------------------------------------------


def test_debug_dump_shows_rendered_lines_without_terminal_sequences(tmp_path, monkeypatch):
    # MISAKA fork: since 1.2 a rendered line carries the fake cursor's APC markers; the dump lists the visible text.
    from misaka.modes.interactive import interactive_mode
    from misaka.ui.tui import renderFakeCursor

    debugLogPath = tmp_path / "logs" / "debug.log"
    monkeypatch.setattr(interactive_mode, "get_debug_log_path", lambda: str(debugLogPath))
    line = f"\x1b[31mred\x1b[39m {renderFakeCursor('x')}"
    fakeThis = SimpleNamespace(
        ui=SimpleNamespace(terminal=SimpleNamespace(columns=40, rows=10), render=lambda _width: [line], requestRender=Mock()),
        session=SimpleNamespace(messages=[]),
        chatContainer=Container(),
    )

    InteractiveMode.handleDebugCommand(fakeThis)

    dump = debugLogPath.read_text(encoding="utf-8")
    assert '[0] (w=5) "red x"' in dump
    assert "\\u001b" not in dump
