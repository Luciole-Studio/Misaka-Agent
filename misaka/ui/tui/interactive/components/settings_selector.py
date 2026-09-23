"""Interactive settings selector with submenus for theme and reasoning choices."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from misaka.agent.types import ThinkingLevel
from misaka.ai.models import get_supported_thinking_levels
from misaka.ai.types import Model, Transport
from misaka.core.http_dispatcher import (
    HTTP_IDLE_TIMEOUT_CHOICES,
    formatHttpIdleTimeoutMs,
)
from misaka.core.settings_manager import (
    CACHE_WARMING_MODES,
    DefaultProjectTrust,
    WarningSettings,
)
from misaka.ui.tui import (
    Container,
    SelectItem,
    SelectList,
    SelectListLayoutOptions,
    SettingItem,
    SettingsList,
    Spacer,
    Text,
    getCapabilities,
)
from misaka.ui.tui.components import SettingsListOptions
from misaka.ui.tui.interactive.theme.theme import (
    get_select_list_theme,
    get_settings_list_theme,
    theme,
)

from .dynamic_border import DynamicBorder
from .keybinding_hints import key_display_text

type SteeringMode = Literal["all", "one-at-a-time"]
type DoubleEscapeAction = Literal["fork", "tree", "none"]
type TreeFilterMode = Literal["default", "no-tools", "user-only", "labeled-only", "all"]

SETTINGS_SUBMENU_SELECT_LIST_LAYOUT = SelectListLayoutOptions(minPrimaryColumnWidth=12, maxPrimaryColumnWidth=32)

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
DEFAULT_PROJECT_TRUST_BY_LABEL = {
    label: value for value, label in DEFAULT_PROJECT_TRUST_LABELS.items()
}


@dataclass(slots=True)
class SettingsConfig:
    autoCompact: bool
    showImages: bool
    imageWidthCells: int
    autoResizeImages: bool
    blockImages: bool
    enableSkillCommands: bool
    steeringMode: SteeringMode
    followUpMode: SteeringMode
    transport: Transport
    httpIdleTimeoutMs: int
    cacheWarmingMode: str
    thinkingLevel: ThinkingLevel
    availableThinkingLevels: list[ThinkingLevel]
    currentModel: Model | None
    availableDefaultModels: list[Model]
    modelThinkingLevels: dict[str, ThinkingLevel]
    currentTheme: str
    availableThemes: list[str]
    hideThinkingBlock: bool
    collapseChangelog: bool
    doubleEscapeAction: DoubleEscapeAction
    treeFilterMode: TreeFilterMode
    showHardwareCursor: bool
    editorPaddingX: int
    outputPad: Literal[0, 1]
    autocompleteMaxVisible: int
    quietStartup: bool
    defaultProjectTrust: DefaultProjectTrust
    clearOnShrink: bool
    showTerminalProgress: bool
    warnings: WarningSettings
    # Off is also SettingsManager's default; a host that does not pass the matching
    # callback below shows no row for it.
    enableInstallTelemetry: bool = False


@dataclass(slots=True)
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
    onHttpIdleTimeoutMsChange: Callable[[int], None]
    onCacheWarmingModeChange: Callable[[str], None]
    onThinkingLevelChange: Callable[[ThinkingLevel], None]
    onModelThinkingLevelChange: Callable[[str, str, ThinkingLevel], None]
    onModelThinkingLevelRemove: Callable[[str, str], None]
    onThemeChange: Callable[[str], None]
    onThemePreview: Callable[[str], None] | None
    onHideThinkingBlockChange: Callable[[bool], None]
    onCollapseChangelogChange: Callable[[bool], None]
    onDoubleEscapeActionChange: Callable[[DoubleEscapeAction], None]
    onTreeFilterModeChange: Callable[[TreeFilterMode], None]
    onShowHardwareCursorChange: Callable[[bool], None]
    onEditorPaddingXChange: Callable[[int], None]
    onOutputPadChange: Callable[[Literal[0, 1]], None]
    onAutocompleteMaxVisibleChange: Callable[[int], None]
    onQuietStartupChange: Callable[[bool], None]
    onDefaultProjectTrustChange: Callable[[DefaultProjectTrust], None]
    onClearOnShrinkChange: Callable[[bool], None]
    onShowTerminalProgressChange: Callable[[bool], None]
    onWarningsChange: Callable[[WarningSettings], None]
    onCancel: Callable[[], None]
    # Optional: the client-attribution row appears only for a host that passes it.
    onEnableInstallTelemetryChange: Callable[[bool], None] | None = None


class WarningSettingsSubmenu(Container):
    def __init__(self, warnings: WarningSettings, onChange, onCancel) -> None:
        super().__init__()
        self.state = dict(warnings)
        items = [
            SettingItem(
                id="anthropic-extra-usage",
                label="Anthropic extra usage",
                description="Warn when Anthropic subscription auth may use paid extra usage",
                currentValue="true" if self.state.get("anthropicExtraUsage", True) else "false",
                values=["true", "false"],
            )
        ]
        self.settingsList = SettingsList(
            items,
            min(len(items), 10),
            get_settings_list_theme(),
            lambda setting_id, new_value: self._apply_change(setting_id, new_value, onChange),
            onCancel,
        )
        self.addChild(self.settingsList)

    def _apply_change(self, setting_id: str, new_value: str, onChange) -> None:
        if setting_id == "anthropic-extra-usage":
            self.state = {**self.state, "anthropicExtraUsage": new_value == "true"}
            onChange(dict(self.state))

    def handleInput(self, data: str) -> None:
        self.settingsList.handleInput(data)


class SelectSubmenu(Container):
    def __init__(
        self,
        title: str,
        description: str,
        options: list[SelectItem],
        currentValue: str,
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        onSelectionChange: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.addChild(Text(theme.bold(theme.fg("accent", title)), 0, 0))
        if description:
            self.addChild(Spacer(1))
            self.addChild(Text(theme.fg("muted", description), 0, 0))
        self.addChild(Spacer(1))

        self.selectList = SelectList(
            options,
            min(len(options), 10),
            get_select_list_theme(),
            SETTINGS_SUBMENU_SELECT_LIST_LAYOUT,
        )
        current_index = next((index for index, item in enumerate(options) if item.value == currentValue), -1)
        if current_index >= 0:
            self.selectList.setSelectedIndex(current_index)

        self.selectList.onSelect = lambda item: onSelect(item.value)
        self.selectList.onCancel = onCancel
        if onSelectionChange is not None:
            self.selectList.onSelectionChange = lambda item: onSelectionChange(item.value)

        self.addChild(self.selectList)
        self.addChild(Spacer(1))
        self.addChild(Text(theme.fg("dim", "  Enter to select · Esc to go back"), 0, 0))

    def handleInput(self, data: str) -> None:
        self.selectList.handleInput(data)


_CLEAR_MODEL_THINKING_OVERRIDE = "__clear__"
_NO_MODEL = "__none__"


def _model_setting_key(model: Model) -> str:
    return f"{model.provider}/{model.id}"


def _model_thinking_summary(overrides: dict[str, ThinkingLevel]) -> str:
    count = len(overrides)
    return "none" if count == 0 else f"{count} configured"


class ModelThinkingSettingsSubmenu(Container):
    """Two-step model/level selector using Misaka's existing submenu primitives."""

    def __init__(
        self,
        models: list[Model],
        currentModel: Model | None,
        globalDefault: ThinkingLevel,
        overrides: dict[str, ThinkingLevel],
        onChange: Callable[[str, str, ThinkingLevel], None],
        onRemove: Callable[[str, str], None],
        onDone: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.models = sorted(
            models,
            key=lambda model: (
                0 if currentModel is not None and _model_setting_key(model) == _model_setting_key(currentModel) else 1,
                model.provider,
                model.id,
            ),
        )
        self.modelsByKey = {_model_setting_key(model): model for model in self.models}
        self.currentModelKey = _model_setting_key(currentModel) if currentModel is not None else None
        self.globalDefault = globalDefault
        self.overrides = dict(overrides)
        self.onChange = onChange
        self.onRemove = onRemove
        self.onDone = onDone
        self.activeSubmenu: SelectSubmenu | None = None
        self._show_models()

    def _replace(self, submenu: SelectSubmenu) -> None:
        self.clear()
        self.activeSubmenu = submenu
        self.addChild(submenu)

    def _show_models(self, selectedKey: str | None = None) -> None:
        options = [
            SelectItem(
                value=key,
                label=f"{model.id} [{model.provider}]",
                description=self.overrides.get(key),
            )
            for key, model in self.modelsByKey.items()
        ]
        if not options:
            options = [
                SelectItem(
                    value=_NO_MODEL,
                    label="No models available",
                    description="Log in to a provider or configure an API key first",
                )
            ]
        self._replace(
            SelectSubmenu(
                "Per-Model Thinking Level",
                "Select a model to configure",
                options,
                selectedKey or self.currentModelKey or "",
                self._select_model,
                lambda: self.onDone(_model_thinking_summary(self.overrides)),
            )
        )

    def _select_model(self, key: str) -> None:
        model = self.modelsByKey.get(key)
        if model is None:
            return
        levels = get_supported_thinking_levels(model)
        options = [
            SelectItem(value=level, label=level, description=THINKING_DESCRIPTIONS[level])
            for level in levels
        ]
        if key in self.overrides:
            options.append(
                SelectItem(
                    value=_CLEAR_MODEL_THINKING_OVERRIDE,
                    label="(clear override)",
                    description=f"Revert to global default ({self.globalDefault})",
                )
            )
        self._replace(
            SelectSubmenu(
                f"Thinking Level for {model.id} [{model.provider}]",
                "Select default thinking level for this model",
                options,
                self.overrides.get(key, ""),
                lambda level: self._select_level(model, level),
                lambda: self._show_models(key),
            )
        )

    def _select_level(self, model: Model, level: str) -> None:
        key = _model_setting_key(model)
        if level == _CLEAR_MODEL_THINKING_OVERRIDE:
            self.onRemove(model.provider, model.id)
            self.overrides.pop(key, None)
        else:
            self.onChange(model.provider, model.id, level)  # type: ignore[arg-type]
            self.overrides[key] = level  # type: ignore[assignment]
        self._show_models(key)

    def handleInput(self, data: str) -> None:
        if self.activeSubmenu is not None:
            self.activeSubmenu.handleInput(data)


class SettingsSelectorComponent(Container):
    def __init__(self, config: SettingsConfig, callbacks: SettingsCallbacks) -> None:
        super().__init__()
        supports_images = bool(getCapabilities().images)
        follow_up_key = key_display_text("app.message.followUp")
        cycle_thinking_key = key_display_text("app.thinking.cycle")
        self.currentWarnings = dict(config.warnings)

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
                    f"{follow_up_key} queues follow-up messages until agent stops. "
                    "'one-at-a-time': deliver one, wait for response. 'all': deliver all at once."
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
                id="http-idle-timeout",
                label="HTTP idle timeout",
                description=(
                    "Maximum idle gap while waiting for HTTP headers or body chunks. "
                    "Disable for local models that pause longer than five minutes."
                ),
                currentValue=formatHttpIdleTimeoutMs(config.httpIdleTimeoutMs),
                values=[str(choice["label"]) for choice in HTTP_IDLE_TIMEOUT_CHOICES],
            ),
            SettingItem(
                id="cache-warming-mode",
                label="Cache warming",
                description="off; streaming while the agent runs; idle also between runs while continuation stays profitable",
                currentValue=config.cacheWarmingMode,
                values=list(CACHE_WARMING_MODES),
            ),
            SettingItem(
                id="hide-thinking",
                label="Hide thinking",
                description="Hide thinking blocks in assistant responses",
                currentValue="true" if config.hideThinkingBlock else "false",
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
                description="Disable verbose printing at startup",
                currentValue="true" if config.quietStartup else "false",
                values=["true", "false"],
            ),
            SettingItem(
                id="default-project-trust",
                label="Default project trust",
                description=(
                    "Fallback behavior when no extension or saved trust decision "
                    "decides project trust"
                ),
                currentValue=DEFAULT_PROJECT_TRUST_LABELS[config.defaultProjectTrust],
                values=list(DEFAULT_PROJECT_TRUST_LABELS.values()),
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
                submenu=lambda _current_value, done: WarningSettingsSubmenu(
                    self.currentWarnings,
                    lambda warnings: self._update_warnings(warnings, callbacks),
                    lambda: done(),
                ),
            ),
            SettingItem(
                id="thinking",
                label="Thinking level",
                description="Reasoning depth for thinking-capable models",
                currentValue=config.thinkingLevel,
                submenu=lambda current_value, done: SelectSubmenu(
                    "Thinking Level",
                    "Select reasoning depth for thinking-capable models",
                    [
                        SelectItem(value=level, label=level, description=THINKING_DESCRIPTIONS[level])
                        for level in config.availableThinkingLevels
                    ],
                    current_value,
                    lambda value: self._select_and_close(value, callbacks.onThinkingLevelChange, done),
                    lambda: done(),
                ),
            ),
            SettingItem(
                id="model-thinking",
                label="Default thinking level per model",
                description=(
                    "Override the default thinking level for specific models. "
                    f"{cycle_thinking_key} cycles in-session."
                ),
                currentValue=_model_thinking_summary(config.modelThinkingLevels),
                submenu=lambda _current_value, done: ModelThinkingSettingsSubmenu(
                    config.availableDefaultModels,
                    config.currentModel,
                    config.thinkingLevel,
                    config.modelThinkingLevels,
                    callbacks.onModelThinkingLevelChange,
                    callbacks.onModelThinkingLevelRemove,
                    done,
                ),
            ),
            SettingItem(
                id="theme",
                label="Theme",
                description="Color theme for the interface",
                currentValue=config.currentTheme,
                submenu=lambda current_value, done: SelectSubmenu(
                    "Theme",
                    "Select color theme",
                    [SelectItem(value=name, label=name) for name in config.availableThemes],
                    current_value,
                    lambda value: self._select_and_close(value, callbacks.onThemeChange, done),
                    lambda: self._cancel_theme_preview(current_value, callbacks, done),
                    (lambda value: callbacks.onThemePreview(value)) if callbacks.onThemePreview else None,
                ),
            ),
        ]

        if supports_images:
            items[1:1] = [
                SettingItem(
                    id="show-images",
                    label="Show images",
                    description="Render images inline in terminal",
                    currentValue="true" if config.showImages else "false",
                    values=["true", "false"],
                ),
                SettingItem(
                    id="image-width-cells",
                    label="Image width",
                    description="Preferred inline image width in terminal cells",
                    currentValue=str(config.imageWidthCells),
                    values=["60", "80", "120"],
                ),
            ]

        insert_index = 3 if supports_images else 1
        items[insert_index:insert_index] = [
            SettingItem(
                id="auto-resize-images",
                label="Auto-resize images",
                description="Resize large images to 2000x2000 max for better model compatibility",
                currentValue="true" if config.autoResizeImages else "false",
                values=["true", "false"],
            )
        ]

        block_images_index = next(index for index, item in enumerate(items) if item.id == "auto-resize-images") + 1
        items[block_images_index:block_images_index] = [
            SettingItem(
                id="block-images",
                label="Block images",
                description="Prevent images from being sent to LLM providers",
                currentValue="true" if config.blockImages else "false",
                values=["true", "false"],
            )
        ]

        extra_items = [
            (
                "skill-commands",
                "Skill commands",
                "Register automatic /skill-name commands (explicit /skill remains available)",
                "true" if config.enableSkillCommands else "false",
                ["true", "false"],
            ),
            (
                "show-hardware-cursor",
                "Show hardware cursor",
                "Show the terminal cursor while still positioning it for IME support",
                "true" if config.showHardwareCursor else "false",
                ["true", "false"],
            ),
            (
                "editor-padding",
                "Editor padding",
                "Horizontal padding for input editor (0-3)",
                str(config.editorPaddingX),
                ["0", "1", "2", "3"],
            ),
            (
                "output-padding",
                "Output padding",
                "Horizontal padding for user messages, assistant messages, and thinking",
                str(config.outputPad),
                ["0", "1"],
            ),
            (
                "autocomplete-max-visible",
                "Autocomplete max items",
                "Max visible items in autocomplete dropdown (3-20)",
                str(config.autocompleteMaxVisible),
                ["3", "5", "7", "10", "15", "20"],
            ),
            (
                "clear-on-shrink",
                "Clear on shrink",
                "Clear empty rows when content shrinks (may cause flicker)",
                "true" if config.clearOnShrink else "false",
                ["true", "false"],
            ),
            (
                "terminal-progress",
                "Terminal progress",
                "Show OSC 9;4 progress indicators in the terminal tab bar",
                "true" if config.showTerminalProgress else "false",
                ["true", "false"],
            ),
        ]
        if callbacks.onEnableInstallTelemetryChange is not None:
            extra_items.append(
                (
                    "install-telemetry",
                    "Client attribution",
                    "Name this client \"misaka\" to OpenRouter, NVIDIA and Cloudflare gateways; nothing else is sent",
                    "true" if config.enableInstallTelemetry else "false",
                    ["true", "false"],
                )
            )

        insert_after = next(index for index, item in enumerate(items) if item.id == "block-images") + 1
        for offset, (item_id, label, description, current_value, values) in enumerate(extra_items):
            items.insert(
                insert_after + offset,
                SettingItem(
                    id=item_id,
                    label=label,
                    description=description,
                    currentValue=current_value,
                    values=values,
                ),
            )

        self.addChild(DynamicBorder())
        self.settingsList = SettingsList(
            items,
            10,
            get_settings_list_theme(),
            lambda setting_id, new_value: self._apply_change(setting_id, new_value, callbacks),
            callbacks.onCancel,
            SettingsListOptions(enableSearch=True),
        )
        self.addChild(self.settingsList)
        self.addChild(DynamicBorder())

    def _update_warnings(self, warnings: WarningSettings, callbacks: SettingsCallbacks) -> None:
        self.currentWarnings = dict(warnings)
        callbacks.onWarningsChange(dict(warnings))

    def _select_and_close(
        self,
        value: str,
        callback: Callable[[str], None],
        done: Callable[[str | None], None],
    ) -> None:
        callback(value)
        done(value)

    def _cancel_theme_preview(
        self,
        current_value: str,
        callbacks: SettingsCallbacks,
        done: Callable[[str | None], None],
    ) -> None:
        if callbacks.onThemePreview is not None:
            callbacks.onThemePreview(current_value)
        done()

    def _apply_change(self, setting_id: str, new_value: str, callbacks: SettingsCallbacks) -> None:
        match setting_id:
            case "autocompact":
                callbacks.onAutoCompactChange(new_value == "true")
            case "show-images":
                callbacks.onShowImagesChange(new_value == "true")
            case "image-width-cells":
                callbacks.onImageWidthCellsChange(int(new_value))
            case "auto-resize-images":
                callbacks.onAutoResizeImagesChange(new_value == "true")
            case "block-images":
                callbacks.onBlockImagesChange(new_value == "true")
            case "skill-commands":
                callbacks.onEnableSkillCommandsChange(new_value == "true")
            case "steering-mode":
                callbacks.onSteeringModeChange(new_value)
            case "follow-up-mode":
                callbacks.onFollowUpModeChange(new_value)
            case "transport":
                callbacks.onTransportChange(new_value)  # type: ignore[arg-type]
            case "http-idle-timeout":
                choice = next(
                    (item for item in HTTP_IDLE_TIMEOUT_CHOICES if item["label"] == new_value),
                    None,
                )
                if choice is not None:
                    callbacks.onHttpIdleTimeoutMsChange(int(choice["timeoutMs"]))
            case "cache-warming-mode":
                callbacks.onCacheWarmingModeChange(new_value)
            case "hide-thinking":
                callbacks.onHideThinkingBlockChange(new_value == "true")
            case "collapse-changelog":
                callbacks.onCollapseChangelogChange(new_value == "true")
            case "quiet-startup":
                callbacks.onQuietStartupChange(new_value == "true")
            case "default-project-trust":
                value = DEFAULT_PROJECT_TRUST_BY_LABEL.get(new_value)
                if value is not None:
                    callbacks.onDefaultProjectTrustChange(value)
            case "double-escape-action":
                callbacks.onDoubleEscapeActionChange(new_value)
            case "tree-filter-mode":
                callbacks.onTreeFilterModeChange(new_value)
            case "show-hardware-cursor":
                callbacks.onShowHardwareCursorChange(new_value == "true")
            case "editor-padding":
                callbacks.onEditorPaddingXChange(int(new_value))
            case "output-padding":
                callbacks.onOutputPadChange(0 if new_value == "0" else 1)
            case "autocomplete-max-visible":
                callbacks.onAutocompleteMaxVisibleChange(int(new_value))
            case "clear-on-shrink":
                callbacks.onClearOnShrinkChange(new_value == "true")
            case "terminal-progress":
                callbacks.onShowTerminalProgressChange(new_value == "true")
            case "install-telemetry":
                if callbacks.onEnableInstallTelemetryChange is not None:
                    callbacks.onEnableInstallTelemetryChange(new_value == "true")

    def getSettingsList(self) -> SettingsList:
        return self.settingsList

    def handleInput(self, data: str) -> None:
        # Without this, keystrokes are dropped once /settings opens (even Esc).
        # Forward to the active submenu if one is open, else the settings list.
        submenu = getattr(self, "activeSubmenu", None)
        target = submenu if submenu is not None else self.settingsList
        handler = getattr(target, "handleInput", None)
        if callable(handler):
            handler(data)


__all__ = [
    "SettingsCallbacks",
    "SettingsConfig",
    "SettingsSelectorComponent",
]
