"""Translated from pi's ``modes/interactive/components/settings-selector.ts`` (v1.3.0).

MISAKA forks:
- (D1) The fullscreen rows (``tui-mode``, ``fullscreen-exit-output``, ``fullscreen-scrollbar``,
  ``fullscreen-copy-on-select``, ``fullscreen-wheel-scroll-lines``) and their config fields and callbacks are not
  ported: MISAKA has only the regular renderer.
- (D11) No system theme: where upstream lists or falls back to it, MISAKA uses the built-in theme matching the
  terminal.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from misaka.agent.types import ThinkingLevel
from misaka.ai.models import getSupportedThinkingLevels
from misaka.ai.types import Model, Transport
from misaka.core.http_dispatcher import (
    HTTP_IDLE_TIMEOUT_CHOICES,
    formatHttpIdleTimeoutMs,
)
from misaka.core.settings_defaults import SETTINGS_DEFAULTS
from misaka.core.settings_manager import (
    CACHE_WARMING_MODES,
    CacheWarmingMode,
    DefaultProjectTrust,
    MermaidRenderingMode,
    QuietStartup,
    SessionShareDestination,
    WarningSettings,
)
from misaka.modes.interactive.theme.theme import (
    TerminalTheme,
    getSettingsListTheme,
    getTerminalTheme,
    parseAutoThemeSetting,
    theme,
)
from misaka.ui.tui import (
    Container,
    SelectItem,
    SelectListLayoutOptions,
    SettingItem,
    SettingsList,
    Spacer,
    Text,
    getCapabilities,
)
from misaka.ui.tui.components.settings_list import SettingsListOptions
from misaka.utils.collation import localeCompare

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyDisplayText
from .settings_submenu import SelectSubmenu, SteppedSubmenu, SteppedSubmenuStep

type SteeringMode = Literal["all", "one-at-a-time"]
type DoubleEscapeAction = Literal["fork", "tree", "none"]
type TreeFilterMode = Literal["default", "no-tools", "user-only", "labeled-only", "all"]

MODEL_PICKER_LAYOUT = SelectListLayoutOptions(minPrimaryColumnWidth=12, maxPrimaryColumnWidth=46)

THINKING_DESCRIPTIONS: dict[ThinkingLevel, str] = {
    "off": "No reasoning",
    "minimal": "Very brief reasoning (~1k tokens)",
    "low": "Light reasoning (~2k tokens)",
    "medium": "Moderate reasoning (~8k tokens)",
    "high": "Deep reasoning (~16k tokens)",
    "xhigh": "Extra-high reasoning (~32k tokens)",
    "max": "Maximum reasoning",
}

DEFAULT_PROJECT_TRUST_LABELS: dict[DefaultProjectTrust, str] = {
    "ask": "Ask",
    "always": "Always trust",
    "never": "Never trust",
}

DEFAULT_PROJECT_TRUST_BY_LABEL: dict[str, DefaultProjectTrust] = {
    label: value for value, label in DEFAULT_PROJECT_TRUST_LABELS.items()
}


@dataclass(slots=True, kw_only=True)
class SettingsConfig:
    autoCompact: bool
    defaultModel: str
    currentModel: Model | None = None
    availableDefaultModels: list[Model]
    showImages: bool
    imageWidthCells: int
    autoResizeImages: bool
    blockImages: bool
    enableSkillCommands: bool
    steeringMode: SteeringMode
    followUpMode: SteeringMode
    transport: Transport
    sessionShareDestination: SessionShareDestination
    httpIdleTimeoutMs: int
    cacheWarmingMode: CacheWarmingMode
    thinkingLevel: ThinkingLevel
    availableThinkingLevels: list[ThinkingLevel]
    modelThinkingLevels: dict[str, ThinkingLevel]
    currentTheme: str
    terminalTheme: TerminalTheme
    availableThemes: list[str]
    hideThinkingBlock: bool
    mermaidRenderingMode: MermaidRenderingMode
    showCacheMissNotices: bool
    collapseChangelog: bool
    enableInstallTelemetry: bool
    doubleEscapeAction: DoubleEscapeAction
    treeFilterMode: TreeFilterMode
    showHardwareCursor: bool
    editorPaddingX: int
    outputPad: Literal[0, 1]
    autocompleteMaxVisible: int
    quietStartup: QuietStartup
    defaultProjectTrust: DefaultProjectTrust
    clearOnShrink: bool
    showTerminalProgress: bool
    warnings: WarningSettings


@dataclass(slots=True, kw_only=True)
class SettingsCallbacks:
    onAutoCompactChange: Callable[[bool], None]
    onShowImagesChange: Callable[[bool], None]
    onImageWidthCellsChange: Callable[[int], None]
    onAutoResizeImagesChange: Callable[[bool], None]
    onBlockImagesChange: Callable[[bool], None]
    onEnableSkillCommandsChange: Callable[[bool], None]
    onSteeringModeChange: Callable[[SteeringMode], None]
    onFollowUpModeChange: Callable[[SteeringMode], None]
    onTransportChange: Callable[[Transport], None]
    onSessionShareDestinationChange: Callable[[SessionShareDestination], None]
    onHttpIdleTimeoutMsChange: Callable[[int], None]
    onCacheWarmingModeChange: Callable[[CacheWarmingMode], None]
    # MISAKA fork: the global default thinking level row (see `thinking` below).
    onThinkingLevelChange: Callable[[ThinkingLevel], None]
    onModelThinkingLevelChange: Callable[[str, str, ThinkingLevel], None]
    onModelThinkingLevelRemove: Callable[[str, str], None]
    onThemeChange: Callable[[str], None]
    onThemePreview: Callable[[str], None] | None = None
    onHideThinkingBlockChange: Callable[[bool], None]
    onMermaidRenderingModeChange: Callable[[MermaidRenderingMode], None]
    onShowCacheMissNoticesChange: Callable[[bool], None]
    onCollapseChangelogChange: Callable[[bool], None]
    onEnableInstallTelemetryChange: Callable[[bool], None]
    onDoubleEscapeActionChange: Callable[[DoubleEscapeAction], None]
    onTreeFilterModeChange: Callable[[TreeFilterMode], None]
    onShowHardwareCursorChange: Callable[[bool], None]
    onEditorPaddingXChange: Callable[[int], None]
    onOutputPadChange: Callable[[Literal[0, 1]], None]
    onAutocompleteMaxVisibleChange: Callable[[int], None]
    onQuietStartupChange: Callable[[QuietStartup], None]
    onDefaultProjectTrustChange: Callable[[DefaultProjectTrust], None]
    onClearOnShrinkChange: Callable[[bool], None]
    onShowTerminalProgressChange: Callable[[bool], None]
    onWarningsChange: Callable[[WarningSettings], None]
    onCancel: Callable[[], None]


class WarningSettingsSubmenu(Container):
    """A submenu component for selecting from a list of options."""

    def __init__(
        self,
        warnings: WarningSettings,
        onChange: Callable[[WarningSettings], None],
        onCancel: Callable[[], None],
    ) -> None:
        super().__init__()

        self.state: WarningSettings = {**warnings}

        anthropicExtraUsage = self.state.get("anthropicExtraUsage")
        items: list[SettingItem] = [
            SettingItem(
                id="anthropic-extra-usage",
                label="Anthropic extra usage",
                description="Warn when Anthropic subscription auth may use paid extra usage",
                currentValue=(
                    "true"
                    if (
                        anthropicExtraUsage
                        if anthropicExtraUsage is not None
                        else SETTINGS_DEFAULTS["warnings"]["anthropicExtraUsage"]
                    )
                    else "false"
                ),
                values=["true", "false"],
            ),
        ]

        def onSettingChange(id: str, newValue: str) -> None:
            match id:
                case "anthropic-extra-usage":
                    self.state = {**self.state, "anthropicExtraUsage": newValue == "true"}
                    onChange({**self.state})

        self.settingsList = SettingsList(
            items,
            min(len(items), 10),
            getSettingsListTheme(),
            onSettingChange,
            onCancel,
        )

        self.addChild(self.settingsList)

    def handleInput(self, data: str) -> None:
        self.settingsList.handleInput(data)


CLEAR_OVERRIDE_VALUE = "__clear__"


def modelSettingKey(model: Model) -> str:
    return f"{model.provider}/{model.id}"


def modelDisplayLabel(model: Model) -> str:
    return f"{model.id} [{model.provider}]"


def modelThinkingOverridesSummary(overrides: dict[str, ThinkingLevel]) -> str:
    count = len(overrides)
    if count == 0:
        return "none"
    return f"{count} configured"


def modelItemLabel(model: Model) -> str:
    return f"{model.id} {theme.fg('muted', f'[{model.provider}]')}"


def themeItems(availableThemes: list[str], currentTheme: str) -> list[SelectItem]:
    # MISAKA fork (D11): no system theme, so no "Theme created from your terminal's colors" description.
    return [SelectItem(value=name, label=f"{'✓ ' if name == currentTheme else '  '}{name}") for name in availableThemes]


AUTOMATIC_THEME_VALUE = "/"


def singleModeThemeItems(availableThemes: list[str], currentTheme: str) -> list[SelectItem]:
    """Automatic mode comes first, then the themes. MISAKA fork (D11): upstream puts the system theme before it."""
    items = themeItems(availableThemes, currentTheme)
    return [
        SelectItem(
            value=AUTOMATIC_THEME_VALUE,
            label="  automatic",
            description="Use separate themes for light and dark terminal appearance",
        ),
        *items,
    ]


def preferredTheme(availableThemes: list[str], preferred: str | None, fallback: str) -> str:
    if preferred and preferred in availableThemes:
        return preferred
    if fallback in availableThemes:
        return fallback
    return availableThemes[0] if availableThemes else fallback


def defaultAutomaticThemes(currentThemeSetting: str, availableThemes: list[str]) -> dict[str, str]:
    autoTheme = parseAutoThemeSetting(currentThemeSetting)
    if autoTheme:
        return {"lightTheme": autoTheme["lightTheme"], "darkTheme": autoTheme["darkTheme"]}

    currentFixedTheme = None if "/" in currentThemeSetting else currentThemeSetting
    # MISAKA fork (D11): the fallback is the built-in theme matching the terminal, not the system theme.
    themeName = preferredTheme(availableThemes, currentFixedTheme, getTerminalTheme())
    return {"lightTheme": themeName, "darkTheme": themeName}


class ThemeSubmenu(Container):
    def __init__(
        self,
        currentThemeSetting: str,
        terminalTheme: TerminalTheme,
        availableThemes: list[str],
        callbacks: SettingsCallbacks,
        onDone: Callable[..., None],
    ) -> None:
        super().__init__()
        self.inputComponent: Any = None
        self.callbacks = callbacks
        self.availableThemes = availableThemes
        self.terminalTheme: TerminalTheme = terminalTheme
        self.onDone = onDone
        self.originalThemeSetting = currentThemeSetting
        autoTheme = parseAutoThemeSetting(currentThemeSetting)
        automaticThemes = defaultAutomaticThemes(currentThemeSetting, availableThemes)
        fixedTheme = None if autoTheme or "/" in currentThemeSetting else currentThemeSetting
        self.mode: Literal["single", "automatic"] = "automatic" if autoTheme else "single"
        self.lightTheme = automaticThemes["lightTheme"]
        self.darkTheme = automaticThemes["darkTheme"]
        self.singleTheme = preferredTheme(
            availableThemes,
            fixedTheme if fixedTheme is not None else (self.getActiveAutomaticTheme() if autoTheme else None),
            # MISAKA fork (D11): the built-in theme matching the terminal, not the system theme.
            getTerminalTheme(),
        )

        if self.mode == "automatic":
            self.showAutomaticMenu()
        else:
            self.showSingleMenu()

    def handleInput(self, data: str) -> None:
        handleInput = getattr(self.inputComponent, "handleInput", None)
        if handleInput is not None:
            handleInput(data)

    def setContent(self, renderComponent: Any, inputComponent: Any = None) -> None:
        self.clear()
        self.addChild(renderComponent)
        self.inputComponent = inputComponent if inputComponent is not None else renderComponent

    def _preview(self, themeSetting: str) -> None:
        """``this.callbacks.onThemePreview?.(themeSetting)``."""
        if self.callbacks.onThemePreview:
            self.callbacks.onThemePreview(themeSetting)

    def showSingleMenu(self) -> None:
        self.mode = "single"

        def onSelect(value: str) -> None:
            if value == AUTOMATIC_THEME_VALUE:
                self.mode = "automatic"
                self._preview(self.getThemeSetting())
                self.showAutomaticMenu()
                return

            self.singleTheme = value
            self.apply(value)

        def onSelectionChange(value: str) -> None:
            self._preview(self.getAutomaticThemeSetting() if value == AUTOMATIC_THEME_VALUE else value)

        menu = SelectSubmenu(
            "Theme",
            "Select a theme, or choose automatic to follow terminal appearance.",
            singleModeThemeItems(self.availableThemes, self.singleTheme),
            self.singleTheme,
            onSelect,
            lambda: self.cancel(),
            onSelectionChange,
        )
        self.setContent(menu)

    def showAutomaticMenu(self) -> None:
        self.mode = "automatic"
        content = Container()
        content.addChild(Text(theme.bold(theme.fg("accent", "Automatic Theme")), 0, 0))
        content.addChild(Spacer(1))
        content.addChild(Text(theme.fg("muted", "Choose themes for terminal light and dark appearance."), 0, 0))
        content.addChild(Text(theme.fg("muted", "Light/dark detection requires terminal support."), 0, 0))
        content.addChild(Spacer(1))

        def lightSubmenu(currentValue: str, done: Callable[..., None]) -> SelectSubmenu:
            def onSelect(value: str) -> None:
                self.lightTheme = value
                self._preview(self.getThemeSetting())
                done(value)

            return self.createThemeSelect(
                "Light Theme",
                "Select the theme to use for light terminal appearance",
                currentValue,
                done,
                onSelect,
            )

        def darkSubmenu(currentValue: str, done: Callable[..., None]) -> SelectSubmenu:
            def onSelect(value: str) -> None:
                self.darkTheme = value
                self._preview(self.getThemeSetting())
                done(value)

            return self.createThemeSelect(
                "Dark Theme",
                "Select the theme to use for dark terminal appearance",
                currentValue,
                done,
                onSelect,
            )

        items: list[SettingItem] = [
            SettingItem(
                id="light-theme",
                label="Light theme",
                description="Theme to use in automatic mode when the terminal is light",
                currentValue=self.lightTheme,
                submenu=lightSubmenu,
            ),
            SettingItem(
                id="dark-theme",
                label="Dark theme",
                description="Theme to use in automatic mode when the terminal is dark",
                currentValue=self.darkTheme,
                submenu=darkSubmenu,
            ),
            SettingItem(
                id="apply",
                label="Apply",
                description="Save and go back",
                currentValue="save and go back",
                values=["save and go back"],
            ),
            SettingItem(
                id="single-mode",
                label="Change mode",
                description="Switch to one theme for light and dark",
                currentValue="switch to single theme",
                values=["switch to single theme"],
            ),
        ]

        def onChange(id: str, _newValue: str) -> None:
            match id:
                case "single-mode":
                    self.mode = "single"
                    self.singleTheme = self.getActiveAutomaticTheme()
                    self._preview(self.singleTheme)
                    self.showSingleMenu()
                case "apply":
                    self.apply(self.getAutomaticThemeSetting())

        settingsList = SettingsList(
            items,
            min(len(items), 10),
            getSettingsListTheme(),
            onChange,
            lambda: self.cancel(),
        )
        content.addChild(settingsList)
        self.setContent(content, settingsList)

    def createThemeSelect(
        self,
        title: str,
        description: str,
        currentValue: str,
        done: Callable[..., None],
        onSelect: Callable[[str], None],
    ) -> SelectSubmenu:
        def onCancel() -> None:
            self._preview(self.getThemeSetting())
            done()

        return SelectSubmenu(
            title,
            description,
            themeItems(self.availableThemes, currentValue),
            currentValue,
            onSelect,
            onCancel,
            lambda value: self._preview(value),
        )

    def getThemeSetting(self) -> str:
        return self.getAutomaticThemeSetting() if self.mode == "automatic" else self.singleTheme

    def getActiveAutomaticTheme(self) -> str:
        return self.lightTheme if self.terminalTheme == "light" else self.darkTheme

    def getAutomaticThemeSetting(self) -> str:
        return f"{self.lightTheme}/{self.darkTheme}"

    def apply(self, themeSetting: str) -> None:
        self.onDone(themeSetting)

    def cancel(self) -> None:
        self._preview(self.originalThemeSetting)
        self.onDone()


class SettingsSelectorComponent(Container):
    """Main settings selector component."""

    def __init__(self, config: SettingsConfig, callbacks: SettingsCallbacks) -> None:
        super().__init__()

        supportsImages = getCapabilities().images
        followUpKey = keyDisplayText("app.message.followUp")
        cycleThinkingKey = keyDisplayText("app.thinking.cycle")
        currentWarnings: WarningSettings = {**config.warnings}
        currentModelThinkingLevels: dict[str, ThinkingLevel] = {**config.modelThinkingLevels}
        defaultModelByValue = {modelSettingKey(model): model for model in config.availableDefaultModels}
        currentDefaultModelKey = config.defaultModel if config.defaultModel in defaultModelByValue else None
        currentModelKey = modelSettingKey(config.currentModel) if config.currentModel else None

        def warningsSubmenu(_currentValue: str, done: Callable[..., None]) -> WarningSettingsSubmenu:
            def onChange(warnings: WarningSettings) -> None:
                nonlocal currentWarnings
                currentWarnings = warnings
                callbacks.onWarningsChange(warnings)

            return WarningSettingsSubmenu(currentWarnings, onChange, lambda: done())

        def thinkingSubmenu(currentValue: str, done: Callable[..., None]) -> SelectSubmenu:
            def onSelect(value: str) -> None:
                callbacks.onThinkingLevelChange(value)  # type: ignore[arg-type]
                done(value)

            return SelectSubmenu(
                "Thinking Level",
                "Select reasoning depth for thinking-capable models",
                [
                    SelectItem(value=level, label=level, description=THINKING_DESCRIPTIONS[level])
                    for level in config.availableThinkingLevels
                ],
                currentValue,
                onSelect,
                lambda: done(),
            )

        def modelThinkingSubmenu(_currentValue: str, done: Callable[..., None]) -> SteppedSubmenu:
            def modelOptions(_ctx: dict[str, str]) -> list[SelectItem]:
                def byPreference(a: Model, b: Model) -> int:
                    aKey = modelSettingKey(a)
                    bKey = modelSettingKey(b)
                    if aKey == currentModelKey:
                        return -1
                    if bKey == currentModelKey:
                        return 1
                    if aKey == currentDefaultModelKey:
                        return -1
                    if bKey == currentDefaultModelKey:
                        return 1
                    return localeCompare(a.provider, b.provider)

                sorted_ = sorted(config.availableDefaultModels, key=functools.cmp_to_key(byPreference))
                items: list[SelectItem] = []
                for model in sorted_:
                    key = modelSettingKey(model)
                    override = currentModelThinkingLevels.get(key)
                    items.append(SelectItem(value=key, label=modelItemLabel(model), description=override))
                if len(items) == 0:
                    items.append(
                        SelectItem(
                            value="__none__",
                            label="No models available",
                            description="Log in to a provider or configure an API key first",
                        )
                    )
                return items

            def levelTitle(ctx: dict[str, str]) -> str:
                m = defaultModelByValue.get(ctx["model"])
                return f"Thinking Level for {modelDisplayLabel(m) if m else ctx['model']}"

            def levelOptions(ctx: dict[str, str]) -> list[SelectItem]:
                model = defaultModelByValue.get(ctx["model"])
                if not model:
                    return []
                levels: list[ThinkingLevel] = list(getSupportedThinkingLevels(model)) if model.reasoning else ["off"]
                activeLevel = currentModelThinkingLevels.get(ctx["model"])
                items = [
                    SelectItem(
                        value=level,
                        label=f"{'✓ ' if level == activeLevel else '  '}{level}",
                        description=THINKING_DESCRIPTIONS[level],
                    )
                    for level in levels
                ]
                if currentModelThinkingLevels.get(ctx["model"]) is not None:
                    items.append(
                        SelectItem(
                            value=CLEAR_OVERRIDE_VALUE,
                            label="  (clear override)",
                            description=f"Revert to global default ({config.thinkingLevel})",
                        )
                    )
                return items

            steps: list[SteppedSubmenuStep] = [
                SteppedSubmenuStep(
                    key="model",
                    title="Per-Model Thinking Level",
                    description="Select a model to configure",
                    options=modelOptions,
                    preselect=lambda _ctx: currentModelKey if currentModelKey is not None else currentDefaultModelKey,
                    searchable=True,
                    layout=MODEL_PICKER_LAYOUT,
                ),
                SteppedSubmenuStep(
                    key="level",
                    title=levelTitle,
                    description="Select default thinking level for this model",
                    options=levelOptions,
                    preselect=lambda ctx: currentModelThinkingLevels.get(ctx["model"]),
                ),
            ]

            def summary() -> str:
                return modelThinkingOverridesSummary(currentModelThinkingLevels)

            def onComplete(selections: dict[str, str]) -> None:
                model = defaultModelByValue.get(selections["model"])
                if not model:
                    return
                if selections["level"] == CLEAR_OVERRIDE_VALUE:
                    callbacks.onModelThinkingLevelRemove(model.provider, model.id)
                    currentModelThinkingLevels.pop(selections["model"], None)
                else:
                    level: Any = selections["level"]
                    callbacks.onModelThinkingLevelChange(model.provider, model.id, level)
                    currentModelThinkingLevels[selections["model"]] = level

            return SteppedSubmenu(steps, onComplete, lambda: done(summary()), {"loop": True})

        def themeSubmenu(currentValue: str, done: Callable[..., None]) -> ThemeSubmenu:
            return ThemeSubmenu(currentValue, config.terminalTheme, config.availableThemes, callbacks, done)

        items: list[SettingItem] = [
            SettingItem(
                id="autocompact",
                label="Auto-compact",
                description="Automatically compact context when it gets too large",
                currentValue="true" if config.autoCompact else "false",
                values=["true", "false"],
            ),
            SettingItem(
                id="steering-mode",
                label="Steering mode",
                description=(
                    "Enter while streaming queues steering messages. 'one-at-a-time': deliver one, wait for response. "
                    "'all': deliver all at once."
                ),
                currentValue=config.steeringMode,
                values=["one-at-a-time", "all"],
            ),
            SettingItem(
                id="follow-up-mode",
                label="Follow-up mode",
                description=(
                    f"{followUpKey} queues follow-up messages until agent stops. 'one-at-a-time': deliver one, wait "
                    "for response. 'all': deliver all at once."
                ),
                currentValue=config.followUpMode,
                values=["one-at-a-time", "all"],
            ),
            SettingItem(
                id="transport",
                label="Transport",
                description="Preferred transport for providers that support multiple transports",
                currentValue=config.transport,
                values=["sse", "websocket", "websocket-cached", "auto"],
            ),
            SettingItem(
                id="share-destination",
                label="Session share destination",
                description=(
                    "Where to upload sessions with /share (auto prefers Radius when authenticated, otherwise GitHub "
                    "Gist)"
                ),
                currentValue=config.sessionShareDestination,
                values=["auto", "gist", "radius"],
            ),
            SettingItem(
                id="http-idle-timeout",
                label="HTTP idle timeout",
                description=(
                    "Maximum idle gap while waiting for HTTP headers or body chunks. Disable for local models that "
                    "pause longer than five minutes."
                ),
                currentValue=formatHttpIdleTimeoutMs(config.httpIdleTimeoutMs),
                values=[str(choice["label"]) for choice in HTTP_IDLE_TIMEOUT_CHOICES],
            ),
            SettingItem(
                id="cache-warming-mode",
                label="Cache warming",
                description="off; streaming while the agent runs; idle also between runs while continuation stays profitable",
                currentValue=config.cacheWarmingMode,
                values=[*CACHE_WARMING_MODES],
            ),
            SettingItem(
                id="hide-thinking",
                label="Hide thinking",
                description="Hide thinking blocks in assistant responses",
                currentValue="true" if config.hideThinkingBlock else "false",
                values=["true", "false"],
            ),
            SettingItem(
                id="mermaid-rendering",
                label="Mermaid diagrams",
                description="Render Mermaid code blocks as Unicode diagrams",
                currentValue=config.mermaidRenderingMode,
                values=["off", "final", "streaming"],
            ),
            SettingItem(
                id="cache-miss-notices",
                label="Cache miss notices",
                description="Show transcript notices for cache costs and provider recovery diagnostics",
                currentValue="true" if config.showCacheMissNotices else "false",
                values=["true", "false"],
            ),
            SettingItem(
                id="collapse-changelog",
                label="Collapse changelog",
                description="Show condensed changelog after updates",
                currentValue="true" if config.collapseChangelog else "false",
                values=["true", "false"],
            ),
            SettingItem(
                id="quiet-startup",
                label="Quiet startup",
                description="Disable verbose printing at startup (header: keep only the startup header)",
                currentValue="header" if config.quietStartup == "header" else "true" if config.quietStartup else "false",
                values=["true", "header", "false"],
            ),
            SettingItem(
                id="install-telemetry",
                # MISAKA fork: MISAKA sends no install ping; the setting names the client "misaka" to these gateways.
                label="Client attribution",
                description='Name this client "misaka" to OpenRouter, NVIDIA and Cloudflare gateways; nothing else is sent',
                currentValue="true" if config.enableInstallTelemetry else "false",
                values=["true", "false"],
            ),
            SettingItem(
                id="default-project-trust",
                label="Default project trust",
                description="Fallback behavior when no extension or saved trust decision decides project trust",
                currentValue=DEFAULT_PROJECT_TRUST_LABELS[config.defaultProjectTrust],
                values=[*DEFAULT_PROJECT_TRUST_LABELS.values()],
            ),
            SettingItem(
                id="double-escape-action",
                label="Double-escape action",
                description="Action when pressing Escape twice with empty editor",
                currentValue=config.doubleEscapeAction,
                values=["tree", "fork", "none"],
            ),
            SettingItem(
                id="tree-filter-mode",
                label="Tree filter mode",
                description="Default filter when opening /tree",
                currentValue=config.treeFilterMode,
                values=["default", "no-tools", "user-only", "labeled-only", "all"],
            ),
            SettingItem(
                id="warnings",
                label="Warnings",
                description="Enable or disable individual warnings",
                currentValue="configure",
                submenu=warningsSubmenu,
            ),
            SettingItem(
                id="thinking",
                label="Thinking level",
                # MISAKA fork: a global default thinking level row; in a Last Order or Sister window the default saved
                # here is hers alone.
                description="Default reasoning depth; in a Last Order or Sister window, hers alone",
                currentValue=config.thinkingLevel,
                submenu=thinkingSubmenu,
            ),
            SettingItem(
                id="model-thinking",
                label="Default thinking level per model",
                # MISAKA fork: an agent with her own default thinking level keeps it.
                description=(
                    "Override the default thinking level for specific models (an agent with her own default keeps "
                    f"it). {cycleThinkingKey} cycles in-session."
                ),
                currentValue=modelThinkingOverridesSummary(currentModelThinkingLevels),
                submenu=modelThinkingSubmenu,
            ),
            SettingItem(
                id="theme",
                label="Theme",
                description="Color theme for the interface",
                currentValue=config.currentTheme,
                submenu=themeSubmenu,
            ),
        ]

        # Only show image toggle if terminal supports it
        if supportsImages:
            # Insert after autocompact
            items.insert(
                1,
                SettingItem(
                    id="show-images",
                    label="Show images",
                    description="Render images inline in terminal",
                    currentValue="true" if config.showImages else "false",
                    values=["true", "false"],
                ),
            )
            items.insert(
                2,
                SettingItem(
                    id="image-width-cells",
                    label="Image width",
                    description="Preferred inline image width in terminal cells",
                    currentValue=str(config.imageWidthCells),
                    values=["60", "80", "120"],
                ),
            )

        # Image auto-resize toggle (always available, affects both attached and read images)
        items.insert(
            3 if supportsImages else 1,
            SettingItem(
                id="auto-resize-images",
                label="Auto-resize images",
                description="Resize large images to 2000x2000 max for better model compatibility",
                currentValue="true" if config.autoResizeImages else "false",
                values=["true", "false"],
            ),
        )

        def indexOf(id: str) -> int:
            return next((index for index, item in enumerate(items) if item.id == id), -1)

        # Block images toggle (always available, insert after auto-resize-images)
        autoResizeIndex = indexOf("auto-resize-images")
        items.insert(
            autoResizeIndex + 1,
            SettingItem(
                id="block-images",
                label="Block images",
                description="Prevent images from being sent to LLM providers",
                currentValue="true" if config.blockImages else "false",
                values=["true", "false"],
            ),
        )

        # Skill commands toggle (insert after block-images)
        blockImagesIndex = indexOf("block-images")
        items.insert(
            blockImagesIndex + 1,
            SettingItem(
                id="skill-commands",
                label="Skill commands",
                # MISAKA fork: skills register as /skill-name (no /skill: prefix), and /skill stays available.
                description="Register automatic /skill-name commands (explicit /skill remains available)",
                currentValue="true" if config.enableSkillCommands else "false",
                values=["true", "false"],
            ),
        )

        # Hardware cursor toggle (insert after skill-commands)
        skillCommandsIndex = indexOf("skill-commands")
        items.insert(
            skillCommandsIndex + 1,
            SettingItem(
                id="show-hardware-cursor",
                label="Show hardware cursor",
                # MISAKA fork: MISAKA's drawn cursor (pi: "Pi's").
                description="Use the terminal cursor instead of MISAKA's drawn cursor",
                currentValue="true" if config.showHardwareCursor else "false",
                values=["true", "false"],
            ),
        )

        # Editor padding toggle (insert after show-hardware-cursor)
        hardwareCursorIndex = indexOf("show-hardware-cursor")
        items.insert(
            hardwareCursorIndex + 1,
            SettingItem(
                id="editor-padding",
                label="Editor padding",
                description="Horizontal padding for input editor (0-3)",
                currentValue=str(config.editorPaddingX),
                values=["0", "1", "2", "3"],
            ),
        )

        # Output padding toggle (insert after editor-padding)
        editorPaddingIndex = indexOf("editor-padding")
        items.insert(
            editorPaddingIndex + 1,
            SettingItem(
                id="output-padding",
                label="Output padding",
                description="Horizontal padding for messages, tool output, and command output",
                currentValue=str(config.outputPad),
                values=["0", "1"],
            ),
        )

        # Autocomplete max visible toggle (insert after output-padding)
        outputPaddingIndex = indexOf("output-padding")
        items.insert(
            outputPaddingIndex + 1,
            SettingItem(
                id="autocomplete-max-visible",
                label="Autocomplete max items",
                description="Max visible items in autocomplete dropdown (3-20)",
                currentValue=str(config.autocompleteMaxVisible),
                values=["3", "5", "7", "10", "15", "20"],
            ),
        )

        # Clear on shrink toggle (insert after autocomplete-max-visible)
        autocompleteIndex = indexOf("autocomplete-max-visible")
        items.insert(
            autocompleteIndex + 1,
            SettingItem(
                id="clear-on-shrink",
                label="Clear on shrink",
                description="Clear empty rows when content shrinks (may cause flicker)",
                currentValue="true" if config.clearOnShrink else "false",
                values=["true", "false"],
            ),
        )

        # Terminal progress toggle (insert after clear-on-shrink)
        clearOnShrinkIndex = indexOf("clear-on-shrink")
        items.insert(
            clearOnShrinkIndex + 1,
            SettingItem(
                id="terminal-progress",
                label="Terminal progress",
                description="Show OSC 9;4 progress indicators in the terminal tab bar",
                currentValue="true" if config.showTerminalProgress else "false",
                values=["true", "false"],
            ),
        )

        # Add borders
        self.addChild(DynamicBorder())

        def onChange(id: str, newValue: str) -> None:
            value: Any = newValue
            match id:
                case "autocompact":
                    callbacks.onAutoCompactChange(newValue == "true")
                case "show-images":
                    callbacks.onShowImagesChange(newValue == "true")
                case "image-width-cells":
                    callbacks.onImageWidthCellsChange(int(newValue, 10))
                case "auto-resize-images":
                    callbacks.onAutoResizeImagesChange(newValue == "true")
                case "block-images":
                    callbacks.onBlockImagesChange(newValue == "true")
                case "skill-commands":
                    callbacks.onEnableSkillCommandsChange(newValue == "true")
                case "steering-mode":
                    callbacks.onSteeringModeChange(value)
                case "follow-up-mode":
                    callbacks.onFollowUpModeChange(value)
                case "transport":
                    callbacks.onTransportChange(value)
                case "share-destination":
                    callbacks.onSessionShareDestinationChange(value)
                case "http-idle-timeout":
                    choice: Any = next((item for item in HTTP_IDLE_TIMEOUT_CHOICES if item["label"] == newValue), None)
                    if choice:
                        callbacks.onHttpIdleTimeoutMsChange(choice["timeoutMs"])
                case "cache-warming-mode":
                    callbacks.onCacheWarmingModeChange(value)
                case "hide-thinking":
                    callbacks.onHideThinkingBlockChange(newValue == "true")
                case "mermaid-rendering":
                    callbacks.onMermaidRenderingModeChange(newValue)  # type: ignore[arg-type]
                case "cache-miss-notices":
                    callbacks.onShowCacheMissNoticesChange(newValue == "true")
                case "collapse-changelog":
                    callbacks.onCollapseChangelogChange(newValue == "true")
                case "quiet-startup":
                    callbacks.onQuietStartupChange("header" if newValue == "header" else newValue == "true")
                case "install-telemetry":
                    callbacks.onEnableInstallTelemetryChange(newValue == "true")
                case "default-project-trust":
                    defaultProjectTrust = DEFAULT_PROJECT_TRUST_BY_LABEL.get(newValue)
                    if defaultProjectTrust:
                        callbacks.onDefaultProjectTrustChange(defaultProjectTrust)
                case "double-escape-action":
                    callbacks.onDoubleEscapeActionChange(value)
                case "tree-filter-mode":
                    callbacks.onTreeFilterModeChange(value)
                case "show-hardware-cursor":
                    callbacks.onShowHardwareCursorChange(newValue == "true")
                case "editor-padding":
                    callbacks.onEditorPaddingXChange(int(newValue, 10))
                case "output-padding":
                    callbacks.onOutputPadChange(0 if newValue == "0" else 1)
                case "autocomplete-max-visible":
                    callbacks.onAutocompleteMaxVisibleChange(int(newValue, 10))
                case "clear-on-shrink":
                    callbacks.onClearOnShrinkChange(newValue == "true")
                case "terminal-progress":
                    callbacks.onShowTerminalProgressChange(newValue == "true")
                case "theme":
                    callbacks.onThemeChange(newValue)

        self.settingsList = SettingsList(
            items,
            10,
            getSettingsListTheme(),
            onChange,
            callbacks.onCancel,
            SettingsListOptions(enableSearch=True),
        )

        self.addChild(self.settingsList)
        self.addChild(DynamicBorder())

    def getSettingsList(self) -> SettingsList:
        return self.settingsList


__all__ = [
    "SettingsCallbacks",
    "SettingsConfig",
    "SettingsSelectorComponent",
]
