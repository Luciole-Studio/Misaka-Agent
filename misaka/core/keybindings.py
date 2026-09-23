"""Coding-agent keybinding registry."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Literal

from misaka.config import get_agent_dir
from misaka.ui.tui import (
    TUI_KEYBINDINGS,
    Keybinding,
    KeybindingDefinition,
    KeybindingDefinitions,
    KeybindingsConfig,
    KeyId,
)
from misaka.ui.tui import (
    KeybindingsManager as TuiKeybindingsManager,
)
from misaka.utils.clipboard_image import is_wsl

type AppKeybinding = Literal[
    "app.interrupt",
    "app.clear",
    "app.exit",
    "app.suspend",
    "app.thinking.cycle",
    "app.model.cycleForward",
    "app.model.cycleBackward",
    "app.model.select",
    "app.tools.expand",
    "app.thinking.toggle",
    "app.session.toggleNamedFilter",
    "app.editor.external",
    "app.message.copy",
    "app.message.followUp",
    "app.message.dequeue",
    "app.clipboard.pasteImage",
    "app.session.new",
    "app.session.tree",
    "app.session.fork",
    "app.session.resume",
    "app.tree.foldOrUp",
    "app.tree.unfoldOrDown",
    "app.tree.editLabel",
    "app.tree.toggleLabelTimestamp",
    "app.session.togglePath",
    "app.session.toggleSort",
    "app.session.rename",
    "app.session.delete",
    "app.session.deleteNoninvasive",
    "app.models.save",
    "app.models.enableAll",
    "app.models.clearAll",
    "app.models.toggleProvider",
    "app.models.reorderUp",
    "app.models.reorderDown",
    "app.tree.filter.default",
    "app.tree.filter.noTools",
    "app.tree.filter.userOnly",
    "app.tree.filter.labeledOnly",
    "app.tree.filter.all",
    "app.tree.filter.cycleForward",
    "app.tree.filter.cycleBackward",
]
type AppKeybindings = dict[AppKeybinding, Literal[True]]



def useWindowsKeybindings(platform: str = sys.platform, env: dict[str, str] | None = None) -> bool:
    """pi keybindings.ts:61-66 (27b7a626): a WSL terminal is a Windows terminal as far as key chords go."""
    return platform == "win32" or (platform.startswith("linux") and is_wsl(env))


def _default_keybindings(windowsKeybindings: bool, platform: str = sys.platform) -> KeybindingDefinitions:
    return {
        **TUI_KEYBINDINGS,
        "tui.editor.undo": KeybindingDefinition(
            "ctrl+z" if platform == "win32" else "alt+z" if windowsKeybindings else "ctrl+-",
            TUI_KEYBINDINGS["tui.editor.undo"].description,
        ),
        "app.interrupt": KeybindingDefinition("escape", "Cancel or abort"),
        "app.clear": KeybindingDefinition("ctrl+c", "Clear editor"),
        "app.exit": KeybindingDefinition("ctrl+d", "Exit when editor is empty"),
        "app.suspend": KeybindingDefinition([] if platform == "win32" else "ctrl+z", "Suspend to background"),
        "app.thinking.cycle": KeybindingDefinition("shift+tab", "Cycle thinking level"),
        "app.model.cycleForward": KeybindingDefinition("ctrl+p", "Cycle to next model"),
        "app.model.cycleBackward": KeybindingDefinition(
            "alt+p" if windowsKeybindings else "shift+ctrl+p", "Cycle to previous model"
        ),
        "app.model.select": KeybindingDefinition("ctrl+l", "Open model selector"),
        "app.tools.expand": KeybindingDefinition("ctrl+o", "Toggle tool output"),
        "app.thinking.toggle": KeybindingDefinition("ctrl+t", "Toggle thinking blocks"),
        "app.session.toggleNamedFilter": KeybindingDefinition("ctrl+n", "Toggle named session filter"),
        "app.editor.external": KeybindingDefinition("ctrl+g", "Open external editor"),
        "app.message.copy": KeybindingDefinition("ctrl+x", "Copy message to clipboard"),
        "app.message.followUp": KeybindingDefinition(
            "ctrl+q" if windowsKeybindings else "alt+enter", "Queue follow-up message"
        ),
        "app.message.dequeue": KeybindingDefinition("alt+q" if windowsKeybindings else "alt+up", "Restore queued messages"),
        "app.clipboard.pasteImage": KeybindingDefinition(
            "alt+v" if windowsKeybindings else "ctrl+v",
            "Paste image from clipboard",
        ),
        "app.session.new": KeybindingDefinition([], "Start a new session"),
        "app.session.tree": KeybindingDefinition([], "Open session tree"),
        "app.session.fork": KeybindingDefinition([], "Fork current session"),
        "app.session.resume": KeybindingDefinition([], "Resume a session"),
        "app.tree.foldOrUp": KeybindingDefinition(
            ["alt+left", "ctrl+left"] if platform == "darwin" else ["ctrl+left", "alt+left"],
            "Fold tree branch or move up",
        ),
        "app.tree.unfoldOrDown": KeybindingDefinition(
            ["alt+right", "ctrl+right"] if platform == "darwin" else ["ctrl+right", "alt+right"],
            "Unfold tree branch or move down",
        ),
        "app.tree.editLabel": KeybindingDefinition("shift+l", "Edit tree label"),
        "app.tree.toggleLabelTimestamp": KeybindingDefinition("shift+t", "Toggle tree label timestamps"),
        "app.session.togglePath": KeybindingDefinition("ctrl+p", "Toggle session path display"),
        "app.session.toggleSort": KeybindingDefinition("ctrl+s", "Toggle session sort mode"),
        "app.session.rename": KeybindingDefinition("ctrl+r", "Rename session"),
        "app.session.delete": KeybindingDefinition("ctrl+d", "Delete session"),
        "app.session.deleteNoninvasive": KeybindingDefinition("ctrl+backspace", "Delete session when query is empty"),
        "app.models.save": KeybindingDefinition("ctrl+s", "Save model selection"),
        "app.models.enableAll": KeybindingDefinition("ctrl+a", "Enable all models"),
        "app.models.clearAll": KeybindingDefinition("ctrl+x", "Clear all models"),
        "app.models.toggleProvider": KeybindingDefinition("ctrl+p", "Toggle all models for provider"),
        "app.models.reorderUp": KeybindingDefinition("alt+up", "Move model up in order"),
        "app.models.reorderDown": KeybindingDefinition("alt+down", "Move model down in order"),
        "app.tree.filter.default": KeybindingDefinition("ctrl+d", "Tree filter: default view"),
        "app.tree.filter.noTools": KeybindingDefinition("ctrl+t", "Tree filter: hide tool results"),
        "app.tree.filter.userOnly": KeybindingDefinition("ctrl+u", "Tree filter: user messages only"),
        "app.tree.filter.labeledOnly": KeybindingDefinition("ctrl+l", "Tree filter: labeled entries only"),
        "app.tree.filter.all": KeybindingDefinition("ctrl+a", "Tree filter: show all entries"),
        "app.tree.filter.cycleForward": KeybindingDefinition("ctrl+o", "Tree filter: cycle forward"),
        "app.tree.filter.cycleBackward": KeybindingDefinition("shift+ctrl+o", "Tree filter: cycle backward"),
    }


KEYBINDINGS: KeybindingDefinitions = _default_keybindings(useWindowsKeybindings())

def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _to_keybindings_config(value: Any) -> KeybindingsConfig:
    if not _is_record(value):
        return {}

    config: KeybindingsConfig = {}
    for key, binding in value.items():
        if isinstance(binding, str):
            config[key] = binding
            continue
        if isinstance(binding, list) and all(isinstance(entry, str) for entry in binding):
            config[key] = list(binding)
    return config


def _load_raw_config(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8-sig") as handle:
            parsed = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None
    return parsed if _is_record(parsed) else None


class KeybindingsManager(TuiKeybindingsManager):
    def __init__(self, userBindings: KeybindingsConfig | None = None, configPath: str | None = None) -> None:
        super().__init__(KEYBINDINGS, userBindings)
        self.configPath = configPath

    @staticmethod
    def create(agentDir: str | None = None) -> KeybindingsManager:
        config_path = os.path.join(agentDir or get_agent_dir(), "keybindings.json")
        user_bindings = KeybindingsManager._load_from_file(config_path)
        return KeybindingsManager(user_bindings, config_path)

    def reload(self) -> None:
        if self.configPath is None:
            return
        self.setUserBindings(self._load_from_file(self.configPath))

    def getEffectiveConfig(self) -> KeybindingsConfig:
        return self.getResolvedBindings()

    @staticmethod
    def _load_from_file(path: str) -> KeybindingsConfig:
        raw_config = _load_raw_config(path)
        if raw_config is None:
            return {}
        return _to_keybindings_config(raw_config)

__all__ = [
    "KEYBINDINGS",
    "AppKeybinding",
    "AppKeybindings",
    "KeyId",
    "Keybinding",
    "KeybindingsConfig",
    "KeybindingsManager",
    "useWindowsKeybindings",
]
