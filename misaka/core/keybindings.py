"""Coding-agent keybinding registry and migration helpers."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from typing import Literal

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

from misaka.config import get_agent_dir

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

KEYBINDINGS: KeybindingDefinitions = {
    **TUI_KEYBINDINGS,
    "app.interrupt": KeybindingDefinition("escape", "Cancel or abort"),
    "app.clear": KeybindingDefinition("ctrl+c", "Clear editor"),
    "app.exit": KeybindingDefinition("ctrl+d", "Exit when editor is empty"),
    "app.suspend": KeybindingDefinition([] if os.name == "nt" else "ctrl+z", "Suspend to background"),
    "app.thinking.cycle": KeybindingDefinition("shift+tab", "Cycle thinking level"),
    "app.model.cycleForward": KeybindingDefinition("ctrl+p", "Cycle to next model"),
    "app.model.cycleBackward": KeybindingDefinition("shift+ctrl+p", "Cycle to previous model"),
    "app.model.select": KeybindingDefinition("ctrl+l", "Open model selector"),
    "app.tools.expand": KeybindingDefinition("ctrl+o", "Toggle tool output"),
    "app.thinking.toggle": KeybindingDefinition("ctrl+t", "Toggle thinking blocks"),
    "app.session.toggleNamedFilter": KeybindingDefinition("ctrl+n", "Toggle named session filter"),
    "app.editor.external": KeybindingDefinition("ctrl+g", "Open external editor"),
    "app.message.copy": KeybindingDefinition("ctrl+x", "Copy message to clipboard"),
    "app.message.followUp": KeybindingDefinition("alt+enter", "Queue follow-up message"),
    "app.message.dequeue": KeybindingDefinition("alt+up", "Restore queued messages"),
    "app.clipboard.pasteImage": KeybindingDefinition(
        "alt+v" if os.name == "nt" else "ctrl+v",
        "Paste image from clipboard",
    ),
    "app.session.new": KeybindingDefinition([], "Start a new session"),
    "app.session.tree": KeybindingDefinition([], "Open session tree"),
    "app.session.fork": KeybindingDefinition([], "Fork current session"),
    "app.session.resume": KeybindingDefinition([], "Resume a session"),
    "app.tree.foldOrUp": KeybindingDefinition(
        ["alt+left", "ctrl+left"] if sys.platform == "darwin" else ["ctrl+left", "alt+left"],
        "Fold tree branch or move up",
    ),
    "app.tree.unfoldOrDown": KeybindingDefinition(
        ["alt+right", "ctrl+right"] if sys.platform == "darwin" else ["ctrl+right", "alt+right"],
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

_KEYBINDING_NAME_MIGRATIONS = {
    "cursorUp": "tui.editor.cursorUp",
    "cursorDown": "tui.editor.cursorDown",
    "cursorLeft": "tui.editor.cursorLeft",
    "cursorRight": "tui.editor.cursorRight",
    "cursorWordLeft": "tui.editor.cursorWordLeft",
    "cursorWordRight": "tui.editor.cursorWordRight",
    "cursorLineStart": "tui.editor.cursorLineStart",
    "cursorLineEnd": "tui.editor.cursorLineEnd",
    "jumpForward": "tui.editor.jumpForward",
    "jumpBackward": "tui.editor.jumpBackward",
    "pageUp": "tui.editor.pageUp",
    "pageDown": "tui.editor.pageDown",
    "deleteCharBackward": "tui.editor.deleteCharBackward",
    "deleteCharForward": "tui.editor.deleteCharForward",
    "deleteWordBackward": "tui.editor.deleteWordBackward",
    "deleteWordForward": "tui.editor.deleteWordForward",
    "deleteToLineStart": "tui.editor.deleteToLineStart",
    "deleteToLineEnd": "tui.editor.deleteToLineEnd",
    "yank": "tui.editor.yank",
    "yankPop": "tui.editor.yankPop",
    "undo": "tui.editor.undo",
    "newLine": "tui.input.newLine",
    "submit": "tui.input.submit",
    "tab": "tui.input.tab",
    "copy": "tui.input.copy",
    "selectUp": "tui.select.up",
    "selectDown": "tui.select.down",
    "selectPageUp": "tui.select.pageUp",
    "selectPageDown": "tui.select.pageDown",
    "selectConfirm": "tui.select.confirm",
    "selectCancel": "tui.select.cancel",
    "interrupt": "app.interrupt",
    "clear": "app.clear",
    "exit": "app.exit",
    "suspend": "app.suspend",
    "cycleThinkingLevel": "app.thinking.cycle",
    "cycleModelForward": "app.model.cycleForward",
    "cycleModelBackward": "app.model.cycleBackward",
    "selectModel": "app.model.select",
    "expandTools": "app.tools.expand",
    "toggleThinking": "app.thinking.toggle",
    "toggleSessionNamedFilter": "app.session.toggleNamedFilter",
    "externalEditor": "app.editor.external",
    "followUp": "app.message.followUp",
    "dequeue": "app.message.dequeue",
    "pasteImage": "app.clipboard.pasteImage",
    "newSession": "app.session.new",
    "tree": "app.session.tree",
    "fork": "app.session.fork",
    "resume": "app.session.resume",
    "treeFoldOrUp": "app.tree.foldOrUp",
    "treeUnfoldOrDown": "app.tree.unfoldOrDown",
    "treeEditLabel": "app.tree.editLabel",
    "treeToggleLabelTimestamp": "app.tree.toggleLabelTimestamp",
    "toggleSessionPath": "app.session.togglePath",
    "toggleSessionSort": "app.session.toggleSort",
    "renameSession": "app.session.rename",
    "deleteSession": "app.session.delete",
    "deleteSessionNoninvasive": "app.session.deleteNoninvasive",
}


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


def _order_keybindings_config(config: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for keybinding in KEYBINDINGS:
        if keybinding in config:
            ordered[keybinding] = config[keybinding]

    extras = sorted(key for key in config if key not in ordered)
    for key in extras:
        ordered[key] = config[key]
    return ordered


def migrateKeybindingsConfig(raw_config: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    migrated = False

    for key, value in raw_config.items():
        next_key = _KEYBINDING_NAME_MIGRATIONS.get(key, key)
        if next_key != key:
            migrated = True
        if key != next_key and next_key in raw_config:
            migrated = True
            continue
        config[next_key] = value

    return {"config": _order_keybindings_config(config), "migrated": migrated}


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
        migrated = migrateKeybindingsConfig(raw_config)["config"]
        return _to_keybindings_config(migrated)

__all__ = [
    "AppKeybinding",
    "AppKeybindings",
    "KEYBINDINGS",
    "Keybinding",
    "KeyId",
    "KeybindingsConfig",
    "KeybindingsManager",
    "migrateKeybindingsConfig",
]
