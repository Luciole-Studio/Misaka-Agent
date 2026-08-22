"""Global keybinding registry for the TUI package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from misaka.tui.keys import KeyId, matchesKey

type Keybinding = str
type KeybindingsConfig = dict[str, KeyId | list[KeyId] | None]


@dataclass(slots=True)
class KeybindingDefinition:
    defaultKeys: KeyId | list[KeyId]
    description: str | None = None


@dataclass(slots=True)
class KeybindingConflict:
    key: KeyId
    keybindings: list[str]


TUI_KEYBINDINGS: dict[str, KeybindingDefinition] = {
    "tui.editor.cursorUp": KeybindingDefinition("up", "Move cursor up"),
    "tui.editor.cursorDown": KeybindingDefinition("down", "Move cursor down"),
    "tui.editor.cursorLeft": KeybindingDefinition(["left", "ctrl+b"], "Move cursor left"),
    "tui.editor.cursorRight": KeybindingDefinition(["right", "ctrl+f"], "Move cursor right"),
    "tui.editor.cursorWordLeft": KeybindingDefinition(
        ["alt+left", "ctrl+left", "alt+b"], "Move cursor word left"
    ),
    "tui.editor.cursorWordRight": KeybindingDefinition(
        ["alt+right", "ctrl+right", "alt+f"], "Move cursor word right"
    ),
    "tui.editor.cursorLineStart": KeybindingDefinition(["home", "ctrl+home", "ctrl+a"], "Move to line start"),
    "tui.editor.cursorLineEnd": KeybindingDefinition(["end", "ctrl+end", "ctrl+e"], "Move to line end"),
    "tui.editor.jumpForward": KeybindingDefinition("ctrl+]", "Jump forward to character"),
    "tui.editor.jumpBackward": KeybindingDefinition("ctrl+alt+]", "Jump backward to character"),
    "tui.editor.pageUp": KeybindingDefinition(["pageUp", "ctrl+pageUp"], "Page up"),
    "tui.editor.pageDown": KeybindingDefinition(["pageDown", "ctrl+pageDown"], "Page down"),
    # Dedicated history actions: unbound by default; users bind them in keybindings.json (e.g. ctrl+p/ctrl+n). (pi 16ad96a)
    "tui.editor.historyPrevious": KeybindingDefinition([], "Select the previous prompt history entry"),
    "tui.editor.historyNext": KeybindingDefinition([], "Select the next prompt history entry"),
    "tui.editor.deleteCharBackward": KeybindingDefinition("backspace", "Delete character backward"),
    "tui.editor.deleteCharForward": KeybindingDefinition(["delete", "ctrl+d"], "Delete character forward"),
    "tui.editor.deleteWordBackward": KeybindingDefinition(
        ["ctrl+w", "alt+backspace"], "Delete word backward"
    ),
    "tui.editor.deleteWordForward": KeybindingDefinition(["alt+d", "alt+delete"], "Delete word forward"),
    "tui.editor.deleteToLineStart": KeybindingDefinition("ctrl+u", "Delete to line start"),
    "tui.editor.deleteToLineEnd": KeybindingDefinition("ctrl+k", "Delete to line end"),
    "tui.editor.yank": KeybindingDefinition("ctrl+y", "Yank"),
    "tui.editor.yankPop": KeybindingDefinition("alt+y", "Yank pop"),
    "tui.editor.undo": KeybindingDefinition("ctrl+-", "Undo"),
    "tui.input.newLine": KeybindingDefinition(["shift+enter", "ctrl+j"], "Insert newline"),
    "tui.input.submit": KeybindingDefinition("enter", "Submit input"),
    "tui.input.tab": KeybindingDefinition("tab", "Tab / autocomplete"),
    "tui.input.copy": KeybindingDefinition("ctrl+c", "Copy selection"),
    "tui.select.up": KeybindingDefinition("up", "Move selection up"),
    "tui.select.down": KeybindingDefinition("down", "Move selection down"),
    "tui.select.pageUp": KeybindingDefinition("pageUp", "Selection page up"),
    "tui.select.pageDown": KeybindingDefinition("pageDown", "Selection page down"),
    "tui.select.confirm": KeybindingDefinition("enter", "Confirm selection"),
    "tui.select.cancel": KeybindingDefinition(["escape", "ctrl+c"], "Cancel selection"),
}


def _normalize_keys(keys: KeyId | list[KeyId] | None) -> list[KeyId]:
    if keys is None:
        return []
    key_list = keys if isinstance(keys, list) else [keys]
    seen: set[KeyId] = set()
    result: list[KeyId] = []
    for key in key_list:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


class KeybindingsManager:
    def __init__(
        self,
        definitions: dict[str, KeybindingDefinition],
        userBindings: KeybindingsConfig | None = None,
    ) -> None:
        self.definitions = definitions
        self.userBindings = userBindings or {}
        self.keysById: dict[Keybinding, list[KeyId]] = {}
        self.conflicts: list[KeybindingConflict] = []
        self.rebuild()

    def rebuild(self) -> None:
        self.keysById.clear()
        self.conflicts = []

        user_claims: dict[KeyId, list[Keybinding]] = {}
        for keybinding, keys in self.userBindings.items():
            if keybinding not in self.definitions:
                continue
            for key in _normalize_keys(keys):
                claimants = user_claims.setdefault(key, [])
                if keybinding not in claimants:
                    claimants.append(keybinding)

        for key, keybindings in user_claims.items():
            if len(keybindings) > 1:
                self.conflicts.append(KeybindingConflict(key=key, keybindings=list(keybindings)))

        for keybinding, definition in self.definitions.items():
            user_keys = self.userBindings.get(keybinding)
            keys = (
                _normalize_keys(definition.defaultKeys)
                if user_keys is None
                else _normalize_keys(user_keys)
            )
            self.keysById[keybinding] = keys

    def matches(self, data: str, keybinding: Keybinding) -> bool:
        for key in self.keysById.get(keybinding, []):
            if matchesKey(data, key):
                return True
        return False

    def getKeys(self, keybinding: Keybinding) -> list[KeyId]:
        return list(self.keysById.get(keybinding, []))

    def getDefinition(self, keybinding: Keybinding) -> KeybindingDefinition:
        return self.definitions[keybinding]

    def getConflicts(self) -> list[KeybindingConflict]:
        return [
            KeybindingConflict(key=conflict.key, keybindings=list(conflict.keybindings))
            for conflict in self.conflicts
        ]

    def setUserBindings(self, userBindings: KeybindingsConfig) -> None:
        self.userBindings = userBindings
        self.rebuild()

    def getUserBindings(self) -> KeybindingsConfig:
        return dict(self.userBindings)

    def getResolvedBindings(self) -> KeybindingsConfig:
        resolved: KeybindingsConfig = {}
        for keybinding in self.definitions:
            keys = self.keysById.get(keybinding, [])
            resolved[keybinding] = keys[0] if len(keys) == 1 else list(keys)
        return resolved


_global_keybindings: KeybindingsManager | None = None


def setKeybindings(keybindings: KeybindingsManager) -> None:
    global _global_keybindings
    _global_keybindings = keybindings


def getKeybindings() -> KeybindingsManager:
    global _global_keybindings
    if _global_keybindings is None:
        _global_keybindings = KeybindingsManager(TUI_KEYBINDINGS)
    return _global_keybindings


Keybindings = dict[str, Any]
KeybindingDefinitions = dict[str, KeybindingDefinition]

__all__ = [
    "Keybinding",
    "KeybindingConflict",
    "KeybindingDefinition",
    "KeybindingDefinitions",
    "Keybindings",
    "KeybindingsConfig",
    "KeybindingsManager",
    "TUI_KEYBINDINGS",
    "getKeybindings",
    "setKeybindings",
]
