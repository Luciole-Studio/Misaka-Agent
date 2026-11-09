"""Translated from pi coding-agent's ``src/core/keybindings.ts`` (v1.3.0).

MISAKA forks:

- D1: the ``tui.altScreen.*`` overrides serve the fullscreen renderer, which is not ported.
- No legacy keybinding-name migration (``KEYBINDING_NAME_MIGRATIONS``, ``migrateKeybindingsConfig``,
  ``orderKeybindingsConfig``): MISAKA keeps no backward-compatibility layer for old configuration names.
- TypeBox's ``Compile`` is a jsonschema Draft 2020-12 validator (as in ``misaka/ai/utils/validation.py``) whose
  ``pattern`` is read as an ECMAScript regular expression, as TypeBox reads it (``validateKeybindingValue``).
"""

from __future__ import annotations

import dataclasses
import os
import sys
from collections.abc import Iterator, Mapping
from typing import Any, Literal

from jsonschema import Draft202012Validator, ValidationError, validators

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
from misaka.utils.js import jsonParse
from misaka.utils.js_regexp import RegExp

from .key_id_schema import KeybindingValueSchema


def useWindowsKeybindings(platform: str = sys.platform, env: Mapping[str, str] = os.environ) -> bool:
    return platform == "win32" or (platform == "linux" and bool(env.get("WSL_DISTRO_NAME") or env.get("WSL_INTEROP")))


windowsKeybindings = useWindowsKeybindings()

APP_KEYBINDINGS: KeybindingDefinitions = {
    "app.interrupt": KeybindingDefinition(defaultKeys="escape", description="Cancel or abort"),
    "app.clear": KeybindingDefinition(defaultKeys="ctrl+c", description="Clear editor"),
    "app.exit": KeybindingDefinition(defaultKeys="ctrl+d", description="Exit when editor is empty"),
    "app.suspend": KeybindingDefinition(
        defaultKeys=[] if sys.platform == "win32" else "ctrl+z",
        description="Suspend to background",
    ),
    "app.thinking.cycle": KeybindingDefinition(
        defaultKeys="shift+tab",
        description="Cycle thinking level",
    ),
    "app.thinking.save": KeybindingDefinition(
        defaultKeys="ctrl+s",
        description="Save thinking level",
    ),
    "app.model.cycleForward": KeybindingDefinition(
        defaultKeys="ctrl+p",
        description="Cycle to next model",
    ),
    "app.model.cycleBackward": KeybindingDefinition(
        defaultKeys="alt+p" if windowsKeybindings else "shift+ctrl+p",
        description="Cycle to previous model",
    ),
    "app.model.select": KeybindingDefinition(defaultKeys="ctrl+l", description="Open model selector"),
    "app.tools.expand": KeybindingDefinition(defaultKeys="ctrl+o", description="Toggle tool output"),
    "app.thinking.toggle": KeybindingDefinition(
        defaultKeys="ctrl+t",
        description="Toggle thinking blocks",
    ),
    "app.session.toggleNamedFilter": KeybindingDefinition(
        defaultKeys="ctrl+n",
        description="Toggle named session filter",
    ),
    "app.editor.external": KeybindingDefinition(
        defaultKeys="ctrl+g",
        description="Open external editor",
    ),
    "app.message.copy": KeybindingDefinition(
        defaultKeys="ctrl+x",
        description="Copy selection or last assistant message",
    ),
    "app.message.followUp": KeybindingDefinition(
        defaultKeys="ctrl+q" if windowsKeybindings else "alt+enter",
        description="Queue follow-up message",
    ),
    "app.message.dequeue": KeybindingDefinition(
        defaultKeys="alt+q" if windowsKeybindings else "alt+up",
        description="Restore queued messages",
    ),
    "app.clipboard.pasteImage": KeybindingDefinition(
        defaultKeys="alt+v" if windowsKeybindings else "ctrl+v",
        # MISAKA fork: Windows copies of files in Explorer paste as paths too (pi: macOS only).
        description="Paste files on macOS and Windows, images, or text from clipboard",
    ),
    "app.session.new": KeybindingDefinition(defaultKeys=[], description="Start a new session"),
    "app.session.tree": KeybindingDefinition(defaultKeys=[], description="Open session tree"),
    "app.session.fork": KeybindingDefinition(defaultKeys=[], description="Fork current session"),
    "app.session.resume": KeybindingDefinition(defaultKeys=[], description="Resume a session"),
    "app.tree.foldOrUp": KeybindingDefinition(
        defaultKeys=["alt+left", "ctrl+left"] if sys.platform == "darwin" else ["ctrl+left", "alt+left"],
        description="Fold tree branch or move up",
    ),
    "app.tree.unfoldOrDown": KeybindingDefinition(
        defaultKeys=["alt+right", "ctrl+right"] if sys.platform == "darwin" else ["ctrl+right", "alt+right"],
        description="Unfold tree branch or move down",
    ),
    "app.tree.editLabel": KeybindingDefinition(
        defaultKeys="shift+l",
        description="Edit tree label",
    ),
    "app.tree.toggleLabelTimestamp": KeybindingDefinition(
        defaultKeys="shift+t",
        description="Toggle tree label timestamps",
    ),
    "app.session.togglePath": KeybindingDefinition(
        defaultKeys="ctrl+p",
        description="Toggle session path display",
    ),
    "app.session.toggleSort": KeybindingDefinition(
        defaultKeys="ctrl+s",
        description="Toggle session sort mode",
    ),
    "app.session.rename": KeybindingDefinition(
        defaultKeys="ctrl+r",
        description="Rename session",
    ),
    "app.session.delete": KeybindingDefinition(
        defaultKeys="ctrl+d",
        description="Delete session",
    ),
    "app.session.deleteNoninvasive": KeybindingDefinition(
        defaultKeys="ctrl+backspace",
        description="Delete session when query is empty",
    ),
    "app.models.save": KeybindingDefinition(
        defaultKeys="ctrl+s",
        description="Save model selection",
    ),
    "app.models.enableAll": KeybindingDefinition(
        defaultKeys="ctrl+a",
        description="Enable all models",
    ),
    "app.models.clearAll": KeybindingDefinition(
        defaultKeys="ctrl+x",
        description="Clear all models",
    ),
    "app.models.toggleProvider": KeybindingDefinition(
        defaultKeys="ctrl+p",
        description="Toggle all models for provider",
    ),
    "app.models.reorderUp": KeybindingDefinition(
        defaultKeys="alt+up",
        description="Move model up in order",
    ),
    "app.models.reorderDown": KeybindingDefinition(
        defaultKeys="alt+down",
        description="Move model down in order",
    ),
    "app.tree.filter.default": KeybindingDefinition(
        defaultKeys="ctrl+d",
        description="Tree filter: default view",
    ),
    "app.tree.filter.noTools": KeybindingDefinition(
        defaultKeys="ctrl+t",
        description="Tree filter: hide tool results",
    ),
    "app.tree.filter.userOnly": KeybindingDefinition(
        defaultKeys="ctrl+u",
        description="Tree filter: user messages only",
    ),
    "app.tree.filter.labeledOnly": KeybindingDefinition(
        defaultKeys="ctrl+l",
        description="Tree filter: labeled entries only",
    ),
    "app.tree.filter.all": KeybindingDefinition(
        defaultKeys="ctrl+a",
        description="Tree filter: show all entries",
    ),
    "app.tree.filter.cycleForward": KeybindingDefinition(
        defaultKeys="ctrl+o",
        description="Tree filter: cycle forward",
    ),
    "app.tree.filter.cycleBackward": KeybindingDefinition(
        defaultKeys="shift+ctrl+o",
        description="Tree filter: cycle backward",
    ),
}

# MISAKA fork: `keyof typeof APP_KEYBINDINGS` spelled out as the literal union of the table's names
# (tests/test_core_keybindings.py keeps the two equal).
type AppKeybinding = Literal[
    "app.interrupt",
    "app.clear",
    "app.exit",
    "app.suspend",
    "app.thinking.cycle",
    "app.thinking.save",
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
    "tui.editor.undo": dataclasses.replace(
        TUI_KEYBINDINGS["tui.editor.undo"],
        defaultKeys="ctrl+z" if sys.platform == "win32" else "alt+z" if windowsKeybindings else "ctrl+-",
    ),
    # MISAKA fork (D1): upstream overrides `tui.altScreen.previousPrompt`, `nextPrompt` and `search` here.
    **APP_KEYBINDINGS,
}


def _ecmaPattern(validator: Any, pattern: str, instance: Any, _schema: Any) -> Iterator[ValidationError]:
    """MISAKA fork: ``pattern`` as TypeBox checks it, an ECMAScript regular expression (Python's ``$`` would also
    accept a trailing newline)."""
    if validator.is_type(instance, "string") and not RegExp(pattern, "u").test(instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


validateKeybindingValue = validators.extend(Draft202012Validator, {"pattern": _ecmaPattern})(KeybindingValueSchema)


def toKeybindingsConfig(value: dict[str, Any]) -> KeybindingsConfig:
    config: KeybindingsConfig = {}
    for key, binding in value.items():
        if key == "$schema":
            continue
        if validateKeybindingValue.is_valid(binding):
            config[key] = binding
    return config


def loadRawConfig(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        # `readFileSync(path, "utf-8")` decodes invalid bytes as U+FFFD; `utf-8-sig` is `stripBom`.
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            parsed = jsonParse(handle.read())
        # MISAKA fork: a JSON array is no object here (upstream reads its indices as keybinding names, which name
        # no keybinding).
        if not isinstance(parsed, dict):
            return None
        return parsed
    except (OSError, ValueError, RecursionError):
        return None


class KeybindingsManager(TuiKeybindingsManager):
    def __init__(self, userBindings: KeybindingsConfig | None = None, configPath: str | None = None) -> None:
        super().__init__(KEYBINDINGS, userBindings if userBindings is not None else {})
        self.configPath = configPath

    @staticmethod
    def create(agentDir: str | None = None) -> KeybindingsManager:
        if agentDir is None:
            agentDir = get_agent_dir()
        configPath = os.path.join(agentDir, "keybindings.json")
        userBindings = KeybindingsManager.loadFromFile(configPath)
        return KeybindingsManager(userBindings, configPath)

    def reload(self) -> None:
        if not self.configPath:
            return
        self.setUserBindings(KeybindingsManager.loadFromFile(self.configPath))

    def getEffectiveConfig(self) -> KeybindingsConfig:
        return self.getResolvedBindings()

    @staticmethod
    def loadFromFile(path: str) -> KeybindingsConfig:
        rawConfig = loadRawConfig(path)
        if not rawConfig:
            return {}
        return toKeybindingsConfig(rawConfig)


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
