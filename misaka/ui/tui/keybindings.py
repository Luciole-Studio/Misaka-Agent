"""Translated from pi-tui's ``packages/tui/src/keybindings.ts`` (v1.1.0).

MISAKA fork (D1): the ``tui.altScreen.*`` keybindings serve the fullscreen renderer, which is not ported.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from misaka.ui.tui.keys import KeyId, matchesKey

# Global keybinding registry.
# Downstream packages can add keybindings via declaration merging.
# MISAKA fork: Python has no declaration merging; the registry lists pi-tui's own keybindings, and a `Keybinding`
# is any string so downstream ids (`app.*`) type-check.
Keybindings = TypedDict(
    "Keybindings",
    {
        # Editor navigation and editing
        "tui.editor.cursorUp": Literal[True],
        "tui.editor.cursorDown": Literal[True],
        "tui.editor.historyPrevious": Literal[True],
        "tui.editor.historyNext": Literal[True],
        "tui.editor.cursorLeft": Literal[True],
        "tui.editor.cursorRight": Literal[True],
        "tui.editor.cursorWordLeft": Literal[True],
        "tui.editor.cursorWordRight": Literal[True],
        "tui.editor.cursorLineStart": Literal[True],
        "tui.editor.cursorLineEnd": Literal[True],
        "tui.editor.jumpForward": Literal[True],
        "tui.editor.jumpBackward": Literal[True],
        "tui.editor.pageUp": Literal[True],
        "tui.editor.pageDown": Literal[True],
        "tui.editor.deleteCharBackward": Literal[True],
        "tui.editor.deleteCharForward": Literal[True],
        "tui.editor.deleteWordBackward": Literal[True],
        "tui.editor.deleteWordForward": Literal[True],
        "tui.editor.deleteToLineStart": Literal[True],
        "tui.editor.deleteToLineEnd": Literal[True],
        "tui.editor.yank": Literal[True],
        "tui.editor.yankPop": Literal[True],
        "tui.editor.undo": Literal[True],
        # Generic input actions
        "tui.input.newLine": Literal[True],
        "tui.input.submit": Literal[True],
        "tui.input.tab": Literal[True],
        "tui.input.copy": Literal[True],
        # Generic selection actions
        "tui.select.up": Literal[True],
        "tui.select.down": Literal[True],
        "tui.select.pageUp": Literal[True],
        "tui.select.pageDown": Literal[True],
        "tui.select.confirm": Literal[True],
        "tui.select.cancel": Literal[True],
        # Alternate-screen viewport navigation: MISAKA fork (D1), not ported.
    },
)

type Keybinding = str


@dataclass(slots=True, kw_only=True)
class KeybindingDefinition:
    defaultKeys: KeyId | list[KeyId]
    description: str | None = None


type KeybindingDefinitions = dict[str, KeybindingDefinition]
type KeybindingsConfig = dict[str, KeyId | list[KeyId] | None]

TUI_KEYBINDINGS: KeybindingDefinitions = {
    "tui.editor.cursorUp": KeybindingDefinition(defaultKeys="up", description="Move cursor up"),
    "tui.editor.cursorDown": KeybindingDefinition(defaultKeys="down", description="Move cursor down"),
    "tui.editor.historyPrevious": KeybindingDefinition(
        defaultKeys=[],
        description="Select previous prompt history entry",
    ),
    "tui.editor.historyNext": KeybindingDefinition(
        defaultKeys=[],
        description="Select next prompt history entry",
    ),
    "tui.editor.cursorLeft": KeybindingDefinition(
        defaultKeys=["left", "ctrl+b"],
        description="Move cursor left",
    ),
    "tui.editor.cursorRight": KeybindingDefinition(
        defaultKeys=["right", "ctrl+f"],
        description="Move cursor right",
    ),
    "tui.editor.cursorWordLeft": KeybindingDefinition(
        defaultKeys=["alt+left", "ctrl+left", "alt+b"],
        description="Move cursor word left",
    ),
    "tui.editor.cursorWordRight": KeybindingDefinition(
        defaultKeys=["alt+right", "ctrl+right", "alt+f"],
        description="Move cursor word right",
    ),
    "tui.editor.cursorLineStart": KeybindingDefinition(
        defaultKeys=["home", "ctrl+a"],
        description="Move to line start",
    ),
    "tui.editor.cursorLineEnd": KeybindingDefinition(
        defaultKeys=["end", "ctrl+e"],
        description="Move to line end",
    ),
    "tui.editor.jumpForward": KeybindingDefinition(
        defaultKeys="ctrl+]",
        description="Jump forward to character",
    ),
    "tui.editor.jumpBackward": KeybindingDefinition(
        defaultKeys="ctrl+alt+]",
        description="Jump backward to character",
    ),
    "tui.editor.pageUp": KeybindingDefinition(defaultKeys=["pageUp", "ctrl+pageUp"], description="Page up"),
    "tui.editor.pageDown": KeybindingDefinition(defaultKeys=["pageDown", "ctrl+pageDown"], description="Page down"),
    "tui.editor.deleteCharBackward": KeybindingDefinition(
        defaultKeys="backspace",
        description="Delete character backward",
    ),
    "tui.editor.deleteCharForward": KeybindingDefinition(
        defaultKeys=["delete", "ctrl+d"],
        description="Delete character forward",
    ),
    "tui.editor.deleteWordBackward": KeybindingDefinition(
        defaultKeys=["ctrl+w", "alt+backspace"],
        description="Delete word backward",
    ),
    "tui.editor.deleteWordForward": KeybindingDefinition(
        defaultKeys=["alt+d", "alt+delete"],
        description="Delete word forward",
    ),
    "tui.editor.deleteToLineStart": KeybindingDefinition(
        defaultKeys="ctrl+u",
        description="Delete to line start",
    ),
    "tui.editor.deleteToLineEnd": KeybindingDefinition(
        defaultKeys="ctrl+k",
        description="Delete to line end",
    ),
    "tui.editor.yank": KeybindingDefinition(defaultKeys="ctrl+y", description="Yank"),
    "tui.editor.yankPop": KeybindingDefinition(defaultKeys="alt+y", description="Yank pop"),
    "tui.editor.undo": KeybindingDefinition(defaultKeys="ctrl+-", description="Undo"),
    "tui.input.newLine": KeybindingDefinition(defaultKeys=["shift+enter", "ctrl+j"], description="Insert newline"),
    "tui.input.submit": KeybindingDefinition(defaultKeys="enter", description="Submit input"),
    "tui.input.tab": KeybindingDefinition(defaultKeys="tab", description="Tab / autocomplete"),
    "tui.input.copy": KeybindingDefinition(defaultKeys="ctrl+c", description="Copy selection"),
    "tui.select.up": KeybindingDefinition(defaultKeys="up", description="Move selection up"),
    "tui.select.down": KeybindingDefinition(defaultKeys="down", description="Move selection down"),
    "tui.select.pageUp": KeybindingDefinition(defaultKeys="pageUp", description="Selection page up"),
    "tui.select.pageDown": KeybindingDefinition(
        defaultKeys="pageDown",
        description="Selection page down",
    ),
    "tui.select.confirm": KeybindingDefinition(defaultKeys="enter", description="Confirm selection"),
    "tui.select.cancel": KeybindingDefinition(
        defaultKeys=["escape", "ctrl+c"],
        description="Cancel selection",
    ),
    # MISAKA fork (D1): upstream's `tui.altScreen.*` entries (fullscreen viewport navigation and transcript search)
    # follow here.
}


@dataclass(slots=True, kw_only=True)
class KeybindingConflict:
    key: KeyId
    keybindings: list[str]


def normalizeKeys(keys: KeyId | list[KeyId] | None) -> list[KeyId]:
    if keys is None:
        return []
    keyList = keys if isinstance(keys, list) else [keys]
    seen: set[KeyId] = set()
    result: list[KeyId] = []
    for key in keyList:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


class KeybindingsManager:
    def __init__(self, definitions: KeybindingDefinitions, userBindings: KeybindingsConfig | None = None) -> None:
        self.keysById: dict[Keybinding, list[KeyId]] = {}
        self.conflicts: list[KeybindingConflict] = []
        self.definitions = definitions
        self.userBindings = userBindings if userBindings is not None else {}
        self.rebuild()

    def rebuild(self) -> None:
        self.keysById.clear()
        self.conflicts = []

        userClaims: dict[KeyId, dict[Keybinding, None]] = {}
        for keybinding, keys in self.userBindings.items():
            if keybinding not in self.definitions:
                continue
            for key in normalizeKeys(keys):
                claimants = userClaims.get(key, {})
                claimants[keybinding] = None
                userClaims[key] = claimants

        for key, keybindings in userClaims.items():
            if len(keybindings) > 1:
                self.conflicts.append(KeybindingConflict(key=key, keybindings=[*keybindings]))

        for id, definition in self.definitions.items():
            userKeys = self.userBindings.get(id)
            keys = normalizeKeys(definition.defaultKeys) if userKeys is None else normalizeKeys(userKeys)
            self.keysById[id] = keys

    def matches(self, data: str, keybinding: Keybinding) -> bool:
        keys = self.keysById.get(keybinding, [])
        for key in keys:
            if matchesKey(data, key):
                return True
        return False

    def getKeys(self, keybinding: Keybinding) -> list[KeyId]:
        return [*self.keysById.get(keybinding, [])]

    def getDefinition(self, keybinding: Keybinding) -> KeybindingDefinition | None:
        return self.definitions.get(keybinding)

    def getConflicts(self) -> list[KeybindingConflict]:
        return [KeybindingConflict(key=conflict.key, keybindings=[*conflict.keybindings]) for conflict in self.conflicts]

    def setUserBindings(self, userBindings: KeybindingsConfig) -> None:
        self.userBindings = userBindings
        self.rebuild()

    def getUserBindings(self) -> KeybindingsConfig:
        return {**self.userBindings}

    def getResolvedBindings(self) -> KeybindingsConfig:
        resolved: KeybindingsConfig = {}
        for id in self.definitions:
            keys = self.keysById.get(id, [])
            resolved[id] = keys[0] if len(keys) == 1 else [*keys]
        return resolved


globalKeybindings: KeybindingsManager | None = None


def setKeybindings(keybindings: KeybindingsManager) -> None:
    global globalKeybindings
    globalKeybindings = keybindings


def getKeybindings() -> KeybindingsManager:
    global globalKeybindings
    if not globalKeybindings:
        globalKeybindings = KeybindingsManager(TUI_KEYBINDINGS)
    return globalKeybindings


__all__ = [
    "TUI_KEYBINDINGS",
    "Keybinding",
    "KeybindingConflict",
    "KeybindingDefinition",
    "KeybindingDefinitions",
    "Keybindings",
    "KeybindingsConfig",
    "KeybindingsManager",
    "getKeybindings",
    "setKeybindings",
]
